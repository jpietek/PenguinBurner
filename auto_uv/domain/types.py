"""Typed data passed between Auto-UV rule modules.

The types describe algorithm state and decisions only; they do not touch GPU APIs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class AutoUvError(RuntimeError):
    pass


class AutoUvPowerLimitApplyError(AutoUvError):
    """The requested scan power regime could not be established reliably."""


class AutoUvFinalChoiceDiscarded(RuntimeError):
    pass


@dataclass(slots=True)
class AutoUvProbeSummary:
    candidate_voltage_mv: int
    lock_clock_mhz: int
    live_voltage_before_mv: int | None
    live_voltage_after_mv: int | None
    avg_voltage_mv: float | None
    frames_per_run: int | None
    avg_seconds_per_run: float | None
    avg_fps: float | None
    min_fps: float | None
    max_fps: float | None
    avg_power_w: float | None
    max_power_w: float | None
    avg_temperature_c: float | None
    max_temperature_c: float | None
    avg_fan_speed_pct: float | None
    max_fan_speed_pct: float | None
    avg_core_clock_mhz: float | None
    efficiency_fps_per_w: float | None
    efficiency_mhz_per_w: float | None
    watts_per_mhz: float | None
    used_companion_load: bool
    result_reason: str
    log_path: Path
    q2rtx_avg_voltage_mv: float | None = None
    q2rtx_avg_core_clock_mhz: float | None = None
    cuda_avg_voltage_mv: float | None = None
    cuda_avg_core_clock_mhz: float | None = None
    loaded_median_voltage_mv: float | None = None
    loaded_median_core_clock_mhz: float | None = None
    loaded_p90_core_clock_mhz: float | None = None
    loaded_qualified_sample_count: int = 0
    observed_vdroop_mv: float | None = None
    perf_cap_reason: str | None = None
    # Samples where the board's power-delivery protection (EDPp/OCP) braked
    # the clock. Distinct from the configured power limit: it is reported so
    # a brake-heavy run is visible rather than reading as an ordinary cap.
    hw_power_brake_samples: int = 0
    telemetry_sample_count: int = 0
    fps_stddev: float | None = None
    fps_variance_pct: float | None = None
    # Exact applied geometry belonging to these measurements. Selection must
    # not reconstruct another curve from the voltage/clock label.
    tested_plan: list[dict] | None = None


@dataclass(slots=True)
class AutoUvVoltageScanResult:
    success: bool
    final_voltage_mv: int
    lock_clock_mhz: int
    stop_reason: str
    failed_candidate_voltage_mv: int | None
    probes: list[AutoUvProbeSummary]
    baseline_core_clock_mhz: float | None = None
    baseline_power_w: float | None = None
    baseline_temperature_c: float | None = None
    baseline_fan_speed_pct: float | None = None
    baseline_efficiency_mhz_per_w: float | None = None
    final_core_clock_mhz: float | None = None
    final_power_w: float | None = None
    final_temperature_c: float | None = None
    final_fan_speed_pct: float | None = None
    final_efficiency_mhz_per_w: float | None = None
    core_clock_drop_mhz: float | None = None
    core_clock_drop_pct: float | None = None
    power_saved_w: float | None = None
    power_saved_pct: float | None = None


class FailureKind(str, Enum):
    NONE = "none"
    LOW_CLOCK = "low-clock"
    FPS_REGRESSION = "fps-regression"
    LOAD_LOST = "load-lost"
    TIMED_OUT = "timed-out"
    FATAL_OUTPUT = "fatal-output"
    CUDA_FAILED = "cuda-failed"
    Q2RTX_FAILED = "q2rtx-failed"
    NVIDIA_XID = "nvidia-xid"
    GPU_HANG = "gpu-hang"
    METRICS_MISSING = "metrics-missing"
    METRICS_INVALID = "metrics-invalid"
    USER_STOP = "user-stop"
    CACHED_UNSAFE = "cached-unsafe"


class FailureSeverity(str, Enum):
    PASS = "pass"
    RECOVERABLE = "recoverable"
    UNSAFE = "unsafe"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    elapsed_s: float | None = None
    power_w: float | None = None
    core_clock_mhz: float | None = None
    voltage_mv: float | None = None
    temperature_c: float | None = None
    fan_speed_pct: float | None = None
    gpu_util_pct: float | None = None
    perf_cap_reason: str | None = None


@dataclass(frozen=True, slots=True)
class BaseLoadTarget:
    measured_clock_mhz: float
    target_clock_mhz: int
    active_avg_clock_mhz: float | None
    active_preferred_clock_mhz: float | None
    saturated_clock_mhz: float | None
    fallback_clock_mhz: float | None
    active_sample_count: int
    saturated_sample_count: int


@dataclass(frozen=True, slots=True)
class BaseVfPoint:
    index: int
    voltage_mv: int
    base_mhz: int
    target_mhz: int
    preserve_base: bool = False
    original: dict[str, Any] = field(default_factory=dict)

    @property
    def new_offset_mhz(self) -> int:
        return int(self.target_mhz) - int(self.base_mhz)


@dataclass(frozen=True, slots=True)
class VfCurveCandidate:
    label: str
    voltage_mv: int
    target_mhz: int
    flattened_plan: list[dict]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StableRunDecision:
    passed: bool
    failure_kind: FailureKind
    severity: FailureSeverity
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    log_path: Path | None = None
