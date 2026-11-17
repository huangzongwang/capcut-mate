"""Opt-in shot candidates from one decode; no model calls or fixed-time cuts."""
import numpy as np
from scenedetect import ContentDetector, FrameTimecode

from enhanced import boundary_strength


def active_rows(features):
    """Use a smaller region only when temporal variation supports static margins."""
    if len(features) < 4:
        return None
    activity = np.std(features.astype(np.float32), axis=0).mean(axis=(1, 2))
    high = float(np.max(activity))
    if high < 4:
        return None
    rows = np.flatnonzero(activity > max(4, high * 0.25))
    if not len(rows):
        return None
    low, end = int(rows[0]), int(rows[-1]) + 1
    height = len(activity)
    if not height * 0.25 <= end - low <= height * 0.85:
        return None
    outside = np.concatenate((activity[:low], activity[end:]))
    if not len(outside) or float(np.mean(outside)) > float(np.mean(activity[low:end])) * 0.2:
        return None
    return max(0, low - 1), min(height, end + 1)


def region_scores(features, fps):
    detector = ContentDetector(threshold=255, min_scene_len=0)
    scores = []
    for index, frame in enumerate(features):
        detector.process_frame(FrameTimecode(index, fps), np.ascontiguousarray(frame))
        scores.append(float(detector._frame_score or 0))
    return np.asarray(scores)


def select_auto_boundaries(signals, config):
    fps = float(signals["fps"])
    features = signals["full_features"]
    count = len(features)
    rows = active_rows(features)
    regions = {"full": (signals["full"], features),
               "roi": (signals["roi"], features[:, 2:26])}
    if rows is not None:
        active = features[:, rows[0]:rows[1]]
        regions["active"] = (region_scores(active, fps), active)
    floor = config.get("min_content_val", 15)
    threshold = config.get("threshold", 3)
    window = max(2, config.get("window_width", 4))
    span = max(2, round(fps * 0.15))
    minimum_gap = config.get("min_scene_len", 15)
    persistence = config.get("persistence_ratio", 1.5)
    candidates = {}
    # Ratios use a robust local baseline so a second nearby cut does not mask the first.
    for name, (scores, region) in regions.items():
        for frame in range(1, count):
            if scores[frame] < floor:
                continue
            neighbors = np.concatenate((scores[max(0, frame-window):frame],
                                        scores[frame+1:min(count, frame+window+1)]))
            baseline = max(1.0, float(np.median(neighbors))) if len(neighbors) else 1.0
            ratio = float(scores[frame]) / baseline
            if ratio < max(1.5, threshold * 0.7):
                continue
            if scores[frame] < max(scores[max(0, frame-1):min(count, frame+2)]):
                continue
            separation, stable = boundary_strength(region, frame, span)
            left = np.median(region[max(0, frame-span):frame], axis=0).astype(float)
            right = np.median(region[frame:min(count, frame+span)], axis=0).astype(float)
            changed = float(np.mean(np.mean(np.abs(left-right), axis=-1) >= 12))
            # Compare both halves of the local context: a one-frame flash returns to baseline.
            accepted = (frame >= span and count-frame >= span and separation >= 8
                        and stable >= persistence and changed >= 0.12 and ratio >= threshold)
            evidence = dict(region=name, score=round(float(scores[frame]), 3),
                            local_ratio=round(ratio, 3), separation=round(separation, 3),
                            persistence_ratio=round(stable, 3), changed_fraction=round(changed, 3))
            item = candidates.setdefault(frame, {"accepted": False, "evidence": [], "strength": 0})
            item["evidence"].append(evidence)
            if accepted:
                item["accepted"] = True
                item["strength"] = max(item["strength"], separation * min(stable, 10))
    chosen = []
    for frame, item in sorted(candidates.items()):
        if not item["accepted"] or frame < minimum_gap or count-frame < minimum_gap:
            continue
        if chosen and frame - chosen[-1] < minimum_gap:
            if item["strength"] > candidates[chosen[-1]]["strength"]:
                chosen[-1] = frame
        else:
            chosen.append(frame)
    diagnostics = []
    for frame, item in sorted(candidates.items()):
        kept = frame in chosen
        diagnostics.append({
            "candidate_frame": frame, "final_frame": frame if kept else None,
            "status": "accepted" if kept else "rejected",
            "reason": "persistent_multiregion_cut" if kept else "weak_or_short_change",
            "evidence": item["evidence"],
        })
    # Recheck only cached candidates inside long spans. Ambiguous changes are surfaced,
    # never inserted as certain cuts or split at arbitrary timestamps.
    for start, end in zip([0, *chosen], [*chosen, count]):
        if end-start < fps * 12:
            continue
        for item in diagnostics:
            frame = item["candidate_frame"]
            if start+minimum_gap <= frame < end-minimum_gap and item["status"] == "rejected":
                if any(e["separation"] >= 8 and e["changed_fraction"] >= 0.12
                       and e["persistence_ratio"] >= persistence for e in item["evidence"]):
                    item.update(status="needs_review", reason="ambiguous_change_in_long_scene")
        # Gradual changes can have no single high-scoring frame. Inspect cached
        # thumbnails sparsely and report an interval, not a fabricated exact cut.
        gradual = []
        for frame in range(start + max(minimum_gap, span), end - max(minimum_gap, span), span):
            evidence = []
            for name, (scores, region) in regions.items():
                if max(scores[max(start, frame-span):min(end, frame+span)]) >= floor:
                    continue
                separation, stable = boundary_strength(region, frame, span)
                if separation >= 12 and stable >= max(1.5, persistence):
                    evidence.append({"region": name, "separation": round(separation, 3),
                                     "persistence_ratio": round(stable, 3)})
            if evidence:
                if gradual and frame-gradual[-1]["end_frame"] <= span * 2:
                    gradual[-1]["end_frame"] = frame+span
                else:
                    gradual.append({
                        "candidate_frame": frame, "final_frame": None,
                        "start_frame": frame-span, "end_frame": frame+span,
                        "status": "needs_review", "reason": "gradual_or_camera_change",
                        "evidence": evidence,
                    })
        diagnostics.extend(gradual)
    settings = {"auto_version": 1, "active_rows_32": list(rows) if rows else None,
                "regions": list(regions), "long_scene_review_seconds": 12,
                "review_required": any(d["status"] == "needs_review" for d in diagnostics)}
    return chosen, diagnostics, settings
