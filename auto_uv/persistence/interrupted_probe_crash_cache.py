from __future__ import annotations

import json
from pathlib import Path

from .auto_uv_persisted_json_files import probe_in_progress_path
from .unsafe_voltage_blacklist_file import record_unsafe_voltage


CRASH_CACHE_MIN_VOLTAGE_DROP_PCT = 5.0
CRASH_CACHE_MIN_CANDIDATE_TARGET_BASELINE_PCT = 95.0
CRASH_CACHE_CANDIDATE_PHASES = {"candidate", "final-verify"}


def consume_interrupted_probe_crash_marker() -> tuple[Path, dict] | None:
    path = probe_in_progress_path()
    if not path.is_file():
        return None
    try:
        marker = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        marker = {}
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    if not isinstance(marker, dict) or marker.get("state") != "probing":
        return None
    try:
        candidate_voltage_mv = int(marker["candidate_voltage_mv"])
        lock_clock_mhz = int(marker["lock_clock_mhz"])
    except (KeyError, TypeError, ValueError):
        return None

    validation = interrupted_marker_crash_cache_validation(marker)
    if not bool(validation.get("accepted")):
        return None
    details = marker_details(marker)
    return record_unsafe_voltage(
        candidate_voltage_mv=candidate_voltage_mv,
        lock_clock_mhz=lock_clock_mhz,
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


def interrupted_marker_crash_cache_validation(marker: dict) -> dict:
    phase = str(marker.get("phase") or "")
    candidate_voltage_mv = marker_float(marker, "candidate_voltage_mv")
    start_voltage_mv = marker_float(marker, "start_voltage_mv")
    voltage_drop_pct = marker_float(marker, "voltage_drop_from_start_pct")
    if (
        voltage_drop_pct is None
        and candidate_voltage_mv is not None
        and start_voltage_mv is not None
        and float(start_voltage_mv) > 0.0
    ):
        voltage_drop_pct = (
            (float(start_voltage_mv) - float(candidate_voltage_mv))
            / float(start_voltage_mv)
            * 100.0
        )

    target_clock_pct_of_baseline = normal_candidate_target_clock_pct(marker)
    validation = {
        "phase": phase,
        "voltage_drop_from_start_pct": (
            round(float(voltage_drop_pct), 4)
            if voltage_drop_pct is not None
            else None
        ),
        "target_clock_pct_of_baseline": (
            round(float(target_clock_pct_of_baseline), 4)
            if target_clock_pct_of_baseline is not None
            else None
        ),
        "min_voltage_drop_pct": float(CRASH_CACHE_MIN_VOLTAGE_DROP_PCT),
        "min_candidate_target_baseline_pct": float(
            CRASH_CACHE_MIN_CANDIDATE_TARGET_BASELINE_PCT
        ),
    }
    if normal_candidate_crash_marker_accepted(
        phase=phase,
        voltage_drop_pct=voltage_drop_pct,
        target_clock_pct_of_baseline=target_clock_pct_of_baseline,
    ):
        validation["accepted"] = True
        validation["reason"] = "validated candidate hard-hang marker"
        return validation
    if voltage_drop_pct is None or target_clock_pct_of_baseline is None:
        validation["accepted"] = False
        validation["reason"] = "missing crash-cache validation metrics"
        return validation
    validation["accepted"] = False
    validation["reason"] = "unsupported crash-marker phase"
    return validation


def normal_candidate_crash_marker_accepted(
    *,
    phase: str,
    voltage_drop_pct: float | None,
    target_clock_pct_of_baseline: float | None,
) -> bool:
    if str(phase) not in CRASH_CACHE_CANDIDATE_PHASES:
        return False
    if voltage_drop_pct is None or target_clock_pct_of_baseline is None:
        return False
    return (
        float(voltage_drop_pct) >= float(CRASH_CACHE_MIN_VOLTAGE_DROP_PCT)
        and float(target_clock_pct_of_baseline)
        >= float(CRASH_CACHE_MIN_CANDIDATE_TARGET_BASELINE_PCT)
    )


def normal_candidate_target_clock_pct(marker: dict) -> float | None:
    target_pct = marker_float(marker, "target_clock_pct_of_baseline")
    if target_pct is not None:
        return float(target_pct)
    lock_clock_mhz = marker_float(marker, "lock_clock_mhz")
    baseline_clock_mhz = marker_float(marker, "initial_probe_clock_mhz")
    if baseline_clock_mhz is None:
        baseline_clock_mhz = marker_float(marker, "baseline_clock_mhz")
    if (
        lock_clock_mhz is None
        or baseline_clock_mhz is None
        or float(baseline_clock_mhz) <= 0.0
    ):
        return None
    return float(lock_clock_mhz) / float(baseline_clock_mhz) * 100.0


def marker_details(marker: dict) -> dict:
    details = marker.get("details")
    return details if isinstance(details, dict) else {}


def marker_float(marker: dict, key: str) -> float | None:
    for source in (marker, marker_details(marker)):
        try:
            value = source.get(key)
        except AttributeError:
            continue
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None
