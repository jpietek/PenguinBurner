"""Summarize generic stability output into the Auto-UV probe data model.

The summary uses only loaded telemetry samples so flattening and stability checks reflect actual GPU load.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from auto_uv.domain.types import AutoUvProbeSummary
from auto_uv.domain.user_options import AUTO_UV_METRIC_TUNING
from ..curve.base_load_telemetry import (
    derive_active_power_floor_w,
    decision_samples,
    sample_is_loaded,
    saturated_tail_samples,
)
from ..shared.probe_data_fields import read_field


POWER_CAP_BUSY_SAMPLE_FRACTION = 0.5
POWER_CAP_MIN_REASON_SAMPLES = 3
POWER_CAP_MIN_REASON_COVERAGE = 0.5


def mean(values: Sequence[float | int | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return None
    return sum(usable) / len(usable)


def max_or_none(values: Sequence[float | int | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return max(usable) if usable else None


def stddev_or_none(values: Sequence[float | int | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    if len(usable) < 2:
        return None
    average = sum(usable) / len(usable)
    variance = sum((value - average) ** 2 for value in usable) / len(usable)
    return variance**0.5


def stddev_pct_or_none(values: Sequence[float | int | None]) -> float | None:
    average = mean(values)
    if average in (None, 0.0):
        return None
    stddev = stddev_or_none(values)
    if stddev is None:
        return None
    return float(stddev) / float(average) * 100.0


def _probe_decision_samples(samples: list, *, skip_elapsed_warmup: bool) -> list:
    if skip_elapsed_warmup:
        return list(samples)
    return decision_samples(samples, rules=AUTO_UV_METRIC_TUNING)


def sample_mean(
    samples: list,
    attr: str,
    *,
    skip_elapsed_warmup: bool = False,
) -> float | None:
    return mean(
        [
            read_field(sample, attr)
            for sample in _probe_decision_samples(
                samples,
                skip_elapsed_warmup=skip_elapsed_warmup,
            )
            if sample is not None and read_field(sample, attr) is not None
        ]
    )


def sample_max(samples: list, attr: str) -> float | None:
    return max_or_none(
        [
            read_field(sample, attr)
            for sample in samples
            if sample is not None and read_field(sample, attr) is not None
        ]
    )


def summarize_perf_cap_reason(
    samples: Sequence,
    *,
    suppress_idle: bool = False,
    require_sw_power_dominant: bool = False,
) -> str | None:
    counts: Counter[str] = Counter()
    reason_sample_count = 0
    power_reason_sample_count = 0
    saw_none = False
    for sample in samples:
        reason_text = read_field(sample, "perf_cap_reason")
        if reason_text in (None, ""):
            continue
        sample_reasons: set[str] = set()
        for token in str(reason_text).replace(",", "+").split("+"):
            reason = token.strip()
            if not reason:
                continue
            sample_reasons.add(reason)
            if reason == "none" or (suppress_idle and reason == "idle"):
                saw_none = True
                continue
            counts[reason] += 1
        if sample_reasons:
            reason_sample_count += 1
            if any(_perf_cap_reason_indicates_power(reason) for reason in sample_reasons):
                power_reason_sample_count += 1
    power_reason_vote_qualified = (
        reason_sample_count >= POWER_CAP_MIN_REASON_SAMPLES
        and reason_sample_count
        >= POWER_CAP_MIN_REASON_COVERAGE * len(samples)
        and power_reason_sample_count / reason_sample_count
        >= POWER_CAP_BUSY_SAMPLE_FRACTION
    )
    if (
        require_sw_power_dominant
        and not power_reason_vote_qualified
    ):
        for reason in tuple(counts):
            if _perf_cap_reason_indicates_power(reason):
                counts.pop(reason, None)
        saw_none = True
    if not counts:
        return "none" if saw_none else None

    ordered_reasons = sorted(counts, key=lambda reason: (-counts[reason], reason))
    return "+".join(ordered_reasons[:3])


def _perf_cap_reason_indicates_power(reason_text: object) -> bool:
    reason = str(reason_text or "").lower()
    return any(
        "power" in token.strip() for token in reason.replace(",", "+").split("+")
    )


def summarize_loaded_perf_cap_reason(
    q2rtx_samples: list,
    companion_samples: list,
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool,
    skip_elapsed_warmup: bool = False,
) -> str | None:
    q2rtx_loaded, _q2rtx_power_floor_w = loaded_telemetry_samples(
        q2rtx_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
        skip_elapsed_warmup=skip_elapsed_warmup,
    )
    companion_loaded, _companion_power_floor_w = loaded_telemetry_samples(
        companion_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
    )
    return summarize_perf_cap_reason(
        [*q2rtx_loaded, *companion_loaded],
        suppress_idle=True,
        require_sw_power_dominant=True,
    ) or summarize_perf_cap_reason(
        [*q2rtx_samples, *companion_samples],
        require_sw_power_dominant=True,
    )


def count_hw_power_brake_samples(samples: Sequence) -> int:
    """Samples where the power-delivery hardware asserted its brake.

    Counted over whatever window the caller passes — the probe summary reports
    it across the probe's telemetry, the stability decision across the busy
    window — so read the paired total before comparing two coverage figures.

    ``hw-power-brake`` is not the configured power limit doing its job — it is
    an external protection signal (EDPp/OCP) from the board's power delivery.
    Report it per probe so a brake-heavy scan is visible in the log, events,
    and saved result.
    """
    brake = 0
    for sample in samples:
        reason = str(read_field(sample, "perf_cap_reason") or "").lower()
        if any(
            "power-brake" in token
            for token in reason.replace(",", "+").split("+")
        ):
            brake += 1
    return brake


def loaded_telemetry_samples(
    telemetry_samples: list,
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool = False,
    skip_elapsed_warmup: bool = False,
) -> tuple[list, float | None]:
    samples = _probe_decision_samples(
        telemetry_samples,
        skip_elapsed_warmup=skip_elapsed_warmup,
    )
    active_power_floor_w = derive_active_power_floor_w(
        samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
        rules=AUTO_UV_METRIC_TUNING,
    )
    active_samples = [
        sample
        for sample in samples
        if sample is not None
        and sample_is_loaded(
            sample,
            active_power_floor_w=active_power_floor_w,
            rules=AUTO_UV_METRIC_TUNING,
        )
    ]
    return (
        active_samples,
        float(active_power_floor_w)
        if active_power_floor_w is not None
        else None,
    )


def loaded_telemetry_means(
    telemetry_samples: list,
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool = False,
    skip_elapsed_warmup: bool = False,
) -> tuple[
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    int,
    float | None,
]:
    active_samples, active_power_floor_w = loaded_telemetry_samples(
        telemetry_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
        skip_elapsed_warmup=skip_elapsed_warmup,
    )
    if not active_samples:
        return None, None, None, None, None, 0, active_power_floor_w

    return (
        mean([read_field(sample, "power_w") for sample in active_samples]),
        mean([read_field(sample, "core_clock_mhz") for sample in active_samples]),
        mean([read_field(sample, "voltage_mv") for sample in active_samples]),
        mean([read_field(sample, "temperature_c") for sample in active_samples]),
        mean([read_field(sample, "fan_speed_pct") for sample in active_samples]),
        len(active_samples),
        float(active_power_floor_w)
        if active_power_floor_w is not None
        else None,
    )


def loaded_telemetry_diagnostics(
    telemetry_samples: list,
    *,
    power_limit_w: int | None,
    use_power_limit_floor: bool = False,
    skip_elapsed_warmup: bool = False,
) -> tuple[float | None, float | None, float | None, int]:
    active_samples, _active_power_floor_w = loaded_telemetry_samples(
        telemetry_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
        skip_elapsed_warmup=skip_elapsed_warmup,
    )
    if not active_samples:
        return None, None, None, 0

    loaded_voltage_values = [
        float(read_field(sample, "voltage_mv"))
        for sample in active_samples
        if read_field(sample, "voltage_mv") is not None
        and float(read_field(sample, "voltage_mv")) > 0.0
    ]
    loaded_clock_values = [
        float(read_field(sample, "core_clock_mhz"))
        for sample in active_samples
        if read_field(sample, "core_clock_mhz") is not None
    ]
    return (
        upper_median(loaded_voltage_values),
        upper_median(loaded_clock_values),
        value_at_fractional_index(loaded_clock_values, 0.90),
        len(active_samples),
    )


def upper_median(values: Sequence[float | int]) -> float | None:
    if not values:
        return None
    sorted_values = sorted(float(value) for value in values)
    return sorted_values[len(sorted_values) // 2]


def value_at_fractional_index(
    values: Sequence[float | int],
    fraction: float,
) -> float | None:
    if not values:
        return None
    sorted_values = sorted(float(value) for value in values)
    position = int(len(sorted_values) * float(fraction))
    position = max(0, min(len(sorted_values) - 1, position))
    return sorted_values[position]


def summarize_q2rtx_cuda_probe(
    *,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    live_voltage_before_mv: int | None,
    live_voltage_after_mv: int | None,
    used_companion_load: bool,
    power_limit_w: int | None,
    result,
    telemetry_samples: list | None = None,
    use_power_limit_floor: bool = False,
) -> AutoUvProbeSummary:
    benchmark_summary = getattr(result, "benchmark_summary", None)
    if benchmark_summary is not None:
        avg_fps = float(benchmark_summary.fps_avg)
        min_fps = (
            float(benchmark_summary.fps_min)
            if benchmark_summary.fps_min is not None
            else avg_fps
        )
        max_fps = (
            float(benchmark_summary.fps_max)
            if benchmark_summary.fps_max is not None
            else avg_fps
        )
        frames_per_run = int(benchmark_summary.render_frames)
        avg_seconds_per_run = float(benchmark_summary.measured_s)
        fps_values: list[float] = []
        fps_stddev = None
        fps_variance_pct = None
    else:
        fps_values = []
        avg_fps = None
        min_fps = None
        max_fps = None
        frames_per_run = None
        avg_seconds_per_run = None
        fps_stddev = stddev_or_none(fps_values)
        fps_variance_pct = stddev_pct_or_none(fps_values)
    if telemetry_samples is not None:
        q2rtx_samples = list(telemetry_samples)
        skip_elapsed_warmup = benchmark_summary is not None
        rebuild_telemetry_summary = True
    elif hasattr(result, "measurement_telemetry_samples"):
        q2rtx_samples = list(result.measurement_telemetry_samples())
        skip_elapsed_warmup = (
            getattr(result, "benchmark_telemetry_samples", None) is not None
        )
        rebuild_telemetry_summary = skip_elapsed_warmup
    elif getattr(result, "benchmark_telemetry_samples", None) is not None:
        q2rtx_samples = list(result.benchmark_telemetry_samples)
        skip_elapsed_warmup = True
        rebuild_telemetry_summary = True
    else:
        q2rtx_samples = list(result.telemetry_samples)
        skip_elapsed_warmup = False
        rebuild_telemetry_summary = False
    telemetry = result.telemetry_summary()
    if rebuild_telemetry_summary:
        telemetry = {
            "sample_count": len(q2rtx_samples),
            "power_max": sample_max(q2rtx_samples, "power_w"),
            "temperature_max": sample_max(q2rtx_samples, "temperature_c"),
            "fan_max": sample_max(q2rtx_samples, "fan_speed_pct"),
        }
    companion_samples = list(getattr(result, "companion_telemetry_samples", []) or [])
    perf_cap_reason = summarize_loaded_perf_cap_reason(
        q2rtx_samples,
        companion_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
        skip_elapsed_warmup=skip_elapsed_warmup,
    )
    hw_power_brake_samples = count_hw_power_brake_samples(
        [*q2rtx_samples, *companion_samples]
    )
    max_power_w = max_or_none(
        [telemetry.get("power_max"), sample_max(companion_samples, "power_w")]
    )
    max_temperature_c = max_or_none(
        [
            telemetry.get("temperature_max"),
            sample_max(companion_samples, "temperature_c"),
        ]
    )
    max_fan_speed_pct = max_or_none(
        [telemetry.get("fan_max"), sample_max(companion_samples, "fan_speed_pct")]
    )
    loaded = loaded_telemetry_means(
        q2rtx_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
        skip_elapsed_warmup=skip_elapsed_warmup,
    )
    loaded_diagnostics = loaded_telemetry_diagnostics(
        q2rtx_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
        skip_elapsed_warmup=skip_elapsed_warmup,
    )
    cuda_loaded = loaded_telemetry_means(
        companion_samples,
        power_limit_w=power_limit_w,
        use_power_limit_floor=use_power_limit_floor,
    )
    loaded_power_w, loaded_clock_mhz, loaded_voltage_mv = loaded[:3]
    loaded_temperature_c, loaded_fan_speed_pct = loaded[3:5]
    (
        loaded_median_voltage_mv,
        loaded_median_clock_mhz,
        loaded_p90_clock_mhz,
        loaded_qualified_sample_count,
    ) = loaded_diagnostics
    _cuda_power_w, cuda_loaded_clock_mhz, cuda_loaded_voltage_mv = cuda_loaded[:3]
    q2rtx_summary_clock_mhz = loaded_clock_mhz or sample_mean(
        q2rtx_samples,
        "core_clock_mhz",
        skip_elapsed_warmup=skip_elapsed_warmup,
    )
    q2rtx_summary_voltage_mv = loaded_voltage_mv or sample_mean(
        q2rtx_samples,
        "voltage_mv",
        skip_elapsed_warmup=skip_elapsed_warmup,
    )
    cuda_summary_clock_mhz = cuda_loaded_clock_mhz or sample_mean(
        companion_samples,
        "core_clock_mhz",
    )
    cuda_summary_voltage_mv = cuda_loaded_voltage_mv or sample_mean(
        companion_samples,
        "voltage_mv",
    )
    summary_power_w = loaded_power_w or telemetry.get("power_avg")
    summary_clock_mhz = loaded_clock_mhz or telemetry.get("core_clock_avg")
    observed_vdroop_mv = (
        float(candidate_voltage_mv) - float(loaded_median_voltage_mv)
        if loaded_median_voltage_mv is not None
        else None
    )
    return AutoUvProbeSummary(
        candidate_voltage_mv=int(candidate_voltage_mv),
        lock_clock_mhz=int(lock_clock_mhz),
        live_voltage_before_mv=live_voltage_before_mv,
        live_voltage_after_mv=live_voltage_after_mv,
        avg_voltage_mv=loaded_voltage_mv or telemetry.get("voltage_avg"),
        frames_per_run=frames_per_run,
        avg_seconds_per_run=avg_seconds_per_run,
        avg_fps=avg_fps,
        min_fps=min_fps,
        max_fps=max_fps,
        avg_power_w=summary_power_w,
        max_power_w=float(max_power_w) if max_power_w is not None else None,
        avg_temperature_c=loaded_temperature_c or telemetry.get("temperature_avg"),
        max_temperature_c=(
            float(max_temperature_c) if max_temperature_c is not None else None
        ),
        avg_fan_speed_pct=loaded_fan_speed_pct or telemetry.get("fan_avg"),
        max_fan_speed_pct=(
            float(max_fan_speed_pct) if max_fan_speed_pct is not None else None
        ),
        avg_core_clock_mhz=summary_clock_mhz,
        efficiency_fps_per_w=(
            avg_fps / float(summary_power_w)
            if avg_fps is not None
            and summary_power_w not in (None, 0, 0.0)
            else None
        ),
        efficiency_mhz_per_w=(
            float(summary_clock_mhz) / float(summary_power_w)
            if summary_clock_mhz is not None and summary_power_w not in (None, 0, 0.0)
            else None
        ),
        watts_per_mhz=(
            float(summary_power_w) / float(summary_clock_mhz)
            if summary_clock_mhz not in (None, 0, 0.0) and summary_power_w is not None
            else None
        ),
        used_companion_load=bool(used_companion_load),
        result_reason=str(result.reason),
        log_path=Path(result.log_path),
        q2rtx_avg_voltage_mv=q2rtx_summary_voltage_mv,
        q2rtx_avg_core_clock_mhz=q2rtx_summary_clock_mhz,
        cuda_avg_voltage_mv=cuda_summary_voltage_mv,
        cuda_avg_core_clock_mhz=cuda_summary_clock_mhz,
        loaded_median_voltage_mv=loaded_median_voltage_mv,
        loaded_median_core_clock_mhz=loaded_median_clock_mhz,
        loaded_p90_core_clock_mhz=loaded_p90_clock_mhz,
        loaded_qualified_sample_count=int(loaded_qualified_sample_count),
        observed_vdroop_mv=observed_vdroop_mv,
        perf_cap_reason=perf_cap_reason,
        hw_power_brake_samples=int(hw_power_brake_samples),
        telemetry_sample_count=len(q2rtx_samples) + len(companion_samples),
        fps_stddev=fps_stddev,
        fps_variance_pct=fps_variance_pct,
    )


def saturated_probe_tail_samples(telemetry_samples: list) -> list:
    return saturated_tail_samples(telemetry_samples, rules=AUTO_UV_METRIC_TUNING)
