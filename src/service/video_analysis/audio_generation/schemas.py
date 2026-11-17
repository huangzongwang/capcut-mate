from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CredentialsRequest(StrictModel):
    bpid: Annotated[str, Field(pattern=r"^\d{1,32}$")]
    cookie: Annotated[str, Field(min_length=1, max_length=16000)]

    @field_validator("cookie")
    @classmethod
    def valid_cookie_header(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Cookie must be a single header value")
        return value


class CredentialsResponse(BaseModel):
    configured: bool
    message: str


class PronunciationOverride(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=8)]
    phonetic: Annotated[str, Field(min_length=1, max_length=32)]


class GenerateAudioRequest(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=12000)]
    voice_element_id: Annotated[str, Field(min_length=1, max_length=100)]
    voice_speed: float = Field(default=1.1, ge=0.5, le=2.0, allow_inf_nan=False)
    pronunciation_overrides: list[PronunciationOverride] = Field(default_factory=list, max_length=50)

    @field_validator("text")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text cannot be blank")
        return value


class AudioTaskResponse(BaseModel):
    task_id: str
    status: str
    stage: str
    progress: int
    error_message: str = ""
    created_at: str
    completed_at: str | None = None
    download_url: str | None = None
    format: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    sample_width_bits: int | None = None
    size_bytes: int | None = None


class AudioTaskSubmitResponse(BaseModel):
    task_id: str
    status: str
    stage: str
    progress: int
    created_at: str
