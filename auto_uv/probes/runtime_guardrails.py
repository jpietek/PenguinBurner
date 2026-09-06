"""Runtime guardrails for an Auto-UV candidate probe.

Classify busy telemetry and failures that should be cached as unsafe.
"""

from __future__ import annotations

from auto_uv.domain.user_options import AUTO_UV_STALL_TUNING
from ..persistence.unsafe_voltage_cache import controlled_failure_reason


def telemetry_sample_is_busy(sample, busy_power_floor_w: float | None) -> bool:
    if sample is None:
        return False
    gpu_util_pct = getattr(sample, "gpu_util_pct", None)
    if (
        gpu_util_pct is not None
        and float(gpu_util_pct) >= AUTO_UV_STALL_TUNING.busy_gpu_util_pct
    ):
        return True
    power_w = getattr(sample, "power_w", None)
    return (
        power_w is not None
        and busy_power_floor_w is not None
        and float(power_w) >= float(busy_power_floor_w)
    )


def probe_failure_should_mark_voltage_unsafe(reason: str) -> bool:
    if controlled_failure_reason(reason):
        return False
    if str(reason).startswith(
        (
            "q2rtx-selected-nvidia-gpu-idle",
            "user-stop-requested",
            "busy core-clock telemetry missing",
        )
    ):
        return False
    return True
