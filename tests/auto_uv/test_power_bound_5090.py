"""Power-bound 5090 scenarios: captured geometry plus synthetic governors.

The simulator explains causality (a configured cap can make measured clock
differ from the curve target) without claiming to predict a particular board.
Product cap values and orchestration parity are tested separately against the
real PenguinBurner tier logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from auto_uv.curve.base_load_flatten_target import choose_base_load_flatten_target
from auto_uv.curve.measured_probe_lock_clock import lock_clock_from_probe_loaded_clock
from auto_uv.curve.shipped_plan import assert_monotonic_editable_targets
from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.domain.types import AutoUvProbeSummary, FailureKind
from auto_uv.probes.stability_decision import (
    StabilityThresholds,
    evaluate_loaded_telemetry,
)
from auto_uv_test_data import (
    rtx_5080_20260524_high_oc_base_curve,
    rtx_5090_steep_synthetic_curve,
    rtx_5090_zotac_amp_stock_curve,
)
from power_governor_sim import (
    GovernorPowerModel,
    scenario_5090_power_models,
    settle_operating_point,
    synthesize_busy_samples,
)

EFFICIENCY_CAP_W = 430
BALANCED_CAP_W = 575
PERFORMANCE_CAP_W = 575


def _editable_targets(plan: list[dict]) -> list[tuple[int, int]]:
    return sorted(
        (int(point["voltage_mv"]), int(point["target_mhz"]))
        for point in plan
        if not point.get("preserve_base")
    )


def _probe(
    *,
    avg_core_clock_mhz: float,
    avg_power_w: float,
    perf_cap_reason: str | None,
) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=1000,
        lock_clock_mhz=2595,
        live_voltage_before_mv=1000,
        live_voltage_after_mv=1000,
        avg_voltage_mv=950.0,
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=float(avg_power_w),
        max_power_w=float(avg_power_w) + 10.0,
        avg_temperature_c=74.0,
        max_temperature_c=76.0,
        avg_fan_speed_pct=60.0,
        max_fan_speed_pct=85.0,
        avg_core_clock_mhz=float(avg_core_clock_mhz),
        efficiency_fps_per_w=0.2,
        efficiency_mhz_per_w=4.6,
        watts_per_mhz=0.21,
        used_companion_load=True,
        result_reason="stable run",
        log_path=Path("/tmp/q2rtx.log"),
        perf_cap_reason=perf_cap_reason,
    )


@pytest.mark.parametrize("model", scenario_5090_power_models())
def test_s1_capped_baseline_targets_what_the_cap_allows(
    model: GovernorPowerModel,
) -> None:
    curve = rtx_5090_steep_synthetic_curve()

    settle = settle_operating_point(curve, model=model, power_limit_w=BALANCED_CAP_W)

    # The curve's top point is unreachable: the governor parks in the knee
    # band, hundreds of MHz below the top target.
    assert settle["power_capped"]
    assert settle["voltage_mv"] < max(p["voltage_mv"] for p in curve)
    assert settle["clock_mhz"] < max(p["target_mhz"] for p in curve)

    target = choose_base_load_flatten_target(
        curve,
        synthesize_busy_samples(settle),
        power_limit_w=BALANCED_CAP_W,
        fallback_clock_mhz=None,
    )

    # Target derivation under the shipped cap is honest by construction:
    # the flat target is the snapped settled clock, not a stock-limit clock.
    assert target.target_clock_mhz == (settle["clock_mhz"] // 15) * 15


@pytest.mark.parametrize("model", scenario_5090_power_models())
def test_s2_honest_target_flat_holds_its_clock_under_the_cap(
    model: GovernorPowerModel,
) -> None:
    curve = rtx_5090_steep_synthetic_curve()
    baseline = settle_operating_point(curve, model=model, power_limit_w=BALANCED_CAP_W)
    lock_clock_mhz = (baseline["clock_mhz"] // 15) * 15
    plan = build_flattened_plan(
        curve,
        lock_clock_mhz=lock_clock_mhz,
        candidate_voltage_mv=int(baseline["voltage_mv"]),
    )

    settle = settle_operating_point(plan, model=model, power_limit_w=BALANCED_CAP_W)
    decision = evaluate_loaded_telemetry(
        synthesize_busy_samples(settle),
        baseline_power_w=float(baseline["power_w"]),
        baseline_core_clock_mhz=float(lock_clock_mhz),
        power_limit_w=BALANCED_CAP_W,
        thresholds=StabilityThresholds(min_core_clock_pct=94.0),
        log_path=None,
    )

    # A cap-derived flat is cheap enough that the governor holds the full
    # lock clock (it parks on the flat, above the lock voltage if it likes).
    assert settle["clock_mhz"] == lock_clock_mhz
    assert decision.passed


@pytest.mark.parametrize("model", scenario_5090_power_models())
def test_product_tier_caps_order_synthetic_operating_points(
    model: GovernorPowerModel,
) -> None:
    curve = rtx_5090_steep_synthetic_curve()
    points = [
        settle_operating_point(curve, model=model, power_limit_w=cap_w)
        for cap_w in (EFFICIENCY_CAP_W, BALANCED_CAP_W, PERFORMANCE_CAP_W)
    ]

    assert all(point["power_capped"] for point in points)
    assert [point["clock_mhz"] for point in points] == sorted(
        point["clock_mhz"] for point in points
    )
    assert [point["voltage_mv"] for point in points] == sorted(
        point["voltage_mv"] for point in points
    )


def test_s3_ratchet_keeps_target_when_probe_was_power_capped() -> None:
    curve = rtx_5090_steep_synthetic_curve()
    probe = _probe(
        avg_core_clock_mhz=2418.5,
        avg_power_w=553.0,
        perf_cap_reason="sw-power",
    )

    # The field scan turned this exact probe into a 2595 -> 2418 ratchet.
    assert (
        lock_clock_from_probe_loaded_clock(
            curve,
            probe=probe,
            previous_lock_clock_mhz=2595,
            power_limit_w=575,
        )
        == 2595
    )


def test_s4_changed_cap_moves_operation_below_lock_without_predicting_hardware() -> None:
    # Illustrate the old regime mismatch with actual PenguinBurner tier caps:
    # a target selected at the 575W Performance budget is then operated at the
    # 430W Efficiency budget. This is not a replay of the Reddit card because
    # its configured final power limit is unknown.
    curve = rtx_5090_steep_synthetic_curve()
    model = scenario_5090_power_models()[1]
    shipped = build_flattened_plan(
        curve,
        lock_clock_mhz=2595,
        candidate_voltage_mv=1000,
    )

    settle = settle_operating_point(
        shipped, model=model, power_limit_w=EFFICIENCY_CAP_W
    )

    assert settle["voltage_mv"] < 1000
    assert settle["clock_mhz"] < 2595
    assert settle["power_capped"]

    decision = evaluate_loaded_telemetry(
        synthesize_busy_samples(settle),
        baseline_power_w=500.0,
        baseline_core_clock_mhz=2595.0,
        power_limit_w=EFFICIENCY_CAP_W,
        thresholds=StabilityThresholds(min_core_clock_pct=94.0),
        log_path=None,
    )

    # The saturation-aware floor recognizes governor descent. The full probe
    # path still enforces same-cap FPS and crash/companion-load decisions.
    assert decision.passed
    assert "power-walled but stable" in decision.reason


def test_s5_selected_5090_ramp_stays_below_lock_without_downward_edge() -> None:
    plan = build_flattened_plan(
        rtx_5090_steep_synthetic_curve(),
        lock_clock_mhz=2595,
        candidate_voltage_mv=1000,
    )
    assert_monotonic_editable_targets(plan)
    targets = _editable_targets(plan)
    below_lock = [(v, t) for v, t in targets if v < 1000]
    assert below_lock
    assert all(t <= 2595 - 15 for _, t in below_lock)
    assert dict(targets)[1000] == 2595


def test_s6_5080_selected_ramp_is_preserved_above_stock() -> None:
    plan = build_flattened_plan(
        rtx_5080_20260524_high_oc_base_curve(),
        lock_clock_mhz=2730,
        candidate_voltage_mv=1000,
    )
    tested = [dict(point) for point in plan]

    assert_monotonic_editable_targets(plan)

    assert plan == tested
    # The tested approach contains raised points; restoring stock here would
    # discard that ramp and no longer verify the selected curve.
    assert any(
        int(point["target_mhz"]) > int(point["base_mhz"])
        for point in plan
        if not point.get("preserve_base") and int(point["voltage_mv"]) < 1000
    )


def test_s7_negative_control_demotion_still_fails() -> None:
    # Same low clock as S4, but power is far off the cap and the driver
    # reports no power cap: silent reliability demotion must keep failing.
    curve = rtx_5090_steep_synthetic_curve()
    model = scenario_5090_power_models()[1]
    baseline = settle_operating_point(curve, model=model, power_limit_w=BALANCED_CAP_W)
    demoted_samples = [
        {**sample, "power_w": 350.0, "core_clock_mhz": 1900.0, "perf_cap_reason": "none"}
        for sample in synthesize_busy_samples(
            {**baseline, "power_capped": False}
        )
    ]

    decision = evaluate_loaded_telemetry(
        demoted_samples,
        baseline_power_w=float(baseline["power_w"]),
        baseline_core_clock_mhz=float(baseline["clock_mhz"]),
        power_limit_w=BALANCED_CAP_W,
        thresholds=StabilityThresholds(min_core_clock_pct=94.0),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOW_CLOCK


def test_zotac_capture_matches_crosschecked_anchors() -> None:
    curve = rtx_5090_zotac_amp_stock_curve()
    anchors = {int(p["voltage_mv"]): int(p["base_mhz"]) for p in curve}

    assert len(curve) == 127
    assert anchors[850] == 1320
    assert anchors[900] == 2002
    assert anchors[950] == 2602
    assert anchors[1000] == 2767
    assert anchors[1240] == 3195
    # The structural signature: cliff below the knee, shelf above it.
    assert (anchors[950] - anchors[900]) / 50 > 10.0
    assert (anchors[1000] - anchors[950]) / 50 < 4.0
