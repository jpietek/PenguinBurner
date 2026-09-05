"""Loop-level power-bound scenarios: the scan's algorithms vs a governor.

The unit scenarios in ``test_power_bound_5090`` prove each fixed helper in
isolation. They cannot answer the question the field failure actually poses —
"does the scan produce a usable profile on a power-bound card, or does the
undervolt effectively stop?" — because that answer is produced by the descent
loop and the clock-reclaim climb, not by any single helper.

These tests run those real algorithms against the simulated governor, so a
regression that leaves a power-bound card parked near its capped stock clock
fails here instead of shipping.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

from auto_uv.auto_oc.search import AUTO_OC_WALL_SHORTFALL_TOLERANCE_MHZ
from auto_uv.base_uv_loop import BaseUvLoopIO, run_base_uv_loop
from auto_uv.curve.measured_probe_lock_clock import probe_indicates_power_saturation
from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.domain.scan_settings import AutoUvScanSettings
from auto_uv.domain.types import FailureKind, VfCurveCandidate
from auto_uv.performance_uv_loop import select_power_bound_clock_reclaim_candidate
from auto_uv.probes.stability_decision import (
    StabilityThresholds,
    evaluate_loaded_telemetry,
)
from auto_uv_test_data import rtx_5090_steep_synthetic_curve
from power_governor_sim import (
    GovernorPowerModel,
    GovernorProbeHarness,
    scenario_5090_blackwell_models,
    settle_operating_point,
    sustainable_clock_mhz,
    synthesize_busy_samples,
)

GPU_NAME = "NVIDIA GeForce RTX 5090"
EFFICIENCY_CAP_W = 430
PERFORMANCE_CAP_W = 575


def _harness(
    model: GovernorPowerModel,
    *,
    power_limit_w: int,
    curve: list[dict] | None = None,
    min_core_clock_pct: float = 94.0,
) -> GovernorProbeHarness:
    base_curve = curve if curve is not None else rtx_5090_steep_synthetic_curve()
    stock = settle_operating_point(
        base_curve,
        model=model,
        power_limit_w=float(power_limit_w),
    )
    return GovernorProbeHarness(
        base_curve=base_curve,
        model=model,
        power_limit_w=int(power_limit_w),
        baseline_core_clock_mhz=float(stock["clock_mhz"]),
        baseline_power_w=float(stock["power_w"]),
        baseline_fps=100.0,
        min_core_clock_pct=float(min_core_clock_pct),
    )


def _candidate(
    curve: list[dict],
    *,
    voltage_mv: int,
    clock_mhz: int,
    label: str = "sim",
) -> VfCurveCandidate:
    return VfCurveCandidate(
        label=label,
        voltage_mv=int(voltage_mv),
        target_mhz=int(clock_mhz),
        flattened_plan=build_flattened_plan(
            curve,
            lock_clock_mhz=int(clock_mhz),
            candidate_voltage_mv=int(voltage_mv),
        ),
    )


def test_capped_stock_baseline_is_the_low_clock_the_field_report_shows() -> None:
    # Frames the scenario the rest of the file is about: under a savings cap
    # the 5090's stock curve operates far below its top point, so a scan that
    # simply adopts the capped measurement ships that low clock.
    model = scenario_5090_blackwell_models()[1]
    curve = rtx_5090_steep_synthetic_curve()

    stock = settle_operating_point(
        curve, model=model, power_limit_w=float(EFFICIENCY_CAP_W)
    )

    assert stock["power_capped"]
    assert stock["clock_mhz"] < max(int(p["target_mhz"]) for p in curve) - 400


def test_clock_reclaim_climbs_to_the_power_wall_not_to_the_first_rung() -> None:
    """The reclaim must recover the headroom the undervolt created.

    This is the regression that would reproduce "after 2.1GHz the undervolt
    effectively stopped": every rung on a power-bound card is saturated, so a
    climb that stops at the first rung whose measured clock trails its request
    ships the capped stock clock and calls it a profile.
    """
    for model in scenario_5090_blackwell_models():
        curve = rtx_5090_steep_synthetic_curve()
        harness = _harness(model, power_limit_w=EFFICIENCY_CAP_W, curve=curve)
        descended_voltage_mv = 900
        wall_mhz = sustainable_clock_mhz(
            model,
            voltage_mv=float(descended_voltage_mv),
            power_limit_w=float(EFFICIENCY_CAP_W),
        )
        start_clock_mhz = int(harness.baseline_core_clock_mhz)
        start = _candidate(
            curve,
            voltage_mv=descended_voltage_mv,
            clock_mhz=start_clock_mhz,
        )

        _plan, _voltage_mv, selected_clock_mhz, _probe, metadata = (
            select_power_bound_clock_reclaim_candidate(
                curve,
                auto_uv_mode="efficiency",
                stable_plan=list(start.flattened_plan),
                stable_voltage_mv=descended_voltage_mv,
                stable_lock_clock_mhz=start_clock_mhz,
                stable_probe=harness.probe(start).raw_probe,
                stable_history=[],
                runner=cast(Any, harness),
                gpu_name=GPU_NAME,
                clock_ceiling=None,
                probe_history=[],
                log=lambda _message: None,
            )
        )

        assert metadata.get("clock_reclaim") is True
        # The climb must recover most of the gap to the wall this undervolt
        # opened up, instead of stalling on the capped stock clock.
        recovered = float(selected_clock_mhz) - float(start_clock_mhz)
        available = wall_mhz - float(start_clock_mhz)
        assert available > 0.0, "scenario must leave real headroom to reclaim"
        assert recovered >= 0.5 * available, (
            f"model {model.dynamic_w_per_mhz_v2}: reclaimed only "
            f"{recovered:.0f}MHz of {available:.0f}MHz available headroom "
            f"(selected {selected_clock_mhz}MHz, wall {wall_mhz:.0f}MHz)"
        )
        # And it must not overshoot the wall it measured.
        assert float(selected_clock_mhz) <= wall_mhz + 60.0


def test_vdroop_shortfall_alone_does_not_end_the_climb() -> None:
    """Blackwell measures below its requested lock even with power to spare.

    The live 5080 log shows unsaturated rungs trailing their request by up to
    103MHz. A wall rule that reads any shortfall as a power wall would stop
    those climbs, so the shortfall must only count while saturated.
    """
    model = GovernorPowerModel(
        dynamic_w_per_mhz_v2=0.20,
        idle_w=40.0,
        spike_margin_mv=0.0,
        vdroop_mv=25.0,
        clock_jitter_mhz=20.0,
    )
    curve = rtx_5090_steep_synthetic_curve()
    # A cap so generous nothing saturates: every shortfall here is droop.
    harness = _harness(model, power_limit_w=2000, curve=curve)
    # Lock inside the GB202 knee, where a few mV of droop costs real clock.
    descended_voltage_mv = 925
    start_clock_mhz = 2400
    start = _candidate(
        curve, voltage_mv=descended_voltage_mv, clock_mhz=start_clock_mhz
    )
    # The climb is judged against the descended candidate it starts from, not
    # against an uncapped stock top clock this tier never asked for.
    harness.baseline_core_clock_mhz = float(
        harness.operating_point(list(start.flattened_plan))["clock_mhz"]
    )

    _plan, _voltage_mv, selected_clock_mhz, _probe, _metadata = (
        select_power_bound_clock_reclaim_candidate(
            curve,
            auto_uv_mode="balanced",
            stable_plan=list(start.flattened_plan),
            stable_voltage_mv=descended_voltage_mv,
            stable_lock_clock_mhz=start_clock_mhz,
            stable_probe=harness.probe(start).raw_probe,
            stable_history=[],
            runner=cast(Any, harness),
            gpu_name=GPU_NAME,
            clock_ceiling=None,
            probe_history=[],
            log=lambda _message: None,
        )
    )

    droop_shortfalls = [
        float(probe["requested_mhz"]) - float(probe["measured_mhz"])
        for probe in harness.probes
        if probe["measured_mhz"] is not None
    ]
    assert max(droop_shortfalls) > AUTO_OC_WALL_SHORTFALL_TOLERANCE_MHZ, (
        "scenario must actually produce a droop shortfall past the wall "
        "tolerance, otherwise it proves nothing"
    )
    assert not any(probe["power_capped"] for probe in harness.probes)
    assert selected_clock_mhz > start_clock_mhz


def test_sparse_hardware_brake_ends_the_climb_the_summary_no_longer_names() -> None:
    """The board's protection brake must stop a climb even when it is sparse.

    Same scenario as the droop test above — generous cap, nothing saturates —
    with the one difference that the board asserts hw-power-brake on a
    minority of samples. The brake is transient by nature, so the summarizer's
    reason vote drops it from ``perf_cap_reason`` entirely; only the counted
    samples survive. Reading the lossy string here let the climb keep asking
    for clocks the power delivery had already refused.
    """
    droop_only = GovernorPowerModel(
        dynamic_w_per_mhz_v2=0.20,
        idle_w=40.0,
        spike_margin_mv=0.0,
        vdroop_mv=25.0,
        clock_jitter_mhz=20.0,
    )
    braked = replace(droop_only, brake_duty=0.2)
    curve = rtx_5090_steep_synthetic_curve()
    descended_voltage_mv = 925
    start_clock_mhz = 2400
    start = _candidate(
        curve, voltage_mv=descended_voltage_mv, clock_mhz=start_clock_mhz
    )

    selected: dict[str, int] = {}
    for name, model in (("droop-only", droop_only), ("braked", braked)):
        # A cap so generous nothing saturates: no sw-power, no near-limit
        # average power. The brake is the only power evidence in play.
        harness = _harness(model, power_limit_w=2000, curve=curve)
        harness.baseline_core_clock_mhz = float(
            harness.operating_point(list(start.flattened_plan))["clock_mhz"]
        )
        start_probe = harness.probe(start).raw_probe
        assert start_probe is not None
        if model is braked:
            # The evidence the decision has to run on: counted, not named.
            assert start_probe.hw_power_brake_samples > 0
            assert "brake" not in str(start_probe.perf_cap_reason or "")
            assert probe_indicates_power_saturation(
                start_probe,
                power_limit_w=2000,
                require_power_evidence=True,
            )
        _plan, _voltage_mv, selected_clock_mhz, _probe, _metadata = (
            select_power_bound_clock_reclaim_candidate(
                curve,
                auto_uv_mode="balanced",
                stable_plan=list(start.flattened_plan),
                stable_voltage_mv=descended_voltage_mv,
                stable_lock_clock_mhz=start_clock_mhz,
                stable_probe=start_probe,
                stable_history=[],
                runner=cast(Any, harness),
                gpu_name=GPU_NAME,
                clock_ceiling=None,
                probe_history=[],
                log=lambda _message: None,
            )
        )
        assert not any(probe["power_capped"] for probe in harness.probes)
        selected[name] = int(selected_clock_mhz)

    assert selected["droop-only"] > start_clock_mhz, (
        "control must still climb, otherwise the brake proves nothing"
    )
    assert selected["braked"] < selected["droop-only"]


def test_descent_walks_down_instead_of_stopping_at_the_capped_clock() -> None:
    """The descent is the product; a power wall must not end it early.

    On a power-bound card the busy clock sits below the flat at every step.
    If that reads as instability the sweep stops at its first probe and the
    profile is stock. The sweep must instead descend until a genuine V/F
    limit stops it.
    """
    unstable_floor_mv = 890
    model = replace(
        scenario_5090_blackwell_models()[1],
        unstable_below_mv=unstable_floor_mv,
    )
    curve = rtx_5090_steep_synthetic_curve()
    harness = _harness(model, power_limit_w=EFFICIENCY_CAP_W, curve=curve)
    start_voltage_mv = 1000
    start_clock_mhz = int(harness.baseline_core_clock_mhz)
    start = _candidate(
        curve, voltage_mv=start_voltage_mv, clock_mhz=start_clock_mhz
    )
    unsafe: list[dict] = []

    result = run_base_uv_loop(
        curve,
        settings=AutoUvScanSettings(
            start_voltage_mv=start_voltage_mv,
            min_search_voltage_mv=850,
            baseline_core_clock_mhz=float(start_clock_mhz),
            auto_uv_mode="balanced",
            min_core_clock_pct=94.0,
            tail_rise_bins=4,
        ),
        initial_stable_candidate=start,
        io=BaseUvLoopIO(
            probe_candidate=harness.probe,
            write_verified_candidate=lambda _candidate, _outcome: None,
            mark_unsafe_candidate=lambda candidate, _outcome: unsafe.append(
                {"voltage_mv": int(candidate.voltage_mv)}
            ),
        ),
        unsafe_entries=None,
        initial_stable_outcome=harness.probe(start),
    )

    probed_voltages = sorted({probe["voltage_mv"] for probe in harness.probes})
    assert len(probed_voltages) >= 4, (
        f"descent stopped after {probed_voltages}: a power-bound sweep must "
        "keep walking the voltage down"
    )
    assert int(result.stable_candidate.voltage_mv) < start_voltage_mv
    # It stopped for a real reason, not for a governed clock: the sweep
    # reached the injected V/F floor and nothing else failed on the way.
    assert min(probed_voltages) <= unstable_floor_mv
    assert unsafe, "the crash at the instability floor must be recorded"
    assert all(
        probe["passed"] or probe["crashed"] for probe in harness.probes
    ), (
        "a power-governed clock was misread as instability: "
        f"{[probe for probe in harness.probes if not probe['passed']]}"
    )


def test_low_clock_still_fails_when_the_cap_is_only_named_by_a_few_samples() -> None:
    """A sparse sw-power minority must not buy a power-walled pass.

    The summarizer's coverage gate is the only thing standing between an
    occasional cap blip and an exemption that would hide real V/F demotion.
    """
    model = scenario_5090_blackwell_models()[1]
    curve = rtx_5090_steep_synthetic_curve()
    capped = settle_operating_point(
        curve, model=model, power_limit_w=float(EFFICIENCY_CAP_W)
    )
    samples = synthesize_busy_samples(capped, count=12, model=model)
    sparse = [
        {
            **sample,
            # Far off the cap, so only the reason vote could grant a pass.
            "power_w": 300.0,
            "core_clock_mhz": 1900.0,
            "perf_cap_reason": "sw-power" if index == 0 else None,
        }
        for index, sample in enumerate(samples)
    ]

    decision = evaluate_loaded_telemetry(
        sparse,
        baseline_power_w=float(capped["power_w"]),
        baseline_core_clock_mhz=2595.0,
        power_limit_w=EFFICIENCY_CAP_W,
        thresholds=StabilityThresholds(min_core_clock_pct=94.0),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOW_CLOCK


def test_thermal_cap_does_not_buy_a_power_walled_pass() -> None:
    """Only a power cap explains a clock shortfall as the cap working."""
    model = scenario_5090_blackwell_models()[1]
    curve = rtx_5090_steep_synthetic_curve()
    capped = settle_operating_point(
        curve, model=model, power_limit_w=float(EFFICIENCY_CAP_W)
    )
    thermal = [
        {
            **sample,
            "power_w": 300.0,
            "core_clock_mhz": 1900.0,
            "perf_cap_reason": "hw-thermal",
        }
        for sample in synthesize_busy_samples(capped, count=12, model=model)
    ]

    decision = evaluate_loaded_telemetry(
        thermal,
        baseline_power_w=float(capped["power_w"]),
        baseline_core_clock_mhz=2595.0,
        power_limit_w=EFFICIENCY_CAP_W,
        thresholds=StabilityThresholds(min_core_clock_pct=94.0),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOW_CLOCK


def test_ramp_samples_do_not_change_the_power_walled_verdict() -> None:
    """The idle/ramp head must not dilute the busy window's evidence."""
    model = scenario_5090_blackwell_models()[0]
    curve = rtx_5090_steep_synthetic_curve()
    harness = _harness(model, power_limit_w=EFFICIENCY_CAP_W, curve=curve)
    candidate = _candidate(curve, voltage_mv=1000, clock_mhz=2595)

    outcome = harness.probe(candidate)
    busy_only = evaluate_loaded_telemetry(
        synthesize_busy_samples(
            harness.operating_point(list(candidate.flattened_plan)),
            count=12,
            model=model,
        ),
        baseline_power_w=float(harness.baseline_power_w),
        baseline_core_clock_mhz=float(harness.baseline_core_clock_mhz),
        power_limit_w=EFFICIENCY_CAP_W,
        thresholds=StabilityThresholds(min_core_clock_pct=94.0),
        log_path=None,
    )

    assert outcome.decision.passed == busy_only.passed


def test_sw_power_reason_far_below_the_cap_does_not_end_the_climb() -> None:
    """The driver names a power cap while the board is nowhere near it.

    Live evidence: the 2026-08-04 Blackwell log summarizes ``sw-power`` on a
    balanced probe drawing 287W under a 319W cap, and the 5090 field report's
    failing soak reports ``sw-power`` at 453W average. If the reason token
    alone proves a power wall, those runs are "walled" while hundreds of watts
    of headroom remain — and a climb that stops there ships the low clock the
    field report complains about.
    """
    model = replace(
        scenario_5090_blackwell_models()[0],
        # Reason-bearing telemetry that names the cap, while the operating
        # point draws far less than the configured limit.
        reports_power_cap_off_the_wall=True,
    )
    curve = rtx_5090_steep_synthetic_curve()
    harness = _harness(model, power_limit_w=PERFORMANCE_CAP_W, curve=curve)
    descended_voltage_mv = 900
    start_clock_mhz = 2200
    start = _candidate(
        curve, voltage_mv=descended_voltage_mv, clock_mhz=start_clock_mhz
    )
    harness.baseline_core_clock_mhz = float(
        harness.operating_point(list(start.flattened_plan))["clock_mhz"]
    )

    _plan, _voltage_mv, selected_clock_mhz, _probe, _metadata = (
        select_power_bound_clock_reclaim_candidate(
            curve,
            auto_uv_mode="efficiency",
            stable_plan=list(start.flattened_plan),
            stable_voltage_mv=descended_voltage_mv,
            stable_lock_clock_mhz=start_clock_mhz,
            stable_probe=harness.probe(start).raw_probe,
            stable_history=[],
            runner=cast(Any, harness),
            gpu_name=GPU_NAME,
            clock_ceiling=None,
            probe_history=[],
            log=lambda _message: None,
        )
    )

    climbed = [probe for probe in harness.probes if probe["measured_mhz"] is not None]
    headroom_w = [
        float(PERFORMANCE_CAP_W) - float(probe["power_w"]) for probe in climbed
    ]
    assert min(headroom_w) > 60.0, (
        "scenario must keep real power headroom at every rung, otherwise it "
        "is testing a genuine wall"
    )
    assert selected_clock_mhz > start_clock_mhz, (
        "the climb stopped while the board still had "
        f"{min(headroom_w):.0f}W of headroom at every rung"
    )
