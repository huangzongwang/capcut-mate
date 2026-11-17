from pydantic import BaseModel, Field


class CreateAnalysisDraftRequest(BaseModel):
    embed_scene_clips: bool = Field(
        default=True,
        description="将正式切割后的每个分镜作为独立素材写入草稿，便于单独预览和替换。",
    )
    use_original_video: bool = Field(
        default=True,
        description="将原视频按检测出的分镜源时间段加入草稿。",
    )
    include_unreplaceable: bool = Field(
        default=True,
        description="是否保留口播等不可替换分镜；当前版本只影响说明，不会丢弃原始素材。",
    )
    render_preview: bool = Field(
        default=False,
        description="是否提交已有云渲染接口。需要提供 gen_video 所需的 UUID API Key。",
    )
    api_key: str | None = Field(default=None, description="云渲染 UUID API Key")
    width: int = Field(default=1080, ge=1, le=7680)
    height: int = Field(default=1920, ge=1, le=7680)


class CreateAnalysisDraftResponse(BaseModel):
    task_id: str
    draft_id: str
    draft_url: str
    scene_count: int
    track_id: str = ""
    segment_ids: list[str] = Field(default_factory=list)
    segment_infos: list[dict] = Field(default_factory=list)
    source_video_url: str
    media_mode: str = Field(
        default="source_timerange",
        description="草稿素材使用模式；source_timerange 表示复用源视频并由时间范围裁剪。",
    )
    media_note: str = Field(default="", description="素材链接与分镜裁剪关系说明")
    scene_clips: list[dict] = Field(default_factory=list, description="独立分镜素材信息")
    preview_url: str = ""
    preview_status_url: str = ""
    render_submitted: bool = False
    render_message: str = ""
    warnings: list[str] = Field(default_factory=list)


class ReplaceDraftSegmentRequest(BaseModel):
    draft_url: str
    segment_id: str
    video_url: str
    source_start: int = Field(default=0, ge=0, description="替换素材的源视频起点，微秒")
    volume: float = Field(default=1.0, ge=0, le=10)


class ReplaceDraftSegmentResponse(BaseModel):
    draft_url: str
    segment_id: str
    track_id: str
    start: int
    end: int
    source_start: int
    video_url: str
