"""Opt-in live Docker tests using generated video, not user footage."""

import json
import os
import shutil
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request, urlopen


@unittest.skipUnless(os.getenv("SCENE_TEST_API_URL"), "Requires live Docker API and writable fixture mount")
class SceneDetectionAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import cv2
        import numpy as np

        cls.directory = tempfile.TemporaryDirectory(prefix="scene-test-", dir="/fixtures")
        root = Path(cls.directory.name)
        cls.relative = root.name
        for name, colors in [
            ("cuts.avi", [(0, 0, 255), (0, 255, 0), (255, 0, 0)]),
            ("single.avi", [(50, 50, 50)]),
        ]:
            writer = cv2.VideoWriter(str(root / name), cv2.VideoWriter_fourcc(*"MJPG"), 25, (320, 240))
            assert writer.isOpened()
            try:
                for color in colors:
                    for _ in range(50):
                        writer.write(np.full((240, 320, 3), color, dtype=np.uint8))
            finally:
                writer.release()
        (root / "invalid.mp4").write_bytes(b"not a video")
        cls.http_directory = tempfile.TemporaryDirectory(prefix="scene-test-", dir="/http-fixtures")
        Path(cls.http_directory.name).chmod(0o755)
        shutil.copyfile(root / "cuts.avi", Path(cls.http_directory.name) / "cuts.avi")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()
        cls.http_directory.cleanup()

    def call(self, payload, endpoint="detect_scenes"):
        request = Request(
            os.environ["SCENE_TEST_API_URL"] + "/openapi/capcut-mate/v1/" + endpoint,
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=200) as response:
            return json.load(response)

    def test_split_and_download(self):
        result = self.call({
            "video_path": f"{self.relative}/cuts.avi", "min_content_val": 15,
        }, "split_scenes")
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse(result["review_required"])
        self.assertEqual(len(result["clips"]), 3)
        try:
            for index, clip in enumerate(result["clips"]):
                self.assertEqual((clip["start_frame"], clip["end_frame"]), (index * 50, (index + 1) * 50))
                self.assertEqual(clip["encoding"], "compatible")
                self.assertEqual(clip["verification"], "frame_count_and_quality_checked")
                with urlopen(os.environ["SCENE_TEST_API_URL"] + clip["download_path"]) as response:
                    self.assertEqual(response.headers["Content-Type"], "video/mp4")
                    self.assertEqual(len(response.read()), clip["size_bytes"])
        finally:
            exported = Path("/split-fixtures") / result["job_id"]
            if exported.is_dir():
                shutil.rmtree(exported)

    def test_invalid_split_range_and_download(self):
        result = self.call({
            "video_path": f"{self.relative}/cuts.avi",
            "confirmed_ranges": [{"start_frame": 1, "end_frame": 1000}],
        }, "split_scenes")
        self.assertEqual(result["code"], 400, result)
        result = self.call({"video_path": f"{self.relative}/cuts.avi", "detector": "content"}, "split_scenes")
        self.assertEqual(result["code"], 1001, result)
        with urlopen(os.environ["SCENE_TEST_API_URL"] + "/openapi/capcut-mate/v1/scene_clips/invalid/scene-001.mp4") as response:
            self.assertEqual(json.load(response)["code"], 404)

    def test_split_from_trusted_url(self):
        url = f"http://nginx/output/{Path(self.http_directory.name).name}/cuts.avi"
        result = self.call({"video_url": url, "min_content_val": 15}, "split_scenes")
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(result["clips"]), 3)
        exported = Path("/split-fixtures") / result["job_id"]
        if exported.is_dir():
            shutil.rmtree(exported)

    def test_adaptive_and_content(self):
        for detector in ["adaptive", "content"]:
            with self.subTest(detector=detector):
                payload = {"video_path": f"{self.relative}/cuts.avi", "detector": detector}
                # Pure hue changes score below the default content threshold.
                if detector == "content":
                    payload["threshold"] = 10
                result = self.call(payload)
                self.assertEqual(result["code"], 0, result)
                self.assertEqual(result["scene_count"], 3)
                self.assertEqual(result["frame_count"], 150)
                self.assertEqual(result["duration_us"], 6_000_000)
                self.assertEqual(
                    [(scene["start_frame"], scene["end_frame"]) for scene in result["scenes"]],
                    [(0, 50), (50, 100), (100, 150)],
                )

    def test_single_scene(self):
        result = self.call({"video_path": f"{self.relative}/single.avi"})
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(result["detector"], "enhanced")
        self.assertEqual(result["scene_count"], 1)
        self.assertEqual(result["scenes"][0]["end_us"], 2_000_000)

    def test_enhanced_and_diagnostics(self):
        result = self.call({
            "video_path": f"{self.relative}/cuts.avi", "detector": "enhanced",
            "min_content_val": 15, "include_diagnostics": True, "refine_seconds": 0.2,
        })
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(result["scene_count"], 3)
        self.assertEqual(result["settings"]["window_width"], 4)
        self.assertEqual(len(result["diagnostics"]), 2)
        self.assertEqual([item["final_frame"] for item in result["diagnostics"]], [50, 100])
        result = self.call({"video_path": f"{self.relative}/single.avi", "detector": "enhanced"})
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(result["scene_count"], 1)
        self.assertEqual(result["diagnostics"], [])

    def test_trusted_video_url(self):
        url = f"http://nginx/output/{Path(self.http_directory.name).name}/cuts.avi"
        result = self.call({"video_url": url, "detector": "adaptive"})
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(result["scene_count"], 3)

    def test_invalid_requests(self):
        for payload, expected in [
            ({}, 1001),
            ({"video_path": f"{self.relative}/cuts.avi", "detector": "unknown"}, 1001),
            ({"video_path": "../config.py"}, 400),
            ({"video_path": "missing-file.mp4"}, 404),
            ({"video_url": "http://127.0.0.1/private"}, 400),
            ({"video_path": f"{self.relative}/invalid.mp4"}, 400),
        ]:
            with self.subTest(payload=payload):
                self.assertEqual(self.call(payload)["code"], expected)


if __name__ == "__main__":
    unittest.main()
