"""Create review contact sheets and compare API boundaries with existing labels."""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from calibrate_scene_detection import aggregate, match_cuts


def contact_sheets(video, selections, folder, prefix, title):
    if not selections:
        return []
    cap = cv2.VideoCapture(str(video))
    count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    wanted = {max(0, min(count - 1, frame)) for _, frames in selections for frame in frames}
    thumbnails = {}
    frame_index = 0
    try:
        while wanted:
            ok, image = cap.read()
            if not ok:
                raise RuntimeError(f"Could not read requested source frames: {video}")
            if frame_index in wanted:
                height, width = image.shape[:2]
                ratio = min(220 / width, 300 / height)
                scaled = cv2.resize(image, (round(width * ratio), round(height * ratio)))
                tile = np.full((300, 220, 3), 240, np.uint8)
                y, x = (300 - scaled.shape[0]) // 2, (220 - scaled.shape[1]) // 2
                tile[y:y + scaled.shape[0], x:x + scaled.shape[1]] = scaled
                thumbnails[frame_index] = tile
                wanted.remove(frame_index)
            frame_index += 1
    finally:
        cap.release()
    names = []
    for page, offset in enumerate(range(0, len(selections), 4), 1):
        rows = selections[offset:offset + 4]
        cols = len(rows[0][1])
        canvas = np.full((50 + 350 * len(rows), 220 * cols, 3), 255, np.uint8)
        cv2.putText(canvas, title, (8, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1)
        for row, (label, frames) in enumerate(rows):
            top = 50 + row * 350
            cv2.putText(canvas, label, (8, top + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 0, 0), 1)
            for col, frame in enumerate(frames):
                frame = max(0, min(count - 1, frame))
                canvas[top + 45:top + 345, col * 220:(col + 1) * 220] = thumbnails[frame]
                cv2.putText(canvas, f"frame {frame}", (col * 220 + 8, top + 39),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 1)
        name = f"{prefix}-{page:02d}.jpg"
        if not cv2.imwrite(str(folder / name), canvas):
            raise RuntimeError("Could not write contact sheet")
        names.append(name)
    return names


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--batch", required=True)
    args = parser.parse_args()
    cv2.setNumThreads(1)
    folder = args.root / "reports" / args.batch
    results = json.loads((folder / "results.json").read_text(encoding="utf-8-sig"))
    labels = {v["filename"]: v for v in json.loads(
        (args.root / "reports/desktop4-20260917-enhanced/annotations.json").read_text(encoding="utf-8-sig")
    )}
    baseline = {v["filename"]: v for v in json.loads(
        (args.root / "reports/desktop4-20260917-enhanced/api-results.json").read_text(encoding="utf-8-sig")
    )}
    text = [
        "# 人工对照与画面索引", "",
        "全镜头总览每行依次为首帧、中间帧、尾帧。边界截图每行依次为切点前 2 帧、前 1 帧、切点帧、后 1 帧、后 2 帧。",
        "边界截图优先列出待复核和有移动的切点；若全部通过，则展示变化分离度最低的两个边界。", "",
        "人工时间沿用此前 6 个视频标注，不是新标注；业务合并和粗略时间偏差会影响指标。",
        "检测/复核匹配率不代表导出画质，也不代表平台视频相似度。", "",
    ]
    comparison = []
    for entry in results:
        index = entry["index"]
        detection = entry["detect"]["response"]
        split = entry["split"]["response"]
        text += [f"## {index}. {entry['filename']}", ""]
        if detection["code"] != 0:
            text.append("检测失败，详见主报告。")
            continue
        fps = detection["fps"]
        source = args.root / "media" / entry["detect"]["request"]["video_path"]
        scenes = detection["scenes"]
        cuts = [s["end_frame"] / fps for s in scenes[:-1]]
        reviews = split.get("boundary_reviews", [])
        refined = [r["selected_frame"] / fps for r in reviews] if split["code"] == 0 else None
        old = baseline.get(entry["filename"])
        if old:
            unchanged = [s["end_frame"] for s in scenes] == [s["end_frame"] for s in old["response"]["scenes"]]
            text.append(f"与 9 月 17 日第二版检测结果一致：{unchanged}。")
        label = labels.get(entry["filename"])
        if label:
            human = [row[1] for row in label["intervals"][:-1]]
            stats = {"index": index, "filename": entry["filename"], "human_scenes": len(human) + 1}
            text += ["", "| 容差 | 阶段 | 匹配切点 | 机器未匹配 | 人工未匹配 | F1 |",
                     "|---|---|---:|---:|---:|---:|"]
            for tolerance in [0.2, 0.5]:
                for name, times in [("detect", cuts), ("review", refined)]:
                    if times is None:
                        continue
                    metrics = match_cuts(times, human, tolerance)
                    metrics.update(aggregate([metrics]))
                    stats[f"{name}_{tolerance}"] = metrics
                    text.append(f"| ±{tolerance}s | {name} | {metrics['tp']} | {metrics['fp']} | {metrics['fn']} | {metrics['f1']:.3f} |")
            comparison.append(stats)
        else:
            text.append("该视频暂无人工标注，不计算准确率。")
        overview = [(f"Scene {s['index']} | {s['start_frame']/fps:.3f}-{s['end_frame']/fps:.3f}s",
                     [s["start_frame"], (s["start_frame"] + s["end_frame"] - 1) // 2, s["end_frame"] - 1])
                    for s in scenes]
        names = contact_sheets(source, overview, folder, f"overview-{index:02d}", f"Video {index}: first / middle / last")
        text += ["", "### 全镜头总览", ""]
        text += [f"[总览 {n + 1}]({name})" for n, name in enumerate(names)]
        flagged = [r for r in reviews if r["status"] == "needs_review" or r["shift_frames"] != 0]
        selected = flagged or sorted(reviews, key=lambda r: r["separation"])[:2]
        selections = []
        for row in selected:
            frame = row["selected_frame"]
            selections.append((
                f"{row['status']} {row['detected_frame']}->{frame} | {frame/fps:.3f}s",
                list(range(frame - 2, frame + 3)),
            ))
        names = contact_sheets(source, selections, folder, f"boundaries-{index:02d}",
                               f"Video {index}: -2 / -1 / CUT / +1 / +2")
        text += ["", "### 边界复核", ""]
        text += [f"![边界截图 {n + 1}]({name})" for n, name in enumerate(names)]
        (folder / "visual-review.md").write_text("\n".join(text), encoding="utf-8")
        (folder / "label-comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Video {index}: overview {len(overview)} scenes; {len(selected)} boundary rows", flush=True)


if __name__ == "__main__":
    main()
