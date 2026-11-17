import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.service.video_analysis import analysis_task_store as store
from src.service.video_analysis.analysis_task_manager import AnalysisTaskManager


class AutoDraftTests(unittest.TestCase):
    def test_success_and_failure_preserve_report(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with patch.dict(os.environ, {
                    "VIDEO_ANALYSIS_STORAGE_ROOT": directory,
                    "VIDEO_ANALYSIS_NODE_DB_ROOT": directory,
                }):
                    store.ensure_schema()
                    for fail in (False, True):
                        task_id = f"test_{fail}"
                        store.create_task(task_id, json.dumps({
                            "video_path": "sample.mp4", "use_external_model": False,
                            "_model_bindings": {},
                        }))
                        result = SimpleNamespace(draft={}, warnings=[])
                        result.model_dump = lambda: {
                            "draft": result.draft, "warnings": result.warnings,
                            "analysis_report": {"content": "retained"},
                        }

                        async def build(identifier, embed_scene_clips=False):
                            self.assertTrue(embed_scene_clips)
                            task = store.get_task(identifier)
                            self.assertEqual(task["stage"], "creating_draft")
                            self.assertTrue(Path(task["result_path"]).is_file())
                            if fail:
                                raise ValueError("test failure")
                            return {"draft_url": "http://localhost/draft", "draft_id": "test"}

                        manager = AnalysisTaskManager()
                        with patch.object(manager, "_analyze", AsyncMock(return_value=result)), patch(
                            "src.service.video_analysis.draft_builder.create_draft_from_analysis",
                            side_effect=build,
                        ):
                            manager._execute(task_id)
                        task = store.get_task(task_id)
                        saved = json.loads(Path(task["result_path"]).read_text(encoding="utf-8"))
                        self.assertEqual(saved["analysis_report"]["content"], "retained")
                        self.assertEqual(task["status"], "completed_with_warnings" if fail else "completed")
                        self.assertEqual(bool(saved["draft"]), not fail)
                        self.assertIn("draft_create_seconds", json.loads(task["timings_json"]))
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
