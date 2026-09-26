"""Load saved Auto-UV profiles for runtime application.

The loader turns archived JSON into a V/F apply plan and optional memory offset policy.
"""

from __future__ import annotations

import json

from auto_uv.curve.rising_tail import tail_ceiling_clock_mhz
from auto_uv.gpu.memory_clock_offset_user_option import (
    driver_memory_offset_limit_mhz,
)
from common.penguin_burner_errors import NvmlError
from integrations.afterburner.policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ

from .profile_store import STOCK_PROFILE_SELECTOR, resolve_auto_uv_profile
from .profile_tiers import profile_tier_summary_fields


def load_auto_uv_final_curve(profile_selector="", *, allow_unverified: bool = False):
    selector = str(profile_selector or "").strip()
    if selector == STOCK_PROFILE_SELECTOR:
        # Reserved "keep stock" runtime: apply NO undervolt curve. Distinct from
        # an empty selector, which falls back to the latest saved profile.
        return None
    resolved_profile = resolve_auto_uv_profile(
        selector or "latest",
        allow_unverified=bool(allow_unverified),
    )
    if resolved_profile is None:
        if selector:
            raise NvmlError(f"Auto-UV profile not found: {selector}")
        return None
    path, profile_payload = resolved_profile
    if not path.is_file():
        return None

    payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    raw_points = payload.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raw_points = payload.get("plan")
    if not isinstance(raw_points, list) or not raw_points:
        raise NvmlError(f"auto-UV final curve has no V/F points: {path}")

    plan = []
    for raw in raw_points:
        if not isinstance(raw, dict):
            raise NvmlError(f"auto-UV final curve contains a non-object point: {path}")
        try:
            item = {
                "index": int(raw["index"]),
                "voltage_mv": int(raw["voltage_mv"]),
                "base_mhz": int(raw["base_mhz"]),
                "target_mhz": int(raw["target_mhz"]),
                "new_offset_mhz": int(raw["new_offset_mhz"]),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise NvmlError(
                f"auto-UV final curve point is invalid: {path}: {raw}"
            ) from exc
        plan.append(item)

    lock_clock_mhz = int(payload.get("lock_clock_mhz", 0) or 0)
    candidate_voltage_mv = int(payload.get("candidate_voltage_mv", 0) or 0)
    if lock_clock_mhz <= 0 or candidate_voltage_mv <= 0:
        raise NvmlError(f"auto-UV final curve is missing lock clock or voltage: {path}")
    # No static cap at load time: apply_auto_uv_profile_memory_offset clamps
    # against the driver-reported limit, which can exceed the fallback cap.
    memory_offset_mhz = profile_memory_offset_mhz(payload, limit_mhz=None)
    power_limit_w = profile_power_limit_w(payload)

    voltage_bins = sorted({int(item["voltage_mv"]) for item in plan})
    tail_point_count = sum(
        1 for item in plan if int(item["voltage_mv"]) >= int(candidate_voltage_mv)
    )
    flatten_target = {
        "source": "auto-uv-final",
        "lock_clock_mhz": int(lock_clock_mhz),
        "lock_voltage_mv": int(candidate_voltage_mv),
        "end_voltage_mv": int(voltage_bins[-1]),
        "tail_point_count": int(tail_point_count),
    }
    ceiling_clock_mhz = tail_ceiling_clock_mhz(
        plan,
        fallback_clock_mhz=int(lock_clock_mhz),
        lock_voltage_mv=int(candidate_voltage_mv),
    )
    if int(ceiling_clock_mhz) > int(lock_clock_mhz):
        flatten_target["ceiling_clock_mhz"] = int(ceiling_clock_mhz)
    saved_flatten_target = payload.get("flatten_target")
    saved_tail_rise_bins = (
        saved_flatten_target.get("tail_rise_bins")
        if isinstance(saved_flatten_target, dict)
        else payload.get("tail_rise_bins")
    )
    if saved_tail_rise_bins is not None:
        try:
            flatten_target["tail_rise_bins"] = max(0, int(saved_tail_rise_bins))
        except (TypeError, ValueError):
            pass
    tier_fields = profile_tier_summary_fields(payload)
    return {
        "path": path,
        "profile_id": str(
            payload.get("profile_id") or profile_payload.get("profile_id") or ""
        ),
        "candidate_id": str(
            payload.get("candidate_id") or profile_payload.get("candidate_id") or ""
        ),
        **tier_fields,
        "plan": plan,
        "lock_clock_mhz": int(lock_clock_mhz),
        "candidate_voltage_mv": int(candidate_voltage_mv),
        "memory_offset_mhz": memory_offset_mhz,
        "power_limit_w": power_limit_w,
        "q2rtx_resolution": payload.get("q2rtx_resolution"),
        # Scan-measured load power (profile vs stock baseline): the runtime
        # spec forwards these so the daemon can account energy saved.
        "avg_power_w": payload.get("avg_power_w"),
        "base_avg_power_w": payload.get("base_avg_power_w"),
        "flatten_target": flatten_target,
    }


def profile_memory_offset_mhz(
    payload, *, limit_mhz: int | None = MAX_AFTERBURNER_MEM_OFFSET_MHZ
):
    """Parse the profile's memory offset, clamped to ``limit_mhz``.

    Pass ``limit_mhz=None`` to skip the cap (the apply path clamps against the
    driver-reported limit instead, which can exceed the static fallback).
    """
    for key in ("memory_offset_mhz", "mem_clk_vf_offset_mhz"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if value in (None, ""):
            continue
        try:
            offset = max(0, round(float(value)))
        except (TypeError, ValueError):
            return None
        if limit_mhz is not None:
            offset = min(int(limit_mhz), offset)
        return offset
    return None


def profile_power_limit_w(payload):
    value = payload.get("power_limit_w") if isinstance(payload, dict) else None
    if value in (None, ""):
        return None
    try:
        power_limit = round(float(value))
    except (TypeError, ValueError):
        return None
    return power_limit if power_limit > 0 else None


def apply_auto_uv_profile_memory_offset(
    *,
    profile_label: str,
    memory_offset_mhz,
    gpu_policy_controller,
) -> dict:
    memory_offset = profile_memory_offset_mhz(
        {"memory_offset_mhz": memory_offset_mhz},
        limit_mhz=driver_memory_offset_limit_mhz(gpu_policy_controller),
    )
    if memory_offset is None:
        return {}
    if gpu_policy_controller is None:
        if int(memory_offset) == 0:
            return {"mem_clk_vf_offset_mhz": int(memory_offset)}
        raise NvmlError(
            "failed to apply saved profile memory offset "
            f"{int(memory_offset):+d} MHz for {profile_label}: "
            "Linux GPU policy helper is unavailable"
        )
    try:
        gpu_policy_controller.apply_clock_offsets(
            mem_clk_vf_offset_mhz=int(memory_offset)
        )
    except Exception as exc:
        raise NvmlError(
            "failed to apply saved profile memory offset "
            f"{int(memory_offset):+d} MHz for {profile_label}: "
            f"driver rejected nvmlDeviceSetMemClkVfOffset: {exc}"
        ) from exc
    return {"mem_clk_vf_offset_mhz": int(memory_offset)}
