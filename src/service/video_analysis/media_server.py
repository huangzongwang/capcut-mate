"""Optional Windows media sidecar; leaves the framework middleware untouched."""
from pathlib import Path

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

import config
from src.service.video_analysis.platform_runtime import (
    digital_human_root, ensure_writable_directory, media_root, scene_draft_root, scene_split_root,
)
from src.service.video_analysis.local_export.storage import export_root as local_export_root


def create_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # Only media outputs are exposed. StaticFiles rejects traversal and symlink escapes.
    for url, directory in (
        ("/media", media_root()),
        ("/output/analysis-clips", scene_draft_root()),
        ("/output/scene-clips", scene_split_root()),
        ("/output/digital-human", digital_human_root() / "results"),
        ("/output/local-exports", local_export_root()),
        ("/output/draft", Path(config.DRAFT_DIR)),
    ):
        ensure_writable_directory(directory)
        app.mount(url, StaticFiles(directory=str(directory), follow_symlink=False))
    return app
