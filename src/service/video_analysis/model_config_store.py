"""Read-only online model directory. Never opens the legacy SQLite config table."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[3]
DB_FILE = ROOT / "env.db.dev"


class ModelConfigError(ValueError):
    """Safe to expose to callers; never includes database/provider credentials."""


def _settings():
    if not DB_FILE.is_file():
        raise ModelConfigError("Missing project env.db.dev")
    values = dotenv_values(DB_FILE, encoding="utf-8-sig")
    for suffix in ("TYPE", "HOST", "PORT", "USER", "PASSWORD", "NAME"):
        if values.get("ANALYSIS_DB_" + suffix):
            values["DATABASE_" + suffix] = values["ANALYSIS_DB_" + suffix]
    required = ("DATABASE_HOST", "DATABASE_PORT", "DATABASE_USER",
                "DATABASE_PASSWORD", "DATABASE_NAME")
    if any(not values.get(key) for key in required):
        raise ModelConfigError("env.db.dev is missing database settings")
    if values.get("DATABASE_TYPE", "mysql").lower() != "mysql":
        raise ModelConfigError("Online model directory requires MySQL")
    return values


def _rows(model_id=None, include_secret=False):
    import pymysql

    settings = _settings()
    secret = ", p.api_key_ciphertext" if include_secret else ""
    sql = f"""
        SELECT m.id, m.provider_id, m.model_id AS model, m.display_name AS name,
               m.capabilities, m.metadata, m.updated_at AS model_updated_at,
               p.updated_at AS provider_updated_at, p.provider_code AS provider,
               p.base_url, p.timeout_seconds, m.is_default,
               m.status AS model_status, p.status AS provider_status,
               m.deleted_at AS model_deleted_at, p.deleted_at AS provider_deleted_at
               {secret}
        FROM myh_video_models m
        JOIN myh_video_model_providers p ON p.id = m.provider_id
    """
    if model_id is not None:
        sql += " WHERE m.id = %s"
    else:
        sql += " WHERE m.deleted_at IS NULL AND p.deleted_at IS NULL ORDER BY m.sort_no, m.id"
    try:
        with pymysql.connect(
            host=settings["DATABASE_HOST"], port=int(settings["DATABASE_PORT"]),
            user=settings["DATABASE_USER"], password=settings["DATABASE_PASSWORD"],
            database=settings["DATABASE_NAME"], charset="utf8mb4",
            connect_timeout=10, read_timeout=15, write_timeout=15,
            cursorclass=pymysql.cursors.DictCursor,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute(sql, (model_id,) if model_id is not None else ())
                return cursor.fetchall()
    except (pymysql.MySQLError, ValueError, OSError):
        raise ModelConfigError("Online model database unavailable; check env.db.dev and connectivity") from None


def _json(value, expected):
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        raise ModelConfigError("Invalid model capabilities or metadata JSON") from None
    if parsed is None:
        return expected()
    if not isinstance(parsed, expected):
        raise ModelConfigError("Invalid model capabilities or metadata type")
    return parsed


def _normalize(row):
    value = dict(row)
    value["capabilities"] = _json(value["capabilities"], list)
    if not all(isinstance(item, str) for item in value["capabilities"]):
        raise ModelConfigError("Model capabilities must be a string array")
    value["metadata"] = _json(value["metadata"], dict)
    state = {key: value.pop(key) for key in (
        "model_status", "provider_status", "model_deleted_at", "provider_deleted_at",
    )}
    value["enabled"] = (
        state["model_status"] == 1 and state["provider_status"] == 1
        and state["model_deleted_at"] is None and state["provider_deleted_at"] is None
    )
    for key in ("model_updated_at", "provider_updated_at"):
        value[key] = str(value[key])
    return value


def list_configs():
    return [_normalize(row) for row in _rows()]


def _decrypt(cipher):
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    if not cipher:
        raise ModelConfigError("Provider API key is empty")
    if not cipher.startswith("gAAAAA"):
        raise ModelConfigError("Provider API key must be encrypted using the upstream Fernet format")
    # Same key derivation as the online platform's CryptoUtil; no implicit default.
    # The online platform stores the provider key with the same CryptoUtil
    # implementation.  Prefer process-level secrets, but allow this service
    # to use the project-local env.db.dev fallback when started without an
    # environment loader.  Secrets are kept in memory only and never logged
    # or returned by a public endpoint.
    file_values = dotenv_values(DB_FILE, encoding="utf-8-sig") if DB_FILE.is_file() else {}
    primary = (
        os.getenv("DATA_ENCRYPTION_KEY")
        or os.getenv("SECRET_KEY")
        or file_values.get("DATA_ENCRYPTION_KEY")
        or file_values.get("SECRET_KEY")
    )
    old = [
        key.strip()
        for key in (
            os.getenv("DATA_ENCRYPTION_OLD_KEYS")
            or file_values.get("DATA_ENCRYPTION_OLD_KEYS")
            or ""
        ).split(",")
        if key.strip()
    ]
    keys = []
    for secret in ([primary] if primary else []) + old:
        keys.append(base64.urlsafe_b64encode(HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None,
            info=b"fastapiadmin:data-encryption:v1",
        ).derive(secret.encode("utf-8"))))
    legacy_secret = os.getenv("SECRET_KEY") or file_values.get("SECRET_KEY")
    if legacy_secret:
        keys.append(base64.urlsafe_b64encode(hashlib.sha256(legacy_secret.encode()).digest()))
    if not keys:
        raise ModelConfigError("Set upstream DATA_ENCRYPTION_KEY or SECRET_KEY in the API process environment")
    for key in keys:
        try:
            return Fernet(key).decrypt(cipher.encode()).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeError):
            continue
    raise ModelConfigError("Provider key decryption failed; verify upstream encryption keys")


def snapshot(config):
    """Non-secret execution identity, safe to persist in tasks/reports."""
    return {**{key: config[key] for key in (
        "id", "provider_id", "model", "provider", "model_updated_at", "provider_updated_at",
    )}, "revision": config.get("revision")}


def resolve_model(model_id, purpose, expected=None):
    if not isinstance(model_id, int) or isinstance(model_id, bool) or model_id < 1:
        raise ModelConfigError("An explicit online myh_video_models.id is required")
    rows = _rows(model_id, include_secret=True)
    if not rows:
        raise ModelConfigError(f"Online model {model_id} does not exist")
    config = _normalize(rows[0])
    if not config["enabled"]:
        raise ModelConfigError(f"Online model {model_id} or its provider is disabled/deleted")
    metadata = config["metadata"]
    # Catalog tags select a purpose-specific transport; capabilities do not gate execution.
    if purpose not in ("scene", "global", "script", "material_review"):
        raise ModelConfigError(f"Unknown analysis model purpose: {purpose}")
    url = config["base_url"].rstrip("/")
    if url.endswith("/responses"):
        url = url[:-10]
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelConfigError("Provider base_url must be an HTTP(S) API root without credentials or query")
    if url.endswith(("/files", "/chat/completions")):
        raise ModelConfigError("Provider base_url must be the API root, not files/chat/completions")
    config["base_url"] = url
    config["revision"] = hashlib.sha256(json.dumps(
        config, sort_keys=True, default=str, ensure_ascii=True,
    ).encode()).hexdigest()
    if expected is not None and snapshot(config) != expected:
        raise ModelConfigError("Online model configuration changed while queued; resubmit the task")
    timeout = metadata.get(f"{purpose}_timeout_seconds", config["timeout_seconds"])
    try:
        timeout = float(timeout)
        if not 1 <= timeout <= 1800:
            raise ValueError
    except (TypeError, ValueError):
        raise ModelConfigError("Model timeout must be between 1 and 1800 seconds") from None
    config["request_timeout_seconds"] = timeout
    for name, default in (("upload_timeout_seconds", 180), ("file_process_timeout_seconds", 300)):
        try:
            value = float(metadata.get(name, default))
            if not 1 <= value <= 1800:
                raise ValueError
        except (TypeError, ValueError):
            raise ModelConfigError(f"{name} must be between 1 and 1800 seconds") from None
        config[name] = value
    config["api_key"] = _decrypt(config.pop("api_key_ciphertext"))
    config["purpose"] = purpose
    return config
