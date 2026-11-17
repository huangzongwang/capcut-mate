"""Read-only adapter for the existing online model directory.

The video integration deliberately keeps its protocol validation separate from
the analysis model resolver because Grok Video is not a Responses endpoint.
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit

from src.service.video_analysis import model_config_store


GrokVideoModelError = model_config_store.ModelConfigError


def resolve(model_id: int, expected: dict | None = None, *, image=False) -> dict:
    if not isinstance(model_id, int) or isinstance(model_id, bool) or model_id < 1:
        raise GrokVideoModelError("An explicit online model_id is required")
    rows = model_config_store._rows(model_id, include_secret=True)
    if not rows:
        raise GrokVideoModelError(f"Online model {model_id} does not exist")
    config = model_config_store._normalize(rows[0])
    if not config["enabled"]:
        raise GrokVideoModelError(f"Online model {model_id} or its provider is disabled/deleted")
    if image:
        if config["metadata"].get("protocol") not in (None, "gpt_image_2"):
            raise GrokVideoModelError("First-frame metadata.protocol must be gpt_image_2 (or unset)")
    else:
        if config["model"] != "grok-imagine-video-1.5":
            raise GrokVideoModelError("Scene images require grok-imagine-video-1.5")
        if config["metadata"].get("protocol") not in (None, "grok_video"):
            raise GrokVideoModelError("Model metadata.protocol must be grok_video (or unset)")
    base_url = str(config.get("base_url") or "").rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise GrokVideoModelError("Provider base_url must be an HTTP(S) API root")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GrokVideoModelError("Provider base_url cannot contain credentials or query parameters")
    if parsed.path not in ("", "/v1"):
        raise GrokVideoModelError("Provider base_url must end at the host root or /v1")
    if not parsed.path:
        base_url += "/v1"
    config["base_url"] = base_url
    snapshot = {
        key: config.get(key)
        for key in ("id", "provider_id", "model", "provider", "model_updated_at", "provider_updated_at")
    }
    snapshot["revision"] = hashlib.sha256(json.dumps(
        config, sort_keys=True, default=str,
    ).encode()).hexdigest()
    if expected is not None and snapshot != expected:
        raise GrokVideoModelError("Online model configuration changed while queued; resubmit the task")
    config["snapshot"] = snapshot
    max_timeout = 300 if image else 180
    try:
        timeout = float(config["timeout_seconds"])
        if not 1 <= timeout <= max_timeout:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise GrokVideoModelError(f"Provider timeout_seconds must be between 1 and {max_timeout}") from None
    config["request_timeout_seconds"] = timeout
    config["api_key"] = model_config_store._decrypt(config.pop("api_key_ciphertext"))
    return config
