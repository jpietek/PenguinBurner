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

LUTRIS_GAME_SETTINGS_STORE = GameSettingsStore(LUTRIS_GAME_SETTINGS_FILENAME)

lutris_game_settings_path = LUTRIS_GAME_SETTINGS_STORE.path
load_lutris_game_settings = LUTRIS_GAME_SETTINGS_STORE.load
lutris_game_setting = LUTRIS_GAME_SETTINGS_STORE.get
store_lutris_game_setting = LUTRIS_GAME_SETTINGS_STORE.store
