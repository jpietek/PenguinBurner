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

# Measured scatter of the edge offset on the RTX 3080 was about +/-1.5 bins;
# two bins would have allowed the 937 mV / 1920 MHz climb rung that froze it.
EDGE_MARGIN_BINS = 3
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

    def minimum_voltage_mv(self, lock_clock_mhz: int) -> tuple[int, EdgePoint] | None:
        try:
            stock_here_mv = lock_voltage_for_target_clock(self.base_curve, int(lock_clock_mhz))
        except AutoUvError:
            return None
        best: tuple[int, EdgePoint] | None = None
        for point in self.points():
            predicted_mv = point.voltage_mv + (stock_here_mv - point.stock_voltage_mv)
            # At the freeze's own clock the prediction is the measurement
            # itself and the exact blacklist band already applies; one bin
            # above it is the same margin a resume uses. The wider margin
            # covers the model's scatter when extrapolating to other clocks.
            bins = (
                min(1, self.margin_bins)
                if abs(int(lock_clock_mhz) - point.lock_clock_mhz) <= STOCK_CLOCK_STEP_MHZ
                else self.margin_bins
            )
            minimum_mv = self._bins_above(predicted_mv, bins)
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
        return (
            f"predicted edge: {int(candidate_voltage_mv)}mV@{int(lock_clock_mhz)}MHz "
            f"is under the {int(minimum_mv)}mV floor ({self.margin_bins} bins above "
            f"the edge the {point.voltage_mv}mV@{point.lock_clock_mhz}MHz freeze implies)"
        )

    def describe(self) -> str:
        points = self.points()
        if not points:
            return ""
        listed = ", ".join(f"{p.voltage_mv}mV@{p.lock_clock_mhz}MHz" for p in points)
        return (
            f"edge margin: {len(points)} freeze(s) on record ({listed}); new probes "
            f"stay {self.margin_bins} bins above the edge they predict"
        )

    def _bins_above(self, predicted_mv: int, bins: int) -> int:
        if not self._bins:
            return int(predicted_mv)
        index = bisect_left(self._bins, int(predicted_mv)) + int(bins)
        return int(self._bins[min(index, len(self._bins) - 1)])
