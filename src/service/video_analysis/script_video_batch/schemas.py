from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateBatchRequest(StrictModel):
    storyboard_id: str = Field(pattern=r"^sb_[0-9a-f]{32}$")
    source_mode: Literal["existing_video", "digital_human"] = "existing_video"
    # Local relative path inside VIDEO_ANALYSIS_MEDIA_ROOT, returned by upload_video.
    source_video_path: str | None = Field(default=None, min_length=1, max_length=2048)
    # Existing-video mode: optional audio source to use instead of the video's audio.
    source_audio_path: str | None = Field(default=None, min_length=1, max_length=2048)
    voice_element_id: str | None = Field(default=None, min_length=1, max_length=100)
    voice_speed: float = Field(default=1.1, ge=0.5, le=2.0, allow_inf_nan=False)
    pronunciation_overrides: list[dict[str, str]] = Field(default_factory=list, max_length=50)
    # Digital-human mode requires a pre-approved silent avatar base video and Duix settings.
    avatar_video_path: str | None = Field(default=None, min_length=1, max_length=2048)
    duix_api_base_url: str | None = Field(default=None, min_length=8, max_length=512)
    duix_shared_dir: str | None = Field(default=None, min_length=3, max_length=2048)
    count: int = Field(default=20, ge=1, le=20)
    width: int = Field(default=1080, ge=1, le=7680)
    height: int = Field(default=1920, ge=1, le=7680)
    split_screen_enabled: bool = False
    split_fit_mode: Literal["contain_blur", "contain_black", "cover"] = "contain_blur"
    split_ratio: float = Field(default=0.5, ge=0.25, le=0.75, allow_inf_nan=False)
    split_gap_px: int = Field(default=0, ge=0, le=512)
    pip_enabled: bool = False
    pip_probability: float = Field(default=0.35, ge=0.05, le=1.0, allow_inf_nan=False)
    pip_scale: float = Field(default=0.28, ge=0.1, le=0.5, allow_inf_nan=False)
    pip_mask_mode: Literal["circle", "mirror", "auto"] = "circle"
    max_drafts_in_parallel: int = Field(default=2, ge=1, le=4)
    keep_master_audio: bool = True
    background_music_enabled: bool = True
    background_music_volume: float = Field(default=0.0, ge=0.0, le=2.0, allow_inf_nan=False)
    subtitle_font: str = Field(default="点宋体", min_length=1, max_length=100)
    subtitle_font_size: int = Field(default=12, ge=1, le=200)
    subtitle_text_color: str = Field(default="#ffde00", pattern=r"^#[0-9a-fA-F]{6}$")
    subtitle_bold: bool = False
    subtitle_underline: bool = False
    subtitle_italic: bool = False
    subtitle_transform_y: float | None = Field(default=None, ge=-7680, le=7680)
    subtitle_remove_punctuation: bool = True
    subtitle_in_animation: str | None = Field(default="打字机 II", max_length=100)
    pause_mode: Literal["off", "hybrid"] = "hybrid"
    pause_detect_ms: int = Field(default=300, ge=100, le=5000)
    pause_keep_ms: int = Field(default=220, ge=50, le=1000)
    long_pause_ms: int = Field(default=1000, ge=500, le=10000)
    cover_long_pauses: bool = True
    material_review_primary_model_id: int | None = Field(
        default=None, ge=1,
        description="myh_video_models.id；传入后启用素材视频模型审核",
    )
    material_review_backup_model_id: int | None = Field(
        default=None, ge=1,
        description="主审核模型失败或没有确认候选时使用的备用模型ID",
    )

    @model_validator(mode="after")
    def validate_mode_inputs(self):
        if self.source_mode == "existing_video" and not self.source_video_path:
            raise ValueError("existing_video 模式必须提供 source_video_path")
        if self.source_mode == "digital_human":
            if not self.avatar_video_path:
                raise ValueError("digital_human 模式必须提供 avatar_video_path")
            if not self.source_audio_path and not self.voice_element_id:
                raise ValueError("digital_human 模式须提供 source_audio_path 或 voice_element_id")
            if not self.duix_api_base_url or not self.duix_shared_dir:
                raise ValueError("digital_human 模式须提供 duix_api_base_url 与 duix_shared_dir")
        if self.pause_keep_ms > self.pause_detect_ms:
            raise ValueError("pause_keep_ms 不能大于 pause_detect_ms")
        if self.long_pause_ms <= self.pause_detect_ms:
            raise ValueError("long_pause_ms 必须大于 pause_detect_ms")
        if self.split_screen_enabled and self.height - self.split_gap_px < 2:
            raise ValueError("split_gap_px 过大，无法生成上下分屏")
        if self.material_review_backup_model_id and not self.material_review_primary_model_id:
            raise ValueError("配置备用审核模型前必须先配置主审核模型")
        if self.material_review_primary_model_id \
                and self.material_review_primary_model_id == self.material_review_backup_model_id:
            raise ValueError("主审核模型与备用审核模型不能相同")
        return self

    @field_validator("pronunciation_overrides")
    @classmethod
    def validate_pronunciation_overrides(cls, values: list[dict[str, str]]) -> list[dict[str, str]]:
        for item in values:
            if set(item) != {"text", "phonetic"} or not item["text"].strip() or not item["phonetic"].strip():
                raise ValueError("pronunciation_overrides 每项必须包含非空 text 和 phonetic")
        return values

    @field_validator("source_video_path", "source_audio_path", "avatar_video_path")
    @classmethod
    def relative_media_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.replace("\\", "/")
        if normalized.startswith("uploads/"):
            normalized = normalized[len("uploads/"):]
        parts = normalized.split("/")
        if normalized.startswith("/") or ":" in normalized or any(part in ("", ".", "..") for part in parts):
            raise ValueError("媒体路径必须是根目录下的安全相对路径")
        return normalized

class BatchTaskResponse(BaseModel):
    task_id: str
    status: str
    stage: str
    progress: int
    count: int
    completed_count: int = 0
    failed_count: int = 0
    storyboard_id: str | None = None
    material_review_primary_model_id: int | None = None
    material_review_backup_model_id: int | None = None
    error_message: str = ""
    created_at: str
    completed_at: str | None = None
    versions: list[dict] = Field(default_factory=list)
