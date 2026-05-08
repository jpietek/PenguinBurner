"""Define the JSON files Auto-UV uses for UI handoff and crash recovery.

The helpers make writes atomic and preserve desktop-user ownership for files created by elevated runs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from penguin_burner_paths import (
    claim_desktop_user_ownership,
    default_saved_uv_dir,
    default_user_config_dir,
)


def auto_uv_user_config_dir() -> Path:
    return default_user_config_dir()


def auto_uv_saved_uv_dir() -> Path:
    return default_saved_uv_dir()


def probe_in_progress_path() -> Path:
    return auto_uv_user_config_dir() / "uv-result" / "auto-uv-probe-in-progress.json"


def unsafe_voltage_blacklist_path() -> Path:
    return auto_uv_user_config_dir() / "uv-result" / "auto-uv-unsafe-voltages.json"


def verified_candidates_path() -> Path:
    return auto_uv_user_config_dir() / "uv-result" / "auto-uv-verified-candidates.json"


def final_choice_request_path() -> Path:
    return auto_uv_user_config_dir() / "auto-uv-final-choice-request.json"


def final_choice_response_path() -> Path:
    return auto_uv_user_config_dir() / "auto-uv-final-choice.json"


def auto_uv_stop_request_path() -> Path:
    return auto_uv_user_config_dir() / "auto-uv-stop-requested"


def auto_uv_stop_requested() -> bool:
    return auto_uv_stop_request_path().exists()


def clear_auto_uv_stop_request() -> None:
    try:
        auto_uv_stop_request_path().unlink()
    except FileNotFoundError:
        pass


def safe_json_write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(path.parent, include_parents=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(path)
    fsync_directory(path.parent)
    claim_desktop_user_ownership(path)
    return path


def fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
