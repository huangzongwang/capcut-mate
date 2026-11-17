from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import threading
import uuid

from ..platform_runtime import (
    digital_human_root, ensure_writable_directory,
    video_analysis_database_file, video_analysis_database_root,
)
from ..shared_results import publisher

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _connect():
    path = video_analysis_database_file("digital-human-tasks.sqlite3")
    ensure_writable_directory(path.parent)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS digital_human_tasks (
        task_id TEXT PRIMARY KEY, provider_code TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, stage TEXT NOT NULL,
        progress INTEGER NOT NULL, request_json TEXT NOT NULL,
        error_message TEXT NOT NULL DEFAULT '', preview_path TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, completed_at TEXT
    )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(digital_human_tasks)")}
    if "provider_code" not in columns:
        conn.execute("ALTER TABLE digital_human_tasks ADD COLUMN provider_code TEXT NOT NULL DEFAULT ''")
    # A queued legacy row has not reached Duix yet, so assigning it a UUID is safe.
    for row in conn.execute(
        "SELECT task_id FROM digital_human_tasks WHERE status='queued' AND provider_code=''"
    ).fetchall():
        conn.execute(
            "UPDATE digital_human_tasks SET provider_code=? WHERE task_id=?",
            (str(uuid.uuid4()), row["task_id"]),
        )
    conn.commit()


def create(request: dict) -> dict:
    task_id, provider_code, now = "dh_" + uuid.uuid4().hex, str(uuid.uuid4()), _now()
    with _lock, _connection() as conn:
        conn.execute(
            "INSERT INTO digital_human_tasks (task_id,provider_code,status,stage,progress,request_json,created_at) "
            "VALUES (?,?,'queued','queued',0,?,?)",
            (task_id, provider_code, json.dumps(request, ensure_ascii=False), now),
        )
    task = get(task_id)
    publisher.publish("digital_human", task)
    return task


def get(task_id: str) -> dict | None:
    with _lock, _connection() as conn:
        row = conn.execute("SELECT * FROM digital_human_tasks WHERE task_id=?", (task_id,)).fetchone()
    if row:
        return dict(row)
    legacy = digital_human_root() / "database" / "tasks.sqlite3"
    if legacy.is_file():
        try:
            conn = sqlite3.connect(legacy, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                _ensure_schema(conn)
                row = conn.execute(
                    "SELECT * FROM digital_human_tasks WHERE task_id=?", (task_id,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            row = None
        if row:
            return dict(row)
    return publisher.get_shared("digital_human", task_id)


def list_recoverable() -> list[dict]:
    with _lock, _connection() as conn:
        rows = conn.execute(
            "SELECT task_id,provider_code,request_json FROM digital_human_tasks "
            "WHERE status='running' AND stage IN ('generating','upstream_0','upstream_1','timed_out')"
        ).fetchall()
    return [dict(row) for row in rows]


def list_queued() -> list[dict]:
    with _lock, _connection() as conn:
        rows = conn.execute(
            "SELECT task_id,provider_code,request_json FROM digital_human_tasks "
            "WHERE status='queued' AND stage='queued'"
        ).fetchall()
    return [dict(row) for row in rows]


def get_recoverable(task_id: str) -> dict | None:
    with _lock, _connection() as conn:
        row = conn.execute(
            "SELECT task_id,provider_code,request_json FROM digital_human_tasks "
            "WHERE task_id=? AND status='running' "
            "AND stage IN ('generating','upstream_0','upstream_1','timed_out')",
            (task_id,),
        ).fetchone()
    return dict(row) if row else None


def fail_ambiguous_submissions() -> None:
    with _lock, _connection() as conn:
        task_ids = [row[0] for row in conn.execute(
            "SELECT task_id FROM digital_human_tasks WHERE status='running' "
            "AND stage IN ('preparing','submitting')"
        ).fetchall()]
        conn.execute(
            "UPDATE digital_human_tasks SET status='failed',stage='failed',progress=100, "
            "error_message='服务在提交阶段中断；为避免重复生成，请核对Duix任务后再提交', "
            "completed_at=? WHERE status='running' AND stage='preparing'",
            (_now(),),
        )
        conn.execute(
            "UPDATE digital_human_tasks SET stage='timed_out', "
            "error_message='提交阶段中断，正在使用原 UUID 查询以确认上游状态', "
            "progress=90 WHERE status='running' AND stage='submitting'",
        )
    for task_id in task_ids:
        publisher.publish("digital_human", get(task_id))


def update(task_id: str, **values) -> None:
    allowed = {"status", "stage", "progress", "error_message", "preview_path", "completed_at"}
    values = {key: value for key, value in values.items() if key in allowed}
    if not values:
        return
    assignments = ",".join(f"{key}=?" for key in values)
    with _lock, _connection() as conn:
        conn.execute(f"UPDATE digital_human_tasks SET {assignments} WHERE task_id=?", (*values.values(), task_id))
    publisher.publish("digital_human", get(task_id))

