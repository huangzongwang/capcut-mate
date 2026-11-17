"""SQLite persistence for long-running video-analysis tasks."""
from __future__ import annotations

import os
import sqlite3
import threading
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.service.video_analysis.platform_runtime import (
    ensure_writable_directory, legacy_video_analysis_task_db, video_analysis_task_db,
)
from src.service.video_analysis.shared_results import publisher

_lock = threading.RLock()


def _connect_path(path: Path, *, writable: bool = True) -> sqlite3.Connection:
    if writable:
        ensure_writable_directory(path.parent)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _connect() -> sqlite3.Connection:
    return _connect_path(video_analysis_task_db())


def ensure_schema() -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS video_analysis_tasks (
                    task_id TEXT PRIMARY KEY NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    request_json TEXT NOT NULL,
                    result_path TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    current_scene INTEGER,
                    total_scenes INTEGER,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    heartbeat_at TEXT
                    ,timings_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(video_analysis_tasks)"
                ).fetchall()
            }
            if "timings_json" not in columns:
                conn.execute(
                    "ALTER TABLE video_analysis_tasks "
                    "ADD COLUMN timings_json TEXT NOT NULL DEFAULT '{}'"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vat_status_created "
                "ON video_analysis_tasks(status, created_at)"
            )
            conn.commit()
        finally:
            conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_task(task_id: str, request_json: str) -> dict[str, Any]:
    now = _now()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO video_analysis_tasks
                (task_id, status, stage, progress, request_json, created_at, heartbeat_at)
                VALUES (?, 'queued', 'queued', 0, ?, ?, ?)
                """,
                (task_id, request_json, now, now),
            )
            conn.commit()
        finally:
            conn.close()
    task = get_task(task_id)  # type: ignore[assignment]
    publisher.publish("video_analysis", task)
    return task  # type: ignore[return-value]


def get_task(task_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM video_analysis_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row:
                return dict(row)
        finally:
            conn.close()
    legacy = legacy_video_analysis_task_db()
    if legacy != video_analysis_task_db() and legacy.is_file():
        try:
            conn = _connect_path(legacy, writable=False)
            try:
                row = conn.execute(
                    "SELECT * FROM video_analysis_tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
                if row:
                    return dict(row)
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return publisher.get_shared("video_analysis", task_id)


def list_pending_tasks() -> list[dict[str, Any]]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM video_analysis_tasks
                WHERE status IN ('queued', 'running')
                ORDER BY created_at ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


def claim_task(task_id: str) -> dict[str, Any] | None:
    """Atomically move one queued task to running before any paid work begins."""
    now = _now()
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE video_analysis_tasks
                SET status='running', stage='preparing', progress=MAX(progress, 5),
                    started_at=COALESCE(started_at, ?), heartbeat_at=?
                WHERE task_id=? AND status='queued'
                """,
                (now, now, task_id),
            )
            conn.commit()
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM video_analysis_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            task = dict(row) if row else None
            publisher.publish("video_analysis", task)
            return task
        finally:
            conn.close()


def update_task(task_id: str, **values: Any) -> dict[str, Any] | None:
    allowed = {
        "status", "stage", "progress", "result_path", "error_message",
        "current_scene", "total_scenes", "started_at", "completed_at",
        "timings_json",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return get_task(task_id)
    values["heartbeat_at"] = _now()
    assignments, parameters = [], []
    for key, value in values.items():
        if key == "progress":
            assignments.append("progress = MAX(progress, ?)")
            parameters.append(value)
        elif key == "stage" and "progress" in values:
            assignments.append("stage = CASE WHEN progress <= ? THEN ? ELSE stage END")
            parameters.extend((values["progress"], value))
        else:
            assignments.append(f"{key} = ?")
            parameters.append(value)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                f"UPDATE video_analysis_tasks SET {', '.join(assignments)} WHERE task_id = ?",
                (*parameters, task_id),
            )
            conn.commit()
        finally:
            conn.close()
    task = get_task(task_id)
    publisher.publish("video_analysis", task)
    return task


def merge_timings(task_id: str, **values: Any) -> dict[str, Any] | None:
    """Merge live timing data without overwriting timings from earlier phases."""
    with _lock:
        task = get_task(task_id)
        if not task:
            return None
        try:
            timings = json.loads(task.get("timings_json") or "{}")
        except (TypeError, ValueError):
            timings = {}
        event = dict(values)
        event["recorded_at"] = _now()
        events = timings.setdefault("events", [])
        if isinstance(events, list):
            events.append(event)
        timings.update(values)
        timings["events"] = events
        return update_task(task_id, timings_json=json.dumps(timings, ensure_ascii=False))


ensure_schema()
