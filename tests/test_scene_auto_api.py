"""API contract compatibility without loading the analysis runtime."""
import unittest
import json
import subprocess
import tempfile
from unittest.mock import patch
from pathlib import Path

from src.schemas.video_analysis import DetectScenesRequest, SplitScenesRequest, AnalyzeVideoRequest
from src.service.video_analysis.scene_detection import build_worker_input


class AutoContractTests(unittest.TestCase):
    def test_default_and_legacy_parameters_unchanged(self):
        with patch.object(Path, "is_file", return_value=True):
            for detector, floor, window, persistence in [
                ("enhanced", 30, 4, 1.5), ("adaptive", 15, 2, 0), ("content", 15, 2, 0),
                ("auto", 15, 4, 1.5),
            ]:
                data = build_worker_input(DetectScenesRequest(video_path="test.mp4", detector=detector))
                self.assertEqual((data["min_content_val"], data["window_width"], data["persistence_ratio"]),
                                 (floor, window, persistence))
        self.assertEqual(DetectScenesRequest(video_path="x").detector, "enhanced")
        self.assertEqual(SplitScenesRequest(video_path="x").detector, "enhanced")

    def test_auto_available_in_all_requests_and_rejects_ignored_options(self):
        for cls in (DetectScenesRequest, SplitScenesRequest, AnalyzeVideoRequest):
            kwargs = {"use_external_model": False} if cls is AnalyzeVideoRequest else {}
            self.assertEqual(cls(video_path="x", detector="auto", **kwargs).detector, "auto")
        with self.assertRaises(ValueError):
            DetectScenesRequest(video_path="x", detector="auto", score_region="roi")
        with self.assertRaises(ValueError):
            DetectScenesRequest(video_path="x", detector="auto", refine_seconds=0.2)

    def test_auto_draft_exports_final_frames_with_and_without_audio(self):
        from src.service.video_analysis.draft_builder import _export_scene_clips
        from src.service.video_analysis.platform_runtime import media_tool
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for audio in (False, True):
                source = root / f"source-{audio}.mp4"
                command = [media_tool("ffmpeg"), "-v", "error", "-f", "lavfi",
                           "-i", "testsrc2=size=160x120:rate=25:duration=2"]
                if audio:
                    command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
                command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)]
                subprocess.run(command, check=True, capture_output=True, timeout=30)
                with patch("src.service.video_analysis.draft_builder.scene_draft_root", return_value=root):
                    clips = _export_scene_clips(source, [{
                        "index": 1, "start_frame": 7, "end_frame": 37,
                        "start_us": 280000, "end_us": 1480000,
                    }], f"task-{audio}", frame_exact=True)
                probe = subprocess.run([
                    media_tool("ffprobe"), "-v", "error", "-count_frames",
                    "-show_entries", "stream=codec_type,nb_read_frames,duration,start_time",
                    "-of", "json", str(clips[0]["path"]),
                ], check=True, capture_output=True, text=True, timeout=30)
                streams = json.loads(probe.stdout)["streams"]
                video = next(s for s in streams if s["codec_type"] == "video")
                self.assertEqual(int(video["nb_read_frames"]), 30)
                self.assertAlmostEqual(float(video["duration"]), 1.2, places=3)
                self.assertEqual(any(s["codec_type"] == "audio" for s in streams), audio)


if __name__ == "__main__":
    unittest.main()
