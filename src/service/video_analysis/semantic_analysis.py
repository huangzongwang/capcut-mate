"""Video-file based analysis. Local decoding only determines clip boundaries."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from functools import partial
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import time
from pathlib import Path

from src.schemas.video_analysis import (
    AnalyzeVideoRequest, AnalyzeVideoResponse, DetectScenesResponse,
    SceneSemanticAnalysis, SceneTranscript, TranscriptSegment, TransitionAnalysis,
    TranscribeVideoResponse, TranscribeVideoRequest, ViralAnalysis,
)
from src.service.video_analysis.scene_detection import build_worker_input, transcribe_video, _run_worker
from src.service.video_analysis.model_config_store import resolve_model, snapshot
from src.service.video_analysis.model_client import (
    analyze_scene_with_responses, analyze_global_report_with_responses,
)
from src.service.video_analysis.responses_client import Scope
from src.service.video_analysis.gemini_client import uses_gemini
from src.service.video_analysis.video_files import VideoFiles
from src.service.video_analysis.transcription import to_srt
from src.service.video_analysis.platform_runtime import analysis_temporary_directory


_REPLACEABLE_SCENE_CATEGORIES = {
    "product_empty_scene", "pain_point", "motion",
}


_PRODUCT_VISUAL_INTENTS = {
    "product_overview", "product_detail", "instruction_manual", "otc_certification",
    "qr_traceability", "authenticity_comparison", "usage_instruction",
}
_PAIN_VISUAL_INTENTS = {"pain_point", "body_condition"}


def _apply_replacement_rules(result: SceneSemanticAnalysis) -> SceneSemanticAnalysis:
    """Require visible evidence before a category can authorize replacement."""
    reported_category = result.scene_category
    intent = result.visual_intent
    subject = result.primary_visual_subject

    if result.on_camera_speech == "confirmed":
        category = "oral_delivery"
    elif intent in _PRODUCT_VISUAL_INTENTS and subject == "product":
        category = "product_empty_scene"
    elif intent in _PAIN_VISUAL_INTENTS and subject in {"body", "person"}:
        category = "pain_point"
    elif intent == "exercise_action" and subject in {"action", "person", "body"}:
        category = "motion"
    else:
        category = "unknown"

    result.scene_category = category
    if category == "unknown":
        if reported_category == "product_empty_scene" and subject != "product":
            reason = (
                "原分类为产品空境，但主视觉主体不是产品；角落包装、OTC底栏/贴字、"
                "说明文字或产品缩略图不能证明产品是画面主体"
            )
        elif reported_category == "oral_delivery" and result.on_camera_speech != "confirmed":
            reason = "未确认画面人物与当前音轨口型同步，不能仅凭字幕、转写或画外音判为口播"
        elif reported_category == "intro":
            reason = "片头/intro是叙事位置，不是视觉类别；缺少明确主体与画面意图，需人工复核"
        else:
            reason = (
                f"画面证据不足以支持原分类 {reported_category}："
                f"visual_intent={intent}, primary_visual_subject={subject}, "
                f"on_camera_speech={result.on_camera_speech}"
            )
        result.replaceable = False
        result.replacement_type = "需人工复核"
        result.replace_score = min(result.replace_score, 0.5)
        result.replace_reason = reason
        return result

    if reported_category != category:
        correction_reason = (
            f"原分类 {reported_category} 与视觉证据不一致，已按 visual_intent={intent}、"
            f"primary_visual_subject={subject}、on_camera_speech={result.on_camera_speech} "
            f"修正为 {category}"
        )
        if result.replace_reason:
            result.replace_reason = f"{correction_reason}；模型约束：{result.replace_reason}"
        else:
            result.replace_reason = correction_reason

    if category in _REPLACEABLE_SCENE_CATEGORIES:
        if result.replaceable:
            result.replacement_type = "可替换"
        else:
            result.replacement_type = "需人工复核"
            result.replace_score = min(result.replace_score, 0.5)
            result.replace_reason = result.replace_reason or (
                "主体类别有画面证据，但模型判断替换约束不足，保留原画面并人工复核"
            )
        if result.replaceable and not result.replace_reason:
            result.replace_reason = f"{category} 的画面主体和具体意图均有证据支持，可评估替换"
    elif category == "oral_delivery":
        result.replaceable = False
        result.replacement_type = "不可替换"
        result.replace_score = min(result.replace_score, 0.2)
        result.replace_reason = result.replace_reason or "已确认画内人物与当前音轨同步，需保留原人物、口型和台词"
    else:
        result.replaceable = False
        result.replacement_type = "需人工复核"
        result.replace_score = min(result.replace_score, 0.5)
        result.replace_reason = result.replace_reason or (
            "场景属于混合或无法确认类型，不能自动替换"
        )
    return result


def _scene_caption_items(scene, transcript):
    """Assign word-timestamped transcript data to one scene without duplication.

    Scene intervals are half-open: [start_us, end_us). A word belongs to the
    scene containing its midpoint, so a cross-boundary word can never appear
    in both adjacent scenes.
    """
    if not transcript:
        return []
    scene_start = int(scene.start_us)
    scene_end = int(scene.end_us)
    items = []
    for segment in transcript.segments:
        raw_start = round(float(segment.start) * 1_000_000)
        raw_end = round(float(segment.end) * 1_000_000)
        segment_start = max(scene_start, raw_start)
        segment_end = min(scene_end, raw_end)
        if segment_end <= segment_start or not segment.text.strip():
            continue
        raw_words = getattr(segment, "words", None) or []
        words = []
        for word_index, word in enumerate(raw_words):
            word_raw_start = round(float(word.start) * 1_000_000)
            word_raw_end = round(float(word.end) * 1_000_000)
            if word_raw_end <= word_raw_start:
                continue
            midpoint = (word_raw_start + word_raw_end) / 2
            if scene_start <= midpoint < scene_end and word.word.strip():
                word_start = max(scene_start, word_raw_start)
                word_end = min(scene_end, word_raw_end)
                words.append({
                    "start_us": word_start,
                    "end_us": word_end,
                    "text": word.word.strip(),
                    "probability": getattr(word, "probability", None),
                    "word_id": f"{segment.id}:{word_index}",
                    "ownership_scene_index": int(getattr(scene, "index", 0) or 0),
                })

        # With word timestamps, an empty selection means that this scene owns
        # none of the segment's words. Do not fall back to the full segment,
        # otherwise the sentence would leak across a scene boundary.
        if words:
            caption_text = "".join(item["text"] for item in words).strip()
            item_start = words[0]["start_us"]
            item_end = words[-1]["end_us"]
        elif not raw_words:
            # Older/failed transcription backends may not provide words.
            # Assign the complete segment to one scene by midpoint.
            midpoint = (raw_start + raw_end) / 2
            if not (scene_start <= midpoint < scene_end):
                continue
            caption_text = segment.text.strip()
            item_start = segment_start
            item_end = segment_end
        else:
            continue
        if not caption_text:
            continue
        items.append({
            "start_us": item_start,
            "end_us": item_end,
            "text": caption_text,
            "segment_id": segment.id,
            "words": words,
        })
    return items


async def _transcribe_scene_clips(request, detection, clips, progress=None):
    """Transcribe the already-cut scene files, then restore global timestamps."""
    if not clips:
        return None
    data = build_worker_input(request)
    data.update(
        operation="transcribe_scene_clips",
        video_url="",
        video_path=clips[0]["path"],
        scene_clip_paths=[clip["path"] for clip in clips],
        worker_lock_wait_seconds=int(os.getenv("SCENE_WORKER_LOCK_WAIT_SECONDS", "1800")),
    )
    raw = await _run_worker(data)
    global_segments = []
    scene_rows = []
    warnings = []
    clip_results = raw.get("clips", [])
    if len(clip_results) != len(detection.scenes):
        raise ValueError(
            f"逐镜字幕结果数量不一致: scenes={len(detection.scenes)}, clips={len(clip_results)}"
        )
    for scene, clip_result in zip(detection.scenes, clip_results):
        status = clip_result.get("status", "completed")
        clip_warning = ""
        if status == "no_audio":
            clip_warning = f"分镜 {scene.index} 没有音轨，已跳过转写"
        elif status == "failed":
            clip_warning = f"分镜 {scene.index} 转写失败，已保留其他分析结果: {clip_result.get('error', 'unknown error')}"
        elif clip_result.get("retry_count"):
            clip_warning = f"分镜 {scene.index} 原始音频解码失败，FFmpeg重试后转写成功"
        if clip_warning:
            warnings.append(clip_warning)
        if progress:
            progress("transcribing", 32, {
                "event": "scene_transcription_completed",
                "scene_index": scene.index,
                "status": status,
                "retry_count": clip_result.get("retry_count", 0),
                **clip_result.get("timings", {}),
                **({"error": clip_result["error"]} if clip_result.get("error") else {}),
                "decode_error": clip_result.get("decode_error", ""),
            })
        offset = scene.start_us / 1_000_000
        local_segments = clip_result.get("segments", [])
        scene_ids = []
        scene_text = []
        speech_seconds = 0.0
        for local in local_segments:
            segment_id = scene.index * 1_000_000 + int(local["id"])
            start = round(offset + float(local["start"]), 3)
            end = round(offset + float(local["end"]), 3)
            words = []
            for word in local.get("words", []):
                words.append({
                    **word,
                    "start": round(offset + float(word["start"]), 3),
                    "end": round(offset + float(word["end"]), 3),
                })
            global_segments.append({
                **local, "id": segment_id, "start": start, "end": end, "words": words,
            })
            scene_ids.append(segment_id)
            if local.get("text", "").strip():
                scene_text.append(local["text"].strip())
            speech_seconds += max(0.0, float(local["end"]) - float(local["start"]))
        scene_rows.append(SceneTranscript(
            scene_index=scene.index,
            start_us=scene.start_us,
            end_us=scene.end_us,
            text=" ".join(scene_text).strip(),
            segment_ids=scene_ids,
            speech_seconds=round(speech_seconds, 3),
            speech_density=round(min(1.0, speech_seconds / max(0.001, scene.duration_us / 1e6)), 3),
            transcription_status=status,
            transcription_error=clip_result.get("error", ""),
            transcription_seconds=clip_result.get("timings", {}).get("total_seconds", 0.0),
            retry_count=clip_result.get("retry_count", 0),
        ))
    first = next((clip for clip in clip_results if clip.get("device") != "not_used"), clip_results[0])
    device = first.get("device", "not_used")
    return TranscribeVideoResponse.model_validate({
        "model_name": first.get("model_name", request.model_name),
        "device": device,
        "compute_type": first.get("compute_type", "not_used"),
        "language": first.get("language", request.language or "zh"),
        "language_probability": first.get("language_probability"),
        "duration_seconds": detection.duration_us / 1_000_000,
        "transcription_status": (
            "failed" if all(clip.get("status") == "failed" for clip in clip_results)
            else "no_audio" if all(clip.get("status") == "no_audio" for clip in clip_results)
            else "partial" if any(clip.get("status") == "failed" for clip in clip_results)
            else "completed"
        ),
        "segments": global_segments,
        "scenes": scene_rows,
        "detection": detection,
        "srt": to_srt(global_segments),
        "warnings": [
            "字幕按正式切割后的每个分镜文件分别使用 faster-whisper 转写。",
            "分镜字幕时间已恢复到原视频全局时间轴。",
        ] + warnings,
    })


def _build_report(detection, transcript, semantics, transitions, viral):
    speeches = {s.scene_index: s.text for s in transcript.scenes} if transcript else {}
    shots = []
    scene_captions = []
    for scene, result in zip(detection.scenes, semantics):
        captions = _scene_caption_items(scene, transcript)
        scene_captions.append({
            "scene_index": scene.index,
            "start_us": scene.start_us,
            "end_us": scene.end_us,
            "full_text": " ".join(item["text"] for item in captions),
            "items": captions,
        })
        transcript_scene = next(
            (item for item in transcript.scenes if item.scene_index == scene.index),
            None,
        ) if transcript else None
        shots.append({
            "shot_index": scene.index, "start_us": scene.start_us, "end_us": scene.end_us,
            "duration_us": scene.duration_us,
            "start": f"{scene.start_us / 1e6:.3f}s",
            "end": f"{scene.end_us / 1e6:.3f}s",
            "duration": f"{scene.duration_us / 1e6:.3f}s",
            "analysis_status": result.analysis_status,
            "model_id": result.model_id,
            "fallback_used": result.fallback_used,
            "content": result.content,
            "cinematography": " / ".join(
                (result.shot_type, result.camera_angle, result.camera_motion, result.composition)
            ),
            "action": result.actions, "emotion": result.emotions,
            "clothing": result.clothing, "environment": result.environment,
            "lighting": result.lighting,
            "scene_category": result.scene_category,
            "visual_intent": result.visual_intent,
            "primary_visual_subject": result.primary_visual_subject,
            "on_camera_speech": result.on_camera_speech,
            "dialogue": speeches.get(scene.index, ""),
            "dialogue_source": "faster-whisper" if speeches.get(scene.index) else "unavailable",
            "inferred": not bool(speeches.get(scene.index)),
            "transcription_status": transcript_scene.transcription_status if transcript_scene else "not_requested",
            "transcription_error": transcript_scene.transcription_error if transcript_scene else "",
            "captions": captions,
            "replaceable": result.replaceable, "replace_score": result.replace_score,
            "replacement_type": result.replacement_type,
            "replace_reason": result.replace_reason, "hook_score": result.hook_score,
            "hook_reason": result.hook_reason,
        })
    successful = [s for s in semantics if s.analysis_status == "completed"]
    return {
        "report_version": "2.0", "input_mode": "video_file_id",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "global_model_enriched": False,
        "step_1_global_genes": {
            "duration": f"{detection.duration_us / 1e6:.3f}s",
            "content_type": "unknown", "overall_rhythm": "unknown",
            "visual_style": "unknown", "lighting": "unknown", "color_system": "unknown",
            "core_copy": " ".join(speeches.values()),
            "bgm_and_sfx": "未完成独立音频分析",
            "hook_scenes": viral.hook_scenes, "viral_score": viral.score,
        },
        "step_2_core_visual_elements": {
            key: list(dict.fromkeys(v for s in successful for v in getattr(s, field)))
            for key, field in (("people", "people"), ("products_props", "products"),
                               ("environment", "environment"))
        },
        "step_3_shot_breakdown": shots,
        "scene_captions": scene_captions,
        "step_4_prompt_configuration": {
            "negative_prompt": "不得新增字幕、水印或无关人物物品",
            "duration": f"{detection.duration_us / 1e6:.3f}s",
            "aspect_ratio": "unknown",
            "shots": [{"shot": f"Shot {s.scene_index:02d}", "prompt": ""} for s in semantics],
        },
        "production_actions": {
            "replaceable_shots": [s.scene_index for s in successful if s.replaceable],
            "manual_review_shots": [
                s.scene_index for s in semantics
                if s.analysis_status != "completed" or s.replacement_type == "需人工复核"
            ],
            "transition_notes": [t.model_dump() for t in transitions],
        },
    }


def _merge_global(report, enriched):
    if not isinstance(enriched.get("content_type"), str) or not enriched["content_type"].strip():
        raise ValueError("Global response has no content_type")
    for key in ("content_type", "overall_rhythm", "visual_style", "lighting",
                "color_system", "story_summary"):
        if isinstance(enriched.get(key), str) and enriched[key].strip():
            report["step_1_global_genes"][key] = enriched[key]
    for key in ("products_props", "people", "environment"):
        value = enriched.get(key)
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            report["step_2_core_visual_elements"][key] = value
    for key in ("viral_reasons", "transition_highlights"):
        value = enriched.get(key)
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            report[key] = value
    by_index = {s["shot_index"]: s for s in report["step_3_shot_breakdown"]}
    for item in enriched.get("shot_breakdown", []):
        if not isinstance(item, dict) or not isinstance(item.get("shot_index"), int):
            continue
        target = by_index.get(item["shot_index"])
        if target and target["analysis_status"] == "completed":
            for key in ("content", "cinematography"):
                if isinstance(item.get(key), str):
                    target[key] = item[key]
    completed = {f"Shot {s['shot_index']:02d}" for s in by_index.values()
                 if s["analysis_status"] == "completed"}
    prompts = {s["shot"]: s for s in report["step_4_prompt_configuration"]["shots"]
               if s["shot"] in completed}
    for item in enriched.get("prompt_shots", []):
        if isinstance(item, dict) and isinstance(item.get("shot"), str):
            if item["shot"] in prompts and isinstance(item.get("prompt"), str):
                prompts[item["shot"]]["prompt"] = item["prompt"]
    report["global_model_enriched"] = True


def resolve_analysis_models(request, expected=None):
    if not request.use_external_model:
        return {}
    expected = expected or {}
    models = {
        "scene": resolve_model(request.scene_model_id, "scene", expected.get("scene")),
        "global": resolve_model(request.global_model_id, "global", expected.get("global")),
    }
    for purpose in ("scene", "global"):
        key = purpose + "_fallback"
        model_id = getattr(request, key + "_model_id")
        if model_id is not None:
            models[key] = resolve_model(model_id, purpose, expected.get(key))
    return models


async def analyze_video(request: AnalyzeVideoRequest, progress=None, model_bindings=None, task_id=None) -> AnalyzeVideoResponse:
    def stage(name, percent, details=None):
        if progress:
            try:
                progress(name, percent, details)
            except TypeError:
                # Keep compatibility with older callers that accept two args.
                progress(name, percent)

    models = await asyncio.to_thread(resolve_analysis_models, request, model_bindings)
    model_calls = {key: [] for key in models}
    selected = models.get("scene")
    global_selected = models.get("global")
    enabled = bool(models)
    warnings = []
    fallbacks = []
    global_model_used_id = None
    timings = {}
    started = time.monotonic()
    stage("models_resolved", 5, {"models_used": {k: snapshot(v) for k, v in models.items()}})
    with analysis_temporary_directory("capcut-analysis-") as directory:
        stage("preparing_scene_videos", 10, {"event": "phase_started", "phase": "detect_and_split"})
        stage("preparing_scene_videos", 10)
        data = build_worker_input(request)
        data.update(
            operation="prepare_analysis", work_directory=directory,
            # Scene clips are also required when only local transcription is enabled.
            use_external_model=bool(enabled or request.use_transcription),
            include_ocr=False, include_sample_images=False,
            worker_lock_wait_seconds=int(os.getenv("SCENE_WORKER_LOCK_WAIT_SECONDS", "1800")),
        )
        mark = time.monotonic()
        prepared = await _run_worker(data)
        detection = DetectScenesResponse.model_validate(prepared["detection"])
        warnings.extend(prepared.get("warnings", []))
        if prepared.get("boundary_reviews"):
            if request.detector == "auto":
                warnings.append("auto 使用统一的最终帧边界；候选证据与待复核项见 detection.diagnostics。")
            else:
                warnings.append("分析使用正式切割流程的边界复核结果；allow_review 模式会采用最佳候选边界。")
        timings["detect_and_split_seconds"] = round(time.monotonic() - mark, 3)
        stage("preparing_scene_videos", 20, {
            "event": "phase_completed",
            "phase": "detect_and_split",
            "detect_and_split_seconds": timings["detect_and_split_seconds"],
        })
        transcript = None
        if request.use_transcription:
            stage("transcribing", 30, {"event": "phase_started", "phase": "transcription"})
            stage("transcribing", 30)
            mark = time.monotonic()
            transcript = await _transcribe_scene_clips(
                TranscribeVideoRequest(**request.model_dump()),
                detection,
                prepared.get("clips", []),
                progress=stage,
            )
            warnings.extend(transcript.warnings)
            timings["transcription_seconds"] = round(time.monotonic() - mark, 3)
            stage("transcribing", 35, {
                "event": "phase_completed",
                "phase": "transcription",
                "transcription_seconds": timings["transcription_seconds"],
            })
        speeches = {s.scene_index: s.text for s in transcript.scenes} if transcript else {}
        files = VideoFiles(selected) if enabled and not uses_gemini(selected) else None
        file_ids = {}
        scene_times = []
        try:
            if enabled:
                # File uploads remain bounded to avoid saturating the network. Model
                # calls are intentionally launched once per scene below.
                upload_concurrency = max(1, int(os.getenv("SCENE_UPLOAD_CONCURRENCY", "2")))
                upload_semaphore = asyncio.Semaphore(upload_concurrency)

                async def upload_scene(index, scene):
                    async with upload_semaphore:
                        mark = time.monotonic()
                        stage("uploading_scene_video", 40, {
                            "event": "scene_upload_started", "scene_index": scene.index,
                        })
                        try:
                            clip = prepared["clips"][index]
                            file_id = await asyncio.to_thread(
                                files.upload, clip["path"], request.video_fps,
                            ) if files else clip["path"]
                            elapsed = round(time.monotonic() - mark, 3)
                            file_ids[scene.index] = file_id
                            stage("uploading_scene_video", 55, {
                                "event": "scene_upload_completed",
                                "scene_index": scene.index,
                                "upload_and_process_seconds": elapsed,
                            })
                            return scene.index, elapsed, None
                        except Exception as exc:
                            elapsed = round(time.monotonic() - mark, 3)
                            return scene.index, elapsed, exc

                upload_results = await asyncio.gather(*[
                    upload_scene(index, scene)
                    for index, scene in enumerate(detection.scenes)
                ])
                upload_errors = {
                    scene_index: (elapsed, error)
                    for scene_index, elapsed, error in upload_results
                    if error is not None
                }
                model_concurrency = min(
                    max(1, int(os.getenv("SCENE_MODEL_CONCURRENCY", "4"))),
                    max(1, len(detection.scenes)),
                )
                model_executor = ThreadPoolExecutor(
                    max_workers=model_concurrency,
                    thread_name_prefix="scene-model",
                )
                model_loop = asyncio.get_running_loop()

                async def analyze_scene(index, scene):
                    elapsed = {"scene_index": scene.index}
                    mark = time.monotonic()
                    last_error = None
                    routes = ["scene", "scene_fallback"] if models.get("scene_fallback") else ["scene", "scene"]
                    for attempt, route in enumerate(routes, 1):
                        config = models[route]
                        scope = Scope(
                            task_id=task_id, deadline=time.monotonic() + 600,
                            on_event=lambda event: model_loop.call_soon_threadsafe(
                                stage, "analyzing_scene_video", 60,
                                {"event": "model_stream", "model_request": event},
                            ),
                        )
                        backup_files = None
                        if route == "scene_fallback":
                            event = {
                                "purpose": "scene", "scene_index": scene.index,
                                "from_model_id": selected["id"], "to_model_id": config["id"],
                                "reason": type(last_error).__name__, "succeeded": False,
                            }
                            fallbacks.append(event)
                            stage("scene_model_fallback", 60, {"event": "model_fallback", **event})
                        try:
                            if route == "scene":
                                if scene.index in upload_errors:
                                    raise upload_errors[scene.index][1]
                                current = file_ids[scene.index]
                                previous = file_ids.get(detection.scenes[index - 1].index) if index else None
                            else:
                                current = prepared["clips"][index]["path"]
                                previous = prepared["clips"][index - 1]["path"] if index else None
                                # Provider file IDs cannot be reused by another model/provider.
                                if not uses_gemini(config):
                                    backup_files = VideoFiles(config)
                                    async with upload_semaphore:
                                        current = await asyncio.to_thread(
                                            backup_files.upload, current, request.video_fps,
                                        )
                                        if previous:
                                            previous = await asyncio.to_thread(
                                                backup_files.upload, previous, request.video_fps,
                                            )
                            enriched = await model_loop.run_in_executor(
                                model_executor,
                                partial(
                                    analyze_scene_with_responses, config, scene.model_dump(),
                                    speeches.get(scene.index, ""), current, previous,
                                    scope=scope,
                                ),
                            )
                            required = {"replaceable", "replace_score", "replace_reason", "hook_score",
                                        "hook_reason", "transition_type", "transition_reason", "content"}
                            if not isinstance(enriched, dict) or not required.issubset(enriched):
                                raise ValueError("Incomplete scene model judgement")
                            result = SceneSemanticAnalysis.model_validate({
                                **enriched, "scene_index": scene.index,
                                "analysis_status": "completed", "analysis_source": "video_model",
                                "ocr_text": [], "model_id": config["id"],
                                "fallback_used": route == "scene_fallback",
                            }, strict=True)
                            result = _apply_replacement_rules(result)
                            if result.hook_score is None:
                                raise ValueError("Model hook_score is missing")
                            if index and previous is None:
                                result.transition_type = "unknown"
                                result.continuity_score = None
                                result.transition_reason = "上一分镜视频上传失败，无法判断衔接"
                            elapsed["model_seconds"] = round(time.monotonic() - mark, 3)
                            elapsed["model_attempts"] = attempt
                            if route == "scene_fallback":
                                event["succeeded"] = True
                            return result, elapsed, None
                        except Exception as exc:
                            last_error = exc
                            if attempt == 1 and models.get("scene_fallback"):
                                continue
                            if attempt == 1 and isinstance(exc, ValueError) and scene.index not in upload_errors:
                                stage("retrying_scene_model", 60, {
                                    "event": "scene_retry",
                                    "scene_index": scene.index,
                                    "attempt": 2,
                                    "error": str(exc)[:500],
                                })
                                await asyncio.sleep(0.5)
                            else:
                                break
                        finally:
                            model_calls[route].extend(scope.calls)
                            if backup_files:
                                warnings.extend(await asyncio.to_thread(backup_files.cleanup))
                    elapsed["failed_phase_seconds"] = round(time.monotonic() - mark, 3)
                    elapsed["model_attempts"] = attempt
                    return SceneSemanticAnalysis(
                        scene_index=scene.index, analysis_status="failed",
                        replace_reason="模型分析失败，需人工复核",
                    ), elapsed, last_error

                scene_results = await asyncio.gather(*[
                    analyze_scene(index, scene)
                    for index, scene in enumerate(detection.scenes)
                ])
                model_executor.shutdown(wait=True)
                semantics = []
                for scene, (result, elapsed, error) in zip(detection.scenes, scene_results):
                    semantics.append(result)
                    scene_times.append(elapsed)
                    if error:
                        warnings.append(f"分镜 {scene.index} 模型分析失败: {error}")
                        stage("analyzing_scene_video", 80, {
                            "event": "scene_failed", "scene_index": scene.index,
                            **elapsed, "error": str(error)[:500],
                        })
                    else:
                        stage("analyzing_scene_video", 80, {
                            "event": "scene_completed", "scene_index": scene.index,
                            **elapsed,
                        })
            else:
                semantics = [
                    SceneSemanticAnalysis(scene_index=scene.index, replace_reason="模型未完成判断")
                    for scene in detection.scenes
                ]
        finally:
            if files:
                warnings.extend(await asyncio.to_thread(files.cleanup))
        timings["scenes"] = scene_times

    transitions = [
        TransitionAnalysis(
            from_scene=left.scene_index, to_scene=right.scene_index,
            transition_type=right.transition_type,
            continuity_score=right.continuity_score,
            highlight=right.transition_reason,
            analysis_status="completed" if right.analysis_status == "completed"
            and right.transition_type not in ("unknown", "opening") else "unavailable",
        ) for left, right in zip(semantics, semantics[1:])
    ]
    scores = {s.scene_index: s.hook_score for s in semantics
              if s.analysis_status == "completed" and s.hook_score is not None}
    viral = ViralAnalysis(
        hook_scenes=sorted(scores, key=scores.get, reverse=True)[:3],
        scene_scores=scores, score=round(sum(scores.values()) / len(scores), 2) if scores else 0,
        reasons=[f"分镜 {s.scene_index}: {s.hook_reason}" for s in semantics if s.scene_index in scores],
        analysis_status="completed" if len(scores) == len(semantics) else "partial" if scores else "unavailable",
    )
    report = _build_report(detection, transcript, semantics, transitions, viral)
    scene_captions = report["scene_captions"]
    successful = [s for s in semantics if s.analysis_status == "completed"]
    if enabled and successful:
        stage("global_model_analysis", 85, {"event": "phase_started", "phase": "global_model"})
        stage("global_model_analysis", 85)
        mark = time.monotonic()
        global_loop = asyncio.get_running_loop()
        routes = ["global", "global_fallback"] if models.get("global_fallback") else ["global"]
        last_error = None
        for route in routes:
            config = models[route]
            global_scope = Scope(
                task_id=task_id,
                on_event=lambda event: global_loop.call_soon_threadsafe(
                    stage, "global_model_analysis", 85,
                    {"event": "model_stream", "model_request": event},
                ),
            )
            if route == "global_fallback":
                event = {
                    "purpose": "global", "from_model_id": global_selected["id"],
                    "to_model_id": config["id"], "reason": type(last_error).__name__,
                    "succeeded": False,
                }
                fallbacks.append(event)
                stage("global_model_fallback", 85, {"event": "model_fallback", **event})
            try:
                enriched = await asyncio.to_thread(
                    analyze_global_report_with_responses, config,
                    [{**result.model_dump(), "start_us": scene.start_us,
                      "end_us": scene.end_us, "duration_us": scene.duration_us}
                     for scene, result in zip(detection.scenes, semantics)],
                    " ".join(speeches.values()), [t.model_dump() for t in transitions],
                    scope=global_scope,
                )
                # A malformed primary must not partially mutate the backup's report.
                candidate_report = deepcopy(report)
                _merge_global(candidate_report, enriched)
                report = candidate_report
                global_model_used_id = config["id"]
                if route == "global_fallback":
                    event["succeeded"] = True
                break
            except Exception as exc:
                last_error = exc
                if route == routes[-1]:
                    warnings.append(f"全局报告模型分析失败，已保留逐镜报告: {exc}")
            finally:
                model_calls[route].extend(global_scope.calls)
        timings["global_model_seconds"] = round(time.monotonic() - mark, 3)
        stage("global_model_analysis", 90, {
            "event": "phase_completed" if report["global_model_enriched"] else "phase_failed",
            "phase": "global_model",
            "global_model_seconds": timings["global_model_seconds"],
        })
    timings["total_seconds"] = round(time.monotonic() - started, 3)
    stage("reporting", 95, {
        "event": "analysis_timing",
        "timings": timings,
    })
    report["timings"] = timings
    report["fallbacks"] = fallbacks
    report["global_model_used_id"] = global_model_used_id
    video_protocols = {
        "gemini_inline_video" if uses_gemini(config) else "video_file_id"
        for key, config in models.items() if key in ("scene", "scene_fallback")
    }
    if video_protocols:
        report["input_mode"] = next(iter(video_protocols)) if len(video_protocols) == 1 else "mixed_video"
    stage("reporting", 95)
    return AnalyzeVideoResponse(
        fallbacks=fallbacks, global_model_used_id=global_model_used_id,
        detection=detection, transcript=transcript, visual_features=[],
        semantics=semantics, transitions=transitions, viral_analysis=viral,
        scene_captions=scene_captions,
        scene_model_id=selected["id"] if enabled else None,
        global_model_id=global_selected["id"] if enabled else None,
        models_used={k: {**snapshot(v), "calls": model_calls[k]} for k, v in models.items()},
        model_used=selected["model"] if enabled else None,
        analysis_report=report, warnings=warnings,
    )
