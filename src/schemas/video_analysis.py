from typing import Literal

from pydantic import BaseModel, Field, model_validator


class DetectScenesRequest(BaseModel):
    video_path: str | None = Field(
        default=None, min_length=1, max_length=2048,
        description="Relative path inside SCENE_MEDIA_ROOT; mutually exclusive with video_url.",
    )
    video_url: str | None = Field(
        default=None, min_length=1, max_length=8192,
        description="HTTP(S) URL on a configured trusted host; redirects are not followed.",
    )
    detector: Literal["adaptive", "content", "enhanced", "auto"] = "enhanced"
    threshold: float | None = Field(
        default=None, gt=0, le=255, allow_inf_nan=False,
        description="Default: adaptive=3.0, content=27.0. Lower values detect more cuts.",
    )
    min_scene_len: int = Field(default=15, ge=1, le=1500, description="Minimum scene length in frames.")
    min_content_val: float | None = Field(
        default=None, ge=0, le=255, allow_inf_nan=False,
        description="Minimum content change: adaptive/auto default 15; enhanced default 30.",
    )
    window_width: int | None = Field(
        default=None, ge=1, le=30,
        description="Lookaround in frames: adaptive default 2; enhanced/auto default 4.",
    )
    score_region: Literal["full", "roi"] = Field(
        default="full", description="Enhanced only. roi uses vertical 8%-80%, not semantic text removal.",
    )
    persistence_ratio: float | None = Field(
        default=None, ge=0, le=20, allow_inf_nan=False,
        description="Enhanced/auto default 1.5. Zero disables the ratio gate; auto retains spatial checks.",
    )
    refine_seconds: float = Field(
        default=0.0, ge=0, le=0.5, allow_inf_nan=False,
        description="Enhanced only. Optional local visual boundary refinement; off by default.",
    )
    include_diagnostics: bool = False

    @model_validator(mode="after")
    def exactly_one_source(self):
        if (self.video_path is None) == (self.video_url is None):
            raise ValueError("Provide exactly one of video_path or video_url")
        if self.detector not in ("enhanced", "auto") and (
            self.score_region != "full" or self.persistence_ratio is not None
            or self.refine_seconds != 0
        ):
            raise ValueError("Post-processing options require detector=enhanced")
        if self.include_diagnostics and self.detector not in ("enhanced", "auto"):
            raise ValueError("Diagnostics require detector=enhanced")
        if self.detector == "content" and (self.min_content_val is not None or self.window_width is not None):
            raise ValueError("Adaptive parameters do not apply to detector=content")
        if self.detector == "auto" and (self.score_region != "full" or self.refine_seconds != 0):
            raise ValueError("auto selects regions and exact candidate frames; score_region/refine_seconds are enhanced-only")
        return self


class DetectedScene(BaseModel):
    index: int
    start_frame: int
    end_frame: int
    start_us: int
    end_us: int
    duration_us: int
    start_timecode: str
    end_timecode: str


class DetectScenesResponse(BaseModel):
    detector: str
    threshold: float
    min_scene_len: int
    fps: float
    frame_count: int
    duration_us: int
    scene_count: int
    scenes: list[DetectedScene]
    warnings: list[str]
    settings: dict = Field(default_factory=dict)
    diagnostics: list[dict] = Field(default_factory=list)


class TranscriptWord(BaseModel):
    start: float
    end: float
    word: str
    probability: float | None = None


class TranscriptSegment(BaseModel):
    id: int
    start: float
    end: float
    text: str
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    words: list[TranscriptWord] = Field(default_factory=list)


class SceneTranscript(BaseModel):
    scene_index: int
    start_us: int
    end_us: int
    text: str
    segment_ids: list[int] = Field(default_factory=list)
    speech_seconds: float = 0.0
    speech_density: float = 0.0
    transcription_status: Literal["completed", "no_audio", "failed"] = "completed"
    transcription_error: str = ""
    transcription_seconds: float = 0.0
    retry_count: int = 0


class TranscribeVideoRequest(DetectScenesRequest):
    model_name: Literal["large-v3"] = Field(
        default="large-v3", description="Local faster-whisper model. The first request downloads it to the model cache."
    )
    language: str | None = Field(default="zh", min_length=2, max_length=16)
    word_timestamps: bool = True
    vad_filter: bool = True


class TranscribeVideoResponse(BaseModel):
    model_name: str
    device: Literal["cuda", "cpu", "not_used"]
    compute_type: str
    language: str
    language_probability: float | None = None
    duration_seconds: float
    transcription_status: Literal["completed", "no_audio", "partial", "failed"] = "completed"
    segments: list[TranscriptSegment]
    scenes: list[SceneTranscript]
    detection: DetectScenesResponse
    srt: str
    warnings: list[str] = Field(default_factory=list)


class SceneFrameRange(BaseModel):
    start_frame: int = Field(ge=0, strict=True)
    end_frame: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def nonempty(self):
        if self.end_frame <= self.start_frame:
            raise ValueError("Frame ranges must be nonempty and end-exclusive")
        return self


class SplitScenesRequest(DetectScenesRequest):
    detector: Literal["enhanced", "auto"] = "enhanced"
    encoding: Literal["compatible", "lossless"] = Field(
        default="compatible",
        description="Compatible H.264 High by default. Lossless requires a player supporting High 4:4:4 Predictive.",
    )
    encoder: Literal["auto", "cpu", "nvenc"] = Field(
        default="auto",
        description="Auto uses NVIDIA NVENC when available for compatible output, otherwise libx264.",
    )
    boundary_policy: Literal["strict", "allow_review"] = Field(
        default="strict", description="Strict returns needs_review without files for ambiguous boundaries.",
    )
    confirmed_ranges: list[SceneFrameRange] | None = Field(
        default=None, min_length=1, max_length=100,
        description="Optional manually confirmed, ordered frame ranges. Gaps deliberately discard footage.",
    )

    @model_validator(mode="after")
    def ordered_ranges(self):
        if self.encoding == "lossless" and self.encoder == "nvenc":
            raise ValueError("encoder=nvenc only supports encoding=compatible")
        if self.confirmed_ranges and any(
            right.start_frame < left.end_frame
            for left, right in zip(self.confirmed_ranges, self.confirmed_ranges[1:])
        ):
            raise ValueError("Confirmed ranges must be ordered and must not overlap")
        return self


class SplitSceneClip(SceneFrameRange):
    index: int
    start_us: int
    end_us: int
    frame_count: int
    filename: str
    download_path: str
    size_bytes: int
    audio_included: bool
    encoding: Literal["compatible", "lossless"]
    actual_encoder: Literal["libx264", "h264_nvenc"]
    verification: Literal["all_decoded_frames_match", "frame_count_and_quality_checked"]
    min_frame_psnr_db: float | None = None


class SplitScenesResponse(BaseModel):
    status: Literal["completed", "needs_review"]
    job_id: str | None = None
    detection: DetectScenesResponse
    clips: list[SplitSceneClip] = Field(default_factory=list)
    boundary_reviews: list[dict] = Field(default_factory=list)
    review_required: bool
    requested_encoder: Literal["auto", "cpu", "nvenc"] = "auto"
    actual_encoder: Literal["libx264", "h264_nvenc"] | None = None
    warnings: list[str] = Field(default_factory=list)


class VisualSceneFeature(BaseModel):
    scene_index: int
    start_us: int
    end_us: int
    sample_timestamps_us: list[int]
    brightness: float
    saturation: float
    edge_density: float
    motion_score: float
    sample_images: list[str] = Field(default_factory=list)
    ocr_items: list[dict] = Field(default_factory=list)
    subtitle_candidates: list[dict] = Field(default_factory=list)
    fixed_text_items: list[dict] = Field(default_factory=list)
    ocr_status: Literal["disabled", "completed", "completed_no_text", "partial", "unavailable"] = "disabled"
    ocr_warnings: list[str] = Field(default_factory=list)
    semantic_status: Literal["pending_model_analysis"] = "pending_model_analysis"


class ExtractFeaturesRequest(DetectScenesRequest):
    sample_points: Literal["auto", 1, 3] = Field(default="auto")
    include_sample_images: bool = False
    include_ocr: bool = False


class ExtractFeaturesResponse(BaseModel):
    detection: DetectScenesResponse
    scenes: list[VisualSceneFeature]
    ocr_status: Literal["disabled", "completed", "completed_no_text", "partial", "unavailable"] = "disabled"
    warnings: list[str] = Field(default_factory=list)


class AnalyzeVideoRequest(ExtractFeaturesRequest):
    video_fps: float = Field(default=2.0, gt=0, le=5, description="Cloud video sampling rate")
    scene_model_id: int | None = Field(default=None, ge=1, description="Online myh_video_models.id for video analysis")
    global_model_id: int | None = Field(default=None, ge=1, description="Online myh_video_models.id for text summarization")
    scene_fallback_model_id: int | None = Field(default=None, ge=1, description="Optional backup video model, used after primary scene failure.")
    global_fallback_model_id: int | None = Field(default=None, ge=1, description="Optional backup text model, used after primary global failure.")
    use_transcription: bool = True
    use_external_model: bool = True
    use_ocr: bool = Field(default=False, description="Compatibility field; unified analysis does not run OCR. Use extract_features with include_ocr instead.")

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_model(cls, data):
        if isinstance(data, dict) and "model_config_id" in data:
            raise ValueError("model_config_id was removed; use scene_model_id and global_model_id")
        return data

    @model_validator(mode="after")
    def require_online_models(self):
        if self.use_external_model and (self.scene_model_id is None or self.global_model_id is None):
            raise ValueError("External analysis requires scene_model_id and global_model_id")
        for primary, backup in (
            (self.scene_model_id, self.scene_fallback_model_id),
            (self.global_model_id, self.global_fallback_model_id),
        ):
            if backup is not None and (not self.use_external_model or backup == primary):
                raise ValueError("Fallback models require external analysis and must differ from the primary")
        return self


class SceneSemanticAnalysis(BaseModel):
    model_id: int | None = None
    fallback_used: bool = False
    analysis_status: Literal["completed", "failed", "disabled"] = "disabled"
    analysis_source: str = "video_model"
    content: str = ""
    hook_score: float | None = Field(default=None, ge=0, le=100)
    hook_reason: str = ""
    transition_type: str = "unknown"
    transition_reason: str = ""
    continuity_score: float | None = Field(default=None, ge=0, le=1)
    scene_index: int
    scene_category: Literal[
        "product_empty_scene", "pain_point", "motion", "intro",
        "oral_delivery", "mixed", "unknown"
    ] = "unknown"
    visual_intent: Literal[
        "product_overview", "product_detail", "instruction_manual", "otc_certification",
        "qr_traceability", "authenticity_comparison", "usage_instruction", "medication_moment",
        "pain_point", "body_condition", "exercise_action", "mechanism_explanation",
        "lifestyle_scene", "service_support", "factory_process", "experience_proof", "cta",
        "talking_head", "opening", "other", "unknown",
    ] = "unknown"
    primary_visual_subject: Literal[
        "product", "person", "body", "action", "mechanism_graphic", "environment",
        "document", "text_graphic", "multiple", "unknown",
    ] = "unknown"
    on_camera_speech: Literal["confirmed", "absent", "uncertain"] = "uncertain"
    people: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    scene_type: str = "unknown"
    shot_type: str = "unknown"
    camera_motion: str = "unknown"
    ocr_text: list[str] = Field(default_factory=list)
    semantic_confidence: float = 0.0
    replaceable: bool = False
    replace_reason: str = ""
    replace_score: float = Field(default=0.0, ge=0, le=1)
    replacement_type: str = "unknown"
    actions: list[str] = Field(default_factory=list)
    emotions: list[str] = Field(default_factory=list)
    clothing: list[str] = Field(default_factory=list)
    environment: list[str] = Field(default_factory=list)
    lighting: str = "unknown"
    composition: str = "unknown"
    camera_angle: str = "unknown"
    dialogue: str = ""
    dialogue_inferred: bool = False


class TransitionAnalysis(BaseModel):
    analysis_status: str = "unavailable"
    analysis_source: str = "video_model"
    from_scene: int
    to_scene: int
    transition_type: str = "cut"
    continuity_score: float | None = Field(default=None, ge=0, le=1)
    highlight: str = ""


class ViralAnalysis(BaseModel):
    analysis_source: str = "video_model"
    analysis_status: str = "unavailable"
    hook_scenes: list[int] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    score: float = 0.0
    scene_scores: dict[int, float] = Field(default_factory=dict)


class AnalyzeVideoResponse(BaseModel):
    fallbacks: list[dict] = Field(default_factory=list)
    global_model_used_id: int | None = None
    detection: DetectScenesResponse
    transcript: TranscribeVideoResponse | None = None
    visual_features: list[VisualSceneFeature]
    semantics: list[SceneSemanticAnalysis]
    transitions: list[TransitionAnalysis]
    viral_analysis: ViralAnalysis
    scene_captions: list[dict] = Field(
        default_factory=list,
        description="按分镜整理的语音字幕数据，时间单位为微秒。",
    )
    scene_model_id: int | None = None
    global_model_id: int | None = None
    models_used: dict = Field(default_factory=dict)
    model_used: str | None = None
    analysis_report: dict = Field(default_factory=dict)
    draft: dict = Field(
        default_factory=dict,
        description="分析完成后自动生成的剪映草稿信息。",
    )
    warnings: list[str] = Field(default_factory=list)
