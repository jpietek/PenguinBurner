"""Validate the selected curve without changing its tested geometry.

Current scans preserve the exact tested ramp; they do not restore stock or
rebuild lower points during final selection.
"""

from __future__ import annotations

from auto_uv.domain.types import AutoUvError


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
