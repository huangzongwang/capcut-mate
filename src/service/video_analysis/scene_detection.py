import asyncio
import json
import os
import signal
import sys
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit
from src.service.video_analysis.platform_runtime import (
    analysis_python, analysis_temporary_directory, media_root,
    WindowsWorkerJob, worker_creation_kwargs, worker_environment,
)

from src.schemas.video_analysis import (
    DetectScenesRequest, DetectScenesResponse, SplitScenesResponse,
    TranscribeVideoRequest, TranscribeVideoResponse, ExtractFeaturesResponse,
)


class SceneDetectionError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def build_worker_input(request: DetectScenesRequest) -> dict:
    data = request.model_dump()
    if request.video_path is not None:
        root = media_root()
        relative = Path(request.video_path)
        if relative.is_absolute() or PureWindowsPath(request.video_path).drive:
            raise SceneDetectionError(400, "video_path must be relative to SCENE_MEDIA_ROOT")
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root):
            raise SceneDetectionError(400, "video_path is outside SCENE_MEDIA_ROOT")
        if not candidate.is_file():
            raise SceneDetectionError(404, "Video file not found")
        data["video_path"] = str(candidate)
    else:
        try:
            url = urlsplit(request.video_url)
            allowed = {
                value.strip().lower()
                for value in os.getenv("SCENE_ALLOWED_MEDIA_HOSTS", "").split(",")
                if value.strip()
            }
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.fragment
                or url.netloc.lower() not in allowed
            ):
                raise ValueError()
            # Validate malformed ports before starting the subprocess.
            _ = url.port
        except ValueError:
            raise SceneDetectionError(400, "URL must use HTTP(S) and an explicitly trusted host:port")
    data["threshold"] = request.threshold if request.threshold is not None else (
        27.0 if request.detector == "content" else 3.0
    )
    is_enhanced = request.detector == "enhanced"
    data["min_content_val"] = request.min_content_val if request.min_content_val is not None else (30.0 if is_enhanced else 15.0)
    data["window_width"] = request.window_width if request.window_width is not None else (4 if request.detector in ("enhanced", "auto") else 2)
    data["persistence_ratio"] = request.persistence_ratio if request.persistence_ratio is not None else (1.5 if request.detector in ("enhanced", "auto") else 0.0)
    data["max_bytes"] = int(os.getenv("SCENE_MAX_FILE_BYTES", str(200 * 1024 * 1024)))
    data["max_duration_seconds"] = int(os.getenv("SCENE_MAX_DURATION_SECONDS", "600"))
    return data


async def detect_scenes(request: DetectScenesRequest) -> DetectScenesResponse:
    data = build_worker_input(request)
    # The parent owns temporary files so killing a timed-out worker cannot leak downloads.
    with analysis_temporary_directory("capcut-scenes-") as directory:
        data["work_directory"] = directory
        return await _run_worker(data)


async def transcribe_video(
    request: TranscribeVideoRequest,
    detection: DetectScenesResponse | dict | None = None,
) -> TranscribeVideoResponse:
    data = build_worker_input(request)
    data["operation"] = "transcribe"
    if detection is not None:
        data["detection_override"] = (
            detection.model_dump() if hasattr(detection, "model_dump") else detection
        )
    with analysis_temporary_directory("capcut-transcribe-") as directory:
        data["work_directory"] = directory
        return await _run_worker(data)


async def _run_worker(data: dict) -> DetectScenesResponse | SplitScenesResponse | TranscribeVideoResponse:
    job = None
    if os.name == "nt" and data.get("operation") in (
        "split", "transcribe", "transcribe_scene_clips", "prepare_analysis",
    ):
        try:
            job = WindowsWorkerJob()
        except (ImportError, OSError) as exc:
            raise SceneDetectionError(503, "Windows analysis requires pywin32 in the API environment") from exc
    try:
        return await _run_worker_process(data, job)
    finally:
        if job is not None:
            job.close()


async def _run_worker_process(data: dict, job=None):
    python = analysis_python()
    worker = Path(__file__).with_name("worker.py")
    operation = data.get("operation")
    managed = operation in ("split", "transcribe", "transcribe_scene_clips", "prepare_analysis")
    try:
        process = await asyncio.create_subprocess_exec(
            python, str(worker),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=worker_environment(),
            **(worker_creation_kwargs() if managed else {}),
        )
    except (OSError, NotImplementedError) as exc:
        raise SceneDetectionError(503, "Scene detection runtime is unavailable") from exc
    if job is not None:
        try:
            job.attach(process.pid)
        except BaseException:
            process.kill()
            await process.wait()
            raise
    try:
        lock_wait = max(0, int(data.get("worker_lock_wait_seconds", 0)))
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(data).encode("utf-8")),
                timeout=lock_wait + int(
                os.getenv("SCENE_TRANSCRIBE_TIMEOUT_SECONDS", "1800")
                if operation in ("transcribe", "transcribe_scene_clips")
                else os.getenv("SCENE_SPLIT_TIMEOUT_SECONDS", "900") if operation in ("split", "prepare_analysis")
                else os.getenv("SCENE_TIMEOUT_SECONDS", "180")
            ),
        )
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if managed or process.returncode is None:
            try:
                if job is not None:
                    job.close()
                elif managed:
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise SceneDetectionError(504, "Scene detection timed out")
    try:
        result = json.loads(stdout)
        if process.returncode != 0:
            if stderr:
                print(stderr.decode("utf-8", errors="replace"), file=sys.stderr, flush=True)
            if managed and job is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            raise SceneDetectionError(result.get("status", 400), result["error"])
        if operation in ("prepare_analysis", "transcribe_scene_clips"):
            return result
        model = (TranscribeVideoResponse if operation == "transcribe"
                 else ExtractFeaturesResponse if operation == "extract_features"
                 else SplitScenesResponse if operation == "split" else DetectScenesResponse)
        return model.model_validate(result)
    except (ValueError, KeyError) as exc:
        raise SceneDetectionError(500, "Invalid response from scene detection worker") from exc
