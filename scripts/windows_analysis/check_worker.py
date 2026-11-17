"""Run with the isolated analysis Python, not the API environment."""
import argparse
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def check(transcribe_sample=None):
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("The analysis environment must use Python 3.11")
    results = {"python": sys.executable, "modules": {}, "media_tools": {}}
    for name in ("cv2", "scenedetect", "numpy", "faster_whisper", "ctranslate2", "requests"):
        module = importlib.import_module(name)
        results["modules"][name] = getattr(module, "__version__", "installed")
    for name in ("ffmpeg", "ffprobe"):
        path = shutil.which(os.getenv("SCENE_" + name.upper(), name))
        if not path:
            raise RuntimeError(f"{name} not found in PATH")
        subprocess.run([path, "-version"], check=True, capture_output=True, timeout=15)
        results["media_tools"][name] = path
    help_text = subprocess.run(
        [results["media_tools"]["ffmpeg"], "-h", "full"],
        check=True, capture_output=True, timeout=15,
    ).stdout
    if b"-fps_mode" not in help_text:
        raise RuntimeError("FFmpeg is too old: -fps_mode is required. Install FFmpeg >=6 in capcut-analysis or set SCENE_FFMPEG/SCENE_FFPROBE.")
    import ctranslate2
    results["cuda_device_count"] = ctranslate2.get_cuda_device_count()
    results["whisper_device"] = os.getenv("SCENE_WHISPER_DEVICE", "auto")
    results["model_cache"] = os.getenv("SCENE_WHISPER_ROOT", "")
    results["model_path_override"] = os.getenv("SCENE_WHISPER_MODEL_PATH", "")
    if results["whisper_device"] == "cuda" and not results["cuda_device_count"]:
        raise RuntimeError("CUDA was requested but CTranslate2 found no GPU")
    results["inference_verified"] = False
    if transcribe_sample:
        worker_dir = Path(__file__).resolve().parents[2] / "src" / "service" / "video_analysis"
        sys.path.insert(0, str(worker_dir))
        from transcription import transcribe
        transcript = transcribe(transcribe_sample)
        results.update(inference_verified=True, inference_device=transcript["device"],
                       segment_count=len(transcript["segments"]))
    print(json.dumps(results, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcribe-sample", help="Optional short audio/video; loads large-v3 and runs inference")
    args = parser.parse_args()
    check(args.transcribe_sample)
