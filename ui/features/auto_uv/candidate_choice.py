"""Ask the UI which passed candidate should receive the final long verification.

Candidates are sorted by the active mode so the default pick matches the scan objective.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from auto_uv.scan_mode.auto_uv_mode import normalize_auto_uv_mode
from .final_choice_ranking import final_choice_sort_label
from .final_choice_ranking import sort_candidates_for_final_choice
from .final_choice_ranking import best_final_choice_candidate_id
from auto_uv.persistence.auto_uv_persisted_json_files import (
    auto_uv_stop_requested,
    final_choice_request_path,
    final_choice_response_path,
    safe_json_write,
)
from auto_uv.domain.types import (
    AutoUvFinalChoiceDiscarded,
    AutoUvProbeSummary,
)
from auto_uv.domain.events import AutoUvEventCallback, emit_auto_uv_event
from auto_uv.curve.vf_curve_flattening import build_flattened_plan


def choose_final_verification_candidate(
    *,
    log: Callable[[str], None],
    event_callback: AutoUvEventCallback | None,
    auto_uv_mode: str,
    base_probe: AutoUvProbeSummary | None,
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    stable_probe: AutoUvProbeSummary | None,
    stable_history: list[AutoUvProbeSummary],
    base_curve: list[dict],
    final_verification_duration_s: int,
    initial_target_voltage_mv: int,
    short_probe_base_duration_s: int,
    tail_rise_bins: int = 0,
    request_reason: str = "sweep-complete",
) -> tuple[list[dict], int, int, AutoUvProbeSummary | None, int]:
    normalized_mode = normalize_auto_uv_mode(auto_uv_mode)
    candidates_by_id = candidate_records_from_history(
        stable_history,
        base_curve=base_curve,
        stable_plan=stable_plan,
        stable_voltage_mv=int(stable_voltage_mv),
        stable_lock_clock_mhz=int(stable_lock_clock_mhz),
        tail_rise_bins=int(tail_rise_bins),
    )
    current_stable_id = selection_candidate_id(
        voltage_mv=int(stable_voltage_mv),
        lock_clock_mhz=int(stable_lock_clock_mhz),
    )
    candidates_by_id[current_stable_id] = current_stable_record(
        candidate_id=current_stable_id,
        stable_plan=stable_plan,
        stable_voltage_mv=int(stable_voltage_mv),
        stable_lock_clock_mhz=int(stable_lock_clock_mhz),
        stable_probe=stable_probe,
        existing_record=candidates_by_id.get(current_stable_id),
    )

    candidate_records = list(candidates_by_id.values())

    candidates, sort_label = sorted_final_choice_candidates(
        candidate_records,
        auto_uv_mode=normalized_mode,
        base_probe=base_probe,
    )
    default_id = (
        str(candidates[0].get("candidate_id", "")) if candidates else current_stable_id
    )
    # Adaptive tiers preselect the tier's confirmed candidate, not the
    # mode-metric best, so accepting the default saves this tier's own point.
    if str(request_reason or "").startswith("adaptive-") and any(
        str(candidate.get("candidate_id", "")) == current_stable_id
        for candidate in candidates
    ):
        default_id = current_stable_id
    selected, selected_final_duration_s = request_final_choice_candidate(
        log=log,
        event_callback=event_callback,
        auto_uv_mode=normalized_mode,
        base_probe=base_probe,
        candidates=candidates,
        default_candidate_id=default_id,
        sort_label=sort_label,
        final_verification_duration_s=int(final_verification_duration_s),
        initial_target_voltage_mv=int(initial_target_voltage_mv),
        short_probe_base_duration_s=int(short_probe_base_duration_s),
        request_reason=str(request_reason or "sweep-complete"),
        discard_returns_none=False,
    )

    selected_plan = candidate_plan_from_record(selected) or stable_plan
    selected_voltage_mv = int(selected.get("candidate_voltage_mv") or 0)
    selected_lock_clock_mhz = int(selected.get("lock_clock_mhz") or 0)
    selected_probe = matching_probe_for_candidate(
        stable_history,
        voltage_mv=int(selected_voltage_mv),
        lock_clock_mhz=int(selected_lock_clock_mhz),
    )
    return (
        selected_plan,
        selected_voltage_mv,
        selected_lock_clock_mhz,
        selected_probe,
        selected_final_duration_s,
    )


def choose_recovery_final_verification_candidate(
    *,
    log: Callable[[str], None],
    event_callback: AutoUvEventCallback | None,
    auto_uv_mode: str,
    base_probe: AutoUvProbeSummary | None,
    candidate_records: list[dict],
    default_candidate_id: str,
    final_verification_duration_s: int,
    initial_target_voltage_mv: int,
    short_probe_base_duration_s: int,
    recovery_decision: dict | None = None,
) -> tuple[list[dict], int, int, AutoUvProbeSummary | None, int, int, dict] | None:
    normalized_mode = normalize_auto_uv_mode(auto_uv_mode)
    candidates_by_id = {}
    for record in candidate_records:
        if not isinstance(record, dict):
            continue
        candidate_id = str(record.get("candidate_id", "")).strip()
        if not candidate_id:
            continue
        try:
            voltage_mv = int(record.get("candidate_voltage_mv"))
            lock_clock_mhz = int(record.get("lock_clock_mhz"))
        except (TypeError, ValueError):
            continue
        if voltage_mv <= 0 or lock_clock_mhz <= 0:
            continue
        plan = candidate_plan_from_record(record)
        if not plan:
            continue
        existing = candidates_by_id.get(candidate_id)
        if existing is not None and bool(existing.get("final_verified")):
            continue
        candidates_by_id[candidate_id] = dict(record)

    candidates = list(candidates_by_id.values())
    sort_label = "last-failed-run"
    if not candidates:
        return None
    default_id = str(default_candidate_id or "").strip()
    candidate_ids = {
        str(candidate.get("candidate_id", "")) for candidate in candidates
    }
    if default_id not in candidate_ids:
        default_id = str(candidates[0].get("candidate_id", ""))

    selected, selected_final_duration_s = request_final_choice_candidate(
        log=log,
        event_callback=event_callback,
        auto_uv_mode=normalized_mode,
        base_probe=base_probe,
        candidates=candidates,
        default_candidate_id=default_id,
        sort_label=sort_label,
        final_verification_duration_s=int(final_verification_duration_s),
        initial_target_voltage_mv=int(initial_target_voltage_mv),
        short_probe_base_duration_s=int(short_probe_base_duration_s),
        request_reason="previous-crash",
        recovery_decision=recovery_decision,
        discard_returns_none=True,
    )
    if selected is None:
        return None

    selected_plan = candidate_plan_from_record(selected)
    if not selected_plan:
        return None
    selected_voltage_mv = int(selected.get("candidate_voltage_mv"))
    selected_lock_clock_mhz = int(selected.get("lock_clock_mhz"))
    try:
        tail_rise_bins = int(selected.get("tail_rise_bins", 0) or 0)
    except (TypeError, ValueError):
        tail_rise_bins = 0
    return (
        selected_plan,
        selected_voltage_mv,
        selected_lock_clock_mhz,
        None,
        selected_final_duration_s,
        tail_rise_bins,
        dict(selected),
    )


def choose_next_final_verification_candidate_after_failure(
    *,
    log: Callable[[str], None],
    event_callback: AutoUvEventCallback | None,
    auto_uv_mode: str,
    base_probe: AutoUvProbeSummary | None,
    candidate_records: list[dict],
    stable_history: list[AutoUvProbeSummary] | None,
    failed_voltage_mv: int,
    final_verification_duration_s: int,
    initial_target_voltage_mv: int,
    short_probe_base_duration_s: int,
    recovery_decision: dict | None = None,
) -> tuple[list[dict], int, int, AutoUvProbeSummary | None, int, int, dict] | None:
    normalized_mode = normalize_auto_uv_mode(auto_uv_mode)
    candidates_by_id = {}
    for record in candidate_records:
        if not isinstance(record, dict):
            continue
        candidate_id = str(record.get("candidate_id", "")).strip()
        if not candidate_id:
            continue
        try:
            voltage_mv = int(record.get("candidate_voltage_mv"))
            lock_clock_mhz = int(record.get("lock_clock_mhz"))
        except (TypeError, ValueError):
            continue
        if voltage_mv <= int(failed_voltage_mv) or lock_clock_mhz <= 0:
            continue
        plan = candidate_plan_from_record(record)
        if not plan:
            continue
        candidates_by_id[candidate_id] = dict(record)

    candidate_records = list(candidates_by_id.values())
    candidates, sort_label = sorted_final_choice_candidates(
        candidate_records,
        auto_uv_mode=normalized_mode,
        base_probe=base_probe,
    )
    if not candidates:
        return None
    default_id = best_final_choice_candidate_id(candidates, normalized_mode) or str(
        candidates[0].get("candidate_id", "")
    )
    selected, selected_final_duration_s = request_final_choice_candidate(
        log=log,
        event_callback=event_callback,
        auto_uv_mode=normalized_mode,
        base_probe=base_probe,
        candidates=candidates,
        default_candidate_id=default_id,
        sort_label=sort_label,
        final_verification_duration_s=int(final_verification_duration_s),
        initial_target_voltage_mv=int(initial_target_voltage_mv),
        short_probe_base_duration_s=int(short_probe_base_duration_s),
        request_reason="final-verification-failed",
        recovery_decision=recovery_decision,
        discard_returns_none=True,
    )
    if selected is None:
        return None

    selected_plan = candidate_plan_from_record(selected)
    if not selected_plan:
        return None
    selected_voltage_mv = int(selected.get("candidate_voltage_mv"))
    selected_lock_clock_mhz = int(selected.get("lock_clock_mhz"))
    try:
        tail_rise_bins = int(selected.get("tail_rise_bins", 0) or 0)
    except (TypeError, ValueError):
        tail_rise_bins = 0
    selected_probe = matching_probe_for_candidate(
        stable_history or [],
        voltage_mv=int(selected_voltage_mv),
        lock_clock_mhz=int(selected_lock_clock_mhz),
    )
    return (
        selected_plan,
        selected_voltage_mv,
        selected_lock_clock_mhz,
        selected_probe,
        selected_final_duration_s,
        tail_rise_bins,
        dict(selected),
    )


def request_final_choice_candidate(
    *,
    log: Callable[[str], None],
    event_callback: AutoUvEventCallback | None,
    auto_uv_mode: str,
    base_probe: AutoUvProbeSummary | None,
    candidates: list[dict],
    default_candidate_id: str,
    sort_label: str,
    final_verification_duration_s: int,
    initial_target_voltage_mv: int,
    short_probe_base_duration_s: int,
    request_reason: str,
    recovery_decision: dict | None = None,
    discard_returns_none: bool = False,
) -> tuple[dict | None, int]:
    normalized_mode = normalize_auto_uv_mode(auto_uv_mode)
    default_id = str(default_candidate_id or "").strip()
    request_path = final_choice_request_path()
    response_path = final_choice_response_path()
    try:
        response_path.unlink()
    except FileNotFoundError:
        pass

    request_payload = {
        "format_version": 1,
        "auto_uv_mode": normalized_mode,
        "request_reason": str(request_reason or "sweep-complete"),
        "default_sort_metric": sort_label,
        "default_candidate_id": default_id,
        "final_verification_duration_s": int(final_verification_duration_s),
        "final_verification_duration_label": format_user_duration(
            final_verification_duration_s
        ),
        "max_final_verification_duration_s": 3600,
        "request_path": str(request_path),
        "response_path": str(response_path),
        "candidates": [
            candidate_selection_summary(
                candidate,
                base_probe=base_probe,
                short_verification_duration_s=candidate_short_verification_duration_s(
                    candidate,
                    initial_target_voltage_mv=int(initial_target_voltage_mv),
                    base_duration_s=int(short_probe_base_duration_s),
                    use_recorded_duration=False,
                ),
            )
            for candidate in candidates
        ],
    }
    if isinstance(recovery_decision, dict) and recovery_decision:
        request_payload["recovery_decision"] = dict(recovery_decision)
    safe_json_write(request_path, request_payload)
    emit_auto_uv_event(event_callback, "final_choice_request", **request_payload)
    log(
        "Auto-UV phase=final-choice "
        f"waiting-for-ui-selection default={default_id} sort={sort_label} "
        f"response={response_path}"
    )

    response = wait_for_final_choice_response(response_path)
    if final_choice_aborted(response):
        log("Auto-UV phase=final-choice aborted-by-user")
        emit_auto_uv_event(
            event_callback,
            "final_choice_discarded",
            reason="user-aborted",
        )
        raise AutoUvFinalChoiceDiscarded("Auto-UV recovery aborted by user.")

    if final_choice_discarded(response):
        if discard_returns_none:
            log("Auto-UV phase=final-choice discarded-by-user; starting new scan")
            return None, int(final_verification_duration_s)
        log("Auto-UV phase=final-choice discarded-by-user; final verification skipped")
        emit_auto_uv_event(
            event_callback,
            "final_choice_discarded",
            reason="user-discarded",
        )
        raise AutoUvFinalChoiceDiscarded(
            "Final verification discarded by user; no profile was saved."
        )

    selected_id = str(response.get("candidate_id", default_id))
    selected = next(
        (
            candidate
            for candidate in candidates
            if str(candidate.get("candidate_id", "")) == selected_id
        ),
        None,
    )
    if selected is None:
        log(
            "Auto-UV phase=final-choice "
            f"selection-missing id={selected_id}; using default={default_id}"
        )
        selected = next(
            candidate
            for candidate in candidates
            if str(candidate.get("candidate_id", "")) == default_id
        )

    selected_voltage_mv = int(selected.get("candidate_voltage_mv") or 0)
    selected_lock_clock_mhz = int(selected.get("lock_clock_mhz") or 0)
    selected_short_duration_s = candidate_short_verification_duration_s(
        selected,
        initial_target_voltage_mv=int(initial_target_voltage_mv),
        base_duration_s=int(short_probe_base_duration_s),
        use_recorded_duration=False,
    )
    selected_final_duration_s = coerce_final_choice_duration_s(
        response.get("final_verification_duration_s"),
        default_s=int(final_verification_duration_s),
        min_s=int(selected_short_duration_s),
        max_s=3600,
    )
    log(
        "Auto-UV phase=final-choice "
        f"selected={selected_id} {selected_voltage_mv}mV@{selected_lock_clock_mhz}MHz "
        f"duration={selected_final_duration_s}s "
        f"min-duration={selected_short_duration_s}s max-duration=3600s"
    )
    return selected, selected_final_duration_s


def candidate_records_from_history(
    stable_history: list[AutoUvProbeSummary],
    *,
    base_curve: list[dict],
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    tail_rise_bins: int = 0,
) -> dict[str, dict]:
    records = {}
    for probe in stable_history:
        candidate = candidate_record_from_probe(
            probe,
            base_curve=base_curve,
            stable_plan=stable_plan,
            stable_voltage_mv=int(stable_voltage_mv),
            stable_lock_clock_mhz=int(stable_lock_clock_mhz),
            tail_rise_bins=int(tail_rise_bins),
        )
        if candidate is not None:
            records[str(candidate.get("candidate_id", ""))] = candidate
    return records


def candidate_record_from_probe(
    probe: AutoUvProbeSummary,
    *,
    base_curve: list[dict],
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    tail_rise_bins: int = 0,
) -> dict | None:
    voltage_mv = int(probe.candidate_voltage_mv)
    lock_clock_mhz = int(probe.lock_clock_mhz)
    if probe.tested_plan is not None:
        plan = [dict(point) for point in probe.tested_plan]
    elif voltage_mv == int(stable_voltage_mv) and lock_clock_mhz == int(
        stable_lock_clock_mhz
    ):
        plan = list(stable_plan)
    else:
        try:
            plan = build_flattened_plan(
                base_curve,
                lock_clock_mhz=int(lock_clock_mhz),
                candidate_voltage_mv=int(voltage_mv),
                tail_rise_bins=int(tail_rise_bins),
            )
        except ValueError:
            return None
    return {
        "candidate_id": selection_candidate_id(
            voltage_mv=int(voltage_mv),
            lock_clock_mhz=int(lock_clock_mhz),
        ),
        "label": "passed-initial-stability",
        "reason": str(probe.result_reason or "passed-initial-stability"),
        "final_verified": False,
        "candidate_voltage_mv": int(voltage_mv),
        "lock_clock_mhz": int(lock_clock_mhz),
        "tail_rise_bins": int(tail_rise_bins),
        "avg_core_clock_mhz": float_or_none(probe.avg_core_clock_mhz),
        "avg_fps": float_or_none(probe.avg_fps),
        "avg_power_w": float_or_none(probe.avg_power_w),
        "efficiency_fps_per_w": float_or_none(probe.efficiency_fps_per_w),
        "plan": plan,
    }


def current_stable_record(
    *,
    candidate_id: str,
    stable_plan: list[dict],
    stable_voltage_mv: int,
    stable_lock_clock_mhz: int,
    stable_probe: AutoUvProbeSummary | None,
    existing_record: dict | None,
) -> dict:
    record = {
        "candidate_id": str(candidate_id),
        "label": "current-stable-candidate",
        "reason": "current-stable-candidate",
        "candidate_voltage_mv": int(stable_voltage_mv),
        "lock_clock_mhz": int(stable_lock_clock_mhz),
        "avg_core_clock_mhz": probe_float(stable_probe, "avg_core_clock_mhz"),
        "avg_fps": probe_float(stable_probe, "avg_fps"),
        "avg_power_w": probe_float(stable_probe, "avg_power_w"),
        "efficiency_fps_per_w": probe_float(stable_probe, "efficiency_fps_per_w"),
        "plan": list(stable_plan),
    }
    if existing_record:
        record.update(
            {
                key: value
                for key, value in existing_record.items()
                if key not in {"plan"}
            }
        )
    return record


def sorted_final_choice_candidates(
    candidates: list[dict],
    *,
    auto_uv_mode: str,
    base_probe: AutoUvProbeSummary | None,
) -> tuple[list[dict], str]:
    _ = base_probe
    normalized_mode = normalize_auto_uv_mode(auto_uv_mode)
    return (
        sort_candidates_for_final_choice(candidates, normalized_mode),
        final_choice_sort_label(normalized_mode),
    )


def candidate_selection_summary(
    candidate: dict,
    *,
    base_probe: AutoUvProbeSummary | None = None,
    short_verification_duration_s: int | None = None,
) -> dict:
    summary = {
        "candidate_id": str(candidate.get("candidate_id", "")),
        "label": str(candidate.get("label", candidate.get("reason", ""))),
        "reason": str(candidate.get("reason", "")),
        "final_verified": bool(candidate.get("final_verified", False)),
        "candidate_voltage_mv": candidate.get("candidate_voltage_mv"),
        "lock_clock_mhz": candidate.get("lock_clock_mhz"),
        "avg_core_clock_mhz": candidate.get("avg_core_clock_mhz"),
        "avg_fps": candidate.get("avg_fps"),
        "avg_power_w": candidate.get("avg_power_w"),
        "efficiency_fps_per_w": candidate.get("efficiency_fps_per_w"),
    }
    base_avg_fps = _candidate_or_probe_base_metric(
        candidate,
        "base_avg_fps",
        base_probe,
        "avg_fps",
    )
    if base_avg_fps is not None:
        summary["base_avg_fps"] = base_avg_fps
    base_avg_core_clock_mhz = _candidate_or_probe_base_metric(
        candidate,
        "base_avg_core_clock_mhz",
        base_probe,
        "avg_core_clock_mhz",
    )
    if base_avg_core_clock_mhz is not None:
        summary["base_avg_core_clock_mhz"] = base_avg_core_clock_mhz
    base_avg_power_w = _candidate_or_probe_base_metric(
        candidate,
        "base_avg_power_w",
        base_probe,
        "avg_power_w",
    )
    if base_avg_power_w is not None:
        summary["base_avg_power_w"] = base_avg_power_w
    core_oc_mhz = candidate_core_oc_mhz(
        candidate,
        base_core_clock_mhz=base_avg_core_clock_mhz,
    )
    if core_oc_mhz is not None:
        summary["core_oc_mhz"] = core_oc_mhz
    base_efficiency_fps_per_w = _candidate_or_probe_base_metric(
        candidate,
        "base_efficiency_fps_per_w",
        base_probe,
        "efficiency_fps_per_w",
    )
    if base_efficiency_fps_per_w is not None:
        summary["base_efficiency_fps_per_w"] = base_efficiency_fps_per_w
    if "performance_score" in candidate:
        summary["performance_score"] = candidate.get("performance_score")
    if short_verification_duration_s is not None:
        summary["short_verification_duration_s"] = int(
            short_verification_duration_s
        )
    return summary


def _candidate_or_probe_base_metric(
    candidate: dict,
    candidate_key: str,
    base_probe: AutoUvProbeSummary | None,
    probe_field_name: str,
) -> float | None:
    existing_value = candidate.get(candidate_key)
    if existing_value not in (None, ""):
        existing_float = float_or_none(existing_value)
        if existing_float is not None:
            return existing_float
    return probe_float(base_probe, probe_field_name)


def candidate_plan_from_record(candidate: dict) -> list[dict] | None:
    plan = candidate.get("plan")
    if isinstance(plan, list) and plan:
        return [dict(item) for item in plan if isinstance(item, dict)]
    points = candidate.get("points")
    if not isinstance(points, list) or not points:
        return None
    converted = []
    for point in points:
        if not isinstance(point, dict):
            continue
        item = dict(point)
        if "target_mhz" in item and "base_mhz" in item:
            item["new_offset_mhz"] = int(item["target_mhz"]) - int(item["base_mhz"])
        converted.append(item)
    return converted or None


def candidate_core_oc_mhz(
    candidate: dict,
    *,
    base_core_clock_mhz: float | None = None,
) -> int | None:
    existing = float_or_none(candidate.get("core_oc_mhz"))
    if existing is not None:
        return int(round(existing))
    baseline = (
        float(base_core_clock_mhz)
        if base_core_clock_mhz is not None
        else float_or_none(candidate.get("base_avg_core_clock_mhz"))
    )
    lock_clock = float_or_none(candidate.get("lock_clock_mhz"))
    if baseline is None or lock_clock is None:
        return None
    return int(round(float(lock_clock) - float(baseline)))


def matching_probe_for_candidate(
    history: list[AutoUvProbeSummary],
    *,
    voltage_mv: int,
    lock_clock_mhz: int,
) -> AutoUvProbeSummary | None:
    for probe in reversed(history):
        if int(probe.candidate_voltage_mv) == int(voltage_mv) and int(
            probe.lock_clock_mhz
        ) == int(lock_clock_mhz):
            return probe
    return history[-1] if history else None


def candidate_short_verification_duration_s(
    candidate: dict,
    *,
    initial_target_voltage_mv: int,
    base_duration_s: int | None = None,
    use_recorded_duration: bool = True,
) -> int:
    value = candidate.get("short_verification_duration_s")
    if use_recorded_duration and value not in (None, ""):
        try:
            return max(1, int(round(float(value))))
        except (TypeError, ValueError):
            pass
    return tiered_short_verification_duration_s(
        initial_target_voltage_mv=int(initial_target_voltage_mv),
        candidate_voltage_mv=candidate.get("candidate_voltage_mv") or 0,
        base_duration_s=base_duration_s,
    )


def tiered_short_verification_duration_s(
    *,
    initial_target_voltage_mv: int,
    candidate_voltage_mv: int,
    base_duration_s: int | None = None,
) -> int:
    base_duration = 30 if base_duration_s is None else max(10, int(base_duration_s))
    if int(initial_target_voltage_mv) <= 0:
        return int(base_duration)
    voltage_ratio = float(candidate_voltage_mv) / float(initial_target_voltage_mv)
    if voltage_ratio >= 0.95:
        multiplier = 1
    elif voltage_ratio >= 0.90:
        multiplier = 2
    else:
        multiplier = 3
    return max(1, int(base_duration) * int(multiplier))


def wait_for_final_choice_response(response_path) -> dict:
    while not response_path.exists():
        if auto_uv_stop_requested():
            raise KeyboardInterrupt()
        time.sleep(0.25)
    try:
        response = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return response if isinstance(response, dict) else {}


def final_choice_discarded(response: dict) -> bool:
    response_action = str(response.get("action", "")).strip().lower()
    return response_action in {"discard", "cancel", "cancelled"} or bool(
        response.get("discarded")
    )


def final_choice_aborted(response: dict) -> bool:
    response_action = str(response.get("action", "")).strip().lower()
    return response_action in {"abort", "aborted"}


def coerce_final_choice_duration_s(
    value,
    *,
    default_s: int,
    min_s: int = 1,
    max_s: int = 3600,
) -> int:
    max_duration_s = max(1, int(max_s))
    min_duration_s = min(max(1, int(min_s)), max_duration_s)
    try:
        duration_s = int(round(float(value)))
    except (TypeError, ValueError):
        duration_s = int(default_s)
    return max(min_duration_s, min(max_duration_s, int(duration_s)))


def selection_candidate_id(*, voltage_mv: int, lock_clock_mhz: int) -> str:
    return f"{int(voltage_mv)}mv-{int(lock_clock_mhz)}mhz"


def probe_float(probe: AutoUvProbeSummary | None, field_name: str) -> float | None:
    if probe is None:
        return None
    return float_or_none(getattr(probe, field_name, None))


def float_or_none(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def format_user_duration(duration_s: int | float | None) -> str:
    if duration_s is None:
        return "n/a"
    seconds = int(round(float(duration_s)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if remaining_seconds == 0:
        return f"{minutes}min"
    return f"{minutes}min {remaining_seconds}s"
