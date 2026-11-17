"""List online models and test analysis. Model configuration is managed upstream."""
import argparse
import json
from pathlib import Path

from test_analysis_task import request


def main():
    parser = argparse.ArgumentParser(description="List online models or test video analysis")
    parser.add_argument("--api-url", default="http://localhost:30000")
    parser.add_argument("--video-path")
    parser.add_argument("--scene-model-id", type=int)
    parser.add_argument("--global-model-id", type=int)
    parser.add_argument("--scene-fallback-model-id", type=int)
    parser.add_argument("--global-fallback-model-id", type=int)
    parser.add_argument("--no-transcription", action="store_true")
    parser.add_argument("--output", default="reports/model-analysis-result.json")
    args = parser.parse_args()
    root = args.api_url.rstrip("/") + "/openapi/capcut-mate/v1"
    print(json.dumps(request("GET", root + "/models"), ensure_ascii=False, indent=2))
    if not args.video_path:
        return
    if not args.scene_model_id or not args.global_model_id:
        parser.error("--video-path requires --scene-model-id and --global-model-id")
    result = request("POST", root + "/analyze_video", timeout=1800, json={
        "video_path": args.video_path, "detector": "enhanced",
        "scene_model_id": args.scene_model_id, "global_model_id": args.global_model_id,
        "scene_fallback_model_id": args.scene_fallback_model_id,
        "global_fallback_model_id": args.global_fallback_model_id,
        "use_transcription": not args.no_transcription, "use_external_model": True,
    })
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output.resolve())


if __name__ == "__main__":
    main()
