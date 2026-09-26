"""Heroic's library adapter; settings writes belong to HeroicIntegrationManager."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from integrations.launchers.library import LauncherField, LibraryGame
from integrations.launchers.library_source import WrapperLibrarySource
from integrations.launchers.wrapper_manager import LauncherGameRow
from overlay.render_api import overlay_support

from .manager import HeroicIntegrationManager
from .process import (
    heroic_available,
    launch_heroic_game,
    probe_heroic_sessions,
)


class HeroicLibrarySource(WrapperLibrarySource):
    launcher_id = "heroic"
    display_name = "Heroic"
    icon_asset = "tab-heroic.png"
    #: The distro builds ship a plain "heroic", the Flatpak its application id.
    desktop_icon_names = ("heroic", "com.heroicgameslauncher.hgl")

    command_field_key = "wrapper_command"
    command_field_subtitle = "Wrapper command in the Heroic settings"
    command_field_inherited_subtitle = "Wrapper command — inherited from {source}"

    def watch_paths(self) -> tuple[Path, ...]:
        from .paths import (
            heroic_config_root,
            installed_store_paths,
            library_cache_paths,
        )

        root = heroic_config_root(self._home)
        return (*installed_store_paths(self._home), *library_cache_paths(self._home),
                root / "store", root / "GamesConfig", root / "config.json")

    def build_manager(self, *, home, settings_path) -> HeroicIntegrationManager:
        return HeroicIntegrationManager(home=home, settings_path=settings_path)

    def probe_can_launch(self) -> bool:
        return heroic_available(self._home)

    def fields(self, game: LibraryGame) -> tuple[LauncherField, ...]:
        fields = super().fields(game)
        installation = self.manager.installation
        if not game.wrapped or not installation.flatpak:
            return fields
        from pathlib import Path

        wrapper = Path(installation.wrapper)
        row = game.detail
        ready = (
            isinstance(row, LauncherGameRow)
            and str(wrapper) in row.command
            and wrapper.is_file()
        )
        note = (
            "Configured for Flatpak; Play refreshes Heroic's saved launch settings."
            if ready else
            "Flatpak setup required: turn Wrap this game off and on, then use Play."
        )
        return tuple(
            replace(field, subtitle=note) if field.key == self.command_field_key else field
            for field in fields
        )

    def overlay_capability(self, row: LauncherGameRow) -> tuple[bool, str]:
        game = row.game
        # Windows games run under Proton, where everything is translated to
        # Vulkan and the overlay always reaches them. Only a Linux-native
        # build can be an OpenGL program the overlay cannot draw in.
        if not game.is_native:
            return True, ""
        return overlay_support(
            translated_to_vulkan=False,
            executable=None,
            directory=game.install_path or None,
        )

    def _launch_game(self, row: LauncherGameRow) -> bool:
        return launch_heroic_game(row.game.runner, row.game.game_id, home=self._home)

    def probe_sessions(self, *, known_pids=()):
        return probe_heroic_sessions(known_pids=known_pids)
