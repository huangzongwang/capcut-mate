"""Phase-2 local visual feature extraction."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

from src.schemas.video_analysis import ExtractFeaturesRequest, ExtractFeaturesResponse
from src.service.video_analysis.scene_detection import SceneDetectionError, build_worker_input
from src.service.video_analysis.platform_runtime import analysis_python, WindowsWorkerJob, worker_environment


def _parse_worker_json(raw: bytes) -> dict:
    text = raw.decode("utf-8", errors="replace").strip()
    # PaddleOCR/PaddleX may write model-loading notices to stdout. The worker
    # emits its JSON result on the final line, so tolerate those notices.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise SceneDetectionError(
        500,
        f"Invalid feature extraction response: {text[-500:]}",
    )


def _run_worker(data: dict) -> dict:
    python = analysis_python()
    worker = Path(__file__).with_name("worker.py")
    job = None
    try:
        if os.name == "nt":
            job = WindowsWorkerJob()
        with subprocess.Popen(
            [python, str(worker)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=worker_environment(),
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        ) as process:
            try:
                if job is not None:
                    job.attach(process.pid)
                stdout, _ = process.communicate(
                    json.dumps(data).encode("utf-8"),
                    timeout=int(os.getenv("SCENE_FEATURE_TIMEOUT_SECONDS", "900")),
                )
            except BaseException:
                if job is not None:
                    job.close()
                process.kill()
                process.communicate()
                raise
    except subprocess.TimeoutExpired as exc:
        raise SceneDetectionError(504, "Feature extraction timed out") from exc
    except (ImportError, OSError) as exc:
        raise SceneDetectionError(503, "Feature extraction runtime is unavailable") from exc
    finally:
        if job is not None:
            job.close()
    if process.returncode:
        payload = _parse_worker_json(stdout)
        raise SceneDetectionError(payload.get("status", 400), payload.get("error", "Feature extraction failed"))
    return _parse_worker_json(stdout)


async def extract_features(request: ExtractFeaturesRequest) -> ExtractFeaturesResponse:
    data = build_worker_input(request)
    data["operation"] = "extract_features"
    data["sample_points"] = request.sample_points
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _run_worker, data)
    return ExtractFeaturesResponse.model_validate(result)
