from __future__ import annotations

import re
from dataclasses import dataclass

from drivers.nvidia.daemon_gpu import DaemonGpuClient

from .constants import DEFAULT_HEIGHT, DEFAULT_WIDTH

Q2RTX_SCAN_RESOLUTIONS: dict[str, tuple[int | None, int | None]] = {
    "auto": (None, None),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4k": (3840, 2160),
}

@dataclass(frozen=True, slots=True)
class Q2RTXResolutionChoice:
    width: int
    height: int
    reason: str
    gpu_name: str = ""
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
        gpu_name = DaemonGpuClient(int(gpu_index)).capabilities().identity.name
    except Exception:  # noqa: BLE001
        gpu_name = ""
    # Only explicitly supported desktop models use the heavier 4K workload.
    # Full matching excludes laptop variants and unknown model suffixes;
    # memory capacity alone does not establish ray-tracing capability.
    use_4k = re.fullmatch(
        r"(?:NVIDIA\s+)?(?:GEFORCE\s+)?RTX\s*"
        r"(?:4080(?:\s*SUPER)?|4090|5070\s*TI|5080|5090)",
        gpu_name.strip(),
        flags=re.IGNORECASE,
    ) is not None
    return Q2RTXResolutionChoice(
        width=DEFAULT_WIDTH if use_4k else 1920,
        height=DEFAULT_HEIGHT if use_4k else 1080,
        reason="auto-gpu-4k" if use_4k else "auto-gpu-1080p",
        gpu_name=gpu_name,
        auto_selected=True,
    )


def format_q2rtx_resolution_choice(choice: Q2RTXResolutionChoice) -> str:
    text = f"{int(choice.width)}x{int(choice.height)}"
    if not choice.auto_selected:
        return f"{text} ({choice.reason})"
    return f"{text} ({choice.reason}, gpu={choice.gpu_name or 'unknown'})"


def _requested_dimension(value: int | None, label: str) -> int | None:
    if value is None:
        return None
    dimension = int(value)
    if dimension < 0:
        raise ValueError(f"Q2RTX render {label} must be positive or omitted")
    if dimension == 0:
        return None
    return dimension
