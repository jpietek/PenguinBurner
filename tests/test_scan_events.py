"""Coverage for auto_uv/session/scan_events.py: pure payload->display transforms (no Qt)."""

from __future__ import annotations

from auto_uv.session.scan_events import (
    candidate_id_from_payload,
    event_base_points,
    event_points,
    fan_measurement_point,
    fan_points,
    probe_decision_label,
    probe_failure_label,
    probe_reason_tooltip,
    stage_title,
    status_value,
    top_status_text,
)


def test_event_points_reads_voltage_clock_pairs() -> None:
    payload = {"points": [{"voltage_mv": 900, "clock_mhz": 2500}, {"bad": 1}]}
    assert event_points(payload) == [(900.0, 2500.0)]


def test_event_base_points_falls_back_to_event_points() -> None:
    # No base_mhz -> falls back to the candidate clock points.
    payload = {"points": [{"voltage_mv": 900, "clock_mhz": 2500}]}
    assert event_base_points(payload) == [(900.0, 2500.0)]
    # base_mhz present -> used directly.
    payload2 = {"points": [{"voltage_mv": 900, "base_mhz": 2400, "clock_mhz": 2500}]}
    assert event_base_points(payload2) == [(900.0, 2400.0)]


def test_fan_points_accepts_alternate_keys() -> None:
    payload = {"curve_points": [{"temp_c": 40, "fan_speed_pct": 30}]}
    assert fan_points(payload) == [(40.0, 30.0)]


def test_fan_measurement_point_and_none() -> None:
    assert fan_measurement_point({"temp_c": 50, "fan_pct": 45}) == (50.0, 45.0)
    assert fan_measurement_point({"temp_c": 50}) is None


def test_candidate_id_uses_explicit_then_derived_then_empty() -> None:
    assert candidate_id_from_payload({"candidate_id": "c1"}) == "c1"
    assert (
        candidate_id_from_payload({"candidate_voltage_mv": 900, "lock_clock_mhz": 2500})
        == "900mv-2500mhz"
    )
    assert candidate_id_from_payload({"candidate_voltage_mv": 900}) == ""


def test_stage_title_variants() -> None:
    assert stage_title("stock-baseline") == "Baseline"
    assert stage_title("lower_voltage_candidate") == "Undervolting Candidates Sweep"
    assert stage_title("") == "Probe"
    assert stage_title("final-verify") == "Final Verify"


def test_status_value_formats_integers_and_decimals() -> None:
    assert status_value(2500.0) == "2500"  # near-integer collapses to int
    assert status_value(12.5, precision=2) == "12.50"
    assert status_value(None) == ""
    assert status_value("not-a-number") == ""


def test_top_status_text_rounds_embedded_decimals() -> None:
    assert top_status_text("clock 2500.12345 MHz") == "clock 2500.12 MHz"


def test_probe_decision_label_branches() -> None:
    assert probe_decision_label({"decision": "accepted"}) == "Pass"
    assert probe_decision_label({"decision": "running"}) == "Running"
    assert probe_decision_label({"decision": "stopping"}) == "Stopping"
    assert probe_decision_label({"decision": "stopped"}) == "Failed"
    assert probe_decision_label({"decision": "queued"}) == "Failed"
    assert probe_decision_label({"decision": "fail", "failure_kind": "timed-out"}) == "Failed"
    assert probe_decision_label({}) == ""


def test_probe_failure_label_covers_each_kind() -> None:
    cases = [
        ({"fatal_output_matches": ["VK_ERROR_DEVICE_LOST"]}, "Vulkan device lost"),
        ({"failure_kind": "low-clock"}, "Clock too low"),
        ({"reason": "core clock under floor"}, "Clock too low"),
        ({"failure_kind": "fps-regression", "reason": "single-run dip"}, "Single run FPS low"),
        ({"failure_kind": "fps-regression"}, "Average FPS low"),
        ({"failure_kind": "frame-count-regression"}, "Frame count changed"),
        ({"failure_kind": "load-lost"}, "GPU load too low"),
        ({"failure_kind": "metrics-missing"}, "Metrics invalid"),
        ({"failure_kind": "cuda-failed"}, "CUDA failed"),
        ({"failure_kind": "nvidia-xid"}, "Nvidia Xid fail"),
        ({"failure_kind": "fatal-output"}, "Fatal output"),
        ({"failure_kind": "timed-out"}, "Timed out"),
        ({"failure_kind": "user-stop"}, "Stopped"),
        ({"failure_kind": "q2rtx-failed"}, "Q2RTX failed"),
        ({"failure_kind": "something-else"}, "Failed"),
    ]
    for payload, expected in cases:
        assert probe_failure_label(payload) == expected, payload


def test_probe_reason_tooltip_joins_present_fields() -> None:
    tooltip = probe_reason_tooltip(
        {
            "reason": "clock too low",
            "failure_kind": "low-clock",
            "failure_severity": "fatal",
            "fatal_output_matches": ["panic"],
            "log_path": "/tmp/x.log",
        }
    )
    assert tooltip.splitlines() == [
        "clock too low",
        "kind: low-clock",
        "severity: fatal",
        "fatal output: panic",
        "log: /tmp/x.log",
    ]
    assert probe_reason_tooltip({}) == ""
