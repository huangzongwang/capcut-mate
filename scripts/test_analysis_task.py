"""Submit and monitor an asynchronous video-analysis task.

Example:
  python scripts/test_analysis_task.py ^
    --video-path "desktop4-20260917-baseline/08.mp4" ^
    --scene-model-id 14 --global-model-id 13
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests


def request(method: str, url: str, timeout: int = 30, **kwargs) -> dict:
    response = requests.request(method, url, timeout=timeout, **kwargs)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"接口返回非 JSON: {response.text[:500]}") from exc
    if response.status_code >= 400 or payload.get("code", 0) not in (0, None):
        raise RuntimeError(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="测试异步视频分析任务")
    parser.add_argument("--api-url", default="http://localhost:30000")
    parser.add_argument("--video-path", required=True)
    parser.add_argument("--scene-model-id", type=int)
    parser.add_argument("--global-model-id", type=int)
    parser.add_argument("--scene-fallback-model-id", type=int)
    parser.add_argument("--global-fallback-model-id", type=int)
    parser.add_argument("--no-transcription", action="store_true")
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--no-external-model", action="store_true")
    parser.add_argument("--sample-points", choices=("auto", "1", "3"), default="auto")
    parser.add_argument("--video-fps", type=float, default=2.0)
    parser.add_argument("--poll-interval", type=int, default=3)
    parser.add_argument("--output", default="reports/async-analysis-result.json")
    args = parser.parse_args()

    root = args.api_url.rstrip("/")
    endpoint = f"{root}/openapi/capcut-mate/v1/analyze_video_tasks"
    payload = {
        "video_path": args.video_path,
        "detector": "enhanced",
        "sample_points": args.sample_points if args.sample_points == "auto" else int(args.sample_points),
        "use_transcription": not args.no_transcription,
        "use_ocr": False,
        "video_fps": args.video_fps,
        "use_external_model": not args.no_external_model,
    }
    if not args.no_external_model and (not args.scene_model_id or not args.global_model_id):
        parser.error("Cloud analysis requires --scene-model-id and --global-model-id")
    payload["scene_model_id"] = args.scene_model_id
    payload["global_model_id"] = args.global_model_id
    payload["scene_fallback_model_id"] = args.scene_fallback_model_id
    payload["global_fallback_model_id"] = args.global_fallback_model_id

    task = request("POST", endpoint, json=payload)
    task_id = task["task_id"]
    print(f"任务已提交: {task_id}")

    last_line = ""
    while True:
        status = request("GET", f"{endpoint}/{task_id}")
        line = (
            f"状态={status['status']} "
            f"阶段={status['stage']} "
            f"进度={status['progress']}%"
        )
        if line != last_line:
            print(line)
            last_line = line
        if status["status"] in ("completed", "completed_with_warnings", "failed"):
            break
        time.sleep(max(1, args.poll_interval))

    if status["status"] == "failed":
        print(f"分析失败: {status.get('error_message', '')}", file=sys.stderr)
        return 1

    result_payload = request("GET", f"{endpoint}/{task_id}/result")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result_payload["result"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report = result_payload["result"]
    scenes = report.get("detection", {}).get("scenes", [])
    semantics = report.get("semantics", [])
    print(f"分析完成: {output.resolve()}")
    print(f"分镜数量: {len(scenes)}")
    print(f"可替换镜头: {sum(1 for item in semantics if item.get('replaceable'))}")
    print(f"吸睛镜头: {report.get('viral_analysis', {}).get('hook_scenes', [])}")
    if status["status"] == "completed_with_warnings":
        print("任务完成但有警告:")
        print(status.get("error_message", ""))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (requests.RequestException, RuntimeError) as exc:
        print(f"请求失败: {exc}", file=sys.stderr)
        raise SystemExit(1)
