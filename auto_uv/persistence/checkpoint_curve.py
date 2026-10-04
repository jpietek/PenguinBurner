"""Reconcile stock V/F readback drift without changing measured clock targets."""

from __future__ import annotations


def compatible_stock_curves(saved: object, current: object) -> bool:
    """Allow one 15 MHz stock bin of drift; retain every other curve constraint."""
    if not isinstance(saved, list) or not isinstance(current, list):
        return False
    if not saved or len(saved) != len(current):
        return False
    try:
        for before, after in zip(saved, current, strict=True):
            for point in (before, after):
                if (
                    int(point["target_mhz"]) != int(point["base_mhz"])
                    or int(point["current_offset_mhz"]) != 0
                    or int(point["new_offset_mhz"]) != 0
                ):
                    return False
            if int(after["base_mhz"]) - int(before["base_mhz"]) not in {-15, 0, 15}:
                return False
            if {
                key: value
                for key, value in before.items()
                if key not in {"base_mhz", "target_mhz"}
            } != {
                key: value
                for key, value in after.items()
                if key not in {"base_mhz", "target_mhz"}
            }:
                return False
        indices = [point["index"] for point in current]
        voltages = [point["voltage_mv"] for point in current]
        return len(set(indices)) == len(indices) and len(set(voltages)) == len(voltages)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def rebase_measured_plan(plan: list[dict], base_curve: list[dict]) -> list[dict]:
    """Keep the measured absolute curve, recalculating offsets for today's base."""
    current = {int(point["index"]): point for point in base_curve}
    if len(plan) != len(current) or {int(point["index"]) for point in plan} != set(
        current
    ):
        raise ValueError("saved candidate V/F grid changed")
    result = []
    for point in plan:
        base = current[int(point["index"])]
        if int(point["voltage_mv"]) != int(base["voltage_mv"]):
            raise ValueError("saved candidate voltage bin changed")
        target = int(point["target_mhz"])
        result.append(
            {
                **point,
                "base_mhz": int(base["base_mhz"]),
                "current_offset_mhz": int(base["current_offset_mhz"]),
                "new_offset_mhz": target - int(base["base_mhz"]),
            }
        )
    return result
