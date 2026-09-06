"""A power-delivery brake must never pass as an ordinary capped run.

``hw-power-brake`` is the board's protection circuit (EDPp/OCP), not the
configured power limit. The scan reports it on every surface so a brake-heavy
run is legible in the
log, the event stream, the saved result, and the decision text itself.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from auto_uv.domain.console_log import hw_power_brake_note, log_benchmark
from auto_uv.persistence.verified_candidate_result_file import (
    base_probe_metrics,
    probe_metrics,
)
from auto_uv.probes.event_payload import probe_summary_event_payload
from auto_uv.probes.stability_decision import (
    StabilityThresholds,
    evaluate_loaded_telemetry,
    evaluate_stable_run,
)
from auto_uv.probes.summary import (
    count_hw_power_brake_samples,
    summarize_q2rtx_cuda_probe,
)
from auto_uv.run.crash_recovery import (
    base_probe_summary_from_candidate_record,
    probe_summary_from_candidate_record,
)


def _samples(
    reason: str,
    *,
    count: int = 8,
    clock_mhz: float = 1900.0,
) -> list[dict]:
    return [
        {
            "elapsed_s": 6.0 + float(index),
            "gpu_util_pct": 99.0,
            "power_w": 300.0,
            "core_clock_mhz": float(clock_mhz),
            "voltage_mv": 900.0,
            "perf_cap_reason": reason,
        }
        for index in range(int(count))
    ]


def _decision(reason: str):
    return evaluate_loaded_telemetry(
        _samples(reason),
        baseline_power_w=300.0,
        power_limit_w=430,
        thresholds=StabilityThresholds(),
        log_path=None,
    )


def _summary(reason: str):
    samples = _samples(reason)
    return summarize_q2rtx_cuda_probe(
        candidate_voltage_mv=900,
        lock_clock_mhz=2595,
        live_voltage_before_mv=900,
        live_voltage_after_mv=900,
        used_companion_load=False,
        power_limit_w=430,
        result=SimpleNamespace(
            telemetry_samples=samples,
            companion_telemetry_samples=[],
            telemetry_summary=lambda: {},
            reason="ok",
            log_path=Path("/tmp/q2rtx.log"),
        ),
    )


def test_brake_samples_are_counted_and_plain_power_caps_are_not() -> None:
    assert count_hw_power_brake_samples(_samples("hw-power-brake", count=5)) == 5
    assert count_hw_power_brake_samples(_samples("sw-power", count=5)) == 0
    # Combined masks are the common real shape and must still count.
    assert count_hw_power_brake_samples(
        _samples("sw-power+hw-power-brake", count=3)
    ) == 3
    assert count_hw_power_brake_samples(_samples("none", count=3)) == 0


def test_power_walled_pass_names_the_brake_in_its_reason() -> None:
    braked = _decision("hw-power-brake")
    capped = _decision("sw-power")

    # The exemption still applies — this is the deliberate behavior.
    assert braked.passed
    assert capped.passed
    # But the brake is never silent.
    assert "hw-power-brake=8/8" in braked.reason
    assert "hw-power-brake" not in capped.reason


def test_stable_run_preserves_the_brake_reason() -> None:
    samples = _samples("hw-power-brake")
    result = {
        "success": True,
        "reason": "ok",
        "log_path": "/tmp/q2rtx.log",
        "benchmark_summary": {
            "render_frames": 100,
            "measured_s": 10.0,
            "fps_avg": 100.0,
            "fps_min": 99.0,
            "fps_max": 101.0,
            "fps_mean": 100.0,
        },
        "telemetry_samples": samples,
        "benchmark_telemetry_samples": samples,
    }

    decision = evaluate_stable_run(
        result,
        baseline_fps=100.0,
        baseline_power_w=300.0,
        power_limit_w=430,
        cuda_required=False,
        thresholds=StabilityThresholds(),
    )

    assert decision.passed
    assert "hw-power-brake=8/8" in decision.reason


def test_brake_reason_is_visible_without_a_clock_shortfall() -> None:
    decision = evaluate_loaded_telemetry(
        _samples("hw-power-brake", clock_mhz=2500.0),
        baseline_power_w=300.0,
        power_limit_w=430,
        thresholds=StabilityThresholds(),
        log_path=None,
    )

    assert decision.passed
    assert "hw-power-brake=8/8" in decision.reason


def test_probe_summary_reports_brake_coverage() -> None:
    braked = _summary("hw-power-brake")
    capped = _summary("sw-power")

    assert braked.hw_power_brake_samples == 8
    assert braked.telemetry_sample_count == 8
    assert capped.hw_power_brake_samples == 0


def test_brake_reaches_the_event_stream_and_the_saved_result() -> None:
    summary = _summary("hw-power-brake")

    payload = probe_summary_event_payload(summary, stage="candidate")
    metrics = probe_metrics(summary)

    assert payload["hw_power_brake_samples"] == 8
    assert metrics["hw_power_brake_samples"] == 8


def test_crash_recovery_restores_candidate_and_baseline_brake_counts() -> None:
    summary = _summary("hw-power-brake")
    candidate_record = {
        "candidate_voltage_mv": summary.candidate_voltage_mv,
        "lock_clock_mhz": summary.lock_clock_mhz,
        **probe_metrics(summary),
    }
    base_record = {
        **base_probe_metrics(summary),
    }

    recovered = probe_summary_from_candidate_record(candidate_record)
    recovered_base = base_probe_summary_from_candidate_record(base_record)

    assert recovered is not None
    assert recovered.hw_power_brake_samples == 8
    assert recovered_base is not None
    assert recovered_base.hw_power_brake_samples == 8


def test_benchmark_log_calls_out_the_brake_only_when_it_fired() -> None:
    braked = _summary("hw-power-brake")
    capped = _summary("sw-power")
    lines: list[str] = []

    log_benchmark(lines.append, phase="candidate", probe=braked)
    log_benchmark(lines.append, phase="candidate", probe=capped)

    brake_lines = [line for line in lines if "hw-power-brake" in line]
    assert len(brake_lines) == 1
    assert "8/8" in brake_lines[0]
    assert "not the configured power limit" in brake_lines[0]
    assert hw_power_brake_note(capped) is None
