"""Shape the optional rising tail above an Auto-UV lock voltage.

A zero-bin curve remains flat. A non-zero tail lets a small number of
higher-voltage bins rise softly while still capping the rest of the tail.
"""

from __future__ import annotations

from typing import Any, cast


def normalize_tail_rise_bins(value: object) -> int:
    try:
        return max(0, int(cast(Any, value or 0)))
    except (TypeError, ValueError):
        return 0


def rising_tail_targets(
    base_curve: list[dict],
    *,
    lock_clock_mhz: int,
    candidate_voltage_mv: int,
    tail_rise_bins: int,
    clock_step_mhz: int,
) -> dict[int, int]:
    """Return target clocks for the candidate bin and all editable bins above it."""

    lock_clock = int(lock_clock_mhz)
    candidate_voltage = int(candidate_voltage_mv)
    bins = normalize_tail_rise_bins(tail_rise_bins)
    step_mhz = max(1, int(clock_step_mhz))
    editable_tail_voltages = [
        int(point["voltage_mv"])
        for point in base_curve
        if not point.get("preserve_base")
        and int(point["voltage_mv"]) > candidate_voltage
    ]
    tail_index_by_voltage = {
        voltage_mv: index + 1
        for index, voltage_mv in enumerate(sorted(set(editable_tail_voltages)))
    }
    max_tail_clock_mhz = lock_clock + (step_mhz * bins)

    targets: dict[int, int] = {}
    for point in base_curve:
        voltage_mv = int(point["voltage_mv"])
        if voltage_mv < candidate_voltage or point.get("preserve_base"):
            continue
        if voltage_mv == candidate_voltage or bins <= 0:
            targets[voltage_mv] = lock_clock
            continue
        rise_index = tail_index_by_voltage.get(voltage_mv, bins)
        soft_cap_mhz = lock_clock + (step_mhz * min(int(rise_index), bins))
        # The lock bin can already be above the stock clock for nearby higher
        # voltages. Clamping to each original target would create a flat shelf
        # before the requested tail rise begins.
        targets[voltage_mv] = max(
            lock_clock,
            min(soft_cap_mhz, max_tail_clock_mhz),
        )
    return targets


def tail_ceiling_clock_mhz(
    plan: list[dict],
    *,
    fallback_clock_mhz: int,
    lock_voltage_mv: int | None = None,
) -> int:
    ceiling_mhz = int(fallback_clock_mhz)
    for point in plan or []:
        if lock_voltage_mv is not None and int(point["voltage_mv"]) < int(lock_voltage_mv):
            continue
        ceiling_mhz = max(ceiling_mhz, int(point["target_mhz"]))
    return ceiling_mhz
