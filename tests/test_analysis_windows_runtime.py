"""Native process-tree, path and media checks without model downloads or cloud calls."""
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from src.service.video_analysis import platform_runtime as runtime
from src.service.video_analysis.scene_detection import _run_worker, SceneDetectionError
from src.service.video_analysis.video_upload import save_video_upload


class AnalysisPathTests(unittest.TestCase):
    def test_windows_launcher_sets_lan_media_url_for_script_drafts(self):
        from scripts.windows_analysis import launch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                "SCENE_PYTHON": sys.executable,
                "SCENE_MEDIA_ROOT": str(root / "media"),
                "SCENE_DRAFT_CLIP_ROOT": str(root / "clips"),
                "SCENE_SPLIT_ROOT": str(root / "splits"),
                "SCENE_WHISPER_ROOT": str(root / "whisper"),
            }
            with patch.dict(os.environ, env, clear=True):
                launch.configure("192.168.0.191", 30001, "cpu")
                self.assertEqual(
                    os.environ["SCRIPT_VIDEO_MEDIA_URL_BASE"],
                    "http://192.168.0.191:30001/media",
                )
                self.assertEqual(
                    os.environ["DRAFT_MEDIA_BASE_URL"],
                    "http://127.0.0.1:30001/media",
                )

    def test_node_id_can_be_loaded_from_env_db_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "env.db.dev"
            env_file.write_text("VIDEO_ANALYSIS_NODE_ID=Server_A\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch.object(
                runtime, "ENV_DB_FILE", env_file,
            ):
                runtime._file_settings.cache_clear()
                try:
                    self.assertEqual(runtime.video_analysis_node_id(), "server_a")
                finally:
                    runtime._file_settings.cache_clear()

    def test_node_id_rejects_unsafe_path_characters(self):
        with patch.dict(os.environ, {"VIDEO_ANALYSIS_NODE_ID": "../server-a"}):
            with self.assertRaises(ValueError):
                runtime.video_analysis_node_id()

    def test_analysis_temp_defaults_to_local_temp_directory(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                runtime.analysis_temp_root(),
                Path(tempfile.gettempdir()) / "capcut-mate" / "video-analysis",
            )

    def test_analysis_temp_rejects_network_or_nas_paths(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {
            "VIDEO_ANALYSIS_STORAGE_ROOT": root,
            "VIDEO_ANALYSIS_TEMP_ROOT": str(Path(root) / "temp"),
        }):
            with self.assertRaises(ValueError):
                runtime.analysis_temp_root()
        with patch.dict(os.environ, {"VIDEO_ANALYSIS_TEMP_ROOT": r"\\server\share\temp"}):
            with self.assertRaises(ValueError):
                runtime.analysis_temp_root()

    def test_explicit_paths_and_interpreter(self):
        with tempfile.TemporaryDirectory(prefix="analysis path ") as directory:
            with patch.dict(os.environ, {
                "SCENE_MEDIA_ROOT": directory, "SCENE_WHISPER_ROOT": directory,
                "SCENE_DRAFT_CLIP_ROOT": directory, "SCENE_PYTHON": sys.executable,
            }):
                self.assertEqual(runtime.media_root(), Path(directory).resolve())
                self.assertEqual(runtime.scene_draft_root(), Path(directory).resolve())
                self.assertEqual(runtime.whisper_root(), str(Path(directory).resolve()))
                self.assertEqual(runtime.analysis_python(), sys.executable)

    def test_linux_defaults_are_unchanged(self):
        # Mock only the platform predicate; never construct a foreign Path class.
        with patch.dict(os.environ, {}, clear=True), patch.object(runtime.os, "name", "posix"):
            self.assertEqual(runtime.analysis_python(), "/opt/scenedetect/bin/python")
            self.assertEqual(runtime.worker_creation_kwargs(), {"start_new_session": True})


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_and_range_without_framework_changes(self):
        import config
        from fastapi import UploadFile
        from src.service.video_analysis.media_server import create_app

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {
                "SCENE_MEDIA_ROOT": str(root / "media"),
                "SCENE_DRAFT_CLIP_ROOT": str(root / "clips"),
            }), patch.object(config, "DRAFT_DIR", str(root / "drafts")):
                result = await save_video_upload(UploadFile(filename="sample.mp4", file=io.BytesIO(b"0123456789")))
                app = create_app()
                messages = []

                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message):
                    messages.append(message)

                scope = {
                    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                    "method": "GET", "scheme": "http", "root_path": "",
                    "path": "/media/" + result["video_path"],
                    "query_string": b"", "headers": [(b"range", b"bytes=2-5")],
                    "server": ("localhost", 30001), "client": ("localhost", 12345),
                }
                await app(scope, receive, send)
                self.assertEqual(messages[0]["status"], 206)
                self.assertEqual(b"".join(m.get("body", b"") for m in messages), b"2345")
                self.assertIn((b"content-range", b"bytes 2-5/10"), messages[0]["headers"])
                for path in ("/db/video_gen_tasks.sqlite3", "/media/../secret.txt"):
                    messages.clear()
                    await app({**scope, "path": path, "headers": []}, receive, send)
                    self.assertEqual(messages[0]["status"], 404)


@unittest.skipUnless(os.name == "nt", "Native Windows Job Object regression")
class WindowsWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_and_cancel_kill_descendants(self):
        import win32api
        import win32event
        import win32con

        for cancel in (False, True):
            with tempfile.TemporaryDirectory(prefix="worker path ") as directory:
                pid_path = Path(directory) / "child.pid"
                script = (
                    "import sys,json,subprocess,time; from pathlib import Path; "
                    "json.load(sys.stdin); "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)']); "
                    f"Path({str(pid_path)!r}).write_text(str(p.pid)); time.sleep(90)"
                )
                original = asyncio.create_subprocess_exec

                async def launch(*args, **kwargs):
                    return await original(sys.executable, "-c", script, **kwargs)

                with patch("asyncio.create_subprocess_exec", side_effect=launch), patch.dict(
                    os.environ, {"SCENE_SPLIT_TIMEOUT_SECONDS": "3"},
                ):
                    task = asyncio.create_task(_run_worker({"operation": "prepare_analysis"}))
                    for _ in range(100):
                        if pid_path.exists() or task.done():
                            break
                        await asyncio.sleep(0.02)
                    if task.done():
                        await task
                    self.assertTrue(pid_path.exists())
                    handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, int(pid_path.read_text()))
                    try:
                        if cancel:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        else:
                            with self.assertRaises(SceneDetectionError) as error:
                                await task
                            self.assertEqual(error.exception.status, 504)
                        self.assertEqual(win32event.WaitForSingleObject(handle, 3000), win32event.WAIT_OBJECT_0)
                    finally:
                        handle.Close()


@unittest.skipUnless(os.getenv("SCENE_TEST_NATIVE") == "1", "Opt-in installed analysis environment and FFmpeg")
class NativeVideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_worker_detects_and_splits(self):
        from src.schemas.video_analysis import DetectScenesRequest
        from src.service.video_analysis.scene_detection import build_worker_input

        with tempfile.TemporaryDirectory(prefix="native analysis \u6d4b\u8bd5 ") as directory:
            root = Path(directory)
            video = root / "two shots.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=red:s=320x240:r=25:d=2",
                "-f", "lavfi", "-i", "color=blue:s=320x240:r=25:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-threads", "2",
                "-c:a", "aac", str(video),
            ], check=True, capture_output=True, timeout=30)
            with patch.dict(os.environ, {"SCENE_MEDIA_ROOT": directory}):
                data = build_worker_input(DetectScenesRequest(video_path=video.name, detector="enhanced"))
                work = root / "work"
                work.mkdir()
                data.update(operation="prepare_analysis", work_directory=str(work), use_external_model=True)
                result = await _run_worker(data)
                self.assertEqual(len(result["clips"]), 2, result)
                frames = 0
                for clip in result["clips"]:
                    path = Path(clip["path"])
                    self.assertGreater(path.stat().st_size, 0)
                    probe = subprocess.run([
                        "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_frames", "-of", "json", str(path),
                    ], check=True, capture_output=True, timeout=15)
                    frames += int(json.loads(probe.stdout)["streams"][0]["nb_read_frames"])
                self.assertEqual(frames, 100)


if __name__ == "__main__":
    unittest.main()
