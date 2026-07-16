"""The Frame DNA tab: one game's full telemetry detail.

Large fingerprint + stat row, the MangoHUD-style frametime graph (30-minute
overview or the live last-10-seconds zoom), and the operating orbit
(power x clock, colored by the profile tier active at each sample). Reads
the daemon's ring for the selected game and refreshes while visible.
Visuals follow the approved mockup via the ``theme.DNA_*`` palette.
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

        root = QtWidgets.QVBoxLayout(self.widget)
        root.setContentsMargins(14, 12, 14, 12)
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
            cell = QtWidgets.QVBoxLayout()
            cell.setSpacing(1)
            key_label = QtWidgets.QLabel(key)
            key_label.setObjectName("frameDnaStatKey")
            value_label = QtWidgets.QLabel("")
            value_label.setObjectName("frameDnaStatValue")
            cell.addWidget(key_label)
            cell.addWidget(value_label)
            stats.addLayout(cell, index // 3, index % 3)
            self._stat_values[key] = value_label
        stats.setRowStretch(2, 1)
        stats.setColumnStretch(3, 1)
        top.addLayout(stats, 1)
        detail.addLayout(top)

        detail.addWidget(self._build_frametime_figure())
        detail.addWidget(self._build_orbit_figure())
        detail.addStretch(1)
        self._stack.addWidget(detail_page)

        self._refresh_timer = QtCore.QTimer(self.widget)
        self._refresh_timer.setInterval(_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._timer_tick)
        self._refresh_timer.start()

    # ---------- figures ----------

    def _figure(self, title: str, caption: str):
        QtWidgets = self.QtWidgets
        frame = QtWidgets.QFrame()
        frame.setObjectName("frameDnaFigure")
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(6)
        header = QtWidgets.QHBoxLayout()
        section = QtWidgets.QLabel(title)
        section.setObjectName("frameDnaSection")
        header.addWidget(section, 0)
        header.addStretch(1)
        layout.addLayout(header)
        caption_label = QtWidgets.QLabel(caption)
        caption_label.setObjectName("frameDnaCaption")
        caption_label.setWordWrap(True)
        return frame, layout, header, caption_label

    def _styled_plot(self, *, x_label: str, y_label: str):
        pg = self.pg
        plot = pg.PlotWidget()
        plot.setMenuEnabled(False)
        if hasattr(pg, "setConfigOptions"):
            pg.setConfigOptions(antialias=True)
        plot.setBackground(theme.DNA_SURFACE)
        plot.showGrid(x=True, y=True, alpha=0.16)
        axis_pen = pg.mkPen(theme.DNA_AXIS, width=1)
        text_color = theme.DNA_TEXT_MUTED
        for name, label in (("bottom", x_label), ("left", y_label)):
            axis = plot.getAxis(name)
            axis.setPen(axis_pen)
            axis.setTextPen(pg.mkPen(text_color))
            if hasattr(axis, "enableAutoSIPrefix"):
                axis.enableAutoSIPrefix(False)
        plot.setLabel("bottom", x_label, color=text_color)
        plot.setLabel("left", y_label, color=text_color)
        plot.setMinimumHeight(190)
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
        ref_pen = pg.mkPen(theme.DNA_AXIS, width=1)
        emphasis_pen = pg.mkPen(theme.DNA_TEXT_DIM, width=1)
        label_opts = {"color": theme.DNA_TEXT_DIM, "position": 0.94, "fill": None}
        self.median_line = pg.InfiniteLine(
            angle=0, movable=False, pen=ref_pen,
            label="median {value:.1f} ms", labelOpts=dict(label_opts),
        )
        self.p99_line = pg.InfiniteLine(
            angle=0, movable=False, pen=emphasis_pen,
            label="1%-low {value:.1f} ms", labelOpts=dict(label_opts),
        )
        self.frametime_plot.addItem(self.median_line, ignoreBounds=True)
        self.frametime_plot.addItem(self.p99_line, ignoreBounds=True)
        layout.addWidget(self.frametime_plot)
        layout.addWidget(caption)
        self._frametime_caption = caption
        return frame

    def _build_orbit_figure(self):
        pg = self.pg
        frame, layout, _header, caption = self._figure(
            "Operating orbit — power × clock, by profile",
            "every 1 Hz sample · colored by the active profile — "
            "green Efficiency · blue Balanced · red Performance",
        )
        self.orbit_plot = self._styled_plot(
            x_label="power (W)", y_label="clock (MHz)"
        )
        self.orbit_scatter = pg.ScatterPlotItem(size=6, pen=None)
        self.orbit_plot.addItem(self.orbit_scatter)
        layout.addWidget(self.orbit_plot)
        layout.addWidget(caption)
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
        self._stat_values["LATENCY"].setText(f"{summary.latency_ms:.0f} ms")
        self._render_frametime(history, summary, color)
        self._render_orbit(history)

    def _render_frametime(
        self, history: FrameHistory, summary: FrameHistorySummary, color: str
    ) -> None:
        pg = self.pg
        threshold = summary.median_frametime_ms * _STUTTER_RATIO
        if self._live_mode:
            xs, values = live_tail(history.frametimes_ms)
            spikes_x = tuple(x for x, v in zip(xs, values) if v >= threshold)
            spikes_h = tuple(v for v in values if v >= threshold)
            bar_width = 0.03
            self.frametime_plot.setLabel(
                "bottom", "seconds ago", color=theme.DNA_TEXT_MUTED
            )
        else:
            xs, values, maxes = densify_frametimes(history.frametimes_ms)
            spikes_x = tuple(x for x, m in zip(xs, maxes) if m >= threshold)
            spikes_h = tuple(m for m in maxes if m >= threshold)
            bar_width = (abs(xs[0]) / _OVERVIEW_COLUMNS * 0.35) if xs else 0.01
            self.frametime_plot.setLabel(
                "bottom", "minutes ago", color=theme.DNA_TEXT_MUTED
            )
        # The mock's y-ceiling: spikes keep their height, extremes clamp.
        ceiling = max(
            summary.median_frametime_ms * 2.4, summary.p99_frametime_ms * 1.85
        )
        spikes_h = tuple(min(height, ceiling) for height in spikes_h)
        self.frametime_curve.setData(xs, values)
        self.frametime_curve.setPen(pg.mkPen(color, width=1.4))
        self.frametime_curve.setBrush(_mk_color(self.QtGui, color, 38))
        self.stutter_bars.setOpts(
            x=spikes_x, height=spikes_h, width=bar_width,
            brush=_mk_color(self.QtGui, theme.DNA_STUTTER, 230), pen=None,
        )
        self.median_line.setValue(summary.median_frametime_ms)
        self.p99_line.setValue(summary.p99_frametime_ms)
        if values:
            self.frametime_plot.setYRange(0.0, ceiling, padding=0.05)

    def _render_orbit(self, history: FrameHistory) -> None:
        pg = self.pg
        spots = [
            {
                "pos": (sample.power_w, sample.clock_mhz),
                "brush": _mk_color(self.QtGui, tier_color(sample.tier), 158),
            }
            for sample in history.samples
            if sample.power_w > 0 and sample.clock_mhz > 0
        ]
        self.orbit_scatter.setData(spots=spots)
        if spots:
            powers = [s["pos"][0] for s in spots]
            clocks = [s["pos"][1] for s in spots]
            self.orbit_plot.setXRange(min(powers) - 12, max(powers) + 12, padding=0)
            self.orbit_plot.setYRange(min(clocks) - 50, max(clocks) + 50, padding=0)
        _ = pg  # orbit styling is data-driven; pg kept for symmetry

    # ---------- events ----------

    def _live_toggled(self, checked: bool) -> None:
        self._live_mode = bool(checked)
        self.refresh()

    def _timer_tick(self) -> None:
        if self.widget.isVisible() and self._app_id:
            self.refresh()
