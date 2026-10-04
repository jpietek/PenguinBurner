from __future__ import annotations

from dataclasses import dataclass

from drivers.nvidia.daemon_gpu import DaemonGpuClient

from .constants import DEFAULT_HEIGHT, DEFAULT_WIDTH

Q2RTX_SCAN_RESOLUTIONS: dict[str, tuple[int | None, int | None]] = {
    "auto": (None, None),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4k": (3840, 2160),
}

LOW_VRAM_WIDTH = 2560
LOW_VRAM_HEIGHT = 1440
AUTO_RESOLUTION_MAX_1440P_BYTES = 8 * 1024**3


@dataclass(frozen=True, slots=True)
class Q2RTXResolutionChoice:
    width: int
    height: int
    reason: str
    vram_total_bytes: int | None = None
    auto_selected: bool = False


def resolve_q2rtx_render_resolution(
    *,
    gpu_index: int,
    requested_width: int | None = None,
    requested_height: int | None = None,
) -> Q2RTXResolutionChoice:
    width = _requested_dimension(requested_width, "width")
    height = _requested_dimension(requested_height, "height")
    if width is not None and height is not None:
        return Q2RTXResolutionChoice(
            width=width,
            height=height,
            reason="manual",
            auto_selected=False,
        )
    if width is not None:
        return Q2RTXResolutionChoice(
            width=width,
            height=max(1, round(float(width) * 9.0 / 16.0)),
            reason="manual-width-16:9",
            auto_selected=False,
        )
    if height is not None:
        return Q2RTXResolutionChoice(
            width=max(1, round(float(height) * 16.0 / 9.0)),
            height=height,
            reason="manual-height-16:9",
            auto_selected=False,
        )

    try:
        memory_info = DaemonGpuClient(int(gpu_index)).capabilities().memory
    except Exception:  # noqa: BLE001
        memory_info = None
    total_bytes = int(memory_info.total_bytes) if memory_info is not None else None
    if total_bytes is not None and total_bytes <= AUTO_RESOLUTION_MAX_1440P_BYTES:
        return Q2RTXResolutionChoice(
            width=LOW_VRAM_WIDTH,
            height=LOW_VRAM_HEIGHT,
            reason="auto-vram-le8gib",
            vram_total_bytes=total_bytes,
            auto_selected=True,
        )
    return Q2RTXResolutionChoice(
        width=DEFAULT_WIDTH,
        height=DEFAULT_HEIGHT,
        reason=("auto-vram-gt8gib" if total_bytes is not None else "auto-vram-unknown"),
        vram_total_bytes=total_bytes,
        auto_selected=True,
    )


def format_q2rtx_resolution_choice(choice: Q2RTXResolutionChoice) -> str:
    text = f"{int(choice.width)}x{int(choice.height)}"
    if not bool(choice.auto_selected):
        return f"{text} ({choice.reason})"
    if choice.vram_total_bytes is None:
        return f"{text} ({choice.reason})"
    gib = float(choice.vram_total_bytes) / float(1024**3)
    return f"{text} ({choice.reason}, vram={gib:.1f}GiB)"


def _requested_dimension(value: int | None, label: str) -> int | None:
    if value is None:
        return None
    dimension = int(value)
    if dimension < 0:
        raise ValueError(f"Q2RTX render {label} must be positive or omitted")
    if dimension == 0:
        return None
    return dimension
