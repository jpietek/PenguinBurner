"""Answer voltage-bin questions against the validated base curve.

Keeping these helpers together prevents every sweep rule from inventing its own bin ordering.
"""

from __future__ import annotations

from auto_uv.domain.types import AutoUvError

from .base_vf_curve import editable_base_vf_points


def editable_voltage_bins(base_curve: list[dict]) -> list[int]:
    return [int(point.voltage_mv) for point in editable_base_vf_points(base_curve)]


def nearest_editable_voltage_bin(base_curve: list[dict], voltage_mv: int) -> int:
    bins = editable_voltage_bins(base_curve)
    if not bins:
        raise ValueError("base V/F curve did not contain editable voltage bins")
    return int(min(bins, key=lambda value: abs(int(value) - int(voltage_mv))))


def lower_editable_voltage_bins(
    base_curve: list[dict],
    start_voltage_mv: int,
    *,
    min_search_voltage_mv: int | None = None,
) -> list[int]:
    bins = []
    for voltage_mv in editable_voltage_bins(base_curve):
        if int(voltage_mv) >= int(start_voltage_mv):
            continue
        if min_search_voltage_mv is not None and int(voltage_mv) < int(
            min_search_voltage_mv
        ):
            continue
        bins.append(int(voltage_mv))
    return sorted(bins, reverse=True)


def higher_editable_voltage_bins(base_curve: list[dict], voltage_mv: int) -> list[int]:
    return [
        int(value)
        for value in editable_voltage_bins(base_curve)
        if int(value) > int(voltage_mv)
    ]


def next_higher_editable_voltage_bin(
    base_curve: list[dict],
    voltage_mv: int,
) -> int | None:
    higher = higher_editable_voltage_bins(base_curve, int(voltage_mv))
    return int(min(higher)) if higher else None


def lock_voltage_for_target_clock(base_curve: list[dict], target_clock_mhz: int) -> int:
    for point in editable_base_vf_points(base_curve):
        if int(point.target_mhz) >= int(target_clock_mhz):
            return int(point.voltage_mv)
    raise AutoUvError(f"base V/F curve never reaches {int(target_clock_mhz)}MHz")
