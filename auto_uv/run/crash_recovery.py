from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from auto_uv.curve.vf_curve_flattening import build_flattened_plan
from auto_uv.domain.console_log import log_phase
from auto_uv.domain.events import AutoUvEventCallback, emit_auto_uv_event
from auto_uv.domain.types import AutoUvProbeSummary
from auto_uv.persistence.auto_uv_persisted_json_files import final_choice_request_path
from auto_uv.persistence.interrupted_probe_crash_cache import (
    consume_interrupted_probe_crash_marker,
)
from auto_uv.persistence.unsafe_voltage_blacklist_file import (
    load_unsafe_voltage_blacklist,
)
from auto_uv.persistence.unsafe_voltage_cache import (
    adaptive_scan_payload,
    unsafe_voltage_block_reason,
)
from auto_uv.probes.event_payload import probe_summary_event_payload
from auto_uv.scan_mode.auto_uv_mode import (
    AUTO_UV_MODE_ADAPTIVE,
    AUTO_UV_MODE_PERFORMANCE,
)
from auto_uv.shared.positive_int import positive_int
from profiles.uv.profile_tiers import generated_profile_tier, normalize_profile_tier
from ui.features.auto_uv.candidate_choice import candidate_plan_from_record


class CrashCacheEntries(list):
    def __init__(
        self,
        entries=(),
        *,
        interrupted_entry: dict | None = None,
    ) -> None:
        super().__init__(entries)
        self.interrupted_entry = interrupted_entry


def consume_crash_cache(*, log: Callable[[str], None]) -> list[dict]:
    interrupted = consume_interrupted_probe_crash_marker()
    interrupted_entry = None
    if interrupted is not None:
        _path, unsafe_entry = interrupted
        interrupted_entry = dict(unsafe_entry)
        log_phase(
            log,
            "crash-cache",
            "previous auto-UV probe ended abruptly; "
            f"blacklisted={int(unsafe_entry['candidate_voltage_mv'])}mV "
            f"target={int(unsafe_entry['lock_clock_mhz'])}MHz",
        )
    return CrashCacheEntries(
        load_unsafe_voltage_blacklist(),
        interrupted_entry=interrupted_entry,
    )


def crash_recovery_entry_from_cache(unsafe_entries: list[dict]) -> dict | None:
    interrupted_entry = getattr(unsafe_entries, "interrupted_entry", None)
    if isinstance(interrupted_entry, dict):
        return dict(interrupted_entry)
    return None


def next_safer_recovery_candidate_id(
    candidate_records: list[dict],
    *,
    failed_voltage_mv: int | None,
    auto_uv_mode: str,
) -> str:
    if failed_voltage_mv is None:
        return ""
    candidates = []
    for record in candidate_records:
        if not isinstance(record, dict) or not candidate_plan_from_record(record):
            continue
        voltage_mv = positive_int(record.get("candidate_voltage_mv"))
        candidate_id = str(record.get("candidate_id", "")).strip()
        if voltage_mv is None or not candidate_id:
            continue
        if int(voltage_mv) <= int(failed_voltage_mv):
            continue
        candidates.append(dict(record))
    if not candidates:
        return ""
    next_voltage_mv = min(
        int(candidate["candidate_voltage_mv"]) for candidate in candidates
    )
    same_bin = [
        candidate
        for candidate in candidates
        if int(candidate.get("candidate_voltage_mv") or 0) == int(next_voltage_mv)
    ]
    metric_name = (
        "avg_fps"
        if str(auto_uv_mode) == AUTO_UV_MODE_PERFORMANCE
        else "efficiency_fps_per_w"
    )
    return str(
        max(
            same_bin,
            key=lambda candidate: (
                _float_or_negative_infinity(candidate.get(metric_name)),
                _float_or_negative_infinity(candidate.get("avg_fps")),
                int(candidate.get("lock_clock_mhz") or 0),
            ),
        ).get("candidate_id", "")
    )


def recovery_candidate_records_for_failed_run(
    candidate_records: list[dict],
    *,
    crash_recovery_entry: dict | None,
    target_profile_tier: object | None = None,
) -> list[dict]:
    target_tier = normalize_profile_tier(target_profile_tier)
    if isinstance(crash_recovery_entry, dict) and target_tier:
        crash_tier = crash_recovery_entry_profile_tier(crash_recovery_entry)
        if crash_tier and crash_tier != target_tier:
            return []

    records = [
        dict(record)
        for record in candidate_records
        if isinstance(record, dict)
        and positive_int(record.get("candidate_voltage_mv")) is not None
        and candidate_plan_from_record(record)
    ]
    if target_tier:
        records = [
            record for record in records if generated_profile_tier(record) == target_tier
        ]
    if not records or not isinstance(crash_recovery_entry, dict):
        return records

    failed_voltage_mv = positive_int(crash_recovery_entry.get("candidate_voltage_mv"))
    if failed_voltage_mv is None:
        return records

    start_index = None
    for index in range(len(records) - 1, -1, -1):
        voltage_mv = positive_int(records[index].get("candidate_voltage_mv"))
        if voltage_mv is not None and int(voltage_mv) >= int(failed_voltage_mv):
            start_index = index
            break
    if start_index is None:
        return records

    crashed_run = []
    previous_voltage_mv = None
    for index in range(start_index, -1, -1):
        record = records[index]
        voltage_mv = positive_int(record.get("candidate_voltage_mv"))
        if voltage_mv is None:
            break
        if (
            previous_voltage_mv is not None
            and int(voltage_mv) <= int(previous_voltage_mv)
        ):
            break
        crashed_run.append(record)
        previous_voltage_mv = int(voltage_mv)

    crashed_run = crashed_run or records
    return crashed_run


def final_choice_request_recovery_records(
    base_curve: list[dict],
    *,
    fallback_records: list[dict],
    failed_voltage_mv: int | None,
    failed_lock_clock_mhz: int | None,
    auto_uv_mode: str,
    tail_rise_bins: int,
) -> list[dict]:
    path = final_choice_request_path()
    if not path.is_file():
        return fallback_records
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return fallback_records
    if not isinstance(payload, dict):
        return fallback_records
    if normalize_profile_tier(payload.get("auto_uv_mode")) != normalize_profile_tier(
        auto_uv_mode
    ):
        return fallback_records
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return fallback_records

    failed_voltage = positive_int(failed_voltage_mv)
    failed_clock = positive_int(failed_lock_clock_mhz)
    if failed_voltage is not None and not final_choice_request_matches_failed_candidate(
        candidates,
        failed_voltage_mv=int(failed_voltage),
        failed_lock_clock_mhz=failed_clock,
    ):
        return fallback_records

    records = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        voltage_mv = positive_int(candidate.get("candidate_voltage_mv"))
        lock_clock_mhz = positive_int(candidate.get("lock_clock_mhz"))
        if voltage_mv is None or lock_clock_mhz is None:
            continue
        record = dict(candidate)
        record["candidate_id"] = str(
            record.get("candidate_id") or f"{voltage_mv}mv-{lock_clock_mhz}mhz"
        )
        record["candidate_voltage_mv"] = int(voltage_mv)
        record["lock_clock_mhz"] = int(lock_clock_mhz)
        record["tail_rise_bins"] = int(tail_rise_bins)
        if not candidate_plan_from_record(record):
            try:
                record["plan"] = build_flattened_plan(
                    base_curve,
                    lock_clock_mhz=int(lock_clock_mhz),
                    candidate_voltage_mv=int(voltage_mv),
                    tail_rise_bins=int(tail_rise_bins),
                )
            except ValueError:
                continue
        records.append(record)
    return records or fallback_records


def final_choice_request_matches_failed_candidate(
    candidates: list,
    *,
    failed_voltage_mv: int,
    failed_lock_clock_mhz: int | None,
) -> bool:
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        voltage_mv = positive_int(candidate.get("candidate_voltage_mv"))
        lock_clock_mhz = positive_int(candidate.get("lock_clock_mhz"))
        if voltage_mv is None:
            continue
        if int(voltage_mv) != int(failed_voltage_mv):
            continue
        if failed_lock_clock_mhz is None:
            return True
        if lock_clock_mhz is not None and int(lock_clock_mhz) == int(
            failed_lock_clock_mhz
        ):
            return True
    return False


def crash_recovery_entry_profile_tier(entry: dict) -> str:
    for payload in crash_recovery_entry_tier_payloads(entry):
        tier = explicit_profile_tier(payload)
        if tier:
            return tier
    return ""


def crash_recovery_entry_tier_payloads(entry: dict):
    if isinstance(entry, dict):
        yield entry
        details = entry.get("details")
        if isinstance(details, dict):
            yield details
            marker_details = details.get("marker_details")
            if isinstance(marker_details, dict):
                yield marker_details


def explicit_profile_tier(payload: dict) -> str:
    for key in (
        "generated_profile_tier",
        "profile_generated_tier",
        "auto_uv_requested_mode",
        "auto_uv_profile_tier",
        "profile_tier",
    ):
        tier = normalize_profile_tier(payload.get(key))
        if tier:
            return tier
    mode = normalize_profile_tier(payload.get("auto_uv_mode"))
    if mode == AUTO_UV_MODE_PERFORMANCE:
        return AUTO_UV_MODE_PERFORMANCE
    if adaptive_scan_payload(payload):
        return ""
    tail_rise_bins = _int_or_none(payload.get("tail_rise_bins"))
    if tail_rise_bins is not None:
        return generated_profile_tier({"tail_rise_bins": int(tail_rise_bins)})
    return ""


def _int_or_none(value: object) -> int | None:
    try:
        return int(cast(Any, value))
    except (TypeError, ValueError):
        return None


def auto_uv_run_profile_tier(
    runtime_options: dict,
    settings,
    *,
    tail_rise_bins: int,
) -> str:
    options = runtime_options if isinstance(runtime_options, dict) else {}
    requested = normalize_profile_tier(
        options.get("auto_uv_requested_mode")
        or getattr(settings, "auto_uv_requested_mode", None)
    )
    if requested:
        return requested
    # Adaptive runs sweep all three tiers in one scan: no single tier, so
    # crash markers and the pre-scan recovery filter stay tier-agnostic
    # instead of falling into tail-bins inference (flat tail = efficiency).
    if _adaptive_auto_uv_run(options, settings):
        return ""
    runtime_mode = normalize_profile_tier(options.get("auto_uv_mode"))
    if runtime_mode == AUTO_UV_MODE_PERFORMANCE:
        return AUTO_UV_MODE_PERFORMANCE
    settings_mode = normalize_profile_tier(getattr(settings, "auto_uv_mode", None))
    if settings_mode == AUTO_UV_MODE_PERFORMANCE:
        return AUTO_UV_MODE_PERFORMANCE
    runtime_tail_rise_bins = _int_or_none(options.get("auto_uv_tail_rise_bins"))
    if runtime_tail_rise_bins is not None:
        return generated_profile_tier({"tail_rise_bins": int(runtime_tail_rise_bins)})
    return generated_profile_tier(
        {
            "auto_uv_mode": settings_mode or runtime_mode,
            "tail_rise_bins": int(tail_rise_bins),
        }
    )


def _adaptive_auto_uv_run(options: dict, settings) -> bool:
    for value in (
        options.get("auto_uv_requested_mode"),
        options.get("auto_uv_mode"),
        getattr(settings, "auto_uv_requested_mode", None),
        getattr(settings, "auto_uv_mode", None),
    ):
        if str(value or "").strip().lower() == AUTO_UV_MODE_ADAPTIVE:
            return True
    return False


def auto_uv_run_marker_details(
    runtime_options: dict,
    settings,
    *,
    tail_rise_bins: int,
    profile_tier: str,
) -> dict:
    details = {
        "auto_uv_mode": str(getattr(settings, "auto_uv_mode", "") or ""),
        "generated_profile_tier": str(profile_tier or ""),
        "tail_rise_bins": int(tail_rise_bins),
    }
    requested_mode = str(runtime_options.get("auto_uv_requested_mode") or "").strip()
    if requested_mode:
        details["auto_uv_requested_mode"] = requested_mode
    return details


def recovery_initial_target_voltage_mv(
    candidate_records: list[dict],
    *,
    fallback_voltage_mv: int | None,
) -> int:
    voltages = []
    for record in candidate_records:
        if not isinstance(record, dict):
            continue
        voltage_mv = positive_int(record.get("candidate_voltage_mv"))
        if voltage_mv is not None:
            voltages.append(int(voltage_mv))
    if voltages:
        return max(voltages)
    return int(fallback_voltage_mv or 1)


def crash_recovery_decision(entry: dict) -> dict:
    details = entry.get("details")
    details = details if isinstance(details, dict) else {}
    log_path = str(details.get("log_path") or "").strip()
    error_excerpt = previous_run_error_excerpt(log_path)
    result_reason = str(details.get("result_reason") or "").strip()
    shutdown_mode = str(details.get("shutdown_mode") or "").strip()
    decision = error_excerpt or result_reason or shutdown_mode or str(
        entry.get("reason") or "previous unsafe point"
    )
    return {
        "candidate_voltage_mv": positive_int(entry.get("candidate_voltage_mv")),
        "lock_clock_mhz": positive_int(entry.get("lock_clock_mhz")),
        "recorded_at": str(entry.get("recorded_at") or ""),
        "reason": str(entry.get("reason") or ""),
        "phase": str(entry.get("phase") or ""),
        "decision": decision,
        "result_reason": result_reason,
        "shutdown_mode": shutdown_mode,
        "process_exit_code": details.get("process_exit_code"),
        "log_path": log_path,
    }


def previous_run_error_excerpt(log_path: str) -> str:
    path_text = str(log_path or "").strip()
    if not path_text:
        return ""
    path = Path(path_text)
    if not path.is_file():
        return ""
    patterns = ("Device lost", "device lost", "Xid", "ERROR", "Error", "fatal")
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = " ".join(str(line).strip().split())
                if not stripped:
                    continue
                if any(pattern in stripped for pattern in patterns):
                    return stripped[:240]
    except OSError:
        return ""
    return ""


def append_unique_probe_summary(
    history: list[AutoUvProbeSummary],
    probe: AutoUvProbeSummary | None,
) -> None:
    if probe is None:
        return
    for existing in history:
        if int(existing.candidate_voltage_mv) == int(probe.candidate_voltage_mv) and int(
            existing.lock_clock_mhz
        ) == int(probe.lock_clock_mhz):
            return
    history.append(probe)


def probe_summary_from_candidate_record(record: dict) -> AutoUvProbeSummary | None:
    voltage_mv = positive_int(record.get("candidate_voltage_mv"))
    lock_clock_mhz = positive_int(record.get("lock_clock_mhz"))
    if voltage_mv is None or lock_clock_mhz is None:
        return None
    avg_voltage_mv = _float_or_none(
        record.get("avg_voltage_mv"),
        record.get("loaded_median_voltage_mv"),
        voltage_mv,
    )
    log_path_text = str(record.get("log_path") or "/dev/null")
    return AutoUvProbeSummary(
        candidate_voltage_mv=int(voltage_mv),
        lock_clock_mhz=int(lock_clock_mhz),
        live_voltage_before_mv=int(voltage_mv),
        live_voltage_after_mv=int(voltage_mv),
        avg_voltage_mv=avg_voltage_mv,
        frames_per_run=None,
        avg_seconds_per_run=None,
        avg_fps=_float_or_none(record.get("avg_fps")),
        min_fps=_float_or_none(record.get("min_fps")),
        max_fps=_float_or_none(record.get("max_fps")),
        avg_power_w=_float_or_none(record.get("avg_power_w")),
        max_power_w=_float_or_none(record.get("max_power_w")),
        avg_temperature_c=_float_or_none(record.get("avg_temperature_c")),
        max_temperature_c=_float_or_none(record.get("max_temperature_c")),
        avg_fan_speed_pct=_float_or_none(record.get("avg_fan_speed_pct")),
        max_fan_speed_pct=_float_or_none(record.get("max_fan_speed_pct")),
        avg_core_clock_mhz=_float_or_none(record.get("avg_core_clock_mhz")),
        efficiency_fps_per_w=_float_or_none(record.get("efficiency_fps_per_w")),
        efficiency_mhz_per_w=_float_or_none(record.get("efficiency_mhz_per_w")),
        watts_per_mhz=_float_or_none(record.get("watts_per_mhz")),
        used_companion_load=bool(record.get("used_companion_load", True)),
        result_reason=str(record.get("reason") or "recovered saved candidate"),
        log_path=Path(log_path_text),
        loaded_median_voltage_mv=_float_or_none(
            record.get("loaded_median_voltage_mv")
        ),
        loaded_median_core_clock_mhz=_float_or_none(
            record.get("loaded_median_core_clock_mhz")
        ),
        loaded_p90_core_clock_mhz=_float_or_none(
            record.get("loaded_p90_core_clock_mhz")
        ),
        loaded_qualified_sample_count=int(
            record.get("loaded_qualified_sample_count") or 0
        ),
        observed_vdroop_mv=_float_or_none(record.get("observed_vdroop_mv")),
        perf_cap_reason=str(record.get("perf_cap_reason") or "") or None,
        hw_power_brake_samples=int(record.get("hw_power_brake_samples") or 0),
        fps_stddev=_float_or_none(record.get("fps_stddev")),
        fps_variance_pct=_float_or_none(record.get("fps_variance_pct")),
        tested_plan=candidate_plan_from_record(record) or None,
    )


def recovery_probe_history(
    records: list[dict], *, selected_record: dict, profile_tier: str,
) -> list[AutoUvProbeSummary]:
    """Restore tested fallback curves from the already selected failed-run block."""
    regime_fields = (
        "configured_power_limit_w", "tail_rise_bins",
        "base_candidate_voltage_mv", "base_lock_clock_mhz", "base_avg_core_clock_mhz",
    )
    selected_plan = candidate_plan_from_record(selected_record)
    if not selected_plan:
        return []
    base_points = [
        (p.get("index"), p.get("voltage_mv"), p.get("base_mhz")) for p in selected_plan
    ]
    unsafe = load_unsafe_voltage_blacklist()
    history: list[AutoUvProbeSummary] = []
    for record in records:
        if any(record.get(field) != selected_record.get(field) for field in regime_fields):
            continue
        if generated_profile_tier(record) != generated_profile_tier(selected_record):
            continue
        try:
            plan = candidate_plan_from_record(record)
            if not plan or [
                (p.get("index"), p.get("voltage_mv"), p.get("base_mhz")) for p in plan
            ] != base_points:
                continue
            probe = probe_summary_from_candidate_record(record)
        except (ValueError, TypeError, KeyError):
            continue
        if probe is None or unsafe_voltage_block_reason(
            unsafe,
            candidate_voltage_mv=probe.candidate_voltage_mv,
            lock_clock_mhz=probe.lock_clock_mhz,
            profile_tier=profile_tier,
        ):
            continue
        append_unique_probe_summary(history, probe)
    return history


def base_probe_summary_from_candidate_record(record: dict) -> AutoUvProbeSummary | None:
    voltage_mv = positive_int(record.get("base_candidate_voltage_mv"))
    lock_clock_mhz = positive_int(record.get("base_lock_clock_mhz"))
    avg_core_clock_mhz = _float_or_none(record.get("base_avg_core_clock_mhz"))
    if voltage_mv is None or lock_clock_mhz is None or avg_core_clock_mhz is None:
        return None
    avg_voltage_mv = _float_or_none(record.get("base_avg_voltage_mv"), voltage_mv)
    return AutoUvProbeSummary(
        candidate_voltage_mv=int(voltage_mv),
        lock_clock_mhz=int(lock_clock_mhz),
        live_voltage_before_mv=int(voltage_mv),
        live_voltage_after_mv=int(voltage_mv),
        avg_voltage_mv=avg_voltage_mv,
        frames_per_run=None,
        avg_seconds_per_run=None,
        avg_fps=_float_or_none(record.get("base_avg_fps")),
        min_fps=_float_or_none(record.get("base_min_fps")),
        max_fps=_float_or_none(record.get("base_max_fps")),
        avg_power_w=_float_or_none(record.get("base_avg_power_w")),
        max_power_w=_float_or_none(record.get("base_max_power_w")),
        avg_temperature_c=_float_or_none(record.get("base_avg_temperature_c")),
        max_temperature_c=_float_or_none(record.get("base_max_temperature_c")),
        avg_fan_speed_pct=_float_or_none(record.get("base_avg_fan_speed_pct")),
        max_fan_speed_pct=_float_or_none(record.get("base_max_fan_speed_pct")),
        avg_core_clock_mhz=float(avg_core_clock_mhz),
        efficiency_fps_per_w=_float_or_none(record.get("base_efficiency_fps_per_w")),
        efficiency_mhz_per_w=_float_or_none(record.get("base_efficiency_mhz_per_w")),
        watts_per_mhz=_float_or_none(record.get("base_watts_per_mhz")),
        used_companion_load=bool(record.get("base_used_companion_load", True)),
        result_reason=str(record.get("base_reason") or "recovered baseline"),
        log_path=Path(str(record.get("base_log_path") or "/dev/null")),
        loaded_median_voltage_mv=_float_or_none(
            record.get("base_loaded_median_voltage_mv")
        ),
        loaded_median_core_clock_mhz=_float_or_none(
            record.get("base_loaded_median_core_clock_mhz")
        ),
        loaded_p90_core_clock_mhz=_float_or_none(
            record.get("base_loaded_p90_core_clock_mhz")
        ),
        loaded_qualified_sample_count=int(
            record.get("base_loaded_qualified_sample_count") or 0
        ),
        observed_vdroop_mv=_float_or_none(record.get("base_observed_vdroop_mv")),
        perf_cap_reason=str(record.get("base_perf_cap_reason") or "") or None,
        hw_power_brake_samples=int(
            record.get("base_hw_power_brake_samples") or 0
        ),
        fps_stddev=_float_or_none(record.get("base_fps_stddev")),
        fps_variance_pct=_float_or_none(record.get("base_fps_variance_pct")),
    )


def replay_recovered_resume_probe_rows(
    *,
    event_callback: AutoUvEventCallback | None,
    base_probe: AutoUvProbeSummary | None,
    stable_probe: AutoUvProbeSummary | None,
) -> None:
    if base_probe is not None:
        emit_auto_uv_event(
            event_callback,
            "probe_result",
            **probe_summary_event_payload(
                base_probe,
                stage="base-baseline",
                decision="pass",
                reason=str(base_probe.result_reason or "recovered baseline"),
            ),
        )
    if stable_probe is not None:
        emit_auto_uv_event(
            event_callback,
            "probe_result",
            **probe_summary_event_payload(
                stable_probe,
                stage="candidate",
                decision="pass",
                reason=str(stable_probe.result_reason or "recovered saved candidate"),
            ),
        )


def _float_or_negative_infinity(value: object) -> float:
    try:
        return float(cast(Any, value))
    except (TypeError, ValueError):
        return float("-inf")


def _float_or_none(*values: object) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(cast(Any, value))
        except (TypeError, ValueError):
            continue
    return None
