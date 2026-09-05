from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from auto_uv.auto_oc import search, target_search
from auto_uv.auto_oc.ladder import build_auto_oc_ladder
from auto_uv.curve.flattened_voltage_probe_curve import (
    build_flattened_voltage_probe_curve,
)
from auto_uv.domain.types import (
    AutoUvError,
    AutoUvProbeSummary,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
)
from auto_uv.final_verification.main_loop import final_probe_stability_decision
from auto_uv.main_loop import select_final_scan_candidate
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from auto_uv.probes.stability_decision import evaluate_stable_run, StabilityThresholds
from auto_uv.scan_mode.target_overrides import (
    TierTargetOverrides,
    custom_target_min_core_clock_pct,
    custom_tier_target,
    tier_target_overrides,
)
from stability.q2rtx.models import Q2RTXStabilityConfig
from auto_uv_test_data import (
    base_curve,
    rtx_5080_20260524_high_oc_base_curve,
    stable_probe_result,
)


def summary(candidate, *, measured=None, power=200.0, fps=100.0):
    clock = candidate.target_mhz if measured is None else measured
    return AutoUvProbeSummary(
        candidate_voltage_mv=candidate.voltage_mv,
        lock_clock_mhz=candidate.target_mhz,
        live_voltage_before_mv=candidate.voltage_mv,
        live_voltage_after_mv=candidate.voltage_mv,
        avg_voltage_mv=float(candidate.voltage_mv),
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=fps,
        min_fps=fps,
        max_fps=fps,
        avg_power_w=power,
        max_power_w=power,
        avg_temperature_c=60.0,
        max_temperature_c=60.0,
        avg_fan_speed_pct=40.0,
        max_fan_speed_pct=40.0,
        avg_core_clock_mhz=float(clock),
        efficiency_fps_per_w=fps / power,
        efficiency_mhz_per_w=clock / power,
        watts_per_mhz=power / clock,
        used_companion_load=True,
        result_reason="stable run",
        log_path=Path("/tmp/test-probe.log"),
        tested_plan=[dict(p) for p in candidate.flattened_plan],
    )


def harness(monkeypatch, *, start_clock=2800, fail_clock=None, loss=0, stop_at=None):
    monkeypatch.setattr(target_search, "load_unsafe_voltage_blacklist", lambda: [])
    monkeypatch.setattr(search, "load_unsafe_voltage_blacklist", lambda: [])
    curve = base_curve(850, 950, 5, 2100, 15)
    start = build_flattened_voltage_probe_curve(
        curve,
        candidate_voltage_mv=925,
        target_clock_mhz=start_clock,
        label="start",
        tail_rise_bins=2,
    )
    initial = summary(start)
    tried = []

    def probe(candidate, **kwargs):
        tried.append(candidate)
        stopped = len(tried) == stop_at
        passed = not stopped and candidate.target_mhz != fail_clock
        measured = candidate.target_mhz - loss
        record = summary(
            candidate,
            measured=measured,
            fps=100.0 * candidate.target_mhz / start_clock,
            power=200 + (candidate.target_mhz - start_clock) / 5,
        )
        kind = (
            FailureKind.NONE
            if passed
            else FailureKind.USER_STOP
            if stopped
            else FailureKind.LOW_CLOCK
        )
        history = kwargs["stable_history"]
        decision = (
            evaluate_stable_run(
                stable_probe_result(
                    fps=record.avg_fps, clock_mhz=measured, power_w=record.avg_power_w
                ),
                baseline_fps=history[0].avg_fps if history else None,
                baseline_power_w=history[0].avg_power_w if history else None,
                baseline_core_clock_mhz=None,
                power_limit_w=360,
                cuda_required=True,
                companion_result={"success": True},
                thresholds=StabilityThresholds(min_core_clock_pct=0),
            )
            if passed
            else StableRunDecision(False, kind, FailureSeverity.RECOVERABLE, "test")
        )
        return VoltageProbeOutcome(decision=decision, raw_probe=record)

    runner = SimpleNamespace(probe_candidate=probe, power_limit_w=360)
    kwargs = dict(
        base_curve=curve,
        start_candidate=start,
        start_probe=initial,
        runner=runner,
        gpu_name="RTX 5080",
        clock_ceiling=None,
        probe_history=[],
        stable_history=[initial],
        log=lambda _: None,
        tier="efficiency",
        overrides=TierTargetOverrides(900, 2400),
        tail_rise_bins=2,
        measured_baseline_clock_mhz=float(start_clock),
        min_core_clock_pct=custom_target_min_core_clock_pct(
            target_clock_mhz=2400,
            baseline_clock_mhz=float(start_clock),
            default_pct=90.0,
        ),
    )
    return kwargs, tried


def test_lower_target_keeps_sweep_voltage_and_measured_curve(
    monkeypatch,
):
    kwargs, tried = harness(monkeypatch)
    result = target_search.run_custom_tier_target_search(**kwargs)
    assert tried and all(c.voltage_mv == 925 for c in tried)
    assert tried[-1].target_mhz == 2400
    assert result.selected_candidate.voltage_mv == 925
    assert result.selected_candidate.target_mhz == 2400
    assert result.selected_probe.avg_fps < kwargs["start_probe"].avg_fps * 0.9
    assert result.selected_candidate.flattened_plan == result.selected_probe.tested_plan
    assert (
        max(p["target_mhz"] for p in result.selected_candidate.flattened_plan) == 2430
    )


def test_failed_lower_clock_never_enters_final_custom_selection(monkeypatch):
    kwargs, tried = harness(monkeypatch, fail_clock=2400)
    with pytest.raises(AutoUvError, match="No stable efficiency candidate"):
        target_search.run_custom_tier_target_search(**kwargs)
    assert tried[-1].target_mhz == 2400
    assert all(p.lock_clock_mhz != 2400 for p in kwargs["stable_history"])


def test_custom_clock_reduction_still_rejects_unexpected_clock_loss(monkeypatch):
    kwargs, _ = harness(monkeypatch, loss=700)
    with pytest.raises(AutoUvError, match="No stable efficiency candidate"):
        target_search.run_custom_tier_target_search(**kwargs)


def test_custom_target_search_stops_immediately_on_user_stop(monkeypatch):
    kwargs, tried = harness(monkeypatch, stop_at=2)
    with pytest.raises(AutoUvError, match="user-stop-requested"):
        target_search.run_custom_tier_target_search(**kwargs)
    assert len(tried) == 2


def test_lower_target_aborts_on_critical_gpu_failure(monkeypatch):
    kwargs, tried = harness(monkeypatch)
    probe = kwargs["runner"].probe_candidate

    def fail(candidate, **options):
        outcome = probe(candidate, **options)
        return VoltageProbeOutcome(
            decision=StableRunDecision(
                False, FailureKind.NVIDIA_XID, FailureSeverity.CRITICAL, "GPU Xid"
            ),
            raw_probe=outcome.raw_probe,
        )

    kwargs["runner"].probe_candidate = fail
    with pytest.raises(AutoUvError, match="critical probe failure: GPU Xid"):
        target_search.run_custom_tier_target_search(**kwargs)
    assert len(tried) == 1
    assert kwargs["stable_history"] == [kwargs["start_probe"]]


def test_efficiency_keeps_peak_efficiency_within_custom_upper_targets(monkeypatch):
    kwargs, tried = harness(monkeypatch, start_clock=2400)
    kwargs["overrides"] = TierTargetOverrides(925, 2700)
    result = target_search.run_custom_tier_target_search(**kwargs)
    assert tried and max(c.target_mhz for c in tried) == 2700
    assert result.selected_candidate.target_mhz == 2400


def test_final_selection_carries_custom_floor_and_filters_choice_history(monkeypatch):
    import auto_uv.main_loop as main

    kwargs, _ = harness(monkeypatch)
    captured = {}

    def choose(**options):
        captured.update(options)
        return (
            options["stable_plan"],
            options["stable_voltage_mv"],
            options["stable_lock_clock_mhz"],
            options["stable_probe"],
            60,
        )

    monkeypatch.setattr(main, "choose_final_verification_candidate", choose)
    result = select_final_scan_candidate(
        base_curve=kwargs["base_curve"],
        settings=SimpleNamespace(
            auto_uv_mode="efficiency",
            min_performance_core_clock_pct=90.0,
            short_probe_base_duration_s=10,
        ),
        runtime_options={
            "auto_uv_efficiency_target_clock_mhz": 2400,
            "auto_uv_efficiency_target_voltage_mv": 900,
            "auto_uv_require_final_choice": True,
        },
        stable_plan=kwargs["start_candidate"].flattened_plan,
        stable_voltage_mv=925,
        stable_lock_clock_mhz=2800,
        stable_probe=kwargs["start_probe"],
        stable_history=kwargs["stable_history"],
        runner=kwargs["runner"],
        gpu=SimpleNamespace(
            translated_gpu_policy={"gpu_name": "RTX 5080"}, clock_ceiling=None
        ),
        probe_history=[],
        log=lambda _: None,
        tail_rise_bins=2,
        measured_baseline_clock_mhz=2800.0,
        discovery_summary=kwargs["start_probe"],
        baseline_candidate=kwargs["start_candidate"],
        final_verification_duration_s=60,
        event_callback=None,
        run_performance_auto_oc=False,
        request_reason="test",
    )
    assert result.min_core_clock_pct == pytest.approx(90 * 2400 / 2800)
    assert all(
        p.lock_clock_mhz <= 2400 and p.candidate_voltage_mv <= 925
        for p in captured["stable_history"]
    )
    assert result.plan == result.probe.tested_plan
    # The final guard accepts the intentional reduction, but remains armed.
    for clock, expected in [(2400, True), (2000, False)]:
        decision = final_probe_stability_decision(
            stable_probe_result(clock_mhz=clock, fps=result.probe.avg_fps),
            stable_history=[kwargs["start_probe"]],
            power_limit_w=360,
            q2rtx_config=Q2RTXStabilityConfig(),
            min_performance_core_clock_pct=result.min_core_clock_pct,
            performance_reference=result.probe,
        )
        assert decision.passed is expected


@pytest.mark.parametrize(
    "tier,low,high",
    [("efficiency", 2380, 2800), ("balanced", 2800, 2950), ("performance", 2800, 3098)],
)
def test_target_ranges_are_validated_at_runtime(tier, low, high):
    key = f"auto_uv_{tier}_target_clock_mhz"
    for value in (low, high):
        assert (
            tier_target_overrides(
                {key: value}, gpu_name="RTX 5080", tier=tier
            ).clock_mhz
            == value
        )
    for value in (low - 1, high + 1):
        with pytest.raises(AutoUvError, match="clock target must be"):
            tier_target_overrides({key: value}, gpu_name="RTX 5080", tier=tier)


def test_automatic_targets_are_not_invented_for_unknown_gpus():
    overrides = tier_target_overrides({}, gpu_name="Unknown GPU", tier="efficiency")
    assert not overrides.specified
    assert (
        custom_tier_target(overrides, gpu_name="Unknown GPU", tier="efficiency") is None
    )


def test_sparse_voltage_bins_retain_the_complete_requested_endpoint():
    ladder = build_auto_oc_ladder(
        rtx_5080_20260524_high_oc_base_curve(),
        start_voltage_mv=910,
        start_clock_mhz=2686,
        endpoint_voltage_mv=925,
        endpoint_clock_mhz=2950,
    )
    assert (ladder[-1].voltage_mv, ladder[-1].target_mhz) == (925, 2950)
    assert len({s.voltage_mv for s in ladder}) == len(ladder)


def test_final_custom_fps_guard_rejects_regression_from_lowered_clock(monkeypatch):
    kwargs, _ = harness(monkeypatch)
    result = target_search.run_custom_tier_target_search(**kwargs)
    for fps_ratio, expected in [(1.0, True), (0.8, False)]:
        decision = final_probe_stability_decision(
            stable_probe_result(
                clock_mhz=2400, fps=result.selected_probe.avg_fps * fps_ratio
            ),
            stable_history=[kwargs["start_probe"]],
            power_limit_w=360,
            q2rtx_config=Q2RTXStabilityConfig(),
            min_performance_core_clock_pct=kwargs["min_core_clock_pct"],
            performance_reference=result.selected_probe,
        )
        assert decision.passed is expected


def test_final_recovery_stays_within_custom_voltage_and_clock_limits(monkeypatch):
    import auto_uv.main_loop as main

    kwargs, _ = harness(monkeypatch)
    candidate = kwargs["start_candidate"]
    safe = replace(kwargs["start_probe"], candidate_voltage_mv=925, lock_clock_mhz=2985)
    too_high_voltage = replace(safe, candidate_voltage_mv=950)
    too_high_clock = replace(safe, lock_clock_mhz=3105)
    offered = []
    monkeypatch.setattr(
        main,
        "choose_next_final_verification_candidate_after_failure",
        lambda **options: offered.append(options) or None,
    )
    failed = main.FinalScanCandidate(
        plan=candidate.flattened_plan,
        voltage_mv=900,
        lock_clock_mhz=2985,
        probe=safe,
        verification_duration_s=60,
        tail_rise_bins=2,
        auto_oc_metadata={
            "custom_target": True,
            "custom_target_clock_mhz": 3098,
            "custom_selection_voltage_limit_mv": 925,
        },
        min_core_clock_pct=90.0,
    )
    assert (
        main.choose_next_candidate_after_final_failure(
            base_curve=kwargs["base_curve"],
            settings=SimpleNamespace(
                auto_uv_mode="performance", min_performance_core_clock_pct=95
            ),
            stable_plan=failed.plan,
            stable_voltage_mv=failed.voltage_mv,
            stable_lock_clock_mhz=failed.lock_clock_mhz,
            stable_history=[safe, too_high_voltage, too_high_clock],
            discovery_summary=kwargs["start_probe"],
            baseline_candidate=candidate,
            final_verification_duration_s=60,
            short_probe_base_duration_s=10,
            failed_error=AutoUvError("final long verification failed: low FPS"),
            failed_selection=failed,
            run_profile_tier="performance",
            log=lambda _: None,
            event_callback=None,
            tail_rise_bins=2,
        )
        is None
    )
    assert offered[0]["stable_history"] == [safe]
    assert [
        (r["candidate_voltage_mv"], r["lock_clock_mhz"])
        for r in offered[0]["candidate_records"]
    ] == [(925, 2985)]
