from __future__ import annotations

import json
from pathlib import Path

from stability.q2rtx.long_stability_config import (
    build_long_stability_test_config,
    long_stability_workload_durations,
)
from stability.q2rtx.models import Q2RTXStabilityConfig

from auto_uv.domain.types import AutoUvProbeSummary
from auto_uv.final_verification import fan_curve, result_files
from auto_uv.final_verification.main_loop import (
    choose_profile_metrics_probe,
    final_candidate,
)
from auto_uv.persistence import auto_uv_persisted_json_files as persisted_files
from auto_uv_test_data import wide_base_curve


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
    q2rtx_s, cuda_s = long_stability_workload_durations(300)

    assert (q2rtx_s, cuda_s) == (225, 75)


def test_stop_request_abort_final_choice_marker(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(persisted_files, "auto_uv_user_config_dir", lambda: tmp_path)
    stop_path = persisted_files.auto_uv_stop_request_path()

    assert persisted_files.auto_uv_stop_request_aborts_final_choice() is False

    stop_path.write_text(
        "stop requested by PenguinBurner daemon client\nreason=offer-final-choice\n",
        encoding="utf-8",
    )
    assert persisted_files.auto_uv_stop_request_aborts_final_choice() is False

    stop_path.write_text(
        "stop requested by PenguinBurner daemon client\nreason=abort-final-choice\n",
        encoding="utf-8",
    )
    assert persisted_files.auto_uv_stop_request_aborts_final_choice() is True


def test_final_probe_config_adds_cuda_and_long_timeout() -> None:
    config = build_long_stability_test_config(
        Q2RTXStabilityConfig(gpu_index=2, single_pass_timeout_s=9999.0),
        total_duration_s=300,
    )

    assert config.companion_command is not None
    assert "--gpu-index" in config.companion_command
    assert "2" in config.companion_command
    assert "--duration-seconds" in config.companion_command
    assert "75" in config.companion_command
    assert config.duration_s == 225
    assert config.single_pass_timeout_s == 360.0


def test_final_verification_candidate_uses_plain_label() -> None:
    candidate = final_candidate(
        plan=wide_base_curve(),
        voltage_mv=925,
        lock_clock_mhz=2910,
    )

    assert candidate.label == "final-verify 925mV"
    assert candidate.metadata == {}


def test_final_verification_candidate_carries_auto_oc_metadata() -> None:
    candidate = final_candidate(
        plan=wide_base_curve(),
        voltage_mv=915,
        lock_clock_mhz=2745,
        metadata={
            "auto_oc": True,
            "auto_oc_applied_mhz": 145,
            "auto_oc_limit_mhz": 380,
        },
    )

    assert candidate.metadata["auto_oc"] is True
    assert candidate.metadata["auto_oc_applied_mhz"] == 145
    assert candidate.metadata["auto_oc_limit_mhz"] == 380


def test_profile_metrics_keep_matching_short_probe() -> None:
    short_probe = _summary(voltage_mv=925, clock_mhz=2830)
    long_probe = _summary(voltage_mv=925, clock_mhz=2888)

    assert choose_profile_metrics_probe(
        stable_probe=short_probe,
        final_probe=long_probe,
        final_voltage_mv=925,
        final_lock_clock_mhz=2830,
    ) is short_probe


def test_final_fan_curve_blocks_when_final_load_is_too_hot() -> None:
    payload = fan_curve.build_final_verification_fan_curve_payload(
        final_probe=_summary(temp_c=81.0),
        probes=[_summary(temp_c=81.0)],
    )

    assert payload is not None
    assert payload["fan_curve_blocked"] is True
    assert payload["block_reason"] == "base-load-temperature-too-high"


def test_final_fan_curve_is_more_aggressive_from_75_to_80c() -> None:
    payload = fan_curve.build_final_verification_fan_curve_payload(
        final_probe=_summary(temp_c=76.0, fan_pct=47.0),
        probes=[_summary(temp_c=76.0, fan_pct=47.0)],
    )

    assert payload is not None
    assert payload.get("fan_curve_blocked") is not True
    curve = payload["fan"]["curve"]
    hot_points = [point for point in curve if 75.0 <= point[0] <= 90.0]
    assert hot_points
    assert hot_points[-4:] == [
        [76.0, 52.0],
        [80.0, 60.0],
        [85.0, 75.0],
        [90.0, 100.0],
    ]


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
        probe=_summary(clock_mhz=2830),
        final_verification_probe=_summary(clock_mhz=2888),
        q2rtx_resolution="1080p",
        base_probe=_summary(voltage_mv=1025, clock_mhz=2754),
        fan_curve_payload={"fan": {"curve": [[45.0, 0.0], [90.0, 100.0]]}},
        memory_offset_mhz=500,
        power_limit_w=360,
        tail_rise_bins=2,
        gpu_identity={
            "name": "NVIDIA RTX 5090",
            "uuid": "GPU-final-a",
            "pci_bus_id": "00000000:01:00.0",
            "pci_device_id": "0x2B8510DE",
            "index": 0,
        },
    )
    payload = json.loads(profile_path.read_text(encoding="utf-8"))

    assert profile_path.parent == tmp_path / "auto-uv-profiles"
    assert payload["q2rtx_resolution"] == "1080p"
    assert payload["final_verified"] is True
    assert payload["memory_offset_mhz"] == 500
    assert payload["power_limit_w"] == 360
    assert payload["tail_rise_bins"] == 2
    assert payload["avg_core_clock_mhz"] == 2888.0
    assert payload["final_q2rtx_avg_core_clock_mhz"] == 2888.0
    assert payload["final_verification_metrics"] is True
    assert payload["flatten_target"]["tail_rise_bins"] == 2
    assert payload["fan_curve_payload"]["fan"]["curve"][-1] == [90.0, 100.0]
    assert payload["gpu_identity"] == {
        "name": "NVIDIA RTX 5090",
        "uuid": "GPU-final-a",
        "pci_bus_id": "00000000:01:00.0",
        "pci_device_id": "0x2B8510DE",
        "index_at_verification": 0,
    }
    assert (tmp_path / "uv-result" / "auto-uv-verified-candidates.json").exists()


def test_verified_candidate_payload_rejects_malformed_plan_point() -> None:
    """A malformed plan point must fail as a clean AutoUvError (caught by the
    scan loop, GPU restored) instead of a bare KeyError/TypeError traceback."""
    import pytest

    from auto_uv.domain.types import AutoUvError
    from auto_uv.persistence.verified_candidate_result_file import (
        verified_candidate_payload,
    )

    good = {
        "index": 12,
        "voltage_mv": 900,
        "base_mhz": 2500,
        "target_mhz": 2700,
        "new_offset_mhz": 200,
    }
    # Sanity: a well-formed plan assembles.
    payload = verified_candidate_payload(
        plan=[good],
        lock_clock_mhz=2700,
        voltage_mv=900,
        probe=None,
        reason="r",
        label="l",
    )
    assert payload["lock_clock_mhz"] == 2700

    for bad in (
        {**good, "target_mhz": None},
        {**good, "voltage_mv": "n/a"},
        {k: v for k, v in good.items() if k != "base_mhz"},
        "not-a-dict",
    ):
        with pytest.raises(AutoUvError):
            verified_candidate_payload(
                plan=[bad],
                lock_clock_mhz=2700,
                voltage_mv=900,
                probe=None,
                reason="r",
                label="l",
            )

    with pytest.raises(AutoUvError):
        verified_candidate_payload(
            plan=[], lock_clock_mhz=2700, voltage_mv=900,
            probe=None, reason="r", label="l",
        )


def test_verified_candidate_payload_records_power_regime() -> None:
    from auto_uv.persistence.verified_candidate_result_file import (
        verified_candidate_payload,
    )

    point = {
        "index": 12,
        "voltage_mv": 900,
        "base_mhz": 2500,
        "target_mhz": 2700,
        "new_offset_mhz": 200,
    }
    capped = verified_candidate_payload(
        plan=[point],
        lock_clock_mhz=2700,
        voltage_mv=900,
        probe=None,
        reason="r",
        label="l",
        configured_power_limit_w=503,
    )
    unrecorded = verified_candidate_payload(
        plan=[point],
        lock_clock_mhz=2700,
        voltage_mv=900,
        probe=None,
        reason="r",
        label="l",
    )

    # Crash recovery reads this to re-establish the measurement regime; the
    # explicit None distinguishes "no cap" from a pre-regime cache entry
    # only by absence of the key in truly old files.
    assert capped["configured_power_limit_w"] == 503
    assert unrecorded["configured_power_limit_w"] is None
