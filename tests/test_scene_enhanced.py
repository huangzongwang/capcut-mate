"""Run with the isolated detector Python; no dependency on the API environment."""

import importlib.util
from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np
from scenedetect import AdaptiveDetector, FrameTimecode, SceneManager, open_video

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("enhanced", ROOT / "src/service/video_analysis/enhanced.py")
enhanced = importlib.util.module_from_spec(spec)
spec.loader.exec_module(enhanced)
evaluation_spec = importlib.util.spec_from_file_location("calibration", ROOT / "scripts/calibrate_scene_detection.py")
calibration = importlib.util.module_from_spec(evaluation_spec)
evaluation_spec.loader.exec_module(calibration)


class EnhancedTests(unittest.TestCase):
    def test_matching_is_one_to_one_and_minimum_error(self):
        result = calibration.match_cuts([1.0, 1.2, 3.0], [1.1, 3.1], 0.2)
        self.assertEqual((result["tp"], result["fp"], result["fn"]), (2, 1, 0))
        self.assertAlmostEqual(result["absolute_error"], 0.2)
        self.assertEqual(calibration.match_cuts([], [1], 0.5)["fn"], 1)
        result = calibration.match_cuts([1.4, 1.6], [1.0, 1.5], 0.5)
        self.assertEqual(result["tp"], 2)

    def test_decimal_time(self):
        self.assertEqual(calibration.seconds("01:04.3"), 64.3)

    def test_persistence_rejects_flash_but_keeps_short_scene(self):
        frames = np.zeros((100, 16, 16, 3), dtype=np.uint8)
        frames[20] = 255
        frames[50:75] = 255
        scores = np.zeros(100, dtype=np.float32)
        scores[[20, 21, 50, 75]] = 90
        signals = {"features": frames, "full": scores, "roi": scores, "fps": 25}
        config = {"threshold": 2, "min_content_val": 15, "window_width": 2,
                  "min_scene_len": 10, "persistence_ratio": 2.5, "refine_seconds": 0.2}
        cuts, decisions = enhanced.select_boundaries(signals, config)
        self.assertEqual(cuts, [50, 75])
        self.assertTrue(any(row["reason"] == "weak_persistent_change" for row in decisions))

    def test_refinement_keeps_boundaries_ordered(self):
        features = np.zeros((120, 16, 16, 3), dtype=np.uint8)
        features[50:] = 200
        scores = np.zeros(120, dtype=np.float32)
        scores[[48, 50]] = [50, 70]
        signals = {"features": features, "full": scores, "roi": scores, "fps": 25}
        cuts, _ = enhanced.select_boundaries(signals, {
            "threshold": 2, "min_content_val": 15, "window_width": 2,
            "min_scene_len": 15, "refine_seconds": 0.2,
        })
        self.assertEqual(cuts, [50])

    def test_scoring_replay_matches_upstream(self):
        cv2.setNumThreads(1)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "fixture.avi")
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 25, (320, 240))
            self.assertTrue(writer.isOpened())
            try:
                for color in [(0, 0, 0), (255, 255, 255), (40, 60, 100), (0, 255, 0)]:
                    for _ in range(30):
                        writer.write(np.full((240, 320, 3), color, dtype=np.uint8))
            finally:
                writer.release()
            signals = enhanced.collect_signals(open_video(path), 200)
            for window in [2, 4, 8]:
                for threshold in [2.5, 4.0]:
                    manager = SceneManager()
                    manager.add_detector(AdaptiveDetector(
                        adaptive_threshold=threshold, window_width=window,
                        min_content_val=15, min_scene_len=15,
                    ))
                    manager.detect_scenes(video=open_video(path))
                    expected = [end.frame_num for _, end in manager.get_scene_list(start_in_scene=True)[:-1]]
                    actual = enhanced.adaptive_candidates(signals["full"], threshold, 15, window, 15)
                    self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
