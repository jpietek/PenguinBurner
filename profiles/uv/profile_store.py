from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from common.atomic_write import atomic_write_json
from common.penguin_burner_paths import (
    claim_desktop_user_ownership,
    default_user_config_dir,
)
from profiles.gpu_identity import (
    normalized_gpu_identity,
    profile_gpu_identity,
    profile_gpu_label,
    profile_gpu_uuid,
)

from .profile_tiers import (
    load_profile_tier_assignments,
    load_profile_tier_disabled_profile_ids,
    migrate_legacy_profile_tier_to_gpu,
    profile_tier_summary_fields,
)

_PROFILE_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_USER_EDITED_PROFILE_SOURCE = "user-edited"

# Reserved profile selector meaning "run the daemon at stock -- apply no
# undervolt". Used so the daemon can keep running (fan control) while the GPU
# stays at factory clocks/voltage, and so this survives daemon restart/reboot.
# Double-underscore + reserved word so it can never collide with a real
# profile_id (which are timestamp-prefixed).
STOCK_PROFILE_SELECTOR = "__stock__"


def auto_uv_profiles_dir() -> Path:
    return default_user_config_dir() / "auto-uv-profiles"


def _now() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now().isoformat()


def _file_time_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
    except OSError:
        return _now_iso()


def read_json_object(path: str | Path) -> dict | None:
    """A JSON file's top-level object, or None when it is missing, unreadable
    or not an object."""
    try:
        payload = json.loads(
            Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, dict) else None


def _safe_profile_id(value: str) -> str:
    text = _PROFILE_ID_SAFE_RE.sub("-", str(value).strip()).strip("-")
    return text or "profile"


def _candidate_id(payload: dict) -> str:
    candidate_id = str(payload.get("candidate_id", "")).strip()
    if candidate_id:
        return _safe_profile_id(candidate_id)
    voltage = payload.get("candidate_voltage_mv", payload.get("voltage_mv", "na"))
    clock = payload.get("lock_clock_mhz", payload.get("clock_mhz", "na"))
    return _safe_profile_id(f"{voltage}mv-{clock}mhz")


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
        return 0.0


def profile_payload_from_path(profile: dict) -> dict | None:
    """The saved payload a profile summary points at through its ``path``."""
    path_text = str(profile.get("path", "")).strip()
    if not path_text:
        return None
    return read_json_object(path_text)


def profile_id_from_archive_path(path: str | Path) -> str:
    name = Path(path).name
    prefix = "auto-uv-profile-"
    suffix = ".json"
    if name.startswith(prefix) and name.endswith(suffix):
        return name[len(prefix) : -len(suffix)]
    return Path(path).stem


def archive_auto_uv_profile(final_curve_payload: dict) -> Path:
    timestamp = _now()
    created_at = timestamp.isoformat()
    candidate_id = _candidate_id(final_curve_payload)
    stamp = timestamp.strftime("%Y%m%d-%H%M%S-%f")
    profile_id = _safe_profile_id(
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
    return atomic_write_json(
        auto_uv_profiles_dir() / f"auto-uv-profile-{profile_id}.json",
        payload,
    )


def mark_auto_uv_profile_verified(
    selector: str | Path,
    *,
    verification: dict | None = None,
    metrics: dict | None = None,
    base_metrics: dict | None = None,
    gpu_identity: dict | None = None,
) -> Path:
    resolved = resolve_auto_uv_profile(str(selector), allow_unverified=True)
    if resolved is None:
        raise FileNotFoundError(f"Auto-UV profile not found: {selector}")
    path, _resolved_payload = resolved
    payload = read_json_object(path)
    if payload is None:
        raise FileNotFoundError(f"Auto-UV profile not readable: {path}")

    now = _now_iso()
    updated = dict(payload)
    updated["final_verified"] = True
    updated["requires_verification"] = False
    updated["verification_status"] = "verified"
    updated["verified_at"] = now
    updated.setdefault("profile_source", "auto-uv-final")
    normalized_identity = normalized_gpu_identity(gpu_identity or {})
    if str(normalized_identity.get("uuid") or "").strip():
        updated["gpu_identity"] = normalized_identity

    merged_verification = {}
    if isinstance(updated.get("verification"), dict):
        merged_verification.update(updated["verification"])
    merged_verification["verified_at"] = now
    if isinstance(verification, dict):
        merged_verification.update(
            {
                key: value
                for key, value in verification.items()
                if value not in (None, "")
            }
        )
    updated["verification"] = merged_verification

    if isinstance(metrics, dict):
        for key in _VERIFICATION_METRIC_KEYS:
            value = metrics.get(key)
            if value in (None, "") or updated.get(key) not in (None, ""):
                continue
            updated[key] = value

    if isinstance(base_metrics, dict):
        for source_key, target_key in _BASE_VERIFICATION_METRIC_KEYS.items():
            value = base_metrics.get(source_key)
            if value in (None, "") or updated.get(target_key) not in (None, ""):
                continue
            updated[target_key] = value

    written_path = atomic_write_json(path, updated)
    if str(normalized_identity.get("uuid") or "").strip():
        migrate_legacy_profile_tier_to_gpu(
            str(updated.get("profile_id") or path.stem),
            str(normalized_identity["uuid"]),
        )
    return written_path


def bind_auto_uv_profile_gpu_identity(
    selector: str | Path,
    gpu_identity: dict,
) -> Path:
    """Bind a legacy profile during an explicit, unambiguous user action."""

    resolved = resolve_auto_uv_profile(str(selector), allow_unverified=True)
    if resolved is None:
        raise FileNotFoundError(f"Auto-UV profile not found: {selector}")
    path, _resolved_payload = resolved
    payload = read_json_object(path)
    if payload is None:
        raise FileNotFoundError(f"Auto-UV profile not readable: {path}")
    normalized_identity = normalized_gpu_identity(gpu_identity)
    target_uuid = str(normalized_identity.get("uuid") or "").strip()
    if not target_uuid:
        raise ValueError("GPU identity requires a stable UUID")
    existing_identity = profile_gpu_identity(payload)
    existing_uuid = str(existing_identity.get("uuid") or "").strip()
    if existing_uuid and existing_uuid.casefold() != target_uuid.casefold():
        raise ValueError(
            f"profile is already bound to {existing_uuid}, not {target_uuid}"
        )
    updated = dict(payload)
    updated["gpu_identity"] = normalized_identity
    written_path = atomic_write_json(path, updated)
    migrate_legacy_profile_tier_to_gpu(
        str(updated.get("profile_id") or path.stem),
        target_uuid,
    )
    return written_path


def mark_auto_uv_profile_verification_failed(
    selector: str | Path,
    *,
    failure: dict | None = None,
) -> Path | None:
    resolved = resolve_auto_uv_profile(str(selector), allow_unverified=True)
    if resolved is None:
        raise FileNotFoundError(f"Auto-UV profile not found: {selector}")
    path, _resolved_payload = resolved
    payload = read_json_object(path)
    if payload is None:
        raise FileNotFoundError(f"Auto-UV profile not readable: {path}")
    if not _is_user_edited_profile_payload(payload):
        return None

    now = _now_iso()
    updated = dict(payload)
    updated["final_verified"] = False
    updated["requires_verification"] = True
    updated["verification_status"] = "failed"
    updated["verification_failed_at"] = now

    merged_verification = {}
    if isinstance(updated.get("verification"), dict):
        merged_verification.update(updated["verification"])
    merged_verification["failed_at"] = now
    if isinstance(failure, dict):
        merged_verification["failure"] = {
            key: value
            for key, value in failure.items()
            if value not in (None, "")
        }
    updated["verification"] = merged_verification
    return atomic_write_json(path, updated)


_VERIFICATION_METRIC_KEYS = (
    "avg_core_clock_mhz",
    "avg_fps",
    "avg_power_w",
    "max_power_w",
    "avg_voltage_mv",
    "max_voltage_mv",
    "avg_temperature_c",
    "max_temperature_c",
    "avg_fan_speed_pct",
    "max_fan_speed_pct",
    "efficiency_fps_per_w",
    "efficiency_mhz_per_w",
    "watts_per_mhz",
)

_BASE_VERIFICATION_METRIC_KEYS = {
    "avg_core_clock_mhz": "base_avg_core_clock_mhz",
    "avg_fps": "base_avg_fps",
    "avg_power_w": "base_avg_power_w",
    "efficiency_fps_per_w": "base_efficiency_fps_per_w",
    "avg_voltage_mv": "base_avg_voltage_mv",
    "max_power_w": "base_max_power_w",
    "max_voltage_mv": "base_max_voltage_mv",
    "avg_temperature_c": "base_avg_temperature_c",
    "max_temperature_c": "base_max_temperature_c",
    "avg_fan_speed_pct": "base_avg_fan_speed_pct",
    "max_fan_speed_pct": "base_max_fan_speed_pct",
}

def _normalize_profile_payload(
    payload: dict,
    *,
    path: Path,
    source: str,
    include_unverified_user_edits: bool = True,
) -> dict | None:
    points = payload.get("points")
    plan = payload.get("plan")
    if not isinstance(points, list) and not isinstance(plan, list):
        return None

    candidate_voltage_mv = payload.get("candidate_voltage_mv", payload.get("voltage_mv"))
    lock_clock_mhz = payload.get("lock_clock_mhz", payload.get("clock_mhz"))
    if candidate_voltage_mv in (None, "") or lock_clock_mhz in (None, ""):
        return None
    if not _profile_payload_is_visible(
        payload,
        include_unverified_user_edits=include_unverified_user_edits,
    ):
        return None

    profile = dict(payload)
    profile.setdefault("profile_source", source)
    profile["path"] = str(path)
    profile.setdefault("candidate_id", _candidate_id(profile))
    profile.setdefault("profile_id", _safe_profile_id(str(profile["candidate_id"])))
    profile.setdefault("profile_created_at", _file_time_iso(path))
    return profile


def _is_final_verified_profile_payload(payload: dict) -> bool:
    return bool(payload.get("final_verified", False))


def _is_user_edited_profile_payload(payload: dict) -> bool:
    return str(payload.get("profile_source", "")).strip() == _USER_EDITED_PROFILE_SOURCE


def _profile_payload_is_visible(
    payload: dict,
    *,
    include_unverified_user_edits: bool,
) -> bool:
    if _is_final_verified_profile_payload(payload):
        return True
    return bool(
        include_unverified_user_edits
        and _is_user_edited_profile_payload(payload)
        and payload.get("requires_verification", True)
    )


def _profile_payload_is_runnable(
    payload: dict,
    *,
    allow_unverified: bool = False,
) -> bool:
    if _is_final_verified_profile_payload(payload):
        return True
    return bool(allow_unverified and _is_user_edited_profile_payload(payload))


def _load_profile_files(
    paths: list[Path],
    *,
    source: str,
    include_unverified_user_edits: bool = True,
) -> list[dict]:
    profiles = []
    for path in paths:
        payload = read_json_object(path)
        if payload is None:
            continue
        profile = _normalize_profile_payload(
            payload,
            path=path,
            source=source,
            include_unverified_user_edits=include_unverified_user_edits,
        )
        if profile is not None:
            profiles.append(profile)
    return profiles


def read_auto_uv_profiles(
    *,
    include_unverified_user_edits: bool = True,
) -> list[dict]:
    profile_paths = sorted(auto_uv_profiles_dir().glob("*.json"))
    profiles = _load_profile_files(
        profile_paths,
        source="profile-store",
        include_unverified_user_edits=include_unverified_user_edits,
    )
    apply_latest_final_verification_metrics(profiles)
    profiles.sort(key=_profile_sort_time, reverse=True)
    return profiles


def apply_latest_final_verification_metrics(profiles: list[dict]) -> None:
    """Repair the latest pre-fix profile in memory from its matching long check."""
    # Post-fix profiles embed their long-check metrics at write time
    # (final_verification_metrics) and their Q2RTX-only clock must not be
    # overwritten with the repair file's blended average; only a legacy
    # profile still needs the repair, so skip the file read when none is
    # present.
    unrepaired = [
        profile
        for profile in profiles
        if bool(profile.get("final_verified"))
        and not bool(profile.get("final_verification_metrics"))
    ]
    if not unrepaired:
        return
    latest = read_json_object(
        default_user_config_dir() / "uv-result" / "auto-uv-latest-verified.json"
    )
    if latest is None:
        return
    candidate_id = _candidate_id(latest)
    verified_at = _iso_timestamp(latest.get("verified_at"))
    if latest.get("avg_core_clock_mhz") in (None, "") or verified_at is None:
        return
    matches = [
        profile
        for profile in unrepaired
        if _candidate_id(profile) == candidate_id
        and _profile_matches_verification_time(profile, verified_at)
    ]
    if not matches:
        return
    newest_match = max(matches, key=_profile_sort_time)
    for key in _VERIFICATION_METRIC_KEYS:
        value = latest.get(key)
        if value not in (None, ""):
            newest_match[key] = value
    newest_match["final_q2rtx_avg_core_clock_mhz"] = latest["avg_core_clock_mhz"]
    newest_match["final_verification_metrics"] = True


def _profile_matches_verification_time(profile: dict, verified_at: float) -> bool:
    created_at = _iso_timestamp(
        profile.get("profile_created_at") or profile.get("verified_at")
    )
    if created_at is None:
        return False
    return 0.0 <= created_at - float(verified_at) <= 300.0


def _iso_timestamp(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def profile_summary(
    profile: dict,
    *,
    tier_assignments: dict[str, str] | None = None,
    disabled_profile_tier_ids: set[str] | None = None,
) -> dict:
    summary = {
        "profile_id": str(profile.get("profile_id", "")),
        "candidate_id": str(profile.get("candidate_id", "")),
        "display_name": str(profile.get("display_name", "")),
        "profile_created_at": str(profile.get("profile_created_at", "")),
        "verified_at": profile.get("verified_at"),
        "profile_source": str(profile.get("profile_source", "")),
        "path": str(profile.get("path", "")),
        "candidate_voltage_mv": profile.get("candidate_voltage_mv"),
        "lock_clock_mhz": profile.get("lock_clock_mhz"),
        "memory_offset_mhz": profile.get("memory_offset_mhz"),
        "power_limit_w": profile.get("power_limit_w"),
        "avg_core_clock_mhz": _effective_profile_clock_mhz(profile),
        "avg_fps": profile.get("avg_fps"),
        "avg_power_w": profile.get("avg_power_w"),
        "efficiency_fps_per_w": profile.get("efficiency_fps_per_w"),
        "base_candidate_voltage_mv": profile.get("base_candidate_voltage_mv"),
        "base_lock_clock_mhz": profile.get("base_lock_clock_mhz"),
        "base_avg_core_clock_mhz": profile.get("base_avg_core_clock_mhz"),
        "base_avg_fps": profile.get("base_avg_fps"),
        "base_avg_power_w": profile.get("base_avg_power_w"),
        "base_efficiency_fps_per_w": profile.get("base_efficiency_fps_per_w"),
        "final_verified": bool(profile.get("final_verified", False)),
        "requires_verification": bool(profile.get("requires_verification", False)),
        "verification_status": profile.get("verification_status"),
        "manual_edit": profile.get("manual_edit"),
        "gpu_identity": profile_gpu_identity(profile),
    }
    summary.update(
        profile_tier_summary_fields(
            profile,
            tier_assignments,
            disabled_profile_ids=disabled_profile_tier_ids,
        )
    )
    return summary


def _effective_profile_clock_mhz(profile: dict):
    long_clock = profile.get("final_q2rtx_avg_core_clock_mhz")
    if long_clock not in (None, ""):
        return long_clock
    return profile.get("avg_core_clock_mhz")


def profile_display_name(profile: dict) -> str:
    clock = _display_number(profile.get("lock_clock_mhz"), precision=0)
    voltage = _display_number(profile.get("candidate_voltage_mv"), precision=0)
    if clock and voltage:
        return f"{clock} MHz {voltage} mV"
    if clock:
        return f"{clock} MHz"
    if voltage:
        return f"{voltage} mV"
    return str(profile.get("candidate_id") or profile.get("profile_id") or "")


def memory_offset_summary(value) -> str:
    """`mem +3000 MHz`, or nothing when the profile carries no offset."""
    text = display_signed_memory_clock(value)
    if not text or text.startswith("0 "):
        return ""
    return f"mem {text}"


def clock_voltage_memory_summary(
    *,
    lock_clock_mhz=None,
    candidate_voltage_mv=None,
    memory_offset_mhz=None,
) -> str:
    """`2500 MHz 850 mV, mem +3000 MHz` -- the one phrasing for a tuning point.

    The running-profile line, the profile table and the names given to
    user-edited profiles all read the same way because they all come through
    here.
    """
    clock = _display_number(lock_clock_mhz, precision=0)
    voltage = _display_number(candidate_voltage_mv, precision=0)
    if clock and voltage:
        text = f"{clock} MHz {voltage} mV"
    else:
        text = f"{clock} MHz" if clock else (f"{voltage} mV" if voltage else "")
    memory = memory_offset_summary(memory_offset_mhz)
    if text and memory:
        return f"{text}, {memory}"
    return text or memory


def profile_clock_voltage_memory_summary(profile: dict) -> str:
    return clock_voltage_memory_summary(
        lock_clock_mhz=profile.get("lock_clock_mhz"),
        candidate_voltage_mv=profile.get("candidate_voltage_mv"),
        memory_offset_mhz=profile.get("memory_offset_mhz"),
    )


# The two shapes the edit dialogs generated before they named a profile after
# the point it runs at: "User edited memory offset +6000 MT/s" and the
# memory-less "User edited 2500 MHz 850 mV".
_LEGACY_USER_EDITED_NAME_RE = re.compile(
    r"^User edited (?:memory offset [+-]?\d+ MT/s|\d+ MHz \d+ mV)$"
)


def profile_presentation_name(profile: dict) -> str:
    """The name to show for a saved profile.

    A profile edited before the naming changed still carries the old label in
    its file. Rewriting saved profiles for the sake of a label would be a
    silent write to user data, so the refresh happens here on read: a name
    that is recognisably one the old dialogs generated is recomputed from the
    profile's own clock, voltage and memory offset. Every other name --
    Afterburner imports, edited fan curves, anything hand-picked -- is left
    exactly as saved.
    """
    display_name = str(profile.get("display_name", "")).strip()
    if display_name and _LEGACY_USER_EDITED_NAME_RE.match(display_name):
        summary = profile_clock_voltage_memory_summary(profile)
        if summary:
            return f"User edited {summary}"
    if display_name:
        return display_name
    return profile_display_name(profile)


def user_edited_display_name(
    *,
    lock_clock_mhz=None,
    candidate_voltage_mv=None,
    memory_offset_mhz=None,
) -> str:
    """Name an edited profile after the point it actually runs at."""
    summary = clock_voltage_memory_summary(
        lock_clock_mhz=lock_clock_mhz,
        candidate_voltage_mv=candidate_voltage_mv,
        memory_offset_mhz=memory_offset_mhz,
    )
    return f"User edited {summary}" if summary else "User edited profile"


def _display_date(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text[:19].replace("T", " ")


def read_auto_uv_profile_summaries() -> list[dict]:
    assignments_by_gpu: dict[str, dict[str, str]] = {}
    disabled_by_gpu: dict[str, set[str]] = {}
    summaries: list[dict] = []
    for profile in read_auto_uv_profiles():
        gpu_uuid = profile_gpu_uuid(profile)
        if gpu_uuid not in assignments_by_gpu:
            assignments_by_gpu[gpu_uuid] = load_profile_tier_assignments(
                gpu_uuid=gpu_uuid
            )
            disabled_by_gpu[gpu_uuid] = load_profile_tier_disabled_profile_ids(
                gpu_uuid=gpu_uuid
            )
        summaries.append(
            profile_summary(
                profile,
                tier_assignments=assignments_by_gpu[gpu_uuid],
                disabled_profile_tier_ids=disabled_by_gpu[gpu_uuid],
            )
        )
    return summaries


def delete_auto_uv_profiles(selectors: list[str | Path]) -> list[Path]:
    paths: list[Path] = []
    for selector in selectors:
        text = str(selector or "").strip()
        if not text:
            continue
        resolved = resolve_auto_uv_profile(text, allow_unverified=True)
        if resolved is not None:
            paths.append(resolved[0])
        else:
            paths.append(Path(text).expanduser())
    return delete_auto_uv_profile_paths(paths)


def delete_auto_uv_profile_paths(paths: Sequence[str | Path]) -> list[Path]:
    deleted: list[Path] = []
    seen: set[Path] = set()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not _is_deletable_auto_uv_profile_path(resolved):
            continue
        claim_desktop_user_ownership(path.parent, include_parents=True)
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        deleted.append(resolved)
    return deleted


def resolve_auto_uv_profile(
    selector: str,
    *,
    allow_unverified: bool = False,
) -> tuple[Path, dict] | None:
    text = str(selector or "").strip()
    if not text:
        return None

    if text in {"active", "latest"}:
        profiles = read_auto_uv_profiles(
            include_unverified_user_edits=bool(allow_unverified)
        )
        if not profiles:
            return None
        profile = profiles[0]
        path = Path(str(profile.get("path", ""))).expanduser()
        return path, profile

    path = Path(text).expanduser()
    if path.is_file():
        payload = read_json_object(path)
        if payload is None:
            return None
        if not _profile_payload_is_runnable(
            payload,
            allow_unverified=bool(allow_unverified),
        ):
            return None
        return path, payload

    for profile in read_auto_uv_profiles(
        include_unverified_user_edits=bool(allow_unverified)
    ):
        if text in {
            str(profile.get("profile_id", "")),
            str(profile.get("candidate_id", "")),
            Path(str(profile.get("path", ""))).name,
            Path(str(profile.get("path", ""))).stem,
        }:
            path = Path(str(profile.get("path", ""))).expanduser()
            return path, profile
    return None


def _is_deletable_auto_uv_profile_path(path: Path) -> bool:
    profiles_dir = auto_uv_profiles_dir().resolve()
    resolved = path.resolve(strict=False)
    if resolved.suffix.lower() != ".json":
        return False
    try:
        resolved.relative_to(profiles_dir)
    except ValueError:
        return False
    return True


def format_profile_table(profiles: list[dict]) -> str:
    rows = [
        (
            "created",
            "id",
            "GPU",
            "tier",
            "profile",
            "mV",
            "target MHz",
            "effective MHz",
            "FPS/W",
            "Mem",
            "source",
        )
    ]
    for profile in profiles:
        created = _display_date(
            profile.get("profile_created_at") or profile.get("verified_at")
        )
        rows.append(
            (
                created,
                str(profile.get("profile_id", "")),
                profile_gpu_label(profile),
                str(profile.get("profile_tier", "")),
                profile_display_name(profile),
                _display_number(profile.get("candidate_voltage_mv"), precision=0),
                _display_number(profile.get("lock_clock_mhz"), precision=0),
                _display_number(_effective_profile_clock_mhz(profile), precision=2),
                _display_number(profile.get("efficiency_fps_per_w"), precision=4),
                _display_signed_memory_clock(profile.get("memory_offset_mhz")),
                str(profile.get("profile_source", "")),
            )
        )
    widths = [max(len(str(row[index])) for row in rows) for index in range(len(rows[0]))]
    lines = []
    for row in rows:
        lines.append(
            "  ".join(str(value).ljust(widths[index]) for index, value in enumerate(row))
        )
    return "\n".join(lines)


def _display_number(value, *, precision: int) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if precision <= 0:
        return str(round(number))
    return f"{number:.{int(precision)}f}"


def _display_signed_number(value, *, precision: int) -> str:
    text = _display_number(value, precision=precision)
    if not text:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return text
    if abs(number) < 0.5:
        return "0"
    return f"+{text}" if number > 0 else text


def display_signed_memory_clock(value) -> str:
    # The stored memory offset is an NVML transfer-rate value (MT/s); the
    # realized memory clock moves by half of it (verified on Blackwell,
    # issue #20). Show the memory-clock MHz, matching the Auto-UV dialog.
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    text = _display_signed_number(number / 2, precision=0)
    if not text:
        return ""
    return f"{text} MHz"


# Backwards-compatible private alias for existing in-module callers.
_display_signed_memory_clock = display_signed_memory_clock
