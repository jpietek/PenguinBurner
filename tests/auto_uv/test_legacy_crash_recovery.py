from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from auto_uv_test_data import base_curve

from auto_uv import main_loop as main
from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.domain.types import AutoUvError, AutoUvVoltageScanResult
from auto_uv.persistence.scan_checkpoint import ScanCheckpoint
from auto_uv.persistence.unsafe_voltage_blacklist_file import record_unsafe_voltage
from auto_uv.run.crash_recovery import recovery_probe_history
from stability.q2rtx.models import Q2RTXStabilityConfig


def saved_candidate(voltage, clock, tier="efficiency"):
    curve = base_curve(800, 925, 25, 1800, 15)
    return {
        "candidate_id": f"{voltage}mv-{clock}mhz",
        "candidate_voltage_mv": voltage,
        "lock_clock_mhz": clock,
        "avg_core_clock_mhz": float(clock),
        "avg_voltage_mv": float(voltage),
        "avg_fps": 100.0,
        "avg_power_w": 250.0,
        "efficiency_fps_per_w": 0.4,
        "base_candidate_voltage_mv": 900,
        "base_lock_clock_mhz": 1800,
        "base_avg_core_clock_mhz": 1800.0,
        "configured_power_limit_w": 300,
        "tail_rise_bins": 2,
        "generated_profile_tier": tier,
        "plan": build_flattened_plan(
            curve,
            candidate_voltage_mv=voltage,
            lock_clock_mhz=clock,
            tail_rise_bins=2,
        ),
    }


def fake_gpu():
    return SimpleNamespace(
        runtime_default_plan=base_curve(800, 925, 25, 1800, 15),
        power_limit_w=300,
        reader=object(),
        live_voltage_reader=object(),
        clock_ceiling=None,
        translated_gpu_policy={
            "gpu_name": "NVIDIA GeForce RTX 3080",
            "power_limit_w": 300,
        },
        gpu_identity={},
        close=lambda: None,
    )


@pytest.mark.parametrize(
    "tier,duration", [("efficiency", 60), ("balanced", 180), ("performance", 300)]
)
@pytest.mark.parametrize("override", [None, 120])
@pytest.mark.parametrize("phase", ["candidate", "final-verify"])
def test_legacy_adaptive_recovery_uses_tier_duration_and_retains_safer_plans(
    monkeypatch,
    tier,
    duration,
    override,
    phase,
):
    monkeypatch.delenv("PENGUIN_BURNER_AUTO_UV_FINAL_SECONDS", raising=False)
    options = {"auto_uv_mode": "adaptive", "auto_uv_require_final_choice": True}
    if override is not None:
        options["auto_uv_final_verification_s"] = override
    records = [
        saved_candidate(875, 1770, tier),
        saved_candidate(850, 1800, tier),
        saved_candidate(825, 1812, tier),
    ]
    interrupted = {
        "candidate_voltage_mv": 818,
        "lock_clock_mhz": 1828,
        "phase": phase,
        "details": {"generated_profile_tier": tier},
    }
    monkeypatch.setattr(main, "consume_crash_cache", lambda **_: [])
    monkeypatch.setattr(main, "crash_recovery_entry_from_cache", lambda _: interrupted)
    monkeypatch.setattr(main, "open_live_gpu_vf_curve_applier", lambda **_: fake_gpu())
    monkeypatch.setattr(
        main, "cleanup_managed_q2rtx_processes", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(main, "read_verified_candidates", lambda: records)
    monkeypatch.setattr(
        main,
        "run_discovery_probe",
        lambda *_a, **_kw: pytest.fail("legacy recovery reuses its baseline"),
    )
    selected = records[-1]
    durations, calls, modes = [], [], []

    def choice(**kw):
        durations.append(kw["final_verification_duration_s"])
        return (
            selected["plan"],
            825,
            1812,
            None,
            kw["final_verification_duration_s"],
            2,
            selected,
        )

    monkeypatch.setattr(main, "choose_recovery_final_verification_candidate", choice)
    monkeypatch.setattr(
        main, "choose_next_final_verification_candidate_after_failure", choice
    )

    def select(**kw):
        modes.append(kw["settings"].auto_uv_mode)
        assert not kw["run_performance_auto_oc"]
        return main.FinalScanCandidate(
            kw["stable_plan"],
            kw["stable_voltage_mv"],
            kw["stable_lock_clock_mhz"],
            kw["stable_probe"],
            kw["final_verification_duration_s"],
            {},
            2,
        )

    def verify(**kw):
        calls.append((kw["stable_voltage_mv"], kw["stable_lock_clock_mhz"]))
        assert kw["auto_uv_mode"] == kw["generated_profile_tier"] == tier
        assert kw["final_verification_duration_s"] == (override or duration)
        assert all(probe.tested_plan for probe in kw["stable_history"])
        if kw["stable_voltage_mv"] == 825:
            raise AutoUvError("final long verification failed: unstable")
        assert kw["stable_plan"] == records[1]["plan"]
        return AutoUvVoltageScanResult(True, 850, 1800, "verified", None, [])

    monkeypatch.setattr(main, "select_final_scan_candidate", select)
    monkeypatch.setattr(main, "run_final_verification_and_save", verify)
    result = main.run_voltage_frequency_undervolt_main_loop(
        gpu_index=0,
        runtime_options=options,
        q2rtx_config=Q2RTXStabilityConfig(),
        log=lambda _: None,
    )

    assert durations == [override or duration]
    assert modes == [tier]
    assert calls == [(825, 1812), (850, 1800)]
    assert result.final_voltage_mv == 850


@pytest.mark.parametrize(
    "mismatch",
    [
        "power",
        "tail",
        "tier",
        "baseline",
        "base_curve",
        "missing_plan",
        "malformed_points",
        "blacklist",
    ],
)
def test_legacy_fallback_excludes_incompatible_or_unsafe_records(mismatch):
    selected = saved_candidate(825, 1812)
    compatible = saved_candidate(850, 1800)
    excluded = saved_candidate(875, 1770)
    if mismatch == "power":
        excluded["configured_power_limit_w"] = 380
    elif mismatch == "tail":
        excluded["tail_rise_bins"] = 4
    elif mismatch == "tier":
        excluded["generated_profile_tier"] = "balanced"
    elif mismatch == "baseline":
        excluded["base_lock_clock_mhz"] = 1900
    elif mismatch == "base_curve":
        excluded["plan"][0]["base_mhz"] += 15
    elif mismatch == "missing_plan":
        del excluded["plan"]
    elif mismatch == "malformed_points":
        excluded["points"] = excluded.pop("plan")
        excluded["points"][0]["target_mhz"] = "invalid"
    else:
        # The higher clock at the lower voltage is also blocked, so use a
        # voltage-specific lower-clock blacklist entry for this test.
        record_unsafe_voltage(
            candidate_voltage_mv=875,
            lock_clock_mhz=1770,
            reason="device lost",
            details={"generated_profile_tier": "balanced"},
        )
        excluded["generated_profile_tier"] = "balanced"
        selected["generated_profile_tier"] = "balanced"
        compatible["generated_profile_tier"] = "balanced"
        compatible["candidate_voltage_mv"] = 900
        compatible["plan"] = saved_candidate(900, 1800, "balanced")["plan"]
    before = deepcopy(compatible)
    history = recovery_probe_history(
        [excluded, compatible],
        selected_record=selected,
        profile_tier=selected["generated_profile_tier"],
    )

    assert [
        (probe.candidate_voltage_mv, probe.lock_clock_mhz) for probe in history
    ] == [(compatible["candidate_voltage_mv"], 1800)]
    assert history[0].tested_plan == compatible["plan"]
    assert compatible == before


def test_rejected_checkpoint_cannot_fall_back_to_legacy_candidates(monkeypatch):
    checkpoint = ScanCheckpoint(
        identity={"gpu": "original"}, callback=None, log=lambda _: None
    )
    checkpoint.record("completed-work", 1)
    gpu = fake_gpu()
    gpu.gpu_identity = {"uuid": "GPU-changed"}
    monkeypatch.setattr(main, "scan_checkpoint_identity", lambda *_: {"gpu": "changed"})
    monkeypatch.setattr(main, "open_live_gpu_vf_curve_applier", lambda **_: gpu)
    monkeypatch.setattr(
        main, "cleanup_managed_q2rtx_processes", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(main, "consume_crash_cache", lambda **_: [])
    monkeypatch.setattr(
        main, "crash_recovery_entry_from_cache", lambda _: {"candidate_voltage_mv": 818}
    )
    monkeypatch.setattr(
        main,
        "read_verified_candidates",
        lambda: pytest.fail("must not bypass rejected checkpoint"),
    )
    monkeypatch.setattr(main, "driver_memory_offset_limit_mhz", lambda _: 0)
    gpu.policy_controller = object()

    def fresh_scan(*_a, **_kw):
        raise AutoUvError("fresh scan setup reached")

    monkeypatch.setattr(main, "apply_adaptive_tier_memory_offset", fresh_scan)
    with pytest.raises(AutoUvError, match="fresh scan setup reached"):
        main.run_voltage_frequency_undervolt_main_loop(
            gpu_index=0,
            runtime_options={
                "auto_uv_mode": "adaptive",
                "auto_uv_require_final_choice": True,
            },
            q2rtx_config=Q2RTXStabilityConfig(),
            log=lambda _: None,
        )
