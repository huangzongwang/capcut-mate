"""Native Gemini video requests; local video bytes never use Responses."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

import requests

from .responses_client import Scope


def uses_gemini(config: dict) -> bool:
    metadata = config.get("metadata") or {}
    protocol = metadata.get(f"{config.get('purpose', 'scene')}_protocol", metadata.get("protocol"))
    if protocol in ("gemini", "gemini_native"):
        return True
    if protocol == "responses":
        return False
    return config.get("model", "").lower().startswith("gemini-")


def request_json(config: dict, content: list[dict], *, scope: Scope | None = None,
                 operation="scene", scene_index=None) -> dict:
    scope = scope or Scope()
    started = time.monotonic()
    timeout = float(config.get("request_timeout_seconds", 180))
    deadline = min(started + timeout, scope.deadline or float("inf"))
    record = {
        "call_id": uuid4().hex, "model_id": config.get("id"),
        "provider_id": config.get("provider_id"), "model": config["model"],
        "task_id": scope.task_id, "purpose": config.get("purpose", operation),
        "operation": operation, "scene_index": scene_index, "protocol": "gemini",
        "attempt": 1, "outcome": "failed",
    }

    def emit(event):
        if scope.on_event:
            try:
                scope.on_event({**record, "event": event})
            except Exception:
                pass

    emit("request_started")
    try:
        parts = []
        encoded_size = 0
        limit = int((config.get("metadata") or {}).get("gemini_max_inline_bytes", 20_000_000))
        for item in content:
            if item["type"] == "input_text":
                parts.append({"text": item["text"]})
            elif item["type"] == "input_video":
                path = Path(item["file_id"])
                encoded_size += 4 * ((path.stat().st_size + 2) // 3)
                if encoded_size > limit:
                    raise ValueError("Gemini inline video exceeds configured request size limit")
                parts.append({"inlineData": {
                    "mimeType": "video/mp4",
                    "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                }})
            else:
                raise ValueError("Unsupported Gemini input type")
        base = config["base_url"].rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        if not base.endswith("/v1beta"):
            base += "/v1beta"
        url = base + "/models/" + quote(config["model"], safe="") + ":generateContent"
        headers = (
            {"x-goog-api-key": config["api_key"]}
            if urlsplit(base).hostname == "generativelanguage.googleapis.com"
            else {"Authorization": "Bearer " + config["api_key"]}
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise requests.ReadTimeout("Gemini request budget exhausted")
        with requests.post(
            url, headers=headers,
            json={"contents": [{"role": "user", "parts": parts}],
                  "generationConfig": {"responseMimeType": "application/json"}},
            timeout=(min(15, remaining), remaining), stream=True, allow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise requests.HTTPError(f"Gemini API HTTP {response.status_code}")
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                if time.monotonic() >= deadline:
                    raise requests.ReadTimeout("Gemini request budget exhausted")
                raw.extend(chunk)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("Gemini response exceeds size limit")
            try:
                body = json.loads(raw)
                candidate = body["candidates"][0]
                if candidate.get("finishReason") != "STOP":
                    raise ValueError("Gemini response did not finish successfully")
                text = "".join(
                    part.get("text", "") for part in candidate["content"]["parts"]
                    if not part.get("thought")
                ).strip()
                if text.startswith("```") and text.endswith("```"):
                    text = text[3:-3].strip()
                    if text.startswith("json"):
                        text = text[4:].lstrip()
                result = json.loads(text)
            except (KeyError, IndexError, TypeError, ValueError):
                raise ValueError("Gemini response is not a completed JSON object") from None
            if not isinstance(result, dict) or not result:
                raise ValueError("Gemini response must be a nonempty JSON object")
            record.update(outcome="completed", usage=body.get("usageMetadata", {}),
                          response_id=body.get("responseId"))
            return result
    finally:
        record["seconds"] = round(time.monotonic() - started, 3)
        scope.calls.append(dict(record))
        emit("request_finished")
