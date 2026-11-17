from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import threading
import time

from dotenv import dotenv_values

from ..model_config_store import DB_FILE
from ..platform_runtime import video_analysis_database_file, video_analysis_node_id
from . import store


_wake = threading.Event()
_started = False
_start_lock = threading.Lock()


def _database_path():
    return video_analysis_database_file("shared-result-outbox.sqlite3", migrate=False)


@contextmanager
def _connect():
    conn = sqlite3.connect(str(_database_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""CREATE TABLE IF NOT EXISTS shared_result_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        task_type TEXT NOT NULL,
        revision INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        UNIQUE(task_id,revision)
    )""")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _public_base_url() -> str | None:
    values = dotenv_values(DB_FILE, encoding="utf-8-sig") if DB_FILE.is_file() else {}
    value = os.getenv("VIDEO_ANALYSIS_PUBLIC_BASE_URL") or values.get("VIDEO_ANALYSIS_PUBLIC_BASE_URL")
    return str(value).rstrip("/") if value else None


def _json_value(value, fallback):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def _safe_result(task_type: str, task: dict) -> dict:
    common = {
        key: task.get(key) for key in (
            "result_path", "current_scene", "total_scenes", "storyboard_id",
            "source_analysis_task_id",
            "download_url", "format", "sample_rate", "channels", "sample_width_bits",
            "size_bytes", "preview_path", "preview_url", "total_count", "completed_count",
            "failed_count", "cancelled_count", "cancel_requested",
        ) if task.get(key) is not None
    }
    if task_type == "video_analysis":
        common["current_scene"] = task.get("current_scene")
        common["total_scenes"] = task.get("total_scenes")
        common["timings_json"] = task.get("timings_json") or "{}"
    elif task_type == "script_video_batch":
        common["versions"] = task.get("versions") or []
        request = task.get("request") or _json_value(task.get("request_json"), {})
        common["request"] = {"count": int(request.get("count", 20))}
    elif task_type == "audio_generation":
        excluded = {
            "task_id", "status", "stage", "progress", "error_message", "created_at",
            "completed_at", "request", "request_json", "cookie", "bpid",
        }
        common.update({key: value for key, value in task.items() if key not in excluded})
    elif task_type == "digital_human":
        pass
    elif task_type == "script_storyboard":
        common["storyboard_id"] = task.get("storyboard_id")
        common["timings_json"] = task.get("timings_json") or "{}"
        common["model_request_json"] = task.get("model_request_json") or "{}"
    elif task_type == "local_export":
        common["items"] = task.get("items") or []
        common["request"] = {}
    elif task_type == "video_analysis_replacement_draft":
        common["versions"] = task.get("versions") or []
        request = task.get("request") or _json_value(task.get("request_json"), {})
        common["request"] = {"count": int(request.get("count", len(common["versions"] or [])))}
    return common


def _snapshot(task_type: str, task: dict) -> dict:
    return {
        "task_id": task["task_id"],
        "task_type": task_type,
        "owner_node_id": video_analysis_node_id() or "default",
        "owner_api_base_url": _public_base_url(),
        "status": str(task.get("status") or "unknown"),
        "stage": str(task.get("stage") or task.get("status") or "unknown"),
        "progress": int(task.get("progress") or 0),
        "revision": time.time_ns(),
        "result_json": _safe_result(task_type, task),
        "result_manifest_url": task.get("result_manifest_url"),
        "error_message": str(task.get("error_message") or "")[:4000],
        "created_at": str(task.get("created_at") or ""),
        "started_at": task.get("started_at"),
        "heartbeat_at": task.get("heartbeat_at"),
        "completed_at": task.get("completed_at"),
    }


def publish(task_type: str, task: dict | None) -> None:
    if not task or not task.get("task_id"):
        return
    try:
        payload = _snapshot(task_type, task)
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO shared_result_outbox "
                "(task_id,task_type,revision,payload_json) VALUES(?,?,?,?)",
                (payload["task_id"], task_type, payload["revision"],
                 json.dumps(payload, ensure_ascii=False)),
            )
        start()
        _wake.set()
    except Exception:
        # Shared indexing must never stop local task execution.
        return


def get_shared(task_type: str, task_id: str) -> dict | None:
    try:
        row = store.get(task_id, task_type)
    except Exception:
        return None
    if not row:
        return None
    result = dict(row.get("result_json") or {})
    result.update({
        "task_id": row["task_id"], "status": row["status"], "stage": row["stage"],
        "progress": row["progress"], "error_message": row.get("error_message") or "",
        "created_at": row.get("created_at") or "", "started_at": row.get("started_at"),
        "heartbeat_at": row.get("heartbeat_at"), "completed_at": row.get("completed_at"),
        "owner_node_id": row.get("owner_node_id"), "result_source": "shared_mysql",
    })
    return result


def _drain_once() -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM shared_result_outbox WHERE next_attempt_at<=? ORDER BY id LIMIT 1",
            (time.time(),),
        ).fetchone()
    if not row:
        return False
    try:
        store.upsert(json.loads(row["payload_json"]))
    except Exception as exc:
        attempts = int(row["attempts"]) + 1
        with _connect() as conn:
            conn.execute(
                "UPDATE shared_result_outbox SET attempts=?,next_attempt_at=?,last_error=? WHERE id=?",
                (attempts, time.time() + min(300, 2 ** min(attempts, 8)), str(exc)[:500], row["id"]),
            )
        return False
    with _connect() as conn:
        conn.execute("DELETE FROM shared_result_outbox WHERE id=?", (row["id"],))
    return True


def _run() -> None:
    while True:
        processed = False
        try:
            while _drain_once():
                processed = True
        except Exception:
            pass
        _wake.wait(1 if processed else 5)
        _wake.clear()


def start() -> None:
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        threading.Thread(target=_run, name="video-analysis-result-sync", daemon=True).start()
        _started = True


start()
