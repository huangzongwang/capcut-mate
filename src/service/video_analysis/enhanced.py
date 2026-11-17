"""Experimental, explainable post-processing for PySceneDetect 0.7.1 scores."""

import cv2
import numpy as np
from scenedetect import ContentDetector, SceneManager
from scenedetect.detector import SceneDetector


class ScoreRecorder(SceneDetector):
    """Use the pinned upstream detector for scoring, never reimplement HSV scoring."""

    def __init__(self, retain_full=False):
        super().__init__()
        self.full_detector = ContentDetector(threshold=255, min_scene_len=0)
        self.roi_detector = ContentDetector(threshold=255, min_scene_len=0)
        self.full = []
        self.roi = []
        self.features = []
        self.retain_full = retain_full
        self.full_features = []

    def process_frame(self, timecode, frame_img):
        height = frame_img.shape[0]
        roi = frame_img[int(height * 0.08):max(int(height * 0.8), 1)]
        self.full_detector.process_frame(timecode, frame_img)
        self.roi_detector.process_frame(timecode, roi)
        # These attributes are private upstream; covered by pinned-version integration tests.
        self.full.append(float(self.full_detector._frame_score or 0))
        self.roi.append(float(self.roi_detector._frame_score or 0))
        self.features.append(cv2.resize(roi, (16, 16), interpolation=cv2.INTER_AREA))
        if self.retain_full:
            self.full_features.append(cv2.resize(frame_img, (32, 32), interpolation=cv2.INTER_AREA))
        return []


def collect_signals(video, max_frames, retain_full=False):
    recorder = ScoreRecorder(retain_full=retain_full)
    manager = SceneManager()
    manager.add_detector(recorder)
    decoded = manager.detect_scenes(video=video, end_time=max_frames + 1)
    result = {
        "full": np.asarray(recorder.full, dtype=np.float32),
        "roi": np.asarray(recorder.roi, dtype=np.float32),
        "features": np.asarray(recorder.features, dtype=np.uint8),
        "fps": float(video.frame_rate),
        "decoded": decoded,
    }
    if retain_full:
        result["full_features"] = np.asarray(recorder.full_features, dtype=np.uint8)
    return result


def adaptive_candidates(scores, threshold, minimum, window, min_scene_len):
    """Offline equivalent of the pinned AdaptiveDetector rolling-window decision."""
    scores = np.asarray(scores, dtype=np.float64)
    cuts = []
    last_cut = 0
    for frame in range(window, len(scores) - window):
        average = (
            scores[frame - window:frame].sum()
            + scores[frame + 1:frame + window + 1].sum()
        ) / (2 * window)
        ratio = min(scores[frame] / average, 255.0) if average >= 1e-5 else (
            255.0 if scores[frame] >= minimum else 0
        )
        # Upstream checks the buffered current frame, then emits the target frame.
        if ratio >= threshold and scores[frame] >= minimum and frame + window - last_cut >= min_scene_len:
            cuts.append(frame)
            last_cut = frame
    return cuts


def boundary_strength(features, frame, span):
    before = features[max(0, frame - span):frame].astype(np.float32)
    after = features[frame:min(len(features), frame + span)].astype(np.float32)
    if not len(before) or not len(after):
        return 0.0, 0.0
    left = np.median(before, axis=0)
    right = np.median(after, axis=0)
    separation = float(np.mean(np.abs(left - right)))
    variation = float((np.mean(np.abs(before - left)) + np.mean(np.abs(after - right))) / 2)
    return separation, separation / max(variation, 1.0)


def select_boundaries(signals, config):
    scores = signals[config.get("score_region", "full")]
    fps = float(signals["fps"])
    features = signals["features"]
    minimum = config.get("min_content_val", 15.0)
    threshold = config.get("threshold", 3.0)
    window = config.get("window_width", 2)
    min_scene_len = config.get("min_scene_len", 15)
    span = max(2, round(fps * 0.15))
    radius = round(fps * config.get("refine_seconds", 0.0))
    persistence = config.get("persistence_ratio", 0.0)
    full_candidates = adaptive_candidates(scores, threshold, minimum, window, min_scene_len)
    decisions = []
    accepted = []
    for candidate in full_candidates:
        separation, stable_ratio = boundary_strength(features, candidate, span)
        if persistence and (stable_ratio < persistence or separation < 8):
            decisions.append({
                "candidate_frame": candidate, "final_frame": None,
                "reason": "weak_persistent_change", "separation": round(separation, 3),
                "persistence_ratio": round(stable_ratio, 3),
            })
            continue
        frame = candidate
        if radius:
            # Only move to a stronger persistent visual discontinuity, never to a label.
            best = separation * min(stable_ratio, 10)
            for possible in range(max(1, candidate - radius), min(len(scores), candidate + radius + 1)):
                if scores[possible] < minimum:
                    continue
                distance, ratio = boundary_strength(features, possible, span)
                strength = distance * min(ratio, 10)
                if strength > best * 1.15:
                    frame, best = possible, strength
        accepted.append((frame, float(scores[frame]), candidate))
        decisions.append({
            "candidate_frame": candidate, "final_frame": frame,
            "reason": "refined" if frame != candidate else "kept",
            "separation": round(separation, 3), "persistence_ratio": round(stable_ratio, 3),
        })
    # Local refinement must not create duplicate boundaries or sub-minimum interior gaps.
    chosen = []
    for frame, strength, original in sorted(accepted):
        if chosen and frame - chosen[-1][0] < min_scene_len:
            if strength > chosen[-1][1]:
                chosen[-1] = (frame, strength, original)
        else:
            chosen.append((frame, strength, original))
    final = {frame for frame, _, _ in chosen}
    winners = {original for _, _, original in chosen}
    for decision in decisions:
        if decision["final_frame"] is not None and decision["candidate_frame"] not in winners:
            decision.update(final_frame=None, reason="nearby_weaker_boundary")
    return sorted(final), decisions
