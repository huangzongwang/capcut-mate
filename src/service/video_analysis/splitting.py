"""Worker-only boundary review and verified FFmpeg exports. No API dependencies."""

from fractions import Fraction
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import numpy as np

from enhanced import boundary_strength
from platform_runtime import media_tool


class SplitFailure(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def run_media(command, cwd=None):
    timeout = max(1, int(os.getenv("SCENE_MEDIA_COMMAND_TIMEOUT_SECONDS", "840")))
    started = time.monotonic()
    command_text = " ".join(map(str, command))
    print(
        f"Media command started (timeout={timeout}s): {command_text}",
        file=sys.stderr,
        flush=True,
    )
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise SplitFailure(503, "FFmpeg runtime is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        print(
            f"Media command timed out after {elapsed:.1f}s (limit={timeout}s): "
            f"{' '.join(map(str, command))}",
            file=sys.stderr,
            flush=True,
        )
        raise SplitFailure(504, "Media processing timed out") from exc
    elapsed = time.monotonic() - started
    print(
        f"Media command finished (returncode={result.returncode}, elapsed={elapsed:.3f}s): "
        f"{command_text}",
        file=sys.stderr,
        flush=True,
    )
    if result.returncode:
        print(f"FFmpeg failed ({result.returncode}): {command_text}\n{result.stderr}",
              file=sys.stderr, flush=True)
        raise SplitFailure(400, "FFmpeg could not process or verify the video")
    return result.stdout


def ffmpeg_threads():
    value = os.getenv("SCENE_FFMPEG_THREADS", "2")
    return value if value.isdigit() and int(value) >= 0 else "0"


def nvenc_available():
    try:
        result = subprocess.run([
            media_tool("ffmpeg"), "-nostdin", "-v", "error", "-f", "lavfi",
            "-i", "color=size=320x240:rate=1", "-frames:v", "1",
            "-c:v", "h264_nvenc", "-f", "null", "-",
        ], capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def select_encoder(requested, encoding):
    if encoding == "lossless" or requested == "cpu":
        return "libx264"
    if nvenc_available():
        return "h264_nvenc"
    if requested == "nvenc":
        raise SplitFailure(503, "NVIDIA NVENC is unavailable in the media container")
    return "libx264"


def probe(path, frames=False):
    options = ["-select_streams", "v:0", "-show_frames",
               "-show_entries", "frame=best_effort_timestamp,duration,pkt_duration"] if frames else ["-show_streams"]
    return json.loads(run_media([
        media_tool("ffprobe"), "-v", "error", "-threads", "1", "-protocol_whitelist", "file",
        *options, "-of", "json", str(path),
    ]))


def source_timeline(path, detection):
    streams = probe(path)["streams"]
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    if video is None:
        raise SplitFailure(400, "No video stream")
    if video["width"] % 2 or video["height"] % 2:
        raise SplitFailure(400, "Normalize odd video dimensions before splitting")
    if video.get("color_transfer") in ("smpte2084", "arib-std-b67"):
        raise SplitFailure(400, "Normalize HDR footage to SDR before splitting")
    rotations = [float(video.get("tags", {}).get("rotate", 0))]
    rotations.extend(float(s.get("rotation", 0)) for s in video.get("side_data_list", []))
    if any(rotation % 360 for rotation in rotations):
        raise SplitFailure(400, "Normalize video rotation before splitting")
    frames = probe(path, frames=True)["frames"]
    try:
        base = Fraction(video["time_base"])
        fps = Fraction(video["avg_frame_rate"])
        times = [int(frame["best_effort_timestamp"]) * base for frame in frames]
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise SplitFailure(400, "Video has no reliable frame timestamps") from exc
    if fps <= 0 or not times or len(times) != detection["frame_count"]:
        raise SplitFailure(400, "Decoded timestamps disagree with detected frames")
    if abs(float(fps) - detection["fps"]) > 0.001:
        raise SplitFailure(400, "Decoder frame rates disagree; normalize the video first")
    if any(
        (index and timestamp <= times[index - 1])
        or abs(timestamp - times[0] - Fraction(index, 1) / fps) > base
        for index, timestamp in enumerate(times)
    ):
        raise SplitFailure(400, "Variable or discontinuous frame timestamps; normalize to CFR first")
    duration = frames[-1].get("duration", frames[-1].get("pkt_duration"))
    if duration is not None and abs(int(duration) * base - 1 / fps) > base:
        raise SplitFailure(400, "Unreliable final frame duration; normalize the video first")
    times.append(times[-1] + 1 / fps)
    return times, any(s["codec_type"] == "audio" for s in streams)


def review_boundaries(detection, signals):
    """Local hard-cut heuristic, not a semantic classifier or a confidence probability."""
    if detection.get("detector") == "auto":
        # Auto already validates each candidate using its own region. Do not move
        # ROI-derived cuts toward unrelated full-frame subtitle peaks.
        return [{
            "detected_frame": scene["end_frame"], "selected_frame": scene["end_frame"],
            "status": "hard_cut_candidate", "reason": "auto_multiregion_verified",
            "shift_frames": 0,
        } for scene in detection["scenes"][:-1]]
    scores = signals["full"]
    features = signals["features"]
    radius = max(2, round(detection["fps"] * 0.2))
    span = max(2, round(detection["fps"] * 0.12))
    originals = [s["end_frame"] for s in detection["scenes"][:-1]]
    reviewed = []
    edges = [0, *originals, detection["frame_count"]]
    for index, original in enumerate(originals, 1):
        low = max(1, original - radius, (edges[index - 1] + original) // 2 + 1)
        high = min(len(scores) - 1, original + radius, (original + edges[index + 1]) // 2)
        window = scores[low:high + 1]
        strongest = low + int(np.argmax(window))
        frame = strongest if scores[strongest] > scores[original] * 1.15 else original
        separation, persistence = boundary_strength(features, frame, span)
        others = np.delete(window, frame - low)
        second = float(np.max(others)) if len(others) else 0
        peak = float(scores[frame])
        isolated = peak >= max(15, second * 1.5)
        stable = separation >= 8 and persistence >= 1.5
        enough_context = frame >= span and len(scores) - frame >= span
        accepted = isolated and stable and enough_context
        reviewed.append({
            "detected_frame": original, "selected_frame": frame,
            "status": "hard_cut_candidate" if accepted else "needs_review",
            "reason": "isolated_persistent_change" if accepted else "ambiguous_or_gradual_change",
            "shift_frames": frame - original,
            "peak_score": round(peak, 3),
            "neighbor_peak": round(second, 3),
            "separation": round(separation, 3),
            "persistence_ratio": round(persistence, 3),
        })
    return reviewed


def frame_hashes(path):
    output = run_media([
        media_tool("ffmpeg"), "-nostdin", "-v", "error", "-xerror",
        "-threads", ffmpeg_threads(), "-filter_threads", ffmpeg_threads(), "-noautorotate",
        "-protocol_whitelist", "file", "-i", str(path), "-map", "0:v:0", "-an",
        "-pix_fmt", "yuv420p", "-c:v", "rawvideo", "-threads", ffmpeg_threads(),
        "-fps_mode", "passthrough", "-f", "framemd5", "-",
    ])
    return [line.rsplit(",", 1)[1].strip()
            for line in output.splitlines() if line and not line.startswith("#")]


def encode_clip(source, destination, start, end, times, audio, byte_limit, encoding="lossless", encoder="libx264"):
    duration = float(times[end] - times[start])
    # Explicit CFR timing prevents a zero-duration final MP4 frame; input CFR was checked above.
    command = [
        media_tool("ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-n",
        "-threads", ffmpeg_threads(), "-filter_threads", ffmpeg_threads(), "-copyts", "-noautorotate",
        "-protocol_whitelist", "file", "-i", str(source),
        "-map", "0:v:0", "-vf",
        f"trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,format=yuv420p",
        "-c:v", encoder, "-threads", ffmpeg_threads(),
        "-fps_mode", "cfr", "-r", str(1 / (times[-1] - times[-2])),
        "-enc_time_base:v", str(times[-1] - times[-2]),
    ]
    if encoding == "compatible":
        if encoder == "h264_nvenc":
            command += ["-preset", "p5", "-tune", "hq", "-profile:v", "high",
                        "-rc", "vbr", "-cq", "16", "-b:v", "0", "-maxrate", "20M", "-bufsize", "40M"]
        else:
            command += ["-preset", "veryfast", "-profile:v", "high", "-crf", "16", "-maxrate", "20M", "-bufsize", "40M"]
    else:
        command += ["-crf", "0"]
    if audio:
        command += [
            "-map", "0:a:0", "-af",
            f"atrim=start={float(times[start]):.9f}:end={float(times[end]):.9f},"
            f"asetpts=PTS-({float(times[start]):.9f})/TB,"
            f"aresample=async=1:first_pts=0,apad=whole_dur={duration:.9f},atrim=duration={duration:.9f}",
            "-c:a", "aac", "-b:a", "192k",
        ]
    else:
        command += ["-an"]
    command += [
        "-map_metadata", "-1", "-map_chapters", "-1", "-t", f"{duration:.9f}",
        "-fs", str(byte_limit), "-movflags", "+faststart", str(destination),
    ]
    run_media(command)


def encoder_options(encoding, encoder):
    if encoding != "compatible":
        return ["-crf", "0"]
    if encoder == "h264_nvenc":
        return ["-preset", "p5", "-tune", "hq", "-profile:v", "high",
                "-rc", "vbr", "-cq", "16", "-b:v", "0", "-maxrate", "20M", "-bufsize", "40M"]
    return ["-preset", "veryfast", "-profile:v", "high", "-crf", "16",
            "-maxrate", "20M", "-bufsize", "40M"]


def encode_batch(source, jobs, times, audio, byte_limit, encoding, encoder):
    """Decode the source once and fan out a small group of frame-exact outputs."""
    count = len(jobs)
    filters = [f"[0:v:0]split={count}" + "".join(f"[vi{i}]" for i in range(count))]
    if audio:
        filters.append(f"[0:a:0]asplit={count}" + "".join(f"[ai{i}]" for i in range(count)))
    for i, (start, end, _) in enumerate(jobs):
        filters.append(
            f"[vi{i}]trim=start_frame={start}:end_frame={end},"
            f"setpts=PTS-STARTPTS,format=yuv420p[v{i}]"
        )
        if audio:
            duration = float(times[end] - times[start])
            filters.append(
                f"[ai{i}]atrim=start={float(times[start]):.9f}:end={float(times[end]):.9f},"
                f"asetpts=PTS-({float(times[start]):.9f})/TB,aresample=async=1:first_pts=0,"
                f"apad=whole_dur={duration:.9f},atrim=duration={duration:.9f}[a{i}]"
            )
    command = [
        media_tool("ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-n",
        "-threads", ffmpeg_threads(), "-filter_threads", ffmpeg_threads(),
        "-filter_complex_threads", ffmpeg_threads(), "-copyts", "-noautorotate",
        "-protocol_whitelist", "file", "-i", str(source),
        "-filter_complex", ";".join(filters),
    ]
    frame_rate = str(1 / (times[-1] - times[-2]))
    time_base = str(times[-1] - times[-2])
    for i, (start, end, destination) in enumerate(jobs):
        duration = float(times[end] - times[start])
        command += ["-map", f"[v{i}]", "-c:v", encoder, "-threads", ffmpeg_threads(),
                    "-fps_mode", "cfr", "-r", frame_rate, "-enc_time_base:v", time_base,
                    *encoder_options(encoding, encoder)]
        if audio:
            command += ["-map", f"[a{i}]", "-c:a", "aac", "-b:a", "192k"]
        else:
            command += ["-an"]
        command += ["-map_metadata", "-1", "-map_chapters", "-1", "-t", f"{duration:.9f}",
                    "-fs", str(byte_limit), "-movflags", "+faststart", str(destination)]
    run_media(command)


def compatible_quality(source, path, start, end, stats_path):
    stats_path = Path(stats_path).resolve()
    # Keep drive colons/backslashes out of FFmpeg's filter-expression parser.
    stats_name = "quality-single.log"
    run_media([
        media_tool("ffmpeg"), "-nostdin", "-v", "error", "-xerror",
        "-threads", ffmpeg_threads(), "-noautorotate", "-protocol_whitelist", "file", "-i", str(Path(source).resolve()),
        "-threads", ffmpeg_threads(), "-noautorotate", "-protocol_whitelist", "file", "-i", str(Path(path).resolve()),
        "-filter_complex_threads", ffmpeg_threads(), "-filter_complex",
        f"[0:v:0]trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,format=yuv420p[ref];"
        f"[1:v:0]setpts=PTS-STARTPTS,format=yuv420p[out];"
        f"[out][ref]psnr=stats_file={stats_name}:shortest=1:repeatlast=0[checked]",
        "-map", "[checked]", "-an", "-f", "null", "-",
    ], cwd=stats_path.parent)
    (stats_path.parent / stats_name).replace(stats_path)
    scores = []
    for line in Path(stats_path).read_text().splitlines():
        fields = dict(item.split(":", 1) for item in line.split())
        scores.append(float(fields["psnr_avg"]))
    if len(scores) != end - start or any(math.isnan(score) or score < 35 for score in scores):
        raise SplitFailure(400, "Export verification failed: frame alignment or image quality below the 35 dB floor")
    minimum = min(scores)
    return round(minimum, 2) if math.isfinite(minimum) else None


def compatible_quality_batch(source, jobs, stats_paths):
    count = len(jobs)
    stats_paths = [Path(path).resolve() for path in stats_paths]
    stats_dir = stats_paths[0].parent
    if any(path.parent != stats_dir for path in stats_paths):
        raise SplitFailure(400, "Quality logs must share one working directory")
    command = [media_tool("ffmpeg"), "-nostdin", "-v", "error", "-xerror",
               "-threads", ffmpeg_threads(), "-noautorotate", "-protocol_whitelist", "file",
               "-i", str(Path(source).resolve())]
    for _, _, path in jobs:
        command += ["-threads", ffmpeg_threads(), "-noautorotate", "-protocol_whitelist", "file",
                    "-i", str(Path(path).resolve())]
    filters = [f"[0:v:0]split={count}" + "".join(f"[ri{i}]" for i in range(count))]
    for i, ((start, end, _), stats_path) in enumerate(zip(jobs, stats_paths)):
        filters += [
            f"[ri{i}]trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,format=yuv420p[r{i}]",
            f"[{i + 1}:v:0]setpts=PTS-STARTPTS,format=yuv420p[o{i}]",
            f"[o{i}][r{i}]psnr=stats_file=quality-batch-{i}.log:shortest=1:repeatlast=0[c{i}]",
        ]
    command += ["-filter_complex_threads", ffmpeg_threads(), "-filter_complex", ";".join(filters)]
    for i in range(count):
        command += ["-map", f"[c{i}]"]
    command += ["-an", "-f", "null", "-"]
    run_media(command, cwd=stats_dir)
    for i, stats_path in enumerate(stats_paths):
        (stats_dir / f"quality-batch-{i}.log").replace(stats_path)
    results = []
    for (start, end, _), stats_path in zip(jobs, stats_paths):
        scores = []
        for line in Path(stats_path).read_text().splitlines():
            fields = dict(item.split(":", 1) for item in line.split())
            scores.append(float(fields["psnr_avg"]))
        if len(scores) != end - start or any(math.isnan(score) or score < 35 for score in scores):
            raise SplitFailure(400, "Export verification failed: frame alignment or image quality below the 35 dB floor")
        minimum = min(scores)
        results.append(round(minimum, 2) if math.isfinite(minimum) else None)
    return results


def verify_clip(path, expected_hashes, duration, audio, encoding="lossless",
                source=None, start=0, stats_path=None, check_quality=True):
    actual = frame_hashes(path)
    if len(actual) != len(expected_hashes) or (encoding == "lossless" and actual != expected_hashes):
        raise SplitFailure(400, "Export verification failed: decoded frames differ from the selected source range")
    streams = probe(path)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    quality = None
    if encoding == "compatible":
        if video["profile"] not in ("High", "Main", "Baseline", "Constrained Baseline") or video["pix_fmt"] not in ("yuv420p", "yuvj420p"):
            raise SplitFailure(400, "Export verification failed: incompatible H.264 profile or pixel format")
        if check_quality:
            quality = compatible_quality(source, path, start, start + len(expected_hashes), stats_path)
    rate = float(Fraction(video["avg_frame_rate"]))
    tick = float(Fraction(video["time_base"]))
    if rate <= 0 or abs(float(video.get("start_time", 0))) > tick or abs(float(video["duration"]) - duration) > tick + 1e-5:
        raise SplitFailure(400, "Export verification failed: video timing mismatch")
    tracks = [s for s in streams if s["codec_type"] == "audio"]
    if bool(tracks) != audio:
        raise SplitFailure(400, "Export verification failed: missing or unexpected audio")
    if audio:
        tolerance = max(0.05, 2048 / int(tracks[0]["sample_rate"]))
        if abs(float(tracks[0].get("start_time", 0))) > tolerance or abs(float(tracks[0]["duration"]) - duration) > tolerance:
            raise SplitFailure(400, "Export verification failed: audio timing mismatch")
    return quality


def split_video(path, data, detection, signals):
    encoding = data.get("encoding", "compatible")
    requested_encoder = data.get("encoder", "auto")
    actual_encoder = select_encoder(requested_encoder, encoding)
    times, has_audio = source_timeline(path, detection)
    reviews = review_boundaries(detection, signals)
    manual = data.get("confirmed_ranges")
    needs_review = not manual and (
        any(r["status"] == "needs_review" for r in reviews)
        or (detection.get("detector") == "auto"
            and detection.get("settings", {}).get("review_required", False))
    )
    result = {
        "status": "needs_review", "job_id": None, "detection": detection, "clips": [],
        "boundary_reviews": reviews, "review_required": bool(needs_review),
        "requested_encoder": requested_encoder, "actual_encoder": actual_encoder,
        "warnings": [
            "Visual boundary review is heuristic; missed shots and mixed transition frames may remain.",
            "Frame verification proves extraction accuracy, not semantic shot purity.",
            "Only the first audio track is kept as AAC. Video resolution and frame rate are preserved.",
            ("Compatible H.264 High exports are lossy; frame count and per-frame quality are checked, not pixel equality."
             if encoding == "compatible" else
             "Lossless H.264 uses High 4:4:4 Predictive; some players cannot decode it correctly and files may be large."),
        ],
    }
    if needs_review and data["boundary_policy"] == "strict":
        return result
    if manual:
        ranges = [(r["start_frame"], r["end_frame"]) for r in manual]
        result["warnings"].append("Frame ranges were confirmed by the caller, not automatically validated as pure shots.")
        result["boundary_reviews"] = [
            {"start_frame": start, "end_frame": end, "status": "user_confirmed"} for start, end in ranges
        ]
    else:
        edges = [0, *[r["selected_frame"] for r in reviews], detection["frame_count"]]
        ranges = list(zip(edges, edges[1:]))
    if len(ranges) > 100:
        raise SplitFailure(413, "At most 100 clips can be exported per request")
    if any(start < 0 or end > detection["frame_count"] or start >= end for start, end in ranges):
        raise SplitFailure(400, "Confirmed frame range is outside the decoded video")
    output = Path(data["output_directory"])
    remaining = data["max_output_bytes"]
    if remaining <= 0 or shutil.disk_usage(output).free < remaining + 100 * 1024 ** 2:
        raise SplitFailure(413, "Insufficient export space or invalid output size budget")
    hashes = frame_hashes(path) if encoding == "lossless" else None
    if hashes is not None and len(hashes) != detection["frame_count"]:
        raise SplitFailure(400, "FFmpeg and detector decoded frame counts disagree")
    batch_size = max(1, int(os.getenv("SCENE_EXPORT_BATCH_SIZE", "4")))
    if actual_encoder == "libx264":
        batch_size = min(batch_size, 2)
    for batch_start in range(0, len(ranges), batch_size):
        batch_ranges = ranges[batch_start:batch_start + batch_size]
        jobs = [(start, end, output / f"scene-{batch_start + offset + 1:03d}.mp4")
                for offset, (start, end) in enumerate(batch_ranges)]
        if remaining <= 0:
            raise SplitFailure(413, "Exports exceed the configured total size limit")
        encode_batch(path, jobs, times, has_audio, remaining, encoding, actual_encoder)
        quality_paths = [Path(data.get("work_directory", output)) / f"quality-{batch_start + i + 1:03d}.log"
                         for i in range(len(jobs))]
        if encoding == "compatible":
            qualities = compatible_quality_batch(path, jobs, quality_paths)
        else:
            qualities = [None] * len(jobs)
        for index, ((start, end, destination), quality) in enumerate(zip(jobs, qualities), batch_start + 1):
            size = destination.stat().st_size
            if size >= remaining:
                raise SplitFailure(413, "Exports exceed the configured total size limit")
            expected = hashes[start:end] if hashes is not None else [None] * (end - start)
            verify_clip(destination, expected, float(times[end] - times[start]), has_audio,
                        encoding=encoding, source=path, start=start, check_quality=False)
            remaining -= size
            filename = destination.name
            result["clips"].append({
                "index": index, "start_frame": start, "end_frame": end,
                "start_us": round((times[start] - times[0]) * 1_000_000),
                "end_us": round((times[end] - times[0]) * 1_000_000),
                "frame_count": end - start, "filename": filename, "size_bytes": size,
                "audio_included": has_audio, "encoding": encoding, "actual_encoder": actual_encoder,
                "verification": ("all_decoded_frames_match" if encoding == "lossless"
                                 else "frame_count_and_quality_checked"),
                "min_frame_psnr_db": quality,
                "download_path": f"/openapi/capcut-mate/v1/scene_clips/{data['job_id']}/{filename}",
            })
    if detection.get("detector") == "auto":
        from scenedetect import FrameTimecode
        fps = detection["fps"]
        # The exported ranges become the single source for downstream timestamps,
        # including manually confirmed ranges.
        result["detection"] = {
            **detection, "scene_count": len(ranges),
            "scenes": [{
                "index": index, "start_frame": start, "end_frame": end,
                "start_us": round((times[start] - times[0]) * 1_000_000),
                "end_us": round((times[end] - times[0]) * 1_000_000),
                "duration_us": round((times[end] - times[start]) * 1_000_000),
                "start_timecode": FrameTimecode(start, fps).get_timecode(),
                "end_timecode": FrameTimecode(end, fps).get_timecode(),
            } for index, (start, end) in enumerate(ranges, 1)],
        }
        if needs_review:
            result["warnings"].append("Auto found ambiguous internal changes; inspect detection.diagnostics before using clips.")
    result.update(status="completed", job_id=data["job_id"])
    return result
