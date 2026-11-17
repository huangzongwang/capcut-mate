"""Node-local storyboard queue with shared result publication."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

from . import store
from .schemas import GenerateRequest, Shot, Storyboard
from ..platform_runtime import (
    legacy_node_database_root,
    video_analysis_database_file,
    video_analysis_database_root,
)
from ..shared_results import publisher


_lock = threading.RLock()
_migrated: set[str] = set()


def database_path() -> Path:
    override = os.getenv("STORYBOARD_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve().with_name("storyboard-tasks.sqlite3")
    return video_analysis_database_file("storyboard-tasks.sqlite3", migrate=False)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS storyboard_tasks (
        task_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        stage TEXT NOT NULL,
        progress INTEGER NOT NULL,
        request_json TEXT NOT NULL,
        storyboard_id TEXT UNIQUE,
        result_json TEXT,
        result_path TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        heartbeat_at TEXT,
        timings_json TEXT NOT NULL DEFAULT '{}',
        model_request_json TEXT NOT NULL DEFAULT '{}'
    )""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_storyboard_tasks_status_created "
        "ON storyboard_tasks(status,created_at)"
    )


def _legacy_sources() -> list[Path]:
    if os.getenv("STORYBOARD_DB_PATH", "").strip():
        return []
    return [
        video_analysis_database_root() / "storyboards.sqlite3",
        legacy_node_database_root() / "storyboards.sqlite3",
    ]


def _migrate_legacy(conn: sqlite3.Connection, target: Path) -> None:
    key = os.path.normcase(str(target))
    if key in _migrated:
        return
    columns = [
        "task_id", "status", "stage", "progress", "request_json", "storyboard_id",
        "result_json", "result_path", "error_message", "created_at", "started_at",
        "completed_at", "heartbeat_at", "timings_json", "model_request_json",
    ]
    for source in _legacy_sources():
        if not source.is_file() or os.path.normcase(str(source)) == key:
            continue
        try:
            legacy = sqlite3.connect(source, timeout=10)
            legacy.row_factory = sqlite3.Row
            try:
                exists = legacy.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='storyboard_tasks'"
                ).fetchone()
                if not exists:
                    continue
                available = {row[1] for row in legacy.execute("PRAGMA table_info(storyboard_tasks)")}
                selected = [name for name in columns if name in available]
                for row in legacy.execute(f"SELECT {','.join(selected)} FROM storyboard_tasks"):
                    values = dict(row)
                    values.setdefault("timings_json", "{}")
                    values.setdefault("model_request_json", "{}")
                    insert_columns = [name for name in columns if name in values]
                    conn.execute(
                        f"INSERT OR IGNORE INTO storyboard_tasks ({','.join(insert_columns)}) "
                        f"VALUES ({','.join('?' for _ in insert_columns)})",
                        [values[name] for name in insert_columns],
                    )
            finally:
                legacy.close()
        except sqlite3.Error:
            continue
    _migrated.add(key)


@contextmanager
def connection():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        _ensure_schema(conn)
        _migrate_legacy(conn, path)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def get(task_id: str) -> dict | None:
    with _lock, connection() as conn:
        row = conn.execute(
            "SELECT * FROM storyboard_tasks WHERE task_id=?", (task_id,),
        ).fetchone()
    return dict(row) if row else publisher.get_shared("script_storyboard", task_id)


def create(request: GenerateRequest, binding: dict) -> dict:
    task_id = f"sbt_{uuid4().hex}"
    with _lock, connection() as conn:
        conn.execute(
            "INSERT INTO storyboard_tasks "
            "(task_id,status,stage,progress,request_json,created_at,timings_json) "
            "VALUES (?,'queued','queued',0,?,?,'{}')",
            (task_id, json.dumps({**request.model_dump(), "_model_binding": binding}, ensure_ascii=False), store.now()),
        )
        task = dict(conn.execute(
            "SELECT * FROM storyboard_tasks WHERE task_id=?", (task_id,),
        ).fetchone())
    publisher.publish("script_storyboard", task)
    return task


def claim() -> dict | None:
    with _lock, connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT task_id FROM storyboard_tasks WHERE status='queued' "
            "ORDER BY created_at,task_id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        timestamp = store.now()
        cursor = conn.execute(
            "UPDATE storyboard_tasks SET status='running',stage='planning',progress=10,"
            "started_at=?,heartbeat_at=? WHERE task_id=? AND status='queued'",
            (timestamp, timestamp, row["task_id"]),
        )
        if cursor.rowcount != 1:
            return None
        task = dict(conn.execute(
            "SELECT * FROM storyboard_tasks WHERE task_id=?", (row["task_id"],),
        ).fetchone())
    publisher.publish("script_storyboard", task)
    return task


def running() -> list[dict]:
    with _lock, connection() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM storyboard_tasks WHERE status='running'"
        ).fetchall()]


def heartbeat(task_id: str) -> None:
    with _lock, connection() as conn:
        conn.execute(
            "UPDATE storyboard_tasks SET heartbeat_at=? WHERE task_id=? AND status='running'",
            (store.now(), task_id),
        )


def model_event(task_id: str, event: dict) -> None:
    with _lock, connection() as conn:
        conn.execute(
            "UPDATE storyboard_tasks SET model_request_json=?,heartbeat_at=? "
            "WHERE task_id=? AND status='running'",
            (json.dumps(event), store.now(), task_id),
        )
    publisher.publish("script_storyboard", get(task_id))


def stage(task_id: str, name: str, progress: int, timings: dict) -> None:
    with _lock, connection() as conn:
        conn.execute(
            "UPDATE storyboard_tasks SET stage=?,progress=MAX(progress,?),heartbeat_at=?,"
            "timings_json=? WHERE task_id=? AND status='running'",
            (name, progress, store.now(), json.dumps(timings), task_id),
        )
    publisher.publish("script_storyboard", get(task_id))


def save_board(task_id: str, request: GenerateRequest, shots: list[Shot], model: str) -> Storyboard:
    with _lock, connection() as conn:
        row = conn.execute(
            "SELECT status,result_json FROM storyboard_tasks WHERE task_id=?", (task_id,),
        ).fetchone()
    if not row or row["status"] != "running":
        raise ValueError("Task is not running")
    if row["result_json"]:
        return Storyboard.model_validate_json(row["result_json"])

    timestamp = store.now()
    board = Storyboard(
        storyboard_id=f"sb_{task_id[4:]}", version=1, created_at=timestamp,
        updated_at=timestamp, request=request.model_dump(), model_used=model, shots=shots,
    )
    board = store.insert_document(board, idempotent=True)
    snapshot = board.model_dump_json()
    with _lock, connection() as conn:
        cursor = conn.execute(
            "UPDATE storyboard_tasks SET storyboard_id=?,result_json=? "
            "WHERE task_id=? AND status='running' AND result_json IS NULL",
            (board.storyboard_id, snapshot, task_id),
        )
        if cursor.rowcount != 1:
            current = conn.execute(
                "SELECT result_json FROM storyboard_tasks WHERE task_id=?", (task_id,),
            ).fetchone()
            if not current or not current["result_json"]:
                raise ValueError("Task result could not be persisted")
            return Storyboard.model_validate_json(current["result_json"])
    return board


def finish(task_id: str, path: str, board: Storyboard, timings: dict) -> None:
    warnings = list(dict.fromkeys(warning for shot in board.shots for warning in shot.warnings))
    timestamp = store.now()
    with _lock, connection() as conn:
        conn.execute(
            "UPDATE storyboard_tasks SET status=?,stage='waiting_review',progress=100,"
            "result_path=?,error_message=?,completed_at=?,heartbeat_at=?,timings_json=? "
            "WHERE task_id=? AND status='running'",
            ("completed_with_warnings" if warnings else "completed", path,
             "；".join(warnings)[:2000] or None, timestamp, timestamp,
             json.dumps(timings), task_id),
        )
    publisher.publish("script_storyboard", get(task_id))


def fail(task_id: str, message: str) -> None:
    timestamp = store.now()
    with _lock, connection() as conn:
        conn.execute(
            "UPDATE storyboard_tasks SET status='failed',stage='failed',error_message=?,"
            "completed_at=?,heartbeat_at=? WHERE task_id=? AND status='running'",
            (message, timestamp, timestamp, task_id),
        )
    publisher.publish("script_storyboard", get(task_id))
