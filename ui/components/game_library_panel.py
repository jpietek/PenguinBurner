"""Game Library: every launcher's games in one list, with one settings pane.

Steam and Lutris used to have a tab each, built twice and styled twice from
the same shapes. What differs between them is narrow -- a compatibility tool
here, a command prefix there, and whether PenguinBurner can start a game at
all -- so the differences live in each launcher's adapter and this panel draws
the part they share.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from integrations.launchers.library import (
    FIELD_CHOICE,
    FIELD_MULTILINE,
    FIELD_SWITCH,
    FIELD_TEXT,
    GROUP_COMMAND,
    GROUP_IN_GAME,
    SORT_ALPHABETICAL,
    SORT_LAUNCHER,
    SORT_PLAYTIME,
    SORT_RECENT,
    LibraryGame,
    sorted_library_games,
)
from integrations.launchers.registry import build_sources, known_launcher_names
from profiles.game_profile import (
    GAME_MODE_ADAPTIVE,
    GAME_MODE_STOCK,
    normalize_game_mode,
)
from profiles.uv.profile_tiers import PROFILE_TIER_LABELS, PROFILE_TIERS
from runtime.support.adaptive_target_fps import (
    MAX_ADAPTIVE_TARGET_FPS,
    MIN_ADAPTIVE_TARGET_FPS,
    adaptive_target_fps_from_env,
)

from .. import theme
from ..assets import asset_image_path
from ..dialogs.form_rows import wrapped_tooltip
from .preference_rows import (
    GROUP_SPACING,
    full_width_row,
    preference_group,
    preference_row,
)
from .toggle_switch import make_toggle_switch
from .spinner import make_spinner

_MODE_LABELS = {
    GAME_MODE_ADAPTIVE: "Adaptive",
    **PROFILE_TIER_LABELS,
    GAME_MODE_STOCK: "Stock (factory GPU state)",
}
_MODE_KEYS = (GAME_MODE_ADAPTIVE, *PROFILE_TIERS, GAME_MODE_STOCK)

_SORT_LABELS = (
    ("Alphabetical", SORT_ALPHABETICAL),
    ("Launcher", SORT_LAUNCHER),
    ("Recently played", SORT_RECENT),
    ("Most played", SORT_PLAYTIME),
)

# Lutris cover art is 3:4 box art and Steam icons are square, so the slot keeps
# the taller ratio: a square would letterbox every Lutris cover into a sliver,
# while a square icon in a 3:4 slot simply sits centred.
_ART_WIDTH = 36
_ART_HEIGHT = 48
_ROW_HEIGHT = 58
#: The launcher badge, drawn into the corner of the art.
_BADGE = 18

# Per-game launch lifecycle, tracked only for games started from this tab.
# "launching" -- Play clicked, session not seen yet -- reads as Running: it
# exists so a slow start is not mistaken for an exit.
_GAME_STATE_POLL_MS = 1500
_PENDING_LAUNCH_S = 120.0  # for the launcher's session to appear after Play
_PENDING_STOP_S = 30.0  # for a stop request to take effect
_CONFIRMED_GONE_POLLS = 2  # consecutive not-running polls before Stopped
_ACTIVE_GAME_STATES = ("launching", "running", "stopping")

#: Re-read every library on a timer, so a game installed while the tab is open
#: turns up without the user pressing anything. The cheap pass, not the deep
#: one: this runs forever, where the deep read is a scan the user asked for.
_LIBRARY_SYNC_MS = 10000
#: Roughly three wrapped lines of a launch command.
_MULTILINE_HEIGHT = 78
#: Wide enough for a full Proton build name without eliding it.
_CHOICE_MIN_WIDTH = 220
#: How long the first collect waits for the worker before handing off to a
#: timer. Every launcher but a running Steam reads in well under this, and
#: landing in the same tick spares the user a blink of empty list.
_SCAN_JOIN_S = 0.05
_SCAN_FEEDBACK_DELAY_MS = 500


@dataclass
class _TrackedGame:
    """Launch lifecycle of one game started from this tab."""

    state: str
    deadline: float = 0.0  # monotonic grace cutoff while launching/stopping
    misses: int = 0  # consecutive polls that did not see the session


@dataclass
class _SettingWriteOutcome:
    """Worker result handed back to the Qt thread."""

    game: LibraryGame
    result: Any | None = None
    followup: Any | None = None
    refresh_problem: str = ""
    error: str = ""


@dataclass(frozen=True)
class _SettingWriteRequest:
    """One optimistic control change waiting for its launcher write."""

    game: LibraryGame
    method: str
    args: tuple[Any, ...]


@dataclass(frozen=True)
class _BulkWriteResult:
    ok: bool
    message: str


def no_launchers_text(names=None) -> str:
    """The empty state names what was looked for, without hard-coding it here.

    The set of launchers PenguinBurner reads is the registry's fact, not this
    tab's: adding one should change this sentence with no edit here.
    """
    known = tuple(names if names is not None else known_launcher_names())
    listed = ", ".join(known) or "a game launcher"
    return (
        f"No game launcher found. PenguinBurner looks for {listed}. "
        "Install one, then press Rescan."
    )
EMPTY_LIBRARY_TEXT = (
    "A launcher is installed but has no games yet. Install one, then press "
    "Rescan."
)


def game_key(game: LibraryGame) -> str:
    """List-item identity. Ids repeat across launchers, so both go in."""
    return f"{game.launcher}:{game.game_id}"


def game_metadata_text(game: LibraryGame) -> str:
    """The game's own facts, under its title. Pure.

    The launcher comes first because it is the one thing a merged list makes
    ambiguous; the badge on the row says it too, for anyone who reads icons
    faster than words.
    """
    launcher = game.launcher.title()
    parts = [launcher, game.subtitle]
    if game.playtime_hours:
        parts.append(f"{game.playtime_hours:.1f} h played")
    return " · ".join(part for part in parts if part)


def game_tooltip(game: LibraryGame) -> str:
    """What does not fit on the row. Pure."""
    lines = [game.name, game_metadata_text(game)]
    if not game.ready:
        lines.append("Not installed")
    return "\n".join(line for line in lines if line)


def library_header_text(sources) -> str:
    """Which launchers answered, in the slot the Steam tab gave the account.

    A merged library has no single identity, so it says what it merged. Pure.
    """
    names = [str(source.display_name) for source in sources]
    if not names:
        return "Game library: no launcher found"
    return f"Game library: {', '.join(names)}"


def library_placeholder(*, launcher_count: int, game_count: int) -> str:
    """What to say when the list is empty, and why. Pure."""
    if launcher_count == 0:
        return no_launchers_text()
    if game_count == 0:
        return EMPTY_LIBRARY_TEXT
    return ""


def library_status_text(games, message: str = "", write_state=None) -> str:
    """Bottom line: the counts, the launcher's own words, then the last action.

    ``write_state`` is whatever the selected game's launcher said about itself
    -- who is signed in, whether a write would land right now. Pure.
    """
    total = len(games)
    configured = sum(1 for game in games if game.enabled)
    noun = "game" if total == 1 else "games"
    parts = [f"{total} {noun}", f"{configured} configured"]
    for extra in (
        str(getattr(write_state, "note", "") or ""),
        str(getattr(write_state, "summary", "") or ""),
    ):
        if extra:
            parts.append(extra)
    if message:
        parts.append(message)
    return " · ".join(parts)


class GameLibraryPanel:
    def __init__(
        self,
        *,
        QtCore,
        QtGui,
        QtWidgets,
        sources=None,
        gpu_choices=None,
        adaptive_available=None,
    ):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        # Every launcher PenguinBurner can read, installed here or not. Which
        # of them are actually present is re-asked on every scan -- the empty
        # state tells the user to install one and press Rescan, so Rescan has
        # to be able to notice. Detection itself is a read of the filesystem,
        # so the first answer is safe to take here; the library scan behind it
        # is not run until the tab is first shown. Loosely typed for the same
        # reason as _by_launcher below: the tuple mixes launchers that can
        # start a game with launchers that cannot.
        self._candidate_sources: tuple[Any, ...] = tuple(
            build_sources() if sources is None else sources
        )
        self._sources: tuple[Any, ...] = tuple(
            source
            for source in self._candidate_sources
            if self._source_available(source)
        )
        # Values typed loosely on purpose: whether a source can start a game is
        # declared by can_launch and checked at every call site, which no
        # static shape can express for a dict of mixed launchers.
        self._by_launcher: dict[str, Any] = {
            source.launcher_id: source for source in self._sources
        }
        # A callable, as both old panels took: resolving GPUs talks to the
        # daemon, which must not happen while the window is still being built.
        self._gpu_choices_source = gpu_choices or list
        #: adaptive_available(gpu_uuid, single_gpu) -> bool. Adaptive needs a
        #: verified tier profile to switch between, and that is a property of
        #: the saved profiles rather than of the launcher, so it gates the mode
        #: for every game in the list.
        self._adaptive_available = adaptive_available
        self._gpu_choices: tuple[object, ...] = ()
        self._games: tuple[LibraryGame, ...] = ()
        self._selected_key = ""
        self._sort_mode = SORT_ALPHABETICAL
        self._syncing = False
        self._scanned = False
        self._badges: dict[str, Any] = {}
        # Launcher-declared fields: the widgets, which game they were filled
        # for, and which one is holding text the user has not saved yet.
        self._fields: dict[str, dict[str, Any]] = {}
        self._field_owner = ""
        self._pending_field = ""
        #: Whether the selected game's launcher will accept a write right now.
        self._writable = True
        self._bulk_menu_entries: list[tuple[Any, Any, tuple[Any, ...]]] = []
        self._write_state: Any = None
        self._write_state_source: Any = None
        # Refreshes replace manager caches, so they share the write lock even
        # when a timer started the scan before the user changed a setting.
        # Reentrant because each write also refreshes its source before release.
        self._library_lock = threading.RLock()
        self._scan_thread: threading.Thread | None = None
        self._scan_result: tuple | None = None
        self._scan_active = False
        self._scan_initial = False
        self._scan_quiet = False
        self._scan_sources: tuple[Any, ...] = ()
        self._action_thread: threading.Thread | None = None
        self._action_result: Any = None
        self._setting_thread: threading.Thread | None = None
        self._setting_result: _SettingWriteOutcome | None = None
        self._setting_queue: list[_SettingWriteRequest] = []
        self._write_problems: list[str] = []
        self._write_busy = False
        self._stop_thread: threading.Thread | None = None
        self._stop_result: tuple[bool, str] | None = None
        self._tracked: dict[str, _TrackedGame] = {}
        self._poll_thread: threading.Thread | None = None
        self._poll_result: dict[str, frozenset[str] | None] = {}

        self.widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(12, 18, 12, 12)
        layout.setSpacing(10)

        header_row = QtWidgets.QHBoxLayout()
        header_row.setSpacing(8)
        self.library_label = QtWidgets.QLabel(library_header_text(self._sources))
        self.library_label.setObjectName("libraryLabel")
        header_row.addWidget(self.library_label)
        header_row.addStretch(1)
        self.rescan_button = QtWidgets.QPushButton("Rescan")
        self.rescan_button.setToolTip(self._rescan_tooltip())
        self.rescan_button.clicked.connect(lambda _checked=False: self.rescan())
        # The one thing a launcher can ask the user to do before it will accept
        # a setting -- set itself up, restart itself. Hidden while none does.
        self.write_action_button = QtWidgets.QPushButton("")
        self.write_action_button.setObjectName("libraryWriteAction")
        self.write_action_button.hide()
        self.write_action_button.clicked.connect(
            lambda _checked=False: self._run_write_action()
        )
        header_row.addWidget(self.write_action_button)
        self.rescan_spinner = make_spinner(
            QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, size=16
        )
        self.rescan_spinner.hide()
        header_row.addWidget(self.rescan_spinner)
        header_row.addWidget(self.rescan_button)
        layout.addLayout(header_row)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.setObjectName("librarySplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_library())
        self.splitter.addWidget(self._build_settings())
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([370, 760])
        self.content_stack = QtWidgets.QStackedWidget()
        self.content_stack.addWidget(self.splitter)
        self.loading_page = self._build_loading_page()
        self.content_stack.addWidget(self.loading_page)
        layout.addWidget(self.content_stack, 1)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("libraryStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._loading_delay_timer = QtCore.QTimer(self.widget)
        self._loading_delay_timer.setSingleShot(True)
        self._loading_delay_timer.timeout.connect(self._show_scan_feedback)

        # Only ticks while something this tab started is still alive; the
        # lifecycle stops it again as soon as nothing is.
        self._state_timer = QtCore.QTimer(self.widget)
        self._state_timer.setInterval(_GAME_STATE_POLL_MS)
        self._state_timer.timeout.connect(self._poll_game_states)

        # Starts with the first scan, so a tab nobody has opened costs nothing.
        self._library_timer = QtCore.QTimer(self.widget)
        self._library_timer.setInterval(_LIBRARY_SYNC_MS)
        self._library_timer.timeout.connect(
            lambda: self.rescan(deep=False, quiet=True)
        )

        self._sync_selection(None)
        self._sync_status()

    # -- construction --------------------------------------------------------

    def _build_loading_page(self):
        page = self.QtWidgets.QFrame()
        page.setObjectName("libraryLoadingPane")
        layout = self.QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        self.loading_message = self.QtWidgets.QWidget()
        self.loading_message.setMaximumWidth(480)
        message = self.QtWidgets.QVBoxLayout(self.loading_message)
        message.setSpacing(12)
        self.loading_spinner = make_spinner(
            QtCore=self.QtCore, QtGui=self.QtGui, QtWidgets=self.QtWidgets
        )
        message.addWidget(self.loading_spinner, 0, self.QtCore.Qt.AlignHCenter)
        label = self.QtWidgets.QLabel("Loading installed games…")
        label.setObjectName("libraryLoadingTitle")
        label.setAlignment(self.QtCore.Qt.AlignCenter)
        message.addWidget(label)
        self.loading_sources_label = self.QtWidgets.QLabel("")
        self.loading_sources_label.setObjectName("libraryLoadingSources")
        self.loading_sources_label.setWordWrap(True)
        self.loading_sources_label.setAlignment(self.QtCore.Qt.AlignCenter)
        message.addWidget(self.loading_sources_label)
        layout.addWidget(self.loading_message, 0, self.QtCore.Qt.AlignHCenter)
        layout.addStretch(1)
        self.loading_message.hide()
        return page

    @staticmethod
    def _source_available(source) -> bool:
        try:
            return bool(source.available())
        except Exception:  # noqa: BLE001 - a launcher may fail any way
            return False

    def _rescan_tooltip(self) -> str:
        return wrapped_tooltip(
            "Re-read every launcher's library and each game's current "
            "launch command. Press this after changing a game in "
            + (
                ", ".join(source.display_name for source in self._sources)
                or "your launcher"
            )
            + "."
        )

    def _build_library(self):
        pane = self.QtWidgets.QFrame()
        pane.setObjectName("libraryPane")
        pane.setMinimumWidth(300)
        inner = self.QtWidgets.QVBoxLayout(pane)
        inner.setContentsMargins(10, 10, 10, 10)
        inner.setSpacing(8)

        pane_title = self.QtWidgets.QLabel("Games")
        pane_title.setObjectName("libraryPaneTitle")
        inner.addWidget(pane_title)

        actions_row = self.QtWidgets.QHBoxLayout()
        actions_row.setSpacing(6)
        actions_row.addWidget(self.QtWidgets.QLabel("Sort"))
        self.sort_combo = self.QtWidgets.QComboBox()
        self.sort_combo.setObjectName("librarySort")
        for label, key in _SORT_LABELS:
            self.sort_combo.addItem(label, key)
        self.sort_combo.currentIndexChanged.connect(
            lambda _index: self._on_sort_changed()
        )
        actions_row.addWidget(self.sort_combo)
        actions_row.addStretch(1)
        self.all_games_button = self.QtWidgets.QToolButton()
        self.all_games_button.setObjectName("libraryAllGamesButton")
        self.all_games_button.setText("All games")
        self.all_games_button.setPopupMode(
            self.QtWidgets.QToolButton.InstantPopup
        )
        self.all_games_button.setToolTip(
            wrapped_tooltip(
                "Bulk actions across every launcher listed here. Each one asks "
                "for confirmation first."
            )
        )
        menu = self.QtWidgets.QMenu(self.all_games_button)
        self._bulk_entries = self._merged_bulk_actions()
        for action, sources in self._bulk_entries:
            entry = menu.addAction(
                action.label,
                lambda checked=False, a=action, srcs=sources: self._bulk_apply(a, srcs),
            )
            self._bulk_menu_entries.append((entry, action, sources))
        menu.aboutToShow.connect(self._sync_bulk_menu)
        self.all_games_button.setMenu(menu)
        actions_row.addWidget(self.all_games_button)
        inner.addLayout(actions_row)

        self.game_list = self.QtWidgets.QListWidget()
        self.game_list.setObjectName("gameList")
        self.game_list.setIconSize(self.QtCore.QSize(_ART_WIDTH, _ART_HEIGHT))
        self.game_list.currentItemChanged.connect(
            lambda current, _previous: self._select_from_item(current)
        )
        inner.addWidget(self.game_list, 1)

        self.placeholder_label = self.QtWidgets.QLabel("")
        self.placeholder_label.setObjectName("libraryPlaceholder")
        self.placeholder_label.setWordWrap(True)
        inner.addWidget(self.placeholder_label)
        return pane

    def _build_settings(self):
        """Details pane: a hero line, then settings grouped by what they do.

        The frame is a plain QFrame *around* the scroll area, not the scroll
        area itself. Styling a QAbstractScrollArea's box hands its whole subtree
        to Qt's stylesheet painter, and the scroll bar then loses the platform
        style -- one native bar beside one Fusion-looking bar in the same tab.
        """
        pane = self.QtWidgets.QFrame()
        pane.setObjectName("gameDetailsPane")
        pane_layout = self.QtWidgets.QVBoxLayout(pane)
        pane_layout.setContentsMargins(0, 0, 0, 0)
        pane_layout.setSpacing(0)

        page_widget = self.QtWidgets.QWidget()
        page_widget.setObjectName("gameSettingsPage")
        page = self.QtWidgets.QVBoxLayout(page_widget)
        page.setContentsMargins(24, 24, 24, 24)
        page.setSpacing(GROUP_SPACING)

        # -- hero -------------------------------------------------------------
        hero = self.QtWidgets.QHBoxLayout()
        hero.setSpacing(16)
        info_column = self.QtWidgets.QVBoxLayout()
        info_column.setContentsMargins(0, 0, 0, 0)
        info_column.setSpacing(4)
        self.title_label = self.QtWidgets.QLabel("Select a game")
        self.title_label.setObjectName("gameTitle")
        info_column.addWidget(self.title_label)
        self.metadata_label = self.QtWidgets.QLabel("")
        self.metadata_label.setObjectName("gameMetadata")
        self.metadata_label.setWordWrap(True)
        info_column.addWidget(self.metadata_label)
        hero.addLayout(info_column, 1)
        self.play_button = self.QtWidgets.QPushButton("Play")
        self.play_button.setObjectName("gamePlayButton")
        self.play_button.setProperty("playState", "idle")
        self.play_button.clicked.connect(self._play_stop_clicked)
        hero.addWidget(self.play_button, 0, self.QtCore.Qt.AlignTop)
        page.addLayout(hero)
        self.compatibility_label = self.QtWidgets.QLabel("")
        self.compatibility_label.setObjectName("gameCompatibility")
        self.compatibility_label.setWordWrap(True)
        self.compatibility_label.hide()
        page.addWidget(self.compatibility_label)

        # -- group: PenguinBurner ---------------------------------------------
        group, rows = preference_group(
            QtWidgets=self.QtWidgets, heading="PenguinBurner"
        )
        self.enable_switch = make_toggle_switch(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            object_name="gameEnable",
        )
        self.enable_switch.toggled.connect(self._on_enabled)
        preference_row(
            QtWidgets=self.QtWidgets,
            QtCore=self.QtCore,
            rows_layout=rows,
            title="Wrap this game",
            subtitle=(
                "Puts the PenguinBurner wrapper in this game's launch command. "
                "Off, the launcher starts it untouched."
            ),
            control=self.enable_switch,
        )
        page.addWidget(group)

        # -- group: tuning (everything the wrapper switch governs) ------------
        self._form_widget, tuning_rows = preference_group(
            QtWidgets=self.QtWidgets, heading="Tuning"
        )
        self.mode_combo = self.QtWidgets.QComboBox()
        self.mode_combo.setObjectName("gameMode")
        self.mode_combo.setMinimumWidth(200)
        for key in _MODE_KEYS:
            self.mode_combo.addItem(_MODE_LABELS[key], key)
        self.mode_combo.currentIndexChanged.connect(
            lambda _index: self._on_mode_changed()
        )
        preference_row(
            QtWidgets=self.QtWidgets,
            QtCore=self.QtCore,
            rows_layout=tuning_rows,
            title="Auto-UV mode",
            subtitle=(
                "Adaptive switches tiers from live frame pacing. A named tier "
                "pins that one profile; Stock pins the factory GPU state."
            ),
            control=self.mode_combo,
        )

        target_widget = self.QtWidgets.QWidget()
        target_row = self.QtWidgets.QHBoxLayout(target_widget)
        target_row.setContentsMargins(0, 0, 0, 0)
        target_row.setSpacing(8)
        self.target_follow_label = self.QtWidgets.QLabel("")
        self.target_follow_label.setObjectName("gameTargetFollow")
        self.target_fps_spin = self.QtWidgets.QDoubleSpinBox()
        self.target_fps_spin.setObjectName("gameTargetFps")
        self.target_fps_spin.setRange(MIN_ADAPTIVE_TARGET_FPS, MAX_ADAPTIVE_TARGET_FPS)
        self.target_fps_spin.setDecimals(0)
        self.target_fps_spin.setSingleStep(5.0)
        self.target_fps_spin.setSuffix(" FPS")
        self.target_fps_spin.setKeyboardTracking(False)
        self.target_fps_spin.setFixedWidth(116)
        self.target_fps_spin.setValue(adaptive_target_fps_from_env())
        self.target_fps_spin.valueChanged.connect(
            lambda _value: self._on_target_fps_changed()
        )
        self.per_game_target_switch = make_toggle_switch(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            object_name="gamePerGameTarget",
        )
        self.per_game_target_switch.toggled.connect(self._on_per_game_target_toggled)
        target_row.addWidget(self.target_follow_label)
        target_row.addWidget(self.target_fps_spin)
        target_row.addWidget(self.per_game_target_switch)
        preference_row(
            QtWidgets=self.QtWidgets,
            QtCore=self.QtCore,
            rows_layout=tuning_rows,
            title="Per-game target",
            # Short on purpose: the control beside it already spells out
            # which system-wide rate is being followed, and a second sentence
            # here wraps past the height the row is given.
            subtitle="Off, this game follows the system-wide Adaptive target.",
            control=target_widget,
        )

        self.gpu_combo = self.QtWidgets.QComboBox()
        self.gpu_combo.setObjectName("gameGpu")
        self.gpu_combo.setMinimumWidth(200)
        self.gpu_combo.currentIndexChanged.connect(
            lambda _index: self._on_gpu_changed()
        )
        self._gpu_row = preference_row(
            QtWidgets=self.QtWidgets,
            QtCore=self.QtCore,
            # GPU selection must be available before enabling the wrapper.
            rows_layout=rows,
            title="Graphics card",
            subtitle="Which GPU this game's profile applies to.",
            control=self.gpu_combo,
        )
        # Empty until the first scan: resolving GPUs talks to the daemon, and
        # that round-trip must not run while the window is still being built.
        # Every scan re-reads them, so a daemon that was down at startup fills
        # the row in once it answers.
        self._apply_gpu_choices(())
        page.addWidget(self._form_widget)

        # -- group: in game ---------------------------------------------------
        self._ingame_group, ingame_rows = preference_group(
            QtWidgets=self.QtWidgets, heading=GROUP_IN_GAME
        )
        self.overlay_switch = make_toggle_switch(
            QtCore=self.QtCore,
            QtGui=self.QtGui,
            QtWidgets=self.QtWidgets,
            object_name="gameOverlay",
        )
        self.overlay_switch.toggled.connect(lambda checked: self._on_overlay(checked))
        preference_row(
            QtWidgets=self.QtWidgets,
            QtCore=self.QtCore,
            rows_layout=ingame_rows,
            title="Overlay",
            subtitle="Draw the PenguinBurner overlay in this game.",
            control=self.overlay_switch,
        )
        page.addWidget(self._ingame_group)

        # -- group: the line the launcher actually runs ------------------------
        self._command_group, command_rows = preference_group(
            QtWidgets=self.QtWidgets, heading=GROUP_COMMAND
        )
        page.addWidget(self._command_group)

        # Everything past this point belongs to whichever launcher owns the
        # selected game. The rows are built from what its source declares and
        # kept per key, so clicking down the list re-fills the form instead of
        # rebuilding it -- and so a launcher this tab has never heard of needs
        # no code here at all.
        self._field_rows = {
            GROUP_IN_GAME: ingame_rows,
            GROUP_COMMAND: command_rows,
        }
        self._field_groups = {
            GROUP_IN_GAME: self._ingame_group,
            GROUP_COMMAND: self._command_group,
        }
        self._fields: dict[str, dict[str, Any]] = {}

        page.addStretch(1)

        scroll = self.QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(self.QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(self.QtCore.Qt.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        scroll.setWidget(page_widget)
        pane_layout.addWidget(scroll)
        self.details_scroll = scroll
        return pane

    def _read_gpu_choices(self) -> tuple[object, ...]:
        """Ask the daemon which GPUs exist. Runs on the scan worker thread."""
        try:
            return tuple(self._gpu_choices_source())
        except Exception:
            return ()

    def _apply_gpu_choices(self, choices) -> bool:
        """Fill the GPU combo from a finished read; True when anything changed."""
        choices = tuple(choices)
        if choices == self._gpu_choices and self.gpu_combo.count() > 0:
            return False
        self._gpu_choices = choices
        selected = str(self.gpu_combo.currentData() or "")
        was_syncing = self._syncing
        self._syncing = True
        try:
            self.gpu_combo.clear()
            self.gpu_combo.addItem("Auto (single GPU)", "")
            for choice in choices:
                label = str(getattr(choice, "label", "") or getattr(choice, "name", ""))
                uuid = str(getattr(choice, "uuid", "") or "")
                if uuid:
                    self.gpu_combo.addItem(label or uuid, uuid)
            self.gpu_combo.setCurrentIndex(max(0, self.gpu_combo.findData(selected)))
        finally:
            self._syncing = was_syncing
        # One card needs no choosing, and the whole row goes so the group does
        # not keep a separator for a row that is not there.
        self._gpu_row.setVisible(self.gpu_combo.count() > 2)
        return True

    def _set_gated(self, enabled: bool) -> None:
        """Enable what the wrapper switch governs -- never the switch itself."""
        self._form_widget.setEnabled(enabled)
        self._ingame_group.setEnabled(enabled)

    def _sync_status(self, message: str = "") -> None:
        self.status_label.setText(
            library_status_text(self._games, message, self._write_state)
        )

    # -- scanning ------------------------------------------------------------

    def _show_scan_feedback(self) -> None:
        thread = self._scan_thread
        if not self._scan_active or self._scan_quiet or thread is None or not thread.is_alive():
            return
        if self._scan_initial:
            self.loading_sources_label.setText(
                ", ".join(str(source.display_name) for source in self._scan_sources)
            )
            self.loading_message.show()
        else:
            self.rescan_spinner.show()
            self.rescan_button.setText("Scanning…")

    def _finish_scan_feedback(self) -> None:
        self._scan_active = False
        self._loading_delay_timer.stop()
        self.loading_message.hide()
        self.rescan_spinner.hide()
        self.rescan_button.setText("Rescan")
        self.rescan_button.setEnabled(True)
        self.content_stack.setCurrentWidget(self.splitter)

    def ensure_scanned(self) -> None:
        """Read every launcher once, the first time the tab is shown.

        Not in the constructor: the window builds every tab up front, and a
        library scan there would be filesystem work nobody asked for yet.
        Reading only -- opening a tab must not write anyone's settings.
        """
        if self._scanned:
            return
        self._scanned = True
        self.rescan()
        # Only from here on: a tab nobody has opened polls nothing. The scan
        # itself reads; the one write it may make is the Steam manager
        # re-adopting a wrapped game whose settings entry went missing --
        # repairing our bookkeeping, never a launcher's config.
        self._library_timer.start()

    def rescan(self, *, deep: bool = True, quiet: bool = False) -> None:
        """Re-read every launcher, off the GUI thread.

        Steam's deep pass reads each app's details back over CDP, which is
        seconds of work in the worst case; on the GUI thread that is the window
        frozen for as long as it takes -- and the first scan runs the moment
        the tab is opened.

        ``quiet`` is the timer's pass: no status chatter, and the list is left
        alone unless something in it actually changed.
        """
        if self._write_busy:
            return
        if quiet:
            # The timer must neither steal nor force-save a half-typed edit;
            # it simply comes back in ten seconds.
            if self._pending_field:
                return
        elif not self._flush_pending_field_edit():
            return
        if self._scan_active or (self._scan_thread is not None and self._scan_thread.is_alive()):
            return
        self._scan_result = None
        self._scan_active = True
        self._scan_initial = not quiet and not bool(self._games)
        self._scan_quiet = quiet
        self._scan_sources = ()
        if self._scan_initial:
            self.content_stack.setCurrentWidget(self.loading_page)
        if not quiet:
            self.rescan_button.setEnabled(False)
            self._loading_delay_timer.start(_SCAN_FEEDBACK_DELAY_MS)
        candidates = self._candidate_sources

        def run() -> None:
            # Availability is re-asked every pass: the empty state tells the
            # user to install a launcher and press Rescan, so Rescan has to be
            # able to notice one arriving (or leaving).
            installed = tuple(
                source for source in candidates if self._source_available(source)
            )
            self._scan_sources = installed
            problems = tuple(
                problem
                for problem in (
                    self._refresh_source(source, deep=deep) for source in installed
                )
                if problem
            )
            self._scan_result = (installed, problems, self._read_gpu_choices())

        self._scan_thread = threading.Thread(target=run, daemon=True)
        self._scan_thread.start()
        self._collect_scan(quiet=quiet)

    def _collect_scan(self, *, quiet: bool, first: bool = True) -> None:
        """Apply a finished scan on the GUI thread, where Qt requires it.

        The first attempt gives the worker a moment, so a library that reads
        instantly fills the list in this tick instead of a timer later. Only
        the first: waiting again on every retry would hand back the freeze
        this thread exists to avoid.
        """
        thread = self._scan_thread
        if thread is not None and first:
            thread.join(_SCAN_JOIN_S)
        if (thread is not None and thread.is_alive()) or self._write_busy:
            if self._scan_initial and not self.loading_message.isHidden():
                self.loading_sources_label.setText(
                    ", ".join(str(source.display_name) for source in self._scan_sources)
                )
            self.QtCore.QTimer.singleShot(
                100, lambda: self._collect_scan(quiet=quiet, first=False)
            )
            return
        result = self._scan_result
        if result is None:
            return
        self._scan_result = None
        installed, problems, gpu_choices = result
        self._apply_sources(installed)
        gpu_changed = self._apply_gpu_choices(gpu_choices)
        self._reload_games()
        # The scan just refreshed each launcher's own probe, so the banner,
        # the fix-it button and the field gating follow it here -- otherwise a
        # successful initialize or restart changes no list row and leaves them
        # stale until the user happens to click another game.
        self._sync_write_state(self._selected_game())
        if gpu_changed and not self._pending_field:
            # New GPU facts change the row's visibility and the adaptive
            # gating for the selected game; skipped mid-edit, where refilling
            # the pane would eat the user's text.
            self._sync_selection(self._selected_game())
        if problems:
            self._sync_status(" · ".join(problems))
        elif not quiet:
            self._sync_status()
        self._finish_scan_feedback()

    def _apply_sources(self, installed) -> None:
        installed = tuple(installed)
        if installed == self._sources:
            return
        self._sources = installed
        self._by_launcher = {source.launcher_id: source for source in installed}
        # A launcher that just arrived may bring its own icon with it.
        self._badges.clear()
        self.library_label.setText(library_header_text(installed))
        self.rescan_button.setToolTip(self._rescan_tooltip())

    def _refresh_source(self, source, *, deep: bool = True) -> str:
        """Re-read one launcher; return what went wrong, if anything.

        Runs on a worker thread. One launcher failing is not the library
        failing -- the others still have games to show -- but it is not nothing
        either, so it reaches the status line rather than disappearing.
        """
        try:
            with self._library_lock:
                source.refresh(deep=deep)
        except Exception as error:  # noqa: BLE001 - a launcher may fail any way
            return f"{source.display_name}: {error}"
        return ""

    def _reload_games(self) -> None:
        games: list[LibraryGame] = []
        for source in self._sources:
            games.extend(source.games())
        ordered = self._sorted_games(games)
        changed = self._library_signature(ordered) != self._library_signature(
            self._games
        )
        self._games = ordered
        # Rebuilding an unchanged list every ten seconds would throw away the
        # user's scroll position for nothing.
        if changed or not ordered:
            self._refresh_list()

    def _sorted_games(self, games) -> tuple[LibraryGame, ...]:
        """Order games using launcher names as they appear in the UI."""
        return sorted_library_games(
            games,
            self._sort_mode,
            launcher_names={
                source.launcher_id: source.display_name for source in self._sources
            },
        )

    @staticmethod
    def _library_signature(games) -> tuple:
        """What has to change before the list is worth rebuilding.

        Everything a row or its tooltip renders is in here: a field the
        signature misses is a change the ten-second pass can never show
        (playtime and Proton edits made inside the launcher were exactly
        that).
        """
        return tuple(
            (
                game.launcher,
                game.game_id,
                game.name,
                game.subtitle,
                game.ready,
                game.last_played,
                game.playtime_hours,
                game.enabled,
                game.wrapped,
                game.overlay,
                game.overlay_supported,
                game.overlay_unsupported_reason,
            )
            for game in games
        )

    # -- whether the launcher will take a write -------------------------------

    def _sync_write_state(self, game: LibraryGame | None) -> None:
        """Ask the selected game's launcher whether it can be written to now.

        Steam holds its launch options in memory while the client runs, so
        until its integration is set up a write cannot land -- and the user is
        better told that before typing than after.
        """
        source = self._by_launcher.get(game.launcher) if game is not None else None
        state = source.write_state() if source is not None else None
        self._write_state = state
        self._write_state_source = source
        self._writable = state.ready if state is not None else True
        label = getattr(state, "action_label", "") if state is not None else ""
        self.write_action_button.setText(label)
        self.write_action_button.setVisible(bool(label))
        if state is not None and state.detail:
            self.write_action_button.setToolTip(wrapped_tooltip(state.detail))
        # The gate covers every control that ends in a write, not only the
        # launcher-declared fields: a live wrap switch in front of a launcher
        # that will refuse the write is an animation followed by a snap-back.
        self.enable_switch.setEnabled(game is not None and self._writable)
        self.gpu_combo.setEnabled(game is not None and self._writable)
        self._set_gated(
            bool(game is not None and game.enabled) and self._writable
        )
        for widgets in self._fields.values():
            self._apply_field_enabled(widgets)
        self._sync_status()

    def _run_write_action(self) -> None:
        """Do whatever the launcher offered to do about its own write state."""
        if self._write_busy:
            return
        state = self._write_state
        source = self._write_state_source
        if state is None or source is None or not state.action:
            return
        if self._action_thread is not None and self._action_thread.is_alive():
            return
        if not self._flush_pending_field_edit():
            return
        if state.confirm and not self._confirm(state.action_label, state.confirm):
            return
        action = getattr(getattr(source, "manager", None), state.action, None)
        if action is None:
            action = getattr(source, state.action, None)
        if action is None:
            return
        # Off the GUI thread: a Steam restart polls the client for tens of
        # seconds, and running that inline froze the whole window for as long
        # as Steam took to come back.
        self.write_action_button.setEnabled(False)
        self._action_result = None

        def run() -> None:
            with self._library_lock:
                self._action_result = action()

        self._action_thread = threading.Thread(target=run, daemon=True)
        self._action_thread.start()
        self._collect_write_action()

    def _collect_write_action(self, first: bool = True) -> None:
        thread = self._action_thread
        if thread is not None and first:
            # Same shape as _collect_scan: an action that answers immediately
            # lands in this tick instead of a timer later.
            thread.join(_SCAN_JOIN_S)
        if thread is not None and thread.is_alive():
            self.QtCore.QTimer.singleShot(
                200, lambda: self._collect_write_action(first=False)
            )
            return
        result = self._action_result
        self._action_result = None
        self.write_action_button.setEnabled(True)
        self.rescan()
        self._sync_status(str(getattr(result, "message", "") or ""))

    def _confirm(self, title: str, text: str) -> bool:
        QtWidgets = self.QtWidgets
        answer = QtWidgets.QMessageBox.question(
            self.widget,
            title or "PenguinBurner",
            text,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        return answer == QtWidgets.QMessageBox.Yes

    # -- list ----------------------------------------------------------------

    def _launcher_badge(self, launcher: str):
        """The launcher's own small icon, loaded once per launcher.

        The machine's installed icon first -- that is the mark a user
        recognises -- and PenguinBurner's own glyph only where the launcher
        has none, which is also where its brand mark would be the wrong icon.
        """
        if launcher in self._badges:
            return self._badges[launcher]
        source = self._by_launcher.get(launcher)
        pixmap = None
        path = None
        if source is not None:
            installed = getattr(source, "desktop_icon", None)
            path = installed() if callable(installed) else None
            if path is None:
                asset = getattr(source, "icon_asset", "")
                path = asset_image_path(asset) if asset else None
        if path is not None:
            candidate = self.QtGui.QPixmap(str(path))
            if not candidate.isNull():
                pixmap = candidate.scaled(
                    _BADGE,
                    _BADGE,
                    self.QtCore.Qt.KeepAspectRatio,
                    self.QtCore.Qt.SmoothTransformation,
                )
        self._badges[launcher] = pixmap
        return pixmap

    def _row_icon(self, game: LibraryGame):
        """The game's art with its launcher's badge in the corner.

        The maintainer's ask was a small launcher mark per row; drawing it onto
        the art keeps one icon slot doing both jobs, so a merged list still
        reads at a glance without a second column of chrome.
        """
        canvas = self.QtGui.QPixmap(_ART_WIDTH, _ART_HEIGHT)
        canvas.fill(self.QtCore.Qt.transparent)
        painter = self.QtGui.QPainter(canvas)
        art = None
        if game.art_path is not None:
            loaded = self.QtGui.QPixmap(str(game.art_path))
            if not loaded.isNull():
                art = loaded.scaled(
                    _ART_WIDTH,
                    _ART_HEIGHT,
                    self.QtCore.Qt.KeepAspectRatio,
                    self.QtCore.Qt.SmoothTransformation,
                )
        if art is not None:
            painter.drawPixmap(
                (_ART_WIDTH - art.width()) // 2,
                (_ART_HEIGHT - art.height()) // 2,
                art,
            )
        badge = self._launcher_badge(game.launcher)
        if badge is not None:
            # Bottom-right, and drawn last so it survives whatever art is
            # underneath. With no art at all it becomes the whole icon.
            painter.drawPixmap(
                _ART_WIDTH - badge.width(),
                _ART_HEIGHT - badge.height(),
                badge,
            )
        painter.end()
        return self.QtGui.QIcon(canvas)

    @staticmethod
    def _row_label(game: LibraryGame) -> str:
        mark = "●" if game.enabled else "○"
        return f"{mark}  {game.name}"

    def _refresh_list(self) -> None:
        self._syncing = True
        try:
            self.game_list.clear()
            for game in self._games:
                item = self.QtWidgets.QListWidgetItem(self._row_label(game))
                item.setData(self.QtCore.Qt.UserRole, game_key(game))
                item.setSizeHint(self.QtCore.QSize(0, _ROW_HEIGHT))
                item.setIcon(self._row_icon(game))
                item.setToolTip(game_tooltip(game))
                item.setForeground(
                    self.QtGui.QColor(
                        theme.TEXT_STRONG if game.enabled else theme.TEXT_MUTED
                    )
                )
                self.game_list.addItem(item)
            placeholder = library_placeholder(
                launcher_count=len(self._sources),
                game_count=len(self._games),
            )
            self.placeholder_label.setText(placeholder)
            self.placeholder_label.setVisible(bool(placeholder))
            self.all_games_button.setEnabled(bool(self._games))
            self._sync_status()
        finally:
            self._syncing = False
        if self._games:
            wanted = self._selected_key or game_key(self._games[0])
            self._select_key(wanted)
        else:
            self._selected_key = ""
            self._sync_selection(None)

    def _on_sort_changed(self) -> None:
        self._sort_mode = str(self.sort_combo.currentData() or SORT_ALPHABETICAL)
        # Re-sorted, not re-read: the selection is kept so the row under the
        # cursor does not change identity when the order does.
        self._games = self._sorted_games(self._games)
        self._refresh_list()

    # -- selection -----------------------------------------------------------

    def _game_for_key(self, key: str) -> LibraryGame | None:
        for game in self._games:
            if game_key(game) == key:
                return game
        return None

    def _select_from_item(self, item) -> None:
        if self._syncing or item is None:
            return
        self._select_key(str(item.data(self.QtCore.Qt.UserRole) or ""))

    def _select_key(self, key: str) -> None:
        if key != self._selected_key:
            # Whatever is typed but unsaved belongs to the game it was typed
            # for; save it before the pane is refilled for another one.
            # (QPlainTextEdit has no editingFinished, so the click that moves
            # the selection is the boundary that saves a command edit.)
            if not self._flush_pending_field_edit():
                # QListWidget has already moved its highlight when this came
                # from a click. Keep it on the game that still owns the draft.
                self._select_list_row(self._selected_key)
                return
        game = self._game_for_key(key)
        if game is None:
            return
        self._selected_key = key
        self._select_list_row(key)
        self._sync_selection(game)

    def _select_list_row(self, key: str) -> None:
        for index in range(self.game_list.count()):
            item = self.game_list.item(index)
            if str(item.data(self.QtCore.Qt.UserRole) or "") == key:
                if self.game_list.currentRow() != index:
                    blocked = self.game_list.blockSignals(True)
                    try:
                        self.game_list.setCurrentRow(index)
                    finally:
                        self.game_list.blockSignals(blocked)
                break

    def _sync_selection(self, game: LibraryGame | None) -> None:
        self._syncing = True
        try:
            if game is None:
                self.title_label.setText("Select a game")
                self.metadata_label.setText("")
                self.play_button.setVisible(False)
                self.compatibility_label.hide()
                self._set_gated(False)
                self.enable_switch.setEnabled(False)
                self._sync_fields(None)
                return
            setting = getattr(game.detail, "setting", None)
            self.title_label.setText(game.name)
            self.metadata_label.setText(game_metadata_text(game))
            self.compatibility_label.setText(game.overlay_unsupported_reason)
            self.compatibility_label.setVisible(not game.overlay_supported)
            self.overlay_switch.setEnabled(game.overlay_supported)
            self.overlay_switch.setToolTip(
                wrapped_tooltip(game.overlay_unsupported_reason)
                if not game.overlay_supported else ""
            )
            # Whether the switches are live is the write gate's decision, in
            # _sync_write_state below.
            self.enable_switch.setChecked(game.enabled)

            mode = getattr(setting, "mode", GAME_MODE_ADAPTIVE)
            mode_index = self.mode_combo.findData(
                normalize_game_mode(mode, default=GAME_MODE_ADAPTIVE)
            )
            self.mode_combo.setCurrentIndex(max(0, mode_index))

            target = getattr(setting, "target_fps", None)
            self.per_game_target_switch.setChecked(target is not None)
            self.target_fps_spin.setValue(
                float(target if target is not None else adaptive_target_fps_from_env())
            )
            self._sync_target_controls(mode == GAME_MODE_ADAPTIVE)

            gpu_index = self.gpu_combo.findData(getattr(setting, "gpu_uuid", "") or "")
            self.gpu_combo.setCurrentIndex(max(0, gpu_index))

            self.overlay_switch.setChecked(bool(getattr(setting, "overlay", False)))
            self._sync_write_state(game)
            self._sync_fields(game)

            self._sync_adaptive_availability(getattr(setting, "gpu_uuid", "") or "")
            self._sync_play_button(game)
        finally:
            self._syncing = False

    def _sync_adaptive_availability(self, gpu_uuid: str) -> None:
        """Adaptive needs a supported renderer and an available GPU tier."""
        single_gpu = len(self._gpu_choices) == 1
        target = gpu_uuid
        if not target and single_gpu:
            target = str(getattr(self._gpu_choices[0], "uuid", "") or "")
        available = (
            bool(self._adaptive_available(target, single_gpu))
            if callable(self._adaptive_available) else True
        )
        game = self._selected_game()
        reason = ""
        if game is not None and not game.overlay_supported:
            available = False
            reason = game.overlay_unsupported_reason
        index = self.mode_combo.findData(GAME_MODE_ADAPTIVE)
        if index >= 0:
            item = self.mode_combo.model().item(index)
            if item is not None:
                item.setEnabled(available)
        self.mode_combo.setToolTip(
            ""
            if available
            else wrapped_tooltip(
                reason or "Adaptive needs at least one verified Auto-UV profile; run an "
                "Auto-UV scan first."
            )
        )

    def _sync_target_controls(self, adaptive: bool) -> None:
        game = self._selected_game()
        adaptive = adaptive and (game is None or game.overlay_supported)
        per_game = bool(self.per_game_target_switch.isChecked())
        self.per_game_target_switch.setEnabled(adaptive)
        self.target_fps_spin.setEnabled(adaptive and per_game)
        self.target_follow_label.setText(
            ""
            if per_game
            else f"follows system-wide ({adaptive_target_fps_from_env():g})"
        )

    # -- actions -------------------------------------------------------------

    def _selected_game(self) -> LibraryGame | None:
        return self._game_for_key(self._selected_key)

    def _manager_for(self, launcher: str):
        source = self._by_launcher.get(launcher)
        return getattr(source, "manager", None)

    def _write(self, game: LibraryGame | None, method: str, *args) -> bool:
        """Send one setting to the launcher that owns the game.

        Every launcher's manager names these the same way -- set_game_enabled,
        set_game_mode and the rest -- which is why one pane drives all of them.
        The methods only one manager has arrive here too, by name, from that
        launcher's own field declaration.

        Returns whether the write landed, so an edit that was refused can be
        held rather than treated as saved.
        """
        if game is None:
            return True
        if self._write_busy:
            self._sync_status("Wait for the current save before editing commands.")
            return False
        manager = self._manager_for(game.launcher)
        setter = getattr(manager, method, None)
        if setter is None:
            return True
        # A command save runs at a focus boundary on Qt's thread. Never wait
        # for a background scan here; retain the draft so it can be retried.
        if not self._library_lock.acquire(blocking=False):
            self._sync_status("Library is refreshing. Try saving the command again shortly.")
            return False
        try:
            result = setter(game.game_id, *args)
            if bool(getattr(result, "ok", True)):
                self._after_write(game.launcher, result)
                return True
        except Exception as error:  # noqa: BLE001 - launcher save boundary
            self._sync_status(f"Command save failed ({type(error).__name__}: {error})")
            return False
        finally:
            self._library_lock.release()
        # A refused command remains visible; only a discrete control should
        # snap back to the stored value when its write did not land.
        if not self._pending_field:
            self._sync_selection(self._selected_game())
        self._sync_status(str(getattr(result, "message", "") or ""))
        return False

    def _write_async(self, game: LibraryGame | None, method: str, *args) -> bool:
        """Run a discrete setting write without blocking Qt's event loop.

        A launcher write can include filesystem verification or a live-client
        round trip. Doing either in a toggle callback leaves Qt unable to paint
        the new switch position. Switches and choices come through here; typed
        commands keep their synchronous save-boundary contract so a refused
        edit can remain in the field that owns it.
        """
        if game is None:
            return True
        manager = self._manager_for(game.launcher)
        setter = getattr(manager, method, None)
        if setter is None:
            return True
        request = _SettingWriteRequest(
            game=game,
            method=method,
            args=tuple(args),
        )
        if self._write_busy:
            # Controls stay live while the launcher finishes its previous
            # write. Keep their latest intent and serialize manager access;
            # replacing an older queued value prevents a quick off/on/off
            # gesture from replaying every intermediate position.
            key = (game.launcher, game.game_id, method)
            for index, queued in enumerate(self._setting_queue):
                queued_key = (
                    queued.game.launcher,
                    queued.game.game_id,
                    queued.method,
                )
                if queued_key == key:
                    self._setting_queue[index] = request
                    break
            else:
                self._setting_queue.append(request)
            self._sync_status(f"{game.name}: saving…")
            return True
        self._start_setting_write(request)
        return True

    def _start_setting_write(self, request: _SettingWriteRequest) -> None:
        """Start one queued write; completion is always collected by Qt."""
        game = request.game
        manager = self._manager_for(game.launcher)
        setter = getattr(manager, request.method, None)
        if setter is None:
            self._setting_thread = None
            self._setting_result = _SettingWriteOutcome(game=game)
            self.QtCore.QTimer.singleShot(0, self._collect_setting_write)
            return
        source = self._by_launcher.get(game.launcher)
        self._setting_result = None
        self._set_write_busy(True)
        self._sync_status(f"{game.name}: saving…")

        def run() -> None:
            try:
                with self._library_lock:
                    result = setter(game.game_id, *request.args)
                    followup = None
                    problem = ""
                    if bool(getattr(result, "ok", True)) and source is not None:
                        after_write = getattr(source, "after_setting_write", None)
                        if callable(after_write):
                            try:
                                followup = after_write(game.game_id, request.method)
                            except Exception as error:  # noqa: BLE001 - live update boundary
                                problem = f"Setting saved; live update failed ({error})."
                        refresh_problem = self._refresh_source(source, deep=False)
                        problem = "; ".join(part for part in (problem, refresh_problem) if part)
                    self._setting_result = _SettingWriteOutcome(
                        game=game,
                        result=result,
                        followup=followup,
                        refresh_problem=problem,
                    )
            except Exception as error:  # noqa: BLE001 - launcher boundary
                self._setting_result = _SettingWriteOutcome(
                    game=game,
                    error=f"{type(error).__name__}: {error}",
                )

        self._setting_thread = threading.Thread(target=run, daemon=True)
        self._setting_thread.start()
        # Never join here, even briefly: ToggleSwitch starts its 130 ms
        # animation in the same signal delivery, and Qt needs this callback to
        # return before it can paint the first frame.
        self.QtCore.QTimer.singleShot(0, self._collect_setting_write)

    def _set_write_busy(self, busy: bool) -> None:
        """Gate structural actions that could race the launcher worker.

        Setting controls deliberately remain live. Their writes are queued by
        ``_write_async``, while greying a switch here hid its checked colour
        until Steam's verification delay ended.
        """
        self._write_busy = bool(busy)
        interactive = not self._write_busy
        self.rescan_button.setEnabled(interactive)
        self.all_games_button.setEnabled(interactive and bool(self._games))
        self.write_action_button.setEnabled(interactive)
        for widgets in self._fields.values():
            self._apply_field_enabled(widgets)
        if not interactive:
            self.play_button.setEnabled(False)

    def _collect_setting_write(self) -> None:
        """Finish a launcher write on the Qt thread."""
        thread = self._setting_thread
        if thread is not None and thread.is_alive():
            self.QtCore.QTimer.singleShot(100, self._collect_setting_write)
            return
        outcome = self._setting_result
        self._setting_thread = None
        self._setting_result = None
        if outcome is not None:
            if outcome.error:
                self._write_problems.append(
                    f"{outcome.game.name}: save failed ({outcome.error})"
                )
            elif not bool(getattr(outcome.result, "ok", True)):
                self._write_problems.append(str(getattr(outcome.result, "message", "")))
            if outcome.refresh_problem:
                self._write_problems.append(outcome.refresh_problem)
            if not bool(getattr(outcome.followup, "ok", True)):
                self._write_problems.append(str(getattr(outcome.followup, "message", "")))
        if self._setting_queue:
            # Do not refill the pane between queued optimistic changes: doing
            # so would visibly rewind a switch to the first persisted value
            # before the user's latest value is written.
            request = self._setting_queue.pop(0)
            self._start_setting_write(request)
            return
        self._set_write_busy(False)
        problems = "; ".join(self._write_problems)
        self._write_problems.clear()
        if outcome is None:
            self._sync_selection(self._selected_game())
            self._sync_status("Setting write returned no result.")
            return
        result = outcome.result
        self._reload_games()
        self._sync_selection(self._selected_game())
        self._sync_status(
            problems
            or str(getattr(outcome.followup, "message", "") or "")
            or str(getattr(result, "message", "") or "")
        )

    def _after_write(self, launcher: str, result) -> None:
        source = self._by_launcher.get(launcher)
        # The cheap pass: the write already updated the manager's own caches,
        # so disk state is enough to re-read -- the deep pass would sweep
        # every app over CDP, which on the GUI thread was the window frozen
        # after each toggle.
        problem = (
            self._refresh_source(source, deep=False) if source is not None else ""
        )
        self._reload_games()
        # Re-read the pane, always. The list only rebuilds when a game's name
        # or state changed, but a write is precisely what changes the selected
        # game's own fields -- a latency toggle rewrites the launch command
        # while leaving every list-visible fact alone, and the command field
        # would otherwise keep showing the value from before the toggle until
        # the user clicked away and back.
        self._sync_selection(self._selected_game())
        self._sync_status(problem or str(getattr(result, "message", "") or ""))

    def _on_enabled(self, enabled: bool) -> None:
        if self._syncing:
            return
        if (
            enabled
            and len(self._gpu_choices) >= 2
            and not str(self.gpu_combo.currentData() or "").strip()
        ):
            signals_were_blocked = self.enable_switch.blockSignals(True)
            try:
                self.enable_switch.setChecked(False)
            finally:
                self.enable_switch.blockSignals(signals_were_blocked)
            self._set_gated(False)
            self._sync_status("Choose a Game GPU before enabling PenguinBurner.")
            return
        # Mirror the optimistic switch immediately. In particular, turning
        # wrapping on should reveal live tuning controls now, not after
        # Steam's offline verification pause.
        self._set_gated(bool(enabled) and self._writable)
        self._write_async(self._selected_game(), "set_game_enabled", bool(enabled))

    def _on_mode_changed(self) -> None:
        if self._syncing:
            return
        mode = str(self.mode_combo.currentData() or GAME_MODE_ADAPTIVE)
        self._write_async(self._selected_game(), "set_game_mode", mode)

    def _on_per_game_target_toggled(self, per_game: bool) -> None:
        if self._syncing:
            return
        target = float(self.target_fps_spin.value()) if per_game else None
        self._write_async(self._selected_game(), "set_game_target_fps", target)

    def _on_target_fps_changed(self) -> None:
        if self._syncing or not self.per_game_target_switch.isChecked():
            return
        self._write_async(
            self._selected_game(),
            "set_game_target_fps",
            float(self.target_fps_spin.value()),
        )

    def _on_gpu_changed(self) -> None:
        if self._syncing:
            return
        self._write_async(
            self._selected_game(),
            "set_game_gpu",
            str(self.gpu_combo.currentData() or ""),
        )

    def _on_overlay(self, overlay: bool) -> None:
        if self._syncing:
            return
        self._write_async(
            self._selected_game(), "set_game_overlay", bool(overlay)
        )

    # -- library-wide actions -------------------------------------------------

    def _merged_bulk_actions(self):
        """One menu entry per action key, applied to every launcher with it.

        Two libraries but one "disable everything": launchers that declare the
        same key share the entry, and a launcher that declares a key nobody
        else does simply gets its own.
        """
        merged: dict[str, tuple[Any, list[Any]]] = {}
        order: list[str] = []
        for source in self._sources:
            for action in source.bulk_actions():
                if action.key not in merged:
                    merged[action.key] = (action, [])
                    order.append(action.key)
                merged[action.key][1].append(source)
        return tuple((merged[key][0], tuple(merged[key][1])) for key in order)

    def _sync_bulk_menu(self) -> None:
        """Grey out a direction that would change nothing.

        "Enable all" with everything already enabled is not an action, it is a
        readout -- so the menu doubles as one. Which field an action sets is
        the action's own declaration, so this stays true for a launcher this
        tab has never seen.
        """
        for entry, action, sources in self._bulk_menu_entries:
            entry.setEnabled(self._bulk_would_change(action, sources))

    def _bulk_scope(self, action, sources):
        """The games an action would touch: its launchers', and in its scope."""
        wanted = {source.launcher_id for source in sources}
        return [
            game
            for game in self._games
            if game.launcher in wanted and (game.enabled or not action.enabled_only)
        ]

    def _bulk_would_change(self, action, sources) -> bool:
        scope = self._bulk_scope(action, sources)
        if not scope:
            return False
        if not action.affects:
            return True
        return any(
            bool(getattr(game, action.affects, None)) != action.value
            for game in scope
        )

    def _bulk_ids(self, action, sources) -> dict[str, list[str]]:
        ids: dict[str, list[str]] = {}
        for game in self._bulk_scope(action, sources):
            ids.setdefault(game.launcher, []).append(game.game_id)
        return ids

    def _bulk_apply(self, action, sources) -> None:
        if self._write_busy:
            return
        if not self._flush_pending_field_edit():
            return
        ids = self._bulk_ids(action, sources)
        count = sum(len(entries) for entries in ids.values())
        if not count:
            return
        # Only enabling the wrapper needs a GPU chosen, and only for the games
        # this action would actually touch: the overlay action was refused on
        # multi-GPU hosts because some unrelated game had no card picked.
        if (
            action.affects == "enabled"
            and action.value
            and self._needs_gpu_choice(self._bulk_scope(action, sources))
        ):
            self._sync_status(
                "Choose a Game GPU for each game before enabling all games."
            )
            return
        if action.confirm:
            text = action.confirm.format(
                count=count, games="game" if count == 1 else "games"
            )
            if not self._confirm("All games", text):
                return
        game = self._bulk_scope(action, sources)[0]
        self._setting_result = None
        self._set_write_busy(True)
        self._sync_status(f"Saving changes for {count} games…")

        def run() -> None:
            messages: list[str] = []
            ok = True
            for source in sources:
                entries = ids.get(source.launcher_id) or []
                if not entries:
                    continue
                setter = getattr(getattr(source, "manager", None), action.setter, None)
                if setter is None:
                    continue
                try:
                    with self._library_lock:
                        result = setter(entries, action.value)
                        ok = bool(getattr(result, "ok", True)) and ok
                        message = str(getattr(result, "message", "") or "")
                        if message:
                            messages.append(f"{source.launcher_id}: {message}")
                except Exception as error:  # noqa: BLE001 - launcher boundary
                    ok = False
                    messages.append(f"{source.launcher_id}: {type(error).__name__}: {error}")
                problem = self._refresh_source(source, deep=False)
                if problem:
                    ok = False
                    messages.append(problem)
            self._setting_result = _SettingWriteOutcome(
                game=game, result=_BulkWriteResult(ok, "; ".join(messages))
            )

        self._setting_thread = threading.Thread(target=run, daemon=True)
        self._setting_thread.start()
        self.QtCore.QTimer.singleShot(0, self._collect_setting_write)

    def _needs_gpu_choice(self, games) -> bool:
        """Enabling games at once needs each of them to have a card first.

        With one card there is nothing to choose; with two, a wrapper that does
        not know which one to tune is a setting the user has to revisit anyway.
        """
        if len(self._gpu_choices) < 2:
            return False
        return any(
            not str(getattr(getattr(game.detail, "setting", None), "gpu_uuid", "") or "")
            for game in games
        )

    # -- launcher-declared fields --------------------------------------------
    #
    # The tab draws these without knowing what they are. A compatibility tool,
    # a prefix command and any future launcher-only option differ in their
    # label, control and manager method -- so those are what the launcher
    # declares, and nothing here names a launcher.

    def _sync_fields(self, game: LibraryGame | None) -> None:
        # An unsaved edit for this same game is the user's, not the model's:
        # refilling it here (a write on another control refreshes the whole
        # pane) would silently discard what they typed. Selection changes
        # flush before they get here, so a held edit is always same-game.
        hold = (
            self._pending_field
            if game is not None and game_key(game) == self._field_owner
            else ""
        )
        source = self._by_launcher.get(game.launcher) if game is not None else None
        declared = tuple(source.fields(game)) if source is not None else ()
        shown: set[str] = set()
        for field in declared:
            widgets = self._fields.get(field.key)
            if widgets is None:
                widgets = self._build_field(field)
                if widgets is None:
                    continue
                self._fields[field.key] = widgets
            if field.key != hold:
                self._fill_field(field, widgets)
            shown.add(field.key)
        for key, widgets in self._fields.items():
            if key not in shown:
                widgets["row"].setVisible(False)
        self._field_owner = game_key(game) if game is not None else ""
        self._pending_field = hold
        self._restripe_groups()

    def _build_field(self, field) -> dict[str, Any] | None:
        rows = self._field_rows.get(field.group)
        if rows is None:
            return None
        object_name = f"gameField_{field.key}"
        # Seeded non-empty: the row builders only make a subtitle label when
        # they have something to put in it, and every caption below is
        # re-texted per game.
        caption_seed = field.subtitle or " "
        if field.kind == FIELD_SWITCH:
            control = make_toggle_switch(
                QtCore=self.QtCore,
                QtGui=self.QtGui,
                QtWidgets=self.QtWidgets,
                object_name=object_name,
            )
            control.toggled.connect(
                lambda checked, key=field.key: self._on_field_toggled(key, checked)
            )
            row = preference_row(
                QtWidgets=self.QtWidgets,
                QtCore=self.QtCore,
                rows_layout=rows,
                title=field.title,
                subtitle=caption_seed,
                control=control,
            )
        elif field.kind == FIELD_CHOICE:
            control = self.QtWidgets.QComboBox()
            control.setObjectName(object_name)
            # Proton build names run long, and a truncated one is unreadable
            # exactly where the user is choosing between two of them.
            control.setMinimumWidth(_CHOICE_MIN_WIDTH)
            control.currentIndexChanged.connect(
                lambda _index, key=field.key: self._on_field_chosen(key)
            )
            row = preference_row(
                QtWidgets=self.QtWidgets,
                QtCore=self.QtCore,
                rows_layout=rows,
                title=field.title,
                subtitle=caption_seed,
                control=control,
            )
        elif field.kind in (FIELD_TEXT, FIELD_MULTILINE):
            if field.kind == FIELD_MULTILINE:
                control = self.QtWidgets.QPlainTextEdit("")
                # A launch line runs long. Wrapping costs a little height and
                # saves the user scrolling sideways through their own command.
                control.setLineWrapMode(self.QtWidgets.QPlainTextEdit.WidgetWidth)
                control.setFixedHeight(_MULTILINE_HEIGHT)
                control.textChanged.connect(
                    lambda key=field.key: self._on_field_typed(key)
                )
                # QPlainTextEdit has no editingFinished, so focus leaving the
                # box is the save boundary a plain line edit gets for free.
                original_focus_out = control.focusOutEvent

                def _focus_out(
                    event, key=field.key, original=original_focus_out
                ) -> None:
                    original(event)
                    if self._pending_field == key:
                        self._flush_field(key)

                control.focusOutEvent = _focus_out
            else:
                control = self.QtWidgets.QLineEdit("")
                control.textEdited.connect(
                    lambda _text, key=field.key: self._on_field_typed(key)
                )
                control.editingFinished.connect(
                    lambda key=field.key: self._flush_field(key)
                )
            control.setObjectName(object_name)
            # Styled by property, not by name: there is more than one of these
            # now, and a launcher may declare another tomorrow.
            control.setProperty("launcherField", True)
            row, body = full_width_row(
                QtWidgets=self.QtWidgets,
                rows_layout=rows,
                title=field.title,
                subtitle=caption_seed,
            )
            body.addWidget(control)
        else:
            return None
        return {
            "row": row,
            "control": control,
            "caption": row.property("subtitleLabel"),
            "kind": field.kind,
            "group": field.group,
            "setter": field.setter,
            "rendered": None,
            "launcher_enabled": True,
        }

    def _fill_field(self, field, widgets: dict[str, Any]) -> None:
        """Put this game's value in, without the write handlers firing."""
        control = widgets["control"]
        widgets["setter"] = field.setter
        was_syncing = self._syncing
        self._syncing = True
        try:
            if field.kind == FIELD_SWITCH:
                control.setChecked(bool(field.value))
            elif field.kind == FIELD_CHOICE:
                control.clear()
                for data, label in field.choices:
                    control.addItem(label, data)
                control.setCurrentIndex(max(0, control.findData(field.value)))
            elif field.kind == FIELD_MULTILINE:
                control.setPlainText(str(field.value or ""))
            else:
                control.setText(str(field.value or ""))
        finally:
            self._syncing = was_syncing
        widgets["rendered"] = self._field_value(widgets)
        widgets["launcher_enabled"] = bool(field.enabled)
        caption = widgets["caption"]
        if caption is not None:
            caption.setText(field.subtitle)
            caption.setVisible(bool(field.subtitle))
        if field.tooltip:
            control.setToolTip(wrapped_tooltip(field.tooltip))
        widgets["row"].setVisible(True)
        self._apply_field_enabled(widgets)

    def _apply_field_enabled(self, widgets: dict[str, Any]) -> None:
        """Two vetoes, both real: the launcher's own, and the tab's write gate."""
        widgets["control"].setEnabled(
            bool(widgets.get("launcher_enabled", True))
            and self._writable
            and not (self._write_busy and widgets["kind"] in (FIELD_TEXT, FIELD_MULTILINE))
        )

    @staticmethod
    def _field_value(widgets: dict[str, Any]):
        control = widgets["control"]
        kind = widgets["kind"]
        if kind == FIELD_SWITCH:
            return bool(control.isChecked())
        if kind == FIELD_CHOICE:
            return control.currentData()
        if kind == FIELD_MULTILINE:
            return control.toPlainText()
        return control.text()

    def _restripe_groups(self) -> None:
        """Separators follow what is on screen, not what was built.

        Rows are made once and shown per launcher, so which row draws the top
        line changes as the user clicks between libraries.
        """
        for group_name, rows in self._field_rows.items():
            first = True
            for index in range(rows.count()):
                item = rows.itemAt(index)
                row = item.widget() if item is not None else None
                if row is None or row.isHidden():
                    continue
                if bool(row.property("hasSeparator")) == first:
                    row.setProperty("hasSeparator", not first)
                    row.style().unpolish(row)
                    row.style().polish(row)
                first = False
            if self._field_groups.get(group_name) is self._command_group:
                # Nothing but launcher fields lives in that group, so a
                # launcher declaring none would leave a heading over a gap.
                self._command_group.setVisible(not first)

    # -- writing a field back -------------------------------------------------

    def _on_field_toggled(self, key: str, checked: bool) -> None:
        if self._syncing:
            return
        self._write_field_async(key, bool(checked))

    def _on_field_chosen(self, key: str) -> None:
        if self._syncing:
            return
        widgets = self._fields.get(key)
        if widgets is None:
            return
        self._write_field_async(key, widgets["control"].currentData())

    def _on_field_typed(self, key: str) -> None:
        """Mark the edit pending; it saves at a boundary, never mid-keystroke.

        The boundaries are the ones a form has: focus leaving the field,
        Enter, clicking another game, or an action that must see the saved
        value (a rescan, a bulk action, the launcher's fix-it button). A
        debounce timer here used to commit half of ``gamescope -w 2560 --
        %comm`` the moment the user paused to think.
        """
        if self._syncing:
            return
        self._pending_field = key

    def _flush_field(self, key: str) -> bool:
        widgets = self._fields.get(key)
        if widgets is None:
            if self._pending_field == key:
                self._pending_field = ""
            return True
        value = self._field_value(widgets)
        if value == widgets["rendered"]:
            if self._pending_field == key:
                self._pending_field = ""
            return True  # focus moved through an untouched field
        ok = self._write_field(key, value)
        if ok:
            widgets["rendered"] = value
            if self._pending_field == key:
                self._pending_field = ""
        # A refused edit stays pending and stays on screen as typed: the
        # quiet rescan defers to it, and nothing overwrites it with the
        # stored value.
        return ok

    def _flush_pending_field_edit(self) -> bool:
        """Save an edit still in the field before something else disturbs it.

        A rescan, a bulk action or a launcher restart all replace what is on
        screen, and whatever the user typed but has not saved would go with it.
        """
        key = self._pending_field
        if not key:
            return True
        return self._flush_field(key)

    def _write_field(self, key: str, value) -> bool:
        """Send one launcher-declared field to the manager that owns it.

        Applied to the game the field was filled for, not to whatever is
        selected now: clicking another game moves focus before the selection
        changes, so an unguarded write would land the old text on the new game.
        """
        widgets = self._fields.get(key)
        game = self._game_for_key(self._field_owner)
        if widgets is None or game is None or not widgets.get("setter"):
            return True
        return self._write(game, str(widgets["setter"]), value)

    def _write_field_async(self, key: str, value) -> bool:
        """Asynchronous counterpart for launcher switches and choices."""
        widgets = self._fields.get(key)
        game = self._game_for_key(self._field_owner)
        if widgets is None or game is None or not widgets.get("setter"):
            return True
        return self._write_async(game, str(widgets["setter"]), value)

    # -- launch lifecycle ----------------------------------------------------
    #
    # Ported from the Steam tab, where it was Steam-specific because that was
    # the only tab that had it. Here it asks the launcher whether it can start
    # a game at all, so one that cannot -- a launcher with no API for it, or a
    # client that is not installed here -- simply shows no button.

    def _can_launch(self, game: LibraryGame | None) -> bool:
        if game is None:
            return False
        source = self._by_launcher.get(game.launcher)
        return bool(getattr(source, "can_launch", False))

    def _tracked_state(self, key: str) -> str:
        track = self._tracked.get(key)
        return track.state if track is not None else ""

    def _other_active_game(self) -> str:
        """Name of another game that is launching, running or stopping.

        One game at a time: an active game other than the selection blocks
        Play, across launchers rather than within one.
        """
        for key, track in self._tracked.items():
            if key == self._selected_key or track.state not in _ACTIVE_GAME_STATES:
                continue
            game = self._game_for_key(key)
            return game.name if game is not None else key
        return ""

    def _sync_play_button(self, game: LibraryGame | None) -> None:
        """One mutating button: Play -> Starting… -> Stop -> Stopping… -> Play.

        The button is the state display, so a click can only mean what the
        label shows; transitional states keep it disabled, and the grace
        deadlines in _apply_game_states guarantee they resolve back.
        """
        if not self._can_launch(game):
            self.play_button.setVisible(False)
            return
        self.play_button.setVisible(True)
        state = self._tracked_state(self._selected_key)
        blocking = self._other_active_game()
        if state == "launching":
            text, enabled, play_state = "Starting…", False, "starting"
        elif state == "running":
            text, enabled, play_state = "Stop", True, "running"
        elif state == "stopping":
            # Live, not greyed out. A launcher's stop is a request its game may
            # shrug off -- Lutris passes one SIGTERM on and only insists when
            # told twice -- so the second press has to stay available. Greying
            # the button here is what made a stop look like nothing happening.
            text, enabled, play_state = "Stopping…", True, "stopping"
        elif blocking:
            text, enabled, play_state = "Play", False, "idle"
        else:
            text, enabled, play_state = "Play", True, "idle"
        self.play_button.setText(text)
        self.play_button.setEnabled(
            enabled and not self._write_busy and self._stop_thread is None
        )
        self.play_button.setToolTip(
            wrapped_tooltip(
                f"{blocking} is still running. Stop it before launching another game."
            )
            if blocking and state not in _ACTIVE_GAME_STATES
            else ""
        )
        if self.play_button.property("playState") != play_state:
            self.play_button.setProperty("playState", play_state)
            style = self.play_button.style()
            style.unpolish(self.play_button)
            style.polish(self.play_button)

    def _play_stop_clicked(self, _checked: bool = False) -> None:
        game = self._selected_game()
        if game is None or not self._can_launch(game):
            return
        state = self._tracked_state(self._selected_key)
        if state in ("running", "stopping"):
            # Pressed again while a stop is pending: ask once more rather than
            # ignore it. Which signal that turns into is the launcher's affair.
            self._stop_game(game)
        elif state not in _ACTIVE_GAME_STATES and not self._other_active_game():
            self._play_game(game)

    def _play_game(self, game: LibraryGame) -> None:
        source = self._by_launcher.get(game.launcher)
        if source is None:
            return
        started, message = source.launch(game.game_id)
        if started:
            self._set_game_state(game_key(game), "launching")
        self._sync_status(f"{game.name}: {message}")

    def _stop_game(self, game: LibraryGame) -> None:
        source = self._by_launcher.get(game.launcher)
        if source is None or self._stop_thread is not None:
            return
        key = game_key(game)
        previous = self._tracked_state(key)
        self._stop_result = None

        def run() -> None:
            try:
                self._stop_result = source.stop(game.game_id)
            except Exception as error:  # noqa: BLE001 - launcher boundary
                self._stop_result = (False, f"Stop failed ({type(error).__name__}: {error})")

        self._stop_thread = threading.Thread(target=run, daemon=True)
        self._set_game_state(key, "stopping")
        self._sync_status(f"{game.name}: requesting stop…")
        self._stop_thread.start()
        self.QtCore.QTimer.singleShot(0, lambda: self._collect_stop(game, previous))

    def _collect_stop(self, game: LibraryGame, previous: str) -> None:
        if self._stop_thread is not None and self._stop_thread.is_alive():
            self.QtCore.QTimer.singleShot(100, lambda: self._collect_stop(game, previous))
            return
        stopped, message = self._stop_result or (False, "Stop returned no result.")
        self._stop_thread = None
        self._stop_result = None
        key = game_key(game)
        # A polling result may already have confirmed exit while stop waited.
        if self._tracked_state(key) == "stopping":
            self._set_game_state(key, "stopping" if stopped else previous)
        self._sync_play_button(self._selected_game())
        self._sync_status(f"{game.name}: {message}")

    def _set_game_state(self, key: str, state: str) -> None:
        grace = {"launching": _PENDING_LAUNCH_S, "stopping": _PENDING_STOP_S}
        self._tracked[key] = _TrackedGame(
            state, time.monotonic() + grace.get(state, 0.0)
        )
        if state in _ACTIVE_GAME_STATES and not self._state_timer.isActive():
            self._state_timer.start()
        self._sync_play_button(self._selected_game())

    def _poll_game_states(self) -> None:
        """Ask each launcher which of its games are running, off the GUI thread.

        The Steam check shells out to pgrep -- a flatpak-spawn host round-trip
        inside a Flatpak -- and can stall for seconds, which would freeze the
        window if it ran here.
        """
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        sources = [
            source
            for source in self._sources
            if getattr(source, "can_launch", False)
            and hasattr(source, "running_game_ids")
        ]
        self._poll_result = {}

        def run() -> None:
            for source in sources:
                self._poll_result[source.launcher_id] = source.running_game_ids()

        self._poll_thread = threading.Thread(target=run, daemon=True)
        self._poll_thread.start()
        self._collect_poll()

    def _collect_poll(self) -> None:
        thread = self._poll_thread
        if thread is not None and thread.is_alive():
            self.QtCore.QTimer.singleShot(100, self._collect_poll)
            return
        self._apply_game_states(dict(self._poll_result))

    def _apply_game_states(self, running_by_launcher: dict) -> None:
        """Advance each tracked game from what its launcher reports.

        A launcher answering None means the check failed this tick: hold every
        one of its states rather than misread a stalled probe as every game
        having exited.
        """
        now = time.monotonic()
        for key, track in self._tracked.items():
            if track.state not in _ACTIVE_GAME_STATES:
                continue
            launcher, _, game_id = key.partition(":")
            running = running_by_launcher.get(launcher)
            if running is None:
                continue
            if game_id in running:
                track.misses = 0
                if track.state == "launching":
                    track.state = "running"
                elif track.state == "stopping" and now > track.deadline:
                    # The launcher declined to stop it (a save dialog, a hung
                    # shutdown): say so and re-arm Stop.
                    track.state = "running"
                    self._state_message(key, "did not stop; still running")
                continue
            track.misses += 1
            if track.state == "launching":
                # Not appearing yet is normal while a game spins up; only an
                # expired grace window means the launch is dead.
                if now > track.deadline:
                    track.state = "stopped"
                    self._state_message(key, "never started")
            elif track.misses >= _CONFIRMED_GONE_POLLS:
                # Consecutive polls agree, so one failed check cannot
                # misreport a live game as gone.
                track.state = "stopped"
                self._state_message(key, "stopped")
        if not any(
            track.state in _ACTIVE_GAME_STATES for track in self._tracked.values()
        ):
            self._state_timer.stop()
        self._sync_play_button(self._selected_game())

    def _state_message(self, key: str, event: str) -> None:
        game = self._game_for_key(key)
        self._sync_status(f"{game.name if game is not None else key}: {event}.")
