from __future__ import annotations

import math

from auto_uv.scan_mode.auto_uv_mode import AUTO_UV_MODE_PERFORMANCE
from auto_uv.scan_mode.auto_uv_mode import AUTO_UV_MODE_EFFICIENCY
from auto_uv.scan_mode.auto_uv_mode import normalize_auto_uv_mode
from auto_uv.scan_mode.efficiency_fps_per_w_policy import best_efficiency_candidate_index


FINAL_CHOICE_FPSW_SORT_COLUMN = 4
FINAL_CHOICE_FPS_SORT_COLUMN = 5
FINAL_CHOICE_DEFAULT_SORT_COLUMN = FINAL_CHOICE_FPSW_SORT_COLUMN
FINAL_CHOICE_SORTABLE_COLUMNS = frozenset({2, 3, 4, 5, 6})
FINAL_CHOICE_HIGHER_FIRST_COLUMNS = frozenset({2, 3, 4, 5})


def final_choice_sort_values(candidate: dict) -> list[float | str]:
    oc_mhz = candidate_oc_mhz(candidate)
    return [
        numeric_sort_value(candidate.get("candidate_voltage_mv")),
        numeric_sort_value(candidate.get("lock_clock_mhz")),
        "" if oc_mhz is None else float(oc_mhz),
        numeric_sort_value(candidate.get("avg_core_clock_mhz")),
        numeric_sort_value(candidate.get("efficiency_fps_per_w")),
        numeric_sort_value(candidate.get("avg_fps")),
        numeric_sort_value(candidate.get("avg_power_w")),
        float(candidate_short_duration_s(candidate)),
        candidate_status_sort_value(candidate),
    ]


def final_choice_sort_column_for_mode(auto_uv_mode: object) -> int:
    if normalize_auto_uv_mode(auto_uv_mode) == AUTO_UV_MODE_PERFORMANCE:
        return FINAL_CHOICE_FPS_SORT_COLUMN
    return FINAL_CHOICE_FPSW_SORT_COLUMN


def final_choice_sort_label(auto_uv_mode: object) -> str:
    if normalize_auto_uv_mode(auto_uv_mode) == AUTO_UV_MODE_PERFORMANCE:
        return "fps"
    return "fps-per-w"


def final_choice_shows_oc_column(auto_uv_mode: object) -> bool:
    return normalize_auto_uv_mode(auto_uv_mode) == AUTO_UV_MODE_PERFORMANCE


def sort_candidates_for_final_choice(
    candidates: list[dict],
    auto_uv_mode: object,
) -> list[dict]:
    if normalize_auto_uv_mode(auto_uv_mode) == AUTO_UV_MODE_PERFORMANCE:
        return sorted(
            candidates,
            key=lambda candidate: (
                candidate_fps(candidate) is None,
                -float(candidate_fps(candidate) or 0.0),
                -int(candidate.get("lock_clock_mhz") or 0),
                int(candidate.get("candidate_voltage_mv") or 99999),
            ),
        )
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate_fpsw(candidate) is None,
            -float(candidate_fpsw(candidate) or 0.0),
            int(candidate.get("candidate_voltage_mv") or 99999),
            -int(candidate.get("lock_clock_mhz") or 0),
        ),
    )


def best_final_choice_candidate_id(candidates: list[dict], auto_uv_mode: object) -> str:
    if normalize_auto_uv_mode(auto_uv_mode) == AUTO_UV_MODE_EFFICIENCY:
        index = best_efficiency_candidate_index(candidates)
        if index is not None:
            return str(candidates[index].get("candidate_id", ""))
    if normalize_auto_uv_mode(auto_uv_mode) == AUTO_UV_MODE_PERFORMANCE:
        for candidate in candidates:
            if candidate_fps(candidate) is not None:
                return str(candidate.get("candidate_id", ""))
        return str(candidates[0].get("candidate_id", "")) if candidates else ""
    for candidate in candidates:
        if candidate_fpsw(candidate) is not None:
            return str(candidate.get("candidate_id", ""))
    return str(candidates[0].get("candidate_id", "")) if candidates else ""


def candidate_short_duration_s(candidate: dict) -> int:
    try:
        duration_s = int(round(float(candidate.get("short_verification_duration_s"))))
    except (TypeError, ValueError):
        duration_s = 30
    return max(1, min(3600, duration_s))


def candidate_status_sort_value(candidate: dict) -> str:
    if bool(candidate.get("final_verified")):
        return "final stability verified"
    return "passed short probe"


def numeric_sort_value(value) -> float | str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return float(number) if math.isfinite(number) else ""


def candidate_fpsw(candidate: dict) -> float | None:
    value = numeric_sort_value(candidate.get("efficiency_fps_per_w"))
    return None if value == "" else float(value)


def candidate_fps(candidate: dict) -> float | None:
    value = numeric_sort_value(candidate.get("avg_fps"))
    return None if value == "" else float(value)


def candidate_oc_mhz(candidate: dict) -> int | None:
    target_clock = numeric_sort_value(candidate.get("lock_clock_mhz"))
    baseline_clock = numeric_sort_value(
        candidate.get("base_avg_core_clock_mhz")
        or candidate.get("measured_baseline_clock_mhz")
        or candidate.get("baseline_core_clock_mhz")
        or candidate.get("baseline_clock_mhz")
        or candidate.get("auto_oc_baseline_clock_mhz")
    )
    if target_clock != "" and baseline_clock != "":
        return int(round(float(target_clock) - float(baseline_clock)))
    return None
