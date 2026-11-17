from fastapi import APIRouter, HTTPException

from . import manager, store
from .schemas import SubmitRequest, SubmitResponse, TaskResponse

router = APIRouter(
    prefix="/digital-human", tags=["video-analysis-digital-human"],
)


@router.post("/tasks", response_model=SubmitResponse, status_code=202)
def submit_task(request: SubmitRequest):
    try:
        task = manager.submit(request)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None
    task_id = task["task_id"]
    return SubmitResponse(
        task_id=task_id, status=task["status"], stage=task["stage"],
        progress=task["progress"], status_url=f"/openapi/capcut-mate/v1/digital-human/tasks/{task_id}",
    )


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str):
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "数字人任务不存在")
    return manager.public_task(task)


@router.post("/tasks/{task_id}/refresh", status_code=202)
def refresh_task(task_id: str):
    task = store.get(task_id)
    if not task:
        raise HTTPException(404, "数字人任务不存在")
    if not manager.resume(task_id):
        if task["status"] == "running" and task["stage"] in (
            "generating", "upstream_0", "upstream_1", "timed_out",
        ):
            return {"task_id": task_id, "status": "running", "message": "查询已在进行中"}
        raise HTTPException(409, "当前任务状态不可恢复查询")
    return {"task_id": task_id, "status": "running", "message": "已恢复 Duix 状态查询"}

