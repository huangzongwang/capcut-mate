"""Offline labeled-video evaluation. Run in the isolated Docker detector environment."""

import argparse
import csv
import importlib.util
import itertools
import json
from pathlib import Path
import time

import cv2
import numpy as np
from scenedetect import open_video

spec = importlib.util.spec_from_file_location(
    "enhanced", Path(__file__).parents[1] / "src/service/video_analysis/enhanced.py"
)
enhanced = importlib.util.module_from_spec(spec)
spec.loader.exec_module(enhanced)


def seconds(value):
    minute, second = value.split(":")
    return int(minute) * 60 + float(second)


def load_annotations(path, baseline):
    groups = {}
    for row in csv.reader(path.open(encoding="utf-8-sig"), delimiter="\t"):
        if not row:
            continue
        key = int(row[0])
        group = groups.setdefault(key, {"index": key, "filename": row[1], "intervals": []})
        group["intervals"].append([seconds(row[3]), seconds(row[4])])
    groups[8] = {
        "index": 8, "filename": "555.mp4",
        "intervals": [[0, 8], [8, 20.1], [20.1, 21.2], [21.2, 34.3],
                      [34.3, 36.2], [36.2, 64.3], [64.3, 72.1]],
    }
    for key, group in groups.items():
        intervals = group["intervals"]
        previous = 0
        fps = baseline[key]["response"]["fps"]
        duration = baseline[key]["response"]["duration_us"] / 1e6
        for start, end in intervals:
            if abs(start - previous) > 1e-6 or end <= start:
                raise ValueError(f"Invalid annotation continuity in video {key}")
            previous = end
        group["duration_difference_seconds"] = round(intervals[-1][1] - duration, 6)
        if abs(group["duration_difference_seconds"]) > 0.5:
            raise ValueError(f"Annotation duration does not match video {key}")
        group["cuts"] = [end for _, end in intervals[:-1]]
        group["recomputed_scenes"] = [
            {"index": i + 1, "start_seconds": start, "end_seconds": end,
             "duration_seconds": round(end - start, 6),
             "start_frame_approx": round(start * fps), "end_frame_approx": round(end * fps)}
            for i, (start, end) in enumerate(intervals)
        ]
    return groups


def match_cuts(predicted, truth, tolerance):
    """Ordered, one-to-one matching: maximize matches, then minimize total timing error."""
    # A small DP over <100 boundaries avoids greedy matching ambiguous close cuts.
    table = [[(0, 0.0, []) for _ in range(len(truth) + 1)] for _ in range(len(predicted) + 1)]
    for i, p in enumerate(predicted, 1):
        for j, t in enumerate(truth, 1):
            options = [table[i - 1][j], table[i][j - 1]]
            if abs(p - t) <= tolerance + 1e-8:
                count, error, pairs = table[i - 1][j - 1]
                options.append((count + 1, error + abs(p - t), pairs + [(i - 1, j - 1)]))
            table[i][j] = max(options, key=lambda item: (item[0], -item[1]))
    count, error, pairs = table[-1][-1]
    return {
        "tp": count, "fp": len(predicted) - count, "fn": len(truth) - count,
        "absolute_error": error, "pairs": pairs,
    }


def aggregate(items):
    tp, fp, fn = (sum(item[key] for item in items) for key in ("tp", "fp", "fn"))
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": tp / max(tp + fp, 1), "recall": tp / max(tp + fn, 1),
        "f1": 2 * tp / max(2 * tp + fp + fn, 1),
        "mae_seconds": sum(item["absolute_error"] for item in items) / max(tp, 1),
    }


def parameter_grid():
    # Search knobs independently of filenames or annotated times.
    for threshold, minimum, window, region in itertools.product(
        [2.5, 3.0, 4.0, 5.0], [15.0, 20.0, 25.0, 30.0], [2, 4, 8], ["full", "roi"],
    ):
        yield {
            "threshold": threshold, "min_content_val": minimum, "window_width": window,
            "score_region": region, "min_scene_len": 15,
            "persistence_ratio": 0.0, "refine_seconds": 0.0,
        }


def evaluate(config, signals, annotations):
    cuts = {}
    metrics = {}
    for key, signal in signals.items():
        frames, _ = enhanced.select_boundaries(signal, config)
        cuts[key] = [frame / float(signal["fps"]) for frame in frames]
        metrics[key] = match_cuts(cuts[key], annotations[key]["cuts"], 0.5)
    return cuts, metrics


def rank(metrics):
    total = aggregate(metrics)
    return (total["f1"], total["precision"], -total["mae_seconds"])


def capture_sheet(path, candidates, fps, output):
    cap = cv2.VideoCapture(str(path))
    cells = []
    for label, moment in candidates:
        row = []
        for offset in [-0.2, 0.0, 0.2]:
            frame_number = max(0, round((moment + offset) * fps))
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
            ok, frame = cap.read()
            tile = np.full((260, 210, 3), 245, dtype=np.uint8)
            if ok:
                scale = min(210 / frame.shape[1], 222 / frame.shape[0])
                image = cv2.resize(frame, (round(frame.shape[1] * scale), round(frame.shape[0] * scale)))
                x = (210 - image.shape[1]) // 2
                tile[30:30 + image.shape[0], x:x + image.shape[1]] = image
            cv2.putText(tile, f"{label} {frame_number / fps:.3f}s", (5, 19), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (20, 20, 20), 1)
            row.append(tile)
        cells.append(np.concatenate(row, axis=1))
    cap.release()
    if cells:
        cv2.imwrite(str(output), np.concatenate(cells, axis=0))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--media-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extract-only", action="store_true")
    args = parser.parse_args()
    cv2.setNumThreads(1)
    args.output.mkdir(parents=True, exist_ok=True)
    baseline = {item["index"]: item for item in json.loads(args.baseline.read_text(encoding="utf-8-sig"))}
    annotations = load_annotations(args.annotations, baseline)
    (args.output / "annotations.json").write_text(json.dumps(list(annotations.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    with (args.output / "annotations.csv").open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.writer(output)
        writer.writerow(["video_index", "filename", "scene", "start_seconds", "end_seconds",
                         "duration_seconds", "start_frame_approx", "end_frame_approx"])
        for key, annotation in annotations.items():
            for scene in annotation["recomputed_scenes"]:
                writer.writerow([key, annotation["filename"], *scene.values()])
    signals = {}
    for key in annotations:
        cache = args.output / f"signals-{key:02d}.npz"
        if not cache.exists():
            path = args.media_root / baseline[key]["request"]["video_path"]
            print(f"Extract video {key}: {path.name}", flush=True)
            started = time.monotonic()
            video = open_video(str(path))
            signal = enhanced.collect_signals(video, int(float(video.frame_rate) * 600))
            np.savez_compressed(cache, **signal)
            print(f"  {signal['decoded']} frames in {time.monotonic() - started:.1f}s", flush=True)
        with np.load(cache, allow_pickle=False) as cached:
            signals[key] = dict(cached)
    if args.extract_only:
        return
    experiments = []
    grid = list(parameter_grid())
    for index, config in enumerate(grid):
        cuts, metrics = evaluate(config, signals, annotations)
        experiments.append({"config": config, "cuts": cuts, "metrics": metrics})
        if index % 32 == 0:
            print(f"Parameters {index}/{len(grid)}", flush=True)
    # Post-processing is tested around several competitive settings, not just one lucky fit.
    top = sorted(experiments, key=lambda item: rank(item["metrics"].values()), reverse=True)[:6]
    for candidate in top:
        for persistence, refinement in itertools.product([1.5, 2.5, 4.0], [0.0, 0.2]):
            config = {**candidate["config"], "persistence_ratio": persistence, "refine_seconds": refinement}
            cuts, metrics = evaluate(config, signals, annotations)
            experiments.append({"config": config, "cuts": cuts, "metrics": metrics})
    best = max(experiments, key=lambda item: rank(item["metrics"].values()))
    # Leave one entire video out of parameter selection. Use the complete predefined
    # parameter grid only, not the globally-selected post-processing shortlist.
    folds = []
    for held in annotations:
        selected = max(experiments[:len(grid)], key=lambda item: rank(
            [metric for key, metric in item["metrics"].items() if key != held]
        ))
        folds.append({"held_out": held, "config": selected["config"], "metrics": selected["metrics"][held]})
    summary = {
        "best_config": best["config"],
        "experimental_fit_only": True,
        "leave_one_video_out": {"folds": folds, "aggregate": aggregate([fold["metrics"] for fold in folds])},
        "videos": [], "aggregate": {},
    }
    for tolerance in [0.2, 0.5]:
        base_metrics = []
        new_metrics = []
        for key, annotation in annotations.items():
            base = [scene["end_us"] / 1e6 for scene in baseline[key]["response"]["scenes"][:-1]]
            base_metrics.append(match_cuts(base, annotation["cuts"], tolerance))
            new_metrics.append(match_cuts(best["cuts"][key], annotation["cuts"], tolerance))
        summary["aggregate"][str(tolerance)] = {"baseline": aggregate(base_metrics), "enhanced": aggregate(new_metrics)}
    for key, annotation in annotations.items():
        base = [scene["end_us"] / 1e6 for scene in baseline[key]["response"]["scenes"][:-1]]
        frames, decisions = enhanced.select_boundaries(signals[key], best["config"])
        summary["videos"].append({
            "index": key, "filename": annotation["filename"],
            "human_cuts": annotation["cuts"], "baseline_cuts": base,
            "enhanced_cuts": best["cuts"][key], "decisions": decisions,
            "baseline_metrics": match_cuts(base, annotation["cuts"], 0.5),
            "enhanced_metrics": best["metrics"][key],
        })
    (args.output / "evaluation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "experiments.json").write_text(json.dumps([
        {"config": item["config"], "metrics": aggregate(item["metrics"].values())}
        for item in experiments
    ], indent=2), encoding="utf-8")
    print(json.dumps(summary["aggregate"], indent=2), flush=True)
    print("Selected: " + json.dumps(best["config"]), flush=True)
    lines = [
        "# 镜头检测增强对比报告", "",
        "人工时间按小数秒解析，仅使用人工开始/结束时间；旧时长、帧号、微秒列已忽略并重算。", "",
        "人工标注精度为 0.1 秒，不视为逐帧真值。未匹配的切点不一定是物理误切，也可能是语义合并或标注偏差。",
        "视频总时长仍以实际解码为准；人工末尾存在 0.02～0.33 秒差异，评测不包括视频起点与终点。", "",
        f"参数对照：{len(grid)} 组基础配置 + {len(experiments) - len(grid)} 组复核/精修配置。全画面与上下裁剪区域均参与实验。", "",
        "## 总体结果", "",
        "| 时间容差 | 方案 | 匹配切点 | 未匹配机器切点 | 未匹配人工切点 | Precision | Recall | F1 | 匹配点平均误差 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for tolerance, modes in summary["aggregate"].items():
        for label, metric in modes.items():
            lines.append(
                f"| ±{tolerance}s | {label} | {metric['tp']} | {metric['fp']} | {metric['fn']} | "
                f"{metric['precision']:.1%} | {metric['recall']:.1%} | {metric['f1']:.1%} | {metric['mae_seconds']:.3f}s |"
            )
    lines += [
        "", "增强配置是在这 6 条上选出的实验配置，以上不是独立测试集成绩，不代表总体准确率。",
        f"基础参数网格的逐视频留一验证 F1：{summary['leave_one_video_out']['aggregate']['f1']:.1%}（±0.5 秒）。",
        "留一验证每折仅用其余 5 条选择基础参数；未把全数据选出的复核短名单冒充留一结果。", "",
        "## 推荐实验参数", "", "```json", json.dumps(best["config"], indent=2), "```", "",
        "旧 adaptive 默认保留；新配置通过 detector=enhanced 显式开启。",
        "局部精修已实现并参与对照，但没有提升本批最优结果，因此默认关闭。没有对切点统一提前或对齐人工标注。", "",
        "## 文件说明", "",
        "- annotations.csv：重新计算的人工区间，帧号为时间换算近似值。",
        "- comparison.csv：每条视频全部原始/增强/人工区间，可在 Excel 中对比。",
        "- boundary_matches.csv：±0.5 秒下一对一切点配对、偏移和未匹配记录。",
        "- evaluation.json：逐视频、两种时间容差、留一验证和增强决策明细。",
        "- experiments.json：全部参数配置的指标。",
        "- 截图每行依次为切点前 0.2 秒、切点、切点后 0.2 秒；BASE=原算法，HUMAN=人工，NEW=增强。", "",
    ]
    comparison = []
    matches = []
    for row in summary["videos"]:
        key = row["index"]
        lines.extend([f"## {key}. {row['filename']}", "",
                      f"人工镜头：{len(row['human_cuts']) + 1}；原算法：{len(row['baseline_cuts']) + 1}；增强：{len(row['enhanced_cuts']) + 1}。", "",
                      f"人工切点（秒）：{row['human_cuts']}", "",
                      f"增强切点（秒）：{[round(t, 3) for t in row['enhanced_cuts']]}", ""])
        for mode in ["human", "baseline", "enhanced"]:
            duration = (annotations[key]["intervals"][-1][1] if mode == "human"
                        else baseline[key]["response"]["duration_us"] / 1e6)
            boundaries = [0, *row[f"{mode}_cuts"], duration]
            for i, (start, end) in enumerate(zip(boundaries, boundaries[1:]), 1):
                comparison.append([key, row["filename"], mode, i, round(start, 6), round(end, 6), round(end - start, 6)])
        for mode in ["baseline", "enhanced"]:
            metric = row[f"{mode}_metrics"]
            pairs = dict(metric["pairs"])
            for i, cut in enumerate(row[f"{mode}_cuts"]):
                reference = row["human_cuts"][pairs[i]] if i in pairs else None
                matches.append([key, mode, round(cut, 6), reference,
                                round(cut - reference, 6) if reference is not None else "",
                                "matched" if reference is not None else "unmatched_machine"])
            matched_truth = set(pairs.values())
            for j, cut in enumerate(row["human_cuts"]):
                if j not in matched_truth:
                    matches.append([key, mode, "", cut, "", "unmatched_human"])
        disputes = []
        metric = row["baseline_metrics"]
        matched_base = {i for i, _ in metric["pairs"]}
        matched_truth = {j for _, j in metric["pairs"]}
        disputes.extend(("BASE", t) for i, t in enumerate(row["baseline_cuts"]) if i not in matched_base)
        disputes.extend(("HUMAN", t) for j, t in enumerate(row["human_cuts"]) if j not in matched_truth)
        metric = row["enhanced_metrics"]
        matched_new = {i for i, _ in metric["pairs"]}
        disputes.extend(("NEW", t) for i, t in enumerate(row["enhanced_cuts"]) if i not in matched_new)
        for page, start in enumerate(range(0, len(disputes), 6), 1):
            name = f"disputes-{key:02d}-{page:02d}.jpg"
            if not (args.output / name).exists():
                capture_sheet(args.media_root / baseline[key]["request"]["video_path"],
                              disputes[start:start + 6], float(signals[key]["fps"]), args.output / name)
            lines.append(f"![Video {key}, dispute page {page}]({name})")
        print(f"Screenshots video {key}: {len(disputes)} disputed boundaries", flush=True)
    (args.output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    for name, headers, rows in [
        ("comparison.csv", ["video", "filename", "mode", "scene", "start_seconds", "end_seconds", "duration_seconds"], comparison),
        ("boundary_matches.csv", ["video", "mode", "machine_seconds", "human_seconds", "offset_seconds", "status"], matches),
    ]:
        with (args.output / name).open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.writer(output)
            writer.writerow(headers)
            writer.writerows(rows)


if __name__ == "__main__":
    main()
