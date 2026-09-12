"""What PenguinBurner remembers about one game, and where that is kept.

Every launcher stores the same answers -- is PenguinBurner on for this game,
which tier, is the overlay drawn, which GPU, what the launch command said
before we touched it and what we wrote -- so the record and its JSON file live
here rather than once per launcher. Only the file name differs, because two
launchers must not share one map: their game ids collide.

Steam is the exception and keeps its own store: its settings are keyed by
account first, since two Steam accounts on one machine must not overwrite each
other's presets. Nothing else has an account layer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from auto_uv.persistence.auto_uv_persisted_json_files import safe_json_write
from common.atomic_write import preserve_unreadable_file
from common.penguin_burner_paths import default_user_config_dir
from overlay.wrapper_tokens import ingame_latency_present
from profiles.game_profile import (
    GAME_MODE_ADAPTIVE,
    GAME_MODE_NONE,
    GAME_MODES,
    normalize_game_mode,
    normalize_game_target_fps,
)


class GameSettingsError(RuntimeError):
    """A settings file could not be saved without losing what it already held."""


@dataclass(frozen=True)
class SettingsWrite:
    """Where a setting landed, and what had to be rescued to put it there."""

    path: Path
    #: Where an unreadable previous file was kept, when there was one.
    preserved: Path | None = None


@dataclass(frozen=True)
class LauncherGameSetting:
    """One game's PenguinBurner preset, as the launcher tabs edit it."""

    enabled: bool = False
    mode: str = GAME_MODE_ADAPTIVE
    overlay: bool = False
    # Launch-line read-back for marker capture. Managed writes derive this from
    # the mode: Adaptive enables it and fixed modes do not. The stored field
    # keeps old settings and exact raw-command parsing compatible.
    ingame_latency: bool = False
    # The launch command as it stood before injection, and as we last wrote it.
    # "Command" is whatever field the launcher puts a wrapper in: Lutris's
    # prefix_command, Heroic's wrapperOptions.
    original_command: str = ""
    injected_command: str = ""
    # None = follow the global [adaptive] target_fps from the runtime config.
    target_fps: float | None = None
    # Stable NVML UUID. Empty keeps single-GPU settings compatible; multi-GPU
    # hosts require an explicit value before enabling the wrapper.
    gpu_uuid: str = ""
    # Whether the original command came from a level above this game. None is
    # a legacy record whose source was never written down.
    original_inherited: bool | None = None

class GameSettingsStore:
    """One launcher's ``game_id -> setting`` map, in one JSON file.

    Every entry point takes an optional ``path`` so a test never reaches the
    real user configuration.
    """

    def __init__(
        self,
        filename: str,
        *,
        legacy_keys: Mapping[str, str] | None = None,
    ) -> None:
        self.filename = filename
        #: Field -> the name this launcher wrote it under before the launchers
        #: shared a record. Read, never written: the next save migrates the
        #: file to the current keys.
        self.legacy_keys = dict(legacy_keys or {})

    def path(self, path: str | Path | None = None) -> Path:
        if path is not None:
            return Path(path).expanduser()
        return default_user_config_dir() / self.filename

    def load(self, path: str | Path | None = None) -> dict[str, LauncherGameSetting]:
        """Every game's preset. A file we cannot read reads as no presets.

        Deliberately not an error: one bad file must not take the library tab
        down. The write path is where an unreadable file is dealt with, since
        that is the only place its content can actually be lost.
        """
        payload, _ = _read_payload(self.path(path))
        return _settings_from_payload(payload, self.legacy_keys)

    def get(
        self,
        game_id: str,
        *,
        path: str | Path | None = None,
    ) -> LauncherGameSetting | None:
        return self.load(path).get(str(game_id))

    def store(
        self,
        game_id: str,
        setting: LauncherGameSetting,
        *,
        path: str | Path | None = None,
    ) -> SettingsWrite:
        """Save one game's preset, keeping every other game's.

        Read-modify-write, and the read is the dangerous half: a file that
        does not parse reads as empty, and writing that back would delete
        every other game's preset to record this one. So an unparseable file
        is moved aside first and the caller is told where it went; if it
        cannot be moved it is not written over at all.

        Single-writer by design: the desktop app is the only thing that writes
        these files (the launch wrapper only reads them), so two PenguinBurner
        windows open at once are the one way to lose an update here. That is
        not defended against -- a lock file would be the fix if it ever
        becomes a real workflow.
        """
        target = self.path(path)
        payload, unreadable = _read_payload(target)
        preserved = _preserve(target) if unreadable else None
        settings = _settings_from_payload(payload, self.legacy_keys)
        settings[str(game_id)] = setting
        return SettingsWrite(
            safe_json_write(target, _payload_for(settings)),
            preserved=preserved,
        )


def _read_payload(path: Path) -> tuple[dict, bool]:
    """The stored mapping, and whether the file exists but could not be read.

    Three cases, and only the third is a problem: absent (nothing was ever
    saved), empty (a write that never got any content into it), and present
    but unparseable -- which is the one that holds settings we would destroy
    by treating it as absent.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {}, False
    except OSError:
        return {}, True
    if not text.strip():
        return {}, False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}, True
    return (payload, False) if isinstance(payload, dict) else ({}, True)


def _preserve(path: Path) -> Path | None:
    try:
        return preserve_unreadable_file(path)
    except OSError as error:
        raise GameSettingsError(
            f"{path} could not be read and could not be moved aside: {error}"
        ) from error


def _settings_from_payload(
    payload: dict, legacy_keys: Mapping[str, str]
) -> dict[str, LauncherGameSetting]:
    games = payload.get("games")
    if not isinstance(games, dict):
        return {}
    return {
        str(game_id): _setting_from_entry(entry, legacy_keys)
        for game_id, entry in games.items()
        if isinstance(entry, dict)
    }


def _value(
    entry: dict,
    key: str,
    legacy_keys: Mapping[str, str],
    default: object = "",
) -> object:
    if key in entry:
        return entry[key]
    legacy = legacy_keys.get(key)
    return entry.get(legacy, default) if legacy else default


def _setting_from_entry(
    entry: dict, legacy_keys: Mapping[str, str]
) -> LauncherGameSetting:
    stored_mode = normalize_game_mode(entry.get("mode"))
    injected = str(_value(entry, "injected_command", legacy_keys) or "")
    inherited = _value(entry, "original_inherited", legacy_keys, None)
    return LauncherGameSetting(
        enabled=bool(
            entry.get("enabled", bool(injected) and stored_mode != GAME_MODE_NONE)
        ),
        mode=stored_mode if stored_mode in GAME_MODES else GAME_MODE_ADAPTIVE,
        overlay=bool(entry.get("overlay")),
        # Absent in files written before this key existed. The command we
        # injected is stored beside it and carries the answer, so an upgrade
        # reads the flag off that rather than defaulting it off -- which would
        # have shown the switch off for a game whose command plainly asks for
        # the markers, and stripped the opt-in on the next apply.
        ingame_latency=bool(
            entry.get("ingame_latency", ingame_latency_present(injected))
        ),
        original_command=str(_value(entry, "original_command", legacy_keys) or ""),
        injected_command=injected,
        target_fps=normalize_game_target_fps(entry.get("target_fps")),
        gpu_uuid=str(entry.get("gpu_uuid") or "").strip(),
        original_inherited=inherited if isinstance(inherited, bool) else None,
    )


def _payload_for(settings: dict[str, LauncherGameSetting]) -> dict:
    return {
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
                "original_command": setting.original_command,
                "injected_command": setting.injected_command,
                "original_inherited": setting.original_inherited,
            }
            for game_id, setting in sorted(settings.items())
        },
    }
