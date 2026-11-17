from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path as PathParameter
from starlette.concurrency import run_in_threadpool

from . import task_store, planner
from .router import RESPONSE_NOTE
from .schemas import GenerateRequest, Storyboard, TaskResponse, TaskResultResponse
from .task_manager import manager, result_root


@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(manager.start)
    try:
        yield
    finally:
        await run_in_threadpool(manager.stop)


router = APIRouter(prefix="/script_storyboard_tasks", tags=["script-storyboard-tasks"], lifespan=lifespan)
TaskId = Annotated[str, PathParameter(description="提交接口返回的sbt_任务ID。")]


def response(task: dict) -> TaskResponse:
    return TaskResponse(
        **{key: task[key] for key in TaskResponse.model_fields if key not in ("timings", "model_request")},
        timings=json.loads(task["timings_json"]),
        model_request=json.loads(task.get("model_request_json") or "{}"),
    )


def load(task_id: str) -> dict:
    task = task_store.get(task_id)
    if task is None:
        raise HTTPException(404, "分镜任务不存在")
    return task


@router.post(
    "", response_model=TaskResponse, summary="提交脚本分镜任务",
    description="校验并持久化脚本后返回任务ID，不等待模型。单Worker后台执行，不生成数字人或视频。"
    "尚无请求幂等键，不要因超时盲目重复提交。" + RESPONSE_NOTE,
    responses={503: {"description": "Worker不可用或任务持久化失败。"}},
)
def submit(request: GenerateRequest) -> TaskResponse:
    try:
        return response(manager.submit(request))
    except planner.PlanningError as exc:
        raise HTTPException(503, str(exc)) from None
    except Exception:
        raise HTTPException(503, "分镜队列不可用，请检查Worker及存储") from None


@router.get(
    "/{task_id}", response_model=TaskResponse, summary="查询脚本分镜任务",
    description="建议每2至5秒查询。请求成功仍须检查status；failed是任务失败，waiting_review是生成完成待人工确认。"
    + RESPONSE_NOTE,
    responses={404: {"description": "任务不存在。"}},
)
def read(task_id: TaskId) -> TaskResponse:
    return response(load(task_id))


@router.get(
    "/{task_id}/result", response_model=TaskResultResponse, summary="读取分镜任务生成快照",
    description="仅成功终态可读取。result是首次生成快照，不随人工编辑变化；最新版本使用分镜读取接口。"
    + RESPONSE_NOTE,
    responses={
        404: {"description": "任务不存在。"},
        409: {"description": "任务尚未完成或已经失败。"},
        500: {"description": "结果文件缺失、损坏或路径不合法。"},
    },
)
def result(task_id: TaskId) -> TaskResultResponse:
    task = load(task_id)
    if task["status"] not in ("completed", "completed_with_warnings"):
        raise HTTPException(409, "分镜任务尚未成功完成")
    try:
        path = Path(task["result_path"]).resolve()
        if path.parent != result_root() or path.name != f"{task_id}.json":
            raise ValueError("Invalid result path")
        board = Storyboard.model_validate_json(path.read_text(encoding="utf-8"))
        if board.storyboard_id != task["storyboard_id"]:
            raise ValueError("Mismatched result")
    except (OSError, ValueError, TypeError):
        raise HTTPException(500, "分镜结果文件不可用，请检查存储") from None
    return TaskResultResponse(
        task_id=task_id, status=task["status"], storyboard_id=board.storyboard_id, result=board,
    )
