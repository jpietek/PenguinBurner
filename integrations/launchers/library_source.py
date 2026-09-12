"""The library-tab face of a launcher whose games we wrap through a config file.

Lutris and Heroic answer the tab identically -- the same rows, the same write
readiness, the same two library-wide actions, the same one editable command
field -- because the shared write path underneath them already made their
managers the same shape. What is left per launcher is small and genuinely its
own: its icon, what its command field is called, whether the overlay can reach
a given game, and how it starts and stops one.

Steam keeps its own adapter. It applies settings to a running game, needs an
account layer, and its write readiness depends on a client that may be holding
the file -- none of which this shape can express.
"""

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
)
from .wrapper_manager import LauncherGameRow, WrapperManager


class WrapperLibrarySource:
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

    #: The one field only this launcher has: where its wrapper command lives.
    command_field_key = "command"
    command_field_title = "Command"
    command_field_setter = "set_game_command"
    #: Named as the launcher's own UI names it, with and without inheritance.
    command_field_subtitle = ""
    command_field_inherited_subtitle = "{name} — inherited from {source}"
    #: How the library-wide confirmations refer to it.
    command_noun = "launch command"

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
        return (
            LauncherField(
                key=self.command_field_key,
                kind=FIELD_TEXT,
                title=self.command_field_title,
                subtitle=subtitle,
                setter=self.command_field_setter,
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
                    f"{self.command_noun}."
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
                    f"restore their own {self.command_noun}?"
                ),
            ),
        )

    def after_setting_write(self, game_id: str, setter: str) -> None:
        """These launchers pick a change up on the next launch, not live."""
        del game_id, setter

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
            playtime_hours=float(game.playtime_hours or 0.0),
            art_path=game.art_path,
            ready=bool(game.ready),
            wrapped=bool(row.wrapped),
            enabled=bool(row.setting.enabled),
            overlay=bool(row.setting.overlay),
            detail=row,
            overlay_supported=supported,
            overlay_unsupported_reason=reason,
        )
