from __future__ import annotations

import pytest

from pathlib import Path

from auto_uv.domain.types import FailureKind, FailureSeverity
from stability.q2rtx.cuda_companion import (
    cuda_bruteforce_companion_command,
)
from auto_uv.probes.stability_decision import (
    classify_failed_result,
    evaluate_cuda_companion,
    evaluate_loaded_telemetry,
    evaluate_stable_run,
    sample_is_busy,
)
from auto_uv.probes.stability_decision import StabilityThresholds
from auto_uv_test_data import stable_probe_result


def test_cuda_companion_command_points_to_repo_stability_script() -> None:
    command = cuda_bruteforce_companion_command(gpu_index=0, duration_s=5)

    assert command[1].endswith("/stability/cuda_bruteforce.py")
    assert "/auto_uv/stability/" not in command[1]
    assert Path(command[1]).is_file()


def test_stability_pass_requires_benchmark_telemetry_and_cuda_when_enabled() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(),
        baseline_fps=100.0,
        baseline_power_w=180.0,
        power_limit_w=220,
        cuda_required=True,
        companion_result={"success": True},
    )

    assert decision.passed
    assert decision.failure_kind is FailureKind.NONE


def test_stability_fails_closed_when_benchmark_summary_is_missing() -> None:
    result = stable_probe_result()
    result.pop("benchmark_summary")

    decision = evaluate_stable_run(
        result,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.METRICS_MISSING
    assert decision.severity is FailureSeverity.CRITICAL


def test_stability_uses_benchmark_summary_without_frame_count_check() -> None:
    result = stable_probe_result(frames=1234)
    result["benchmark_summary"] = {
        "render_frames": 1234,
        "demo_frames": 631,
        "measured_s": 30.0,
        "fps_avg": 100.0,
        "fps_min": 92.0,
        "fps_max": 108.0,
        "loops": 4,
    }
    result["benchmark_telemetry_samples"] = result["telemetry_samples"]

    decision = evaluate_stable_run(
        result,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert decision.passed
    assert decision.reason == "stable run"


def test_stability_reports_benchmark_average_fps_regression() -> None:
    result = stable_probe_result()
    result["benchmark_summary"] = {
        "render_frames": 1234,
        "measured_s": 30.0,
        "fps_avg": 89.0,
        "fps_min": 80.0,
        "fps_max": 95.0,
        "loops": 4,
    }
    result["benchmark_telemetry_samples"] = result["telemetry_samples"]

    decision = evaluate_stable_run(
        result,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.FPS_REGRESSION
    assert decision.reason == "benchmark average FPS below floor current=89.00 floor=90.00"


def test_stability_fails_benchmark_summary_with_invalid_metrics() -> None:
    result = stable_probe_result()
    result["benchmark_summary"] = {
        "render_frames": 0,
        "measured_s": 30.0,
        "fps_avg": 100.0,
    }

    decision = evaluate_stable_run(
        result,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.METRICS_INVALID
    assert decision.severity is FailureSeverity.CRITICAL


def test_stability_fails_when_required_cuda_result_is_missing() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(),
        baseline_fps=100.0,
        baseline_power_w=180.0,
        power_limit_w=220,
        cuda_required=True,
        companion_result=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.CUDA_FAILED
    assert decision.severity is FailureSeverity.CRITICAL


def test_stability_treats_nvidia_xid_as_unsafe_even_without_metrics() -> None:
    # Real failed Q2RTX probes can have both fatal output and an Xid, with no
    # completed benchmark or useful telemetry after the device was lost.
    decision = evaluate_stable_run(
        {"success": False, "reason": "fatal-q2rtx-output"},
        baseline_fps=100.0,
        baseline_power_w=180.0,
        power_limit_w=220,
        cuda_required=False,
        fatal_output_found=True,
        xid_found=True,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.NVIDIA_XID
    assert decision.severity is FailureSeverity.UNSAFE


# --- coverage: short-circuit guards in evaluate_stable_run ---


def test_stability_user_stop_is_recoverable() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(),
        baseline_fps=100.0,
        baseline_power_w=180.0,
        power_limit_w=220,
        cuda_required=False,
        stop_requested=True,
        xid_found=True,
        fatal_output_found=True,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.USER_STOP
    assert decision.severity is FailureSeverity.RECOVERABLE
    assert decision.reason == "user stop requested"


def test_stability_fatal_output_is_critical() -> None:
    decision = evaluate_stable_run(
        stable_probe_result(),
        baseline_fps=100.0,
        baseline_power_w=180.0,
        power_limit_w=220,
        cuda_required=False,
        fatal_output_found=True,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.FATAL_OUTPUT
    assert decision.severity is FailureSeverity.CRITICAL
    assert decision.reason == "fatal output pattern detected"


def test_stability_failed_q2rtx_result_is_classified() -> None:
    result = stable_probe_result()
    result["success"] = False
    result["reason"] = "benchmark-timeout"
    result["log_path"] = "/tmp/probe.log"

    decision = evaluate_stable_run(
        result,
        baseline_fps=100.0,
        baseline_power_w=180.0,
        power_limit_w=220,
        cuda_required=False,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.Q2RTX_FAILED
    assert decision.severity is FailureSeverity.CRITICAL
    assert decision.log_path == Path("/tmp/probe.log")


# --- coverage: classify_failed_result branches ---


def test_classify_xid_prefix_is_unsafe_nvidia_xid() -> None:
    decision = classify_failed_result("nvidia-xid-detected", log_path=None)

    assert not decision.passed
    assert decision.failure_kind is FailureKind.NVIDIA_XID
    assert decision.severity is FailureSeverity.UNSAFE


def test_classify_fatal_prefix_is_critical_q2rtx() -> None:
    decision = classify_failed_result("fatal-cuda-output", log_path=None)

    assert not decision.passed
    assert decision.failure_kind is FailureKind.Q2RTX_FAILED
    assert decision.severity is FailureSeverity.CRITICAL


def test_classify_cuda_kernel_failure_is_unsafe() -> None:
    decision = classify_failed_result("cuda kernel launch failed", log_path=None)

    assert not decision.passed
    assert decision.failure_kind is FailureKind.CUDA_FAILED
    assert decision.severity is FailureSeverity.UNSAFE
    assert decision.reason == "cuda kernel launch failed"


@pytest.mark.parametrize(
    "reason,fatal_matches,output",
    [
        ("fatal-q2rtx-output", ["device lost"], ["Vulkan device lost"]),
        ("benchmark-fatal-event", ["VK_ERROR_DEVICE_LOST"], []),
        ("benchmark-crashed-signal:11", [], []),
        ("benchmark-nonzero-exit:139", ["Segmentation fault"], ["core dumped"]),
        ("gpu-hang-watchdog", [], []),
        ("cuda-bruteforce-failed exit=1", [], ["verify verification mismatch idx=123"]),
        ("cuda-bruteforce-failed exit=1", [], ["cuCtxSynchronize failed: an illegal memory access was encountered"]),
        ("cuda-bruteforce-failed exit=-11", [], []),
        ("cuda-bruteforce-failed exit=-6", [], []),
    ],
)
def test_workload_instability_is_unsafe_and_keeps_failure_log(reason, fatal_matches, output):
    decision = evaluate_stable_run(
        {
            "success": False,
            "reason": reason,
            "fatal_output_matches": fatal_matches,
            "output_tail": output,
            "log_path": "/tmp/failed-probe.log",
        },
        baseline_fps=100.0,
        baseline_power_w=180.0,
        power_limit_w=220,
        cuda_required=False,
        fatal_output_found=bool(fatal_matches),
    )

    assert not decision.passed
    assert decision.severity is FailureSeverity.UNSAFE
    assert decision.log_path == Path("/tmp/failed-probe.log")


@pytest.mark.parametrize(
    "reason,output",
    [
        ("fatal-q2rtx-output", ["No game data files detected", "Aborted"]),
        ("fatal-q2rtx-output", ["Error during initialization", "device lost"]),
        ("fatal-q2rtx-output", ["assertion failed", "SIGABRT"]),
        ("q2rtx-launcher-error", ["error while loading shared libraries"]),
        ("benchmark-event-protocol-error", []),
        ("benchmark-summary-missing", []),
        ("cuda-bruteforce-failed exit=1", ["cuInit failed rc=100: no CUDA-capable device is detected"]),
        ("cuda-bruteforce-failed exit=1", ["cuMemAlloc_v2(stress_x) failed: CUDA_ERROR_OUT_OF_MEMORY"]),
        ("cuda-bruteforce-failed exit=1", []),
        ("cuda-bruteforce-failed exit=-15", []),
    ],
)
def test_setup_or_unexplained_workload_failure_stays_critical(reason, output):
    decision = classify_failed_result(reason, log_path=None, output=output)

    assert not decision.passed
    assert decision.severity is FailureSeverity.CRITICAL


def test_classify_unknown_reason_is_recoverable_q2rtx() -> None:
    decision = classify_failed_result("", log_path=None)

    assert not decision.passed
    assert decision.failure_kind is FailureKind.Q2RTX_FAILED
    assert decision.severity is FailureSeverity.RECOVERABLE
    assert decision.reason == "Q2RTX probe failed"


# --- coverage: evaluate_loaded_telemetry failure branches ---


def test_telemetry_samples_missing_is_load_lost() -> None:
    decision = evaluate_loaded_telemetry(
        [],
        baseline_power_w=180.0,
        power_limit_w=220,
        thresholds=StabilityThresholds(),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOAD_LOST
    assert decision.severity is FailureSeverity.CRITICAL
    assert decision.reason == "telemetry samples missing"


def test_telemetry_no_busy_samples_is_load_lost() -> None:
    # Idle: util far below 60%, power far below the 50%-of-baseline floor (90 W).
    decision = evaluate_loaded_telemetry(
        [{"elapsed_s": 6.0, "power_w": 10.0, "core_clock_mhz": 2100.0, "gpu_util_pct": 5.0}],
        baseline_power_w=180.0,
        power_limit_w=220,
        thresholds=StabilityThresholds(),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.LOAD_LOST
    assert decision.reason == "no busy telemetry samples"
    assert decision.evidence["power_floor_w"] == 90.0


def test_telemetry_busy_but_missing_core_clock_is_missing_metrics() -> None:
    # Busy via gpu_util, but no core_clock telemetry on the busy sample.
    decision = evaluate_loaded_telemetry(
        [{"elapsed_s": 6.0, "power_w": 180.0, "core_clock_mhz": None, "gpu_util_pct": 99.0}],
        baseline_power_w=180.0,
        power_limit_w=220,
        thresholds=StabilityThresholds(),
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.METRICS_MISSING
    assert decision.severity is FailureSeverity.RECOVERABLE
    assert decision.reason == "busy core-clock telemetry missing"
    from auto_uv.probes.runtime_guardrails import probe_failure_should_mark_voltage_unsafe

    assert not probe_failure_should_mark_voltage_unsafe(decision.reason)


def test_telemetry_derives_power_floor_when_baseline_power_absent() -> None:
    # baseline_power_w=None forces the derive_active_power_floor_w fallback path.
    # power_limit_w=200 -> floor uses the power-limit floor; sample stays busy via util.
    decision = evaluate_loaded_telemetry(
        [{"elapsed_s": 6.0, "power_w": 180.0, "core_clock_mhz": 2100.0, "gpu_util_pct": 99.0}],
        baseline_power_w=None,
        power_limit_w=200,
        thresholds=StabilityThresholds(),
        log_path=None,
    )

    assert decision.passed
    assert decision.reason == "loaded telemetry stable"


# --- coverage: evaluate_cuda_companion failure branch ---


def test_cuda_companion_unsuccessful_uses_its_reason() -> None:
    decision = evaluate_cuda_companion(
        {"success": False, "reason": "cuda OOM"}, log_path=None
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.CUDA_FAILED
    assert decision.severity is FailureSeverity.CRITICAL
    assert decision.reason == "cuda OOM"


def test_cuda_companion_verification_mismatch_is_unsafe() -> None:
    decision = evaluate_cuda_companion(
        {
            "success": False,
            "reason": "cuda-bruteforce-failed exit=1",
            "output_tail": ["verify verification mismatch idx=123 expected=1 got=2"],
        },
        log_path=None,
    )

    assert not decision.passed
    assert decision.failure_kind is FailureKind.CUDA_FAILED
    assert decision.severity is FailureSeverity.UNSAFE


# --- coverage: sample_is_busy power-floor path ---


def test_sample_is_busy_via_power_when_util_below_threshold() -> None:
    # gpu_util below busy threshold falls through to the power check.
    sample = {"gpu_util_pct": 10.0, "power_w": 150.0}

    assert sample_is_busy(sample, busy_power_floor_w=100.0, busy_gpu_util_pct=60.0)


def test_sample_is_busy_false_when_power_below_floor_and_util_low() -> None:
    sample = {"gpu_util_pct": 10.0, "power_w": 50.0}

    assert not sample_is_busy(sample, busy_power_floor_w=100.0, busy_gpu_util_pct=60.0)


@pytest.mark.parametrize("cap_reason", [None, "sw-power", "sw-reliability", "sw-thermal", "hw-power-brake"])
@pytest.mark.parametrize("fps,expected", [(100.0, True), (89.0, False)])
def test_measured_clock_shortfall_is_judged_by_workload_performance(cap_reason, fps, expected):
    result = stable_probe_result(clock_mhz=1750.0, fps=fps)
    for sample in result["telemetry_samples"]:
        sample["perf_cap_reason"] = cap_reason
    decision = evaluate_stable_run(
        result, baseline_fps=100.0, baseline_power_w=180.0,
        power_limit_w=220, cuda_required=False,
    )
    assert decision.passed is expected
    assert decision.failure_kind is (FailureKind.NONE if expected else FailureKind.FPS_REGRESSION)
