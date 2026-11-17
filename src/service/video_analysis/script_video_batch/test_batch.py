"""Offline checks for script batch matching/allocation; no API or provider calls."""
from __future__ import annotations

import asyncio
import inspect
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from . import manager
from . import gemini_client, material_review, media_library, music_library, vertical_renderer
from .schemas import CreateBatchRequest


class BatchTests(unittest.TestCase):
    storyboard_id = "sb_" + "a" * 32

    def test_paths_reject_absolute_and_traversal(self):
        for value in ("../secret.mp4", "C:/secret.mp4", "/tmp/video.mp4", "uploads/../x.mp4"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CreateBatchRequest(storyboard_id=self.storyboard_id, source_video_path=value)

    def test_request_restricts_batch_to_twenty(self):
        with self.assertRaises(ValueError):
            CreateBatchRequest(storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4", count=21)

    def test_background_music_defaults_to_enabled_but_muted(self):
        request = CreateBatchRequest(
            storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4",
        )
        self.assertTrue(request.background_music_enabled)
        self.assertEqual(request.background_music_volume, 0.0)

    def test_music_ranking_uses_script_semantics_and_rejects_vocals(self):
        shots = [SimpleNamespace(visual_intent="otc_certification")]
        rows = [
            {"id": 1, "suggested_title": "明亮科技感电子纯音乐", "duration": 60,
             "oss_url": "https://media/tech.mp3", "has_vocals": 0,
             "moods": '["明亮", "专业"]'},
            {"id": 2, "suggested_title": "温柔人声歌曲", "duration": 60,
             "oss_url": "https://media/vocal.mp3", "has_vocals": 1,
             "moods": '["明亮"]'},
            {"id": 3, "suggested_title": "紧张悬疑纯音乐", "duration": 60,
             "oss_url": "https://media/serious.mp3", "has_vocals": 0},
        ]
        ranked = music_library.rank_rows(rows, "正品有红标OTC标识", shots)
        self.assertEqual(ranked[0]["id"], 1)
        self.assertNotIn(2, [item["id"] for item in ranked])

    def test_request_accepts_distinct_primary_and_backup_review_models(self):
        request = CreateBatchRequest(
            storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4",
            material_review_primary_model_id=14,
            material_review_backup_model_id=18,
        )
        self.assertEqual(request.material_review_primary_model_id, 14)
        self.assertEqual(request.material_review_backup_model_id, 18)

    def test_request_rejects_backup_without_primary_or_duplicate_models(self):
        with self.assertRaisesRegex(ValueError, "主审核模型"):
            CreateBatchRequest(
                storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4",
                material_review_backup_model_id=18,
            )
        with self.assertRaisesRegex(ValueError, "不能相同"):
            CreateBatchRequest(
                storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4",
                material_review_primary_model_id=14,
                material_review_backup_model_id=14,
            )

    def test_material_review_rejects_invalid_max_output_tokens(self):
        with self.assertRaisesRegex(material_review.MaterialReviewError, "max_output_tokens"):
            material_review._request_options({
                "metadata": {"material_review_max_output_tokens": "invalid"},
            })

    def test_gemini_material_review_adapter_is_selected_by_provider_or_protocol(self):
        self.assertEqual(material_review._adapter({
            "provider": "zetarouter_gemini", "model": "gemini-2.5-flash-lite",
            "base_url": "https://api.example.test", "metadata": {},
        }), "gemini_video")
        self.assertEqual(material_review._adapter({
            "provider": "custom", "model": "custom-video",
            "base_url": "https://api.example.test", "metadata": {"protocol": "gemini"},
        }), "gemini_video")
        self.assertEqual(material_review._adapter({
            "provider": "zetarouter_gemini", "model": "gemini-2.5-flash-lite",
            "base_url": "https://api.example.test",
            "metadata": {"material_review_input_mode": "multi_frame"},
        }), "multi_frame")

    def test_gemini_transport_sends_model_from_selected_config_and_inline_video(self):
        with TemporaryDirectory() as directory:
            video = Path(directory) / "candidate.mp4"
            video.write_bytes(b"video")
            response = SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "candidates": [{"content": {"parts": [{
                        "text": "```json\n{\"candidates\": [{\"candidate_id\": \"A\"}]}\n```",
                    }]}}],
                    "usageMetadata": {"totalTokenCount": 10},
                    "responseId": "gemini-response",
                },
            )
            config = {
                "id": 31, "provider_id": 10, "provider": "zetarouter_gemini",
                "model": "gemini-2.5-flash-lite", "base_url": "https://api.example.test",
                "api_key": "secret", "request_timeout_seconds": 60, "metadata": {},
            }
            with patch.object(gemini_client.requests, "post", return_value=response) as post:
                result, calls = gemini_client.generate_json(
                    config=config, instruction="review", videos=[("A", video)],
                    max_output_tokens=100, task_id="task-a",
                )
        self.assertEqual(result["candidates"][0]["candidate_id"], "A")
        self.assertEqual(calls[0]["model_id"], 31)
        self.assertIn("gemini-2.5-flash-lite:generateContent", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "secret")
        parts = post.call_args.kwargs["json"]["contents"][0]["parts"]
        self.assertEqual(parts[-1]["inlineData"]["mimeType"], "video/mp4")
        self.assertEqual(post.call_args.kwargs["json"]["generationConfig"]["maxOutputTokens"], 100)

    def test_request_uses_existing_storyboard_only(self):
        request = CreateBatchRequest(storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4")
        self.assertEqual(request.storyboard_id, self.storyboard_id)
        for field, value in (
            ("script", "口播文本"), ("model_id", 13),
            ("product_names", ["商品"]), ("product_reference", "资料"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                CreateBatchRequest(
                    storyboard_id=self.storyboard_id,
                    source_video_path="uploads/a.mp4",
                    **{field: value},
                )

    def test_request_uses_reference_subtitle_style_by_default(self):
        request = CreateBatchRequest(storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4")
        self.assertEqual(request.subtitle_font, "点宋体")
        self.assertEqual(request.subtitle_font_size, 12)
        self.assertEqual(request.subtitle_text_color, "#ffde00")
        self.assertFalse(request.subtitle_bold)
        self.assertFalse(request.subtitle_underline)
        self.assertFalse(request.subtitle_italic)
        self.assertIsNone(request.subtitle_transform_y)
        self.assertTrue(request.subtitle_remove_punctuation)
        self.assertEqual(request.subtitle_in_animation, "打字机 II")
        self.assertFalse(request.split_screen_enabled)
        self.assertEqual(request.split_fit_mode, "contain_blur")
        self.assertEqual(request.split_ratio, 0.5)
        self.assertEqual(request.split_gap_px, 0)
        self.assertFalse(request.pip_enabled)
        self.assertEqual(request.pip_probability, 0.35)
        self.assertEqual(request.pip_scale, 0.28)
        self.assertEqual(request.pip_mask_mode, "circle")
        self.assertEqual(request.pause_mode, "hybrid")
        self.assertEqual(request.pause_detect_ms, 300)
        self.assertEqual(request.pause_keep_ms, 220)
        self.assertEqual(request.long_pause_ms, 1000)
        self.assertTrue(request.cover_long_pauses)

    def test_request_rejects_invalid_subtitle_color(self):
        with self.assertRaises(ValueError):
            CreateBatchRequest(
                storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4",
                subtitle_text_color="yellow",
            )

    def test_split_screen_rejects_gap_larger_than_canvas(self):
        with self.assertRaisesRegex(ValueError, "split_gap_px"):
            CreateBatchRequest(
                storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4",
                height=100, split_screen_enabled=True, split_gap_px=100,
            )

    def test_split_screen_and_pip_can_be_enabled_together(self):
        request = CreateBatchRequest(
            storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4",
            split_screen_enabled=True, pip_enabled=True,
        )
        self.assertTrue(request.split_screen_enabled)
        self.assertTrue(request.pip_enabled)

    def test_pip_selection_is_stable_and_always_selects_one(self):
        assignments = {"shot-a": {}, "shot-b": {}, "shot-c": {}}
        first = manager._pip_unit_ids(assignments, "task-1", 1, 0.05)
        self.assertEqual(first, manager._pip_unit_ids(assignments, "task-1", 1, 0.05))
        self.assertTrue(first)

    def test_auto_pip_mask_selection_is_stable_and_keeps_both_modes(self):
        pip_units = {"shot-a", "shot-b", "shot-c"}
        first = manager._pip_mask_unit_ids(pip_units, "task-1", 1, "auto")
        self.assertEqual(first, manager._pip_mask_unit_ids(pip_units, "task-1", 1, "auto"))
        circle_units, mirror_units = first
        self.assertTrue(circle_units)
        self.assertTrue(mirror_units)
        self.assertEqual(circle_units | mirror_units, pip_units)
        self.assertFalse(circle_units & mirror_units)

    def test_combined_layout_keeps_pip_and_split_units(self):
        assignments = {"shot-a": {}, "shot-b": {}, "shot-c": {}}
        pip_units, split_units = manager._layout_unit_ids(
            assignments, "task-1", 1, 1.0,
            split_enabled=True, pip_enabled=True,
        )
        self.assertTrue(pip_units)
        self.assertTrue(split_units)
        self.assertFalse(pip_units & split_units)
        self.assertEqual(pip_units | split_units, set(assignments))

    def test_pip_transform_targets_actual_top_right_quadrant(self):
        transform_x, transform_y = manager._pip_transform(1080, 1920, 0.28)
        self.assertEqual((transform_x, transform_y), (692, 1228))

    def test_media_elements_match_catalog_synonyms(self):
        self.assertTrue(manager.media_library._element_matches(
            "裤子穿不上的人物或穿衣情境",
            "人物双手拉扯牛仔裤腰头，因腹部赘肉较多裤腰无法正常闭合",
        ))
        self.assertTrue(manager.media_library._element_matches(
            "上楼梯动作",
            "女性连续向上爬楼梯，随后弯腰扶膝",
        ))
        self.assertFalse(manager.media_library._element_matches(
            "上楼梯动作",
            "女性站在楼梯口，手扶栏杆",
        ))
        self.assertTrue(manager.media_library._element_matches(
            "短距离行走后气喘的状态",
            "女性步行一段后气喘吁吁，停下扶膝",
        ))
        self.assertFalse(manager.media_library._element_matches(
            "短距离行走后气喘的状态",
            "女性上楼梯，随后弯腰扶膝，手扶栏杆面露不适",
        ))

    def test_pip_draft_adds_circle_mask_to_master_overlay(self):
        request = CreateBatchRequest(
            storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4",
            pip_enabled=True, pip_probability=1,
        ).model_dump(mode="json")
        shot = SimpleNamespace(
            shot_id="shot-1", category="product_empty_scene",
            visual_intent="product_overview", source_text="展示产品",
        )
        render_units = [{"shot": shot, "unit_id": "shot-1", "start_us": 0, "end_us": 2_000_000}]
        clip = {
            "shot_id": "shot-1", "category": "product_empty_scene",
            "visual_intent": "product_overview", "sub_index": 0, "sub_count": 1,
            "target_text": "展示产品", "asset_id": 1, "temporal_feature_id": 2,
            "target_start_us": 0, "target_end_us": 2_000_000,
            "alignment_confidence": 1.0, "clip_start_us": 0, "clip_end_us": 2_000_000,
            "source_duration_us": 2_000_000, "source_video_url": "http://media/material.mp4",
            "match_score": 0.9, "match_reason": "matched",
        }
        add_videos = AsyncMock(side_effect=[
            ("draft", "base-track", [], ["base-segment"], []),
            ("draft", "pip-track", [], ["pip-segment"], []),
        ])
        add_masks = AsyncMock()
        draft_id = "pip-test"
        manager.DRAFT_CACHE[draft_id] = object()
        try:
            with TemporaryDirectory() as temp_dir:
                audio = Path(temp_dir) / "audio.wav"
                audio.write_bytes(b"audio")
                with patch.object(manager, "_probe", return_value=(1080, 1920, 2_000_000, True)), \
                        patch.object(manager, "_export_video_clip", return_value=Path("material.mp4")), \
                        patch.object(manager, "_public_media_url", side_effect=lambda path: f"http://media/{Path(path).name}"), \
                        patch.object(manager, "create_draft", return_value=f"http://draft/get_draft?draft_id={draft_id}"), \
                        patch.object(manager, "add_videos_async", add_videos), \
                        patch.object(manager, "add_masks_async", add_masks), \
                        patch.object(manager, "add_audios_async", new=AsyncMock()), \
                        patch.object(manager, "add_captions_async", new=AsyncMock()):
                    result = asyncio.run(manager._make_draft(
                        Path("master.mp4"), audio,
                        [{"shot": shot, "start_us": 0, "end_us": 2_000_000}],
                        [{"start": 300_000, "end": 900_000, "text": "展示产品", "in_animation": "打字机 II"}],
                        render_units,
                        {"shot-1": clip}, request, 1, "task-1",
                    ))
        finally:
            manager.DRAFT_CACHE.pop(draft_id, None)
        self.assertEqual(result["layout"]["mode"], "pip")
        detail = result["detail"]
        self.assertEqual(detail["schema_version"], 1)
        self.assertEqual(detail["shots"][0]["shot_id"], "shot-1")
        self.assertEqual(detail["shots"][0]["layout"], "pip_circle")
        self.assertEqual([visual["role"] for visual in detail["shots"][0]["visuals"]], ["main", "pip"])
        self.assertEqual(detail["shots"][0]["visuals"][1]["mask"], "circle")
        self.assertEqual(detail["shots"][0]["visuals"][1]["position"], "top_right")
        self.assertEqual(detail["shots"][0]["captions"][0]["text"], "展示产品")
        self.assertEqual(detail["shots"][0]["captions"][0]["animation"], "打字机 II")
        self.assertEqual(detail["shots"][0]["captions"][0]["style"]["font_size"], 12)
        self.assertEqual(detail["shots"][0]["stickers"], [])
        self.assertEqual(detail["sticker_status"], "not_generated")
        self.assertEqual(result["layout"]["pip_count"], 1)
        self.assertEqual(result["segment_ids"], ["base-segment", "pip-segment"])
        self.assertEqual(add_videos.await_count, 2)
        self.assertEqual(add_videos.await_args_list[1].kwargs["scale_x"], 0.28)
        add_masks.assert_awaited_once_with(
            "http://draft/get_draft?draft_id=pip-test", ["pip-segment"], name="圆形",
            width=1080, height=1080,
        )

    def test_draft_adds_background_music_as_separate_track(self):
        request = CreateBatchRequest(
            storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4",
        ).model_dump(mode="json")
        shot = SimpleNamespace(shot_id="shot-1", category="oral_delivery")
        render_units = [{"shot": shot, "unit_id": "shot-1", "start_us": 0, "end_us": 2_000_000}]
        add_audios = AsyncMock()
        draft_id = "music-test"
        manager.DRAFT_CACHE[draft_id] = object()
        try:
            with TemporaryDirectory() as temp_dir:
                audio = Path(temp_dir) / "audio.wav"
                audio.write_bytes(b"audio")
                with patch.object(manager, "_probe", return_value=(1080, 1920, 2_000_000, True)), \
                        patch.object(manager, "_public_media_url", side_effect=lambda path: f"http://media/{Path(path).name}"), \
                        patch.object(manager, "create_draft", return_value=f"http://draft/get_draft?draft_id={draft_id}"), \
                        patch.object(manager, "add_videos_async", new=AsyncMock(return_value=("draft", "track", [], ["segment"], []))), \
                        patch.object(manager, "add_audios_async", add_audios), \
                        patch.object(manager, "add_captions_async", new=AsyncMock()):
                    result = asyncio.run(manager._make_draft(
                        Path("master.mp4"), audio,
                        [{"start_us": 0, "end_us": 2_000_000}], [], render_units,
                        {}, request, 1, "task-1",
                        background_music={
                            "id": 7, "title": "科技纯音乐", "url": "https://media/music.mp3",
                            "duration_us": 60_000_000, "match_score": 1.0,
                            "match_reasons": ["专业科技感"],
                        },
                    ))
        finally:
            manager.DRAFT_CACHE.pop(draft_id, None)
        self.assertEqual(add_audios.await_count, 2)
        music_audio = json.loads(add_audios.await_args_list[1].args[1])[0]
        self.assertEqual(music_audio["volume"], 0.0)
        self.assertEqual(music_audio["end"], 2_000_000)
        self.assertEqual(result["background_music"]["id"], 7)

    def test_mirror_mask_uses_complete_non_overlapping_half_panels(self):
        request = CreateBatchRequest(
            storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4",
            pip_enabled=True, pip_probability=1, pip_mask_mode="mirror",
        ).model_dump(mode="json")
        shot = SimpleNamespace(shot_id="shot-1", category="product_empty_scene")
        render_units = [{"shot": shot, "unit_id": "shot-1", "start_us": 0, "end_us": 2_000_000}]
        clip = {
            "shot_id": "shot-1", "category": "product_empty_scene",
            "visual_intent": "product_overview", "sub_index": 0, "sub_count": 1,
            "target_text": "展示产品", "asset_id": 1, "temporal_feature_id": 2,
            "target_start_us": 0, "target_end_us": 2_000_000,
            "alignment_confidence": 1.0, "clip_start_us": 0, "clip_end_us": 2_000_000,
            "source_duration_us": 2_000_000, "source_video_url": "http://media/material.mp4",
            "match_score": 0.9, "match_reason": "matched",
        }
        add_videos = AsyncMock(side_effect=[
            ("draft", "mirror-top-track", [], ["mirror-top-segment"], []),
            ("draft", "mirror-bottom-track", [], ["mirror-bottom-segment"], []),
        ])
        add_masks = AsyncMock()
        draft_id = "mirror-test"
        manager.DRAFT_CACHE[draft_id] = object()
        try:
            with TemporaryDirectory() as temp_dir:
                audio = Path(temp_dir) / "audio.wav"
                audio.write_bytes(b"audio")
                with patch.object(manager, "_probe", return_value=(1080, 1920, 2_000_000, True)), \
                        patch.object(manager, "_export_panel_clip", side_effect=[Path("speaker.mp4"), Path("material.mp4")]) as export_panel, \
                        patch.object(manager.media_library, "resolve_local_source", return_value=Path("material-source.mp4")), \
                        patch.object(manager, "_public_media_url", side_effect=lambda path: f"http://media/{Path(path).name}"), \
                        patch.object(manager, "create_draft", return_value=f"http://draft/get_draft?draft_id={draft_id}"), \
                        patch.object(manager, "add_videos_async", add_videos), \
                        patch.object(manager, "add_masks_async", add_masks), \
                        patch.object(manager, "add_audios_async", new=AsyncMock()), \
                        patch.object(manager, "add_captions_async", new=AsyncMock()):
                    result = asyncio.run(manager._make_draft(
                        Path("master.mp4"), audio,
                        [{"start_us": 0, "end_us": 2_000_000}], [], render_units,
                        {"shot-1": clip}, request, 1, "task-1",
                    ))
        finally:
            manager.DRAFT_CACHE.pop(draft_id, None)
        self.assertEqual(result["layout"]["mode"], "mirror")
        self.assertEqual(result["layout"]["mirror_height_ratio"], 0.5)
        self.assertEqual(result["layout"]["mirror_fit_mode"], "contain_blur")
        self.assertEqual(result["detail"]["shots"][0]["layout"], "mirror")
        self.assertEqual([visual["role"] for visual in result["detail"]["shots"][0]["visuals"]],
                         ["speaker", "material"])
        self.assertEqual(result["detail"]["shots"][0]["visuals"][0]["mask"], "mirror")
        self.assertEqual(result["segment_ids"], ["mirror-top-segment", "mirror-bottom-segment"])
        self.assertEqual(add_videos.await_args_list[0].kwargs["transform_y"], 960)
        self.assertEqual(add_videos.await_args_list[1].kwargs["transform_y"], -960)
        self.assertEqual([call.args[7] for call in export_panel.call_args_list], ["contain_blur", "contain_blur"])
        add_masks.assert_awaited_once_with(
            "http://draft/get_draft?draft_id=mirror-test", ["mirror-top-segment"], name="镜面",
            width=1080, height=960, feather=5,
        )

    def test_split_panel_layout_keeps_gap_and_centers_panels(self):
        layout = manager._split_panel_layout(1080, 1920, 0.5, 8)
        self.assertEqual(layout["top_height"], 956)
        self.assertEqual(layout["bottom_height"], 956)
        self.assertEqual(layout["top_transform_y"], 964)
        self.assertEqual(layout["bottom_transform_y"], -964)

    def test_split_panel_layout_default_has_no_visible_seam(self):
        layout = manager._split_panel_layout(1080, 1920, 0.5, 0)
        self.assertEqual(layout["top_height"] + layout["bottom_height"], 1920)
        self.assertEqual(layout["top_transform_y"], 960)
        self.assertEqual(layout["bottom_transform_y"], -960)

    def test_contain_blur_filter_keeps_complete_foreground(self):
        value, args = manager._panel_video_filter(1080, 956, 2_000_000, "contain_blur")
        self.assertIn("[panel_fg_source]scale=1080:956:force_original_aspect_ratio=decrease", value)
        self.assertIn("overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2", value)
        self.assertEqual(args[-2:], ["-map", "[panel_out]"])

    def test_split_draft_writes_top_and_bottom_video_tracks(self):
        request = CreateBatchRequest(
            storyboard_id=self.storyboard_id, source_video_path="uploads/a.mp4",
            split_screen_enabled=True,
        ).model_dump(mode="json")
        shot = SimpleNamespace(shot_id="shot-1", category="product_empty_scene")
        render_units = [{"shot": shot, "unit_id": "shot-1", "start_us": 0, "end_us": 2_000_000}]
        clip = {
            "shot_id": "shot-1", "category": "product_empty_scene",
            "visual_intent": "product_overview", "sub_index": 0, "sub_count": 1,
            "target_text": "展示产品", "asset_id": 1, "temporal_feature_id": 2,
            "target_start_us": 0, "target_end_us": 2_000_000,
            "alignment_confidence": 1.0, "clip_start_us": 0, "clip_end_us": 2_000_000,
            "source_duration_us": 2_000_000, "source_video_url": "http://media/material.mp4",
            "match_score": 0.9, "match_reason": "matched",
        }
        add_videos = AsyncMock(side_effect=[
            ("draft", "top-track", [], ["top-segment"], []),
            ("draft", "bottom-track", [], ["bottom-segment"], []),
        ])
        draft_id = "split-test"
        manager.DRAFT_CACHE[draft_id] = object()
        try:
            with TemporaryDirectory() as temp_dir:
                audio = Path(temp_dir) / "audio.wav"
                audio.write_bytes(b"audio")
                with patch.object(manager, "_probe", return_value=(1080, 1920, 2_000_000, True)), \
                        patch.object(manager, "_export_panel_clip", side_effect=[Path("top.mp4"), Path("bottom.mp4")]), \
                        patch.object(manager.media_library, "resolve_local_source", return_value=Path("material.mp4")), \
                        patch.object(manager, "_public_media_url", side_effect=lambda path: f"http://media/{Path(path).name}"), \
                        patch.object(manager, "create_draft", return_value=f"http://draft/get_draft?draft_id={draft_id}"), \
                        patch.object(manager, "add_videos_async", add_videos), \
                        patch.object(manager, "add_audios_async", new=AsyncMock()), \
                        patch.object(manager, "add_captions_async", new=AsyncMock()):
                    result = asyncio.run(manager._make_draft(
                        Path("master.mp4"), audio,
                        [{"start_us": 0, "end_us": 2_000_000}], [], render_units,
                        {"shot-1": clip}, request, 1, "task-1",
                    ))
        finally:
            manager.DRAFT_CACHE.pop(draft_id, None)
        self.assertEqual(result["layout"]["mode"], "split_screen")
        self.assertEqual(result["detail"]["shots"][0]["layout"], "split_screen")
        self.assertEqual([visual["role"] for visual in result["detail"]["shots"][0]["visuals"]],
                         ["speaker", "material"])
        self.assertEqual(result["segment_ids"], ["top-segment", "bottom-segment"])
        self.assertEqual(add_videos.await_count, 2)
        self.assertEqual(add_videos.await_args_list[0].kwargs["transform_y"], 960)
        self.assertEqual(add_videos.await_args_list[1].kwargs["transform_y"], -960)

    def test_landscape_candidate_requires_safe_crop_or_split_screen(self):
        candidate = {"width": 1920, "height": 1080, "vertical_fit": "contain_blur"}
        rejected = manager._apply_portrait_layout_policy(
            {"shot": [candidate]}, {"width": 1080, "height": 1920, "split_screen_enabled": False},
        )
        self.assertEqual(rejected["shot"], [])
        split = manager._apply_portrait_layout_policy(
            {"shot": [candidate]}, {"width": 1080, "height": 1920, "split_screen_enabled": True},
        )
        self.assertEqual(split["shot"][0]["layout_mode"], "split_screen")

    def test_corrected_landscape_orientation_can_fill_portrait_without_crop(self):
        candidate = {
            "width": 1920, "height": 1080, "vertical_fit": "contain_blur",
            "rotation_degrees": 90,
        }
        result = manager._apply_portrait_layout_policy(
            {"shot": [candidate]},
            {"width": 1080, "height": 1920, "split_screen_enabled": False},
        )
        self.assertEqual(result["shot"], [candidate])

    def test_landscape_candidate_with_verified_smart_crop_can_fill_portrait(self):
        candidate = {"width": 1920, "height": 1080, "vertical_fit": "smart_crop"}
        result = manager._apply_portrait_layout_policy(
            {"shot": [candidate]}, {"width": 1080, "height": 1920, "split_screen_enabled": False},
        )
        self.assertEqual(result["shot"], [candidate])

    def test_forced_landscape_split_is_not_randomly_assigned_to_pip(self):
        assignments = {
            "landscape": {"layout_mode": "split_screen"},
            "portrait": {},
        }
        pip_units, split_units = manager._layout_unit_ids(
            assignments, "task", 1, 1.0, split_enabled=True, pip_enabled=True,
        )
        self.assertNotIn("landscape", pip_units)
        self.assertIn("landscape", split_units)

    def test_upload_resolution_prefers_uploads_and_supports_legacy_root(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "uploads").mkdir()
            canonical = root / "uploads" / "video.mp4"
            legacy = root / "video.mp4"
            canonical.write_bytes(b"canonical")
            legacy.write_bytes(b"legacy")
            with patch.object(manager, "media_root", return_value=root):
                self.assertEqual(manager._resolve_media("video.mp4", "口播视频"), canonical)
                canonical.unlink()
                self.assertEqual(manager._resolve_media("video.mp4", "口播视频"), legacy)

    def test_transcription_runs_in_isolated_analysis_worker(self):
        captured = {}

        async def run_worker(payload):
            captured.update(payload)
            return {"clips": [{
                "status": "completed",
                "segments": [{"text": "测试", "words": []}],
            }]}

        with patch.object(manager.scene_detection, "_run_worker", side_effect=run_worker):
            segments = manager._transcribe(Path("audio.wav"))
        self.assertEqual(segments[0]["text"], "测试")
        self.assertEqual(captured["operation"], "transcribe_scene_clips")
        self.assertEqual(captured["scene_clip_paths"], ["audio.wav"])

    def test_storyboard_review_is_optional_for_video_generation(self):
        confirmed_shot = SimpleNamespace(review_status="confirmed")
        pending_shot = SimpleNamespace(review_status="pending")
        with patch.object(manager.storyboard_store, "get", return_value=None):
            with self.assertRaisesRegex(ValueError, "不存在"):
                manager._load_storyboard(self.storyboard_id)

        for board in (
            SimpleNamespace(status="needs_review", shots=[pending_shot]),
            SimpleNamespace(status="needs_review", shots=[confirmed_shot, pending_shot]),
            SimpleNamespace(status="confirmed", shots=[confirmed_shot]),
        ):
            with self.subTest(status=board.status, reviews=[shot.review_status for shot in board.shots]):
                with patch.object(manager.storyboard_store, "get", return_value=board):
                    self.assertIs(manager._load_storyboard(self.storyboard_id), board)

        with patch.object(manager.storyboard_store, "get", return_value=SimpleNamespace(shots=[])):
            with self.assertRaisesRegex(ValueError, "没有镜头"):
                manager._load_storyboard(self.storyboard_id)

    def test_batch_run_does_not_generate_or_create_storyboard(self):
        source = inspect.getsource(manager._run)
        self.assertNotIn("planner.", source)
        self.assertNotIn("storyboard_store.create", source)

    def test_alignment_maps_storyboard_ranges_by_word_timestamps(self):
        class Shot:
            def __init__(self, text, start, end):
                self.source_text, self.source_start, self.source_end = text, start, end
                self.shot_id = f"shot-{start}"

        class Board:
            request = type("Request", (), {"script": "今天去跑步。"})()
            shots = [Shot("今天", 0, 2), Shot("去跑步。", 2, 6)]

        transcript = [{"words": [
            {"word": "今天", "start": 0.2, "end": 0.7},
            {"word": "去", "start": 0.8, "end": 1.0},
            {"word": "跑步", "start": 1.0, "end": 1.8},
        ]}]
        result = manager._align_shots(Board(), transcript, 2_000_000)
        self.assertEqual([(item["start_us"], item["end_us"]) for item in result],
                         [(200_000, 750_000), (750_000, 1_800_000)])
        self.assertEqual(result[0]["caption_char_boundaries_us"][0], 200_000)
        self.assertEqual(result[0]["caption_char_boundaries_us"][-1], 700_000)
        self.assertEqual(result[1]["caption_char_boundaries_us"][0], 800_000)
        self.assertEqual(result[1]["caption_char_boundaries_us"][-1], 1_800_000)

    def test_subtitles_start_at_spoken_word_not_expanded_shot_boundary(self):
        captions = manager._subtitle_items([{
            "subtitle": "开始说话。",
            "start_us": 500_000,
            "end_us": 2_500_000,
            "char_boundaries_us": [500_000, 900_000, 1_300_000, 1_700_000, 2_100_000],
            "caption_char_boundaries_us": [800_000, 1_100_000, 1_400_000, 1_700_000, 2_000_000],
        }], [{"text": "开始说话。"}])
        self.assertEqual(captions[0]["start"], 800_000)
        self.assertEqual(captions[0]["end"], 2_000_000)

    def test_alignment_splits_word_time_when_storyboard_boundary_is_inside_word(self):
        class Shot:
            def __init__(self, text, start, end):
                self.source_text, self.source_start, self.source_end = text, start, end
                self.shot_id = f"shot-{start}"

        class Board:
            request = type("Request", (), {"script": "你好"})()
            shots = [Shot("你", 0, 1), Shot("好", 1, 2)]

        transcript = [{"words": [{"word": "你好", "start": 0.0, "end": 1.0}]}]
        result = manager._align_shots(Board(), transcript, 1_000_000)
        self.assertEqual([(item["start_us"], item["end_us"]) for item in result],
                         [(0, 500_000), (500_000, 1_000_000)])

    def test_alignment_tolerates_asr_homophone_substitution(self):
        class Shot:
            def __init__(self, text, start, end):
                self.source_text, self.source_start, self.source_end = text, start, end
                self.shot_id = f"shot-{start}"

        class Board:
            request = type("Request", (), {"script": "奥利司他拿到手。大家别买错。"})()
            shots = [Shot("奥利司他拿到手。", 0, 8), Shot("大家别买错。", 8, 14)]

        transcript = [{"words": [
            {"word": "奥利斯他拿到手", "start": 0.0, "end": 2.0},
            {"word": "大家别买错", "start": 2.2, "end": 3.5},
        ]}]
        result = manager._align_shots(Board(), transcript, 3_500_000)
        self.assertEqual([(item["start_us"], item["end_us"]) for item in result],
                         [(0, 2_100_000), (2_100_000, 3_500_000)])

    def test_low_transcript_match_is_rejected(self):
        self.assertFalse(manager._transcript_matches_script("原稿台词", [{"text": "完全不同"}]))
        self.assertTrue(manager._transcript_matches_script("今天去跑步。", [{"text": "今天，去跑步！"}]))

    def test_pause_optimizer_uses_short_and_long_pause_targets(self):
        transcript = [{"words": [
            {"word": "一", "start": 0.0, "end": 1.0},
            {"word": "二", "start": 1.5, "end": 2.0},
            {"word": "三", "start": 3.2, "end": 3.5},
        ]}]
        removals = manager._pause_removals_us(
            transcript, detect_ms=300, keep_ms=220,
            long_pause_ms=1000, cover_long_pauses=True,
        )
        self.assertEqual(removals, [(1_110_000, 1_390_000), (2_175_000, 3_025_000)])

    def test_pause_optimizer_remaps_existing_word_timestamps(self):
        transcript = [{
            "text": "一二", "start": 0.0, "end": 2.5,
            "words": [
                {"word": "一", "start": 0.0, "end": 1.0},
                {"word": "二", "start": 2.0, "end": 2.5},
            ],
        }]
        removals = manager._pause_removals_us(
            transcript, detect_ms=300, keep_ms=220,
            long_pause_ms=1000, cover_long_pauses=True,
        )
        mapped = manager._remap_transcript(transcript, removals)
        self.assertEqual(removals, [(1_175_000, 1_825_000)])
        self.assertEqual(mapped[0]["words"][1]["start"], 1.35)
        self.assertEqual(mapped[0]["words"][1]["end"], 1.85)
        self.assertEqual(mapped[0]["end"], 1.85)
        self.assertEqual(transcript[0]["words"][1]["start"], 2.0)

    def test_pause_optimizer_can_leave_long_pauses_unchanged(self):
        transcript = [{"words": [
            {"word": "一", "start": 0.0, "end": 1.0},
            {"word": "二", "start": 2.2, "end": 2.5},
        ]}]
        self.assertEqual(manager._pause_removals_us(
            transcript, detect_ms=300, keep_ms=220,
            long_pause_ms=1000, cover_long_pauses=False,
        ), [])

    def test_long_replaceable_shot_remains_one_render_unit(self):
        text = "展示产品包装，放大OTC标识，扫描包装二维码。"
        normalized_length = manager._normalized_length(text)
        shot = SimpleNamespace(
            shot_id="shot-product", category="product_empty_scene", source_text=text,
        )
        units = manager._render_units([{
            "shot": shot, "start_us": 0, "end_us": 12_000_000,
            "subtitle": text, "alignment_confidence": 0.95,
            "char_boundaries_us": [
                round(12_000_000 * index / normalized_length)
                for index in range(normalized_length + 1)
            ],
        }])
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["start_us"], 0)
        self.assertEqual(units[0]["end_us"], 12_000_000)
        self.assertEqual(units[0]["unit_id"], "shot-product")
        self.assertEqual(units[0]["sub_index"], 0)
        self.assertEqual(units[0]["sub_count"], 1)
        self.assertEqual(units[0]["query_text"], text)

    def test_long_oral_delivery_is_not_split_for_material_matching(self):
        shot = SimpleNamespace(shot_id="shot-oral", category="oral_delivery", source_text="很长的口播内容")
        units = manager._render_units([{
            "shot": shot, "start_us": 0, "end_us": 12_000_000,
            "subtitle": shot.source_text, "alignment_confidence": 1.0,
        }])
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["unit_id"], "shot-oral")

    def test_subtitles_use_provider_segments_and_keep_exact_timeline(self):
        text = "奥利司他拿到手，很多人担心：买到的是不是正品？"
        normalized_length = manager._normalized_length(text)
        captions = manager._subtitle_items([{
            "subtitle": text, "start_us": 1_000_000, "end_us": 7_000_000,
            "char_boundaries_us": [
                1_000_000 + round(6_000_000 * index / normalized_length)
                for index in range(normalized_length + 1)
            ],
        }], [
            {"text": "奥利司他拿到手，"},
            {"text": "很多人担心："},
            {"text": "买到的是不是正品？"},
        ])
        self.assertEqual(len(captions), 3)
        self.assertEqual(captions[0]["start"], 1_000_000)
        self.assertEqual(captions[-1]["end"], 7_000_000)
        self.assertTrue(all(left["end"] == right["start"] for left, right in zip(captions, captions[1:])))
        self.assertEqual("".join(item["text"] for item in captions), "奥利司他拿到手很多人担心买到的是不是正品")
        self.assertTrue(all(item["in_animation"] == "打字机 II" for item in captions))

    def test_provider_segment_crossing_storyboard_boundary_is_clipped(self):
        texts = ["第一句话。", "第二句话。"]
        aligned = []
        for index, text in enumerate(texts):
            length = manager._normalized_length(text)
            start = index * 2_000_000
            aligned.append({
                "subtitle": text, "start_us": start, "end_us": start + 2_000_000,
                "char_boundaries_us": [
                    start + round(2_000_000 * offset / length)
                    for offset in range(length + 1)
                ],
            })
        captions = manager._subtitle_items(
            aligned, [{"text": "第一句话。第二句"}, {"text": "话。"}],
        )
        self.assertEqual([item["text"] for item in captions], ["第一句话", "第二句", "话"])
        self.assertEqual(captions[0]["end"], 2_000_000)
        self.assertEqual(captions[1]["start"], 2_000_000)

    def test_provider_text_mismatch_is_rejected(self):
        text = "原始脚本。"
        length = manager._normalized_length(text)
        with self.assertRaisesRegex(RuntimeError, "断句结果与脚本不一致"):
            manager._subtitle_items([{
                "subtitle": text, "start_us": 0, "end_us": 2_000_000,
                "char_boundaries_us": [round(2_000_000 * index / length)
                                       for index in range(length + 1)],
            }], [{"text": "不同文字。"}])

    def test_provider_segments_require_word_timestamps(self):
        with self.assertRaisesRegex(RuntimeError, "缺少词级时间戳"):
            manager._subtitle_items([{
                "subtitle": "测试字幕。", "start_us": 0, "end_us": 2_000_000,
            }], [{"text": "测试字幕。"}])

    def test_subtitle_display_can_keep_punctuation_and_disable_animation(self):
        text = "测试，字幕。"
        length = manager._normalized_length(text)
        captions = manager._subtitle_items([{
            "subtitle": text, "start_us": 0, "end_us": 2_000_000,
            "char_boundaries_us": [round(2_000_000 * index / length)
                                   for index in range(length + 1)],
        }], [{"text": text}], remove_punctuation=False, in_animation=None)
        self.assertEqual(captions, [{"start": 0, "end": 2_000_000, "text": text}])

    def test_default_subtitle_position_is_near_lower_quarter(self):
        self.assertEqual(manager._subtitle_transform_y({"height": 1920}), -960.0)
        self.assertEqual(
            manager._subtitle_transform_y({"height": 1920, "subtitle_transform_y": -720}),
            -720.0,
        )

    def test_allocation_avoids_repeats_inside_a_version(self):
        shots = [type("Shot", (), {"shot_id": "a"})(), type("Shot", (), {"shot_id": "b"})()]
        items = [
            {"shot": shots[0], "start_us": 0, "end_us": 1_000_000},
            {"shot": shots[1], "start_us": 1_000_000, "end_us": 2_000_000},
        ]
        candidate_pool = [{
            "asset_id": asset, "temporal_feature_id": asset, "match_score": 0.9,
            "quality_score": 80, "commercial_score": 80,
        } for asset in (1, 2)]
        with patch.object(manager.media_library, "usage_count", return_value=0):
            allocated = manager._allocate(items, {"a": candidate_pool, "b": candidate_pool}, 2)
        for version in allocated:
            selected = [(item["asset_id"], item["temporal_feature_id"]) for item in version.values()]
            self.assertEqual(len(selected), len(set(selected)))

    def test_only_adjacent_oral_delivery_segments_merge(self):
        track = []
        common = {"video_url": "master.mp4", "duration": 10_000_000,
                  "width": 1080, "height": 1920, "volume": 0.0}
        manager._append_master_segment(track, {
            **common, "source_start": 0, "start": 0, "end": 2_000_000,
        }, "oral_delivery")
        manager._append_master_segment(track, {
            **common, "source_start": 2_000_000, "start": 2_000_000, "end": 5_000_000,
        }, "oral_delivery")
        manager._append_master_segment(track, {
            **common, "source_start": 5_000_000, "start": 5_000_000, "end": 7_000_000,
        }, "product_empty_scene")
        manager._append_master_segment(track, {
            **common, "source_start": 7_000_000, "start": 7_000_000, "end": 9_000_000,
        }, "product_empty_scene")
        self.assertEqual(len(track), 3)
        self.assertEqual((track[0]["start"], track[0]["end"]), (0, 5_000_000))
        self.assertEqual((track[1]["start"], track[1]["end"]), (5_000_000, 7_000_000))
        self.assertEqual((track[2]["start"], track[2]["end"]), (7_000_000, 9_000_000))

    def test_candidate_search_uses_canonical_storyboard_category_once(self):
        shot = SimpleNamespace(
            category="product_empty_scene",
            visual_description="展示药盒",
            source_text="红标OTC",
            evidence="红标OTC",
            semantic_reason="展示药盒",
            required_elements=["红标OTC标识"],
            forbidden_elements=[],
        )
        with patch.object(manager.media_library, "search_clips", return_value=[]) as search:
            manager._candidate_for({"shot": shot, "start_us": 0, "end_us": 3_000_000}, ["赛乐赛"])
        search.assert_called_once_with(
            query="红标OTC 红标OTC 展示药盒 展示药盒 红标OTC标识",
            usage_role="product_empty_scene",
            product_names=["赛乐赛"],
            visual_intent="product_overview",
            duration_us=3_000_000,
            required_elements=["红标OTC标识"],
            forbidden_elements=[],
            limit=40,
        )

    def test_medication_storyboard_scene_uses_medication_candidate_search(self):
        shot = SimpleNamespace(
            shot_id="medication", category="scene_empty_scene",
            visual_intent="medication_moment",
            source_text="随餐喝水吞服一粒", evidence="喝水吞服",
            semantic_reason="展示真实服药时刻",
            visual_description="人物拿起胶囊送入口中并喝水吞服",
            required_elements=["人物喝水吞服胶囊"], forbidden_elements=[],
        )
        candidate = {
            "asset_id": 1, "temporal_feature_id": 2, "sha256": "medication",
            "match_score": 0.9, "successful_use_count": 0,
        }
        with patch.object(manager.media_library, "search_clips", return_value=[candidate]) as search:
            result = manager._candidate_for({
                "shot": shot, "start_us": 0, "end_us": 3_000_000,
            }, ["赛乐赛"])
        self.assertEqual(result, [candidate])
        self.assertEqual(search.call_args.kwargs["usage_role"], "scene_empty_scene")
        self.assertEqual(search.call_args.kwargs["visual_intent"], "medication_moment")
        self.assertEqual(search.call_args.kwargs["required_elements"], ["人物喝水吞服胶囊"])

    def test_qr_storyboard_search_uses_only_qr_visibility_requirement(self):
        shot = SimpleNamespace(
            shot_id="qr", category="product_empty_scene", visual_intent="qr_traceability",
            source_text="直接扫包装上的溯源码", evidence="二维码查询追溯信息",
            semantic_reason="展示二维码", visual_description="包装背面的二维码",
            required_elements=["产品盒身", "扫码动作", "追溯生产信息"], forbidden_elements=[],
        )
        with patch.object(manager.media_library, "search_clips", return_value=[]) as search:
            manager._candidate_for({"shot": shot, "start_us": 0, "end_us": 3_000_000}, [])
        self.assertEqual(search.call_args.kwargs["visual_intent"], "qr_code_visible")
        self.assertEqual(search.call_args.kwargs["required_elements"], ["二维码或条形码"])

        target = manager._material_review_target({
            "shot": shot, "start_us": 0, "end_us": 3_000_000,
        }, [], {"width": 1080, "height": 1920})
        self.assertEqual(target["visual_intent"], "qr_code_visible")
        self.assertEqual(target["required_elements"], ["二维码或条形码"])

    def test_candidate_search_does_not_filter_text_or_watermark_flags(self):
        source = inspect.getsource(media_library.search_clips)
        self.assertNotIn("tf.has_subtitles=0", source)
        self.assertNotIn("tf.has_overlay_text=0", source)
        self.assertNotIn("tf.has_watermark=0", source)

    def test_low_local_alignment_keeps_original_picture(self):
        shot = SimpleNamespace(
            category="product_empty_scene", visual_description="展示药盒",
            source_text="展示正品", required_elements=["药盒"], forbidden_elements=[],
        )
        with patch.object(manager.media_library, "search_clips") as search:
            result = manager._candidate_for({
                "shot": shot, "start_us": 0, "end_us": 3_000_000,
                "alignment_confidence": 0.5,
            }, ["赛乐赛"])
        self.assertEqual(result, [])
        search.assert_not_called()

    def test_material_review_batches_until_each_draft_has_a_unique_candidate(self):
        shot = SimpleNamespace(
            shot_id="a", category="motion", visual_intent="exercise_action",
            source_text="走楼梯", semantic_reason="展示运动状态",
            visual_description="人物走楼梯", required_elements=[], forbidden_elements=[],
        )
        candidates = [{
            "asset_id": index, "temporal_feature_id": index,
            "sha256": f"sha-{index}", "match_score": 0.9,
        } for index in range(1, 6)]

        def approve(**kwargs):
            return list(kwargs["candidates"]), []

        with patch.dict("os.environ", {"SCRIPT_VIDEO_MATERIAL_REVIEW_CANDIDATES": "2"}), \
                patch.object(manager.material_review, "review_with_fallback", side_effect=approve) as review:
            result = manager._review_candidate_sets(
                task_id="task", render_units=[{
                    "shot": shot, "unit_id": "a", "start_us": 0, "end_us": 2_000_000,
                }], candidates={"a": candidates}, product_names=[],
                request={"count": 4, "width": 1080, "height": 1920},
                primary={"id": 1}, backup=None,
            )

        self.assertEqual(len(result["a"]), 4)
        self.assertEqual(review.call_count, 2)

    def test_allocation_uses_distinct_candidates_across_versions_before_reuse(self):
        shot = type("Shot", (), {
            "shot_id": "a", "category": "product_empty_scene", "visual_intent": "product_detail",
        })()
        candidates = [{
            "asset_id": asset, "temporal_feature_id": asset, "match_score": 0.9,
            "quality_score": 80, "commercial_score": 80, "successful_use_count": 0,
        } for asset in (1, 2, 3)]
        with patch.object(manager.media_library, "usage_count", return_value=0):
            allocated = manager._allocate(
                [{"shot": shot, "start_us": 0, "end_us": 1_000_000}],
                {"a": candidates}, 3,
            )
        self.assertEqual([item["a"]["asset_id"] for item in allocated], [1, 2, 3])
        self.assertEqual(allocated[0]["a"]["visual_intent"], "product_detail")

    def test_allocation_never_reuses_same_underlying_video_across_drafts(self):
        shot = type("Shot", (), {"shot_id": "a", "category": "motion"})()
        candidates = [
            {"asset_id": 1, "temporal_feature_id": 11, "sha256": "same",
             "match_score": 0.95, "successful_use_count": 0},
            {"asset_id": 2, "temporal_feature_id": 22, "sha256": "same",
             "match_score": 0.94, "successful_use_count": 0},
            {"asset_id": 3, "temporal_feature_id": 33, "sha256": "different",
             "match_score": 0.90, "successful_use_count": 0},
        ]
        allocated = manager._allocate(
            [{"shot": shot, "start_us": 0, "end_us": 1_000_000}],
            {"a": candidates}, 2,
        )
        self.assertEqual([item["a"]["asset_id"] for item in allocated], [1, 3])

    def test_allocation_keeps_original_when_unique_material_is_exhausted(self):
        shot = type("Shot", (), {"shot_id": "a", "category": "motion"})()
        candidate = {"asset_id": 1, "temporal_feature_id": 11, "sha256": "same",
                     "match_score": 0.95, "successful_use_count": 0}
        allocated = manager._allocate(
            [{"shot": shot, "start_us": 0, "end_us": 1_000_000}],
            {"a": [candidate]}, 2,
        )
        self.assertIn("a", allocated[0])
        self.assertNotIn("a", allocated[1])

    def test_missing_catalog_creates_only_analysis_tables_when_assets_exist(self):
        class Cursor:
            def __init__(self):
                self.statements = []
                self.rows = [{"TABLE_NAME": "myh_assets"}]
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def execute(self, sql):
                self.statements.append(sql)
                if "information_schema.TABLES" in sql:
                    return
            def fetchall(self): return self.rows

        class Connection:
            def __init__(self): self.cursor_instance = Cursor()
            def cursor(self): return self.cursor_instance

        connection = Connection()
        with patch.object(media_library, "_connection") as connect:
            connect.return_value.__enter__.return_value = connection
            media_library.ensure_catalog_tables()
        creates = [sql for sql in connection.cursor_instance.statements if sql.startswith("CREATE TABLE")]
        self.assertEqual(len(creates), 2)
        self.assertTrue(all("IF NOT EXISTS" in sql for sql in creates))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS myh_media_analysis" in sql for sql in creates))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS myh_media_temporal_features" in sql for sql in creates))

    def test_access_url_requires_http_or_configured_base(self):
        with patch.dict("os.environ", {"MEDIA_LIBRARY_PUBLIC_BASE_URL": ""}), \
                patch.object(media_library, "_settings", return_value={}):
            with self.assertRaises(RuntimeError):
                media_library._join_access_url("", "clips/a.mp4")

    def test_access_url_uses_config_file_public_base(self):
        with patch.dict("os.environ", {"MEDIA_LIBRARY_PUBLIC_BASE_URL": ""}), \
                patch.object(media_library, "_settings", return_value={
                    "MEDIA_LIBRARY_PUBLIC_BASE_URL": "https://media.example/static/upload",
                }):
            self.assertEqual(
                media_library._join_access_url("http://127.0.0.1/a.mp4", "clips/a.mp4"),
                "https://media.example/static/upload/clips/a.mp4",
            )

    def test_product_identity_combines_brand_and_product_fields(self):
        row = {
            "detected_brand_name": "赛乐赛",
            "detected_product_name": "奥利司他胶囊",
        }
        self.assertTrue(media_library._product_identity_matches(
            ["赛乐赛奥利司他胶囊"], row, "奥利司他胶囊 赛乐赛 OTC减重药品",
        ))
        row["detected_brand_name"] = "赛乐素"
        self.assertFalse(media_library._product_identity_matches(
            ["赛乐赛奥利司他胶囊"], row, "奥利司他胶囊 赛乐素 OTC减重药品",
        ))

    def test_chinese_semantic_similarity_does_not_require_identical_sentence(self):
        score = media_library._semantic_similarity(
            "展示赛乐赛奥利司他包装，突出红标OTC标识",
            "手持赛乐赛奥利司他胶囊药盒，指示棒指向包装上的OTC标识位置",
        )
        self.assertGreater(score, 0.25)

    def test_visual_element_detection_distinguishes_specific_content(self):
        otc_text = "手持赛乐赛奥利司他胶囊包装盒，指示棒指向OTC标识位置"
        self.assertTrue(media_library._element_matches("红标OTC标识", otc_text))
        self.assertFalse(media_library._element_matches("手机扫码动作", otc_text))
        self.assertFalse(media_library._element_matches("溯源码", otc_text))
        self.assertFalse(media_library._element_matches("成分功能标签", otc_text))

    def test_visual_intents_accept_equivalent_product_evidence(self):
        self.assertEqual(
            media_library._visual_intent_matches(
                "它有红标OTC标识", "手指指向药盒右上角的OTC标识", ["赛乐赛奥利司他"],
            ),
            {"otc_emphasis": True},
        )
        self.assertEqual(
            media_library._visual_intent_matches(
                "用手机扫码验证", "赛乐赛包装背面的二维码清晰出现", ["赛乐赛奥利司他"],
            ),
            {"qr_scan": True},
        )
        self.assertEqual(
            media_library._visual_intent_matches(
                "溯源码可以查验", "镜头展开赛乐赛药品说明书", ["赛乐赛奥利司他"],
            ),
            {"traceability": True},
        )
        self.assertEqual(
            media_library._visual_intent_matches(
                "二维码可以查验", "包装背面的二维码清晰出现", ["赛乐赛奥利司他"],
                visual_intent="qr_code_visible",
            ),
            {"intent:qr_code_visible": True},
        )

    def test_code_script_intent_accepts_qr_or_barcode_visibility(self):
        self.assertEqual(
            media_library.matching_visual_intent("直接扫包装背面的二维码", "qr_traceability"),
            "qr_code_visible",
        )
        self.assertEqual(
            media_library.matching_visual_intent("扫描盒身条码查询溯源", "qr_traceability"),
            "qr_code_visible",
        )
        self.assertTrue(media_library._visual_intent_matches(
            "扫码查询溯源码", "包装背面的二维码清晰出现", [],
            visual_intent="qr_code_visible",
        )["intent:qr_code_visible"])
        self.assertTrue(media_library._visual_intent_matches(
            "扫码查询溯源码", "药盒上的条形码清晰出现", [],
            visual_intent="qr_code_visible",
        )["intent:qr_code_visible"])
        self.assertTrue(media_library._visual_intent_matches(
            "扫描条码查看产品信息", "包装上的barcode清晰可见", [],
            visual_intent="qr_code_visible",
        )["intent:qr_code_visible"])
        self.assertTrue(media_library.qr_code_visible("包装没有二维码，但条形码清晰出现"))
        self.assertFalse(media_library.qr_code_visible("包装上的二维码或条形码都不可见"))
        self.assertFalse(media_library.qr_code_visible("包装上的条形码不可见"))
        self.assertFalse(media_library.qr_code_visible("The barcode is not visible"))
        self.assertTrue(media_library._element_matches("二维码", "药盒上的条码清晰可见"))
        self.assertFalse(media_library._element_matches("二维码", "药盒上的条码不可见"))
        self.assertFalse(media_library._visual_intent_matches(
            "扫码查询溯源码", "镜头展开药品说明书", [],
            visual_intent="qr_code_visible",
        )["intent:qr_code_visible"])

    def test_action_event_match_anchors_clip_at_verified_action_start(self):
        match = media_library._best_action_event(
            [{
                "id": 7, "subject": "女性人物", "action": "沿楼梯向上攀爬",
                "object": "楼梯", "description": "连续上楼动作",
                "start_us": 4_000_000, "end_us": 8_000_000,
                "evidence_frame_times_us": "[4200000, 6000000]", "confidence": 0.94,
            }],
            query="女性连续向上爬楼梯",
            usage_role="pain_point", product_names=[], visual_intent="exercise_action",
            required_elements=["爬楼梯"], duration_us=2_000_000,
            usable_start_us=1_000_000, usable_end_us=9_000_000,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match["start_us"], 4_000_000)
        self.assertEqual(match["evidence_frame_times_us"], [4_200_000, 6_000_000])

    def test_action_event_is_not_used_when_target_duration_cannot_fit_after_action(self):
        match = media_library._best_action_event(
            [{
                "id": 8, "subject": "女性人物", "action": "沿楼梯向上攀爬",
                "object": "楼梯", "start_us": 7_000_000, "end_us": 8_000_000,
                "confidence": 0.94,
            }],
            query="女性连续向上爬楼梯",
            usage_role="pain_point", product_names=[], visual_intent="exercise_action",
            required_elements=["爬楼梯"], duration_us=3_000_000,
            usable_start_us=0, usable_end_us=9_000_000,
        )
        self.assertIsNone(match)

    def test_explicit_storyboard_intent_uses_catalog_evidence(self):
        self.assertTrue(media_library._visual_intent_matches(
            "展示资质", "镜头特写药盒右上角OTC标识", ["赛乐赛"],
            visual_intent="otc_certification", roles=[],
        )["intent:otc_certification"])
        self.assertFalse(media_library._visual_intent_matches(
            "展示资质", "赛乐赛药盒摆放在桌面", ["赛乐赛"],
            visual_intent="otc_certification", roles=["product_beauty"],
        )["intent:otc_certification"])
        self.assertFalse(media_library._visual_intent_matches(
            "展示资质", "普通赛乐赛药盒近景摆拍，包装自然带有OTC文字", ["赛乐赛"],
            visual_intent="otc_certification", roles=["otc_certification"],
        )["intent:otc_certification"])
        self.assertTrue(media_library._visual_intent_matches(
            "展示产品", "赛乐赛药盒普通摆放在桌面", ["赛乐赛"],
            visual_intent="product_overview", roles=["product_beauty"],
        )["intent:product_overview"])

        self.assertTrue(media_library._visual_intent_matches(
            "展示溯源", "包装二维码静态清晰出现", ["赛乐赛"],
            visual_intent="qr_code_visible", roles=["product_beauty"],
        )["intent:qr_code_visible"])
        self.assertFalse(media_library._visual_intent_matches(
            "展示溯源", "镜头展开药品说明书", ["赛乐赛"],
            visual_intent="qr_code_visible", roles=["product_beauty"],
        )["intent:qr_code_visible"])

    def test_every_visual_intent_requires_content_evidence_not_role_labels(self):
        cases = {
            "pain_point": "肥胖人物走路后气喘，停下扶膝",
            "product_overview": "赛乐赛产品盒摆放在桌面，完整展示包装",
            "product_detail": "镜头特写产品盒包装标签和规格文字",
            "instruction_manual": "镜头清晰展示药品说明书上的使用文字",
            "otc_certification": "手指明确指向药盒上的OTC红色标识",
            "qr_traceability": "镜头近景展示包装二维码，手指指向二维码",
            "authenticity_comparison": "正品与假货进行真假对比辨别",
            "usage_instruction": "说明书清晰标明每日随餐服用用量",
            "medication_moment": "人物拿起胶囊送入口中并喝水吞服",
            "factory_process": "仓库工作人员正在整理包裹并发货",
            "service_support": "客服人员通过电话进行一对一指导",
            "body_condition": "人物腰腹赘肉和体态清晰可见",
            "exercise_action": "人物连续跑步锻炼，动作持续发生",
            "mechanism_explanation": "动画示意脂肪分解机制和作用过程",
            "lifestyle_scene": "办公室里的人物在餐桌旁进餐",
            "experience_proof": "用户分享减重前后身材变化和体验反馈",
            "cta": "手机打开商品链接并点击购买下单",
        }
        unrelated = "人物在室内静止站立，没有其他可辨认内容"
        for intent, evidence in cases.items():
            with self.subTest(intent=intent):
                self.assertTrue(media_library._explicit_visual_intent_matches(
                    intent, media_library._normalized_text(evidence), [],
                ))
                self.assertFalse(media_library._explicit_visual_intent_matches(
                    intent, media_library._normalized_text(unrelated), [intent],
                ))
        self.assertFalse(media_library._visual_intent_matches(
            "展示产品", "一名人物对镜头讲解", ["赛乐赛"],
            visual_intent="product_overview", roles=["product_overview"],
        )["intent:product_overview"])

    def test_medication_moment_requires_a_real_intake_action(self):
        self.assertTrue(media_library._visual_intent_matches(
            "随餐吃一粒", "人物拿起胶囊放入口中并喝水吞服", ["赛乐赛"],
            visual_intent="medication_moment", roles=["scene_empty_scene"],
        )["intent:medication_moment"])
        self.assertFalse(media_library._visual_intent_matches(
            "随餐吃一粒", "手持胶囊和药盒进行静态展示", ["赛乐赛"],
            visual_intent="medication_moment", roles=["medication_moment"],
        )["intent:medication_moment"])
        self.assertFalse(media_library._visual_intent_matches(
            "随餐吃一粒", "说明书文字标注随餐服用", ["赛乐赛"],
            visual_intent="medication_moment", roles=["usage_instruction"],
        )["intent:medication_moment"])

    def test_negated_forbidden_element_does_not_reject_positive_otc_evidence(self):
        self.assertFalse(media_library._forbidden_element_matches(
            "非OTC标识", "手指明确指向药盒上的红色OTC标识",
        ))
        self.assertTrue(media_library._forbidden_element_matches(
            "非OTC标识", "包装上没有OTC标识",
        ))

    def test_scan_and_trace_elements_accept_qr_or_instructions(self):
        self.assertTrue(media_library._element_matches(
            "手机扫码动作", "包装盒背面的二维码清晰出现",
        ))
        self.assertTrue(media_library._element_matches(
            "溯源码可查验", "镜头展示药品说明书和使用说明",
        ))

    def test_visual_intent_boost_prioritizes_matching_action(self):
        generic = media_library._candidate_match_score(
            similarity=0.05, element_coverage=0,
            product_identity_match=True, usage_role="product_empty_scene",
        )
        matching = media_library._candidate_match_score(
            similarity=0.20, element_coverage=1.0,
            product_identity_match=True, usage_role="product_empty_scene",
            visual_intent_score=1.0,
        )
        self.assertLess(generic, 0.58)
        self.assertGreaterEqual(matching, 0.58)

    def test_semantic_score_rejects_low_evidence_and_empty_required_elements(self):
        related = media_library._candidate_match_score(
            similarity=0.20, element_coverage=1 / 3,
            product_identity_match=True, usage_role="product_empty_scene", visual_intent_score=1.0,
        )
        unrelated = media_library._candidate_match_score(
            similarity=0.05, element_coverage=0,
            product_identity_match=True, usage_role="product_empty_scene", visual_intent_score=0,
        )
        self.assertLess(related, 0.58)
        self.assertLess(unrelated, 0.58)

    def test_semantic_overlap_is_symmetric_not_candidate_coverage(self):
        query = "长期肥胖的人物在生活中遇到行动困难和身材困扰"
        unrelated_short_summary = "人物生活"
        self.assertLess(
            media_library._semantic_similarity(query, unrelated_short_summary), 0.35,
        )

    def test_short_material_is_rejected_and_long_material_is_cut_to_target(self):
        near = {"usable_start_us": 0, "usable_end_us": 6_385_465}
        self.assertFalse(media_library._fit_clip_range(near, 6_480_000))
        long = {"usable_start_us": 500_000, "usable_end_us": 8_000_000}
        self.assertTrue(media_library._fit_clip_range(long, 6_480_000))
        self.assertEqual(long["clip_start_us"], 500_000)
        self.assertEqual(long["clip_end_us"], 6_980_000)
        self.assertEqual(long["source_duration_us"], 6_480_000)
        self.assertEqual(long["playback_rate"], 1.0)

    def test_otc_review_requires_an_explicit_emphasis_action(self):
        target = {"visual_intent": "otc_certification"}
        self.assertFalse(material_review._strict_evidence_ok(target, {
            "evidence": ["包装盒上可以自然看到OTC文字"],
        }))
        self.assertTrue(material_review._strict_evidence_ok(target, {
            "evidence": ["黄色指示棒明确指向红色OTC标识"],
        }))
        self.assertFalse(material_review._strict_evidence_ok(target, {
            "otc_text_clear": True, "otc_emphasis_action": "none",
            "evidence": ["普通产品摆拍中自然出现OTC"],
        }))
        self.assertTrue(material_review._strict_evidence_ok(target, {
            "otc_text_clear": True, "otc_emphasis_action": "magnifier",
            "evidence": ["放大镜放大OTC文字"],
        }))

    def test_medication_review_requires_a_real_intake_action(self):
        target = {"visual_intent": "medication_moment"}
        self.assertFalse(material_review._strict_evidence_ok(target, {
            "evidence": ["人物手持药盒并展示一粒胶囊"],
        }))
        self.assertFalse(material_review._strict_evidence_ok(target, {
            "evidence": ["说明书上写着随餐服用"],
        }))
        self.assertTrue(material_review._strict_evidence_ok(target, {
            "evidence": ["人物拿起胶囊送入口中并喝水吞服"],
        }))

    def test_qr_material_review_requires_visibility_but_not_scan_action(self):
        target = {"visual_intent": "qr_code_visible", "category": "product_empty_scene"}
        self.assertTrue(material_review._strict_evidence_ok(target, {
            "evidence": ["包装背面的二维码清晰出现"],
        }))
        self.assertTrue(material_review._strict_evidence_ok(target, {
            "evidence": ["包装背面的条码清晰出现"],
        }))
        self.assertFalse(material_review._strict_evidence_ok(target, {
            "evidence": ["镜头展示药品说明书，没有二维码"],
        }))
        self.assertFalse(material_review._strict_evidence_ok(target, {
            "evidence": ["镜头展示包装，条形码不可见"],
        }))

    def test_review_rejects_verified_window_shorter_than_target_duration(self):
        normalized = material_review._normalize(
            {"duration_us": 2_000_000}, [("A", {})],
            {"candidates": [{
                "candidate_id": "A", "matched": True, "confidence": 0.95,
                "best_start_ms": 500, "best_end_ms": 1200,
                "rotation_degrees": 270,
            }]},
            {"A": 5_000_000}, {"id": 1, "model": "test"}, "multi_frame",
        )
        self.assertFalse(normalized["A"]["matched"])
        self.assertEqual(normalized["A"]["rotation_degrees"], 270)

    def test_product_overview_does_not_misread_negated_otc_language(self):
        self.assertFalse(material_review._strict_evidence_ok(
            {"visual_intent": "product_overview"},
            {"evidence": ["OTC只是自然出现，没有指向、放大或近景强调动作"]},
        ))
        self.assertTrue(material_review._strict_evidence_ok(
            {"visual_intent": "product_overview"},
            {"evidence": ["赛乐赛产品盒摆放在桌面，镜头完整展示包装正面"]},
        ))

    def test_review_requires_all_specific_elements_for_any_visual_intent(self):
        target = {
            "category": "motion",
            "visual_intent": "exercise_action",
            "required_elements": ["上楼梯动作", "人物连续向上攀爬"],
        }
        self.assertFalse(material_review._strict_evidence_ok(target, {
            "evidence": ["人物站在楼梯口，扶着栏杆"]
        }))
        self.assertTrue(material_review._strict_evidence_ok(target, {
            "evidence": ["人物连续向上爬楼梯，脚步持续踏上台阶"]
        }))

    def test_product_and_mechanism_required_elements_use_any_match(self):
        product_target = {
            "category": "product_empty_scene", "visual_intent": "product_overview",
            "required_elements": ["产品包装盒", "人物指向包装"],
        }
        self.assertTrue(material_review._strict_evidence_ok(product_target, {
            "evidence": ["产品包装盒正面清晰摆放在桌上"]
        }))
        mechanism_target = {
            "category": "mechanism", "visual_intent": "mechanism_explanation",
            "required_elements": ["脂肪分解过程", "人物运动动作"],
        }
        self.assertTrue(material_review._strict_evidence_ok(mechanism_target, {
            "evidence": ["动画演示脂肪分解过程"]
        }))
        self.assertFalse(material_review._strict_evidence_ok({
            **product_target, "required_elements": ["药盒", "人物指向包装"],
        }, {"evidence": ["完全无关的街景"]}))

    def test_review_cache_signature_changes_with_visual_policy_inputs(self):
        target = {
            "visual_intent": "pain_point", "text": "走两步就喘",
            "required_elements": ["步行后气喘"],
            "visual_description": "人物短距离步行后出现气喘",
        }
        changed = {**target, "visual_description": "人物正在连续上楼梯"}
        self.assertNotEqual(
            material_review.target_signature(target),
            material_review.target_signature(changed),
        )

    def test_review_window_covers_full_short_feature_not_only_first_six_seconds(self):
        candidate = {"usable_start_us": 1_000_000, "usable_end_us": 9_000_000}
        with patch.dict("os.environ", {"SCRIPT_VIDEO_REVIEW_MAX_SECONDS": "20"}):
            self.assertEqual(material_review._review_duration_us(candidate, 2_000_000), 8_000_000)

    def test_localized_candidate_starts_at_the_verified_action(self):
        candidate = {"usable_start_us": 1_000_000, "usable_end_us": 9_000_000}
        result = {
            "matched": True, "best_start_ms": 3000, "best_end_ms": 4000,
            "vertical_fit": "smart_crop", "focus_x": 0.7, "focus_y": 0.4,
            "confidence": 0.95,
        }
        selected = material_review._localized_candidate(candidate, result, 2_000_000)
        self.assertEqual(selected["clip_start_us"], 4_000_000)
        self.assertEqual(selected["clip_end_us"], 6_000_000)
        self.assertEqual(selected["vertical_fit"], "smart_crop")

    def test_localized_candidate_does_not_shift_before_action_to_fill_duration(self):
        candidate = {"usable_start_us": 0, "usable_end_us": 5_000_000}
        result = {
            "matched": True, "best_start_ms": 4000, "best_end_ms": 5000,
            "vertical_fit": "cover", "focus_x": 0.5, "focus_y": 0.5,
            "confidence": 0.95,
        }
        self.assertIsNone(material_review._localized_candidate(candidate, result, 2_000_000))

    def test_vertical_renderer_supports_focus_crop_and_blurred_contain(self):
        smart = vertical_renderer.filter_args(
            width=1080, height=1920, duration_us=2_000_000,
            fit_mode="smart_crop", focus_x=0.75, focus_y=0.5,
        )
        self.assertEqual(smart[0], "-vf")
        self.assertIn("iw*0.7500", smart[1])
        blurred = vertical_renderer.filter_args(
            width=1080, height=1920, duration_us=2_000_000,
            fit_mode="contain_blur",
        )
        self.assertEqual(blurred[0], "-filter_complex")
        self.assertIn("gblur=sigma=30", blurred[1])

    def test_vertical_renderer_applies_orientation_correction_filters(self):
        clockwise = vertical_renderer.filter_args(
            width=1080, height=1920, duration_us=2_000_000,
            fit_mode="contain_blur", rotation_degrees=90,
        )
        upside_down = vertical_renderer.filter_args(
            width=1080, height=1920, duration_us=2_000_000,
            fit_mode="cover", rotation_degrees=180,
        )
        self.assertIn("transpose=1", clockwise[1])
        self.assertIn("hflip,vflip", upside_down[1])
        panel_filter, _ = manager._panel_video_filter(
            1080, 960, 2_000_000, "contain_blur", rotation_degrees=270,
        )
        self.assertIn("transpose=2", panel_filter)

    def test_local_media_mirror_resolves_by_size_and_hash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "asset" / "2026" / "10"
            folder.mkdir(parents=True)
            content = b"video-content"
            mirrored = folder / "product-different-timestamp.mp4"
            mirrored.write_bytes(content)
            clip = {
                "storage_path": "asset/2026/10/product-original.mp4",
                "file_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            with patch.dict("os.environ", {"MEDIA_LIBRARY_LOCAL_ROOT": str(root)}):
                self.assertEqual(media_library.resolve_local_source(clip), mirrored)

    def test_candidate_source_skips_unavailable_loopback_record(self):
        with patch.object(media_library, "resolve_local_source", return_value=None), \
                patch.dict("os.environ", {"MEDIA_LIBRARY_PUBLIC_BASE_URL": ""}), \
                patch.object(media_library, "_settings", return_value={}):
            self.assertIsNone(media_library._candidate_source_url({
                "access_url": "http://127.0.0.1/static/a.mp4",
                "storage_path": "asset/a.mp4",
            }))


if __name__ == "__main__":
    unittest.main()
