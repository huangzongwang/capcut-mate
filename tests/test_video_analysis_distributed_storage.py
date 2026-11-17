from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextlib import closing
from io import BytesIO
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from starlette.datastructures import UploadFile

from src.service.video_analysis import platform_runtime, video_upload
from src.service.video_analysis.shared_results import publisher


class DistributedStorageTests(unittest.TestCase):
    def test_node_database_root_is_local_and_node_scoped(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "VIDEO_ANALYSIS_NODE_DB_ROOT": directory,
            "VIDEO_ANALYSIS_NODE_ID": "node-a",
        }, clear=False):
            root = platform_runtime.video_analysis_database_root()
            self.assertEqual(root, Path(directory).resolve() / "node-a")
            self.assertFalse(str(root).startswith(("\\\\", "//")))

    def test_node_database_root_rejects_unc(self):
        with patch.dict(os.environ, {
            "VIDEO_ANALYSIS_NODE_DB_ROOT": r"\\server\share\runtime",
            "VIDEO_ANALYSIS_NODE_ID": "node-a",
        }, clear=False), self.assertRaisesRegex(ValueError, "local disk"):
            platform_runtime.video_analysis_database_root()

    def test_named_node_does_not_copy_shared_legacy_queue(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "nas-database"
            node = shared / "nodes" / "node-a"
            shared.mkdir(parents=True)
            node.mkdir(parents=True)
            filename = "queue.sqlite3"
            with closing(sqlite3.connect(shared / filename)) as conn:
                conn.execute("CREATE TABLE marker(value TEXT)")
                conn.execute("INSERT INTO marker VALUES ('shared')")
                conn.commit()
            with closing(sqlite3.connect(node / filename)) as conn:
                conn.execute("CREATE TABLE marker(value TEXT)")
                conn.execute("INSERT INTO marker VALUES ('node')")
                conn.commit()
            with patch.dict(os.environ, {
                "VIDEO_ANALYSIS_NODE_DB_ROOT": str(root / "local"),
                "VIDEO_ANALYSIS_NODE_ID": "node-a",
            }, clear=False), patch.object(
                platform_runtime, "legacy_node_database_root", return_value=node,
            ), patch.object(
                platform_runtime, "legacy_video_analysis_database_root", return_value=shared,
            ):
                target = platform_runtime.video_analysis_database_file(filename)
            with closing(sqlite3.connect(target)) as conn:
                self.assertEqual(conn.execute("SELECT value FROM marker").fetchone()[0], "node")

    def test_nas_directory_check_does_not_create_probe_file(self):
        with TemporaryDirectory() as directory, patch.object(
            platform_runtime, "_is_nas_path", return_value=True,
        ), patch.object(Path, "write_bytes", side_effect=AssertionError("probe created")):
            self.assertEqual(
                platform_runtime.ensure_writable_directory(Path(directory)), Path(directory),
            )

    def test_upload_stages_locally_and_nas_only_receives_final_name(self):
        with TemporaryDirectory() as nas, TemporaryDirectory() as local:
            @contextmanager
            def local_work(_prefix):
                yield local

            upload = UploadFile(filename="sample.mp4", file=BytesIO(b"video-data"))
            with patch.object(video_upload, "media_root", return_value=Path(nas)), patch.object(
                video_upload, "analysis_temporary_directory", side_effect=local_work,
            ):
                result = asyncio.run(video_upload.save_video_upload(upload))
            files = list((Path(nas) / "uploads").iterdir())
            self.assertEqual([path.name for path in files], [result["filename"]])
            self.assertFalse(any(path.name.startswith(".") for path in files))

    def test_shared_snapshots_keep_required_nullable_fields(self):
        analysis = publisher._safe_result("video_analysis", {
            "result_path": "", "current_scene": None, "total_scenes": None,
        })
        storyboard = publisher._safe_result("script_storyboard", {
            "result_path": "", "storyboard_id": None,
        })
        self.assertIn("current_scene", analysis)
        self.assertIn("total_scenes", analysis)
        self.assertIn("storyboard_id", storyboard)


if __name__ == "__main__":
    unittest.main()
