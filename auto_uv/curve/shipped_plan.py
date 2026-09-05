"""Validate saved curve ordering and retain legacy stock-restoration helpers.

Current scans preserve the exact tested ramp; they do not restore stock or
rebuild lower points during final selection.
"""

from __future__ import annotations

from auto_uv.domain.types import AutoUvError


def restore_stock_below_validated_floor(
    plan: list[dict],
    *,
    floor_voltage_mv: int,
    lock_clock_mhz: int,
    below_lock_gap_mhz: int = 15,
) -> list[dict]:
    """Remove probe-only shaping and prevent downward edges at the floor.

    Every rewritten target is at most its stock clock and at least one clock
    step below the selected lock. This intermediate result can have an upward
    step. Current scans keep their measured candidate plan instead.
    """
    floor = int(floor_voltage_mv)
    below_lock_cap_mhz = max(
        0,
        int(lock_clock_mhz) - max(1, int(below_lock_gap_mhz)),
    )
    shipped: list[dict] = []
    for point in plan:
        new_point = dict(point)
        try:
            voltage_mv = int(point["voltage_mv"])
            base_mhz = int(point["base_mhz"])
        except (KeyError, TypeError, ValueError):
            shipped.append(new_point)
            continue
        if not point.get("preserve_base") and voltage_mv < floor:
            target_mhz = min(int(base_mhz), int(below_lock_cap_mhz))
            new_point["target_mhz"] = int(target_mhz)
            new_point["new_offset_mhz"] = int(target_mhz) - int(base_mhz)
        shipped.append(new_point)
    assert_monotonic_editable_targets(shipped)
    return shipped


def assert_monotonic_editable_targets(plan: list[dict]) -> None:
    """Reject a shipped editable V/F curve that falls as voltage rises."""
    points: list[tuple[int, int]] = []
    for point in plan:
        if point.get("preserve_base"):
            continue
        try:
            points.append((int(point["voltage_mv"]), int(point["target_mhz"])))
        except (KeyError, TypeError, ValueError):
            continue
    points.sort(key=lambda item: item[0])
    for previous, current in zip(points, points[1:]):
        if int(current[1]) < int(previous[1]):
            raise AutoUvError(
                "refusing to ship non-monotonic V/F curve: "
                f"{int(previous[0])}mV@{int(previous[1])}MHz -> "
                f"{int(current[0])}mV@{int(current[1])}MHz"
            )


def validated_floor_voltage_mv(
    stable_history,
    *,
    fallback_voltage_mv: int,
) -> int:
    """The lowest voltage the current scan proved with a passed probe."""
    voltages = [
        int(probe.candidate_voltage_mv)
        for probe in list(stable_history or [])
        if getattr(probe, "candidate_voltage_mv", None) is not None
    ]
    voltages.append(int(fallback_voltage_mv))
    return min(voltages)
