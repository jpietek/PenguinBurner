"""Faugus's library adapter; settings writes belong to FaugusIntegrationManager."""

from __future__ import annotations

from integrations.launchers.library_source import WrapperLibrarySource
from integrations.launchers.wrapper_manager import LauncherGameRow
from overlay.render_api import overlay_support

from .manager import FaugusIntegrationManager
from .process import (
    faugus_available,
    launch_faugus_game,
    probe_faugus_sessions,
)


class FaugusLibrarySource(WrapperLibrarySource):
    launcher_id = "faugus"
    display_name = "Faugus"
    icon_asset = "tab-faugus.png"
    #: The distro builds ship a plain "faugus-launcher", the Flatpak its id.
    desktop_icon_names = ("faugus-launcher", "io.github.Faugus.faugus-launcher")

    command_field_key = "launch_arguments"
    command_field_subtitle = "Launch arguments in the Faugus game settings"

    def build_manager(self, *, home, settings_path) -> FaugusIntegrationManager:
        return FaugusIntegrationManager(home=home, settings_path=settings_path)

    def probe_can_launch(self) -> bool:
        return faugus_available(self._home)

    def overlay_capability(self, row: LauncherGameRow) -> tuple[bool, str]:
        game = row.game
        # Faugus exists to run Windows games through Proton, where everything
        # is translated to Vulkan and the overlay always reaches them. Only a
        # Linux-Native entry can be an OpenGL program it cannot draw in.
        if not game.is_native:
            return True, ""
        return overlay_support(
            translated_to_vulkan=False,
            executable=game.executable or None,
            directory=game.install_path or None,
        )

    def _launch_game(self, row: LauncherGameRow) -> bool:
        return launch_faugus_game(row.game.game_id, home=self._home)

    def probe_sessions(self, *, known_pids=()):
        return probe_faugus_sessions(
            known_pids=known_pids,
            executables={row.game.game_id: row.game.executable for row in self._rows
                         if row.game.executable},
        )
