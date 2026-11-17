"""Single-process worker with a durable SQLite queue and an OS ownership lock."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
import shutil
import threading
import time

from . import planner, task_store
from .schemas import GenerateRequest, Storyboard
from ..model_config_store import snapshot
from ..responses_client import Scope
from ..platform_runtime import (
    analysis_temporary_directory, ensure_writable_directory, video_analysis_report_root,
)


log = logging.getLogger(__name__)
PROGRESS = {"planning": 10, "saving": 90}


def result_root() -> Path:
    configured = os.getenv("STORYBOARD_RESULT_ROOT")
    directory = Path(configured).resolve() if configured else video_analysis_report_root() / "storyboards"
    return ensure_writable_directory(directory)


class TaskManager:
    def __init__(self):
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._wake = queue.Queue(maxsize=1)
        self._thread = None
        self._owner = None

    def _lock(self):
        path = task_store.database_path().with_suffix(".worker.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
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
            raise RuntimeError("分镜任务库已有Worker，请使用单进程部署并停止旧实例") from None
        self._owner = handle

    def start(self):
        with self._guard:
            if self._thread and self._thread.is_alive():
                if self._stop.is_set():
                    raise RuntimeError("分镜Worker正在停止")
                return
            self._lock()
            try:
                self._recover()
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="storyboard-worker", daemon=True)
                self._thread.start()
            except BaseException:
                self._owner.close()
                self._owner = None
                raise

    def stop(self):
        with self._guard:
            self._stop.set()
            self._notify()
            worker = self._thread
        if worker:
            worker.join(timeout=5)
        # The worker retains ownership while an in-flight model call finishes.

    def _notify(self):
        try:
            self._wake.put_nowait(True)
        except queue.Full:
            pass

    def submit(self, request: GenerateRequest) -> dict:
        binding = snapshot(planner.configured_model(request))
        self.start()
        with self._guard:
            if self._stop.is_set():
                raise RuntimeError("分镜Worker正在停止")
            task = task_store.create(request, binding)
            self._notify()
            return task

    def _publish(self, task_id: str, board: Storyboard, timings: dict):
        root = result_root()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{task_id}.json"
        with analysis_temporary_directory("storyboard-result-") as directory:
            temporary = Path(directory) / path.name
            with temporary.open("w", encoding="utf-8") as output:
                output.write(board.model_dump_json())
                output.flush()
                os.fsync(output.fileno())
            shutil.copyfile(temporary, path)
        task_store.finish(task_id, str(path), board, timings)

    def _recover(self):
        for task in task_store.running():
            if task["result_json"]:
                try:
                    self._publish(
                        task["task_id"], Storyboard.model_validate_json(task["result_json"]),
                        json.loads(task["timings_json"]),
                    )
                except Exception:
                    task_store.fail(task["task_id"], "分镜结果恢复失败，请检查存储")
            else:
                task_store.fail(task["task_id"], "服务中断，无法确认模型是否完成；请人工决定是否重新提交")

    def _heartbeat(self, task_id: str, done: threading.Event):
        while not done.wait(5):
            try:
                task_store.heartbeat(task_id)
            except Exception:
                log.error("Storyboard heartbeat persistence failed: %s", task_id)

    def _execute(self, task: dict):
        task_id = task["task_id"]
        timings = {}
        previous = None
        started = time.monotonic()

        def report(name):
            nonlocal previous, started
            current = time.monotonic()
            if previous:
                key = previous + "_ms"
                timings[key] = timings.get(key, 0) + round((current - started) * 1000)
            previous, started = name, current
            task_store.stage(task_id, name, PROGRESS[name], timings)

        def model_event(event):
            task_store.model_event(task_id, event)

        def record_failed_stage():
            if previous:
                timings[previous + "_ms"] = timings.get(previous + "_ms", 0) + round(
                    (time.monotonic() - started) * 1000,
                )
                task_store.stage(task_id, previous, PROGRESS[previous], timings)

        done = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(task_id, done), daemon=True)
        heartbeat.start()
        try:
            data = json.loads(task["request_json"])
            binding = data.pop("_model_binding", None)
            if binding is None:
                raise planner.ModelUnavailable("Legacy queued task: resubmit with online model_id")
            request = GenerateRequest.model_validate(data)
            config = planner.configured_model(request, binding)
            scope = Scope(task_id=task_id, on_event=model_event, deadline=time.monotonic() + 600)
            shots, model = planner.generate(request, config=config, on_stage=report, scope=scope)
            report("saving")
            board = task_store.save_board(task_id, request, shots, model)
            timings["saving_ms"] = round((time.monotonic() - started) * 1000)
            self._publish(task_id, board, timings)
        except planner.PlanningError as exc:
            record_failed_stage()
            task_store.fail(task_id, str(exc))
        except Exception:
            record_failed_stage()
            # Never persist raw provider errors, credentials or script contents.
            task_store.fail(task_id, "分镜任务执行或结果保存失败，请检查服务日志及存储")
            log.error("Storyboard task failed: %s", task_id)
        finally:
            done.set()
            heartbeat.join()

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    task = task_store.claim()
                    if task:
                        self._execute(task)
                        continue
                except Exception:
                    log.error("Storyboard queue persistence unavailable")
                try:
                    self._wake.get(timeout=1)
                except queue.Empty:
                    pass
        finally:
            self._owner.close()


manager = TaskManager()
