import unittest
import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.grok_video.client import submit
from src.grok_video.schemas import GrokVideoTaskRequest
from src.grok_video import store, client, model
from src.grok_video.manager import GrokVideoManager
from src.grok_video.prompt import render


class GrokVideoTests(unittest.TestCase):
    def test_poll_202_then_done_and_preserve_terminal_states(self):
        config = {"base_url": "https://example.test/v1", "api_key": "test"}
        for payload in ({"status": "pending"}, {}, None):
            pending = MagicMock(status_code=202, content=b"{}" if payload is not None else b"")
            pending.json.return_value = payload
            pending.__enter__.return_value = pending
            done = MagicMock(status_code=200)
            done.json.return_value = {"status": "done", "video": {"duration": 3}}
            done.__enter__.return_value = done
            stop = MagicMock()
            stop.is_set.return_value = stop.wait.return_value = False
            with patch.object(client.requests, "get", side_effect=[pending, done]) as get:
                result = client.wait(config, "existing_id", lambda status: None, stop, store._now())
                self.assertEqual(result["status"], "done")
                self.assertEqual(get.call_count, 2)
                stop.wait.assert_called_once()
        failed = MagicMock(status_code=202, content=b"{}")
        failed.json.return_value = {"status": "failed"}
        failed.__enter__.return_value = failed
        with patch.object(client.requests, "get", return_value=failed):
            with self.assertRaisesRegex(client.GrokVideoClientError, "task failed"):
                client.wait(config, "existing_id", lambda status: None, stop, store._now())

    def test_submit_202_requires_request_id_and_does_not_retry(self):
        request = dict(model="grok-imagine-video-1.5", prompt="p", image={"url": "x"},
                       duration=3, resolution="480p", aspect_ratio="9:16", n=1)
        with patch.object(client.requests, "post") as post:
            post.return_value.status_code = 202
            post.return_value.json.return_value = {"request_id": "task_accepted"}
            self.assertEqual(client.submit(
                {"base_url": "https://example.test/v1", "api_key": "test"}, request), "task_accepted")
            post.assert_called_once()
            post.return_value.json.return_value = {}
            with self.assertRaises(client.SubmissionUnknown):
                client.submit({"base_url": "https://example.test/v1", "api_key": "test"}, request)

    def test_defaults_and_duration_contract(self):
        request = GrokVideoTaskRequest(
            model_id=14, prompt="真人产品口播", image_url="https://example.test/a.png"
        )
        self.assertEqual(request.aspect_ratio, "9:16")
        self.assertEqual(request.duration, 15)
        with self.assertRaises(ValueError):
            GrokVideoTaskRequest(
                model_id=14, prompt="x", image_url="https://example.test/a.png", duration=16
            )

    @patch("src.grok_video.client.requests.post")
    def test_submit_uses_documented_video_payload(self, post):
        post.return_value.json.return_value = {"request_id": "task_1"}
        post.return_value.status_code = 200
        post.return_value.raise_for_status.return_value = None
        request = {
            "model": "grok-imagine-video-1.5",
            "prompt": "prompt",
            "duration": 12,
            "resolution": "720p",
            "aspect_ratio": "9:16",
            "image": {"url": "https://example.test/a.png"},
            "n": 1,
        }
        self.assertEqual(
            submit({"base_url": "https://zetarouter.com", "api_key": "secret"}, request),
            "task_1",
        )
        self.assertEqual(post.call_args.kwargs["json"], request)
        self.assertNotIn("size", post.call_args.kwargs["json"])

    def test_template_and_invalid_inputs(self):
        request = GrokVideoTaskRequest(
            model_id=14, image_url="https://example.test/a.png", duration=12,
            person_description="Adult woman, short hair", product_name="Product",
            script="中文口播", display_mode="handheld",
        )
        text = render(request)
        self.assertIn("12-second", text)
        self.assertIn("中文口播", text)
        self.assertIn("any other hand gesture", text)
        self.assertIn("single pickup", text)
        self.assertIn("realistic vertical talking-head", text)
        self.assertIn("Do not force a magenta blazer", text)
        self.assertIn("single pickup", text)
        self.assertNotIn("deep red hanging curtain", text)
        self.assertNotIn("Stand behind the white table", text)
        for changes in (
            {"duration": True}, {"duration": 17}, {"size": "720p"},
            {"image_url": "file:///secret"}, {"image_url": "http://127.0.0.1/a"},
            {"image_url": "data:image/png;base64,abc"}, {"model_id": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                GrokVideoTaskRequest.model_validate({**request.model_dump(), **changes})

    def test_unknown_submission_is_not_retried(self):
        request = dict(model="grok-imagine-video-1.5", prompt="p", image={"url": "x"},
                       duration=15, resolution="720p", aspect_ratio="9:16", n=1)
        with patch.object(client.requests, "post", side_effect=client.requests.Timeout("private-secret")) as post:
            with self.assertRaises(client.SubmissionUnknown) as error:
                client.submit({"base_url": "https://example.test/v1", "api_key": "secret"}, request)
            self.assertEqual(post.call_count, 1)
            self.assertNotIn("private-secret", str(error.exception))

    def test_model_adapter_uses_online_id_and_rejects_analysis_model(self):
        from test_video_model_config import row
        value = row(model="grok-imagine-video-1.5", base_url="https://example.test",
                    metadata='{"protocol":"grok_video"}')
        with patch.object(model.model_config_store, "_rows", return_value=[value]) as rows, \
             patch.object(model.model_config_store, "_decrypt", return_value="private-secret"):
            config = model.resolve(14)
            rows.assert_called_once_with(14, include_secret=True)
            self.assertEqual(config["base_url"], "https://example.test/v1")
            self.assertNotIn("private-secret", json.dumps(config["snapshot"]))
            with self.assertRaises(model.GrokVideoModelError):
                model.resolve(14, {"id": 99})
        with patch.object(model.model_config_store, "_rows", return_value=[row()]):
            with self.assertRaises(model.GrokVideoModelError):
                model.resolve(14)


class AppIntegrationTests(unittest.TestCase):
    def test_default_entrypoint_registers_routes_and_worker_once(self):
        from main import app
        from grok_video_app import app as compatibility_app
        from src.grok_video.manager import manager
        from src.service.video_analysis.analysis_task_manager import manager as analysis_manager
        from src.service.video_analysis.storyboard.task_manager import manager as storyboard_manager

        self.assertIs(app, compatibility_app)
        paths = app.openapi()["paths"]
        prefix = "/openapi/capcut-mate/v1/grok_video_tasks"
        for suffix, method in (
            ("", "post"), ("/{task_id}", "get"), ("/{task_id}/result", "get"),
            ("/{task_id}/content", "get"), ("/{task_id}/retry_download", "post"),
            ("/{task_id}/first_frame", "get"), ("/{task_id}/confirm_first_frame", "post"),
            ("/{task_id}/retry_cached_stage", "post"),
        ):
            self.assertIn(method, paths[prefix + suffix])

        async def run_lifespan():
            async with app.router.lifespan_context(app):
                start.assert_called_once_with()
                stop.assert_not_called()

        with patch.object(manager, "start") as start, \
             patch.object(manager, "stop") as stop, \
             patch.object(analysis_manager, "start"), \
             patch.object(analysis_manager, "stop"), \
             patch.object(storyboard_manager, "start"), \
             patch.object(storyboard_manager, "stop"), \
             patch("src.utils.draft_cleanup.draft_cleanup_background_loop", new_callable=AsyncMock), \
             patch("src.utils.deferred_delete.deferred_delete_background_loop", new_callable=AsyncMock):
            asyncio.run(run_lifespan())
            stop.assert_called_once_with()


class TaskFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "GROK_VIDEO_DB_PATH": str(Path(self.temp.name) / "tasks.sqlite3"),
            "GROK_VIDEO_RESULT_ROOT": str(Path(self.temp.name) / "results"),
        })
        self.env.start()
        store.ensure_schema()
        self.manager = GrokVideoManager()
        self.config = dict(model="grok-imagine-video-1.5", snapshot={"id": 14})
        self.request = GrokVideoTaskRequest(
            model_id=14, prompt="p", image_url="https://example.test/product.png",
        )

    def tearDown(self):
        self.manager.stop()
        self.env.stop()
        self.temp.cleanup()

    def enqueue(self):
        with patch.object(model, "resolve", return_value=self.config), \
             patch.object(self.manager, "start"):
            task = self.manager.submit(self.request)
        self.assertEqual(store.claim()["task_id"], task["task_id"])
        self.assertIsNone(store.claim())
        return task["task_id"]

    def test_submit_claim_complete_result_and_download(self):
        from fastapi import HTTPException
        from src.grok_video.router import result, content, status
        task_id = self.enqueue()
        video = b"\x00\x00\x00\x18ftypmp42" + b"mock-video"

        def download(config, request_id, target, stop):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(video)

        with patch.object(model, "resolve", return_value=self.config), \
             patch.object(client, "submit", return_value="upstream_1") as submit, \
             patch.object(client, "wait", return_value={"video": {"duration": 15}}), \
             patch.object(client, "download", side_effect=download), \
             patch("src.grok_video.manager.ensure_geometry", return_value={"width": 720, "height": 1280}):
            self.manager._execute(task_id)
            submit.assert_called_once()
        self.assertEqual(store.get(task_id)["status"], "completed")
        self.assertEqual(result(task_id).status, "completed")

        async def consume():
            return b"".join([chunk async for chunk in content(task_id).body_iterator])

        self.assertEqual(asyncio.run(consume()), video)
        with self.assertRaises(HTTPException) as error:
            status("missing")
        self.assertEqual(error.exception.status_code, 404)

    def test_resume_and_download_retry_never_generate_again(self):
        task_id = self.enqueue()
        store.update(task_id, provider_request_id="upstream_1", started_at=store._now())
        with patch.object(model, "resolve", return_value=self.config), \
             patch.object(client, "submit") as submit, \
             patch.object(client, "wait", return_value={"video": {"duration": 15}}), \
             patch.object(client, "download", side_effect=OSError("private-secret")):
            self.manager._execute(task_id)
            submit.assert_not_called()
        self.assertEqual(store.get(task_id)["status"], "download_failed")
        self.assertNotIn("private-secret", store.get(task_id)["error_message"])
        self.assertTrue(store.retry_download(task_id))
        store.claim()
        with patch.object(model, "resolve", return_value=self.config), \
             patch.object(client, "submit") as submit, \
             patch.object(client, "wait") as wait, \
             patch.object(client, "download"), \
             patch("src.grok_video.manager.ensure_geometry", return_value={"width": 720, "height": 1280}):
            self.manager._execute(task_id)
            submit.assert_not_called()
            wait.assert_not_called()
        self.assertEqual(store.get(task_id)["status"], "completed")

    def test_crash_during_submit_is_unknown_and_worker_lock_is_exclusive(self):
        task_id = self.enqueue()
        store.update(task_id, stage="submitting")
        with patch.object(self.manager, "_execute") as execute:
            self.manager.start()
            self.assertEqual(store.get(task_id)["status"], "submission_unknown")
            execute.assert_not_called()
            other = GrokVideoManager()
            with self.assertRaises(RuntimeError):
                other.start()


if __name__ == "__main__":
    unittest.main()
