"""Faugus's per-game store uses the shared record and JSON format.

Its separate file prevents collisions with other launchers' game ids."""

from __future__ import annotations

from integrations.launchers.game_settings import GameSettingsStore

FAUGUS_GAME_SETTINGS_FILENAME = "faugus-game-settings.json"

FAUGUS_GAME_SETTINGS_STORE = GameSettingsStore(FAUGUS_GAME_SETTINGS_FILENAME)
