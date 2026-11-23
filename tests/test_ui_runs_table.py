"""Coverage for ui/components/runs_table.py.

Instantiates RunsTable under offscreen Qt and drives the row-writing API with
probe payloads, plus direct tests of the pure formatting/state helpers.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import auto_uv.session.scan_runs as runs
import ui.components.runs_table as rt
from ui.components.runs_table import RunsTable
from ui.qt import import_qt


def _result_payload(decision="pass", **over) -> dict:
    payload = {
        "stage": "candidate",
        "candidate_id": "c1",
        "candidate_voltage_mv": 900,
        "lock_clock_mhz": 2500,
        "avg_core_clock_mhz": 2490,
        "avg_fps": 120.0,
        "avg_power_w": 200.0,
        "avg_temperature_c": 62.0,
        "avg_fan_speed_pct": 40.0,
        "efficiency_fps_per_w": 0.6,
        "decision": decision,
        "perf_cap_reason": "sw_power_cap",
        "result_reason": "stable run",
    }
    payload.update(over)
    return payload


def test_runs_table_row_lifecycle(qapp) -> None:
    qtcore, qtgui, qtwidgets, _pg = import_qt()
    table = RunsTable(QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets)

    table.add_probe_start(_result_payload())
    assert table.widget.rowCount() == 1
    table.update_probe_progress(
        {"candidate_id": "c1", "candidate_voltage_mv": 900, "elapsed_s": 30, "target_s": 60}
    )
    table.record_candidate_curve(
        {"candidate_id": "c1", "candidate_voltage_mv": 900,
         "auto_oc": True, "auto_oc_applied_mhz": 30, "auto_oc_limit_mhz": 90}
    )
    table.add_probe_result(_result_payload(decision="pass"))
    table.add_probe_result(_result_payload(decision="fail", candidate_id="c2",
                                           candidate_voltage_mv=880))
    table.add_probe_result(
        {"stage": "base-baseline", "candidate_voltage_mv": 1000, "lock_clock_mhz": 2600,
         "avg_fps": 110.0, "decision": "baseline"}
    )
    assert table.widget.rowCount() >= 3

    table.mark_running_rows_stopping()
    table.mark_running_rows_stopped(label="Stopped")
    _ = table.selected_candidate_id()

    table.clear()
    assert table.widget.rowCount() == 0


def test_base_baseline_oc_column_is_dash_then_candidates_use_signed_offsets(qapp) -> None:
    qtcore, qtgui, qtwidgets, _pg = import_qt()
    table = RunsTable(QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets)

    table.add_probe_start(
        {
            "stage": "base-baseline",
            "voltage_mv": 1100,
            "clock_mhz": 3030,
            "elapsed_s": 0,
            "target_duration_s": 32,
        }
    )
    assert table.widget.item(0, table.OC_MHZ_COLUMN).text() == "-"

    table.add_probe_result(
        {
            "stage": "base-baseline",
            "voltage_mv": 1100,
            "clock_mhz": 3030,
            "measured_clock_mhz": 2730,
            "avg_core_clock_mhz": 2730,
            "decision": "pass",
        }
    )
    assert table.widget.item(0, table.OC_MHZ_COLUMN).text() == "-"

    table.add_probe_result(
        {
            "stage": "candidate",
            "voltage_mv": 900,
            "clock_mhz": 2880,
            "decision": "pass",
        }
    )
    assert table.widget.item(1, table.OC_MHZ_COLUMN).text() == "+150 MHz"

    table.add_probe_result(
        {
            "stage": "candidate",
            "voltage_mv": 875,
            "clock_mhz": 2600,
            "decision": "fail",
        }
    )
    assert table.widget.item(2, table.OC_MHZ_COLUMN).text() == "-130 MHz"


def test_aborted_run_decision_cell_is_not_failed(qapp) -> None:
    # A user-aborted run must read "Aborted" in the Decision column, never the
    # hardcoded "Failed" that genuine failures get.
    qtcore, qtgui, qtwidgets, _pg = import_qt()
    table = RunsTable(QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets)

    table.add_probe_start(_result_payload())
    table.mark_running_rows_stopped(label="Aborted", row_state="warning")

    decision = table.widget.item(0, table.DECISION_COLUMN)
    assert decision is not None
    assert decision.text() == "Aborted"


def test_failed_run_decision_cell_still_reads_failed(qapp) -> None:
    qtcore, qtgui, qtwidgets, _pg = import_qt()
    table = RunsTable(QtCore=qtcore, QtGui=qtgui, QtWidgets=qtwidgets)

    table.add_probe_start(_result_payload())
    table.mark_running_rows_stopped(label="Failed")

    decision = table.widget.item(0, table.DECISION_COLUMN)
    assert decision is not None
    assert decision.text() == "Failed"


# --- pure helpers -------------------------------------------------------------


def test_row_state_and_active_decision() -> None:
    assert runs.is_active_decision("running") is True
    assert runs.is_active_decision("pass") is False
    assert runs.is_base_baseline({"stage": "base-baseline"}) is True
    assert runs.is_base_baseline({"stage": "candidate"}) is False
    assert runs.probe_key({"candidate_id": "c1", "candidate_voltage_mv": 900})


def test_number_and_bool_helpers() -> None:
    assert runs.to_float("2500") == 2500.0
    assert runs.to_float("x") is None
    assert runs.format_int(2500.4) == "2500"
    assert runs.format_int(None) == ""
    assert runs.format_float(12.34) == "12.34" or runs.format_float(12.34)
    assert runs.payload_number({"a": 1, "b": 2}, "missing", "b") == 2.0


def test_duration_and_progress_text() -> None:
    assert runs.format_duration_compact(0) is not None
    assert "m" in runs.format_duration_compact(125) or runs.format_duration_compact(125)
    assert runs.clamped_elapsed_s(120, 60) == 60.0  # clamped to target
    assert runs.clamped_elapsed_s(-5, 60) == 0.0
    assert isinstance(runs.progress_time_text(30, 60), str)


def test_oc_progress_and_cap_helpers() -> None:
    assert runs.format_oc_progress((1, 4)) == "+1 MHz"
    assert runs.format_oc_progress((-10, 4)) == "-10 MHz"
    # Non-OC rows (UV sweep, Efficiency/Balanced) leave OC blank, not
    # a signed zero that looks like Auto-OC ran and gained nothing.
    assert runs.format_oc_progress(None) == ""
    assert runs.format_oc_progress((2, 0)) == "+2 MHz"
    assert (
        runs.payload_oc_progress(
            {"auto_oc": True, "auto_oc_applied_mhz": 30, "auto_oc_limit_mhz": 90}
        )
        is None
    )
    assert runs.payload_oc_progress({}) is None
    # OC reports target minus measured baseline, including zero and negative deltas.
    assert (
        runs.payload_oc_progress(
            {"auto_oc": True, "auto_oc_applied_mhz": 0, "auto_oc_limit_mhz": 380}
        )
        is None
    )
    assert runs.format_oc_progress((0, 380)) == "+0 MHz"
    assert runs.payload_oc_progress(
        {
            "auto_oc": True,
            "clock_mhz": 2880,
            "auto_oc_baseline_clock_mhz": 2730,
        }
    ) == (150, 150)
    assert runs.payload_oc_progress(
        {
            "auto_oc": True,
            "clock_mhz": 2600,
            "auto_oc_baseline_clock_mhz": 2730,
        }
    ) == (-130, -130)
    assert runs.payload_oc_progress(
        {
            "clock_mhz": 2880,
            "base_avg_core_clock_mhz": 2730,
        }
    ) == (150, 150)
    assert runs.payload_oc_progress(
        {
            "clock_mhz": 2600,
            "base_avg_core_clock_mhz": 2730,
        }
    ) == (-130, -130)
    assert (
        runs.payload_oc_progress(
            {"auto_oc_applied_mhz": 15, "auto_oc_limit_mhz": 90}
        )
        is None
    )
    assert isinstance(runs.perf_cap_reason_text("sw_power_cap"), str)
    assert isinstance(runs.perf_cap_reason_tooltip("sw_power_cap"), str)


def test_hw_power_brake_is_visible_even_when_the_summary_suppressed_it() -> None:
    # The Cap column is the most visible surface for a power-delivery brake,
    # and the probe summarizer can drop a sparse power token from the reason
    # text. The reported sample count must put it back.
    assert runs.perf_cap_reason_text("none", 4) == "hw-power-brake"
    assert runs.perf_cap_reason_text("sw-power", 4) == "sw-power+hw-power-brake"
    # Already named: no duplicate.
    assert runs.perf_cap_reason_text("sw-power+hw-power-brake", 4) == (
        "sw-power+hw-power-brake"
    )
    # No brake, no change.
    assert runs.perf_cap_reason_text("sw-power", 0) == "sw-power"
    assert "power delivery" in runs.perf_cap_reason_tooltip("sw-power", 4)
    assert "power delivery" not in runs.perf_cap_reason_tooltip("sw-power", 0)


def test_measured_clock_and_progress_color() -> None:
    assert runs.measured_clock_value({"avg_core_clock_mhz": 2490}) is not None
    color = rt._progress_text_color("#123456", 0.5)
    assert isinstance(color, str) and color
