"""Build the immutable runtime intent consumed by the Rust GPU daemon.

Python owns user configuration and saved-profile interpretation.  The root
daemon receives only resolved values, so it never re-reads mutable Python files
or duplicates profile-selection policy.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from auto_uv.domain.user_options import AUTO_UV_FAN_TUNING
from cli.runtime_config_file import default_runtime_config, load_runtime_config
from common.penguin_burner_errors import NvmlError
from overlay.config import load_overlay_config
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR, read_auto_uv_profiles
from profiles.uv.profile_tiers import (
    PROFILE_TIER_BALANCED,
    PROFILE_TIERS,
    available_adaptive_tiers,
    normalize_profile_tier,
    resolve_profile_tier_profiles,
)
from profiles.uv.runtime_auto_uv_profile import load_auto_uv_final_curve
from runtime.daemon_client import gpu_capabilities, require_daemon_capabilities
from runtime.gpu_control.adaptive_profile_policy import AdaptiveProfilePolicyConfig
from runtime.support.adaptive_target_fps import (
    adaptive_target_fps_from_env,
    parse_adaptive_target_fps,
)
from ui.features.curves.fan_profiles import (
    auto_uv_fan_curve_payload_path,
    fan_curve_points_from_payload,
    read_json_file,
)


RUNTIME_SPEC_FORMAT_VERSION = 1
RUNTIME_SPEC_CAPABILITY = "runtime-spec-v1"


def runtime_intent(
    *,
    profile_selector: str = "",
    silent_fan_curve: bool = False,
    adaptive_auto_uv: bool = False,
    adaptive_target_fps: float | None = None,
    gpu_index: int | None = None,
    game_telemetry: bool = True,
) -> dict[str, Any]:
    return {
        "profile_selector": str(profile_selector or "").strip(),
        "silent_fan_curve": bool(silent_fan_curve),
        "adaptive_auto_uv": bool(adaptive_auto_uv),
        "adaptive_target_fps": (
            None if adaptive_target_fps is None else float(adaptive_target_fps)
        ),
        "gpu_index": None if gpu_index is None else max(0, int(gpu_index)),
        "game_telemetry": bool(game_telemetry),
    }


def runtime_intent_from_argv(argv) -> dict[str, Any]:
    """One-release Python compatibility bridge for existing CLI flags."""
    selector = ""
    silent_fan_curve = False
    adaptive_auto_uv = False
    adaptive_target_fps = None
    gpu_index = None
    game_telemetry = True
    values = list(argv or ())
    index = 0
    while index < len(values):
        item = str(values[index])
        if item in {"--auto-uv-profile", "--gpu-index", "--adaptive-target-fps"}:
            if index + 1 >= len(values):
                raise RuntimeError(f"{item} requires a value")
            value = values[index + 1]
            if item == "--auto-uv-profile":
                selector = str(value)
            elif item == "--adaptive-target-fps":
                adaptive_target_fps = float(value)
            else:
                gpu_index = max(0, int(value))
            index += 2
            continue
        if item.startswith("--auto-uv-profile="):
            selector = item.split("=", 1)[1]
        elif item.startswith("--gpu-index="):
            gpu_index = max(0, int(item.split("=", 1)[1]))
        elif item.startswith("--adaptive-target-fps="):
            adaptive_target_fps = float(item.split("=", 1)[1])
        elif item == "--silent-fan-curve":
            silent_fan_curve = True
        elif item == "--adaptive-auto-uv":
            adaptive_auto_uv = True
        elif item == "--no-game-telemetry":
            game_telemetry = False
        else:
            raise RuntimeError(f"unsupported runtime profile argument: {item}")
        index += 1
    return runtime_intent(
        profile_selector=selector,
        silent_fan_curve=silent_fan_curve,
        adaptive_auto_uv=adaptive_auto_uv,
        adaptive_target_fps=adaptive_target_fps,
        gpu_index=gpu_index,
        game_telemetry=game_telemetry,
    )


def build_runtime_spec_from_intent(
    intent: dict[str, Any],
    *,
    socket_path: str | Path | None = None,
) -> dict[str, Any]:
    if not isinstance(intent, dict):
        raise RuntimeError("runtime intent JSON must be an object")
    unknown = sorted(
        set(intent)
        - {
            "profile_selector",
            "silent_fan_curve",
            "adaptive_auto_uv",
            "adaptive_target_fps",
            "gpu_index",
            "game_telemetry",
        }
    )
    if unknown:
        raise RuntimeError(f"unknown runtime intent field: {', '.join(unknown)}")
    return build_runtime_spec(**runtime_intent(**intent), socket_path=socket_path)


def build_runtime_spec(
    *,
    profile_selector: str = "",
    silent_fan_curve: bool = False,
    adaptive_auto_uv: bool = False,
    adaptive_target_fps: float | None = None,
    gpu_index: int | None = None,
    game_telemetry: bool = True,
    socket_path: str | Path | None = None,
) -> dict[str, Any]:
    require_daemon_capabilities(
        RUNTIME_SPEC_CAPABILITY,
        "gpu-capabilities-v1",
        socket_path=socket_path,
    )
    config, _config_path = load_runtime_config()
    configured_gpu = config.get("gpu", {}) if isinstance(config, dict) else {}
    selected_index = (
        _nonnegative_int(configured_gpu.get("index"), default=0)
        if gpu_index is None
        else max(0, int(gpu_index))
    )
    capabilities = gpu_capabilities(selected_index, socket_path=socket_path)
    identity = capabilities.get("identity")
    if not isinstance(identity, dict) or not str(identity.get("uuid") or "").strip():
        raise RuntimeError(f"GPU {selected_index} did not report a stable UUID")

    selector = str(profile_selector or "").strip()
    selected_curve = (
        # Static mode keeps the documented "empty selector = latest saved
        # profile" contract; adaptive mode treats an empty selector as "no
        # explicit tier" so the initial tier falls back to the fastest one.
        None
        if selector == STOCK_PROFILE_SELECTOR or (adaptive_auto_uv and not selector)
        else load_auto_uv_final_curve(selector)
    )
    mode = "stock"
    static_profile = None
    adaptive = None
    if adaptive_auto_uv:
        adaptive = _adaptive_spec(selected_curve, target_fps_override=adaptive_target_fps)
        if adaptive is not None:
            mode = "adaptive"
    elif selected_curve is not None:
        mode = "static"
        static_profile = _profile_spec(selected_curve)

    fan = _fan_spec(config, enabled=bool(silent_fan_curve))
    overlay = load_overlay_config()
    return {
        "format_version": RUNTIME_SPEC_FORMAT_VERSION,
        "gpu": {
            "uuid": str(identity["uuid"]).strip(),
            "index_at_resolution": selected_index,
            "pci_bus_id": str(identity.get("pci_bus_id") or ""),
            "name": str(identity.get("name") or ""),
        },
        "mode": mode,
        "static_profile": static_profile,
        "adaptive": adaptive,
        "fan": fan,
        "policy": {
            "enable_persistence_mode": _config_bool(
                configured_gpu.get("enable_persistence_mode"),
                default=True,
            ),
        },
        "overlay": {
            "enabled": bool(overlay.enabled),
            "update_interval_s": int(overlay.update_interval_s),
        },
        # Frame-history capture for the watched game (Game Perf Profile); per-game
        # opt-out from the Steam tab. Absent means enabled (older callers).
        "game_telemetry": bool(game_telemetry),
    }


def _adaptive_spec(
    selected_curve: dict | None,
    *,
    target_fps_override: float | None = None,
) -> dict[str, Any] | None:
    resolved = resolve_profile_tier_profiles(read_auto_uv_profiles())
    tier_profiles: dict[str, dict[str, Any]] = {}
    for tier in available_adaptive_tiers(resolved):
        summary = resolved.get(tier)
        profile_id = str(summary.get("profile_id") or "") if isinstance(summary, dict) else ""
        if not profile_id:
            continue
        curve = load_auto_uv_final_curve(profile_id)
        if curve is not None:
            tier_profiles[tier] = _profile_spec(curve)

    selected_tier = ""
    if selected_curve is not None:
        selected_tier = normalize_profile_tier(
            selected_curve.get("profile_tier_key"),
            default=PROFILE_TIER_BALANCED,
        )
        tier_profiles[selected_tier] = _profile_spec(selected_curve)
    if not tier_profiles:
        return None

    initial_tier = (
        selected_tier
        if selected_tier in tier_profiles
        else next(tier for tier in reversed(PROFILE_TIERS) if tier in tier_profiles)
    )
    target_fps = (
        parse_adaptive_target_fps(target_fps_override)
        if target_fps_override is not None
        else adaptive_target_fps_from_env()
    )
    policy = AdaptiveProfilePolicyConfig.for_target_fps(target_fps)
    return {
        "initial_tier": initial_tier,
        "profiles": tier_profiles,
        "policy": {
            "target_fps": float(target_fps),
            "target_slow_windows": int(policy.target_slow_windows),
            "near_slow_windows": int(policy.near_slow_windows),
            "comfort_windows": int(policy.comfort_windows),
            "performance_comfort_windows": int(policy.performance_comfort_windows),
            "demote_dwell_s": float(policy.demote_dwell_s),
            "performance_demote_dwell_s": float(
                policy.performance_demote_dwell_s
            ),
            "cpu_bound_gpu_util_max_pct": float(
                policy.cpu_bound_gpu_util_max_pct
            ),
            "cpu_bound_peak_thread_min_pct": float(
                policy.cpu_bound_peak_thread_min_pct
            ),
            "cpu_bound_process_util_min_pct": float(
                policy.cpu_bound_process_util_min_pct
            ),
        },
    }


def _profile_spec(curve: dict[str, Any]) -> dict[str, Any]:
    path = Path(curve.get("path") or "")
    profile_id = str(curve.get("profile_id") or "").strip()
    if not profile_id and path.name:
        profile_id = path.stem.removeprefix("auto-uv-profile-")
    if not profile_id:
        raise NvmlError("resolved Auto-UV profile has no profile id")
    flatten = curve.get("flatten_target")
    if not isinstance(flatten, dict):
        flatten = {}
    return {
        "path": str(path),
        "profile_id": profile_id,
        "profile_tier": str(curve.get("profile_tier") or ""),
        "profile_tier_key": str(curve.get("profile_tier_key") or ""),
        "plan": [
            {
                "index": int(point["index"]),
                "voltage_mv": int(point["voltage_mv"]),
                "base_mhz": int(point["base_mhz"]),
                "target_mhz": int(point["target_mhz"]),
                "new_offset_mhz": int(point["new_offset_mhz"]),
            }
            for point in curve.get("plan", [])
        ],
        "lock_clock_mhz": int(curve.get("lock_clock_mhz") or 0),
        "candidate_voltage_mv": int(curve.get("candidate_voltage_mv") or 0),
        "memory_offset_mhz": _optional_int(curve.get("memory_offset_mhz")),
        "power_limit_w": _optional_int(curve.get("power_limit_w")),
        # Scan-measured load power for this profile and its stock baseline:
        # the daemon's energy-saved accounting rate. Included only when the
        # profile carries them, so older specs keep their exact shape.
        **{
            key: value
            for key in ("avg_power_w", "base_avg_power_w")
            if (value := _optional_float(curve.get(key))) is not None
        },
        "flatten_target": {
            "source": str(flatten.get("source") or "auto-uv-final"),
            "lock_clock_mhz": int(
                flatten.get("lock_clock_mhz") or curve.get("lock_clock_mhz") or 0
            ),
            "lock_voltage_mv": _optional_int(flatten.get("lock_voltage_mv")),
            "end_voltage_mv": _optional_int(flatten.get("end_voltage_mv")),
            "tail_point_count": _optional_int(flatten.get("tail_point_count")),
            "ceiling_clock_mhz": _optional_int(flatten.get("ceiling_clock_mhz")),
            "tail_rise_bins": _optional_int(flatten.get("tail_rise_bins")),
        },
    }


def _fan_spec(config: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    defaults = default_runtime_config()["fan"]
    base_values = config.get("fan", {}) if isinstance(config, dict) else {}
    base = _normalize_fan_config(base_values, defaults=defaults)
    if not enabled:
        return {"enabled": False, "config": base, "notice": ""}

    path = auto_uv_fan_curve_payload_path()
    if not path.is_file():
        return {"enabled": True, "config": base, "notice": ""}
    payload = read_json_file(path)
    rejection = _fan_payload_rejection(payload, path)
    if rejection:
        return {"enabled": False, "config": base, "notice": rejection}
    assert isinstance(payload, dict)

    raw_fan = payload.get("fan", {})
    fan = _normalize_fan_config(raw_fan, defaults=base)
    points = fan_curve_points_from_payload(payload)
    fan["curve"] = [[float(temp), float(speed)] for temp, speed in points]
    fan["curve_source"] = "auto-uv"
    fan["curve_source_path"] = str(path)
    return {"enabled": True, "config": fan, "notice": ""}


def _fan_payload_rejection(payload: dict | None, path: Path) -> str:
    prefix = "Manual fan control disabled"
    if not isinstance(payload, dict):
        return f"{prefix} because the auto-UV fan curve is invalid: path={path}"
    if bool(payload.get("fan_curve_blocked")):
        reason = str(payload.get("block_reason") or "unknown")
        return f"{prefix} by auto-UV safety guard: reason={reason}"
    loaded_temp = _finite_float(payload.get("loaded_temperature_c"))
    limit = float(AUTO_UV_FAN_TUNING.max_base_curve_load_temp_c)
    if loaded_temp is None:
        return f"{prefix} by auto-UV safety guard: missing saved final load temperature"
    if loaded_temp > limit:
        return (
            f"{prefix} by auto-UV safety guard: saved final load temperature "
            f"{loaded_temp:.1f}C is above {limit:.1f}C"
        )
    points = fan_curve_points_from_payload(payload)
    error = _validate_fan_curve(points)
    if error:
        return f"{prefix} because the auto-UV fan curve is invalid: {error}"
    return ""


def _validate_fan_curve(points: list[tuple[float, float]]) -> str:
    tuning = AUTO_UV_FAN_TUNING
    if len(points) < 2:
        return "curve must contain at least two points"
    if len(points) > int(tuning.max_curve_points):
        return f"curve has too many points: {len(points)}"
    previous_temp = None
    previous_speed = None
    for temp, speed in points:
        if not math.isfinite(temp) or not math.isfinite(speed):
            return "curve contains a non-finite point"
        if previous_temp is not None and temp <= previous_temp:
            return "curve temperatures must be strictly increasing"
        if not 0.0 <= speed <= 100.0:
            return "curve fan speeds must be in the range 0..100"
        if previous_speed is not None and speed < previous_speed:
            return "curve fan speeds must not decrease as temperature rises"
        previous_temp, previous_speed = temp, speed
    checks = (
        (tuning.zero_rpm_until_temp_c, 0.0, "zero-rpm point", True),
        (tuning.minimum_active_temp_c, tuning.minimum_active_speed_pct, "active minimum", False),
        (tuning.max_base_curve_load_temp_c, tuning.minimum_active_speed_pct, "safe load target", False),
        (tuning.emergency_temp_c, tuning.emergency_min_speed_pct, "emergency", False),
        (tuning.full_speed_temp_c, tuning.full_speed_pct, "full speed", False),
    )
    for temp, expected, label, exact_zero in checks:
        observed = _fan_speed_for_temp(float(temp), points)
        if exact_zero and observed > 0.01:
            return f"curve is unsafe at {label}: {observed:.1f}% instead of 0%"
        if not exact_zero and observed + 0.01 < float(expected):
            return f"curve is unsafe at {label}: {observed:.1f}% < {float(expected):.1f}%"
    return ""


def _fan_speed_for_temp(temp_c: float, points: list[tuple[float, float]]) -> float:
    if temp_c <= points[0][0]:
        return points[0][1]
    for (left_temp, left_speed), (right_temp, right_speed) in zip(points, points[1:]):
        if temp_c <= right_temp:
            position = (temp_c - left_temp) / (right_temp - left_temp)
            return left_speed + ((right_speed - left_speed) * position)
    return points[-1][1]


def _normalize_fan_config(values: Any, *, defaults: dict[str, Any]) -> dict[str, Any]:
    values = values if isinstance(values, dict) else {}
    return {
        "poll_interval_s": _finite_or_default(values.get("poll_interval_s"), defaults["poll_interval_s"]),
        "curve": _normalized_curve(values.get("curve"), defaults["curve"]),
        "hysteresis_c": _finite_or_default(values.get("hysteresis_c"), defaults["hysteresis_c"]),
        "mode": str(values.get("mode") or defaults["mode"]),
        "min_fan_speed_pct": _int_or_default(values.get("min_fan_speed_pct"), defaults["min_fan_speed_pct"]),
        "max_fan_speed_pct": _int_or_default(values.get("max_fan_speed_pct"), defaults["max_fan_speed_pct"]),
        "max_step_up_pct_per_s": _finite_or_default(values.get("max_step_up_pct_per_s"), defaults["max_step_up_pct_per_s"]),
        "max_step_down_pct_per_s": _finite_or_default(values.get("max_step_down_pct_per_s"), defaults["max_step_down_pct_per_s"]),
        "manual_enable_temp_c": _finite_or_default(values.get("manual_enable_temp_c"), defaults["manual_enable_temp_c"]),
        "auto_restore_temp_c": _finite_or_default(values.get("auto_restore_temp_c"), defaults["auto_restore_temp_c"]),
        "emergency_auto_override_temp_c": _finite_or_default(values.get("emergency_auto_override_temp_c"), defaults["emergency_auto_override_temp_c"]),
        "emergency_auto_resume_temp_c": _finite_or_default(values.get("emergency_auto_resume_temp_c"), defaults["emergency_auto_resume_temp_c"]),
        "force_update_every_poll": _config_bool(values.get("force_update_every_poll"), default=bool(defaults["force_update_every_poll"])),
        "curve_source": _optional_text(values.get("curve_source", defaults.get("curve_source"))),
        "curve_source_path": _optional_text(values.get("curve_source_path", defaults.get("curve_source_path"))),
    }


def _normalized_curve(value: Any, fallback: Any) -> list[list[float]]:
    source = value if isinstance(value, list) else fallback
    try:
        points = [[float(point[0]), float(point[1])] for point in source]
    except (TypeError, ValueError, IndexError):
        points = [[float(point[0]), float(point[1])] for point in fallback]
    return points


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(round(float(value)))


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _nonnegative_int(value: Any, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return int(default)


def _int_or_default(value: Any, default: Any) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite_or_default(value: Any, default: Any) -> float:
    number = _finite_float(value)
    return float(default) if number is None else number


def _config_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
