"""Read and update the persisted unsafe voltage blacklist.

Only hard crash-like entries block future search; controlled stops remain visible but non-blocking.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .auto_uv_persisted_json_files import (
    legacy_unsafe_voltage_blacklist_path,
    safe_json_write,
    unsafe_voltage_blacklist_path,
)
from .unsafe_voltage_cache import unsafe_entry_blocks_future_search


def load_unsafe_voltage_blacklist() -> list[dict]:
    path = unsafe_voltage_blacklist_path()
    if not path.is_file():
        legacy = legacy_unsafe_voltage_blacklist_path()
        if not legacy.is_file():
            return []
        # Releases up to 0.8.3 kept the blacklist inside uv-result/. Adopt it
        # once at the new location; the old file is left in place.
        entries = _read_entries(legacy)
        if entries:
            try:
                write_unsafe_voltage_entries(entries)
            except OSError:
                pass
        return entries
    return _read_entries(path)


def _read_entries(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    return [
        dict(entry)
        for entry in entries
        if isinstance(entry, dict) and unsafe_entry_blocks_future_search(entry)
    ]


def record_unsafe_voltage(
    *,
    candidate_voltage_mv: int,
    lock_clock_mhz: int,
    reason: str,
    phase: str | None = None,
    marker_started_at: str | None = None,
    details: dict | None = None,
    blocked_lock_clock_mhz: list[int] | None = None,
) -> tuple[Path, dict]:
    blocked_locks = normalize_blocked_lock_clocks(
        int(lock_clock_mhz),
        blocked_lock_clock_mhz,
    )
    entry = {
        "recorded_at": datetime.now().astimezone().isoformat(),
        "reason": str(reason),
        "candidate_voltage_mv": int(candidate_voltage_mv),
        "lock_clock_mhz": int(lock_clock_mhz),
        "phase": str(phase).strip() if phase else None,
        "marker_started_at": (
            str(marker_started_at).strip() if marker_started_at else None
        ),
    }
    if blocked_locks != [int(lock_clock_mhz)]:
        entry["blocked_lock_clock_mhz"] = blocked_locks
    if details:
        entry["details"] = details

    entries = []
    for item in load_unsafe_voltage_blacklist():
        if not unsafe_entry_matches(item, entry, blocked_locks=blocked_locks):
            entries.append(item)
    entries.append(entry)
    return write_unsafe_voltage_entries(entries), entry


def write_unsafe_voltage_entries(entries: list[dict]) -> Path:
    return safe_json_write(
        unsafe_voltage_blacklist_path(),
        {
            "format_version": 1,
            "updated_at": datetime.now().astimezone().isoformat(),
            "entries": entries,
        },
    )


def unsafe_entry_matches(
    existing: dict,
    entry: dict,
    *,
    blocked_locks: list[int],
) -> bool:
    try:
        return (
            int(existing.get("candidate_voltage_mv", -1))
            == int(entry["candidate_voltage_mv"])
            and int(existing.get("lock_clock_mhz", -1)) == int(entry["lock_clock_mhz"])
            and str(existing.get("reason", "")) == str(entry["reason"])
            and normalize_blocked_lock_clocks(
                int(existing.get("lock_clock_mhz", -1)),
                existing.get("blocked_lock_clock_mhz"),
            )
            == blocked_locks
        )
    except (KeyError, TypeError, ValueError):
        return False


def normalize_blocked_lock_clocks(
    lock_clock_mhz: int,
    blocked_lock_clock_mhz: object,
) -> list[int]:
    values = {int(lock_clock_mhz)} if int(lock_clock_mhz) > 0 else set()
    if isinstance(blocked_lock_clock_mhz, (list, tuple, set)):
        for value in blocked_lock_clock_mhz:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                values.add(int(parsed))
    return sorted(values, reverse=True)
