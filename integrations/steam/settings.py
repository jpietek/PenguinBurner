"""Per-account, per-game PenguinBurner settings persistence.

Everything the Steam tab configures lives here (mode, overlay, the pre- and
post-injection launch strings), keyed by Steam account id so two accounts on
one machine never overwrite each other's presets. Steam's launch options only
carry the wrapper tokens; the wrapper resolves the mode from this file at
launch time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path

from auto_uv.persistence.auto_uv_persisted_json_files import safe_json_write
from common.penguin_burner_paths import default_user_config_dir
from profiles.uv.profile_tiers import PROFILE_TIERS, normalize_profile_tier
from runtime.support.adaptive_target_fps import (
    MAX_ADAPTIVE_TARGET_FPS,
    MIN_ADAPTIVE_TARGET_FPS,
)


# Steam integration is opt-in per game. New games keep the wrapper disabled,
# preselect Adaptive, and keep overlay visibility off. Hidden legacy modes stay
# readable and migrate to Adaptive.
GAME_MODE_NONE = "none"
GAME_MODE_DEFAULT = "default"
GAME_MODE_STOCK = "stock"
GAME_MODE_ADAPTIVE = "adaptive"
# Modes a stored per-game setting may hold. Stock is a real choice: pin the
# factory GPU state for this game while the system-wide profile stays tuned.
GAME_MODES = (GAME_MODE_ADAPTIVE, *PROFILE_TIERS, GAME_MODE_STOCK)

STEAM_GAME_SETTINGS_FILENAME = "steam-game-settings.json"


def normalize_game_mode(
    value: object | None, *, default: str = GAME_MODE_DEFAULT
) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in (GAME_MODE_NONE, GAME_MODE_DEFAULT, GAME_MODE_STOCK, GAME_MODE_ADAPTIVE):
        return text
    return normalize_profile_tier(text, default=default)


def normalize_game_target_fps(value: object | None) -> float | None:
    """Per-game adaptive target FPS; None means "use the global default"."""
    try:
        fps = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fps):
        return None
    if not MIN_ADAPTIVE_TARGET_FPS <= fps <= MAX_ADAPTIVE_TARGET_FPS:
        return None
    return fps


@dataclass(frozen=True)
class SteamGameSetting:
    enabled: bool = False
    mode: str = GAME_MODE_ADAPTIVE
    overlay: bool = False
    # Frame-history capture (Game Perf Profile). On by default: the daemon records a
    # rolling window for every enabled game unless the user opts out.
    telemetry: bool = True
    original_launch_options: str = ""
    injected_launch_options: str = ""
    # None = follow the global [adaptive] target_fps from the runtime config.
    target_fps: float | None = None

    @property
    def active(self) -> bool:
        return self.enabled


def steam_game_settings_path() -> Path:
    return default_user_config_dir() / STEAM_GAME_SETTINGS_FILENAME


def load_steam_game_settings(
    path: str | Path | None = None,
) -> dict[str, dict[str, SteamGameSetting]]:
    """account_id -> app_id -> setting."""
    payload = _read_payload(path)
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    settings: dict[str, dict[str, SteamGameSetting]] = {}
    for account_id, account in accounts.items():
        games = account.get("games") if isinstance(account, dict) else None
        if not isinstance(games, dict):
            continue
        parsed: dict[str, SteamGameSetting] = {}
        for app_id, entry in games.items():
            if not isinstance(entry, dict):
                continue
            stored_mode = normalize_game_mode(entry.get("mode"))
            mode = stored_mode
            if stored_mode not in GAME_MODES:
                mode = GAME_MODE_ADAPTIVE
            injected = str(entry.get("injected_launch_options") or "")
            parsed[str(app_id)] = SteamGameSetting(
                enabled=bool(
                    entry.get(
                        "enabled",
                        bool(injected) and stored_mode != GAME_MODE_NONE,
                    )
                ),
                mode=mode,
                overlay=bool(entry.get("overlay")),
                telemetry=bool(entry.get("telemetry", True)),
                original_launch_options=str(entry.get("original_launch_options") or ""),
                injected_launch_options=injected,
                target_fps=normalize_game_target_fps(entry.get("target_fps")),
            )
        if parsed:
            settings[str(account_id)] = parsed
    return settings


def steam_game_setting(
    account_id: str,
    app_id: str,
    *,
    path: str | Path | None = None,
) -> SteamGameSetting | None:
    return load_steam_game_settings(path).get(str(account_id), {}).get(str(app_id))


def account_display_names(path: str | Path | None = None) -> dict[str, str]:
    """Persisted human-readable name per account id (for the settings file)."""
    accounts = _read_payload(path).get("accounts")
    if not isinstance(accounts, dict):
        return {}
    return {
        str(account_id): str(account.get("display_name") or "")
        for account_id, account in accounts.items()
        if isinstance(account, dict) and account.get("display_name")
    }


def store_steam_game_setting(
    account_id: str,
    app_id: str,
    setting: SteamGameSetting,
    *,
    path: str | Path | None = None,
    display_name: str = "",
) -> Path:
    names = account_display_names(path)
    if display_name:
        names[str(account_id)] = display_name
    settings = load_steam_game_settings(path)
    settings.setdefault(str(account_id), {})[str(app_id)] = setting
    return _write_settings(settings, display_names=names, path=path)


def remove_steam_game_setting(
    account_id: str,
    app_id: str,
    *,
    path: str | Path | None = None,
) -> Path:
    names = account_display_names(path)
    settings = load_steam_game_settings(path)
    account = settings.get(str(account_id), {})
    account.pop(str(app_id), None)
    if not account:
        settings.pop(str(account_id), None)
    return _write_settings(settings, display_names=names, path=path)


def _settings_path(path: str | Path | None) -> Path:
    return Path(path).expanduser() if path is not None else steam_game_settings_path()


def _read_payload(path: str | Path | None) -> dict:
    try:
        payload = json.loads(
            _settings_path(path).read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_settings(
    settings: dict[str, dict[str, SteamGameSetting]],
    *,
    display_names: dict[str, str] | None = None,
    path: str | Path | None = None,
) -> Path:
    settings_path = _settings_path(path)
    names = display_names or {}
    payload = {
        "format_version": 1,
        "updated_at": datetime.now().astimezone().isoformat(),
        "accounts": {
            account_id: {
                **(
                    {"display_name": names[account_id]}
                    if names.get(account_id)
                    else {}
                ),
                "games": {
                    app_id: {
                        "enabled": setting.enabled,
                        "mode": setting.mode,
                        "overlay": setting.overlay,
                        "telemetry": setting.telemetry,
                        **(
                            {"target_fps": setting.target_fps}
                            if setting.target_fps is not None
                            else {}
                        ),
                        "original_launch_options": setting.original_launch_options,
                        "injected_launch_options": setting.injected_launch_options,
                    }
                    for app_id, setting in sorted(games.items())
                }
            }
            for account_id, games in sorted(settings.items())
            if games
        },
    }
    return safe_json_write(settings_path, payload)
