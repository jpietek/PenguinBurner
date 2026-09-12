"""One place the Lutris tab talks to: library, settings, and game configs.

The write path itself -- injecting the wrapper, remembering what the command
said before, restoring it -- is shared with every other config-file launcher
in integrations/launchers/wrapper_manager.py. What is Lutris's own is where a
game's command lives: a ``system.prefix_command`` resolved across the game,
runner and system YAML levels.

Deliberately narrower than the Steam manager. Lutris exposes no CDP or DBus
API, so there is no live apply and no account layer; every change lands in the
game's YAML for its next launch.
"""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.wrapper_manager import (
    ApplyResult,
    CommandWrite,
    EffectiveCommand,
    LauncherGameRow,
    WrapperManager,
)

from .config_store import (
    SOURCE_LABELS,
    LutrisConfigError,
    effective_prefix_command,
    write_prefix_command,
)
from .library import InstalledLutrisGame, read_lutris_games
from .paths import lutris_installed, runner_config_path, system_config_path
from .settings import LUTRIS_GAME_SETTINGS_STORE

#: The Lutris tab reads rows through the shared shape; the alias keeps the
#: launcher's own vocabulary at its own boundary.
LutrisGameRow = LauncherGameRow

__all__ = ["ApplyResult", "LutrisGameRow", "LutrisIntegrationManager"]


class LutrisIntegrationManager(WrapperManager):
    launcher_id = "lutris"
    display_name = "Lutris"
    source_labels = SOURCE_LABELS

    def __init__(
        self,
        *,
        home: Path | None = None,
        settings_path: str | Path | None = None,
    ):
        super().__init__(LUTRIS_GAME_SETTINGS_STORE, settings_path=settings_path)
        self._home = home

    @property
    def available(self) -> bool:
        return lutris_installed(self._home)

    def read_games(self) -> tuple[InstalledLutrisGame, ...]:
        return read_lutris_games(self._home)

    def read_effective(self, game: InstalledLutrisGame) -> EffectiveCommand:
        """What the game really launches with, across all three config levels."""
        return self._resolve(game, game_config=game.config_path)

    def read_inherited(self, game: InstalledLutrisGame) -> str:
        """What this game would launch with if its own level said nothing."""
        return self._resolve(game, game_config=None).value

    def _resolve(
        self,
        game: InstalledLutrisGame,
        *,
        game_config: Path | None,
    ) -> EffectiveCommand:
        try:
            return effective_prefix_command(
                game_config=game_config,
                runner_config=runner_config_path(game.runner, self._home),
                system_config=system_config_path(self._home),
            )
        except LutrisConfigError:
            # A malformed config must not take the whole list down; the row
            # still renders and the write path reports the real error.
            return EffectiveCommand("", "")

    def write_block(self, game: InstalledLutrisGame) -> str:
        if game.config_path is None:
            return f"{game.display_name} has no Lutris configuration file to write."
        return ""

    def write_command(self, game: InstalledLutrisGame, command: str) -> CommandWrite:
        if game.config_path is None:
            return CommandWrite(False, "", self.write_block(game))
        return write_prefix_command(game.config_path, command)

    #: The Lutris tab still calls the field by the name Lutris gives it.
    set_game_prefix_command = WrapperManager.set_game_command
