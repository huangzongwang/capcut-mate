"""Protocol and fallback contracts; all model/network calls are isolated."""
import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import requests
from pydantic import ValidationError

from src.schemas.video_analysis import AnalyzeVideoRequest
from src.service.video_analysis import gemini_client, model_client
from src.service.video_analysis import semantic_analysis as pipeline
from src.service.video_analysis.responses_client import Scope
from tests.test_video_file_analysis import judgement, prepared


def config(identifier, model="test", purpose="scene"):
    return {
        "id": identifier, "provider_id": identifier, "model": model,
        "provider": "test", "model_updated_at": "v1", "provider_updated_at": "v1",
        "base_url": "https://example.test/v1", "api_key": "private-test-key",
        "purpose": purpose,
    }


class GeminiTests(unittest.TestCase):
    def response(self, text, finish="STOP"):
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        response.iter_content.return_value = [json.dumps({
            "candidates": [{"finishReason": finish, "content": {
                "parts": [{"thought": True, "text": "not final"}, {"text": text}],
            }}],
            "usageMetadata": {"totalTokenCount": 12},
        }).encode()]
        return response

    def test_native_full_video_bytes_and_no_responses(self):
        scope = Scope()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"complete video bytes")
            with patch.object(gemini_client.requests, "post", return_value=self.response(
                "```json\n" + json.dumps(judgement()) + "\n```"
            )) as post, patch.object(model_client, "_request_json") as responses:
                result = model_client.analyze_scene_with_responses(
                    config(31, "gemini-2.5-flash-lite"),
                    {"index": 2}, "speech", str(path), str(path), scope=scope,
                )
            self.assertEqual(result["hook_score"], 75)
            responses.assert_not_called()
            self.assertEqual(post.call_args.args[0],
                             "https://example.test/v1beta/models/gemini-2.5-flash-lite:generateContent")
            parts = post.call_args.kwargs["json"]["contents"][0]["parts"]
            videos = [p["inlineData"] for p in parts if "inlineData" in p]
            self.assertEqual(len(videos), 2)
            self.assertEqual(base64.b64decode(videos[0]["data"]), path.read_bytes())
        self.assertEqual(scope.calls[0]["outcome"], "completed")
        self.assertNotIn("private-test-key", json.dumps(scope.calls))

    def test_invalid_json_and_truncation_are_failures(self):
        for text, finish in (("- bullet list", "STOP"), ('{"content":"partial"}', "MAX_TOKENS")):
            with self.subTest(text=text), patch.object(
                gemini_client.requests, "post", return_value=self.response(text, finish)
            ):
                scope = Scope()
                with self.assertRaises(ValueError):
                    gemini_client.request_json(config(31, "gemini-test"),
                                               [{"type": "input_text", "text": "test"}], scope=scope)
                self.assertEqual(scope.calls[0]["outcome"], "failed")

    def test_inline_limit_and_http_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"too big")
            limited = {**config(31, "gemini-test"), "metadata": {"gemini_max_inline_bytes": 1}}
            with patch.object(gemini_client.requests, "post") as post:
                with self.assertRaisesRegex(ValueError, "size limit"):
                    gemini_client.request_json(limited, [{"type": "input_video", "file_id": str(path)}])
                post.assert_not_called()
        response = self.response("{}")
        response.status_code = 503
        with patch.object(gemini_client.requests, "post", return_value=response):
            with self.assertRaisesRegex(requests.HTTPError, "503"):
                gemini_client.request_json(config(31), [{"type": "input_text", "text": "test"}])

    def test_default_inline_limit_allows_video_above_old_18mb_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(b"x" * 13_500_001)
            with patch.object(gemini_client.requests, "post", return_value=self.response(
                json.dumps({"ok": True})
            )) as post:
                result = gemini_client.request_json(
                    config(31, "gemini-test"),
                    [{"type": "input_video", "file_id": str(path)}],
                )
            self.assertEqual(result, {"ok": True})
            self.assertEqual(post.call_count, 1)

    def test_gpt_text_uses_responses_without_temperature(self):
        with patch.object(model_client, "_request_json", return_value={"content_type": "demo"}) as call:
            model_client.analyze_global_report_with_responses(config(16, "gpt-5.5", "global"), [], "", [])
        payload = call.call_args.args[1]
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["input"][0]["content"][0]["type"], "input_text")


class FallbackTests(unittest.TestCase):
    def run_pipeline(self, *, primary_error=None, backup_error=None, upload_error=False,
                     global_error=None, global_backup_error=None, primary_gemini=False,
                     backup_gemini=True):
        models = {
            "scene": config(1, "gemini-primary" if primary_gemini else "video-primary"),
            "scene_fallback": config(2, "gemini-backup" if backup_gemini else "video-backup"),
            "global": config(3, "gpt-primary", "global"),
            "global_fallback": config(4, "gpt-backup", "global"),
        }
        files_created = []

        def files_factory(selected):
            files = Mock()
            files.cleanup.return_value = []
            files.upload.side_effect = (
                requests.ConnectionError("upload failed")
                if upload_error and selected["id"] == 1
                else lambda path, fps: f"provider-{selected['id']}:{path}"
            )
            files_created.append(files)
            return files

        def scene_call(selected, scene, speech, current, previous, **kwargs):
            error = primary_error if selected["id"] == 1 else backup_error
            if error:
                if isinstance(error, dict):
                    return error
                raise error
            if selected["id"] == 2:
                if backup_gemini:
                    self.assertEqual(current, prepared()["clips"][scene["index"] - 1]["path"])
                else:
                    self.assertTrue(current.startswith("provider-2:"))
                    if previous:
                        self.assertTrue(previous.startswith("provider-2:"))
            return judgement()

        def global_call(selected, *args, **kwargs):
            error = global_error if selected["id"] == 3 else global_backup_error
            if error:
                if isinstance(error, dict):
                    return error
                raise error
            return {"content_type": "demo"}

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(pipeline, "analysis_temporary_directory", return_value=tempfile.TemporaryDirectory(dir=directory)), \
             patch.object(pipeline, "resolve_analysis_models", return_value=models), \
             patch.object(pipeline, "build_worker_input", return_value={}), \
             patch.object(pipeline, "_run_worker", AsyncMock(return_value=prepared())), \
             patch.object(pipeline, "VideoFiles", side_effect=files_factory), \
             patch.object(pipeline, "analyze_scene_with_responses", side_effect=scene_call) as scene_model, \
             patch.object(pipeline, "analyze_global_report_with_responses", side_effect=global_call) as global_model:
            result = asyncio.run(pipeline.analyze_video(AnalyzeVideoRequest(
                video_path="x.mp4", use_transcription=False, scene_model_id=1, global_model_id=3,
                scene_fallback_model_id=2, global_fallback_model_id=4,
            )))
        for files in files_created:
            files.cleanup.assert_called_once()
        return result, scene_model, global_model, files_created

    def test_success_never_calls_backup(self):
        result, scene, global_model, _ = self.run_pipeline()
        self.assertEqual(result.fallbacks, [])
        self.assertEqual([c.args[0]["id"] for c in scene.call_args_list], [1, 1])
        self.assertEqual(global_model.call_count, 1)
        self.assertEqual(result.global_model_used_id, 3)

    def test_scene_failure_uses_backup_after_http_or_invalid_output(self):
        for error in (requests.ReadTimeout("timeout"), {"content": "missing required fields"}):
            with self.subTest(error=error):
                result, _, _, _ = self.run_pipeline(primary_error=error)
                self.assertTrue(all(s.fallback_used and s.model_id == 2 for s in result.semantics))
                self.assertEqual(len(result.fallbacks), 2)
                self.assertTrue(all(e["succeeded"] for e in result.fallbacks))

    def test_upload_failure_uses_backup_video(self):
        result, scene, _, _ = self.run_pipeline(upload_error=True)
        self.assertTrue(all(s.analysis_status == "completed" for s in result.semantics))
        self.assertTrue(all(c.args[0]["id"] == 2 for c in scene.call_args_list))

    def test_gemini_to_files_backup_reuploads_and_cleans(self):
        result, _, _, files = self.run_pipeline(
            primary_gemini=True, backup_gemini=False,
            primary_error=requests.HTTPError("502"),
        )
        self.assertEqual(len(files), 2)
        self.assertTrue(all(s.model_id == 2 for s in result.semantics))

    def test_both_scene_models_fail_no_global_call(self):
        result, _, global_model, _ = self.run_pipeline(
            primary_error=requests.HTTPError("502"), backup_error=ValueError("not JSON"),
        )
        self.assertTrue(all(s.analysis_status == "failed" for s in result.semantics))
        self.assertTrue(all(not e["succeeded"] for e in result.fallbacks))
        global_model.assert_not_called()

    def test_global_fallback_and_all_failed_preserve_scenes(self):
        for backup_error in (None, requests.HTTPError("503")):
            with self.subTest(backup_error=backup_error):
                result, _, model, _ = self.run_pipeline(
                    global_error={"content_type": "demo", "shot_breakdown": None},
                    global_backup_error=backup_error,
                )
                self.assertEqual(model.call_count, 2)
                self.assertTrue(all(s.analysis_status == "completed" for s in result.semantics))
                self.assertEqual(result.global_model_used_id, 4 if backup_error is None else None)
                self.assertEqual(result.analysis_report["global_model_enriched"], backup_error is None)
                if backup_error:
                    self.assertEqual(result.analysis_report["step_1_global_genes"]["content_type"], "unknown")

    def test_request_and_queued_backup_binding(self):
        for fields in ({"scene_fallback_model_id": 1}, {"global_fallback_model_id": 3},
                       {"scene_fallback_model_id": 0}):
            with self.assertRaises(ValidationError):
                AnalyzeVideoRequest(video_path="x.mp4", scene_model_id=1, global_model_id=3, **fields)
        request = AnalyzeVideoRequest(video_path="x.mp4", scene_model_id=1, global_model_id=3,
                                      scene_fallback_model_id=2, global_fallback_model_id=4)
        expected = {"scene_fallback": {"revision": "backup-version"}}
        with patch.object(pipeline, "resolve_model", return_value={}) as resolve:
            result = pipeline.resolve_analysis_models(request, expected)
        self.assertEqual(set(result), {"scene", "global", "scene_fallback", "global_fallback"})
        self.assertEqual(resolve.call_args_list[2].args, (2, "scene", expected["scene_fallback"]))


if __name__ == "__main__":
    unittest.main()
