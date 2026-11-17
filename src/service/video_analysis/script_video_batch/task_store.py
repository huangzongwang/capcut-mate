"""Durable state for script-to-speech multi-draft jobs."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
import threading

from ..platform_runtime import (
    ensure_writable_directory, legacy_video_analysis_database_root,
    video_analysis_database_file, video_analysis_database_root,
)
from ..shared_results import publisher

_lock = threading.RLock()


def _connect():
    path = video_analysis_database_file("script-video-batch.sqlite3")
    ensure_writable_directory(path.parent)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""CREATE TABLE IF NOT EXISTS script_video_batch_tasks (
        task_id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0, request_json TEXT NOT NULL,
        storyboard_id TEXT, versions_json TEXT NOT NULL DEFAULT '[]',
        error_message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        started_at TEXT, completed_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_script_video_batch_status ON script_video_batch_tasks(status,created_at)")
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat()


def create(task_id: str, request: dict):
    with _lock, _connect() as conn:
        conn.execute("""INSERT INTO script_video_batch_tasks(
            task_id,status,stage,request_json,storyboard_id,created_at
        ) VALUES(?,'queued','queued',?,?,?)""", (
            task_id, json.dumps(request, ensure_ascii=False), request["storyboard_id"], _now(),
        ))
    task = get(task_id)
    publisher.publish("script_video_batch", task)
    return task


def get(task_id: str):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM script_video_batch_tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        legacy = legacy_video_analysis_database_root() / "script-video-batch.sqlite3"
        if legacy != video_analysis_database_root() / "script-video-batch.sqlite3" and legacy.is_file():
            try:
                conn = sqlite3.connect(legacy, timeout=30)
                conn.row_factory = sqlite3.Row
                try:
                    row = conn.execute(
                        "SELECT * FROM script_video_batch_tasks WHERE task_id=?", (task_id,),
                    ).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error:
                row = None
    if not row:
        return publisher.get_shared("script_video_batch", task_id)
    item = dict(row)
    item["versions"] = json.loads(item.pop("versions_json") or "[]")
    item["request"] = json.loads(item.pop("request_json") or "{}")
    return item


def claim(task_id: str) -> bool:
    with _lock, _connect() as conn:
        cursor = conn.execute("""UPDATE script_video_batch_tasks SET status='running',stage='loading_storyboard',
            progress=2,started_at=COALESCE(started_at,?) WHERE task_id=? AND status='queued'""",
            (_now(), task_id))
        claimed = cursor.rowcount == 1
    if claimed:
        publisher.publish("script_video_batch", get(task_id))
    return claimed


def update(task_id: str, **values):
    allowed = {"status", "stage", "progress", "storyboard_id", "error_message", "completed_at"}
    fields = {key: value for key, value in values.items() if key in allowed}
    if "versions" in values:
        fields["versions_json"] = json.dumps(values["versions"], ensure_ascii=False)
    if not fields:
        return get(task_id)
    assignments = ",".join(f"{key}=?" for key in fields)
    with _lock, _connect() as conn:
        conn.execute(f"UPDATE script_video_batch_tasks SET {assignments} WHERE task_id=?",
                     (*fields.values(), task_id))
    task = get(task_id)
    publisher.publish("script_video_batch", task)
    return task


def fail_interrupted():
    with _lock, _connect() as conn:
        task_ids = [row[0] for row in conn.execute(
            "SELECT task_id FROM script_video_batch_tasks WHERE status IN ('queued','running')"
        ).fetchall()]
        count = conn.execute("""UPDATE script_video_batch_tasks SET status='failed',stage='failed',progress=100,
            error_message='服务重启时批量草稿仍在队列或执行；为避免重复生成，请核实已创建草稿后重新提交',
            completed_at=? WHERE status IN ('queued','running')""", (_now(),)).rowcount
    for task_id in task_ids:
        publisher.publish("script_video_batch", get(task_id))
    return count
