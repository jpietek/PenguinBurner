"""Adapt Heroic's library and wrapperOptions to the shared wrapper manager.

Game-level rows override global defaults; changes apply at the next launch."""

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
    HeroicConfigError,
    effective_wrapper_command,
    read_global_entries,
    write_wrapper_command,
)
from .library import InstalledHeroicGame, read_heroic_games
from .paths import game_config_path, heroic_installed
from .settings import HEROIC_GAME_SETTINGS_STORE
from .flatpak import ensure_integration, sandbox_command, uses_flatpak


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
                global_entries=self._global_entries,
            )
        except HeroicConfigError:
            # A malformed config must not take the whole list down; the row
            # still renders and the write path reports the real error.
            return EffectiveCommand("", "")

    def write_block(self, game: InstalledHeroicGame) -> str:
        if game_config_path(game.game_id, self._home) is None:
            return f"{game.display_name} has no Heroic configuration to write."
        return ""

    def write_command(self, game: InstalledHeroicGame, command: str | None) -> CommandWrite:
        try:
            if command and uses_flatpak(self._home):
                command = sandbox_command(command, self._home)
            return write_wrapper_command(
                game.game_id, command, self._home, global_entries=self._global_entries
            )
        except (HeroicConfigError, ValueError) as error:
            return CommandWrite(False, "", str(error))

    def _ensure_wrapper_installed(self) -> str:
        if not uses_flatpak(self._home):
            return super()._ensure_wrapper_installed()
        try:
            ensure_integration(self._home)
        except (OSError, RuntimeError) as error:
            return str(error)
        return ""

    def _describe(self, game, setting) -> str:
        description = super()._describe(game, setting)
        if setting.enabled:
            description += " Play in Game Library refreshes Heroic's saved launch settings."
        return description
