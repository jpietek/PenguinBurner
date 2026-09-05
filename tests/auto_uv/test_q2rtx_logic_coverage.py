"""Line-coverage tests for pure-logic q2rtx probe modules.

Covers config-building, runtime-guardrail, and summary-computation branches
that the existing focused suites leave untouched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_uv.domain.user_options import (
    AUTO_UV_DEFAULTS,
    AUTO_UV_STALL_TUNING,
)
from auto_uv.probes.config import (
    base_q2rtx_probe_duration_s,
    cuda_companion_enabled_for_voltage_band,
    tiered_q2rtx_probe_duration_s,
)
from auto_uv.shared.probe_data_fields import percent
from auto_uv.probes.runtime_guardrails import (
    probe_failure_should_mark_voltage_unsafe,
    telemetry_sample_is_busy,
)
from auto_uv.probes.summary import (
    loaded_telemetry_diagnostics,
    loaded_telemetry_means,
    saturated_probe_tail_samples,
    stddev_pct_or_none,
    summarize_perf_cap_reason,
    summarize_q2rtx_cuda_probe,
    value_at_fractional_index,
)


# --------------------------------------------------------------------------- #
# q2rtx_cuda_probe_config.py
# --------------------------------------------------------------------------- #


def test_cuda_companion_enabled_returns_true_on_non_numeric_voltage() -> None:
    assert (
        cuda_companion_enabled_for_voltage_band(
            initial_target_voltage_mv="not-a-number",  # type: ignore[arg-type]
            candidate_voltage_mv=900,
        )
        is True
    )


def test_cuda_companion_enabled_returns_true_when_initial_voltage_non_positive() -> None:
    assert (
        cuda_companion_enabled_for_voltage_band(
            initial_target_voltage_mv=0,
            candidate_voltage_mv=900,
        )
        is True
    )


def test_tiered_q2rtx_duration_falls_back_to_base_on_non_numeric_voltage() -> None:
    assert (
        tiered_q2rtx_probe_duration_s(
            initial_target_voltage_mv="bad",  # type: ignore[arg-type]
            candidate_voltage_mv=900,
            base_duration_s=10,
        )
        == 10
    )


def test_tiered_q2rtx_duration_falls_back_to_base_when_initial_non_positive() -> None:
    assert (
        tiered_q2rtx_probe_duration_s(
            initial_target_voltage_mv=0,
            candidate_voltage_mv=900,
            base_duration_s=10,
        )
        == 10
    )


def test_base_probe_duration_uses_default_when_unspecified() -> None:
    assert base_q2rtx_probe_duration_s(None) == int(AUTO_UV_DEFAULTS.probe_duration_s)


def test_base_probe_duration_clamps_to_minimum_ten() -> None:
    assert base_q2rtx_probe_duration_s(3) == 10


# --------------------------------------------------------------------------- #
# runtime_guardrails.py
# --------------------------------------------------------------------------- #


def test_percent_clamps_negative_to_zero() -> None:
    assert percent(-10) == 0.0
    assert percent(50) == 0.5


def test_telemetry_sample_is_busy_returns_false_for_none_sample() -> None:
    assert telemetry_sample_is_busy(None, busy_power_floor_w=100.0) is False


def test_telemetry_sample_is_busy_true_on_high_gpu_util() -> None:
    busy_util = AUTO_UV_STALL_TUNING.busy_gpu_util_pct
    sample = SimpleNamespace(gpu_util_pct=busy_util, power_w=None)
    assert telemetry_sample_is_busy(sample, busy_power_floor_w=None) is True


def test_telemetry_sample_is_busy_true_on_power_floor() -> None:
    sample = SimpleNamespace(gpu_util_pct=0.0, power_w=200.0)
    assert telemetry_sample_is_busy(sample, busy_power_floor_w=150.0) is True


def test_telemetry_sample_is_busy_false_when_below_thresholds() -> None:
    sample = SimpleNamespace(gpu_util_pct=1.0, power_w=10.0)
    assert telemetry_sample_is_busy(sample, busy_power_floor_w=150.0) is False


def test_probe_failure_controlled_reason_does_not_mark_unsafe() -> None:
    assert probe_failure_should_mark_voltage_unsafe("user-stop-requested") is False
    assert probe_failure_should_mark_voltage_unsafe("q2rtx-launcher-error") is False


def test_probe_failure_idle_prefix_does_not_mark_unsafe() -> None:
    assert (
        probe_failure_should_mark_voltage_unsafe("q2rtx-selected-nvidia-gpu-idle-12s")
        is False
    )


def test_probe_failure_unknown_reason_marks_unsafe() -> None:
    assert probe_failure_should_mark_voltage_unsafe("gpu-hang-detected") is True


# --------------------------------------------------------------------------- #
# summary.py
# --------------------------------------------------------------------------- #


def test_stddev_pct_returns_none_when_single_usable_value() -> None:
    # mean is non-zero so we pass the first guard, but stddev needs >= 2 values.
    assert stddev_pct_or_none([5.0, None]) is None


def test_stddev_pct_returns_none_when_mean_zero() -> None:
    assert stddev_pct_or_none([0.0, 0.0]) is None


def test_stddev_pct_computes_coefficient_of_variation() -> None:
    assert stddev_pct_or_none([10.0, 30.0]) == pytest.approx(50.0)


def test_summarize_perf_cap_reason_skips_blank_tokens() -> None:
    # Tokens that are blank after stripping ("+ +") must be ignored, while the
    # real reason is still reported.
    samples = [{"perf_cap_reason": "+ +sw-power+"}]
    assert summarize_perf_cap_reason(samples) == "sw-power"


def test_summarize_perf_cap_reason_returns_none_when_empty() -> None:
    assert summarize_perf_cap_reason([{"perf_cap_reason": None}]) is None


def test_loaded_telemetry_means_empty_active_samples_branch() -> None:
    # use_power_limit_floor sets the floor from the power limit, above every
    # sample's power, so active_samples is empty but the floor is not None.
    samples = [
        {"elapsed_s": 6.0, "power_w": 10.0},
        {"elapsed_s": 7.0, "power_w": 12.0},
    ]
    result = loaded_telemetry_means(
        samples,
        power_limit_w=1000,
        use_power_limit_floor=True,
    )
    assert result[:5] == (None, None, None, None, None)
    assert result[5] == 0
    assert result[6] == pytest.approx(750.0)  # 1000 * 75%


def test_loaded_telemetry_means_none_floor_for_no_samples() -> None:
    assert loaded_telemetry_means([], power_limit_w=None) == (
        None,
        None,
        None,
        None,
        None,
        0,
        None,
    )


def test_loaded_telemetry_diagnostics_empty_when_no_active_samples() -> None:
    samples = [
        {"elapsed_s": 6.0, "power_w": 10.0},
        {"elapsed_s": 7.0, "power_w": 12.0},
    ]
    assert loaded_telemetry_diagnostics(
        samples,
        power_limit_w=1000,
        use_power_limit_floor=True,
    ) == (None, None, None, 0)


def test_value_at_fractional_index_none_for_empty_values() -> None:
    assert value_at_fractional_index([], 0.9) is None


def test_value_at_fractional_index_clamps_to_last_element() -> None:
    # int(3 * 0.9) == 2, clamped to the final index → 3.0.
    assert value_at_fractional_index([1.0, 2.0, 3.0], 0.9) == 3.0


def test_summarize_uses_override_samples_for_telemetry_summary() -> None:
    # Passing telemetry_samples overrides result.telemetry_samples and rebuilds
    # the telemetry summary from those override samples (max power/temp/fan).
    override_samples = [
        {
            "elapsed_s": 6.0,
            "power_w": 220.0,
            "core_clock_mhz": 2400.0,
            "voltage_mv": 850.0,
            "temperature_c": 60.0,
            "fan_speed_pct": 55.0,
        },
        {
            "elapsed_s": 7.0,
            "power_w": 240.0,
            "core_clock_mhz": 2450.0,
            "voltage_mv": 860.0,
            "temperature_c": 65.0,
            "fan_speed_pct": 60.0,
        },
    ]
    result = SimpleNamespace(
        telemetry_samples=[{"elapsed_s": 6.0, "power_w": 1.0}],
        companion_telemetry_samples=[],
        telemetry_summary=lambda: {"power_max": 9999.0},
        reason="ok",
        log_path=Path("/tmp/q2rtx.log"),
    )

    summary = summarize_q2rtx_cuda_probe(
        candidate_voltage_mv=860,
        lock_clock_mhz=2450,
        live_voltage_before_mv=860,
        live_voltage_after_mv=858,
        used_companion_load=False,
        power_limit_w=None,
        result=result,
        telemetry_samples=override_samples,
    )

    # Max power comes from the override samples (240W), not the stale summary.
    assert summary.max_power_w == pytest.approx(240.0)
    assert summary.max_temperature_c == pytest.approx(65.0)
    assert summary.max_fan_speed_pct == pytest.approx(60.0)


def test_saturated_probe_tail_samples_returns_high_load_tail() -> None:
    low_load = {"elapsed_s": 6.0, "power_w": 50.0, "core_clock_mhz": 1000.0}
    samples = [
        low_load,
        {"elapsed_s": 7.0, "power_w": 200.0, "core_clock_mhz": 2400.0},
        {"elapsed_s": 8.0, "power_w": 205.0, "core_clock_mhz": 2420.0},
        {"elapsed_s": 9.0, "power_w": 210.0, "core_clock_mhz": 2450.0},
    ]
    tail = saturated_probe_tail_samples(samples)
    assert isinstance(tail, list)
    # The low-power sample is excluded from the saturated (high-load) tail.
    assert low_load not in tail
    assert len(tail) >= 1
