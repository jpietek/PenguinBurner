from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from stability.q2rtx import Q2RTXStabilityConfig

from auto_uv3.auto_uv_types import AutoUvProbeSummary
from auto_uv3.final_verification import (
    final_verification_fan_curve as fan_curve,
)
from auto_uv3.final_verification import final_verification_result_files as result_files
from auto_uv3.final_verification.final_verification_clock_recovery import (
    build_final_clock_recovery_candidate,
)
from auto_uv3.final_verification.final_verification_probe_config import (
    final_q2rtx_cuda_duration_s,
    final_q2rtx_cuda_probe_config,
)
from auto_uv3.persistence import auto_uv_persisted_json_files as persisted_files
from auto_uv3.recovery import final_failure_upward_stabilization as upward_stabilization
from auto_uv3_test_data import wide_base_curve


def _summary(
    voltage_mv: int = 950,
    clock_mhz: int = 2550,
    *,
    temp_c: float = 62.0,
    fan_pct: float = 35.0,
) -> AutoUvProbeSummary:
    return AutoUvProbeSummary(
        candidate_voltage_mv=int(voltage_mv),
        lock_clock_mhz=int(clock_mhz),
        live_voltage_before_mv=int(voltage_mv),
        live_voltage_after_mv=int(voltage_mv),
        avg_voltage_mv=float(voltage_mv),
        frames_per_run=1000,
        avg_seconds_per_run=10.0,
        avg_fps=100.0,
        min_fps=100.0,
        max_fps=100.0,
        avg_power_w=200.0,
        max_power_w=210.0,
        avg_temperature_c=float(temp_c),
        max_temperature_c=float(temp_c),
        avg_fan_speed_pct=float(fan_pct),
        max_fan_speed_pct=float(fan_pct),
        avg_core_clock_mhz=float(clock_mhz),
        efficiency_fps_per_w=0.5,
        efficiency_mhz_per_w=10.0,
        watts_per_mhz=0.1,
        used_companion_load=True,
        result_reason="stable run",
        log_path=Path("/tmp/q2rtx.log"),
    )


def test_final_probe_duration_split_keeps_cuda_inside_total_budget() -> None:
    q2rtx_s, cuda_s = final_q2rtx_cuda_duration_s(600)

    assert (q2rtx_s, cuda_s) == (450, 150)


def test_final_probe_config_adds_cuda_and_long_timeout() -> None:
    config = final_q2rtx_cuda_probe_config(
        Q2RTXStabilityConfig(gpu_index=2, single_pass_timeout_s=10.0),
        total_duration_s=600,
    )

    assert config.companion_command is not None
    assert "--gpu-index" in config.companion_command
    assert "2" in config.companion_command
    assert "--duration-seconds" in config.companion_command
    assert "150" in config.companion_command
    assert config.single_pass_timeout_s >= 660.0


def test_final_clock_recovery_uses_same_budget_units_as_lower_sweep() -> None:
    recovery = build_final_clock_recovery_candidate(
        wide_base_curve(),
        voltage_mv=935,
        previous_target_mhz=2550,
        measured_target_mhz=2504,
        baseline_clock_mhz=2754.0,
        max_clock_drop_pct=10.0,
        current_budget_used_pct=2.16,
        budget_limit_pct=2.40,
        clock_cap_mhz=2754.0,
        reason="average busy core clock below floor",
    )

    assert recovery is not None
    assert recovery.candidate.target_mhz > 2550
    assert recovery.budget_used_pct == 2.40
    assert recovery.marker_details["previous_target_clock_mhz"] == 2550


def test_final_upward_voltage_recovery_respects_performance_voltage_ceiling(
    monkeypatch,
) -> None:
    probed: list[int] = []

    def fake_probe_voltage_candidate(**kwargs):
        voltage_mv = int(kwargs["candidate_voltage_mv"])
        probed.append(voltage_mv)
        return _summary(voltage_mv=voltage_mv, clock_mhz=2550), SimpleNamespace(
            success=True,
            reason="stable run",
        )

    monkeypatch.setattr(
        upward_stabilization,
        "probe_voltage_candidate",
        fake_probe_voltage_candidate,
    )
    monkeypatch.setattr(
        upward_stabilization,
        "final_recovery_probe_passed",
        lambda *_args, **_kwargs: True,
    )

    candidate, summary, result = upward_stabilization.find_upward_stable_final_candidate(
        reader=object(),
        plan_source=wide_base_curve(),
        failure_voltage_mv=900,
        failure_live_voltage_mv=None,
        minimum_candidate_voltage_mv=925,
        target_clock_mhz=2550,
        q2rtx_config=Q2RTXStabilityConfig(gpu_index=0, duration_s=10),
        stable_history=[_summary(voltage_mv=1025, clock_mhz=2754)],
        nvml_session=None,
        clock_ceiling=None,
        log=lambda _message: None,
        probe_history=[],
        baseline_probe=None,
        initial_target_voltage_mv=1025,
        initial_probe_clock_mhz=2754.0,
        power_limit_w=None,
        min_performance_core_clock_pct=90.0,
        max_candidate_voltage_mv=925,
        short_probe_base_duration_s=20,
    )

    assert probed == [925]
    assert candidate is not None
    assert candidate.candidate_voltage_mv == 925
    assert summary is not None
    assert result is not None


def test_final_fan_curve_blocks_when_final_load_is_too_hot() -> None:
    payload = fan_curve.build_final_verification_fan_curve_payload(
        final_probe=_summary(temp_c=80.0),
        probes=[_summary(temp_c=80.0)],
    )

    assert payload is not None
    assert payload["fan_curve_blocked"] is True
    assert payload["block_reason"] == "base-load-temperature-too-high"


def test_final_fan_curve_keeps_runtime_curve_fields() -> None:
    payload = fan_curve.build_final_verification_fan_curve_payload(
        final_probe=_summary(temp_c=62.0, fan_pct=38.0),
        probes=[_summary(temp_c=62.0, fan_pct=38.0)],
    )

    assert payload is not None
    assert payload["fan"]["curve"][0] == [45.0, 0.0]
    assert payload["fan"]["curve"][-1] == [90.0, 100.0]
    assert payload["fan"]["curve_source"] == "auto-uv"
    assert payload["telemetry"]["measured_fan_points"]


def test_final_verified_profile_contains_fan_payload_and_memory_offset(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(result_files, "auto_uv_user_config_dir", lambda: tmp_path)
    monkeypatch.setattr(persisted_files, "auto_uv_user_config_dir", lambda: tmp_path)
    plan = wide_base_curve()

    profile_path = result_files.write_final_verified_profile(
        plan=plan,
        lock_clock_mhz=2550,
        voltage_mv=935,
        probe=_summary(),
        base_probe=_summary(voltage_mv=1025, clock_mhz=2754),
        fan_curve_payload={"fan": {"curve": [[45.0, 0.0], [90.0, 100.0]]}},
        memory_offset_mhz=500,
    )
    payload = json.loads(profile_path.read_text(encoding="utf-8"))

    assert profile_path.parent == tmp_path / "auto-uv-profiles"
    assert payload["final_verified"] is True
    assert payload["memory_offset_mhz"] == 500
    assert payload["fan_curve_payload"]["fan"]["curve"][-1] == [90.0, 100.0]
    assert (tmp_path / "uv-result" / "auto-uv-verified-candidates.json").exists()
