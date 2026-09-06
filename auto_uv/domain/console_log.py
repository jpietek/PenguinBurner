"""Write concise user-facing Auto-UV progress lines.

This module formats messages only; scan decisions stay in the rule modules.
"""

from __future__ import annotations

from typing import Callable

from auto_uv.domain.types import AutoUvProbeSummary
from auto_uv.scan_mode.efficiency_fps_per_w_policy import compare_temperature_normalized_fps_per_w


def log_phase(log: Callable[[str], None], phase: str, message: str) -> None:
    log(f"Auto-UV phase={phase} {message}")


def log_user_stage(
    log: Callable[[str], None],
    title: str,
    lines: list[str],
) -> None:
    log("")
    log(f"Auto-UV: {title}")
    for line in lines:
        log(f"  {line}")


def log_benchmark(
    log: Callable[[str], None],
    *,
    phase: str,
    probe: AutoUvProbeSummary,
    reference_probe: AutoUvProbeSummary | None = None,
    reference_label: str = "initial",
) -> None:
    parts = []
    if probe.efficiency_fps_per_w is not None:
        parts.append(f"fps_per_w={probe.efficiency_fps_per_w:.5f}FPS/W")
    if probe.efficiency_mhz_per_w is not None:
        parts.append(f"mhz_per_w={probe.efficiency_mhz_per_w:.5f}MHz/W")
    if reference_probe is not None:
        parts.extend(benchmark_delta_parts(probe, reference_probe, reference_label))
    log_phase(log, phase, "benchmark " + " ".join(parts))
    brake_note = hw_power_brake_note(probe)
    if brake_note is not None:
        log_phase(log, phase, brake_note)


def hw_power_brake_note(probe: AutoUvProbeSummary) -> str | None:
    """Report power-delivery brake events, which a capped run alone hides.

    A stable candidate can still trip the board's protection brake. Report
    that event next to the probe even when other power-cap reasons occur.
    """
    brake_samples = int(getattr(probe, "hw_power_brake_samples", 0) or 0)
    if brake_samples <= 0:
        return None
    total = int(getattr(probe, "telemetry_sample_count", 0) or 0)
    coverage = f"{brake_samples}/{total}" if total > 0 else str(brake_samples)
    return (
        f"hw-power-brake asserted on {coverage} telemetry samples: the board's "
        "power delivery limited the clock, not the configured power limit"
    )


def benchmark_delta_parts(
    probe: AutoUvProbeSummary,
    reference_probe: AutoUvProbeSummary,
    reference_label: str,
) -> list[str]:
    parts = [
        f"vs-{reference_label}-fps_per_w="
        + format_benchmark_delta(
            probe.efficiency_fps_per_w,
            reference_probe.efficiency_fps_per_w,
            higher_is_better=True,
            unit="FPS/W",
        ),
        f"vs-{reference_label}-mhz_per_w="
        + format_benchmark_delta(
            probe.efficiency_mhz_per_w,
            reference_probe.efficiency_mhz_per_w,
            higher_is_better=True,
            unit="MHz/W",
        ),
    ]
    normalized = compare_temperature_normalized_fps_per_w(reference_probe, probe)
    reference_temp_c = normalized["reference_temperature_c"]
    if (
        reference_temp_c is not None
        and normalized["candidate_fps_per_w"] is not None
        and normalized["previous_fps_per_w"] is not None
    ):
        parts.append(
            f"vs-{reference_label}-temp_norm_fps_per_w@{float(reference_temp_c):.1f}C="
            + format_benchmark_delta(
                normalized["candidate_fps_per_w"],
                normalized["previous_fps_per_w"],
                higher_is_better=True,
                unit="FPS/W",
            )
        )
    if (
        reference_temp_c is not None
        and normalized["candidate_power_w"] is not None
        and normalized["previous_power_w"] is not None
    ):
        parts.append(
            f"vs-{reference_label}-temp_norm_power@{float(reference_temp_c):.1f}C="
            + format_benchmark_delta(
                normalized["candidate_power_w"],
                normalized["previous_power_w"],
                higher_is_better=False,
                unit="W",
            )
        )
    return parts


def format_benchmark_delta(
    current_value: float | None,
    reference_value: float | None,
    *,
    higher_is_better: bool,
    unit: str,
) -> str:
    if current_value is None or reference_value is None:
        return "n/a"
    delta = float(current_value) - float(reference_value)
    pct = (
        (delta / float(reference_value) * 100.0)
        if float(reference_value) != 0.0
        else 0.0
    )
    direction = (
        "better" if (delta > 0.0 if higher_is_better else delta < 0.0) else "worse"
    )
    if abs(delta) < 1e-9:
        direction = "same"
    return f"{delta:+.5f}{unit} ({pct:+.2f}%, {direction})"
