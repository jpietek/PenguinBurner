from __future__ import annotations

from types import SimpleNamespace

import pytest

from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.domain.types import AutoUvCriticalProbeError, AutoUvError, VfCurveCandidate
from auto_uv.probes import runner as runner_module
from auto_uv.probes.runner import AutoUvProbeRunner
from auto_uv.run import baseline_probe
from auto_uv_test_data import base_curve, stable_probe_result
from stability.q2rtx.models import Q2RTXStabilityConfig


@pytest.fixture
def baseline(monkeypatch):
    curve = base_curve(800, 1025, 25, 2100, 30)
    state = SimpleNamespace(
        curve=curve, calls=[], history=[], logs=[], ceilings=[], unsafe=[],
        results=[],
        candidate=VfCurveCandidate(
            label="baseline-loaded-flattened-curve", voltage_mv=950, target_mhz=2400,
            flattened_plan=build_flattened_plan(
                curve, lock_clock_mhz=2400, candidate_voltage_mv=950, tail_rise_bins=2
            ),
        ),
    )

    def probe(**kwargs):
        state.calls.append(kwargs)
        result = state.results.pop(0) if state.results else SimpleNamespace(
            **stable_probe_result(clock_mhz=kwargs["lock_clock_mhz"])
        )
        if isinstance(result, BaseException):
            raise result
        return SimpleNamespace(
            candidate_voltage_mv=kwargs["candidate_voltage_mv"],
            lock_clock_mhz=kwargs["lock_clock_mhz"],
            avg_core_clock_mhz=float(kwargs["lock_clock_mhz"]), avg_voltage_mv=950.0,
        ), result

    monkeypatch.setattr(runner_module, "probe_voltage_candidate", probe)
    monkeypatch.setattr(baseline_probe, "load_unsafe_voltage_blacklist", lambda: state.unsafe)
    state.runner = AutoUvProbeRunner(
        reader=object(), live_voltage_reader=object(), q2rtx_config=Q2RTXStabilityConfig(),
        runtime_default_plan=curve, power_limit_w=220, start_voltage_mv=950,
        baseline_clock_mhz=2408.0, short_probe_base_duration_s=10, log=state.logs.append,
    )
    return state


def run_baseline(state):
    return baseline_probe.probe_loaded_baseline_with_backoff(
        state.curve, candidate=state.candidate, runner=state.runner,
        clock_ceiling=SimpleNamespace(retarget=lambda **kwargs: state.ceilings.append(kwargs)),
        tail_rise_bins=2, probe_history=state.history, log=state.logs.append,
    )


def failed_xid():
    return SimpleNamespace(
        success=False, reason="fatal-q2rtx-output", fatal_output_matches=["device lost"],
        xid_messages=["Xid 109"], output_tail=["device lost"],
    )


def test_passing_baseline_is_returned_unchanged(baseline):
    candidate, outcome = run_baseline(baseline)

    assert candidate is baseline.candidate
    assert outcome.decision.passed
    assert len(baseline.calls) == len(baseline.history) == 1


def test_failed_flattened_baseline_retreats_and_keeps_the_tested_shape(baseline):
    baseline.results = [failed_xid()]

    candidate, outcome = run_baseline(baseline)

    assert outcome.decision.passed
    assert candidate.voltage_mv == 950
    assert candidate.target_mhz == 2385
    assert [call["lock_clock_mhz"] for call in baseline.calls] == [2400, 2385]
    assert all(call["phase_label"] == "baseline" for call in baseline.calls)
    assert all(call["q2rtx_config"].companion_command is None for call in baseline.calls)
    assert baseline.calls[-1]["candidate_plan"] is candidate.flattened_plan
    assert baseline.ceilings[-1]["lock_clock_mhz"] == 2385
    assert len(baseline.history) == 2
    assert baseline.runner.baseline_clock_mhz == 2408.0
    assert any("2400MHz -> 2385MHz" in line for line in baseline.logs)
    assert {point["voltage_mv"]: point["target_mhz"] for point in candidate.flattened_plan}[1000] == 2415


@pytest.mark.parametrize("after_failure", [False, True])
def test_baseline_jumps_below_cached_clock_band_before_probing(baseline, monkeypatch, after_failure):
    baseline.unsafe = [{
        "candidate_voltage_mv": 950, "lock_clock_mhz": 2400,
        "blocked_lock_clock_mhz": [2370, 2385, 2400], "reason": "nvidia-xid-detected",
    }]
    if after_failure:
        baseline.results = [failed_xid()]
        monkeypatch.setattr(
            baseline_probe, "load_unsafe_voltage_blacklist",
            lambda: baseline.unsafe if baseline.calls else [],
        )

    candidate, outcome = run_baseline(baseline)

    assert outcome.decision.passed
    assert candidate.target_mhz == 2355
    assert [call["lock_clock_mhz"] for call in baseline.calls] == ([2400, 2355] if after_failure else [2355])


def test_baseline_does_not_probe_a_legacy_unsafe_voltage(baseline):
    baseline.unsafe = [{"candidate_voltage_mv": 950, "reason": "previous-run-abruptly-ended"}]

    with pytest.raises(AutoUvError, match="cached unsafe voltage"):
        run_baseline(baseline)

    assert baseline.calls == []


def test_baseline_retreat_has_finite_ten_probe_budget(baseline):
    baseline.results = [failed_xid() for _ in range(10)]

    with pytest.raises(AutoUvError, match="failed after clock backoff"):
        run_baseline(baseline)

    assert [call["lock_clock_mhz"] for call in baseline.calls] == list(range(2400, 2250, -15))


@pytest.mark.parametrize("stop", [False, True])
def test_baseline_does_not_retreat_after_user_stop_or_invalid_metrics(baseline, stop):
    baseline.results = [KeyboardInterrupt() if stop else SimpleNamespace(success=True)]

    with pytest.raises(KeyboardInterrupt if stop else AutoUvCriticalProbeError):
        run_baseline(baseline)

    assert len(baseline.calls) == 1
