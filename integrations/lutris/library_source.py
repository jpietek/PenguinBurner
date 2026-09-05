"""Lutris seen through the launcher contract the game library tab speaks.

A thin read-only face over LutrisIntegrationManager, which stays the only thing
that edits a game's prefix_command.
"""

from __future__ import annotations

from pathlib import Path

from overlay.render_api import overlay_support

from integrations.launchers.library import (
    FIELD_TEXT,
    GROUP_COMMAND,
    LauncherBulkAction,
    LauncherField,
    LauncherWriteState,
    LibraryGame,
)

from .config_store import LutrisConfigError, read_game_config
from .manager import LutrisGameRow, LutrisIntegrationManager
from .paths import lutris_desktop_icon
from .process import (
    launch_lutris_game,
    lutris_available,
    running_lutris_games,
    stop_lutris_game,
)


class LutrisLibrarySource:
    launcher_id = "lutris"
    display_name = "Lutris"
    #: Shipped fallback, used when the machine has no Lutris icon of its own.
    icon_asset = "tab-lutris.png"
    #: Probed during refresh, off the GUI thread: a machine can hold a Lutris
    #: database whose Lutris is no longer installed, and those games are still
    #: worth listing and configuring -- just not startable. Inside a Flatpak
    #: the probe is a flatpak-spawn round-trip, which is also why it cannot
    #: live in the constructor the window builds tabs with.
    can_launch = False

    def __init__(
        self,
        manager: LutrisIntegrationManager | None = None,
        *,
        home: Path | None = None,
        settings_path: str | Path | None = None,
    ) -> None:
        self.manager = manager or LutrisIntegrationManager(
            home=home,
            settings_path=settings_path,
        )
        self._home = home
        self._rows: tuple[LutrisGameRow, ...] = ()
        self._overlay_support: dict[str, tuple[bool, str]] = {}

    def desktop_icon(self):
        """Lutris's own installed icon, or None when it has none here."""
        return lutris_desktop_icon(self._home)

    def available(self) -> bool:
        return bool(self.manager.available)

    def refresh(self, *, deep: bool = True) -> None:
        # Lutris keeps everything in a local database and config files, so
        # there is no expensive pass to skip: the cheap one is the only one.
        self.manager.refresh()
        self._rows = tuple(self.manager.rows())
        # Renderer inspection belongs on the scan worker, never in games()
        # or selection handling. A deep rescan picks up renderer changes.
        self._overlay_support = {
            row.game.game_id: (
                self._overlay_capability(row)
                if deep or row.game.game_id not in self._overlay_support
                else self._overlay_support[row.game.game_id]
            )
            for row in self._rows
        }
        # Installing or removing Lutris while the tab is open should change
        # the Play button on the next scan, not on the next app start.
        self.can_launch = lutris_available()

    # -- what only Lutris has ------------------------------------------------

    def fields(self, game: LibraryGame) -> tuple[LauncherField, ...]:
        row = game.detail
        if not isinstance(row, LutrisGameRow):
            return ()
        return (self._prefix_command_field(row),)

    @staticmethod
    def _prefix_command_field(row: LutrisGameRow) -> LauncherField:
        value = str(row.prefix_command or "")
        subtitle = "prefix_command in the Lutris config"
        if value and row.inherited_prefix:
            subtitle = f"prefix_command — inherited from {row.prefix_source_label}"
        return LauncherField(
            key="prefix_command",
            kind=FIELD_TEXT,
            title="Command",
            subtitle=subtitle,
            setter="set_game_prefix_command",
            value=value,
            group=GROUP_COMMAND,
        )

    def write_state(self) -> LauncherWriteState:
        """Always ready: Lutris's settings are files we own the writing of.

        Nothing to initialise and nothing racing us for them, unlike Steam,
        which holds its launch options in memory while the client runs.
        """
        return LauncherWriteState()

    def bulk_actions(self) -> tuple[LauncherBulkAction, ...]:
        # Keys shared with the other launchers on purpose: the tab shows one
        # "disable everything" and means it across the whole library.
        return (
            LauncherBulkAction(
                key="enable_all",
                label="Enable PenguinBurner for all games",
                setter="set_all_games_enabled",
                value=True,
                affects="enabled",
                confirm=(
                    "Add the PenguinBurner wrapper to the launch command of "
                    "{count} {games}?\n\nThe In-Game overlay stays off, and "
                    "MangoHud is disabled in wrapped games. \"Disable "
                    "PenguinBurner for all games\" restores each game's own "
                    "launch command."
                ),
            ),
            LauncherBulkAction(
                key="disable_all",
                label="Disable PenguinBurner for all games",
                setter="set_all_games_enabled",
                value=False,
                affects="enabled",
                confirm=(
                    "Remove the PenguinBurner wrapper from {count} {games} and "
                    "restore their own launch command?"
                ),
            ),
        )

    def after_setting_write(self, game_id: str, setter: str) -> None:
        """Lutris changes are picked up on the next launch, not live."""
        del game_id, setter
        return None

    def games(self) -> tuple[LibraryGame, ...]:
        return tuple(self._library_game(row) for row in self._rows)

    def _library_game(self, row: LutrisGameRow) -> LibraryGame:
        supported, reason = self._overlay_support.get(row.game.game_id, (True, ""))
        return LibraryGame(
            launcher=self.launcher_id,
            game_id=row.game.game_id,
            name=row.game.display_name,
            subtitle=str(row.game.runner_label or ""),
            last_played=int(row.game.last_played or 0),
            playtime_hours=float(row.game.playtime_hours or 0.0),
            art_path=row.game.cover_path,
            ready=bool(row.game.ready),
            wrapped=bool(row.wrapped),
            enabled=bool(row.setting.enabled),
            overlay=bool(row.setting.overlay),
            detail=row,
            overlay_supported=supported,
            overlay_unsupported_reason=reason,
        )

    @staticmethod
    def _overlay_capability(row: LutrisGameRow) -> tuple[bool, str]:
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
