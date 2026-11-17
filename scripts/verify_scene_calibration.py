"""Verify deployed enhanced defaults against the independently cached evaluation."""

import argparse
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    args = parser.parse_args()
    baseline = {r["index"]: r for r in json.loads(args.baseline.read_text(encoding="utf-8-sig"))}
    evaluation = json.loads(args.evaluation.read_text(encoding="utf-8"))
    responses = []
    for row in evaluation["videos"]:
        index = row["index"]
        payload = {
            "video_path": baseline[index]["request"]["video_path"],
            "detector": "enhanced", "include_diagnostics": True,
        }
        request = Request(
            args.api + "/openapi/capcut-mate/v1/detect_scenes",
            data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
        )
        started = time.monotonic()
        with urlopen(request, timeout=200) as response:
            result = json.load(response)
        assert result["code"] == 0, result
        fps = result["fps"]
        expected = [round(t * fps) for t in row["enhanced_cuts"]]
        actual = [scene["end_frame"] for scene in result["scenes"][:-1]]
        assert actual == expected, (index, actual, expected)
        responses.append({
            "index": index, "filename": row["filename"], "request": payload,
            "elapsed_seconds": round(time.monotonic() - started, 2), "response": result,
        })
        (args.evaluation.parent / "api-results.json").write_text(
            json.dumps(responses, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Video {index}: PASS, {result['scene_count']} scenes, {responses[-1]['elapsed_seconds']}s", flush=True)


if __name__ == "__main__":
    main()
