"""Durable audio-task status; credentials are deliberately never persisted."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import threading
from typing import Any

from ..platform_runtime import (
    ensure_writable_directory, legacy_video_analysis_database_root,
    video_analysis_database_file, video_analysis_database_root,
)
from ..shared_results import publisher


_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    directory = ensure_writable_directory(video_analysis_database_root())
    conn = sqlite3.connect(video_analysis_database_file("audio-generation-tasks.sqlite3"), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audio_generation_tasks (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            stage TEXT NOT NULL,
            progress INTEGER NOT NULL,
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            details_json TEXT NOT NULL DEFAULT '{}'
        )
    """)
    return conn


@contextmanager
def _connection():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(task_id: str) -> dict[str, Any]:
    task = {
        "task_id": task_id, "status": "queued", "stage": "queued", "progress": 0,
        "error_message": "", "created_at": _now(), "completed_at": None,
    }
    with _lock, _connection() as conn:
        conn.execute(
            "INSERT INTO audio_generation_tasks "
            "(task_id,status,stage,progress,error_message,created_at,completed_at) "
            "VALUES (:task_id,:status,:stage,:progress,:error_message,:created_at,:completed_at)",
            task,
        )
    publisher.publish("audio_generation", task)
    return task


def get(task_id: str) -> dict[str, Any] | None:
    with _lock, _connection() as conn:
        row = conn.execute(
            "SELECT * FROM audio_generation_tasks WHERE task_id=?", (task_id,),
        ).fetchone()
    if not row:
        legacy = legacy_video_analysis_database_root() / "audio-generation-tasks.sqlite3"
        if legacy != video_analysis_database_root() / "audio-generation-tasks.sqlite3" and legacy.is_file():
            try:
                conn = sqlite3.connect(legacy, timeout=30)
                conn.row_factory = sqlite3.Row
                try:
                    row = conn.execute(
                        "SELECT * FROM audio_generation_tasks WHERE task_id=?", (task_id,),
                    ).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error:
                row = None
    if not row:
        return publisher.get_shared("audio_generation", task_id)
    task = dict(row)
    task.update(json.loads(task.pop("details_json") or "{}"))
    return task


def update(task_id: str, **fields: Any) -> None:
    details = {
        key: value for key, value in fields.items()
        if key not in {"status", "stage", "progress", "error_message", "completed_at"}
    }
    allowed = {key: value for key, value in fields.items() if key not in details}
    assignments = [f"{key}=?" for key in allowed]
    values = list(allowed.values())
    with _lock:
        if details:
            current = get(task_id)
            if current is None:
                return
            merged = {
                key: value for key, value in current.items()
                if key not in {
                    "task_id", "status", "stage", "progress", "error_message",
                    "created_at", "completed_at",
                }
            }
            merged.update(details)
            assignments.append("details_json=?")
            values.append(json.dumps(merged, ensure_ascii=False))
        if not assignments:
            return
        with _connection() as conn:
            conn.execute(
                f"UPDATE audio_generation_tasks SET {','.join(assignments)} WHERE task_id=?",
                (*values, task_id),
            )
        publisher.publish("audio_generation", get(task_id))


def fail_unfinished() -> int:
    message = "服务重启后任务未自动重跑；凭证未持久化，请确认后重新提交"
    with _lock, _connection() as conn:
        task_ids = [row[0] for row in conn.execute(
            "SELECT task_id FROM audio_generation_tasks WHERE status IN ('queued','running')"
        ).fetchall()]
        cursor = conn.execute(
            "UPDATE audio_generation_tasks SET status='failed',stage='failed',progress=100,"
            "error_message=?,completed_at=? WHERE status IN ('queued','running')",
            (message, _now()),
        )
        count = cursor.rowcount
    for task_id in task_ids:
        publisher.publish("audio_generation", get(task_id))
    return count
