from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from stability.q2rtx.models import Q2RTXStabilityConfig

from auto_uv.domain.types import (
    AutoUvCriticalProbeError,
    AutoUvProbeSummary,
    FailureSeverity,
    VfCurveCandidate,
)
from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.probes.runner import (
    AutoUvProbeRunner,
    probe_runner_marker_details,
)
from auto_uv_test_data import base_curve


def test_probe_runner_emits_candidate_table_start_and_result(monkeypatch) -> None:
    from auto_uv.probes import runner as module

    events: list[tuple[str, dict]] = []
    captured: dict[str, object] = {}
    summary = AutoUvProbeSummary(
        candidate_voltage_mv=950,
        lock_clock_mhz=2400,
        live_voltage_before_mv=950,
        live_voltage_after_mv=948,
        avg_voltage_mv=948.0,
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=180.0,
        max_power_w=190.0,
        avg_temperature_c=60.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=36.0,
        avg_core_clock_mhz=2400.0,
        efficiency_fps_per_w=0.56,
        efficiency_mhz_per_w=13.33,
        watts_per_mhz=0.075,
        used_companion_load=False,
        result_reason="ok",
        log_path=Path("/tmp/q2rtx.log"),
    )
    result = {
        "success": True,
        "benchmark_summary": _benchmark_summary(1000, 10.0, 100.0),
        "telemetry_samples": [
            {"power_w": 180.0, "core_clock_mhz": 2400.0, "gpu_util_pct": 99.0}
        ],
        "benchmark_telemetry_samples": [
            {"power_w": 180.0, "core_clock_mhz": 2400.0, "gpu_util_pct": 99.0}
        ],
    }
    monkeypatch.setattr(
        module,
        "q2rtx_cuda_probe_config_for_voltage_band",
        lambda *args, **kwargs: object(),
    )

    def fake_probe_voltage_candidate(**kwargs):
        captured["candidate_plan"] = kwargs["candidate_plan"]
        return summary, result

    monkeypatch.setattr(
        module,
        "probe_voltage_candidate",
        fake_probe_voltage_candidate,
    )

    runner = AutoUvProbeRunner(
        reader=object(),
        live_voltage_reader=object(),
        q2rtx_config=Q2RTXStabilityConfig(),
        runtime_default_plan=[],
        power_limit_w=220,
        start_voltage_mv=1000,
        baseline_clock_mhz=None,
        short_probe_base_duration_s=10,
        log=lambda _message: None,
        event_callback=lambda name, payload: events.append((name, payload)),
    )

    candidate_plan = build_flattened_plan(
        base_curve(900, 1025, 25, 2200, 20),
        lock_clock_mhz=2400,
        candidate_voltage_mv=950,
        tail_rise_bins=2,
    )

    runner.probe_sweep_candidate(
        VfCurveCandidate(
            label="lower-voltage 950mV",
            voltage_mv=950,
            target_mhz=2400,
            flattened_plan=candidate_plan,
        ),
        stable_history=[],
    )

    assert [name for name, _payload in events] == [
        "candidate_curve",
        "probe_start",
        "probe_result",
    ]
    assert events[1][1]["voltage_mv"] == 950
    assert events[2][1]["fps"] == 100.0
    assert events[2][1]["measured_clock_mhz"] == 2400.0
    assert captured["candidate_plan"] is candidate_plan
    by_voltage = {int(point["voltage_mv"]): point for point in candidate_plan}
    assert by_voltage[975]["target_mhz"] == 2415
    assert by_voltage[1000]["target_mhz"] == 2430


def test_probe_runner_marker_details_add_candidate_tier_metadata() -> None:
    candidate = VfCurveCandidate(
        label="candidate",
        voltage_mv=885,
        target_mhz=2880,
        flattened_plan=[],
        metadata={
            "tail_rise_bins": 6,
            "generated_profile_tier": "performance",
            "custom_target": True,
            "auto_oc": True,
        },
    )

    details = probe_runner_marker_details({"auto_uv_mode": "performance"}, candidate)

    assert details["auto_uv_mode"] == "performance"
    assert details["generated_profile_tier"] == "performance"
    assert details["tail_rise_bins"] == 6
    assert details["custom_target"] is True
    assert details["auto_oc"] is True


def test_probe_runner_discovery_doubles_q2rtx_and_skips_cuda(monkeypatch) -> None:
    from auto_uv.probes import runner as module

    captured: dict[str, object] = {}
    summary = _summary(1000, 2500, used_companion_load=False)
    result = _stable_result()

    def fake_probe_voltage_candidate(**kwargs):
        captured["q2rtx_config"] = kwargs["q2rtx_config"]
        return summary, result

    monkeypatch.setattr(module, "probe_voltage_candidate", fake_probe_voltage_candidate)
    runner = _runner(
        q2rtx_config=Q2RTXStabilityConfig(companion_command=("cuda",)),
        short_probe_base_duration_s=10,
    )

    runner.probe_default_curve(
        base_curve=base_curve(900, 1025, 25, 2200, 20),
        label_voltage_mv=1000,
        label_clock_mhz=2500,
    )

    config = captured["q2rtx_config"]
    assert isinstance(config, Q2RTXStabilityConfig)
    assert config.duration_s == 20
    assert config.companion_command is None


def test_probe_runner_baseline_skips_cuda_companion(monkeypatch) -> None:
    from auto_uv.probes import runner as module

    captured: dict[str, object] = {}
    summary = _summary(1000, 2500, used_companion_load=False)
    result = _stable_result()

    def fake_probe_voltage_candidate(**kwargs):
        captured["q2rtx_config"] = kwargs["q2rtx_config"]
        return summary, result

    monkeypatch.setattr(module, "probe_voltage_candidate", fake_probe_voltage_candidate)
    runner = _runner(
        q2rtx_config=Q2RTXStabilityConfig(companion_command=("cuda",)),
        short_probe_base_duration_s=10,
        start_voltage_mv=1000,
    )

    runner.probe_baseline_candidate(
        VfCurveCandidate(
            label="baseline",
            voltage_mv=1000,
            target_mhz=2500,
            flattened_plan=base_curve(900, 1025, 25, 2200, 20),
        )
    )

    config = captured["q2rtx_config"]
    assert isinstance(config, Q2RTXStabilityConfig)
    assert config.duration_s == 10
    assert config.companion_command is None


@pytest.mark.parametrize("reason,critical", [
    ("nvidia-xid-detected: 109", True),
    ("workload-setup-failed", False),
])
def test_baseline_wrapper_aborts_critical_failure_but_returns_recoverable_failure(
    monkeypatch, reason: str, critical: bool,
) -> None:
    from auto_uv.probes import runner as module

    calls: list[dict] = []
    summary = _summary(1000, 2500, used_companion_load=False)
    raw_result = {"success": False, "reason": reason}

    def fake_probe_voltage_candidate(**kwargs):
        calls.append(kwargs)
        return summary, raw_result

    monkeypatch.setattr(module, "probe_voltage_candidate", fake_probe_voltage_candidate)
    runner = _runner(q2rtx_config=Q2RTXStabilityConfig(), short_probe_base_duration_s=10)
    candidate = VfCurveCandidate("baseline", 1000, 2500, base_curve())
    outcome = None

    with pytest.raises(AutoUvCriticalProbeError, match=reason) if critical else nullcontext():
        outcome = runner.probe_baseline_candidate(candidate)

    assert len(calls) == 1
    assert calls[0]["phase_label"] == "baseline"
    assert calls[0]["candidate_plan"] is candidate.flattened_plan
    if not critical:
        assert outcome is not None
        assert not outcome.decision.passed
        assert outcome.decision.severity is FailureSeverity.RECOVERABLE
        assert outcome.decision.reason == reason
        assert outcome.raw_probe is summary
        assert outcome.raw_result is raw_result


def test_probe_runner_evaluates_cuda_from_per_voltage_config() -> None:
    summary = AutoUvProbeSummary(
        candidate_voltage_mv=950,
        lock_clock_mhz=2400,
        live_voltage_before_mv=950,
        live_voltage_after_mv=948,
        avg_voltage_mv=948.0,
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=180.0,
        max_power_w=190.0,
        avg_temperature_c=60.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=36.0,
        avg_core_clock_mhz=2400.0,
        efficiency_fps_per_w=0.56,
        efficiency_mhz_per_w=13.33,
        watts_per_mhz=0.075,
        used_companion_load=True,
        result_reason="ok",
        log_path=Path("/tmp/q2rtx.log"),
    )
    result = {
        "success": True,
        "benchmark_summary": _benchmark_summary(1000, 10.0, 100.0),
        "telemetry_samples": [
            {"power_w": 180.0, "core_clock_mhz": 2400.0, "gpu_util_pct": 99.0}
        ],
        "benchmark_telemetry_samples": [
            {"power_w": 180.0, "core_clock_mhz": 2400.0, "gpu_util_pct": 99.0}
        ],
    }
    runner = AutoUvProbeRunner(
        reader=object(),
        live_voltage_reader=object(),
        q2rtx_config=Q2RTXStabilityConfig(companion_command=None),
        runtime_default_plan=[],
        power_limit_w=220,
        start_voltage_mv=1000,
        baseline_clock_mhz=None,
        short_probe_base_duration_s=10,
        log=lambda _message: None,
    )

    outcome = runner.outcome_from_probe_result(
        summary,
        result,
        stable_history=[summary],
        q2rtx_config=Q2RTXStabilityConfig(companion_command=("cuda",)),
    )

    assert outcome.decision.passed is True
    assert outcome.decision.evidence["cuda_required"] is True


@pytest.mark.parametrize("tail_bins", [0, 2])
@pytest.mark.parametrize("tier", ["efficiency", "balanced", "performance"])
def test_5070_ti_sweep_reaches_auto_oc_after_initial_clock_shortfall(
    monkeypatch, tail_bins, tier,
) -> None:
    from auto_uv.auto_oc.search import run_auto_oc_candidate_search
    from auto_uv.domain.scan_settings import AutoUvScanSettings
    from auto_uv.main_loop import run_adaptive_tier_descent

    curve = base_curve(850, 1051, 5, 2300, 15)
    baseline = _summary(1025, 2737, used_companion_load=True)
    probed = []

    def workload(**kwargs):
        voltage = kwargs["candidate_voltage_mv"]
        target = kwargs["lock_clock_mhz"]
        measured = 2603 if voltage > 925 else target
        probed.append((voltage, target))
        summary = _summary(voltage, measured, used_companion_load=True)
        summary.lock_clock_mhz = target
        summary.avg_power_w = float(voltage - 800)
        summary.efficiency_fps_per_w = 100.0 / summary.avg_power_w
        result = _stable_result()
        for sample in result["telemetry_samples"]:
            sample["core_clock_mhz"] = float(measured)
        return summary, result

    monkeypatch.setattr("auto_uv.probes.runner.probe_voltage_candidate", workload)
    monkeypatch.setattr("auto_uv.auto_oc.search.load_unsafe_voltage_blacklist", list)
    runner = AutoUvProbeRunner(
        reader=object(), live_voltage_reader=object(),
        q2rtx_config=Q2RTXStabilityConfig(companion_command=("cuda",), duration_s=10),
        runtime_default_plan=curve, power_limit_w=300, start_voltage_mv=1025,
        baseline_clock_mhz=2737.0,
        short_probe_base_duration_s=10, log=lambda _: None,
    )

    monkeypatch.setattr("auto_uv.main_loop.write_verified_candidate", lambda *_a, **_k: None)
    monkeypatch.setattr("auto_uv.main_loop.adaptive_tier_descent_tail_rise_bins", lambda _: tail_bins)
    accumulated_unsafe = []
    candidate, _, sweep_probe, _ = run_adaptive_tier_descent(
        curve, tier_mode=tier,
        base_loop_settings=AutoUvScanSettings(
            start_voltage_mv=1025, min_search_voltage_mv=925,
            auto_uv_mode=tier,
            tail_rise_bins=tail_bins,
        ),
        baseline_candidate=VfCurveCandidate("baseline", 1025, 2730, curve),
        initial_stable_outcome=None, fallback_probe=baseline,
        discovery_summary=baseline, accumulated_unsafe=accumulated_unsafe,
        runner=runner, probe_history=[],
        gpu=SimpleNamespace(clock_ceiling=None, power_limit_w=300), log=lambda _: None,
    )

    assert candidate.voltage_mv == 925
    assert len(probed) > 4
    assert all(target == 2730 for _, target in probed)
    assert not accumulated_unsafe
    assert sweep_probe is not None
    climbed = run_auto_oc_candidate_search(
        base_curve=curve, start_candidate=candidate,
        start_probe=sweep_probe, runner=runner,
        gpu_name="RTX 5070 Ti", clock_ceiling=None, probe_history=[],
        log=lambda _: None, tail_rise_bins=tail_bins,
    )
    assert climbed.attempts
    assert climbed.selected_candidate.target_mhz == 2920

    # Verify the final wrapper accepts the clock shortfall but still guards FPS/CUDA.
    from auto_uv.final_verification.main_loop import final_probe_stability_decision

    for fps, reason, expected in [(100, "", True), (89, "", False), (100, "fatal-cuda-output", False)]:
        _, result = workload(candidate_voltage_mv=975, lock_clock_mhz=2730)
        result["benchmark_summary"] = _benchmark_summary(1000, 10.0, fps)
        result.update(success=not reason, reason=reason)
        final = final_probe_stability_decision(
            result, stable_history=[baseline], power_limit_w=300,
            q2rtx_config=runner.q2rtx_config,
        )
        assert final.passed is expected


def _summary(
    voltage_mv: int,
    clock_mhz: int,
    *,
    used_companion_load: bool,
) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=voltage_mv,
        lock_clock_mhz=clock_mhz,
        live_voltage_before_mv=voltage_mv,
        live_voltage_after_mv=voltage_mv,
        avg_voltage_mv=float(voltage_mv),
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=180.0,
        max_power_w=190.0,
        avg_temperature_c=60.0,
        max_temperature_c=62.0,
        avg_fan_speed_pct=35.0,
        max_fan_speed_pct=36.0,
        avg_core_clock_mhz=float(clock_mhz),
        efficiency_fps_per_w=0.56,
        efficiency_mhz_per_w=13.33,
        watts_per_mhz=0.075,
        used_companion_load=bool(used_companion_load),
        result_reason="ok",
        log_path=Path("/tmp/q2rtx.log"),
    )


def _stable_result() -> dict:
    telemetry_samples = [
        {"power_w": 180.0, "core_clock_mhz": 2400.0, "gpu_util_pct": 99.0}
    ]
    return {
        "success": True,
        "benchmark_summary": _benchmark_summary(1000, 10.0, 100.0),
        "telemetry_samples": telemetry_samples,
        "benchmark_telemetry_samples": telemetry_samples,
    }


def _runner(
    *,
    q2rtx_config,
    short_probe_base_duration_s: int,
    start_voltage_mv: int = 1000,
) -> AutoUvProbeRunner:
    return AutoUvProbeRunner(
        reader=object(),
        live_voltage_reader=object(),
        q2rtx_config=q2rtx_config,
        runtime_default_plan=[],
        power_limit_w=220,
        start_voltage_mv=int(start_voltage_mv),
        baseline_clock_mhz=None,
        short_probe_base_duration_s=int(short_probe_base_duration_s),
        log=lambda _message: None,
    )


def _benchmark_summary(frames: int, seconds: float, fps: float) -> dict:
    return {
        "render_frames": int(frames),
        "measured_s": float(seconds),
        "fps_avg": float(fps),
        "fps_min": float(fps),
        "fps_max": float(fps),
        "fps_mean": float(fps),
        "loops": 1,
    }
