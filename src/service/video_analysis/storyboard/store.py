from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

from .schemas import GenerateRequest, Shot, Storyboard
from ..online_db import connection as online_connection
from ..platform_runtime import (
    legacy_node_database_root,
    legacy_video_analysis_database_root,
    video_analysis_database_file,
    video_analysis_node_id,
)


class VersionConflict(Exception):
    pass


_schema_lock = threading.Lock()
_schema_ready = False


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sqlite_override() -> Path | None:
    value = os.getenv("STORYBOARD_DB_PATH", "").strip()
    return Path(value).expanduser().resolve() if value else None


def database_path() -> Path:
    """Legacy/test SQLite path; production storyboard documents use MySQL."""
    return _sqlite_override() or video_analysis_database_file("storyboards.sqlite3")


def legacy_database_path() -> Path:
    return legacy_video_analysis_database_root() / "storyboards.sqlite3"


@contextmanager
def connection():
    """Compatibility SQLite connection used by offline tests and migration."""
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS storyboards "
            "(id TEXT PRIMARY KEY, version INTEGER NOT NULL, document TEXT NOT NULL)"
        )
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with online_connection(write=True) as conn, conn.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS myh_video_storyboards (
                storyboard_id VARCHAR(80) NOT NULL,
                version INT UNSIGNED NOT NULL,
                status VARCHAR(32) NOT NULL,
                document_json JSON NOT NULL,
                created_by_node VARCHAR(64) NOT NULL,
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL,
                PRIMARY KEY (storyboard_id),
                KEY idx_myh_video_storyboards_status_updated (status,updated_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        _schema_ready = True


def _read_sqlite(path: Path, board_id: str) -> Storyboard | None:
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(path, timeout=10)
        try:
            row = conn.execute(
                "SELECT document FROM storyboards WHERE id=?", (board_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return Storyboard.model_validate_json(row[0]) if row else None


def _read_mysql(board_id: str) -> Storyboard | None:
    ensure_schema()
    with online_connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT document_json FROM myh_video_storyboards WHERE storyboard_id=%s",
            (board_id,),
        )
        row = cursor.fetchone()
    if not row:
        return None
    value = row["document_json"]
    return (
        Storyboard.model_validate_json(value)
        if isinstance(value, (str, bytes, bytearray))
        else Storyboard.model_validate(value)
    )


def _legacy_paths() -> list[Path]:
    candidates = [
        database_path(),
        legacy_node_database_root() / "storyboards.sqlite3",
        legacy_database_path(),
    ]
    nodes = legacy_video_analysis_database_root() / "nodes"
    if nodes.is_dir():
        candidates.extend(nodes.glob("*/storyboards.sqlite3"))
    result, seen = [], set()
    for path in candidates:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def insert_document(board: Storyboard, *, idempotent: bool = False) -> Storyboard:
    if _sqlite_override() is not None:
        with connection() as conn:
            command = "INSERT OR IGNORE" if idempotent else "INSERT"
            cursor = conn.execute(
                f"{command} INTO storyboards VALUES (?,?,?)",
                (board.storyboard_id, board.version, board.model_dump_json()),
            )
            if cursor.rowcount != 1:
                current = _read_sqlite(database_path(), board.storyboard_id)
                if idempotent and current is not None:
                    return current
                raise VersionConflict("分镜已存在，请重新读取")
        return board

    ensure_schema()
    command = "INSERT IGNORE" if idempotent else "INSERT"
    with online_connection(write=True) as conn, conn.cursor() as cursor:
        cursor.execute(f"""{command} INTO myh_video_storyboards (
            storyboard_id,version,status,document_json,created_by_node,created_at,updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s)""", (
            board.storyboard_id, board.version, board.status, board.model_dump_json(),
            video_analysis_node_id() or "default", board.created_at, board.updated_at,
        ))
        inserted = cursor.rowcount == 1
    if inserted:
        return board
    current = _read_mysql(board.storyboard_id)
    if idempotent and current is not None:
        return current
    raise VersionConflict("分镜已存在，请重新读取")


def create(request: GenerateRequest, shots: list[Shot], model: str) -> Storyboard:
    timestamp = now()
    board = Storyboard(
        storyboard_id=f"sb_{uuid4().hex}", version=1,
        created_at=timestamp, updated_at=timestamp, request=request.model_dump(),
        model_used=model, shots=shots,
    )
    return insert_document(board)


def get(board_id: str) -> Storyboard | None:
    if _sqlite_override() is not None:
        return _read_sqlite(database_path(), board_id)
    board = _read_mysql(board_id)
    if board is not None:
        return board
    for path in _legacy_paths():
        legacy = _read_sqlite(path, board_id)
        if legacy is not None:
            return insert_document(legacy, idempotent=True)
    return None


def save(board: Storyboard, expected_version: int) -> Storyboard:
    updated = board.model_copy(update={"version": expected_version + 1, "updated_at": now()})
    updated.status = (
        "confirmed" if all(shot.review_status == "confirmed" for shot in updated.shots)
        else "needs_review"
    )
    if _sqlite_override() is not None:
        with connection() as conn:
            cursor = conn.execute(
                "UPDATE storyboards SET version=?,document=? WHERE id=? AND version=?",
                (updated.version, updated.model_dump_json(), updated.storyboard_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise VersionConflict("分镜已被其他操作更新，请重新读取后再保存")
        return updated

    ensure_schema()
    with online_connection(write=True) as conn, conn.cursor() as cursor:
        cursor.execute("""UPDATE myh_video_storyboards
            SET version=%s,status=%s,document_json=%s,updated_at=%s
            WHERE storyboard_id=%s AND version=%s""", (
            updated.version, updated.status, updated.model_dump_json(), updated.updated_at,
            updated.storyboard_id, expected_version,
        ))
        if cursor.rowcount != 1:
            raise VersionConflict("分镜已被其他操作更新，请重新读取后再保存")
    return updated
