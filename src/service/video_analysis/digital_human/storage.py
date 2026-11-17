from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import subprocess

from ..platform_runtime import (
    digital_human_root, ensure_writable_directory, media_tool, worker_creation_kwargs,
)


def root() -> Path:
    return digital_human_root()


def prepare_directories() -> None:
    for directory in (root() / "uploads", root() / "results"):
        ensure_writable_directory(directory)


def resolve_upload(relative: str, suffixes: set[str]) -> Path:
    posix, windows = PurePosixPath(relative), PureWindowsPath(relative)
    if posix.is_absolute() or windows.is_absolute() or windows.drive or ".." in posix.parts:
        raise ValueError("输入路径必须是上传接口返回的相对路径")
    base = root().resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base) or path.parent != (base / "uploads").resolve():
        raise ValueError("输入文件必须位于数字人上传目录")
    if path.suffix.lower() not in suffixes:
        raise ValueError("不支持的输入文件格式")
    if not path.is_file() or path.stat().st_size == 0:
        raise FileNotFoundError("NAS 上传文件不存在或为空")
    return path


def shared_root(configured: str) -> Path:
    if not isinstance(configured, str) or not configured.strip():
        raise RuntimeError("必须在数字人任务中传入 duix_shared_dir")
    raw = configured.strip()
    posix, windows = PurePosixPath(raw), PureWindowsPath(raw)
    if ".." in posix.parts or ".." in windows.parts:
        raise ValueError("duix_shared_dir 不允许包含路径回退")
    if not windows.is_absolute() and not posix.is_absolute():
        raise ValueError("duix_shared_dir 必须是 UNC 路径或绝对挂载路径")
    try:
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise RuntimeError(f"Duix 共享目录必须预先创建: {raw}")
        return path
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError(f"Duix 共享目录不可访问或不可写: {raw}") from exc


def prepare(video: Path, audio: Path, task_id: str, shared_dir: str) -> tuple[Path, Path]:
    shared = shared_root(shared_dir)
    video_out = shared / f"duix_{task_id}.mp4"
    audio_out = shared / f"duix_{task_id}.wav"
    common = [media_tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    try:
        subprocess.run(
            common + ["-i", str(video), "-map", "0:v:0", "-an", "-c:v", "libx264",
                      "-crf", "18", "-pix_fmt", "yuv420p", str(video_out)],
            check=True, capture_output=True, timeout=900, **worker_creation_kwargs(),
        )
        subprocess.run(
            common + ["-i", str(audio), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                      "-c:a", "pcm_s16le", str(audio_out)],
            check=True, capture_output=True, timeout=900, **worker_creation_kwargs(),
        )
        if min(video_out.stat().st_size, audio_out.stat().st_size) == 0:
            raise RuntimeError("FFmpeg returned empty media")
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        video_out.unlink(missing_ok=True)
        audio_out.unlink(missing_ok=True)
        detail = getattr(exc, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise RuntimeError("数字人输入预处理失败: " + str(detail or exc)[-1000:]) from exc
    return video_out, audio_out


def publish(source: Path, task_id: str) -> Path:
    directory = root() / "results"
    ensure_writable_directory(directory)
    destination = directory / f"{task_id}.mp4"
    try:
        with source.open("rb") as reader, destination.open("wb") as writer:
            shutil.copyfileobj(reader, writer, length=4 * 1024 * 1024)
        if destination.stat().st_size == 0:
            raise OSError("empty result")
        return destination
    except OSError:
        destination.unlink(missing_ok=True)
        raise
