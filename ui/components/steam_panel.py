"""Steam tab: a library list and one focused per-game launcher.

The left pane is only for choosing an installed game. The right pane owns the
single editable Steam command line, Auto-UV mode, overlay toggle, and Play
button for that selection. Changes still apply immediately through
``integrations.steam.manager`` and persist per Steam account.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
import textwrap
import threading
import time

from common.flatpak_wrappers import ensure_steam_integration
from integrations.steam.manager import SteamGameRow, SteamIntegrationManager
from integrations.steam.launch_options import injection_state
from integrations.steam.process import launch_steam_game, restart_steam
from integrations.steam.settings import (
    GAME_MODE_ADAPTIVE,
    GAME_MODE_STOCK,
    normalize_game_mode,
)
from profiles.uv.profile_tiers import PROFILE_TIER_LABELS, PROFILE_TIERS
from runtime.frame_history import read_frame_history_for_app, summarize
from runtime.support.adaptive_target_fps import (
    MAX_ADAPTIVE_TARGET_FPS,
    MIN_ADAPTIVE_TARGET_FPS,
    adaptive_target_fps_from_env,
)
from ui.components.frame_dna import (
    FrameDnaPeek,
    dna_axes,
    dna_pixmap,
    warming_text,
)


# Wrapper enablement is separate from mode selection. The menu contains only
# Adaptive, concrete profile tiers, and per-game Stock: the standing profile
# can stay tuned system-wide (e.g. Balanced) while this one game pins the
# factory GPU state for its whole session.
_MODE_LABELS = {
    GAME_MODE_ADAPTIVE: "Adaptive",
    **PROFILE_TIER_LABELS,
    GAME_MODE_STOCK: "Stock (factory GPU state)",
}
_MODE_KEYS = (
    GAME_MODE_ADAPTIVE,
    *PROFILE_TIERS,
    GAME_MODE_STOCK,
)

SORT_RECENT = "recent"
SORT_ALPHABETICAL = "alphabetical"
SORT_INSTALLED = "installed"

_AUTO_SYNC_INTERVAL_MS = 10000
_ROW_HEIGHT = 42

# Per-game launch lifecycle, tracked only for games the user played from this
# panel. "launching" (play clicked, session not seen yet) reads as Running per
# the visible contract; it exists so a slow start is not mistaken for an exit.
_GAME_STATE_POLL_MS = 1500
_PENDING_LAUNCH_S = 120.0  # for the Steam session to appear after Play
_PENDING_STOP_S = 30.0  # for a stop request to take effect
_CONFIRMED_GONE_POLLS = 2  # consecutive not-running polls before Stopped
_GAME_STATE_LABELS = {
    "launching": "Running",
    "running": "Running",
    "stopping": "Stopping",
    "stopped": "Stopped",
}
_ACTIVE_GAME_STATES = ("launching", "running", "stopping")


@dataclass
class _TrackedGame:
    """Launch lifecycle of one game the user played from this panel."""

    state: str
    deadline: float = 0.0  # monotonic grace cutoff while launching/stopping
    misses: int = 0  # consecutive polls that did not see the session


def _wrapped_tooltip(text: str) -> str:
    return "\n".join(textwrap.wrap(text, width=72))


def _mode_keys_for_standing_mode(standing_mode_label: str) -> tuple[str, ...]:
    _ = standing_mode_label
    return _MODE_KEYS


def sorted_steam_rows(
    rows: tuple[SteamGameRow, ...], sort_mode: str
) -> tuple[SteamGameRow, ...]:
    """Sort the visible library; games without a timestamp trail the rest."""
    if sort_mode == SORT_ALPHABETICAL:
        return tuple(
            sorted(rows, key=lambda row: (row.game.name.casefold(), row.game.app_id))
        )
    if sort_mode == SORT_INSTALLED:
        return tuple(
            sorted(
                rows,
                key=lambda row: (
                    0 if row.game.last_updated > 0 else 1,
                    -row.game.last_updated,
                    row.game.name.casefold(),
                    row.game.app_id,
                ),
            )
        )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                0 if row.game.last_played > 0 else 1,
                -row.game.last_played,
                row.game.name.casefold(),
                row.game.app_id,
            ),
        )
    )


def last_played_text(timestamp: int) -> str:
    if timestamp <= 0:
        return "Never played on this PC"
    try:
        played = datetime.fromtimestamp(timestamp).astimezone()
    except (OSError, OverflowError, ValueError):
        return "Last played: unknown"
    return f"Last played: {played:%Y-%m-%d %H:%M}"


class SteamPanel:
    def __init__(
        self,
        *,
        QtCore,
        QtGui,
        QtWidgets,
        manager: SteamIntegrationManager | None = None,
        adaptive_available=None,
    ):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.manager = manager if manager is not None else SteamIntegrationManager()
        self.adaptive_available = adaptive_available or (lambda: True)
        self._syncing = False
        self._scan_running = False
        self._rows: dict[str, SteamGameRow] = {}
        self._selected_app_id = ""
        self._restart_thread: threading.Thread | None = None
        self._restart_result: bool | None = None
        self._rescan_thread: threading.Thread | None = None
        self._rescan_rows: tuple[SteamGameRow, ...] | None = None
        self._live_ready = False
        self._compat_tool_live_ready = False
        self._tracked: dict[str, _TrackedGame] = {}
        self._game_poll_thread: threading.Thread | None = None
        self._game_poll_result: frozenset[str] | None = None
        self._auto_sync_thread: threading.Thread | None = None
        self._auto_sync_result: tuple[tuple[SteamGameRow, ...], dict] | None = None
        # Frame DNA: the window wires this to open the Frame DNA tab.
        self.on_open_frame_dna: (
            Callable[[str, str, float | None], None] | None
        ) = None
        self._frame_dna_summary = None
        self._frame_dna_power_limit = 0
        self._frame_dna_peek: FrameDnaPeek | None = None

        self.widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(12, 18, 12, 12)
        layout.setSpacing(10)

        header_row = QtWidgets.QHBoxLayout()
        header_row.setSpacing(8)
        self.user_label = QtWidgets.QLabel("")
        self.user_label.setToolTip(
            _wrapped_tooltip(
                "The Steam account currently logged in. Live apply always "
                "targets this account; per-game presets are stored per "
                "account, so switching Steam users never overwrites another "
                "account's presets."
            )
        )
        header_row.addWidget(self.user_label)
        header_row.addStretch(1)
        self.rescan_button = QtWidgets.QPushButton("Rescan library")
        self.rescan_button.setToolTip(
            _wrapped_tooltip(
                "Re-read the installed-game list, last-played timestamps, and "
                "each game's launch options from Steam. Newly discovered games "
                "keep PenguinBurner disabled, with Adaptive preselected and "
                "the overlay off."
            )
        )
        header_row.addWidget(self.rescan_button)
        layout.addLayout(header_row)

        self.setup_view = QtWidgets.QWidget()
        setup_view_layout = QtWidgets.QVBoxLayout(self.setup_view)
        setup_view_layout.setContentsMargins(24, 24, 24, 24)
        setup_view_layout.addStretch(1)

        self.setup_card = QtWidgets.QFrame()
        self.setup_card.setObjectName("steamLibrarySetupCard")
        self.setup_card.setMaximumWidth(620)
        setup_card_layout = QtWidgets.QVBoxLayout(self.setup_card)
        setup_card_layout.setContentsMargins(42, 36, 42, 36)
        setup_card_layout.setSpacing(16)

        self.setup_title = QtWidgets.QLabel("Set up your Steam library")
        self.setup_title.setObjectName("steamLibrarySetupTitle")
        self.setup_title.setAlignment(QtCore.Qt.AlignCenter)
        setup_card_layout.addWidget(self.setup_title)

        self.setup_label = QtWidgets.QLabel("")
        self.setup_label.setObjectName("steamLibrarySetupText")
        self.setup_label.setAlignment(QtCore.Qt.AlignCenter)
        self.setup_label.setWordWrap(True)
        setup_card_layout.addWidget(self.setup_label)

        setup_button_row = QtWidgets.QHBoxLayout()
        setup_button_row.addStretch(1)
        self.setup_button = QtWidgets.QPushButton("Scan my Steam library")
        self.setup_button.setObjectName("steamLibrarySetupButton")
        self.setup_button.setMinimumWidth(260)
        self.setup_button.setMinimumHeight(48)
        self._setup_action = "scan"
        setup_button_row.addWidget(self.setup_button)
        setup_button_row.addStretch(1)
        setup_card_layout.addLayout(setup_button_row)

        setup_view_layout.addWidget(self.setup_card, 0, QtCore.Qt.AlignCenter)
        setup_view_layout.addStretch(1)
        layout.addWidget(self.setup_view, 1)
        # Startup discovery runs in the background. Do not briefly paint an
        # onboarding state before _sync_header() knows it is actually needed.
        self.setup_view.setVisible(False)

        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter.setObjectName("steamLibrarySplitter")
        self.splitter.setChildrenCollapsible(False)

        self.library_pane = QtWidgets.QFrame()
        self.library_pane.setObjectName("steamLibraryPane")
        self.library_pane.setMinimumWidth(300)
        library_layout = QtWidgets.QVBoxLayout(self.library_pane)
        library_layout.setContentsMargins(10, 10, 10, 10)
        library_layout.setSpacing(8)

        library_title = QtWidgets.QLabel("Installed games")
        library_title.setObjectName("steamPaneTitle")
        library_layout.addWidget(library_title)

        sort_row = QtWidgets.QHBoxLayout()
        sort_row.setSpacing(6)
        sort_row.addWidget(QtWidgets.QLabel("Sort"))
        self.sort_combo = QtWidgets.QComboBox()
        self.sort_combo.setObjectName("steamGameSort")
        # Size to the longest label ("Recently installed") so it never clips.
        self.sort_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self.sort_combo.addItem("Recently played", SORT_RECENT)
        self.sort_combo.addItem("Recently installed", SORT_INSTALLED)
        self.sort_combo.addItem("Alphabetical", SORT_ALPHABETICAL)
        self.sort_combo.setToolTip(
            _wrapped_tooltip(
                "Recently played uses Steam's LastPlayed timestamp and Recently "
                "installed uses LastUpdated (set on install and each update), "
                "both from the installed app manifest. Games without a "
                "timestamp appear last."
            )
        )
        sort_row.addWidget(self.sort_combo, 1)
        self.all_games_button = QtWidgets.QToolButton()
        self.all_games_button.setObjectName("steamAllGamesButton")
        self.all_games_button.setText("All games")
        self.all_games_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.all_games_button.setToolTip(
            _wrapped_tooltip(
                "Bulk actions for every installed game: enable or disable the "
                "PenguinBurner wrapper, or show/hide the In-Game overlay for "
                "games that already have PenguinBurner enabled. Each action "
                "asks for confirmation first."
            )
        )
        all_games_menu = QtWidgets.QMenu(self.all_games_button)
        self.enable_all_action = all_games_menu.addAction(
            "Enable PenguinBurner for all games",
            lambda: self._bulk_set_enabled(True),
        )
        self.disable_all_action = all_games_menu.addAction(
            "Disable PenguinBurner for all games",
            lambda: self._bulk_set_enabled(False),
        )
        all_games_menu.addSeparator()
        self.overlay_all_action = all_games_menu.addAction(
            "Show In-Game overlay for enabled games",
            lambda: self._bulk_set_overlay(True),
        )
        self.overlay_none_action = all_games_menu.addAction(
            "Hide In-Game overlay for all games",
            lambda: self._bulk_set_overlay(False),
        )
        # Gray out no-op directions (e.g. "enable all" when everything is
        # already enabled) right before the menu opens.
        all_games_menu.aboutToShow.connect(self._sync_all_games_menu)
        self.all_games_button.setMenu(all_games_menu)
        sort_row.addWidget(self.all_games_button)
        library_layout.addLayout(sort_row)

        self.game_list = QtWidgets.QListWidget()
        self.game_list.setObjectName("steamGameList")
        self.game_list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.game_list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.game_list.setIconSize(QtCore.QSize(28, 28))
        self.game_list.setAlternatingRowColors(True)
        library_layout.addWidget(self.game_list, 1)
        self.splitter.addWidget(self.library_pane)
        self.splitter.setCollapsible(0, False)

        self.details_pane = QtWidgets.QFrame()
        self.details_pane.setObjectName("steamGameDetailsPane")
        details_layout = QtWidgets.QVBoxLayout(self.details_pane)
        details_layout.setContentsMargins(22, 18, 22, 18)
        details_layout.setSpacing(12)

        # Header: the game info (title, live status, last-played line) sits in
        # a block on the left, and the Play/Stop button sits right beside that
        # block — next to where the text ends, not floating in the far corner.
        title_row = QtWidgets.QHBoxLayout()
        title_row.setSpacing(18)
        info_column = QtWidgets.QVBoxLayout()
        info_column.setContentsMargins(0, 0, 0, 0)
        info_column.setSpacing(6)
        self.game_title = QtWidgets.QLabel("Select a game")
        self.game_title.setObjectName("steamGameTitle")
        self.game_title.setWordWrap(True)
        info_column.addWidget(self.game_title)
        self.game_status = QtWidgets.QLabel("")
        self.game_status.setObjectName("steamGameStatus")
        self.game_status.setVisible(False)
        info_column.addWidget(self.game_status)
        self.game_metadata = QtWidgets.QLabel("")
        self.game_metadata.setObjectName("steamGameMetadata")
        # Single line so the info block's width follows the last-played line and
        # the Play button sits right beside it.
        self.game_metadata.setWordWrap(False)
        info_column.addWidget(self.game_metadata)
        title_row.addLayout(info_column, 0)
        self.play_button = QtWidgets.QPushButton("Play")
        self.play_button.setObjectName("steamPlayButton")
        self.play_button.setProperty("playState", "idle")
        self.play_button.setMinimumWidth(104)
        self.play_button.setToolTip(
            _wrapped_tooltip(
                "Launch the selected game through Steam. The command line, "
                "Auto-UV mode, and overlay choice below are used for the "
                "launch. While the game runs, this same button becomes Stop "
                "— Steam's own clean shutdown."
            )
        )
        title_row.addWidget(self.play_button, 0, QtCore.Qt.AlignVCenter)
        self.frame_dna_badge = QtWidgets.QToolButton()
        self.frame_dna_badge.setObjectName("steamFrameDnaBadge")
        self.frame_dna_badge.setFixedSize(72, 72)
        self.frame_dna_badge.setIconSize(QtCore.QSize(64, 64))
        self.frame_dna_badge.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.frame_dna_badge.setVisible(False)
        self.frame_dna_badge.setAccessibleName(
            "Frame DNA — open the Frame DNA tab"
        )
        self.frame_dna_badge.clicked.connect(self._open_frame_dna)
        panel = self

        class _FrameDnaBadgeHover(QtCore.QObject):
            """Hover or keyboard focus peeks the fingerprint; any click
            falls through to open the tab."""

            def eventFilter(self, watched, event):
                event_type = event.type()
                if event_type in (
                    QtCore.QEvent.Type.Enter,
                    QtCore.QEvent.Type.FocusIn,
                ):
                    panel._show_frame_dna_peek()
                elif event_type in (
                    QtCore.QEvent.Type.Leave,
                    QtCore.QEvent.Type.FocusOut,
                    QtCore.QEvent.Type.MouseButtonPress,
                    QtCore.QEvent.Type.Hide,
                ):
                    panel._hide_frame_dna_peek()
                return False

        self._frame_dna_hover_filter = _FrameDnaBadgeHover(self.widget)
        self.frame_dna_badge.installEventFilter(self._frame_dna_hover_filter)
        title_row.addWidget(self.frame_dna_badge, 0, QtCore.Qt.AlignVCenter)
        title_row.addStretch(1)
        details_layout.addLayout(title_row)

        self.proton_label = QtWidgets.QLabel("Compatibility tool")
        self.proton_label.setObjectName("steamFieldLabel")
        details_layout.addWidget(self.proton_label)
        self.proton_combo = QtWidgets.QComboBox()
        self.proton_combo.setObjectName("steamCompatTool")
        self._proton_combo_default_tooltip = _wrapped_tooltip(
            "Keeps Steam's current choice by default. Selecting another entry "
            "uses Steam's own compatibility-tool setting for this game."
        )
        self.proton_combo.setToolTip(self._proton_combo_default_tooltip)
        details_layout.addWidget(self.proton_combo)

        command_label = QtWidgets.QLabel("Steam command line")
        command_label.setObjectName("steamFieldLabel")
        details_layout.addWidget(command_label)
        self.launch_edit = QtWidgets.QPlainTextEdit()
        self.launch_edit.setObjectName("steamLaunchOptions")
        self.launch_edit.setPlaceholderText("%command%")
        self.launch_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        self.launch_edit.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.launch_edit.setToolTip(
            _wrapped_tooltip(
                "The selected game's one Steam launch-options command line. "
                "Changes apply immediately and Steam remembers them for every launch."
            )
        )
        details_layout.addWidget(self.launch_edit)

        self.mode_label = QtWidgets.QLabel("Auto-UV mode")
        self.mode_label.setObjectName("steamFieldLabel")
        details_layout.addWidget(self.mode_label)
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.setObjectName("steamAutoUvMode")
        for key in _MODE_KEYS:
            self.mode_combo.addItem(_MODE_LABELS[key], key)
        details_layout.addWidget(self.mode_combo)

        target_fps_tooltip = _wrapped_tooltip(
            "Adaptive Auto-UV target base-present FPS for this game. "
            "PenguinBurner uses it to decide when adaptive profiles should "
            "promote or demote. Range is 15 to 1000 FPS, default 60 FPS. "
            "Changes update a running game live."
        )
        self.target_fps_label = QtWidgets.QLabel("Adaptive target FPS")
        self.target_fps_label.setObjectName("steamFieldLabel")
        self.target_fps_label.setToolTip(target_fps_tooltip)
        details_layout.addWidget(self.target_fps_label)
        self.target_fps_spin = QtWidgets.QDoubleSpinBox()
        self.target_fps_spin.setObjectName("steamAdaptiveTargetFps")
        self.target_fps_spin.setRange(
            MIN_ADAPTIVE_TARGET_FPS,
            MAX_ADAPTIVE_TARGET_FPS,
        )
        self.target_fps_spin.setDecimals(0)
        self.target_fps_spin.setSingleStep(1.0)
        self.target_fps_spin.setSuffix(" FPS")
        self.target_fps_spin.setValue(adaptive_target_fps_from_env())
        self.target_fps_spin.setToolTip(target_fps_tooltip)
        # Size the box for its widest value ("1000 FPS") instead of letting it
        # stretch across the whole details pane.
        _fps_width = (
            self.target_fps_spin.fontMetrics().horizontalAdvance("1000 FPS") + 48
        )
        self.target_fps_spin.setFixedWidth(_fps_width)
        target_fps_row = QtWidgets.QHBoxLayout()
        target_fps_row.setContentsMargins(0, 0, 0, 0)
        target_fps_row.addWidget(self.target_fps_spin)
        target_fps_row.addStretch(1)
        details_layout.addLayout(target_fps_row)

        self.enabled_checkbox = QtWidgets.QCheckBox(
            "Enable PenguinBurner per-game profiles"
        )
        self.enabled_checkbox.setObjectName("steamPenguinBurnerToggle")
        details_layout.addWidget(self.enabled_checkbox)

        self.overlay_checkbox = QtWidgets.QCheckBox("Enable In-Game overlay")
        self.overlay_checkbox.setObjectName("steamOverlayToggle")
        details_layout.addWidget(self.overlay_checkbox)
        self.telemetry_checkbox = QtWidgets.QCheckBox("Capture game telemetry")
        self.telemetry_checkbox.setObjectName("steamTelemetryToggle")
        self.telemetry_checkbox.setToolTip(
            _wrapped_tooltip(
                "Record this game's Frame DNA telemetry (a rolling 30-minute "
                "window of frametimes, clocks, power, and the active tier) "
                "while it runs. On by default; turning it off stops new "
                "capture from the next launch."
            )
        )
        details_layout.addWidget(self.telemetry_checkbox)

        details_layout.addStretch(1)
        self.splitter.addWidget(self.details_pane)
        self.splitter.setCollapsible(1, False)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([370, 760])
        layout.addWidget(self.splitter, 1)

        self.status_label = QtWidgets.QLabel("")
        layout.addWidget(self.status_label)

        self.rescan_button.clicked.connect(self.rescan)
        self.setup_button.clicked.connect(self._setup_steam_library)
        self.sort_combo.currentIndexChanged.connect(self._sort_changed)
        self.game_list.currentItemChanged.connect(self._selection_changed)
        self.proton_combo.currentIndexChanged.connect(self._proton_changed)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.target_fps_spin.valueChanged.connect(self._target_fps_changed)
        self.enabled_checkbox.toggled.connect(self._enabled_changed)
        self.overlay_checkbox.toggled.connect(self._overlay_changed)
        self.telemetry_checkbox.toggled.connect(self._telemetry_changed)
        self._launch_edit_timer = QtCore.QTimer(self.widget)
        self._launch_edit_timer.setSingleShot(True)
        self._launch_edit_timer.setInterval(600)
        self._launch_edit_timer.timeout.connect(self._launch_options_edited)
        self.launch_edit.textChanged.connect(self._launch_options_changed)
        self.play_button.clicked.connect(self._play_stop_clicked)

        self._sync_timer = QtCore.QTimer(self.widget)
        self._sync_timer.setInterval(_AUTO_SYNC_INTERVAL_MS)
        self._sync_timer.timeout.connect(self._auto_sync)
        self._sync_timer.start()

        self._game_state_timer = QtCore.QTimer(self.widget)
        self._game_state_timer.setInterval(_GAME_STATE_POLL_MS)
        self._game_state_timer.timeout.connect(self._poll_game_states)

        self._sync_selected_details()
        # Startup discovery is read-only. Only an explicit Rescan button click
        # records the disabled, Adaptive-preselected defaults for new games.
        self.rescan(initialize_defaults=False)

    # -- refresh ------------------------------------------------------------

    def rescan(
        self,
        _checked: bool = False,
        *,
        initialize_defaults: bool = True,
    ) -> None:
        """Refresh the library and Steam command lines outside the UI thread."""
        _ = _checked
        if self._rescan_thread is not None and self._rescan_thread.is_alive():
            return
        if not self._flush_pending_launch_edit():
            return
        self._scan_running = True
        self.rescan_button.setEnabled(False)
        self._sync_interaction_state()
        self._sync_status("Scanning library…")
        self._rescan_rows = None

        def run() -> None:
            self._rescan_rows = self.manager.refresh(
                read_launch_options=True,
                initialize_defaults=initialize_defaults,
            )

        self._rescan_thread = threading.Thread(target=run, daemon=True)
        self._rescan_thread.start()
        self._poll_rescan()

    def _poll_rescan(self) -> None:
        thread = self._rescan_thread
        if thread is not None and thread.is_alive():
            self.QtCore.QTimer.singleShot(200, self._poll_rescan)
            return
        self._scan_running = False
        self.rescan_button.setEnabled(True)
        if self._rescan_rows is not None:
            self._populate(self._rescan_rows)
        self._sync_header()

    def _auto_sync(self) -> None:
        """Track installs and updated LastPlayed values without a full CDP read.

        The library refresh (disk VDF reads) and header probe (pgrep + a CDP
        HTTP connect, ~6 s worst case) run on a worker thread so a slow Steam
        never freezes the window; the UI apply happens back on the GUI thread.
        """
        if self._rescan_thread is not None and self._rescan_thread.is_alive():
            return
        if self._auto_sync_thread is not None and self._auto_sync_thread.is_alive():
            return
        if not self._flush_pending_launch_edit():
            return
        self._auto_sync_result = None

        def run() -> None:
            self._auto_sync_result = (
                self.manager.refresh(read_launch_options=False),
                self._probe_header(),
            )

        self._auto_sync_thread = threading.Thread(target=run, daemon=True)
        self._auto_sync_thread.start()
        self._collect_auto_sync()

    def _collect_auto_sync(self) -> None:
        thread = self._auto_sync_thread
        if thread is not None and thread.is_alive():
            self.QtCore.QTimer.singleShot(150, self._collect_auto_sync)
            return
        result = self._auto_sync_result
        if result is None:
            return
        rows, probe = result
        if self._row_signature(rows) != self._row_signature(tuple(self._rows.values())):
            self._populate(rows)
        else:
            self._sync_default_mode_label()
            # Ring data grows while a game runs even when the rows are
            # unchanged, so the fingerprint badge refreshes on every sync.
            self._sync_frame_dna_badge(self._rows.get(self._selected_app_id))
        self._sync_header(probe)

    @staticmethod
    def _row_signature(rows: tuple[SteamGameRow, ...]) -> tuple[tuple[object, ...], ...]:
        return tuple(
            (
                row.game.app_id,
                row.game.name,
                row.game.last_played,
                row.game.icon_path,
                row.game.compat_tool,
                row.game.effective_compat_tool,
                row.game.effective_compat_tool_display,
                row.setting,
                row.launch_options,
            )
            for row in rows
        )

    def _populate(self, rows: tuple[SteamGameRow, ...]) -> None:
        previous_app_id = self._selected_app_id or self._current_app_id()
        self._rows = {row.game.app_id: row for row in rows}
        self._sync_default_mode_label()
        self._refresh_game_list(preferred_app_id=previous_app_id)
        self._sync_status()

    def _sync_default_mode_label(self) -> None:
        mode_keys = _mode_keys_for_standing_mode("")
        current_mode = normalize_game_mode(self.mode_combo.currentData())
        if current_mode not in mode_keys:
            current_mode = GAME_MODE_ADAPTIVE
        signals_were_blocked = self.mode_combo.blockSignals(True)
        try:
            self.mode_combo.clear()
            for key in mode_keys:
                self.mode_combo.addItem(_MODE_LABELS[key], key)
            self.mode_combo.setCurrentIndex(mode_keys.index(current_mode))
        finally:
            self.mode_combo.blockSignals(signals_were_blocked)

        adaptive_ok = bool(self.adaptive_available())
        adaptive_index = self.mode_combo.findData(GAME_MODE_ADAPTIVE)
        if adaptive_index >= 0:
            item = self.mode_combo.model().item(adaptive_index)
            if item is not None:
                item.setEnabled(adaptive_ok)
        if not adaptive_ok:
            self.mode_combo.setToolTip(
                _wrapped_tooltip(
                    "Adaptive needs at least one verified Auto-UV profile; run "
                    "an Auto-UV scan first."
                )
            )
        else:
            self.mode_combo.setToolTip("")

    def _refresh_game_list(self, *, preferred_app_id: str = "") -> None:
        sort_mode = str(self.sort_combo.currentData() or SORT_RECENT)
        rows = sorted_steam_rows(tuple(self._rows.values()), sort_mode)
        selected_item = None
        self._syncing = True
        try:
            self.game_list.clear()
            for row in rows:
                item = self.QtWidgets.QListWidgetItem(row.game.name)
                item.setData(self.QtCore.Qt.UserRole, row.game.app_id)
                item.setSizeHint(self.QtCore.QSize(0, _ROW_HEIGHT))
                if row.game.icon_path is not None:
                    item.setIcon(self.QtGui.QIcon(str(row.game.icon_path)))
                runtime = row.game.runtime_label
                if row.game.is_proton and row.game.effective_compat_tool_label:
                    runtime = row.game.effective_compat_tool_label
                item.setToolTip(
                    f"{row.game.name}\nApp {row.game.app_id} · {runtime}\n"
                    f"{last_played_text(row.game.last_played)}"
                )
                self.game_list.addItem(item)
                if row.game.app_id == preferred_app_id:
                    selected_item = item
            if selected_item is None and self.game_list.count():
                selected_item = self.game_list.item(0)
            self.game_list.setCurrentItem(selected_item)
        finally:
            self._syncing = False
        self._sync_selected_details()

    # -- selected game ------------------------------------------------------

    def _current_app_id(self) -> str:
        item = self.game_list.currentItem()
        return str(item.data(self.QtCore.Qt.UserRole) or "") if item is not None else ""

    def _selection_changed(self, _current, _previous) -> None:
        if self._syncing:
            return
        if not self._flush_pending_launch_edit(self._selected_app_id):
            signals_were_blocked = self.game_list.blockSignals(True)
            try:
                self.game_list.setCurrentItem(_previous)
            finally:
                self.game_list.blockSignals(signals_were_blocked)
            return
        self._sync_selected_details()

    def _sort_changed(self, _index: int) -> None:
        if self._syncing:
            return
        if not self._flush_pending_launch_edit():
            return
        self._refresh_game_list(preferred_app_id=self._current_app_id())

    def _sync_selected_details(self) -> None:
        app_id = self._current_app_id()
        row = self._rows.get(app_id)
        self._selected_app_id = app_id if row is not None else ""
        was_syncing = self._syncing
        self._syncing = True
        try:
            if row is None:
                self.game_title.setText("Select a game")
                self.game_metadata.setText("")
                self.proton_combo.clear()
                self.proton_combo.addItem("Steam default", "")
                self.launch_edit.clear()
                self.mode_combo.setCurrentIndex(
                    max(0, self.mode_combo.findData(GAME_MODE_ADAPTIVE))
                )
                self.target_fps_spin.setValue(adaptive_target_fps_from_env())
                self.enabled_checkbox.setChecked(False)
                self.overlay_checkbox.setChecked(False)
                self.telemetry_checkbox.setChecked(True)
            else:
                self.game_title.setText(row.game.name)
                self.game_metadata.setText(
                    f"App {row.game.app_id} · {row.game.runtime_label} · "
                    f"{last_played_text(row.game.last_played)}"
                )
                tools = self.manager.available_compat_tools(row.game.app_id)
                self.proton_combo.clear()
                default_label = "Steam default"
                if not row.game.compat_tool:
                    effective = row.game.effective_compat_tool_label
                    if effective:
                        default_label = f"Steam default ({effective})"
                    elif row.game.is_native_linux:
                        default_label = "Native Linux — no compatibility tool"
                self.proton_combo.addItem(default_label, "")
                for name, display in tools:
                    self.proton_combo.addItem(display, name)
                compat_index = self.proton_combo.findData(row.game.compat_tool)
                if row.game.compat_tool and compat_index < 0:
                    self.proton_combo.addItem(row.game.compat_tool, row.game.compat_tool)
                    compat_index = self.proton_combo.count() - 1
                self.proton_combo.setCurrentIndex(max(0, compat_index))
                self.launch_edit.setPlainText(row.launch_options)
                cursor = self.launch_edit.textCursor()
                cursor.setPosition(0)
                self.launch_edit.setTextCursor(cursor)
                self._resize_launch_editor()
                mode_index = self.mode_combo.findData(normalize_game_mode(row.setting.mode))
                self.mode_combo.setCurrentIndex(max(0, mode_index))
                self.target_fps_spin.setValue(
                    row.setting.target_fps
                    if row.setting.target_fps is not None
                    else adaptive_target_fps_from_env()
                )
                self.enabled_checkbox.setChecked(row.setting.enabled)
                self.overlay_checkbox.setChecked(row.setting.overlay)
                self.telemetry_checkbox.setChecked(row.setting.telemetry)
        finally:
            self._syncing = was_syncing
        self._sync_frame_dna_badge(row)
        self._sync_interaction_state()

    def _badge_device_pixel_ratio(self) -> float:
        window = self.widget.window()
        handle = window.windowHandle() if window is not None else None
        if handle is not None:
            return float(handle.devicePixelRatio())
        return 1.0

    def _sync_frame_dna_badge(self, row: SteamGameRow | None) -> None:
        """Refresh the fingerprint badge for the selected game's ring."""
        badge = self.frame_dna_badge
        if row is None:
            self._frame_dna_summary = None
            self._hide_frame_dna_peek()
            badge.setVisible(False)
            return
        history = read_frame_history_for_app(row.game.app_id)
        summary = summarize(history) if history is not None else None
        self._frame_dna_power_limit = (
            history.header.power_limit_w if history is not None else 0
        )
        ratio = self._badge_device_pixel_ratio()
        if summary is not None and summary.qualified:
            self._frame_dna_summary = summary
            axes = dna_axes(
                summary,
                target_fps=row.setting.target_fps,
                power_limit_w=self._frame_dna_power_limit,
            )
            pixmap = dna_pixmap(
                self.QtCore, self.QtGui, axes=axes, tier=summary.tier,
                size=64, device_pixel_ratio=ratio,
            )
            badge.setToolTip("")
        else:
            self._frame_dna_summary = None
            self._hide_frame_dna_peek()
            pixmap = dna_pixmap(
                self.QtCore, self.QtGui, axes=None, tier="",
                size=64, device_pixel_ratio=ratio,
            )
            if summary is not None:
                badge.setToolTip(
                    _wrapped_tooltip(f"Frame DNA — {warming_text(summary.minutes)}")
                )
            else:
                badge.setToolTip(
                    _wrapped_tooltip(
                        "No Frame DNA yet. Play this game with PenguinBurner "
                        "running to record its telemetry fingerprint."
                    )
                )
        badge.setIcon(self.QtGui.QIcon(pixmap))
        badge.setVisible(True)

    def _show_frame_dna_peek(self) -> None:
        summary = self._frame_dna_summary
        row = self._rows.get(self._selected_app_id)
        if summary is None or row is None:
            return
        if self._frame_dna_peek is None:
            self._frame_dna_peek = FrameDnaPeek(
                QtCore=self.QtCore,
                QtGui=self.QtGui,
                QtWidgets=self.QtWidgets,
                parent=self.widget,
            )
        badge = self.frame_dna_badge
        below = badge.mapToGlobal(badge.rect().bottomLeft())
        below.setY(below.y() + 6)
        self._frame_dna_peek.show_for(
            game_name=row.game.name,
            summary=summary,
            target_fps=row.setting.target_fps,
            power_limit_w=self._frame_dna_power_limit,
            global_pos=below,
            device_pixel_ratio=self._badge_device_pixel_ratio(),
        )

    def _hide_frame_dna_peek(self) -> None:
        if self._frame_dna_peek is not None:
            self._frame_dna_peek.hide()

    def _open_frame_dna(self) -> None:
        row = self._rows.get(self._selected_app_id)
        self._hide_frame_dna_peek()
        if row is None or self.on_open_frame_dna is None:
            return
        self.on_open_frame_dna(
            row.game.app_id, row.game.name, row.setting.target_fps
        )

    def _sync_interaction_state(self) -> None:
        has_selection = bool(self._selected_app_id and self._selected_app_id in self._rows)
        self.game_list.setEnabled(not self._scan_running)
        self.sort_combo.setEnabled(not self._scan_running)
        restart_running = (
            self._restart_thread is not None and self._restart_thread.is_alive()
        )
        self.setup_button.setEnabled(not self._scan_running and not restart_running)
        editable = has_selection and self._live_ready and not self._scan_running
        self.all_games_button.setEnabled(
            bool(self._rows) and self._live_ready and not self._scan_running
        )
        self._sync_all_games_menu()
        self.launch_edit.setEnabled(has_selection)
        self.launch_edit.setReadOnly(not editable)
        self.enabled_checkbox.setEnabled(editable)
        wrapper_enabled = editable and self.enabled_checkbox.isChecked()
        self.mode_label.setEnabled(wrapper_enabled)
        self.mode_combo.setEnabled(wrapper_enabled)
        self.target_fps_label.setEnabled(wrapper_enabled)
        self.target_fps_spin.setEnabled(wrapper_enabled)
        # Native Linux games run without a compatibility tool; changing Proton
        # for them is not a PenguinBurner workflow, so the selector grays out.
        # A game already forced onto Proton keeps the selector until the user
        # reverts it to Steam default (through Steam) — no lock-in.
        selected_row = self._rows.get(self._selected_app_id)
        selected_is_native = (
            selected_row is not None and selected_row.game.is_native_linux
        )
        proton_enabled = (
            has_selection
            and self._compat_tool_live_ready
            and not self._scan_running
            and not selected_is_native
        )
        self.proton_combo.setEnabled(proton_enabled)
        self.proton_label.setEnabled(proton_enabled or not has_selection)
        if selected_is_native:
            self.proton_combo.setToolTip(
                _wrapped_tooltip(
                    "Steam reports that this game runs natively on Linux "
                    "without a compatibility tool. To force Proton for it, "
                    "use Steam's game "
                    "properties; the selector unlocks once a tool is set."
                )
            )
        else:
            self.proton_combo.setToolTip(self._proton_combo_default_tooltip)
        self.overlay_checkbox.setEnabled(wrapper_enabled)
        self.telemetry_checkbox.setEnabled(wrapper_enabled)
        # The Play/Stop button state (including availability) is derived from
        # the selected game's lifecycle in _sync_game_status.
        self._sync_game_status()

    def _sync_game_status(self) -> None:
        """Show the launch lifecycle of the selected game under its title.

        Only games the user played from this panel carry a state; everything
        else keeps the status line hidden.
        """
        track = self._tracked.get(self._selected_app_id)
        state = track.state if track is not None else ""
        label = _GAME_STATE_LABELS.get(state, "")
        self.game_status.setText(label)
        self.game_status.setVisible(bool(label))
        if self.game_status.property("gameState") != state:
            self.game_status.setProperty("gameState", state)
            style = self.game_status.style()
            style.unpolish(self.game_status)
            style.polish(self.game_status)
        self._sync_play_stop_button(state)

    def _sync_play_stop_button(self, state: str) -> None:
        """One mutating Steam-style button: Play -> Starting… -> Stop ->
        Stopping… -> Play.

        The button is the state display, so a click can only mean what the
        label shows: transitional states keep it disabled (TerminateApp on a
        game Steam has not spawned yet is a silent no-op that would strand
        the tracker in "stopping", and a second Play during a slow launch is
        noise). All transitions run on the GUI thread; the launch/stop grace
        deadlines in _apply_game_states guarantee a transitional state always
        resolves back to Play or Stop.
        """
        has_selection = bool(
            self._selected_app_id and self._selected_app_id in self._rows
        )
        blocking = self._other_active_game_name()
        if state == "launching":
            text, enabled, play_state = "Starting…", False, "starting"
        elif state == "running":
            text, enabled, play_state = "Stop", True, "running"
        elif state == "stopping":
            text, enabled, play_state = "Stopping…", False, "stopping"
        elif blocking:
            # Only one game may run at a time: another game is already
            # launching/running/stopping, so Play is blocked here until it
            # exits.
            text, enabled, play_state = "Play", False, "idle"
        else:
            # Playing does not mutate Steam configuration, so it remains
            # available even before live launch-option apply is initialized.
            text, enabled, play_state = "Play", has_selection, "idle"
        self.play_button.setToolTip(
            _wrapped_tooltip(f"{blocking} is still running. Stop it before launching another game.")
            if blocking and state not in _ACTIVE_GAME_STATES
            else ""
        )
        self.play_button.setText(text)
        self.play_button.setEnabled(enabled)
        if self.play_button.property("playState") != play_state:
            self.play_button.setProperty("playState", play_state)
            style = self.play_button.style()
            style.unpolish(self.play_button)
            style.polish(self.play_button)

    def _selected_game_state(self) -> str:
        track = self._tracked.get(self._selected_app_id)
        return track.state if track is not None else ""

    def _other_active_game_name(self) -> str:
        """Name of another game that is launching/running/stopping, if any.

        Only one game is allowed active at a time, so an active game other
        than the selection blocks Play. Empty when nothing else is active."""
        for app_id, track in self._tracked.items():
            if app_id == self._selected_app_id:
                continue
            if track.state in _ACTIVE_GAME_STATES:
                row = self._rows.get(app_id)
                return row.game.name if row is not None else app_id
        return ""

    def _play_stop_clicked(self, _checked: bool = False) -> None:
        # Dispatch strictly on the tracked state so the click always performs
        # the action the button displayed; transitional states are disabled
        # and fall through to a no-op even if triggered programmatically.
        state = self._selected_game_state()
        if state == "running":
            self._stop_game()
        elif state not in _ACTIVE_GAME_STATES and not self._other_active_game_name():
            self._play_game()

    # -- header / status ----------------------------------------------------

    def _probe_header(self) -> dict:
        """Gather the header's live signals (may block: pgrep + CDP HTTP).

        Kept separate from the UI apply so the 10 s auto-sync can run it on a
        worker thread instead of freezing the window while Steam is slow.
        """
        marker = self.manager.marker_present()
        running = self.manager.steam_running()
        return {
            "user": self.manager.active_user(),
            "marker": marker,
            "running": running,
            "cdp_ready": self.manager.cdp_ready() if marker and running else False,
        }

    def _sync_header(self, probe: dict | None = None) -> None:
        if probe is None:
            probe = self._probe_header()
        user = probe["user"]
        self.user_label.setText(
            f"Steam user: {user.display_name}" if user is not None else "Steam user: —"
        )
        marker = probe["marker"]
        running = probe["running"]
        cdp_ready = probe["cdp_ready"]
        if not marker:
            self.setup_title.setText("Set up your Steam library")
            self.setup_label.setText(
                "Find your installed games and connect them to PenguinBurner "
                "for simple per-game profiles."
            )
            self.setup_button.setText("Scan my Steam library")
            self._setup_action = "scan"
            show_setup = True
        elif running and not cdp_ready:
            self.setup_title.setText("Your library is ready")
            self.setup_label.setText(
                "Restart Steam once to finish connecting per-game profiles. "
                "Your games and saved settings stay exactly as they are."
            )
            self.setup_button.setText("Restart Steam to finish")
            self._setup_action = "restart"
            show_setup = True
        else:
            show_setup = False
        self.setup_view.setVisible(show_setup)
        self.splitter.setVisible(not show_setup)
        self.rescan_button.setVisible(not show_setup)
        self.status_label.setVisible(not show_setup)
        self._live_ready = cdp_ready or not running
        self._compat_tool_live_ready = cdp_ready
        self._sync_interaction_state()
        self._sync_status()

    def _sync_status(self, message: str = "") -> None:
        configured = sum(
            1
            for row in self._rows.values()
            if injection_state(row.launch_options).wrapped
        )
        parts = [f"{len(self._rows)} games", f"{configured} configured"]
        if self._live_ready:
            parts.append("live apply" if self.manager.steam_running() else "Steam stopped")
        else:
            parts.append("read-only until initialized")
        if message:
            parts.append(message)
        self.status_label.setText(" · ".join(parts))

    # -- edit handlers -----------------------------------------------------

    def _mode_changed(self, _index: int) -> None:
        if self._syncing:
            return
        if not self._flush_pending_launch_edit():
            return
        app_id = self._current_app_id()
        if not app_id:
            return
        result = self.manager.set_game_mode(app_id, self.mode_combo.currentData())
        hot = self.manager.hot_reapply(app_id) if result.ok else None
        self._after_apply(app_id, result, extra=hot)

    def _sync_all_games_menu(self) -> None:
        """Each bulk action stays clickable only while it would change
        something: no mass-enable when everything is already enabled, no
        overlay actions without a PenguinBurner-enabled game."""
        rows = self._rows.values()
        enabled_rows = [row for row in rows if row.setting.enabled]
        self.enable_all_action.setEnabled(len(enabled_rows) < len(self._rows))
        self.disable_all_action.setEnabled(bool(enabled_rows))
        self.overlay_all_action.setEnabled(
            any(not row.setting.overlay for row in enabled_rows)
        )
        self.overlay_none_action.setEnabled(
            any(row.setting.overlay for row in enabled_rows)
        )

    def _bulk_set_enabled(self, enabled: bool) -> None:
        if not self._flush_pending_launch_edit():
            return
        app_ids = list(self._rows)
        if not app_ids:
            return
        count = len(app_ids)
        games_word = "game" if count == 1 else "games"
        if enabled:
            text = (
                f"Add the PenguinBurner wrapper to the launch options of "
                f"{count} {games_word}?\n\n"
                "The In-Game overlay stays off, and MangoHud is disabled in "
                'wrapped games. "Disable PenguinBurner for all games" '
                "restores the original launch options."
            )
        else:
            text = (
                f"Remove the PenguinBurner wrapper from {count} {games_word} "
                "and restore their original Steam launch options?"
            )
        if not self._confirm_bulk("All games", text):
            return
        self._after_bulk_apply(self.manager.set_all_games_enabled(app_ids, enabled))

    def _bulk_set_overlay(self, overlay: bool) -> None:
        if not self._flush_pending_launch_edit():
            return
        app_ids = [
            app_id for app_id, row in self._rows.items() if row.setting.enabled
        ]
        if not app_ids:
            self._sync_status("No PenguinBurner-enabled games to change.")
            return
        count = len(app_ids)
        games_word = "game" if count == 1 else "games"
        verb = "Show" if overlay else "Hide"
        if not self._confirm_bulk(
            "All games",
            f"{verb} the In-Game overlay in {count} PenguinBurner-enabled "
            f"{games_word}?",
        ):
            return
        self._after_bulk_apply(self.manager.set_all_games_overlay(app_ids, overlay))

    def _confirm_bulk(self, title: str, text: str) -> bool:
        QtWidgets = self.QtWidgets
        answer = QtWidgets.QMessageBox.question(
            self.widget,
            title,
            text,
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        return answer == QtWidgets.QMessageBox.Yes

    def _after_bulk_apply(self, result) -> None:
        rows = self.manager.refresh(read_launch_options=False)
        self._populate(rows)
        prefix = "" if result.ok else "FAILED: "
        self._sync_status(f"{prefix}{result.message}")

    def _target_fps_changed(self, value: float) -> None:
        if self._syncing:
            return
        if not self._flush_pending_launch_edit():
            return
        app_id = self._current_app_id()
        if not app_id:
            return
        result = self.manager.set_game_target_fps(app_id, float(value))
        hot = self.manager.hot_reapply(app_id) if result.ok else None
        self._after_apply(app_id, result, extra=hot)

    def _enabled_changed(self, checked: bool) -> None:
        if self._syncing:
            return
        if not self._flush_pending_launch_edit():
            return
        app_id = self._current_app_id()
        if not app_id:
            return
        result = self.manager.set_game_enabled(app_id, checked)
        hot = self.manager.hot_reapply(app_id) if result.ok else None
        self._after_apply(app_id, result, extra=hot)

    def _proton_changed(self, _index: int) -> None:
        if self._syncing:
            return
        if not self._flush_pending_launch_edit():
            return
        app_id = self._current_app_id()
        if not app_id:
            return
        tool_name = str(self.proton_combo.currentData() or "")
        result = self.manager.set_game_compat_tool(app_id, tool_name)
        if not result.ok:
            self._sync_status(f"Proton: FAILED ({result.message})")
            self._sync_selected_details()
            return
        row = self._rows.get(app_id)
        if row is not None:
            self._rows[app_id] = SteamGameRow(
                game=replace(row.game, compat_tool=tool_name),
                setting=row.setting,
                launch_options=row.launch_options,
            )
        self._sync_status(result.message)

    def _telemetry_changed(self, checked: bool) -> None:
        if self._syncing:
            return
        app_id = self._selected_app_id
        if not app_id:
            return
        result = self.manager.set_game_telemetry(app_id, bool(checked))
        self._after_apply(app_id, result)

    def _overlay_changed(self, checked: bool) -> None:
        if self._syncing:
            return
        if not self._flush_pending_launch_edit():
            return
        app_id = self._current_app_id()
        if not app_id:
            return
        result = self.manager.set_game_overlay(app_id, checked)
        if result.ok and self.manager.game_running(app_id):
            # The native layer polls the override file about once a second,
            # so the running game's overlay follows the checkbox live — no
            # restart. The wrapper clears the override on the next launch,
            # where the persisted launch options take over again.
            from overlay.state import write_overlay_override

            if write_overlay_override(bool(checked)):
                row = self._rows.get(app_id)
                name = row.game.name if row is not None else app_id
                self._after_apply(app_id, result)
                self._sync_status(
                    f"{name}: overlay switched "
                    f"{'on' if checked else 'off'} live in the running game."
                )
                return
        self._after_apply(app_id, result)

    def _launch_options_edited(self) -> bool:
        if self._syncing:
            return True
        return self._save_launch_options(self._current_app_id())

    def _save_launch_options(self, app_id: str) -> bool:
        row = self._rows.get(app_id)
        text = self.launch_edit.toPlainText().strip()
        if row is None or text == row.launch_options:
            return True
        result = self.manager.set_raw_launch_options(app_id, text)
        if not result.ok:
            self._sync_status(f"{row.game.name}: FAILED: {result.message}")
            return False
        self._rows[app_id] = SteamGameRow(
            game=row.game,
            setting=row.setting,
            launch_options=text,
        )
        self._sync_status(f"{row.game.name}: {result.message}")
        return True

    def _flush_pending_launch_edit(self, app_id: str | None = None) -> bool:
        if not self._launch_edit_timer.isActive():
            return True
        self._launch_edit_timer.stop()
        return self._save_launch_options(app_id or self._current_app_id())

    def _launch_options_changed(self) -> None:
        self._resize_launch_editor()
        if not self._syncing:
            self._launch_edit_timer.start()

    def _resize_launch_editor(self) -> None:
        document_height = int(self.launch_edit.document().size().height())
        self.launch_edit.setFixedHeight(max(72, document_height + 18))

    def _play_game(self, _checked: bool = False) -> None:
        if not self._flush_pending_launch_edit():
            return
        app_id = self._current_app_id()
        row = self._rows.get(app_id)
        if row is None:
            return
        try:
            ensure_steam_integration()
        except (OSError, RuntimeError) as exc:
            self._sync_status(
                f"{row.game.name}: FAILED to repair the PenguinBurner Steam "
                f"integration ({exc})"
            )
            return
        if launch_steam_game(app_id):
            self._set_game_state(app_id, "launching")
            self._sync_status(f"{row.game.name}: launching via Steam…")
        else:
            self._sync_status(
                f"{row.game.name}: FAILED to launch (steam not available)"
            )

    def _stop_game(self, _checked: bool = False) -> None:
        app_id = self._current_app_id()
        row = self._rows.get(app_id)
        if row is None:
            return
        result = self.manager.stop_game(app_id)
        if result.ok:
            self._set_game_state(app_id, "stopping")
            self._sync_status(f"{row.game.name}: stopping…")
        else:
            self._sync_status(f"{row.game.name}: FAILED to stop ({result.message})")

    def _set_game_state(self, app_id: str, state: str) -> None:
        grace = {"launching": _PENDING_LAUNCH_S, "stopping": _PENDING_STOP_S}
        self._tracked[app_id] = _TrackedGame(
            state, time.monotonic() + grace.get(state, 0.0)
        )
        if state in _ACTIVE_GAME_STATES and not self._game_state_timer.isActive():
            self._game_state_timer.start()
        self._sync_game_status()

    def _poll_game_states(self) -> None:
        """Kick off the running-games check on a worker thread.

        The check shells out to pgrep (a flatpak-spawn host round-trip under
        Flatpak) and can stall for seconds; running it on the GUI thread would
        freeze the window, so it goes to a worker and the lifecycle transitions
        are applied back on the GUI thread when it returns.
        """
        if self._game_poll_thread is not None and self._game_poll_thread.is_alive():
            return
        self._game_poll_result = None

        def run() -> None:
            self._game_poll_result = self.manager.running_game_ids()

        self._game_poll_thread = threading.Thread(target=run, daemon=True)
        self._game_poll_thread.start()
        self._collect_game_poll()

    def _collect_game_poll(self) -> None:
        thread = self._game_poll_thread
        if thread is not None and thread.is_alive():
            self.QtCore.QTimer.singleShot(100, self._collect_game_poll)
            return
        self._apply_game_states(self._game_poll_result)

    def _apply_game_states(self, running: frozenset[str] | None) -> None:
        """Advance each tracked game's lifecycle from the running-games set.

        ``running is None`` means the check failed this tick — hold every
        state (do not count a miss) rather than misread a stalled probe as
        every game having exited.
        """
        now = time.monotonic()
        for app_id, track in self._tracked.items():
            if track.state not in _ACTIVE_GAME_STATES:
                continue
            if running is None:
                continue
            if app_id in running:
                track.misses = 0
                if track.state == "launching":
                    track.state = "running"
                elif track.state == "stopping" and now > track.deadline:
                    # Steam declined to stop it (save dialog, hung shutdown);
                    # surface the truth and re-arm the Stop button.
                    track.state = "running"
                    self._transition_message(app_id, "did not stop; still running")
                continue
            track.misses += 1
            if track.state == "launching":
                # Not appearing yet is normal while Steam spins the game up;
                # only a fully expired grace window means the launch is dead
                # (cancelled or failed inside Steam).
                if now > track.deadline:
                    track.state = "stopped"
                    self._transition_message(app_id, "never started")
            elif track.misses >= _CONFIRMED_GONE_POLLS:
                # Consecutive polls agree the session is gone, so one failed
                # or timed-out process check cannot misreport a live game.
                track.state = "stopped"
                self._transition_message(app_id, "stopped")
        if not any(
            track.state in _ACTIVE_GAME_STATES for track in self._tracked.values()
        ):
            self._game_state_timer.stop()
        self._sync_game_status()

    def _transition_message(self, app_id: str, event: str) -> None:
        row = self._rows.get(app_id)
        self._sync_status(f"{row.game.name if row is not None else app_id}: {event}.")

    def _after_apply(self, app_id: str, result, extra=None) -> None:
        rows = self.manager.refresh(read_launch_options=False)
        self._selected_app_id = app_id
        self._populate(rows)
        row = self._rows.get(app_id)
        name = row.game.name if row is not None else app_id
        prefix = "" if result.ok else "FAILED: "
        message = f"{name}: {prefix}{result.message}"
        if extra is not None:
            message += f" {extra.message}"
        self._sync_status(message)

    # -- library setup / restart ------------------------------------------

    def _setup_steam_library(self) -> None:
        if self._setup_action == "restart":
            self._confirm_restart_steam()
            return
        result = self.manager.initialize()
        self._sync_header()
        self._sync_status(result.message)
        if result.ok:
            self.rescan(initialize_defaults=True)
        elif self.setup_view.isVisible():
            self.setup_label.setText(
                "PenguinBurner could not find your Steam installation. "
                "Open Steam once, then try again."
            )

    def _confirm_restart_steam(self) -> None:
        QtWidgets = self.QtWidgets
        if self._restart_thread is not None and self._restart_thread.is_alive():
            return
        answer = QtWidgets.QMessageBox.question(
            self.widget,
            "Restart Steam",
            "Cleanly shut down and relaunch the Steam client now?",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        if not self._flush_pending_launch_edit():
            self._sync_status("Steam restart cancelled: command-line save failed.")
            return
        self.setup_button.setEnabled(False)
        self.setup_label.setText(
            "Restarting Steam… Your games and saved settings will stay in place."
        )
        self._sync_status("Restarting Steam…")
        self._restart_result = None

        def run() -> None:
            self._restart_result = restart_steam()

        self._restart_thread = threading.Thread(target=run, daemon=True)
        self._restart_thread.start()
        self._poll_restart()

    def _poll_restart(self) -> None:
        thread = self._restart_thread
        if thread is not None and thread.is_alive():
            self.QtCore.QTimer.singleShot(500, self._poll_restart)
            return
        self._sync_header()
        self.setup_button.setEnabled(True)
        if self._restart_result:
            self._sync_status("Steam restarted.")
            if self.setup_view.isVisible():
                self.setup_label.setText(
                    "Steam is starting. Your library will open here automatically."
                )
        else:
            self._sync_status("Steam restart failed.")
            self.setup_label.setText(
                "Steam could not be restarted automatically. Restart it manually, "
                "then return here."
            )
