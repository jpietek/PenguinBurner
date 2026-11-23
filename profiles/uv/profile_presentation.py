"""How saved profiles read in a table: cell text, metric context, sorting.

Pure formatting over profile summaries (``read_auto_uv_profile_summaries``)
shared by the Profiles views and the final-choice dialog. No Qt.
"""

from __future__ import annotations

from datetime import datetime

from profiles.gpu_identity import profile_gpu_label, profile_gpu_uuid
from profiles.uv.profile_store import profile_presentation_name
from profiles.uv.profile_tiers import normalize_profile_tier

PROFILE_COLUMNS = (
    "Date",
    "Profile",
    "GPU",
    "mV",
    "Target MHz",
    "Effective MHz",
    "FPS/W",
    "FPS",
    "Power W",
    "Mem",
    "Tier",
    "Source",
)
# Date, GPU, mV, Target MHz, Effective MHz, FPS/W, FPS and Power W sort.
PROFILE_SORTABLE_COLUMNS = frozenset({0, 2, 3, 4, 5, 6, 7, 8})


def sort_value_less(left, right, *, descending: bool = False) -> bool:
    left_missing = left in (None, "")
    right_missing = right in (None, "")
    if left_missing != right_missing:
        return left_missing if descending else right_missing
    if left_missing and right_missing:
        return False
    left_key = sort_key(left)
    right_key = sort_key(right)
    return left_key < right_key


def sort_key(value) -> tuple[int, float, str]:
    number = to_float(value)
    if number is not None:
        return (0, float(number), "")
    return (1, 0.0, str(value).casefold())


def date_text(profile: dict) -> str:
    value = str(
        profile.get("profile_created_at") or profile.get("verified_at") or ""
    ).strip()
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value[:19].replace("T", " ")


def format_number(value, *, precision: int) -> str:
    if value in (None, ""):
        return ""
    number = to_float(value)
    if number is None:
        return ""
    precision = max(0, min(int(precision), 4))
    if precision <= 0:
        return str(round(number))
    return f"{number:.{precision}f}"


def format_signed_number(value, *, precision: int) -> str:
    text = format_number(value, precision=precision)
    if not text:
        return ""
    number = to_float(value)
    if number is None:
        return text
    if abs(number) < 0.5:
        return "0"
    return f"+{text}" if number > 0 else text


def format_signed_memory_clock(value) -> str:
    # The stored memory offset is an NVML transfer-rate value (MT/s); the
    # realized memory clock moves by half of it (verified on Blackwell,
    # issue #20). Show the memory-clock MHz, matching the Auto-UV dialog.
    number = to_float(value)
    if number is None:
        return ""
    text = format_signed_number(number / 2, precision=0)
    if not text:
        return ""
    return f"{text} MHz"


def to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def metric_delta_percent(
    current,
    baseline,
) -> float | None:
    current_number = to_float(current)
    baseline_number = to_float(baseline)
    if current_number is None or baseline_number in (None, 0.0):
        return None
    return (
        (current_number - float(baseline_number)) / float(baseline_number)
    ) * 100.0


def profile_metric_tooltip(
    current,
    baseline,
    *,
    label: str,
    precision: int = 2,
) -> str:
    delta = metric_delta_percent(
        current,
        baseline,
    )
    if delta is None:
        return ""
    value_text = format_number(current, precision=precision)
    base_text = format_number(baseline, precision=precision)
    return (
        f"{label}: {value_text}\n"
        f"{delta:+.2f}% vs this scan's baseline: {base_text}.\n"
        "Baselines differ between scans; these percentages do not compare profiles."
    )


def format_profile_metric_with_delta(
    current,
    baseline,
    *,
    precision: int,
    lower_is_better: bool = False,
) -> str:
    value_text = format_number(current, precision=precision)
    if not value_text:
        return ""
    delta = metric_delta_percent(
        current,
        baseline,
    )
    if delta is None:
        return value_text
    return f"{value_text} ({delta:+.2f}%)"


def profile_base_metric(profile: dict, metric: str):
    lookup = {
        "candidate_voltage_mv": (
            "base_candidate_voltage_mv",
            "baseline_candidate_voltage_mv",
            "baseline_voltage_mv",
            "base_voltage_mv",
        ),
        "avg_core_clock_mhz": (
            "base_avg_core_clock_mhz",
            "baseline_avg_core_clock_mhz",
            "baseline_core_clock_mhz",
        ),
        "efficiency_fps_per_w": (
            "base_efficiency_fps_per_w",
            "baseline_efficiency_fps_per_w",
            "baseline_fps_per_w",
        ),
        "avg_power_w": (
            "base_avg_power_w",
            "baseline_avg_power_w",
            "baseline_power_w",
        ),
        "avg_fps": (
            "base_avg_fps",
            "baseline_avg_fps",
            "baseline_fps",
        ),
    }
    for key in lookup.get(str(metric), ()):
        value = profile.get(key)
        if to_float(value) is not None:
            return value
    return None


def date_sort_value(profile: dict) -> float:
    value = str(
        profile.get("profile_created_at") or profile.get("verified_at") or ""
    ).strip()
    if not value:
        return 0.0
    try:
        return float(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return 0.0


def profile_value_sort_value(profile: dict, metric: str) -> float | str:
    value = to_float(profile.get(metric))
    return "" if value is None else float(value)


def profile_sort_values(profile: dict) -> list[float | str]:
    return [
        date_sort_value(profile),
        "",
        profile_gpu_label(profile).casefold(),
        profile_value_sort_value(profile, "candidate_voltage_mv"),
        profile_value_sort_value(profile, "lock_clock_mhz"),
        profile_value_sort_value(profile, "avg_core_clock_mhz"),
        profile_value_sort_value(profile, "efficiency_fps_per_w"),
        profile_value_sort_value(profile, "avg_fps"),
        profile_value_sort_value(profile, "avg_power_w"),
        "",
        "",
        "",
    ]


def profile_source_label(profile: dict) -> str:
    source = str(profile.get("profile_source", "")).strip()
    labels = {
        "auto-uv-final": "Auto UV",
        "user-edited": "User edited",
    }
    return labels.get(source, source)


def profile_tier_column_label(
    profile: dict,
    tier_winner_ids: dict[str, str] | None = None,
) -> str:
    if truthy(profile.get("profile_tier_disabled")):
        return ""
    assigned = str(profile.get("assigned_profile_tier") or "").strip()
    label = (
        assigned
        or str(profile.get("profile_tier") or "").strip()
        or str(profile.get("generated_profile_tier") or "").strip()
    )
    if not label:
        return ""
    if tier_winner_ids:
        tier_key = normalize_profile_tier(label)
        gpu_uuid = profile_gpu_uuid(profile)
        winner_key = f"{gpu_uuid}\0{tier_key}" if gpu_uuid else tier_key
        winner_id = str(tier_winner_ids.get(winner_key) or "").strip()
        profile_id = str(profile.get("profile_id") or "").strip()
        # Only the resolved winner keeps the tier; duplicates show no tier so
        # the column stays unique and matches adaptive mode's choice.
        if winner_id and profile_id and winner_id != profile_id:
            return ""
    return label


def truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def profile_name(profile: dict) -> str:
    return profile_presentation_name(profile)


def profile_matches_preference(
    profile: dict,
    *,
    preferred_candidate_id: str = "",
    preferred_profile_id: str = "",
) -> bool:
    profile_id = str(profile.get("profile_id", ""))
    candidate_id = str(profile.get("candidate_id", ""))
    if preferred_profile_id and profile_id == str(preferred_profile_id):
        return True
    return bool(preferred_candidate_id and candidate_id == str(preferred_candidate_id))


def promote_preferred_profile(
    profiles: list[dict],
    *,
    preferred_candidate_id: str = "",
    preferred_profile_id: str = "",
) -> list[dict]:
    if not preferred_candidate_id and not preferred_profile_id:
        return list(profiles)
    promoted_index = None
    for index, profile in enumerate(profiles):
        if profile_matches_preference(
            profile,
            preferred_candidate_id=preferred_candidate_id,
            preferred_profile_id=preferred_profile_id,
        ):
            promoted_index = index
            break
    if promoted_index is None:
        return list(profiles)
    promoted = profiles[promoted_index]
    return [promoted, *profiles[:promoted_index], *profiles[promoted_index + 1 :]]
