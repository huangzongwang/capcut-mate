from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid
from urllib.parse import urlencode

import config
from src.utils.draft_downloader import download_draft_with_result, verify_local_draft_ready
from src.utils.logger import logger

from . import jianying_adapter, storage, store
from .schemas import CreateLocalExportTaskRequest
from ..platform_runtime import analysis_temporary_directory, media_tool, worker_creation_kwargs


def _draft_url(draft_id: str) -> str:
    separator = "&" if "?" in config.DRAFT_URL else "?"
    return f"{config.DRAFT_URL}{separator}{urlencode({'draft_id': draft_id})}"


def _validate_draft_duration(draft_id: str) -> None:
    content = Path(config.DRAFT_SAVE_PATH) / draft_id / "draft_content.json"
    try:
        payload = json.loads(content.read_text(encoding="utf-8"))
        duration = int(payload.get("duration") or 0)
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"草稿内容无法读取: {exc}") from exc
    if duration < 3_000_000:
        raise RuntimeError("草稿时长不足3秒，不能导出")


def _validate_video(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size < 1024:
        raise RuntimeError("剪映未生成有效MP4文件")
    command = [
        media_tool("ffprobe"), "-v", "error", "-show_entries",
        "format=duration:stream=codec_type", "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120, check=True,
            **worker_creation_kwargs(),
        )
        payload = json.loads(result.stdout)
        duration = float((payload.get("format") or {}).get("duration") or 0)
        has_video = any(
            stream.get("codec_type") == "video" for stream in payload.get("streams") or []
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as exc:
        raise RuntimeError(f"导出视频质量校验失败: {exc}") from exc
    if duration <= 0 or not has_video:
        raise RuntimeError("导出文件缺少有效视频流")
    return {"duration_seconds": round(duration, 3), "size_bytes": path.stat().st_size}


class LocalExportManager:
    def __init__(self):
        self._queue: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            store.fail_interrupted()
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="local-jianying-export", daemon=True,
            )
            self._thread.start()
            for task_id in store.queued_task_ids():
                self._enqueue(task_id)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread:
            thread.join(timeout=3)

    def submit(self, request: CreateLocalExportTaskRequest) -> dict:
        jianying_adapter.require_export_environment()
        self.start()
        task_id = "lex_" + uuid.uuid4().hex
        task = store.create(task_id, request.model_dump(mode="json"))
        self._enqueue(task_id)
        return task

    def get(self, task_id: str) -> dict | None:
        return store.get(task_id)

    def cancel(self, task_id: str) -> dict | None:
        if store.get(task_id) is None:
            return None
        store.request_cancel(task_id)
        task = store.get(task_id)
        if task and task["status"] == "queued":
            store.cancel_queued_items(task_id)
            store.refresh_task(task_id, terminal=True)
        return store.get(task_id)

    def retry_failed(self, task_id: str) -> tuple[int, dict | None]:
        count = store.retry_failed(task_id)
        if count > 0:
            self.start()
            self._enqueue(task_id)
        return count, store.get(task_id)

    def _enqueue(self, task_id: str) -> None:
        with self._lock:
            if task_id in self._queued:
                return
            self._queued.add(task_id)
            self._queue.put(task_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                self._queued.discard(task_id)
            try:
                self._process_task(task_id)
            except Exception:
                logger.exception("local_export event=task_crashed task_id=%s", task_id)

    def _process_task(self, task_id: str) -> None:
        if not store.claim_task(task_id):
            return
        task = store.get(task_id)
        if task is None:
            return
        request = task["request"]
        for item in task["items"]:
            current = store.get(task_id)
            if current is None:
                return
            if current["cancel_requested"]:
                store.cancel_queued_items(task_id)
                break
            if not store.claim_item(item["item_id"]):
                continue
            try:
                self._process_item(task_id, item, request)
            except Exception as exc:
                logger.exception(
                    "local_export event=item_failed task_id=%s draft_id=%s",
                    task_id, item["draft_id"],
                )
                store.update_item(
                    item["item_id"], status="failed", stage="failed", progress=100,
                    error_message=str(exc)[:2000], completed_at=store.now(),
                )
                if not request.get("continue_on_error", True):
                    store.request_cancel(task_id)
                    store.cancel_queued_items(task_id)
                    break
            finally:
                store.refresh_task(task_id)
        store.refresh_task(task_id, terminal=True)

    def _process_item(self, task_id: str, item: dict, request: dict) -> None:
        item_id = item["item_id"]
        draft_id = item["draft_id"]
        timings: dict[str, float | dict] = {}
        total_started = time.monotonic()

        prepare_started = time.monotonic()
        store.update_item(item_id, stage="preparing_draft", progress=10)
        ready = verify_local_draft_ready(draft_id)
        if not ready.ok:
            result = download_draft_with_result(_draft_url(draft_id))
            if not result.ok:
                raise RuntimeError(f"草稿下载失败[{result.kind}]: {result.detail}")
            ready = verify_local_draft_ready(draft_id)
            if not ready.ok:
                raise RuntimeError(f"草稿准备失败[{ready.kind}]: {ready.detail}")
        _validate_draft_duration(draft_id)
        timings["prepare_seconds"] = round(time.monotonic() - prepare_started, 3)

        target = storage.output_path(task_id, int(item["position"]), draft_id)
        target.unlink(missing_ok=True)
        with analysis_temporary_directory(f"{item_id}-export-") as directory:
            temporary = Path(directory) / target.name
            export_started = time.monotonic()
            store.update_item(item_id, stage="exporting", progress=35)
            last_error: Exception | None = None
            for attempt in range(1, 3):
                try:
                    jianying_adapter.export_draft(
                        draft_id, temporary,
                        resolution=request.get("resolution"),
                        framerate=request.get("framerate"),
                        timeout_seconds=int(request["timeout_seconds"]),
                    )
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    temporary.unlink(missing_ok=True)
                    if attempt < 2:
                        logger.warning(
                            "local_export event=retry task_id=%s draft_id=%s error=%s",
                            task_id, draft_id, exc,
                        )
                        time.sleep(2)
            if last_error is not None:
                raise last_error
            timings["export_seconds"] = round(time.monotonic() - export_started, 3)

            validate_started = time.monotonic()
            store.update_item(item_id, stage="validating", progress=90)
            media = _validate_video(temporary)
            shutil.copyfile(temporary, target)
        timings["validate_seconds"] = round(time.monotonic() - validate_started, 3)
        timings["total_seconds"] = round(time.monotonic() - total_started, 3)
        timings["media"] = media
        store.update_item(
            item_id, status="completed", stage="completed", progress=100,
            output_path=str(target), video_url=storage.public_url(task_id, target.name),
            error_message="", timings=timings, completed_at=store.now(),
        )


manager = LocalExportManager()

