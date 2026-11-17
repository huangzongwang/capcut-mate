from pydantic import BaseModel


class AnalysisTaskSubmitResponse(BaseModel):
    task_id: str
    status: str
    stage: str
    progress: int
    created_at: str


class AnalysisTaskStatusResponse(BaseModel):
    task_id: str
    status: str
    stage: str
    progress: int
    current_scene: int | None = None
    total_scenes: int | None = None
    error_message: str = ""
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    heartbeat_at: str | None = None
    timings: dict = {}


class AnalysisTaskResultResponse(BaseModel):
    task_id: str
    status: str
    result: dict
