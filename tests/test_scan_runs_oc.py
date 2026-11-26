"""The runs table's Auto-OC column, as ScanRuns computes it
(auto_uv/session/scan_runs.py). Ported from the classic RunsTable tests."""

from __future__ import annotations

from auto_uv.session.scan_runs import ScanRuns, format_oc_progress


def oc_text(runs: ScanRuns, row: int = 0) -> str:
    return format_oc_progress(runs.rows[row].oc)


def test_a_measured_baseline_gives_a_signed_oc_delta() -> None:
    runs = ScanRuns()
    runs.probe_result({"stage": "candidate", "voltage_mv": 885, "clock_mhz": 2880,
                       "base_avg_core_clock_mhz": 2730, "decision": "pass"})
    runs.probe_result({"stage": "candidate", "voltage_mv": 875, "clock_mhz": 2600,
                       "measured_baseline_clock_mhz": 2730, "decision": "fail"})
    assert oc_text(runs, 0) == "+150 MHz"
    assert oc_text(runs, 1) == "-130 MHz"


def test_a_run_without_a_measured_baseline_leaves_oc_blank() -> None:
    runs = ScanRuns()
    runs.probe_result({"stage": "candidate", "voltage_mv": 900, "clock_mhz": 2600,
                       "decision": "pass"})
    assert oc_text(runs) == ""


def test_the_auto_oc_budget_alone_is_not_an_oc_delta() -> None:
    runs = ScanRuns()
    runs.probe_started({"stage": "candidate", "voltage_mv": 885, "clock_mhz": 2880,
                        "elapsed_s": 0, "target_duration_s": 32})
    runs.candidate_curve({"stage": "candidate", "voltage_mv": 885, "clock_mhz": 2880,
                          "auto_oc": True, "auto_oc_applied_mhz": 7, "auto_oc_limit_mhz": 107})
    assert oc_text(runs) == ""
    runs.probe_result({"stage": "candidate", "voltage_mv": 885, "clock_mhz": 2880,
                       "base_avg_core_clock_mhz": 2730, "decision": "pass"})
    assert oc_text(runs) == "+150 MHz"


def test_a_candidate_curve_fills_oc_into_its_running_row() -> None:
    runs = ScanRuns()
    runs.probe_started({"stage": "candidate", "voltage_mv": 910, "clock_mhz": 2890,
                        "elapsed_s": 0, "target_duration_s": 32})
    assert oc_text(runs) == ""
    runs.candidate_curve({"stage": "candidate", "voltage_mv": 910, "clock_mhz": 2890,
                          "auto_oc": True, "auto_oc_baseline_clock_mhz": 2730,
                          "points": [{"voltage_mv": 910, "base_mhz": 2400, "clock_mhz": 2890}]})
    assert oc_text(runs) == "+160 MHz"


def test_an_auto_oc_curve_before_its_probe_keeps_one_row_with_the_oc_delta() -> None:
    runs = ScanRuns()
    runs.candidate_curve({"stage": "candidate", "label": "performance-oc 3/10",
                          "voltage_mv": 950, "clock_mhz": 2745, "auto_oc": True,
                          "auto_oc_baseline_clock_mhz": 2565, "auto_oc_applied_mhz": 180,
                          "auto_oc_limit_mhz": 180,
                          "points": [{"voltage_mv": 950, "base_mhz": 2565, "clock_mhz": 2745,
                                      "offset_mhz": 180}]})
    runs.probe_started({"stage": "candidate", "label": "performance-oc 3/10",
                        "voltage_mv": 950, "clock_mhz": 2745, "elapsed_s": 0,
                        "target_duration_s": 32})
    runs.probe_result({"stage": "candidate", "label": "performance-oc 3/10",
                       "voltage_mv": 950, "clock_mhz": 2745, "measured_clock_mhz": 2733.5,
                       "decision": "pass"})
    assert len(runs.rows) == 1
    assert runs.rows[0].payload["clock_mhz"] == 2745
    assert oc_text(runs) == "+180 MHz"
