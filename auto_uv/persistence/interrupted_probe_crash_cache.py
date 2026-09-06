"""Avoid retrying an interrupted probe, regardless of its voltage or clock margin."""

from __future__ import annotations

import json
from pathlib import Path

from .auto_uv_persisted_json_files import probe_in_progress_path
from .unsafe_voltage_blacklist_file import record_unsafe_voltage


CRASH_CACHE_CANDIDATE_PHASES = {
    "candidate",
    "final-verify",
    "efficiency-candidate",
    "balanced-candidate",
    "performance-candidate",
}


def consume_interrupted_probe_crash_marker() -> tuple[Path, dict] | None:
    path = probe_in_progress_path()
    if not path.is_file():
        return None
    try:
        marker = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        marker = {}
    if not isinstance(marker, dict):
        path.unlink(missing_ok=True)
        return None

    validation = interrupted_marker_crash_cache_validation(marker)
    if not bool(validation.get("accepted")):
        path.unlink(missing_ok=True)
        return None
    details = marker_details(marker)
    recorded = record_unsafe_voltage(
        candidate_voltage_mv=validation["candidate_voltage_mv"],
        lock_clock_mhz=validation["lock_clock_mhz"],
        reason="previous-run-abruptly-ended",
        phase=str(marker.get("phase") or ""),
        marker_started_at=str(marker.get("started_at") or ""),
        blocked_lock_clock_mhz=details.get("blocked_lock_clock_mhz"),
        details={
            "marker_pid": marker.get("pid"),
            "marker_host": marker.get("host"),
            "marker_log_context": marker.get("log_context"),
            "marker_details": details,
            "crash_cache_validation": validation,
            "classification": (
                "stale probing marker remained on disk; clean Ctrl-C/SIGTERM "
                "cleanup removes this marker"
            ),
        },
    )
    # Preserve the marker if the durable blacklist write fails.
    path.unlink(missing_ok=True)
    return recorded


def interrupted_marker_crash_cache_validation(marker: dict) -> dict:
    phase = str(marker.get("phase") or "")
    validation: dict = {"phase": phase, "accepted": False}
    if marker.get("state") != "probing":
        validation["reason"] = "not an in-progress probe"
        return validation
    if phase not in CRASH_CACHE_CANDIDATE_PHASES:
        validation["reason"] = "unsupported crash-marker phase"
        return validation
    try:
        for key in ("candidate_voltage_mv", "lock_clock_mhz"):
            value = marker.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError("invalid candidate voltage or clock")
            parsed = int(value)
            if parsed <= 0 or float(value) != parsed:
                raise ValueError("invalid candidate voltage or clock")
            validation[key] = parsed
    except (TypeError, ValueError, OverflowError):
        validation["reason"] = "invalid candidate voltage or clock"
        return validation
    # A stale marker does not prove the GPU caused the exit, but repeating the
    # same V/F blindly is unsafe even on the first shallow voltage step.
    validation.update(accepted=True, reason="interrupted candidate probe")
    return validation


def marker_details(marker: dict) -> dict:
    details = marker.get("details")
    return details if isinstance(details, dict) else {}
