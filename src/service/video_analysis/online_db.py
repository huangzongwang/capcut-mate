"""Shared MySQL access owned by video_analysis."""
from __future__ import annotations

from contextlib import contextmanager

from dotenv import dotenv_values

from .model_config_store import DB_FILE


class OnlineDatabaseError(RuntimeError):
    pass


def settings() -> dict:
    values = dotenv_values(DB_FILE, encoding="utf-8-sig")
    for suffix in ("TYPE", "HOST", "PORT", "USER", "PASSWORD", "NAME"):
        if values.get("ANALYSIS_DB_" + suffix):
            values["DATABASE_" + suffix] = values["ANALYSIS_DB_" + suffix]
    required = ("DATABASE_HOST", "DATABASE_PORT", "DATABASE_USER", "DATABASE_PASSWORD", "DATABASE_NAME")
    if any(not values.get(key) for key in required):
        raise OnlineDatabaseError("env.db.dev is missing online database settings")
    if values.get("DATABASE_TYPE", "mysql").lower() != "mysql":
        raise OnlineDatabaseError("video_analysis shared storage requires MySQL")
    return values


@contextmanager
def connection(*, write: bool = False):
    import pymysql

    values = settings()
    try:
        conn = pymysql.connect(
            host=values["DATABASE_HOST"], port=int(values["DATABASE_PORT"]),
            user=values["DATABASE_USER"], password=values["DATABASE_PASSWORD"],
            database=values["DATABASE_NAME"], charset="utf8mb4", autocommit=False,
            connect_timeout=10, read_timeout=30, write_timeout=30,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except (pymysql.MySQLError, ValueError, OSError) as exc:
        raise OnlineDatabaseError("online video_analysis database unavailable") from exc
    try:
        if not write:
            with conn.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
        yield conn
        conn.commit() if write else conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
