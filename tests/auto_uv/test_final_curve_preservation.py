from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace

import pytest

from auto_uv import performance_uv_loop
from auto_uv.auto_oc.search import AutoOcSearchResult
from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.domain.types import (
    AutoUvProbeSummary,
    FailureKind,
    FailureSeverity,
    StableRunDecision,
    VfCurveCandidate,
)
from auto_uv.final_verification import main_loop as final_loop
from auto_uv.main_loop import select_final_scan_candidate
from auto_uv.run.voltage_sweep_state import VoltageProbeOutcome
from ui.features.auto_uv import candidate_choice
from ui.features.auto_uv.candidate_choice import candidate_record_from_probe
from stability.q2rtx.models import Q2RTXStabilityConfig
from auto_uv_test_data import rtx_5080_20260524_high_oc_base_curve, stable_probe_result


def _summary(voltage: int, clock: int) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=voltage,
        lock_clock_mhz=clock,
        live_voltage_before_mv=voltage,
        live_voltage_after_mv=voltage,
        avg_voltage_mv=float(voltage),
        frames_per_run=1000,
        avg_seconds_per_run=15.0,
        avg_fps=60.0,
        min_fps=55.0,
        max_fps=65.0,
        avg_power_w=200.0,
        max_power_w=210.0,
        avg_temperature_c=60.0,
        max_temperature_c=61.0,
        avg_fan_speed_pct=40.0,
        max_fan_speed_pct=40.0,
        avg_core_clock_mhz=float(clock),
        efficiency_fps_per_w=0.3,
        efficiency_mhz_per_w=clock / 200.0,
        watts_per_mhz=200.0 / clock,
        used_companion_load=True,
        result_reason="ok",
        log_path=Path("/tmp/final-curve.log"),
        q2rtx_avg_voltage_mv=float(voltage),
        q2rtx_avg_core_clock_mhz=float(clock),
        cuda_avg_voltage_mv=float(voltage),
        cuda_avg_core_clock_mhz=float(clock),
        loaded_qualified_sample_count=10,
    )


def _plan() -> list[dict]:
    stock = rtx_5080_20260524_high_oc_base_curve()
    return build_flattened_plan(
        stock, candidate_voltage_mv=850, lock_clock_mhz=2745, tail_rise_bins=2
    )


def test_efficiency_selects_run_21_and_preserves_every_tested_point() -> None:
    stock = rtx_5080_20260524_high_oc_base_curve()
    run21_plan = _plan()
    # A measured shape cannot be recovered just from its lock voltage/clock.
    point = next(point for point in run21_plan if point["voltage_mv"] == 840)
    point["target_mhz"] -= 15
    point["new_offset_mhz"] -= 15
    run21 = replace(
        _summary(850, 2745),
        avg_core_clock_mhz=2653.076923076923,
        avg_fps=59.547,
        avg_power_w=246.75123076923077,
        tested_plan=run21_plan,
    )
    run24_plan = build_flattened_plan(
        stock, candidate_voltage_mv=850, lock_clock_mhz=2800, tail_rise_bins=2
    )
    run24 = replace(
        _summary(850, 2800),
        avg_core_clock_mhz=2682.92,
        avg_fps=58.9,
        avg_power_w=249.05,
        tested_plan=run24_plan,
    )
    selected = select_final_scan_candidate(
        base_curve=stock,
        settings=SimpleNamespace(
            auto_uv_mode="efficiency", min_performance_core_clock_pct=88.9
        ),
        runtime_options={},
        stable_plan=run24_plan,
        stable_voltage_mv=850,
        stable_lock_clock_mhz=2800,
        stable_probe=run24,
        stable_history=[run21, run24],
        runner=None,
        gpu=None,
        probe_history=[],
        log=lambda _: None,
        tail_rise_bins=2,
        measured_baseline_clock_mhz=2627.764705882353,
        discovery_summary=_summary(975, 2625),
        baseline_candidate=None,
        final_verification_duration_s=60,
        event_callback=None,
        run_performance_auto_oc=False,
        request_reason="sweep-complete",
    )
    assert selected.probe is run21
    assert selected.lock_clock_mhz == 2745
    assert selected.voltage_mv == 850
    assert selected.plan == run21_plan
    assert selected.plan is not run21_plan
    assert selected.plan[0] is not run21_plan[0]
    # Selecting an older row in the GUI follows the same exact-plan rule.
    record = candidate_record_from_probe(
        run21,
        base_curve=stock,
        stable_plan=run24_plan,
        stable_voltage_mv=850,
        stable_lock_clock_mhz=2800,
        tail_rise_bins=2,
    )
    assert record["plan"] == run21_plan
    assert record["plan"] is not run21_plan


@pytest.mark.parametrize("require_choice", [False, True])
def test_performance_preserves_chosen_rung_instead_of_merging_earlier_rungs(
    monkeypatch, require_choice: bool
) -> None:
    stock = rtx_5080_20260524_high_oc_base_curve()
    attempts = []
    for voltage, clock in ((895, 2775), (900, 2820), (910, 2850),
                           (915, 2895), (920, 2910), (925, 2950)):
        plan = build_flattened_plan(
            stock, candidate_voltage_mv=voltage, lock_clock_mhz=clock, tail_rise_bins=4
        )
        probe = replace(_summary(voltage, clock), tested_plan=plan)
        candidate = VfCurveCandidate("performance-oc", voltage, clock, plan)
        outcome = VoltageProbeOutcome(
            decision=StableRunDecision(True, FailureKind.NONE, FailureSeverity.PASS, "ok"),
            raw_probe=probe,
        )
        attempts.append(SimpleNamespace(candidate=candidate, outcome=outcome))
    chosen = attempts[-1]
    original = [dict(point) for point in chosen.candidate.flattened_plan]
    history = [_summary(890, 2755)]
    monkeypatch.setattr(
        performance_uv_loop,
        "run_auto_oc_candidate_search",
        lambda **_: AutoOcSearchResult(
            selected_candidate=chosen.candidate,
            selected_probe=chosen.outcome.raw_probe,
            attempts=tuple(attempts),
        ),
    )
    choices = []

    def choose(**kwargs):
        selected = next(c for c in kwargs["candidates"]
                        if c["candidate_id"] == kwargs["default_candidate_id"])
        assert selected["plan"] == original
        choices.append(selected)
        return selected, 60

    monkeypatch.setattr(candidate_choice, "request_final_choice_candidate", choose)
    result = select_final_scan_candidate(
        base_curve=stock,
        settings=SimpleNamespace(
            auto_uv_mode="performance", min_performance_core_clock_pct=93.7,
            short_probe_base_duration_s=20,
        ),
        runtime_options={"auto_uv_require_final_choice": require_choice},
        stable_plan=build_flattened_plan(
            stock, candidate_voltage_mv=890, lock_clock_mhz=2755, tail_rise_bins=4
        ),
        stable_voltage_mv=890,
        stable_lock_clock_mhz=2755,
        stable_probe=history[0],
        stable_history=history,
        runner=None,
        gpu=SimpleNamespace(
            translated_gpu_policy={"gpu_name": "NVIDIA GeForce RTX 5080"},
            clock_ceiling=None,
        ),
        probe_history=[],
        log=lambda _: None,
        tail_rise_bins=4,
        measured_baseline_clock_mhz=2743.53,
        discovery_summary=_summary(1025, 2745),
        baseline_candidate=VfCurveCandidate("baseline", 1025, 2730, []),
        final_verification_duration_s=60,
        event_callback=None,
        run_performance_auto_oc=True,
        request_reason="adaptive-performance",
    )
    assert result.plan == original
    assert result.probe is chosen.outcome.raw_probe
    assert (result.voltage_mv, result.lock_clock_mhz) == (925, 2950)
    assert chosen.candidate.flattened_plan == original
    assert bool(choices) == require_choice


@pytest.mark.parametrize("voltage,clock,cap,tail,tier", [
    (850, 2745, 300, 2, "efficiency"),
    (890, 2800, 360, 4, "balanced"),
    (925, 2950, 360, 4, "performance"),
])
def test_final_soak_preserves_selected_curve_and_power_limit(
    monkeypatch, voltage: int, clock: int, cap: int, tail: int, tier: str
) -> None:
    plan = build_flattened_plan(
        rtx_5080_20260524_high_oc_base_curve(), candidate_voltage_mv=voltage,
        lock_clock_mhz=clock, tail_rise_bins=tail,
    )
    original = [dict(p) for p in plan]
    stages, saved, applied, events, retargets = [], [], [], [], []

    def probe(**kwargs):
        stage = kwargs["phase_label"]
        stages.append(stage)
        if stage == "final-verify":
            assert kwargs["candidate_plan"] == original
            assert kwargs["power_limit_w"] == cap
        voltage, clock = kwargs["candidate_voltage_mv"], kwargs["lock_clock_mhz"]
        return _summary(voltage, clock), stable_probe_result(clock_mhz=clock)

    def save(**kwargs):
        assert stages[-1] == "final-verify"
        saved.append(kwargs)
        return Path("/tmp/mock-verified-profile.json")

    monkeypatch.setattr(
        final_loop,
        "write_last_stable_result_snapshot",
        lambda **_: Path("/tmp/mock-snapshot.json"),
    )
    monkeypatch.setattr(
        final_loop,
        "apply_plan_and_refresh",
        lambda _reader, curve: applied.append([dict(p) for p in curve]),
    )
    monkeypatch.setattr(final_loop, "write_latest_verified_candidate", save)
    monkeypatch.setattr(final_loop, "write_final_stable_result", save)
    monkeypatch.setattr(final_loop, "write_final_verified_profile", save)
    monkeypatch.setattr(
        final_loop, "write_final_verification_fan_curve_payload", lambda **_: None
    )
    final_loop.run_final_verification_and_save(
        probe_voltage_candidate=probe,
        build_voltage_scan_result=lambda **kwargs: kwargs,
        log=lambda _: None,
        reader=None,
        stable_plan=plan,
        stable_voltage_mv=voltage,
        stable_lock_clock_mhz=clock,
        stable_probe=_summary(voltage, clock),
        stable_history=[],
        probe_history=[],
        q2rtx_config=Q2RTXStabilityConfig(),
        final_verification_duration_s=60,
        start_voltage_mv=1025,
        measured_clock_mhz=clock,
        nvml_session=None,
        clock_ceiling=SimpleNamespace(
            retarget=lambda **kwargs: retargets.append(kwargs),
            describe=lambda: f"{voltage}mV@{clock}MHz",
        ),
        discovery_summary=_summary(1025, 2745),
        translated_gpu_policy={"power_limit_w": cap},
        gpu_identity={},
        min_performance_core_clock_pct=90,
        runtime_default_plan=[],
        final_clock_drop_margin_pct=10,
        tail_rise_bins=tail,
        auto_uv_mode=tier,
        generated_profile_tier=tier,
        event_callback=lambda event, payload: events.append((event, payload)),
    )
    assert stages == ["final-verify"]
    assert len(saved) == 3
    assert all(item["plan"] == original for item in saved)
    assert saved[-1]["power_limit_w"] == cap
    assert plan == original
    assert applied and all(curve == original for curve in applied)
    assert retargets and all(item["lock_clock_mhz"] == clock for item in retargets)
    assert all(item["lock_voltage_mv"] == voltage for item in retargets)
    starts = [payload for event, payload in events if event == "probe_start"]
    assert len(starts) == 1
    assert starts[0]["stage"] == "final-verify"
    assert starts[0]["clock_mhz"] == clock
    assert starts[0]["voltage_mv"] == voltage
