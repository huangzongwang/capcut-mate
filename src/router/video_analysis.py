from contextlib import asynccontextmanager

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from src.schemas.video_analysis import (
    DetectScenesRequest, DetectScenesResponse, SplitScenesRequest, SplitScenesResponse,
    TranscribeVideoRequest, TranscribeVideoResponse,
    ExtractFeaturesRequest, ExtractFeaturesResponse,
    AnalyzeVideoRequest, AnalyzeVideoResponse,
)
from src.schemas.analysis_tasks import (
    AnalysisTaskResultResponse, AnalysisTaskStatusResponse, AnalysisTaskSubmitResponse,
)
from src.schemas.video_analysis_draft import (
    CreateAnalysisDraftRequest, CreateAnalysisDraftResponse,
    ReplaceDraftSegmentRequest, ReplaceDraftSegmentResponse,
)
from src.service.video_analysis import model_config_store
from src.service.video_analysis.feature_extraction import extract_features
from src.service.video_analysis.semantic_analysis import analyze_video
from src.service.video_analysis.analysis_task_manager import manager
from src.service.video_analysis import analysis_task_store
from src.service.video_analysis.video_upload import save_video_upload
import json
from pathlib import Path
from src.service.video_analysis.scene_detection import SceneDetectionError, detect_scenes, transcribe_video
from src.service.video_analysis.scene_splitting import clip_path, split_scenes
from src.service.video_analysis.draft_builder import create_draft_from_analysis
from src.service.video_analysis.draft_replacement import replace_draft_segment
from src.service.video_analysis.storyboard.router import router as storyboard_router
from src.service.video_analysis.storyboard.task_router import router as storyboard_task_router
from src.service.video_analysis.audio_generation.router import router as audio_generation_router
from src.service.video_analysis.digital_human.router import router as digital_human_router
from src.service.video_analysis.script_video_batch.router import router as script_video_batch_router
from src.service.video_analysis.local_export.router import router as local_export_router
from src.service.video_analysis.replacement_drafts.router import router as replacement_drafts_router


@asynccontextmanager
async def _video_analysis_lifespan(app):
    from src.service.video_analysis.digital_human.manager import startup_recover
    from src.service.video_analysis.audio_generation.manager import manager as audio_manager
    from src.service.video_analysis.script_video_batch.task_store import fail_interrupted as fail_script_batches
    from src.service.video_analysis.local_export.manager import manager as local_export_manager
    from src.service.video_analysis.replacement_drafts import manager as replacement_drafts_manager

    await run_in_threadpool(startup_recover)
    await run_in_threadpool(audio_manager.recover_interrupted)
    await run_in_threadpool(fail_script_batches)
    await run_in_threadpool(local_export_manager.start)
    await run_in_threadpool(replacement_drafts_manager.start)
    try:
        yield
    finally:
        await run_in_threadpool(replacement_drafts_manager.stop)
        await run_in_threadpool(local_export_manager.stop)


router = APIRouter(
    prefix="/v1", tags=["video-analysis"], lifespan=_video_analysis_lifespan,
)
router.include_router(storyboard_router)
router.include_router(storyboard_task_router)
router.include_router(audio_generation_router)
router.include_router(digital_human_router)
router.include_router(script_video_batch_router)
router.include_router(local_export_router)
router.include_router(replacement_drafts_router)


@router.post("/upload_video")
async def upload_video_endpoint(
    file: UploadFile = File(...), purpose: str = Form(default="video-analysis"),
) -> dict:
    if purpose not in ("video-analysis", "digital-human"):
        await file.close()
        raise HTTPException(status_code=400, detail="purpose must be video-analysis or digital-human")
    try:
        result = await save_video_upload(file, purpose)
        if Path(file.filename or "").suffix.lower() in {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"}:
            result["audio_path"] = result.pop("video_path")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not save uploaded video") from exc
    finally:
        await file.close()


@router.post("/detect_scenes", response_model=DetectScenesResponse)
async def detect_scenes_endpoint(request: DetectScenesRequest) -> DetectScenesResponse:
    """Detect shot boundaries without modifying or splitting the source video."""
    try:
        return await detect_scenes(request)
    except SceneDetectionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.post("/split_scenes", response_model=SplitScenesResponse)
async def split_scenes_endpoint(request: SplitScenesRequest) -> SplitScenesResponse:
    """Review enhanced boundaries, export and verify frame-exact clips."""
    try:
        return await split_scenes(request)
    except SceneDetectionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.post("/transcribe_video", response_model=TranscribeVideoResponse)
async def transcribe_video_endpoint(request: TranscribeVideoRequest) -> TranscribeVideoResponse:
    """Transcribe locally with faster-whisper large-v3 and align speech to detected scenes."""
    try:
        return await transcribe_video(request)
    except SceneDetectionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.get("/models")
def list_online_models() -> dict:
    try:
        models = model_config_store.list_configs()
        # Explicit allowlist: never return arbitrary metadata or provider secrets.
        return {"models": [
            {key: value[key] for key in (
                "id", "provider_id", "model", "name", "provider", "enabled",
                "capabilities", "model_updated_at", "provider_updated_at",
            )} for value in models
        ]}
    except model_config_store.ModelConfigError as exc:
        raise HTTPException(503, str(exc)) from None


@router.api_route("/model_configs", methods=["GET", "POST"])
@router.api_route("/model_configs/{config_id}", methods=["PATCH", "DELETE"])
def retired_model_configs(config_id: int | None = None):
    raise HTTPException(410, "Local model configs removed. Use GET /models; manage models in the online platform.")


@router.post("/extract_features", response_model=ExtractFeaturesResponse)
async def extract_features_endpoint(request: ExtractFeaturesRequest) -> ExtractFeaturesResponse:
    try:
        return await extract_features(request)
    except SceneDetectionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.post("/analyze_video", response_model=AnalyzeVideoResponse)
async def analyze_video_endpoint(request: AnalyzeVideoRequest) -> AnalyzeVideoResponse:
    try:
        return await analyze_video(request)
    except SceneDetectionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except model_config_store.ModelConfigError as exc:
        raise HTTPException(503, str(exc)) from None


@router.post("/analyze_video_tasks", response_model=AnalysisTaskSubmitResponse)
def submit_analyze_video_task(request: AnalyzeVideoRequest) -> AnalysisTaskSubmitResponse:
    try:
        task = manager.submit(request)
    except model_config_store.ModelConfigError as exc:
        raise HTTPException(503, str(exc)) from None
    return AnalysisTaskSubmitResponse(
        task_id=task["task_id"], status=task["status"], stage=task["stage"],
        progress=task["progress"], created_at=task["created_at"],
    )


@router.get("/analyze_video_tasks/{task_id}", response_model=AnalysisTaskStatusResponse)
def get_analyze_video_task(task_id: str) -> AnalysisTaskStatusResponse:
    task = analysis_task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="分析任务不存在")
    return AnalysisTaskStatusResponse(
        task_id=task["task_id"], status=task["status"], stage=task["stage"],
        progress=task["progress"], current_scene=task["current_scene"],
        total_scenes=task["total_scenes"], error_message=task["error_message"],
        created_at=task["created_at"], started_at=task["started_at"],
        completed_at=task["completed_at"], heartbeat_at=task["heartbeat_at"],
        timings=json.loads(task.get("timings_json") or "{}"),
    )


@router.get("/analyze_video_tasks/{task_id}/result", response_model=AnalysisTaskResultResponse)
def get_analyze_video_task_result(task_id: str) -> AnalysisTaskResultResponse:
    task = analysis_task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="分析任务不存在")
    if task["status"] not in ("completed", "completed_with_warnings"):
        raise HTTPException(status_code=409, detail="分析任务尚未完成")
    path = Path(task["result_path"]).resolve()
    from src.service.video_analysis.platform_runtime import video_analysis_report_root
    root = video_analysis_report_root().resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=500, detail="分析结果文件不存在")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="分析结果无法读取") from exc
    return AnalysisTaskResultResponse(
        task_id=task["task_id"], status=task["status"], result=result,
    )


@router.post(
    "/analyze_video_tasks/{task_id}/create_draft",
    response_model=CreateAnalysisDraftResponse,
)
async def create_analysis_draft(
    task_id: str,
    request: CreateAnalysisDraftRequest,
) -> CreateAnalysisDraftResponse:
    """Create a Jianying/CapCut draft whose clips follow the detected scene ranges."""
    task = analysis_task_store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="分析任务不存在")
    try:
        result = await create_draft_from_analysis(
            task_id,
            width=request.width,
            height=request.height,
            render_preview=request.render_preview,
            api_key=request.api_key,
            include_unreplaceable=request.include_unreplaceable,
            embed_scene_clips=request.embed_scene_clips,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"创建草稿失败: {exc}") from exc
    return CreateAnalysisDraftResponse.model_validate(result)


@router.post(
    "/drafts/replace_segment",
    response_model=ReplaceDraftSegmentResponse,
)
async def replace_analysis_draft_segment(
    request: ReplaceDraftSegmentRequest,
) -> ReplaceDraftSegmentResponse:
    """Replace one video segment while keeping the same draft and timeline slot."""
    try:
        result = await replace_draft_segment(request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"替换分镜失败: {exc}") from exc
    return ReplaceDraftSegmentResponse.model_validate(result)


@router.get("/scene_clips/{job_id}/{filename}", response_class=StreamingResponse)
def download_scene_clip(job_id: str, filename: str):
    try:
        path = clip_path(job_id, filename)
    except SceneDetectionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    def chunks():
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                yield chunk

    # Full downloads only: the existing response middleware cannot pass through HTTP 206.
    return StreamingResponse(chunks(), media_type="video/mp4", headers={
        "Content-Length": str(path.stat().st_size),
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Accept-Ranges": "none",
    })
