from __future__ import annotations

from auto_uv.domain.types import (
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.probes.events import (
    emit_voltage_probe_finished,
    emit_voltage_probe_started,
)
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from auto_uv_test_data import base_curve


def test_fpsw_event_precision_preserves_distinct_efficiency_candidates() -> None:
    from auto_uv.probes.event_payload import probe_summary_event_payload

    values = [
        probe_summary_event_payload({"candidate_voltage_mv": 850, "lock_clock_mhz": 2700, "efficiency_fps_per_w": value}, stage="candidate", decision="pass", reason="ok")["efficiency_fps_per_w"]
        for value in (0.2417602874, 0.2407566677, 0.2362143808)
    ]
    assert values == [0.2418, 0.2408, 0.2362]


def test_ui_probe_start_events_create_curve_and_table_row() -> None:
    events: list[tuple[str, dict]] = []
    candidate = VfCurveCandidate(
        label="lower-voltage 950mV",
        voltage_mv=950,
        target_mhz=2400,
        flattened_plan=base_curve(900, 1000, 25, 2200, 20),
    )

    emit_voltage_probe_started(
        lambda name, payload: events.append((name, payload)),
        candidate,
        stage="candidate",
        max_clock_drop_pct=10.0,
    )

    assert [name for name, _payload in events] == ["candidate_curve", "probe_start"]
    assert events[1][1] == {
        "stage": "candidate",
        "voltage_mv": 950,
        "clock_mhz": 2400,
        "label": "lower-voltage 950mV",
    }
    assert len(events[0][1]["points"]) == 4


def test_ui_probe_start_events_include_auto_oc_progress_metadata() -> None:
    events: list[tuple[str, dict]] = []
    candidate = VfCurveCandidate(
        label="performance-oc 1/2",
        voltage_mv=935,
        target_mhz=2715,
        flattened_plan=base_curve(900, 1000, 25, 2200, 20),
        metadata={
            "auto_oc": True,
            "auto_oc_baseline_clock_mhz": 2670,
            "auto_oc_applied_mhz": 45,
            "auto_oc_limit_mhz": 75,
        },
    )

    emit_voltage_probe_started(
        lambda name, payload: events.append((name, payload)),
        candidate,
        stage="candidate",
    )

    assert events[0][1]["auto_oc"] is True
    assert events[0][1]["auto_oc_baseline_clock_mhz"] == 2670
    assert events[0][1]["auto_oc_applied_mhz"] == 45
    assert events[0][1]["auto_oc_limit_mhz"] == 75
    assert events[1][1]["auto_oc"] is True
    assert events[1][1]["auto_oc_baseline_clock_mhz"] == 2670
    assert events[1][1]["auto_oc_applied_mhz"] == 45
    assert events[1][1]["auto_oc_limit_mhz"] == 75


def test_ui_probe_result_payload_contains_measured_table_values() -> None:
    events: list[tuple[str, dict]] = []
    candidate = VfCurveCandidate(
        label="lower-voltage",
        voltage_mv=950,
        target_mhz=2400,
        flattened_plan=[],
    )
    outcome = VoltageProbeOutcome(
        decision=StableRunDecision(
            passed=False,
            failure_kind=FailureKind.LOW_CLOCK,
            severity=FailureSeverity.RECOVERABLE,
            reason="clock floor miss",
        ),
        measured_core_clock_mhz=2325.4,
        measured_voltage_mv=947.8,
    )

    emit_voltage_probe_finished(
        lambda name, payload: events.append((name, payload)),
        candidate,
        outcome,
        stage="candidate",
    )

    assert events == [
        (
            "probe_result",
            {
                "stage": "candidate",
                "voltage_mv": 950,
                "clock_mhz": 2400,
                "label": "lower-voltage",
                "measured_clock_mhz": 2325.4,
                "avg_voltage_mv": 947.8,
                "decision": "fail",
                "failure_evidence": {},
                "failure_kind": "low-clock",
                "failure_severity": "recoverable",
                "fatal_output_matches": [],
                "reason": "clock floor miss",
            },
        )
    ]
