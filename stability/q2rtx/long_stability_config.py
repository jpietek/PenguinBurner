"""Build generic long Q2RTX + CUDA stability-test configurations.

The helper splits one user duration between Q2RTX benchmark and CUDA companion load.
"""

from __future__ import annotations

from dataclasses import replace

from stability.cuda_bruteforce import PULSE_LOW_FRACTION_DEFAULT

from .constants import DEFAULT_SINGLE_PASS_TIMEOUT_S
from .cuda_companion import (
    cuda_bruteforce_companion_command,
    cuda_pulse_companion_command,
)
from .models import Q2RTXStabilityConfig

LONG_STABILITY_CUDA_FRACTION = 0.25
LONG_STABILITY_TIMEOUT_BUFFER_S = 60.0
# Final verification runs the CUDA half as load steps: full grid, then a
# fractional grid, so current changes while the engine stays loaded. Profile
# verification keeps the steady companion unless it asks for pulses.
FINAL_CUDA_PULSE_ON_S = 4.0
FINAL_CUDA_PULSE_OFF_S = 3.0
FINAL_CUDA_PULSE_LOW_FRACTION = PULSE_LOW_FRACTION_DEFAULT


def long_stability_workload_durations(
    total_duration_s: int,
) -> tuple[int, int]:
    total_s = max(2, int(total_duration_s))
    cuda_s = max(
        1,
        round(float(total_s) * float(LONG_STABILITY_CUDA_FRACTION)),
    )
    return max(1, int(total_s) - int(cuda_s)), int(cuda_s)


def build_long_stability_test_config(
    config: Q2RTXStabilityConfig,
    *,
    total_duration_s: int,
    cuda_pulses: bool = False,
) -> Q2RTXStabilityConfig:
    q2rtx_s, cuda_s = long_stability_workload_durations(int(total_duration_s))
    if not cuda_pulses:
        companion = cuda_bruteforce_companion_command(
            gpu_index=int(config.gpu_index),
            duration_s=int(cuda_s),
        )
    else:
        companion = cuda_pulse_companion_command(
            gpu_index=int(config.gpu_index),
            duration_s=int(cuda_s),
            pulse_on_s=FINAL_CUDA_PULSE_ON_S,
            pulse_off_s=FINAL_CUDA_PULSE_OFF_S,
            pulse_low_fraction=FINAL_CUDA_PULSE_LOW_FRACTION,
        )
    return replace(
        config,
        duration_s=int(q2rtx_s),
        companion_command=companion,
        single_pass_timeout_s=max(
            float(DEFAULT_SINGLE_PASS_TIMEOUT_S),
            float(max(1, int(total_duration_s)))
            + float(LONG_STABILITY_TIMEOUT_BUFFER_S),
        ),
    )
