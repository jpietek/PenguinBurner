"""Game Library page: every launcher's games, laid out as the new window draws them.

The page is the classic ``GameLibraryPanel`` with the new window's layout on
top: a search field and folding filters over the list, a painted wrapped
marker, the page heading, and the Perf Profile entry beside Play. Every scan,
write, launch, session and write-state gate is still the panel's own code,
over the real launcher registry and the daemon's launcher-session stream.

``LibraryPage`` reports to the rest of the window through signals: the
selected game, a game session starting or ending, the window status line
while a game plays, and Capture telemetry changes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from integrations.launchers.library import FIELD_CHOICE, LibraryGame
from profiles.game_profile import (
    GAME_MODE_ADAPTIVE,
    GAME_MODE_STOCK,
    normalize_game_mode,
)
from profiles.uv.profile_store import read_auto_uv_profile_summaries
from profiles.uv.profile_tiers import (
    PROFILE_TIER_LABELS,
    PROFILE_TIERS,
    adaptive_tier_available,
)
from runtime.support.adaptive_target_fps import adaptive_target_fps_from_env
from ui.components.game_library_panel import GameLibraryPanel, game_key
from ui.components.library_style import (
    heading_into_card,
    library_stylesheet,
    normalize_group_headings,
)
from ui.components.page_header import PAGE_MARGINS
from ui.components.perf_entry import PerfEntryButton
from ui.components.perf_profile_plot import PerfProfileSource
from ui.design.style import COL, GAP, focus_visible
from ui.features.tuning.gpu_selection import detected_gpu_choices

DETAILS_MIN_WIDTH = 460
#: A painted wrapped marker replaces the classic row's ●/○ glyph.
WRAPPED_ROLE = int(C.Qt.ItemDataRole.UserRole) + 7
#: Tracked states that are a game session of this library: the wrapper
#: confirmed the game (running), or Stop was asked and has not landed yet.
SESSION_STATES = ("running", "stopping")
#: Modes whose wrapped session runs a profile, so the daemon records a ring.
RECORDING_MODES = (GAME_MODE_ADAPTIVE, *PROFILE_TIERS)
MODE_NAMES = {GAME_MODE_ADAPTIVE: "Adaptive", GAME_MODE_STOCK: "Stock", **PROFILE_TIER_LABELS}


def launcher_names_text(names: Iterable[str]) -> str:
    """The page heading's caption: the launchers the library merged. Pure."""
    names = [str(name) for name in names if name]
    if not names:
        return "No game launcher found"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def game_setting(game: LibraryGame | None) -> Any:
    return getattr(getattr(game, "detail", None), "setting", None)


def game_mode(game: LibraryGame | None) -> str:
    """The game's saved mode, read the way the panel's mode combo reads it."""
    mode = getattr(game_setting(game), "mode", GAME_MODE_ADAPTIVE)
    return normalize_game_mode(mode, default=GAME_MODE_ADAPTIVE)


def gpu_text(choices, gpu_uuid: str) -> str:
    """The GPU a game's profile applies to, for the status line. Pure."""
    choices = tuple(choices)
    if gpu_uuid:
        choice = next(
            (c for c in choices if str(getattr(c, "uuid", "") or "") == gpu_uuid), None
        )
    else:
        choice = choices[0] if len(choices) == 1 else None
    if choice is None:
        return ""
    name = str(getattr(choice, "name", "") or "").strip()
    index = getattr(choice, "index", None)
    return f"GPU {index} · {name}" if index is not None else name


class GameRowDelegate(W.QStyledItemDelegate):
    """The list row as the panel draws it, plus a painted 6 px marker on the
    right edge: an accent dot when wrapped, a quiet ring when not."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        wrapped = index.data(WRAPPED_ROLE)
        if wrapped is None:
            return
        painter.save()
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        center = C.QPointF(option.rect.right() - 14, option.rect.center().y())
        if wrapped:
            painter.setPen(C.Qt.PenStyle.NoPen)
            painter.setBrush(G.QColor(COL["accent"]))
        else:
            painter.setPen(G.QPen(G.QColor(COL["quiet"]), 1.2))
            painter.setBrush(C.Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, 3, 3)
        painter.restore()


def find_in_layout(layout, widget):
    """(layout, index) of the item holding widget, searching nested layouts."""
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item.widget() is widget:
            return layout, i
        if item.layout() is not None:
            found = find_in_layout(item.layout(), widget)
            if found:
                return found
    return None


class LibraryPanel(GameLibraryPanel):
    """The classic Game Library panel in the new window's layout.

    ``owner`` is the ``LibraryPage``: it holds the perf source and the
    signals this panel reports through.
    """

    def __init__(
        self,
        owner: LibraryPage,
        *,
        sources=None,
        gpu_choices=None,
        adaptive_available=None,
    ):
        self.owner = owner
        self._last_status = ""
        # Game sessions already reported, and each game's Capture telemetry
        # as last read, so changes are reported once.
        self._live_sessions: set[str] = set()
        self._capture_seen: dict[str, bool] = {}
        super().__init__(
            QtCore=C,
            QtGui=G,
            QtWidgets=W,
            sources=sources,
            gpu_choices=gpu_choices,
            adaptive_available=adaptive_available,
        )
        # The shared page margins; the list and details scroll in their panes.
        self.widget.layout().setContentsMargins(*PAGE_MARGINS)
        self._apply_library_heading()
        self.select_filters()
        self.splitter.setSizes([370, 800])

    # -- list pane -------------------------------------------------------------

    def _build_library(self):
        """Search, then Sort · Filters · All games, then the filters panel.

        The three filters fold away behind a Filters button that counts the
        active ones.
        """
        pane = super()._build_library()
        self.game_list.setItemDelegate(GameRowDelegate(self.game_list))
        self.search = W.QLineEdit()
        self.search.setAccessibleName("Search games")
        self.search.setPlaceholderText("Search games…")
        self.search.setClearButtonEnabled(True)
        self.launcher_filter = self._filter_combo("Launcher filter", [("", "All launchers")])
        self._fill_launcher_filter()
        self.wrap_filter = self._filter_combo(
            "Wrapper filter",
            [("", "Any setup"), ("on", "Wrapped"), ("off", "Unwrapped")],
        )
        self.record_filter = self._filter_combo(
            "Recording filter",
            [("", "Any history"), ("on", "Recorded"), ("off", "No recording")],
        )
        self.filters_button = W.QPushButton("Filters")
        self.filters_button.setObjectName("libraryFiltersButton")
        self.filters_button.setProperty("expanded", False)
        self.filters_button.setAccessibleDescription("Show or hide the list filters")
        self.filters_button.clicked.connect(self.toggle_filters)
        self.sort_combo.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToContents)
        found = find_in_layout(pane.layout(), self.sort_combo)
        assert found, "Sort is laid out in the list pane"
        actions, index = found
        actions.insertWidget(index + 1, self.filters_button)
        self.filters_panel = W.QFrame()
        self.filters_panel.setObjectName("libraryFilters")
        grid = W.QGridLayout(self.filters_panel)
        grid.setContentsMargins(GAP["md"], GAP["sm"], GAP["md"], GAP["sm"])
        grid.setHorizontalSpacing(GAP["md"])
        grid.setVerticalSpacing(GAP["sm"])
        for row, (title, combo) in enumerate(
            [
                ("Launcher", self.launcher_filter),
                ("Wrapper", self.wrap_filter),
                ("Recording", self.record_filter),
            ]
        ):
            label = W.QLabel(title)
            label.setObjectName("libraryFilterLabel")
            label.setBuddy(combo)
            grid.addWidget(label, row, 0)
            grid.addWidget(combo, row, 1, C.Qt.AlignmentFlag.AlignRight)
        clear = W.QPushButton("Clear filters")
        clear.setObjectName("libraryClearFilters")
        clear.clicked.connect(self.reset_filters)
        grid.addWidget(clear, 3, 0, 1, 2, C.Qt.AlignmentFlag.AlignLeft)
        grid.setColumnStretch(1, 1)
        self.filters_panel.hide()
        layout = pane.layout()
        layout.insertWidget(1, self.search)
        layout.insertWidget(layout.indexOf(self.game_list), self.filters_panel)
        self.search.textChanged.connect(self.select_filters)
        for combo in (self.launcher_filter, self.wrap_filter, self.record_filter):
            combo.currentIndexChanged.connect(self.select_filters)
        return pane

    @staticmethod
    def _filter_combo(name, items):
        combo = W.QComboBox()
        combo.setAccessibleName(name)
        combo.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToContents)
        for key, title in items:
            combo.addItem(title, key)
        return combo

    def _fill_launcher_filter(self) -> None:
        """One entry per installed launcher; a scan can add or drop one."""
        combo = self.launcher_filter
        selected = str(combo.currentData() or "")
        with C.QSignalBlocker(combo):
            while combo.count() > 1:
                combo.removeItem(1)
            for source in self._sources:
                combo.addItem(str(source.display_name), source.launcher_id)
            combo.setCurrentIndex(max(0, combo.findData(selected)))

    def toggle_filters(self):
        shown = not self.filters_panel.isVisible()
        self.filters_panel.setVisible(shown)
        self.filters_button.setProperty("expanded", shown)
        self.filters_button.style().unpolish(self.filters_button)
        self.filters_button.style().polish(self.filters_button)

    def reset_filters(self):
        with C.QSignalBlocker(self.search):
            self.search.clear()
        for combo in (self.launcher_filter, self.wrap_filter, self.record_filter):
            with C.QSignalBlocker(combo):
                combo.setCurrentIndex(0)
        self.select_filters()

    def fit_list_tools(self):
        """Size the list pane once the page style (caption-sized tools) is on:
        "Filters (3)" never squeezes the Sort combo, and the pane keeps the
        width its tools row needs."""
        pane = self.splitter.widget(0)
        for widget in [pane, *pane.findChildren(W.QWidget)]:
            widget.ensurePolished()
        # The three filter combos share one width, so their edges line up.
        combos = (self.launcher_filter, self.wrap_filter, self.record_filter)
        widest = max(combo.sizeHint().width() for combo in combos)
        for combo in combos:
            combo.setFixedWidth(widest)
        button = self.filters_button
        button.setMinimumWidth(
            button.fontMetrics().horizontalAdvance("Filters (3)") + 2 * GAP["sm"] + 4
        )
        width = max(300, pane.minimumSizeHint().width())
        pane.setMinimumWidth(width)
        self.splitter.setSizes([width, 800])

    def _active_filter_count(self):
        return sum(
            bool(combo.currentData())
            for combo in (self.launcher_filter, self.wrap_filter, self.record_filter)
        )

    def select_filters(self, *_):
        name = self.search.text().casefold()
        launcher = self.launcher_filter.currentData()
        wrapped = self.wrap_filter.currentData()
        recorded = self.record_filter.currentData()
        visible = []
        for i in range(self.game_list.count()):
            item = self.game_list.item(i)
            key = item.data(C.Qt.ItemDataRole.UserRole)
            game = self._game_for_key(key)
            if game is None:
                item.setHidden(True)
                continue
            show = (not launcher or game.launcher == launcher) and name in game.name.casefold()
            show = show and (not wrapped or game.enabled == (wrapped == "on"))
            show = show and (
                not recorded or self.owner.has_recording(key) == (recorded == "on")
            )
            item.setHidden(not show)
            if show:
                visible.append(key)
        if visible and self._selected_key not in visible:
            self._select_key(visible[0])
        elif not visible:
            self.game_list.clearSelection()
            self._selected_key = ""
            self._sync_selection(None)
        # The panel's own empty-library text stays; only a filter miss is ours.
        if self._games:
            self.placeholder_label.setVisible(not visible)
            self.placeholder_label.setText("No games match these filters.")
        count = self._active_filter_count()
        self.filters_button.setText(f"Filters ({count})" if count else "Filters")
        self.filters_button.setEnabled(bool(self._games))
        # Filtering changes no count the status line shows: keep its message.
        self._sync_status(self._last_status)

    def _sync_status(self, message: str = "") -> None:
        self._last_status = message
        super()._sync_status(message)

    @staticmethod
    def _row_label(game):
        # The wrapped marker is painted by GameRowDelegate, not a glyph.
        return game.name

    def _refresh_list(self):
        super()._refresh_list()
        # The panel mutes unwrapped rows; in the token colours here.
        for i in range(self.game_list.count()):
            item = self.game_list.item(i)
            game = self._game_for_key(item.data(C.Qt.ItemDataRole.UserRole))
            if game is not None:
                item.setForeground(G.QColor(COL["ink"] if game.enabled else COL["secondary"]))
                item.setData(WRAPPED_ROLE, bool(game.enabled))
        self.select_filters()

    def _reload_games(self) -> None:
        super()._reload_games()
        # A telemetry write, a bulk action or an edit in the launcher's own
        # settings: whichever changed a game's Capture telemetry, say so once.
        seen = {
            game_key(game): bool(getattr(game_setting(game), "telemetry", True))
            for game in self._games
        }
        changed = [
            (key, enabled)
            for key, enabled in seen.items()
            if key in self._capture_seen and self._capture_seen[key] != enabled
        ]
        self._capture_seen = seen
        for key, enabled in changed:
            self.owner.capture_changed.emit(key, enabled)

    # -- heading ---------------------------------------------------------------

    def _apply_library_heading(self):
        """The page heading: a title, then the launchers the library merged."""
        self.library_label.setText(
            launcher_names_text(source.display_name for source in self._sources)
        )
        header = find_in_layout(self.widget.layout(), self.library_label)
        assert header, "the launcher caption sits in the header row"
        row, index = header
        row.takeAt(index)
        heading = W.QVBoxLayout()
        heading.setSpacing(2)
        self.page_title = W.QLabel("Game Library")
        self.page_title.setObjectName("libraryPageTitle")
        self.page_title.hide()  # the tab names the page
        heading.addWidget(self.page_title)
        heading.addWidget(self.library_label)
        row.insertLayout(index, heading)
        row.setAlignment(self.rescan_button, C.Qt.AlignmentFlag.AlignVCenter)

    def _apply_sources(self, installed) -> None:
        changed = tuple(installed) != self._sources
        super()._apply_sources(installed)
        if changed:
            self.library_label.setText(
                launcher_names_text(source.display_name for source in self._sources)
            )
            self._fill_launcher_filter()

    # -- details pane ----------------------------------------------------------

    def _build_settings(self):
        pane = super()._build_settings()
        # A long title wraps; unwrapped, it set the page's minimum width and
        # pushed Play and the row controls past the card at narrow sizes.
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(W.QSizePolicy.Policy.Ignored, W.QSizePolicy.Policy.Preferred)
        self.title_label.setMinimumWidth(160)
        # "follows system-wide (60)" wraps rather than widening its row.
        self.target_follow_label.setWordWrap(True)
        self._details_pane = pane
        pane.setMinimumWidth(DETAILS_MIN_WIDTH)
        # The Perf Profile entry replaces the classic badge beside Play, in a
        # secondary style, with the game's own frametime sparkline as its icon.
        found = find_in_layout(self.play_button.parentWidget().layout(), self.play_button)
        assert found, "Play is laid out in the game header"
        hero, _index = found
        badge = self.perf_profile_badge.button
        hero.removeWidget(badge)
        badge.hide()
        self.perf_button = PerfEntryButton(self.owner.perf_source)
        self.perf_button.open_requested.connect(self.owner.open_perf_profile)
        hero.insertWidget(
            hero.indexOf(self.play_button), self.perf_button, 0, C.Qt.AlignmentFlag.AlignTop
        )
        return pane

    def _build_field(self, field):
        widgets = super()._build_field(field)
        # A choice sizes to its longest entry ("Native Linux — no compatibility
        # tool"); the row subtitle takes the slack.
        if widgets is not None and field.kind == FIELD_CHOICE:
            widgets["control"].setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToContents)
        return widgets

    def _select_key(self, key):
        previous = self._selected_key
        super()._select_key(key)
        if previous != self._selected_key:
            self.details_scroll.verticalScrollBar().setValue(0)

    def _sync_selection(self, game):
        super()._sync_selection(game)
        if game is None:
            self.perf_button.sync("")
        self._fit_details()
        self.owner.selected_game_changed.emit(game)

    def _sync_perf_profile_badge(self, game) -> None:
        """The entry beside Play stands in for the classic badge, on the same
        calls: each selection and each game-state poll (a live ring grows)."""
        setting = game_setting(game)
        self.perf_button.sync(
            game_key(game) if game is not None else "",
            capture=bool(getattr(setting, "telemetry", True)) if setting is not None else None,
        )

    def _fit_details(self):
        """The splitter never squeezes the details below what their rows need;
        the list gives way instead, so nothing runs past the card edge."""
        page = self.details_scroll.widget()
        page.adjustSize()
        need = (
            page.minimumSizeHint().width()
            + self.details_scroll.verticalScrollBar().sizeHint().width()
        )
        self._details_pane.setMinimumWidth(max(DETAILS_MIN_WIDTH, need))

    # -- game sessions -----------------------------------------------------------

    def _sync_play_button(self, game) -> None:
        super()._sync_play_button(game)
        # Every tracked-state change ends here (session snapshots, recovery
        # polls, Play, Stop), so this is where sessions are reported.
        self._sync_sessions()

    def _sync_sessions(self) -> None:
        active = {
            key
            for key, track in self._tracked.items()
            if track.state in SESSION_STATES and self._is_game_session(key)
        }
        ended = sorted(self._live_sessions - active)
        started = sorted(active - self._live_sessions)
        self._live_sessions = active
        for key in ended:
            self.owner.session_changed(key, False)
        for key in started:
            self.owner.session_changed(key, True)
        playing = next(
            (key for key in sorted(active) if self._tracked[key].state == "running"), ""
        )
        self.owner.publish_session_status(self._game_for_key(playing) if playing else None)

    def adaptive_allowed(self, game: LibraryGame) -> bool:
        """The panel's Adaptive gate for a game: a supported renderer and a
        verified tier for the game's GPU (``_sync_adaptive_availability``)."""
        if not game.overlay_supported:
            return False
        if not callable(self._adaptive_available):
            return True
        single_gpu = len(self._gpu_choices) == 1
        target = str(getattr(game_setting(game), "gpu_uuid", "") or "")
        if not target and single_gpu:
            target = str(getattr(self._gpu_choices[0], "uuid", "") or "")
        return bool(self._adaptive_available(target, single_gpu))


class LibraryPage(W.QWidget):
    """The Game Library page over the real launchers.

    Signals (for the window to wire pages together):

    - ``selected_game_changed(LibraryGame | None)``: the selection, refreshed
      after every write (the In-Game Overlay page follows it).
    - ``open_perf_profile(key)``: the user opened a game's Perf Profile.
    - ``status_changed(dict | None)``: ``{"running", "detail"}`` while a game
      plays, None after; the window's status line, source "library".
    - ``game_session_changed(key, running, mode, target_fps)``: a wrapped game
      session started or ended, with its saved mode and Adaptive target.
    - ``recording_changed(key, running, mode)``: the same session, with the mode
      the daemon records its ring on ("" when it records none: Stock,
      unwrapped or Capture telemetry off).
    - ``capture_changed(key, enabled)``: a game's Capture telemetry changed.
    """

    selected_game_changed = C.Signal(object)
    open_perf_profile = C.Signal(str)
    status_changed = C.Signal(object)
    game_session_changed = C.Signal(str, bool, str, int)
    recording_changed = C.Signal(str, bool, str)
    capture_changed = C.Signal(str, bool)

    def __init__(
        self,
        perf_source: PerfProfileSource,
        parent=None,
        *,
        sources=None,
        gpu_choices: Callable[[], Iterable[object]] | None = None,
    ):
        super().__init__(parent)
        self.perf_source = perf_source
        self._status: dict | None = None
        self._profile_summaries: list[dict] | None = None
        self._recording = False
        # A live ring grows while its game plays: the sparkline follows. Runs
        # only while this page is shown and a session records.
        self.perf_timer = C.QTimer(self)
        self.perf_timer.setInterval(2000)
        self.perf_timer.timeout.connect(self._sync_live_perf)
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = self.build_panel(
            sources=sources,
            gpu_choices=gpu_choices or detected_gpu_choices,
            adaptive_available=self.adaptive_available,
        )
        self.setStyleSheet(focus_visible(library_stylesheet()))
        layout.addWidget(self.panel.widget)
        normalize_group_headings(self.panel.widget)
        # Section headings sit inside their cards, as on every other page.
        for card in self.panel.widget.findChildren(W.QFrame, "prefGroupCard"):
            if card.parentWidget() is not None:
                heading_into_card(card.parentWidget())
        self.panel._restripe_groups()
        self.panel.fit_list_tools()

    def build_panel(self, **options) -> LibraryPanel:
        return LibraryPanel(self, **options)

    # -- selection ---------------------------------------------------------------

    def selected_key(self) -> str:
        return self.panel._selected_key

    def selected_game(self) -> LibraryGame | None:
        return self.panel._selected_game()

    def game(self, key: str) -> LibraryGame | None:
        return self.panel._game_for_key(key)

    def select_game(self, key: str) -> None:
        """Select a game by key, clearing the filters when they hide it."""
        p = self.panel
        for i in range(p.game_list.count()):
            item = p.game_list.item(i)
            if item.data(C.Qt.ItemDataRole.UserRole) == key and item.isHidden():
                p.reset_filters()
                break
        p._select_key(key)

    def has_recording(self, key: str) -> bool:
        return bool(self.perf_source.has_recording(key))

    def return_from_perf_profile(self) -> None:
        """Back from the Perf Profile: the selection and filters never
        changed; focus returns to the entry without opening its peek."""
        button = self.panel.perf_button
        button.sync(self.panel._selected_key)
        if button.isVisible():
            button.setFocus(C.Qt.FocusReason.OtherFocusReason)

    # -- sessions ----------------------------------------------------------------

    def recording_mode(self, game: LibraryGame | None) -> str:
        """The mode a session records its ring on, or "" when the daemon
        records none: a wrapped game, a mode that runs a profile, and
        Capture telemetry on."""
        setting = game_setting(game)
        mode = game_mode(game)
        records = bool(
            game is not None
            and game.enabled
            and bool(getattr(setting, "telemetry", True))
            and mode in RECORDING_MODES
        )
        return mode if records else ""

    def system_target_fps(self) -> float:
        return adaptive_target_fps_from_env()

    def session_target_fps(self, game: LibraryGame | None) -> int:
        target = getattr(game_setting(game), "target_fps", None)
        return int(target or self.system_target_fps())

    def session_changed(self, key: str, running: bool) -> None:
        game = self.panel._game_for_key(key)
        tier = self.recording_mode(game)
        self.recording_changed.emit(key, bool(running), tier)
        self._recording = bool(running and tier)
        self._sync_perf_timer()
        self.panel.perf_button.sync(self.panel._selected_key)
        self.panel.select_filters()
        self.game_session_changed.emit(
            key, bool(running), game_mode(game), self.session_target_fps(game)
        )

    def publish_session_status(self, game: LibraryGame | None) -> None:
        """The window status line while a game plays: its name, then its
        mode, GPU and launcher."""
        status = None
        if game is not None:
            setting = game_setting(game)
            mode = game_mode(game)
            source = self.panel._by_launcher.get(game.launcher)
            parts = [
                MODE_NAMES.get(mode, mode.title()),
                gpu_text(self.panel._gpu_choices, str(getattr(setting, "gpu_uuid", "") or "")),
                f"via {source.display_name if source is not None else game.launcher.title()}",
            ]
            status = {
                "running": f"Playing {game.name}",
                "detail": " · ".join(part for part in parts if part),
            }
        if status != self._status:
            self._status = status
            self.status_changed.emit(status)

    def _sync_perf_timer(self) -> None:
        if self._recording and self.isVisible():
            if not self.perf_timer.isActive():
                self.perf_timer.start()
        else:
            self.perf_timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        # The first look reads every launcher, as the classic tab does on
        # first entry; never while the window is being built.
        self.panel.ensure_scanned()
        self._sync_perf_timer()
        if self._recording:
            self._sync_live_perf()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._sync_perf_timer()

    def _sync_live_perf(self) -> None:
        button = self.panel.perf_button
        if self.isVisible() and button.recording is not None and button.recording.live:
            button.sync(self.panel._selected_key)

    def refresh(self) -> None:
        """Re-read live state after the root service changed: saved profiles
        (Adaptive's gate), then a quiet launcher and GPU pass."""
        self.set_profile_summaries(None)
        if self.panel._scanned:
            self.panel.rescan(deep=False, quiet=True)

    # -- Adaptive ----------------------------------------------------------------

    def adaptive_available(self, gpu_uuid: str = "", include_legacy_profiles: bool = False) -> bool:
        """The panel's Adaptive gate, over the saved profiles (as the classic
        window's)."""
        if self._profile_summaries is None:
            self._profile_summaries = list(read_auto_uv_profile_summaries())
        return adaptive_tier_available(
            self._profile_summaries,
            gpu_uuid=gpu_uuid,
            include_legacy_profiles=include_legacy_profiles,
        )

    def set_profile_summaries(self, summaries: Iterable[dict] | None) -> None:
        """The saved profiles changed (Profiles reloaded them, a scan saved
        one); None re-reads them. Re-gates Adaptive for the selection."""
        self._profile_summaries = list(summaries) if summaries is not None else None
        if not self.panel._pending_field:
            self.panel._sync_selection(self.panel._selected_game())

    def adaptive_eligible(self, game: LibraryGame | None) -> bool:
        """A game Adaptive can run on: Vulkan frame data, a game its
        launcher can run, and a verified tier for its GPU."""
        return bool(game is not None and game.ready and self.panel.adaptive_allowed(game))

    def when_games_listed(self, callback) -> None:
        """Run ``callback`` once every launcher has been read: now if it has,
        else after the first read, which starts here (Profiles can ask for
        the games before this tab was ever shown)."""
        panel = self.panel
        panel.ensure_scanned()
        if not panel._scan_active:
            callback()
            return
        timer = C.QTimer(self)
        timer.setInterval(100)

        def check() -> None:
            if not panel._scan_active:
                timer.stop()
                timer.deleteLater()
                callback()

        timer.timeout.connect(check)
        timer.start()

    def adaptive_picker_state(self) -> tuple[str, list[tuple[str, str, int]]]:
        """(selected key, [(key, name, effective target FPS)]) for the games
        Adaptive can run on, for Profiles' Use Adaptive for a game."""
        games = [
            (game_key(game), game.name, self.session_target_fps(game))
            for game in self.panel._games
            if self.adaptive_eligible(game)
        ]
        return self.panel._selected_key, games

    def apply_adaptive(self, key: str, target_fps: float) -> bool:
        """Wrap a game on Adaptive with its own target: the same three writes
        as its switch, mode and target controls, queued in order. Refused
        (False) where the panel would refuse them."""
        p = self.panel
        game = p._game_for_key(key)
        if game is None or not self.adaptive_eligible(game):
            return False
        self.select_game(key)
        if p._selected_key != key or not p._writable:
            return False
        if not game.enabled and p._needs_gpu_choice([game]):
            p._sync_status("Choose a Game GPU before enabling PenguinBurner.")
            return False
        writes: list[tuple[str, Any]] = [
            ("set_game_mode", GAME_MODE_ADAPTIVE),
            ("set_game_target_fps", float(target_fps)),
        ]
        if not game.enabled:
            writes.insert(0, ("set_game_enabled", True))
        for method, value in writes:
            p._write_async(game, method, value)
        return True

    # -- per-game state other pages read -----------------------------------------

    def overlay_publishing(self, key: str) -> bool:
        """Whether a running game publishes overlay telemetry: wrapped, its
        overlay on, and a renderer the Vulkan layer reaches."""
        game = self.panel._game_for_key(key)
        return bool(game is not None and game.enabled and game.overlay and game.overlay_supported)

    def enable_capture(self, key: str) -> None:
        """The Perf Profile's "Turn on Capture telemetry": the same write as
        the game's own switch."""
        game = self.panel._game_for_key(key)
        if game is not None:
            self.panel._write_async(game, "set_game_telemetry", True)
