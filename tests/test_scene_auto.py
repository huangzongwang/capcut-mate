"""Run in the isolated analysis environment; no cloud calls."""
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from scenedetect import open_video

sys.path.insert(0, str(Path(__file__).parents[1] / "src/service/video_analysis"))
import auto_detection
import enhanced
import splitting
import worker

CONFIG = dict(threshold=3.0, min_content_val=15, window_width=4,
              min_scene_len=15, persistence_ratio=1.5, refine_seconds=0, score_region="full")


def signals(frames, scores):
    return dict(full_features=frames, features=frames[:, 2:26], full=scores,
                roi=scores, fps=25, decoded=len(frames))


class AutoTests(unittest.TestCase):
    def test_flash_and_small_subtitle_changes_are_not_shots(self):
        frames = np.zeros((180, 32, 32, 3), np.uint8)
        frames[25] = 255
        frames[60:80, 28:30, 8:24] = 255
        frames[100:] = 180
        scores = np.zeros(180)
        scores[[25, 26, 60, 80, 100]] = 50
        cuts, _, _ = auto_detection.select_auto_boundaries(signals(frames, scores), CONFIG)
        self.assertEqual(cuts, [100])

    def test_short_shots_and_borders(self):
        frames = np.zeros((200, 32, 32, 3), np.uint8)
        frames[50:75, 12:24] = 200
        frames[100:, 12:24] = 120
        scores = np.zeros(200)
        scores[[50, 75, 100]] = 22
        cuts, _, settings = auto_detection.select_auto_boundaries(signals(frames, scores), CONFIG)
        self.assertEqual(cuts, [50, 75, 100])
        self.assertIsNotNone(settings["active_rows_32"])

    def test_top_only_content_not_lost_to_fixed_roi(self):
        frames = np.zeros((160, 32, 32, 3), np.uint8)
        frames[80:, :8] = 200
        scores = np.zeros(160)
        scores[80] = 30
        data = signals(frames, scores)
        data["roi"] = np.zeros(160)
        cuts, _, _ = auto_detection.select_auto_boundaries(data, CONFIG)
        self.assertEqual(cuts, [80])

    def test_ambiguous_long_scene_requires_review_without_forced_cut(self):
        frames = np.zeros((600, 32, 32, 3), np.uint8)
        frames[300:] = 180
        scores = np.full(600, 8.0)
        scores[300] = 20
        cuts, decisions, settings = auto_detection.select_auto_boundaries(signals(frames, scores), CONFIG)
        self.assertEqual(cuts, [])
        self.assertTrue(settings["review_required"])
        self.assertEqual([d["candidate_frame"] for d in decisions if d["status"] == "needs_review"], [300])

    def test_continuous_motion_no_forced_length_cuts(self):
        frames = np.asarray([np.full((32, 32, 3), i % 100, np.uint8) for i in range(800)])
        scores = np.full(800, 5.)
        cuts, _, _ = auto_detection.select_auto_boundaries(signals(frames, scores), CONFIG)
        self.assertEqual(cuts, [])

    def test_gradual_change_is_review_interval_not_exact_cut(self):
        frames = np.zeros((600, 32, 32, 3), np.uint8)
        for i in range(300, 320):
            frames[i] = (i-300)*10
        frames[320:] = 200
        scores = np.zeros(600)
        scores[300:321] = 6
        cuts, diagnostics, settings = auto_detection.select_auto_boundaries(signals(frames, scores), CONFIG)
        self.assertEqual(cuts, [])
        self.assertTrue(settings["review_required"])
        self.assertTrue(any(d["reason"] == "gradual_or_camera_change" for d in diagnostics))

    def test_auto_review_keeps_region_boundary_and_strict_stops(self):
        detection = dict(detector="auto", frame_count=600, fps=25,
                         scenes=[dict(end_frame=300), dict(end_frame=600)],
                         settings={"review_required": True})
        reviews = splitting.review_boundaries(detection, {})
        self.assertEqual(reviews[0]["selected_frame"], 300)
        with patch.object(splitting, "select_encoder", return_value="libx264"), \
             patch.object(splitting, "source_timeline", return_value=([], False)):
            result = splitting.split_video("unused", {"boundary_policy": "strict"}, detection, {})
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["clips"], [])

    def test_single_decode_real_video_and_old_signal_parity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.avi"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 25, (160, 240))
            for color in [(20, 20, 20), (200, 40, 40), (40, 200, 40)]:
                for _ in range(50):
                    frame = np.zeros((240, 160, 3), np.uint8)
                    frame[75:165] = color
                    writer.write(frame)
            writer.release()
            old = enhanced.collect_signals(open_video(str(path)), 200)
            new = enhanced.collect_signals(open_video(str(path)), 200, retain_full=True)
            for field in ("full", "roi", "features"):
                np.testing.assert_array_equal(old[field], new[field])
            cuts, _, _ = auto_detection.select_auto_boundaries(new, CONFIG)
            self.assertEqual(cuts, [50, 100])
            result = worker.analyze_video(path, {
                **CONFIG, "detector": "auto", "max_bytes": 10_000_000,
                "max_duration_seconds": 10, "include_diagnostics": False,
            })
            self.assertEqual(result["scene_count"], 3)
            self.assertTrue(result["diagnostics"])
            split = splitting.split_video(path, {
                "boundary_policy": "strict", "encoding": "compatible", "encoder": "cpu",
                "output_directory": directory, "work_directory": directory,
                "job_id": "test", "max_output_bytes": 10_000_000,
                "confirmed_ranges": [{"start_frame": 7, "end_frame": 47},
                                     {"start_frame": 53, "end_frame": 110}],
            }, result, new)
            self.assertEqual(split["status"], "completed")
            final = split["detection"]["scenes"]
            self.assertEqual([(s["start_frame"], s["end_frame"]) for s in final], [(7, 47), (53, 110)])
            self.assertEqual([s["frame_count"] for s in split["clips"]], [40, 57])
            self.assertEqual([s["start_us"] for s in final], [280000, 2120000])


if __name__ == "__main__":
    unittest.main()
