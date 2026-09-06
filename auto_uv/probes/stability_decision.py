"""Convert generic stability results into Auto-UV candidate decisions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, cast

from auto_uv.domain.types import FailureKind, FailureSeverity, StableRunDecision
from ..curve.base_load_telemetry import derive_active_power_floor_w
from ..shared.probe_data_fields import percent as _percent
from ..shared.probe_data_fields import read_field
from .summary import (
    count_hw_power_brake_samples,
)


@dataclass(frozen=True, slots=True)
class StabilityThresholds:
    min_average_fps_pct: float = 90.0
    min_power_pct: float = 50.0
    busy_gpu_util_pct: float = 60.0


FATAL_REASON_PREFIXES = (
    "benchmark-crashed-signal",
    "benchmark-duration-short",
    "benchmark-event-pipe-closed",
    "benchmark-event-protocol-error",
    "benchmark-fatal-event",
    "benchmark-measure-start-missing",
    "benchmark-metrics-invalid",
    "benchmark-nonzero-exit",
    "benchmark-start-missing",
    "benchmark-summary-missing",
    "benchmark-timeout",
    "fatal-cuda-output",
    "fatal-q2rtx-output",
    "gpu-hang-watchdog",
    "nvidia-xid-detected",
    "q2rtx-selected-nvidia-gpu-idle",
    "q2rtx-launcher-error",
)

# A failed workload can identify an unsafe setting without making the backend
# unusable. The next candidate still has to pass the normal reset/readback path.
SETUP_FAILURE_PATTERNS = (
    "error during initialization",
    "failed to create vulkan",
    "failed to create swapchain",
    "no game data files",
    "couldn't read demo",
    "couldn't load maps/",
    "couldn't load pics/",
    "couldn't open demos/",
    "couldn't open pics/",
    "out of memory",
    "out_of_memory",
    " oom",
    "malloc() failed",
    "couldn't allocate",
    "cuinit failed",
    "cumemalloc",
    "assertion",
    "error while loading shared libraries",
)
INSTABILITY_OUTPUT_PATTERNS = (
    "device lost",
    "vk_error_device_lost",
    "segmentation fault",
    "core dumped",
    "aborted",
    "bus error",
    "illegal instruction",
    "floating point exception",
    "trace/breakpoint trap",
    "sigsegv",
    "sigabrt",
    "sigbus",
    "sigill",
    "sigfpe",
    "sigtrap",
    "sigkill",
    "illegal memory access",
    "unspecified launch failure",
    "launch timeout",
    "kernel launch failed",
    "culaunchkernel failed",
    "verification mismatch",
)


def _failed_workload_severity(reason: str, output: Iterable[str] = ()) -> FailureSeverity:
    if str(reason).startswith(("nvidia-xid", "gpu-hang")):
        return FailureSeverity.UNSAFE
    text = "\n".join([str(reason), *map(str, output)]).lower()
    if any(pattern in text for pattern in SETUP_FAILURE_PATTERNS):
        return FailureSeverity.CRITICAL
    # CUDA reports subprocess return codes rather than Q2RTX's signal names.
    # SIGTERM (-15), used for controlled cleanup, is deliberately excluded.
    if reason.startswith("cuda-bruteforce-failed exit=") and reason.rpartition("=")[2] in {
        "-4", "-5", "-6", "-7", "-8", "-9", "-11",
    }:
        return FailureSeverity.UNSAFE
    if str(reason).startswith("benchmark-crashed-signal") or any(
        pattern in text for pattern in INSTABILITY_OUTPUT_PATTERNS
    ):
        return FailureSeverity.UNSAFE
    return FailureSeverity.CRITICAL


def evaluate_stable_run(
    result: Any,
    *,
    baseline_fps: float | None,
    baseline_power_w: float | None,
    power_limit_w: int | None,
    cuda_required: bool,
    companion_result: Any | None = None,
    fatal_output_found: bool = False,
    xid_found: bool = False,
    stop_requested: bool = False,
    thresholds: StabilityThresholds = StabilityThresholds(),
) -> StableRunDecision:
    """Return the strict pass/fail decision for one Auto-UV stability probe."""

    if stop_requested:
        return _fail(
            FailureKind.USER_STOP,
            FailureSeverity.RECOVERABLE,
            "user stop requested",
        )
    if xid_found:
        return _fail(
            FailureKind.NVIDIA_XID,
            FailureSeverity.UNSAFE,
            "NVIDIA Xid detected after probe launch",
        )
    reason = str(read_field(result, "reason") or "")
    log_path = _path_or_none(read_field(result, "log_path"))
    output = [
        *(read_field(result, "fatal_output_matches") or []),
        *(read_field(result, "output_tail") or []),
    ]
    if fatal_output_found:
        return _fail(
            FailureKind.FATAL_OUTPUT,
            _failed_workload_severity(reason, output),
            "fatal output pattern detected",
            log_path=log_path,
        )

    # Q2RTX success alone is not enough, but Q2RTX failure always invalidates the probe.
    if not bool(read_field(result, "success")):
        return classify_failed_result(reason, log_path=log_path, output=output)

    benchmark_summary = read_field(result, "benchmark_summary")
    if benchmark_summary is not None:
        metrics_decision = evaluate_benchmark_summary(
            benchmark_summary,
            baseline_fps=baseline_fps,
            thresholds=thresholds,
            log_path=log_path,
        )
    else:
        metrics_decision = _fail(
            FailureKind.METRICS_MISSING,
            FailureSeverity.CRITICAL,
            "benchmark summary missing",
            log_path=log_path,
        )
    if not metrics_decision.passed:
        return metrics_decision

    # Telemetry proves Q2RTX was under real load.
    telemetry_decision = evaluate_loaded_telemetry(
        _measurement_telemetry_samples(result),
        baseline_power_w=baseline_power_w,
        power_limit_w=power_limit_w,
        thresholds=thresholds,
        log_path=log_path,
    )
    if not telemetry_decision.passed:
        return telemetry_decision

    # CUDA is part of the workload contract when enabled; a CUDA failure fails the point.
    if cuda_required:
        cuda_decision = evaluate_cuda_companion(
            companion_result,
            log_path=log_path,
        )
        if not cuda_decision.passed:
            return cuda_decision

    stable_reason = (
        telemetry_decision.reason
        if "hw-power-brake=" in str(telemetry_decision.reason)
        else "stable run"
    )
    return StableRunDecision(
        passed=True,
        failure_kind=FailureKind.NONE,
        severity=FailureSeverity.PASS,
        reason=stable_reason,
        evidence={
            "telemetry_samples": len(
                _measurement_telemetry_samples(result)
            ),
            "cuda_required": bool(cuda_required),
            "benchmark_summary": bool(benchmark_summary is not None),
        },
        log_path=log_path,
    )


def classify_failed_result(
    reason: str,
    *,
    log_path: Path | None,
    output: Iterable[str] = (),
) -> StableRunDecision:
    text = str(reason or "")
    if text.startswith(FATAL_REASON_PREFIXES):
        if text.startswith("nvidia-xid"):
            kind = FailureKind.NVIDIA_XID
        elif text.startswith("gpu-hang"):
            kind = FailureKind.GPU_HANG
        else:
            kind = FailureKind.Q2RTX_FAILED
        return _fail(
            kind,
            _failed_workload_severity(text, output),
            text or "critical Q2RTX failure",
            log_path=log_path,
        )
    if text.startswith("cuda"):
        return _fail(
            FailureKind.CUDA_FAILED,
            _failed_workload_severity(text, output),
            text,
            log_path=log_path,
        )
    return _fail(
        FailureKind.Q2RTX_FAILED,
        FailureSeverity.RECOVERABLE,
        text or "Q2RTX probe failed",
        log_path=log_path,
    )


def evaluate_benchmark_summary(
    benchmark_summary: Any,
    *,
    baseline_fps: float | None,
    thresholds: StabilityThresholds,
    log_path: Path | None,
) -> StableRunDecision:
    render_frames = read_field(benchmark_summary, "render_frames")
    measured_s = read_field(benchmark_summary, "measured_s")
    fps_avg = read_field(benchmark_summary, "fps_avg")
    fps_min = read_field(benchmark_summary, "fps_min")
    fps_max = read_field(benchmark_summary, "fps_max")
    fps_mean = read_field(benchmark_summary, "fps_mean")
    if render_frames is None or measured_s is None or fps_avg is None:
        return _fail(
            FailureKind.METRICS_MISSING,
            FailureSeverity.CRITICAL,
            "benchmark summary missing render_frames/measured_s/fps_avg",
            evidence=_benchmark_evidence(benchmark_summary),
            log_path=log_path,
        )
    if (
        int(render_frames) <= 0
        or float(measured_s) <= 0.0
        or float(fps_avg) <= 0.0
    ):
        return _fail(
            FailureKind.METRICS_INVALID,
            FailureSeverity.CRITICAL,
            "benchmark summary has non-positive metrics",
            evidence=_benchmark_evidence(benchmark_summary),
            log_path=log_path,
        )
    for optional_name, optional_value in (
        ("fps_min", fps_min),
        ("fps_max", fps_max),
        ("fps_mean", fps_mean),
    ):
        if optional_value is not None and float(optional_value) <= 0.0:
            return _fail(
                FailureKind.METRICS_INVALID,
                FailureSeverity.CRITICAL,
                f"benchmark summary has non-positive {optional_name}",
                evidence=_benchmark_evidence(benchmark_summary),
                log_path=log_path,
            )

    average_fps_floor = (
        float(baseline_fps) * _percent(thresholds.min_average_fps_pct)
        if baseline_fps is not None
        else None
    )
    if average_fps_floor is not None and float(fps_avg) < float(average_fps_floor):
        return _fail(
            FailureKind.FPS_REGRESSION,
            FailureSeverity.RECOVERABLE,
            (
                f"benchmark average FPS below floor current={float(fps_avg):.2f} "
                f"floor={float(average_fps_floor):.2f}"
            ),
            evidence={
                "average_fps": float(fps_avg),
                "floor_fps": float(average_fps_floor),
                "render_frames": int(render_frames),
                "measured_s": float(measured_s),
            },
            log_path=log_path,
        )
    return _pass("benchmark metrics stable", log_path=log_path)


def evaluate_loaded_telemetry(
    telemetry_samples: list[Any],
    *,
    baseline_power_w: float | None,
    power_limit_w: int | None,
    thresholds: StabilityThresholds,
    log_path: Path | None,
) -> StableRunDecision:
    if not telemetry_samples:
        return _fail(
            FailureKind.LOAD_LOST,
            FailureSeverity.CRITICAL,
            "telemetry samples missing",
            log_path=log_path,
        )
    power_floor_w = (
        float(baseline_power_w) * _percent(thresholds.min_power_pct)
        if baseline_power_w is not None
        else None
    )
    if power_floor_w is None:
        power_floor_w = derive_active_power_floor_w(
            telemetry_samples,
            power_limit_w=power_limit_w,
            use_power_limit_floor=power_limit_w is not None,
        )
    busy_samples = [
        sample
        for sample in telemetry_samples
        if sample_is_busy(
            sample,
            busy_power_floor_w=power_floor_w,
            busy_gpu_util_pct=float(thresholds.busy_gpu_util_pct),
        )
    ]
    if not busy_samples:
        return _fail(
            FailureKind.LOAD_LOST,
            FailureSeverity.CRITICAL,
            "no busy telemetry samples",
            evidence={"power_floor_w": power_floor_w},
            log_path=log_path,
        )
    brake_samples = count_hw_power_brake_samples(busy_samples)
    brake_note = (
        f" hw-power-brake={brake_samples}/{len(busy_samples)}"
        if brake_samples
        else ""
    )
    if not any(read_field(sample, "core_clock_mhz") is not None for sample in busy_samples):
        return _fail(
            FailureKind.METRICS_MISSING,
            FailureSeverity.RECOVERABLE,
            "busy core-clock telemetry missing",
            log_path=log_path,
        )
    return _pass(f"loaded telemetry stable{brake_note}", log_path=log_path)


def evaluate_cuda_companion(
    companion_result: Any | None,
    *,
    log_path: Path | None,
) -> StableRunDecision:
    if companion_result is None:
        return _fail(
            FailureKind.CUDA_FAILED,
            FailureSeverity.CRITICAL,
            "CUDA companion result missing",
            log_path=log_path,
        )
    if not bool(read_field(companion_result, "success")):
        reason = str(read_field(companion_result, "reason") or "CUDA companion failed")
        return _fail(
            FailureKind.CUDA_FAILED,
            _failed_workload_severity(reason, read_field(companion_result, "output_tail") or []),
            reason,
            log_path=log_path,
        )
    return _pass("CUDA companion stable", log_path=log_path)


def sample_is_busy(
    sample: Any,
    *,
    busy_power_floor_w: float | None,
    busy_gpu_util_pct: float,
) -> bool:
    gpu_util_pct = read_field(sample, "gpu_util_pct")
    if gpu_util_pct is not None and float(gpu_util_pct) >= float(busy_gpu_util_pct):
        return True
    power_w = read_field(sample, "power_w")
    return (
        power_w is not None
        and busy_power_floor_w is not None
        and float(power_w) >= float(busy_power_floor_w)
    )


def _pass(reason: str, *, log_path: Path | None) -> StableRunDecision:
    return StableRunDecision(
        passed=True,
        failure_kind=FailureKind.NONE,
        severity=FailureSeverity.PASS,
        reason=str(reason),
        log_path=log_path,
    )


def _fail(
    kind: FailureKind,
    severity: FailureSeverity,
    reason: str,
    *,
    evidence: dict[str, Any] | None = None,
    log_path: Path | None = None,
) -> StableRunDecision:
    return StableRunDecision(
        passed=False,
        failure_kind=kind,
        severity=severity,
        reason=str(reason),
        evidence=dict(evidence or {}),
        log_path=log_path,
    )


def _benchmark_evidence(benchmark_summary: Any) -> dict[str, Any]:
    return {
        "render_frames": read_field(benchmark_summary, "render_frames"),
        "demo_frames": read_field(benchmark_summary, "demo_frames"),
        "measured_s": read_field(benchmark_summary, "measured_s"),
        "target_s": read_field(benchmark_summary, "target_s"),
        "fps_avg": read_field(benchmark_summary, "fps_avg"),
        "fps_min": read_field(benchmark_summary, "fps_min"),
        "fps_max": read_field(benchmark_summary, "fps_max"),
        "fps_mean": read_field(benchmark_summary, "fps_mean"),
        "loops": read_field(benchmark_summary, "loops"),
    }


def _measurement_telemetry_samples(result: Any) -> list[Any]:
    getter = getattr(result, "measurement_telemetry_samples", None)
    if callable(getter):
        return list(cast(Iterable[Any], getter()))
    benchmark_samples = read_field(result, "benchmark_telemetry_samples")
    if benchmark_samples is not None:
        return list(benchmark_samples)
    return list(read_field(result, "telemetry_samples") or [])


def _path_or_none(value: Any) -> Path | None:
    if value in (None, ""):
        return None
    return Path(value)
