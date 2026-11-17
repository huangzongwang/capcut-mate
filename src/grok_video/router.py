from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import model, store, first_frame
from .manager import manager
from .schemas import (
    GrokVideoTaskRequest,
    GrokVideoTaskResultResponse,
    GrokVideoTaskStatusResponse,
    GrokVideoTaskSubmitResponse,
)

@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(manager.start)
    try:
        yield
    finally:
        await run_in_threadpool(manager.stop)


router = APIRouter(prefix="/v1", tags=["grok-video"], lifespan=lifespan)


def _status(task: dict) -> GrokVideoTaskStatusResponse:
    return GrokVideoTaskStatusResponse(
        task_id=task["task_id"], status=task["status"], stage=task["stage"],
        progress=task["progress"], provider_request_id=task["provider_request_id"] or None,
        error_message=task["error_message"], created_at=task["created_at"],
        started_at=task["started_at"], completed_at=task["completed_at"],
        heartbeat_at=task["heartbeat_at"],
        first_frame=json.loads(task["first_frame_json"]) if task.get("first_frame_json") else None,
    )


def _load(task_id: str) -> dict:
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "Grok Video任务不存在")
    return task


@router.post("/grok_video_tasks", response_model=GrokVideoTaskSubmitResponse)
def submit(request: GrokVideoTaskRequest):
    try:
        task = manager.submit(request)
    except model.GrokVideoModelError as exc:
        raise HTTPException(503, str(exc)) from None
    except Exception:
        raise HTTPException(503, "Grok Video queue unavailable") from None
    return GrokVideoTaskSubmitResponse(
        task_id=task["task_id"], status=task["status"], stage=task["stage"],
        progress=task["progress"], created_at=task["created_at"],
    )


@router.get("/grok_video_tasks/{task_id}", response_model=GrokVideoTaskStatusResponse)
def status(task_id: str):
    return _status(_load(task_id))


@router.get("/grok_video_tasks/{task_id}/result", response_model=GrokVideoTaskResultResponse)
def result(task_id: str):
    task = _load(task_id)
    if task["status"] != "completed":
        raise HTTPException(409, "Grok Video任务尚未完成")
    try:
        value = json.loads(task["result_json"])
    except (TypeError, ValueError):
        raise HTTPException(500, "Grok Video结果不可用") from None
    return GrokVideoTaskResultResponse(task_id=task_id, status=task["status"], result=value)


@router.post("/grok_video_tasks/{task_id}/retry_download", response_model=GrokVideoTaskStatusResponse)
def retry_download(task_id: str):
    _load(task_id)
    if not store.retry_download(task_id):
        raise HTTPException(409, "Only download_failed tasks can retry downloading")
    return _status(_load(task_id))


@router.post("/grok_video_tasks/{task_id}/confirm_first_frame", response_model=GrokVideoTaskStatusResponse)
def confirm_first_frame(task_id: str):
    task = _load(task_id)
    if not task.get("first_frame_json") or not first_frame.path_for(task_id).is_file():
        raise HTTPException(409, "Saved first frame is unavailable")
    if not store.continue_first_frame(task_id):
        raise HTTPException(409, "Task is not awaiting first-frame confirmation")
    return _status(_load(task_id))


@router.post("/grok_video_tasks/{task_id}/retry_cached_stage", response_model=GrokVideoTaskStatusResponse)
def retry_cached_stage(task_id: str):
    _load(task_id)
    if not store.retry_cached_stage(task_id):
        raise HTTPException(409, "No safely retryable cached stage; unknown submissions cannot be retried")
    return _status(_load(task_id))


@router.get("/grok_video_tasks/{task_id}/first_frame", response_class=StreamingResponse)
def first_frame_content(task_id: str):
    task = _load(task_id)
    if not task.get("first_frame_json"):
        raise HTTPException(409, "First frame is not ready")
    path = first_frame.path_for(task_id)
    if not path.is_file():
        raise HTTPException(404, "First frame file is missing")

    def chunks():
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                yield chunk

    return StreamingResponse(chunks(), media_type="image/png", headers={
        "Content-Length": str(path.stat().st_size), "Accept-Ranges": "none",
    })


@router.get("/grok_video_tasks/{task_id}/content", response_class=StreamingResponse)
def content(task_id: str):
    task = _load(task_id)
    if task["status"] != "completed":
        raise HTTPException(409, "Grok Video任务尚未完成")
    try:
        root = store.result_root()
        path = (root / f"{task_id}.mp4").resolve()
        if path.parent != root or not path.is_file():
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(500, "Grok Video文件不存在") from None
    def chunks():
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                yield chunk

    # Framework middleware cannot handle 206; provide full streaming downloads.
    return StreamingResponse(chunks(), media_type="video/mp4", headers={
        "Content-Length": str(path.stat().st_size),
        "Content-Disposition": f'attachment; filename="{task_id}.mp4"',
        "Accept-Ranges": "none",
    })
