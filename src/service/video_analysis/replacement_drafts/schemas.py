from __future__ import annotations

from pydantic import BaseModel, Field


class CreateReplacementDraftsRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=20, description="要创建的裂变草稿数量")
    width: int = Field(default=1080, ge=1, le=7680)
    height: int = Field(default=1920, ge=1, le=7680)
    material_review_primary_model_id: int | None = Field(default=None, ge=1)
    material_review_backup_model_id: int | None = Field(default=None, ge=1)
    min_match_score: float = Field(default=0.58, ge=0, le=1)
    keep_original_audio: bool = Field(default=True, description="将原视频音轨独立保留，替换素材静音")
    preserve_dialogue_captions: bool = Field(default=True, description="恢复被替换分镜在分析报告中保存的逐字时间戳字幕")
    preserve_on_screen_text: bool = Field(
        default=True,
        description="兼容字段：使用OCR校正语音字幕文字，不创建包装字、水印或贴纸等独立文字轨",
    )


class ReplacementDraftTaskResponse(BaseModel):
    task_id: str
    source_analysis_task_id: str
    status: str
    stage: str
    progress: int
    count: int
    completed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    deleted_count: int = 0
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    error_message: str = ""
    versions: list[dict] = Field(default_factory=list)


class SceneMaterialCandidate(BaseModel):
    rank: int
    asset_id: int
    temporal_feature_id: int
    asset_name: str
    url: str
    clip_start_us: int
    clip_end_us: int
    usable_start_us: int
    usable_end_us: int
    match_score: float
    semantic_similarity: float
    visual_intent_score: float
    auto_selectable: bool


class SceneMaterialCandidatesResponse(BaseModel):
    analysis_task_id: str
    shot_index: int
    scene_category: str = ""
    scene_content: str = ""
    scene_duration_us: int
    min_match_score: float
    candidate_count: int
    url_unavailable_count: int = 0
    candidates: list[SceneMaterialCandidate] = Field(default_factory=list)


class ReplaceSelectedDraftMaterialRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=128)
    segment_id: str = Field(min_length=1, max_length=128)
    analysis_task_id: str = Field(min_length=1, max_length=128)
    shot_index: int = Field(ge=1)
    asset_id: int = Field(ge=1)
    temporal_feature_id: int = Field(ge=1)
    source_start_us: int | None = Field(default=None, ge=0)
    volume: float | None = Field(default=None, ge=0, le=10)


class ReplaceSelectedDraftMaterialResponse(BaseModel):
    draft_id: str
    draft_url: str
    modified_in_place: bool = True
    segment_id: str
    track_id: str
    start: int
    end: int
    asset_id: int
    temporal_feature_id: int
    asset_name: str
    video_url: str
    source_start_us: int
    volume: float

