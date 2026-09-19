"""Adapt Faugus's library and launch_arguments to the shared wrapper manager.

Faugus has one level -- the game's own entry -- so nothing is ever inherited
and a disable simply clears the field back to empty. Changes apply at the next
launch; Faugus reads games.json when it starts a game."""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.wrapper_manager import (
    CommandWrite,
    EffectiveCommand,
    LauncherGameRow,
    WrapperManager,
)

from .config_store import (
    SOURCE_LABELS,
    FaugusConfigError,
    effective_launch_arguments,
    read_games_document,
    write_launch_arguments,
)
from .library import InstalledFaugusGame, read_faugus_games
from .paths import faugus_installed
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
        self._document: list[dict] | None = None

    def refresh(self) -> tuple[LauncherGameRow, ...]:
        # The whole library is one file, so a scan parses it once here instead
        # of once per game while resolving commands below.
        try:
            self._document = read_games_document(self._home)
        except FaugusConfigError:
            # A malformed library must not take the tab down; the write path
            # reports the real error when the user tries to change something.
            self._document = []
        return super().refresh()

    @property
    def available(self) -> bool:
        return faugus_installed(self._home)

    def read_games(self) -> tuple[InstalledFaugusGame, ...]:
        return read_faugus_games(self._home)

    def read_effective(self, game: InstalledFaugusGame) -> EffectiveCommand:
        try:
            return effective_launch_arguments(
                game.game_id, self._home, document=self._document
            )
        except FaugusConfigError:
            return EffectiveCommand("", "")

    def read_inherited(self, game: InstalledFaugusGame) -> str:
        """Nothing to inherit: Faugus has no global launch arguments."""
        del game
        return ""

    def write_block(self, game: InstalledFaugusGame) -> str:
        if not game.ready:
            return f"{game.display_name} has no executable for Faugus to launch."
        return ""

    def write_command(
        self, game: InstalledFaugusGame, command: str | None
    ) -> CommandWrite:
        try:
            result = write_launch_arguments(game.game_id, command, self._home)
        except FaugusConfigError as error:
            return CommandWrite(False, "", str(error))
        if result.ok:
            # The cached parse is now stale, and the row this write refreshes
            # is resolved from it.
            self._document = None
        return result
