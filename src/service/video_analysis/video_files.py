"""Files API upload, bounded processing wait, and cleanup of task-owned files."""
import time
from pathlib import Path
from urllib.parse import quote

import requests


class VideoFiles:
    def __init__(self, config):
        base = config["base_url"].rstrip("/")
        self.url = (base[:-10] if base.endswith("/responses") else base) + "/files"
        self.headers = {"Authorization": f"Bearer {config['api_key']}"}
        self.owned_ids = []
        self.upload_timeout = config.get("upload_timeout_seconds", 180)
        self.process_timeout = config.get("file_process_timeout_seconds", 300)

    def _upload(self, path, media_type, video_fps=None, retries=0):
        data = {
            "purpose": "user_data",
            "expire_at": str(int(time.time()) + 4 * 86400),
        }
        if video_fps is not None:
            data["preprocess_configs[video][fps]"] = str(video_fps)
        response = None
        for attempt in range(retries + 1):
            try:
                with Path(path).open("rb") as stream:
                    response = requests.post(
                        self.url, headers=self.headers,
                        files={"file": (Path(path).name, stream, media_type)},
                        data=data,
                        timeout=(15, self.upload_timeout), allow_redirects=False,
                    )
                break
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt >= retries:
                    raise type(exc)("Files upload connection failed after retry") from None
                time.sleep(2)
        if response is None:
            raise RuntimeError("Files upload did not return a response")
        if response.status_code >= 300:
            raise RuntimeError(f"Files upload HTTP {response.status_code}")
        payload = response.json()
        file_id = payload.get("id")
        if not isinstance(file_id, str) or not file_id:
            raise ValueError("Upload did not return a file id")
        self.owned_ids.append(file_id)
        deadline = time.monotonic() + self.process_timeout
        while payload.get("status") != "active":
            if payload.get("status") in {"failed", "expired", "deleted"}:
                raise ValueError(f"Video file processing failed: {payload.get('status')}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Video file processing exceeded {self.process_timeout} seconds")
            time.sleep(3)
            response = requests.get(
                self.url + "/" + quote(file_id, safe=""), headers=self.headers,
                timeout=(15, 30), allow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        return file_id

    def upload(self, path, fps):
        # Retry one transient network failure; HTTP 4xx is never retried.
        return self._upload(path, "video/mp4", fps, retries=1)

    def upload_document(self, path):
        return self._upload(path, "application/pdf")

    def cleanup(self):
        warnings = []
        for file_id in self.owned_ids:
            try:
                response = requests.delete(
                    self.url + "/" + quote(file_id, safe=""), headers=self.headers,
                    timeout=(10, 15), allow_redirects=False,
                )
                if response.status_code != 404:
                    response.raise_for_status()
            except Exception:
                warnings.append("Uploaded analysis file cleanup failed; expiry was requested at upload.")
        return warnings
