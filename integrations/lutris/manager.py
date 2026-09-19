"""Adapt Lutris's library and prefix_command to the shared wrapper manager.

Commands resolve across game, runner and system YAML. Changes apply at the
next launch; Lutris has no live-apply API or account layer."""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.wrapper_manager import (
    CommandWrite,
    EffectiveCommand,
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

    def write_command(self, game: InstalledLutrisGame, command: str | None) -> CommandWrite:
        if game.config_path is None:
            return CommandWrite(False, "", self.write_block(game))
        return write_prefix_command(game.config_path, command or "")
