"""Create independent replacement drafts from a completed analysis report."""
from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import config
from src.service.add_audios import add_audios_async
from src.service.add_captions import add_captions_async
from src.service.add_videos import add_videos_async
from src.service.create_draft import create_draft
from src.service.video_analysis import analysis_task_store
from src.service.video_analysis.draft_builder import _probe, _source_for_task
from src.service.video_analysis.platform_runtime import (
    analysis_temporary_directory, ensure_writable_directory, media_root, media_tool,
    video_analysis_setting,
    video_analysis_report_root,
)
from src.service.video_analysis.script_video_batch import media_library, material_review, vertical_renderer
from src.service.video_analysis.draft_replacement import replace_draft_segment
from src.schemas.video_analysis_draft import ReplaceDraftSegmentRequest
import src.pyJianYingDraft as draft
from src.utils.logger import logger
from src.utils.draft_cache import DRAFT_CACHE

from . import store
from .schemas import CreateReplacementDraftsRequest


_active_lock = threading.RLock()
_active: set[str] = set()
_queue: queue.Queue[str] = queue.Queue()
_queued: set[str] = set()
_started = False
_start_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_MATERIAL_REVIEW_SETTING = "VIDEO_ANALYSIS_REPLACEMENT_MATERIAL_REVIEW_ENABLED"


def _material_review_enabled() -> bool:
    value = video_analysis_setting(_MATERIAL_REVIEW_SETTING, "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _public_task(task: dict) -> dict:
    versions = task.get("versions") or []
    finished = {"completed", "completed_with_warnings"}
    return {
        "task_id": task["task_id"],
        "source_analysis_task_id": task.get("source_analysis_task_id", ""),
        "status": task["status"], "stage": task["stage"], "progress": task["progress"],
        "count": int((task.get("request") or {}).get("count", len(versions))),
        "completed_count": sum(item.get("status") in finished for item in versions),
        "failed_count": sum(item.get("status") == "failed" for item in versions),
        "skipped_count": sum(item.get("status") == "skipped" for item in versions),
        "deleted_count": sum(item.get("status") == "deleted" for item in versions),
        "created_at": task["created_at"], "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"), "error_message": task.get("error_message", ""),
        "versions": versions,
    }


def submit(analysis_task_id: str, request: CreateReplacementDraftsRequest) -> dict:
    source = analysis_task_store.get_task(analysis_task_id)
    if not source:
        raise ValueError("分析任务不存在")
    if source["status"] not in ("completed", "completed_with_warnings"):
        raise ValueError("分析任务尚未完成")
    if not source.get("result_path") or not source.get("request_json"):
        raise ValueError("该分析任务不属于当前执行节点，请在原分析节点创建裂变草稿")
    report_path = Path(source["result_path"]).resolve()
    if not report_path.is_relative_to(video_analysis_report_root().resolve()) or not report_path.is_file():
        raise ValueError("分析报告不在当前节点可访问的 NAS 路径中，请在原分析节点提交裂变任务")
    request_data = request.model_dump(mode="json")
    review_enabled = _material_review_enabled()
    request_data["_material_review_enabled"] = review_enabled
    if review_enabled:
        if request.material_review_backup_model_id and not request.material_review_primary_model_id:
            raise ValueError("配置备用审核模型前必须先配置主审核模型")
        if request.material_review_primary_model_id == request.material_review_backup_model_id and request.material_review_primary_model_id:
            raise ValueError("主审核模型与备用审核模型不能相同")
        for field, snapshot_field in (
            ("material_review_primary_model_id", "_material_review_primary_snapshot"),
            ("material_review_backup_model_id", "_material_review_backup_snapshot"),
        ):
            model_id = request_data.get(field)
            if model_id is None:
                continue
            try:
                selected = material_review.resolve_selected(int(model_id))
                request_data[snapshot_field] = material_review.selected_model(selected)
            except material_review.MaterialReviewError as exc:
                raise ValueError(str(exc)) from None
    start()
    task_id = "vrd_" + uuid.uuid4().hex
    task = store.create(task_id, analysis_task_id, request_data)
    with _active_lock:
        _queue_task(task_id)
    return _public_task(task)


def get(task_id: str) -> dict | None:
    task = store.get(task_id)
    return _public_task(task) if task else None


def delete_version(task_id: str, version_number: int) -> dict | None:
    if not re.fullmatch(r"vrd_[0-9a-f]{32}", task_id):
        raise ValueError("裂变任务 ID 格式无效")
    task = store.get_local(task_id)
    if task is None:
        if store.get(task_id):
            raise ValueError("只能在创建该裂变任务的节点删除草稿")
        return None
    if task.get("status") in {"queued", "running"}:
        raise ValueError("裂变任务仍在执行，请完成后再删除草稿")

    versions = task.get("versions") or []
    version = next((item for item in versions if int(item.get("version") or 0) == version_number), None)
    if version is None:
        return None
    if version.get("status") == "deleted":
        return _public_task(task)
    if version.get("status") not in {"completed", "completed_with_warnings"} or not version.get("draft_id"):
        raise ValueError("指定版本没有可删除的已生成草稿")

    draft_id = str(version["draft_id"])
    if not re.fullmatch(r"\d{14}[0-9a-fA-F]{8}", draft_id):
        raise ValueError("草稿 ID 格式无效，拒绝删除非本模块生成的草稿")
    draft_root = Path(config.DRAFT_DIR).resolve()
    draft_path = (draft_root / draft_id).resolve()
    if not draft_path.is_relative_to(draft_root):
        raise ValueError("草稿目录越界，拒绝删除")

    media_root_resolved = Path(media_root()).resolve()
    media_task_root = (media_root_resolved / "replacement-drafts" / task_id).resolve()
    media_version_path = (media_task_root / f"version-{version_number:02d}").resolve()
    if not media_task_root.is_relative_to(media_root_resolved) or not media_version_path.is_relative_to(media_task_root):
        raise ValueError("裂变媒体目录越界，拒绝删除")

    if draft_path.exists():
        try:
            shutil.rmtree(draft_path)
        except FileNotFoundError:
            pass
    DRAFT_CACHE.pop(draft_id, None)
    cleanup_warning = ""
    if media_version_path.exists():
        try:
            shutil.rmtree(media_version_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            cleanup_warning = f"草稿已删除，但版本媒体清理失败: {exc}"[:500]

    version.update({
        "status": "deleted",
        "deleted_at": store._now(),
        "draft_url": None,
        "preview_url": None,
        "detail": None,
        "materials": [],
        "segment_infos": [],
        "segment_ids": [],
    })
    if cleanup_warning:
        version["cleanup_warning"] = cleanup_warning
    updated = store.update(task_id, versions=versions)
    return _public_task(updated) if updated else None


def start() -> None:
    global _started, _worker_thread
    with _start_lock:
        if _started and _worker_thread and _worker_thread.is_alive():
            return
        store.fail_running()
        _started = True
        _stop_event.clear()
        for task_id in store.queued():
            _queue_task(task_id)
        _worker_thread = threading.Thread(
            target=_worker, name="analysis-replacement-drafts", daemon=True,
        )
        _worker_thread.start()


def stop() -> None:
    _stop_event.set()
    thread = _worker_thread
    if thread and thread.is_alive():
        thread.join(timeout=5)
    with _active_lock:
        _queued.clear()
        _active.clear()
        while True:
            try:
                _queue.get_nowait()
                _queue.task_done()
            except queue.Empty:
                break


def _queue_task(task_id: str) -> None:
    if task_id in _queued:
        return
    _queued.add(task_id)
    _queue.put(task_id)


def _worker() -> None:
    global _started
    while not _stop_event.is_set():
        try:
            task_id = _queue.get(timeout=0.5)
        except queue.Empty:
            continue
        _queued.discard(task_id)
        try:
            with _active_lock:
                if task_id in _active:
                    continue
                _active.add(task_id)
            _run(task_id)
        except Exception:
            logger.exception("replacement_drafts event=task_crashed task_id=%s", task_id)
        finally:
            with _active_lock:
                _active.discard(task_id)
            _queue.task_done()
    with _start_lock:
        _started = False


def _load_report(task_id: str) -> tuple[dict, dict, dict]:
    task = analysis_task_store.get_task(task_id)
    if not task:
        raise ValueError("分析任务不存在")
    if not task.get("result_path") or not task.get("request_json"):
        raise ValueError("该分析任务不属于当前执行节点，请在原分析节点创建裂变草稿")
    path = Path(task["result_path"]).resolve()
    if not path.is_relative_to(video_analysis_report_root().resolve()) or not path.is_file():
        raise ValueError("分析报告文件不存在")
    report = json.loads(path.read_text(encoding="utf-8"))
    request = json.loads(task["request_json"] or "{}")
    return report, request, task


def _scene_rows(report: dict) -> list[dict]:
    rows = report.get("step_3_shot_breakdown") or (report.get("analysis_report") or {}).get("step_3_shot_breakdown") or []
    valid = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("shot_index") is None:
            continue
        start, end = int(row.get("start_us") or 0), int(row.get("end_us") or 0)
        if start < 0 or end <= start:
            continue
        index = int(row["shot_index"])
        if index in seen:
            raise ValueError(f"分析报告中分镜编号重复: {index}")
        seen.add(index)
        valid.append(row)
    valid.sort(key=lambda row: (int(row["start_us"]), int(row["shot_index"])))
    for previous, current in zip(valid, valid[1:]):
        if int(current["start_us"]) < int(previous["end_us"]):
            raise ValueError("分析报告中的分镜时间范围互相重叠，拒绝创建不确定的覆盖草稿")
    return valid


def _match_target(row: dict, request: dict) -> dict:
    category = str(row.get("scene_category") or "unknown")
    role = {"product_empty_scene": "product_empty_scene", "pain_point": "pain_point",
            "motion": "motion", "intro": "scene_empty_scene"}.get(category, category)
    content = " ".join(str(row.get(key) or "") for key in (
        "content", "dialogue", "action", "emotion", "environment", "cinematography",
    ))
    actions = row.get("action") or []
    if isinstance(actions, str):
        actions = [actions]
    visual_intent = media_library.matching_visual_intent(
        content, str(row.get("visual_intent") or role),
    )
    required_elements = [str(value) for value in actions]
    if visual_intent == "qr_code_visible":
        required_elements = ["二维码或条形码"]
    return {
        "shot_id": f"analysis-scene-{row.get('shot_index')}",
        "category": role, "visual_intent": visual_intent, "text": content,
        "semantic_reason": str(row.get("replace_reason") or ""),
        "visual_description": str(row.get("content") or ""),
        "required_elements": required_elements, "forbidden_elements": [],
        "product_names": [str(value) for value in request.get("product_names", []) if str(value).strip()],
        "duration_us": int(row["end_us"]) - int(row["start_us"]),
        "output_width": request["width"], "output_height": request["height"],
    }


def _search_candidates(row: dict, request: dict) -> list[dict]:
    target = _match_target(row, request)
    query = target["text"] or target["category"]
    candidates = media_library.search_clips(
        query=query, usage_role=target["category"], product_names=target["product_names"],
        duration_us=target["duration_us"], visual_intent=target["visual_intent"],
        required_elements=target["required_elements"], forbidden_elements=[], limit=30,
        semantic_visual_only=True,
    )
    return _filter_and_rank_candidates(candidates, target, request)


def _filter_and_rank_candidates(candidates: list[dict], target: dict, request: dict) -> list[dict]:
    threshold = float(request.get("min_match_score", 0.58))
    required = target.get("required_elements") or []
    accepted = []
    for original in candidates:
        item = dict(original)
        reason = dict(item.get("match_reason") or {})
        similarity = float(reason.get("semantic_similarity") or 0)
        visual_intent_score = float(reason.get("visual_intent_score") or 0)
        match_score = round(min(1.0, (similarity + visual_intent_score) / 2), 4)
        coverage = float(reason.get("required_element_coverage") or (1.0 if not required else 0.0))
        motion = max(0.0, float(item.get("motion_intensity") or reason.get("motion_intensity") or 0))
        if match_score < threshold:
            continue
        item["match_score"] = match_score
        if not item.get("model_verification"):
            usable_start = int(item.get("usable_start_us") or 0)
            usable_end = int(item.get("usable_end_us") or 0)
            duration = int(target.get("duration_us") or 0)
            if duration > 0 and usable_end - usable_start >= duration:
                start = int(item.get("clip_start_us") or usable_start) if item.get("action_event_match") else usable_start
                if start < usable_start or start + duration > usable_end:
                    start = usable_start
                    item.pop("action_event_match", None)
                    reason.pop("action_event", None)
                item["clip_start_us"] = start
                item["clip_end_us"] = item["clip_start_us"] + duration
        reason["replacement_quality"] = {
            "semantic_similarity": round(similarity, 4),
            "visual_intent_score": round(visual_intent_score, 4),
            "combined_match_score": match_score,
            "score_basis": "semantic_similarity_and_visual_intent_equal_weight",
            "required_element_coverage": round(coverage, 4),
            "motion_intensity": round(motion, 6),
            "selection_mode": "model_localized" if item.get("model_verification") else (
                "database_action_event" if item.get("action_event_match") else "database_strict_start"
            ),
        }
        item["match_reason"] = reason
        item["replacement_rank_score"] = match_score
        accepted.append(item)
    return sorted(accepted, key=lambda item: item["replacement_rank_score"], reverse=True)


def _search_text_candidates(row: dict, request: dict) -> list[dict]:
    return _search_candidates(row, request)


def scene_material_candidates(
    analysis_task_id: str, shot_index: int, *, limit: int = 10,
    min_match_score: float = 0.58,
) -> dict:
    task = analysis_task_store.get_task(analysis_task_id)
    if not task:
        raise LookupError("分析任务不存在")
    status = str(task.get("status") or "unknown")
    if status in {"failed", "cancelled", "canceled"}:
        detail = str(task.get("error_message") or "未提供错误详情").strip()
        raise ValueError(f"分析任务失败，无法查询分镜素材: {detail}")
    if status not in {"completed", "completed_with_warnings"}:
        raise ValueError(f"分析任务尚未完成，当前状态: {status}")

    report, source_request, _ = _load_report(analysis_task_id)
    rows = _scene_rows(report)
    row = next((item for item in rows if int(item["shot_index"]) == shot_index), None)
    if row is None:
        raise LookupError(f"分析任务中不存在分镜 {shot_index}")
    scene_status = str(row.get("analysis_status") or row.get("status") or "").lower()
    if scene_status in {"failed", "error", "pending", "queued", "running"}:
        detail = str(row.get("error_message") or row.get("error") or "").strip()
        suffix = f": {detail}" if detail else ""
        raise ValueError(f"分镜分析未成功，状态: {scene_status}{suffix}")

    request = {
        **source_request,
        "width": int(source_request.get("width") or 1080),
        "height": int(source_request.get("height") or 1920),
        # Query below-threshold candidates too; return the threshold separately
        # as an advisory flag for callers rather than hiding useful diagnostics.
        "min_match_score": 0,
    }
    candidates = _search_candidates(row, request)
    result = []
    unavailable = 0
    for candidate in candidates:
        try:
            url = media_library._join_access_url(
                candidate.get("access_url", ""), candidate.get("storage_path", ""),
            )
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
            is_loopback = host in {"localhost", "localhost.localdomain"}
            if host:
                try:
                    address = ipaddress.ip_address(host)
                    is_loopback = is_loopback or address.is_loopback or address.is_unspecified
                except ValueError:
                    pass
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or is_loopback:
                raise RuntimeError("素材 URL 不是可访问的 HTTP(S) 地址")
        except (RuntimeError, ValueError):
            unavailable += 1
            continue
        reason = candidate.get("match_reason") or {}
        score = float(candidate.get("match_score") or 0)
        result.append({
            "rank": len(result) + 1,
            "asset_id": int(candidate["asset_id"]),
            "temporal_feature_id": int(candidate["temporal_feature_id"]),
            "asset_name": str(candidate.get("asset_name") or ""),
            "url": url,
            "clip_start_us": int(candidate.get("clip_start_us") or candidate["usable_start_us"]),
            "clip_end_us": int(candidate.get("clip_end_us") or candidate["usable_end_us"]),
            "usable_start_us": int(candidate["usable_start_us"]),
            "usable_end_us": int(candidate["usable_end_us"]),
            "match_score": score,
            "semantic_similarity": float(reason.get("semantic_similarity") or 0),
            "visual_intent_score": float(reason.get("visual_intent_score") or 0),
            "auto_selectable": score >= min_match_score,
        })
        if len(result) >= limit:
            break
    return {
        "analysis_task_id": analysis_task_id,
        "shot_index": shot_index,
        "scene_category": str(row.get("scene_category") or ""),
        "scene_content": str(row.get("content") or row.get("dialogue") or ""),
        "scene_duration_us": int(row["end_us"]) - int(row["start_us"]),
        "min_match_score": min_match_score,
        "candidate_count": len(result),
        "url_unavailable_count": unavailable,
        "candidates": result,
    }


async def replace_selected_draft_material(request) -> dict:
    """Replace one cached draft video segment with a chosen indexed library clip."""
    draft_id = request.draft_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", draft_id):
        raise ValueError("draft_id 格式无效")
    script = DRAFT_CACHE.get(draft_id)
    if script is None:
        raise ValueError("草稿不在当前服务进程缓存中；请在创建该草稿的服务实例上操作")

    original = None
    for track in script.tracks.values():
        for segment in track.segments:
            if segment.segment_id == request.segment_id:
                if not isinstance(segment, draft.VideoSegment):
                    raise ValueError("指定片段不是视频分镜")
                original = segment
                break
        if original is not None:
            break
    if original is None:
        raise LookupError("草稿中未找到指定的视频分镜")

    analysis_task = analysis_task_store.get_task(request.analysis_task_id)
    if not analysis_task:
        raise LookupError("素材来源分析任务不存在")
    if analysis_task.get("status") not in {"completed", "completed_with_warnings"}:
        raise ValueError(f"素材来源分析任务尚未完成，当前状态: {analysis_task.get('status') or 'unknown'}")
    report, source_request, _ = _load_report(request.analysis_task_id)
    row = next((
        item for item in _scene_rows(report)
        if int(item["shot_index"]) == request.shot_index
    ), None)
    if row is None:
        raise LookupError(f"分析任务中不存在分镜 {request.shot_index}")

    target_duration = int(original.duration)
    if target_duration <= 0:
        raise ValueError("目标草稿片段时长无效")
    row = dict(row)
    row["end_us"] = int(row["start_us"]) + target_duration
    search_request = {
        **source_request,
        "width": int(getattr(script, "width", 1080) or 1080),
        "height": int(getattr(script, "height", 1920) or 1920),
        "min_match_score": 0,
    }
    candidate = next((
        item for item in _search_candidates(row, search_request)
        if int(item.get("asset_id") or 0) == request.asset_id
        and int(item.get("temporal_feature_id") or 0) == request.temporal_feature_id
    ), None)
    if candidate is None:
        raise ValueError("所选素材分镜不存在、未分析成功或可用时长不足以覆盖目标片段")

    candidate_start = candidate.get("clip_start_us")
    source_start = int(request.source_start_us if request.source_start_us is not None else (
        candidate["usable_start_us"] if candidate_start is None else candidate_start
    ))
    usable_start = int(candidate["usable_start_us"])
    usable_end = int(candidate["usable_end_us"])
    if source_start < usable_start or source_start + target_duration > usable_end:
        raise ValueError("所选素材起点超出有效片段范围，或不足以覆盖目标分镜时长")

    video_url = _candidate_draft_url(candidate)
    base_url = urlsplit(config.DRAFT_URL)
    query = urlencode([*parse_qsl(base_url.query, keep_blank_values=True), ("draft_id", draft_id)])
    draft_url = urlunsplit((base_url.scheme, base_url.netloc, base_url.path, query, ""))
    volume = float(request.volume if request.volume is not None else original.volume)
    replaced = await replace_draft_segment(ReplaceDraftSegmentRequest(
        draft_url=draft_url,
        segment_id=request.segment_id,
        video_url=video_url,
        source_start=source_start,
        volume=volume,
    ))
    return {
        "draft_id": draft_id,
        "draft_url": replaced["draft_url"],
        "modified_in_place": True,
        "segment_id": replaced["segment_id"],
        "track_id": replaced["track_id"],
        "start": replaced["start"],
        "end": replaced["end"],
        "asset_id": request.asset_id,
        "temporal_feature_id": request.temporal_feature_id,
        "asset_name": str(candidate.get("asset_name") or ""),
        "video_url": video_url,
        "source_start_us": source_start,
        "volume": volume,
    }


def _review_candidates(task_id: str, row: dict, candidates: list[dict], request: dict) -> list[dict]:
    primary_id = request.get("material_review_primary_model_id")
    review_enabled = request.get("_material_review_enabled")
    if review_enabled is None:
        review_enabled = _material_review_enabled()
    if not review_enabled or not primary_id or not candidates:
        return candidates
    primary = material_review.resolve_selected(int(primary_id), request.get("_material_review_primary_snapshot"))
    backup = None
    if request.get("material_review_backup_model_id"):
        backup = material_review.resolve_selected(int(request["material_review_backup_model_id"]), request.get("_material_review_backup_snapshot"))
    target = _match_target(row, request)
    source_cache: dict[tuple, Path] = {}
    lock = threading.Lock()

    # Candidate inspection is allowed to use local analysis scratch space only.
    # It must not create task/temp directories on the NAS media root.
    with analysis_temporary_directory(f"{task_id}-material-review-") as directory:
        review_root = Path(directory)

        def resolve(clip: dict) -> Path:
            key = (clip.get("asset_id"), clip.get("temporal_feature_id"), clip.get("source_video_url"))
            with lock:
                if key not in source_cache:
                    source_cache[key] = media_library.resolve_local_source(clip) or _download_candidate(
                        task_id, clip, review_root,
                    )
                return source_cache[key]

        try:
            verified, _ = material_review.review_with_fallback(
                target=target, candidates=candidates[:5], primary=primary, backup=backup,
                source_resolver=resolve, task_id=task_id,
            )
            return verified
        except Exception as exc:
            logger.warning("replacement_drafts event=material_review_failed task_id=%s scene=%s error=%s", task_id, row.get("shot_index"), exc)
            return []


def _download_candidate(task_id: str, clip: dict, root: Path) -> Path:
    import requests

    source_url = _candidate_draft_url(clip)
    root.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    suffix = Path(source_url.split("?", 1)[0]).suffix.lower()
    target = root / f"{identity}{suffix if suffix in {'.mp4', '.mov', '.mkv', '.webm', '.avi'} else '.mp4'}"
    expected_size = int(clip.get("file_size") or 0)
    if target.is_file() and (not expected_size or target.stat().st_size == expected_size):
        return target
    target.unlink(missing_ok=True)
    with requests.get(source_url, stream=True, timeout=(15, 120)) as response:
        response.raise_for_status()
        with analysis_temporary_directory(f"{task_id}-material-") as directory:
            temporary = Path(directory) / target.name
            size = 0
            with temporary.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        size += len(chunk)
                        if size > 2 * 1024 * 1024 * 1024:
                            raise RuntimeError("素材文件超过2GB限制")
                        output.write(chunk)
            if not size or (expected_size and size != expected_size):
                raise RuntimeError("素材下载文件大小无效")
            target.write_bytes(temporary.read_bytes())
    expected_hash = str(clip.get("sha256") or "").lower()
    if expected_hash and hashlib.sha256(target.read_bytes()).hexdigest().lower() != expected_hash:
        target.unlink(missing_ok=True)
        raise RuntimeError("素材下载校验失败")
    return target


def _identity(clip: dict) -> tuple[str, str]:
    sha = str(clip.get("sha256") or "").strip().lower()
    if sha:
        return "sha256", sha
    return "asset", str(clip.get("asset_id") or clip.get("source_video_url") or "")


def _render_replacement_media(task_id: str, version: int, selected: dict[int, dict],
                              request: dict) -> dict[int, dict]:
    """Render final overlay clips; downloads and inspection scratch stay local."""
    import requests

    output_root = ensure_writable_directory(
        media_root() / "replacement-drafts" / task_id / f"version-{version:02d}"
    )
    media_base = _media_base_url()
    media_root_resolved = media_root().resolve()
    rendered = {}
    for scene_index, original in sorted(selected.items()):
        clip = dict(original)
        duration_us = int(clip["target_end_us"]) - int(clip["target_start_us"])
        if duration_us <= 0:
            raise ValueError(f"分镜 {scene_index} 替换时长无效")
        source = media_library.resolve_local_source(clip)
        if source is None:
            source_url = _candidate_draft_url(clip)
            with analysis_temporary_directory(f"{task_id}-v{version}-s{scene_index}-") as scratch:
                source = Path(scratch) / f"source-{scene_index}.mp4"
                expected_size = int(clip.get("file_size") or 0)
                expected_hash = str(clip.get("sha256") or "").lower()
                digest = hashlib.sha256()
                size = 0
                with requests.get(source_url, stream=True, timeout=(15, 120)) as response:
                    response.raise_for_status()
                    with source.open("wb") as output:
                        for chunk in response.iter_content(1024 * 1024):
                            if not chunk:
                                continue
                            size += len(chunk)
                            if size > 2 * 1024 * 1024 * 1024:
                                raise RuntimeError("素材文件超过2GB限制")
                            digest.update(chunk)
                            output.write(chunk)
                if not size or (expected_size and size != expected_size):
                    raise RuntimeError("素材下载文件大小无效")
                if expected_hash and digest.hexdigest() != expected_hash:
                    raise RuntimeError("素材下载哈希校验失败")
                rendered[scene_index] = _encode_replacement_clip(
                    task_id, version, scene_index, clip, source, output_root,
                    media_root_resolved, media_base, duration_us, request,
                )
        else:
            rendered[scene_index] = _encode_replacement_clip(
                task_id, version, scene_index, clip, source, output_root,
                media_root_resolved, media_base, duration_us, request,
            )
    return rendered


def _encode_replacement_clip(task_id: str, version: int, scene_index: int,
                             clip: dict, source: Path, output_root: Path,
                             media_root_resolved: Path, media_base: str,
                             duration_us: int, request: dict) -> dict:
    asset_id = int(clip.get("asset_id") or 0)
    output = output_root / f"scene-{scene_index:03d}-asset-{asset_id}.mp4"
    source_width, source_height, source_duration = _probe(source)
    source_start = int(clip.get("clip_start_us") or 0)
    if source_start < 0 or source_start + duration_us > source_duration + 100_000:
        raise ValueError(f"素材 {asset_id} 可用时长不足以覆盖分镜 {scene_index}")
    fit_mode = str(clip.get("vertical_fit") or ("contain_blur" if source_width > source_height else "cover"))
    filters = vertical_renderer.filter_args(
        width=int(request["width"]), height=int(request["height"]),
        duration_us=duration_us, fit_mode=fit_mode,
        focus_x=float(clip.get("focus_x", 0.5)), focus_y=float(clip.get("focus_y", 0.5)),
    )
    output.unlink(missing_ok=True)
    try:
        subprocess.run([
            media_tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-ss",
            f"{source_start / 1_000_000:.6f}", "-i", str(source), "-an", *filters,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(output),
        ], capture_output=True, timeout=900, check=True)
        width, height, rendered_duration = _probe(output)
        if (width, height) != (int(request["width"]), int(request["height"])):
            raise RuntimeError("替换素材输出分辨率不正确")
        if rendered_duration < duration_us:
            raise RuntimeError("替换素材输出时长不足")
    except Exception:
        output.unlink(missing_ok=True)
        raise
    relative = output.resolve().relative_to(media_root_resolved).as_posix()
    from urllib.parse import quote

    clip["rendered_video_url"] = f"{media_base}/{quote(relative, safe='/')}"
    clip["rendered_width"] = width
    clip["rendered_height"] = height
    clip["rendered_duration_us"] = rendered_duration
    clip["rendered_fit_mode"] = fit_mode
    return clip


def _select_for_versions(task_id: str, rows: list[dict], count: int, request: dict) -> tuple[list[dict[int, dict]], dict[int, str]]:
    selected = [dict() for _ in range(count)]
    used: set[tuple[str, str]] = set()
    reasons: dict[int, str] = {}
    media_library.ensure_usage_tables()
    for row in rows:
        scene_index = int(row["shot_index"])
        if row.get("replaceable") is not True:
            reasons[scene_index] = str(row.get("replace_reason") or "分析结果未标记可替换")
            continue
        if row.get("analysis_status") != "completed" or row.get("replacement_type") == "需人工复核" \
                or row.get("scene_category") in {"oral_delivery", "talking_head"}:
            reasons[scene_index] = str(row.get("replace_reason") or "分镜分析未完成或需要人工复核")
            continue
        candidates = _review_candidates(task_id, row, _search_text_candidates(row, request), request)
        if not candidates:
            reasons[scene_index] = "素材库无满足语义、视觉意图、时长及审核条件的素材"
            continue
        for version in range(count):
            choice = next((item for item in candidates if _identity(item) not in used), None)
            if choice is None:
                reasons[scene_index] = "符合条件的素材已分配给本批次其他分镜或版本"
                continue
            choice = dict(choice)
            choice["scene_index"] = int(row["shot_index"])
            choice["target_start_us"] = int(row["start_us"])
            choice["target_end_us"] = int(row["end_us"])
            selected[version][scene_index] = choice
            used.add(_identity(choice))
    return selected, reasons


def _build_draft(task_id: str, source_url: str, source: Path | None, report: dict,
                 row_map: dict[int, dict], selected: dict, request: dict,
                 duration_us: int, source_segments: dict[str, dict] | None = None,
                 source_audio_url: str | None = None, source_dimensions: tuple[int, int] = (0, 0),
                 overlay_events: list[dict] | None = None, overlay_warnings: list[str] | None = None) -> dict:
    draft_url = create_draft(width=int(request["width"]), height=int(request["height"]))
    source_segments = source_segments or {}
    source_track = []
    detail_segments = []
    for slot in _timeline_slots(row_map, duration_us):
        start, end = slot["start_us"], slot["end_us"]
        scene_index = slot.get("scene_index")
        clip = selected.get(scene_index) if scene_index is not None else None
        if clip:
            video_url = str(clip.get("rendered_video_url") or _candidate_draft_url(clip))
            media_width = int(clip.get("rendered_width") or clip.get("width") or request["width"])
            media_height = int(clip.get("rendered_height") or clip.get("height") or request["height"])
        else:
            asset = source_segments.get(slot["key"])
            if not asset:
                raise ValueError(f"源视频片段缺失: {slot['key']}")
            video_url = asset["video_url"]
            media_width = int(asset["width"])
            media_height = int(asset["height"])
        row = row_map.get(int(scene_index), {}) if scene_index is not None else {}
        segment = {
            "segment_id": slot["key"],
            "scene_index": scene_index,
            "type": "replacement" if clip else ("original" if scene_index is not None else "gap"),
            "start_us": start,
            "end_us": end,
            "duration_us": end - start,
            "video_url": video_url,
            "width": media_width,
            "height": media_height,
            "source_start_us": int(clip.get("clip_start_us") or 0) if clip else start,
            "source_end_us": (
                int(clip.get("clip_end_us") or (int(clip.get("clip_start_us") or 0) + end - start))
                if clip else end
            ),
            "captions": [],
        }
        if row:
            segment.update({
                "scene_category": row.get("scene_category"),
                "visual_intent": row.get("visual_intent"),
                "content": row.get("content", ""),
                "dialogue": row.get("dialogue", ""),
            })
        if clip:
            segment["replacement"] = {
                "asset_id": clip.get("asset_id"),
                "temporal_feature_id": clip.get("temporal_feature_id"),
                "source_start_us": segment["source_start_us"],
                "source_end_us": segment["source_end_us"],
                "match_score": clip.get("match_score"),
                "match_reason": clip.get("match_reason", {}),
                "vertical_fit": clip.get("rendered_fit_mode"),
            }
        detail_segments.append(segment)
        source_track.append({
            "video_url": video_url, "source_start": 0, "start": start, "end": end,
            "duration": end - start, "width": media_width, "height": media_height,
            "volume": 0.0,
        })
    _, source_track_id, _, source_segment_ids, source_infos = asyncio.run(
        add_videos_async(draft_url, json.dumps(source_track, ensure_ascii=False))
    )

    audio_track_id = None
    audio_ids = []
    audio_warning = ""
    if request.get("keep_original_audio", True):
        if source_audio_url:
            _, audio_track_id, audio_ids = asyncio.run(add_audios_async(
                draft_url,
                json.dumps([{
                    "audio_url": source_audio_url, "duration": duration_us,
                    "start": 0, "end": duration_us, "volume": 1.0,
                }], ensure_ascii=False),
            ))
        else:
            audio_warning = "源视频未发现可保留的音轨。"

    speech_captions = _speech_captions_for_scenes(row_map, selected) \
        if request.get("preserve_dialogue_captions", True) else []
    ocr_adjusted_caption_count = 0
    ocr_timing_adjusted_caption_count = 0
    ocr_positioned_caption_count = 0
    if speech_captions and request.get("preserve_on_screen_text", True):
        speech_captions, ocr_adjusted_caption_count = _adjust_speech_captions_with_ocr(
            row_map, selected, speech_captions, overlay_events or [],
        )
        ocr_timing_adjusted_caption_count = sum(
            bool(item.get("_ocr_timing_adjusted")) for item in speech_captions
        )
        ocr_positioned_caption_count = sum(bool(item.get("_ocr_box")) for item in speech_captions)
    caption_tracks = []
    speech_caption_count = 0
    included_captions = set()
    if speech_captions:
        for group in _group_speech_captions(
            speech_captions, int(request["width"]), int(request["height"]),
        ):
            try:
                result = asyncio.run(add_captions_async(
                    draft_url, json.dumps(group["items"], ensure_ascii=False),
                    text_color="#ffde00", border_color="#000000", alignment=1,
                    font="点宋体", font_size=12, has_shadow=True,
                    transform_x=group["transform_x"], transform_y=group["transform_y"],
                ))
                caption_tracks.append(result[1])
                speech_caption_count += len(group["items"])
                included_captions.update(
                    (int(item["start"]), int(item["end"]), str(item["text"]))
                    for item in group["items"]
                )
            except Exception as exc:
                audio_warning = _append_warning(audio_warning, f"口播字幕恢复失败: {exc}")

    for segment in detail_segments:
        if segment["type"] != "replacement":
            continue
        for caption in speech_captions:
            if int(caption["start"]) >= int(segment["end_us"]) or int(caption["end"]) <= int(segment["start_us"]):
                continue
            caption_key = (int(caption["start"]), int(caption["end"]), str(caption["text"]))
            segment["captions"].append({
                "start_us": caption_key[0],
                "end_us": caption_key[1],
                "text": caption_key[2],
                "included_in_draft": caption_key in included_captions,
            })

    warnings = list(overlay_warnings or [])
    if selected:
        warnings.append("非文字贴纸未从压平视频中抠取；如需原样复用，请提供带独立图层的原剪映草稿或透明贴纸素材。")
    missing_caption_scenes = [
        int(scene_index) for scene_index in selected
        if str(row_map.get(int(scene_index), {}).get("dialogue") or "").strip()
        and not row_map.get(int(scene_index), {}).get("captions")
    ]
    if missing_caption_scenes and request.get("preserve_dialogue_captions", True):
        warnings.append(
            "以下被替换分镜有口播文本，但报告未保存逐字时间戳字幕，无法准确恢复字幕: "
            + ", ".join(map(str, sorted(missing_caption_scenes)))
        )
    if audio_warning:
        warnings.append(audio_warning)
    draft_id = draft_url.rsplit("draft_id=", 1)[-1]
    return {
        "status": "completed_with_warnings" if warnings else "completed",
        "draft_id": draft_id, "draft_url": draft_url,
        "preview_url": draft_url, "track_id": source_track_id,
        "audio_track_id": audio_track_id, "audio_ids": audio_ids,
        "caption_track_ids": caption_tracks,
        "segment_ids": list(source_segment_ids or []),
        "scene_count": len(row_map),
        "replaced_scene_indexes": sorted(selected),
        "retained_scene_indexes": sorted(set(row_map) - set(selected)),
        "segment_infos": [item.model_dump() for item in (source_infos or [])],
        "detail": {
            "schema_version": 1,
            "duration_us": duration_us,
            "canvas": {"width": int(request["width"]), "height": int(request["height"])},
            "segments": detail_segments,
        },
        "materials": [{
            "scene_index": int(index), "asset_id": clip.get("asset_id"),
            "temporal_feature_id": clip.get("temporal_feature_id"),
            "source_start_us": clip.get("clip_start_us"), "source_end_us": clip.get("clip_end_us"),
            "rendered_video_url": clip.get("rendered_video_url"),
            "vertical_fit": clip.get("rendered_fit_mode"),
            "match_score": clip.get("match_score"), "match_reason": clip.get("match_reason", {}),
        } for index, clip in sorted(selected.items())],
        "unreplaced_reasons": [],
        "media_mode": "edited_main_track_with_separate_original_audio",
        "media_note": (
            "主画面轨按报告时间轴拼接原片段与替换素材；原音频独立保留，替换素材静音。"
            if request.get("keep_original_audio", True)
            else "主画面轨按报告时间轴拼接原片段与替换素材；所有画面片段静音，未保留原音频。"
        ),
        "preserved_dialogue_caption_count": speech_caption_count,
        "ocr_adjusted_caption_count": ocr_adjusted_caption_count,
        "ocr_timing_adjusted_caption_count": ocr_timing_adjusted_caption_count,
        "ocr_positioned_caption_count": ocr_positioned_caption_count,
        "ocr_added_caption_count": 0,
        "preserved_on_screen_text_count": 0,
        "overlay_warnings": warnings,
    }


def _timeline_slots(row_map: dict[int, dict], duration_us: int) -> list[dict]:
    slots = []
    cursor = 0
    gap_index = 0
    for scene_index, row in sorted(row_map.items(), key=lambda item: int(item[1]["start_us"])):
        start, end = int(row["start_us"]), int(row["end_us"])
        if start > cursor:
            gap_index += 1
            slots.append({"key": f"gap:{gap_index}", "start_us": cursor, "end_us": start})
        slots.append({
            "key": f"scene:{scene_index}", "scene_index": scene_index,
            "start_us": start, "end_us": end,
        })
        cursor = end
    if cursor < duration_us:
        gap_index += 1
        slots.append({"key": f"gap:{gap_index}", "start_us": cursor, "end_us": duration_us})
    if not slots and duration_us > 0:
        slots.append({"key": "gap:1", "start_us": 0, "end_us": duration_us})
    return slots


def _media_base_url() -> str:
    for value in (os.getenv("SCRIPT_VIDEO_MEDIA_URL_BASE"), os.getenv("DRAFT_MEDIA_BASE_URL")):
        base = str(value or "").strip().rstrip("/")
        if not base:
            continue
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith(".localhost") or host == "::1":
            continue
        try:
            address = ipaddress.ip_address(host)
            if address.is_loopback or address.is_unspecified:
                continue
        except ValueError:
            pass
        return base
    if os.name == "nt":
        raise RuntimeError(
            "裂变媒体需要可被局域网访问的地址；请配置 SCRIPT_VIDEO_MEDIA_URL_BASE，不能使用 127.0.0.1"
        )
    return "http://nginx/media"


def _render_source_assets(task_id: str, source_url: str, source_path: Path | None,
                          row_map: dict[int, dict], duration_us: int,
                          keep_audio: bool) -> tuple[dict[str, dict], str | None, int, tuple[int, int], list[str]]:
    source_input = str(source_path) if source_path else source_url
    # Keep HTTP URLs intact; Path() rewrites their slashes on Windows.
    width, height, probed_duration = _probe(source_input)
    duration_us = max(int(duration_us or 0), max(
        (int(row["end_us"]) for row in row_map.values()), default=0,
    ))
    if duration_us <= 0:
        duration_us = probed_duration
    root = ensure_writable_directory(media_root() / "replacement-drafts" / task_id / "source")
    media_root_resolved = media_root().resolve()
    media_base = _media_base_url()
    assets = {}
    for slot in _timeline_slots(row_map, duration_us):
        start, end = int(slot["start_us"]), int(slot["end_us"])
        if end <= start:
            continue
        name = f"scene-{int(slot['scene_index']):03d}.mp4" if slot.get("scene_index") is not None else f"{slot['key'].replace(':', '-')}.mp4"
        output = root / name
        output.unlink(missing_ok=True)
        subprocess.run([
            media_tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i",
            source_input, "-ss", f"{start / 1_000_000:.6f}", "-t",
            f"{(end - start) / 1_000_000:.6f}", "-map", "0:v:0", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
        ], capture_output=True, timeout=900, check=True)
        clip_width, clip_height, clip_duration = _probe(output)
        if not clip_width or not clip_height or clip_duration <= 0:
            raise RuntimeError(f"原片分镜 {slot['key']} 导出后无法读取")
        if clip_duration + 100_000 < end - start:
            raise RuntimeError(f"原片分镜 {slot['key']} 导出时长不足")
        relative = output.resolve().relative_to(media_root_resolved).as_posix()
        assets[slot["key"]] = {
            "video_url": f"{media_base}/{quote(relative, safe='/')}",
            "width": clip_width, "height": clip_height,
            "duration_us": clip_duration,
        }

    warnings = []
    audio_url = None
    if keep_audio:
        probe = subprocess.run([
            media_tool("ffprobe"), "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_type", "-of", "json", source_input,
        ], capture_output=True, text=True, timeout=60, check=True)
        if json.loads(probe.stdout).get("streams"):
            audio_output = root / "source-audio.m4a"
            audio_output.unlink(missing_ok=True)
            subprocess.run([
                media_tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i", source_input,
                "-map", "0:a:0", "-vn", "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(audio_output),
            ], capture_output=True, timeout=900, check=True)
            if not audio_output.is_file() or audio_output.stat().st_size <= 0:
                raise RuntimeError("原片音轨提取结果为空")
            relative = audio_output.resolve().relative_to(media_root_resolved).as_posix()
            audio_url = f"{media_base}/{quote(relative, safe='/')}"
        else:
            warnings.append("源视频没有音轨，无法保留原口播音频。")
    return assets, audio_url, duration_us, (width, height), warnings


def _extract_overlay_text(task_id: str, source_url: str, source_path: Path | None,
                          row_map: dict[int, dict], selected_by_version: list[dict],
                          request: dict) -> tuple[list[dict], list[str]]:
    if not request.get("preserve_dialogue_captions", True) \
            or not request.get("preserve_on_screen_text", True):
        return [], []
    scene_ids = sorted({int(scene_id) for selected in selected_by_version for scene_id in selected})
    if not scene_ids:
        return [], []
    from src.service.video_analysis.feature_extraction import _run_worker

    ranges = [{
        "scene_index": scene_id,
        "start_us": int(row_map[scene_id]["start_us"]),
        "end_us": int(row_map[scene_id]["end_us"]),
    } for scene_id in scene_ids]
    with analysis_temporary_directory(f"{task_id}-replacement-ocr-") as work_directory:
        data = {
            "operation": "extract_overlay_text",
            "video_url": "" if source_path else source_url,
            "video_path": str(source_path) if source_path else "",
            "work_directory": work_directory,
            "max_bytes": int(os.getenv("SCENE_MAX_FILE_BYTES", str(200 * 1024 * 1024))),
            "worker_lock_wait_seconds": int(os.getenv("SCENE_WORKER_LOCK_WAIT_SECONDS", "1800")),
            "scene_ranges": ranges,
            "sample_interval_us": int(os.getenv("REPLACEMENT_OCR_SAMPLE_INTERVAL_US", "500000")),
            "max_ocr_frames": int(os.getenv("REPLACEMENT_OCR_MAX_FRAMES", "1200")),
        }
        try:
            result = _run_worker(data)
        except Exception as exc:
            logger.warning("replacement_drafts event=overlay_ocr_failed task_id=%s error=%s", task_id, exc)
            return [], [f"字幕 OCR 未完成: {exc}"]
    warnings = [str(item) for item in result.get("warnings", [])]
    if result.get("status") == "unavailable" and not warnings:
        warnings.append("当前分析环境没有可用的 OCR 引擎。")
    return result.get("events", []), warnings


def _speech_captions_for_scenes(row_map: dict[int, dict], selected: dict) -> list[dict]:
    captions = []
    for scene_index in sorted(selected):
        row = row_map.get(int(scene_index), {})
        for item in row.get("captions") or []:
            start = int(item.get("start_us") or 0)
            end = int(item.get("end_us") or 0)
            text = str(item.get("text") or "").strip()
            start = max(start, int(row.get("start_us") or 0))
            end = min(end, int(row.get("end_us") or end))
            if text and end > start:
                captions.append({"start": start, "end": end, "text": text})
    return captions


def _normalize_overlay_text(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value).casefold()


def _asr_caption_confidence(row: dict, start: int, end: int) -> float:
    best_overlap = 0
    best_confidence = 1.0
    for item in row.get("captions") or []:
        item_start = int(item.get("start_us") or 0)
        item_end = int(item.get("end_us") or 0)
        overlap = max(0, min(end, item_end) - max(start, item_start))
        probabilities = [
            float(word.get("probability")) for word in item.get("words") or []
            if word.get("probability") is not None
        ]
        if overlap > best_overlap and probabilities:
            best_overlap = overlap
            best_confidence = sum(probabilities) / len(probabilities)
    return best_confidence


def _is_subtitle_ocr_event(event: dict) -> bool:
    box = event.get("box") or []
    if len(box) != 4:
        return False
    left, top, right, bottom = (float(value) for value in box)
    width, height = right - left, bottom - top
    center_y = (top + bottom) / 2
    return width >= 0.12 and height >= 0.02 and 0.1 <= center_y <= 0.9


def _group_speech_captions(captions: list[dict], width: int, height: int) -> list[dict]:
    groups = {}
    for caption in captions:
        box = caption.get("_ocr_box") or []
        if len(box) == 4:
            left, top, right, bottom = (float(value) for value in box)
            transform_x = round(((left + right) / 2 - 0.5) * width)
            transform_y = round((0.5 - (top + bottom) / 2) * height)
        else:
            transform_x, transform_y = 0, -round(height * 0.5)
        key = (round(transform_x / 20), round(transform_y / 20))
        group = groups.setdefault(key, {
            "items": [], "x": [], "y": [],
        })
        group["items"].append({key: caption[key] for key in ("start", "end", "text")})
        group["x"].append(transform_x)
        group["y"].append(transform_y)
    return [{
        "items": group["items"],
        "transform_x": round(sum(group["x"]) / len(group["x"])),
        "transform_y": round(sum(group["y"]) / len(group["y"])),
    } for group in groups.values()]


def _adjust_speech_captions_with_ocr(row_map: dict[int, dict], selected: dict,
                                     speech: list[dict], events: list[dict]) -> tuple[list[dict], int]:
    """Correct ASR wording with OCR while keeping speech start times."""
    selected_ids = {int(value) for value in selected}
    minimum_confidence = float(os.getenv("REPLACEMENT_OCR_SUBTITLE_MIN_CONFIDENCE", "0.65"))
    output = [dict(item) for item in speech]
    adjusted = 0
    for caption in output:
        start, end = int(caption["start"]), int(caption["end"])
        scene_index = next((
            scene_id for scene_id in selected_ids
            if start < int(row_map.get(scene_id, {}).get("end_us") or 0)
            and end > int(row_map.get(scene_id, {}).get("start_us") or 0)
        ), None)
        if scene_index is None:
            continue
        asr_text = str(caption.get("text") or "").strip()
        normalized_asr = _normalize_overlay_text(asr_text)
        if len(normalized_asr) < 2:
            continue
        row = row_map.get(scene_index, {})
        asr_confidence = _asr_caption_confidence(row, start, end)
        candidates = []
        for event in events:
            if int(event.get("scene_index") or 0) != scene_index:
                continue
            confidence = float(event.get("confidence") or 0)
            event_start = int(event.get("start_us") or 0)
            event_end = int(event.get("end_us") or 0)
            overlap = max(0, min(end, event_end) - max(start, event_start))
            if confidence < minimum_confidence or overlap <= 0:
                continue
            ocr_text = str(event.get("text") or "").strip()
            normalized_ocr = _normalize_overlay_text(ocr_text)
            if len(normalized_ocr) < 2:
                continue
            has_box = len(event.get("box") or []) == 4
            subtitle_event = _is_subtitle_ocr_event(event)
            if has_box and not subtitle_event:
                continue
            length_ratio = min(len(normalized_asr), len(normalized_ocr)) / max(
                len(normalized_asr), len(normalized_ocr),
            )
            similarity = SequenceMatcher(None, normalized_asr, normalized_ocr).ratio()
            strict_match = length_ratio >= 0.65 and similarity >= 0.62
            low_confidence_match = (
                subtitle_event and asr_confidence < 0.75
                and length_ratio >= 0.75 and similarity >= 0.35
            )
            if not strict_match and not low_confidence_match:
                continue
            overlap_ratio = overlap / max(1, min(end - start, event_end - event_start))
            score = (
                similarity * 0.65 + confidence * 0.2
                + min(1.0, overlap_ratio) * 0.1 + (0.05 if subtitle_event else 0)
            )
            candidates.append((score, ocr_text, normalized_ocr, event))
        if not candidates:
            continue
        _, ocr_text, normalized_ocr, event = max(candidates, key=lambda item: item[0])
        if normalized_ocr != normalized_asr:
            caption["text"] = ocr_text
            adjusted += 1
        if _is_subtitle_ocr_event(event):
            caption["_ocr_box"] = event["box"]
            scene_end = int(row.get("end_us") or end)
            next_caption_start = min((
                int(other["start"]) for other in output
                if other is not caption and int(other["start"]) > start
                and int(other["start"]) < scene_end
            ), default=scene_end)
            visible_end = min(int(event.get("end_us") or end), scene_end, next_caption_start)
            if visible_end > end:
                caption["end"] = visible_end
                caption["_ocr_timing_adjusted"] = True
    return output, adjusted


def _append_warning(existing: str, value: str) -> str:
    return f"{existing}; {value}" if existing else value


def _candidate_draft_url(clip: dict) -> str:
    """Return a URL the existing draft downloader can consume.

    The catalog may resolve a local NAS file as file:// for model review. Draft
    creation still needs an HTTP URL, so prefer the catalog's public access URL
    in that case. No local/NAS copy is created here.
    """
    source = str(clip.get("source_video_url") or "").strip()
    if source.startswith(("http://", "https://")):
        return source
    try:
        url = media_library._join_access_url(clip.get("access_url", ""), clip.get("storage_path", ""))
    except Exception as exc:
        raise ValueError("素材没有可供剪映下载的 HTTP(S) 地址") from exc
    if not url.startswith(("http://", "https://")):
        raise ValueError("素材没有可供剪映下载的 HTTP(S) 地址")
    return url


def _run(task_id: str) -> None:
    if not store.claim(task_id):
        return
    task = store.get(task_id)
    request = task["request"]
    versions = [{"version": index + 1, "status": "queued", "draft_id": None, "draft_url": None}
                for index in range(int(request["count"]))]
    try:
        report, source_request, _ = _load_report(task["source_analysis_task_id"])
        analysis_report = report.get("analysis_report") or report
        products = ((analysis_report.get("step_2_core_visual_elements") or {}).get("products_props") or [])
        if isinstance(products, str):
            products = [products]
        request["product_names"] = [str(item) for item in products if str(item).strip()]
        rows = _scene_rows(report)
        if not rows:
            raise ValueError("分析报告没有可用的分镜时间范围")
        duration_hint = int((report.get("detection") or {}).get("duration_us") or 0)
        if duration_hint and max(int(row["end_us"]) for row in rows) > duration_hint:
            raise ValueError("分析报告分镜结束时间超过源视频时长")
        row_map = {int(row["shot_index"]): row for row in rows}
        store.update(task_id, stage="matching", progress=15, versions=versions)
        selected_by_version, reasons = _select_for_versions(task_id, rows, int(request["count"]), request)
        if not any(selected_by_version):
            skip_reason = "未找到符合条件的替换素材，未创建空草稿"
            for version in versions:
                version.update({
                    "status": "skipped", "draft_id": None, "draft_url": None,
                    "skip_reason": skip_reason, "replacement_count": 0,
                })
            message = "所有裂变版本均未找到可用替换素材，未创建草稿"
            store.update(
                task_id, status="completed_with_warnings", stage="completed_with_warnings",
                progress=100, versions=versions, error_message=message, completed_at=store._now(),
            )
            return
        source_url, source_path = _source_for_task(source_request)
        detection = report.get("detection") or {}
        source_duration = int(detection.get("duration_us") or max(int(row["end_us"]) for row in rows))
        source_segments, source_audio_url, source_duration, source_dimensions, source_warnings = _render_source_assets(
            task_id, source_url, source_path, row_map, source_duration,
            keep_audio=bool(request.get("keep_original_audio", True)),
        )
        overlay_events, overlay_warnings = _extract_overlay_text(
            task_id, source_url, source_path, row_map, selected_by_version, request,
        )
        _create_versions(
            task_id, versions, selected_by_version, row_map, source_url, source_path,
            request, reasons, source_duration, source_segments, source_audio_url,
            source_dimensions, overlay_events, [*source_warnings, *overlay_warnings],
        )
        completed = sum(item.get("status") in {"completed", "completed_with_warnings"} for item in versions)
        skipped = sum(item.get("status") == "skipped" for item in versions)
        failed = sum(item.get("status") == "failed" for item in versions)
        has_warnings = any(item.get("status") == "completed_with_warnings" for item in versions)
        status = (
            "completed_with_warnings" if skipped and not completed and not failed
            else "failed" if not completed
            else "completed_with_warnings" if completed < len(versions) or has_warnings
            else "completed"
        )
        error_message = (
            "部分裂变版本未找到替换素材，已跳过" if skipped and completed
            else "所有裂变版本均未找到可用替换素材，未创建草稿"
            if skipped and not failed else ""
        )
        store.update(task_id, status=status, stage=status, progress=100,
                     versions=versions, error_message=error_message, completed_at=store._now())
    except Exception as exc:
        logger.exception("replacement_drafts event=failed task_id=%s", task_id)
        error_message = str(exc)[:4000]
        for version in versions:
            if version.get("status") in {"queued", "running"}:
                version.update({"status": "failed", "error_message": error_message})
        store.update(task_id, status="failed", stage="failed", progress=100,
                     versions=versions, error_message=error_message, completed_at=store._now())


def _create_versions(task_id: str, versions: list[dict], selected_by_version: list[dict[int, dict]],
                     row_map: dict[int, dict], source_url: str, source_path: Path | None,
                     request: dict, reasons: dict[int, str], source_duration: int,
                     source_segments: dict[str, dict], source_audio_url: str | None,
                     source_dimensions: tuple[int, int], overlay_events: list[dict],
                     overlay_warnings: list[str]) -> None:
    audio_warning = "按请求未保留原音轨" if not request.get("keep_original_audio", True) else ""
    for index, selected in enumerate(selected_by_version):
        started = time.monotonic()
        if not selected:
            versions[index].update({
                "status": "skipped",
                "draft_id": None,
                "draft_url": None,
                "replacement_count": 0,
                "skip_reason": "未找到符合条件的替换素材，未创建空草稿",
            })
            store.update(
                task_id, stage="creating_drafts",
                progress=45 + int(50 * (index + 1) / len(versions)), versions=versions,
            )
            continue
        versions[index]["status"] = "running"
        reservation_keys = []
        try:
            for clip in selected.values():
                key = f"{task_id}:v{index + 1}:s{clip['scene_index']}"
                media_library.record_reservation(
                    idempotency_key=key, batch_task_id=task_id, variant_id=str(index + 1),
                    source_scene_id=str(clip["scene_index"]), clip=clip,
                )
                reservation_keys.append(key)
            rendered = _render_replacement_media(task_id, index + 1, selected, request) if selected else {}
            result = _build_draft(
                task_id, source_url, source_path, {}, row_map, rendered, request, source_duration,
                source_segments=source_segments, source_audio_url=source_audio_url,
                source_dimensions=source_dimensions, overlay_events=overlay_events,
                overlay_warnings=overlay_warnings,
            )
            result["unreplaced_reasons"] = [
                {"scene_index": scene_index, "reason": reasons.get(scene_index, "本镜头未分配到独立素材"),
                 "start_us": int(row["start_us"]), "end_us": int(row["end_us"])}
                for scene_index, row in sorted(row_map.items()) if scene_index not in selected
            ]
            result["replacement_count"] = len(selected)
            replaceable_indexes = {
                scene_index for scene_index, row in row_map.items()
                if row.get("replaceable") is True and row.get("analysis_status") == "completed"
                and row.get("scene_category") not in {"oral_delivery", "talking_head"}
            }
            if replaceable_indexes - set(selected):
                result["status"] = "completed_with_warnings"
            if audio_warning:
                result["audio_warning"] = audio_warning
            versions[index].update(result)
            for key in reservation_keys:
                try:
                    media_library.finish_reservation(key, draft_id=result["draft_id"])
                except Exception as exc:
                    logger.exception(
                        "replacement_drafts event=usage_commit_failed task_id=%s key=%s",
                        task_id, key,
                    )
                    versions[index]["usage_warning"] = str(exc)[:500]
            versions[index]["elapsed_seconds"] = round(time.monotonic() - started, 3)
        except Exception as exc:
            for key in reservation_keys:
                try:
                    media_library.finish_reservation(key, draft_id=None, error=str(exc))
                except Exception:
                    logger.exception(
                        "replacement_drafts event=usage_release_failed task_id=%s key=%s",
                        task_id, key,
                    )
            versions[index].update({"status": "failed", "error_message": str(exc)[:2000]})
        finally:
            store.update(task_id, stage="creating_drafts", progress=45 + int(50 * (index + 1) / len(versions)), versions=versions)

