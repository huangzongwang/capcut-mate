import asyncio
import base64
from contextlib import ExitStack, closing
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
import requests

from src.grok_video import client, first_frame, model, store, product_image
from src.grok_video.manager import GrokVideoManager
from src.grok_video.schemas import GrokVideoTaskRequest
from src.grok_video.prompt import render


def encoded_image(size=(360, 640)):
    output = io.BytesIO()
    Image.new("RGB", size, "green").save(output, format="PNG")
    return {"b64_json": base64.b64encode(output.getvalue()).decode()}


class FirstFrameTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "GROK_VIDEO_DB_PATH": str(Path(self.temp.name) / "tasks.sqlite3"),
            "GROK_VIDEO_RESULT_ROOT": str(Path(self.temp.name) / "results"),
        })
        self.env.start()
        store.ensure_schema()
        self.manager = GrokVideoManager()
        self.request = GrokVideoTaskRequest(
            model_id=27, image_model_id=28, product_image_url="https://example.test/product.png",
            first_frame_mode="auto", prompt="A presenter talks about the product", duration=3,
        )
        self.config = dict(model="grok-imagine-video-1.5", snapshot={"id": 27})
        self.image_config = dict(model="user-selected-image-model", snapshot={"id": 28},
                                 api_key="secret", base_url="https://example.test/v1",
                                 request_timeout_seconds=180)

    def tearDown(self):
        self.manager.stop()
        self.env.stop()
        self.temp.cleanup()

    def resolve(self, model_id, expected=None, *, image=False):
        self.assertEqual(model_id, 28 if image else 27)
        return self.image_config if image else self.config

    def enqueue(self):
        with patch.object(model, "resolve", side_effect=self.resolve), patch.object(self.manager, "start"):
            task = self.manager.submit(self.request)
        store.claim()
        return task["task_id"]

    def execute(self, task_id):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(model, "resolve", side_effect=self.resolve))
        generate = stack.enter_context(patch.object(first_frame, "generate", return_value=encoded_image()))
        submit = stack.enter_context(patch.object(client, "submit", return_value="video_1"))
        stack.enter_context(patch.object(client, "wait", return_value={"video": {"duration": 3}}))
        stack.enter_context(patch.object(client, "download"))
        stack.enter_context(patch("src.grok_video.manager.ensure_geometry", return_value={"width": 720, "height": 1280}))
        self.manager._execute(task_id)
        stack.close()
        return generate, submit

    def test_product_to_scene_to_video_and_frame_download(self):
        from src.grok_video.router import first_frame_content, result
        task_id = self.enqueue()
        generate, submit = self.execute(task_id)
        self.assertEqual(store.get(task_id)["status"], "completed")
        generate.assert_called_once()
        sent = submit.call_args.args[1]
        self.assertTrue(sent["image"]["url"].startswith("data:image/png;base64,"))
        self.assertNotEqual(sent["image"]["url"], self.request.product_image_url)
        self.assertEqual(result(task_id).result["first_frame"]["width"], 720)

        async def read():
            return b"".join([chunk async for chunk in first_frame_content(task_id).body_iterator])
        with Image.open(io.BytesIO(asyncio.run(read()))) as image:
            self.assertEqual(image.size, (720, 1280))

    def test_confirmation_gates_video_and_is_atomic(self):
        from fastapi import HTTPException
        from src.grok_video.router import confirm_first_frame
        self.request.confirm_first_frame = True
        task_id = self.enqueue()
        _, submit = self.execute(task_id)
        submit.assert_not_called()
        self.assertEqual(store.get(task_id)["status"], "awaiting_frame_confirmation")
        self.assertIsNone(store.claim())
        confirm_first_frame(task_id)
        with self.assertRaises(HTTPException):
            confirm_first_frame(task_id)
        store.claim()
        generate, submit = self.execute(task_id)
        generate.assert_not_called()
        submit.assert_called_once()
        self.assertEqual(store.get(task_id)["status"], "completed")

    def test_recover_saved_image_response_without_generating_again(self):
        task_id = self.enqueue()
        store.update(task_id, stage="image_submitting", image_response_json=json.dumps(encoded_image()))
        generate, submit = self.execute(task_id)
        generate.assert_not_called()
        submit.assert_called_once()
        self.assertEqual(store.get(task_id)["status"], "completed")

    def test_wrong_ratio_stops_before_video(self):
        task_id = self.enqueue()
        store.update(task_id, image_response_json=json.dumps(encoded_image((512, 512))))
        generate, submit = self.execute(task_id)
        generate.assert_not_called()
        submit.assert_not_called()
        task = store.get(task_id)
        self.assertEqual(task["status"], "first_frame_failed")
        self.assertIn("aspect ratio", task["error_message"])
        self.assertTrue(task["image_response_json"])

    def test_image_timeout_is_unknown_no_auto_retry(self):
        task_id = self.enqueue()
        with patch.object(model, "resolve", side_effect=self.resolve), \
             patch.object(first_frame, "generate", side_effect=client.SubmissionUnknown("unknown")), \
             patch.object(client, "submit") as submit:
            self.manager._execute(task_id)
        submit.assert_not_called()
        self.assertEqual(store.get(task_id)["stage"], "image_submission_unknown")
        self.assertIsNone(store.claim())
        self.assertFalse(store.retry_cached_stage(task_id))

    def test_known_video_failure_reuses_first_frame_on_explicit_retry(self):
        task_id = self.enqueue()
        with patch.object(model, "resolve", side_effect=self.resolve), \
             patch.object(first_frame, "generate", return_value=encoded_image()), \
             patch.object(client, "submit", side_effect=client.GrokVideoClientError("HTTP 429")):
            self.manager._execute(task_id)
        self.assertEqual(store.get(task_id)["status"], "failed")
        self.assertTrue(store.retry_cached_stage(task_id))
        self.assertFalse(store.retry_cached_stage(task_id))
        store.claim()
        generate, submit = self.execute(task_id)
        generate.assert_not_called()
        submit.assert_called_once()
        self.assertEqual(store.get(task_id)["status"], "completed")

    def test_first_frame_download_failure_never_repeats_paid_stages(self):
        task_id = self.enqueue()
        with patch.object(model, "resolve", side_effect=self.resolve), \
             patch.object(first_frame, "generate", return_value=encoded_image()), \
             patch.object(client, "submit", return_value="video_1"), \
             patch.object(client, "wait", return_value={"video": {"duration": 3}}), \
             patch.object(client, "download", side_effect=OSError("disk")):
            self.manager._execute(task_id)
        self.assertEqual(store.get(task_id)["status"], "download_failed")
        self.assertTrue(store.retry_download(task_id))
        store.claim()
        generate, submit = self.execute(task_id)
        generate.assert_not_called()
        submit.assert_not_called()
        self.assertIn("first_frame", json.loads(store.get(task_id)["result_json"]))

    def test_restart_during_image_submission_does_not_repeat(self):
        task_id = self.enqueue()
        store.update(task_id, stage="image_submitting")
        with patch.object(self.manager, "_execute") as execute:
            self.manager.start()
            self.assertEqual(store.get(task_id)["status"], "submission_unknown")
            execute.assert_not_called()
            self.manager.stop()

    def test_explicit_image_protocol_and_independent_binding(self):
        from test_video_model_config import row
        value = row(model="user-selected-image-model", metadata='{"protocol":"gpt_image_2"}',
                    base_url="https://example.test/v1", timeout_seconds=300)
        with patch.object(model.model_config_store, "_rows", return_value=[value]), \
             patch.object(model.model_config_store, "_decrypt", return_value="secret"):
            resolved = model.resolve(28, image=True)
            self.assertEqual(resolved["model"], "user-selected-image-model")
            self.assertEqual(resolved["request_timeout_seconds"], 300)
            with self.assertRaises(model.GrokVideoModelError):
                model.resolve(28, {"id": 99}, image=True)
            with self.assertRaises(model.GrokVideoModelError):
                model.resolve(28)
        for changes in ({"metadata": '{"protocol":"responses"}'},
                        {"metadata": '{"protocol":"grok_image_edit"}'}, {"timeout_seconds": 301}):
            value = row(**{"model": "gpt-image-2", "base_url": "https://example.test",
                           "metadata": '{"protocol":"gpt_image_2"}', **changes})
            with patch.object(model.model_config_store, "_rows", return_value=[value]):
                with self.assertRaises(model.GrokVideoModelError):
                    model.resolve(28, image=True)

    def test_image_edit_contract_and_ambiguous_responses(self):
        with patch.object(first_frame.requests, "post") as post:
            response = post.return_value
            response.status_code = 200
            response.json.return_value = {"data": [encoded_image()]}
            request = self.request.model_dump()
            request["product_image_url"] = "data:image/png;base64," + encoded_image()["b64_json"]
            callback = MagicMock()
            first_frame.generate(self.image_config, request, callback)
            callback.assert_called_once()
            payload = post.call_args.kwargs["data"]
            self.assertEqual(payload["model"], "user-selected-image-model")
            self.assertEqual(payload["response_format"], "b64_json")
            self.assertEqual(payload["size"], "720x1280")
            self.assertEqual(payload["quality"], "high")
            self.assertEqual(payload["output_format"], "png")
            self.assertNotIn("aspect_ratio", payload)
            self.assertNotIn("json", post.call_args.kwargs)
            self.assertNotIn("Content-Type", post.call_args.kwargs["headers"])
            filename, raw, mime = post.call_args.kwargs["files"]["image"]
            self.assertEqual((filename, mime), ("product.png", "image/png"))
            self.assertEqual(raw, base64.b64decode(encoded_image()["b64_json"]))
            wire = requests.Request("POST", post.call_args.args[0],
                                    data=payload, files=post.call_args.kwargs["files"]).prepare()
            self.assertIn("multipart/form-data; boundary=", wire.headers["Content-Type"])
            self.assertIn(b'name="image"; filename="product.png"', wire.body)
            self.assertIn("PRODUCT ASSET ONLY", payload["prompt"])
            self.assertTrue(post.call_args.args[0].endswith("/v1/images/edits"))
            for code in (202, 408, 502):
                response.status_code = code
                with self.assertRaises(client.SubmissionUnknown):
                    first_frame.generate(self.image_config, request)

    def test_invalid_product_prevents_paid_image_request(self):
        request = self.request.model_dump()
        request["product_image_url"] = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nbad").decode()
        with patch.object(first_frame.requests, "post") as post:
            with self.assertRaises(client.GrokVideoClientError):
                first_frame.generate(self.image_config, request)
            post.assert_not_called()

    def test_product_download_pins_ip_and_does_not_send_credentials(self):
        raw = base64.b64decode(encoded_image()["b64_json"])
        address = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch.object(product_image.socket, "getaddrinfo", return_value=address), \
             patch.object(product_image.urllib3, "HTTPSConnectionPool") as pool:
            response = pool.return_value.urlopen.return_value
            response.status = 200
            response.headers = {"Content-Length": str(len(raw))}
            response.read.side_effect = [raw, b""]
            loaded = product_image.load("https://example.test/product.png")
            self.assertEqual(loaded[1], raw)
            self.assertEqual(pool.call_args.kwargs["host"], "93.184.216.34")
            self.assertEqual(pool.call_args.kwargs["server_hostname"], "example.test")
            kwargs = pool.return_value.urlopen.call_args.kwargs
            self.assertEqual(kwargs["headers"]["Host"], "example.test")
            self.assertNotIn("Authorization", kwargs["headers"])
            self.assertFalse(kwargs["redirect"])
            response.close.assert_called_once()

    def test_product_download_blocks_private_dns_redirects_and_large_files(self):
        with patch.object(product_image.socket, "getaddrinfo",
                          return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]), \
             patch.object(product_image.urllib3, "HTTPSConnectionPool") as pool:
            with self.assertRaises(client.GrokVideoClientError):
                product_image.load("https://example.test/product.png")
            pool.assert_not_called()
        for status, headers in ((302, {}), (200, {"Content-Length": str(product_image.LIMIT + 1)})):
            with patch.object(product_image.socket, "getaddrinfo",
                              return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
                 patch.object(product_image.urllib3, "HTTPSConnectionPool") as pool:
                response = pool.return_value.urlopen.return_value
                response.status, response.headers = status, headers
                with self.assertRaises(client.GrokVideoClientError):
                    product_image.load("https://example.test/product.png")
                response.read.assert_not_called()

    def test_input_contract_and_prompt_separation(self):
        for changes in (
            {"image_url": "https://example.test/scene.png"},
            {"image_model_id": None}, {"first_frame_mode": None},
            {"first_frame_mode": "prompt"}, {"first_frame_prompt": "a scene"},
            {"product_image_url": "http://127.0.0.1/product.png"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                GrokVideoTaskRequest.model_validate({**self.request.model_dump(), **changes})
        request = GrokVideoTaskRequest.model_validate({
            **self.request.model_dump(), "prompt": None, "person_description": "Adult presenter",
            "product_name": "Product", "script": "Example",
            "first_frame_mode": "prompt", "first_frame_prompt": "A bright garden",
        })
        text = render(request)
        self.assertIn("Preserve the composed first frame", text)
        self.assertNotIn("magenta blazer", text)
        self.assertIn("A bright garden", first_frame.render({**request.model_dump(), "prompt": text}))

    def test_auto_scene_preserves_product_and_allows_diverse_adult_presenters(self):
        text = first_frame.render(self.request.model_dump())
        self.assertIn("ONE person actively introducing", text)
        self.assertIn("young, middle-aged or older adults", text)
        self.assertIn("product launch event", text)
        self.assertIn("outdoor", text)
        self.assertNotIn("adult female product presenter", text)
        self.assertIn("stable surface appropriate to the chosen setting", text)
        request = GrokVideoTaskRequest.model_validate({
            **self.request.model_dump(), "prompt": None,
            "person_description": "A 65-year-old man", "product_name": "Product",
            "script": "Example", "display_mode": "handheld",
        })
        video_prompt = render(request)
        image_prompt = first_frame.render({**request.model_dump(), "prompt": video_prompt})
        self.assertIn("A 65-year-old man", image_prompt)
        self.assertNotIn("female", video_prompt)
        self.assertNotIn("white display table", video_prompt)
        self.assertNotIn("Other identical products", video_prompt)
        self.assertIn("same hand and grip", video_prompt)

    def test_schema_migration_is_additive_and_idempotent(self):
        task_id = self.enqueue()
        with closing(sqlite3.connect(store.database_path())) as conn:
            for name in ("image_snapshot_json", "image_response_json", "first_frame_json"):
                conn.execute(f"ALTER TABLE grok_video_tasks DROP COLUMN {name}")
            conn.commit()
        store.ensure_schema()
        store.ensure_schema()
        self.assertEqual(store.get(task_id)["status"], "running")
        with closing(sqlite3.connect(store.database_path())) as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(grok_video_tasks)")}
        self.assertTrue({"image_snapshot_json", "image_response_json", "first_frame_json"} <= columns)


if __name__ == "__main__":
    unittest.main()
