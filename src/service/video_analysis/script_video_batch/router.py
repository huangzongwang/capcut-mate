from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from . import manager
from .schemas import BatchTaskResponse, CreateBatchRequest


router = APIRouter(prefix="/script-video-batch-tasks", tags=["video-analysis-script-video-batch"])


@router.post("", response_model=BatchTaskResponse, status_code=202)
def submit(request: CreateBatchRequest):
    try:
        task = manager.submit(request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None
    return BatchTaskResponse(**task)


@router.get("/{task_id}", response_model=BatchTaskResponse)
def get_status(task_id: str):
    task = manager.get(task_id)
    if not task:
        raise HTTPException(404, "批量口播任务不存在")
    return BatchTaskResponse(**task)


@router.get("/{task_id}/captions.srt")
def get_captions(task_id: str):
    if not task_id.startswith("svb_") or not task_id[4:].isalnum():
        raise HTTPException(400, "无效的批量任务ID")
    root = manager._runtime_root() / task_id
    path = (root / "captions.srt").resolve()
    root = root.resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "字幕文件尚未生成")
    return FileResponse(path, media_type="application/x-subrip; charset=utf-8", filename=f"{task_id}.srt")
