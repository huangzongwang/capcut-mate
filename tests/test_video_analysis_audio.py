import wave
import os
import tempfile
import unittest
from unittest.mock import patch

from src.service.video_analysis.audio_generation import client, manager, storage
from src.service.video_analysis.audio_generation import router as audio_router
from src.service.video_analysis.audio_generation.schemas import (
    CredentialsRequest, PronunciationOverride,
)
from src.service.video_analysis.audio_generation.manager import AudioTaskManager, _with_pronunciation


class AudioGenerationTests(unittest.TestCase):
    def test_pronunciation_overrides_are_applied_only_to_matching_text(self):
        result = _with_pronunciation(
            [{"text": "赛乐赛，乐享生活"}],
            [PronunciationOverride(text="乐", phonetic="le4").model_dump()],
        )
        self.assertEqual(result, [{
            "text": "赛乐赛，乐享生活",
            "textElements": [
                {"text": "赛", "type": 1},
                {"text": "乐", "type": 4, "attributes": {"alphabet": "py", "ph": "le4"}},
                {"text": "赛，", "type": 1},
                {"text": "乐", "type": 4, "attributes": {"alphabet": "py", "ph": "le4"}},
                {"text": "享生活", "type": 1},
            ],
        }])

    def test_provider_auth_expiry_is_actionable_and_does_not_expose_cookie(self):
        class Response:
            status_code = 401

            def close(self):
                pass

        with patch.object(client.requests, "post", return_value=Response()):
            with self.assertRaises(client.CredentialsExpired) as error:
                client.split_sentences("测试文本", "test-bpid", "secret-cookie")
        self.assertIn("Cookie 已失效", str(error.exception))
        self.assertNotIn("secret-cookie", str(error.exception))

    def test_credentials_are_not_returned_and_can_be_cleared(self):
        manager.manager.clear_credentials()
        with patch.object(client, "split_sentences", return_value=[{"text": "验证"}]):
            configured = manager.manager.configure("test-bpid", "secret-cookie")
        self.assertEqual(configured, {"configured": True, "message": "凭证已配置"})
        self.assertNotIn("secret-cookie", str(configured))
        self.assertFalse(manager.manager.clear_credentials()["configured"])

    def test_expired_credentials_are_cleared_only_if_still_current(self):
        instance = AudioTaskManager()
        old = {"bpid": "1", "cookie": "old"}
        new = {"bpid": "1", "cookie": "new"}
        instance._credentials = old
        instance.invalidate_credentials(old)
        self.assertIsNone(instance._credentials)
        instance._credentials = new
        instance.invalidate_credentials(old)
        self.assertEqual(instance._credentials, new)

    def test_split_sentences_reuses_configured_credentials(self):
        instance = AudioTaskManager()
        instance._credentials = {"bpid": "test-bpid", "cookie": "secret-cookie"}
        with patch.object(client, "split_sentences", return_value=[{"text": "第一句"}]) as split:
            self.assertEqual(instance.split_sentences("第一句"), [{"text": "第一句"}])
        split.assert_called_once_with("第一句", "test-bpid", "secret-cookie")

    def test_split_sentences_requires_configured_credentials(self):
        instance = AudioTaskManager()
        with self.assertRaisesRegex(RuntimeError, "凭证未配置"):
            instance.split_sentences("测试")

    def test_split_sentences_clears_expired_credentials(self):
        instance = AudioTaskManager()
        instance._credentials = {"bpid": "test-bpid", "cookie": "expired-cookie"}
        with patch.object(client, "split_sentences", side_effect=client.CredentialsExpired("expired")):
            with self.assertRaises(client.CredentialsExpired):
                instance.split_sentences("测试")
        self.assertIsNone(instance._credentials)

    def test_audio_routes_are_registered(self):
        paths = {route.path for route in audio_router.router.routes}
        self.assertIn("/audio/credentials", paths)
        self.assertIn("/audio/tasks", paths)
        self.assertIn("/audio/tasks/{task_id}", paths)
        self.assertIn("/audio/files/{task_id}", paths)

    def test_credentials_route_calls_manager_instance(self):
        with patch.object(
            manager.manager, "configure",
            return_value={"configured": True, "message": "凭证已配置"},
        ) as configure:
            response = audio_router.set_credentials(
                CredentialsRequest(bpid="12345", cookie="session=value"),
            )
        self.assertEqual(response["configured"], True)
        configure.assert_called_once_with("12345", "session=value")

    def test_wav_conversion_uses_verified_pcm_output(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {
                "VIDEO_ANALYSIS_AUDIO_ROOT": directory,
                "VIDEO_ANALYSIS_TEMP_ROOT": temp_dir,
            },
        ):
            def fake_download(url, target):
                target.write_bytes(b"source-audio")

            def fake_ffmpeg(command, **kwargs):
                with wave.open(command[-1], "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(16000)
                    output.writeframes(b"\x00\x00" * 160)

            with patch.object(storage, "_download", side_effect=fake_download), \
                 patch.object(storage.subprocess, "run", side_effect=fake_ffmpeg):
                result = storage.download_and_convert("https://audio.example/test", "vaa_test")
            self.assertEqual(result["format"], "wav")
            self.assertEqual(result["sample_rate"], 16000)
            self.assertEqual(result["channels"], 1)
            self.assertEqual(result["sample_width_bits"], 16)
            self.assertTrue(storage.audio_path("vaa_test").is_file())
            self.assertEqual(os.listdir(directory), ["vaa_test.wav"])
            self.assertEqual(os.listdir(temp_dir), [])

    def test_old_generated_audio_files_are_cleaned(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"VIDEO_ANALYSIS_AUDIO_ROOT": directory},
        ):
            old_file = os.path.join(directory, "vaa_old.wav")
            current_file = os.path.join(directory, "vaa_current.wav")
            for path in (old_file, current_file):
                with open(path, "wb") as output:
                    output.write(b"audio")
            os.utime(old_file, (1, 1))
            storage.cleanup_old_files(max_age_seconds=60)
            self.assertFalse(os.path.exists(old_file))
            self.assertTrue(os.path.exists(current_file))

    def test_audio_task_state_survives_restart_and_unfinished_tasks_are_marked(self):
        from src.service.video_analysis.audio_generation import task_store

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {
                "VIDEO_ANALYSIS_STORAGE_ROOT": directory,
                "VIDEO_ANALYSIS_NODE_DB_ROOT": directory,
                "VIDEO_ANALYSIS_NODE_ID": "audio-test",
            },
        ), patch.object(task_store.publisher, "publish"):
            task_store.create("vaa_persist")
            task_store.update(
                "vaa_persist", status="running", stage="generating", progress=35,
            )
            self.assertEqual(task_store.get("vaa_persist")["stage"], "generating")
            self.assertEqual(task_store.fail_unfinished(), 1)
            task = task_store.get("vaa_persist")
            self.assertEqual(task["status"], "failed")
            self.assertIn("凭证未持久化", task["error_message"])


if __name__ == "__main__":
    unittest.main()
