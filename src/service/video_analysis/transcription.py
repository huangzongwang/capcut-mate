"""Local faster-whisper transcription and scene-aligned transcript helpers."""

from math import isfinite
import os
import json
import subprocess
import time
from pathlib import Path

# This module is also imported by the isolated worker as a top-level module.
try:
    from .platform_runtime import analysis_temporary_directory, whisper_root, media_tool, worker_creation_kwargs
except ImportError:
    from platform_runtime import analysis_temporary_directory, whisper_root, media_tool, worker_creation_kwargs


def _srt_time(seconds):
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def to_srt(segments):
    lines = []
    for index, segment in enumerate(segments, 1):
        lines.extend([
            str(index),
            f"{_srt_time(segment['start'])} --> {_srt_time(segment['end'])}",
            segment["text"].strip(),
            "",
        ])
    return "\n".join(lines)


def map_segments_to_scenes(segments, scenes):
    mapped = []
    for scene in scenes:
        scene_start = scene["start_us"] / 1_000_000
        scene_end = scene["end_us"] / 1_000_000
        selected = []
        for segment in segments:
            overlap = max(0.0, min(scene_end, segment["end"]) - max(scene_start, segment["start"]))
            if overlap > 0:
                selected.append((segment, overlap))
        text = " ".join(item[0]["text"].strip() for item in selected if item[0]["text"].strip()).strip()
        speech_seconds = sum(overlap for _, overlap in selected)
        duration = max(0.001, scene_end - scene_start)
        mapped.append({
            "scene_index": scene["index"],
            "start_us": scene["start_us"],
            "end_us": scene["end_us"],
            "text": text,
            "segment_ids": [item[0]["id"] for item in selected],
            "speech_seconds": round(speech_seconds, 3),
            "speech_density": round(min(1.0, speech_seconds / duration), 3),
        })
    return mapped


class TranscriptionRuntimeError(RuntimeError):
    status = 503


def transcribe_clips(paths, model_name="large-v3", language="zh",
                     word_timestamps=True, vad_filter=True):
    try:
        return _transcribe_clips(paths, model_name, language, word_timestamps, vad_filter)
    except (ImportError, OSError, RuntimeError) as exc:
        raise TranscriptionRuntimeError(
            f"Whisper transcription runtime failed: {exc}. "
            "Check the analysis environment, model files and CUDA/cuDNN libraries."
        ) from exc


def _media_command(args):
    result = subprocess.run(
        args, capture_output=True, timeout=120, **worker_creation_kwargs(),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace")[-2000:])
    return result.stdout


def _decode_clip(path, result):
    mark = time.monotonic()
    try:
        probe = json.loads(_media_command([
            media_tool("ffprobe"), "-v", "error", "-show_entries",
            "stream=codec_type", "-of", "json", str(path),
        ]))
    finally:
        result["timings"]["probe_seconds"] = round(time.monotonic() - mark, 3)
    if not any(s.get("codec_type") == "audio" for s in probe["streams"]):
        result["status"] = "no_audio"
        return None
    from faster_whisper.audio import decode_audio

    mark = time.monotonic()
    try:
        audio = decode_audio(str(path), sampling_rate=16000)
        if not audio.size:
            raise ValueError("Audio stream decoded to zero samples")
        return audio
    except Exception as exc:
        result["decode_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["timings"]["decode_seconds"] = round(time.monotonic() - mark, 3)
    mark = time.monotonic()
    result["retry_count"] = 1
    try:
        with analysis_temporary_directory("capcut-audio-") as folder:
            wav = str(Path(folder) / "audio.wav")
            _media_command([
                media_tool("ffmpeg"), "-nostdin", "-v", "error", "-y",
                "-i", str(path), "-map", "0:a:0", "-vn", "-ac", "1",
                "-ar", "16000", "-c:a", "pcm_s16le", wav,
            ])
            audio = decode_audio(wav, sampling_rate=16000)
            if not audio.size:
                raise ValueError("FFmpeg fallback decoded to zero samples")
            return audio
    finally:
        result["timings"]["retry_seconds"] = round(time.monotonic() - mark, 3)


def _transcribe_clips(paths, model_name, language, word_timestamps, vad_filter):
    results = []
    model = None
    model_error = None
    device, compute_type = "cpu", "int8"
    for path in paths:
        mark = time.monotonic()
        result = {
            "model_name": model_name, "language": language or "zh",
            "language_probability": None, "duration_seconds": 0.0,
            "segments": [], "status": "completed", "error": "",
            "decode_error": "", "retry_count": 0, "timings": {},
            "device": "not_used", "compute_type": "not_used",
        }
        try:
            audio = _decode_clip(path, result)
            if audio is not None:
                if model is None:
                    if model_error:
                        raise RuntimeError(model_error)
                    try:
                        model, device, compute_type = _load_model(model_name)
                    except Exception as exc:
                        model_error = f"{type(exc).__name__}: {exc}"
                        raise
                result.update(device=device, compute_type=compute_type)
                infer_started = time.monotonic()
                try:
                    result.update(_recognize(model, audio, language, word_timestamps, vad_filter))
                finally:
                    result["timings"]["inference_seconds"] = round(time.monotonic() - infer_started, 3)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if result["decode_error"]:
                error += f" (PyAV: {result['decode_error']})"
            result.update(status="failed", error=error, segments=[])
        result["timings"]["total_seconds"] = round(time.monotonic() - mark, 3)
        results.append(result)
    return results


def _load_model(model_name):
    import ctranslate2
    from faster_whisper import WhisperModel

    device = os.getenv("SCENE_WHISPER_DEVICE", "auto").lower()
    if device not in ("auto", "cuda", "cpu"):
        raise ValueError("SCENE_WHISPER_DEVICE must be auto, cuda or cpu")
    if device == "auto":
        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    model = WhisperModel(
        os.getenv("SCENE_WHISPER_MODEL_PATH") or model_name,
        device=device,
        compute_type=compute_type,
        download_root=whisper_root(),
    )
    return model, device, compute_type


def _recognize(model, audio, language, word_timestamps, vad_filter):
    # Consume the lazy iterator here so inference failures stay isolated per clip.
    iterator, info = model.transcribe(
        audio, language=language, beam_size=5, vad_filter=vad_filter,
        word_timestamps=word_timestamps, condition_on_previous_text=True,
    )
    segments = []
    for index, segment in enumerate(iterator):
        words = []
        for word in (segment.words or []):
            if word.start is None or word.end is None:
                continue
            words.append({
                "start": round(float(word.start), 3), "end": round(float(word.end), 3),
                "word": word.word, "probability": round(float(word.probability), 4)
                if word.probability is not None and isfinite(float(word.probability)) else None,
            })
        segments.append({
            "id": index, "start": round(float(segment.start), 3),
            "end": round(float(segment.end), 3), "text": segment.text.strip(),
            "avg_logprob": round(float(segment.avg_logprob), 4)
            if segment.avg_logprob is not None and isfinite(float(segment.avg_logprob)) else None,
            "no_speech_prob": round(float(segment.no_speech_prob), 4)
            if segment.no_speech_prob is not None and isfinite(float(segment.no_speech_prob)) else None,
            "words": words,
        })
    return {
        "language": info.language,
        "language_probability": round(float(info.language_probability), 4)
        if info.language_probability is not None else None,
        "duration_seconds": round(float(info.duration), 3),
        "segments": segments,
    }


def transcribe(path, model_name="large-v3", language="zh", word_timestamps=True, vad_filter=True):
    return transcribe_clips(
        [path], model_name=model_name, language=language,
        word_timestamps=word_timestamps, vad_filter=vad_filter,
    )[0]
