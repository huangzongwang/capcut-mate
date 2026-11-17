import tempfile
import unittest
import os
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


class AnalysisTaskStoreTests(unittest.TestCase):
    def test_task_lifecycle_persists(self):
        import config
        original = config.VIDEO_GEN_TASK_DB_PATH
        original_root = os.environ.get("VIDEO_ANALYSIS_STORAGE_ROOT")
        original_db_root = os.environ.get("VIDEO_ANALYSIS_NODE_DB_ROOT")
        try:
            with tempfile.TemporaryDirectory() as directory:
                config.VIDEO_GEN_TASK_DB_PATH = str(Path(directory) / "tasks.sqlite3")
                os.environ["VIDEO_ANALYSIS_STORAGE_ROOT"] = directory
                os.environ["VIDEO_ANALYSIS_NODE_DB_ROOT"] = directory
                from src.service.video_analysis import analysis_task_store as store
                store.ensure_schema()
                created = store.create_task("va_test", '{"video_path":"x.mp4"}')
                self.assertEqual(created["status"], "queued")
                with ThreadPoolExecutor(8) as executor:
                    claims = list(executor.map(lambda _: store.claim_task("va_test"), range(8)))
                self.assertEqual(sum(task is not None for task in claims), 1)
                updated = store.update_task(
                    "va_test", stage="ocr", progress=42
                )
                self.assertEqual(updated["stage"], "ocr")
                self.assertEqual(store.get_task("va_test")["progress"], 42)
                self.assertEqual(len(store.list_pending_tasks()), 1)
                store.update_task("va_test", stage="global_model", progress=85)
                store.update_task("va_test", stage="analyzing_scene_video", progress=60)
                latest = store.get_task("va_test")
                self.assertEqual(latest["progress"], 85)
                self.assertEqual(latest["stage"], "global_model")
                with ThreadPoolExecutor(8) as executor:
                    list(executor.map(
                        lambda index: store.merge_timings("va_test", event=f"e{index}"),
                        range(8),
                    ))
                events = json.loads(store.get_task("va_test")["timings_json"])["events"]
                self.assertEqual(sum(event.get("event", "").startswith("e") for event in events), 8)
        finally:
            config.VIDEO_GEN_TASK_DB_PATH = original
            if original_root is None:
                os.environ.pop("VIDEO_ANALYSIS_STORAGE_ROOT", None)
            else:
                os.environ["VIDEO_ANALYSIS_STORAGE_ROOT"] = original_root
            if original_db_root is None:
                os.environ.pop("VIDEO_ANALYSIS_NODE_DB_ROOT", None)
            else:
                os.environ["VIDEO_ANALYSIS_NODE_DB_ROOT"] = original_db_root

    def test_nodes_use_independent_task_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            from src.service.video_analysis import analysis_task_store as store
            from src.service.video_analysis import platform_runtime as runtime

            with patch.dict(os.environ, {
                "VIDEO_ANALYSIS_STORAGE_ROOT": directory,
                "VIDEO_ANALYSIS_NODE_DB_ROOT": directory,
                "VIDEO_ANALYSIS_NODE_ID": "server-a",
            }), patch.object(store.publisher, "publish"), patch.object(
                store.publisher, "get_shared", return_value=None,
            ):
                store.ensure_schema()
                store.create_task("va_owned_by_a", '{"video_path":"a.mp4"}')
                path_a = runtime.video_analysis_task_db()

            with patch.dict(os.environ, {
                "VIDEO_ANALYSIS_STORAGE_ROOT": directory,
                "VIDEO_ANALYSIS_NODE_DB_ROOT": directory,
                "VIDEO_ANALYSIS_NODE_ID": "server-b",
            }), patch.object(store.publisher, "get_shared", return_value=None):
                store.ensure_schema()
                path_b = runtime.video_analysis_task_db()
                self.assertIsNone(store.get_task("va_owned_by_a"))
                self.assertEqual(store.list_pending_tasks(), [])

            self.assertNotEqual(path_a, path_b)
            self.assertIn("server-a", path_a.parts)
            self.assertIn("server-b", path_b.parts)

    def test_different_nodes_can_hold_worker_locks_at_the_same_time(self):
        from src.service.video_analysis.analysis_task_manager import AnalysisTaskManager

        first, second = AnalysisTaskManager(), AnalysisTaskManager()
        with tempfile.TemporaryDirectory() as directory:
            try:
                with patch.dict(os.environ, {
                    "VIDEO_ANALYSIS_STORAGE_ROOT": directory,
                    "VIDEO_ANALYSIS_NODE_DB_ROOT": directory,
                    "VIDEO_ANALYSIS_NODE_ID": "server-a",
                }):
                    first._acquire_process_lock()
                with patch.dict(os.environ, {
                    "VIDEO_ANALYSIS_STORAGE_ROOT": directory,
                    "VIDEO_ANALYSIS_NODE_DB_ROOT": directory,
                    "VIDEO_ANALYSIS_NODE_ID": "server-b",
                }):
                    second._acquire_process_lock()
            finally:
                first._release_process_lock()
                second._release_process_lock()


if __name__ == "__main__":
    unittest.main()
