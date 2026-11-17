from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateLocalExportTaskRequest(StrictModel):
    draft_ids: list[str] = Field(min_length=1, max_length=20)
    resolution: Literal["8K", "4K", "2K", "1080P", "720P", "480P"] | None = "1080P"
    framerate: Literal[24, 25, 30, 50, 60] | None = 30
    timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    continue_on_error: bool = True

    @field_validator("draft_ids")
    @classmethod
    def validate_draft_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("draft_ids cannot contain duplicates")
        for value in values:
            if not 1 <= len(value) <= 128 or not all(
                character.isalnum() or character in "_-" for character in value
            ):
                raise ValueError(f"Invalid draft id: {value}")
        return values


class LocalExportItemResponse(BaseModel):
    item_id: str
    position: int
    draft_id: str
    status: str
    stage: str
    progress: int
    output_path: str = ""
    video_url: str = ""
    error_message: str = ""
    attempts: int = 0
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    timings: dict = Field(default_factory=dict)


class LocalExportTaskResponse(BaseModel):
    task_id: str
    status: str
    stage: str
    progress: int
    total_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    cancel_requested: bool
    error_message: str = ""
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    items: list[LocalExportItemResponse] = Field(default_factory=list)

