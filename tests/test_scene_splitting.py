"""API-environment tests: ownership, validation, cancellation and publication."""

import asyncio
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from src.schemas.video_analysis import SplitScenesRequest, SplitScenesResponse
from src.service.video_analysis.scene_detection import SceneDetectionError, _run_worker
from src.service.video_analysis.scene_splitting import clip_path, split_scenes


def result(status="completed"):
    return SplitScenesResponse(
        status=status, review_required=status == "needs_review",
        detection=dict(
            detector="enhanced", threshold=3, min_scene_len=15, fps=25,
            frame_count=50, duration_us=2_000_000, scene_count=1, scenes=[], warnings=[],
        ),
    )


class SplitInputTests(unittest.TestCase):
    def test_ranges_and_mode_validation(self):
        for extra in [
            {"detector": "adaptive"}, {"boundary_policy": "skip"},
            {"encoding": "unknown"},
            {"encoder": "unknown"},
            {"encoding": "lossless", "encoder": "nvenc"},
            {"confirmed_ranges": []},
            {"confirmed_ranges": [{"start_frame": 1, "end_frame": 1}]},
            {"confirmed_ranges": [{"start_frame": 1.5, "end_frame": 5}]},
            {"confirmed_ranges": [{"start_frame": 2, "end_frame": 5}, {"start_frame": 4, "end_frame": 8}]},
        ]:
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                SplitScenesRequest(video_path="x.mp4", **extra)
        request = SplitScenesRequest(video_path="x.mp4", confirmed_ranges=[
            {"start_frame": 0, "end_frame": 5}, {"start_frame": 8, "end_frame": 9},
        ])
        self.assertEqual(request.boundary_policy, "strict")
        self.assertEqual(request.encoding, "compatible")
        self.assertEqual(request.encoder, "auto")

    def test_download_path_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            with patch.dict(os.environ, {"SCENE_SPLIT_ROOT": root}):
                job = Path(root) / ("a" * 32)
                job.mkdir()
                (job / "scene-001.mp4").touch()
                with self.assertRaises(SceneDetectionError):
                    clip_path(job.name, "scene-001.mp4")
                (job / "manifest.json").touch()
                self.assertEqual(clip_path(job.name, "scene-001.mp4"), job / "scene-001.mp4")
                for name, filename in [("../x", "scene-001.mp4"), (job.name, "../manifest.json"),
                                       (job.name, "manifest.json"), (".staging", "scene-001.mp4")]:
                    with self.assertRaises(SceneDetectionError):
                        clip_path(name, filename)
                target = Path(outside) / "secret.mp4"
                target.touch()
                try:
                    (job / "scene-002.mp4").symlink_to(target)
                except OSError:
                    return
                with self.assertRaises(SceneDetectionError):
                    clip_path(job.name, "scene-002.mp4")


class SplitProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_and_review_do_not_publish(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "source.mp4").touch()
            with patch.dict(os.environ, {"SCENE_MEDIA_ROOT": root, "SCENE_SPLIT_ROOT": str(Path(root) / "out"), "VIDEO_ANALYSIS_TEMP_ROOT": str(Path(root) / "local-temp")}):
                for failure in [SceneDetectionError(400, "failed"), asyncio.CancelledError(), None]:
                    async def worker(data):
                        Path(data["output_directory"], "partial.mp4").touch()
                        if failure:
                            raise failure
                        return result("needs_review")
                    with patch("src.service.video_analysis.scene_splitting._run_worker", side_effect=worker):
                        if failure:
                            with self.assertRaises(type(failure)):
                                await split_scenes(SplitScenesRequest(video_path="source.mp4"))
                        else:
                            response = await split_scenes(SplitScenesRequest(video_path="source.mp4"))
                            self.assertEqual(response.status, "needs_review")
                    self.assertFalse(Path(root, "out", ".staging").exists())
                    self.assertEqual(list(Path(root, "out").iterdir()), [])
                    self.assertEqual(list(Path(root, "local-temp").iterdir()), [])

    async def test_success_publishes_manifest_and_file(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "source.mp4").touch()
            with patch.dict(os.environ, {"SCENE_MEDIA_ROOT": root, "SCENE_SPLIT_ROOT": str(Path(root) / "out"), "VIDEO_ANALYSIS_TEMP_ROOT": str(Path(root) / "local-temp")}):
                async def worker(data):
                    Path(data["output_directory"], "scene-001.mp4").write_bytes(b"fixture")
                    with self.assertRaises(SceneDetectionError):
                        clip_path(data["job_id"], "scene-001.mp4")
                    response = result()
                    response.job_id = data["job_id"]
                    return response
                with patch("src.service.video_analysis.scene_splitting._run_worker", side_effect=worker):
                    response = await split_scenes(SplitScenesRequest(video_path="source.mp4"))
                self.assertEqual(clip_path(response.job_id, "scene-001.mp4").read_bytes(), b"fixture")
                self.assertTrue(Path(root, "out", response.job_id, "manifest.json").is_file())

    async def test_locked_cleanup_preserves_original_failure(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "source.mp4").touch()
            with patch.dict(os.environ, {"SCENE_MEDIA_ROOT": root, "SCENE_SPLIT_ROOT": str(Path(root) / "out"), "VIDEO_ANALYSIS_TEMP_ROOT": str(Path(root) / "local-temp")}):
                with patch("src.service.video_analysis.scene_splitting._run_worker",
                           side_effect=SceneDetectionError(400, "Original worker failure")):
                    with patch("src.service.video_analysis.scene_splitting.shutil.rmtree",
                               side_effect=PermissionError("locked")):
                        with patch("src.service.video_analysis.scene_splitting.enqueue_path") as enqueue:
                            with self.assertRaises(SceneDetectionError) as error:
                                await split_scenes(SplitScenesRequest(video_path="source.mp4"))
                            self.assertEqual(str(error.exception), "Original worker failure")
                            self.assertEqual(enqueue.call_count, 2)
                            self.assertTrue(all(call.kwargs["is_dir"] for call in enqueue.call_args_list))

    @unittest.skipUnless(os.name == "posix", "Linux process groups required")
    async def test_timeout_and_cancellation_kill_entire_group(self):
        for failure in [asyncio.TimeoutError(), asyncio.CancelledError()]:
            process = AsyncMock()
            process.pid = 98765
            process.returncode = None
            process.communicate.side_effect = failure
            with patch("asyncio.create_subprocess_exec", return_value=process) as create:
                with patch("os.killpg") as kill:
                    with self.assertRaises(SceneDetectionError if isinstance(failure, asyncio.TimeoutError)
                                           else asyncio.CancelledError):
                        await _run_worker({"operation": "split"})
            kill.assert_called_once_with(98765, signal.SIGKILL)
            process.wait.assert_awaited_once()
            self.assertTrue(create.call_args.kwargs["start_new_session"])

    @unittest.skipUnless(os.name == "posix" and Path("/proc").is_dir(), "Linux runtime required")
    async def test_real_timeout_leaves_no_running_child(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            script = (
                "import subprocess,sys,time; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"Path({str(pid_file)!r}).write_text(str(child.pid)); time.sleep(60)"
            )
            original_exec = asyncio.create_subprocess_exec

            async def launch(*args, **kwargs):
                return await original_exec(sys.executable, "-c", script, **kwargs)

            with patch.dict(os.environ, {"SCENE_SPLIT_TIMEOUT_SECONDS": "1"}):
                with patch("asyncio.create_subprocess_exec", side_effect=launch):
                    with self.assertRaises(SceneDetectionError) as error:
                        await _run_worker({"operation": "split"})
            self.assertEqual(error.exception.status, 504)
            child_pid = int(pid_file.read_text())
            state = Path(f"/proc/{child_pid}/stat")
            for _ in range(20):
                if not state.exists() or state.read_text().split()[2] == "Z":
                    break
                await asyncio.sleep(0.05)
            else:
                self.fail("Timed-out worker left a running child")


if __name__ == "__main__":
    unittest.main()
