from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from auto_uv.persistence.auto_uv_persisted_json_files import probe_in_progress_path
from auto_uv.persistence.interrupted_probe_crash_cache import (
    consume_interrupted_probe_crash_marker,
)
from auto_uv.persistence.unsafe_voltage_blacklist_file import load_unsafe_voltage_blacklist
from auto_uv.persistence.unsafe_voltage_cache import unsafe_voltage_block_reason
from auto_uv.domain.types import AutoUvCriticalProbeError, AutoUvPowerLimitApplyError
from auto_uv.probes import voltage_probe
from auto_uv.probes.voltage_probe import (
    probe_crash_marker_details,
    probe_unsafe_details,
)
from stability.q2rtx.models import Q2RTXStabilityConfig


def test_probe_crash_marker_details_describe_normal_candidate_context() -> None:
    details = probe_crash_marker_details(
        phase_label="candidate",
        candidate_voltage_mv=875,
        lock_clock_mhz=1919,
        stable_history=[],
        initial_target_voltage_mv=987,
        initial_probe_clock_mhz=1933.29,
        used_companion_load=True,
        expected_total_duration_s=None,
        marker_details={"custom": "kept"},
    )

    assert details["phase_label"] == "candidate"
    assert details["candidate_voltage_mv"] == 875
    assert details["lock_clock_mhz"] == 1919
    assert details["start_voltage_mv"] == 987
    assert details["voltage_drop_from_start_pct"] == pytest.approx(11.3475)
    assert details["target_clock_pct_of_baseline"] == pytest.approx(99.2608)
    assert details["used_companion_load"] is True
    assert details["custom"] == "kept"


def test_probe_unsafe_details_keep_marker_tier_context() -> None:
    details = probe_unsafe_details(
        {
            "generated_profile_tier": "performance",
            "tail_rise_bins": 6,
            "phase_label": "candidate",
        },
        {
            "result_reason": "fatal-q2rtx-output",
            "phase_label": "candidate",
        },
    )

    assert details["generated_profile_tier"] == "performance"
    assert details["tail_rise_bins"] == 6
    assert details["result_reason"] == "fatal-q2rtx-output"


@pytest.mark.parametrize("phase,marked", [
    ("candidate", True),
    ("final-verify", True),
    ("efficiency-candidate", True),
    ("balanced-candidate", True),
    ("performance-candidate", True),
    ("baseline", False),
    ("base-baseline", False),
    ("unknown-candidate", False),
])
@pytest.mark.parametrize("reason,start_voltage,voltage,clock", [
    ("stable", 1000, 900, 2400),
    ("stable", 985, 970, 2400),
    ("stable", 985, 970, 2160),
    ("fatal-q2rtx-output", 1000, 900, 2400),
    ("q2rtx-launcher-error", 1000, 900, 2400),
    ("user-stop-requested", 1000, 900, 2400),
])
def test_probe_wrapper_persists_only_supported_candidate_failures(
    monkeypatch, phase: str, marked: bool, reason: str,
    start_voltage: int, voltage: int, clock: int,
) -> None:
    marker_path = probe_in_progress_path()
    captured_marker = None
    result = SimpleNamespace(
        success=reason == "stable", reason=reason, telemetry_samples=[],
        workload_kind="q2rtx", workload_name="Q2RTX", shutdown_mode="completed",
        process_exit_code=0 if reason == "stable" else 1, log_path=None,
    )

    def workload(_config):
        nonlocal captured_marker
        assert marker_path.exists() is marked
        if marked:
            captured_marker = marker_path.read_text()
            marker = json.loads(captured_marker)
            assert marker["phase"] == phase
            assert marker["candidate_voltage_mv"] == voltage
            assert marker["lock_clock_mhz"] == clock
            assert marker["details"]["generated_profile_tier"] == "efficiency"
        return result

    monkeypatch.setattr(voltage_probe, "run_q2rtx_stability_test", workload)
    monkeypatch.setattr(voltage_probe, "log_probe_start", lambda *_a, **_k: None)
    monkeypatch.setattr(voltage_probe, "apply_plan", lambda *_a: None)
    monkeypatch.setattr(voltage_probe, "print_q2rtx_stability_result", lambda *_a: None)
    monkeypatch.setattr(voltage_probe, "summarize_q2rtx_cuda_probe", lambda **_k: SimpleNamespace())
    reader = SimpleNamespace(
        refresh_points=lambda: None,
        editable_core_points=lambda: [{"index": 0, "current_offset_khz": 0}],
    )
    plan = [{"index": 0, "voltage_mv": voltage, "base_mhz": clock,
             "target_mhz": clock, "new_offset_mhz": 0}]
    with pytest.raises(KeyboardInterrupt) if reason == "user-stop-requested" else nullcontext():
        voltage_probe.probe_voltage_candidate(
            reader=reader, candidate_plan=plan, candidate_voltage_mv=voltage,
            lock_clock_mhz=clock, q2rtx_config=Q2RTXStabilityConfig(),
            stable_history=[], initial_probe_clock_mhz=2400,
            initial_target_voltage_mv=start_voltage,
            nvml_session=SimpleNamespace(read_live_voltage_mv=lambda: voltage),
            phase_label=phase, log=lambda _: None,
            marker_details={"generated_profile_tier": "efficiency"},
        )

    assert not marker_path.exists()
    entries = load_unsafe_voltage_blacklist()
    if marked and reason == "fatal-q2rtx-output":
        assert len(entries) == 1
        assert entries[0]["phase"] == phase
        assert entries[0]["candidate_voltage_mv"] == voltage
        assert entries[0]["lock_clock_mhz"] == clock
        assert entries[0]["details"]["result_reason"] == reason
    else:
        assert entries == []

    if marked and reason == "stable":
        # Replay the actual in-flight marker as if the process died before cleanup.
        assert captured_marker is not None
        marker_path.write_text(captured_marker)
        assert consume_interrupted_probe_crash_marker() is not None
        assert not marker_path.exists()
        recovered = load_unsafe_voltage_blacklist()
        assert len(recovered) == 1
        assert recovered[0]["phase"] == phase
        assert recovered[0]["reason"] == "previous-run-abruptly-ended"
        assert unsafe_voltage_block_reason(
            recovered, candidate_voltage_mv=voltage, lock_clock_mhz=clock,
            profile_tier="efficiency",
        )


@pytest.mark.parametrize("write_fails", [False, True])
def test_probe_keeps_crash_evidence_if_readback_or_blacklist_write_fails(monkeypatch, write_fails):
    power_reads = 0

    def capabilities(*, refresh):
        nonlocal power_reads
        power_reads += 1
        if power_reads == 2:
            raise RuntimeError("GPU unavailable after device loss")
        return SimpleNamespace(power=SimpleNamespace(current_w=300, enforced_w=300))

    result = SimpleNamespace(
        success=False, reason="nvidia-xid-detected", telemetry_samples=[],
        workload_kind="q2rtx", workload_name="Q2RTX", shutdown_mode="fatal-q2rtx-output",
        process_exit_code=1, log_path=None,
    )
    monkeypatch.setattr(voltage_probe, "run_q2rtx_stability_test", lambda _config: result)
    monkeypatch.setattr(voltage_probe, "log_probe_start", lambda *_a, **_k: None)
    monkeypatch.setattr(voltage_probe, "apply_plan", lambda *_a: None)
    monkeypatch.setattr(voltage_probe, "print_q2rtx_stability_result", lambda *_a: None)
    if write_fails:
        def fail_write(**_kwargs):
            raise OSError("test disk full")
        monkeypatch.setattr(voltage_probe, "record_unsafe_voltage", fail_write)
    reader = SimpleNamespace(
        capabilities=capabilities, refresh_points=lambda: None,
        editable_core_points=lambda: [{"index": 0, "current_offset_khz": 0}],
    )
    plan = [{"index": 0, "voltage_mv": 850, "base_mhz": 2800,
             "target_mhz": 2800, "new_offset_mhz": 0}]

    with pytest.raises(
        AutoUvCriticalProbeError if write_fails else AutoUvPowerLimitApplyError,
        match="crash marker retained" if write_fails else "GPU unavailable",
    ):
        voltage_probe.probe_voltage_candidate(
            reader=reader, candidate_plan=plan, candidate_voltage_mv=850,
            lock_clock_mhz=2800, q2rtx_config=Q2RTXStabilityConfig(),
            stable_history=[], initial_probe_clock_mhz=2730,
            initial_target_voltage_mv=985, power_limit_w=300,
            nvml_session=SimpleNamespace(read_live_voltage_mv=lambda: 850),
            phase_label="balanced-candidate", log=lambda _: None,
        )

    assert power_reads == (1 if write_fails else 2)
    assert probe_in_progress_path().exists() is write_fails
    entries = load_unsafe_voltage_blacklist()
    if write_fails:
        assert entries == []
        # A later run can consume the real retained marker once persistence works.
        assert consume_interrupted_probe_crash_marker() is not None
        assert not probe_in_progress_path().exists()
        entries = load_unsafe_voltage_blacklist()
        assert entries[0]["reason"] == "previous-run-abruptly-ended"
    else:
        assert entries[0]["details"]["result_reason"] == "nvidia-xid-detected"
    assert len(entries) == 1
    assert unsafe_voltage_block_reason(entries, candidate_voltage_mv=850, lock_clock_mhz=2800)
