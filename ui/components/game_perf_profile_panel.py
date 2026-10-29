"""The Game Perf Profile tab: one game's full telemetry detail.

Fingerprint card, boxed stat groups and the MangoHUD-style frametime graph
(30-minute overview or the live last-10-seconds zoom), laid out like the
In-Game Overlay tab. Reads the daemon's ring for the selected game and
refreshes while visible. The trace is always the ``theme.PERF_PROFILE_*``
violet-blue; red is reserved for the stutter needles.
"""

from __future__ import annotations

import math
from typing import Any

from runtime.frame_history import (
    QUALIFY_MINUTES,
    FrameHistory,
    FrameHistorySummary,
    read_frame_history_for_game,
    summarize,
)
from ui import theme
from ui.components.game_perf_profile import (
    perf_profile_axes,
    perf_profile_pixmap,
    tier_color,
    tier_label,
    warming_text,
)
from ui.components.preference_rows import preference_group, preference_row

_REFRESH_MS = 2000
# A recording ring flushes once a second; match it so a live session visibly
# advances instead of jumping two seconds at a time.
_LIVE_REFRESH_MS = 1000
_OVERVIEW_COLUMNS = 300
_LIVE_WINDOW_MS = 10_000.0
_STUTTER_RATIO = 1.6
_STUTTER_MILD_MS = 16.7
_STUTTER_SEVERE_MS = 33.3
# Sized so the fingerprint card ends level with the five-row stat card.
_PERF_PROFILE_SIZE = 170
# Three columns, then two, then one, like the In-Game Overlay tab. These cards
# are narrower than the overlay's (fingerprint ~256 px, stat groups ~261 px),
# so three columns hold down to a smaller window.
_THREE_COLUMN_WIDTH = 960
_TWO_COLUMN_WIDTH = 720

_EMPTY_HINT = (
    "Select a game in the Game Library tab and click its Perf Profile button.\n"
    "Telemetry is recorded while a game's PenguinBurner profile runs."
)


def meta_text(history: FrameHistory, summary: FrameHistorySummary) -> str:
    """The line under the game title: how much window is behind the numbers.

    A live ring says so, and a window still short of the qualify gate says the
    fingerprint has not settled — the graph is real either way.
    """
    minutes = summary.minutes
    parts: list[str] = []
    if history.live:
        parts.append("● REC")
    parts.append(
        f"{minutes:.1f} min captured" if minutes < 10.0
        else f"{minutes:.0f} min captured"
    )
    parts.append(f"{len(history.frametimes_ms):,} frames")
    if not summary.qualified:
        parts.append(f"settling · fingerprint at {QUALIFY_MINUTES:.0f} min")
    if summary.framegen_seen:
        parts.append("frame-gen seen")
    return " · ".join(parts)


def densify_frametimes(
    frametimes_ms: tuple[float, ...], columns: int = _OVERVIEW_COLUMNS
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    """Bucket a frame ring into plot columns: (x_minutes, mean_ms, max_ms).

    x is minutes relative to now (0 = newest), derived from the frametimes
    themselves. The per-column max preserves the stutter spikes that a plain
    mean would smooth away.
    """
    if not frametimes_ms:
        return (), (), ()
    total_ms = sum(frametimes_ms)
    bucket = max(1, len(frametimes_ms) // columns)
    xs: list[float] = []
    means: list[float] = []
    maxes: list[float] = []
    elapsed = 0.0
    for start in range(0, len(frametimes_ms), bucket):
        chunk = frametimes_ms[start : start + bucket]
        elapsed += sum(chunk)
        xs.append((elapsed - total_ms) / 60_000.0)
        means.append(sum(chunk) / len(chunk))
        maxes.append(max(chunk))
    return tuple(xs), tuple(means), tuple(maxes)


def live_tail(
    frametimes_ms: tuple[float, ...], window_ms: float = _LIVE_WINDOW_MS
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """The last ~10 s of frames at full resolution: (x_seconds, frametime)."""
    xs: list[float] = []
    values: list[float] = []
    elapsed = 0.0
    for frametime in reversed(frametimes_ms):
        elapsed += frametime
        if elapsed > window_ms and values:
            break
        xs.append(-elapsed / 1000.0)
        values.append(frametime)
    xs.reverse()
    values.reverse()
    return tuple(xs), tuple(values)


_REFLOW_FILTER_CLASSES: dict[int, Any] = {}


def _ReflowFilter(QtCore, panel):
    """An event filter that reflows the panel's grid on resize."""
    cls = _REFLOW_FILTER_CLASSES.get(id(QtCore))
    if cls is None:

        class ReflowFilter(QtCore.QObject):
            def __init__(self, owner):
                super().__init__(owner.widget)
                self.owner = owner

            def eventFilter(self, watched, event):
                if event.type() == QtCore.QEvent.Type.Resize:
                    self.owner._reflow_columns(watched.width())
                return False

        cls = _REFLOW_FILTER_CLASSES[id(QtCore)] = ReflowFilter
    return cls(panel)


def _mk_color(QtGui, color: str, alpha: int):
    value = QtGui.QColor(color)
    value.setAlpha(alpha)
    return value


def stutter_severity_color(QtGui, frametime_ms: float):
    """Needle color for a stutter spike: yellow -> orange -> red by magnitude.

    A 12 ms worst-1% at high fps is still good, so mild spikes read yellow;
    red is reserved for frames that genuinely lag (~30 ms and beyond).
    """
    span = _STUTTER_SEVERE_MS - _STUTTER_MILD_MS
    t = min(1.0, max(0.0, (frametime_ms - _STUTTER_MILD_MS) / span))
    mild = QtGui.QColor(theme.PERF_PROFILE_STUTTER_MILD)
    severe = QtGui.QColor(theme.PERF_PROFILE_STUTTER)
    return QtGui.QColor(
        round(mild.red() + (severe.red() - mild.red()) * t),
        round(mild.green() + (severe.green() - mild.green()) * t),
        round(mild.blue() + (severe.blue() - mild.blue()) * t),
    )


class GamePerfProfilePanel:
    """Owns the Game Perf Profile tab. Plain component: root widget in ``.widget``."""

    def __init__(
        self,
        *,
        QtCore,
        QtGui,
        QtWidgets,
        pg=None,
        history_env: dict[str, str] | None = None,
    ) -> None:
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.pg: Any = pg
        # Injectable ring-directory env for tests; None reads the real dirs.
        self._history_env = history_env
        self._game_key = ""
        self._game_name = ""
        self._target_fps: float | None = None
        self._live_mode = False
        self._last_summary: FrameHistorySummary | None = None

        self.widget = QtWidgets.QWidget()
        self.widget.setObjectName("gamePerfProfilePanel")
        if pg is None:
            fallback = QtWidgets.QPlainTextEdit()
            fallback.setReadOnly(True)
            fallback.setPlainText("pyqtgraph is not installed.")
            layout = QtWidgets.QVBoxLayout(self.widget)
            layout.addWidget(fallback)
            self._refresh_timer = None
            return

        # The In-Game Overlay tab's language and geometry: page title and muted
        # subtitle, a scrolling grid of a framed card and boxed preference
        # groups with muted values. Only the trace and the stutter needles keep
        # the PERF_PROFILE palette; everything around them uses the app theme.
        self.widget.setStyleSheet(
            f"""
            QWidget#gamePerfProfilePanel, QWidget#gamePerfProfilePage {{
                background: {theme.WINDOW_BG};
            }}
            QLabel#gamePerfProfileTitle {{
                color: {theme.TEXT_STRONG}; font-size: 23px; font-weight: 700;
            }}
            QLabel#gamePerfProfileMeta {{ color: {theme.TEXT_MUTED}; font-size: 12px; }}
            QLabel#gamePerfProfileTierChip {{
                color: {theme.TEXT_MUTED}; font-size: 11px; font-weight: 700;
                border: 1px solid {theme.BORDER_STRONG}; border-radius: 10px;
                padding: 3px 12px;
            }}
            QLabel#gamePerfProfileGroupNote {{ color: {theme.TEXT_DISABLED}; font-size: 11px; }}
            QLabel#gamePerfProfileCaption {{ color: {theme.TEXT_MUTED}; font-size: 11px; }}
            QLabel#gamePerfProfileValue {{ color: {theme.TEXT_MUTED}; font-size: 12px; }}
            QLabel#gamePerfProfileEmpty {{ color: {theme.TEXT_MUTED}; font-size: 13px; }}
            QFrame#gamePerfProfileCard {{
                background: {theme.SURFACE_BG};
                border: 1px solid {theme.BORDER_STRONG}; border-radius: 6px;
            }}
            QToolButton#gamePerfProfileLiveToggle {{
                color: #9fd3b6; background: transparent; border: 0;
                padding: 2px 6px; font-size: 12px;
            }}
            QToolButton#gamePerfProfileLiveToggle:hover {{
                color: {theme.PRIMARY_BUTTON_TEXT}; background: {theme.CONTROL_HOVER_BG};
            }}
            QToolButton#gamePerfProfileLiveToggle:checked {{
                color: {theme.PRIMARY_BUTTON_TEXT}; background: {theme.PRIMARY_BUTTON_BG};
                border-radius: 4px;
            }}
            QComboBox#gamePerfProfileLiveWindow {{
                min-height: 20px; padding: 1px 8px; font-size: 12px;
                background: {theme.SURFACE_BG}; color: {theme.TEXT};
                border: 1px solid {theme.BORDER_STRONG}; border-radius: 4px;
            }}
            QWidget#gamePerfProfilePanel QScrollArea {{ background: transparent; border: 0; }}
            QWidget#gamePerfProfilePanel QScrollBar:vertical {{
                background: transparent; width: 9px; margin: 0;
            }}
            QWidget#gamePerfProfilePanel QScrollBar::handle:vertical {{
                background: {theme.BORDER_STRONG}; border-radius: 4px; min-height: 32px;
            }}
            QWidget#gamePerfProfilePanel QScrollBar::add-line:vertical,
            QWidget#gamePerfProfilePanel QScrollBar::sub-line:vertical {{ height: 0; }}
            QWidget#gamePerfProfilePanel QScrollBar::add-page:vertical,
            QWidget#gamePerfProfilePanel QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            """
        )

        root = QtWidgets.QVBoxLayout(self.widget)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(0)
        self._stack = QtWidgets.QStackedLayout()
        root.addLayout(self._stack)

        # Page 0: empty / warming message.
        message_page = QtWidgets.QWidget()
        message_layout = QtWidgets.QVBoxLayout(message_page)
        message_layout.addStretch(1)
        self.empty_profile = QtWidgets.QLabel("")
        self.empty_profile.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        message_layout.addWidget(self.empty_profile)
        self.empty_label = QtWidgets.QLabel(_EMPTY_HINT)
        self.empty_label.setObjectName("gamePerfProfileEmpty")
        self.empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        message_layout.addWidget(self.empty_label)
        message_layout.addStretch(2)
        self._stack.addWidget(message_page)

        # Page 1: the detail. The header stays put; the grid scrolls.
        detail_page = QtWidgets.QWidget()
        detail = QtWidgets.QVBoxLayout(detail_page)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.setSpacing(20)

        header = QtWidgets.QHBoxLayout()
        heading = QtWidgets.QVBoxLayout()
        heading.setSpacing(4)
        self.game_title = QtWidgets.QLabel("")
        self.game_title.setObjectName("gamePerfProfileTitle")
        heading.addWidget(self.game_title)
        self.meta_label = QtWidgets.QLabel("")
        self.meta_label.setObjectName("gamePerfProfileMeta")
        heading.addWidget(self.meta_label)
        header.addLayout(heading, 1)
        self.tier_chip = QtWidgets.QLabel("")
        self.tier_chip.setObjectName("gamePerfProfileTierChip")
        header.addWidget(self.tier_chip, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        detail.addLayout(header)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        page = QtWidgets.QWidget()
        page.setObjectName("gamePerfProfilePage")
        self._grid = QtWidgets.QGridLayout(page)
        self._grid.setContentsMargins(0, 0, 8, 0)
        self._grid.setHorizontalSpacing(20)
        self._grid.setVerticalSpacing(20)
        scroll.setWidget(page)
        detail.addWidget(scroll, 1)

        self._stat_values: dict[str, Any] = {}
        self._fingerprint_card = self._build_fingerprint_card()
        self._frame_rate_group, frame_rows, _ = self._group("Frame rate", "window medians")
        self._stat_row(frame_rows, "MEDIAN", "Median")
        self._stat_row(frame_rows, "1%-LOW", "1% low")
        # Latency has no spoke and no placeholder: the row exists only while
        # there is a real measurement to show.
        self._latency_cell = self._stat_row(frame_rows, "LATENCY", "Latency")
        self._latency_key = self._latency_cell.property("titleLabel")
        self._latency_cell.setVisible(False)
        self._hardware_group, hardware_rows, _ = self._group(
            "GPU and system", "window medians"
        )
        self._stat_row(hardware_rows, "POWER", "Power draw")
        self._stat_row(hardware_rows, "CLOCK", "GPU clock")
        self._stat_row(hardware_rows, "GPU / CPU-THREAD", "GPU / busiest CPU thread")
        self._stat_row(hardware_rows, "TEMP", "GPU temperature")
        self._stat_row(hardware_rows, "FAN", "Fan")
        self._frametime_figure = self._build_frametime_figure()
        self._layout_columns = 0
        self._reflow_columns(1300)
        self._reflow_filter = _ReflowFilter(QtCore, self)
        self.widget.installEventFilter(self._reflow_filter)
        self._stack.addWidget(detail_page)

        self._refresh_timer = QtCore.QTimer(self.widget)
        self._refresh_timer.setInterval(_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._timer_tick)
        self._refresh_timer.start()

    # ---------- layout ----------

    def _reflow_columns(self, width: int) -> None:
        """Place the cards like the In-Game Overlay tab: the fingerprint beside
        the stat columns, the frametime card full width below them, taking the
        rest of the window's height."""
        mode = 3 if width >= _THREE_COLUMN_WIDTH else 2 if width >= _TWO_COLUMN_WIDTH else 1
        if mode == self._layout_columns:
            return
        self._layout_columns = mode
        grid = self._grid
        top = self.QtCore.Qt.AlignmentFlag.AlignTop
        widgets = (
            self._fingerprint_card, self._frame_rate_group,
            self._hardware_group, self._frametime_figure,
        )
        for widget in widgets:
            grid.removeWidget(widget)
        for index in range(4):
            grid.setColumnStretch(index, 0)
            grid.setRowStretch(index, 0)
        if mode == 3:
            grid.addWidget(self._fingerprint_card, 0, 0, top)
            grid.addWidget(self._frame_rate_group, 0, 1, top)
            grid.addWidget(self._hardware_group, 0, 2, top)
            figure_row, columns = 1, 3
            for index, stretch in enumerate((10, 11, 11)):
                grid.setColumnStretch(index, stretch)
        elif mode == 2:
            grid.addWidget(self._fingerprint_card, 0, 0, 2, 1, top)
            grid.addWidget(self._frame_rate_group, 0, 1, top)
            grid.addWidget(self._hardware_group, 1, 1, top)
            figure_row, columns = 2, 2
            grid.setColumnStretch(0, 10)
            grid.setColumnStretch(1, 13)
        else:
            for row, widget in enumerate(widgets[:-1]):
                grid.addWidget(widget, row, 0)
            figure_row, columns = 3, 1
            grid.setColumnStretch(0, 1)
        grid.addWidget(self._frametime_figure, figure_row, 0, 1, columns)
        grid.setRowStretch(figure_row, 1)

    def _group(self, heading: str, note: str):
        """A preference group whose heading row also carries a muted note and
        room on the right for controls, like the overlay's metric sections."""
        QtWidgets = self.QtWidgets
        group, rows = preference_group(QtWidgets=QtWidgets, heading=heading)
        outer = group.layout()
        heading_label = outer.takeAt(0).widget()
        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(heading_label)
        note_label = QtWidgets.QLabel(note)
        note_label.setObjectName("gamePerfProfileGroupNote")
        header.addWidget(note_label)
        header.addStretch(1)
        outer.insertLayout(0, header)
        return group, rows, header

    def _stat_row(self, rows_layout, key: str, title: str):
        value = self.QtWidgets.QLabel("")
        value.setObjectName("gamePerfProfileValue")
        value.setAlignment(
            self.QtCore.Qt.AlignmentFlag.AlignRight | self.QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        self._stat_values[key] = value
        row = preference_row(
            QtWidgets=self.QtWidgets, QtCore=self.QtCore, rows_layout=rows_layout,
            title=title, control=value,
        )
        row.layout().setContentsMargins(14, 12, 14, 12)
        row.layout().setSpacing(14)
        return row

    def _build_fingerprint_card(self):
        """The fingerprint in a group of its own, headed like the stat groups
        so the three cards start on one line."""
        group, rows, _ = self._group("Fingerprint", "window medians")
        # The darker surface of the overlay's Preview card: the fingerprint's
        # rings vanish on the lighter stat-card fill.
        group.findChild(self.QtWidgets.QFrame, "prefGroupCard").setObjectName(
            "gamePerfProfileCard"
        )
        rows.setContentsMargins(8, 8, 8, 8)
        self.profile_label = self.QtWidgets.QLabel("")
        self.profile_label.setFixedSize(_PERF_PROFILE_SIZE + 30, _PERF_PROFILE_SIZE + 30)
        self.profile_label.setAlignment(self.QtCore.Qt.AlignmentFlag.AlignCenter)
        rows.addWidget(self.profile_label, 0, self.QtCore.Qt.AlignmentFlag.AlignHCenter)
        return group

    def _figure(self, title: str, caption: str):
        """A preference group whose card holds a plot: overline heading with
        the controls on its right, then the plot and a muted caption inside."""
        QtWidgets = self.QtWidgets
        frame, _rows, header = self._group(title, "every frame, in milliseconds")
        card = frame.findChild(QtWidgets.QFrame, "prefGroupCard")
        layout = card.layout()
        layout.setContentsMargins(12, 12, 12, 10)
        frame.layout().setStretchFactor(card, 1)
        caption_label = QtWidgets.QLabel(caption)
        caption_label.setObjectName("gamePerfProfileCaption")
        caption_label.setWordWrap(True)
        return frame, layout, header, caption_label

    def _edge_label_axis(self, orientation: str):
        """An axis that keeps its edge tick labels: stock pyqtgraph culls any
        label protruding past the axis rect, which always swallows the "0"
        at the origin once the range is pinned to the data."""
        pg = self.pg

        class _EdgeLabelAxis(pg.AxisItem):
            def boundingRect(self):
                rect = super().boundingRect()
                if self.orientation in ("left", "right"):
                    return rect.adjusted(0, -12, 0, 12)
                return rect.adjusted(-12, 0, 12, 0)

            def tickStrings(self, values, scale, spacing):
                # The axis label already says "minutes ago" — a "-5" under it
                # would read as the future. Ticks show unsigned magnitudes.
                if self.orientation == "bottom":
                    return [
                        f"{abs(value * scale):g}" for value in values
                    ]
                return super().tickStrings(values, scale, spacing)

        return _EdgeLabelAxis(orientation=orientation)

    def _styled_plot(self, *, x_label: str, y_label: str):
        pg = self.pg
        plot = pg.PlotWidget(
            axisItems={
                "left": self._edge_label_axis("left"),
                "bottom": self._edge_label_axis("bottom"),
            }
        )
        plot.setMenuEnabled(False)
        if hasattr(pg, "setConfigOptions"):
            pg.setConfigOptions(antialias=True)
        plot.setBackground(theme.CONTROL_BG)
        plot.showGrid(x=True, y=True, alpha=0.16)
        # A read-only figure: the panel owns the ranges, the user's wheel
        # must not fight the 2 s refresh.
        plot.setMouseEnabled(x=False, y=False)
        plot.hideButtons()
        axis_pen = pg.mkPen(theme.BORDER_STRONG, width=1)
        text_color = theme.TEXT_MUTED
        for name in ("bottom", "left"):
            axis = plot.getAxis(name)
            axis.setPen(axis_pen)
            axis.setTextPen(pg.mkPen(text_color))
            if hasattr(axis, "enableAutoSIPrefix"):
                axis.enableAutoSIPrefix(False)
            try:
                # Keep the edge ticks ("0" at the origin) instead of
                # pyqtgraph's default of hiding labels near the axis ends.
                axis.setStyle(hideOverlappingLabels=False)
            except (NameError, TypeError):
                pass
        plot.setLabel("bottom", x_label, color=text_color)
        plot.setLabel("left", y_label, color=text_color)
        plot.setMinimumHeight(150)
        # The plot fills whatever height the window has left below the cards.
        # Inside the scrolling grid a preferred height would decide the page
        # height instead, so it is ignored.
        policy = self.QtWidgets.QSizePolicy
        plot.setSizePolicy(policy.Policy.Expanding, policy.Policy.Ignored)
        return plot

    def _build_frametime_figure(self):
        QtWidgets = self.QtWidgets
        pg = self.pg
        frame, layout, header, caption = self._figure(
            "Frametime",
            "flat & low = smooth · stutter needles yellow -> red by severity "
            "(red ~30 ms) · lines: median & 1%-low (p99)",
        )
        self._frametime_caption = caption
        self._frametime_caption_base = caption.text()
        self.live_toggle = QtWidgets.QToolButton()
        self.live_toggle.setObjectName("gamePerfProfileLiveToggle")
        self.live_toggle.setText("Live")
        self.live_toggle.setCheckable(True)
        self.live_toggle.toggled.connect(self._live_toggled)
        header.addWidget(self.live_toggle, 0)
        self.live_window_combo = QtWidgets.QComboBox()
        self.live_window_combo.setObjectName("gamePerfProfileLiveWindow")
        for seconds in (10, 30, 60):
            self.live_window_combo.addItem(f"{seconds} s", seconds)
        self.live_window_combo.currentIndexChanged.connect(self._live_window_changed)
        header.addWidget(self.live_window_combo, 0)

        self.frametime_plot = self._styled_plot(
            x_label="minutes ago", y_label="frametime (ms)"
        )
        # The trace is ALWAYS the mock's violet-blue, whatever the game's
        # tier: red belongs exclusively to the stutter needles.
        self.frametime_curve = self.frametime_plot.plot(
            [], [], pen=pg.mkPen(theme.PERF_PROFILE_TIER_BALANCED, width=1.4),
            fillLevel=0.0, brush=_mk_color(self.QtGui, theme.PERF_PROFILE_TIER_BALANCED, 38),
        )
        # Needles sit above the trace fill, as in the mock.
        self.stutter_bars = pg.BarGraphItem(
            x=[], height=[], width=0.001,
            brush=_mk_color(self.QtGui, theme.PERF_PROFILE_STUTTER, 230), pen=None,
        )
        self.stutter_bars.setZValue(5)
        self.frametime_plot.addItem(self.stutter_bars)
        # Unlabeled reference lines: the numbers live in the stat row, and
        # on-plot labels collide whenever median and 1%-low sit close. The
        # median rides its own trace, so it dashes in a brighter ink to stay
        # visible against the stroke it hugs.
        median_pen = pg.mkPen(theme.PERF_PROFILE_TEXT_MUTED, width=1)
        median_pen.setStyle(self.QtCore.Qt.PenStyle.DashLine)
        self.median_line = pg.InfiniteLine(angle=0, movable=False, pen=median_pen)
        self.p99_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(theme.PERF_PROFILE_TEXT_DIM, width=1),
        )
        self.frametime_plot.addItem(self.median_line, ignoreBounds=True)
        self.frametime_plot.addItem(self.p99_line, ignoreBounds=True)
        layout.addWidget(self.frametime_plot, 1)
        layout.addSpacing(6)
        layout.addWidget(caption)
        return frame

    # ---------- data flow ----------

    def select_game(
        self, game_key: str, *, game_name: str = "", target_fps: float | None = None
    ) -> None:
        """Show one game, by the ``<launcher>:<id>`` key its rings are stored under."""
        self._game_key = str(game_key)
        self._game_name = game_name or str(game_key)
        self._target_fps = target_fps
        self.refresh()

    def refresh(self) -> None:
        if self.pg is None:
            return
        if not self._game_key:
            self._show_message(_EMPTY_HINT, warming=False)
            return
        history = read_frame_history_for_game(self._game_key, env=self._history_env)
        summary = summarize(history) if history is not None else None
        if history is None or summary is None:
            self._show_message(
                f"No telemetry captured for {self._game_name} yet.\n"
                "Play it with PenguinBurner enabled to record a window.",
                warming=False,
            )
            return
        # 1 Hz costs a full-window decode per tick, so buy it only where it
        # shows: the LIVE zoom's 10-60 s window moves visibly every second. An
        # overview column spans ~6 s of play, so a faster tick would redraw the
        # same picture.
        self._set_refresh_interval(
            _LIVE_REFRESH_MS if history.live and self._live_mode else _REFRESH_MS
        )
        # The 5-minute gate decides whether a fingerprint is worth trusting at
        # a glance, so it still hides a short *archived* session. A ring the
        # daemon is writing right now is different: the game is on and the user
        # wants to watch it fill, so show the detail from the first second and
        # label it as still settling.
        if not summary.qualified and not history.live:
            self._show_message(
                f"{self._game_name} — {warming_text(summary.minutes)}",
                warming=True,
            )
            return
        self._last_summary = summary
        self._populate(history, summary)
        self._stack.setCurrentIndex(1)

    def _show_message(self, text: str, *, warming: bool) -> None:
        self.empty_label.setText(text)
        pixmap = perf_profile_pixmap(
            self.QtCore, self.QtGui, axes=None, tier="", size=72,
            device_pixel_ratio=self._device_pixel_ratio(),
        )
        self.empty_profile.setPixmap(pixmap)
        self.empty_profile.setVisible(warming or not self._game_key)
        self._stack.setCurrentIndex(0)

    def _device_pixel_ratio(self) -> float:
        window = self.widget.window()
        handle = window.windowHandle() if window is not None else None
        if handle is not None:
            return float(handle.devicePixelRatio())
        return 1.0

    def _populate(self, history: FrameHistory, summary: FrameHistorySummary) -> None:
        color = tier_color(summary.tier)
        self.game_title.setText(self._game_name)
        self.tier_chip.setText(tier_label(summary.tier).upper())
        self.tier_chip.setStyleSheet(
            f"color: {color}; border: 1px solid {color}; border-radius: 10px;"
            "padding: 3px 12px; font-size: 11px; font-weight: 700;"
        )
        self.meta_label.setText(meta_text(history, summary))
        axes = perf_profile_axes(
            summary,
            target_fps=self._target_fps,
            power_limit_w=history.header.power_limit_w,
        )
        self.profile_label.setPixmap(
            perf_profile_pixmap(
                self.QtCore, self.QtGui, axes=axes, tier=summary.tier,
                size=_PERF_PROFILE_SIZE, labels=True,
                device_pixel_ratio=self._device_pixel_ratio(),
            )
        )
        self.profile_label.setToolTip(
            "\n".join(f"{axis.label}: {axis.text}" for axis in axes)
        )
        self._stat_values["MEDIAN"].setText(
            f"{summary.median_present_fps:.0f} fps · "
            f"{summary.median_frametime_ms:.1f} ms"
        )
        self._stat_values["1%-LOW"].setText(
            f"{summary.low_1pct_fps:.0f} fps · {summary.p99_frametime_ms:.1f} ms"
        )
        self._stat_values["POWER"].setText(f"{summary.median_power_w} W")
        self._stat_values["CLOCK"].setText(f"{summary.median_clock_mhz} MHz")
        self._stat_values["TEMP"].setText(f"{summary.median_temperature_c:.0f} °C")
        self._stat_values["FAN"].setText(f"{summary.median_fan_pct:.0f}%")
        self._stat_values["GPU / CPU-THREAD"].setText(
            f"{summary.gpu_util_pct}% / {summary.cpu_peak_thread_pct}%"
        )
        self._update_latency_cell(summary)
        self._render_frametime(history, summary)

    def _update_latency_cell(self, summary: FrameHistorySummary) -> None:
        """Latency is numeric-only and appears only when a marker source
        actually measured it. The present→scanout display tail is recorded in
        the ring but never displayed — a bare scanout time reads as garbage."""
        if summary.latency_ms > 0:
            self._latency_key.setText("Latency")
            self._stat_values["LATENCY"].setText(f"{summary.latency_ms:.0f} ms")
            self._latency_cell.setVisible(True)
        else:
            self._latency_cell.setVisible(False)

    def _render_frametime(
        self, history: FrameHistory, summary: FrameHistorySummary
    ) -> None:
        threshold = summary.median_frametime_ms * _STUTTER_RATIO
        if self._live_mode:
            window_s = float(self._live_window_s)
            xs, values = live_tail(history.frametimes_ms, window_s * 1000.0)
            spikes_x = tuple(x for x, v in zip(xs, values) if v >= threshold)
            spikes_h = tuple(v for v in values if v >= threshold)
            bar_width = window_s * 0.003
            x_range = (-window_s - window_s * 0.03, window_s * 0.01)
            self.frametime_plot.setLabel(
                "bottom", "seconds ago", color=theme.TEXT_MUTED
            )
        else:
            xs, values, maxes = densify_frametimes(history.frametimes_ms)
            spikes_x = tuple(x for x, m in zip(xs, maxes) if m >= threshold)
            spikes_h = tuple(m for m in maxes if m >= threshold)
            span = abs(xs[0]) if xs else 1.0
            bar_width = span / _OVERVIEW_COLUMNS * 0.5
            # Pin the view to the captured span so the band meets the plot
            # edges instead of floating in autorange padding.
            x_range = (xs[0] - span * 0.01, span * 0.01) if xs else (-1.0, 0.0)
            self.frametime_plot.setLabel(
                "bottom", "minutes ago", color=theme.TEXT_MUTED
            )
        # The mock's y-ceiling, rounded to a nice number so the top tick and
        # the range end coincide; spikes keep their height, extremes clamp.
        raw_ceiling = max(
            20.0, summary.median_frametime_ms * 2.4, summary.p99_frametime_ms * 1.85
        )
        ceiling = float(math.ceil(raw_ceiling / 5.0) * 5)
        worst = max(spikes_h) if spikes_h else 0.0
        # Colors come from the TRUE spike magnitude, before the plot clamp.
        spike_brushes = [
            stutter_severity_color(self.QtGui, height) for height in spikes_h
        ]
        spikes_h = tuple(min(height, ceiling) for height in spikes_h)
        # A 250 ms hitch must not silently render like a 20 ms blip: the
        # caption discloses the true worst frame whenever the plot clamps.
        caption = self._frametime_caption_base
        if worst > ceiling:
            caption = f"{caption} · worst frame {worst:.0f} ms (clipped)"
        self._frametime_caption.setText(caption)
        self.frametime_curve.setData(xs, values)
        self.stutter_bars.setOpts(
            x=spikes_x, height=spikes_h, width=bar_width, brushes=spike_brushes
        )
        self.median_line.setValue(summary.median_frametime_ms)
        self.p99_line.setValue(summary.p99_frametime_ms)
        if values:
            # Explicit ticks, mock-style: 0 is always on the axis.
            step = 5 if ceiling <= 25 else 10 if ceiling <= 50 else 20
            self.frametime_plot.getAxis("left").setTicks(
                [[(v, str(v)) for v in range(0, int(ceiling) + 1, step)]]
            )
            self.frametime_plot.setXRange(*x_range, padding=0.0)
            # 4% headroom keeps the top tick's label unclipped; clamped
            # needles still terminate exactly on the ceiling tick.
            self.frametime_plot.setYRange(0.0, ceiling * 1.04, padding=0.0)

    # ---------- events ----------

    def _live_window_changed(self, _index: int) -> None:
        if self._live_mode:
            self.refresh()

    @property
    def _live_window_s(self) -> int:
        combo = getattr(self, "live_window_combo", None)
        if combo is None:
            return 10
        return int(combo.currentData() or 10)

    def _live_toggled(self, checked: bool) -> None:
        self._live_mode = bool(checked)
        self.refresh()

    def _refresh_is_worth_it(self) -> bool:
        """Never spend a decode nobody can see.

        Refreshing costs a full-window ring decode. A tab the user switched
        away from reports ``isVisible()`` false and costs nothing, but Qt keeps
        it true for a minimized window, so ask about that outright.

        This cannot catch the case that matters most: a window merely occluded
        by a fullscreen game still reports visible *and* exposed, and Qt offers
        no portable occlusion signal. A Game Perf Profile tab left open behind a game
        keeps paying, which is why the tick above stays as slow as it can.
        """
        if not self._game_key or not self.widget.isVisible():
            return False
        window = self.widget.window()
        return window is None or not window.isMinimized()

    def _set_refresh_interval(self, interval_ms: int) -> None:
        timer = self._refresh_timer
        if timer is not None and timer.interval() != interval_ms:
            timer.setInterval(interval_ms)

    def _timer_tick(self) -> None:
        if self._refresh_is_worth_it():
            self.refresh()
