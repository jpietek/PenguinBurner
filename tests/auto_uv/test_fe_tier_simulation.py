"""Run real tier searches against assumed FE power/voltage responses.

NVIDIA publishes boost/TGP as 2620MHz/360W (5080), 2410MHz/575W (5090):
https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5080/
https://www.nvidia.com/en-us/geforce/graphics-cards/50-series/rtx-5090/

Those specs do not provide a stock V/F table or an undervolt stability limit.
We scale existing fixtures to nominal boost at an ASSUMED 1V, then vary dynamic
power +/-20%. Only algorithm invariants are asserted, never predicted FE MHz,
FPS, stability, or a guaranteed Performance advantage under saturation.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from auto_uv.base_uv_loop import BaseUvLoopIO
from auto_uv.curve.base_load_flatten_target import choose_base_load_flatten_target
from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import VfCurveCandidate
from auto_uv.main_loop import (
    adaptive_tier_descent_tail_rise_bins,
    adaptive_tier_power_limit_w,
    run_preset_uv_loop,
    select_final_scan_candidate,
)
from auto_uv.scan_mode.uv_limits import (
    uv_limit_clock_drop_pct_for_gpu,
    uv_limit_power_limit_pct_for_gpu,
    uv_limit_profile_target_for_gpu,
)
from auto_uv_test_data import (
    rtx_5080_20260524_high_oc_base_curve,
    rtx_5090_zotac_amp_stock_curve,
)
from power_governor_sim import (
    GovernorPowerModel,
    GovernorProbeHarness,
    settle_operating_point,
    synthesize_busy_samples,
)


@pytest.mark.parametrize("tier", ["efficiency", "balanced", "performance"])
@pytest.mark.parametrize("load", [0.8, 1.0, 1.2])
@pytest.mark.parametrize(
    "gpu,boost,tgp,curve_factory,cap_override",
    [
        ("RTX 5080", 2620, 360, rtx_5080_20260524_high_oc_base_curve, None),
        ("RTX 5090", 2410, 575, rtx_5090_zotac_amp_stock_curve, None),
        ("RTX 5090", 2410, 575, rtx_5090_zotac_amp_stock_curve, 300),
    ],
    ids=["5080-defaults", "5090-defaults", "5090-severe-cap"],
)
def test_fe_tier_search_preserves_measured_curve_under_power_limits(
    gpu, boost, tgp, curve_factory, cap_override, load, tier,
) -> None:
    curve = curve_factory()
    scale = boost / next(p["base_mhz"] for p in curve if p["voltage_mv"] == 1000)
    curve = [
        dict(p, base_mhz=round(p["base_mhz"] * scale),
             target_mhz=round(p["base_mhz"] * scale))
        for p in curve
    ]
    model = GovernorPowerModel(
        dynamic_w_per_mhz_v2=(tgp - 40) / boost * load,
        idle_w=40, spike_margin_mv=0, vdroop_mv=25,
        clock_jitter_mhz=12, cap_reason_duty=0.75,
    )
    cap = adaptive_tier_power_limit_w(
        power_limit_pct=uv_limit_power_limit_pct_for_gpu(gpu, tier),
        baseline_power_limit_w=tgp, scan_request_w=None, balanced_pct=None,
    )
    assert cap is not None
    if gpu == "RTX 5080":
        cap = max(300, cap)  # Hardware minimum observed on our 5080.
    if cap_override is not None:
        cap = min(cap, cap_override)
    stock = settle_operating_point(curve, model=model, power_limit_w=cap)
    target = choose_base_load_flatten_target(
        curve, synthesize_busy_samples(stock, model=model),
        power_limit_w=cap, fallback_clock_mhz=stock["clock_mhz"],
    )
    efficiency_target = uv_limit_profile_target_for_gpu(gpu, "efficiency")
    drop_pct = uv_limit_clock_drop_pct_for_gpu(gpu, tier)
    assert efficiency_target is not None and drop_pct is not None
    floor = efficiency_target.voltage_mv
    bins = adaptive_tier_descent_tail_rise_bins(tier)
    minimum_pct = 100 - drop_pct
    harness = GovernorProbeHarness(
        curve, model, cap, target.measured_clock_mhz, stock["power_w"], 100,
        min_core_clock_pct=minimum_pct,
    )
    history = []
    tested = []

    def probe(candidate):
        outcome = harness.probe(candidate)
        if outcome.raw_probe is not None:
            outcome.raw_probe.tested_plan = [dict(p) for p in candidate.flattened_plan]
            tested.append((candidate, outcome))
            if outcome.decision.passed:
                history.append(outcome.raw_probe)
        return outcome

    runner = SimpleNamespace(
        power_limit_w=cap, probe_candidate=lambda candidate, **_: probe(candidate),
    )
    start = VfCurveCandidate(
        "baseline", max(floor, stock["voltage_mv"]), target.target_clock_mhz, [],
    )
    start = replace(start, flattened_plan=build_flattened_plan(
        curve, lock_clock_mhz=start.target_mhz,
        candidate_voltage_mv=start.voltage_mv, tail_rise_bins=bins,
    ))
    initial = probe(start)
    result = run_preset_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=start.voltage_mv, min_search_voltage_mv=floor,
            baseline_core_clock_mhz=target.measured_clock_mhz, auto_uv_mode=tier,
            min_core_clock_pct=minimum_pct, tail_rise_bins=bins,
        ),
        initial_stable_candidate=start,
        io=BaseUvLoopIO(probe, lambda *_: None, lambda *_: None),
        initial_stable_outcome=initial, unsafe_entries=None,
        min_search_voltage_mv=floor, initial_tail_rise_bins=bins,
        log=lambda _: None,
    )
    assert result.stable_outcome is not None
    selected = select_final_scan_candidate(
        base_curve=curve,
        settings=SimpleNamespace(
            auto_uv_mode=tier, min_performance_core_clock_pct=minimum_pct,
        ),
        runtime_options={}, stable_plan=result.stable_candidate.flattened_plan,
        stable_voltage_mv=result.stable_candidate.voltage_mv,
        stable_lock_clock_mhz=result.stable_candidate.target_mhz,
        stable_probe=result.stable_outcome.raw_probe, stable_history=history,
        runner=runner,
        gpu=SimpleNamespace(translated_gpu_policy={"gpu_name": gpu}, clock_ceiling=None),
        probe_history=[], log=lambda _: None, tail_rise_bins=bins,
        measured_baseline_clock_mhz=target.measured_clock_mhz,
        discovery_summary=initial.raw_probe, baseline_candidate=start,
        final_verification_duration_s=60, event_callback=None,
        run_performance_auto_oc=tier == "performance",
        run_power_bound_clock_reclaim=tier != "performance",
        request_reason="simulation",
    )
    # Final selection must reference an actual passing probe, point for point.
    assert any(
        outcome.decision.passed and candidate.flattened_plan == selected.plan
        for candidate, outcome in tested
    )
    assert selected.probe is not None and selected.plan == selected.probe.tested_plan
    assert all(
        a["target_mhz"] <= b["target_mhz"]
        for a, b in zip(selected.plan, selected.plan[1:])
    )
    tail = [p["target_mhz"] for p in selected.plan if p["voltage_mv"] > selected.voltage_mv]
    assert len(set(tail)) == bins
    if tier == "efficiency":
        eligible = [p for p in history if p.avg_core_clock_mhz >= target.measured_clock_mhz * minimum_pct / 100]
        assert selected.probe.avg_fps / selected.probe.avg_power_w == max(
            p.avg_fps / p.avg_power_w for p in eligible
        )
    final = probe(VfCurveCandidate(
        "final", selected.voltage_mv, selected.lock_clock_mhz, selected.plan,
    ))
    assert final.decision.passed
    assert all(p["power_w"] <= cap for p in harness.probes if p["power_w"] is not None)
    print(
        f"{gpu} {tier} load={load}: cap={cap}W, "
        f"baseline={target.measured_clock_mhz:.0f}MHz, "
        f"selected={selected.voltage_mv}mV@{selected.lock_clock_mhz}MHz, "
        f"measured={final.measured_core_clock_mhz:.0f}MHz"
    )
