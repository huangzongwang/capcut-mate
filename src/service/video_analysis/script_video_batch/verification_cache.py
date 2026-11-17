"""Shared MySQL cache for model-verified material matches."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from . import media_library


PROMPT_VERSION = "material-review-v5"


def ensure_table() -> None:
    statement = """CREATE TABLE IF NOT EXISTS myh_media_match_verifications (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        target_signature CHAR(64) NOT NULL,
        candidate_signature CHAR(64) NOT NULL,
        model_id BIGINT UNSIGNED NOT NULL,
        model_revision CHAR(64) NOT NULL,
        prompt_version VARCHAR(32) NOT NULL,
        matched TINYINT(1) NOT NULL,
        confidence DECIMAL(7,4) NOT NULL DEFAULT 0,
        result_json JSON NOT NULL,
        expires_at DATETIME(3) NOT NULL,
        created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
        updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
            ON UPDATE CURRENT_TIMESTAMP(3),
        PRIMARY KEY (id),
        UNIQUE KEY uk_myh_media_match_verification
            (target_signature,candidate_signature,model_id,model_revision,prompt_version),
        KEY idx_myh_media_match_expiry (expires_at),
        KEY idx_myh_media_match_candidate (candidate_signature,matched,confidence)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    with media_library._connection(write=True) as connection, connection.cursor() as cursor:
        cursor.execute(statement)


def get(*, target_signature: str, candidate_signature: str,
        model_id: int, model_revision: str) -> dict | None:
    with media_library._connection() as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT result_json FROM myh_media_match_verifications
            WHERE target_signature=%s AND candidate_signature=%s AND model_id=%s
              AND model_revision=%s AND prompt_version=%s
              AND expires_at>CURRENT_TIMESTAMP(3) LIMIT 1""", (
            target_signature, candidate_signature, model_id,
            model_revision, PROMPT_VERSION,
        ))
        row = cursor.fetchone()
    if not row:
        return None
    value = row.get("result_json")
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def put(*, target_signature: str, candidate_signature: str,
        model_id: int, model_revision: str, result: dict) -> None:
    matched = bool(result.get("matched"))
    ttl_days = 30 if matched else 7
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=ttl_days)
    confidence = max(0.0, min(1.0, float(result.get("confidence") or 0)))
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    with media_library._connection(write=True) as connection, connection.cursor() as cursor:
        cursor.execute("""INSERT INTO myh_media_match_verifications
            (target_signature,candidate_signature,model_id,model_revision,prompt_version,
             matched,confidence,result_json,expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE matched=VALUES(matched),confidence=VALUES(confidence),
                result_json=VALUES(result_json),expires_at=VALUES(expires_at),
                updated_at=CURRENT_TIMESTAMP(3)""", (
            target_signature, candidate_signature, model_id, model_revision,
            PROMPT_VERSION, int(matched), confidence, payload, expires_at,
        ))
