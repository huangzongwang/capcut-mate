"""Validate delivered geometry and fit the complete picture to the requested canvas."""
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import shutil
import subprocess


class OutputVideoError(RuntimeError):
    """Safe message for task responses."""


def tool(name):
    return os.getenv("GROK_VIDEO_" + name.upper()) or os.getenv("SCENE_" + name.upper()) or name


def probe(path):
    try:
        result = subprocess.run(
            [tool("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        )
        streams = json.loads(result.stdout)["streams"]
        video = next(s for s in streams if s["codec_type"] == "video"
                     and not s.get("disposition", {}).get("attached_pic"))
        width, height = int(video["width"]), int(video["height"])
        sar = Fraction(video.get("sample_aspect_ratio", "1:1").replace(":", "/"))
        if sar <= 0:
            raise ValueError
        rotation = next((int(s["rotation"]) for s in video.get("side_data_list", [])
                         if "rotation" in s), int(video.get("tags", {}).get("rotate", 0))) % 360
        dar = Fraction(width, height) * sar
        if rotation in (90, 270):
            dar = 1 / dar
        duration = float(video["duration"])
        if width <= 0 or height <= 0 or not math.isfinite(duration) or duration <= 0:
            raise ValueError
        return dict(width=width, height=height, display_aspect_ratio=str(dar),
                    rotation=rotation, duration=duration,
                    has_audio=any(s["codec_type"] == "audio" for s in streams))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, StopIteration, ZeroDivisionError):
        raise OutputVideoError("Could not inspect video geometry; check FFprobe and the downloaded file") from None


def canvas(resolution, aspect_ratio):
    short = {"480p": 480, "720p": 720, "1080p": 1080}[resolution]
    ratio = Fraction(aspect_ratio.replace(":", "/"))
    # H.264 requires even dimensions. setdar below preserves the exact requested DAR.
    if ratio >= 1:
        return round(short * float(ratio) / 2) * 2, short
    return short, round(short / float(ratio) / 2) * 2


def ensure_geometry(path: Path, request: dict, stop):
    from .client import Stopped

    actual = probe(path)
    width, height = canvas(request["resolution"], request["aspect_ratio"])
    ratio = Fraction(request["aspect_ratio"].replace(":", "/"))
    matches = (actual["width"] == width and actual["height"] == height
               and actual["rotation"] == 0
               and abs(float(Fraction(actual["display_aspect_ratio"]) / ratio) - 1) < 0.001)
    original = path.with_name(path.stem + ".original.mp4")
    converted = False
    if not matches:
        if stop.is_set():
            raise Stopped
        temporary = path.with_name(path.stem + ".geometry.tmp.mp4")
        backup_temporary = path.with_name(path.stem + ".original.tmp.mp4")
        # Convert non-square input pixels to square pixels before fitting.
        filters = (
            "scale=trunc(iw*sar/2)*2:ih,setsar=1,"
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setdar={ratio.numerator}/{ratio.denominator}"
        )
        try:
            subprocess.run([
                tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-i", str(path),
                "-map", "0:V:0", "-map", "0:a:0?", "-vf", filters,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-c:a", "copy", "-map_metadata", "-1",
                "-movflags", "+faststart", str(temporary),
            ], capture_output=True, timeout=300, check=True)
            checked = probe(temporary)
            if (checked["width"] != width or checked["height"] != height
                    or checked["rotation"] != 0
                    or abs(float(Fraction(checked["display_aspect_ratio"]) / ratio) - 1) > 0.001
                    or abs(checked["duration"] - actual["duration"]) > 0.1
                    or checked["has_audio"] != actual["has_audio"]):
                raise OutputVideoError("Output geometry/audio verification failed; original preserved")
            if stop.is_set():
                raise Stopped
            if not original.exists():
                shutil.copyfile(path, backup_temporary)
                os.replace(backup_temporary, original)
            os.replace(temporary, path)
            converted = True
        except (OSError, subprocess.SubprocessError):
            raise OutputVideoError("Video geometry conversion failed; original preserved, check FFmpeg") from None
        finally:
            temporary.unlink(missing_ok=True)
            backup_temporary.unlink(missing_ok=True)
    final = probe(path)
    source = probe(original) if original.is_file() else actual
    normalized = converted or original.is_file()
    return {
        "width": final["width"], "height": final["height"],
        "actual_aspect_ratio": final["display_aspect_ratio"].replace("/", ":"),
        "duration": final["duration"],
        "source_width": source["width"], "source_height": source["height"],
        "source_aspect_ratio": source["display_aspect_ratio"].replace("/", ":"),
        "geometry_processing": "fit_pad" if normalized else "none",
        "warnings": (["Provider output did not match requested geometry; fitted with black padding. "
                      "Original preserved. Resizing does not add native detail."] if normalized else []),
    }
