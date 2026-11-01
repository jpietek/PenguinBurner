from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from .constants import (
    FATAL_OUTPUT_PATTERNS,
    FATAL_OUTPUT_REGEXES,
)
from .models import (
    Q2RTXBenchmarkSummary,
    Q2RTXStabilityConfig,
    TelemetrySample,
)


def _format_live_progress_state(state: dict, *, prefix: str) -> str:
    elapsed_s = float(state.get("progress_elapsed_s", state.get("elapsed_s", 0.0)))
    workload_name = str(state.get("workload_name") or "?")
    latest_sample = state.get("latest_sample")
    running = str(state.get("running") or "q2rtx").strip().lower()

    parts = [
        prefix,
        (
            "workload=cuda-compute"
            if running == "cuda"
            else f"demo={workload_name}"
        ),
        f"running={running}",
        f"elapsed={elapsed_s:.1f}s",
    ]
    if state.get("benchmark_phase") == "startup":
        parts.append("phase=startup")
    if latest_sample is not None and latest_sample.power_w is not None:
        parts.append(f"power={float(latest_sample.power_w):.1f}W")
    if latest_sample is not None and latest_sample.core_clock_mhz is not None:
        parts.append(f"core_clock={float(latest_sample.core_clock_mhz):.0f}MHz")
    if latest_sample is not None and latest_sample.voltage_mv is not None:
        parts.append(f"volt={float(latest_sample.voltage_mv):.0f}mV")
    if latest_sample is not None and latest_sample.gpu_util_pct is not None:
        parts.append(f"gpu={float(latest_sample.gpu_util_pct):.0f}%")
    if latest_sample is not None and latest_sample.temperature_c is not None:
        parts.append(f"temp={float(latest_sample.temperature_c):.0f}C")
    if latest_sample is not None and latest_sample.fan_speed_pct is not None:
        parts.append(f"fan={float(latest_sample.fan_speed_pct):.0f}%")
    fatal_output_matches = list(state.get("fatal_output_matches") or [])
    if fatal_output_matches:
        parts.append("fatal=" + ",".join(fatal_output_matches))
    return " ".join(parts)


def _format_sample_metrics(sample: TelemetrySample | None) -> str:
    if sample is None:
        return ""
    parts: list[str] = []
    if sample.power_w is not None:
        parts.append(f"power={float(sample.power_w):.1f}W")
    if sample.core_clock_mhz is not None:
        parts.append(f"core_clock={float(sample.core_clock_mhz):.0f}MHz")
    if sample.voltage_mv is not None:
        parts.append(f"volt={float(sample.voltage_mv):.0f}mV")
    if sample.gpu_util_pct is not None:
        parts.append(f"gpu={float(sample.gpu_util_pct):.0f}%")
    if sample.temperature_c is not None:
        parts.append(f"temp={float(sample.temperature_c):.0f}C")
    if sample.fan_speed_pct is not None:
        parts.append(f"fan={float(sample.fan_speed_pct):.0f}%")
    return " ".join(parts)


def attach_stdout_progress(
    config: Q2RTXStabilityConfig,
    *,
    prefix: str = "Stability live:",
) -> Q2RTXStabilityConfig:
    previous_progress_callback = config.progress_callback

    def _progress_callback(state: dict) -> None:
        print(_format_live_progress_state(state, prefix=prefix), flush=True)
        if previous_progress_callback is not None:
            previous_progress_callback(state)

    config.progress_callback = _progress_callback
    return config


def attach_stdout_telemetry_events(
    config: Q2RTXStabilityConfig,
    *,
    target_voltage_mv: int,
    target_clock_mhz: int,
) -> Q2RTXStabilityConfig:
    """Print a load_telemetry JSON line per tick alongside the human status
    line, so a listening GUI can plot the live GPU position on the curve
    being verified (mirroring what a live Auto-UV scan already emits)."""
    previous_progress_callback = config.progress_callback

    def _progress_callback(state: dict) -> None:
        latest_sample = state.get("latest_sample")
        if latest_sample is not None:
            payload = {
                "event": "load_telemetry",
                "voltage_mv": int(target_voltage_mv),
                "clock_mhz": int(target_clock_mhz),
            }
            if latest_sample.voltage_mv is not None:
                payload["measured_voltage_mv"] = round(float(latest_sample.voltage_mv))
            if latest_sample.core_clock_mhz is not None:
                payload["measured_clock_mhz"] = round(float(latest_sample.core_clock_mhz))
            print(json.dumps(payload, sort_keys=True), flush=True)
        if previous_progress_callback is not None:
            previous_progress_callback(state)

    config.progress_callback = _progress_callback
    return config


def _read_recent_output(log_path: Path, *, tail_lines: int = 40) -> list[str]:
    if not log_path.exists():
        return []

    tail: deque[str] = deque(maxlen=max(1, int(tail_lines)))
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            tail.append(line.rstrip())
    return list(tail)


def _scan_output_for_fatal_patterns(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []

    matches: list[str] = []
    text = log_path.read_text(encoding="utf-8", errors="replace")
    normalized_text = text.casefold()
    for pattern in FATAL_OUTPUT_PATTERNS:
        if str(pattern).casefold() in normalized_text:
            matches.append(pattern)
    for regex in FATAL_OUTPUT_REGEXES:
        for match in regex.finditer(text):
            matches.append(match.group(0))
    return matches


def _optional_float(event: dict, key: str) -> float | None:
    value = event.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(event: dict, key: str) -> int | None:
    value = event.get(key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_ms_s(event: dict, key: str) -> float | None:
    value = _optional_float(event, key)
    if value is None:
        return None
    return float(value) / 1000.0


def _benchmark_summary_from_events(
    events: list[dict],
) -> Q2RTXBenchmarkSummary | None:
    measure_start_s = None
    done_event = None
    for event in events:
        if event.get("event") == "phase" and event.get("name") == "measure_start":
            measure_start_s = _optional_ms_s(event, "elapsed_ms")
        elif event.get("event") == "done":
            done_event = event

    if done_event is None:
        return None

    try:
        render_frames = int(done_event["render_frames"])
        measured_s = float(done_event["measured_ms"]) / 1000.0
        fps_avg = float(done_event["fps_avg"])
    except (KeyError, TypeError, ValueError):
        return None

    return Q2RTXBenchmarkSummary(
        reason=str(done_event.get("reason") or ""),
        loops=int(done_event.get("loops") or 0),
        loops_started=_optional_int(done_event, "loops_started"),
        demo_frames=_optional_int(done_event, "demo_frames"),
        render_frames=render_frames,
        target_s=_optional_ms_s(done_event, "target_ms"),
        measured_s=measured_s,
        render_s=_optional_ms_s(done_event, "render_ms"),
        drain_s=_optional_ms_s(done_event, "drain_ms"),
        loop_fps_mean=_optional_float(done_event, "loop_fps_mean"),
        fps_avg=fps_avg,
        fps_min=_optional_float(done_event, "fps_min"),
        fps_max=_optional_float(done_event, "fps_max"),
        fps_mean=_optional_float(done_event, "fps_mean"),
        frame_ms_min=_optional_float(done_event, "frame_ms_min"),
        frame_ms_max=_optional_float(done_event, "frame_ms_max"),
        frame_ms_mean=_optional_float(done_event, "frame_ms_mean"),
        measure_start_elapsed_s=measure_start_s,
    )
