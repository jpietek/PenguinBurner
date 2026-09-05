"""One row of the game library, and the contract a launcher fills to supply it.

`profiles.game_profile.GameProfileSetting` already says what a *preset* means
independently of the launcher that configured it. This says the same for the
*library entry* it belongs to, so one tab can list Steam and Lutris games
together without knowing which is which.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

SORT_ALPHABETICAL = "alphabetical"
SORT_LAUNCHER = "launcher"
SORT_RECENT = "recent"
SORT_PLAYTIME = "playtime"

SORT_MODES = (SORT_ALPHABETICAL, SORT_LAUNCHER, SORT_RECENT, SORT_PLAYTIME)

#: The kinds of control a launcher-specific field can ask the tab to draw.
FIELD_TEXT = "text"
FIELD_MULTILINE = "multiline"
FIELD_CHOICE = "choice"
FIELD_SWITCH = "switch"

#: Headings a field can declare itself into. The tab owns the rows above them
#: -- the wrapper toggle, the tuning rows -- because every launcher has those.
GROUP_IN_GAME = "In game"
GROUP_COMMAND = "Launch command"

#: Whether a launcher will accept a setting right now.
WRITE_READY = "ready"
WRITE_NEEDS_SETUP = "needs-setup"


@dataclass(frozen=True)
class LibraryGame:
    """A game as the library list needs it, whichever launcher owns it."""

    launcher: str
    game_id: str
    name: str
    #: Epoch seconds; 0 when the launcher never recorded a session.
    last_played: int = 0
    #: Hours. Steam reports minutes and Lutris hours, so both are normalised
    #: here rather than at every place that wants to compare them. 0.0 means
    #: "never played or not reported" -- the two are indistinguishable in the
    #: data both launchers hand us, so the sort treats them the same.
    playtime_hours: float = 0.0
    #: The launcher's own one-line facts about this game -- a Proton version,
    #: a wine runner. Supplied by the adapter so the library view never has to
    #: know which launcher it is drawing.
    subtitle: str = ""
    art_path: Path | None = None
    #: Installed and configured enough to be tuned.
    ready: bool = True
    #: The launch command currently goes through our wrapper.
    wrapped: bool = False
    #: The user has switched this game on in PenguinBurner.
    enabled: bool = False
    #: Whether the overlay is on for this game. Every launcher has this one --
    #: the tab draws the row for all of them -- so it belongs here rather than
    #: behind ``detail``, where a library-wide action could not reach it.
    overlay: bool = False
    #: The launcher's own row, handed back untouched so the detail pane can
    #: read the fields only that launcher has.
    detail: object | None = None
    #: Native renderer capability, independent of wrapper and saved settings.
    overlay_supported: bool = True
    overlay_unsupported_reason: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """Identity across launchers: an app id alone is not unique."""
        return (self.launcher, self.game_id)


@dataclass(frozen=True)
class LauncherField:
    """One setting only this launcher has, described rather than drawn.

    The adapter computes the whole descriptor for the selected game and the tab
    renders it. That is the whole reason a third launcher needs no GUI change:
    a compatibility tool, a prefix command and any future launcher-only option
    differ in their label, control and write method -- and in nothing else.

    ``setter`` names a method on the launcher's own manager. Writes keep going
    there; this says which one, it does not do the writing.
    """

    key: str
    kind: str
    title: str
    subtitle: str = ""
    setter: str = ""
    value: object = ""
    #: (data, label) pairs for FIELD_CHOICE, in the order they should appear.
    choices: tuple[tuple[str, str], ...] = ()
    #: The launcher's own veto: the field exists but cannot be written now.
    #: Distinct from the tab greying everything out because the game is off.
    enabled: bool = True
    tooltip: str = ""
    group: str = GROUP_COMMAND


@dataclass(frozen=True)
class LauncherWriteState:
    """Whether this launcher will accept a setting right now, and what to do.

    Shaped after ui.daemon_setup.DaemonHealth, which ui/components/scan_controls
    already renders as summary + detail + one optional action button. Same
    problem -- a subsystem that may need one user action before it can serve --
    so the same shape, rather than a second idiom for it.
    """

    state: str = WRITE_READY
    #: A word or two for the status bar: "live apply", "Steam stopped".
    summary: str = ""
    #: The sentence explaining what is wrong and why it matters.
    detail: str = ""
    #: A standing readout worth showing regardless: "Steam user: …".
    note: str = ""
    #: Empty when the launcher has nothing the user could press to fix it.
    action_label: str = ""
    #: Method on the launcher's manager that the action button calls.
    action: str = ""
    #: Non-empty asks the user first; the text is the question.
    confirm: str = ""

    @property
    def ready(self) -> bool:
        return self.state == WRITE_READY


@dataclass(frozen=True)
class LauncherBulkAction:
    """One entry of the library-wide "All games" menu.

    Launchers that share a ``key`` share a menu entry: the tab shows it once
    and applies it to each launcher that declared it, so two libraries are
    still one "disable everything".
    """

    key: str
    label: str
    setter: str
    value: bool
    #: The question to ask first. ``{count}`` becomes the number of games.
    confirm: str = ""
    #: Which LibraryGame field this action sets. Named rather than assumed, so
    #: the tab can grey out a direction that would change nothing without
    #: knowing what any particular action means.
    affects: str = ""
    #: Whether it only touches games the user already switched on. Turning the
    #: overlay on for a game PenguinBurner does not wrap would do nothing.
    enabled_only: bool = False


@runtime_checkable
class LauncherSource(Protocol):
    """Everything the library tab may ask of a launcher.

    The tab knows no launcher by name. Anything one launcher has and another
    does not -- a compatibility tool, a prefix command, an integration that
    needs initialising before it will accept a write -- is *described* here and
    drawn generically, which is what keeps a third launcher to one new adapter.

    Writes are named here, never performed: every setter this contract hands
    back is a method on the launcher's own manager, which already knows how to
    put a wrapper in front of a game and how to take it back out. Duplicating
    that knowledge here is how the two would drift.
    """

    launcher_id: str
    display_name: str
    #: File name under ui/assets used as this launcher's badge.
    icon_asset: str
    #: Whether PenguinBurner can start a game itself. Steam exposes an API for
    #: it, Lutris does not.
    can_launch: bool

    def available(self) -> bool:
        """Whether this launcher is installed on the machine at all."""
        ...

    def games(self) -> tuple[LibraryGame, ...]:
        """The current library, already read."""
        ...

    def refresh(self, *, deep: bool = True) -> None:
        """Re-read the library.

        ``deep=False`` is the cheap pass the tab runs on a timer: it may skip
        anything slow or off-machine and settle for what is on disk. It must
        still be correct about which games exist and when they were played --
        noticing a new install is the whole point of that timer.
        """
        ...

    def fields(self, game: LibraryGame) -> tuple[LauncherField, ...]:
        """The settings only this launcher has, for this one game.

        Computed per game because that is where the answer lives: which
        compatibility tools exist depends on the game, and whether a prefix
        command is inherited depends on where it was set.
        """
        ...

    def write_state(self) -> LauncherWriteState:
        """Whether this launcher will accept a setting right now."""
        ...

    def bulk_actions(self) -> tuple[LauncherBulkAction, ...]:
        """Library-wide actions this launcher can apply to all its games."""
        ...

    def after_setting_write(self, game_id: str, setter: str) -> object | None:
        """Optional launcher-owned follow-up after a successful setting write.

        Runs on the same worker as the write. Launchers without a live runtime
        action return None; Steam uses it to re-apply profile settings to a
        game the daemon is already watching.
        """
        ...


@runtime_checkable
class LaunchableSource(Protocol):
    """A launcher that can also start and stop a game on our behalf.

    Separate from LauncherSource because it is genuinely a separate capability,
    and one a launcher can lack for its own reasons -- no API for it, or the
    client simply not installed on this machine. A launcher that cannot start a
    game should not have to carry three methods that raise. `can_launch` is the
    flag the library tab reads; this is the shape behind it.
    """

    def launch(self, game_id: str) -> tuple[bool, str]:
        """Start a game. Returns (started, what to tell the user)."""
        ...

    def stop(self, game_id: str) -> tuple[bool, str]:
        """Stop a game. Returns (stopping, what to tell the user)."""
        ...

    def running_game_ids(self) -> frozenset[str] | None:
        """Which of this launcher's games are running.

        None means the check itself failed, which is not the same as nothing
        running: the caller must hold what it knows rather than read a stalled
        probe as every game having exited.
        """
        ...


def sorted_library_games(
    games,
    mode: str = SORT_ALPHABETICAL,
    *,
    launcher_names: Mapping[str, str] | None = None,
) -> tuple[LibraryGame, ...]:
    """Order the merged library.

    Every mode falls back to the name and then to the launcher-qualified id, so
    two games that tie on the sort key keep a stable position between refreshes
    instead of swapping places under the cursor.
    """
    rows = tuple(games)
    if mode == SORT_LAUNCHER:
        names = launcher_names or {}
        return tuple(
            sorted(
                rows,
                key=lambda game: (
                    names.get(game.launcher, game.launcher).casefold(),
                    game.name.casefold(),
                    game.key,
                ),
            )
        )
    if mode == SORT_RECENT:
        # Descending, so a 0 -- never played, or never recorded -- lands at the
        # end on its own: negating it makes it the largest key there is.
        return tuple(
            sorted(
                rows,
                key=lambda game: (-game.last_played, game.name.casefold(), game.key),
            )
        )
    if mode == SORT_PLAYTIME:
        return tuple(
            sorted(
                rows,
                key=lambda game: (-game.playtime_hours, game.name.casefold(), game.key),
            )
        )
    return tuple(sorted(rows, key=lambda game: (game.name.casefold(), game.key)))
