from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from . import jianying_adapter
from .manager import manager
from .schemas import CreateLocalExportTaskRequest, LocalExportTaskResponse


router = APIRouter(tags=["video-analysis-local-export"])


@router.get("/local-export/health")
def health(probe_ui: bool = Query(default=False)) -> dict:
    return jianying_adapter.inspect_environment(probe_ui=probe_ui)


@router.post("/local-export-tasks", response_model=LocalExportTaskResponse)
def submit(request: CreateLocalExportTaskRequest) -> LocalExportTaskResponse:
    try:
        task = manager.submit(request)
    except (RuntimeError, OSError, ValueError) as exc:
        raise HTTPException(503, str(exc)) from None
    return LocalExportTaskResponse.model_validate(task)


@router.get("/local-export-tasks/{task_id}", response_model=LocalExportTaskResponse)
def get_task(task_id: str) -> LocalExportTaskResponse:
    task = manager.get(task_id)
    if task is None:
        raise HTTPException(404, "本地剪映导出任务不存在")
    return LocalExportTaskResponse.model_validate(task)


@router.post("/local-export-tasks/{task_id}/cancel", response_model=LocalExportTaskResponse)
def cancel(task_id: str) -> LocalExportTaskResponse:
    task = manager.cancel(task_id)
    if task is None:
        raise HTTPException(404, "本地剪映导出任务不存在")
    return LocalExportTaskResponse.model_validate(task)


@router.post("/local-export-tasks/{task_id}/retry-failed", response_model=LocalExportTaskResponse)
def retry_failed(task_id: str) -> LocalExportTaskResponse:
    count, task = manager.retry_failed(task_id)
    if count == -1 or task is None:
        raise HTTPException(404, "本地剪映导出任务不存在")
    if count == -2:
        raise HTTPException(409, "任务仍在排队或执行中")
    if count == 0:
        raise HTTPException(409, "任务没有可重试的失败项")
    return LocalExportTaskResponse.model_validate(task)

