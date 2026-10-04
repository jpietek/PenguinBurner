"""Write the final Auto-UV JSON artifacts consumed by the UI and profile tools.

Short-probe candidates and final-verified profiles share one payload shape so humans can compare them directly.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from auto_uv.domain.types import AutoUvProbeSummary
from profiles.gpu_identity import normalized_gpu_identity

from ..persistence.auto_uv_persisted_json_files import (
    auto_uv_user_config_dir,
    safe_json_write,
)
from ..persistence.verified_candidate_result_file import (
    append_verified_candidate,
    verified_candidate_payload,
)

PROFILE_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def write_last_stable_result_snapshot(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    tail_rise_bins: int = 0,
) -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return safe_json_write(
        auto_uv_user_config_dir()
        / "uv-result"
        / f"auto-uv-last-stable-{timestamp}.json",
        verified_candidate_payload(
            plan=plan,
            lock_clock_mhz=int(lock_clock_mhz),
            voltage_mv=int(voltage_mv),
            probe=probe,
            reason="last-stable",
            label="last-stable",
            tail_rise_bins=int(tail_rise_bins),
        ),
    )


def write_final_stable_result(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    final_verification_probe: AutoUvProbeSummary | None = None,
    verification_duration_s: int | None = None,
    tail_rise_bins: int = 0,
    power_limit_w: int | None = None,
) -> Path:
    payload = verified_candidate_payload(
        plan=plan,
        lock_clock_mhz=int(lock_clock_mhz),
        voltage_mv=int(voltage_mv),
        probe=final_verification_probe or probe,
        reason="stable",
        label="final",
        tail_rise_bins=int(tail_rise_bins),
    )
    payload.update(
        {
            "stable": True,
            "verification_duration_s": (
                int(verification_duration_s)
                if verification_duration_s is not None
                else None
            ),
        }
    )
    _add_long_verification_clock(payload, final_verification_probe)
    if power_limit_w is not None:
        payload["power_limit_w"] = int(power_limit_w)
    return safe_json_write(
        auto_uv_user_config_dir() / "uv-result" / "auto-uv-final-stable.json",
        payload,
    )


def write_final_verified_profile(
    *,
    plan: list[dict],
    lock_clock_mhz: int,
    voltage_mv: int,
    probe: AutoUvProbeSummary | None,
    final_verification_probe: AutoUvProbeSummary | None = None,
    base_probe: AutoUvProbeSummary | None,
    fan_curve_payload: dict | None = None,
    memory_offset_mhz: int | None = None,
    power_limit_w: int | None = None,
    tail_rise_bins: int = 0,
    auto_uv_mode: str = "",
    generated_profile_tier: str = "",
    gpu_identity: dict | None = None,
    q2rtx_resolution: str | None = None,
) -> Path:
    # The plan is archived EXACTLY as final verification proved it. The old
    # save-time "verified envelope" raised below-lock bins to the best clock
    # any archived probe had ever passed at that voltage — but the archive
    # spans previous scans (other memory offsets, tails, days), so profiles
    # shipped mid-ramp clocks up to ~150MHz beyond anything the current scan
    # validated, and did so AFTER the final soak (which pins at the lock
    # voltage and never operates those bins). Real games dip through the
    # mid-ramp on every load transition and crashed there repeatedly
    # (2026-07-13). Never decorate the shipped curve past verification.
    payload = verified_candidate_payload(
        plan=plan,
        lock_clock_mhz=int(lock_clock_mhz),
        voltage_mv=int(voltage_mv),
        probe=final_verification_probe or probe,
        base_probe=base_probe,
        reason="final-verified",
        label="final-verified",
        final_verified=True,
        tail_rise_bins=int(tail_rise_bins),
    )
    _add_long_verification_clock(payload, final_verification_probe)
    if isinstance(fan_curve_payload, dict):
        payload["fan_curve_payload"] = dict(fan_curve_payload)
    if memory_offset_mhz is not None:
        payload["memory_offset_mhz"] = int(memory_offset_mhz)
    if power_limit_w is not None:
        payload["power_limit_w"] = int(power_limit_w)
    if str(auto_uv_mode or "").strip():
        payload["auto_uv_mode"] = str(auto_uv_mode).strip()
    if str(generated_profile_tier or "").strip():
        payload["generated_profile_tier"] = str(generated_profile_tier).strip()
    if q2rtx_resolution is not None:
        payload["q2rtx_resolution"] = q2rtx_resolution
    normalized_identity = normalized_gpu_identity(gpu_identity or {})
    if str(normalized_identity.get("uuid") or "").strip():
        payload["gpu_identity"] = normalized_identity
    append_verified_candidate(payload)
    return archive_final_verified_profile(payload)


def _add_long_verification_clock(
    payload: dict,
    probe: AutoUvProbeSummary | None,
) -> None:
    if probe is None:
        return
    clock_mhz = probe.q2rtx_avg_core_clock_mhz
    if clock_mhz is None:
        clock_mhz = probe.avg_core_clock_mhz
    if clock_mhz is not None:
        payload["final_q2rtx_avg_core_clock_mhz"] = float(clock_mhz)
        payload["final_verification_metrics"] = True


def archive_final_verified_profile(final_curve_payload: dict) -> Path:
    timestamp = datetime.now().astimezone()
    created_at = timestamp.isoformat()
    candidate_id = profile_candidate_id(final_curve_payload)
    stamp = timestamp.strftime("%Y%m%d-%H%M%S-%f")
    profile_id = safe_profile_id(
        str(final_curve_payload.get("profile_id", "")).strip()
        or f"{stamp}-{candidate_id}"
    )
    payload = dict(final_curve_payload)
    payload.update(
        {
            "format_version": 1,
            "profile_id": profile_id,
            "profile_created_at": created_at,
            "profile_source": str(payload.get("profile_source") or "auto-uv-final"),
        }
    )
    return safe_json_write(
        auto_uv_user_config_dir()
        / "auto-uv-profiles"
        / f"auto-uv-profile-{profile_id}.json",
        payload,
    )


def profile_candidate_id(payload: dict) -> str:
    candidate_id = str(payload.get("candidate_id", "")).strip()
    if candidate_id:
        return safe_profile_id(candidate_id)
    voltage = payload.get("candidate_voltage_mv", payload.get("voltage_mv", "na"))
    clock = payload.get("lock_clock_mhz", payload.get("clock_mhz", "na"))
    return safe_profile_id(f"{voltage}mv-{clock}mhz")


def safe_profile_id(value: str) -> str:
    text = PROFILE_ID_SAFE_RE.sub("-", str(value).strip()).strip("-")
    return text or "profile"
