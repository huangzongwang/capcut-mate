from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote

from . import client, storage, store
from ..platform_runtime import analysis_temp_root, digital_human_root, ensure_writable_directory


_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="digital-human")
_poll_interval = max(1.0, float(os.getenv("DUIX_POLL_INTERVAL_SECONDS", "5")))
_timeout = max(60.0, float(os.getenv("DUIX_TASK_TIMEOUT_SECONDS", "1800")))
_resume_lock = threading.Lock()
_polling_tasks: set[str] = set()


def _resolve_any_upload(relative: str, kind: str) -> Path:
    posix, windows = PurePosixPath(relative), PureWindowsPath(relative)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in posix.parts:
        raise ValueError(f"{kind} 路径必须是 upload_video 接口返回的相对路径")
    if posix.parts[0:1] != ("uploads",):
        raise ValueError(f"{kind} 必须来自 upload_video 接口")
    from ..platform_runtime import media_root
    roots = [digital_human_root(), media_root()]
    for root in roots:
        candidate = (root / relative).resolve()
        if candidate.parent == (root / "uploads").resolve() and candidate.is_file() and candidate.stat().st_size:
            return candidate
    raise FileNotFoundError(f"{kind} 上传文件不存在；请确认与上传时使用相同的 purpose")


def submit(request) -> dict:
    video = _resolve_any_upload(request.video_path, "video")
    audio = _resolve_any_upload(request.audio_path, "audio")
    if audio.suffix.lower() not in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}:
        raise ValueError("驱动音频格式仅支持 WAV/MP3/M4A/AAC/FLAC/OGG")
    if video.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
        raise ValueError("人物视频格式不支持")
    try:
        client.validate_base_url(request.duix_api_base_url)
    except client.DuixError as exc:
        raise ValueError(str(exc)) from exc
    _validate_shared_dir(request.duix_shared_dir, request.duix_api_base_url)
    storage.shared_root(request.duix_shared_dir)
    ensure_writable_directory(analysis_temp_root())
    task = store.create(request.model_dump())
    try:
        _pool.submit(
            _run, task["task_id"], task["provider_code"], str(video), str(audio),
            request.model_dump(),
        )
    except RuntimeError as exc:
        store.update(task["task_id"], status="failed", stage="failed", progress=100,
                     error_message=f"无法启动数字人工作线程: {exc}",
                     completed_at=datetime.now(timezone.utc).isoformat())
    return store.get(task["task_id"])


def _validate_shared_dir(value: str, api_base_url: str) -> None:
    from urllib.parse import urlsplit

    host = urlsplit(client.validate_base_url(api_base_url)).hostname
    windows = PureWindowsPath(value)
    if windows.is_absolute() and windows.drive.startswith("\\\\"):
        share_host = windows.drive.lstrip("\\").split("\\", 1)[0]
        if share_host.casefold() != host.casefold():
            raise ValueError("UNC 共享目录主机必须与 Duix API 地址主机一致")
        return
    if os.name == "nt":
        raise ValueError("Windows 下 duix_shared_dir 必须是 UNC 网络路径")
    allowed_root = os.getenv("DUIX_SHARED_ALLOWED_ROOT")
    if not allowed_root:
        raise ValueError("Linux/Docker 必须配置 DUIX_SHARED_ALLOWED_ROOT 限定共享挂载范围")
    path = Path(value).resolve()
    root = Path(allowed_root).resolve()
    if not path.is_relative_to(root):
        raise ValueError("duix_shared_dir 必须位于 DUIX_SHARED_ALLOWED_ROOT 内")


def _run(task_id: str, provider_code: str, video_path: str, audio_path: str, options: dict) -> None:
    prepared = []
    try:
        store.update(task_id, status="running", stage="preparing", progress=5)
        video, audio = storage.prepare(
            Path(video_path), Path(audio_path), task_id, options["duix_shared_dir"],
        )
        prepared = [video, audio]
        store.update(task_id, stage="submitting", progress=20)
        payload = {
            "audio_url": audio.name, "video_url": video.name, "code": provider_code,
            "chaofen": options["chaofen"], "watermark_switch": options["watermark_switch"],
            "pn": options["pn"],
        }
        try:
            client.submit(options["duix_api_base_url"], payload)  # Never retry POST.
        except client.DuixConnectionError as exc:
            store.update(
                task_id, status="running", stage="timed_out", progress=90,
                error_message=(
                    "提交请求连接中断，Duix 可能已接收任务；为避免重复生成，"
                    "未重发请求，请调用 refresh 查询原 UUID。"
                    f" ({exc})"
                )[:2000],
            )
            return
        store.update(task_id, stage="generating", progress=25)
        _poll(task_id, provider_code, options)
    except TimeoutError as exc:
        store.update(task_id, status="running", stage="timed_out", progress=90,
                     error_message=str(exc)[:2000])
    except Exception as exc:
        store.update(task_id, status="failed", stage="failed", progress=100,
                     error_message=str(exc)[:2000], completed_at=datetime.now(timezone.utc).isoformat())
    finally:
        task = store.get(task_id)
        if task and task["status"] in ("completed", "failed"):
            for path in prepared:
                path.unlink(missing_ok=True)


def _resolve_result(value, shared_dir: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("Duix 已报告完成，但没有结果文件名")
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") and raw.count("/") == 1:
        raw = raw[1:]
    part = PurePosixPath(raw)
    if len(part.parts) != 1 or part.is_absolute() or part.name in ("", ".", ".."):
        raise RuntimeError("Duix 返回的结果必须是共享目录下的单个文件名")
    shared = storage.shared_root(shared_dir).resolve()
    source = (shared / part.name).resolve()
    if source.parent != shared or not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError("Duix 已报告完成，但共享目录中找不到有效输出")
    return source


def _poll(task_id: str, provider_code: str, options: dict) -> None:
    deadline = time.monotonic() + _timeout
    last_state = None
    while time.monotonic() < deadline:
        try:
            data = client.query(options["duix_api_base_url"], provider_code)
        except client.DuixConnectionError:
            time.sleep(min(_poll_interval, max(0.0, deadline - time.monotonic())))
            continue
        state = str(data.get("status"))
        try:
            percent = max(25, min(90, 25 + int(float(data.get("progress", 0)) * 0.65)))
        except (TypeError, ValueError):
            percent = 25
        if state != last_state:
            store.update(task_id, status="running", stage=f"upstream_{state}", progress=percent)
            last_state = state
        if state == "2":
            source = _resolve_result(data.get("result"), options["duix_shared_dir"])
            destination = storage.publish(source, task_id)
            store.update(task_id, status="completed", stage="completed", progress=100,
                         preview_path=str(destination), completed_at=datetime.now(timezone.utc).isoformat())
            _cleanup_shared_inputs(task_id, options["duix_shared_dir"])
            return
        if state == "3":
            _cleanup_shared_inputs(task_id, options["duix_shared_dir"])
            raise RuntimeError("数字人生成失败: " + str(data.get("msg", "未知错误")))
        if state not in ("0", "1"):
            raise RuntimeError("Duix 返回未知任务状态: " + state)
        time.sleep(min(_poll_interval, max(0.0, deadline - time.monotonic())))
    raise TimeoutError("数字人任务查询超时；上游任务未取消，可以稍后查询")


def public_task(task: dict) -> dict:
    result = {key: task.get(key) for key in (
        "task_id", "status", "stage", "progress", "error_message", "created_at", "completed_at",
    )}
    result["preview_url"] = (
        f"{os.getenv('DUIX_PUBLIC_MEDIA_BASE_URL', 'http://127.0.0.1:30001').rstrip('/')}"
        f"/output/digital-human/{quote(Path(task['preview_path']).name)}"
        if task.get("status") == "completed" and task.get("preview_path") else None
    )
    return result


def startup_recover():
    """Resume provider polling; ambiguous submissions are not repeated."""
    storage.prepare_directories()
    store.fail_ambiguous_submissions()
    for row in store.list_queued():
        _resume_queued(row)
    for row in store.list_recoverable():
        resume(row["task_id"], row)


def _resume_queued(row: dict) -> None:
    task_id = row["task_id"]
    try:
        options = json.loads(row["request_json"])
        uuid.UUID(row["provider_code"])
        client.validate_base_url(options["duix_api_base_url"])
        _validate_shared_dir(options["duix_shared_dir"], options["duix_api_base_url"])
        video = _resolve_any_upload(options["video_path"], "video")
        audio = _resolve_any_upload(options["audio_path"], "audio")
        _pool.submit(
            _run, task_id, row["provider_code"], str(video), str(audio), options,
        )
    except Exception as exc:
        store.update(task_id, status="failed", stage="failed", progress=100,
                     error_message=f"排队任务恢复失败: {exc}",
                     completed_at=datetime.now(timezone.utc).isoformat())


def resume(task_id: str, row: dict | None = None) -> bool:
    row = row or store.get_recoverable(task_id)
    if not row:
        return False
    provider_code = row.get("provider_code", "")
    try:
        uuid.UUID(provider_code)
    except (ValueError, TypeError, AttributeError):
        store.update(task_id, status="failed", stage="failed", progress=100,
                     error_message="任务缺少有效的 Duix UUID，无法安全恢复",
                     completed_at=datetime.now(timezone.utc).isoformat())
        return False
    try:
        options = json.loads(row["request_json"])
        client.validate_base_url(options["duix_api_base_url"])
        _validate_shared_dir(options["duix_shared_dir"], options["duix_api_base_url"])
    except (ValueError, KeyError, TypeError, client.DuixError) as exc:
        store.update(task_id, status="failed", stage="failed", progress=100,
                     error_message=f"恢复配置无效，不能安全查询 Duix 任务: {exc}",
                     completed_at=datetime.now(timezone.utc).isoformat())
        return False
    with _resume_lock:
        if task_id in _polling_tasks:
            return False
        _polling_tasks.add(task_id)
    try:
        _pool.submit(_resume_poll_guarded, task_id, provider_code, options)
    except RuntimeError:
        with _resume_lock:
            _polling_tasks.discard(task_id)
        raise
    return True


def _resume_poll_guarded(task_id: str, provider_code: str, options: dict) -> None:
    try:
        _resume_poll(task_id, provider_code, options)
    finally:
        with _resume_lock:
            _polling_tasks.discard(task_id)


def _resume_poll(task_id: str, provider_code: str, options: dict) -> None:
    try:
        _poll(task_id, provider_code, options)
        task = store.get(task_id)
        if task and task["status"] == "completed":
            _cleanup_shared_inputs(task_id, options["duix_shared_dir"])
    except TimeoutError as exc:
        store.update(task_id, status="running", stage="timed_out", progress=90,
                     error_message=str(exc)[:2000])
    except Exception as exc:
        try:
            _cleanup_shared_inputs(task_id, options["duix_shared_dir"])
        except OSError:
            pass
        store.update(task_id, status="failed", stage="failed", progress=100,
                     error_message=str(exc)[:2000], completed_at=datetime.now(timezone.utc).isoformat())


def _cleanup_shared_inputs(task_id: str, shared_dir: str) -> None:
    try:
        shared = storage.shared_root(shared_dir)
        for suffix in (".mp4", ".wav"):
            (shared / f"duix_{task_id}{suffix}").unlink(missing_ok=True)
    except OSError:
        logging.getLogger(__name__).warning(
            "Could not clean Duix temporary inputs for task_id=%s", task_id,
        )

