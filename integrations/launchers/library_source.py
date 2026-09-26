"""Library adapter shared by config-file launchers.

Subclasses supply launcher labels, renderer support and process control.
Steam keeps its own adapter for accounts, live apply and client write locking."""

from __future__ import annotations

from pathlib import Path

from .desktop_icons import desktop_icon
from .library import (
    FIELD_TEXT,
    GROUP_COMMAND,
    LauncherBulkAction,
    LauncherField,
    LauncherWriteState,
    LibraryGame,
    library_bulk_actions,
)
from .live_overlay import LiveOverlaySource
from .wrapped_sessions import LauncherSessions, stop_wrapped_session
from .wrapper_manager import LauncherGameRow, WrapperManager


class WrapperLibrarySource(LiveOverlaySource):
    launcher_id = ""
    display_name = ""
    #: Shipped fallback, used when the machine has no icon of the launcher's own.
    icon_asset = ""
    #: What the launcher's installed icon is called, best spelling first.
    desktop_icon_names: tuple[str, ...] = ()
    #: Probed during refresh, off the GUI thread: a machine can hold the
    #: configuration of a launcher that is no longer installed, and those games
    #: are still worth listing and configuring -- just not startable. Inside a
    #: Flatpak the probe is a flatpak-spawn round-trip, which is also why it
    #: cannot live in the constructor the window builds tabs with.
    can_launch = False
    _running_pids: tuple[int, ...] = ()
    _external_games: frozenset[str] = frozenset()

    #: The launcher's wrapper command field.
    command_field_key = "command"
    #: Named as the launcher's own UI names it, with and without inheritance.
    command_field_subtitle = ""
    command_field_inherited_subtitle = "{name} — inherited from {source}"

    def __init__(
        self,
        manager: WrapperManager | None = None,
        *,
        home: Path | None = None,
        settings_path: str | Path | None = None,
    ) -> None:
        self.manager = manager or self.build_manager(
            home=home, settings_path=settings_path
        )
        self._home = home
        self._rows: tuple[LauncherGameRow, ...] = ()
        self._overlay_support: dict[str, tuple[bool, str]] = {}

    # -- what a launcher must answer -----------------------------------------

    def build_manager(
        self,
        *,
        home: Path | None,
        settings_path: str | Path | None,
    ) -> WrapperManager:
        raise NotImplementedError

    def probe_can_launch(self) -> bool:
        """Whether this launcher can start a game for us, asked afresh."""
        return False

    def overlay_capability(self, row: LauncherGameRow) -> tuple[bool, str]:
        """Whether the overlay's renderer can draw in this game."""
        del row
        return True, ""

    # -- the tab's questions --------------------------------------------------

    def desktop_icon(self, *, data_dirs=None):
        """The launcher's own installed icon, or None when it has none here.

        Preferred over the shipped glyph: it is the mark the user recognises,
        it appears exactly when that launcher is present, and nobody's artwork
        is redistributed.
        """
        for name in self.desktop_icon_names:
            found = desktop_icon(name, self._home, data_dirs=data_dirs)
            if found is not None:
                return found
        return None

    def available(self) -> bool:
        return bool(self.manager.available)

    def refresh(self, *, deep: bool = True) -> None:
        self.manager.refresh()
        if deep and self.manager.compatibility is not None:
            self.manager.compatibility.refresh()
        self._rows = tuple(self.manager.rows())
        # Renderer inspection belongs on the scan worker, never in games() or
        # selection handling: it walks a game's install directory. A deep
        # rescan picks up renderer changes; the cheap tick reuses the answer.
        self._overlay_support = {
            row.game.game_id: (
                self.overlay_capability(row)
                if deep or row.game.game_id not in self._overlay_support
                else self._overlay_support[row.game.game_id]
            )
            for row in self._rows
        }
        # Installing or removing the launcher while the tab is open should
        # change the Play button on the next scan, not on the next app start.
        self.can_launch = self.probe_can_launch()

    def fields(self, game: LibraryGame) -> tuple[LauncherField, ...]:
        row = game.detail
        if not isinstance(row, LauncherGameRow):
            return ()
        value = str(row.command or "")
        subtitle = self.command_field_subtitle
        if value and row.inherited:
            subtitle = self.command_field_inherited_subtitle.format(
                name=self.command_field_key, source=row.source_label
            )
        compatibility = self.manager.compatibility
        return ((compatibility.field(row.game),) if compatibility is not None else ()) + (
            LauncherField(
                key=self.command_field_key,
                kind=FIELD_TEXT,
                title="Command",
                subtitle=subtitle,
                setter="set_game_command",
                value=value,
                group=GROUP_COMMAND,
            ),
        )

    def write_state(self) -> LauncherWriteState:
        """Always ready: these settings are files PenguinBurner owns the writing
        of. Nothing to initialise and nothing racing us for them, unlike Steam,
        which holds its launch options in memory while the client runs."""
        return LauncherWriteState()

    def bulk_actions(self) -> tuple[LauncherBulkAction, ...]:
        return library_bulk_actions()

    def saved_overlay(self, game_id: str) -> bool:
        row = self.manager.row(game_id)
        if row is None:
            raise ValueError("Unknown game.")
        return row.setting.overlay

    def after_setting_write(self, game_id: str, setter: str) -> object | None:
        """Launchers can deliver supported changes to an existing session."""
        if setter not in ("set_game_target_fps", "set_game_mode"):
            return super().after_setting_write(game_id, setter)
        from .runtime_profile import hot_reapply_game_profile

        row = self.manager.row(game_id)
        if row is None:
            return None
        return hot_reapply_game_profile(
            f"{self.launcher_id}:{game_id}", row.setting, mode_change=setter == "set_game_mode",
        )

    def games(self) -> tuple[LibraryGame, ...]:
        return tuple(self._library_game(row) for row in self._rows)

    def _library_game(self, row: LauncherGameRow) -> LibraryGame:
        game = row.game
        supported, reason = self._overlay_support.get(game.game_id, (True, ""))
        return LibraryGame(
            launcher=self.launcher_id,
            game_id=game.game_id,
            name=game.display_name,
            subtitle=str(game.runner_label or ""),
            last_played=int(game.last_played or 0),
            installed_at=int(getattr(game, "installed_at", 0) or 0),
            playtime_hours=float(game.playtime_hours or 0.0),
            art_path=game.art_path,
            ready=bool(game.ready),
            wrapped=bool(row.wrapped),
            enabled=bool(row.setting.enabled),
            overlay=bool(row.setting.overlay),
            detail=row,
            overlay_supported=supported,
            overlay_unsupported_reason=reason,
            executable=str(getattr(game, "executable", "") or ""),
        )

    def launch(self, game_id: str) -> tuple[bool, str]:
        """Validate the library entry, then let its launcher start the game."""
        if not self.can_launch:
            return False, f"FAILED to launch ({self.display_name} is not installed or could not be found)"
        row = self.manager.row(game_id)
        if row is None:
            return False, f"FAILED to launch (no such game in the {self.display_name} library)"
        if self._launch_game(row):
            return True, f"launching via {self.display_name}…"
        return False, f"FAILED to launch ({self.display_name} would not start the game)"

    def _launch_game(self, row: LauncherGameRow) -> bool:
        raise NotImplementedError

    def probe_sessions(self, *, known_pids: tuple[int, ...] = ()) -> LauncherSessions | None:
        raise NotImplementedError

    def stop(self, game_id: str) -> tuple[bool, str]:
        """Signal the wrapped game's surviving session members."""
        running = self._running_sessions()
        if running is None:
            return False, "FAILED to stop (could not tell what is running)"
        pids = running.wrapped.get(str(game_id), ())
        if not pids:
            return False, "FAILED to stop (no running session for this game)"
        # Handoffs can leave several identified successors. Stop every wrapped
        # member, never an external launcher process or detached PB helper.
        stopped = [stop_wrapped_session(pid, f"{self.launcher_id}:{game_id}") for pid in pids]
        if all(stopped):
            return True, "stopping…"
        return False, "FAILED to stop (the wrapper would not take the signal)"

    def running_game_ids(self) -> frozenset[str] | None:
        """Which of this launcher's games are running, or None if unknowable.

        The launcher's own children remain observable without our wrapper.
        They are kept separate from the wrapper sessions Stop can control.
        """
        running = self._running_sessions()
        return None if running is None else frozenset(running.wrapped) | frozenset(running.external)

    def external_game_ids(self) -> frozenset[str]:
        """Observed games that must be closed in the launcher, from the latest poll."""
        return self._external_games

    def observed_processes(self) -> dict[str, tuple[int, ...]] | None:
        sessions = self._running_sessions()
        return None if sessions is None else sessions.external

    def _running_sessions(self) -> LauncherSessions | None:
        running = self.probe_sessions(known_pids=self._running_pids)
        if running is not None:
            self._running_pids = tuple(
                pid for group in (running.wrapped, running.external)
                for pids in group.values() for pid in pids
            )
            self._external_games = frozenset(running.external) - frozenset(running.wrapped)
        return running
