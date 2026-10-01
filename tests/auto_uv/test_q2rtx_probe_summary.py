from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_uv.persistence.verified_candidate_result_file import probe_metrics
from auto_uv.probes.summary import (
    loaded_telemetry_means,
    summarize_loaded_perf_cap_reason,
    summarize_q2rtx_cuda_probe,
)
from auto_uv.probes.event_payload import probe_summary_event_payload
from stability.q2rtx.models import Q2RTXBenchmarkSummary


def test_probe_summary_records_loaded_median_and_p90_diagnostics() -> None:
    telemetry_samples = [
        {
            "elapsed_s": 1.0,
            "power_w": 35.0,
            "core_clock_mhz": 1100.0,
            "voltage_mv": 750.0,
        },
        {
            "elapsed_s": 6.0,
            "power_w": 100.0,
            "core_clock_mhz": 2100.0,
            "voltage_mv": 820.0,
            "perf_cap_reason": "none",
        },
        {
            "elapsed_s": 7.0,
            "power_w": 150.0,
            "core_clock_mhz": 2300.0,
            "voltage_mv": 850.0,
            "perf_cap_reason": "sw-power",
        },
        {
            "elapsed_s": 8.0,
            "power_w": 180.0,
            "core_clock_mhz": 2400.0,
            "voltage_mv": 860.0,
            "perf_cap_reason": "sw-power+hw-thermal",
        },
        {
            "elapsed_s": 9.0,
            "power_w": 200.0,
            "core_clock_mhz": 2450.0,
            "voltage_mv": 870.0,
        },
        {
            "elapsed_s": 10.0,
            "power_w": 160.0,
            "core_clock_mhz": 2350.0,
            "voltage_mv": 855.0,
        },
    ]
    result = SimpleNamespace(
        telemetry_samples=telemetry_samples,
        companion_telemetry_samples=[],
        telemetry_summary=lambda: {},
        reason="ok",
        log_path=Path("/tmp/q2rtx.log"),
    )

    summary = summarize_q2rtx_cuda_probe(
        candidate_voltage_mv=870,
        lock_clock_mhz=2400,
        live_voltage_before_mv=870,
        live_voltage_after_mv=868,
        used_companion_load=False,
        power_limit_w=None,
        result=result,
    )

    assert summary.avg_power_w == pytest.approx(172.5)
    assert summary.avg_core_clock_mhz == pytest.approx(2375.0)
    assert summary.avg_voltage_mv == pytest.approx(858.75)
    assert summary.loaded_median_core_clock_mhz == 2400.0
    assert summary.loaded_p90_core_clock_mhz == 2450.0
    assert summary.loaded_median_voltage_mv == 860.0
    assert summary.loaded_qualified_sample_count == 4
    assert summary.observed_vdroop_mv == 10.0
    assert summary.perf_cap_reason == "hw-thermal"

    payload = probe_summary_event_payload(summary, stage="probe")
    metrics = probe_metrics(summary)
    assert payload["loaded_median_core_clock_mhz"] == 2400.0
    assert payload["perf_cap_reason"] == "hw-thermal"
    assert payload["observed_vdroop_mv"] == 10.0
    assert metrics["loaded_median_voltage_mv"] == 860.0
    assert metrics["loaded_qualified_sample_count"] == 4
    assert metrics["perf_cap_reason"] == "hw-thermal"


def test_probe_summary_without_benchmark_summary_has_no_fps_metrics() -> None:
    result = SimpleNamespace(
        telemetry_samples=[
            {"elapsed_s": 6.0, "power_w": 180.0, "core_clock_mhz": 2200.0}
        ],
        companion_telemetry_samples=[],
        telemetry_summary=lambda: {"power_avg": 180.0, "core_clock_avg": 2200.0},
        reason="ok",
        log_path=Path("/tmp/q2rtx.log"),
    )

    summary = summarize_q2rtx_cuda_probe(
        candidate_voltage_mv=900,
        lock_clock_mhz=2200,
        live_voltage_before_mv=900,
        live_voltage_after_mv=900,
        used_companion_load=False,
        power_limit_w=None,
        result=result,
    )

    assert summary.avg_fps is None
    assert summary.min_fps is None
    assert summary.max_fps is None
    assert summary.fps_stddev is None
    assert summary.fps_variance_pct is None


def test_probe_summary_prefers_benchmark_summary_and_hot_telemetry() -> None:
    hot_samples = [
        {
            "elapsed_s": 2.0,
            "power_w": 120.0,
            "core_clock_mhz": 2300.0,
            "voltage_mv": 840.0,
            "temperature_c": 61.0,
            "fan_speed_pct": 52.0,
        },
        {
            "elapsed_s": 3.0,
            "power_w": 140.0,
            "core_clock_mhz": 2400.0,
            "voltage_mv": 850.0,
            "temperature_c": 63.0,
            "fan_speed_pct": 54.0,
        },
    ]
    result = SimpleNamespace(
        benchmark_summary=Q2RTXBenchmarkSummary(
            reason="target",
            loops=4,
            loops_started=5,
            demo_frames=2524,
            render_frames=1500,
            target_s=30.0,
            measured_s=30.0,
            render_s=29.9,
            drain_s=0.1,
            loop_fps_mean=10.0,
            fps_avg=50.0,
            fps_min=44.0,
            fps_max=55.0,
            fps_mean=49.5,
            frame_ms_min=18.0,
            frame_ms_max=23.0,
            frame_ms_mean=20.2,
            measure_start_elapsed_s=2.0,
        ),
        telemetry_samples=[
            {"elapsed_s": 0.5, "power_w": 500.0, "core_clock_mhz": 1000.0},
            *hot_samples,
        ],
        benchmark_telemetry_samples=hot_samples,
        companion_telemetry_samples=[],
        telemetry_summary=lambda: {"power_max": 999.0, "core_clock_avg": 1.0},
        measurement_telemetry_samples=lambda: hot_samples,
        reason="ok",
        log_path=Path("/tmp/q2rtx.log"),
    )

    summary = summarize_q2rtx_cuda_probe(
        candidate_voltage_mv=850,
        lock_clock_mhz=2400,
        live_voltage_before_mv=850,
        live_voltage_after_mv=848,
        used_companion_load=False,
        power_limit_w=None,
        result=result,
    )

    assert summary.frames_per_run == 1500
    assert summary.avg_seconds_per_run == pytest.approx(30.0)
    assert summary.avg_fps == pytest.approx(50.0)
    assert summary.min_fps == pytest.approx(44.0)
    assert summary.max_fps == pytest.approx(55.0)
    assert summary.fps_stddev is None
    assert summary.max_power_w == pytest.approx(140.0)
    assert summary.avg_core_clock_mhz == pytest.approx(2350.0)


def test_probe_summary_accepts_busy_util_below_power_limit_floor() -> None:
    samples = [
        {
            "elapsed_s": 5.0 + index,
            "power_w": power_w,
            "gpu_util_pct": 99.0,
            "core_clock_mhz": clock_mhz,
            "voltage_mv": voltage_mv,
            "temperature_c": 60.0,
            "fan_speed_pct": 36.0,
        }
        for index, (power_w, clock_mhz, voltage_mv) in enumerate(
            [
                (252.1, 2752.0, 1025.0),
                (255.6, 2760.0, 1030.0),
                (256.4, 2760.0, 1025.0),
                (255.3, 2752.0, 1025.0),
            ]
        )
    ]
    result = SimpleNamespace(
        telemetry_samples=samples,
        companion_telemetry_samples=[],
        telemetry_summary=lambda: {},
        reason="ok",
        log_path=Path("/tmp/q2rtx.log"),
    )

    loaded = loaded_telemetry_means(
        samples,
        power_limit_w=390,
        use_power_limit_floor=True,
        skip_elapsed_warmup=True,
    )
    summary = summarize_q2rtx_cuda_probe(
        candidate_voltage_mv=1030,
        lock_clock_mhz=2760,
        live_voltage_before_mv=1045,
        live_voltage_after_mv=1025,
        used_companion_load=False,
        power_limit_w=390,
        result=result,
        telemetry_samples=samples,
        use_power_limit_floor=True,
    )

    assert loaded[0] == pytest.approx(254.85)
    assert loaded[1] == pytest.approx(2756.0)
    assert loaded[5] == 4
    assert loaded[6] == pytest.approx(292.5)
    assert summary.avg_power_w == pytest.approx(254.85)
    assert summary.avg_core_clock_mhz == pytest.approx(2756.0)
    assert summary.avg_voltage_mv == pytest.approx(1026.25)
    assert summary.loaded_qualified_sample_count == 4


def test_probe_summary_accepts_busy_util_without_power_telemetry() -> None:
    samples = [
        {
            "elapsed_s": 5.0 + index,
            "power_w": None,
            "gpu_util_pct": 99.0,
            "core_clock_mhz": clock_mhz,
            "voltage_mv": voltage_mv,
            "temperature_c": 60.0,
            "fan_speed_pct": 36.0,
        }
        for index, (clock_mhz, voltage_mv) in enumerate(
            ((2700.0, 1015.0), (2715.0, 1020.0), (2730.0, 1025.0))
        )
    ]
    result = SimpleNamespace(
        telemetry_samples=samples,
        companion_telemetry_samples=[],
        telemetry_summary=lambda: {},
        reason="ok",
        log_path=Path("/tmp/q2rtx.log"),
    )

    loaded = loaded_telemetry_means(
        samples,
        power_limit_w=115,
        use_power_limit_floor=True,
        skip_elapsed_warmup=True,
    )
    summary = summarize_q2rtx_cuda_probe(
        candidate_voltage_mv=1025,
        lock_clock_mhz=2730,
        live_voltage_before_mv=1015,
        live_voltage_after_mv=1025,
        used_companion_load=False,
        power_limit_w=115,
        result=result,
        telemetry_samples=samples,
        use_power_limit_floor=True,
    )

    assert loaded[0] is None
    assert loaded[1] == pytest.approx(2715.0)
    assert loaded[2] == pytest.approx(1020.0)
    assert loaded[5] == 3
    assert loaded[6] is None
    assert summary.avg_power_w is None
    assert summary.avg_core_clock_mhz == pytest.approx(2715.0)
    assert summary.avg_voltage_mv == pytest.approx(1020.0)
    assert summary.avg_temperature_c == pytest.approx(60.0)
    assert summary.avg_fan_speed_pct == pytest.approx(36.0)
    assert summary.efficiency_fps_per_w is None


def test_loaded_perf_cap_reason_suppresses_idle_as_none() -> None:
    loaded_samples = [
        {"elapsed_s": 6.0, "power_w": 220.0, "perf_cap_reason": "idle"},
        {"elapsed_s": 7.0, "power_w": 230.0, "perf_cap_reason": "idle"},
    ]

    assert (
        summarize_loaded_perf_cap_reason(
            loaded_samples,
            [],
            power_limit_w=None,
            use_power_limit_floor=False,
        )
        == "none"
    )


def test_loaded_perf_cap_reason_hides_minority_sw_power() -> None:
    loaded_samples = [
        *[
            {"elapsed_s": 6.0 + index, "power_w": 290.0, "perf_cap_reason": "idle"}
            for index in range(116)
        ],
        *[
            {
                "elapsed_s": 122.0 + index,
                "power_w": 292.0,
                "perf_cap_reason": "sw-power",
            }
            for index in range(17)
        ],
    ]

    assert (
        summarize_loaded_perf_cap_reason(
            loaded_samples,
            [],
            power_limit_w=360,
            use_power_limit_floor=True,
        )
        == "none"
    )


def test_loaded_perf_cap_reason_reports_dominant_sw_power() -> None:
    loaded_samples = [
        {"elapsed_s": 6.0, "power_w": 290.0, "perf_cap_reason": "sw-power"},
        {"elapsed_s": 7.0, "power_w": 291.0, "perf_cap_reason": "sw-power"},
        {"elapsed_s": 8.0, "power_w": 292.0, "perf_cap_reason": "sw-power"},
        {"elapsed_s": 9.0, "power_w": 293.0, "perf_cap_reason": "idle"},
        {"elapsed_s": 10.0, "power_w": 294.0, "perf_cap_reason": "none"},
    ]

    assert (
        summarize_loaded_perf_cap_reason(
            loaded_samples,
            [],
            power_limit_w=360,
            use_power_limit_floor=True,
        )
        == "sw-power"
    )


def test_loaded_perf_cap_reason_rejects_sparse_power_evidence() -> None:
    loaded_samples = [
        {
            "elapsed_s": 6.0,
            "power_w": 200.0,
            "perf_cap_reason": "sw-power",
        },
        *[
            {
                "elapsed_s": 7.0 + index,
                "power_w": 200.0,
                "perf_cap_reason": None,
            }
            for index in range(9)
        ],
    ]

    # One reason-bearing sample must not classify an otherwise reason-less,
    # far-below-cap window as power-saturated for clock reclaim.
    assert (
        summarize_loaded_perf_cap_reason(
            loaded_samples,
            [],
            power_limit_w=450,
            use_power_limit_floor=False,
        )
        == "none"
    )
