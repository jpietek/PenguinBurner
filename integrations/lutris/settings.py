"""Per-game PenguinBurner settings for Lutris games.

Steam keys its settings by account because two Steam accounts on one machine
must not overwrite each other's presets. Lutris has no account concept — the
library is the user's — so this file is a flat game_id -> setting map and
carries no account layer at all.

``prefix_command`` bookkeeping replaces Steam's launch-options bookkeeping: the
string as it was before we touched it, and the string we wrote, so a later
removal can restore the user's own value instead of guessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from auto_uv.persistence.auto_uv_persisted_json_files import safe_json_write
from common.penguin_burner_paths import default_user_config_dir
from overlay.wrapper_tokens import ingame_latency_present
from profiles.game_profile import (
    GAME_MODE_ADAPTIVE,
    GAME_MODE_NONE,
    GAME_MODES,
    normalize_game_mode,
    normalize_game_target_fps,
)

LUTRIS_GAME_SETTINGS_FILENAME = "lutris-game-settings.json"


@dataclass(frozen=True)
class LutrisGameSetting:
    enabled: bool = False
    mode: str = GAME_MODE_ADAPTIVE
    overlay: bool = False
    # Launch-line read-back for marker capture. Managed writes derive this from
    # the mode: Adaptive enables it and fixed modes do not. The stored field
    # keeps old settings and exact raw-prefix parsing compatible.
    ingame_latency: bool = False
    # prefix_command as it stood before injection, and as we last wrote it.
    original_prefix_command: str = ""
    injected_prefix_command: str = ""
    # None = follow the global [adaptive] target_fps from the runtime config.
    target_fps: float | None = None
    # Stable NVML UUID. Empty keeps single-GPU settings compatible; multi-GPU
    # hosts require an explicit value before enabling the wrapper.
    gpu_uuid: str = ""
    # None is a legacy record whose original source was not recorded.
    original_prefix_inherited: bool | None = None

    @property
    def active(self) -> bool:
        return self.enabled


def lutris_game_settings_path() -> Path:
    return default_user_config_dir() / LUTRIS_GAME_SETTINGS_FILENAME


def load_lutris_game_settings(
    path: str | Path | None = None,
) -> dict[str, LutrisGameSetting]:
    """game_id -> setting."""
    payload = _read_payload(path)
    games = payload.get("games")
    if not isinstance(games, dict):
        return {}
    parsed: dict[str, LutrisGameSetting] = {}
    for game_id, entry in games.items():
        if not isinstance(entry, dict):
            continue
        stored_mode = normalize_game_mode(entry.get("mode"))
        mode = stored_mode if stored_mode in GAME_MODES else GAME_MODE_ADAPTIVE
        injected = str(entry.get("injected_prefix_command") or "")
        parsed[str(game_id)] = LutrisGameSetting(
            enabled=bool(
                entry.get("enabled", bool(injected) and stored_mode != GAME_MODE_NONE)
            ),
            mode=mode,
            overlay=bool(entry.get("overlay")),
            # Absent in files written before this key existed. The line we
            # injected is stored beside it and carries the answer, so an
            # upgrade reads the flag off that rather than defaulting it off --
            # which would have shown the switch off for a game whose command
            # plainly asks for the markers, and stripped the opt-in on the next
            # apply.
            ingame_latency=bool(
                entry.get("ingame_latency", ingame_latency_present(injected))
            ),
            original_prefix_command=str(entry.get("original_prefix_command") or ""),
            injected_prefix_command=injected,
            original_prefix_inherited=(
                entry.get("original_prefix_inherited")
                if isinstance(entry.get("original_prefix_inherited"), bool)
                else None
            ),
            target_fps=normalize_game_target_fps(entry.get("target_fps")),
            gpu_uuid=str(entry.get("gpu_uuid") or "").strip(),
        )
    return parsed


def lutris_game_setting(
    game_id: str,
    *,
    path: str | Path | None = None,
) -> LutrisGameSetting | None:
    return load_lutris_game_settings(path).get(str(game_id))


def store_lutris_game_setting(
    game_id: str,
    setting: LutrisGameSetting,
    *,
    path: str | Path | None = None,
) -> Path:
    settings = load_lutris_game_settings(path)
    settings[str(game_id)] = setting
    return _write_settings(settings, path=path)


def _settings_path(path: str | Path | None) -> Path:
    return Path(path).expanduser() if path is not None else lutris_game_settings_path()


def _read_payload(path: str | Path | None) -> dict:
    try:
        payload = json.loads(
            _settings_path(path).read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_settings(
    settings: dict[str, LutrisGameSetting],
    *,
    path: str | Path | None = None,
) -> Path:
    payload = {
        "format_version": 1,
        "updated_at": datetime.now().astimezone().isoformat(),
        "games": {
            game_id: {
                "enabled": setting.enabled,
                "mode": setting.mode,
                "overlay": setting.overlay,
                "ingame_latency": setting.ingame_latency,
                **(
                    {"target_fps": setting.target_fps}
                    if setting.target_fps is not None
                    else {}
                ),
                **({"gpu_uuid": setting.gpu_uuid} if setting.gpu_uuid else {}),
                "original_prefix_command": setting.original_prefix_command,
                "injected_prefix_command": setting.injected_prefix_command,
                "original_prefix_inherited": setting.original_prefix_inherited,
            }
            for game_id, setting in sorted(settings.items())
        },
    }
    return safe_json_write(_settings_path(path), payload)
