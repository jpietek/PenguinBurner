"""Build bounded voltage/clock interpolation steps for performance Auto-OC."""

from __future__ import annotations

from dataclasses import dataclass

from auto_uv.curve.base_vf_curve_voltage_bins import editable_voltage_bins
from auto_uv.curve.vf_curve_flattening import FlatteningRules, snap_target_clock
from .settings import AUTO_OC_DEFAULT_MAX_INTERPOLATION_STEPS


@dataclass(frozen=True, slots=True)
class AutoOcStep:
    index: int
    voltage_mv: int
    target_mhz: int
    ratio: float


def build_auto_oc_ladder(
    base_curve: list[dict],
    *,
    start_voltage_mv: int,
    start_clock_mhz: int,
    endpoint_voltage_mv: int,
    endpoint_clock_mhz: int,
    max_steps: int = AUTO_OC_DEFAULT_MAX_INTERPOLATION_STEPS,
    clock_step_mhz: int = 15,
) -> list[AutoOcStep]:
    """Return unique candidate steps from the verified UV point to the OC cap."""

    start_voltage = int(start_voltage_mv)
    start_clock = int(start_clock_mhz)
    endpoint_voltage = int(endpoint_voltage_mv)
    endpoint_clock = int(endpoint_clock_mhz)
    if start_voltage > endpoint_voltage or start_clock > endpoint_clock:
        return []
    if start_voltage == endpoint_voltage and start_clock == endpoint_clock:
        return []

    # Power-bound savings tiers have already found their lowest stable
    # voltage. Reclaim the clock headroom created by that undervolt without
    # walking voltage upward again.
    if start_voltage == endpoint_voltage:
        step_count = max(1, int(max_steps))
        seen_clocks: set[int] = set()
        steps: list[AutoOcStep] = []
        for raw_index in range(1, step_count + 1):
            ratio = float(raw_index) / float(step_count)
            requested_clock_mhz = int(
                round(
                    float(start_clock)
                    + (float(endpoint_clock - start_clock) * ratio)
                )
            )
            target_mhz = bounded_clock(
                requested_clock_mhz,
                start_clock_mhz=start_clock,
                endpoint_clock_mhz=endpoint_clock,
                clock_step_mhz=int(clock_step_mhz),
            )
            if target_mhz <= start_clock or target_mhz in seen_clocks:
                continue
            seen_clocks.add(target_mhz)
            steps.append(
                AutoOcStep(
                    index=len(steps) + 1,
                    voltage_mv=int(start_voltage),
                    target_mhz=int(target_mhz),
                    ratio=ratio,
                )
            )
        return steps

    voltages = [
        int(voltage)
        for voltage in sorted(editable_voltage_bins(base_curve))
        if start_voltage <= int(voltage) <= endpoint_voltage
    ]
    if not voltages:
        return []

    step_count = max(1, int(max_steps))
    seen_voltages: set[int] = set()
    steps: list[AutoOcStep] = []
    for raw_index in range(1, step_count + 1):
        ratio = float(raw_index) / float(step_count)
        voltage_mv = interpolated_voltage(
            voltages,
            start_voltage_mv=start_voltage,
            endpoint_voltage_mv=endpoint_voltage,
            ratio=ratio,
        )
        requested_clock_mhz = int(
            round(float(start_clock) + (float(endpoint_clock - start_clock) * ratio))
        )
        target_mhz = bounded_clock(
            requested_clock_mhz,
            start_clock_mhz=start_clock,
            endpoint_clock_mhz=endpoint_clock,
            clock_step_mhz=int(clock_step_mhz),
        )
        voltage_key = int(voltage_mv)
        if ratio == 1.0 and steps and steps[-1].voltage_mv == voltage_key:
            # A sparse grid may reach the endpoint voltage before the final
            # clock. Keep one probe per voltage, but test the full endpoint.
            steps[-1] = AutoOcStep(
                index=steps[-1].index,
                voltage_mv=voltage_key,
                target_mhz=int(target_mhz),
                ratio=ratio,
            )
            continue
        if voltage_key <= start_voltage or voltage_key in seen_voltages:
            continue
        seen_voltages.add(voltage_key)
        steps.append(
            AutoOcStep(
                index=len(steps) + 1,
                voltage_mv=int(voltage_mv),
                target_mhz=int(target_mhz),
                ratio=ratio,
            )
        )
    return steps


def interpolated_voltage(
    voltages: list[int],
    *,
    start_voltage_mv: int,
    endpoint_voltage_mv: int,
    ratio: float,
) -> int:
    if int(start_voltage_mv) == int(endpoint_voltage_mv):
        return int(start_voltage_mv)
    requested = float(start_voltage_mv) + (
        float(endpoint_voltage_mv - start_voltage_mv) * float(ratio)
    )
    return int(min(voltages, key=lambda voltage: abs(float(voltage) - requested)))


def bounded_clock(
    requested_clock_mhz: int,
    *,
    start_clock_mhz: int,
    endpoint_clock_mhz: int,
    clock_step_mhz: int = 15,
) -> int:
    if int(requested_clock_mhz) >= int(endpoint_clock_mhz):
        return int(endpoint_clock_mhz)
    snapped = snap_target_clock(
        int(requested_clock_mhz),
        rules=FlatteningRules(clock_step_mhz=max(1, int(clock_step_mhz))),
    )
    return max(int(start_clock_mhz), min(int(endpoint_clock_mhz), int(snapped)))
