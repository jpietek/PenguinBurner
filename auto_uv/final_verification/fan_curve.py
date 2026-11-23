"""Generate the optional silent fan-curve suggestion after final verification.

The curve is written only when final load temperature leaves enough cooling headroom.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from auto_uv.domain.types import AutoUvProbeSummary
from auto_uv.domain.user_options import AUTO_UV_FAN_TUNING
from profiles.uv.profile_fan_curve import write_auto_uv_fan_curve_payload


@dataclass(frozen=True, slots=True)
class FinalVerificationFanCurveResult:
    path: Path
    payload: dict

    @property
    def blocked(self) -> bool:
        return bool(self.payload.get("fan_curve_blocked"))

    @property
    def block_reason(self) -> str | None:
        reason = self.payload.get("block_reason")
        return str(reason) if reason else None

    @property
    def curve(self) -> list[list[float]]:
        fan = self.payload.get("fan")
        if not isinstance(fan, dict):
            return []
        return [list(point) for point in fan.get("curve", [])]


def build_final_verification_fan_curve_payload(
    *,
    final_probe: AutoUvProbeSummary | None,
    probes: list[AutoUvProbeSummary],
) -> dict | None:
    telemetry = probe_telemetry_payload(final_probe=final_probe, probes=probes)
    return build_fan_curve_payload_from_telemetry(telemetry)


def build_fan_curve_payload_from_telemetry(telemetry: dict) -> dict | None:
    """Build this profile's curve from saved scan telemetry."""
    if not isinstance(telemetry, dict):
        return None
    final_telemetry = telemetry["final"]
    scan_telemetry = telemetry["scan"]
    loaded_temp_c = (
        finite(final_telemetry["max_temperature_c"])
        or finite(final_telemetry["avg_temperature_c"])
        or finite(scan_telemetry["max_temperature_c"])
        or finite(scan_telemetry["avg_temperature_c"])
    )
    max_base_load_temp_c = float(AUTO_UV_FAN_TUNING.max_base_curve_load_temp_c)
    if loaded_temp_c is None:
        return blocked_payload(
            telemetry=telemetry,
            reason="missing-final-load-temperature",
            loaded_temp_c=None,
            limit_temp_c=max_base_load_temp_c,
        )
    if loaded_temp_c > max_base_load_temp_c:
        return blocked_payload(
            telemetry=telemetry,
            reason="base-load-temperature-too-high",
            loaded_temp_c=float(loaded_temp_c),
            limit_temp_c=max_base_load_temp_c,
        )

    observed_fan_pct = (
        finite(final_telemetry["avg_fan_speed_pct"])
        or finite(final_telemetry["max_fan_speed_pct"])
        or finite(scan_telemetry["avg_fan_speed_pct"])
        or finite(scan_telemetry["max_fan_speed_pct"])
        or AUTO_UV_FAN_TUNING.fallback_speed_pct
    )
    zero_rpm_temp_c = float(AUTO_UV_FAN_TUNING.zero_rpm_until_temp_c)
    active_temp_c = float(AUTO_UV_FAN_TUNING.minimum_active_temp_c)
    min_active_speed_pct = float(AUTO_UV_FAN_TUNING.minimum_active_speed_pct)
    emergency_temp_c = float(AUTO_UV_FAN_TUNING.emergency_temp_c)
    emergency_speed_pct = float(AUTO_UV_FAN_TUNING.emergency_min_speed_pct)
    full_speed_temp_c = float(AUTO_UV_FAN_TUNING.full_speed_temp_c)
    full_speed_pct = float(AUTO_UV_FAN_TUNING.full_speed_pct)
    max_curve_points = max(5, int(AUTO_UV_FAN_TUNING.max_curve_points))

    safe_curve_temp_c = clamp(
        max_base_load_temp_c,
        active_temp_c + 1.0,
        emergency_temp_c - 1.0,
    )
    cooling_headroom_c = max(0.0, max_base_load_temp_c - float(loaded_temp_c))
    speed_reduction_pct = min(
        float(AUTO_UV_FAN_TUNING.cooling_headroom_max_speed_reduction_pct),
        cooling_headroom_c
        * float(AUTO_UV_FAN_TUNING.cooling_headroom_speed_reduction_pct_per_c),
    )
    power_bonus = min(
        float(AUTO_UV_FAN_TUNING.cooling_headroom_max_exponential_power_bonus),
        cooling_headroom_c
        * float(AUTO_UV_FAN_TUNING.cooling_headroom_exponential_power_per_c),
    )
    effective_power = max(1.0, float(AUTO_UV_FAN_TUNING.exponential_power) + power_bonus)
    safe_anchor_speed_pct = load_anchor_fan_speed_pct(
        observed_fan_pct=float(observed_fan_pct),
        safe_curve_temp_c=float(safe_curve_temp_c),
        active_temp_c=float(active_temp_c),
        emergency_temp_c=float(emergency_temp_c),
        min_active_speed_pct=float(min_active_speed_pct),
        emergency_speed_pct=float(emergency_speed_pct),
        speed_reduction_pct=float(speed_reduction_pct),
        effective_power=float(effective_power),
    )
    safe_anchor_speed_pct = clamp(
        max(
            float(safe_anchor_speed_pct),
            float(AUTO_UV_FAN_TUNING.hot_range_end_speed_pct),
        ),
        float(min_active_speed_pct),
        float(AUTO_UV_FAN_TUNING.load_anchor_max_speed_pct),
    )
    curve = monotonic_curve(
        fan_curve_points(
            zero_rpm_temp_c=float(zero_rpm_temp_c),
            active_temp_c=float(active_temp_c),
            min_active_speed_pct=float(min_active_speed_pct),
            safe_curve_temp_c=float(safe_curve_temp_c),
            safe_anchor_speed_pct=float(safe_anchor_speed_pct),
            emergency_temp_c=float(emergency_temp_c),
            emergency_speed_pct=float(emergency_speed_pct),
            full_speed_temp_c=float(full_speed_temp_c),
            full_speed_pct=float(full_speed_pct),
            max_curve_points=int(max_curve_points),
            effective_power=float(effective_power),
            hot_range_start_temp_c=float(
                AUTO_UV_FAN_TUNING.hot_range_start_temp_c
            ),
            hot_range_start_speed_pct=float(
                AUTO_UV_FAN_TUNING.hot_range_start_speed_pct
            ),
        )
    )
    generated_at = datetime.now().astimezone().isoformat()
    return {
        "source": "auto-uv",
        "format_version": 1,
        "generated_at": generated_at,
        "zero_rpm_until_temperature_c": float(zero_rpm_temp_c),
        "minimum_active_temperature_c": float(active_temp_c),
        "minimum_active_fan_speed_pct": float(min_active_speed_pct),
        "max_base_curve_load_temperature_c": float(max_base_load_temp_c),
        "cooling_headroom_c": float(cooling_headroom_c),
        "cooling_headroom_speed_reduction_pct": float(speed_reduction_pct),
        "cooling_headroom_exponential_power_bonus": float(power_bonus),
        "effective_exponential_power": float(effective_power),
        "max_curve_points": int(max_curve_points),
        "safe_curve_temperature_c": float(safe_curve_temp_c),
        "load_anchor_temperature_c": float(safe_curve_temp_c),
        "load_anchor_fan_speed_pct": float(safe_anchor_speed_pct),
        "observed_fan_speed_before_headroom_pct": float(observed_fan_pct),
        "emergency_temperature_c": float(emergency_temp_c),
        "emergency_min_fan_speed_pct": float(emergency_speed_pct),
        "full_speed_temperature_c": float(full_speed_temp_c),
        "loaded_temperature_c": float(loaded_temp_c),
        "observed_fan_speed_pct": float(observed_fan_pct),
        "fan": fan_runtime_config(
            curve=curve,
            generated_at=generated_at,
            safe_curve_temp_c=float(safe_curve_temp_c),
        ),
        "telemetry": telemetry,
    }


def write_final_verification_fan_curve_payload(
    *,
    final_probe: AutoUvProbeSummary | None,
    probes: list[AutoUvProbeSummary],
) -> FinalVerificationFanCurveResult | None:
    payload = build_final_verification_fan_curve_payload(
        final_probe=final_probe,
        probes=probes,
    )
    if payload is None:
        return None
    path = write_auto_uv_fan_curve_payload(payload)
    return FinalVerificationFanCurveResult(path=path, payload=payload)


def fan_curve_points(
    *,
    zero_rpm_temp_c: float,
    active_temp_c: float,
    min_active_speed_pct: float,
    safe_curve_temp_c: float,
    safe_anchor_speed_pct: float,
    emergency_temp_c: float,
    emergency_speed_pct: float,
    full_speed_temp_c: float,
    full_speed_pct: float,
    max_curve_points: int,
    effective_power: float,
    hot_range_start_temp_c: float,
    hot_range_start_speed_pct: float,
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = [
        (zero_rpm_temp_c, 0.0),
        (active_temp_c, min_active_speed_pct),
    ]
    max_exponential_points = max(1, int(max_curve_points) - 4)
    exponential_points = min(
        max(1, int(AUTO_UV_FAN_TUNING.exponential_points)),
        max_exponential_points,
    )
    for step in range(1, exponential_points + 1):
        position = float(step) / float(exponential_points)
        temp_c = active_temp_c + ((safe_curve_temp_c - active_temp_c) * position)
        speed_pct = min_active_speed_pct + (
            (safe_anchor_speed_pct - min_active_speed_pct)
            * (position**effective_power)
        )
        points.append((temp_c, speed_pct))
    points.extend(
        [
            (emergency_temp_c, max(emergency_speed_pct, safe_anchor_speed_pct)),
            (full_speed_temp_c, full_speed_pct),
        ]
    )
    hot_span_c = max(1.0, safe_curve_temp_c - hot_range_start_temp_c)
    hot_speed_span_pct = max(
        0.0,
        safe_anchor_speed_pct - hot_range_start_speed_pct,
    )
    return [
        (
            temp_c,
            max(
                speed_pct,
                hot_range_start_speed_pct
                + (
                    hot_speed_span_pct
                    * clamp(
                        (temp_c - hot_range_start_temp_c) / hot_span_c,
                        0.0,
                        1.0,
                    )
                ),
            ),
        )
        if hot_range_start_temp_c <= temp_c <= safe_curve_temp_c
        else (temp_c, speed_pct)
        for temp_c, speed_pct in points
    ]


def load_anchor_fan_speed_pct(
    *,
    observed_fan_pct: float,
    safe_curve_temp_c: float,
    active_temp_c: float,
    emergency_temp_c: float,
    min_active_speed_pct: float,
    emergency_speed_pct: float,
    speed_reduction_pct: float,
    effective_power: float,
) -> float:
    thermal_span_c = max(1.0, emergency_temp_c - active_temp_c)
    safe_position = clamp((safe_curve_temp_c - active_temp_c) / thermal_span_c, 0.0, 1.0)
    thermal_floor_pct = min_active_speed_pct + (
        (emergency_speed_pct - min_active_speed_pct)
        * (safe_position**effective_power)
    )
    relaxed_observed_pct = max(min_active_speed_pct, observed_fan_pct - speed_reduction_pct)
    return clamp(
        max(relaxed_observed_pct, thermal_floor_pct, min_active_speed_pct),
        min_active_speed_pct,
        AUTO_UV_FAN_TUNING.load_anchor_max_speed_pct,
    )


def fan_runtime_config(
    *,
    curve: list[list[float]],
    generated_at: str,
    safe_curve_temp_c: float,
) -> dict:
    return {
        "poll_interval_s": AUTO_UV_FAN_TUNING.poll_interval_s,
        "hysteresis_c": AUTO_UV_FAN_TUNING.hysteresis_c,
        "mode": "linear",
        "min_fan_speed_pct": AUTO_UV_FAN_TUNING.min_speed_pct,
        "max_fan_speed_pct": AUTO_UV_FAN_TUNING.max_speed_pct,
        "max_step_up_pct_per_s": AUTO_UV_FAN_TUNING.max_step_up_pct_per_s,
        "max_step_down_pct_per_s": AUTO_UV_FAN_TUNING.max_step_down_pct_per_s,
        "manual_enable_temp_c": AUTO_UV_FAN_TUNING.minimum_active_temp_c,
        "auto_restore_temp_c": AUTO_UV_FAN_TUNING.zero_rpm_until_temp_c,
        "emergency_auto_override_temp_c": AUTO_UV_FAN_TUNING.hardware_auto_override_temp_c,
        "emergency_auto_resume_temp_c": AUTO_UV_FAN_TUNING.emergency_resume_temp_c,
        "force_update_every_poll": False,
        "curve_source": "auto-uv",
        "curve_source_generated_at": generated_at,
        "curve_source_target_load_temp_c": float(safe_curve_temp_c),
        "curve": curve,
    }


def probe_telemetry_payload(
    *,
    final_probe: AutoUvProbeSummary | None,
    probes: list[AutoUvProbeSummary],
) -> dict:
    all_probes = [probe for probe in probes if probe is not None]
    if final_probe is not None and final_probe not in all_probes:
        all_probes.append(final_probe)
    return {
        "measured_fan_points": probe_fan_points(all_probes),
        "final": probe_thermal_payload(final_probe),
        "scan": {
            "max_temperature_c": max_finite(
                [probe.max_temperature_c for probe in all_probes]
            ),
            "avg_temperature_c": mean_finite(
                [probe.avg_temperature_c for probe in all_probes]
            ),
            "max_fan_speed_pct": max_finite(
                [probe.max_fan_speed_pct for probe in all_probes]
            ),
            "avg_fan_speed_pct": mean_finite(
                [probe.avg_fan_speed_pct for probe in all_probes]
            ),
            "max_power_w": max_finite([probe.max_power_w for probe in all_probes]),
            "avg_power_w": mean_finite([probe.avg_power_w for probe in all_probes]),
            "probe_count": len(all_probes),
        },
    }


def probe_thermal_payload(probe: AutoUvProbeSummary | None) -> dict:
    return {
        "avg_power_w": finite(probe.avg_power_w if probe else None),
        "max_power_w": finite(probe.max_power_w if probe else None),
        "avg_temperature_c": finite(probe.avg_temperature_c if probe else None),
        "max_temperature_c": finite(probe.max_temperature_c if probe else None),
        "avg_fan_speed_pct": finite(probe.avg_fan_speed_pct if probe else None),
        "max_fan_speed_pct": finite(probe.max_fan_speed_pct if probe else None),
    }


def probe_fan_points(probes: list[AutoUvProbeSummary]) -> list[dict]:
    points = []
    seen: set[tuple[float, float, int, int]] = set()
    for probe in probes:
        temperature_c = finite(probe.avg_temperature_c)
        fan_speed_pct = finite(probe.avg_fan_speed_pct)
        if temperature_c is None or fan_speed_pct is None:
            continue
        point = {
            "temperature_c": round(float(temperature_c), 2),
            "fan_speed_pct": round(float(fan_speed_pct), 2),
            "voltage_mv": int(probe.candidate_voltage_mv),
            "clock_mhz": int(probe.lock_clock_mhz),
        }
        key = (
            float(point["temperature_c"]),
            float(point["fan_speed_pct"]),
            int(point["voltage_mv"]),
            int(point["clock_mhz"]),
        )
        if key not in seen:
            points.append(point)
            seen.add(key)
    return points


def blocked_payload(
    *,
    telemetry: dict,
    reason: str,
    loaded_temp_c: float | None,
    limit_temp_c: float,
) -> dict:
    payload = {
        "source": "auto-uv",
        "format_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "fan_curve_blocked": True,
        "block_reason": reason,
        "max_base_curve_load_temperature_c": float(limit_temp_c),
        "telemetry": telemetry,
    }
    if loaded_temp_c is not None:
        payload["loaded_temperature_c"] = float(loaded_temp_c)
    return payload


def monotonic_curve(points: list[tuple[float, float]]) -> list[list[float]]:
    normalized: list[list[float]] = []
    last_temp: float | None = None
    last_speed = 0.0
    for temp_c, speed_pct in points:
        temp = round(float(temp_c), 1)
        speed = round(clamp(float(speed_pct), 0.0, 100.0), 1)
        if last_temp is not None and temp <= last_temp:
            temp = round(last_temp + 1.0, 1)
        speed = max(speed, last_speed)
        normalized.append([temp, speed])
        last_temp = temp
        last_speed = speed
    return normalized


def finite(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def mean_finite(values: list[float | int | None]) -> float | None:
    finite_values = [float(value) for value in (finite(item) for item in values) if value is not None]
    return None if not finite_values else sum(finite_values) / float(len(finite_values))


def max_finite(values: list[float | int | None]) -> float | None:
    finite_values = [float(value) for value in (finite(item) for item in values) if value is not None]
    return None if not finite_values else max(finite_values)


def clamp(value: float, lower: float, upper: float) -> float:
    return max(float(lower), min(float(value), float(upper)))
