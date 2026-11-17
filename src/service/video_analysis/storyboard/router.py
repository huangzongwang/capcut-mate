from typing import Annotated

from fastapi import APIRouter, HTTPException, Path

from . import planner, store
from .schemas import (
    CATEGORIES, CategoriesResponse, ConfirmRequest, EditRequest, GenerateRequest, RegenerateRequest,
    ShotContent, Storyboard, VISUAL_INTENTS,
)


router = APIRouter(prefix="/script_storyboards", tags=["script-storyboard"])

BoardId = Annotated[str, Path(description="生成接口返回的storyboard_id，例如sb_加随机标识。")]
ShotId = Annotated[str, Path(description="当前分镜文档中shots[].shot_id；局部重生成时选中此镜头。")]
RESPONSE_NOTE = (
    "\n\n主应用成功响应在业务字段顶层附加code=0及message，没有data包装层。"
    "原响应中间件会把错误包装为HTTP 200及非零code，必须检查code；"
    "下列错误状态码说明的是路由层语义。路由层422在主应用中统一包装为code=1001；"
    "当前中间件可能不保留业务校验的具体文本。"
)
ERRORS = {
    404: {"description": "分镜文档或镜头不存在。"},
    409: {"description": "版本冲突、镜头已锁定，或机理图缺少产品资料。"},
    422: {"description": "字段、原文覆盖或镜头ID不合法。主应用统一包装为code=1001，业务校验的具体文本可能丢失。"},
    502: {"description": "模型输出经有限重试仍不合法，未保存不完整结果。"},
    503: {"description": "模型配置不存在、未启用或模型调用失败。"},
}


def load(board_id: str, version: int | None = None) -> Storyboard:
    board = store.get(board_id)
    if board is None:
        raise HTTPException(404, "分镜不存在")
    if version is not None and version != board.version:
        raise HTTPException(409, "分镜版本已变化，请重新读取")
    return board


def save(board: Storyboard, version: int) -> Storyboard:
    try:
        return store.save(board, version)
    except store.VersionConflict as exc:
        raise HTTPException(409, str(exc)) from exc


def model_error(exc: planner.PlanningError) -> HTTPException:
    return HTTPException(503 if isinstance(exc, planner.ModelUnavailable) else 502, str(exc))


@router.get(
    "/categories", response_model=CategoriesResponse, summary="查询分镜分类与规则版本",
    description="无需请求体，不调用模型。返回六类兼容大类及十八类视觉意图，按完整语义使用，不按产品名称强制映射。" + RESPONSE_NOTE,
)
def categories() -> dict:
    return {"categories": CATEGORIES, "visual_intents": VISUAL_INTENTS, "rules_version": "1.5"}


@router.post(
    "", response_model=Storyboard, summary="根据脚本生成语义分镜",
    description="同步等待一次模型规划，完成本地结构校验后保存并返回完整初稿，非后台任务提交接口。"
    "不执行自动修复或第二次模型复核；所有镜头初始为待人工确认，不触发数字人生成或草稿导出。"
    "请求可能产生模型费用，尚无请求幂等键，超时后不要盲目自动重试。" + RESPONSE_NOTE,
    responses={code: ERRORS[code] for code in (422, 502, 503)},
)
def create(request: GenerateRequest) -> Storyboard:
    """Generate a reviewable storyboard; never trigger avatar generation or draft export."""
    try:
        shots, model = planner.generate(request)
    except planner.PlanningError as exc:
        raise model_error(exc) from exc
    return store.create(request, shots, model)


@router.get(
    "/{board_id}", response_model=Storyboard, summary="读取已保存分镜",
    description="返回当前完整文档、保存版本及审核状态，不调用模型，不改变已有分类。原文位置不是视频时间。" + RESPONSE_NOTE,
    responses={404: ERRORS[404]},
)
def read(board_id: BoardId) -> Storyboard:
    return load(board_id)


@router.patch(
    "/{board_id}", response_model=Storyboard, summary="编辑完整镜头列表",
    description="支持修改、拆分、合并及锁定/解锁。虽使用PATCH，但shots必须提交完整列表，不是单镜增量。"
    "请求字段严格校验，禁止直接回传完整响应Shot中的服务器字段。不得改写或重排原文。"
    "内容变化会执行本地结构校验并重置未锁定镜头的人工确认；编辑和只修改锁状态都不调用模型。"
    "解锁与修改内容必须分两次提交。成功返回完整文档，version加1。" + RESPONSE_NOTE,
    responses={code: ERRORS[code] for code in (404, 409, 422, 503)},
)
def edit(board_id: BoardId, request: EditRequest) -> Storyboard:
    board = load(board_id, request.expected_version)
    fields = set(ShotContent.model_fields)
    contents = [ShotContent.model_validate(s.model_dump(include=fields)) for s in request.shots]
    try:
        planner.validate_content(board.request, contents)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    originals = {s.shot_id: s for s in board.shots}
    provided_ids = [s.shot_id for s in request.shots if s.shot_id]
    if len(set(provided_ids)) != len(provided_ids) or any(i not in originals for i in provided_ids):
        raise HTTPException(422, "镜头ID重复或不存在；新拆分镜头请省略shot_id")
    cursor = 0
    for item, content in zip(request.shots, contents):
        original = originals.get(item.shot_id)
        if original and original.locked:
            if (
                content.model_dump() != original.model_dump(include=fields)
                or cursor != original.source_start
            ):
                raise HTTPException(409, "锁定镜头不可修改或移动，请先单独解锁")
        cursor += len(content.source_text)
    if any(s.locked and s.shot_id not in provided_ids for s in board.shots):
        raise HTTPException(409, "不能删除或合并锁定镜头")
    unchanged = [
        originals.get(item.shot_id) is not None
        and content.model_dump() == originals[item.shot_id].model_dump(include=fields)
        for item, content in zip(request.shots, contents)
    ]
    # Lock/unlock-only updates do not need a paid model request or reset approval.
    if all(unchanged) and provided_ids == [s.shot_id for s in board.shots]:
        for shot, item in zip(board.shots, request.shots):
            shot.locked = item.locked
        return save(board, request.expected_version)
    try:
        effective_request = board.request.model_copy(update={
            "model_id": request.model_id or board.request.model_id,
        })
        shots = planner.review_contents(
            effective_request, contents, planner.configured_model(effective_request),
        )
    except planner.PlanningError as exc:
        raise model_error(exc) from exc
    for index, (shot, item) in enumerate(zip(shots, request.shots)):
        original = originals.get(item.shot_id)
        if original:
            shot.shot_id = original.shot_id
        # Locked shots remain byte-for-byte stable except for an explicit unlock.
        if original and original.locked:
            shots[index] = original.model_copy(update={"locked": item.locked})
        else:
            shot.locked = item.locked
        shots[index].semantic_review = shots[index].semantic_review.model_copy(update={"shot_index": index})
    board.shots = shots
    return save(board, request.expected_version)


@router.post(
    "/{board_id}/confirm", response_model=Storyboard, summary="人工确认指定镜头",
    description="不调用模型。将所选镜头标记confirmed并保存审核说明，原警告及初稿审核占位信息保留。"
    "机理图缺少非空product_reference时禁止确认；需携带资料重新创建分镜。"
    "所有镜头确认后文档status为confirmed，仅代表人工审核完成，不代表已生成视频。"
    "成功返回完整文档，version加1。" + RESPONSE_NOTE,
    responses={code: ERRORS[code] for code in (404, 409, 422)},
)
def confirm(board_id: BoardId, request: ConfirmRequest) -> Storyboard:
    board = load(board_id, request.expected_version)
    wanted = set(request.shot_ids)
    if not wanted.issubset({s.shot_id for s in board.shots}):
        raise HTTPException(422, "待确认的镜头不存在")
    if any(s.shot_id in wanted and s.category == "mechanism" for s in board.shots):
        if not board.request.product_reference.strip():
            raise HTTPException(409, "机理图缺少产品资料，请提供资料重新生成分镜")
    for shot in board.shots:
        if shot.shot_id in wanted:
            shot.review_status = "confirmed"
            shot.review_note = request.review_note
    return save(board, request.expected_version)


@router.post(
    "/{board_id}/shots/{shot_id}/regenerate", response_model=Storyboard, summary="重新生成单镜语义规划",
    description="携带整篇脚本作为上下文，仅重做选中镜头的原文范围，不改写台词。"
    "选中镜头可拆成多个镜头；仅一镜时沿用原ID，多镜时分配新ID。"
    "新结果须重新人工确认，其他镜头内容和确认状态保留，数组索引可能变化。"
    "锁定镜头不允许重生成。同步调用模型，成功返回完整文档，version加1。" + RESPONSE_NOTE,
    responses={code: ERRORS[code] for code in (404, 409, 422, 502, 503)},
)
def regenerate(board_id: BoardId, shot_id: ShotId, request: RegenerateRequest) -> Storyboard:
    board = load(board_id, request.expected_version)
    index = next((i for i, s in enumerate(board.shots) if s.shot_id == shot_id), None)
    if index is None:
        raise HTTPException(404, "镜头不存在")
    original = board.shots[index]
    if original.locked:
        raise HTTPException(409, "锁定镜头不能重新生成")
    # Keep the full script as context, but only replace the selected source interval.
    local_request = board.request.model_copy(update={
        "script": original.source_text, "model_id": request.model_id or board.request.model_id,
    })
    try:
        config = planner.configured_model(local_request)
        shots, _ = planner.generate(local_request, config, context={
            "full_script": board.request.script,
            "source_start": original.source_start,
            "source_end": original.source_end,
        })
    except planner.PlanningError as exc:
        raise model_error(exc) from exc
    for shot in shots:
        shot.source_start += original.source_start
        shot.source_end += original.source_start
    if len(shots) == 1:
        shots[0].shot_id = original.shot_id
    board.shots[index:index + 1] = shots
    for position, shot in enumerate(board.shots):
        shot.semantic_review.shot_index = position
    return save(board, request.expected_version)
