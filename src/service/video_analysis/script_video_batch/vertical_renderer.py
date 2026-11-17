"""FFmpeg filters for deterministic portrait material rendering."""
from __future__ import annotations


FIT_MODES = {"smart_crop", "cover", "contain_blur"}


def rotation_filter(rotation_degrees: int = 0) -> str:
    return {
        0: "",
        90: "transpose=1,",
        180: "hflip,vflip,",
        270: "transpose=2,",
    }.get(rotation_degrees, "")


def filter_args(*, width: int, height: int, duration_us: int,
                fit_mode: str, focus_x: float = 0.5,
                focus_y: float = 0.5, rotation_degrees: int = 0) -> list[str]:
    if fit_mode not in FIT_MODES:
        fit_mode = "cover"
    duration = max(0, duration_us) / 1_000_000
    prefix = (
        f"trim=duration={duration:.6f},setpts=PTS-STARTPTS,"
        f"{rotation_filter(rotation_degrees)}"
    ).rstrip(",")
    suffix = "setsar=1,tpad=stop_mode=clone:stop_duration=0.100"
    if fit_mode == "contain_blur":
        value = (
            f"[0:v]{prefix},split=2[bg_source][fg_source];"
            f"[bg_source]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=30[bg];"
            f"[fg_source]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2,"
            f"{suffix}[out]"
        )
        return ["-filter_complex", value, "-map", "[out]"]

    focus_x = max(0.0, min(1.0, float(focus_x)))
    focus_y = max(0.0, min(1.0, float(focus_y)))
    if fit_mode == "cover":
        focus_x = focus_y = 0.5
    crop_x = f"max(0,min(iw-ow,iw*{focus_x:.4f}-ow/2))"
    crop_y = f"max(0,min(ih-oh,ih*{focus_y:.4f}-oh/2))"
    value = (
        f"{prefix},scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:x='{crop_x}':y='{crop_y}',{suffix}"
    )
    return ["-vf", value]
