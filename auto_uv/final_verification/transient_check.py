"""Prove that the final verification's CUDA pass actually stepped the load.

The final CUDA companion alternates a full grid with a fractional one. If
the pulses never moved board power, the phase ran without testing anything,
and completing is not the same as proving.
"""

from __future__ import annotations

from collections.abc import Sequence

from auto_uv.domain.user_options import (
    AUTO_UV_TRANSIENT_TUNING,
    AutoUvTransientTuning,
)
from auto_uv.shared.probe_data_fields import read_field


def power_pulse_cycle_count(
    samples: Sequence[object],
    *,
    tuning: AutoUvTransientTuning = AUTO_UV_TRANSIENT_TUNING,
) -> int:
    """How many times board power stepped up and fell back."""

    measured: list[float] = []
    for sample in samples:
        power_w = read_field(sample, "power_w")
        if power_w is None:
            continue
        try:
            power = float(power_w)
        except (TypeError, ValueError):
            continue
        if power > 0.0:
            measured.append(power)
    if len(measured) < 2:
        return 0

    loaded_floor_w = max(measured) * float(tuning.power_loaded_min_pct) / 100.0
    powers = [power for power in measured if power >= loaded_floor_w]
    if len(powers) < 2:
        return 0

    low_w = _percentile(powers, float(tuning.power_low_percentile))
    high_w = _percentile(powers, float(tuning.power_high_percentile))
    if low_w is None or high_w is None or high_w <= 0.0:
        return 0
    swing_w = high_w - low_w
    if swing_w < high_w * float(tuning.power_min_swing_pct) / 100.0:
        return 0

    # A quarter of the swing on each side is hysteresis: noise around the low
    # value still arms the counter, and noise around the high value still
    # completes it, but a flat trace never crosses either threshold.
    low_trigger_w = low_w + swing_w * 0.25
    high_trigger_w = high_w - swing_w * 0.25
    cycles = 0
    seen_low = False
    for power in powers:
        if power <= low_trigger_w:
            seen_low = True
        elif seen_low and power >= high_trigger_w:
            cycles += 1
            seen_low = False
    return cycles


def required_pulse_cycles(
    cuda_duration_s: int,
    *,
    pulse_on_s: float,
    pulse_off_s: float,
    tuning: AutoUvTransientTuning = AUTO_UV_TRANSIENT_TUNING,
) -> int:
    """The cycle floor a budget can actually afford.

    The first high phase has no return before it, and the final low phase may
    be cut off by the deadline, so only ``(duration - on) // cycle`` complete
    low-to-high returns can be observed. A 15 s CUDA window fits one, not two;
    requiring two would reject a correct short final.
    """

    cycle_s = max(1.0, float(pulse_on_s) + float(pulse_off_s))
    available = int(
        max(0.0, float(cuda_duration_s) - float(pulse_on_s)) // cycle_s
    )
    return min(int(tuning.pulse_cycles_min), max(1, available))


def missing_pulse_cycles_reason(
    cycle_count: int,
    required_cycles: int,
) -> str | None:
    if int(cycle_count) >= int(required_cycles):
        return None
    return (
        f"CUDA pulses never moved the load: cycles={int(cycle_count)} "
        f"required={int(required_cycles)}"
    )


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = (len(ordered) - 1) * max(0.0, min(100.0, float(percentile))) / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
