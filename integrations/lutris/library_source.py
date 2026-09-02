"""Lutris seen through the launcher contract the game library tab speaks.

A thin read-only face over LutrisIntegrationManager, which stays the only thing
that edits a game's prefix_command.
"""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.library import (
    FIELD_SWITCH,
    FIELD_TEXT,
    GROUP_COMMAND,
    GROUP_IN_GAME,
    LauncherBulkAction,
    LauncherField,
    LauncherWriteState,
    LibraryGame,
)

from overlay.render_api import overlay_support

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
        # Overlay-capability is a per-game disk read (a native game's binary
        # is inspected once for how it presents); cache it so a ten-second
        # rescan does not re-scan every game. Keyed by game id + the exe it was
        # computed for, so a game repointed at another binary is re-checked.
        self._overlay_support: dict[str, tuple[str, bool, str]] = {}

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
        # Installing or removing Lutris while the tab is open should change
        # the Play button on the next scan, not on the next app start.
        self.can_launch = lutris_available()

    # -- what only Lutris has ------------------------------------------------

    def fields(self, game: LibraryGame) -> tuple[LauncherField, ...]:
        row = game.detail
        if not isinstance(row, LutrisGameRow):
            return ()
        return (
            self._latency_field(row, game.overlay_supported),
            self._prefix_command_field(row),
        )

    @staticmethod
    def _latency_field(
        row: LutrisGameRow, overlay_supported: bool = True
    ) -> LauncherField:
        """The markers Adaptive paces on, kept when the overlay is off.

        Declared only here, but not because the wrapper treats Lutris
        specially: it reads PB_INGAME_LATENCY straight out of the environment,
        whoever put it there. Steam's manager simply has no setter for it yet,
        and adding one is not a copy of this line -- Steam's tokens land where
        %command% was, where an `env VAR=1` assignment breaks a
        `gamescope -- %command%` launch. That is why the overlay opt-in rides
        as --pb-overlay=N for Steam, and the latency one would need the same.
        """
        overlay_on = bool(getattr(row.setting, "overlay", False))
        # The markers come from the same Vulkan layer as the overlay, so a game
        # the overlay cannot attach to cannot produce them either: show the
        # switch off and out of reach, with the reason on hover.
        if not overlay_supported:
            return LauncherField(
                key="ingame_latency",
                kind=FIELD_SWITCH,
                title="Latency markers without the overlay",
                subtitle="Unavailable: this game does not render through Vulkan.",
                setter="set_game_ingame_latency",
                value=False,
                enabled=False,
                group=GROUP_IN_GAME,
            )
        return LauncherField(
            key="ingame_latency",
            kind=FIELD_SWITCH,
            title="Latency markers without the overlay",
            subtitle=(
                "Adaptive paces on these markers, and the launcher normally "
                "starts them with the overlay. Keep them when the overlay is "
                "off, or Adaptive loses base-frame pacing under frame generation."
            ),
            setter="set_game_ingame_latency",
            # With the overlay on the launcher already runs the markers, so the
            # switch has nothing left to add: show it on, out of reach.
            value=True if overlay_on else bool(
                getattr(row.setting, "ingame_latency", False)
            ),
            enabled=not overlay_on,
            group=GROUP_IN_GAME,
        )

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

    def games(self) -> tuple[LibraryGame, ...]:
        return tuple(self._library_game(row) for row in self._rows)

    def _library_game(self, row: LutrisGameRow) -> LibraryGame:
        supported, reason = self._overlay_capability(row)
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
            overlay_supported=supported,
            overlay_unsupported_reason=reason,
            detail=row,
        )

    def _overlay_capability(self, row: LutrisGameRow) -> tuple[bool, str]:
        """Whether the Vulkan overlay can attach to this game, cached per exe.

        Wine/Proton games translate to Vulkan and never touch the disk. A
        native game's own binary is inspected once for how it presents.
        """
        game = row.game
        directory = "" if game.is_wine else str(game.directory or "")
        exe = "" if game.is_wine else self._native_executable(game)
        key = f"{directory}\0{exe}"
        cached = self._overlay_support.get(game.game_id)
        if cached is not None and cached[0] == key:
            return cached[1], cached[2]
        supported, reason = overlay_support(
            translated_to_vulkan=game.is_wine,
            executable=exe or None,
            directory=directory or None,
        )
        self._overlay_support[game.game_id] = (key, supported, reason)
        return supported, reason

    def _native_executable(self, game) -> str:
        """The binary a native game launches, from its Lutris config.

        ``game.exe`` in the config, resolved against the install directory when
        it is relative. Empty when the game has no config or names no exe --
        the caller then leaves the overlay enabled rather than guess.
        """
        if game.config_path is None:
            return ""
        try:
            document = read_game_config(game.config_path)
        except LutrisConfigError:
            return ""
        exe = str((document.get("game") or {}).get("exe") or "").strip()
        if not exe:
            return ""
        path = Path(exe)
        if not path.is_absolute() and game.directory:
            path = Path(game.directory) / path
        return str(path)

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
