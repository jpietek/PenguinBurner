"""The Auto UV V/F plot: round tick steps, and a selection that also dims the
live candidate."""

from __future__ import annotations

import math

from ui.design.style import DIM_ALPHA, DemoCurvePlot


def nice_step(span, count):
    """A 1/2/5 x 10^n step so ticks are round numbers."""
    raw = abs(span) / max(1, count)
    power = 10 ** math.floor(math.log10(raw or 1))
    unit = raw / power
    return (1 if unit <= 1 else 2 if unit <= 2 else 5 if unit <= 5 else 10) * power


class ScanPlot(DemoCurvePlot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not hasattr(self, "plot"):
            return
        # y major = nice_step(span, 8) with halves; x major follows the width
        # (one label per ~110 px) with fifths.
        self.plot.getAxis("left").tickSpacing = lambda lo, hi, size: [
            (major := nice_step(hi - lo, 8), 0),
            (major / 2, 0),
        ]
        self.plot.getAxis("bottom").tickSpacing = lambda lo, hi, size: [
            (major := nice_step(hi - lo, max(3, round(size / 110))), 0),
            (major / 5, 0),
        ]

    def _apply_curve_highlights(self):
        super()._apply_curve_highlights()
        if self.candidate_curve is not None:
            self.candidate_curve.setOpacity(DIM_ALPHA if self._emphasis() else 1.0)

    def fit_to(self, curves):
        """Data x range; y padded 6% and rounded out to 100 MHz."""
        xs = [v for points in curves for v, _ in points]
        ys = [m for points in curves for _, m in points]
        if not xs or not ys:
            return
        pad = (max(ys) - min(ys)) * 0.06
        self.plot.setRange(
            xRange=(min(xs), max(xs)),
            yRange=(
                math.floor((min(ys) - pad) / 100) * 100,
                math.ceil((max(ys) + pad) / 100) * 100,
            ),
            padding=0,
        )
