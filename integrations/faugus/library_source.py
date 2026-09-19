"""Faugus's library adapter; settings writes belong to FaugusIntegrationManager."""

from __future__ import annotations

from integrations.launchers.library_source import WrapperLibrarySource
from integrations.launchers.wrapped_sessions import LauncherSessions
from integrations.launchers.wrapper_manager import LauncherGameRow
from overlay.render_api import overlay_support

from .manager import FaugusIntegrationManager
from .process import (
    faugus_available,
    launch_faugus_game,
    probe_faugus_sessions,
    stop_faugus_game,
)


class FaugusLibrarySource(WrapperLibrarySource):
    launcher_id = "faugus"
    display_name = "Faugus"
    icon_asset = "tab-faugus.png"
    #: The distro builds ship a plain "faugus-launcher", the Flatpak its id.
    desktop_icon_names = ("faugus-launcher", "io.github.Faugus.faugus-launcher")

    command_field_key = "launch_arguments"
    command_field_subtitle = "Launch arguments in the Faugus game settings"
    command_field_inherited_subtitle = "Launch arguments — inherited from {source}"
    command_noun = "launch arguments"
    _running_pids: tuple[int, ...] = ()
    _external_games: frozenset[str] = frozenset()

    def build_manager(self, *, home, settings_path) -> FaugusIntegrationManager:
        return FaugusIntegrationManager(home=home, settings_path=settings_path)

    def probe_can_launch(self) -> bool:
        return faugus_available()

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

    # -- launching -------------------------------------------------------------

    def launch(self, game_id: str) -> tuple[bool, str]:
        """Ask Faugus to start a game. Returns (started, what to tell the user)."""
        if not self.can_launch:
            return False, "FAILED to launch (Faugus Launcher is not installed or could not be found)"
        row = self.manager.row(game_id)
        if row is None:
            return False, "FAILED to launch (no such game in the Faugus library)"
        if launch_faugus_game(row.game.game_id):
            return True, "launching via Faugus Launcher…"
        return False, "FAILED to launch (faugus-launcher would not start the game)"

    def stop(self, game_id: str) -> tuple[bool, str]:
        """Signal the game's PenguinBurner wrapper, which is the session itself."""
        running = self._running_sessions()
        if running is None:
            return False, "FAILED to stop (could not tell what is running)"
        pids = running.wrapped.get(str(game_id), ())
        if not pids:
            return False, "FAILED to stop (no running session for this game)"
        if stop_faugus_game(pids[0]):
            return True, "stopping…"
        return False, "FAILED to stop (the wrapper would not take the signal)"

    def running_game_ids(self) -> frozenset[str] | None:
        """Which of this launcher's games are running, or None if unknowable.

        A game Faugus started without our wrapper stays observable through the
        FAUGUSID it stamps on the whole game tree. Those are kept apart from
        the wrapper sessions Stop can control.
        """
        running = self._running_sessions()
        if running is None:
            return None
        return frozenset(running.wrapped) | frozenset(running.external)

    def external_game_ids(self) -> frozenset[str]:
        """Observed games that must be closed in Faugus, from the latest poll."""
        return self._external_games

    def _running_sessions(self) -> LauncherSessions | None:
        running = probe_faugus_sessions(known_pids=self._running_pids)
        if running is not None:
            self._running_pids = tuple(
                pid
                for group in (running.wrapped, running.external)
                for pids in group.values()
                for pid in pids
            )
            self._external_games = frozenset(running.external)
        return running
