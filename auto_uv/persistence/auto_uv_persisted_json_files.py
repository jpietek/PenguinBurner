"""Define the JSON files Auto-UV uses for UI handoff and crash caching.

The helpers make writes atomic and preserve desktop-user ownership for files created by elevated runs.
"""

from __future__ import annotations

from pathlib import Path

from common.atomic_write import atomic_write_json
from common.penguin_burner_paths import default_user_config_dir


def auto_uv_user_config_dir() -> Path:
    return default_user_config_dir()


def probe_in_progress_path() -> Path:
    return auto_uv_user_config_dir() / "uv-result" / "auto-uv-probe-in-progress.json"


def unsafe_voltage_blacklist_path() -> Path:
    # Outside uv-result/ on purpose: clearing resumable scan state for a fresh
    # scan must not erase what the card has already proven unsafe.
    return auto_uv_user_config_dir() / "auto-uv-unsafe-voltages.json"


def legacy_unsafe_voltage_blacklist_path() -> Path:
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


def auto_uv_stop_request_aborts_final_choice() -> bool:
    try:
        request_text = auto_uv_stop_request_path().read_text(
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, OSError):
        return False
    return "abort-final-choice" in request_text.lower()


def clear_auto_uv_stop_request() -> None:
    try:
        auto_uv_stop_request_path().unlink()
    except FileNotFoundError:
        pass


def safe_json_write(path: Path, payload: dict) -> Path:
    return atomic_write_json(path, payload, durable=True)
