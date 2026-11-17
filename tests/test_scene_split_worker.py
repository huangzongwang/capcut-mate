"""Isolated-runtime tests with real FFmpeg decoding, encoding and audio."""

from fractions import Fraction
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parents[1] / "src/service/video_analysis"))
import splitting
import worker


class SplitWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.source = cls.root / "source.mp4"
        splitting.run_media([
            "/usr/bin/ffmpeg", "-nostdin", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v", "libx264", "-g", "250", "-bf", "3", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-threads", "1", str(cls.source),
        ])
        cls.detection = {"frame_count": 75, "fps": 25}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_exact_non_keyframe_exports_and_audio(self):
        times, audio = splitting.source_timeline(self.source, self.detection)
        self.assertTrue(audio)
        hashes = splitting.frame_hashes(self.source)
        self.assertEqual(len(hashes), 75)
        for start, end in [(0, 1), (1, 26), (26, 74), (74, 75)]:
            destination = self.root / f"clip-{start}-{end}.mp4"
            splitting.encode_clip(self.source, destination, start, end, times, audio, 10_000_000)
            splitting.verify_clip(destination, hashes[start:end], float(times[end] - times[start]), audio)
        with self.assertRaises(splitting.SplitFailure):
            splitting.verify_clip(self.root / "clip-1-26.mp4", hashes[0:25], 1, audio)

    def test_encoder_selection_and_auto_fallback(self):
        self.assertEqual(splitting.select_encoder("cpu", "compatible"), "libx264")
        self.assertEqual(splitting.select_encoder("auto", "lossless"), "libx264")
        with patch.object(splitting, "nvenc_available", return_value=False):
            self.assertEqual(splitting.select_encoder("auto", "compatible"), "libx264")
            with self.assertRaises(splitting.SplitFailure) as error:
                splitting.select_encoder("nvenc", "compatible")
            self.assertEqual(error.exception.status, 503)

    def test_media_timeout_uses_configured_limit_and_reports_timeout(self):
        timeout = subprocess.TimeoutExpired(["ffmpeg", "-i", "input.mp4"], 12)
        with patch.dict("os.environ", {"SCENE_MEDIA_COMMAND_TIMEOUT_SECONDS": "12"}), \
             patch.object(splitting.subprocess, "run", side_effect=timeout) as run, \
             patch("builtins.print") as log:
            with self.assertRaises(splitting.SplitFailure) as error:
                splitting.run_media(["ffmpeg", "-i", "input.mp4"])
        self.assertEqual(error.exception.status, 504)
        self.assertEqual(str(error.exception), "Media processing timed out")
        self.assertEqual(run.call_args.kwargs["timeout"], 12)
        self.assertIn("limit=12s", log.call_args.args[0])

    def test_vfr_and_decoder_disagreement_are_rejected(self):
        stream = {"streams": [{"codec_type": "video", "width": 160, "height": 120,
                                "time_base": "1/1000", "avg_frame_rate": "25"}]}
        for timestamps in [[0, 40, 100], [0, 40, 40]]:
            with patch.object(splitting, "probe", side_effect=[
                stream, {"frames": [{"best_effort_timestamp": t} for t in timestamps]},
            ]), self.assertRaises(splitting.SplitFailure):
                splitting.source_timeline(self.source, {"frame_count": 3, "fps": 25})
        with self.assertRaises(splitting.SplitFailure):
            splitting.source_timeline(self.source, {"frame_count": 74, "fps": 25})

    def test_compatible_profile_quality_and_wrong_frame_rejection(self):
        times, audio = splitting.source_timeline(self.source, self.detection)
        hashes = splitting.frame_hashes(self.source)
        destination = self.root / "compatible.mp4"
        splitting.encode_clip(self.source, destination, 1, 26, times, audio, 10_000_000, encoding="compatible")
        quality = splitting.verify_clip(
            destination, hashes[1:26], 1, audio, encoding="compatible", source=self.source,
            start=1, stats_path=self.root / "quality.log",
        )
        self.assertGreaterEqual(quality, 35)
        video = splitting.probe(destination)["streams"][0]
        self.assertEqual(video["profile"], "High")
        self.assertEqual((video["width"], video["height"]), (160, 120))
        with self.assertRaises(splitting.SplitFailure):
            splitting.verify_clip(
                destination, hashes[20:45], 1, audio, encoding="compatible", source=self.source,
                start=20, stats_path=self.root / "wrong-quality.log",
            )

    def test_nonzero_timestamps(self):
        offset = self.root / "offset.mp4"
        splitting.run_media([
            "/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-i", str(self.source),
            "-c", "copy", "-output_ts_offset", "5", str(offset),
        ])
        times, audio = splitting.source_timeline(offset, self.detection)
        self.assertGreaterEqual(times[0], Fraction(49, 10))
        destination = self.root / "offset-clip.mp4"
        splitting.encode_clip(offset, destination, 25, 50, times, audio, 10_000_000)
        splitting.verify_clip(destination, splitting.frame_hashes(offset)[25:50], 1, audio)

    def test_fractional_rate_without_audio(self):
        source = self.root / "fractional.mp4"
        splitting.run_media([
            "/usr/bin/ffmpeg", "-nostdin", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=30000/1001:duration=2",
            "-c:v", "libx264", "-threads", "1", str(source),
        ])
        times, audio = splitting.source_timeline(source, {"frame_count": 60, "fps": 30000 / 1001})
        self.assertFalse(audio)
        destination = self.root / "fractional-clip.mp4"
        splitting.encode_clip(source, destination, 1, 59, times, audio, 10_000_000)
        splitting.verify_clip(destination, splitting.frame_hashes(source)[1:59],
                              float(times[59] - times[1]), audio)

    def test_delayed_audio_preserves_leading_silence(self):
        source = self.root / "delayed.mp4"
        splitting.run_media([
            "/usr/bin/ffmpeg", "-nostdin", "-v", "error", "-i", str(self.source),
            "-itsoffset", "0.5", "-i", str(self.source),
            "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(source),
        ])
        times, audio = splitting.source_timeline(source, self.detection)
        destination = self.root / "delayed-clip.mp4"
        splitting.encode_clip(source, destination, 0, 25, times, audio, 10_000_000)
        splitting.verify_clip(destination, splitting.frame_hashes(source)[:25], 1, audio)
        decoded = subprocess.run([
            "/usr/bin/ffmpeg", "-v", "error", "-i", str(destination), "-map", "0:a:0",
            "-ac", "1", "-ar", "48000", "-f", "f32le", "-",
        ], capture_output=True, check=True)
        samples = np.frombuffer(decoded.stdout, dtype=np.float32)
        self.assertLess(float(np.abs(samples[:12000]).max()), 0.001)
        self.assertGreater(float(np.abs(samples[30000:36000]).max()), 0.01)

    def test_hard_cut_refinement_and_gradual_review(self):
        features = np.zeros((100, 16, 16, 3), dtype=np.uint8)
        features[50:] = 200
        scores = np.zeros(100)
        scores[50] = 90
        detection = {"fps": 25, "frame_count": 100, "scenes": [{"end_frame": 48}, {"end_frame": 100}]}
        signals = {"full": scores, "features": features}
        reviews = splitting.review_boundaries(detection, signals)
        self.assertEqual(reviews[0]["selected_frame"], 50)
        self.assertEqual(reviews[0]["status"], "hard_cut_candidate")
        scores[49:53] = 80
        self.assertEqual(splitting.review_boundaries(detection, signals)[0]["status"], "needs_review")

    def test_strict_policy_does_not_encode_ambiguous_cuts(self):
        detection = {"fps": 25, "frame_count": 75, "scenes": [{"end_frame": 25}, {"end_frame": 75}]}
        signals = {"full": np.zeros(75), "features": np.zeros((75, 16, 16, 3))}
        with patch.object(splitting, "encode_clip") as encode:
            result = splitting.split_video(self.source, {"boundary_policy": "strict"}, detection, signals)
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue(result["review_required"])
        encode.assert_not_called()
        with tempfile.TemporaryDirectory() as output:
            result = splitting.split_video(self.source, {
                "boundary_policy": "allow_review", "max_output_bytes": 10_000_000,
                "output_directory": output, "job_id": "b" * 32,
            }, detection, signals)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["review_required"])
        self.assertEqual([clip["frame_count"] for clip in result["clips"]], [25, 50])

    def test_manual_ranges_preserve_gaps_and_output_limit(self):
        config = {
            "detector": "enhanced", "threshold": 3, "min_scene_len": 15,
            "max_bytes": 200 * 1024 ** 2, "max_duration_seconds": 600,
            "confirmed_ranges": [{"start_frame": 1, "end_frame": 26}, {"start_frame": 30, "end_frame": 50}],
            "boundary_policy": "strict", "max_output_bytes": 10_000_000, "job_id": "a" * 32,
        }
        signals = {}
        detection = worker.analyze_video(self.source, config, signals)
        with tempfile.TemporaryDirectory() as output:
            config["output_directory"] = output
            result = splitting.split_video(self.source, config, detection, signals)
            self.assertEqual(result["status"], "completed")
            self.assertEqual([(c["start_frame"], c["end_frame"]) for c in result["clips"]], [(1, 26), (30, 50)])
        with tempfile.TemporaryDirectory() as output:
            config.update(output_directory=output, max_output_bytes=100)
            with self.assertRaises(splitting.SplitFailure):
                splitting.split_video(self.source, config, detection, signals)


if __name__ == "__main__":
    unittest.main()
