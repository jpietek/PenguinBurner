"""Keep later probes above the stability edge a hard crash has already revealed.

On some cards the first failing probe of a descent or climb is not a wrong
result but a bus drop that takes the host down. Each such freeze is one point
on the card's stability edge. The edge runs parallel to the card's own stock
V/F curve (in requested-lock-clock terms; on an RTX 3080 the four recorded
freezes sat 95 to 112 mV below stock), so one freeze predicts the edge at
every other clock. Later probes stay a fixed number of voltage bins above that
prediction; the final soak still decides what ships. Cards that fail softly
never record a hard crash and keep their full search.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

from auto_uv.curve.base_vf_curve_voltage_bins import (
    editable_voltage_bins,
    lock_voltage_for_target_clock,
)
from auto_uv.domain.types import AutoUvError
from auto_uv.persistence.unsafe_voltage_cache import unsafe_entry_reason_values
from auto_uv.shared.positive_int import positive_int

# The nearest recorded freeze (in requested clock) predicts the edge; the
# margin above it grows with how far that prediction is extrapolated. On the
# issue 109 card the far margin needed three bins (two would have allowed the
# 937 mV / 1920 MHz climb rung that froze it), while at the freeze's own clock
# one bin above it is the point the card went on to prove.
EDGE_MARGIN_BINS = 3
EDGE_MARGIN_NEAR_BINS = 1
EDGE_MARGIN_MID_BINS = 2
EDGE_MARGIN_NEAR_MHZ = 15
EDGE_MARGIN_MID_MHZ = 90
STOCK_CLOCK_STEP_MHZ = 15
HARD_CRASH_REASON_PREFIXES = (
    "previous-run-abruptly-ended",
    "nvidia-xid",
    "gpu-hang",
    "cuda-bruteforce-failed exit=4",  # the companion's hang watchdog
)


@dataclass(frozen=True, slots=True)
class EdgePoint:
    voltage_mv: int
    lock_clock_mhz: int
    stock_voltage_mv: int


def hard_crash_entry(entry: dict) -> bool:
    return any(
        reason.startswith(HARD_CRASH_REASON_PREFIXES)
        for reason in unsafe_entry_reason_values(entry)
    )


def edge_points(unsafe_entries: list[dict], base_curve: list[dict]) -> list[EdgePoint]:
    points: list[EdgePoint] = []
    for entry in unsafe_entries:
        if not isinstance(entry, dict) or not hard_crash_entry(entry):
            continue
        voltage_mv = positive_int(entry.get("candidate_voltage_mv"))
        lock_clock_mhz = positive_int(entry.get("lock_clock_mhz"))
        if voltage_mv is None or lock_clock_mhz is None:
            continue
        try:
            stock_voltage_mv = lock_voltage_for_target_clock(base_curve, int(lock_clock_mhz))
        except AutoUvError:
            continue
        points.append(EdgePoint(int(voltage_mv), int(lock_clock_mhz), int(stock_voltage_mv)))
    return points


class EdgeMargin:
    """Minimum voltage per requested clock, derived from recorded freezes.

    Reads the entry list on every call so a freeze recorded earlier in the
    same scan (the list the tiers accumulate into) counts for later tiers.
    """

    def __init__(
        self,
        unsafe_entries: list[dict],
        base_curve: list[dict],
        *,
        margin_bins: int = EDGE_MARGIN_BINS,
    ) -> None:
        self.unsafe_entries = unsafe_entries
        self.base_curve = base_curve
        self.margin_bins = max(0, int(margin_bins))
        self._bins = sorted(set(editable_voltage_bins(base_curve)))

    def points(self) -> list[EdgePoint]:
        return edge_points(self.unsafe_entries, self.base_curve)

    def margin_bins_for(self, clock_distance_mhz: int) -> int:
        distance = abs(int(clock_distance_mhz))
        if distance <= EDGE_MARGIN_NEAR_MHZ:
            return min(EDGE_MARGIN_NEAR_BINS, self.margin_bins)
        if distance <= EDGE_MARGIN_MID_MHZ:
            return min(EDGE_MARGIN_MID_BINS, self.margin_bins)
        return self.margin_bins

    def minimum_voltage_mv(self, lock_clock_mhz: int) -> tuple[int, EdgePoint] | None:
        """The floor the nearest freeze implies at this clock, with its margin.

        A far freeze extrapolated over a long clock range over- or
        under-predicts by a bin or two; the nearest one is the better
        estimate, so it alone decides (the issue 109 card lost four
        Efficiency bins when a 1920 MHz freeze outvoted its own 1750 MHz one).
        """
        try:
            stock_here_mv = lock_voltage_for_target_clock(self.base_curve, int(lock_clock_mhz))
        except AutoUvError:
            return None
        points = self.points()
        if not points:
            return None
        nearest = min(abs(int(lock_clock_mhz) - p.lock_clock_mhz) for p in points)
        best: tuple[int, EdgePoint] | None = None
        for point in points:
            distance = abs(int(lock_clock_mhz) - point.lock_clock_mhz)
            if distance != nearest:
                continue
            predicted_mv = point.voltage_mv + (stock_here_mv - point.stock_voltage_mv)
            minimum_mv = self._bins_above(predicted_mv, self.margin_bins_for(distance))
            if best is None or minimum_mv > best[0]:
                best = (minimum_mv, point)
        return best

    def block_reason(self, *, candidate_voltage_mv: int, lock_clock_mhz: int) -> str:
        found = self.minimum_voltage_mv(int(lock_clock_mhz))
        if found is None:
            return ""
        minimum_mv, point = found
        if int(candidate_voltage_mv) >= int(minimum_mv):
            return ""
        bins = self.margin_bins_for(int(lock_clock_mhz) - point.lock_clock_mhz)
        return (
            f"predicted edge: {int(candidate_voltage_mv)}mV@{int(lock_clock_mhz)}MHz "
            f"is under the {int(minimum_mv)}mV floor ({bins} bin(s) above "
            f"the edge the {point.voltage_mv}mV@{point.lock_clock_mhz}MHz freeze implies)"
        )

    def describe(self) -> str:
        points = self.points()
        if not points:
            return ""
        listed = ", ".join(f"{p.voltage_mv}mV@{p.lock_clock_mhz}MHz" for p in points)
        return (
            f"edge margin: {len(points)} freeze(s) on record ({listed}); new probes "
            f"stay {EDGE_MARGIN_NEAR_BINS} to {self.margin_bins} bins above the edge "
            "the nearest one predicts"
        )

    def _bins_above(self, predicted_mv: int, bins: int) -> int:
        if not self._bins:
            return int(predicted_mv)
        index = bisect_left(self._bins, int(predicted_mv)) + int(bins)
        return int(self._bins[min(index, len(self._bins) - 1)])
