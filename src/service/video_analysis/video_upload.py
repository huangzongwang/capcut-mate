"""Persist uploaded source videos under the configured analysis media root."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

from fastapi import UploadFile
from src.service.video_analysis.platform_runtime import (
    analysis_temporary_directory, digital_human_root, ensure_writable_directory, media_root,
)


async def save_video_upload(upload: UploadFile, purpose: str = "video-analysis") -> dict:
    if purpose not in ("video-analysis", "digital-human"):
        raise ValueError("purpose must be video-analysis or digital-human")
    root = media_root() if purpose == "video-analysis" else digital_human_root()
    ensure_writable_directory(root)
    directory = root / "uploads"
    ensure_writable_directory(directory)
    if not directory.resolve().is_relative_to(root.resolve()):
        raise OSError("Upload directory is outside the configured media root")
    suffix = Path(upload.filename or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".mp4"

    filename = f"{uuid4().hex}{suffix}"
    destination = directory / filename
    limit_name, default_limit = (
        ("DIGITAL_HUMAN_MAX_FILE_BYTES", 2 * 1024 * 1024 * 1024)
        if purpose == "digital-human"
        else ("SCENE_MAX_FILE_BYTES", 200 * 1024 * 1024)
    )
    limit = int(os.getenv(limit_name, str(default_limit)))
    total = 0
    try:
        with analysis_temporary_directory("video-upload-") as work:
            temporary = Path(work) / filename
            with temporary.open("xb") as output:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("Video exceeds the configured file size limit")
                    await asyncio.to_thread(output.write, chunk)
            if total == 0:
                raise ValueError("Uploaded video is empty")
            await asyncio.to_thread(shutil.copyfile, temporary, destination)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise

    return {
        "video_path": f"uploads/{filename}",
        "purpose": purpose,
        "filename": filename,
        "size_bytes": total,
    }
