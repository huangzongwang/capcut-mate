import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from src.schemas.video_analysis import DetectScenesRequest
from src.service.video_analysis.scene_detection import (
    SceneDetectionError, build_worker_input, detect_scenes,
)

worker_spec = importlib.util.spec_from_file_location(
    "scene_worker", Path(__file__).parents[1] / "src/service/video_analysis/worker.py"
)
worker = importlib.util.module_from_spec(worker_spec)
worker_spec.loader.exec_module(worker)


class SceneInputTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix" and Path("/opt/scenedetect/bin/python").exists(), "Docker runtime required")
    def test_cross_process_lock(self):
        import fcntl

        with (Path(tempfile.gettempdir()) / "capcut-mate-scenedetect.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(
                ["/opt/scenedetect/bin/python", worker_spec.origin],
                input="{}", text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["status"], 409)

    def test_source_and_parameter_validation(self):
        for payload in [
            {}, {"video_path": "a", "video_url": "http://nginx/a"},
            {"video_path": ""}, {"video_path": "a", "detector": "unknown"},
            {"video_path": "a", "threshold": 0},
            {"video_path": "a", "threshold": float("nan")},
            {"video_path": "a", "min_scene_len": 0},
            {"video_path": "a", "window_width": 0},
            {"video_path": "a", "detector": "content", "window_width": 4},
            {"video_path": "a", "detector": "adaptive", "score_region": "roi"},
            {"video_path": "a", "detector": "enhanced", "refine_seconds": 1},
            {"video_path": "a", "detector": "enhanced", "persistence_ratio": float("nan")},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                DetectScenesRequest(**payload)

    def test_local_paths_and_defaults(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"SCENE_MEDIA_ROOT": root}):
            Path(root, "video.mp4").touch()
            for detector, threshold in [("adaptive", 3.0), ("content", 27.0)]:
                data = build_worker_input(DetectScenesRequest(video_path="video.mp4", detector=detector))
                self.assertEqual(data["threshold"], threshold)
                self.assertEqual(data["video_path"], str(Path(root, "video.mp4")))
            data = build_worker_input(DetectScenesRequest(video_path="video.mp4", detector="enhanced"))
            self.assertEqual((data["threshold"], data["min_content_val"], data["window_width"]), (3.0, 30.0, 4))
            self.assertEqual(data["persistence_ratio"], 1.5)
            self.assertEqual(data["refine_seconds"], 0)
            default_data = build_worker_input(DetectScenesRequest(video_path="video.mp4"))
            self.assertEqual(default_data, data)
            for path in ["../secret", "/etc/passwd", "C:\\private\\video.mp4", "missing.mp4"]:
                with self.subTest(path=path), self.assertRaises(SceneDetectionError):
                    build_worker_input(DetectScenesRequest(video_path=path))

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            target = Path(outside, "private.mp4")
            target.touch()
            try:
                Path(root, "link.mp4").symlink_to(target)
            except OSError:
                self.skipTest("Symlinks unavailable")
            with patch.dict(os.environ, {"SCENE_MEDIA_ROOT": root}), self.assertRaises(SceneDetectionError):
                build_worker_input(DetectScenesRequest(video_path="link.mp4"))

    def test_url_allowlist(self):
        with patch.dict(os.environ, {"SCENE_ALLOWED_MEDIA_HOSTS": "nginx,media.example:8443"}):
            for url in ["http://nginx/a.mp4", "https://media.example:8443/a?token=example"]:
                self.assertEqual(build_worker_input(DetectScenesRequest(video_url=url))["video_url"], url)
            for url in [
                "file:///etc/passwd", "http://127.0.0.1/a", "http://nginx.evil/a",
                "http://user:pass@nginx/a", "http://nginx:9999/a", "http://nginx/a#fragment",
            ]:
                with self.subTest(url=url), self.assertRaises(SceneDetectionError):
                    build_worker_input(DetectScenesRequest(video_url=url))

    def test_download_limit_and_redirects(self):
        from io import BytesIO
        from urllib.error import HTTPError

        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "download")
            for headers in [{"Content-Length": "5"}, {}]:
                response = BytesIO(b"12345")
                response.headers = headers
                with patch.object(worker, "build_opener") as opener:
                    opener.return_value.open.return_value = response
                    with self.assertRaises(worker.DetectionFailure) as error:
                        worker.download_video("http://nginx/video", path, 4)
                    self.assertEqual(error.exception.status, 413)
            with patch.object(worker, "build_opener") as opener:
                opener.return_value.open.side_effect = HTTPError("http://nginx", 302, "redirect", {}, None)
                with self.assertRaises(worker.DetectionFailure):
                    worker.download_video("http://nginx/video", path, 4)
            self.assertIsNone(worker.NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil"))


class SceneProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_kills_worker_and_cleans_files(self):
        captured = []
        process = AsyncMock()
        process.returncode = None

        async def communicate(payload):
            data = json.loads(payload)
            directory = Path(data["work_directory"])
            captured.append(directory)
            (directory / "partial-download").touch()
            raise asyncio.TimeoutError()

        process.communicate.side_effect = communicate
        # kill() is synchronous on asyncio.Process.
        from unittest.mock import Mock
        process.kill = Mock()
        with patch.dict(os.environ, {"SCENE_ALLOWED_MEDIA_HOSTS": "nginx"}):
            with patch("asyncio.create_subprocess_exec", return_value=process):
                with self.assertRaises(SceneDetectionError) as error:
                    await detect_scenes(DetectScenesRequest(video_url="http://nginx/a"))
        self.assertEqual(error.exception.status, 504)
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
        self.assertFalse(captured[0].exists())

    async def test_missing_runtime(self):
        with patch.dict(os.environ, {"SCENE_ALLOWED_MEDIA_HOSTS": "nginx"}):
            with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
                with self.assertRaises(SceneDetectionError) as error:
                    await detect_scenes(DetectScenesRequest(video_url="http://nginx/a"))
        self.assertEqual(error.exception.status, 503)

    async def test_busy_and_invalid_result(self):
        for output, returncode, status in [
            (b'{"status":409,"error":"busy"}', 1, 409), (b"invalid-json", 0, 500),
        ]:
            process = AsyncMock()
            process.returncode = returncode
            process.communicate.return_value = (output, b"")
            with patch.dict(os.environ, {"SCENE_ALLOWED_MEDIA_HOSTS": "nginx"}):
                with patch("asyncio.create_subprocess_exec", return_value=process):
                    with self.assertRaises(SceneDetectionError) as error:
                        await detect_scenes(DetectScenesRequest(video_url="http://nginx/a"))
            self.assertEqual(error.exception.status, status)


if __name__ == "__main__":
    unittest.main()
