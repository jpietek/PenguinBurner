"""Live-widget coverage for ui/components/curve_plot.py.

Instantiates CurvePlot with real pyqtgraph (offscreen) and drives its public
data API, plus the pg=None placeholder branch.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from ui.components.curve_plot import CurvePlot
from ui.qt import import_qt


def _make_plot(QtWidgets, pg, **kwargs):
    return CurvePlot(
        QtWidgets=QtWidgets,
        pg=pg,
        x_label="Voltage",
        x_units="mV",
        y_label="Clock",
        y_units="MHz",
        **kwargs,
    )


def test_curve_plot_placeholder_without_pyqtgraph(qapp) -> None:
    _qtcore, _qtgui, QtWidgets, _pg = import_qt()
    plot = _make_plot(QtWidgets, None)
    # No pyqtgraph -> a read-only text placeholder, and data calls are no-ops.
    assert plot.widget is not None
    plot.set_source_points([(800, 2000)])
    plot.set_candidate_points([(850, 2200)])
    plot.clear()


def test_curve_plot_full_data_api(qapp) -> None:
    _qtcore, _qtgui, QtWidgets, pg = import_qt()
    if pg is None:
        pytest.skip("pyqtgraph not available")

    plot = _make_plot(QtWidgets, pg, x_range=(800, 1000), y_range=(2000, 2800))
    plot.enable_point_selection(True)

    plot.set_source_points([(800, 2000), (900, 2400)])
    plot.set_source_points([(820, 2050), (910, 2450)])
    plot.add_comparison_points(
        [(820, 2100), (900, 2400)],
        name="Other",
        color="#ff8800",
        z_value=5,
    )
    assert plot.comparison_curves[-1].zValue() == 5

    # Two candidate sets with different ids -> the first is pushed to previous.
    plot.set_candidate_points([(850, 2200), (900, 2400)], curve_id="c1")
    plot.set_candidate_points([(860, 2250), (910, 2450)], curve_id="c2")

    plot.set_highlighted_curve("c1")
    plot.set_highlighted_curve(None)

    plot.set_target_point(900, 2400)
    plot.set_crosshair_point(900, 2400)
    plot.set_selected_point(880, 2350)
    # Bad crosshair input takes the failure branch without raising.
    plot.set_crosshair_point("x", None)

    plot.set_probe_marker({"candidate_voltage_mv": 900, "lock_clock_mhz": 2400})
    plot.set_live_load_marker(
        {"loaded_median_voltage_mv": 890, "loaded_median_core_clock_mhz": 2380}
    )
    plot.set_load_markers({"candidate_voltage_mv": 900, "lock_clock_mhz": 2400})

    plot.clear_load_markers()
    plot.clear()
    # After clear the candidate/source caches are reset.
    assert plot._candidate_points == []
    assert plot._source_points == []


def test_curve_plot_remembers_previous_curves_with_cap(qapp) -> None:
    _qtcore, _qtgui, QtWidgets, pg = import_qt()
    if pg is None:
        pytest.skip("pyqtgraph not available")

    plot = _make_plot(QtWidgets, pg)
    # Push many distinct candidate curves; previous-curve history caps at 12.
    for i in range(20):
        plot.set_candidate_points([(800 + i, 2000 + i)], curve_id=f"c{i}")
    assert len(plot.previous_curves) <= 12
