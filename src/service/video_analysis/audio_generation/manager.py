"""In-memory credentials and background audio task state."""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import uuid

from . import client, storage
from . import task_store
from .schemas import GenerateAudioRequest


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _with_pronunciation(segments: list[dict], overrides: list[dict]) -> list[dict]:
    mapping = {item["text"]: item["phonetic"] for item in overrides}
    if not mapping:
        return [{"text": item["text"], "textElements": [{"text": item["text"], "type": 1}]}
                for item in segments]
    keys = sorted(mapping, key=len, reverse=True)
    output = []
    for segment in segments:
        text = segment["text"]
        elements = []
        cursor = 0
        while cursor < len(text):
            match = next((key for key in keys if text.startswith(key, cursor)), None)
            if match is None:
                end = min((text.find(key, cursor) for key in keys if text.find(key, cursor) >= 0),
                          default=len(text))
                elements.append({"text": text[cursor:end], "type": 1})
                cursor = end
            else:
                elements.append({
                    "text": match,
                    "type": 4,
                    "attributes": {"alphabet": "py", "ph": mapping[match]},
                })
                cursor += len(match)
        output.append({"text": text, "textElements": elements})
    return output


class AudioTaskManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._credentials: dict | None = None
        self._generation_lock = threading.Lock()
        self._split_lock = threading.Lock()

    def credentials_status(self) -> dict:
        with self._lock:
            return {"configured": self._credentials is not None,
                    "message": "凭证已配置" if self._credentials else "尚未配置凭证"}

    def configure(self, bpid: str, cookie: str) -> dict:
        # Validate using the provider's own split endpoint before retaining the session.
        client.split_sentences("音频凭证验证", bpid, cookie)
        with self._lock:
            self._credentials = {"bpid": bpid, "cookie": cookie}
        return self.credentials_status()

    def clear_credentials(self) -> dict:
        with self._lock:
            self._credentials = None
        return self.credentials_status()

    def invalidate_credentials(self, credentials: dict) -> None:
        with self._lock:
            if self._credentials == credentials:
                self._credentials = None

    def split_sentences(self, text: str) -> list[dict]:
        """Use the configured provider session without exposing its credentials."""
        with self._lock:
            if not self._credentials:
                raise RuntimeError("音频平台凭证未配置，请先设置 Cookie 与 BPID")
            credentials = dict(self._credentials)
        try:
            with self._split_lock:
                return client.split_sentences(text, credentials["bpid"], credentials["cookie"])
        except client.CredentialsExpired:
            self.invalidate_credentials(credentials)
            raise

    def submit(self, request: GenerateAudioRequest) -> dict:
        with self._lock:
            if not self._credentials:
                raise RuntimeError("请先设置并验证 Cookie 与 BPID")
            credentials = dict(self._credentials)
            task_id = f"vaa_{uuid.uuid4().hex}"
            task = {
                "task_id": task_id, "status": "queued", "stage": "queued", "progress": 0,
                "error_message": "", "created_at": _now(), "completed_at": None,
            }
            task_store.create(task_id)
        return {"task": dict(task), "credentials": credentials}

    def execute(self, task_id: str, request: GenerateAudioRequest, credentials: dict) -> None:
        try:
            with self._generation_lock:
                self._update(task_id, status="running", stage="splitting", progress=10)
                segments = client.split_sentences(request.text, credentials["bpid"], credentials["cookie"])
                payload_segments = _with_pronunciation(
                    segments, [item.model_dump() for item in request.pronunciation_overrides],
                )
                self._update(task_id, stage="generating", progress=35)
                audio_url = client.text_to_speech(
                    request.text, payload_segments, credentials["bpid"], credentials["cookie"],
                    request.voice_element_id, request.voice_speed,
                )
                self._update(task_id, stage="downloading", progress=70)
                result = storage.download_and_convert(audio_url, task_id)
                self._update(
                    task_id, status="completed", stage="completed", progress=100,
                    completed_at=_now(), download_url=f"/openapi/capcut-mate/v1/audio/files/{task_id}",
                    format=result["format"], sample_rate=result["sample_rate"],
                    channels=result["channels"], sample_width_bits=result["sample_width_bits"],
                    size_bytes=result["size_bytes"],
                )
        except client.CredentialsExpired as exc:
            self.invalidate_credentials(credentials)
            self._update(task_id, status="failed", stage="failed", progress=100,
                         error_message=str(exc), completed_at=_now())
        except client.AudioProviderError as exc:
            self._update(task_id, status="failed", stage="failed", progress=100,
                         error_message=str(exc), completed_at=_now())
        except Exception:
            self._update(task_id, status="failed", stage="failed", progress=100,
                         error_message="音频任务失败，请检查服务日志、网络及 FFmpeg 配置",
                         completed_at=_now())

    def get(self, task_id: str) -> dict | None:
        return task_store.get(task_id)

    def _update(self, task_id: str, **fields) -> None:
        task_store.update(task_id, **fields)

    def recover_interrupted(self) -> int:
        return task_store.fail_unfinished()


manager = AudioTaskManager()
