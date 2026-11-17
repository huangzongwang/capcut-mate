import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


class DigitalHumanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "nas"
        self.shared = Path(self.temp.name) / "duix"
        self.shared.mkdir(parents=True)
        self.env = patch.dict(os.environ, {
            "VIDEO_ANALYSIS_STORAGE_ROOT": str(self.root),
            "DUIX_SHARED_DIR": str(self.shared),
            "DUIX_PUBLIC_MEDIA_BASE_URL": "http://media.test",
            "DUIX_POLL_INTERVAL_SECONDS": "1",
        })
        self.env.start()
        from src.service.video_analysis.digital_human import manager, storage, store
        self.manager, self.storage, self.store = manager, storage, store

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_local_task_id_and_provider_code_are_separate_uuids(self):
        task = self.store.create(self._request())
        self.assertTrue(task["task_id"].startswith("dh_"))
        self.assertEqual(str(uuid.UUID(task["provider_code"])), task["provider_code"])

    def test_old_schema_migration_assigns_uuid_to_unsubmitted_queue_row(self):
        database = self.root / "digital-human" / "database" / "tasks.sqlite3"
        database.parent.mkdir(parents=True)
        conn = sqlite3.connect(database)
        try:
            conn.execute("""CREATE TABLE digital_human_tasks (
                task_id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL,
                progress INTEGER NOT NULL, request_json TEXT NOT NULL,
                error_message TEXT NOT NULL DEFAULT '', preview_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, completed_at TEXT
            )""")
            conn.execute(
                "INSERT INTO digital_human_tasks VALUES "
                "('dh_legacy','queued','queued',0,'{}','','','2026-01-01',NULL)"
            )
            conn.commit()
        finally:
            conn.close()
        task = self.store.get("dh_legacy")
        self.assertEqual(str(uuid.UUID(task["provider_code"])), task["provider_code"])

    def test_shared_dir_is_separate_from_upload_roots(self):
        self.assertEqual(self.storage.shared_root(str(self.shared)), self.shared)
        self.assertNotEqual(self.shared, self.storage.root() / "uploads")
        self.assertEqual(self.storage.root(), self.root / "digital-human")
        missing = self.shared / "not-created"
        with self.assertRaisesRegex(RuntimeError, "必须预先创建"):
            self.storage.shared_root(str(missing))

    def test_duix_api_and_unc_share_are_request_scoped(self):
        self.assertEqual(
            self.manager.client.validate_base_url("http://192.168.0.26:8383"),
            "http://192.168.0.26:8383",
        )
        with self.assertRaises(self.manager.client.DuixError):
            self.manager.client.validate_base_url("http://0.0.0.0:8383")
        self.manager._validate_shared_dir(
            r"\\192.168.0.26\duix_avatar_data\face2face\temp",
            "http://192.168.0.26:8383",
        )
        with self.assertRaises(ValueError):
            self.manager._validate_shared_dir(
                r"\\192.168.0.30\other\folder",
                "http://192.168.0.26:8383",
            )

    def test_upload_paths_resolve_and_traversal_is_rejected(self):
        upload_dir = self.storage.root() / "uploads"
        upload_dir.mkdir(parents=True)
        source = upload_dir / "person.mp4"
        source.write_bytes(b"sample")
        self.assertEqual(self.manager._resolve_any_upload("uploads/person.mp4", "video"), source)
        with self.assertRaises(ValueError):
            self.manager._resolve_any_upload("../person.mp4", "video")

    def test_refresh_schedules_only_one_query_worker(self):
        task = self.store.create(self._request())
        self.store.update(task["task_id"], status="running", stage="timed_out")
        submitted = []
        with patch.object(self.manager, "_validate_shared_dir"), patch.object(
            self.manager._pool, "submit", side_effect=lambda *args: submitted.append(args),
        ):
            self.assertTrue(self.manager.resume(task["task_id"]))
            self.assertFalse(self.manager.resume(task["task_id"]))
        self.assertEqual(len(submitted), 1)
        with self.manager._resume_lock:
            self.manager._polling_tasks.discard(task["task_id"])

    def test_ambiguous_submit_timeout_is_recoverable_not_resent(self):
        task = self.store.create(self._request())
        original_video = self.root / "source.mp4"
        original_audio = self.root / "source.wav"
        prepared_video = self.shared / "prepared.mp4"
        prepared_audio = self.shared / "prepared.wav"
        for path in (original_video, original_audio, prepared_video, prepared_audio):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test")
        with patch.object(
            self.storage, "prepare", return_value=(prepared_video, prepared_audio),
        ), patch.object(
            self.manager.client, "submit",
            side_effect=self.manager.client.DuixConnectionError("connection lost"),
        ) as submit:
            self.manager._run(
                task["task_id"], task["provider_code"], str(original_video),
                str(original_audio), self._request(),
            )
        current = self.store.get(task["task_id"])
        self.assertEqual(current["stage"], "timed_out")
        self.assertEqual(current["status"], "running")
        self.assertTrue(prepared_video.exists())
        self.assertTrue(original_video.exists())
        submit.assert_called_once()

    def test_poll_publishes_result_and_builds_preview_url(self):
        task = self.store.create(self._request())
        output = self.shared / "result.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"generated")
        with patch.object(self.manager.client, "query", return_value={
            "status": "2", "result": "result.mp4",
        }):
            self.manager._poll(
                task["task_id"], task["provider_code"], self._request(),
            )
        result = self.store.get(task["task_id"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(Path(result["preview_path"]).is_file())
        self.assertEqual(
            self.manager.public_task(result)["preview_url"],
            "http://media.test/output/digital-human/" + Path(result["preview_path"]).name,
        )

    def _request(self):
        return {
            "video_path": "uploads/v.mp4",
            "audio_path": "uploads/a.wav",
            "duix_api_base_url": "http://192.168.0.26:8383",
            "duix_shared_dir": str(self.shared),
            "chaofen": 0,
            "watermark_switch": 0,
            "pn": 1,
        }


if __name__ == "__main__":
    unittest.main()
