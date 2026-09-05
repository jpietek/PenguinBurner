"""Translate one Auto-UV voltage probe into the JSON events the UI table expects.

The scan emits these events around Q2RTX/CUDA probes so the UI can create a row,
stream live telemetry into it, then replace it with the final measured result.
"""

from __future__ import annotations

from auto_uv.domain.types import VfCurveCandidate
from auto_uv.domain.events import AutoUvEventCallback, emit_auto_uv_event
from .event_payload import probe_summary_event_payload, vf_curve_event_points
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome


def emit_voltage_probe_started(
    event_callback: AutoUvEventCallback | None,
    candidate: VfCurveCandidate,
    *,
    stage: str,
    target_duration_s: float | int | None = None,
) -> None:
    identity = voltage_probe_identity(
        candidate,
        stage=stage,
        target_duration_s=target_duration_s,
    )
    emit_auto_uv_event(
        event_callback,
        "candidate_curve",
        **identity,
        points=vf_curve_event_points(candidate.flattened_plan),
    )
    emit_auto_uv_event(event_callback, "probe_start", **identity)


def emit_voltage_probe_finished(
    event_callback: AutoUvEventCallback | None,
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    *,
    stage: str,
) -> None:
    emit_auto_uv_event(
        event_callback,
        "probe_result",
        **voltage_probe_result_payload(
            candidate,
            outcome,
            stage=stage,
        ),
    )


def voltage_probe_identity(
    candidate: VfCurveCandidate,
    *,
    stage: str,
    target_duration_s: float | int | None = None,
) -> dict:
    payload = {
        "stage": str(stage),
        "voltage_mv": int(candidate.voltage_mv),
        "clock_mhz": int(candidate.target_mhz),
        "label": str(candidate.label),
    }
    payload.update(_ui_candidate_metadata(candidate))
    rounded_target = _rounded(target_duration_s)
    if rounded_target is not None:
        payload["elapsed_s"] = 0.0
        payload["target_duration_s"] = rounded_target
    return payload


def voltage_probe_result_payload(
    candidate: VfCurveCandidate,
    outcome: VoltageProbeOutcome,
    *,
    stage: str,
) -> dict:
    decision = outcome.decision
    if outcome.raw_probe is not None:
        payload = probe_summary_event_payload(
            outcome.raw_probe,
            stage=str(stage),
            decision="pass" if bool(decision.passed) else "fail",
            reason=str(decision.reason or decision.failure_kind.value),
        )
        payload.update(probe_decision_payload(decision, result=outcome.raw_result))
        return payload
    payload = {
        **voltage_probe_identity(
            candidate,
            stage=stage,
        ),
        "measured_clock_mhz": _rounded(outcome.measured_core_clock_mhz),
        "avg_voltage_mv": _rounded(outcome.measured_voltage_mv),
        "decision": "pass" if bool(decision.passed) else "fail",
        "reason": str(decision.reason or decision.failure_kind.value),
    }
    payload.update(probe_decision_payload(decision, result=outcome.raw_result))
    return payload


def probe_decision_payload(decision, *, result=None) -> dict:
    return {
        "failure_kind": str(decision.failure_kind.value),
        "failure_severity": str(decision.severity.value),
        "failure_evidence": dict(decision.evidence or {}),
        "fatal_output_matches": list(getattr(result, "fatal_output_matches", []) or []),
    }


def _ui_candidate_metadata(candidate: VfCurveCandidate) -> dict:
    metadata = dict(candidate.metadata or {})
    return {
        key: metadata.get(key)
        for key in (
            "auto_oc",
            "auto_oc_step",
            "auto_oc_steps",
            "auto_oc_baseline_clock_mhz",
            "auto_oc_start_clock_mhz",
            "auto_oc_target_clock_mhz",
            "auto_oc_applied_mhz",
            "auto_oc_limit_mhz",
        )
        if metadata.get(key) is not None
    }


def _rounded(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)
