"""Node-local durable state for analysis replacement-draft jobs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import threading

from ..platform_runtime import ensure_writable_directory, video_analysis_database_file
from ..shared_results import publisher

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _connection():
    path = video_analysis_database_file("replacement-drafts.sqlite3")
    ensure_writable_directory(path.parent)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""CREATE TABLE IF NOT EXISTS replacement_draft_tasks (
        task_id TEXT PRIMARY KEY,
        source_analysis_task_id TEXT NOT NULL,
        status TEXT NOT NULL,
        stage TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0,
        request_json TEXT NOT NULL,
        versions_json TEXT NOT NULL DEFAULT '[]',
        error_message TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_replacement_draft_status "
                 "ON replacement_draft_tasks(status,created_at)")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["request"] = json.loads(item.pop("request_json") or "{}")
    item["versions"] = json.loads(item.pop("versions_json") or "[]")
    return item


def create(task_id: str, source_analysis_task_id: str, request: dict) -> dict:
    with _lock, _connection() as conn:
        conn.execute("""INSERT INTO replacement_draft_tasks(
            task_id,source_analysis_task_id,status,stage,progress,request_json,created_at
        ) VALUES(?,?, 'queued','queued',0,?,?)""", (
            task_id, source_analysis_task_id, json.dumps(request, ensure_ascii=False), _now(),
        ))
    task = get(task_id)
    publisher.publish("video_analysis_replacement_draft", task)
    return task


def get_local(task_id: str) -> dict | None:
    with _lock, _connection() as conn:
        row = conn.execute("SELECT * FROM replacement_draft_tasks WHERE task_id=?", (task_id,)).fetchone()
    return _decode(row)


def get(task_id: str) -> dict | None:
    task = get_local(task_id)
    if task is not None:
        return task
    return publisher.get_shared("video_analysis_replacement_draft", task_id)


def queued() -> list[str]:
    with _lock, _connection() as conn:
        rows = conn.execute("SELECT task_id FROM replacement_draft_tasks "
                            "WHERE status='queued' ORDER BY created_at").fetchall()
    return [str(row[0]) for row in rows]


def claim(task_id: str) -> bool:
    with _lock, _connection() as conn:
        cursor = conn.execute("""UPDATE replacement_draft_tasks
            SET status='running',stage='loading_analysis',progress=2,
                started_at=COALESCE(started_at,?)
            WHERE task_id=? AND status='queued'""", (_now(), task_id))
        claimed = cursor.rowcount == 1
    if claimed:
        publisher.publish("video_analysis_replacement_draft", get(task_id))
    return claimed


def update(task_id: str, **values) -> dict | None:
    fields = {}
    for key in ("status", "stage", "progress", "error_message", "completed_at", "started_at"):
        if key in values:
            fields[key] = values[key]
    if "versions" in values:
        fields["versions_json"] = json.dumps(values["versions"], ensure_ascii=False)
    if not fields:
        return get(task_id)
    assignments = ",".join(f"{key}=?" for key in fields)
    with _lock, _connection() as conn:
        conn.execute(f"UPDATE replacement_draft_tasks SET {assignments} WHERE task_id=?",
                     (*fields.values(), task_id))
    task = get(task_id)
    publisher.publish("video_analysis_replacement_draft", task)
    return task


def fail_interrupted() -> int:
    with _lock, _connection() as conn:
        cursor = conn.execute("""UPDATE replacement_draft_tasks SET
            status='failed',stage='failed',progress=100,
            error_message='服务重启时裂变草稿任务未完成，请重新提交',completed_at=?
            WHERE status IN ('queued','running')""", (_now(),))
        return cursor.rowcount


def fail_running() -> int:
    with _lock, _connection() as conn:
        task_ids = [row[0] for row in conn.execute(
            "SELECT task_id FROM replacement_draft_tasks WHERE status='running'",
        ).fetchall()]
        cursor = conn.execute("""UPDATE replacement_draft_tasks SET
            status='failed',stage='failed',progress=100,
            error_message='服务重启时裂变草稿任务正在执行；已生成草稿请核实后再提交',completed_at=?
            WHERE status='running'""", (_now(),))
        failed = cursor.rowcount
    for task_id in task_ids:
        publisher.publish("video_analysis_replacement_draft", get(str(task_id)))
    return failed

