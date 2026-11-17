from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse

from . import client, storage
from .manager import manager
from .schemas import (
    AudioTaskResponse, AudioTaskSubmitResponse, CredentialsRequest, CredentialsResponse,
    GenerateAudioRequest,
)


router = APIRouter(prefix="/audio", tags=["video-analysis-audio"])


@router.get("/credentials", response_model=CredentialsResponse)
def credentials_status():
    return manager.credentials_status()


@router.post("/credentials", response_model=CredentialsResponse)
def set_credentials(request: CredentialsRequest):
    try:
        return manager.configure(request.bpid, request.cookie)
    except client.CredentialsExpired as exc:
        raise HTTPException(401, str(exc)) from None
    except client.AudioProviderError as exc:
        raise HTTPException(502, str(exc)) from None


@router.delete("/credentials", response_model=CredentialsResponse)
def clear_credentials():
    return manager.clear_credentials()


@router.post("/tasks", response_model=AudioTaskSubmitResponse)
def submit_task(request: GenerateAudioRequest, background_tasks: BackgroundTasks):
    storage.cleanup_old_files()
    try:
        submitted = manager.submit(request)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from None
    task = submitted["task"]
    background_tasks.add_task(manager.execute, task["task_id"], request, submitted["credentials"])
    return AudioTaskSubmitResponse(**{
        key: task[key] for key in ("task_id", "status", "stage", "progress", "created_at")
    })


@router.get("/tasks/{task_id}", response_model=AudioTaskResponse)
def task_status(task_id: str):
    task = manager.get(task_id)
    if not task:
        raise HTTPException(404, "音频任务不存在")
    return AudioTaskResponse(**{key: value for key, value in task.items()
                                if key in AudioTaskResponse.model_fields})


@router.get("/files/{task_id}", response_class=StreamingResponse)
def download_audio(task_id: str):
    try:
        path = storage.audio_path(task_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except FileNotFoundError:
        raise HTTPException(404, "音频文件不存在或任务尚未完成") from None

    def chunks():
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                yield chunk

    return StreamingResponse(chunks(), media_type="audio/wav", headers={
        "Content-Length": str(path.stat().st_size),
        "Content-Disposition": f'attachment; filename="{task_id}.wav"',
        "Accept-Ranges": "none",
    })
