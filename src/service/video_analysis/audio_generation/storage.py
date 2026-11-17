"""Bounded download and isolated storage for generated audio."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
import wave

import urllib3

from ..platform_runtime import analysis_temp_root, ensure_writable_directory, media_tool, nas_root


LIMIT = 100 * 1024 * 1024


def root() -> Path:
    nas = nas_root()
    default = nas / "audio-generation" / "results" if nas else Path("output/video-analysis/audio")
    return Path(os.getenv("VIDEO_ANALYSIS_AUDIO_ROOT", str(default))).expanduser().resolve()


def _download(url: str, target: Path) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("上游音频地址无效")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not _public_ip(item[4][0]) for item in addresses):
            raise ValueError
        pool = urllib3.HTTPSConnectionPool(
            host=addresses[0][4][0], port=parsed.port or 443,
            assert_hostname=parsed.hostname, server_hostname=parsed.hostname,
            timeout=urllib3.Timeout(connect=10, read=30), retries=False,
        )
        with pool:
            response = pool.urlopen(
                "GET", urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
                headers={"Host": parsed.netloc, "Accept-Encoding": "identity"},
                redirect=False, preload_content=False,
            )
            try:
                if response.status != 200:
                    raise ValueError
                length = response.headers.get("Content-Length")
                if length and (int(length) < 1 or int(length) > LIMIT):
                    raise ValueError
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError
                deadline = time.monotonic() + 180
                size = 0
                with target.open("wb") as output:
                    while chunk := response.read(1024 * 1024, decode_content=False):
                        size += len(chunk)
                        if size > LIMIT or time.monotonic() > deadline:
                            raise ValueError
                        output.write(chunk)
                if size == 0 or (length and size != int(length)):
                    raise ValueError
            finally:
                response.close()
    except (OSError, ValueError, urllib3.exceptions.HTTPError):
        target.unlink(missing_ok=True)
        raise ValueError("下载生成音频失败；请确认上游返回的是可直接下载的 HTTPS 地址") from None


def _public_ip(value: str) -> bool:
    from ipaddress import ip_address

    return ip_address(value).is_global


def download_and_convert(url: str, task_id: str) -> dict:
    output_root = root()
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / f"{task_id}.wav"
    temp_root = ensure_writable_directory(analysis_temp_root())
    try:
        with tempfile.TemporaryDirectory(prefix=f"{task_id}-", dir=temp_root) as directory:
            work = Path(directory)
            source = work / "source"
            temporary = work / "converted.wav"
            _download(url, source)
            subprocess.run(
                [
                    media_tool("ffmpeg"), "-nostdin", "-y", "-v", "error", "-xerror",
                    "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(temporary),
                ],
                capture_output=True,
                timeout=300,
                check=True,
            )
            with wave.open(str(temporary), "rb") as audio:
                metadata = {
                    "format": "wav",
                    "sample_rate": audio.getframerate(),
                    "channels": audio.getnchannels(),
                    "sample_width_bits": audio.getsampwidth() * 8,
                }
            if metadata != {
                "format": "wav", "sample_rate": 16000, "channels": 1, "sample_width_bits": 16,
            } or temporary.stat().st_size <= 44:
                raise ValueError
            shutil.copyfile(temporary, output)
            return {"path": output, **metadata, "size_bytes": output.stat().st_size}
    except (OSError, ValueError, subprocess.SubprocessError):
        output.unlink(missing_ok=True)
        raise ValueError("音频格式转换或校验失败，请检查 FFmpeg 安装和上游音频") from None


def audio_path(task_id: str) -> Path:
    if not task_id.startswith("vaa_") or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in task_id):
        raise ValueError("无效的音频任务 ID")
    path = (root() / f"{task_id}.wav").resolve()
    if path.parent != root():
        raise ValueError("无效的音频文件路径")
    if not path.is_file():
        raise FileNotFoundError
    return path


def cleanup_old_files(max_age_seconds: int = 7 * 24 * 60 * 60) -> None:
    cutoff = time.time() - max_age_seconds
    directory = root()
    if not directory.is_dir():
        return
    for path in directory.glob("vaa_*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue
