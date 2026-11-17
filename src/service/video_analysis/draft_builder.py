"""Build a Jianying/CapCut draft from a completed video-analysis task."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

import config
from src.service.create_draft import create_draft
from src.service.add_videos import add_videos_async
from src.service.gen_video import gen_video
from src.service.video_analysis import analysis_task_store
from src.utils.logger import logger
from src.utils.download import download
from src.service.video_analysis.platform_runtime import (
    analysis_temp_root, ensure_writable_directory, media_root, scene_draft_root, media_tool,
    video_analysis_report_root,
)


def _export_scene_clips(source: Path, scenes: list[dict], task_id: str, *, frame_exact=False) -> list[dict]:
    """Export independent H.264/AAC clips for analysis-draft materials."""
    root = ensure_writable_directory(scene_draft_root())
    output = root / task_id
    output.mkdir(parents=True, exist_ok=True)
    base_url = os.getenv(
        "DRAFT_SCENE_CLIP_BASE_URL",
        "http://127.0.0.1:30001/output/analysis-clips" if os.name == "nt" else "http://nginx/output/analysis-clips",
    ).rstrip("/")
    public_base_url = os.getenv(
        "DRAFT_SCENE_CLIP_PUBLIC_BASE_URL",
        "http://127.0.0.1:30001/output/analysis-clips" if os.name == "nt" else "/output/analysis-clips",
    ).rstrip("/")
    clips = []
    for scene in scenes:
        index = int(scene["index"])
        start_us = max(0, int(scene["start_us"]))
        end_us = max(start_us, int(scene["end_us"]))
        duration_us = end_us - start_us
        target = output / f"scene-{index:03d}.mp4"
        command = [
            media_tool("ffmpeg"), "-y", "-v", "error",
            "-i", str(source),
            "-ss", f"{start_us / 1_000_000:.6f}",
            "-t", f"{duration_us / 1_000_000:.6f}",
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(target),
        ]
        if frame_exact:
            # Auto's finalized source frames are authoritative; old exports keep
            # their existing timestamp-based behavior.
            command = [
                media_tool("ffmpeg"), "-y", "-v", "error", "-i", str(source),
                "-vf", f"trim=start_frame={scene['start_frame']}:end_frame={scene['end_frame']},setpts=PTS-STARTPTS",
                "-af", f"atrim=start={start_us / 1_000_000:.6f}:end={end_us / 1_000_000:.6f},asetpts=PTS-STARTPTS",
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-movflags", "+faststart", str(target),
            ]
        subprocess.run(command, check=True, timeout=900)
        clips.append({
            "scene_index": index,
            "path": target,
            "video_url": f"{base_url}/{task_id}/scene-{index:03d}.mp4",
            "public_url": f"{public_base_url}/{task_id}/scene-{index:03d}.mp4",
            "start_us": start_us,
            "end_us": end_us,
            "duration_us": duration_us,
        })
    return clips


def _probe(path: Path) -> tuple[int, int, int]:
    """Return width, height and duration in microseconds."""
    command = [
        media_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
    payload = json.loads(result.stdout)
    stream = (payload.get("streams") or [{}])[0]
    duration = float((payload.get("format") or {}).get("duration") or 0)
    return int(stream.get("width") or 0), int(stream.get("height") or 0), round(duration * 1_000_000)


def _source_for_task(request: dict) -> tuple[str, Path | None]:
    """Map analysis input to a URL usable by the draft media downloader."""
    video_path = request.get("video_path")
    if video_path:
        root = media_root()
        path = (root / video_path).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            raise ValueError("分析源视频不存在或不在 SCENE_MEDIA_ROOT 内")
        # The analysis media sidecar exposes this root at /media.
        base = os.getenv(
            "DRAFT_MEDIA_BASE_URL",
            "http://127.0.0.1:30001/media" if os.name == "nt" else "http://nginx/media",
        ).rstrip("/")
        relative = path.relative_to(root).as_posix()
        return f"{base}/{quote(relative, safe='/')}", path
    video_url = request.get("video_url")
    if not video_url:
        raise ValueError("分析任务没有可用的视频源")
    return video_url, None


def _status_url() -> str:
    return config.DRAFT_URL.rsplit("/", 1)[0] + "/gen_video_status"


async def _create_draft_from_analysis(
    task_id: str,
    *,
    width: int = 1080,
    height: int = 1920,
    render_preview: bool = False,
    api_key: str | None = None,
    include_unreplaceable: bool = True,
    embed_scene_clips: bool = True,
    temporary_sources: list[Path] | None = None,
) -> dict:
    task = analysis_task_store.get_task(task_id)
    if not task:
        raise ValueError("分析任务不存在")
    if task["status"] not in ("completed", "completed_with_warnings") and not (
        task["status"] == "running" and task["stage"] == "creating_draft"
    ):
        raise ValueError("分析任务尚未完成")
    result_path = Path(task["result_path"]).resolve()
    root = video_analysis_report_root().resolve()
    if not result_path.is_relative_to(root) or not result_path.is_file():
        raise ValueError("分析结果文件不存在")
    report = json.loads(result_path.read_text(encoding="utf-8"))
    request = json.loads(task["request_json"])
    source_url, local_path = _source_for_task(request)
    detection = report.get("detection") or {}
    scenes = detection.get("scenes") or []
    if not scenes:
        raise ValueError("分析结果没有分镜")

    source_width = source_height = duration_us = 0
    if local_path:
        source_width, source_height, duration_us = _probe(local_path)
    duration_us = duration_us or int(detection.get("duration_us") or 0)
    if not duration_us:
        duration_us = max(int(scene["end_us"]) for scene in scenes)

    draft_url = create_draft(width=width, height=height)
    embedded_clips = []
    source_for_export = local_path
    temporary_source = None
    if embed_scene_clips and source_for_export is None:
        temporary_root = ensure_writable_directory(analysis_temp_root())
        temporary_source = Path(tempfile.mkdtemp(prefix=f"{task_id}-source-", dir=str(temporary_root)))
        if temporary_sources is not None:
            temporary_sources.append(temporary_source)
        source_for_export = Path(download(url=source_url, save_dir=str(temporary_source)))
    if embed_scene_clips and source_for_export is not None:
        if detection.get("detector") == "auto":
            embedded_clips = _export_scene_clips(source_for_export, scenes, task_id, frame_exact=True)
        else:
            embedded_clips = _export_scene_clips(source_for_export, scenes, task_id)
    video_infos = []
    for scene_index, scene in enumerate(scenes):
        start_us = max(0, int(scene["start_us"]))
        end_us = min(duration_us, int(scene["end_us"]))
        if end_us <= start_us:
            continue
        clip = embedded_clips[scene_index] if embedded_clips else None
        video_infos.append({
            "video_url": clip["video_url"] if clip else source_url,
            "source_start": 0 if clip else start_us,
            "start": start_us,
            "end": end_us,
            "duration": clip["duration_us"] if clip else duration_us,
            "width": source_width or width,
            "height": source_height or height,
            "volume": 1.0,
        })
    if not video_infos:
        raise ValueError("没有有效的分镜时间段")

    _, track_id, _, segment_ids, segment_infos = await add_videos_async(
        draft_url=draft_url, video_infos=json.dumps(video_infos),
    )
    draft_id = draft_url.rsplit("draft_id=", 1)[-1]
    output = {
        "task_id": task_id,
        "draft_id": draft_id,
        "draft_url": draft_url,
        "scene_count": len(video_infos),
        "track_id": track_id,
        "segment_ids": segment_ids,
        "segment_infos": [
            {
                **item.model_dump(),
                "material_url": (
                    embedded_clips[index]["public_url"]
                    if embedded_clips and index < len(embedded_clips)
                    else item.material_url
                ),
            }
            for index, item in enumerate(segment_infos)
        ],
        "source_video_url": source_url,
        "media_mode": "embedded_scene_clips" if embedded_clips else "source_timerange",
        "media_note": (
            "每个分镜使用独立 MP4 素材，source_timerange 从 0 开始。"
            if embedded_clips else
            "每个分镜通过草稿 source_timerange 裁剪；素材链接指向完整源视频。"
        ),
        "scene_clips": [
            {
                "scene_index": clip["scene_index"],
                "clip_url": clip["public_url"],
                "start_us": clip["start_us"],
                "end_us": clip["end_us"],
                "duration_us": clip["duration_us"],
            }
            for clip in embedded_clips
        ],
        "preview_url": "",
        "preview_status_url": _status_url(),
        "render_submitted": False,
        "render_message": "",
        "warnings": [],
    }
    if not include_unreplaceable:
        output["warnings"].append("当前版本保留原始分镜，include_unreplaceable 仅作为兼容参数。")
    if render_preview:
        try:
            output["render_message"] = gen_video(draft_url, api_key)
            output["render_submitted"] = True
        except Exception as exc:
            output["warnings"].append(f"云渲染未提交: {exc}")
            logger.warning("analysis draft render submit failed task_id=%s error=%s", task_id, exc)
    return output


async def create_draft_from_analysis(
    task_id: str,
    *,
    width: int = 1080,
    height: int = 1920,
    render_preview: bool = False,
    api_key: str | None = None,
    include_unreplaceable: bool = True,
    embed_scene_clips: bool = True,
) -> dict:
    temporary_sources: list[Path] = []
    try:
        return await _create_draft_from_analysis(
            task_id,
            width=width,
            height=height,
            render_preview=render_preview,
            api_key=api_key,
            include_unreplaceable=include_unreplaceable,
            embed_scene_clips=embed_scene_clips,
            temporary_sources=temporary_sources,
        )
    finally:
        import shutil
        for path in temporary_sources:
            shutil.rmtree(path, ignore_errors=True)
