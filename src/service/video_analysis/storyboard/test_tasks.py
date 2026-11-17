"""Offline queue, lifecycle and HTTP regression tests."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from . import planner, store, task_router, task_store
from .schemas import GenerateRequest
from .task_manager import TaskManager
from .test_storyboard import MODEL, content, model_response, model_shot


class TaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {
            "STORYBOARD_DB_PATH": str(self.root / "boards.sqlite3"),
            "STORYBOARD_RESULT_ROOT": str(self.root / "results"),
        })
        env.start()
        self.addCleanup(env.stop)
        model = patch.object(planner.model_config_store, "resolve_model", return_value=MODEL)
        model.start()
        self.addCleanup(model.stop)
        caller = patch.object(planner, "call_model", side_effect=model_response)
        self.calls = caller.start()
        self.addCleanup(caller.stop)
        self.manager = TaskManager()
        self.addCleanup(self.manager.stop)
        self.request = GenerateRequest(model_id=13, script="下班后去跑步。")

    def wait(self, task_id):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            task = task_store.get(task_id)
            if task["status"] in ("completed", "completed_with_warnings", "failed"):
                return task
            time.sleep(0.01)
        self.fail("Task did not finish")

    def test_submit_returns_before_model_and_only_one_worker_runs(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(4):
                raise RuntimeError("Test timeout")
            return model_response(*args)

        self.calls.side_effect = blocked
        first = self.manager.submit(self.request)
        self.assertTrue(entered.wait(2))
        try:
            second = self.manager.submit(self.request)
            self.assertEqual(first["status"], "queued")
            self.assertEqual(second["status"], "queued")
            self.assertEqual(self.calls.call_count, 1)
            self.assertEqual(task_store.get(second["task_id"])["status"], "queued")
        finally:
            release.set()
        self.assertEqual(self.wait(first["task_id"])["status"], "completed")
        self.assertEqual(self.wait(second["task_id"])["status"], "completed")
        self.assertEqual(self.calls.call_count, 2)

    def test_result_is_published_and_snapshot_survives_edit(self):
        task = self.wait(self.manager.submit(self.request)["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["progress"], 100)
        self.assertEqual(task["stage"], "waiting_review")
        self.assertTrue(Path(task["result_path"]).is_file())
        board = store.get(task["storyboard_id"])
        store.save(board, board.version)
        result = task_router.result(task["task_id"])
        self.assertEqual(result.result.version, 1)
        self.assertEqual(store.get(board.storyboard_id).version, 2)
        self.assertEqual(result.result.status, "needs_review")
        self.assertEqual(set(json.loads(task["timings_json"])),
                         {"planning_ms", "saving_ms"})

    def test_result_is_saved_pending_human_review_without_second_model_call(self):
        task = self.wait(self.manager.submit(self.request)["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(self.calls.call_count, 1)
        result = task_router.result(task["task_id"])
        self.assertFalse(result.result.shots[0].semantic_review.supported)
        self.assertEqual(
            result.result.shots[0].semantic_review.reason,
            "初稿未执行自动复核，等待人工确认",
        )
        self.assertEqual(result.result.shots[0].review_status, "pending")

    def test_missing_model_fails_and_worker_continues(self):
        with patch.object(planner.model_config_store, "resolve_model",
                          side_effect=planner.model_config_store.ModelConfigError("Unavailable")):
            with self.assertRaises(planner.ModelUnavailable):
                self.manager.submit(self.request)
        self.assertEqual(self.calls.call_count, 0)
        self.assertEqual(self.wait(self.manager.submit(self.request)["task_id"])["status"], "completed")

    def test_failed_stage_time_and_model_event_are_persisted(self):
        def failure(*args, **kwargs):
            kwargs["scope"].on_event({
                "event": "request_started", "call_id": "test-call",
                "task_id": kwargs["scope"].task_id, "attempt": 1,
            })
            time.sleep(0.03)
            raise planner.ModelUnavailable("test timeout")
        self.calls.side_effect = failure
        task = self.wait(self.manager.submit(self.request)["task_id"])
        self.assertEqual(task["status"], "failed")
        self.assertGreaterEqual(json.loads(task["timings_json"])["planning_ms"], 20)
        status = task_router.read(task["task_id"])
        self.assertEqual(status.model_request["call_id"], "test-call")
        self.assertEqual(status.model_request["task_id"], task["task_id"])

    def test_invalid_coverage_fails_without_model_repair(self):
        self.calls.side_effect = lambda *args, **kwargs: {
            "shots": [model_shot(content("错误原文"), 2)],
        }
        task = self.wait(self.manager.submit(self.request)["task_id"])
        self.assertEqual(task["status"], "failed")
        self.assertIsNone(task["storyboard_id"])
        self.assertEqual(self.calls.call_count, 1)

    def test_queued_tasks_resume_on_startup(self):
        task = task_store.create(self.request, planner.model_config_store.snapshot(MODEL))
        self.manager.start()
        self.assertEqual(self.wait(task["task_id"])["status"], "completed")

    def test_legacy_queue_fails_without_model_call(self):
        task = task_store.create(self.request, planner.model_config_store.snapshot(MODEL))
        with task_store.connection() as connection:
            connection.execute("UPDATE storyboard_tasks SET request_json=? WHERE task_id=?",
                               (json.dumps({"script": "old", "model_config_id": 2}), task["task_id"]))
        self.manager.start()
        failed = self.wait(task["task_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Legacy queued task", failed["error_message"])
        self.assertEqual(self.calls.call_count, 0)

    def test_interrupted_running_task_is_not_retried(self):
        task = task_store.create(self.request, planner.model_config_store.snapshot(MODEL))
        task_store.claim()
        self.manager.start()
        self.assertEqual(self.wait(task["task_id"])["status"], "failed")
        self.assertEqual(self.calls.call_count, 0)

    def test_saved_snapshot_is_recovered_without_model_or_duplicate_board(self):
        task = task_store.create(self.request, planner.model_config_store.snapshot(MODEL))
        task_store.claim()
        shots, model = planner.generate(self.request)
        board = task_store.save_board(task["task_id"], self.request, shots, model)
        self.calls.reset_mock()
        self.manager.start()
        recovered = self.wait(task["task_id"])
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["storyboard_id"], board.storyboard_id)
        self.assertEqual(self.calls.call_count, 0)
        with store.connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM storyboards").fetchone()[0], 1)

    def test_publication_failure_is_failed_not_completed(self):
        with patch("shutil.copyfile", side_effect=OSError("secret")):
            task = self.wait(self.manager.submit(self.request)["task_id"])
        self.assertEqual(task["status"], "failed")
        self.assertNotIn("secret", task["error_message"])
        self.assertIsNotNone(task["storyboard_id"])

    def test_ownership_lock_prevents_second_worker(self):
        self.manager.start()
        other = TaskManager()
        with self.assertRaises(RuntimeError):
            other.start()
        self.manager.stop()
        other.start()
        other.stop()

    def test_claim_is_conditional(self):
        task_store.create(self.request, planner.model_config_store.snapshot(MODEL))
        self.assertIsNotNone(task_store.claim())
        self.assertIsNone(task_store.claim())

    def test_http_routes_lifespan_validation_errors_and_openapi(self):
        from src.middlewares.response import ResponseMiddleware
        app = FastAPI()
        app.add_middleware(ResponseMiddleware)
        # Mirror main -> analysis router -> task router nesting.
        from fastapi import APIRouter
        parent = APIRouter(prefix="/v1")
        parent.include_router(task_router.router)
        app.include_router(parent, prefix="/openapi/capcut-mate")
        prefix = "/openapi/capcut-mate/v1/script_storyboard_tasks"
        async def call(method, path, body=None):
            messages = []
            sent = False

            async def receive():
                nonlocal sent
                if sent:
                    await asyncio.Event().wait()
                sent = True
                return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

            async def send(message):
                messages.append(message)

            await app({
                "type": "http", "asgi": {"version": "3.0"}, "method": method,
                "path": path, "raw_path": path.encode(), "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "scheme": "http", "server": ("test", 80), "client": ("test", 1),
                "root_path": "", "http_version": "1.1",
            }, receive, send)
            return json.loads(b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))

        async def check():
            async with app.router.lifespan_context(app):
                self.assertTrue(self.manager._thread.is_alive())
                self.assertEqual((await call("POST", prefix, {"script": ""}))["code"], 1001)
                self.assertEqual((await call("GET", prefix + "/missing"))["code"], 404)
                submitted = await call("POST", prefix, self.request.model_dump())
                self.assertEqual(submitted["code"], 0)
                task = self.wait(submitted["task_id"])
                status = await call("GET", prefix + "/" + task["task_id"])
                self.assertEqual(status["status"], "completed")
                result = await call("GET", prefix + "/" + task["task_id"] + "/result")
                self.assertEqual(result["code"], 0)
                self.assertEqual(result["result"]["request"]["script"], self.request.script)
                self.assertNotIn("api_key", json.dumps(result))
                Path(task["result_path"]).unlink()
                self.assertEqual((await call("GET", prefix + "/" + task["task_id"] + "/result"))["code"], 500)
                with task_store.connection() as conn:
                    conn.execute("UPDATE storyboard_tasks SET status='failed' WHERE task_id=?",
                                 (task["task_id"],))
                self.assertEqual((await call("GET", prefix + "/" + task["task_id"] + "/result"))["code"], 409)
                schema = app.openapi()
                self.assertIn(prefix, schema["paths"])
                for name, model in schema["components"]["schemas"].items():
                    if name not in ("HTTPValidationError", "ValidationError"):
                        for field in model.get("properties", {}).values():
                            self.assertTrue(field.get("description"))
        with patch.object(task_router, "manager", self.manager):
            asyncio.run(check())
        self.assertFalse(self.manager._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
