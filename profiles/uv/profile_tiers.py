from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from common.atomic_write import atomic_write_json
from common.penguin_burner_paths import default_user_config_dir
from profiles.gpu_identity import profile_gpu_uuid

PROFILE_TIER_EFFICIENCY = "efficiency"
PROFILE_TIER_BALANCED = "balanced"
PROFILE_TIER_PERFORMANCE = "performance"
PROFILE_TIER_NONE = "none"
PROFILE_TIERS = (
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_PERFORMANCE,
)
PROFILE_TIER_LABELS = {
    PROFILE_TIER_EFFICIENCY: "Efficiency",
    PROFILE_TIER_BALANCED: "Balanced",
    PROFILE_TIER_PERFORMANCE: "Performance",
}
_BALANCED_TAIL_RISE_BINS = 4
_PERFORMANCE_TAIL_RISE_BINS = 6

_PROFILE_TIER_ALIASES = {
    "eff": PROFILE_TIER_EFFICIENCY,
    "efficiency": PROFILE_TIER_EFFICIENCY,
    "balanced": PROFILE_TIER_BALANCED,
    "balance": PROFILE_TIER_BALANCED,
    "bal": PROFILE_TIER_BALANCED,
    "normal": PROFILE_TIER_BALANCED,
    "perf": PROFILE_TIER_PERFORMANCE,
    "performance": PROFILE_TIER_PERFORMANCE,
    "aggressive": PROFILE_TIER_PERFORMANCE,
}
_PROFILE_TIER_NONE_ALIASES = {"none", "no", "off", "disable", "disabled", "unassigned"}


def normalize_profile_tier(value: object | None, *, default: str = "") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    normalized = _PROFILE_TIER_ALIASES.get(text, "")
    return normalized or default


def profile_tier_label(value: object | None) -> str:
    tier = normalize_profile_tier(value)
    return PROFILE_TIER_LABELS.get(tier, "")


def profile_tier_is_none(value: object | None) -> bool:
    text = str(value or "").strip().lower()
    return text in _PROFILE_TIER_NONE_ALIASES


def _tier_from_tail_rise_bins(tail_rise_bins: int) -> str:
    # Single source of truth for the tail-bins -> tier thresholds, shared by
    # both the runtime-options and saved-profile classifiers so they can never
    # drift apart (efficiency=flat, balanced>=4 bins, performance>=6 bins).
    if tail_rise_bins >= _PERFORMANCE_TAIL_RISE_BINS:
        return PROFILE_TIER_PERFORMANCE
    if tail_rise_bins >= _BALANCED_TAIL_RISE_BINS:
        return PROFILE_TIER_BALANCED
    return PROFILE_TIER_EFFICIENCY


def _tier_from_mode(mode: str) -> str:
    # Mode-only fallback when no tail-bins signal is present: an explicit
    # efficiency mode maps to efficiency, everything else defaults to balanced.
    if mode == PROFILE_TIER_EFFICIENCY:
        return PROFILE_TIER_EFFICIENCY
    return PROFILE_TIER_BALANCED


def generated_profile_tier(profile: dict | None) -> str:
    payload = profile if isinstance(profile, dict) else {}
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
    if mode == PROFILE_TIER_PERFORMANCE:
        return PROFILE_TIER_PERFORMANCE

    profile_curve = str(payload.get("profile_curve") or "").strip().lower()
    if profile_curve in {"performance-sweep", "auto-oc"}:
        return PROFILE_TIER_PERFORMANCE

    tail_rise_bins = _optional_int(payload.get("tail_rise_bins"))
    if tail_rise_bins is None:
        flatten_target = payload.get("flatten_target")
        if isinstance(flatten_target, dict):
            tail_rise_bins = _optional_int(flatten_target.get("tail_rise_bins"))
    if tail_rise_bins is not None:
        return _tier_from_tail_rise_bins(tail_rise_bins)

    return _tier_from_mode(mode)


def profile_tier_assignments_path() -> Path:
    return default_user_config_dir() / "auto-uv-profile-tier-assignments.json"


def load_profile_tier_assignments(
    path: str | Path | None = None,
    *,
    gpu_uuid: str = "",
) -> dict[str, str]:
    payload = _read_profile_tier_assignments(path)
    group = _profile_tier_group(payload, gpu_uuid)
    raw_tiers = group.get("tiers")
    if not isinstance(raw_tiers, dict):
        return {}
    assignments: dict[str, str] = {}
    seen_profiles: set[str] = set()
    disabled_profile_ids = _disabled_profile_ids_from_payload(group)
    for raw_tier, raw_profile_id in raw_tiers.items():
        tier = normalize_profile_tier(raw_tier)
        profile_id = str(raw_profile_id or "").strip()
        if (
            not tier
            or not profile_id
            or profile_id in seen_profiles
            or profile_id in disabled_profile_ids
        ):
            continue
        assignments[tier] = profile_id
        seen_profiles.add(profile_id)
    return assignments


def load_profile_tier_disabled_profile_ids(
    path: str | Path | None = None,
    *,
    gpu_uuid: str = "",
) -> set[str]:
    payload = _read_profile_tier_assignments(path)
    return _disabled_profile_ids_from_payload(_profile_tier_group(payload, gpu_uuid))


def save_profile_tier_assignment(
    profile_id: str,
    tier: object | None,
    *,
    path: str | Path | None = None,
    gpu_uuid: str = "",
) -> dict[str, str]:
    selected_profile_id = str(profile_id or "").strip()
    selected_tier = normalize_profile_tier(tier)
    if not selected_profile_id:
        raise ValueError("profile_id is required")
    if profile_tier_is_none(tier):
        return save_profile_tier_none_assignment(
            selected_profile_id,
            path=path,
            gpu_uuid=gpu_uuid,
        )
    if not selected_tier:
        raise ValueError("profile tier is required")

    disabled_profile_ids = load_profile_tier_disabled_profile_ids(
        path, gpu_uuid=gpu_uuid
    )
    disabled_profile_ids.discard(selected_profile_id)
    assignments = {
        key: value
        for key, value in load_profile_tier_assignments(
            path, gpu_uuid=gpu_uuid
        ).items()
        if value != selected_profile_id
    }
    assignments[selected_tier] = selected_profile_id
    _write_profile_tier_assignments(
        assignments,
        disabled_profile_ids=disabled_profile_ids,
        path=path,
        gpu_uuid=gpu_uuid,
    )
    return assignments


def save_profile_tier_none_assignment(
    profile_id: str,
    *,
    path: str | Path | None = None,
    gpu_uuid: str = "",
) -> dict[str, str]:
    selected_profile_id = str(profile_id or "").strip()
    if not selected_profile_id:
        raise ValueError("profile_id is required")

    assignments = {
        key: value
        for key, value in load_profile_tier_assignments(
            path, gpu_uuid=gpu_uuid
        ).items()
        if value != selected_profile_id
    }
    disabled_profile_ids = load_profile_tier_disabled_profile_ids(
        path, gpu_uuid=gpu_uuid
    )
    disabled_profile_ids.add(selected_profile_id)
    _write_profile_tier_assignments(
        assignments,
        disabled_profile_ids=disabled_profile_ids,
        path=path,
        gpu_uuid=gpu_uuid,
    )
    return assignments


def migrate_legacy_profile_tier_to_gpu(
    profile_id: str,
    gpu_uuid: str,
    *,
    path: str | Path | None = None,
) -> None:
    """Move one legacy global assignment into its newly bound GPU group."""
    selected_profile_id = str(profile_id or "").strip()
    selected_gpu_uuid = str(gpu_uuid or "").strip()
    if not selected_profile_id or not selected_gpu_uuid:
        return
    legacy_assignments = load_profile_tier_assignments(path)
    legacy_disabled = load_profile_tier_disabled_profile_ids(path)
    assigned_tier = next(
        (
            tier
            for tier, assigned_id in legacy_assignments.items()
            if assigned_id == selected_profile_id
        ),
        "",
    )
    was_disabled = selected_profile_id in legacy_disabled
    if not assigned_tier and not was_disabled:
        return

    gpu_assignments = load_profile_tier_assignments(
        path, gpu_uuid=selected_gpu_uuid
    )
    gpu_disabled = load_profile_tier_disabled_profile_ids(
        path, gpu_uuid=selected_gpu_uuid
    )
    existing_tier_profile_id = str(
        gpu_assignments.get(assigned_tier) or ""
    ).strip()
    gpu_assignments = {
        tier: assigned_id
        for tier, assigned_id in gpu_assignments.items()
        if assigned_id != selected_profile_id
    }
    if assigned_tier and (
        not existing_tier_profile_id
        or existing_tier_profile_id == selected_profile_id
    ):
        gpu_assignments[assigned_tier] = selected_profile_id
        gpu_disabled.discard(selected_profile_id)
    if was_disabled:
        gpu_disabled.add(selected_profile_id)
    _write_profile_tier_assignments(
        gpu_assignments,
        disabled_profile_ids=gpu_disabled,
        path=path,
        gpu_uuid=selected_gpu_uuid,
    )

    legacy_assignments = {
        tier: assigned_id
        for tier, assigned_id in legacy_assignments.items()
        if assigned_id != selected_profile_id
    }
    legacy_disabled.discard(selected_profile_id)
    _write_profile_tier_assignments(
        legacy_assignments,
        disabled_profile_ids=legacy_disabled,
        path=path,
    )


def profile_tier_disabled(
    profile: dict,
    disabled_profile_ids: set[str] | None = None,
) -> bool:
    if _truthy(profile.get("profile_tier_disabled")):
        return True
    profile_id = str(profile.get("profile_id") or "").strip()
    if not profile_id:
        return False
    disabled = (
        disabled_profile_ids
        if disabled_profile_ids is not None
        else load_profile_tier_disabled_profile_ids(
            gpu_uuid=profile_gpu_uuid(profile)
        )
    )
    return profile_id in disabled


def assigned_tier_for_profile(
    profile: dict,
    assignments: dict[str, str] | None = None,
) -> str:
    profile_id = str(profile.get("profile_id") or "").strip()
    if not profile_id:
        return ""
    tier_assignments = (
        assignments
        if assignments is not None
        else load_profile_tier_assignments(gpu_uuid=profile_gpu_uuid(profile))
    )
    for tier, assigned_profile_id in tier_assignments.items():
        if str(assigned_profile_id or "").strip() == profile_id:
            return normalize_profile_tier(tier)
    return ""


def profile_tier_summary_fields(
    profile: dict,
    assignments: dict[str, str] | None = None,
    disabled_profile_ids: set[str] | None = None,
) -> dict[str, object]:
    gpu_uuid = profile_gpu_uuid(profile)
    if assignments is None:
        assignments = load_profile_tier_assignments(gpu_uuid=gpu_uuid)
    if disabled_profile_ids is None:
        disabled_profile_ids = load_profile_tier_disabled_profile_ids(
            gpu_uuid=gpu_uuid
        )
    generated_tier = generated_profile_tier(profile)
    disabled = profile_tier_disabled(profile, disabled_profile_ids)
    assigned_tier = "" if disabled else assigned_tier_for_profile(profile, assignments)
    effective_tier = "" if disabled else assigned_tier or generated_tier
    return {
        "generated_profile_tier": profile_tier_label(generated_tier),
        "generated_profile_tier_key": generated_tier,
        "assigned_profile_tier": profile_tier_label(assigned_tier),
        "assigned_profile_tier_key": assigned_tier,
        "profile_tier": profile_tier_label(effective_tier),
        "profile_tier_key": effective_tier,
        "profile_tier_disabled": disabled,
    }


def resolve_profile_tier_profiles(
    profiles: list[dict],
    assignments: dict[str, str] | None = None,
    disabled_profile_ids: set[str] | None = None,
    *,
    gpu_uuid: str = "",
    include_legacy_profiles: bool = False,
) -> dict[str, dict | None]:
    selected_gpu_uuid = str(gpu_uuid or "").strip()
    if assignments is not None:
        tier_assignments = assignments
    else:
        tier_assignments = load_profile_tier_assignments(gpu_uuid=selected_gpu_uuid)
        if selected_gpu_uuid and include_legacy_profiles:
            # Explicit per-GPU assignments win, while old global assignments
            # remain usable on a physically unambiguous one-GPU machine.
            tier_assignments = {
                **load_profile_tier_assignments(),
                **tier_assignments,
            }
    if disabled_profile_ids is not None:
        disabled = disabled_profile_ids
    else:
        disabled = load_profile_tier_disabled_profile_ids(
            gpu_uuid=selected_gpu_uuid
        )
        if selected_gpu_uuid and include_legacy_profiles:
            disabled = disabled | load_profile_tier_disabled_profile_ids()
    visible_profiles = [
        profile
        for profile in profiles
        if bool(profile.get("final_verified", False))
        and not profile_tier_disabled(profile, disabled)
        and (
            not selected_gpu_uuid
            or profile_gpu_uuid(profile).casefold() == selected_gpu_uuid.casefold()
            or (include_legacy_profiles and not profile_gpu_uuid(profile))
        )
    ]
    visible_profiles.sort(key=_profile_sort_time, reverse=True)
    by_profile_id = {
        str(profile.get("profile_id") or ""): profile
        for profile in visible_profiles
        if str(profile.get("profile_id") or "").strip()
    }
    resolved: dict[str, dict | None] = {tier: None for tier in PROFILE_TIERS}
    assigned_profile_ids: set[str] = set()

    for tier in PROFILE_TIERS:
        assigned_profile_id = str(tier_assignments.get(tier) or "").strip()
        profile = by_profile_id.get(assigned_profile_id)
        if profile is None:
            continue
        resolved[tier] = profile
        assigned_profile_ids.add(assigned_profile_id)

    unassigned_profiles = []
    for profile in visible_profiles:
        profile_id = str(profile.get("profile_id") or "").strip()
        if not profile_id or profile_id in assigned_profile_ids:
            continue
        tier = generated_profile_tier(profile)
        if tier in resolved:
            unassigned_profiles.append(profile)
    for tier in PROFILE_TIERS:
        if resolved[tier] is not None:
            continue
        candidates = [
            profile
            for profile in unassigned_profiles
            if generated_profile_tier(profile) == tier
        ]
        if not candidates:
            continue
        resolved[tier] = max(
            candidates,
            key=lambda profile, tier=tier, candidates=candidates: (
                _profile_tier_sort_key(profile, tier, candidates)
            ),
        )
    return resolved


def available_adaptive_tiers(
    resolved_profiles: dict[str, dict | None],
) -> list[str]:
    tiers: list[str] = []
    seen_profile_ids: set[str] = set()
    for tier in PROFILE_TIERS:
        profile = resolved_profiles.get(tier)
        if not isinstance(profile, dict):
            continue
        profile_id = str(profile.get("profile_id") or "").strip()
        if not profile_id or profile_id in seen_profile_ids:
            continue
        tiers.append(tier)
        seen_profile_ids.add(profile_id)
    return tiers


def adaptive_tier_available(
    profiles: list[dict],
    *,
    gpu_uuid: str = "",
    include_legacy_profiles: bool = False,
) -> bool:
    """Whether Adaptive has at least one tier profile to switch to on a GPU.

    The game mode's gate: Adaptive needs a verified tier profile, which is a
    property of the saved profiles rather than of any launcher.
    """
    return bool(
        available_adaptive_tiers(
            resolve_profile_tier_profiles(
                list(profiles),
                gpu_uuid=gpu_uuid,
                include_legacy_profiles=include_legacy_profiles,
            )
        )
    )


def _write_profile_tier_assignments(
    assignments: dict[str, str],
    *,
    disabled_profile_ids: set[str] | None = None,
    path: str | Path | None = None,
    gpu_uuid: str = "",
) -> Path:
    assignment_path = Path(path).expanduser() if path is not None else profile_tier_assignments_path()
    disabled = sorted(
        str(profile_id).strip()
        for profile_id in (disabled_profile_ids or set())
        if str(profile_id).strip()
    )
    group: dict[str, object] = {
        "tiers": {
            tier: str(assignments[tier])
            for tier in PROFILE_TIERS
            if str(assignments.get(tier) or "").strip()
        },
    }
    if disabled:
        group["disabled_profile_ids"] = disabled
    selected_gpu_uuid = str(gpu_uuid or "").strip()
    existing = _read_profile_tier_assignments(path)
    gpu_profiles = existing.get("gpu_profiles")
    groups = dict(gpu_profiles) if isinstance(gpu_profiles, dict) else {}
    if selected_gpu_uuid:
        for key in tuple(groups):
            if str(key).casefold() == selected_gpu_uuid.casefold():
                groups.pop(key, None)
        groups[selected_gpu_uuid] = group
        payload = {
            **existing,
            "format_version": 3,
            "updated_at": datetime.now().astimezone().isoformat(),
            "gpu_profiles": groups,
        }
    else:
        payload = {
            **existing,
            "format_version": 3 if groups else 2,
            "updated_at": datetime.now().astimezone().isoformat(),
            **group,
        }
        if not disabled:
            # ``group`` omits the key when the set is empty, so spreading
            # ``existing`` would resurrect a previously written list and keep
            # re-enabled profiles excluded from tier resolution.
            payload.pop("disabled_profile_ids", None)
    return atomic_write_json(assignment_path, payload)


def _read_profile_tier_assignments(path: str | Path | None = None) -> dict:
    assignment_path = Path(path).expanduser() if path is not None else profile_tier_assignments_path()
    try:
        payload = json.loads(
            assignment_path.read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _disabled_profile_ids_from_payload(payload: dict) -> set[str]:
    raw_values = payload.get("disabled_profile_ids") if isinstance(payload, dict) else None
    if not isinstance(raw_values, list):
        return set()
    return {
        str(profile_id).strip()
        for profile_id in raw_values
        if str(profile_id).strip()
    }


def _profile_tier_group(payload: dict, gpu_uuid: str) -> dict:
    selected_gpu_uuid = str(gpu_uuid or "").strip()
    if not selected_gpu_uuid:
        return payload if isinstance(payload, dict) else {}
    groups = payload.get("gpu_profiles") if isinstance(payload, dict) else None
    if not isinstance(groups, dict):
        return {}
    for key, group in groups.items():
        if str(key).casefold() == selected_gpu_uuid.casefold() and isinstance(
            group, dict
        ):
            return group
    return {}


def _optional_int(value: object | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _profile_tier_sort_key(
    profile: dict,
    tier: str,
    candidates: list[dict],
) -> tuple[float, float]:
    if tier != PROFILE_TIER_BALANCED:
        return (0.0, _profile_sort_time(profile))
    return (
        _balanced_profile_score(profile, candidates),
        _profile_sort_time(profile),
    )


def _balanced_profile_score(profile: dict, candidates: list[dict]) -> float:
    best_fpsw = max(
        (_optional_float(candidate.get("efficiency_fps_per_w")) or 0.0)
        for candidate in candidates
    )
    best_fps = max(
        (_optional_float(candidate.get("avg_fps")) or 0.0)
        for candidate in candidates
    )
    fpsw = _optional_float(profile.get("efficiency_fps_per_w"))
    fps = _optional_float(profile.get("avg_fps"))
    fpsw_score = _relative_score(fpsw, best_fpsw)
    fps_score = _relative_score(fps, best_fps)
    return (fpsw_score + fps_score) / 2.0


def _relative_score(value: float | None, best: float) -> float:
    if value is None or best <= 0.0:
        return 0.0
    return max(0.0, float(value) / float(best))


def _optional_float(value: object | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _truthy(value: object | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _profile_sort_time(profile: dict) -> float:
    for key in ("profile_created_at", "verified_at", "created_at"):
        value = str(profile.get(key, "")).strip()
        if not value:
            continue
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            continue
    path = Path(str(profile.get("path", ""))).expanduser()
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return time.time()
