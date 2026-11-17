"""Isolated Gemini generateContent transport for material-review videos."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import time
from urllib.parse import quote

import requests


class GeminiError(RuntimeError):
    pass


def _url(config: dict) -> str:
    base = str(config["base_url"]).rstrip("/")
    for suffix in ("/v1beta/openai", "/v1beta", "/responses"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    model = str(config["model"])
    if model.startswith("models/"):
        model = model[7:]
    return f"{base}/v1beta/models/{quote(model, safe='-._')}:generateContent"


def _json_text(payload: dict) -> str:
    candidates = payload.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "\n".join(
        str(part.get("text") or "") for part in parts
        if isinstance(part, dict) and not part.get("thought")
    ).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines.pop(0)
        if lines and lines[-1].strip() == "```":
            lines.pop()
        text = "\n".join(lines).strip()
    return text


def generate_json(*, config: dict, instruction: str,
                  videos: list[tuple[str, Path]], max_output_tokens: int,
                  task_id: str) -> tuple[dict, list[dict]]:
    try:
        max_inline_bytes = int(
            (config.get("metadata") or {}).get("material_review_gemini_max_inline_bytes")
            or 12 * 1024 * 1024
        )
    except (TypeError, ValueError):
        raise GeminiError("Gemini素材审核内联视频大小配置无效") from None
    total_bytes = sum(path.stat().st_size for _, path in videos)
    if total_bytes > max_inline_bytes:
        raise GeminiError(
            f"Gemini素材审核视频合计{total_bytes}字节，超过内联限制{max_inline_bytes}字节"
        )

    parts = [{"text": instruction}]
    for label, path in videos:
        parts.extend([
            {"text": f"候选{label}，本视频时间从0毫秒开始。"},
            {"inlineData": {
                "mimeType": "video/mp4",
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            }},
        ])
    generation_config = {
        "responseMimeType": "application/json",
        "maxOutputTokens": max_output_tokens,
    }
    metadata = config.get("metadata") or {}
    if metadata.get("material_review_thinking_budget") is not None:
        try:
            generation_config["thinkingConfig"] = {
                "thinkingBudget": int(metadata["material_review_thinking_budget"]),
            }
        except (TypeError, ValueError):
            raise GeminiError("Gemini素材审核thinking budget配置无效") from None
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": generation_config,
    }
    url = _url(config)
    timeout = float(config.get("request_timeout_seconds") or 300)
    calls = []
    for attempt in (1, 2):
        started = time.monotonic()
        status = None
        try:
            response = requests.post(
                url,
                headers={
                    "x-goog-api-key": config["api_key"],
                    "Content-Type": "application/json",
                },
                json=body, timeout=(15, timeout), allow_redirects=False,
            )
            status = response.status_code
            if status != 200:
                if attempt == 1 and (status in (408, 429) or status >= 500):
                    time.sleep(1)
                    continue
                raise GeminiError(f"Gemini素材审核HTTP {status}")
            payload = response.json()
            text = _json_text(payload)
            data = json.loads(text)
            if not isinstance(data, dict) or not data:
                raise ValueError
            calls.append({
                "task_id": task_id, "model_id": config.get("id"),
                "provider_id": config.get("provider_id"), "model": config.get("model"),
                "operation": "material_review_gemini", "attempt": attempt,
                "outcome": "completed", "status_code": status,
                "seconds": round(time.monotonic() - started, 3),
                "usage": payload.get("usageMetadata") or {},
                "response_id": payload.get("responseId"),
            })
            return data, calls
        except (requests.Timeout, requests.ConnectionError) as exc:
            calls.append({
                "task_id": task_id, "model_id": config.get("id"),
                "provider_id": config.get("provider_id"), "model": config.get("model"),
                "operation": "material_review_gemini", "attempt": attempt,
                "outcome": type(exc).__name__, "status_code": status,
                "seconds": round(time.monotonic() - started, 3),
            })
            if attempt == 1:
                time.sleep(1)
                continue
            raise GeminiError("Gemini素材审核连接失败或超时") from None
        except (ValueError, KeyError, TypeError, requests.JSONDecodeError):
            raise GeminiError("Gemini素材审核返回的不是合法JSON") from None
    raise GeminiError("Gemini素材审核请求失败")
