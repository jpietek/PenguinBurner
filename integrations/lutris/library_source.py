"""Lutris seen through the launcher contract the game library tab speaks.

A thin read-only face over LutrisIntegrationManager, which stays the only thing
that edits a game's prefix_command. Everything shared with the other
config-file launchers lives in integrations/launchers/library_source.py.
"""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.library_source import WrapperLibrarySource
from integrations.launchers.wrapper_manager import LauncherGameRow
from overlay.render_api import overlay_support

from .config_store import LutrisConfigError, read_game_config
from .manager import LutrisIntegrationManager
from .process import (
    launch_lutris_game,
    lutris_available,
    running_lutris_games,
    stop_lutris_game,
)


class LutrisLibrarySource(WrapperLibrarySource):
    launcher_id = "lutris"
    display_name = "Lutris"
    icon_asset = "tab-lutris.png"
    #: Lutris's freedesktop application id, which is also its icon filename.
    desktop_icon_names = ("net.lutris.Lutris",)

    command_field_key = "prefix_command"
    command_field_setter = "set_game_prefix_command"
    command_field_subtitle = "prefix_command in the Lutris config"
    command_noun = "launch command"

    def build_manager(self, *, home, settings_path) -> LutrisIntegrationManager:
        return LutrisIntegrationManager(home=home, settings_path=settings_path)

    def probe_can_launch(self) -> bool:
        return lutris_available()

    def overlay_capability(self, row: LauncherGameRow) -> tuple[bool, str]:
        game = row.game
        # Wine, Proton, and unknown runners are not native ELF programs.
        if game.runner_label.lower() != "linux":
            return True, ""
        executable = None
        if game.config_path is not None:
            try:
                document = read_game_config(game.config_path)
            except LutrisConfigError:
                return True, ""
            section = document.get("game")
            if isinstance(section, dict) and section.get("exe"):
                executable = Path(str(section["exe"]))
                if not executable.is_absolute():
                    executable = Path(game.directory or "") / executable
        return overlay_support(
            translated_to_vulkan=False,
            executable=executable,
            directory=game.directory or None,
        )

    # -- launching -------------------------------------------------------------
    #
    # Through Lutris's own CLI, which starts the game from its stored config --
    # so the prefix_command PenguinBurner wrote is already in the line Lutris
    # builds. Nothing here re-implements a launch.

    def launch(self, game_id: str) -> tuple[bool, str]:
        """Ask Lutris to start a game. Returns (started, what to tell the user)."""
        if not self.can_launch:
            return False, "FAILED to launch (lutris not on PATH)"
        if launch_lutris_game(game_id):
            return True, "launching via Lutris…"
        return False, "FAILED to launch (lutris would not start the game)"

    def stop(self, game_id: str) -> tuple[bool, str]:
        """Signal the game's Lutris wrapper, which takes its tree down."""
        titles = self._titles_by_id()
        # .values(): the probe matches command lines against game *names*, and
        # iterating the mapping itself would hand it the ids instead -- which
        # matches nothing, and reads back as "this game is not running".
        running = running_lutris_games(titles.values())
        if running is None:
            return False, "FAILED to stop (could not tell what is running)"
        title = titles.get(str(game_id))
        pids: tuple[int, ...] = running.get(title, ()) if title else ()
        pid = pids[0] if pids else None
        if pid is None:
            return False, "FAILED to stop (no running session for this game)"
        # The wrapper command line carries only the title, and a title is not
        # unique: two entries can spell the same name, and two sessions of one
        # can run. When the signal could land on the wrong game's wrapper,
        # refuse rather than guess.
        if len(pids) > 1 or sum(1 for value in titles.values() if value == title) > 1:
            return False, (
                "FAILED to stop (more than one session or entry shares this "
                "game's name; stop it from Lutris)"
            )
        if stop_lutris_game(pid):
            return True, "stopping…"
        return False, "FAILED to stop (the wrapper would not take the signal)"

    def running_game_ids(self) -> frozenset[str] | None:
        """Which of this launcher's games are running, or None if unknowable.

        None is not "nothing is running": it means the check itself failed, and
        the caller must hold every state rather than read a stalled probe as
        every game having exited.

        Matched by title rather than by the key we inject, so a game the user
        started with PenguinBurner switched off is still seen running.
        """
        titles = self._titles_by_id()
        running = running_lutris_games(titles.values())
        if running is None:
            return None
        return frozenset(
            game_id for game_id, title in titles.items() if title in running
        )

    def _titles_by_id(self) -> dict[str, str]:
        """The names Lutris will have put on each game's wrapper command line."""
        return {
            str(row.game.game_id): str(row.game.display_name or "")
            for row in self._rows
        }
