import hashlib
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.grok_video import output_video as geometry


class GeometryTests(unittest.TestCase):
    def test_all_documented_canvas_sizes_are_even(self):
        for resolution in ("480p", "720p", "1080p"):
            for aspect in ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"):
                width, height = geometry.canvas(resolution, aspect)
                self.assertEqual(width % 2, 0)
                self.assertEqual(height % 2, 0)
                self.assertEqual(min(width, height), int(resolution[:-1]))

    def test_square_to_portrait_preserves_original_audio_and_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            subprocess.run([
                geometry.tool("ffmpeg"), "-v", "error", "-f", "lavfi",
                "-i", "color=c=red:size=320x320:rate=25:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-c:v", "libx264", "-c:a", "aac", str(path),
            ], check=True, capture_output=True, timeout=30)
            original_hash = hashlib.sha256(path.read_bytes()).digest()
            request = {"resolution": "480p", "aspect_ratio": "9:16"}
            result = geometry.ensure_geometry(path, request, threading.Event())
            self.assertEqual((result["width"], result["height"]), (480, 854))
            self.assertEqual(result["actual_aspect_ratio"], "9:16")
            self.assertEqual(result["geometry_processing"], "fit_pad")
            self.assertAlmostEqual(result["duration"], 1, places=2)
            self.assertTrue(geometry.probe(path)["has_audio"])
            self.assertEqual(hashlib.sha256(path.with_name("video.original.mp4").read_bytes()).digest(),
                             original_hash)
            pixels = subprocess.run([
                geometry.tool("ffmpeg"), "-v", "error", "-i", str(path),
                "-vf", "crop=2:2:0:0", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ], capture_output=True, check=True, timeout=30).stdout
            self.assertLess(max(pixels), 8)
            with patch.object(geometry.subprocess, "run") as run:
                run.side_effect = AssertionError("Matching output must not be reencoded")
                with patch.object(geometry, "probe", side_effect=[
                    dict(width=480, height=854, rotation=0, display_aspect_ratio="9/16", duration=1, has_audio=True),
                    dict(width=480, height=854, rotation=0, display_aspect_ratio="9/16", duration=1, has_audio=True),
                    dict(width=320, height=320, rotation=0, display_aspect_ratio="1", duration=1, has_audio=True),
                ]):
                    second = geometry.ensure_geometry(path, request, threading.Event())
                    self.assertEqual(second["geometry_processing"], "fit_pad")
                    run.assert_not_called()

    def test_conversion_failure_does_not_replace_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"source-placeholder")
            info = dict(width=320, height=320, rotation=0, display_aspect_ratio="1", duration=1, has_audio=False)
            with patch.object(geometry, "probe", return_value=info), \
                 patch.object(geometry.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
                with self.assertRaises(geometry.OutputVideoError):
                    geometry.ensure_geometry(path, {"resolution": "720p", "aspect_ratio": "9:16"},
                                             threading.Event())
            self.assertEqual(path.read_bytes(), b"source-placeholder")


if __name__ == "__main__":
    unittest.main()
