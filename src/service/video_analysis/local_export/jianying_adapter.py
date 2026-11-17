"""Small compatibility boundary around the framework Jianying controller."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import config


def _process_info() -> tuple[bool, str, str]:
    if os.name != "nt":
        return False, "", ""
    command = (
        "$p=Get-CimInstance Win32_Process -Filter \"Name='JianyingPro.exe'\" | "
        "Select-Object -First 1; if($p){$v=(Get-Item -LiteralPath $p.ExecutablePath).VersionInfo; "
        "Write-Output $p.ExecutablePath; Write-Output $v.FileVersion}"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "", ""
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return bool(lines), lines[0] if lines else "", lines[1] if len(lines) > 1 else ""


def _major_version(version: str) -> int | None:
    try:
        return int(version.split(".", 1)[0])
    except (AttributeError, TypeError, ValueError):
        return None


def inspect_environment(*, probe_ui: bool = False) -> dict[str, Any]:
    running, executable, version = _process_info()
    major_version = _major_version(version)
    supported_version = major_version is not None and 1 <= major_version <= 6
    draft_path = Path(config.DRAFT_SAVE_PATH)
    result: dict[str, Any] = {
        "platform": sys.platform,
        "windows": sys.platform == "win32",
        "jianying_running": running,
        "jianying_executable": executable,
        "jianying_version": version,
        "supported_version": supported_version,
        "draft_path": str(draft_path),
        "draft_path_exists": draft_path.is_dir(),
        "ui_automation_available": False,
        "ui_probe_ok": None,
        "warning": "",
    }
    if sys.platform != "win32":
        result["warning"] = "本地剪映导出只支持Windows交互式桌面"
        return result
    try:
        import uiautomation  # noqa: F401
        import pyautogui  # noqa: F401
        result["ui_automation_available"] = True
    except ImportError as exc:
        result["warning"] = f"缺少Windows UI自动化依赖: {exc}"
        return result
    if running and major_version is None:
        result["warning"] = "无法读取剪映版本；本地导出仅支持剪映专业版6.x及以下版本"
        return result
    if running and not supported_version:
        result["warning"] = (
            f"当前剪映版本{version or '未知'}不受支持；"
            "本地导出仅支持剪映专业版6.x及以下版本"
        )
        return result
    if probe_ui and running:
        try:
            from src.pyJianYingDraft import JianyingController
            if JianyingController is None:
                raise RuntimeError("JianyingController不可用")
            controller = JianyingController()
            result["ui_probe_ok"] = bool(getattr(controller, "app", None))
        except Exception as exc:
            result["ui_probe_ok"] = False
            result["ui_probe_error"] = str(exc)
    return result


def require_export_environment() -> dict[str, Any]:
    result = inspect_environment(probe_ui=False)
    if not result["windows"]:
        raise RuntimeError("本地剪映导出只支持Windows交互式桌面")
    if not result["draft_path_exists"]:
        raise RuntimeError(f"剪映草稿目录不存在: {result['draft_path']}")
    if not result["ui_automation_available"]:
        raise RuntimeError(result["warning"] or "缺少Windows UI自动化依赖")
    if not result["jianying_running"]:
        raise RuntimeError("剪映专业版未运行，请登录并停留在草稿首页")
    if not result["supported_version"]:
        raise RuntimeError(result["warning"] or "本地导出仅支持剪映专业版6.x及以下版本")
    return result


def export_draft(
    draft_id: str,
    output_path: Path,
    *,
    resolution: str | None,
    framerate: int | None,
    timeout_seconds: int,
) -> None:
    require_export_environment()
    from uiautomation import UIAutomationInitializerInThread
    from src.pyJianYingDraft import JianyingController, ExportResolution, ExportFramerate

    if JianyingController is None:
        raise RuntimeError("JianyingController不可用，请安装capcut-mate[windows]")
    resolution_value = ExportResolution(resolution) if resolution else None
    framerate_value = ExportFramerate(f"{framerate}fps") if framerate else None
    draft_dir = Path(config.DRAFT_SAVE_PATH) / draft_id
    with UIAutomationInitializerInThread():
        controller = JianyingController()
        controller.export_draft(
            draft_id,
            str(output_path),
            resolution=resolution_value,
            framerate=framerate_value,
            timeout=float(timeout_seconds),
            draft_dir=str(draft_dir),
        )

