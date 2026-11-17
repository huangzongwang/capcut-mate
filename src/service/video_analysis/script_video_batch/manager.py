"""Orchestrate reviewed script shots into independent speech-first drafts."""
from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid

from src.service.create_draft import create_draft
from src.service.add_videos import add_videos_async
from src.service.add_audios import add_audios_async
from src.service.add_captions import add_captions_async
from src.service.add_masks import add_masks_async
from src.service.video_analysis import scene_detection
from src.service.video_analysis.audio_generation.manager import manager as audio_generation_manager
from src.service.video_analysis.digital_human import manager as digital_human_manager
from src.service.video_analysis.digital_human.schemas import SubmitRequest as DigitalHumanRequest
from src.service.video_analysis.platform_runtime import (
    analysis_temporary_directory, digital_human_root, media_root, media_tool,
)
from src.service.video_analysis.storyboard import store as storyboard_store
from src.service.video_analysis.storyboard.schemas import DEFAULT_VISUAL_INTENTS
from src.service.video_analysis.script_video_batch import (
    material_review, media_library, music_library, task_store, verification_cache, vertical_renderer,
)
from src.utils import helper
from src.utils.draft_cache import DRAFT_CACHE
from src.utils.logger import logger


_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="script-video-batch")
_executor_lock = threading.Lock()
_active: set[str] = set()
_ROLE_MAP = {
    "product_empty_scene": ("product_empty_scene", "product_beauty", "product_detail", "product_demo"),
    "scene_empty_scene": ("scene_empty_scene", "lifestyle", "environment"),
    "motion": ("motion", "exercise", "action", "demonstration", "pain_point"),
    "body_display": ("body_display", "pain_point", "demonstration"),
    "mechanism": ("mechanism", "demonstration"),
}


def submit(request) -> dict:
    task_id = f"svb_{uuid.uuid4().hex}"
    request_data = request.model_dump(mode="json")
    for field, snapshot_field in (
        ("material_review_primary_model_id", "_material_review_primary_snapshot"),
        ("material_review_backup_model_id", "_material_review_backup_snapshot"),
    ):
        model_id = request_data.get(field)
        if model_id is None:
            continue
        try:
            request_data[snapshot_field] = material_review.selected_model(
                material_review.resolve_selected(model_id),
            )
        except material_review.MaterialReviewError as exc:
            raise ValueError(str(exc)) from None
    task_store.create(task_id, request_data)
    with _executor_lock:
        if task_id in _active:
            raise RuntimeError("批量任务重复调度")
        _active.add(task_id)
    try:
        _pool.submit(_run_guarded, task_id)
    except RuntimeError:
        with _executor_lock:
            _active.discard(task_id)
        task_store.update(task_id, status="failed", stage="failed", progress=100,
                           error_message="无法启动批量任务线程", completed_at=_now())
        raise
    return public_task(task_store.get(task_id))


def _run_guarded(task_id: str):
    try:
        _run(task_id)
    finally:
        with _executor_lock:
            _active.discard(task_id)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _runtime_root() -> Path:
    from ..platform_runtime import ensure_writable_directory
    return ensure_writable_directory(media_root() / "script-video-batch")


def _snapshot_media(task_id: str, path: Path, name: str) -> Path:
    target = _runtime_root() / task_id / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() != target.resolve():
        shutil.copyfile(path, target)
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError(f"任务媒体快照无效: {name}")
    return target


def _public_media_url(path: Path) -> str:
    base = os.getenv("SCRIPT_VIDEO_MEDIA_URL_BASE", os.getenv("DRAFT_MEDIA_BASE_URL", "")).rstrip("/")
    if not base:
        base = "http://127.0.0.1:30001/media" if os.name == "nt" else "http://nginx/media"
    root = media_root().resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("草稿媒体必须位于 VIDEO_ANALYSIS_MEDIA_ROOT 下")
    from urllib.parse import quote
    return f"{base}/{quote(resolved.relative_to(root).as_posix(), safe='/')}"


def get(task_id: str):
    task = task_store.get(task_id)
    return public_task(task) if task else None


def public_task(task: dict) -> dict:
    versions = task.get("versions") or []
    return {
        "task_id": task["task_id"], "status": task["status"], "stage": task["stage"],
        "progress": task["progress"], "count": task["request"].get("count", 20),
        "completed_count": sum(item.get("status") == "completed" for item in versions),
        "failed_count": sum(item.get("status") == "failed" for item in versions),
        "storyboard_id": task.get("storyboard_id"), "error_message": task.get("error_message", ""),
        "material_review_primary_model_id": task["request"].get("material_review_primary_model_id"),
        "material_review_backup_model_id": task["request"].get("material_review_backup_model_id"),
        "created_at": task["created_at"], "completed_at": task.get("completed_at"),
        "versions": versions,
    }


def _resolve_media(value: str | None, label: str, required=True) -> Path | None:
    if not value:
        if required:
            raise ValueError(f"缺少{label}路径")
        return None
    root = media_root().resolve()
    for path in _upload_candidates(root, value):
        if path.is_file() and path.stat().st_size:
            return path
    raise ValueError(f"{label}不存在或不在 VIDEO_ANALYSIS_MEDIA_ROOT 内")


def _upload_candidates(root: Path, value: str) -> tuple[Path, ...]:
    """Resolve upload_video paths while retaining legacy root-level media support."""
    relative = value[len("uploads/"):] if value.startswith("uploads/") else value
    candidates = ((root / "uploads" / relative).resolve(), (root / relative).resolve())
    return tuple(path for path in candidates if path.is_relative_to(root))


def _resolve_avatar_media(value: str | None) -> Path:
    if not value:
        raise ValueError("缺少数字人形象底片")
    roots = (media_root().resolve(), digital_human_root().resolve())
    for root in roots:
        for path in _upload_candidates(root, value):
            if path.is_file() and path.stat().st_size:
                return path
    raise ValueError("数字人形象底片不存在于 video-analysis 或 digital-human 上传目录")


def _resolve_audio_media(value: str | None, required=False) -> Path | None:
    if not value:
        if required:
            raise ValueError("缺少口播音频")
        return None
    for root in (media_root().resolve(), digital_human_root().resolve()):
        for path in _upload_candidates(root, value):
            if path.is_file() and path.stat().st_size:
                return path
    raise ValueError("口播音频不存在于 video-analysis 或 digital-human 媒体目录")


def _probe(path: Path) -> tuple[int, int, int, bool]:
    result = subprocess.run([
        media_tool("ffprobe"), "-v", "error", "-show_entries",
        "stream=codec_type,width,height:format=duration", "-of", "json", str(path),
    ], capture_output=True, text=True, timeout=30, check=True)
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    has_audio = any(item.get("codec_type") == "audio" for item in streams)
    duration = int(float((payload.get("format") or {}).get("duration") or 0) * 1_000_000)
    return int(video.get("width") or 0), int(video.get("height") or 0), duration, has_audio


def _split_panel_layout(width: int, height: int, ratio: float, gap_px: int) -> dict:
    available = height - gap_px
    if width <= 0 or available < 2:
        raise ValueError("上下分屏画布尺寸无效")
    top_height = max(1, round(available * ratio))
    bottom_height = available - top_height
    if bottom_height <= 0:
        raise ValueError("上下分屏比例无效")
    return {
        "top_width": width,
        "top_height": top_height,
        "bottom_width": width,
        "bottom_height": bottom_height,
        # add_videos converts pixels by the full canvas while Jianying stores
        # offsets in half-canvas units, so pass twice the desired center shift.
        "top_transform_y": height - top_height,
        "bottom_transform_y": -(height - bottom_height),
    }


def _panel_video_filter(width: int, height: int, duration_us: int,
                        fit_mode: str, rotation_degrees: int = 0) -> tuple[str, list[str]]:
    duration = duration_us / 1_000_000
    prefix = (
        f"trim=duration={duration:.6f},setpts=PTS-STARTPTS,"
        f"{vertical_renderer.rotation_filter(rotation_degrees)}"
    ).rstrip(",")
    suffix = "setsar=1,tpad=stop_mode=clone:stop_duration=0.100"
    if fit_mode == "contain_blur":
        value = (
            f"[0:v]{prefix},split=2[panel_bg_source][panel_fg_source];"
            f"[panel_bg_source]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=30[panel_bg];"
            f"[panel_fg_source]scale={width}:{height}:force_original_aspect_ratio=decrease[panel_fg];"
            f"[panel_bg][panel_fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2,"
            f"{suffix}[panel_out]"
        )
        return value, ["-filter_complex", value, "-map", "[panel_out]"]
    if fit_mode == "contain_black":
        value = (
            f"{prefix},scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,{suffix}"
        )
        return value, ["-vf", value]
    if fit_mode == "cover":
        value = (
            f"{prefix},scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},{suffix}"
        )
        return value, ["-vf", value]
    raise ValueError(f"不支持的上下分屏适配模式: {fit_mode}")


def _export_panel_clip(task_id: str, source: Path, source_key: str,
                       source_start_us: int, duration_us: int,
                       width: int, height: int, fit_mode: str,
                       cache: dict[str, Path], rotation_degrees: int = 0) -> Path:
    key = (f"panel|{source_key}|{source_start_us}|{duration_us}|{width}x{height}|"
           f"{fit_mode}|rotate={rotation_degrees}")
    if key in cache:
        return cache[key]
    root = _runtime_root() / task_id / "split-panels"
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"panel-{uuid.uuid4().hex}.mp4"
    _, filter_args = _panel_video_filter(
        width, height, duration_us, fit_mode, rotation_degrees,
    )
    encoded_duration_us = duration_us + 50_000
    subprocess.run([
        media_tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-ss",
        f"{source_start_us / 1_000_000:.6f}", "-i", str(source), "-an", *filter_args,
        "-t", f"{encoded_duration_us / 1_000_000:.6f}", "-c:v", "libx264",
        "-preset", "veryfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
    ], capture_output=True, timeout=900, check=True)
    actual_width, actual_height, actual_duration, _ = _probe(output)
    if (actual_width, actual_height) != (width, height):
        output.unlink(missing_ok=True)
        raise RuntimeError("上下分屏素材尺寸与目标面板不一致")
    if actual_duration < duration_us:
        output.unlink(missing_ok=True)
        raise RuntimeError("上下分屏素材短于目标分镜时长")
    cache[key] = output
    return output


def _export_video_clip(task_id: str, clip: dict, source_start_us: int,
                       duration_us: int, width: int, height: int,
                       cache: dict[str, Path]) -> Path:
    source_url = clip["source_video_url"]
    fit_mode = str(clip.get("vertical_fit") or "cover")
    focus_x = float(clip.get("focus_x", 0.5))
    focus_y = float(clip.get("focus_y", 0.5))
    rotation_degrees = int(clip.get("rotation_degrees") or 0)
    key = (f"{source_url}|{source_start_us}|{duration_us}|{width}x{height}|"
           f"{fit_mode}|{focus_x:.4f}|{focus_y:.4f}|rotate={rotation_degrees}")
    if key in cache:
        return cache[key]
    source = media_library.resolve_local_source(clip) or _snapshot_remote_source(task_id, clip)
    root = _runtime_root() / task_id / "clips"
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"clip-{uuid.uuid4().hex}.mp4"
    source_duration_us = int(clip.get("source_duration_us") or duration_us)
    encoded_duration_us = duration_us + 50_000
    def encode(selected_fit: str):
        filters = vertical_renderer.filter_args(
            width=width, height=height, duration_us=source_duration_us,
            fit_mode=selected_fit, focus_x=focus_x, focus_y=focus_y,
            rotation_degrees=rotation_degrees,
        )
        subprocess.run([
            media_tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-ss",
            f"{source_start_us / 1_000_000:.6f}", "-i", str(source), "-an", *filters,
            "-t", f"{encoded_duration_us / 1_000_000:.6f}", "-c:v", "libx264",
            "-preset", "veryfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(output),
        ], capture_output=True, timeout=900, check=True)

    try:
        encode(fit_mode)
    except subprocess.CalledProcessError:
        if fit_mode == "contain_blur":
            raise
        output.unlink(missing_ok=True)
        encode("contain_blur")
        fit_mode = "contain_blur"
        clip["vertical_fit"] = fit_mode
    actual_width, actual_height, actual_duration, _ = _probe(output)
    if (actual_width, actual_height) != (width, height):
        output.unlink(missing_ok=True)
        raise RuntimeError("竖屏素材转换后的尺寸不正确")
    if actual_duration < duration_us:
        output.unlink(missing_ok=True)
        raise RuntimeError("素材裁剪结果短于目标分镜时长")
    cache[key] = output
    return output


def _snapshot_remote_source(task_id: str, clip: dict) -> Path:
    from urllib.parse import urlsplit
    import requests

    source_url = clip["source_video_url"]
    root = _runtime_root() / task_id / "source-materials"
    root.mkdir(parents=True, exist_ok=True)
    extension = Path(urlsplit(source_url).path).suffix.lower()
    if extension not in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
        extension = ".mp4"
    target = root / (str(abs(hash(source_url))) + extension)
    expected_size = int(clip.get("file_size") or 0)
    expected_hash = str(clip.get("sha256") or "").lower()
    if target.is_file() and _download_matches(target, expected_size, expected_hash):
        return target
    target.unlink(missing_ok=True)
    with requests.get(source_url, stream=True, timeout=(15, 120)) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if content_type.startswith("text/") or "json" in content_type:
            raise RuntimeError(f"素材地址返回的不是视频文件: {content_type or 'unknown'}")
        with analysis_temporary_directory(f"{task_id}-material-") as directory:
            temporary = Path(directory) / target.name
            size = 0
            digest = hashlib.sha256()
            with temporary.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        size += len(chunk)
                        if size > 2 * 1024 * 1024 * 1024:
                            raise RuntimeError("素材文件超过2GB限制")
                        digest.update(chunk)
                        output.write(chunk)
            if size == 0:
                raise RuntimeError("素材下载结果为空")
            if expected_size and size != expected_size:
                raise RuntimeError(f"素材下载大小不匹配: expected={expected_size}, actual={size}")
            if expected_hash and digest.hexdigest() != expected_hash:
                raise RuntimeError("素材下载哈希校验失败")
            shutil.copyfile(temporary, target)
    return target


def _download_matches(path: Path, expected_size: int, expected_hash: str) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if expected_size and path.stat().st_size != expected_size:
        return False
    if not expected_hash:
        return True
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected_hash


def _audio_path_for(task_id: str, request: dict, source_video: Path | None) -> Path:
    explicit = _resolve_media(request.get("source_audio_path"), "口播音频", required=False)
    if explicit:
        return explicit
    if source_video is None:
        raise ValueError("数字人口播流程需要 source_audio_path（WAV）")
    _, _, _, has_audio = _probe(source_video)
    if not has_audio:
        raise ValueError("现有口播视频没有音轨，请通过 source_audio_path 提供音频")
    root = _runtime_root() / task_id
    root.mkdir(parents=True, exist_ok=True)
    output = root / "master-audio.wav"
    subprocess.run([
        media_tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i", str(source_video),
        "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(output),
    ], capture_output=True, timeout=600, check=True)
    return output


def _audio_path_with_offset(task_id: str, source_video: Path, offset_us: int) -> Path:
    root = _runtime_root() / task_id
    root.mkdir(parents=True, exist_ok=True)
    output = root / "master-audio-aligned.wav"
    subprocess.run([
        media_tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i", str(source_video),
        "-map", "0:a:0", "-vn", "-af", f"atrim=start={offset_us / 1_000_000:.6f},asetpts=PTS-STARTPTS",
        "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(output),
    ], capture_output=True, timeout=600, check=True)
    return output


def _create_avatar_master(task_id: str, request: dict, audio_path: Path) -> Path:
    avatar = _resolve_avatar_media(request.get("avatar_video_path"))
    if not request.get("duix_api_base_url") or not request.get("duix_shared_dir"):
        raise ValueError("digital_human 模式需要 duix_api_base_url 与 duix_shared_dir")
    # Reuse the already-generated speech audio while keeping this task's uploaded source intact.
    upload_name = f"script-video-batch-{task_id}.wav"
    target = media_root() / "uploads" / upload_name
    target.parent.mkdir(parents=True, exist_ok=True)
    if audio_path != target:
        shutil.copyfile(audio_path, target)
    root = media_root().resolve() if avatar.is_relative_to(media_root().resolve()) else digital_human_root().resolve()
    video_relative = avatar.relative_to(root).as_posix()
    audio_rel = f"uploads/{upload_name}"
    submitted = digital_human_manager.submit(DigitalHumanRequest(
        video_path=video_relative, audio_path=audio_rel,
        duix_api_base_url=request["duix_api_base_url"], duix_shared_dir=request["duix_shared_dir"],
    ))
    deadline = time.monotonic() + float(os.getenv("SCRIPT_VIDEO_DUIX_TIMEOUT_SECONDS", "1800"))
    while time.monotonic() < deadline:
        task = digital_human_manager.store.get(submitted["task_id"])
        if task and task["status"] == "completed":
            path = Path(task["preview_path"])
            if path.is_file() and path.stat().st_size:
                output = _runtime_root() / task_id / "avatar-master.mp4"
                shutil.copyfile(path, output)
                return output
            raise RuntimeError("Duix任务完成但数字人结果文件不存在")
        if task and task["status"] == "failed":
            raise RuntimeError(task.get("error_message") or "数字人生成失败")
        time.sleep(2)
    # Keep upstream task alive; caller can inspect the nested task in the batch error.
    raise TimeoutError(f"数字人生成超时；Duix子任务仍可查询: {submitted['task_id']}")


def _generate_audio(task_id: str, request: dict, script: str) -> Path:
    from src.service.video_analysis.audio_generation.manager import manager as audio_manager
    from src.service.video_analysis.audio_generation.schemas import GenerateAudioRequest
    from src.service.video_analysis.audio_generation.storage import audio_path

    audio_request = GenerateAudioRequest(
        text=script, voice_element_id=request["voice_element_id"],
        voice_speed=request["voice_speed"],
        pronunciation_overrides=request.get("pronunciation_overrides") or [],
    )
    submitted = audio_manager.submit(audio_request)
    audio_manager.execute(submitted["task"]["task_id"], audio_request, submitted["credentials"])
    result = audio_manager.get(submitted["task"]["task_id"])
    if not result or result["status"] != "completed":
        raise RuntimeError((result or {}).get("error_message") or "口播音频生成失败")
    target = _runtime_root() / task_id / "generated-audio.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(audio_path(submitted["task"]["task_id"]), target)
    return target


def _prepare_master(task_id: str, request: dict, audio_path: Path | None) -> tuple[Path, Path]:
    if request["source_mode"] == "existing_video":
        source = _resolve_media(request.get("source_video_path"), "口播视频")
        audio = audio_path or _audio_path_for(task_id, request, source)
        return source, audio
    if audio_path is None:
        raise ValueError("digital_human 模式应提供本次任务生成的音频")
    return _create_avatar_master(task_id, request, audio_path), audio_path


def _transcribe(audio_path: Path) -> list[dict]:
    payload = asyncio.run(scene_detection._run_worker({
        "operation": "transcribe_scene_clips",
        "video_url": "",
        "video_path": str(audio_path),
        "work_directory": str(audio_path.parent),
        "scene_clip_paths": [str(audio_path)],
        "model_name": os.getenv("SCRIPT_VIDEO_WHISPER_MODEL", "large-v3"),
        "language": "zh",
        "word_timestamps": True,
        "vad_filter": True,
        "worker_lock_wait_seconds": int(os.getenv("SCENE_WORKER_LOCK_WAIT_SECONDS", "1800")),
    }))
    clips = payload.get("clips") or []
    result = clips[0] if clips else {}
    if result.get("status") != "completed" or not result.get("segments"):
        detail = str(result.get("error") or "未识别到字幕")
        raise RuntimeError(f"口播转写失败: {detail}；为避免错误替换，本任务停止")
    return result["segments"]


def _transcript_matches_script(script: str, transcript: list[dict]) -> bool:
    normalize = lambda value: re.sub(r"[^\w\u4e00-\u9fff]", "", value, flags=re.UNICODE)
    expected = normalize(script)
    actual = normalize("".join(str(segment.get("text", "")) for segment in transcript))
    if not expected or not actual:
        return False
    # ASR may vary punctuation or contain a few recognition substitutions.
    from difflib import SequenceMatcher
    return SequenceMatcher(None, expected, actual, autojunk=False).ratio() >= 0.82


def _pause_removals_us(transcript: list[dict], *, detect_ms: int, keep_ms: int,
                       long_pause_ms: int, cover_long_pauses: bool) -> list[tuple[int, int]]:
    words = []
    for segment in transcript:
        for word in segment.get("words") or []:
            try:
                start_us = round(float(word["start"]) * 1_000_000)
                end_us = round(float(word["end"]) * 1_000_000)
            except (KeyError, TypeError, ValueError):
                continue
            if end_us > start_us:
                words.append((start_us, end_us))
    words.sort()
    if len(words) < 2:
        return []

    detect_us = detect_ms * 1000
    keep_us = keep_ms * 1000
    long_us = long_pause_ms * 1000
    removals = []
    previous_end = words[0][1]
    for next_start, next_end in words[1:]:
        gap_us = next_start - previous_end
        if gap_us >= detect_us:
            if gap_us < 700_000:
                target_keep_us = keep_us
            elif gap_us < long_us:
                target_keep_us = max(keep_us, 280_000)
            elif cover_long_pauses:
                target_keep_us = max(keep_us, 350_000)
            else:
                target_keep_us = gap_us
            if target_keep_us < gap_us:
                keep_before_us = target_keep_us // 2
                removals.append((
                    previous_end + keep_before_us,
                    next_start - (target_keep_us - keep_before_us),
                ))
        previous_end = max(previous_end, next_end)
    return removals


def _remap_time_us(value_us: int, removals: list[tuple[int, int]]) -> int:
    removed_us = 0
    for start_us, end_us in removals:
        if value_us >= end_us:
            removed_us += end_us - start_us
            continue
        if value_us > start_us:
            return start_us - removed_us
        break
    return value_us - removed_us


def _remap_transcript(transcript: list[dict], removals: list[tuple[int, int]]) -> list[dict]:
    mapped = copy.deepcopy(transcript)
    for segment in mapped:
        for item in [segment, *(segment.get("words") or [])]:
            original_start = item.get("start")
            original_end = item.get("end")
            if original_start is None or original_end is None:
                continue
            start_us = _remap_time_us(round(float(original_start) * 1_000_000), removals)
            end_us = _remap_time_us(round(float(original_end) * 1_000_000), removals)
            item["start"] = start_us / 1_000_000
            item["end"] = max(start_us, end_us) / 1_000_000
    return mapped


def _keep_intervals(total_us: int, removals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    intervals = []
    cursor = 0
    for start_us, end_us in removals:
        start_us = max(cursor, min(total_us, start_us))
        end_us = max(start_us, min(total_us, end_us))
        if start_us > cursor:
            intervals.append((cursor, start_us))
        cursor = end_us
    if cursor < total_us:
        intervals.append((cursor, total_us))
    return intervals


def _trim_concat_video(source: Path, target: Path, intervals: list[tuple[int, int]]) -> None:
    filters = []
    labels = []
    for index, (start_us, end_us) in enumerate(intervals):
        label = f"v{index}"
        filters.append(
            f"[0:v]trim=start={start_us / 1_000_000:.6f}:end={end_us / 1_000_000:.6f},"
            f"setpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")
    output_label = "v0"
    if len(labels) > 1:
        output_label = "vout"
        filters.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[{output_label}]")
    subprocess.run([
        media_tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i", str(source),
        "-filter_complex", ";".join(filters), "-map", f"[{output_label}]", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(target),
    ], capture_output=True, timeout=1800, check=True)


def _trim_concat_audio(source: Path, target: Path, intervals: list[tuple[int, int]]) -> None:
    filters = []
    labels = []
    for index, (start_us, end_us) in enumerate(intervals):
        label = f"a{index}"
        filters.append(
            f"[0:a]atrim=start={start_us / 1_000_000:.6f}:end={end_us / 1_000_000:.6f},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        labels.append(f"[{label}]")
    output_label = "a0"
    if len(labels) > 1:
        output_label = "aout"
        filters.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[{output_label}]")
    subprocess.run([
        media_tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i", str(source),
        "-filter_complex", ";".join(filters), "-map", f"[{output_label}]", "-vn",
        "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(target),
    ], capture_output=True, timeout=1800, check=True)


def _optimize_master_pauses(task_id: str, video_path: Path, audio_path: Path,
                            transcript: list[dict], duration_us: int, offset_us: int,
                            request: dict) -> tuple[Path, Path, list[dict], dict]:
    if request.get("pause_mode", "hybrid") == "off":
        return video_path, audio_path, transcript, {"removed_us": 0, "pause_count": 0}
    removals = _pause_removals_us(
        transcript,
        detect_ms=int(request.get("pause_detect_ms", 300)),
        keep_ms=int(request.get("pause_keep_ms", 220)),
        long_pause_ms=int(request.get("long_pause_ms", 1000)),
        cover_long_pauses=bool(request.get("cover_long_pauses", True)),
    )
    audio_duration_us = _probe(audio_path)[2]
    timeline_us = min(audio_duration_us, max(0, duration_us - offset_us))
    removals = [(start_us, end_us) for start_us, end_us in removals
                if start_us < timeline_us and min(end_us, timeline_us) > start_us]
    removals = [(start_us, min(end_us, timeline_us)) for start_us, end_us in removals]
    if not removals:
        return video_path, audio_path, transcript, {"removed_us": 0, "pause_count": 0}

    video_removals = [(start_us + offset_us, end_us + offset_us) for start_us, end_us in removals]
    video_intervals = _keep_intervals(duration_us, video_removals)
    audio_intervals = _keep_intervals(timeline_us, removals)
    root = _runtime_root() / task_id
    root.mkdir(parents=True, exist_ok=True)
    optimized_video = root / "pause-optimized-master.mp4"
    optimized_audio = root / "pause-optimized-audio.wav"
    _trim_concat_video(video_path, optimized_video, video_intervals)
    _trim_concat_audio(audio_path, optimized_audio, audio_intervals)
    if not optimized_video.is_file() or not optimized_audio.is_file():
        raise RuntimeError("停顿优化未生成完整的视频和音频")
    removed_us = sum(end_us - start_us for start_us, end_us in removals)
    logger.info(
        "script_video_batch event=pause_optimized task_id=%s pauses=%s removed_ms=%s",
        task_id, len(removals), round(removed_us / 1000),
    )
    return optimized_video, optimized_audio, _remap_transcript(transcript, removals), {
        "removed_us": removed_us,
        "pause_count": len(removals),
    }


def _align_shots(board, transcript: list[dict], duration_us: int, *, offset_us: int = 0) -> list[dict]:
    def normalize(value: str) -> str:
        return re.sub(r"[^\w\u4e00-\u9fff]", "", value, flags=re.UNICODE)

    words = []
    for segment in transcript:
        for word in segment.get("words") or []:
            text = normalize(word.get("word", ""))
            if text:
                words.append({"text": text, "start": float(word["start"]), "end": float(word["end"])})
    if not words:
        raise RuntimeError("Whisper未返回词级时间戳，不能安全定位字幕和替换区间")
    combined = "".join(item["text"] for item in words)
    normalized = combined
    char_spans = []
    for word_index, item in enumerate(words):
        count = len(item["text"])
        for char_index, char in enumerate(item["text"]):
            start = item["start"] + (item["end"] - item["start"]) * char_index / count
            end = item["start"] + (item["end"] - item["start"]) * (char_index + 1) / count
            char_spans.append((char, word_index, start, end))
    expected = normalize(board.request.script)
    from difflib import SequenceMatcher
    boundary_map: list[int | None] = [None] * (len(expected) + 1)
    for tag, expected_start, expected_end, actual_start, actual_end in SequenceMatcher(
        None, expected, normalized, autojunk=False,
    ).get_opcodes():
        expected_length = expected_end - expected_start
        actual_length = actual_end - actual_start
        if expected_length == 0:
            boundary_map[expected_start] = actual_end
            continue
        for offset in range(expected_length + 1):
            if tag == "equal":
                mapped = actual_start + offset
            elif tag == "delete":
                mapped = actual_start
            else:
                mapped = actual_start + round(actual_length * offset / expected_length)
            boundary_map[expected_start + offset] = mapped
    last_boundary = 0
    for index, value in enumerate(boundary_map):
        if value is None:
            value = last_boundary
        boundary_map[index] = max(last_boundary, min(len(normalized), value))
        last_boundary = boundary_map[index]

    shots = []
    expected_cursor = 0
    for shot in board.shots:
        target = normalize(shot.source_text)
        if not target:
            raise RuntimeError(f"分镜内容不包含可对齐文字: {shot.shot_id}")
        expected_end = expected_cursor + len(target)
        if expected_end > len(expected):
            raise RuntimeError(f"分镜台词无法可靠对齐转写结果: {shot.shot_id}")
        actual_start = boundary_map[expected_cursor]
        actual_end = boundary_map[expected_end]
        if actual_end <= actual_start or actual_start >= len(char_spans):
            raise RuntimeError(f"分镜台词无法可靠对齐转写结果: {shot.shot_id}")
        first_char = char_spans[actual_start]
        last_char = char_spans[min(actual_end, len(char_spans)) - 1]
        start = max(0, round(first_char[2] * 1_000_000) + offset_us)
        end = min(duration_us, round(last_char[3] * 1_000_000) + offset_us)
        if end <= start:
            raise RuntimeError(f"分镜时间范围无效: {shot.shot_id}")
        actual_text = normalized[actual_start:actual_end]
        alignment_confidence = SequenceMatcher(
            None, target, actual_text, autojunk=False,
        ).ratio()
        char_boundaries_us = []
        for local_offset in range(len(target) + 1):
            actual_boundary = boundary_map[expected_cursor + local_offset]
            if actual_boundary <= actual_start:
                boundary_seconds = first_char[2]
            elif actual_boundary >= actual_end:
                boundary_seconds = last_char[3]
            else:
                boundary_seconds = (
                    char_spans[actual_boundary - 1][3] + char_spans[actual_boundary][2]
                ) / 2
            char_boundaries_us.append(
                max(0, min(duration_us, round(boundary_seconds * 1_000_000) + offset_us))
            )
        shots.append({"shot": shot, "start_us": start, "end_us": end,
                      "subtitle": shot.source_text.strip(),
                      "alignment_confidence": round(alignment_confidence, 4),
                      "char_boundaries_us": char_boundaries_us,
                      # Shot ranges are expanded below to cover silent gaps for video
                      # replacement. Captions must retain the actual spoken boundaries.
                      "caption_char_boundaries_us": list(char_boundaries_us)})
        expected_cursor = expected_end
    if expected_cursor != len(expected):
        raise RuntimeError("脚本分镜未完整覆盖原文，不能映射到口播时间轴")
    for index in range(len(shots) - 1):
        boundary = (shots[index]["end_us"] + shots[index + 1]["start_us"]) // 2
        shots[index]["end_us"] = boundary
        shots[index + 1]["start_us"] = boundary
    for shot in shots:
        shot["char_boundaries_us"][0] = shot["start_us"]
        shot["char_boundaries_us"][-1] = shot["end_us"]
    return shots


def _normalized_length(value: str) -> int:
    return len(re.sub(r"[^\w\u4e00-\u9fff]", "", value, flags=re.UNICODE))


def _render_units(aligned: list[dict]) -> list[dict]:
    return [{
        **item,
        "unit_id": item["shot"].shot_id,
        "sub_index": 0,
        "sub_count": 1,
        "query_text": item["shot"].source_text.strip(),
    } for item in aligned]


def _candidate_for(shot_info: dict, product_names: list[str]) -> list[dict]:
    shot = shot_info["shot"]
    visual_intent = getattr(shot, "visual_intent", None) or DEFAULT_VISUAL_INTENTS.get(shot.category)
    usage_roles = _ROLE_MAP.get(shot.category)
    if not usage_roles or shot.category == "oral_delivery" or visual_intent == "talking_head":
        return []
    if shot_info.get("alignment_confidence", 1.0) < float(
        os.getenv("SCRIPT_VIDEO_SHOT_ALIGNMENT_MIN", "0.80")
    ):
        return []
    duration = shot_info["end_us"] - shot_info["start_us"]
    local_text = str(shot_info.get("query_text") or shot.source_text).strip()
    required_elements = list(shot.required_elements)
    query_parts = [local_text, shot.evidence, shot.semantic_reason, shot.visual_description]
    query_parts.append(" ".join(required_elements))
    query = " ".join(part for part in query_parts if part)
    visual_intent = media_library.matching_visual_intent(query, visual_intent)
    if visual_intent == "qr_code_visible":
        required_elements = ["二维码或条形码"]
    candidates = media_library.search_clips(
        query=query,
        usage_role=shot.category, product_names=product_names,
        visual_intent=visual_intent,
        duration_us=duration, required_elements=required_elements,
        forbidden_elements=shot.forbidden_elements, limit=40,
    )
    if shot.category == "product_empty_scene" and product_names:
        candidates = [item for item in candidates if item.get("product_identity_match")]
    # Do not map product utterances to unrelated products based only on the generic category.
    distinct = {}
    for item in candidates:
        key = _material_identity(item)
        if key not in distinct or item["match_score"] > distinct[key]["match_score"]:
            distinct[key] = item
    default_threshold = "0.58" if shot.category == "product_empty_scene" else "0.52"
    threshold = float(os.getenv("SCRIPT_VIDEO_MIN_MATCH_SCORE", default_threshold))
    return [item for item in distinct.values() if item["match_score"] >= threshold]


def _material_identity(item: dict) -> tuple[str, str]:
    sha256 = str(item.get("sha256") or "").strip().lower()
    if sha256:
        return "sha256", sha256
    source = str(item.get("source_video_url") or item.get("storage_path") or "").strip().casefold()
    if source:
        return "source", source
    return "asset", str(item.get("asset_id") or "")


def _material_review_target(shot_info: dict, product_names: list[str], request: dict) -> dict:
    shot = shot_info["shot"]
    text = str(shot_info.get("query_text") or shot.source_text).strip()
    visual_intent = getattr(shot, "visual_intent", None) or DEFAULT_VISUAL_INTENTS.get(shot.category)
    query = " ".join(part for part in (
        text, getattr(shot, "evidence", ""), getattr(shot, "semantic_reason", ""),
        getattr(shot, "visual_description", ""),
        " ".join(getattr(shot, "required_elements", []) or []),
    ) if part)
    visual_intent = media_library.matching_visual_intent(query, visual_intent)
    required_elements = list(getattr(shot, "required_elements", []) or [])
    if visual_intent == "qr_code_visible":
        required_elements = ["二维码或条形码"]
    return {
        "shot_id": shot.shot_id,
        "category": shot.category,
        "visual_intent": visual_intent,
        "text": text,
        "semantic_reason": getattr(shot, "semantic_reason", ""),
        "visual_description": getattr(shot, "visual_description", ""),
        "required_elements": required_elements,
        "forbidden_elements": list(getattr(shot, "forbidden_elements", []) or []),
        "product_names": product_names,
        "duration_us": shot_info["end_us"] - shot_info["start_us"],
        "output_width": request["width"],
        "output_height": request["height"],
    }


def _apply_portrait_layout_policy(candidates: dict[str, list[dict]],
                                  request: dict) -> dict[str, list[dict]]:
    """Reject unsafe landscape fills or force them into an enabled split layout."""
    if int(request["height"]) <= int(request["width"]):
        return candidates
    split_enabled = bool(request.get("split_screen_enabled"))
    prepared = {}
    for unit_id, options in candidates.items():
        accepted = []
        for candidate in options:
            source_width = int(candidate.get("width") or 0)
            source_height = int(candidate.get("height") or 0)
            if int(candidate.get("rotation_degrees") or 0) in {90, 270}:
                source_width, source_height = source_height, source_width
            if source_width <= source_height or source_width <= 0 or source_height <= 0:
                accepted.append(candidate)
                continue
            if candidate.get("vertical_fit") == "smart_crop":
                accepted.append(candidate)
                continue
            if split_enabled:
                candidate = dict(candidate)
                candidate["layout_mode"] = "split_screen"
                candidate["vertical_fit"] = "contain_blur"
                accepted.append(candidate)
        prepared[unit_id] = accepted
    return prepared


def _review_candidate_sets(*, task_id: str, render_units: list[dict],
                           candidates: dict[str, list[dict]], product_names: list[str],
                           request: dict, primary: dict, backup: dict | None) -> dict[str, list[dict]]:
    try:
        candidate_limit = int(os.getenv("SCRIPT_VIDEO_MATERIAL_REVIEW_CANDIDATES", "3"))
        concurrency = int(os.getenv("SCRIPT_VIDEO_MATERIAL_REVIEW_CONCURRENCY", "3"))
    except ValueError:
        raise RuntimeError("素材审核并发或候选数量配置不是整数") from None
    if not 1 <= candidate_limit <= 5 or not 1 <= concurrency <= 8:
        raise RuntimeError("素材审核候选数量须为1到5，并发须为1到8")

    source_cache: dict[tuple, Path] = {}
    source_lock = threading.Lock()

    def resolve_source(clip: dict) -> Path:
        key = (
            clip.get("asset_id"), clip.get("source_video_url"),
            clip.get("file_size"), clip.get("sha256"),
        )
        with source_lock:
            source = source_cache.get(key)
            if source is None:
                source = media_library.resolve_local_source(clip) \
                    or _snapshot_remote_source(task_id, clip)
                source_cache[key] = source
            return source

    def verify(item: dict) -> tuple[str, list[dict], list[str]]:
        unit_id = item.get("unit_id") or item["shot"].shot_id
        pool = []
        seen = set()
        for candidate in candidates.get(unit_id) or []:
            identity = _material_identity(candidate)
            if identity in seen:
                continue
            seen.add(identity)
            pool.append(candidate)
        if not pool:
            return unit_id, [], []
        target = _material_review_target(item, product_names, request)
        required = min(len(pool), max(1, int(request["count"])))
        verified_all = []
        warnings_all = []
        for offset in range(0, len(pool), candidate_limit):
            verified, warnings = material_review.review_with_fallback(
                target=target, candidates=pool[offset:offset + candidate_limit],
                primary=primary, backup=backup,
                source_resolver=resolve_source, task_id=task_id,
            )
            verified_all.extend(verified)
            warnings_all.extend(warnings)
            if len(verified_all) >= required:
                break
        return unit_id, verified_all, warnings_all

    reviewed = {item.get("unit_id") or item["shot"].shot_id: [] for item in render_units}
    workers = min(concurrency, max(1, len(render_units)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="material-review") as pool:
        futures = [pool.submit(verify, item) for item in render_units]
        for future in futures:
            try:
                unit_id, verified, warnings = future.result()
            except Exception:
                logger.exception("script_video_batch event=material_review_unit_failed task_id=%s", task_id)
                continue
            reviewed[unit_id] = verified
            for warning in warnings:
                logger.warning(
                    "script_video_batch event=material_review_warning task_id=%s unit_id=%s warning=%s",
                    task_id, unit_id, warning,
                )
    return reviewed


def _load_storyboard(storyboard_id: str):
    board = storyboard_store.get(storyboard_id)
    if board is None:
        raise ValueError(f"脚本分镜不存在: {storyboard_id}")
    if not board.shots:
        raise ValueError("脚本分镜没有镜头，无法生成视频")
    return board


def _allocate(shot_infos: list[dict], candidates: dict[str, list[dict]], count: int) -> list[dict[str, dict]]:
    versions = [dict() for _ in range(count)]
    used_materials: set[tuple[str, str]] = set()
    for info in shot_infos:
        shot = info["shot"]
        unit_id = info.get("unit_id") or shot.shot_id
        options = candidates.get(unit_id) or []
        if not options:
            continue
        ranked = sorted(options, key=lambda item: (
            item["match_score"] - int(item.get("successful_use_count") or 0) * 0.005,
            float((item.get("action_event_match") or {}).get("match_score") or 0),
            item.get("quality_score") or item.get("commercial_score") or 0,
        ), reverse=True)
        for index in range(count):
            choices = [item for item in ranked if _material_identity(item) not in used_materials]
            if not choices:
                continue
            selected = dict(choices[0])
            category = getattr(shot, "category", None)
            selected.update({
                "shot_id": shot.shot_id,
                "category": category,
                "visual_intent": getattr(shot, "visual_intent", None) or DEFAULT_VISUAL_INTENTS.get(category),
                "unit_id": unit_id,
                "sub_index": int(info.get("sub_index") or 0),
                "sub_count": int(info.get("sub_count") or 1),
                "target_text": str(info.get("query_text") or getattr(shot, "source_text", "")).strip(),
                "target_start_us": info["start_us"],
                "target_end_us": info["end_us"],
                "alignment_confidence": info.get("alignment_confidence", 1.0),
            })
            versions[index][unit_id] = selected
            used_materials.add(_material_identity(selected))
    return versions


def _caption_normalize(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", text or "", flags=re.UNICODE).casefold()


def _raw_caption_boundary(text: str, normalized_offset: int) -> int:
    if normalized_offset <= 0:
        return 0
    consumed = 0
    for index, character in enumerate(text):
        consumed += len(_caption_normalize(character))
        if consumed < normalized_offset:
            continue
        boundary = index + 1
        while boundary < len(text) and not _caption_normalize(text[boundary]):
            boundary += 1
        return boundary
    return len(text)


def _display_caption_text(text: str, remove_punctuation: bool) -> str:
    if not remove_punctuation:
        return text.strip()
    return "".join(character for character in text
                   if not unicodedata.category(character).startswith("P")).strip()


def _subtitle_items(aligned: list[dict], provider_segments: list[dict], *,
                    remove_punctuation: bool = True,
                    in_animation: str | None = "打字机 II") -> list[dict]:
    full_text = "".join(str(item.get("subtitle") or "") for item in aligned)
    expected = _caption_normalize(full_text)
    actual = "".join(
        _caption_normalize(str(segment.get("text") or ""))
        for segment in provider_segments
        if isinstance(segment, dict)
    )
    if not expected or actual != expected:
        raise RuntimeError("音频平台断句结果与脚本不一致，不能安全生成字幕")

    provider_boundaries = set()
    cursor = 0
    for segment in provider_segments:
        cursor += len(_caption_normalize(str(segment.get("text") or "")))
        if 0 < cursor < len(expected):
            provider_boundaries.add(cursor)

    captions = []
    global_offset = 0
    for item in aligned:
        text = str(item.get("subtitle") or "")
        if not text or item["end_us"] <= item["start_us"]:
            continue
        normalized_total = _normalized_length(text)
        char_boundaries = (
            item.get("caption_char_boundaries_us")
            or item.get("char_boundaries_us")
            or []
        )
        if len(char_boundaries) != normalized_total + 1:
            raise RuntimeError("缺少词级时间戳，不能映射音频平台断句结果")
        local_boundaries = [0]
        local_boundaries.extend(sorted(
            boundary - global_offset
            for boundary in provider_boundaries
            if global_offset < boundary < global_offset + normalized_total
        ))
        local_boundaries.append(normalized_total)
        for index, (normalized_start, normalized_end) in enumerate(
            zip(local_boundaries, local_boundaries[1:])
        ):
            raw_start = _raw_caption_boundary(text, normalized_start)
            raw_end = _raw_caption_boundary(text, normalized_end)
            start_us = int(char_boundaries[normalized_start])
            end_us = int(char_boundaries[normalized_end])
            if captions and start_us < captions[-1]["end"]:
                start_us = captions[-1]["end"]
            if end_us <= start_us:
                continue
            display_text = _display_caption_text(
                text[raw_start:raw_end], remove_punctuation,
            )
            if not display_text:
                continue
            caption = {"start": start_us, "end": end_us, "text": display_text}
            if in_animation:
                caption["in_animation"] = in_animation
            captions.append(caption)
        global_offset += normalized_total
    return captions


def _write_srt(task_id: str, captions: list[dict]) -> str:
    root = _runtime_root() / task_id
    root.mkdir(parents=True, exist_ok=True)
    target = root / "captions.srt"

    def stamp(seconds: float) -> str:
        milliseconds = max(0, round(seconds * 1000))
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    lines = []
    index = 0
    for item in captions:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        index += 1
        lines.extend((str(index), f"{stamp(item['start'] / 1_000_000)} --> "
                      f"{stamp(item['end'] / 1_000_000)}", text, ""))
    target.write_text("\n".join(lines), encoding="utf-8-sig")
    return f"/openapi/capcut-mate/v1/script-video-batch-tasks/{task_id}/captions.srt"


def _append_master_segment(track: list[dict], segment: dict, category: str) -> None:
    """Keep semantic shots intact; merge only consecutive retained talking-head spans."""
    previous = track[-1] if track else None
    if category == "oral_delivery" and previous \
            and previous.get("_master_category") == "oral_delivery" \
            and previous["video_url"] == segment["video_url"] \
            and previous["end"] == segment["start"] \
            and previous["source_start"] + previous["end"] - previous["start"] == segment["source_start"]:
        previous["end"] = segment["end"]
        return
    segment["_master_category"] = category
    track.append(segment)


def _subtitle_transform_y(request: dict) -> float:
    value = request.get("subtitle_transform_y")
    if value is not None:
        return float(value)
    # Jianying's positive Y moves text upward. -height/2 places the baseline
    # around 75% of the portrait canvas, matching the requested lower position.
    return -float(request["height"]) / 2


def _pip_unit_ids(assignments: dict, task_id: str, version: int, probability: float) -> set[str]:
    """Choose a stable random-looking subset so variants differ but retries stay idempotent."""
    unit_ids = sorted(assignments)
    selected = {
        unit_id for unit_id in unit_ids
        if int(hashlib.sha256(f"{task_id}:{version}:{unit_id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        < probability
    }
    if unit_ids and not selected:
        selected.add(min(unit_ids, key=lambda unit_id: hashlib.sha256(
            f"{task_id}:{version}:{unit_id}".encode()
        ).digest()))
    return selected


def _layout_unit_ids(assignments: dict, task_id: str, version: int,
                     probability: float, split_enabled: bool,
                     pip_enabled: bool) -> tuple[set[str], set[str]]:
    assigned = set(assignments)
    forced_split = {
        unit_id for unit_id, clip in assignments.items()
        if split_enabled and clip.get("layout_mode") == "split_screen"
    }
    pip_units = _pip_unit_ids(
        assignments, task_id, version, probability,
    ) if pip_enabled else set()
    pip_units -= forced_split
    # When both modes are requested and there is enough material, retain at
    # least one unit for each layout instead of turning the whole draft into PIP.
    if split_enabled and len(assigned) > 1 and pip_units == assigned:
        pip_units.remove(max(pip_units))
    split_units = (assigned - pip_units) | forced_split if split_enabled else set()
    return pip_units, split_units


def _pip_mask_unit_ids(pip_units: set[str], task_id: str, version: int,
                       mode: str) -> tuple[set[str], set[str]]:
    if mode == "circle":
        return set(pip_units), set()
    if mode == "mirror":
        return set(), set(pip_units)

    mirror_units = {
        unit_id for unit_id in pip_units
        if int(hashlib.sha256(
            f"{task_id}:{version}:{unit_id}:mask".encode()
        ).hexdigest()[:8], 16) % 2
    }
    circle_units = set(pip_units) - mirror_units
    if len(pip_units) > 1 and not mirror_units:
        selected = min(pip_units, key=lambda unit_id: hashlib.sha256(
            f"{task_id}:{version}:{unit_id}:mirror".encode()
        ).digest())
        mirror_units.add(selected)
        circle_units.remove(selected)
    elif len(pip_units) > 1 and not circle_units:
        selected = min(pip_units, key=lambda unit_id: hashlib.sha256(
            f"{task_id}:{version}:{unit_id}:circle".encode()
        ).digest())
        circle_units.add(selected)
        mirror_units.remove(selected)
    return circle_units, mirror_units


def _pip_transform(width: int, height: int, scale: float) -> tuple[int, int]:
    margin_x = max(24, round(width * 0.04))
    margin_y = max(24, round(height * 0.04))
    # add_videos accepts pixels but normalizes by the full canvas while Jianying
    # stores offsets in half-canvas units, so pass twice the desired center offset.
    return (
        round(width * (1 - scale) - 2 * margin_x),
        round(height * (1 - scale) - 2 * margin_y),
    )


async def _make_draft(master_video: Path, audio_path: Path, aligned: list[dict], captions: list[dict],
                      render_units: list[dict],
                      assignments: dict, request: dict, version: int, task_id: str,
                      *, audio_offset_us: int = 0, background_music: dict | None = None) -> dict:
    master_width, master_height, duration, has_audio = _probe(master_video)
    if audio_path.stat().st_size <= 0:
        raise ValueError("口播音轨文件为空")
    if not duration:
        duration = max(item["end_us"] for item in aligned)
    if not request["keep_master_audio"]:
        raise ValueError("当前版本必须保留口播母版音频")
    split_enabled = bool(request.get("split_screen_enabled"))
    pip_enabled = bool(request.get("pip_enabled"))
    pip_units, split_units = _layout_unit_ids(
        assignments, task_id, version,
        float(request.get("pip_probability", 0.35)), split_enabled, pip_enabled,
    )
    circle_units, mirror_units = _pip_mask_unit_ids(
        pip_units, task_id, version, request.get("pip_mask_mode", "circle"),
    )
    split_layout = _split_panel_layout(
        request["width"], request["height"], request.get("split_ratio", 0.5),
        request.get("split_gap_px", 0),
    ) if split_enabled else None
    mirror_layout = _split_panel_layout(
        request["width"], request["height"], 0.5, 0,
    ) if mirror_units else None
    total_track = []
    top_track = []
    bottom_track = []
    pip_track = []
    mirror_top_track = []
    mirror_bottom_track = []
    shot_visuals = {}
    timeline_gaps = []
    clip_cache: dict[str, Path] = {}
    previous_end = 0
    for item in render_units:
        start, end = item["start_us"], item["end_us"]
        if start > previous_end:
            timeline_gaps.append({"start_us": previous_end, "end_us": start})
            total_track.append({
                "video_url": _public_media_url(master_video), "source_start": previous_end,
                "start": previous_end, "end": start, "duration": duration,
                "width": master_width or request["width"], "height": master_height or request["height"],
                "volume": 0.0,
            })
        unit_id = item.get("unit_id") or item["shot"].shot_id
        clip = assignments.get(unit_id)
        if clip:
            if int(clip.get("source_duration_us") or 0) <= 0:
                raise ValueError(f"素材片段时长不足以覆盖脚本分镜: {item['shot'].shot_id}")
            panel_mode = "split" if unit_id in split_units else (
                "mirror" if unit_id in mirror_units else None
            )
            if panel_mode:
                panel_layout = split_layout if panel_mode == "split" else mirror_layout
                panel_fit_mode = request["split_fit_mode"] if panel_mode == "split" else "contain_blur"
                panel_top_track = top_track if panel_mode == "split" else mirror_top_track
                panel_bottom_track = bottom_track if panel_mode == "split" else mirror_bottom_track
                material_source = media_library.resolve_local_source(clip) or _snapshot_remote_source(task_id, clip)
                top_panel = _export_panel_clip(
                    task_id, master_video, f"master:{master_video}", start, end - start,
                    panel_layout["top_width"], panel_layout["top_height"],
                    panel_fit_mode, clip_cache,
                )
                bottom_panel = _export_panel_clip(
                    task_id, material_source, clip["source_video_url"], clip["clip_start_us"], end - start,
                    panel_layout["bottom_width"], panel_layout["bottom_height"],
                    panel_fit_mode, clip_cache,
                    rotation_degrees=int(clip.get("rotation_degrees") or 0),
                )
                shot_visuals[unit_id] = [
                    {
                        "role": "speaker", "type": "master_video",
                        "video_url": _public_media_url(top_panel),
                        "source_start_us": start, "source_end_us": end,
                        "width": panel_layout["top_width"], "height": panel_layout["top_height"],
                        "transform_y": panel_layout["top_transform_y"], "fit_mode": panel_fit_mode,
                        "mask": "mirror" if panel_mode == "mirror" else None,
                    },
                    {
                        "role": "material", "type": "material_video",
                        "video_url": _public_media_url(bottom_panel),
                        "source_start_us": int(clip["clip_start_us"]),
                        "source_end_us": int(clip["clip_start_us"]) + end - start,
                        "width": panel_layout["bottom_width"], "height": panel_layout["bottom_height"],
                        "transform_y": panel_layout["bottom_transform_y"], "fit_mode": panel_fit_mode,
                        "asset_id": clip.get("asset_id"),
                        "temporal_feature_id": clip.get("temporal_feature_id"),
                    },
                ]
                panel_top_track.append({
                    "video_url": _public_media_url(top_panel), "source_start": 0,
                    "start": start, "end": end, "duration": end - start,
                    "width": panel_layout["top_width"], "height": panel_layout["top_height"],
                    "volume": 0.0,
                })
                panel_bottom_track.append({
                    "video_url": _public_media_url(bottom_panel), "source_start": 0,
                    "start": start, "end": end, "duration": end - start,
                    "width": panel_layout["bottom_width"], "height": panel_layout["bottom_height"],
                    "volume": 0.0,
                })
            else:
                rendered_clip = _export_video_clip(
                    task_id, clip, clip["clip_start_us"], end - start,
                    request["width"], request["height"], clip_cache,
                )
                total_track.append({
                    "video_url": _public_media_url(rendered_clip), "source_start": 0,
                    "start": start, "end": end, "duration": end - start,
                    "width": request["width"], "height": request["height"],
                    "volume": 0.0,
                })
                shot_visuals[unit_id] = [{
                    "role": "main", "type": "material_video",
                    "video_url": _public_media_url(rendered_clip),
                    "source_start_us": int(clip["clip_start_us"]),
                    "source_end_us": int(clip["clip_start_us"]) + end - start,
                    "width": request["width"], "height": request["height"],
                    "fit_mode": clip.get("vertical_fit", "cover"),
                    "focus_x": clip.get("focus_x", 0.5), "focus_y": clip.get("focus_y", 0.5),
                    "asset_id": clip.get("asset_id"),
                    "temporal_feature_id": clip.get("temporal_feature_id"),
                }]
                if unit_id in circle_units:
                    transform_x, transform_y = _pip_transform(
                        request["width"], request["height"], request.get("pip_scale", 0.28),
                    )
                    pip_track.append({
                        "video_url": _public_media_url(master_video), "source_start": start,
                        "start": start, "end": end, "duration": duration,
                        "width": master_width or request["width"],
                        "height": master_height or request["height"], "volume": 0.0,
                    })
                    shot_visuals[unit_id].append({
                        "role": "pip", "type": "master_video",
                        "video_url": _public_media_url(master_video),
                        "source_start_us": start, "source_end_us": end,
                        "width": master_width or request["width"],
                        "height": master_height or request["height"],
                        "scale": request.get("pip_scale", 0.28),
                        "transform_x": transform_x, "transform_y": transform_y,
                        "position": "top_right", "mask": "circle",
                    })
        else:
            _append_master_segment(total_track, {
                "video_url": _public_media_url(master_video), "source_start": start,
                "start": start, "end": end, "duration": duration,
                "width": master_width or request["width"], "height": master_height or request["height"],
                "volume": 0.0,
            }, item["shot"].category)
            shot_visuals[unit_id] = [{
                "role": "main", "type": "master_video",
                "video_url": _public_media_url(master_video),
                "source_start_us": start, "source_end_us": end,
                "width": master_width or request["width"],
                "height": master_height or request["height"],
            }]
        previous_end = end
    if previous_end < duration:
        timeline_gaps.append({"start_us": previous_end, "end_us": duration})
        total_track.append({
            "video_url": _public_media_url(master_video), "source_start": previous_end,
            "start": previous_end, "end": duration, "duration": duration,
            "width": master_width or request["width"], "height": master_height or request["height"],
            "volume": 0.0,
        })
    for segment in total_track:
        segment.pop("_master_category", None)
    audio_duration = duration - audio_offset_us
    if audio_duration <= 0:
        raise ValueError("口播音轨时长无效")
    audio_info = [{"audio_url": _public_media_url(audio_path), "start": audio_offset_us, "end": duration,
                   "duration": audio_duration, "volume": 2.0}]
    applied_background_music = None
    draft_url = create_draft(width=request["width"], height=request["height"])
    draft_id = helper.get_url_param(draft_url, "draft_id")
    try:
        video_segment_ids = []
        if total_track:
            _, _, _, segment_ids, _ = await add_videos_async(draft_url, json.dumps(total_track))
            video_segment_ids.extend(segment_ids)
        if top_track:
            _, _, _, segment_ids, _ = await add_videos_async(
                draft_url, json.dumps(top_track),
                transform_y=split_layout["top_transform_y"],
            )
            video_segment_ids.extend(segment_ids)
        if bottom_track:
            _, _, _, segment_ids, _ = await add_videos_async(
                draft_url, json.dumps(bottom_track),
                transform_y=split_layout["bottom_transform_y"],
            )
            video_segment_ids.extend(segment_ids)
        mirror_segment_ids = []
        if mirror_top_track:
            _, _, _, mirror_segment_ids, _ = await add_videos_async(
                draft_url, json.dumps(mirror_top_track),
                transform_y=mirror_layout["top_transform_y"],
            )
            video_segment_ids.extend(mirror_segment_ids)
            await add_masks_async(
                draft_url, mirror_segment_ids, name="镜面",
                width=mirror_layout["top_width"], height=mirror_layout["top_height"],
                feather=5,
            )
        if mirror_bottom_track:
            _, _, _, segment_ids, _ = await add_videos_async(
                draft_url, json.dumps(mirror_bottom_track),
                transform_y=mirror_layout["bottom_transform_y"],
            )
            video_segment_ids.extend(segment_ids)
        pip_segment_ids = []
        if pip_track:
            transform_x, transform_y = _pip_transform(
                request["width"], request["height"], request.get("pip_scale", 0.28),
            )
            _, _, _, pip_segment_ids, _ = await add_videos_async(
                draft_url, json.dumps(pip_track),
                scale_x=request.get("pip_scale", 0.28),
                scale_y=request.get("pip_scale", 0.28),
                transform_x=transform_x, transform_y=transform_y,
            )
            video_segment_ids.extend(pip_segment_ids)
            await add_masks_async(
                draft_url, pip_segment_ids, name="圆形",
                width=min(master_width or request["width"], master_height or request["height"]),
                height=min(master_width or request["width"], master_height or request["height"]),
            )
        await add_audios_async(draft_url, json.dumps(audio_info))
        if background_music:
            try:
                volume = float(request.get("background_music_volume", 0.0))
                await add_audios_async(draft_url, json.dumps([{
                    "audio_url": background_music["url"], "start": 0, "end": duration,
                    "duration": background_music["duration_us"], "volume": volume,
                }], ensure_ascii=False))
                applied_background_music = {
                    **background_music,
                    "volume": volume,
                }
            except Exception:
                logger.exception(
                    "script_video_batch event=background_music_add_failed task_id=%s version=%s music_id=%s",
                    task_id, version, background_music.get("id"),
                )
        await add_captions_async(
            draft_url, json.dumps(captions, ensure_ascii=False),
            font=request["subtitle_font"], font_size=request["subtitle_font_size"],
            text_color=request["subtitle_text_color"], alignment=1,
            transform_y=_subtitle_transform_y(request),
            bold=request["subtitle_bold"], underline=request["subtitle_underline"],
            italic=request["subtitle_italic"], has_shadow=True,
        )
    except Exception:
        if draft_id:
            DRAFT_CACHE.pop(draft_id, None)
            import config
            shutil.rmtree(Path(config.DRAFT_DIR) / draft_id, ignore_errors=True)
        raise
    if draft_id not in DRAFT_CACHE:
        raise RuntimeError("草稿创建完成后未能通过进程缓存校验")
    assigned_shot_ids = {value["shot_id"] for value in assignments.values()}
    units_per_shot = {}
    assigned_per_shot = {}
    for item in render_units:
        shot_id = item["shot"].shot_id
        units_per_shot[shot_id] = units_per_shot.get(shot_id, 0) + 1
    for value in assignments.values():
        shot_id = value["shot_id"]
        assigned_per_shot[shot_id] = assigned_per_shot.get(shot_id, 0) + 1
    partially_replaced = sum(
        1 for shot_id, assigned_count in assigned_per_shot.items()
        if assigned_count < units_per_shot.get(shot_id, 1)
    )
    active_layouts = [
        name for name, active in (
            ("split_screen", top_track), ("mirror", mirror_top_track), ("pip", pip_track),
        ) if active
    ]
    caption_style = {
        "font": request["subtitle_font"],
        "font_size": request["subtitle_font_size"],
        "color": request["subtitle_text_color"],
        "alignment": 1,
        "transform_x": 0,
        "transform_y": _subtitle_transform_y(request),
        "bold": request["subtitle_bold"],
        "underline": request["subtitle_underline"],
        "italic": request["subtitle_italic"],
        "has_shadow": True,
    }
    detail_shots = []
    for item in aligned:
        shot = item.get("shot") or next((
            unit["shot"] for unit in render_units
            if unit["start_us"] == item["start_us"] and unit["end_us"] == item["end_us"]
        ), None)
        if shot is None:
            continue
        shot_id = shot.shot_id
        start, end = int(item["start_us"]), int(item["end_us"])
        unit_ids = [unit.get("unit_id") or unit["shot"].shot_id
                    for unit in render_units if unit["shot"].shot_id == shot_id]
        material_details = [dict(value) for value in assignments.values()
                            if value.get("shot_id") == shot_id]
        shot_captions = []
        for caption in captions:
            caption_start = max(start, int(caption.get("start") or 0))
            caption_end = min(end, int(caption.get("end") or 0))
            text = str(caption.get("text") or "").strip()
            if caption_end <= caption_start or not text:
                continue
            shot_captions.append({
                "start_us": caption_start, "end_us": caption_end, "text": text,
                "animation": caption.get("in_animation"), "style": caption_style,
            })
        shot_layout = "retained"
        if any(unit_id in split_units for unit_id in unit_ids):
            shot_layout = "split_screen"
        elif any(unit_id in mirror_units for unit_id in unit_ids):
            shot_layout = "mirror"
        elif any(unit_id in circle_units for unit_id in unit_ids):
            shot_layout = "pip_circle"
        elif material_details:
            shot_layout = "replace"
        detail_shots.append({
            "shot_id": shot_id,
            "category": getattr(shot, "category", None),
            "visual_intent": getattr(shot, "visual_intent", None),
            "start_us": start, "end_us": end,
            "script_text": str(getattr(shot, "source_text", "") or item.get("subtitle") or ""),
            "replacement_status": "replaced" if material_details else "retained",
            "layout": shot_layout,
            "visuals": [visual for unit_id in unit_ids for visual in shot_visuals.get(unit_id, [])],
            "captions": shot_captions,
            "materials": [{
                "unit_id": value.get("unit_id"),
                "asset_id": value.get("asset_id"),
                "temporal_feature_id": value.get("temporal_feature_id"),
                "source_start_us": value.get("clip_start_us"),
                "source_end_us": value.get("clip_end_us"),
                "match_score": value.get("match_score"),
                "match_reason": value.get("match_reason"),
            } for value in material_details],
            "stickers": [],
        })
    return {"version": version, "status": "completed", "draft_id": draft_id, "draft_url": draft_url,
            "preview_url": draft_url, "shot_count": len(aligned), "segment_ids": video_segment_ids,
            "layout": {
                "mode": "mixed" if len(active_layouts) > 1 else (
                    active_layouts[0] if active_layouts else "replace"
                ),
                "fit_mode": request.get("split_fit_mode") if top_track else None,
                "ratio": request.get("split_ratio") if top_track else None,
                "gap_px": request.get("split_gap_px") if top_track else None,
                "pip_mask": "mixed" if pip_track and mirror_top_track else (
                    "circle" if pip_track else ("mirror" if mirror_top_track else None)
                ),
                "pip_position": "top_right" if pip_track else None,
                "pip_scale": request.get("pip_scale") if pip_track else None,
                "pip_count": len(pip_track) + len(mirror_top_track),
                "circle_count": len(pip_track),
                "mirror_count": len(mirror_top_track),
                "mirror_height_ratio": 0.5 if mirror_top_track else None,
                "mirror_fit_mode": "contain_blur" if mirror_top_track else None,
            },
            "render_unit_count": len(render_units),
            "replaced_count": len(assigned_shot_ids),
            "replaced_clip_count": len(assignments),
            "partially_replaced_count": partially_replaced,
            "retained_count": len(aligned) - len(assigned_shot_ids),
             "background_music": applied_background_music,
            "materials": [{"shot_id": value["shot_id"], "category": value["category"],
                            "visual_intent": value["visual_intent"], "unit_id": key,
                           "sub_index": value["sub_index"], "sub_count": value["sub_count"],
                           "target_text": value["target_text"], "asset_id": value["asset_id"],
                           "temporal_feature_id": value["temporal_feature_id"],
                           "target_start_us": value["target_start_us"],
                           "target_end_us": value["target_end_us"],
                           "alignment_confidence": value["alignment_confidence"],
                           "source_start_us": value["clip_start_us"], "source_end_us": value["clip_end_us"],
                           "playback_rate": value.get("playback_rate", 1.0),
                            "vertical_fit": value.get("vertical_fit", "cover"),
                            "focus_x": value.get("focus_x", 0.5),
                            "focus_y": value.get("focus_y", 0.5),
                            "model_verification": value.get("model_verification"),
                           "match_score": value["match_score"], "match_reason": value["match_reason"],
                           "pip": key in pip_units,
                           "layout": "split_screen" if key in split_units else (
                               "pip" if key in pip_units else "replace"
                           )}
                          for key, value in assignments.items()],
            "detail": {
                "schema_version": 1,
                "duration_us": duration,
                "width": request["width"], "height": request["height"],
                "timeline_gaps": timeline_gaps,
                "shots": detail_shots,
                "sticker_status": "not_generated",
                "sticker_note": "当前脚本视频流程没有独立贴纸图层生成或提取；压平在源视频中的贴纸不作为独立素材返回。",
            }}


def _run(task_id: str):
    if not task_store.claim(task_id):
        return
    task = task_store.get(task_id)
    request = task["request"]
    versions = [{"version": index + 1, "status": "queued", "draft_id": None, "draft_url": None}
                for index in range(request["count"])]
    try:
        task_store.update(task_id, stage="loading_storyboard", progress=5)
        board = _load_storyboard(request["storyboard_id"])
        script = board.request.script
        product_names = list(board.request.product_names)
        primary_review_model = backup_review_model = None
        primary_model_id = request.get("material_review_primary_model_id")
        backup_model_id = request.get("material_review_backup_model_id")
        if primary_model_id is not None:
            primary_review_model = material_review.resolve_selected(
                int(primary_model_id), request.get("_material_review_primary_snapshot"),
            )
            verification_cache.ensure_table()
        if backup_model_id is not None:
            backup_review_model = material_review.resolve_selected(
                int(backup_model_id), request.get("_material_review_backup_snapshot"),
            )
        task_store.update(task_id, storyboard_id=board.storyboard_id, stage="preparing", progress=10)
        media_library.ensure_catalog_tables()
        media_library.ensure_usage_tables()
        task_store.update(task_id, storyboard_id=board.storyboard_id, stage="transcribing", progress=20)

        source_video = _resolve_media(request.get("source_video_path"), "口播视频", required=request["source_mode"] == "existing_video")
        explicit_audio = _resolve_audio_media(request.get("source_audio_path"), required=False)
        if source_video:
            source_video = _snapshot_media(task_id, source_video, "master-source" + source_video.suffix.lower())
        if explicit_audio:
            explicit_audio = _snapshot_media(task_id, explicit_audio, "source-audio" + explicit_audio.suffix.lower())
        audio_for_alignment = explicit_audio
        if request["source_mode"] == "digital_human":
            if audio_for_alignment is None:
                if not request.get("voice_element_id"):
                    raise ValueError("digital_human 模式需提供 voice_element_id，或传入 source_audio_path")
                task_store.update(task_id, stage="generating_audio", progress=25)
                audio_for_alignment = _generate_audio(task_id, request, script)
        if audio_for_alignment is None:
            if request["source_mode"] == "existing_video":
                offset_us = int(os.getenv("SCRIPT_VIDEO_AUDIO_OFFSET_US", "0"))
                if offset_us < 0:
                    raise RuntimeError("SCRIPT_VIDEO_AUDIO_OFFSET_US 不能小于0")
                if offset_us:
                    if source_video is None:
                        raise ValueError("视频路径缺失，无法应用音频偏移")
                    _, _, source_duration, has_audio = _probe(source_video)
                    if not has_audio or offset_us >= source_duration:
                        raise RuntimeError("SCRIPT_VIDEO_AUDIO_OFFSET_US 超出视频音轨范围")
                    audio_for_alignment = _audio_path_with_offset(task_id, source_video, offset_us)
                else:
                    audio_for_alignment = _audio_path_for(task_id, request, source_video)
            else:
                audio_for_alignment = _audio_path_for(task_id, request, source_video)
        if request["source_mode"] == "digital_human" and audio_for_alignment:
            audio_for_alignment = _snapshot_media(task_id, audio_for_alignment, "avatar-input-audio.wav")
        transcript = _transcribe(audio_for_alignment)
        if source_video is None and request["source_mode"] == "existing_video":
            raise ValueError("existing_video 模式未提供口播视频")
        if request["source_mode"] == "digital_human":
            avatar = _resolve_avatar_media(request.get("avatar_video_path"))
            _, _, avatar_duration, _ = _probe(avatar)
            if not _transcript_matches_script(script, transcript):
                raise RuntimeError("口播转写与脚本不一致，无法安全执行分镜替换")
            if not request.get("duix_api_base_url") or not request.get("duix_shared_dir"):
                raise ValueError("digital_human 模式需要 duix_api_base_url 与 duix_shared_dir")
            if audio_for_alignment.stat().st_size == 0 or avatar_duration <= 0:
                raise ValueError("数字人音频或底片无效")
            task_store.update(task_id, stage="generating_avatar", progress=30)
            source_video = _create_avatar_master(task_id, request, audio_for_alignment)
        width, height, duration, _ = _probe(source_video)
        if duration <= 0:
            raise RuntimeError("口播母版视频时长无效")
        if not _transcript_matches_script(script, transcript):
            raise RuntimeError("口播转写与脚本不一致，无法安全执行分镜替换")
        audio_offset_us = int(os.getenv("SCRIPT_VIDEO_AUDIO_OFFSET_US", "0")) if (
            request["source_mode"] == "existing_video" and explicit_audio is None
        ) else 0
        if request["source_mode"] == "existing_video":
            if explicit_audio is None:
                if audio_offset_us < 0 or audio_offset_us >= duration:
                    raise RuntimeError("SCRIPT_VIDEO_AUDIO_OFFSET_US 超出视频范围")
            else:
                _, _, audio_duration, _ = _probe(explicit_audio)
                if audio_duration < duration:
                    raise RuntimeError("source_audio_path 音频短于口播视频")
        task_store.update(task_id, stage="optimizing_pauses", progress=31)
        source_video, audio_for_alignment, transcript, pause_stats = _optimize_master_pauses(
            task_id, source_video, audio_for_alignment, transcript, duration,
            audio_offset_us, request,
        )
        width, height, duration, _ = _probe(source_video)
        aligned = _align_shots(board, transcript, duration, offset_us=audio_offset_us)
        background_music_by_version = [None] * request["count"]
        if request.get("background_music_enabled", True):
            try:
                background_music_by_version = music_library.select_for_versions(
                    script, board.shots, duration, request["count"],
                )
            except Exception:
                logger.exception(
                    "script_video_batch event=background_music_lookup_failed task_id=%s", task_id,
                )
        task_store.update(task_id, stage="splitting_captions", progress=32)
        provider_segments = audio_generation_manager.split_sentences(script)
        captions = _subtitle_items(
            aligned, provider_segments,
            remove_punctuation=request["subtitle_remove_punctuation"],
            in_animation=request.get("subtitle_in_animation"),
        )
        captions_url = _write_srt(task_id, captions)

        task_store.update(task_id, stage="matching", progress=35)
        render_units = _render_units(aligned)
        candidates = {}
        for item in render_units:
            candidates[item["unit_id"]] = _candidate_for(item, product_names)
        if primary_review_model is not None:
            task_store.update(task_id, stage="verifying_materials", progress=38)
            candidates = _review_candidate_sets(
                task_id=task_id, render_units=render_units, candidates=candidates,
                product_names=product_names, request=request,
                primary=primary_review_model, backup=backup_review_model,
            )
        candidates = _apply_portrait_layout_policy(candidates, request)
        assignments_by_version = _allocate(render_units, candidates, request["count"])
        for version_index, assignments in enumerate(assignments_by_version):
            for unit_id, clip in assignments.items():
                media_library.record_reservation(
                    idempotency_key=f"{task_id}:{version_index + 1}:{unit_id}:{clip['temporal_feature_id']}",
                    batch_task_id=task_id, variant_id=str(version_index + 1),
                    source_scene_id=unit_id, clip=clip,
                )

        completed, failed = 0, 0
        semaphore = asyncio.Semaphore(request["max_drafts_in_parallel"])

        async def build_one(index: int):
            nonlocal completed, failed
            version_id = index + 1
            selected = assignments_by_version[index]
            keys = []
            for unit_id, clip in selected.items():
                key = f"{task_id}:{version_id}:{unit_id}:{clip['temporal_feature_id']}"
                keys.append(key)
            async with semaphore:
                versions[index] = {"version": version_id, "status": "running"}
                task_store.update(task_id, stage="creating_drafts", progress=45 + int(45 * (completed + failed) / request["count"]), versions=versions)
                try:
                    result = await _make_draft(
                        source_video, audio_for_alignment, aligned, captions, render_units, selected,
                        request, version_id, task_id, audio_offset_us=audio_offset_us,
                        background_music=background_music_by_version[index],
                    )
                    result["captions_url"] = captions_url
                    result["detail"]["captions_url"] = captions_url
                    result["pause_optimization"] = pause_stats
                    versions[index] = result
                    for clip, key in zip(selected.values(), keys):
                        media_library.finish_reservation(key, draft_id=result["draft_id"])
                    completed += 1
                except Exception as exc:
                    versions[index] = {"version": version_id, "status": "failed", "error_message": str(exc)[:1000]}
                    for key in keys:
                        media_library.finish_reservation(key, draft_id=None, error=str(exc))
                    failed += 1
                task_store.update(task_id, versions=versions,
                                  progress=45 + int(50 * (completed + failed) / request["count"]))

        task_store.update(task_id, stage="creating_drafts", progress=45, versions=versions)
        async def build_all():
            await asyncio.gather(*(build_one(index) for index in range(request["count"])))

        asyncio.run(build_all())
        status = "completed" if failed == 0 else ("partial_success" if completed else "failed")
        task_store.update(task_id, status=status, stage=status, progress=100,
                          error_message="" if failed == 0 else f"{failed} 条草稿创建失败",
                          completed_at=_now(), versions=versions)
    except Exception as exc:
        try:
            media_library.release_batch(task_id, str(exc))
        except Exception:
            logger.exception("script_video_batch event=reservation_release_failed task_id=%s", task_id)
        for version in versions:
            if version.get("status") in ("queued", "running"):
                version.update(status="failed", error_message=str(exc)[:1000])
        task_store.update(task_id, status="failed", stage="failed", progress=100,
                          error_message=str(exc)[:2000], completed_at=_now(), versions=versions)
        logger.exception("script_video_batch event=failed task_id=%s", task_id)

