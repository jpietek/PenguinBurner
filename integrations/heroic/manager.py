"""One place the Heroic tab talks to: library, settings, and game configs.

The write path itself -- injecting the wrapper, remembering what the command
said before, restoring it -- is shared with every other config-file launcher in
integrations/launchers/wrapper_manager.py. What is Heroic's own is where a
game's command lives: the ``wrapperOptions`` rows in its GamesConfig JSON,
inherited from Heroic's global settings when the game sets none.

Like Lutris and unlike Steam, there is no live apply: every change lands in the
game's config for its next launch.
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
    HeroicConfigError,
    effective_wrapper_command,
    read_global_entries,
    write_wrapper_command,
)
from .library import InstalledHeroicGame, read_heroic_games
from .paths import game_config_path, heroic_installed
from .settings import HEROIC_GAME_SETTINGS_STORE

HeroicGameRow = LauncherGameRow

__all__ = ["ApplyResult", "HeroicGameRow", "HeroicIntegrationManager"]


class HeroicIntegrationManager(WrapperManager):
    launcher_id = "heroic"
    display_name = "Heroic"
    source_labels = SOURCE_LABELS

    def __init__(
        self,
        *,
        home: Path | None = None,
        settings_path: str | Path | None = None,
    ):
        super().__init__(HEROIC_GAME_SETTINGS_STORE, settings_path=settings_path)
        self._home = home
        self._global_entries: list[dict] | None = None

    def refresh(self) -> tuple[LauncherGameRow, ...]:
        # Heroic's global wrappers are one file every game without its own
        # falls back to, so it is read once a pass instead of once a game.
        # A settings change never touches it; the next scan picks up a change
        # the user made in Heroic itself.
        self._global_entries = read_global_entries(self._home)
        return super().refresh()

    def _globals(self) -> list[dict]:
        if self._global_entries is None:
            self._global_entries = read_global_entries(self._home)
        return self._global_entries

    @property
    def available(self) -> bool:
        return heroic_installed(self._home)

    def read_games(self) -> tuple[InstalledHeroicGame, ...]:
        return read_heroic_games(self._home)

    def read_effective(self, game: InstalledHeroicGame) -> EffectiveCommand:
        return self._resolve(game, game_level=True)

    def read_inherited(self, game: InstalledHeroicGame) -> str:
        return self._resolve(game, game_level=False).value

    def _resolve(
        self, game: InstalledHeroicGame, *, game_level: bool
    ) -> EffectiveCommand:
        try:
            return effective_wrapper_command(
                game.game_id,
                self._home,
                game_level=game_level,
                global_entries=self._globals(),
            )
        except HeroicConfigError:
            # A malformed config must not take the whole list down; the row
            # still renders and the write path reports the real error.
            return EffectiveCommand("", "")

    def write_block(self, game: InstalledHeroicGame) -> str:
        if game_config_path(game.game_id, self._home) is None:
            return f"{game.display_name} has no Heroic configuration to write."
        return ""

    def write_command(self, game: InstalledHeroicGame, command: str) -> CommandWrite:
        try:
            return write_wrapper_command(
                game.game_id, command, self._home, global_entries=self._globals()
            )
        except HeroicConfigError as error:
            return CommandWrite(False, "", str(error))

    #: The Heroic tab calls the field by the name Heroic's settings page gives it.
    set_game_wrapper_command = WrapperManager.set_game_command
