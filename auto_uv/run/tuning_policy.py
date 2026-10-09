"""What a scan may do near a card's stability edge, decided once per scan.

Careful (the default) keeps every probe above the edge that recorded freezes
predict, soaks one step above a cleanly reached floor, and lets Performance
spend a little more voltage than Balanced proved. Aggressive drops the
predictions and the floor caution and gives Performance more voltage to climb
with; the hard blacklist of points that actually failed applies in both.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from auto_uv.run.edge_margin import EdgeMargin
from auto_uv.scan_mode.tuning_mode import (
    AUTO_UV_TUNING_MODE_AGGRESSIVE,
    normalize_auto_uv_tuning_mode,
)

# Voltage bins (about 6 mV each) Performance may add above the Balanced-proven
# voltage to climb past rungs the blacklist or the edge margin close at that
# voltage. Four bins cover the issue 109 card (918 proven, climb open at 943).
PERFORMANCE_VOLTAGE_HEADROOM_BINS = {"careful": 4, "aggressive": 8}


@dataclass(frozen=True, slots=True)
class TuningPolicy:
    mode: str
    edge_margin: EdgeMargin | None
    floor_caution: bool
    performance_voltage_headroom_bins: int
    # Whether a climb may retry, at a higher voltage, a rung (or anything
    # above it) that already froze the host at a Performance-class voltage.
    # Careful mode spends at most one reboot per climb; aggressive keeps going.
    retry_frozen_rungs: bool

    @classmethod
    def build(
        cls, mode: object | None, unsafe_entries: list[dict], base_curve: list[dict]
    ) -> TuningPolicy:
        tuning_mode = normalize_auto_uv_tuning_mode(mode)
        aggressive = tuning_mode == AUTO_UV_TUNING_MODE_AGGRESSIVE
        return cls(
            mode=tuning_mode,
            edge_margin=None if aggressive else EdgeMargin(unsafe_entries, base_curve),
            floor_caution=not aggressive,
            performance_voltage_headroom_bins=PERFORMANCE_VOLTAGE_HEADROOM_BINS[tuning_mode],
            retry_frozen_rungs=aggressive,
        )

    def with_entries(self, unsafe_entries: list[dict]) -> TuningPolicy:
        """Bind the edge margin to the list a scan accumulates freezes into."""
        if self.edge_margin is None:
            return self
        return replace(
            self,
            edge_margin=EdgeMargin(
                unsafe_entries, self.edge_margin.base_curve,
                margin_bins=self.edge_margin.margin_bins,
            ),
        )

    def describe(self) -> str:
        parts = [f"tuning mode: {self.mode}"]
        if self.edge_margin is None:
            parts.append("no predicted-edge margin, no floor caution")
        else:
            margin = self.edge_margin.describe()
            parts.append(margin or "no freezes on record")
        parts.append(
            f"performance may add {self.performance_voltage_headroom_bins} voltage bins"
            + (" and retry rungs that froze" if self.retry_frozen_rungs else
               ", one reboot per climb at most")
        )
        return "; ".join(parts)
