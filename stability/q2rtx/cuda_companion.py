"""Build the optional CUDA companion command used beside Q2RTX."""

from __future__ import annotations

import sys
from pathlib import Path

from stability import cuda_bruteforce


def cuda_bruteforce_companion_command(
    *,
    gpu_index: int,
    duration_s: int,
) -> tuple[str, ...]:
    return (
        str(sys.executable),
        str(cuda_bruteforce_script_path()),
        "--gpu-index",
        str(int(gpu_index)),
        "--duration-seconds",
        str(max(1, int(duration_s))),
    )


def cuda_pulse_companion_command(
    *,
    gpu_index: int,
    duration_s: int,
    pulse_on_s: float,
    pulse_off_s: float,
    pulse_low_fraction: float,
) -> tuple[str, ...]:
    """Alternate full and fractional CUDA grids inside one companion run."""

    return cuda_bruteforce_companion_command(
        gpu_index=int(gpu_index),
        duration_s=int(duration_s),
    ) + (
        "--pulse-on-seconds",
        f"{float(pulse_on_s):g}",
        "--pulse-off-seconds",
        f"{float(pulse_off_s):g}",
        "--pulse-low-fraction",
        f"{float(pulse_low_fraction):g}",
    )


def cuda_bruteforce_script_path() -> Path:
    return Path(cuda_bruteforce.__file__).resolve()
