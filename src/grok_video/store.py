from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any
from pathlib import Path

_lock = threading.RLock()
ROOT = Path(__file__).resolve().parents[2]


def database_path():
    return Path(os.getenv("GROK_VIDEO_DB_PATH", str(ROOT / "db/grok_video.sqlite3"))).resolve()


def result_root():
    return Path(os.getenv("GROK_VIDEO_RESULT_ROOT", str(ROOT / "reports/grok-video"))).resolve()


def _connect():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema():
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS grok_video_tasks (
                    task_id TEXT PRIMARY KEY NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    request_json TEXT NOT NULL,
                    model_snapshot_json TEXT NOT NULL,
                    provider_request_id TEXT NOT NULL DEFAULT '',
                    result_json TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    heartbeat_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_grok_video_status_created "
                "ON grok_video_tasks(status, created_at)"
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(grok_video_tasks)")}
            for name in ("image_snapshot_json", "image_response_json", "first_frame_json"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE grok_video_tasks ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            conn.commit()
        finally:
            conn.close()


def create(task_id: str, request: dict, model_snapshot: dict, image_snapshot: dict | None = None) -> dict:
    now = _now()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO grok_video_tasks
                (task_id, status, stage, request_json, model_snapshot_json, created_at, heartbeat_at,
                 image_snapshot_json)
                VALUES (?, 'queued', 'queued', ?, ?, ?, ?, ?)
                """,
                (task_id, json.dumps(request, ensure_ascii=False),
                 json.dumps(model_snapshot, ensure_ascii=False), now, now,
                 json.dumps(image_snapshot) if image_snapshot is not None else ""),
            )
            conn.commit()
        finally:
            conn.close()
    return get(task_id)


def get(task_id: str) -> dict | None:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM grok_video_tasks WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def pending() -> list[dict]:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM grok_video_tasks WHERE status IN ('queued','running') ORDER BY created_at"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


def update(task_id: str, **values: Any) -> dict | None:
    allowed = {
        "status", "stage", "progress", "provider_request_id", "result_json",
        "error_message", "started_at", "completed_at",
        "image_response_json", "first_frame_json",
    }
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return get(task_id)
    values["heartbeat_at"] = _now()
    assignment = ", ".join(f"{key}=?" for key in values)
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                f"UPDATE grok_video_tasks SET {assignment} WHERE task_id=?",
                (*values.values(), task_id),
            )
            conn.commit()
        finally:
            conn.close()
    return get(task_id)


def claim():
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM grok_video_tasks WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE grok_video_tasks SET status='running', heartbeat_at=? WHERE task_id=?",
                (_now(), row["task_id"]),
            )
            conn.commit()
            return get(row["task_id"])
        finally:
            conn.close()


def retry_download(task_id):
    with _lock:
        conn = _connect()
        try:
            result = conn.execute(
                "UPDATE grok_video_tasks SET status='queued', error_message='', completed_at=NULL "
                "WHERE task_id=? AND status='download_failed' AND provider_request_id!=''",
                (task_id,),
            ).rowcount
            conn.commit()
            return result == 1
        finally:
            conn.close()


def continue_first_frame(task_id):
    with _lock:
        conn = _connect()
        try:
            changed = conn.execute(
                "UPDATE grok_video_tasks SET status='queued', stage='first_frame_confirmed', heartbeat_at=? "
                "WHERE task_id=? AND status='awaiting_frame_confirmation' AND first_frame_json!=''",
                (_now(), task_id),
            ).rowcount
            conn.commit()
            return changed == 1
        finally:
            conn.close()


def retry_cached_stage(task_id):
    """Retry local frame preparation or a known video failure, never an unknown POST."""
    with _lock:
        conn = _connect()
        try:
            changed = conn.execute(
                "UPDATE grok_video_tasks SET status='queued', "
                "stage=CASE WHEN status='failed' THEN 'first_frame_confirmed' ELSE 'validating_first_frame' END, "
                "error_message='', completed_at=NULL, heartbeat_at=? "
                "WHERE task_id=? AND provider_request_id='' AND ("
                "(status='first_frame_failed' AND image_response_json!='') OR "
                "(status='failed' AND first_frame_json!=''))",
                (_now(), task_id),
            ).rowcount
            conn.commit()
            return changed == 1
        finally:
            conn.close()
