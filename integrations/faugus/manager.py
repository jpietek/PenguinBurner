"""Adapt Faugus's library and launch_arguments to the shared wrapper manager.

Faugus has one level -- the game's own entry -- so nothing is ever inherited
and disabling restores the original launch arguments. Changes apply at the next
launch; Faugus reads games.json when it starts a game."""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.compatibility import CompatibilityTools
from integrations.launchers.wrapper_manager import (
    CommandWrite,
    EffectiveCommand,
    WrapperManager,
)

from .compatibility import FaugusCompatibility
from .config_store import (
    SOURCE_LABELS,
    FaugusConfigError,
    effective_launch_arguments,
    read_games_document,
    write_launch_arguments,
)
from .library import InstalledFaugusGame, is_store_client, read_faugus_games
from .paths import faugus_installation, faugus_installed
from .settings import FAUGUS_GAME_SETTINGS_STORE


class FaugusIntegrationManager(WrapperManager):
    launcher_id = "faugus"
    display_name = "Faugus"
    source_labels = SOURCE_LABELS

    def __init__(
        self,
        *,
        home: Path | None = None,
        settings_path: str | Path | None = None,
    ):
        super().__init__(FAUGUS_GAME_SETTINGS_STORE, settings_path=settings_path)
        self._home = home
        self.compatibility = CompatibilityTools(
            FaugusCompatibility(home),
            guidance="Applies on the next launch. Close Faugus's game settings before editing here.",
        )
        self._document: list[dict] | None = None
        self.non_game_ids: frozenset[str] = frozenset()

    @property
    def installation(self):
        return faugus_installation(self._home)

    @property
    def available(self) -> bool:
        return faugus_installed(self._home)

    def read_games(self) -> tuple[InstalledFaugusGame, ...]:
        try:
            self._document = read_games_document(self._home)
        except FaugusConfigError:
            self._document = []
            return ()
        # Keep known clients through removal/partial rewrites while they run.
        # An explicitly reused ID for an actual game supersedes that knowledge.
        kinds = {str(entry.get("gameid") or "").strip(): is_store_client(entry)
                 for entry in self._document}
        self.non_game_ids = frozenset(
            game_id for game_id in self.non_game_ids | kinds.keys()
            if kinds.get(game_id, True)
        )
        return read_faugus_games(self._home, document=self._document)

    def read_effective(self, game: InstalledFaugusGame) -> EffectiveCommand:
        try:
            return effective_launch_arguments(
                game.game_id, self._home, document=self._document
            )
        except FaugusConfigError:
            return EffectiveCommand("", "")

    def write_block(self, game: InstalledFaugusGame) -> str:
        if not game.ready:
            return f"{game.display_name} has no executable for Faugus to launch."
        return ""

    def write_command(
        self, game: InstalledFaugusGame, command: str | None
    ) -> CommandWrite:
        result = write_launch_arguments(game.game_id, command, self._home)
        if result.ok:
            # The cached parse is now stale, and the row this write refreshes
            # is resolved from it.
            self._document = None
        return result
