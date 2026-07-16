"""The Frame DNA tab: one game's full telemetry detail.

Large fingerprint + stat row and the MangoHUD-style frametime graph
(30-minute overview or the live last-10-seconds zoom). Reads the daemon's
ring for the selected game and refreshes while visible. Visuals follow the
approved mockup via the ``theme.DNA_*`` palette: the trace is always the
mock's violet-blue; red is reserved for the stutter needles.
"""

from __future__ import annotations

from typing import Any

from runtime.frame_history import (
    FrameHistory,
    FrameHistorySummary,
    read_frame_history_for_app,
    summarize,
)
from ui import theme
from ui.components.frame_dna import (
    bottleneck_label,
    dna_axes,
    dna_pixmap,
    tier_color,
    tier_label,
    warming_text,
)

_REFRESH_MS = 2000
_OVERVIEW_COLUMNS = 300
_LIVE_WINDOW_MS = 10_000.0
_STUTTER_RATIO = 1.6
_DNA_SIZE = 200
# The mock is a measured column, not a full-bleed sprawl: cap the content so
# a very wide window keeps the composed, centered layout.
_CONTENT_MAX_WIDTH = 1500

_EMPTY_HINT = (
    "Select a game in the Steam tab and click its Frame DNA badge.\n"
    "Telemetry is recorded while a PenguinBurner-enabled game runs."
)


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


def _mk_color(QtGui, color: str, alpha: int):
    value = QtGui.QColor(color)
    value.setAlpha(alpha)
    return value


class FrameDnaPanel:
    """Owns the Frame DNA tab. Plain component: root widget in ``.widget``."""

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
        self._app_id = ""
        self._game_name = ""
        self._target_fps: float | None = None
        self._live_mode = False
        self._last_summary: FrameHistorySummary | None = None

        self.widget = QtWidgets.QWidget()
        self.widget.setObjectName("frameDnaPanel")
        if pg is None:
            fallback = QtWidgets.QPlainTextEdit()
            fallback.setReadOnly(True)
            fallback.setPlainText("pyqtgraph is not installed.")
            layout = QtWidgets.QVBoxLayout(self.widget)
            layout.addWidget(fallback)
            self._refresh_timer = None
            return

        self.widget.setStyleSheet(
            f"""
            QLabel#frameDnaTitle {{
                color: {theme.DNA_TEXT}; font-size: 17px; font-weight: 700;
            }}
            QLabel#frameDnaMeta {{ color: {theme.DNA_TEXT_MUTED}; font-size: 11px; }}
            QLabel#frameDnaTierChip {{
                color: {theme.DNA_TEXT_DIM}; font-size: 10px; font-weight: 700;
                border: 1px solid {theme.DNA_BORDER_STRONG}; border-radius: 9px;
                padding: 2px 10px;
            }}
            QLabel#frameDnaSection {{
                color: {theme.DNA_TEXT_DIM}; font-size: 12px; font-weight: 700;
            }}
            QLabel#frameDnaCaption {{ color: {theme.DNA_TEXT_MUTED}; font-size: 10px; }}
            QLabel#frameDnaStatKey {{
                color: {theme.DNA_TEXT_MUTED}; font-size: 10px; font-weight: 600;
            }}
            QLabel#frameDnaStatValue {{
                color: {theme.DNA_TEXT}; font-size: 13px; font-weight: 600;
            }}
            QLabel#frameDnaEmpty {{ color: {theme.DNA_TEXT_MUTED}; font-size: 13px; }}
            QFrame#frameDnaFigure {{
                background: {theme.DNA_SURFACE};
                border: 1px solid {theme.DNA_BORDER}; border-radius: 10px;
            }}
            QToolButton#frameDnaLiveToggle {{
                color: {theme.DNA_TEXT_DIM}; background: {theme.DNA_SURFACE_ALT};
                border: 1px solid {theme.DNA_BORDER_STRONG}; border-radius: 5px;
                font-size: 10px; font-weight: 700; padding: 3px 10px;
            }}
            QToolButton#frameDnaLiveToggle:checked {{
                color: {theme.DNA_TEXT};
                border-color: {theme.DNA_TIER_BALANCED};
            }}
            """
        )

        # A centered, width-capped column (the mock's measure): the content
        # expands up to the cap, extra width becomes symmetric margins.
        outer = QtWidgets.QHBoxLayout(self.widget)
        outer.setContentsMargins(18, 14, 18, 16)
        content = QtWidgets.QWidget()
        content.setMaximumWidth(_CONTENT_MAX_WIDTH)
        outer.addStretch(1)
        outer.addWidget(content, 100)
        outer.addStretch(1)
        root = QtWidgets.QVBoxLayout(content)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        self._stack = QtWidgets.QStackedLayout()
        root.addLayout(self._stack)

        # Page 0: empty / warming message.
        message_page = QtWidgets.QWidget()
        message_layout = QtWidgets.QVBoxLayout(message_page)
        message_layout.addStretch(1)
        self.empty_dna = QtWidgets.QLabel("")
        self.empty_dna.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        message_layout.addWidget(self.empty_dna)
        self.empty_label = QtWidgets.QLabel(_EMPTY_HINT)
        self.empty_label.setObjectName("frameDnaEmpty")
        self.empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        message_layout.addWidget(self.empty_label)
        message_layout.addStretch(2)
        self._stack.addWidget(message_page)

        # Page 1: the detail.
        detail_page = QtWidgets.QWidget()
        detail = QtWidgets.QVBoxLayout(detail_page)
        detail.setContentsMargins(0, 0, 0, 0)
        detail.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(10)
        self.game_title = QtWidgets.QLabel("")
        self.game_title.setObjectName("frameDnaTitle")
        header.addWidget(self.game_title, 0)
        self.tier_chip = QtWidgets.QLabel("")
        self.tier_chip.setObjectName("frameDnaTierChip")
        header.addWidget(self.tier_chip, 0)
        header.addStretch(1)
        self.meta_label = QtWidgets.QLabel("")
        self.meta_label.setObjectName("frameDnaMeta")
        header.addWidget(self.meta_label, 0)
        detail.addLayout(header)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(16)
        self.dna_label = QtWidgets.QLabel("")
        self.dna_label.setFixedSize(_DNA_SIZE + 30, _DNA_SIZE + 30)
        self.dna_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.dna_label, 0)
        stats = QtWidgets.QGridLayout()
        stats.setHorizontalSpacing(26)
        stats.setVerticalSpacing(10)
        self._stat_values: dict[str, Any] = {}
        stat_keys = (
            "MEDIAN", "1%-LOW", "POWER",
            "GPU / CPU-THREAD", "BOTTLENECK", "LATENCY",
        )
        for index, key in enumerate(stat_keys):
            cell_widget = QtWidgets.QWidget()
            cell = QtWidgets.QVBoxLayout(cell_widget)
            cell.setContentsMargins(0, 0, 0, 0)
            cell.setSpacing(1)
            key_label = QtWidgets.QLabel(key)
            key_label.setObjectName("frameDnaStatKey")
            value_label = QtWidgets.QLabel("")
            value_label.setObjectName("frameDnaStatValue")
            cell.addWidget(key_label)
            cell.addWidget(value_label)
            stats.addWidget(cell_widget, index // 3, index % 3)
            self._stat_values[key] = value_label
        # Latency has no spoke and no placeholder: the cell exists only
        # while there is a real measurement to show.
        self._latency_cell = cell_widget
        self._latency_key = key_label
        self._latency_cell.setVisible(False)
        stats.setColumnStretch(3, 1)
        # The mock's tab-top centers the stats against the fingerprint.
        stats_wrap = QtWidgets.QVBoxLayout()
        stats_wrap.addStretch(1)
        stats_wrap.addLayout(stats)
        stats_wrap.addStretch(1)
        top.addLayout(stats_wrap, 1)
        detail.addLayout(top)

        # The figure takes the remaining height (up to its cap) so the plot
        # breathes at any window shape; leftover falls below as quiet margin.
        detail.addWidget(self._build_frametime_figure(), 1)
        detail.addStretch(0)
        self._stack.addWidget(detail_page)

        self._refresh_timer = QtCore.QTimer(self.widget)
        self._refresh_timer.setInterval(_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._timer_tick)
        self._refresh_timer.start()

    # ---------- figures ----------

    def _figure(self, title: str, caption: str):
        """A mock ``.figure``: header row, breathing room, plot, then the
        muted caption under the chart — never riding the plot itself."""
        QtWidgets = self.QtWidgets
        frame = QtWidgets.QFrame()
        frame.setObjectName("frameDnaFigure")
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(0)
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(12)
        section = QtWidgets.QLabel(title)
        section.setObjectName("frameDnaSection")
        header.addWidget(section, 0)
        header.addStretch(1)
        layout.addLayout(header)
        layout.addSpacing(10)
        caption_label = QtWidgets.QLabel(caption)
        caption_label.setObjectName("frameDnaCaption")
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
        plot.setBackground(theme.DNA_SURFACE)
        plot.showGrid(x=True, y=True, alpha=0.16)
        # A read-only figure: the panel owns the ranges, the user's wheel
        # must not fight the 2 s refresh.
        plot.setMouseEnabled(x=False, y=False)
        plot.hideButtons()
        axis_pen = pg.mkPen(theme.DNA_AXIS, width=1)
        text_color = theme.DNA_TEXT_MUTED
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
        # Keep the mock's figure proportions: fill the window's height but
        # never stretch into a barren wall of empty plot.
        plot.setMinimumHeight(220)
        plot.setMaximumHeight(460)
        return plot

    def _build_frametime_figure(self):
        QtWidgets = self.QtWidgets
        pg = self.pg
        frame, layout, header, caption = self._figure(
            "Frametime — every frame, in milliseconds",
            "flat & low = smooth · red = stutter (> 1.6× median) · "
            "lines: median & 1%-low (p99)",
        )
        self.live_toggle = QtWidgets.QToolButton()
        self.live_toggle.setObjectName("frameDnaLiveToggle")
        self.live_toggle.setText("LIVE 10 s")
        self.live_toggle.setCheckable(True)
        self.live_toggle.toggled.connect(self._live_toggled)
        header.addWidget(self.live_toggle, 0)

        self.frametime_plot = self._styled_plot(
            x_label="minutes ago", y_label="frametime (ms)"
        )
        # The trace is ALWAYS the mock's violet-blue, whatever the game's
        # tier: red belongs exclusively to the stutter needles.
        self.frametime_curve = self.frametime_plot.plot(
            [], [], pen=pg.mkPen(theme.DNA_TIER_BALANCED, width=1.4),
            fillLevel=0.0, brush=_mk_color(self.QtGui, theme.DNA_TIER_BALANCED, 38),
        )
        # Needles sit above the trace fill, as in the mock.
        self.stutter_bars = pg.BarGraphItem(
            x=[], height=[], width=0.001,
            brush=_mk_color(self.QtGui, theme.DNA_STUTTER, 230), pen=None,
        )
        self.stutter_bars.setZValue(5)
        self.frametime_plot.addItem(self.stutter_bars)
        # Unlabeled reference lines: the numbers live in the stat row, and
        # on-plot labels collide whenever median and 1%-low sit close.
        self.median_line = pg.InfiniteLine(
            angle=0, movable=False, pen=pg.mkPen(theme.DNA_AXIS, width=1)
        )
        self.p99_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen(theme.DNA_TEXT_MUTED, width=1),
        )
        self.frametime_plot.addItem(self.median_line, ignoreBounds=True)
        self.frametime_plot.addItem(self.p99_line, ignoreBounds=True)
        layout.addWidget(self.frametime_plot, 1)
        layout.addSpacing(6)
        layout.addWidget(caption)
        # The frame must stop where its content stops — no dead surface
        # below the caption once the plot reaches its own height cap.
        frame.setMaximumHeight(560)
        return frame

    # ---------- data flow ----------

    def select_app(
        self, app_id: str, *, game_name: str = "", target_fps: float | None = None
    ) -> None:
        self._app_id = str(app_id)
        self._game_name = game_name or str(app_id)
        self._target_fps = target_fps
        self.refresh()

    def refresh(self) -> None:
        if self.pg is None:
            return
        if not self._app_id:
            self._show_message(_EMPTY_HINT, warming=False)
            return
        history = read_frame_history_for_app(self._app_id, env=self._history_env)
        summary = summarize(history) if history is not None else None
        if history is None or summary is None:
            self._show_message(
                f"No telemetry captured for {self._game_name} yet.\n"
                "Play it with PenguinBurner enabled to record a window.",
                warming=False,
            )
            return
        if not summary.qualified:
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
        pixmap = dna_pixmap(
            self.QtCore, self.QtGui, axes=None, tier="", size=72,
            device_pixel_ratio=self._device_pixel_ratio(),
        )
        self.empty_dna.setPixmap(pixmap)
        self.empty_dna.setVisible(warming or not self._app_id)
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
            f"color: {color}; border: 1px solid {color}; border-radius: 9px;"
            "padding: 2px 10px; font-size: 10px; font-weight: 700;"
        )
        self.meta_label.setText(
            f"{summary.minutes:.0f} min captured · "
            f"{len(history.frametimes_ms):,} frames"
            + (" · frame-gen seen" if summary.framegen_seen else "")
        )
        axes = dna_axes(
            summary,
            target_fps=self._target_fps,
            power_limit_w=history.header.power_limit_w,
        )
        self.dna_label.setPixmap(
            dna_pixmap(
                self.QtCore, self.QtGui, axes=axes, tier=summary.tier,
                size=_DNA_SIZE, labels=True,
                device_pixel_ratio=self._device_pixel_ratio(),
            )
        )
        self.dna_label.setToolTip(
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
        self._stat_values["GPU / CPU-THREAD"].setText(
            f"{summary.gpu_util_pct}% / {summary.cpu_peak_thread_pct}%"
        )
        self._stat_values["BOTTLENECK"].setText(bottleneck_label(summary.bottleneck))
        self._update_latency_cell(summary)
        self._render_frametime(history, summary)

    def _update_latency_cell(self, summary: FrameHistorySummary) -> None:
        """Latency is numeric-only and appears only when actually measured:
        marker latency when present, otherwise the present→scanout tail as
        DISPLAY LAT (a different metric, never labeled plain LATENCY)."""
        display_latency = summary.median_display_latency_ms
        if summary.latency_ms > 0:
            self._latency_key.setText("LATENCY")
            self._stat_values["LATENCY"].setText(f"{summary.latency_ms:.0f} ms")
            self._latency_cell.setVisible(True)
        elif display_latency > 0:
            self._latency_key.setText("DISPLAY LAT")
            self._stat_values["LATENCY"].setText(f"{display_latency:.0f} ms")
            self._latency_cell.setVisible(True)
        else:
            self._latency_cell.setVisible(False)

    def _render_frametime(
        self, history: FrameHistory, summary: FrameHistorySummary
    ) -> None:
        threshold = summary.median_frametime_ms * _STUTTER_RATIO
        if self._live_mode:
            xs, values = live_tail(history.frametimes_ms)
            spikes_x = tuple(x for x, v in zip(xs, values) if v >= threshold)
            spikes_h = tuple(v for v in values if v >= threshold)
            bar_width = 0.03
            x_range = (-_LIVE_WINDOW_MS / 1000.0 - 0.3, 0.1)
            self.frametime_plot.setLabel(
                "bottom", "seconds ago", color=theme.DNA_TEXT_MUTED
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
                "bottom", "minutes ago", color=theme.DNA_TEXT_MUTED
            )
        # The mock's y-ceiling: spikes keep their height, extremes clamp.
        ceiling = max(
            summary.median_frametime_ms * 2.4, summary.p99_frametime_ms * 1.85
        )
        spikes_h = tuple(min(height, ceiling) for height in spikes_h)
        self.frametime_curve.setData(xs, values)
        self.stutter_bars.setOpts(x=spikes_x, height=spikes_h, width=bar_width)
        self.median_line.setValue(summary.median_frametime_ms)
        self.p99_line.setValue(summary.p99_frametime_ms)
        if values:
            # Explicit ticks, mock-style: 0 is always on the axis.
            step = 5 if ceiling <= 25 else 10 if ceiling <= 50 else 20
            self.frametime_plot.getAxis("left").setTicks(
                [[(v, str(v)) for v in range(0, int(ceiling) + 1, step)]]
            )
            self.frametime_plot.setXRange(*x_range, padding=0.0)
            self.frametime_plot.setYRange(0.0, ceiling * 1.04, padding=0.0)

    # ---------- events ----------

    def _live_toggled(self, checked: bool) -> None:
        self._live_mode = bool(checked)
        self.refresh()

    def _timer_tick(self) -> None:
        if self.widget.isVisible() and self._app_id:
            self.refresh()
