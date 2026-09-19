"""Lutris's per-game store, including legacy field-name migration.

Unlike Steam, the library has no account layer."""

from __future__ import annotations

from integrations.launchers.game_settings import GameSettingsStore

LUTRIS_GAME_SETTINGS_FILENAME = "lutris-game-settings.json"

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
