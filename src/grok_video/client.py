from __future__ import annotations

import time
import re
from datetime import datetime, timezone
from pathlib import Path

import requests


class GrokVideoClientError(RuntimeError):
    pass


class SubmissionUnknown(GrokVideoClientError):
    pass


class Stopped(Exception):
    pass


def _url(config: dict, path: str) -> str:
    base = config["base_url"].rstrip("/")
    if base.endswith("/v1"):
        path = path.removeprefix("/v1")
    return f"{base}{path}"


def _request(response: requests.Response) -> dict:
    if response.status_code not in (200, 202):
        raise GrokVideoClientError(f"Grok Video HTTP {response.status_code}")
    try:
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError
        return data
    except ValueError:
        raise GrokVideoClientError("Grok Video returned invalid JSON") from None


def submit(config: dict, request: dict) -> str:
    payload = {key: request[key] for key in (
        "model", "prompt", "duration", "resolution", "aspect_ratio", "image", "n",
    )}
    try:
        response = requests.post(
            _url(config, "/v1/videos/generations"),
            headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"},
            json=payload, allow_redirects=False,
            timeout=(15, config.get("request_timeout_seconds", 180)),
        )
    except requests.RequestException:
        raise SubmissionUnknown("Submission result unknown; check provider before resubmitting") from None
    try:
        if response.status_code >= 500 or response.status_code == 408:
            raise SubmissionUnknown("Submission result unknown; do not blindly resubmit")
        if response.status_code not in (200, 202):
            raise GrokVideoClientError(f"Grok Video submit HTTP {response.status_code}")
        try:
            request_id = _request(response).get("request_id")
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
                raise GrokVideoClientError("Invalid task ID")
        except GrokVideoClientError:
            raise SubmissionUnknown("Submission response has no usable request ID") from None
    finally:
        response.close()
    return request_id


def wait(config: dict, request_id: str, on_status, stop, started_at: str) -> dict:
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds()
    deadline = time.monotonic() + max(0, 900 - age)
    while time.monotonic() < deadline:
        if stop.is_set():
            raise Stopped
        try:
            remaining = max(1, deadline - time.monotonic())
            with requests.get(
                _url(config, f"/v1/videos/{request_id}"),
                headers={"Authorization": f"Bearer {config['api_key']}"},
                timeout=(min(15, remaining), min(config.get("request_timeout_seconds", 180), remaining)),
                allow_redirects=False,
            ) as response:
                if response.status_code in (408, 429) or response.status_code >= 500:
                    data = {"status": "pending"}
                elif response.status_code == 202:
                    # Accepted is not a failure. Preserve explicit terminal states.
                    data = _request(response) if response.content else {}
                    data.setdefault("status", "pending")
                else:
                    data = _request(response)
        except requests.RequestException:
            data = {"status": "pending"}
        status = data.get("status")
        on_status(status)
        if status == "done":
            return data
        if status in ("failed", "expired"):
            raise GrokVideoClientError(f"Grok Video task {status}")
        if status != "pending":
            raise GrokVideoClientError("Unknown provider status")
        if stop.wait(min(5, max(0, deadline - time.monotonic()))):
            raise Stopped
    raise GrokVideoClientError("Grok Video task timed out after 15 minutes")


def download(config: dict, request_id: str, target: Path, stop):
    response = requests.get(
        _url(config, f"/v1/videos/{request_id}/content"),
        headers={"Authorization": f"Bearer {config['api_key']}"},
        timeout=(15, 60),
        stream=True,
    )
    try:
        if response.status_code != 200:
            raise GrokVideoClientError(f"Grok Video download HTTP {response.status_code}")
        temporary = target.with_suffix(".tmp")
        target.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("wb") as output:
            size = 0
            head = b""
            deadline = time.monotonic() + 300
            for chunk in response.iter_content(1024 * 1024):
                if stop.is_set():
                    raise Stopped
                if time.monotonic() > deadline:
                    raise GrokVideoClientError("Download timed out")
                if chunk:
                    size += len(chunk)
                    head = (head + chunk)[:32]
                    if size > 512 * 1024 * 1024:
                        raise GrokVideoClientError("Video exceeds 512 MiB limit")
                    output.write(chunk)
        if size < 12 or b"ftyp" not in head:
            raise GrokVideoClientError("Downloaded content is not MP4")
        length = response.headers.get("Content-Length")
        if length and not response.headers.get("Content-Encoding") and int(length) != size:
            raise GrokVideoClientError("Incomplete download")
        temporary.replace(target)
    except requests.HTTPError as exc:
        raise GrokVideoClientError(f"Grok Video download failed: HTTP {response.status_code}") from exc
    finally:
        response.close()
        target.with_suffix(".tmp").unlink(missing_ok=True)
