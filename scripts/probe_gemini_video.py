"""Opt-in live video probe; uses the existing encrypted model directory."""
import argparse
import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import requests


ROOT = Path(__file__).resolve().parents[1]
PROMPT = (
    "Analyze the attached video itself. Return JSON with: visible_subjects, "
    "setting, actions_in_time_order (at least three observations with approximate "
    "timestamps), camera_changes, visible_text, audio_description, uncertainties. "
    "Describe concrete visual details, not generic guesses. Do not infer unseen "
    "content. If the video is inaccessible, say so explicitly."
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-id", type=int, default=41)
    parser.add_argument("--protocol", choices=["native", "chat", "responses"], default="native")
    parser.add_argument("--json-output", action="store_true")
    parser.add_argument("--production-scene", action="store_true")
    parser.add_argument("--pipeline", action="store_true")
    parser.add_argument("--prepared-clip", action="store_true",
                        help="Live model orchestration only; use the entire video as one prepared scene.")
    parser.add_argument("--analysis-python")
    parser.add_argument("--global-model-id", type=int)
    parser.add_argument("--scene-fallback-model-id", type=int)
    parser.add_argument("--global-fallback-model-id", type=int)
    parser.add_argument("--video", type=Path, required=True)
    args = parser.parse_args()
    if args.pipeline:
        if not args.global_model_id:
            parser.error("--pipeline requires --global-model-id")
        sys.path.insert(0, str(ROOT))
        from src.schemas.video_analysis import AnalyzeVideoRequest
        from src.service.video_analysis.semantic_analysis import analyze_video
        from contextlib import nullcontext

        os.environ["SCENE_MEDIA_ROOT"] = str(args.video.resolve().parent)
        if args.analysis_python:
            os.environ["SCENE_PYTHON"] = args.analysis_python
        preparation = nullcontext()
        if args.prepared_clip:
            import subprocess
            from fractions import Fraction
            from unittest.mock import patch, AsyncMock

            probe = json.loads(subprocess.check_output([
                os.getenv("SCENE_FFPROBE", "ffprobe"), "-v", "error", "-show_streams",
                "-show_format", "-of", "json", str(args.video.resolve()),
            ]))
            video = next(s for s in probe["streams"] if s["codec_type"] == "video")
            fps = float(Fraction(video["avg_frame_rate"]))
            duration_us = round(float(probe["format"]["duration"]) * 1e6)
            count = int(video.get("nb_frames") or round(duration_us / 1e6 * fps))
            prepared = {
                "detection": {
                    "detector": "prepared_clip_probe", "threshold": 0, "min_scene_len": 1,
                    "fps": fps, "frame_count": count, "duration_us": duration_us,
                    "scene_count": 1, "warnings": ["Live probe: detection/splitting bypassed; input is one prepared clip."],
                    "scenes": [{
                        "index": 1, "start_frame": 0, "end_frame": count,
                        "start_us": 0, "end_us": duration_us, "duration_us": duration_us,
                        "start_timecode": "0", "end_timecode": str(duration_us / 1e6),
                    }],
                },
                "clips": [{"scene_index": 1, "path": str(args.video.resolve())}],
            }
            preparation = patch(
                "src.service.video_analysis.semantic_analysis._run_worker",
                AsyncMock(return_value=prepared),
            )
        with preparation:
            result = asyncio.run(analyze_video(AnalyzeVideoRequest(
                video_path=args.video.name, use_transcription=False,
                scene_model_id=args.model_id, global_model_id=args.global_model_id,
                scene_fallback_model_id=args.scene_fallback_model_id,
                global_fallback_model_id=args.global_fallback_model_id,
            )))
        target = ROOT / "reports" / "gemini-gpt-pipeline-probe.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        print(json.dumps({
            "scene_count": result.detection.scene_count,
            "scenes": [{"index": s.scene_index, "status": s.analysis_status,
                        "model_id": s.model_id, "fallback_used": s.fallback_used}
                       for s in result.semantics],
            "global_model_used_id": result.global_model_used_id,
            "global_enriched": result.analysis_report["global_model_enriched"],
            "prepared_clip_probe": args.prepared_clip,
            "fallbacks": result.fallbacks, "warnings": result.warnings,
            "timings": result.analysis_report["timings"], "report": str(target),
        }, ensure_ascii=True, indent=2))
        return
    spec = importlib.util.spec_from_file_location(
        "probe_model_config", ROOT / "src/service/video_analysis/model_config_store.py"
    )
    config_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config_module)
    config = config_module.resolve_model(args.model_id, "scene")
    if args.production_scene:
        sys.path.insert(0, str(ROOT))
        from src.schemas.video_analysis import SceneSemanticAnalysis
        from src.service.video_analysis.model_client import analyze_scene_with_responses
        from src.service.video_analysis.responses_client import Scope

        started = time.monotonic()
        scope = Scope()
        result = {"model_id": args.model_id, "model": config["model"],
                  "video": str(args.video), "production_scene": True}
        try:
            value = analyze_scene_with_responses(
                config, {"index": 1}, "", str(args.video.resolve()), scope=scope,
            )
            validated = SceneSemanticAnalysis.model_validate({
                **value, "scene_index": 1, "analysis_status": "completed",
            }, strict=True)
            if not validated.content or validated.hook_score is None:
                raise ValueError("Incomplete scene output")
            result.update(success=True, result=validated.model_dump())
        except Exception as exc:
            result.update(success=False, error=type(exc).__name__, message=str(exc)[:300])
        result.update(elapsed_seconds=round(time.monotonic() - started, 3), calls=scope.calls)
        target = ROOT / "reports" / ("gemini-video-probe-%s-production.json" % args.model_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return
    encoded = base64.b64encode(args.video.read_bytes()).decode("ascii")
    origin = config["base_url"].rstrip("/")
    headers = {"Authorization": "Bearer " + config["api_key"]}
    if args.protocol == "native":
        url = origin + "/v1beta/models/" + config["model"] + ":generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [
                {"inlineData": {"mimeType": "video/mp4", "data": encoded}},
                {"text": PROMPT},
            ]}],
            "generationConfig": {"maxOutputTokens": 1800},
        }
        if args.json_output:
            payload["generationConfig"]["responseMimeType"] = "application/json"
    elif args.protocol == "chat":
        url = origin + "/v1/chat/completions"
        payload = {
            "model": config["model"],
            "messages": [{"role": "user", "content": [
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + encoded}},
                {"type": "text", "text": PROMPT},
            ]}],
            "max_tokens": 1800,
        }
    else:
        url = origin + "/responses"
        payload = {
            "model": config["model"], "store": False,
            "input": [{"role": "user", "content": [
                {"type": "input_video", "video_url": "data:video/mp4;base64," + encoded},
                {"type": "input_text", "text": PROMPT},
            ]}],
            "max_output_tokens": 1800,
        }
    result = {
        "model_id": args.model_id, "model": config["model"],
        "protocol": args.protocol, "endpoint": url,
        "video": str(args.video), "video_bytes": args.video.stat().st_size,
        "input_method": "complete_mp4_base64_no_client_frame_extraction",
        "json_output_requested": args.json_output,
    }
    started = time.monotonic()
    try:
        response = requests.post(
            url, headers=headers, json=payload, timeout=(15, 180),
            allow_redirects=False,
        )
        result["status_code"] = response.status_code
        # Never persist request data or credentials, even if echoed by a gateway.
        safe = response.text.replace(config["api_key"], "[REDACTED]").replace(encoded, "[VIDEO]")
        try:
            result["response"] = json.loads(safe)
            if args.protocol == "native" and response.status_code == 200:
                text = "".join(
                    part.get("text", "")
                    for candidate in result["response"].get("candidates", [])
                    for part in candidate.get("content", {}).get("parts", [])
                    if not part.get("thought")
                )
                try:
                    result["parsed_model_json"] = json.loads(text)
                    result["valid_model_json"] = True
                except ValueError:
                    result["valid_model_json"] = False
        except ValueError:
            result["response"] = safe[:2000]
    except requests.RequestException as exc:
        result["error"] = type(exc).__name__
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    target = ROOT / "reports" / ("gemini-video-probe-%s-%s%s.json" % (
        args.model_id, args.protocol, "-structured" if args.json_output else ""
    ))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    print("Report: " + str(target))


if __name__ == "__main__":
    main()
