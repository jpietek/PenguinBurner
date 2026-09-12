"""Heroic seen through the launcher contract the game library tab speaks.

A thin read-only face over HeroicIntegrationManager, which stays the only thing
that edits a game's wrapperOptions. Everything shared with the other
config-file launchers lives in integrations/launchers/library_source.py.
"""

from __future__ import annotations

from integrations.launchers.library_source import WrapperLibrarySource
from integrations.launchers.wrapper_manager import LauncherGameRow
from overlay.render_api import overlay_support

from .manager import HeroicIntegrationManager
from .process import (
    heroic_available,
    launch_heroic_game,
    running_heroic_games,
    stop_heroic_game,
)


class HeroicLibrarySource(WrapperLibrarySource):
    launcher_id = "heroic"
    display_name = "Heroic"
    icon_asset = "tab-heroic.png"
    #: The distro builds ship a plain "heroic", the Flatpak its application id.
    desktop_icon_names = ("heroic", "com.heroicgameslauncher.hgl")

    command_field_key = "wrapper_command"
    command_field_setter = "set_game_wrapper_command"
    command_field_subtitle = "Wrapper command in the Heroic settings"
    command_field_inherited_subtitle = "Wrapper command — inherited from {source}"
    command_noun = "wrapper command"

    def build_manager(self, *, home, settings_path) -> HeroicIntegrationManager:
        return HeroicIntegrationManager(home=home, settings_path=settings_path)

    def probe_can_launch(self) -> bool:
        return heroic_available()

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

    # -- launching -------------------------------------------------------------

    def launch(self, game_id: str) -> tuple[bool, str]:
        """Ask Heroic to start a game. Returns (started, what to tell the user)."""
        if not self.can_launch:
            return False, "FAILED to launch (heroic not on PATH)"
        row = self.manager.row(game_id)
        if row is None:
            return False, "FAILED to launch (no such game in the Heroic library)"
        if launch_heroic_game(row.game.runner, row.game.game_id):
            return True, "launching via Heroic…"
        return False, "FAILED to launch (heroic would not start the game)"

    def stop(self, game_id: str) -> tuple[bool, str]:
        """Signal the game's PenguinBurner wrapper, which is the session itself."""
        running = running_heroic_games()
        if running is None:
            return False, "FAILED to stop (could not tell what is running)"
        pids = running.get(str(game_id), ())
        if not pids:
            return False, "FAILED to stop (no running session for this game)"
        if stop_heroic_game(pids[0]):
            return True, "stopping…"
        return False, "FAILED to stop (the wrapper would not take the signal)"

    def running_game_ids(self) -> frozenset[str] | None:
        """Which of this launcher's games are running, or None if unknowable.

        Only games PenguinBurner wraps can be seen: their command line carries
        the identity flag we wrote. Heroic starts a game through a tree of its
        own with no name we could match on, so a game it starts untouched is
        invisible here -- which is also a game this tab has nothing to say
        about.
        """
        running = running_heroic_games()
        return None if running is None else frozenset(running)
