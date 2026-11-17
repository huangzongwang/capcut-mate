"""API-side output ownership; video processing stays in the isolated worker."""

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

from src.schemas.video_analysis import SplitScenesRequest, SplitScenesResponse
from src.service.video_analysis.scene_detection import SceneDetectionError, _run_worker, build_worker_input
from src.service.video_analysis.platform_runtime import (
    analysis_temp_root, ensure_writable_directory, scene_split_root,
)
from src.utils.deferred_delete import enqueue_path


def split_root() -> Path:
    return ensure_writable_directory(scene_split_root().resolve())


async def split_scenes(request: SplitScenesRequest) -> SplitScenesResponse:
    data = build_worker_input(request)
    root = split_root()
    job_id = uuid4().hex
    data.update(operation="split", job_id=job_id,
                max_output_bytes=int(os.getenv("SCENE_SPLIT_MAX_BYTES", str(1024 ** 3))))
    # The parent removes partial exports after worker/group termination, including cancellation.
    work_root = ensure_writable_directory(analysis_temp_root())
    work = Path(tempfile.mkdtemp(prefix=job_id + "-", dir=work_root))
    output = root / job_id
    output_created = published = False
    try:
        output.mkdir()
        output_created = True
        data.update(work_directory=str(work), output_directory=str(output))
        result = await _run_worker(data)
        if result.status == "completed":
            manifest = work / "manifest.json"
            manifest.write_text(
                json.dumps(result.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8",
            )
            shutil.copyfile(manifest, output / "manifest.json")
            published = True
        return result
    except OSError as exc:
        raise SceneDetectionError(500, "Could not publish scene clips to the configured output directory") from exc
    finally:
        for directory in [work] + ([output] if output_created and not published else []):
            try:
                shutil.rmtree(directory)
            except OSError:
                # Windows bind mounts may keep files open briefly; reuse the project's retry queue.
                enqueue_path(str(directory), is_dir=True)


def clip_path(job_id: str, filename: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id) or not re.fullmatch(r"scene-\d{3}\.mp4", filename):
        raise SceneDetectionError(404, "Clip not found")
    root = split_root()
    path = (root / job_id / filename).resolve()
    if not path.is_relative_to(root) or not path.is_file() or not (path.parent / "manifest.json").is_file():
        raise SceneDetectionError(404, "Clip not found")
    return path
