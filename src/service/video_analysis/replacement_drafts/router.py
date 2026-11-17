from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from . import manager
from .schemas import (
    CreateReplacementDraftsRequest, ReplacementDraftTaskResponse,
    SceneMaterialCandidatesResponse,
    ReplaceSelectedDraftMaterialRequest, ReplaceSelectedDraftMaterialResponse,
)

router = APIRouter(tags=["video-analysis-replacement-drafts"])


@router.get(
    "/analyze_video_tasks/{analysis_task_id}/scenes/{shot_index}/material_candidates",
    response_model=SceneMaterialCandidatesResponse,
)
def get_scene_material_candidates(
    analysis_task_id: str,
    shot_index: int,
    limit: int = Query(default=10, ge=1, le=10),
    min_match_score: float = Query(default=0.58, ge=0, le=1),
):
    try:
        return manager.scene_material_candidates(
            analysis_task_id, shot_index, limit=limit, min_match_score=min_match_score,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/replacement_drafts/replace_material",
    response_model=ReplaceSelectedDraftMaterialResponse,
)
async def replace_selected_draft_material(request: ReplaceSelectedDraftMaterialRequest):
    try:
        return await manager.replace_selected_draft_material(request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"下载或写入替换素材失败: {exc}") from exc


@router.post(
    "/analyze_video_tasks/{analysis_task_id}/replacement_draft_tasks",
    response_model=ReplacementDraftTaskResponse,
    status_code=202,
)
def submit(analysis_task_id: str, request: CreateReplacementDraftsRequest):
    try:
        return manager.submit(analysis_task_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/replacement_draft_tasks/{task_id}",
    response_model=ReplacementDraftTaskResponse,
)
def get_status(task_id: str):
    task = manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="裂变替换草稿任务不存在")
    return task


@router.delete(
    "/replacement_draft_tasks/{task_id}/versions/{version}",
    response_model=ReplacementDraftTaskResponse,
)
def delete_version(task_id: str, version: int):
    try:
        task = manager.delete_version(task_id, version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="裂变替换草稿任务或版本不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"删除裂变草稿失败: {exc}") from exc
    if not task:
        raise HTTPException(status_code=404, detail="裂变替换草稿任务或版本不存在")
    return task

