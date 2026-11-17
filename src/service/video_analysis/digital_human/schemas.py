from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SubmitRequest(StrictModel):
    video_path: str = Field(min_length=1, max_length=2048)
    audio_path: str = Field(min_length=1, max_length=2048)
    duix_api_base_url: str = Field(min_length=8, max_length=512)
    duix_shared_dir: str = Field(min_length=3, max_length=2048)
    chaofen: int = Field(default=0, ge=0, le=1)
    watermark_switch: int = Field(default=0, ge=0, le=1)
    pn: int = Field(default=1, ge=1, le=10)


class SubmitResponse(BaseModel):
    task_id: str
    status: str
    stage: str
    progress: int
    status_url: str


class TaskResponse(BaseModel):
    task_id: str
    status: str
    stage: str
    progress: int
    error_message: str = ""
    created_at: str
    completed_at: str | None = None
    preview_url: str | None = None

