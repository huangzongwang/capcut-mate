"""Platform helpers owned by the video-analysis module."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import subprocess
import tempfile
import time
import uuid


MODULE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_NAS_ROOT = r"\\192.168.0.30\技术部\hzw"
ENV_DB_FILE = MODULE_ROOT / "env.db.dev"
_NODE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@lru_cache(maxsize=1)
def _file_settings() -> dict[str, str]:
    if not ENV_DB_FILE.is_file():
        return {}
    settings = {}
    for raw_line in ENV_DB_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value[:1] in ("'", '"'):
            value = value[1:-1]
        settings[key] = value
    return settings


def video_analysis_setting(name: str, default: str = "") -> str:
    configured = os.getenv(name)
    return configured if configured is not None else _file_settings().get(name, default)


def video_analysis_node_id() -> str | None:
    value = (os.getenv("VIDEO_ANALYSIS_NODE_ID") or _file_settings().get(
        "VIDEO_ANALYSIS_NODE_ID", "",
    )).strip()
    if not value:
        return None
    if not _NODE_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "VIDEO_ANALYSIS_NODE_ID must be 1-64 ASCII letters, numbers, dot, underscore or dash"
        )
    return value.lower()


def nas_root() -> Path:
    configured = (
        os.getenv("VIDEO_ANALYSIS_STORAGE_ROOT")
        or _file_settings().get("VIDEO_ANALYSIS_STORAGE_ROOT", "")
    )
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        return Path(DEFAULT_NAS_ROOT)
    raise RuntimeError(
        "VIDEO_ANALYSIS_STORAGE_ROOT must point to the mounted video_analysis NAS directory"
    )


def ensure_writable_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    # NAS policies in production allow final assets but may reject probe/temp files.
    if _is_nas_path(path):
        return path
    probe = path / (".video-analysis-write-test-" + uuid.uuid4().hex)
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise OSError(f"video_analysis NAS directory is not writable: {path}") from exc
    return path


def _is_nas_path(path: Path) -> bool:
    raw = str(path)
    if raw.startswith(("\\\\", "//")):
        return True
    try:
        path.resolve().relative_to(nas_root().resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def analysis_data_root() -> Path:
    root = nas_root()
    return root / "video-analysis"


def video_analysis_report_root() -> Path:
    root = nas_root()
    return root / "video-analysis" / "reports"


def video_analysis_database_root() -> Path:
    configured = (
        os.getenv("VIDEO_ANALYSIS_NODE_DB_ROOT")
        or _file_settings().get("VIDEO_ANALYSIS_NODE_DB_ROOT", "")
    ).strip()
    root = Path(configured).expanduser() if configured else MODULE_ROOT / ".runtime" / "video-analysis" / "database"
    if not root.is_absolute():
        root = MODULE_ROOT / root
    if str(root).startswith(("\\\\", "//")):
        raise ValueError("VIDEO_ANALYSIS_NODE_DB_ROOT must be on a local disk, not NAS")
    node_id = video_analysis_node_id() or "default"
    return ensure_writable_directory(root.resolve() / node_id)


def legacy_video_analysis_database_root() -> Path:
    return nas_root() / "video-analysis" / "database"


def legacy_node_database_root() -> Path:
    node_id = video_analysis_node_id()
    root = legacy_video_analysis_database_root()
    return root / "nodes" / node_id if node_id else root


def video_analysis_database_file(filename: str, *, migrate: bool = True) -> Path:
    if not filename or Path(filename).name != filename:
        raise ValueError("Invalid video-analysis database filename")
    target = video_analysis_database_root() / filename
    if target.exists() or not migrate:
        return target
    sources = [legacy_node_database_root() / filename]
    # A shared legacy queue may be copied only by a pre-node deployment. Copying
    # it into every named node would make the same historical task run twice.
    if video_analysis_node_id() is None:
        sources.append(legacy_video_analysis_database_root() / filename)
    for source in sources:
        if not source.is_file() or source.resolve() == target.resolve():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source_conn = sqlite3.connect(str(source), timeout=30)
        target_conn = sqlite3.connect(str(target), timeout=30)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()
        break
    return target


def legacy_video_analysis_task_db() -> Path:
    return legacy_video_analysis_database_root() / "analysis-tasks.sqlite3"


def video_analysis_task_db() -> Path:
    return video_analysis_database_file("analysis-tasks.sqlite3")


def digital_human_root() -> Path:
    root = nas_root()
    return root / "digital-human"


def analysis_temp_root() -> Path:
    configured = (
        os.getenv("VIDEO_ANALYSIS_TEMP_ROOT")
        or _file_settings().get("VIDEO_ANALYSIS_TEMP_ROOT", "")
    )
    if configured:
        if configured.startswith(("\\\\", "//")):
            raise ValueError("VIDEO_ANALYSIS_TEMP_ROOT must be on a local disk, not a network share")
        path = Path(configured).expanduser()
        try:
            path.resolve().relative_to(nas_root().resolve())
        except ValueError:
            return path
        raise ValueError("VIDEO_ANALYSIS_TEMP_ROOT must not be inside the NAS storage root")
    return Path(tempfile.gettempdir()) / "capcut-mate" / "video-analysis"


@contextmanager
def analysis_temporary_directory(prefix: str):
    root = ensure_writable_directory(analysis_temp_root())
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=str(root)))
    try:
        yield str(directory)
    finally:
        for attempt in range(3):
            try:
                shutil.rmtree(directory)
                break
            except FileNotFoundError:
                break
            except OSError as exc:
                if attempt == 2:
                    logging.getLogger(__name__).warning(
                        "Could not remove local analysis temp directory %s: %s",
                        directory, exc,
                    )
                else:
                    time.sleep(0.25 * (attempt + 1))


def analysis_python() -> str:
    default = sys.executable if os.name == "nt" else "/opt/scenedetect/bin/python"
    return os.getenv("SCENE_PYTHON", default)


def media_root() -> Path:
    configured = os.getenv("VIDEO_ANALYSIS_MEDIA_ROOT") or os.getenv("SCENE_MEDIA_ROOT")
    if configured:
        return Path(configured).expanduser()
    root = nas_root()
    return root / "video-analysis"


def whisper_root() -> str:
    # Docker's Hugging Face cache may contain Linux-only symlink reparse points.
    default = MODULE_ROOT / "models" / "whisper-windows" if os.name == "nt" else Path("/models/whisper")
    return str(Path(os.getenv("SCENE_WHISPER_ROOT", str(default))).expanduser().resolve())


def scene_draft_root() -> Path:
    configured = os.getenv("VIDEO_ANALYSIS_CLIP_ROOT") or os.getenv("SCENE_DRAFT_CLIP_ROOT")
    if configured:
        return Path(configured).expanduser()
    root = nas_root()
    return root / "video-analysis" / "analysis-clips"


def scene_split_root() -> Path:
    configured = os.getenv("VIDEO_ANALYSIS_SCENE_CLIP_ROOT") or os.getenv("SCENE_SPLIT_ROOT")
    if configured:
        return Path(configured).expanduser()
    root = nas_root()
    return root / "video-analysis" / "scene-clips"


def worker_creation_kwargs() -> dict:
    if os.name != "nt":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def media_tool(name: str) -> str:
    return os.getenv("SCENE_" + name.upper(), name if os.name == "nt" else "/usr/bin/" + name)


def worker_environment() -> dict:
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


class WindowsWorkerJob:
    """Closing the parent-owned handle also kills FFmpeg descendants.

    Attach before supplying worker stdin, so it cannot start media processing
    outside the job. OS handle cleanup also covers an unexpected API exit.
    """

    def __init__(self):
        import win32job

        self.api = win32job
        self.handle = win32job.CreateJobObject(None, "")
        try:
            info = win32job.QueryInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation,
            )
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(
                self.handle, win32job.JobObjectExtendedLimitInformation, info,
            )
        except BaseException:
            self.close()
            raise

    def attach(self, pid: int) -> None:
        import win32api
        import win32con

        handle = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid,
        )
        try:
            self.api.AssignProcessToJobObject(self.handle, handle)
        finally:
            handle.Close()

    def close(self) -> None:
        if self.handle is not None:
            self.handle.Close()
            self.handle = None
