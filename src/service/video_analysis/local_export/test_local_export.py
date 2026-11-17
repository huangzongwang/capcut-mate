from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import jianying_adapter, manager as manager_module, storage, store
from .schemas import CreateLocalExportTaskRequest


class LocalExportTests(unittest.TestCase):
    def test_request_rejects_duplicates_and_unsafe_ids(self):
        with self.assertRaises(ValueError):
            CreateLocalExportTaskRequest(draft_ids=["draft-a", "draft-a"])
        for value in ("../draft", "draft/name", "C:drive"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CreateLocalExportTaskRequest(draft_ids=[value])

    def test_storage_output_stays_inside_task_directory(self):
        with TemporaryDirectory() as directory, patch.object(
            storage, "export_root", return_value=Path(directory),
        ):
            path = storage.output_path("lex_" + "a" * 32, 1, "draft-1")
            self.assertTrue(path.is_relative_to(Path(directory).resolve()))
            self.assertEqual(path.name, "01-draft-1.mp4")

    def test_store_persists_task_and_items(self):
        with TemporaryDirectory() as directory, patch.object(
            store, "database_path", return_value=Path(directory) / "tasks.sqlite3",
        ):
            task_id = "lex_" + "b" * 32
            task = store.create(task_id, {
                "draft_ids": ["draft-1", "draft-2"],
                "resolution": "1080P", "framerate": 30,
                "timeout_seconds": 1800, "continue_on_error": True,
            })
            self.assertEqual(task["total_count"], 2)
            self.assertEqual([item["draft_id"] for item in task["items"]], ["draft-1", "draft-2"])
            self.assertTrue(store.claim_task(task_id))
            self.assertTrue(store.claim_item(task["items"][0]["item_id"]))
            store.update_item(
                task["items"][0]["item_id"], status="completed", stage="completed",
                progress=100, completed_at=store.now(), timings={"total_seconds": 1.2},
            )
            refreshed = store.refresh_task(task_id)
            self.assertEqual(refreshed["completed_count"], 1)
            self.assertEqual(refreshed["status"], "running")

    def test_item_export_retries_once_and_returns_local_url(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            draft_root = root / "drafts"
            draft_dir = draft_root / "draft-1"
            draft_dir.mkdir(parents=True)
            (draft_dir / "draft_content.json").write_text(
                json.dumps({"duration": 5_000_000}), encoding="utf-8",
            )
            calls = []

            def export(_draft_id, output_path, **_kwargs):
                calls.append(output_path)
                if len(calls) == 1:
                    raise RuntimeError("transient UI error")
                output_path.write_bytes(b"x" * 2048)

            item = {"item_id": "lei_test_01", "position": 1, "draft_id": "draft-1"}
            request = {
                "resolution": "1080P", "framerate": 30,
                "timeout_seconds": 1800, "continue_on_error": True,
            }
            with patch.object(manager_module.config, "DRAFT_SAVE_PATH", str(draft_root)), \
                    patch.object(storage, "export_root", return_value=root / "exports"), \
                    patch.object(storage, "public_base_url", return_value="http://host/output/local-exports"), \
                    patch.object(manager_module, "verify_local_draft_ready", return_value=SimpleNamespace(ok=True)), \
                    patch.object(manager_module.jianying_adapter, "export_draft", side_effect=export), \
                    patch.object(manager_module, "_validate_video", return_value={"duration_seconds": 5.0}), \
                    patch.object(manager_module.store, "update_item") as update:
                manager_module.LocalExportManager()._process_item(
                    "lex_" + "c" * 32, item, request,
                )
            self.assertEqual(len(calls), 2)
            completed = update.call_args.kwargs
            self.assertEqual(completed["status"], "completed")
            self.assertTrue(completed["video_url"].endswith("/01-draft-1.mp4"))
            self.assertTrue(Path(completed["output_path"]).is_file())

    def test_newer_jianying_version_is_not_supported(self):
        with patch.object(
            jianying_adapter, "_process_info",
            return_value=(True, r"D:\JianyingPro.exe", "7.9.0.12811"),
        ), patch.dict("sys.modules", {"uiautomation": object(), "pyautogui": object()}):
            result = jianying_adapter.inspect_environment()
        self.assertTrue(result["jianying_running"])
        self.assertFalse(result["supported_version"])
        self.assertIn("仅支持剪映专业版6.x及以下版本", result["warning"])


if __name__ == "__main__":
    unittest.main()

