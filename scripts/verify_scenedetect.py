"""Run with /opt/scenedetect/bin/python inside the local Docker image."""

import tempfile
from pathlib import Path

import cv2
import numpy as np
from scenedetect import AdaptiveDetector, detect


def main():
    with tempfile.TemporaryDirectory() as directory:
        video = Path(directory) / "three-scenes.avi"
        writer = cv2.VideoWriter(
            str(video), cv2.VideoWriter_fourcc(*"MJPG"), 25, (320, 240)
        )
        assert writer.isOpened(), "Test video encoder unavailable"
        try:
            for color in [(0, 0, 255), (0, 255, 0), (255, 0, 0)]:
                for _ in range(50):
                    writer.write(np.full((240, 320, 3), color, dtype=np.uint8))
        finally:
            writer.release()
        scenes = detect(str(video), AdaptiveDetector())
        actual = [(start.frame_num, end.frame_num) for start, end in scenes]
        expected = [(0, 50), (50, 100), (100, 150)]
        assert actual == expected, f"Expected {expected}, got {actual}"
        print(f"PASS: three scenes detected at expected frame boundaries: {actual}")


if __name__ == "__main__":
    main()
