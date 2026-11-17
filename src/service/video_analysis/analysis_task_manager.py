"""Durable, single-process worker pool for long video analysis jobs."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid
import os
from datetime import datetime, timezone
from pathlib import Path

from src.utils.logger import logger
from src.schemas.video_analysis import AnalyzeVideoRequest, AnalyzeVideoResponse
from src.service.video_analysis import analysis_task_store as store
from src.service.video_analysis.semantic_analysis import analyze_video, resolve_analysis_models
from src.service.video_analysis.model_config_store import snapshot
from src.service.video_analysis.platform_runtime import (
    ensure_writable_directory, video_analysis_report_root, video_analysis_task_db,
)


class AnalysisTaskManager:
    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._guard = threading.RLock()
        self._lock_file = None
        self._remaining_workers = 0

    def start(self) -> None:
        with self._guard:
            if any(thread.is_alive() for thread in self._threads):
                if self._stop.is_set():
                    raise RuntimeError("视频分析 worker 正在停止")
                return
            self._acquire_process_lock()
            self._stop.clear()
            try:
                for task in store.list_pending_tasks():
                    if task["status"] == "running":
                        store.update_task(
                            task["task_id"], status="failed", stage="failed", progress=100,
                            error_message=(
                                "服务重启时任务仍在运行；为避免重复调用模型和重复生成草稿，"
                                "该任务未自动重跑，请核实后重新提交。"
                            ),
                            completed_at=datetime.now(timezone.utc).isoformat(),
                        )
                        logger.warning(
                            "analysis_task event=interrupted_on_restart task_id=%s",
                            task["task_id"],
                        )
                    else:
                        self._queue.put(task["task_id"])
                workers = min(8, max(1, int(os.getenv("VIDEO_ANALYSIS_TASK_CONCURRENCY", "2"))))
                self._remaining_workers = workers
                self._threads = [
                    threading.Thread(
                        target=self._run, name=f"video-analysis-worker-{index + 1}", daemon=True,
                    )
                    for index in range(workers)
                ]
                for thread in self._threads:
                    thread.start()
            except BaseException:
                self._release_process_lock()
                raise

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)

    def _acquire_process_lock(self) -> None:
        path = video_analysis_task_db().with_suffix(".worker.lock")
        ensure_writable_directory(path.parent)
        handle = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if path.stat().st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError(
                "视频分析任务库已有服务实例；当前版本要求单进程部署"
            ) from None
        self._lock_file = handle

    def _release_process_lock(self) -> None:
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    def _worker_finished(self) -> None:
        with self._guard:
            self._remaining_workers -= 1
            if self._remaining_workers == 0:
                self._release_process_lock()

    def submit(self, request: AnalyzeVideoRequest) -> dict:
        bindings = {k: snapshot(v) for k, v in resolve_analysis_models(request).items()}
        task_id = f"va_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
        store.create_task(task_id, json.dumps(
            {**request.model_dump(), "_model_bindings": bindings}, ensure_ascii=False,
        ))
        logger.info("analysis_task event=queued task_id=%s", task_id)
        self._queue.put(task_id)
        return store.get_task(task_id)  # type: ignore[return-value]

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    task_id = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if self._stop.is_set():
                    self._queue.put(task_id)
                    self._queue.task_done()
                    break
                try:
                    self._execute(task_id)
                finally:
                    self._queue.task_done()
        finally:
            self._worker_finished()

    def _execute(self, task_id: str) -> None:
        task = store.claim_task(task_id)
        if not task:
            return
        try:
            data = json.loads(task["request_json"])
            bindings = data.pop("_model_bindings", None)
            if bindings is None:
                raise ValueError("Legacy queued task: resubmit with online model IDs")
            request = AnalyzeVideoRequest.model_validate(data)
            store.merge_timings(task_id, task_started_at=datetime.now(timezone.utc).isoformat())
            logger.info("analysis_task event=started task_id=%s", task_id)
            store.update_task(task_id, stage="detecting_scenes", progress=15)
            result = asyncio.run(self._analyze(request, task_id, bindings))
            output = ensure_writable_directory(video_analysis_report_root())
            result_path = output / f"{task_id}.json"
            result_path.write_text(
                json.dumps(result.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            store.update_task(
                task_id, stage="creating_draft", progress=97,
                result_path=str(result_path),
            )
            draft_started = time.monotonic()
            # 分析报告落盘后自动创建一个完整草稿：一个草稿、一个视频轨道、多个分镜片段。
            # 草稿失败不影响分析结果，错误会作为 warning 返回。
            try:
                from src.service.video_analysis.draft_builder import create_draft_from_analysis

                draft_info = asyncio.run(
                    create_draft_from_analysis(task_id, embed_scene_clips=True)
                )
                result.draft = draft_info
                result.warnings = [
                    warning for warning in result.warnings
                    if not warning.startswith("自动创建草稿失败:")
                ]
                result_path.write_text(
                    json.dumps(result.model_dump(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.info(
                    "analysis_task event=draft_created task_id=%s draft_id=%s",
                    task_id, draft_info.get("draft_id"),
                )
            except Exception as exc:
                warning = f"自动创建草稿失败: {exc}"
                result.warnings.append(warning)
                result_path.write_text(
                    json.dumps(result.model_dump(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                logger.warning(
                    "analysis_task event=draft_create_failed task_id=%s error=%s",
                    task_id, exc,
                )
            store.merge_timings(
                task_id, draft_create_seconds=round(time.monotonic() - draft_started, 3),
            )
            store.merge_timings(
                task_id,
                report_write_seconds=round(
                    (datetime.now(timezone.utc) -
                     datetime.fromisoformat(task["started_at"])).total_seconds(), 3
                ) if task.get("started_at") else None,
            )
            warnings = result.warnings
            store.update_task(
                task_id,
                status="completed_with_warnings" if warnings else "completed",
                stage="completed", progress=100, result_path=str(result_path),
                completed_at=datetime.now(timezone.utc).isoformat(),
                error_message="\n".join(warnings),
            )
            logger.info(
                "analysis_task event=completed task_id=%s status=%s",
                task_id, "completed_with_warnings" if warnings else "completed",
            )
        except Exception as exc:
            store.update_task(
                task_id, status="failed", stage="failed", progress=100,
                error_message=str(exc),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            logger.exception("analysis_task event=failed task_id=%s error=%s", task_id, exc)

    async def _analyze(self, request: AnalyzeVideoRequest, task_id: str, bindings=None) -> AnalyzeVideoResponse:
        result = await analyze_video(
            request,
            model_bindings=bindings,
            task_id=task_id,
            progress=lambda stage, percent, details=None: self._progress(
                task_id, stage, percent, details,
            ),
        )
        store.update_task(task_id, stage="reporting", progress=95)
        return result

    @staticmethod
    def _progress(task_id: str, stage: str, percent: int, details=None) -> None:
        store.update_task(task_id, stage=stage, progress=percent)
        if details:
            store.merge_timings(task_id, **details)
            logger.info(
                "analysis_task event=%s task_id=%s details=%s",
                details.get("event", "progress"), task_id, details,
            )


manager = AnalysisTaskManager()
