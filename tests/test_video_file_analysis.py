import asyncio
import tempfile
import time
import unittest
import requests
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.schemas.video_analysis import AnalyzeVideoRequest
from src.service.video_analysis import semantic_analysis as pipeline
from src.service.video_analysis import model_client
from src.service.video_analysis.video_files import VideoFiles


def prepared():
    return {
        "detection": {
            "detector": "enhanced", "threshold": 3, "min_scene_len": 15, "fps": 25,
            "frame_count": 100, "duration_us": 4000000, "scene_count": 2, "warnings": [],
            "scenes": [
                {"index": i + 1, "start_frame": i * 50, "end_frame": (i + 1) * 50,
                 "start_us": i * 2000000, "end_us": (i + 1) * 2000000,
                 "duration_us": 2000000, "start_timecode": "00:00:00", "end_timecode": "00:00:02"}
                for i in range(2)
            ],
        },
        "clips": [{"scene_index": i + 1, "path": f"/tmp/{i}.mp4"} for i in range(2)],
    }


def judgement():
    return {"content": "Product demonstration", "replaceable": True,
            "scene_category": "product_empty_scene",
            "visual_intent": "product_overview", "primary_visual_subject": "product",
            "on_camera_speech": "absent",
            "replace_score": 0.8, "replace_reason": "Matching product required",
            "hook_score": 75, "hook_reason": "Visible action",
            "transition_type": "cut", "transition_reason": "Matching composition",
            "continuity_score": 0.8}


class PipelineTests(unittest.TestCase):
    def run_pipeline(self, side_effect):
        def response(config, scene, *args, **kwargs):
            value = side_effect[scene["index"] - 1]
            if isinstance(value, Exception):
                raise value
            return value
        files = Mock()
        files.upload.side_effect = ["file-a", "file-b"]
        files.cleanup.return_value = []
        config = {"id": 14, "model": "test", "enabled": True, "provider_id": 5,
                  "provider": "test", "model_updated_at": "v1", "provider_updated_at": "v1"}
        with patch.object(pipeline, "resolve_analysis_models", return_value={"scene": config, "global": config}), \
             patch.object(pipeline, "build_worker_input", return_value={}), \
             patch.object(pipeline, "_run_worker", new_callable=AsyncMock, return_value=prepared()) as worker, \
             patch.object(pipeline, "VideoFiles", return_value=files), \
             patch.object(pipeline, "analyze_scene_with_responses", side_effect=response) as model, \
             patch.object(pipeline, "analyze_global_report_with_responses",
                          return_value={"content_type": "demo"}):
            result = asyncio.run(pipeline.analyze_video(
                AnalyzeVideoRequest(video_path="x.mp4", use_transcription=False, use_ocr=True,
                                    scene_model_id=14, global_model_id=13)
            ))
        self.assertFalse(worker.call_args.args[0]["include_ocr"])
        self.assertFalse(worker.call_args.args[0]["include_sample_images"])
        files.cleanup.assert_called_once()
        self.last_model_call_count = model.call_count
        self.assertNotIn("_calls", config)
        self.assertNotIn("task_id", config)
        self.assertEqual(model.call_args_list[1].args[-2:], ("file-b", "file-a"))
        return result

    def test_model_judgements_and_previous_video(self):
        result = self.run_pipeline([judgement(), judgement()])
        self.assertEqual(result.visual_features, [])
        self.assertTrue(result.semantics[0].replaceable)
        self.assertEqual(result.viral_analysis.scene_scores[1], 75)
        self.assertEqual(result.transitions[0].transition_type, "cut")
        self.assertTrue(result.analysis_report["global_model_enriched"])

    def test_product_category_requires_product_as_primary_visual_subject(self):
        result = pipeline._apply_replacement_rules(
            pipeline.SceneSemanticAnalysis(
                scene_index=1, scene_category="product_empty_scene", visual_intent="mechanism_explanation",
                primary_visual_subject="mechanism_graphic", on_camera_speech="absent",
                replaceable=True, replace_score=0.95,
            )
        )
        self.assertEqual(result.scene_category, "unknown")
        self.assertFalse(result.replaceable)
        self.assertEqual(result.replacement_type, "需人工复核")
        self.assertIn("主视觉主体不是产品", result.replace_reason)

    def test_clear_visual_intent_corrects_wrong_coarse_category(self):
        result = pipeline._apply_replacement_rules(
            pipeline.SceneSemanticAnalysis(
                scene_index=1, scene_category="product_empty_scene", visual_intent="body_condition",
                primary_visual_subject="body", on_camera_speech="absent", replaceable=True,
            )
        )
        self.assertEqual(result.scene_category, "pain_point")
        self.assertTrue(result.replaceable)
        self.assertIn("修正为 pain_point", result.replace_reason)

    def test_category_evidence_does_not_override_model_replacement_constraints(self):
        result = pipeline._apply_replacement_rules(
            pipeline.SceneSemanticAnalysis(
                scene_index=1, scene_category="product_empty_scene", visual_intent="product_overview",
                primary_visual_subject="product", on_camera_speech="absent",
                replaceable=False, replace_score=0.3, replace_reason="动作和产品身份绑定",
            )
        )
        self.assertEqual(result.scene_category, "product_empty_scene")
        self.assertFalse(result.replaceable)
        self.assertEqual(result.replacement_type, "需人工复核")
        self.assertEqual(result.replace_score, 0.3)
        self.assertEqual(result.replace_reason, "动作和产品身份绑定")

    def test_transcript_without_visible_lip_sync_is_not_oral_delivery(self):
        result = pipeline._apply_replacement_rules(
            pipeline.SceneSemanticAnalysis(
                scene_index=1, scene_category="oral_delivery", visual_intent="service_support",
                primary_visual_subject="person", on_camera_speech="absent",
            )
        )
        self.assertEqual(result.scene_category, "unknown")
        self.assertFalse(result.replaceable)
        self.assertIn("不能仅凭字幕", result.replace_reason)

    def test_confirmed_on_camera_speech_cannot_be_replaced(self):
        result = pipeline._apply_replacement_rules(
            pipeline.SceneSemanticAnalysis(
                scene_index=1, scene_category="product_empty_scene", visual_intent="product_overview",
                primary_visual_subject="product", on_camera_speech="confirmed", replaceable=True,
            )
        )
        self.assertEqual(result.scene_category, "oral_delivery")
        self.assertFalse(result.replaceable)
        self.assertEqual(result.replacement_type, "不可替换")

    def test_intro_alone_does_not_authorize_replacement(self):
        result = pipeline._apply_replacement_rules(
            pipeline.SceneSemanticAnalysis(
                scene_index=1, scene_category="intro", primary_visual_subject="unknown",
                visual_intent="opening", on_camera_speech="uncertain", replaceable=True,
            )
        )
        self.assertEqual(result.scene_category, "unknown")
        self.assertFalse(result.replaceable)
        self.assertIn("叙事位置", result.replace_reason)

    def test_report_exposes_classification_evidence(self):
        result = self.run_pipeline([judgement(), judgement()])
        row = result.analysis_report["step_3_shot_breakdown"][0]
        self.assertEqual(row["visual_intent"], "product_overview")
        self.assertEqual(row["primary_visual_subject"], "product")
        self.assertEqual(row["on_camera_speech"], "absent")

    def test_failure_is_not_local_heuristic(self):
        result = self.run_pipeline([ValueError("failed"), judgement()])
        self.assertEqual(result.semantics[0].analysis_status, "failed")
        self.assertIsNone(result.semantics[0].hook_score)
        self.assertNotIn(1, result.viral_analysis.scene_scores)
        self.assertEqual(result.viral_analysis.analysis_status, "partial")

    def test_network_failure_is_not_retried_by_outer_pipeline(self):
        result = self.run_pipeline([requests.ReadTimeout("test timeout"), judgement()])
        self.assertEqual(self.last_model_call_count, 2)
        self.assertEqual(result.semantics[0].analysis_status, "failed")
        self.assertEqual(result.semantics[1].analysis_status, "completed")

    def test_no_images_in_request(self):
        with patch.object(model_client, "_request_json", return_value={}) as post:
            model_client.analyze_scene_with_responses(
                {"model": "test"}, {"index": 2}, "speech", "file-b", "file-a",
            )
        content = post.call_args.args[1]["input"][0]["content"]
        self.assertFalse(post.call_args.args[1]["store"])
        self.assertEqual([c["file_id"] for c in content if c["type"] == "input_video"],
                         ["file-a", "file-b"])
        self.assertFalse(any(c["type"] == "input_image" for c in content))
        instruction = content[0]["text"]
        self.assertIn("primary_visual_subject", instruction)
        self.assertIn("产品只在角落/底栏/贴图小窗露出", instruction)
        self.assertIn("口型与当前音轨同步", instruction)

    def test_replacement_matching_uses_specific_visual_intent(self):
        from src.service.video_analysis.replacement_drafts.manager import _match_target

        target = _match_target(
            {"shot_index": 1, "start_us": 0, "end_us": 2_000_000,
             "scene_category": "product_empty_scene", "visual_intent": "otc_certification",
             "content": "产品盒正面特写，手指指向OTC标识"},
            {"width": 1080, "height": 1920, "product_names": ["赛乐赛"]},
        )
        self.assertEqual(target["category"], "product_empty_scene")
        self.assertEqual(target["visual_intent"], "otc_certification")

    def test_processing_wait_and_cleanup(self):
        files = VideoFiles({"base_url": "https://example.invalid/api/v3/responses", "api_key": "test"})
        self.assertEqual(files.url, "https://example.invalid/api/v3/files")
        upload = Mock(status_code=200)
        upload.json.return_value = {"id": "file-a", "status": "processing"}
        active = Mock()
        active.json.return_value = {"id": "file-a", "status": "active"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.mp4"
            path.touch()
            with patch("src.service.video_analysis.video_files.requests.post", return_value=upload) as post, \
                 patch("src.service.video_analysis.video_files.requests.get", return_value=active), \
                 patch("src.service.video_analysis.video_files.time.sleep"), \
                 patch("src.service.video_analysis.video_files.requests.delete", return_value=Mock(status_code=204)):
                self.assertEqual(files.upload(path, 2), "file-a")
                self.assertGreater(int(post.call_args.kwargs["data"]["expire_at"]),
                                   time.time() + 3 * 86400)
                self.assertEqual(files.cleanup(), [])

    def test_failed_file_is_owned_for_cleanup(self):
        files = VideoFiles({"base_url": "https://example.invalid/api/v3", "api_key": "test"})
        response = Mock(status_code=200)
        response.json.return_value = {"id": "file-failed", "status": "failed"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.mp4"
            path.touch()
            with patch("src.service.video_analysis.video_files.requests.post", return_value=response):
                with self.assertRaises(ValueError):
                    files.upload(path, 2)
        self.assertEqual(files.owned_ids, ["file-failed"])

    def test_global_report_preserves_model_scores_and_times(self):
        result = self.run_pipeline([judgement(), judgement()])
        report = result.analysis_report
        pipeline._merge_global(report, {
            "content_type": "demo",
            "shot_breakdown": [{"shot_index": 1, "content": "updated", "start_us": 123,
                                "replaceable": False, "hook_score": 1},
                               {"shot_index": 999, "content": "invented"}],
        })
        shots = report["step_3_shot_breakdown"]
        self.assertEqual(len(shots), 2)
        self.assertEqual(shots[0]["start_us"], 0)
        self.assertTrue(shots[0]["replaceable"])
        self.assertEqual(shots[0]["hook_score"], 75)

    def test_disabled_model_skips_upload(self):
        with patch.object(pipeline, "build_worker_input", return_value={}), \
             patch.object(pipeline, "_run_worker", new_callable=AsyncMock, return_value=prepared()) as worker, \
             patch.object(pipeline, "VideoFiles") as files:
            result = asyncio.run(pipeline.analyze_video(
                AnalyzeVideoRequest(video_path="x.mp4", use_transcription=False, use_external_model=False)
            ))
        files.assert_not_called()
        self.assertFalse(worker.call_args.args[0]["use_external_model"])
        self.assertTrue(all(s.analysis_status == "disabled" for s in result.semantics))
        self.assertEqual(result.viral_analysis.analysis_status, "unavailable")


if __name__ == "__main__":
    unittest.main()
