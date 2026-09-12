"""Per-game PenguinBurner settings for Lutris games.

The record and its JSON store are shared with every other launcher
(``integrations.launchers.game_settings``); only the file name is Lutris's own.
Steam keys its settings by account because two Steam accounts on one machine
must not overwrite each other's presets. Lutris has no account concept -- the
library is the user's -- so this file is a flat game_id -> setting map.
"""

from __future__ import annotations

from integrations.launchers.game_settings import (
    GameSettingsStore,
    LauncherGameSetting,
)

LUTRIS_GAME_SETTINGS_FILENAME = "lutris-game-settings.json"

#: Lutris's settings are shaped exactly like every other launcher's.
LutrisGameSetting = LauncherGameSetting

#: What Lutris called these fields before the launchers shared one record.
#: Read, never written: the next save migrates the file to the current keys.
LEGACY_KEYS = {
    "original_command": "original_prefix_command",
    "injected_command": "injected_prefix_command",
    "original_inherited": "original_prefix_inherited",
}

LUTRIS_GAME_SETTINGS_STORE = GameSettingsStore(
    LUTRIS_GAME_SETTINGS_FILENAME, legacy_keys=LEGACY_KEYS
)

load_lutris_game_settings = LUTRIS_GAME_SETTINGS_STORE.load
store_lutris_game_setting = LUTRIS_GAME_SETTINGS_STORE.store
