"""Heroic's per-game store uses the shared record and JSON format.

Its separate file prevents collisions with other launchers' game ids."""

from __future__ import annotations

from integrations.launchers.game_settings import GameSettingsStore

HEROIC_GAME_SETTINGS_FILENAME = "heroic-game-settings.json"

HEROIC_GAME_SETTINGS_STORE = GameSettingsStore(HEROIC_GAME_SETTINGS_FILENAME)
