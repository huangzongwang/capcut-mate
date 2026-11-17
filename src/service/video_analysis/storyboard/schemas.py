from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Text = Annotated[str, Field(min_length=1, max_length=4000)]
ShortText = Annotated[str, Field(min_length=1, max_length=300)]
Category = Literal[
    "product_empty_scene", "scene_empty_scene", "body_display",
    "motion", "mechanism", "oral_delivery",
]
VisualIntent = Literal[
    "pain_point", "product_overview", "product_detail", "instruction_manual", "otc_certification",
    "qr_traceability", "authenticity_comparison", "usage_instruction",
    "medication_moment",
    "factory_process", "service_support", "body_condition", "exercise_action",
    "mechanism_explanation", "lifestyle_scene", "experience_proof", "cta",
    "talking_head",
]
CATEGORIES = {
    "product_empty_scene": "产品空境",
    "scene_empty_scene": "场景空境",
    "body_display": "身材展示",
    "motion": "运动",
    "mechanism": "机理图",
    "oral_delivery": "口播",
}
VISUAL_INTENTS = {
    "pain_point": "痛点展示",
    "product_overview": "产品整体展示",
    "product_detail": "包装细节",
    "instruction_manual": "说明书展示",
    "otc_certification": "OTC/资质标识",
    "qr_traceability": "二维码/溯源码",
    "authenticity_comparison": "真假辨别",
    "usage_instruction": "使用方法/用量",
    "medication_moment": "服药时刻",
    "factory_process": "工厂、生产、发货",
    "service_support": "客服、指导服务",
    "body_condition": "身材展示、体态",
    "exercise_action": "运动行为",
    "mechanism_explanation": "减肥机理、作用机制",
    "lifestyle_scene": "饮食、工作、生活",
    "experience_proof": "体验和效果描述",
    "cta": "链接、购买、行动引导",
    "talking_head": "保留人物口播",
}
DEFAULT_VISUAL_INTENTS = {
    "product_empty_scene": "product_overview",
    "scene_empty_scene": "lifestyle_scene",
    "body_display": "body_condition",
    "motion": "exercise_action",
    "mechanism": "mechanism_explanation",
    "oral_delivery": "talking_head",
}
VISUAL_INTENT_PARENTS = {
    "pain_point": {"body_display", "scene_empty_scene"},
    "product_overview": {"product_empty_scene"},
    "product_detail": {"product_empty_scene"},
    "instruction_manual": {"product_empty_scene"},
    "otc_certification": {"product_empty_scene"},
    "qr_traceability": {"product_empty_scene"},
    "authenticity_comparison": {"product_empty_scene"},
    "usage_instruction": {"product_empty_scene"},
    "medication_moment": {"scene_empty_scene"},
    "factory_process": {"scene_empty_scene"},
    "service_support": {"scene_empty_scene", "oral_delivery"},
    "body_condition": {"body_display"},
    "exercise_action": {"motion"},
    "mechanism_explanation": {"mechanism"},
    "lifestyle_scene": {"scene_empty_scene"},
    "experience_proof": {"body_display", "scene_empty_scene", "oral_delivery"},
    "cta": {"product_empty_scene", "scene_empty_scene", "oral_delivery"},
    "talking_head": {"oral_delivery"},
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerateRequest(StrictModel):
    script: str = Field(
        min_length=1, max_length=12000,
        description="原始脚本文本，1至12000个Unicode码点，须包含有效文字。分镜须按顺序逐字覆盖，包括空白和标点。",
        examples=["下班后去跑步。"],
    )
    product_names: list[ShortText] = Field(
        default_factory=lambda: ["赛乐赛奥利司他胶囊"], min_length=1, max_length=20,
        description="产品全称列表，默认[赛乐赛奥利司他胶囊]。1至20项，每项1至300字符，去首尾空白并去重；"
        "用于身份识别及防止名称被拆镜，不会强制分类为产品空境。",
        examples=[["赛乐赛奥利司他胶囊"]],
    )
    product_reference: str = Field(
        default="", max_length=20000,
        description="供分镜规划和人工审核使用的已审核产品资料，最多20000字符。默认空字符串；缺少非空资料时不能人工确认机理图。",
    )
    model_id: int = Field(ge=1, description="Online myh_video_models.id; required, not a provider ID or API key")

    @field_validator("script")
    @classmethod
    def meaningful_script(cls, value: str) -> str:
        if not any(c.isalnum() for c in value):
            raise ValueError("脚本必须包含有效文字")
        return value

    @field_validator("product_names")
    @classmethod
    def names(cls, values: list[str]) -> list[str]:
        if any(not v.strip() for v in values):
            raise ValueError("产品名称不能为空")
        return list(dict.fromkeys(v.strip() for v in values))


class ShotContent(StrictModel):
    source_text: Text = Field(
        description="本镜对应的原文，1至4000字符，不可改写。全部镜头依次拼接须等于原始script，不能单独包含空白或标点。",
        examples=["下班后去跑步。"],
    )
    category: Category = Field(
        description="语义分镜类别：product_empty_scene=产品空境；scene_empty_scene=场景空境；"
        "body_display=身材展示；motion=运动；mechanism=机理图；oral_delivery=口播。按语义及上下文判断，不按名称匹配。",
        examples=["motion"],
    )
    visual_intent: VisualIntent = Field(
        description="当前镜头唯一的主视觉意图。用于细分同一大类下的产品整体、包装细节、说明书、OTC、溯源、用法等语义，"
        "并直接参与素材检索。旧数据未提供时按category补默认值。",
        examples=["exercise_action"],
    )
    semantic_reason: Text = Field(
        description="分类理由，1至4000字符，解释原文和上下文的表达意图，不能仅复述类别或命中的关键词。",
        examples=["本句描述下班后的跑步行为，应呈现相应运动画面。"],
    )
    evidence: Text = Field(
        description="逐字摘自当前source_text的连续文字，1至4000字符，作为分类依据；不是新增台词。",
        examples=["去跑步"],
    )
    visual_description: Text = Field(
        description="拟呈现的主体、动作、环境等画面要求，1至4000字符。它是分镜规划，不是已经生成或匹配的素材。",
        examples=["人物在户外跑步，中景展示跑步动作。"],
    )
    required_elements: list[ShortText] = Field(
        min_length=1, max_length=20,
        description="画面必须具备的具体元素，1至20项，每项1至300字符且非空白；例如产品身份、动作或身体部位。",
        examples=[["跑步人物", "跑步动作"]],
    )
    forbidden_elements: list[ShortText] = Field(
        default_factory=list, max_length=20,
        description="画面不可出现的元素，默认[]，最多20项，每项1至300字符且非空白。供后续素材匹配及人工审核使用。",
        examples=[["与跑步无关的运动"]],
    )
    relation: Literal["affirmative", "negated", "hypothetical", "contrast", "unspecified"] = Field(
        description="原文语义关系：affirmative=肯定陈述；negated=否定；hypothetical=假设；"
        "contrast=比较或转折；unspecified=无法确定。非肯定关系须检查画面是否与原文相反。",
        examples=["affirmative"],
    )
    confidence: float = Field(
        ge=0, le=1, allow_inf_nan=False,
        description="当前分类的置信度，0至1的有限数值，并非经校准的准确率。低于0.8会产生警告；高分不等于人工确认。",
        examples=[0.9],
    )

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_visual_intent(cls, data):
        if isinstance(data, dict) and not data.get("visual_intent"):
            data = dict(data)
            if data.get("category") in DEFAULT_VISUAL_INTENTS:
                data["visual_intent"] = DEFAULT_VISUAL_INTENTS[data["category"]]
        return data

    @model_validator(mode="after")
    def compatible_visual_intent(self):
        if self.category not in VISUAL_INTENT_PARENTS[self.visual_intent]:
            allowed = "、".join(sorted(VISUAL_INTENT_PARENTS[self.visual_intent]))
            raise ValueError(f"visual_intent={self.visual_intent} 不能归入 {self.category}；允许的大类：{allowed}")
        return self

    @field_validator("source_text", "semantic_reason", "evidence", "visual_description")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("分镜字段不能为空白")
        return value

    @field_validator("required_elements", "forbidden_elements")
    @classmethod
    def nonblank_elements(cls, values: list[str]) -> list[str]:
        if any(not v.strip() for v in values):
            raise ValueError("素材约束不能为空白")
        return values


class ModelShotPlan(StrictModel):
    end_segment_id: int = Field(
        ge=1, strict=True,
        description="本镜包含到哪个标点段结束。镜头起点自动承接上一镜终点，只决定分组，不复制原文。",
    )
    category: Category = Field(description="当前镜头的六类兼容大类。")
    visual_intent: VisualIntent = Field(description="当前镜头唯一的主视觉意图。")
    semantic_reason: Text = Field(description="根据当前标点段原文解释分类理由。")
    visual_description: Text = Field(description="拟呈现的主体、动作和环境。")
    required_elements: list[ShortText] = Field(
        min_length=1, max_length=20, description="素材必须包含的元素。",
    )
    forbidden_elements: list[ShortText] = Field(
        default_factory=list, max_length=20, description="素材不能出现的冲突元素，没有则返回空数组。",
    )
    relation: Literal["affirmative", "negated", "hypothetical", "contrast", "unspecified"] = Field(
        description="当前镜头原文的语义关系。",
    )
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False, description="分类置信度。")


class ModelPlan(StrictModel):
    shots: list[ModelShotPlan] = Field(
        min_length=1, max_length=100,
        description="模型按顺序输出镜头分组；每项只给结束标点段编号，不复制或改写原文。",
    )


class Review(StrictModel):
    shot_index: int = Field(
        ge=0, strict=True, description="镜头在当前shots数组中的零起始索引，不是稳定ID；拆分合并后可能变化。", examples=[0],
    )
    supported: bool = Field(
        strict=True,
        description="兼容字段。当前初稿固定为false，表示未执行自动模型复核；最终以人工review_status为准。",
        examples=[True],
    )
    reason: Text = Field(
        description="兼容字段。当前写入未执行自动复核、等待人工确认的说明，1至4000字符。",
        examples=["初稿未执行自动复核，等待人工确认"],
    )


class ModelReview(StrictModel):
    reviews: list[Review] = Field(
        min_length=1, max_length=100, description="历史自动复核响应结构，仅用于兼容旧文档，当前生成流程不调用。",
    )


class Shot(ShotContent):
    model_usage: dict = Field(default_factory=dict, description="Non-secret model binding and calls for this shot's latest AI operation")
    shot_id: str = Field(description="服务器生成的镜头ID，格式shot_加随机标识。编辑已有镜头时携带；新拆分镜头由服务器分配。")
    source_start: int = Field(
        description="原始script中的起始Unicode码点索引，含此位置，零起始。不是秒数、微秒或UTF-16索引。",
        examples=[0],
    )
    source_end: int = Field(
        description="原始script中的结束Unicode码点索引，不含此位置。原文范围为[source_start, source_end)。",
        examples=[7],
    )
    locked: bool = Field(
        default=False, description="是否锁定，默认false。锁定镜头不可改内容、删除、移动或重新生成；需先单独解锁。",
    )
    review_status: Literal["pending", "confirmed"] = Field(
        default="pending", description="人工审核状态：pending=待确认；confirmed=已人工确认。自动生成均为pending。",
    )
    review_note: str = Field(
        default="", description="最近一次人工确认的说明，默认空字符串。内容变化使确认失效时清空；通过confirm接口填写。",
    )
    warnings: list[str] = Field(
        default_factory=list, description="低置信度、否定关系、机理资料等本地警告。默认[]；人工确认后仍保留供追溯。",
    )
    semantic_review: Review = Field(
        description="初稿审核占位信息。当前流程不自动调用第二次模型复核，最终以人工review_status为准。",
    )


class StoredGenerateRequest(GenerateRequest):
    """Historical documents stay readable; no legacy ID resolution is performed."""
    model_id: int | None = Field(default=None, ge=1, description="Bound online model ID; absent only in historical documents")

    @model_validator(mode="before")
    @classmethod
    def read_legacy_document(cls, data):
        if isinstance(data, dict):
            data = dict(data)
            data.pop("model_config_id", None)
        return data


class Storyboard(StrictModel):
    storyboard_id: str = Field(description="分镜文档ID，格式sb_加随机标识，用作后续接口的board_id。不是后台任务ID。")
    version: int = Field(description="当前保存版本，初始为1。每次成功编辑、确认或局部重生成后加1；下次写入须作为expected_version提交。", examples=[1])
    created_at: str = Field(description="创建时间，UTC ISO 8601字符串，带+00:00时区偏移。", examples=["2026-10-13T03:00:00+00:00"])
    updated_at: str = Field(description="最近保存时间，UTC ISO 8601字符串，带+00:00时区偏移。", examples=["2026-10-13T03:00:00+00:00"])
    request: StoredGenerateRequest = Field(description="Saved request; historical documents may have no online model binding.")
    model_used: str = Field(description="首次生成该分镜文档时使用的模型名称；不是后续每次编辑或局部重生成的模型审计记录。")
    shots: list[Shot] = Field(description="按原文顺序排列的当前镜头列表；镜头内含可编辑内容及服务器维护的定位、审核字段。")
    status: Literal["needs_review", "confirmed"] = Field(
        default="needs_review", description="整份分镜状态：needs_review=至少一镜未人工确认；confirmed=全部已确认，不代表已生成视频。",
    )
    timing_status: Literal["awaiting_audio_alignment"] = Field(
        default="awaiting_audio_alignment", description="当前仅支持awaiting_audio_alignment，表示尚待真实音频对齐，没有可用于视频切片的时间轴。",
    )


class ShotEdit(ShotContent):
    shot_id: str | None = Field(
        default=None, description="已有镜头的ID，必须属于当前文档且不能重复。新增或拆分镜头省略或传null，由服务器生成新ID。",
    )
    locked: bool = Field(
        default=False, description="保存后的锁状态，省略即false，不会自动保留旧值。解除锁定时须先提交一次内容不变的编辑。",
    )


class EditRequest(StrictModel):
    model_id: int | None = Field(default=None, ge=1, description="Optional online model override for this review")
    expected_version: int = Field(ge=1, description="最近读取的文档version，正整数。与当前版本不一致返回409，请重新读取后合并修改。", examples=[1])
    shots: list[ShotEdit] = Field(
        min_length=1, max_length=100,
        description="编辑后的完整镜头列表，1至100项，不是增量。source_text拼接必须保持原脚本；"
        "不要回传source_start、warnings等服务器字段。未提交的旧镜头视作删除，锁定镜头不可删除。",
    )


class ConfirmRequest(StrictModel):
    expected_version: int = Field(ge=1, description="最近读取的文档version，正整数；过期版本返回409。", examples=[1])
    shot_ids: list[str] = Field(
        min_length=1, max_length=100, description="本次人工确认的镜头ID列表，1至100项，均须属于当前文档。不影响未列出的镜头。",
    )
    review_note: str = Field(
        min_length=1, max_length=2000, description="人工审核说明，1至2000字符，不可为空白；写入每个所选镜头，不会清除原警告。",
        examples=["已核对原文、画面要求及警告。"],
    )

    @field_validator("review_note")
    @classmethod
    def nonblank_note(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("需要填写人工审核说明")
        return value


class RegenerateRequest(StrictModel):
    model_id: int | None = Field(default=None, ge=1, description="Optional online model override for this regeneration")
    expected_version: int = Field(ge=1, description="最近读取的文档version，正整数；过期版本返回409。原文与模型配置从已保存的请求读取。", examples=[1])


class CategoriesResponse(StrictModel):
    categories: dict[Category, str] = Field(
        description="六类分镜的英文枚举到中文名称的映射；是允许的分类集合，不是关键词强制分类规则。",
        examples=[CATEGORIES],
    )
    visual_intents: dict[VisualIntent, str] = Field(
        description="十八类视觉意图的英文枚举到中文名称映射。category保持兼容，visual_intent用于精确拆镜与素材匹配。",
        examples=[VISUAL_INTENTS],
    )
    rules_version: str = Field(
        description="当前分镜规则版本。1.5增加服药时刻，并将真实服药动作与用法用量展示分开；与文档保存版本version无关。",
        examples=["1.5"],
    )


TaskStatus = Literal["queued", "running", "completed", "completed_with_warnings", "failed"]


class TaskResponse(StrictModel):
    task_id: str = Field(description="持久化任务ID，sbt_前缀，用于查询任务和生成结果。")
    status: TaskStatus = Field(description="queued排队、running执行、completed完成、completed_with_warnings带警告完成、failed失败。")
    stage: Literal["queued", "planning", "validating", "reviewing", "saving", "waiting_review", "failed"] = Field(
        description="当前阶段。新任务使用queued、planning、saving、waiting_review、failed；validating和reviewing仅兼容旧任务记录。"
        "waiting_review表示生成已结束，等待人工确认，不占用Worker。")
    progress: int = Field(ge=0, le=100, description="阶段性估算进度0至100，不是模型真实进度；失败保留最后进度。")
    storyboard_id: str | None = Field(default=None, description="已保存的分镜ID；尚未保存时为null。失败任务可能已保存分镜。")
    created_at: str = Field(description="创建时间，UTC ISO 8601。")
    started_at: str | None = Field(default=None, description="开始执行时间，未开始为null，UTC ISO 8601。")
    completed_at: str | None = Field(default=None, description="结束时间，未结束为null，UTC ISO 8601。")
    heartbeat_at: str | None = Field(default=None, description="Worker最近心跳，未执行为null；运行期间约每5秒更新。")
    error_message: str | None = Field(default=None, description="失败原因或警告摘要，无问题为null，不含供应商原始异常。")
    timings: dict[str, int] = Field(default_factory=dict, description="已记录阶段耗时，单位毫秒，键如planning_ms；未记录时为空对象。")
    model_request: dict = Field(default_factory=dict, description="最近一次模型通信事件，包含调用ID、阶段、重试次数和耗时；不包含正文或密钥。")


class TaskResultResponse(StrictModel):
    task_id: str = Field(description="生成该结果的任务ID。")
    status: Literal["completed", "completed_with_warnings"] = Field(description="生成任务的成功终态，与人工审核状态独立。")
    storyboard_id: str = Field(description="分镜文档ID，用于读取当前版本及编辑、确认。")
    result: Storyboard = Field(description="生成完成时的不可变分镜快照；后续编辑结果需通过分镜读取接口获取。")
