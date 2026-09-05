from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from stability.q2rtx.models import Q2RTXStabilityConfig

from auto_uv.domain.types import AutoUvProbeSummary, FailureKind, VfCurveCandidate
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
        q2rtx_config=object(),
        runtime_default_plan=[],
        power_limit_w=220,
        start_voltage_mv=1000,
        baseline_clock_mhz=None,
        min_performance_core_clock_pct=90.0,
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
    assert config.duration_s == 10
    assert config.companion_command is None


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
        q2rtx_config=SimpleNamespace(companion_command=None),
        runtime_default_plan=[],
        power_limit_w=220,
        start_voltage_mv=1000,
        baseline_clock_mhz=None,
        min_performance_core_clock_pct=90.0,
        short_probe_base_duration_s=10,
        log=lambda _message: None,
    )

    outcome = runner.outcome_from_probe_result(
        summary,
        result,
        stable_history=[summary],
        q2rtx_config=SimpleNamespace(companion_command=("cuda",)),
    )

    assert outcome.decision.passed is True
    assert outcome.decision.evidence["cuda_required"] is True


def test_probe_runner_uses_original_baseline_clock_for_final_clock_floor() -> None:
    stable_baseline = _summary(1020, 2625, used_companion_load=False)
    current_summary = _summary(1020, 2425, used_companion_load=False)
    result = {
        "success": True,
        "benchmark_summary": _benchmark_summary(1000, 10.0, 100.0),
        "telemetry_samples": [
            {"power_w": 180.0, "core_clock_mhz": 2425.0, "gpu_util_pct": 99.0}
        ],
        "benchmark_telemetry_samples": [
            {"power_w": 180.0, "core_clock_mhz": 2425.0, "gpu_util_pct": 99.0}
        ],
    }
    runner = AutoUvProbeRunner(
        reader=object(),
        live_voltage_reader=object(),
        q2rtx_config=SimpleNamespace(companion_command=None),
        runtime_default_plan=[],
        power_limit_w=360,
        start_voltage_mv=1020,
        baseline_clock_mhz=2745.0,
        min_performance_core_clock_pct=90.0,
        short_probe_base_duration_s=10,
        log=lambda _message: None,
    )

    outcome = runner.outcome_from_probe_result(
        current_summary,
        result,
        stable_history=[stable_baseline],
    )

    assert outcome.decision.passed is False
    assert outcome.decision.failure_kind == FailureKind.LOW_CLOCK
    assert outcome.decision.evidence["floor_mhz"] == 2470.5


def test_probe_runner_can_disable_clock_floor_for_voltage_descent() -> None:
    stable_baseline = _summary(1020, 2625, used_companion_load=False)
    current_summary = _summary(950, 2325, used_companion_load=False)
    result = {
        "success": True,
        "benchmark_summary": _benchmark_summary(1000, 10.0, 100.0),
        "telemetry_samples": [
            {"power_w": 180.0, "core_clock_mhz": 2325.0, "gpu_util_pct": 99.0}
        ],
        "benchmark_telemetry_samples": [
            {"power_w": 180.0, "core_clock_mhz": 2325.0, "gpu_util_pct": 99.0}
        ],
    }
    runner = AutoUvProbeRunner(
        reader=object(),
        live_voltage_reader=object(),
        q2rtx_config=SimpleNamespace(companion_command=None),
        runtime_default_plan=[],
        power_limit_w=360,
        start_voltage_mv=1020,
        baseline_clock_mhz=2745.0,
        min_performance_core_clock_pct=90.0,
        short_probe_base_duration_s=10,
        log=lambda _message: None,
    )

    outcome = runner.outcome_from_probe_result(
        current_summary,
        result,
        stable_history=[stable_baseline],
        enforce_core_clock_floor=False,
    )

    assert outcome.decision.passed is True


def test_probe_sweep_candidate_can_run_without_live_clock_floor(monkeypatch) -> None:
    captured: dict[str, object] = {}
    candidate = VfCurveCandidate("candidate", 965, 2404, [])
    summary = _summary(965, 2325, used_companion_load=True)
    result = {
        "success": True,
        "benchmark_summary": _benchmark_summary(1000, 10.0, 100.0),
        "telemetry_samples": [
            {"power_w": 180.0, "core_clock_mhz": 2325.0, "gpu_util_pct": 99.0}
        ],
        "benchmark_telemetry_samples": [
            {"power_w": 180.0, "core_clock_mhz": 2325.0, "gpu_util_pct": 99.0}
        ],
    }

    def fake_probe_voltage_candidate(**kwargs):
        captured["enforce_target_core_clock_floor"] = kwargs[
            "enforce_target_core_clock_floor"
        ]
        return summary, result

    monkeypatch.setattr(
        "auto_uv.probes.runner.probe_voltage_candidate",
        fake_probe_voltage_candidate,
    )
    runner = AutoUvProbeRunner(
        reader=object(),
        live_voltage_reader=object(),
        q2rtx_config=Q2RTXStabilityConfig(
            companion_command=("cuda",),
            duration_s=10,
        ),
        runtime_default_plan=[],
        power_limit_w=360,
        start_voltage_mv=1020,
        baseline_clock_mhz=2745.0,
        min_performance_core_clock_pct=90.0,
        short_probe_base_duration_s=10,
        log=lambda _message: None,
    )

    outcome = runner.probe_sweep_candidate(
        candidate,
        stable_history=[_summary(1020, 2625, used_companion_load=True)],
        enforce_target_core_clock_floor=False,
    )

    assert captured["enforce_target_core_clock_floor"] is False
    assert outcome.decision.passed is True


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
        min_performance_core_clock_pct=90.0,
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
