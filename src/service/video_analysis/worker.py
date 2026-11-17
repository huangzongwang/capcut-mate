"""Invoked by the API using the isolated PySceneDetect Python environment."""

import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class DetectionFailure(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_video(url, destination, max_bytes):
    # Hosts are explicitly trusted by the API; no redirects or environment proxies.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(Request(url, headers={"User-Agent": "CapCut-Mate-SceneDetection"}), timeout=20) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise DetectionFailure(413, "Video exceeds the configured file size limit")
            total = 0
            with destination.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise DetectionFailure(413, "Video exceeds the configured file size limit")
                    output.write(chunk)
    except (HTTPError, URLError, OSError, ValueError) as exc:
        raise DetectionFailure(400, "Video download failed; redirects are not supported") from exc


def analyze_video(path, data, collected_signals=None):
    import cv2
    from scenedetect import AdaptiveDetector, ContentDetector, FrameTimecode, SceneManager, open_video

    cv2.setNumThreads(1)
    if path.stat().st_size > data["max_bytes"]:
        raise DetectionFailure(413, "Video exceeds the configured file size limit")
    video = open_video(str(path), backend="opencv")
    fps = video.frame_rate
    if not math.isfinite(fps) or fps <= 0:
        raise DetectionFailure(400, "Video has no valid frame rate")
    if video.duration.seconds > data["max_duration_seconds"]:
        raise DetectionFailure(413, "Video exceeds the configured duration limit")
    width, height = video.frame_size
    if width * height > 3840 * 2160:
        raise DetectionFailure(413, "Video resolution exceeds 3840x2160 pixels")
    max_frames = int(data["max_duration_seconds"] * fps)
    diagnostics = []
    auto_settings = {}
    if data["detector"] in ("enhanced", "auto"):
        from enhanced import collect_signals, select_boundaries

        signals = (collect_signals(video, max_frames, retain_full=True)
                   if data["detector"] == "auto" else collect_signals(video, max_frames))
        if collected_signals is not None:
            collected_signals.update(signals)
        decoded = signals["decoded"]
        if data["detector"] == "auto":
            from auto_detection import select_auto_boundaries
            cuts, decisions, auto_settings = select_auto_boundaries(signals, data)
        else:
            cuts, decisions = select_boundaries(signals, data)
        boundaries = [0, *cuts, decoded]
        ranges = [(FrameTimecode(start, fps), FrameTimecode(end, fps))
                  for start, end in zip(boundaries, boundaries[1:])]
        if data.get("include_diagnostics") or data["detector"] == "auto":
            diagnostics = decisions
    else:
        manager = SceneManager()
        if data["detector"] == "adaptive":
            detector = AdaptiveDetector(
                adaptive_threshold=data["threshold"], min_scene_len=data["min_scene_len"],
                min_content_val=data.get("min_content_val", 15.0),
                window_width=data.get("window_width", 2),
            )
        else:
            detector = ContentDetector(threshold=data["threshold"], min_scene_len=data["min_scene_len"])
        manager.add_detector(detector)
        decoded = manager.detect_scenes(video=video, end_time=max_frames + 1)
        ranges = manager.get_scene_list(start_in_scene=True)
    if decoded > max_frames:
        raise DetectionFailure(413, "Video exceeds the configured duration limit")
    if decoded <= 0:
        raise DetectionFailure(400, "Video contains no decodable frames")
    expected = video.duration.frame_num
    if expected and abs(expected - decoded) > 1:
        raise DetectionFailure(400, "Decoded frame count differs from metadata; normalize or repair the video first")
    scenes = []
    for index, (start, end) in enumerate(ranges, 1):
        start_us = round(start.seconds * 1_000_000)
        end_us = round(end.seconds * 1_000_000)
        scenes.append({
            "index": index,
            "start_frame": start.frame_num, "end_frame": end.frame_num,
            "start_us": start_us, "end_us": end_us, "duration_us": end_us - start_us,
            "start_timecode": start.get_timecode(), "end_timecode": end.get_timecode(),
        })
    if not scenes:
        raise DetectionFailure(400, "No scene ranges could be read")
    return {
        "detector": data["detector"], "threshold": data["threshold"],
        "min_scene_len": data["min_scene_len"], "fps": float(fps),
        "frame_count": decoded, "duration_us": scenes[-1]["end_us"],
        "scene_count": len(scenes), "scenes": scenes,
        "settings": {
            **auto_settings,
            **{
            key: data[key] for key in [
                "min_content_val", "window_width", "score_region", "persistence_ratio", "refine_seconds"
            ] if key in data and (
                data["detector"] in ("enhanced", "auto")
                or (data["detector"] == "adaptive" and key in ("min_content_val", "window_width"))
            )}
        },
        "diagnostics": diagnostics,
        "warnings": [
            "Times use the reported frame rate. Normalize variable-frame-rate videos before frame-accurate editing.",
            "Shot boundaries are not semantic labels or permission to replace footage.",
        ] + (["Enhanced is an experimental visual heuristic, not a semantic model."] if data["detector"] == "enhanced" else []),
    }


def transcribe_video(path, data):
    from transcription import map_segments_to_scenes, to_srt, transcribe

    detection = data.get("detection_override") or analyze_video(path, data)
    result = transcribe(
        path, model_name=data.get("model_name", "large-v3"),
        language=data.get("language", "zh"),
        word_timestamps=data.get("word_timestamps", True),
        vad_filter=data.get("vad_filter", True),
    )
    result["scenes"] = map_segments_to_scenes(result["segments"], detection["scenes"])
    status = result.get("status", "completed")
    for scene in result["scenes"]:
        scene.update(
            transcription_status=status,
            transcription_error=result.get("error", ""),
            transcription_seconds=result.get("timings", {}).get("total_seconds", 0.0),
            retry_count=result.get("retry_count", 0),
        )
    result["detection"] = detection
    result["duration_seconds"] = detection["duration_us"] / 1_000_000
    result["transcription_status"] = status
    result["warnings"] = [
        "Transcription is local faster-whisper output; review names, numbers and product claims before publishing.",
        "Scene alignment uses time overlap between speech segments and detected visual scene ranges.",
    ]
    if status == "no_audio":
        result["warnings"].append("源视频没有音轨，已跳过字幕转写。")
    elif status == "failed":
        result["warnings"].append(
            f"字幕转写失败，未影响镜头检测: {result.get('error', 'unknown error')}"
        )
    elif result.get("retry_count"):
        result["warnings"].append("原始音频解码失败，已使用 FFmpeg 提取音频后重试成功。")
    result["srt"] = to_srt(result["segments"])
    return result


def transcribe_scene_clips(data):
    from transcription import transcribe_clips

    paths = [Path(item) for item in data.get("scene_clip_paths", [])]
    if not paths or any(not path.is_file() for path in paths):
        raise DetectionFailure(400, "Scene clip is missing")
    return {
        "clips": transcribe_clips(
            paths,
            model_name=data.get("model_name", "large-v3"),
            language=data.get("language", "zh"),
            word_timestamps=data.get("word_timestamps", True),
            vad_filter=data.get("vad_filter", True),
        )
    }


def extract_features(path, data):
    import base64
    import cv2
    ocr_engine = None
    ocr_init_error = None
    ocr_errors = []
    if data.get("include_ocr"):
        try:
            from ocr import PaddleOCRAdapter
            ocr_engine = PaddleOCRAdapter()
        except Exception as exc:
            ocr_init_error = f"OCR 引擎初始化失败: {exc}"

    detection = analyze_video(path, data)
    video = cv2.VideoCapture(str(path))
    if not video.isOpened():
        raise DetectionFailure(400, "Video could not be opened for feature extraction")
    fps = float(detection["fps"])
    features = []
    try:
        for scene in detection["scenes"]:
            start = scene["start_frame"]
            end = max(start + 1, scene["end_frame"] - 1)
            sample_points = data.get("sample_points", "auto")
            if sample_points == "auto":
                # Short shots rarely benefit from boundary samples; long shots
                # get start/middle/end coverage for OCR and semantic analysis.
                sample_points = 1 if (scene["end_us"] - scene["start_us"]) < 2_000_000 else 3
            points = [start + (end - start) // 2] if sample_points == 1 else [
                start, start + (end - start) // 2, end
            ]
            values = []
            images = []
            ocr_results = []
            previous_gray = None
            for frame_no in points:
                video.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
                ok, frame = video.read()
                if not ok:
                    continue
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                edges = cv2.Canny(gray, 100, 200)
                motion = 0.0 if previous_gray is None else float(
                    cv2.absdiff(gray, previous_gray).mean() / 255.0
                )
                previous_gray = gray
                values.append({
                    "brightness": float(gray.mean() / 255.0),
                    "saturation": float(hsv[:, :, 1].mean() / 255.0),
                    "edge_density": float((edges > 0).mean()),
                    "motion_score": motion,
                })
                if data.get("include_sample_images"):
                    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    if ok:
                        images.append("data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii"))
                if ocr_engine is not None:
                    try:
                        ocr_results.append(ocr_engine.recognize(frame))
                    except Exception as exc:
                        ocr_results.append([])
                        ocr_errors.append(
                            f"分镜 {scene['index']} 帧 {frame_no} OCR 失败: {exc}"
                        )
            if not values:
                raise DetectionFailure(400, f"Could not decode scene {scene['index']}")
            ocr_items = (
                __import__("ocr", fromlist=["aggregate_text"]).aggregate_text(
                    ocr_results, round(points[len(points) // 2] / fps * 1_000_000)
                ) if ocr_engine is not None else []
            )
            if not data.get("include_ocr"):
                ocr_status = "disabled"
            elif ocr_engine is None:
                ocr_status = "unavailable"
            elif ocr_errors and not ocr_items:
                ocr_status = "partial"
            elif not ocr_items:
                ocr_status = "completed_no_text"
            else:
                ocr_status = "partial" if ocr_errors else "completed"
            scene_warnings = ([ocr_init_error] if ocr_init_error else []) + [
                error for error in ocr_errors if error.startswith(f"分镜 {scene['index']} ")
            ]
            features.append({
                "scene_index": scene["index"],
                "start_us": scene["start_us"],
                "end_us": scene["end_us"],
                "sample_timestamps_us": [
                    round(frame_no / fps * 1_000_000) for frame_no in points
                ],
                "brightness": round(sum(item["brightness"] for item in values) / len(values), 6),
                "saturation": round(sum(item["saturation"] for item in values) / len(values), 6),
                "edge_density": round(sum(item["edge_density"] for item in values) / len(values), 6),
                "motion_score": round(sum(item["motion_score"] for item in values) / len(values), 6),
                "sample_images": images,
                "ocr_items": ocr_items,
                "ocr_status": ocr_status,
                "ocr_warnings": scene_warnings,
            })
    finally:
        video.release()
    ocr_status = "disabled"
    if data.get("include_ocr"):
        if ocr_engine is None:
            ocr_status = "unavailable"
        elif ocr_errors:
            ocr_status = "partial"
        elif any(scene["ocr_items"] for scene in features):
            ocr_status = "completed"
        else:
            ocr_status = "completed_no_text"
    warnings = [
        "当前返回的是本地视觉统计特征，不是语义识别结果。",
        "模型配置已可通过模型配置接口管理，后续可在此结果上接入多模态分析。",
    ]
    if ocr_init_error:
        warnings.append(ocr_init_error)
    if ocr_errors:
        warnings.append(f"OCR 有 {len(ocr_errors)} 个采样帧失败，已保留其他成功识别结果。")
    # Mark text that repeats across many scenes as likely fixed packaging text,
    # watermark, or disclaimer without deleting it from the OCR result.
    from collections import Counter
    repeated = Counter(
        item["text"] for scene in features for item in scene["ocr_items"]
    )
    for scene in features:
        for item in scene["ocr_items"]:
            if repeated[item["text"]] >= max(3, len(features) // 2):
                item["text_type"] = "likely_fixed_text"
            else:
                item["text_type"] = "dynamic_or_scene_text"
        scene["fixed_text_items"] = [
            item for item in scene["ocr_items"]
            if item["text_type"] == "likely_fixed_text"
        ]
        scene["subtitle_candidates"] = [
            item for item in scene["ocr_items"]
            if item["text_type"] == "dynamic_or_scene_text"
            and len(item.get("text", "").strip()) >= 2
        ]
    return {
        "detection": detection,
        "scenes": features,
        "ocr_status": ocr_status,
        "warnings": warnings,
    }


def extract_overlay_text(path, data):
    """Read stable, screen-fixed OCR text only inside caller-supplied ranges."""
    import cv2
    from difflib import SequenceMatcher

    try:
        from ocr import PaddleOCRAdapter

        engine = PaddleOCRAdapter()
    except Exception as exc:
        return {"status": "unavailable", "events": [], "warnings": [f"OCR 初始化失败: {exc}"]}

    video = cv2.VideoCapture(str(path))
    if not video.isOpened():
        raise DetectionFailure(400, "Video could not be opened for overlay OCR")
    fps = float(video.get(cv2.CAP_PROP_FPS) or 0)
    if fps <= 0:
        video.release()
        raise DetectionFailure(400, "Video has no valid frame rate for overlay OCR")
    width = int(video.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(video.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    interval = max(200_000, int(data.get("sample_interval_us", 500_000)))
    max_frames = max(1, int(data.get("max_ocr_frames", 1200)))
    events = []
    warnings = []
    frame_count = 0

    def box_for(item):
        points = item.get("polygon") or []
        if not points:
            return None
        xs = [float(point[0]) for point in points if len(point) >= 2]
        ys = [float(point[1]) for point in points if len(point) >= 2]
        if not xs or not ys:
            return None
        left, top = max(0.0, min(xs)), max(0.0, min(ys))
        right, bottom = min(float(width), max(xs)), min(float(height), max(ys))
        if right <= left or bottom <= top:
            return None
        return [left / width, top / height, right / width, bottom / height]

    def overlap(first, second):
        left = max(first[0], second[0])
        top = max(first[1], second[1])
        right = min(first[2], second[2])
        bottom = min(first[3], second[3])
        intersection = max(0.0, right - left) * max(0.0, bottom - top)
        area_first = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
        area_second = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
        union = area_first + area_second - intersection
        return intersection / union if union else 0.0

    try:
        for scene in data.get("scene_ranges", []):
            start_us, end_us = int(scene["start_us"]), int(scene["end_us"])
            tracked = []
            for timestamp_us in range(start_us, end_us, interval):
                if frame_count >= max_frames:
                    warnings.append("OCR 达到单任务采样上限，后续画面文字未扫描。")
                    break
                frame_count += 1
                frame_number = round(timestamp_us * fps / 1_000_000)
                video.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
                ok, frame = video.read()
                if not ok:
                    warnings.append(f"分镜 {scene['scene_index']} OCR 帧读取失败。")
                    continue
                for item in engine.recognize(frame):
                    box = box_for(item)
                    text = str(item.get("text") or "").strip()
                    if not box or not text:
                        continue
                    current = next((candidate for candidate in tracked
                                    if timestamp_us - candidate["last_us"] <= interval * 2
                                    and overlap(candidate["box"], box) >= 0.25
                                    and SequenceMatcher(None, candidate["text"], text).ratio() >= 0.82), None)
                    if current is None:
                        tracked.append({
                            "text": text, "box": box, "first_us": timestamp_us,
                            "last_us": timestamp_us, "boxes": [box],
                            "scores": [float(item.get("confidence") or 0)],
                        })
                    else:
                        current["last_us"] = timestamp_us
                        current["boxes"].append(box)
                        current["scores"].append(float(item.get("confidence") or 0))
                        if float(item.get("confidence") or 0) > max(current["scores"][:-1]):
                            current["text"] = text
                if frame_count >= max_frames:
                    break

            for item in tracked:
                observations = len(item["boxes"])
                confidence = sum(item["scores"]) / len(item["scores"])
                centers_x = [(box[0] + box[2]) / 2 for box in item["boxes"]]
                centers_y = [(box[1] + box[3]) / 2 for box in item["boxes"]]
                stable = (
                    max(centers_x) - min(centers_x) <= 0.025
                    and max(centers_y) - min(centers_y) <= 0.025
                )
                representative = item["boxes"][len(item["boxes"]) // 2]
                center_y = (representative[1] + representative[3]) / 2
                box_width = representative[2] - representative[0]
                anchored = center_y <= 0.25 or center_y >= 0.72 or box_width >= 0.35
                if not stable or not anchored or (observations < 2 and confidence < 0.9):
                    continue
                events.append({
                    "scene_index": int(scene["scene_index"]),
                    "text": item["text"],
                    "start_us": max(start_us, item["first_us"] - interval // 2),
                    "end_us": min(end_us, item["last_us"] + interval // 2),
                    "box": [
                        sum(box[index] for box in item["boxes"]) / observations
                        for index in range(4)
                    ],
                    "confidence": round(confidence, 4),
                    "observations": observations,
                })
    finally:
        video.release()
    return {"status": "completed", "events": events, "warnings": warnings}


def main():
    # Serialize local media workers; queued analysis jobs may wait for the lock.
    lock_path = Path(tempfile.gettempdir()) / "capcut-mate-scenedetect.lock"
    with lock_path.open("a+b") as lock:
        data = json.load(sys.stdin)
        lock_deadline = time.monotonic() + max(0, int(data.get("worker_lock_wait_seconds", 0)))
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    if lock_path.stat().st_size == 0:
                        lock.write(b"0")
                        lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= lock_deadline:
                    raise DetectionFailure(409, "Another scene detection is running; retry later") from exc
                time.sleep(min(0.5, max(0, lock_deadline - time.monotonic())))
        if data["video_url"]:
            path = Path(data["work_directory"]) / "input.video"
            download_video(data["video_url"], path, data["max_bytes"])
        else:
            path = Path(data["video_path"])
        operation = data.get("operation")
        if operation == "prepare_analysis":
            from splitting import SplitFailure, split_video

            if not data.get("use_external_model"):
                return {"detection": analyze_video(path, data), "clips": [], "warnings": []}
            signals = {}
            detection = analyze_video(path, data, signals)
            data.update(
                operation="split",
                output_directory=data["work_directory"],
                job_id="analysis",
                max_output_bytes=1024 * 1024 * 1024,
                boundary_policy="allow_review",
                encoding="compatible",
                encoder="auto",
            )
            try:
                result = split_video(path, data, detection, signals)
            except SplitFailure as exc:
                raise DetectionFailure(exc.status, str(exc)) from exc
            if result.get("status") != "completed":
                raise DetectionFailure(409, "Scene boundary review did not produce analysis clips")
            clips = [
                {"scene_index": clip["index"],
                 "path": str(Path(data["work_directory"]) / clip["filename"])}
                for clip in result["clips"]
            ]
            return {
                "detection": result["detection"] if data["detector"] == "auto" else detection,
                "clips": clips,
                "warnings": result.get("warnings", []),
                "boundary_reviews": result.get("boundary_reviews", []),
            }
        if operation == "transcribe":
            return transcribe_video(path, data)
        if operation == "transcribe_scene_clips":
            return transcribe_scene_clips(data)
        if operation == "extract_features":
            return extract_features(path, data)
        if operation == "extract_overlay_text":
            return extract_overlay_text(path, data)
        signals = {} if operation == "split" else None
        result = analyze_video(path, data, signals)
        if signals is not None:
            from splitting import SplitFailure, split_video

            try:
                return split_video(path, data, result, signals)
            except SplitFailure as exc:
                raise DetectionFailure(exc.status, str(exc)) from exc
        return result


if __name__ == "__main__":
    try:
        print(json.dumps(main()))
    except DetectionFailure as exc:
        print(json.dumps({"status": exc.status, "error": str(exc)}))
        sys.exit(1)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({
            "status": getattr(exc, "status", 400),
            "error": str(exc) if type(exc).__name__ in ("SplitFailure", "TranscriptionRuntimeError")
            else "Video could not be decoded or analyzed",
        }))
        sys.exit(1)
