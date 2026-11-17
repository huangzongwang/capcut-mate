"""Responses SSE transport. No credentials, buffers or callbacks are global."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import json
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from uuid import uuid4

import requests


@dataclass(frozen=True)
class Connection:
    base_url: str
    api_key: str = field(repr=False)
    model: str = ""


@dataclass(frozen=True)
class Context:
    model_id: int | None = None
    provider_id: int | None = None
    task_id: str | None = None
    purpose: str | None = None
    operation: str | None = None
    scene_index: int | None = None
    call_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True)
class Policy:
    connect_seconds: float = 15
    idle_seconds: float = 120
    total_seconds: float = 300
    attempts: int = 2
    max_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        if not all(0 < n <= 1800 for n in (
            self.connect_seconds, self.idle_seconds, self.total_seconds,
        )) or self.attempts not in (1, 2):
            raise ValueError("Invalid streaming timeout/retry policy")


@dataclass
class Scope:
    """Owned by one business invocation, never stored in model configuration."""
    task_id: str | None = None
    on_event: Callable[[dict], None] | None = None
    calls: list[dict] = field(default_factory=list)
    deadline: float | None = None


@dataclass(frozen=True)
class Result:
    data: dict
    calls: tuple[dict, ...]
    response_id: str | None
    usage: dict


class StreamInterrupted(requests.ConnectionError):
    pass


class ModelResponseError(requests.RequestException):
    pass


def _retry_delay(response) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return min(30.0, max(0.0, float(value)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return min(30.0, max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return 1.0


def responses_url(base_url: str) -> str:
    value = base_url.rstrip("/")
    return value if value.endswith("/responses") else value + "/responses"


def output_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    return "\n".join(
        part["text"] for item in payload.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text" and isinstance(part.get("text"), str)
    )


def _events(response, deadline, max_bytes):
    lines, size, pending = [], 0, bytearray()
    # Bound memory and time even when a peer never terminates an SSE line.
    for chunk in response.iter_content(chunk_size=1):
        if time.monotonic() >= deadline:
            raise requests.ReadTimeout("Model request total budget exhausted")
        size += len(chunk)
        if size > max_bytes:
            raise ModelResponseError("Model stream exceeds size limit")
        pending.extend(chunk)
        if not pending.endswith(b"\n"):
            continue
        try:
            line = pending.rstrip(b"\r\n").decode("utf-8")
        except UnicodeError:
            raise ModelResponseError("Invalid UTF-8 model stream") from None
        pending.clear()
        if not line:
            if lines:
                raw = "\n".join(lines)
                lines = []
                if raw == "[DONE]":
                    return
                try:
                    event = json.loads(raw)
                except ValueError:
                    raise ModelResponseError("Invalid model stream event") from None
                if not isinstance(event, dict):
                    raise ModelResponseError("Invalid model stream event")
                yield event
        elif line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
    # A partial final event is not a completed response.


def request_json(
    connection: Connection, payload: dict, context: Context,
    policy: Policy = Policy(), on_event: Callable[[dict], None] | None = None,
) -> Result:
    body = deepcopy(payload)
    body.update(model=connection.model, stream=True)
    started = time.monotonic()
    deadline = started + policy.total_seconds
    calls = []
    identity = {**context.__dict__, "model": connection.model}

    def emit(kind, **details):
        event = {**identity, "event": kind, **details}
        logging.getLogger("src.utils.logger").info(
            "model_stream %s", json.dumps(event, ensure_ascii=True),
        )
        if on_event:
            try:
                on_event(deepcopy(event))
            except Exception:
                logging.getLogger(__name__).warning(
                    "Model progress callback failed call_id=%s", context.call_id,
                )

    for attempt in range(1, policy.attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise requests.ReadTimeout("Model request total budget exhausted")
        mark = time.monotonic()
        first_event = first_text = None
        response_id, usage = None, {}
        chunks, done_texts = {}, {}
        outcome, retryable = "failed", False
        last_event_type = None
        last_progress = mark
        retry_delay = 1.0
        emit("request_started", attempt=attempt)
        try:
            with requests.post(
                responses_url(connection.base_url),
                headers={"Authorization": f"Bearer {connection.api_key}",
                         "Content-Type": "application/json", "Accept": "text/event-stream"},
                json=body, stream=True, allow_redirects=False,
                timeout=(min(policy.connect_seconds, remaining),
                         min(policy.idle_seconds, remaining)),
            ) as response:
                if response.status_code != 200:
                    retryable = response.status_code in (408, 429) or response.status_code >= 500
                    if response.status_code == 429:
                        retry_delay = _retry_delay(response)
                    raise requests.HTTPError(
                        f"Model API HTTP {response.status_code}", response=response,
                    )
                if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
                    raise ModelResponseError("Model endpoint did not return an SSE stream")
                completed = None
                for event in _events(response, deadline, policy.max_bytes):
                    now = time.monotonic()
                    kind = event.get("type", "")
                    last_event_type = kind
                    if first_event is None:
                        first_event = round(now - mark, 3)
                        emit("first_event", attempt=attempt, seconds=first_event)
                    details = event.get("response") or {}
                    response_id = details.get("id", response_id)
                    if kind == "response.output_text.delta":
                        key = (event.get("output_index", 0), event.get("content_index", 0))
                        delta = event.get("delta")
                        if not isinstance(delta, str):
                            raise ModelResponseError("Invalid model text delta")
                        chunks.setdefault(key, []).append(delta)
                        if delta and first_text is None:
                            first_text = round(now - mark, 3)
                            emit("first_text", attempt=attempt, seconds=first_text)
                    elif kind == "response.output_text.done":
                        key = (event.get("output_index", 0), event.get("content_index", 0))
                        if isinstance(event.get("text"), str):
                            done_texts[key] = event["text"]
                    elif kind in ("error", "response.failed", "response.incomplete"):
                        code = (event.get("error") or details.get("error") or {}).get("code")
                        retryable = code in (
                            "rate_limit_exceeded", "server_error", "internal_server_error",
                        )
                        raise ModelResponseError(f"Model stream ended with {kind}")
                    elif kind == "response.completed":
                        if details.get("status") != "completed":
                            raise ModelResponseError("Model response is not completed")
                        completed = details
                        break
                    if now - last_progress >= 5:
                        emit("receiving", attempt=attempt, seconds=round(now - mark, 3),
                             first_event_seconds=first_event, first_text_seconds=first_text,
                             last_event_type=last_event_type)
                        last_progress = now
                if completed is None:
                    raise StreamInterrupted("Model stream closed before response.completed")
                text = output_text(completed)
                if not text:
                    text = "\n".join(
                        done_texts.get(key, "".join(chunks.get(key, [])))
                        for key in sorted(chunks.keys() | done_texts.keys())
                    )
                text = text.strip()
                if text.startswith("```"):
                    text = text.strip("`").lstrip()
                    if text.startswith("json"):
                        text = text[4:].lstrip()
                try:
                    data = json.loads(text)
                except ValueError:
                    raise ValueError("Model response is not valid JSON") from None
                if not isinstance(data, dict) or not data:
                    raise ValueError("Model response must be a nonempty JSON object")
                usage = completed.get("usage") or {}
                outcome = "completed"
        except ValueError:
            outcome = "invalid_json"
            raise
        except (requests.Timeout, requests.ConnectionError,
                requests.exceptions.ChunkedEncodingError) as exc:
            retryable = True
            failure = type(exc)("Model stream interrupted or timed out")
            outcome = type(exc).__name__
        except requests.RequestException as exc:
            failure = exc
            outcome = type(exc).__name__
        finally:
            record = {
                **identity, "attempt": attempt, "outcome": outcome,
                "seconds": round(time.monotonic() - mark, 3),
                "first_event_seconds": first_event, "first_text_seconds": first_text,
                "response_id": response_id, "usage": usage,
                "last_event_type": last_event_type,
            }
            calls.append(record)
            emit("request_finished", **{k: v for k, v in record.items() if k not in identity})
        if outcome == "completed":
            return Result(data, tuple(calls), response_id, usage)
        if not retryable or attempt == policy.attempts:
            raise failure from None
        if deadline - time.monotonic() <= 1:
            raise requests.ReadTimeout("Model request total budget exhausted") from None
        retry_delay = min(retry_delay, max(0.0, deadline - time.monotonic() - 1))
        emit("retry_scheduled", attempt=attempt + 1, delay_seconds=retry_delay)
        time.sleep(retry_delay)
    raise AssertionError("Unreachable")


def configured_request(config, payload, read_timeout, *, scope=None, operation=None, scene_index=None):
    """Compatibility adapter: configuration is read-only; runtime lives in Scope."""
    scope = scope if scope is not None else Scope(task_id=config.get("task_id"))
    metadata = config.get("metadata") or {}
    purpose = config.get("purpose", "scene")
    policy = Policy(
        idle_seconds=float(metadata.get(f"{purpose}_stream_idle_seconds", 120)),
        total_seconds=float(metadata.get(f"{purpose}_stream_total_seconds", max(300, read_timeout))),
    )
    if scope.deadline is not None:
        remaining = scope.deadline - time.monotonic()
        if remaining <= 0:
            raise requests.ReadTimeout("Model task budget exhausted")
        policy = replace(policy, total_seconds=min(policy.total_seconds, remaining))
    context = Context(
        model_id=config.get("id"), provider_id=config.get("provider_id"),
        task_id=scope.task_id, purpose=purpose, operation=operation or purpose,
        scene_index=scene_index,
    )

    def receive(event):
        if event["event"] == "request_finished":
            scope.calls.append({k: v for k, v in event.items() if k != "event"})
        if scope.on_event:
            scope.on_event(event)

    return request_json(
        Connection(config["base_url"], config["api_key"], config["model"]),
        payload, context, policy, receive,
    ).data
