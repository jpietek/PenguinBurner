"""Per-game PenguinBurner settings for Heroic games.

The record and its JSON store are shared with every other launcher
(``integrations.launchers.game_settings``); only the file name is Heroic's own,
because app names and Lutris ids would otherwise share one map.

Heroic signs into several stores at once, but they are all one person's
library, so there is no account layer here -- unlike Steam.
"""

from __future__ import annotations

from integrations.launchers.game_settings import GameSettingsStore

HEROIC_GAME_SETTINGS_FILENAME = "heroic-game-settings.json"

HEROIC_GAME_SETTINGS_STORE = GameSettingsStore(HEROIC_GAME_SETTINGS_FILENAME)

load_heroic_game_settings = HEROIC_GAME_SETTINGS_STORE.load
