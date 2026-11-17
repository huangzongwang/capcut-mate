from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import config

from ..platform_runtime import ensure_writable_directory, nas_root


def export_root() -> Path:
    configured = os.getenv("VIDEO_ANALYSIS_LOCAL_EXPORT_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return nas_root() / "video-analysis" / "local-exports"


def task_output_directory(task_id: str) -> Path:
    if not task_id.startswith("lex_") or not task_id[4:].isalnum():
        raise ValueError("Invalid local export task id")
    root = ensure_writable_directory(export_root())
    path = (root / task_id).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Invalid local export output path")
    return ensure_writable_directory(path)


def output_path(task_id: str, position: int, draft_id: str) -> Path:
    if not draft_id or not all(character.isalnum() or character in "_-" for character in draft_id):
        raise ValueError("Invalid draft id")
    return task_output_directory(task_id) / f"{position:02d}-{draft_id}.mp4"


def public_base_url() -> str:
    configured = os.getenv("VIDEO_ANALYSIS_LOCAL_EXPORT_PUBLIC_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    return config.DOWNLOAD_URL.rstrip("/") + "/output/local-exports"


def public_url(task_id: str, filename: str) -> str:
    return f"{public_base_url()}/{quote(task_id)}/{quote(filename)}"

