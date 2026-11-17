"""Replace one video segment inside an existing Jianying draft."""
from __future__ import annotations

import os
from copy import deepcopy

import src.pyJianYingDraft as draft
from src.schemas.video_analysis_draft import ReplaceDraftSegmentRequest
from src.utils import helper
from src.utils.download import download
from src.utils.draft_cache import DRAFT_CACHE
from src.utils.draft_lock_manager import get_draft_lock_manager
from src.utils.logger import logger
import config


async def replace_draft_segment(request: ReplaceDraftSegmentRequest) -> dict:
    draft_id = helper.get_url_param(request.draft_url, "draft_id")
    if not draft_id or draft_id not in DRAFT_CACHE:
        raise ValueError("草稿不存在或草稿地址无效")
    lock_manager = get_draft_lock_manager()
    await lock_manager.acquire_lock(draft_id, timeout=30.0)
    try:
        script = DRAFT_CACHE[draft_id]
        target_track = None
        target_index = -1
        original = None
        for track in script.tracks.values():
            for index, segment in enumerate(track.segments):
                if segment.segment_id == request.segment_id:
                    target_track, target_index, original = track, index, segment
                    break
            if original is not None:
                break
        if target_track is None or original is None or not isinstance(original, draft.VideoSegment):
            raise ValueError("未找到指定的视频分镜片段")

        video_dir = os.path.join(config.DRAFT_DIR, draft_id, "assets", "videos")
        os.makedirs(video_dir, exist_ok=True)
        local_path = await __import__("asyncio").to_thread(
            download, url=request.video_url, save_dir=video_dir,
        )
        material = draft.VideoMaterial(local_path)
        source_duration = original.duration
        if request.source_start + source_duration > material.duration:
            raise ValueError(
                f"替换素材不足以覆盖原分镜时长: 需要 {source_duration}us，"
                f"可用 {max(0, material.duration - request.source_start)}us"
            )
        replacement = draft.VideoSegment(
            material=material,
            target_timerange=deepcopy(original.target_timerange),
            source_timerange=draft.trange(
                start=request.source_start, duration=source_duration,
            ),
            speed=1.0,
            volume=request.volume,
            clip_settings=deepcopy(original.clip_settings),
        )
        # Keep the stable segment id so callers can replace the same storyboard shot again.
        replacement.segment_id = original.segment_id
        replacement.effects = deepcopy(original.effects)
        replacement.filters = deepcopy(original.filters)
        replacement.mix_modes = deepcopy(original.mix_modes)
        replacement.mask = deepcopy(original.mask)
        replacement.transition = deepcopy(original.transition)
        replacement.background_filling = deepcopy(original.background_filling)
        replacement.fade = deepcopy(original.fade)
        replacement.animations_instance = deepcopy(original.animations_instance)
        target_track.segments[target_index] = replacement
        script.add_material(replacement.material_instance)
        script.save()
        logger.info(
            "draft segment replaced draft_id=%s segment_id=%s start=%s end=%s",
            draft_id, request.segment_id, original.start, original.end,
        )
        return {
            "draft_url": request.draft_url,
            "segment_id": replacement.segment_id,
            "track_id": target_track.track_id,
            "start": replacement.start,
            "end": replacement.end,
            "source_start": request.source_start,
            "video_url": request.video_url,
        }
    finally:
        await lock_manager.release_lock(draft_id)
