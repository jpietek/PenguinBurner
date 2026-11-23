"""V/F curves in saved profiles, the cached base curve, and saving an edited curve."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

from cli.runtime_config_file import runtime_gpu_index
from common.atomic_write import atomic_write_json
from common.penguin_burner_paths import (
    default_runtime_config_path,
    default_user_config_dir,
)
from curve_editors.uv.vf_curve_manual_editor import user_edited_profile_payload
from profiles.uv.profile_store import archive_auto_uv_profile, profile_payload_from_path


def profile_curve_plan(profile: dict) -> list[dict]:
    plan = curve_plan_from_payload(profile)
    if plan:
        return plan
    payload = profile_payload_from_path(profile)
    return [] if payload is None else curve_plan_from_payload(payload)


def profile_base_curve_points(profile: dict) -> list[tuple[float, float]]:
    points = base_curve_points_from_payload(profile)
    if points:
        return points
    payload = profile_payload_from_path(profile)
    return [] if payload is None else base_curve_points_from_payload(payload)


def curve_plan_from_payload(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    for key in ("plan", "points"):
        plan = curve_plan_from_values(payload.get(key))
        if plan:
            return plan
    materialization = payload.get("materialization")
    if isinstance(materialization, dict):
        return curve_plan_from_values(materialization.get("points"))
    return []


def base_curve_points_from_payload(payload: dict) -> list[tuple[float, float]]:
    if not isinstance(payload, dict):
        return []
    for key in ("points", "plan", "curve_points"):
        points = base_curve_points_from_values(payload.get(key))
        if points:
            return points
    return []


def curve_points_from_values(values) -> list[tuple[float, float]]:
    if not isinstance(values, list):
        return []
    points = [
        point
        for value in values
        if (point := curve_point_from_value(value)) is not None
    ]
    return sorted(points, key=lambda point: (point[0], point[1]))


def base_curve_points_from_values(values) -> list[tuple[float, float]]:
    if not isinstance(values, list):
        return []
    points = [
        point
        for value in values
        if (point := base_curve_point_from_value(value)) is not None
    ]
    return sorted(points, key=lambda point: (point[0], point[1]))


def curve_plan_from_values(values) -> list[dict]:
    if not isinstance(values, list):
        return []
    plan = []
    for value in values:
        if not isinstance(value, dict):
            continue
        try:
            item = {
                "index": int(value["index"]),
                "voltage_mv": round(float(value["voltage_mv"])),
                "base_mhz": round(float(value["base_mhz"])),
                "target_mhz": round(float(value["target_mhz"])),
            }
        except (KeyError, TypeError, ValueError):
            continue
        try:
            item["new_offset_mhz"] = int(
                value.get("new_offset_mhz", item["target_mhz"] - item["base_mhz"])
            )
        except (TypeError, ValueError):
            item["new_offset_mhz"] = item["target_mhz"] - item["base_mhz"]
        plan.append(item)
    return sorted(plan, key=lambda item: (item["voltage_mv"], item["index"]))


def curve_point_from_value(value) -> tuple[float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        voltage = value[0]
        clock = value[1]
    elif isinstance(value, dict):
        voltage = curve_point_value(value, "voltage_mv", "voltage", "mv", "x")
        clock = curve_point_value(
            value,
            "clock_mhz",
            "target_mhz",
            "frequency_mhz",
            "base_mhz",
            "mhz",
            "y",
        )
    else:
        return None
    return finite_curve_point(voltage, clock)


def base_curve_point_from_value(value) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    voltage = curve_point_value(value, "voltage_mv", "voltage", "mv", "x")
    clock = curve_point_value(
        value,
        "base_mhz",
        "base_clock_mhz",
        "clock_mhz",
        "target_mhz",
        "frequency_mhz",
        "mhz",
        "y",
    )
    return finite_curve_point(voltage, clock)


def finite_curve_point(voltage, clock) -> tuple[float, float] | None:
    try:
        voltage_value = float(voltage)
        clock_value = float(clock)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(voltage_value) or not math.isfinite(clock_value):
        return None
    return voltage_value, clock_value


def curve_point_value(point: dict, *keys: str):
    for key in keys:
        value = point.get(key)
        if value not in (None, ""):
            return value
    return None


def load_cached_base_curve_points() -> list[tuple[float, float]]:
    try:
        payload = json.loads(
            base_curve_cache_path().read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    gpu_index = payload.get("gpu_index")
    if gpu_index not in (None, ""):
        try:
            if int(gpu_index) != runtime_gpu_index(default_runtime_config_path()):
                return []
        except (TypeError, ValueError):
            return []
    return curve_points_from_values(payload.get("points"))


def save_cached_base_curve_points(points: list[tuple[float, float]]) -> None:
    normalized = curve_points_from_values(points)
    if not normalized:
        return
    payload = {
        "format_version": 1,
        "saved_at": datetime.now().astimezone().isoformat(),
        "gpu_index": runtime_gpu_index(default_runtime_config_path()),
        "points": [
            {"voltage_mv": float(voltage), "clock_mhz": float(clock)}
            for voltage, clock in normalized
        ],
    }
    path = base_curve_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a crash mid-write must not leave a torn cache that a
        # later read then chokes on.
        atomic_write_json(path, payload)
    except OSError:
        return


def base_curve_cache_path() -> Path:
    return default_user_config_dir() / "base-vf-curve.json"


def save_edited_curve_profile(
    profile: dict,
    edit,
    *,
    original_anchor_voltage_mv: int,
    original_anchor_clock_mhz: int,
) -> tuple[Path, dict]:
    parent_payload = profile_payload_from_path(profile) or dict(profile)
    payload = user_edited_profile_payload(
        parent_payload,
        edit,
        original_anchor_voltage_mv=int(original_anchor_voltage_mv),
        original_anchor_clock_mhz=int(original_anchor_clock_mhz),
    )
    return archive_auto_uv_profile(payload), payload
