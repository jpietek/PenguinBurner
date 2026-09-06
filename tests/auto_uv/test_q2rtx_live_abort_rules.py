from __future__ import annotations

import pytest

from auto_uv.domain.types import TelemetrySample
from auto_uv.domain.user_options import (
    AUTO_UV_STALL_TUNING,
)
from auto_uv.probes.runtime_guardrails import (
    probe_failure_should_mark_voltage_unsafe,
)
from auto_uv.probes.live_abort import (
    selected_nvidia_gpu_idle_abort_reason,
    telemetry_live_abort_reason,
)


def _sample(
    elapsed_s: float,
    *,
    gpu_util_pct: float,
    power_w: float,
    core_clock_mhz: float = 210.0,
) -> TelemetrySample:
    return TelemetrySample(
        elapsed_s=float(elapsed_s),
        gpu_util_pct=float(gpu_util_pct),
        power_w=float(power_w),
        core_clock_mhz=float(core_clock_mhz),
    )


def test_telemetry_abort_detects_q2rtx_not_loading_selected_nvidia_gpu() -> None:
    samples = [
        _sample(float(index), gpu_util_pct=0.0, power_w=4.5)
        for index in range(5, 24)
    ]

    reason = telemetry_live_abort_reason(
        {
            "elapsed_s": 23.6,
            "latest_sample": samples[-1],
            "telemetry_samples": samples,
        },
        busy_power_floor_w=None,
        proper_run_power_floor_w=None,
        progress_state={},
    )

    assert reason is not None
    assert reason.startswith("q2rtx-selected-nvidia-gpu-idle")
    assert "may be rendering on another GPU" in reason
    assert "--gpu-index" in reason


def test_telemetry_idle_abort_allows_actual_selected_gpu_load() -> None:
    samples = [
        _sample(float(index), gpu_util_pct=0.0, power_w=4.5)
        for index in range(5, 35)
    ]
    samples.append(
        _sample(35.0, gpu_util_pct=97.0, power_w=95.0, core_clock_mhz=2400.0)
    )

    assert (
        telemetry_live_abort_reason(
            {
                "elapsed_s": 35.0,
                "latest_sample": samples[-1],
                "telemetry_samples": samples,
            },
            busy_power_floor_w=None,
            proper_run_power_floor_w=None,
            progress_state={},
        )
        is None
    )

def test_selected_gpu_idle_failure_does_not_blacklist_voltage() -> None:
    assert (
        probe_failure_should_mark_voltage_unsafe(
            "q2rtx-selected-nvidia-gpu-idle max_util=0.0% max_power=4.5W"
        )
        is False
    )


# --------------------------------------------------------------------------
# selected_nvidia_gpu_idle_abort_reason guards
# --------------------------------------------------------------------------


def test_selected_gpu_idle_skips_when_elapsed_below_min() -> None:
    samples = [_sample(float(i), gpu_util_pct=0.0, power_w=4.5) for i in range(5, 24)]
    state = {
        "elapsed_s": AUTO_UV_STALL_TUNING.selected_gpu_idle_min_s - 1.0,
        "telemetry_samples": samples,
    }

    assert selected_nvidia_gpu_idle_abort_reason(state) is None


def test_selected_gpu_idle_skips_with_too_few_warmed_samples() -> None:
    # Only a couple of post-warmup samples -> below selected_gpu_idle_min_samples.
    samples = [_sample(float(i), gpu_util_pct=0.0, power_w=4.5) for i in range(6, 9)]
    state = {"elapsed_s": 30.0, "telemetry_samples": samples}

    assert len(samples) < AUTO_UV_STALL_TUNING.selected_gpu_idle_min_samples
    assert selected_nvidia_gpu_idle_abort_reason(state) is None


def test_selected_gpu_idle_skips_when_no_util_or_power_values() -> None:
    samples = [
        TelemetrySample(elapsed_s=float(i), gpu_util_pct=None, power_w=None)
        for i in range(6, 20)
    ]
    state = {"elapsed_s": 30.0, "telemetry_samples": samples}

    assert selected_nvidia_gpu_idle_abort_reason(state) is None


def test_selected_gpu_idle_busy_power_blocks_idle_abort() -> None:
    # Utilisation looks idle but power is clearly above the idle ceiling.
    samples = [_sample(float(i), gpu_util_pct=2.0, power_w=120.0) for i in range(6, 20)]
    state = {"elapsed_s": 30.0, "telemetry_samples": samples}

    assert selected_nvidia_gpu_idle_abort_reason(state) is None


def test_selected_gpu_idle_reports_with_missing_power_values() -> None:
    # Util present and idle, power absent -> idle abort fires with power n/a.
    samples = [
        TelemetrySample(elapsed_s=float(i), gpu_util_pct=1.0, power_w=None)
        for i in range(6, 20)
    ]
    state = {"elapsed_s": 30.0, "telemetry_samples": samples}

    reason = selected_nvidia_gpu_idle_abort_reason(state)
    assert reason is not None
    assert reason.startswith("q2rtx-selected-nvidia-gpu-idle")
    assert "max_power=n/a" in reason
    assert "max_util=1.0%" in reason


# --------------------------------------------------------------------------
# load_lost_abort_reason (via telemetry_live_abort_reason) + low live clock
# --------------------------------------------------------------------------


def test_telemetry_abort_reports_load_lost_after_streak() -> None:
    # Idle-but-not-busy samples below the proper-run power floor for the
    # required streak length trigger a load-lost abort.
    streak = AUTO_UV_STALL_TUNING.load_lost_streak_samples
    min_samples = AUTO_UV_STALL_TUNING.load_lost_min_samples
    samples = [
        _sample(float(i), gpu_util_pct=10.0, power_w=20.0)
        for i in range(6, 6 + max(min_samples, 4))
    ]
    progress_state: dict = {}
    reason = None
    for _ in range(streak):
        reason = telemetry_live_abort_reason(
            {
                "elapsed_s": 30.0,
                "latest_sample": samples[-1],
                "telemetry_samples": samples,
            },
            busy_power_floor_w=60.0,
            proper_run_power_floor_w=40.0,
            progress_state=progress_state,
        )

    assert reason is not None
    assert reason.startswith("telemetry-live-load-lost")
    assert "current=20.0W" in reason
    assert "floor=40.0W" in reason
    assert "busy-floor=60.0W" in reason
    assert progress_state["low_power_streak"] == streak


def test_load_lost_streak_resets_when_sample_busy() -> None:
    min_samples = AUTO_UV_STALL_TUNING.load_lost_min_samples
    busy = [
        _sample(float(i), gpu_util_pct=95.0, power_w=20.0)
        for i in range(6, 6 + max(min_samples, 4))
    ]
    progress_state = {"low_power_streak": 2}

    reason = telemetry_live_abort_reason(
        {
            "elapsed_s": 30.0,
            "latest_sample": busy[-1],
            "telemetry_samples": busy,
        },
        busy_power_floor_w=60.0,
        proper_run_power_floor_w=40.0,
        progress_state=progress_state,
    )

    # Busy sample (high util) resets the low-power streak and avoids abort.
    assert reason is None
    assert progress_state["low_power_streak"] == 0


def test_load_lost_skips_when_below_min_samples() -> None:
    samples = [_sample(6.0, gpu_util_pct=10.0, power_w=20.0)]
    progress_state: dict = {}

    reason = telemetry_live_abort_reason(
        {
            "elapsed_s": 30.0,
            "latest_sample": samples[-1],
            "telemetry_samples": samples,
        },
        busy_power_floor_w=60.0,
        proper_run_power_floor_w=40.0,
        progress_state=progress_state,
    )

    assert reason is None
    assert progress_state == {}


@pytest.mark.parametrize("clock", [210.0, 2111.0, 2603.0])
@pytest.mark.parametrize("cap_reason", [None, "sw-power", "hw-power-brake"])
def test_busy_clock_shortfall_does_not_abort(clock, cap_reason) -> None:
    samples = [
        TelemetrySample(elapsed_s=float(i), gpu_util_pct=95.0, power_w=120.0,
                        core_clock_mhz=clock, perf_cap_reason=cap_reason)
        for i in range(6, 30)
    ]
    state = {"elapsed_s": 30.0, "latest_sample": samples[-1], "telemetry_samples": samples}
    progress = {}
    for _ in range(4):
        assert telemetry_live_abort_reason(
            state, busy_power_floor_w=60.0, proper_run_power_floor_w=40.0,
            progress_state=progress,
        ) is None
