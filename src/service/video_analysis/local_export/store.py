"""Durable SQLite state for local Jianying export tasks."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading

from ..platform_runtime import ensure_writable_directory, video_analysis_database_file
from ..shared_results import publisher


_lock = threading.RLock()


def database_path() -> Path:
    return video_analysis_database_file("local-export.sqlite3")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _connect():
    path = database_path()
    ensure_writable_directory(path.parent)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""CREATE TABLE IF NOT EXISTS local_export_tasks (
        task_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        stage TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0,
        request_json TEXT NOT NULL,
        total_count INTEGER NOT NULL,
        completed_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        cancelled_count INTEGER NOT NULL DEFAULT 0,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        error_message TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS local_export_items (
        item_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        draft_id TEXT NOT NULL,
        status TEXT NOT NULL,
        stage TEXT NOT NULL,
        progress INTEGER NOT NULL DEFAULT 0,
        output_path TEXT NOT NULL DEFAULT '',
        video_url TEXT NOT NULL DEFAULT '',
        error_message TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0,
        timings_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        FOREIGN KEY(task_id) REFERENCES local_export_tasks(task_id) ON DELETE CASCADE,
        UNIQUE(task_id, position),
        UNIQUE(task_id, draft_id)
    )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_export_tasks_status "
        "ON local_export_tasks(status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_local_export_items_task "
        "ON local_export_items(task_id, position)"
    )
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _decode_item(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["timings"] = json.loads(item.pop("timings_json") or "{}")
    return item


def _decode_task(row: sqlite3.Row, items: list[sqlite3.Row]) -> dict:
    task = dict(row)
    task["request"] = json.loads(task.pop("request_json") or "{}")
    task["cancel_requested"] = bool(task["cancel_requested"])
    task["items"] = [_decode_item(item) for item in items]
    return task


def create(task_id: str, request: dict) -> dict:
    created_at = now()
    draft_ids = list(request["draft_ids"])
    with _lock, _connect() as conn:
        conn.execute("""INSERT INTO local_export_tasks(
            task_id,status,stage,progress,request_json,total_count,created_at
        ) VALUES(?,'queued','queued',0,?,?,?)""", (
            task_id, json.dumps(request, ensure_ascii=False), len(draft_ids), created_at,
        ))
        conn.executemany("""INSERT INTO local_export_items(
            item_id,task_id,position,draft_id,status,stage,progress,created_at
        ) VALUES(?,?,?,?,'queued','queued',0,?)""", [
            (f"lei_{task_id[4:]}_{position:02d}", task_id, position, draft_id, created_at)
            for position, draft_id in enumerate(draft_ids, 1)
        ])
    task = get(task_id)
    publisher.publish("local_export", task)
    return task


def get(task_id: str) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM local_export_tasks WHERE task_id=?", (task_id,),
        ).fetchone()
        items = [] if row is None else conn.execute(
            "SELECT * FROM local_export_items WHERE task_id=? ORDER BY position", (task_id,),
        ).fetchall()
    if row is None:
        return publisher.get_shared("local_export", task_id)
    return _decode_task(row, items)


def queued_task_ids() -> list[str]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT task_id FROM local_export_tasks WHERE status='queued' ORDER BY created_at",
        ).fetchall()
    return [str(row[0]) for row in rows]


def claim_task(task_id: str) -> bool:
    with _lock, _connect() as conn:
        cursor = conn.execute("""UPDATE local_export_tasks
            SET status='running',stage='preparing',progress=1,
                started_at=COALESCE(started_at,?)
            WHERE task_id=? AND status='queued'""", (now(), task_id))
        claimed = cursor.rowcount == 1
    if claimed:
        publisher.publish("local_export", get(task_id))
    return claimed


def claim_item(item_id: str) -> bool:
    with _lock, _connect() as conn:
        cursor = conn.execute("""UPDATE local_export_items
            SET status='running',stage='preparing',progress=5,
                attempts=attempts+1,started_at=COALESCE(started_at,?)
            WHERE item_id=? AND status='queued'""", (now(), item_id))
        return cursor.rowcount == 1


def update_item(item_id: str, **values) -> None:
    allowed = {
        "status", "stage", "progress", "output_path", "video_url",
        "error_message", "completed_at",
    }
    fields = {key: value for key, value in values.items() if key in allowed}
    if "timings" in values:
        fields["timings_json"] = json.dumps(values["timings"], ensure_ascii=False)
    if not fields:
        return
    assignments = ",".join(f"{key}=?" for key in fields)
    with _lock, _connect() as conn:
        conn.execute(
            f"UPDATE local_export_items SET {assignments} WHERE item_id=?",
            (*fields.values(), item_id),
        )


def request_cancel(task_id: str) -> bool:
    with _lock, _connect() as conn:
        cursor = conn.execute(
            "UPDATE local_export_tasks SET cancel_requested=1 WHERE task_id=? "
            "AND status IN ('queued','running')",
            (task_id,),
        )
        changed = cursor.rowcount == 1
    if changed:
        publisher.publish("local_export", get(task_id))
    return changed


def cancel_queued_items(task_id: str) -> int:
    with _lock, _connect() as conn:
        cursor = conn.execute("""UPDATE local_export_items
            SET status='cancelled',stage='cancelled',progress=100,completed_at=?
            WHERE task_id=? AND status='queued'""", (now(), task_id))
        return cursor.rowcount


def refresh_task(task_id: str, *, terminal: bool = False) -> dict | None:
    with _lock, _connect() as conn:
        counts = conn.execute("""SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
            SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
            SUM(CASE WHEN status='running' THEN progress ELSE 0 END) AS running_progress
            FROM local_export_items WHERE task_id=?""", (task_id,)).fetchone()
        if counts is None or not counts["total"]:
            return None
        total = int(counts["total"])
        completed = int(counts["completed"] or 0)
        failed = int(counts["failed"] or 0)
        cancelled = int(counts["cancelled"] or 0)
        done = completed + failed + cancelled
        running_progress = int(counts["running_progress"] or 0)
        progress = min(100, int((done * 100 + running_progress) / total))
        values = {
            "completed_count": completed,
            "failed_count": failed,
            "cancelled_count": cancelled,
            "progress": progress,
        }
        if terminal or done == total:
            if completed == total:
                status = "completed"
            elif completed:
                status = "partial_success"
            elif cancelled == total:
                status = "cancelled"
            else:
                status = "failed"
            values.update({
                "status": status,
                "stage": status,
                "progress": 100,
                "completed_at": now(),
                "error_message": "" if failed == 0 else f"{failed} draft export(s) failed",
            })
        else:
            values.update({"status": "running", "stage": "exporting"})
        assignments = ",".join(f"{key}=?" for key in values)
        conn.execute(
            f"UPDATE local_export_tasks SET {assignments} WHERE task_id=?",
            (*values.values(), task_id),
        )
    task = get(task_id)
    publisher.publish("local_export", task)
    return task


def fail_interrupted() -> int:
    interrupted_at = now()
    with _lock, _connect() as conn:
        item_count = conn.execute("""UPDATE local_export_items
            SET status='failed',stage='failed',progress=100,
                error_message='服务重启中断了本地剪映导出，请重试失败项',completed_at=?
            WHERE status='running'""", (interrupted_at,)).rowcount
        task_ids = [row[0] for row in conn.execute(
            "SELECT task_id FROM local_export_tasks WHERE status='running'",
        ).fetchall()]
    for task_id in task_ids:
        refresh_task(task_id, terminal=True)
    return item_count


def retry_failed(task_id: str) -> int:
    with _lock, _connect() as conn:
        task = conn.execute(
            "SELECT status FROM local_export_tasks WHERE task_id=?", (task_id,),
        ).fetchone()
        if task is None:
            return -1
        if task["status"] in ("queued", "running"):
            return -2
        cursor = conn.execute("""UPDATE local_export_items
            SET status='queued',stage='queued',progress=0,error_message='',
                output_path='',video_url='',started_at=NULL,completed_at=NULL
            WHERE task_id=? AND status='failed'""", (task_id,))
        if cursor.rowcount:
            conn.execute("""UPDATE local_export_tasks SET
                status='queued',stage='queued',progress=0,cancel_requested=0,
                error_message='',started_at=NULL,completed_at=NULL
                WHERE task_id=?""", (task_id,))
        count = cursor.rowcount
    if count:
        publisher.publish("local_export", get(task_id))
    return count
