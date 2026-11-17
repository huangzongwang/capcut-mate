"""Offline checks for analysis-based material replacement drafts."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from . import manager


class ReplacementDraftTests(unittest.TestCase):
    def test_selected_material_replaces_only_requested_draft_segment(self):
        from types import SimpleNamespace
        from .schemas import ReplaceSelectedDraftMaterialRequest

        class FakeSegment:
            segment_id = "segment-3"
            duration = 3_000_000
            volume = 1.0

        script = SimpleNamespace(
            tracks={"main": SimpleNamespace(segments=[FakeSegment()])}, width=1080, height=1920,
        )
        report = {"step_3_shot_breakdown": [{
            "shot_index": 3, "start_us": 0, "end_us": 3_000_000,
            "scene_category": "pain_point", "content": "人物连续爬楼梯",
        }]}
        candidate = {
            "asset_id": 17, "temporal_feature_id": 31, "asset_name": "stairs.mp4",
            "usable_start_us": 4_000_000, "usable_end_us": 10_000_000,
            "clip_start_us": 5_000_000, "clip_end_us": 8_000_000,
        }
        request = ReplaceSelectedDraftMaterialRequest(
            draft_id="draft123", segment_id="segment-3", analysis_task_id="va_test",
            shot_index=3, asset_id=17, temporal_feature_id=31,
        )
        with patch.object(manager.draft, "VideoSegment", FakeSegment), \
             patch.dict(manager.DRAFT_CACHE, {"draft123": script}, clear=False), \
             patch.object(manager.analysis_task_store, "get_task", return_value={"status": "completed"}), \
             patch.object(manager, "_load_report", return_value=(report, {}, {})), \
             patch.object(manager, "_search_candidates", return_value=[candidate]) as search, \
             patch.object(manager, "_candidate_draft_url", return_value="http://media.example/stairs.mp4"), \
             patch.object(manager, "replace_draft_segment", new_callable=AsyncMock, return_value={
                 "draft_url": "http://draft.example/get_draft?draft_id=draft123",
                 "segment_id": "segment-3", "track_id": "track-main", "start": 0, "end": 3_000_000,
             }) as replace:
            result = asyncio.run(manager.replace_selected_draft_material(request))

        self.assertEqual(search.call_args.args[0]["end_us"], 3_000_000)
        self.assertEqual(search.call_args.args[1]["min_match_score"], 0)
        self.assertTrue(result["modified_in_place"])
        self.assertEqual(result["asset_id"], 17)
        self.assertEqual(result["source_start_us"], 5_000_000)
        self.assertEqual(result["volume"], 1.0)
        replace.assert_awaited_once()

    def test_selected_material_rejects_source_range_that_does_not_cover_target(self):
        from types import SimpleNamespace
        from .schemas import ReplaceSelectedDraftMaterialRequest

        class FakeSegment:
            segment_id = "segment-3"
            duration = 3_000_000
            volume = 1.0

        script = SimpleNamespace(
            tracks={"main": SimpleNamespace(segments=[FakeSegment()])}, width=1080, height=1920,
        )
        report = {"step_3_shot_breakdown": [{
            "shot_index": 3, "start_us": 0, "end_us": 3_000_000,
        }]}
        request = ReplaceSelectedDraftMaterialRequest(
            draft_id="draft123", segment_id="segment-3", analysis_task_id="va_test",
            shot_index=3, asset_id=17, temporal_feature_id=31, source_start_us=8_000_000,
        )
        candidate = {
            "asset_id": 17, "temporal_feature_id": 31,
            "usable_start_us": 4_000_000, "usable_end_us": 10_000_000,
            "clip_start_us": 5_000_000,
        }
        with patch.object(manager.draft, "VideoSegment", FakeSegment), \
             patch.dict(manager.DRAFT_CACHE, {"draft123": script}, clear=False), \
             patch.object(manager.analysis_task_store, "get_task", return_value={"status": "completed"}), \
             patch.object(manager, "_load_report", return_value=(report, {}, {})), \
             patch.object(manager, "_search_candidates", return_value=[candidate]), \
             patch.object(manager, "replace_draft_segment", new_callable=AsyncMock) as replace:
            with self.assertRaisesRegex(ValueError, "有效片段范围"):
                asyncio.run(manager.replace_selected_draft_material(request))
        replace.assert_not_awaited()

    def test_scene_material_candidates_keep_below_threshold_and_hide_loopback_urls(self):
        report = {"step_3_shot_breakdown": [{
            "shot_index": 3, "start_us": 0, "end_us": 3_000_000,
            "scene_category": "pain_point", "content": "人物连续爬楼梯",
        }]}
        candidates = [
            {
                "asset_id": 1, "temporal_feature_id": 11, "asset_name": "local.mp4",
                "access_url": "http://127.0.0.1:30001", "storage_path": "local.mp4",
                "usable_start_us": 0, "usable_end_us": 5_000_000,
                "match_score": 0.9, "match_reason": {},
            },
            {
                "asset_id": 2, "temporal_feature_id": 12, "asset_name": "stairs.mp4",
                "access_url": "http://192.168.0.191:30001", "storage_path": "stairs.mp4",
                "clip_start_us": 1_000_000, "clip_end_us": 4_000_000,
                "usable_start_us": 0, "usable_end_us": 5_000_000,
                "match_score": 0.55,
                "match_reason": {"semantic_similarity": 0.6, "visual_intent_score": 0.5},
            },
        ]
        with patch.object(manager.analysis_task_store, "get_task", return_value={"status": "completed"}), \
             patch.object(manager, "_load_report", return_value=(report, {}, {})), \
             patch.object(manager, "_search_candidates", return_value=candidates) as search:
            result = manager.scene_material_candidates("va_test", 3, limit=1, min_match_score=0.58)

        search_request = search.call_args.args[1]
        self.assertEqual(search_request["min_match_score"], 0)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["url_unavailable_count"], 1)
        self.assertEqual(result["candidates"][0]["asset_id"], 2)
        self.assertFalse(result["candidates"][0]["auto_selectable"])
        self.assertTrue(result["candidates"][0]["url"].startswith("http://"))

    def test_scene_material_candidates_explain_failed_analysis(self):
        with patch.object(manager.analysis_task_store, "get_task", return_value={
            "status": "failed", "error_message": "worker exited",
        }), patch.object(manager, "_load_report", side_effect=AssertionError):
            with self.assertRaisesRegex(ValueError, "worker exited"):
                manager.scene_material_candidates("va_failed", 1)

    def test_scene_material_candidates_reject_missing_scene(self):
        report = {"step_3_shot_breakdown": []}
        with patch.object(manager.analysis_task_store, "get_task", return_value={"status": "completed"}), \
             patch.object(manager, "_load_report", return_value=(report, {}, {})):
            with self.assertRaisesRegex(LookupError, "不存在分镜 7"):
                manager.scene_material_candidates("va_test", 7)

    def test_disabled_material_review_returns_candidates_without_model_lookup(self):
        candidates = [{"asset_id": 12}]
        with patch.dict(os.environ, {"VIDEO_ANALYSIS_REPLACEMENT_MATERIAL_REVIEW_ENABLED": "false"}), \
                patch.object(manager.material_review, "resolve_selected", side_effect=AssertionError), \
                patch.object(manager, "analysis_temporary_directory", side_effect=AssertionError):
            self.assertIs(
                manager._review_candidates("vrd_test", {}, candidates, {
                    "material_review_primary_model_id": 14,
                }),
                candidates,
            )

    def test_scene_rows_keep_valid_absolute_microsecond_boundaries(self):
        rows = manager._scene_rows({"step_3_shot_breakdown": [
            {"shot_index": 2, "start_us": 2_000_000, "end_us": 3_000_000},
            {"shot_index": 1, "start_us": 0, "end_us": 2_000_000},
            {"shot_index": 3, "start_us": 4, "end_us": 4},
            {"shot_index": 4, "start_us": -1, "end_us": 5},
        ]})
        self.assertEqual([row["shot_index"] for row in rows], [1, 2])
        self.assertEqual((rows[0]["start_us"], rows[0]["end_us"]), (0, 2_000_000))

    def test_duplicate_scene_indexes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "编号重复"):
            manager._scene_rows({"step_3_shot_breakdown": [
                {"shot_index": 1, "start_us": 0, "end_us": 1},
                {"shot_index": 1, "start_us": 1, "end_us": 2},
            ]})

    def test_overlapping_scene_ranges_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "互相重叠"):
            manager._scene_rows({"step_3_shot_breakdown": [
                {"shot_index": 1, "start_us": 0, "end_us": 10},
                {"shot_index": 2, "start_us": 9, "end_us": 20},
            ]})

    def test_materials_are_unique_across_all_variants_and_scene_ranges_are_preserved(self):
        rows = [
            {"shot_index": index, "start_us": (index - 1) * 2_000_000,
             "end_us": index * 2_000_000, "replaceable": True,
             "analysis_status": "completed", "scene_category": "pain_point"}
            for index in (1, 2)
        ]
        candidates = [{
            "asset_id": index, "sha256": f"sha-{index}", "match_score": 0.9,
            "clip_start_us": 100, "clip_end_us": 2_000_100,
            "source_duration_us": 2_000_000,
        } for index in range(1, 5)]
        with patch.object(manager.media_library, "ensure_usage_tables"), \
             patch.object(manager, "_search_text_candidates", return_value=candidates), \
             patch.object(manager, "_review_candidates", side_effect=lambda _task, _row, items, _req: items):
            selected, _ = manager._select_for_versions("vrd_test", rows, 2, {"width": 1080, "height": 1920})
        identities = [manager._identity(item) for version in selected for item in version.values()]
        self.assertEqual(len(identities), 4)
        self.assertEqual(len(set(identities)), 4)
        self.assertEqual(selected[0][2]["target_start_us"], 2_000_000)
        self.assertEqual(selected[1][2]["target_end_us"], 4_000_000)

    def test_database_candidates_match_only_semantics_and_visual_intent(self):
        target = {
            "required_elements": ["托举药品包装盒", "指示棒点触品牌区域"],
            "duration_us": 2_000_000,
        }
        weak = {
            "asset_id": 1, "match_score": 0.4931, "motion_intensity": 0.0,
            "usable_start_us": 0, "usable_end_us": 10_000_000,
            "match_reason": {"semantic_similarity": 0.2327, "visual_intent_score": 1.0,
                             "required_element_coverage": 1 / 3},
        }
        valid = {
            "asset_id": 2, "match_score": 0.80, "motion_intensity": 0.04,
            "usable_start_us": 1_000_000, "usable_end_us": 11_000_000,
            "match_reason": {"semantic_similarity": 0.16, "visual_intent_score": 0.8,
                             "required_element_coverage": 1.0},
        }

        result = manager._filter_and_rank_candidates([weak, valid], target, {"min_match_score": 0.58})

        self.assertEqual([item["asset_id"] for item in result], [1])
        self.assertEqual(result[0]["match_score"], 0.6163)
        self.assertEqual((result[0]["clip_start_us"], result[0]["clip_end_us"]),
                         (0, 2_000_000))
        self.assertEqual(
            result[0]["match_reason"]["replacement_quality"]["selection_mode"],
            "database_strict_start",
        )

    def test_database_candidates_keep_action_event_anchor(self):
        candidate = {
            "asset_id": 2, "match_score": 0.80, "motion_intensity": 0.04,
            "usable_start_us": 1_000_000, "usable_end_us": 11_000_000,
            "clip_start_us": 4_000_000, "clip_end_us": 6_000_000,
            "action_event_match": {"start_us": 4_000_000, "match_score": 0.9},
            "match_reason": {"semantic_similarity": 0.16, "visual_intent_score": 1.0,
                             "required_element_coverage": 1.0,
                             "action_event": {"start_us": 4_000_000}},
        }
        result = manager._filter_and_rank_candidates(
            [candidate], {"required_elements": ["爬楼梯"], "duration_us": 2_000_000},
            {"min_match_score": 0.58},
        )
        self.assertEqual(result[0]["clip_start_us"], 4_000_000)
        self.assertEqual(
            result[0]["match_reason"]["replacement_quality"]["selection_mode"],
            "database_action_event",
        )

    def test_high_confidence_action_event_can_replace_generic_motion_floor(self):
        candidate = {
            "asset_id": 3, "match_score": 0.80, "motion_intensity": 0.0,
            "usable_start_us": 0, "usable_end_us": 5_000_000,
            "clip_start_us": 1_000_000, "clip_end_us": 3_000_000,
            "action_event_match": {"start_us": 1_000_000, "match_score": 0.9},
            "match_reason": {"semantic_similarity": 0.2, "visual_intent_score": 1.0,
                             "required_element_coverage": 1.0,
                             "action_event": {"start_us": 1_000_000}},
        }
        result = manager._filter_and_rank_candidates(
            [candidate], {"required_elements": ["手指指向标识"], "duration_us": 2_000_000},
            {"min_match_score": 0.58},
        )
        self.assertEqual(len(result), 1)

    def test_qr_traceability_target_only_requires_visible_qr(self):
        target = manager._match_target({
            "shot_index": 1, "start_us": 0, "end_us": 2_000_000,
            "scene_category": "product_empty_scene", "visual_intent": "qr_traceability",
            "content": "直接扫码查看包装溯源码", "action": ["拿起手机", "扫码"],
        }, {"width": 1080, "height": 1920})
        self.assertEqual(target["visual_intent"], "qr_code_visible")
        self.assertEqual(target["required_elements"], ["二维码或条形码"])

    def test_barcode_storyboard_target_uses_shared_visible_code_intent(self):
        target = manager._match_target({
            "shot_index": 2, "start_us": 0, "end_us": 2_000_000,
            "scene_category": "product_empty_scene", "visual_intent": "qr_traceability",
            "content": "扫描盒身条码查看溯源信息", "action": ["扫描条码"],
        }, {"width": 1080, "height": 1920})
        self.assertEqual(target["visual_intent"], "qr_code_visible")
        self.assertEqual(target["required_elements"], ["二维码或条形码"])

    def test_required_actions_do_not_fall_back_to_unconstrained_search(self):
        row = {
            "shot_index": 1, "start_us": 0, "end_us": 2_000_000,
            "scene_category": "product_empty_scene", "visual_intent": "product_detail",
            "content": "手持产品并用指示棒点触品牌区域",
            "action": ["手持产品", "指示棒点触品牌区域"],
        }
        with patch.object(manager, "_search_candidates", return_value=[]), \
             patch.object(manager.media_library, "search_clips") as fallback:
            result = manager._search_text_candidates(row, {"width": 1080, "height": 1920})
        self.assertEqual(result, [])
        fallback.assert_not_called()

    def test_search_candidates_enables_semantic_visual_matching_mode(self):
        row = {
            "shot_index": 1, "start_us": 0, "end_us": 2_000_000,
            "scene_category": "product_empty_scene", "visual_intent": "product_detail",
            "content": "展示产品包装细节", "action": ["手持产品"],
        }
        request = {"width": 1080, "height": 1920}
        with patch.object(manager.media_library, "search_clips", return_value=[]) as search, \
             patch.object(manager, "_filter_and_rank_candidates", return_value=[]):
            manager._search_candidates(row, request)
        self.assertTrue(search.call_args.kwargs["semantic_visual_only"])

    def test_semantic_visual_score_is_an_equal_weight_average(self):
        score = manager.media_library._candidate_match_score(
            similarity=0.2327, element_coverage=0.0,
            product_identity_match=False, usage_role="product_empty_scene",
            visual_intent_score=1.0, semantic_visual_only=True,
        )
        self.assertEqual(score, 0.6163)

    def test_otc_action_description_counts_as_otc_visual_evidence(self):
        self.assertTrue(manager.media_library._explicit_visual_intent_matches(
            "otc_certification", "白绿色药盒，OTC文字清晰，存在OTC动作并持续强调标识", [],
        ))

    def test_candidate_duration_uses_temporal_boundaries_not_summary_field(self):
        candidate = {
            "usable_start_us": 1_000_000,
            "usable_end_us": 4_000_000,
        }
        self.assertTrue(manager.media_library._fit_clip_range(candidate, 3_000_000))
        self.assertEqual(candidate["clip_end_us"], 4_000_000)

    def test_draft_replaces_main_track_segment_and_keeps_source_audio_separate(self):
        video_calls = []
        audio_calls = []
        caption_calls = []
        caption_kwargs = []

        async def add_videos(_draft_url, video_json):
            video_calls.append(json.loads(video_json))
            return "", "video-track", [], ["video-segment"], []

        async def add_audio(_draft_url, audio_json):
            audio_calls.append(json.loads(audio_json))
            return "", "audio-track", ["audio-segment"]

        async def add_captions(_draft_url, captions_json, **_kwargs):
            caption_calls.append(json.loads(captions_json))
            caption_kwargs.append(_kwargs)
            return "", "caption-track"

        source = Path("source.mp4")
        clip = {
            "rendered_video_url": "https://media.example/replacement.mp4",
            "rendered_width": 1080, "rendered_height": 1920,
            "clip_start_us": 500_000, "clip_end_us": 2_500_000,
            "asset_id": 17, "temporal_feature_id": 31,
        }
        with patch.object(manager, "create_draft", return_value="http://draft?draft_id=d1"), \
             patch.object(manager, "add_videos_async", new=AsyncMock(side_effect=add_videos)), \
             patch.object(manager, "add_audios_async", new=AsyncMock(side_effect=add_audio)), \
             patch.object(manager, "add_captions_async", new=AsyncMock(side_effect=add_captions)):
            result = manager._build_draft(
                "vrd_test", "https://media.example/source.mp4", source, {},
                {
                    1: {"shot_index": 1, "start_us": 0, "end_us": 3_000_000},
                    2: {"shot_index": 2, "start_us": 3_000_000, "end_us": 5_000_000,
                        "dialogue": "保留这句口播", "captions": [
                            {"start_us": 3_400_000, "end_us": 4_200_000, "text": "保留这句口播"},
                        ]},
                    3: {"shot_index": 3, "start_us": 5_000_000, "end_us": 8_000_000},
                },
                {2: clip}, {"width": 1080, "height": 1920, "keep_original_audio": True,
                            "preserve_dialogue_captions": True, "preserve_on_screen_text": True},
                8_000_000,
                source_segments={
                    "scene:1": {"video_url": "https://media.example/source-1.mp4", "width": 1080, "height": 1920},
                    "scene:3": {"video_url": "https://media.example/source-3.mp4", "width": 1080, "height": 1920},
                },
                source_audio_url="https://media.example/source-audio.m4a",
                overlay_events=[{
                    "scene_index": 2, "start_us": 3_400_000, "end_us": 4_500_000,
                    "text": "保留这句口播", "confidence": 0.98,
                    "box": [0.4, 0.6, 0.6, 0.7],
                }],
            )
        self.assertEqual(len(video_calls), 1)
        self.assertEqual([item["video_url"] for item in video_calls[0]], [
            "https://media.example/source-1.mp4",
            "https://media.example/replacement.mp4",
            "https://media.example/source-3.mp4",
        ])
        self.assertEqual([(item["start"], item["end"], item["volume"]) for item in video_calls[0]], [
            (0, 3_000_000, 0.0), (3_000_000, 5_000_000, 0.0),
            (5_000_000, 8_000_000, 0.0),
        ])
        self.assertEqual(audio_calls[0][0]["audio_url"], "https://media.example/source-audio.m4a")
        self.assertEqual((audio_calls[0][0]["start"], audio_calls[0][0]["end"], audio_calls[0][0]["volume"]),
                         (0, 8_000_000, 1.0))
        self.assertEqual(result["media_mode"], "edited_main_track_with_separate_original_audio")
        self.assertEqual(result["replaced_scene_indexes"], [2])
        self.assertEqual(caption_calls[0], [
            {"start": 3_400_000, "end": 4_500_000, "text": "保留这句口播"},
        ])
        self.assertEqual(len(caption_calls), 1)
        self.assertEqual(result["preserved_dialogue_caption_count"], 1)
        self.assertEqual(result["ocr_adjusted_caption_count"], 0)
        self.assertEqual(result["ocr_timing_adjusted_caption_count"], 1)
        self.assertEqual(result["ocr_positioned_caption_count"], 1)
        segments = result["detail"]["segments"]
        self.assertEqual([item["type"] for item in segments], ["original", "replacement", "original"])
        self.assertEqual(segments[0]["video_url"], "https://media.example/source-1.mp4")
        self.assertEqual(segments[1]["video_url"], "https://media.example/replacement.mp4")
        self.assertEqual(segments[1]["replacement"]["source_start_us"], 500_000)
        self.assertEqual(segments[1]["replacement"]["source_end_us"], 2_500_000)
        self.assertEqual(segments[1]["captions"], [{
            "start_us": 3_400_000, "end_us": 4_500_000,
            "text": "保留这句口播", "included_in_draft": True,
        }])
        self.assertEqual(segments[2]["video_url"], "https://media.example/source-3.mp4")
        self.assertEqual(result["ocr_added_caption_count"], 0)
        self.assertEqual(result["preserved_on_screen_text_count"], 0)
        self.assertEqual(caption_kwargs[0]["transform_x"], 0)
        self.assertEqual(caption_kwargs[0]["transform_y"], -288)

    def test_source_asset_probe_keeps_remote_url_unmodified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            probe_inputs = []
            ffmpeg_commands = []

            def probe(value):
                probe_inputs.append(value)
                return (1080, 1920, 4_000_000) if len(probe_inputs) == 1 else (1080, 1920, 4_000_000)

            with patch.dict("os.environ", {"SCRIPT_VIDEO_MEDIA_URL_BASE": "http://192.168.0.191:30001/media"}), \
                 patch.object(manager, "media_root", return_value=root), \
                 patch.object(manager, "media_tool", return_value="ffmpeg"), \
                 patch.object(manager, "_probe", side_effect=probe), \
                 patch.object(manager.subprocess, "run", side_effect=lambda command, **kwargs: ffmpeg_commands.append(command)):
                assets, audio_url, duration, _, warnings = manager._render_source_assets(
                    "vrd_test", "https://media.example/source.mp4", None,
                    {1: {"shot_index": 1, "start_us": 0, "end_us": 4_000_000}},
                    4_000_000, keep_audio=False,
                )
        self.assertEqual(probe_inputs[0], "https://media.example/source.mp4")
        self.assertIn("https://media.example/source.mp4", ffmpeg_commands[0])
        self.assertIn("scene:1", assets)
        self.assertIsNone(audio_url)
        self.assertEqual(duration, 4_000_000)
        self.assertEqual(warnings, [])

    def test_media_base_prefers_lan_address_over_loopback_internal_url(self):
        with patch.dict("os.environ", {
            "SCRIPT_VIDEO_MEDIA_URL_BASE": "http://192.168.0.191:30001/media",
            "DRAFT_MEDIA_BASE_URL": "http://127.0.0.1:30001/media",
        }):
            self.assertEqual(manager._media_base_url(), "http://192.168.0.191:30001/media")

    def test_source_asset_render_uses_report_duration_not_longer_container_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            ffmpeg_commands = []

            def probe(_value):
                if not ffmpeg_commands:
                    return 1080, 1920, 4_200_000
                return 1080, 1920, 4_000_000

            with patch.dict("os.environ", {"SCRIPT_VIDEO_MEDIA_URL_BASE": "http://192.168.0.191:30001/media"}), \
                 patch.object(manager, "media_root", return_value=root), \
                 patch.object(manager, "media_tool", return_value="ffmpeg"), \
                 patch.object(manager, "_probe", side_effect=probe), \
                 patch.object(manager.subprocess, "run", side_effect=lambda command, **kwargs: ffmpeg_commands.append(command)):
                assets, _, duration, _, _ = manager._render_source_assets(
                    "vrd_test", "https://media.example/source.mp4", None,
                    {1: {"shot_index": 1, "start_us": 0, "end_us": 4_000_000}},
                    4_000_000, keep_audio=False,
                )

        self.assertEqual(duration, 4_000_000)
        self.assertEqual(set(assets), {"scene:1"})
        self.assertEqual(len(ffmpeg_commands), 1)
        self.assertTrue(ffmpeg_commands[0][-1].endswith("scene-001.mp4"))

    def test_speech_captions_are_limited_to_replaced_scenes_and_clipped_to_scene(self):
        captions = manager._speech_captions_for_scenes({
            1: {"start_us": 1_000_000, "end_us": 3_000_000, "captions": [
                {"start_us": 800_000, "end_us": 1_500_000, "text": "甲"},
                {"start_us": 2_500_000, "end_us": 3_500_000, "text": "乙"},
            ]},
            2: {"start_us": 3_000_000, "end_us": 4_000_000,
                "captions": [{"start_us": 3_100_000, "end_us": 3_500_000, "text": "丙"}]},
        }, {1: {}})
        self.assertEqual(captions, [
            {"start": 1_000_000, "end": 1_500_000, "text": "甲"},
            {"start": 2_500_000, "end": 3_000_000, "text": "乙"},
        ])

    def test_ocr_corrects_matching_speech_without_changing_timestamps(self):
        speech = [{"start": 500_000, "end": 1_500_000, "text": "正品赛乐赛奥利斯他"}]
        adjusted, count = manager._adjust_speech_captions_with_ocr(
            {1: {"start_us": 0, "end_us": 2_000_000}}, {1: {}}, speech,
            [{
                "scene_index": 1, "start_us": 600_000, "end_us": 1_400_000,
                "text": "正品赛乐赛奥利司他", "confidence": 0.96,
            }],
        )

        self.assertEqual(adjusted, [
            {"start": 500_000, "end": 1_500_000, "text": "正品赛乐赛奥利司他"},
        ])
        self.assertEqual(count, 1)
        self.assertEqual(speech[0]["text"], "正品赛乐赛奥利斯他")

    def test_ocr_corrects_low_confidence_asr_from_large_subtitle_not_packaging_text(self):
        speech = [{"start": 20_440_000, "end": 21_260_000, "text": "在罗塞奥利斯他"}]
        row = {
            "start_us": 20_440_000, "end_us": 21_800_000,
            "captions": [{
                "start_us": 20_440_000, "end_us": 21_260_000,
                "text": "在罗塞奥利斯他",
                "words": [{"probability": value} for value in (0.29, 0.51, 0.82, 0.92, 0.56, 0.99, 0.53)],
            }],
        }
        adjusted, count = manager._adjust_speech_captions_with_ocr(
            {3: row}, {3: {}}, speech,
            [
                {
                    "scene_index": 3, "start_us": 20_440_000, "end_us": 21_690_000,
                    "text": "赛乐赛奥利司他胶囊", "confidence": 0.99,
                    "box": [0.70, 0.849, 0.89, 0.863],
                },
                {
                    "scene_index": 3, "start_us": 20_690_000, "end_us": 21_690_000,
                    "text": "赛乐赛奥利司他", "confidence": 0.98,
                    "box": [0.29, 0.688, 0.71, 0.735],
                },
            ],
        )

        self.assertEqual(adjusted, [
            {
                "start": 20_440_000, "end": 21_690_000, "text": "赛乐赛奥利司他",
                "_ocr_box": [0.29, 0.688, 0.71, 0.735],
                "_ocr_timing_adjusted": True,
            },
        ])
        self.assertEqual(count, 1)

    def test_caption_group_uses_ocr_position_and_strips_internal_metadata(self):
        groups = manager._group_speech_captions([
            {
                "start": 1_000_000, "end": 2_000_000, "text": "识别字幕",
                "_ocr_box": [0.25, 0.65, 0.75, 0.75],
                "_ocr_timing_adjusted": True,
            },
        ], 1080, 1920)

        self.assertEqual(groups, [{
            "items": [{"start": 1_000_000, "end": 2_000_000, "text": "识别字幕"}],
            "transform_x": 0,
            "transform_y": -384,
        }])

    def test_caption_without_ocr_uses_lower_position_and_correct_y_direction(self):
        groups = manager._group_speech_captions([
            {"start": 1_000_000, "end": 2_000_000, "text": "默认位置"},
        ], 1080, 1920)

        self.assertEqual(groups[0]["transform_y"], -960)

    def test_ocr_ignores_unrelated_or_non_overlapping_visual_text(self):
        speech = [{"start": 500_000, "end": 1_500_000, "text": "正品赛乐赛奥利司他"}]
        adjusted, count = manager._adjust_speech_captions_with_ocr(
            {1: {"start_us": 0, "end_us": 3_000_000}}, {1: {}}, speech,
            [
                {
                    "scene_index": 1, "start_us": 600_000, "end_us": 1_400_000,
                    "text": "红标OTC 一盒一码", "confidence": 0.99,
                },
                {
                    "scene_index": 1, "start_us": 2_000_000, "end_us": 2_800_000,
                    "text": "正品赛乐赛奥利斯他", "confidence": 0.99,
                },
            ],
        )

        self.assertEqual(adjusted, speech)
        self.assertEqual(count, 0)

    def test_ocr_never_creates_a_caption_without_asr_timing(self):
        adjusted, count = manager._adjust_speech_captions_with_ocr(
            {1: {"start_us": 0, "end_us": 2_000_000, "dialogue": "正品赛乐赛奥利司他"}},
            {1: {}}, [],
            [{
                "scene_index": 1, "start_us": 500_000, "end_us": 1_500_000,
                "text": "正品赛乐赛奥利司他", "confidence": 0.99,
            }],
        )

        self.assertEqual(adjusted, [])
        self.assertEqual(count, 0)

    def test_ocr_failure_is_a_warning_not_a_task_failure(self):
        with patch.object(manager, "analysis_temporary_directory",
                          side_effect=lambda _prefix: tempfile.TemporaryDirectory()), \
             patch("src.service.video_analysis.feature_extraction._run_worker",
                   side_effect=RuntimeError("OCR runtime unavailable")):
            events, warnings = manager._extract_overlay_text(
                "vrd_test", "https://media.example/source.mp4", None,
                {2: {"shot_index": 2, "start_us": 1_000_000, "end_us": 2_000_000}},
                [{2: {"asset_id": 9}}], {"preserve_on_screen_text": True},
            )
        self.assertEqual(events, [])
        self.assertIn("OCR runtime unavailable", warnings[0])

    def test_run_passes_rendered_source_assets_and_ocr_into_version_builder(self):
        report = {"detection": {"duration_us": 2_000_000}, "step_3_shot_breakdown": [
            {"shot_index": 1, "start_us": 0, "end_us": 2_000_000,
             "replaceable": True, "analysis_status": "completed", "scene_category": "pain_point"},
        ]}
        task = {"source_analysis_task_id": "va_source", "request": {
            "count": 1, "width": 1080, "height": 1920, "keep_original_audio": True,
        }}
        captured = {}

        def create_versions(_task_id, versions, selected, row_map, source_url, source_path,
                            request, reasons, source_duration, source_segments,
                            source_audio_url, source_dimensions, overlay_events, overlay_warnings):
            captured.update({
                "selected": selected, "row_map": row_map, "source_url": source_url,
                "source_duration": source_duration, "source_segments": source_segments,
                "source_audio_url": source_audio_url, "source_dimensions": source_dimensions,
                "overlay_events": overlay_events, "overlay_warnings": overlay_warnings,
            })
            versions[0]["status"] = "completed"

        with patch.object(manager.store, "claim", return_value=True), \
             patch.object(manager.store, "get", return_value=task), \
             patch.object(manager.store, "update"), \
             patch.object(manager, "_load_report", return_value=(report, {"video_url": "https://media.example/source.mp4"}, {})), \
             patch.object(manager, "_select_for_versions", return_value=([{1: {}}], {})), \
             patch.object(manager, "_source_for_task", return_value=("https://media.example/source.mp4", None)), \
             patch.object(manager, "_render_source_assets", return_value=(
                 {"scene:1": {"video_url": "https://media.example/scene.mp4"}},
                 "https://media.example/audio.m4a", 2_000_000, (1080, 1920), ["source warning"],
             )), \
             patch.object(manager, "_extract_overlay_text", return_value=([{"scene_index": 1}], ["ocr warning"])), \
             patch.object(manager, "_create_versions", side_effect=create_versions):
            manager._run("vrd_test")

        self.assertEqual(captured["source_duration"], 2_000_000)
        self.assertEqual(captured["source_segments"], {"scene:1": {"video_url": "https://media.example/scene.mp4"}})
        self.assertEqual(captured["source_audio_url"], "https://media.example/audio.m4a")
        self.assertEqual(captured["overlay_events"], [{"scene_index": 1}])
        self.assertEqual(captured["overlay_warnings"], ["source warning", "ocr warning"])

    def test_run_skips_all_versions_without_creating_empty_drafts(self):
        report = {"detection": {"duration_us": 2_000_000}, "step_3_shot_breakdown": [
            {"shot_index": 1, "start_us": 0, "end_us": 2_000_000,
             "replaceable": True, "analysis_status": "completed", "scene_category": "pain_point"},
        ]}
        task = {"source_analysis_task_id": "va_source", "request": {
            "count": 2, "width": 1080, "height": 1920,
        }}
        updates = []

        with patch.object(manager.store, "claim", return_value=True), \
             patch.object(manager.store, "get", return_value=task), \
             patch.object(manager.store, "update", side_effect=lambda _task_id, **values: updates.append(values)), \
             patch.object(manager, "_load_report", return_value=(report, {}, {})), \
             patch.object(manager, "_select_for_versions", return_value=([{}, {}], {})), \
             patch.object(manager, "_source_for_task") as source_for_task, \
             patch.object(manager, "_render_source_assets") as render_source:
            manager._run("vrd_test")

        source_for_task.assert_not_called()
        render_source.assert_not_called()
        final = updates[-1]
        self.assertEqual(final["status"], "completed_with_warnings")
        self.assertEqual([item["status"] for item in final["versions"]], ["skipped", "skipped"])
        self.assertTrue(all(item["draft_id"] is None for item in final["versions"]))
        self.assertIn("未创建草稿", final["error_message"])

    def test_create_versions_skips_only_unmatched_variants(self):
        versions = [
            {"version": 1, "status": "queued", "draft_id": None},
            {"version": 2, "status": "queued", "draft_id": None},
        ]
        with patch.object(manager.store, "update"), \
             patch.object(manager.media_library, "record_reservation"), \
             patch.object(manager.media_library, "finish_reservation"), \
             patch.object(manager, "_render_replacement_media", return_value={}), \
             patch.object(manager, "_build_draft", return_value={
                 "status": "completed", "draft_id": "20261112123456deadbeef", "draft_url": "http://draft",
             }):
            manager._create_versions(
                "vrd_test", versions, [{}, {1: {"scene_index": 1}}], {},
                "https://media.example/source.mp4", None,
                {"width": 1080, "height": 1920, "keep_original_audio": True}, {},
                1_000_000, {}, None, (1080, 1920), [], [],
            )

        self.assertEqual([item["status"] for item in versions], ["skipped", "completed"])
        self.assertIsNone(versions[0]["draft_id"])
        self.assertEqual(versions[0]["replacement_count"], 0)
        self.assertEqual(versions[1]["draft_id"], "20261112123456deadbeef")

    def test_delete_version_removes_only_its_draft_and_media(self):
        task_id = "vrd_" + "a" * 32
        draft_id = "20261112123456deadbeef"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            draft_root = root / "drafts"
            draft_path = draft_root / draft_id
            draft_path.mkdir(parents=True)
            (draft_path / "draft_content.json").touch()
            media_root = root / "media"
            task_media = media_root / "replacement-drafts" / task_id
            version_media = task_media / "version-02"
            version_media.mkdir(parents=True)
            (version_media / "scene-001.mp4").touch()
            source_media = task_media / "source"
            source_media.mkdir()
            (source_media / "scene-001.mp4").touch()
            task = {
                "task_id": task_id, "source_analysis_task_id": "va_source",
                "status": "completed", "stage": "completed", "progress": 100,
                "request": {"count": 2}, "created_at": "now", "versions": [
                    {"version": 2, "status": "completed", "draft_id": draft_id,
                     "draft_url": "http://draft", "preview_url": "http://draft",
                     "detail": {"segments": []}, "materials": [{"asset_id": 1}]},
                ],
            }

            def update(_task_id, **values):
                task.update(values)
                return task

            cache = manager.DRAFT_CACHE
            cache[draft_id] = object()
            try:
                with patch.object(manager.store, "get_local", return_value=task), \
                     patch.object(manager.store, "update", side_effect=update), \
                     patch.object(manager.config, "DRAFT_DIR", str(draft_root)), \
                     patch.object(manager, "media_root", return_value=media_root):
                    result = manager.delete_version(task_id, 2)
            finally:
                cache.pop(draft_id, None)

            self.assertFalse(draft_path.exists())
            self.assertFalse(version_media.exists())
            self.assertTrue((source_media / "scene-001.mp4").exists())
            self.assertEqual(result["versions"][0]["status"], "deleted")
            self.assertIsNone(result["versions"][0]["draft_url"])
            self.assertIsNone(result["versions"][0]["detail"])

    def test_encode_replacement_clip_is_portrait_and_muted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.touch()
            output_root = root / "media" / "replacement-drafts" / "vrd_test" / "version-01"
            output_root.mkdir(parents=True)
            ffmpeg = []
            with (
                patch.object(manager, "_probe", side_effect=[
                    (1920, 1080, 3_000_000),
                    (1080, 1920, 2_000_000),
                ]),
                patch.object(
                    manager.subprocess, "run",
                    side_effect=lambda command, **kwargs: ffmpeg.append(command),
                ),
            ):
                result = manager._encode_replacement_clip(
                    "vrd_test", 1, 2,
                    {
                        "asset_id": 7,
                        "clip_start_us": 500_000,
                        "vertical_fit": "contain_blur",
                    },
                    source, output_root, (root / "media").resolve(),
                    "http://media.test/media", 2_000_000,
                    {"width": 1080, "height": 1920},
                )
            self.assertEqual(result["rendered_width"], 1080)
            self.assertEqual(result["rendered_height"], 1920)
            self.assertEqual(result["rendered_fit_mode"], "contain_blur")
            command = ffmpeg[0]
            self.assertIn("-an", command)
            self.assertIn("-ss", command)
            self.assertIn("0.500000", command)
            self.assertIn("-filter_complex", command)
            self.assertTrue(result["rendered_video_url"].endswith("scene-002-asset-7.mp4"))

    def test_router_exposes_submit_and_query_endpoints(self):
        from .router import router

        paths = {route.path for route in router.routes}
        self.assertIn("/analyze_video_tasks/{analysis_task_id}/replacement_draft_tasks", paths)
        self.assertIn("/replacement_draft_tasks/{task_id}", paths)
        self.assertIn("/replacement_draft_tasks/{task_id}/versions/{version}", paths)
        self.assertIn("/replacement_drafts/replace_material", paths)

    def test_task_level_failure_marks_unstarted_versions_failed(self):
        task = {
            "source_analysis_task_id": "va_source",
            "request": {"count": 2, "width": 1080, "height": 1920},
        }
        versions = []

        def capture_update(_task_id, **kwargs):
            versions[:] = kwargs.get("versions", versions)

        with patch.object(manager.store, "claim", return_value=True), \
             patch.object(manager.store, "get", return_value=task), \
             patch.object(manager.store, "update", side_effect=capture_update), \
             patch.object(manager, "_load_report", side_effect=RuntimeError("gap export failed")):
            manager._run("vrd_test")

        self.assertEqual([version["status"] for version in versions], ["failed", "failed"])
        self.assertTrue(all(version["error_message"] == "gap export failed" for version in versions))


if __name__ == "__main__":
    unittest.main()
