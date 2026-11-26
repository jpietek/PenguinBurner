"""The Game Perf Profile page: one game's frame-ring detail.

After PR #137 (``origin/build-pr-137`` @ 611e6d3), as a Game Library
drill-down:
a header with Back, breadcrumb, title, meta line and tier pill; the row of
cards on top (Fingerprint · Frame rate · GPU and system, reflowing 3 -> 2 -> 1
columns at 960 / 720 px); the frametime figure below. Header and cards
scroll together, as on every page. It refreshes from the source every 2 s
(1 s in Live while the ring records) and only while shown.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from ui.components.page_header import PAGE_MARGINS
from ui.components.perf_entry import keyboard_focus_ring
from ui.components.perf_fingerprint import (
    PerfFingerprint,
    axes_tooltip,
    perf_profile_axes,
)
from ui.components.perf_profile_plot import (
    FrametimeFigure,
    LegendSwatch,
    PerfProfileSource,
    PerfRecording,
    PerfSummary,
    figure_stylesheet,
    latency_text,
    meta_parts,
    refresh_interval_ms,
    tier_color,
    tier_label,
    warming_text,
)
from ui.components.profile_style import Pill
from ui.design.style import COL, GAP, RAD, TYPE, WEIGHT, focus_visible, qcolor

THREE_COLUMN_WIDTH = 960
TWO_COLUMN_WIDTH = 720
EMPTY_HINT = (
    "Select a game in the Game Library tab and click its Perf Profile button.\n"
    "Telemetry is recorded while a game's PenguinBurner profile runs."
)


def no_recording_text(name: str) -> str:
    return (
        f"No telemetry captured for {name} yet.\n"
        "Play it with PenguinBurner enabled to record a window."
    )


def capture_off_text(name: str) -> str:
    return (
        f"Capture telemetry is off for {name}.\n"
        "Turn it on, then play the game to record a window."
    )


def column_mode(width: int) -> int:
    return 3 if width >= THREE_COLUMN_WIDTH else 2 if width >= TWO_COLUMN_WIDTH else 1


def _back_icon(dpr: float) -> G.QIcon:
    size = 16
    pixmap = G.QPixmap(round(size * dpr), round(size * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(C.Qt.GlobalColor.transparent)
    painter = G.QPainter(pixmap)
    painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
    pen = G.QPen(qcolor("ink"), 1.6)
    pen.setCapStyle(C.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(C.Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawLine(C.QPointF(3, 8), C.QPointF(13, 8))
    painter.drawPolyline(
        G.QPolygonF([C.QPointF(7.5, 3.5), C.QPointF(3, 8), C.QPointF(7.5, 12.5)])
    )
    painter.end()
    return G.QIcon(pixmap)


class _Card(W.QFrame):
    """A titled card: 17 px title, 12 px secondary note, optional controls
    on the right, then a body below a divider."""

    def __init__(self, title: str, note: str, controls: W.QWidget | None = None):
        super().__init__()
        self.setObjectName("perfCard")
        outer = W.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        head = W.QWidget()
        head.setObjectName("perfCardHead")
        row = W.QHBoxLayout(head)
        row.setContentsMargins(20, GAP["lg"], GAP["lg"] + 2, GAP["md"] + 2)
        row.setSpacing(GAP["md"])
        titles = W.QVBoxLayout()
        titles.setSpacing(2)
        self.title = W.QLabel(title)
        self.title.setObjectName("perfCardTitle")
        titles.addWidget(self.title)
        self.note = W.QLabel(note)
        self.note.setObjectName("perfCardNote")
        titles.addWidget(self.note)
        row.addLayout(titles, 1)
        if controls is not None:
            row.addWidget(controls, 0, C.Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(head)
        self.body = W.QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        outer.addLayout(self.body, 1)

    def add_rule(self) -> None:
        rule = W.QFrame()
        rule.setObjectName("perfRule")
        rule.setFixedHeight(1)
        self.body.addWidget(rule)


class _StatRow(W.QFrame):
    """Label left, value right (ink) with an optional quiet note after it."""

    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("perfStatRow")
        self.setMinimumHeight(32)
        row = W.QHBoxLayout(self)
        row.setContentsMargins(20, GAP["sm"], 20, GAP["sm"])
        row.setSpacing(GAP["md"])
        self.title = W.QLabel(title)
        self.title.setObjectName("perfStatTitle")
        row.addWidget(self.title, 1)
        self.value = W.QLabel("")
        self.value.setObjectName("perfStatValue")
        self.value.setTextFormat(C.Qt.TextFormat.RichText)
        self.value.setAlignment(
            C.Qt.AlignmentFlag.AlignRight | C.Qt.AlignmentFlag.AlignVCenter
        )
        row.addWidget(self.value)

    def set_value(self, value: str, note: str = "") -> None:
        text = f'<span style="color:{COL["ink"]};font-weight:{WEIGHT["semibold"]}">{value}</span>'
        if note:
            text += f'<span style="color:{COL["quiet"]}"> · {note}</span>'
        self.value.setText(text)
        self.setAccessibleName(
            f"{self.title.text()}: {value}" + (f", {note}" if note else "")
        )


class _Metric(W.QWidget):
    """A headline figure: caption, big fps number, ms underneath."""

    def __init__(self, label: str):
        super().__init__()
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        caption = W.QLabel(label)
        caption.setObjectName("perfMetricLabel")
        layout.addWidget(caption)
        self.number = W.QLabel("")
        self.number.setObjectName("perfMetricNumber")
        self.number.setTextFormat(C.Qt.TextFormat.RichText)
        layout.addWidget(self.number)
        self.sub = W.QLabel("")
        self.sub.setObjectName("perfMetricSub")
        layout.addWidget(self.sub)
        self.label = label

    def set_value(self, fps: float, ms: float) -> None:
        self.number.setText(
            f'{fps:.0f}<span style="font-size:{TYPE["caption"]}px;font-weight:{WEIGHT["regular"]};'
            f'color:{COL["secondary"]}">&nbsp;fps</span>'
        )
        self.sub.setText(f"{ms:.1f} ms")
        self.setAccessibleName(f"{self.label}: {fps:.0f} fps, {ms:.1f} ms")


class _DotKey(W.QWidget):
    """Fingerprint dot key: In range · Out of range · No range."""

    def __init__(self):
        super().__init__()
        self.setObjectName("perfDotKey")
        self.setAttribute(C.Qt.WidgetAttribute.WA_StyledBackground, True)
        row = W.QHBoxLayout(self)
        row.setContentsMargins(GAP["lg"], GAP["sm"] + 2, GAP["lg"], GAP["sm"] + 2)
        row.setSpacing(GAP["lg"])
        for label, kind, color in (
            ("In range", "dot", "accent"),
            ("Out of range", "hollow", "warning"),
            ("No range", "dot", "quiet"),
        ):
            entry = W.QHBoxLayout()
            entry.setSpacing(6)
            entry.addWidget(
                LegendSwatch(kind, color), 0, C.Qt.AlignmentFlag.AlignVCenter
            )
            text = W.QLabel(label)
            text.setObjectName("perfLegendLabel")
            entry.addWidget(text)
            row.addLayout(entry)
        row.addStretch(1)


class PerfProfilePage(W.QWidget):
    """``PerfProfilePage(source, on_back=...)``; ``show_game(key)`` opens one game.

    ``source`` is any ``PerfProfileSource``. ``on_back`` runs when the user
    presses Back to Game Library (the library restores its selection);
    ``on_enable_capture(key)`` turns Capture telemetry on for a game whose
    capture is off (the empty state offers it when given).
    """

    def __init__(
        self,
        source: PerfProfileSource,
        *,
        on_back: Callable[[], None],
        on_enable_capture: Callable[[str], None] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("perfProfilePage")
        self.setAttribute(C.Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(focus_visible(page_stylesheet()))
        self.source = source
        self._on_back = on_back
        self._on_enable_capture = on_enable_capture
        self.game_key = ""
        self.recording: PerfRecording | None = None
        self.summary: PerfSummary | None = None
        self._rendered: tuple | None = None
        self._columns = 0
        self.state = "nogame"

        root = W.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.scroll_area = W.QScrollArea()
        self.scroll_area.setObjectName("perfScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(W.QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(
            C.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        root.addWidget(self.scroll_area)
        content = W.QWidget()
        content.setObjectName("perfContent")
        column = W.QVBoxLayout(content)
        column.setContentsMargins(*PAGE_MARGINS)
        column.setSpacing(0)
        column.addLayout(self._build_header())
        column.addSpacing(GAP["lg"])
        # Message and detail are shown one at a time (not a stacked widget:
        # the short empty state must not inherit the detail page's height).
        self.message_page = self._build_message_page()
        self.detail_page = self._build_detail_page()
        column.addWidget(self.message_page)
        column.addWidget(self.detail_page, 1)
        # Takes the slack when only the short message shows.
        column.addStretch(0)
        self.scroll_area.setWidget(content)
        self.scroll_area.viewport().installEventFilter(self)
        self.reflow(1300)

        self.timer = C.QTimer(self)
        self.timer.setInterval(refresh_interval_ms(None, False))
        self.timer.timeout.connect(self._tick)
        self.show_game("")

    # ----- build -----
    def _build_header(self) -> W.QVBoxLayout:
        header = W.QVBoxLayout()
        header.setSpacing(GAP["md"])
        self.backline = W.QWidget()
        back = W.QHBoxLayout(self.backline)
        back.setContentsMargins(0, 0, 0, 0)
        back.setSpacing(GAP["md"])
        self.back_button = W.QPushButton("Back to Game Library")
        self.back_button.setObjectName("perfBack")
        self.back_button.setIcon(_back_icon(self.devicePixelRatioF() or 1.0))
        self.back_button.setIconSize(C.QSize(16, 16))
        self.back_button.clicked.connect(self._back)
        keyboard_focus_ring(self.back_button)
        back.addWidget(self.back_button)
        crumb = W.QLabel("Game Library / Perf Profile")
        crumb.setObjectName("perfCrumb")
        back.addWidget(crumb)
        back.addStretch(1)
        header.addWidget(self.backline)
        title_row = W.QHBoxLayout()
        title_row.setSpacing(GAP["lg"])
        heading = W.QVBoxLayout()
        heading.setSpacing(GAP["xs"])
        self.title = W.QLabel("")
        self.title.setObjectName("perfTitle")
        # Launcher-supplied names are text, never markup.
        self.title.setTextFormat(C.Qt.TextFormat.PlainText)
        heading.addWidget(self.title)
        self.meta = W.QLabel("")
        self.meta.setObjectName("perfMeta")
        self.meta.setTextFormat(C.Qt.TextFormat.RichText)
        heading.addWidget(self.meta)
        title_row.addLayout(heading, 1)
        self.tier_chip = Pill("", "quiet")
        self.tier_chip.setObjectName("perfTierChip")
        self.tier_chip.setToolTip("Profile tier while this was recorded")
        title_row.addWidget(self.tier_chip, 0, C.Qt.AlignmentFlag.AlignTop)
        header.addLayout(title_row)
        return header

    def _build_message_page(self) -> W.QWidget:
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        card = W.QFrame()
        card.setObjectName("perfCard")
        inner = W.QVBoxLayout(card)
        inner.setContentsMargins(20, 48, 20, 48)
        inner.setSpacing(GAP["md"])
        self.empty_fingerprint = PerfFingerprint()
        self.empty_fingerprint.setObjectName("perfEmptyFingerprint")
        self.empty_fingerprint.setMinimumHeight(0)
        self.empty_fingerprint.setFixedSize(72, 72)
        self.empty_fingerprint.labels = False
        self.empty_fingerprint.set_state(None)
        inner.addWidget(self.empty_fingerprint, 0, C.Qt.AlignmentFlag.AlignHCenter)
        self.message = W.QLabel(EMPTY_HINT)
        self.message.setObjectName("perfEmptyText")
        self.message.setTextFormat(C.Qt.TextFormat.PlainText)
        self.message.setAlignment(C.Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        inner.addWidget(self.message)
        self.capture_button = W.QPushButton("Turn on Capture telemetry")
        self.capture_button.setObjectName("perfEnableCapture")
        self.capture_button.clicked.connect(self._enable_capture)
        keyboard_focus_ring(self.capture_button)
        inner.addWidget(self.capture_button, 0, C.Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(card)
        return page

    def _build_detail_page(self) -> W.QWidget:
        content = W.QWidget()
        content.setObjectName("perfDetail")
        self.grid = W.QGridLayout(content)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(GAP["lg"])
        self.grid.setVerticalSpacing(GAP["lg"])

        self.fingerprint_card = _Card("Fingerprint", "Each spoke on its own scale")
        self.fingerprint_card.add_rule()
        self.fingerprint = PerfFingerprint()
        # Short enough that the frametime figure fits a 900 px window.
        self.fingerprint.setMinimumHeight(180)
        self.fingerprint_card.body.addWidget(self.fingerprint, 1)
        self.dot_key = _DotKey()
        self.fingerprint_card.body.addWidget(self.dot_key)

        self.frame_rate_card = _Card("Frame rate", "Window medians")
        self.frame_rate_card.add_rule()
        metrics = W.QWidget()
        metrics.setObjectName("perfMetrics")
        grid = W.QGridLayout(metrics)
        grid.setContentsMargins(20, GAP["lg"], 20, GAP["lg"])
        grid.setHorizontalSpacing(GAP["lg"])
        self.median_metric = _Metric("Median")
        self.low_metric = _Metric("1% low")
        grid.addWidget(self.median_metric, 0, 0, C.Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self.low_metric, 0, 1, C.Qt.AlignmentFlag.AlignTop)
        self.frame_rate_card.body.addWidget(metrics, 0)
        self.target_row = _StatRow("Target")
        self.latency_row = _StatRow("Latency")
        self.frame_rate_card.body.addWidget(self.target_row)
        self.frame_rate_card.body.addWidget(self.latency_row)
        self.frame_rate_card.body.addStretch(1)

        self.system_card = _Card("GPU and system", "Window medians")
        self.system_rows = {
            key: _StatRow(title)
            for key, title in (
                ("power", "Power draw"),
                ("clock", "GPU clock"),
                ("load", "GPU / busiest CPU thread"),
                ("temp", "GPU temperature"),
                ("fan", "Fan"),
            )
        }
        for row in self.system_rows.values():
            self.system_card.body.addWidget(row, 1)

        self.figure = FrametimeFigure()
        self.figure_card = _Card(
            "Frametime", "Every frame, in milliseconds", self.figure.controls
        )
        self.figure_card.add_rule()
        self.figure_card.body.addWidget(self.figure, 1)
        self.figure.live_changed.connect(lambda _on: self.refresh(force=True))
        self.figure.window_changed.connect(lambda _s: self.refresh(force=True))
        return content

    # ----- layout -----
    def eventFilter(self, watched, event):
        if (
            watched is self.scroll_area.viewport()
            and event.type() == C.QEvent.Type.Resize
        ):
            self.reflow(watched.width())
        return super().eventFilter(watched, event)

    def reflow(self, width: int) -> None:
        """Cards on top: three columns, then two (fingerprint spanning both
        rows), then one; the frametime figure full width below them."""
        mode = column_mode(width)
        if mode == self._columns:
            return
        self._columns = mode
        grid = self.grid
        cards = (self.fingerprint_card, self.frame_rate_card, self.system_card)
        for widget in (*cards, self.figure_card):
            grid.removeWidget(widget)
        for index in range(5):
            grid.setColumnStretch(index, 0)
            grid.setRowStretch(index, 0)
        if mode == 3:
            for column, card in enumerate(cards):
                grid.addWidget(card, 0, column)
            for column, stretch in enumerate((10, 11, 11)):
                grid.setColumnStretch(column, stretch)
            figure_row, span = 1, 3
        elif mode == 2:
            grid.addWidget(self.fingerprint_card, 0, 0, 2, 1)
            grid.addWidget(self.frame_rate_card, 0, 1)
            grid.addWidget(self.system_card, 1, 1)
            grid.setColumnStretch(0, 10)
            grid.setColumnStretch(1, 13)
            figure_row, span = 2, 2
        else:
            for row, card in enumerate(cards):
                grid.addWidget(card, row, 0)
            grid.setColumnStretch(0, 1)
            figure_row, span = 3, 1
        grid.addWidget(self.figure_card, figure_row, 0, 1, span)
        grid.setRowStretch(figure_row, 1)

    @property
    def columns(self) -> int:
        return self._columns

    # ----- data flow -----
    def show_game(self, game_key: str | None) -> None:
        """Show one game by its ``<launcher>:<id>`` key; empty shows the hint."""
        key = game_key or ""
        changed = key != self.game_key
        # The key first: resetting Live re-renders, and must render this game.
        self.game_key = key
        if changed:
            with C.QSignalBlocker(self.figure):
                self.figure.set_live(False)
        self._rendered = None
        self.refresh(force=True)
        self.scroll_area.verticalScrollBar().setValue(0)

    def refresh(self, *, force: bool = False) -> None:
        if not self.game_key:
            self._show_message("nogame", EMPTY_HINT, None)
            return
        name = self.source.game_name(self.game_key)
        recording = self.source.record(self.game_key)
        summary = recording.summary if recording is not None else None
        self.recording, self.summary = recording, summary
        if recording is None or summary is None:
            if self.source.capture_enabled(self.game_key):
                self._show_message("norecord", no_recording_text(name), name)
            else:
                self._show_message("captureoff", capture_off_text(name), name)
            return
        self.state = "detail"
        self.timer.setInterval(refresh_interval_ms(recording, self.figure.live))
        self.message_page.hide()
        self.detail_page.show()
        self.backline.setVisible(True)
        key = (recording.version, self.figure.live, self.figure.window_s)
        if not force and key == self._rendered:
            return
        self._rendered = key
        self._populate(name, recording, summary)

    def _show_message(self, state: str, text: str, name: str | None) -> None:
        self.state = state
        self.recording = self.summary = None
        self._rendered = None
        self.timer.setInterval(refresh_interval_ms(None, False))
        self.backline.setVisible(name is not None)
        self.title.setText(name or "Perf Profile")
        self.meta.setText("")
        self.meta.setVisible(False)
        self.tier_chip.setVisible(False)
        self.message.setText(text)
        self.capture_button.setVisible(
            state == "captureoff" and self._on_enable_capture is not None
        )
        self.detail_page.hide()
        self.message_page.show()

    def _populate(
        self, name: str, recording: PerfRecording, summary: PerfSummary
    ) -> None:
        self.title.setText(name)
        parts = []
        for part in meta_parts(recording, summary):
            if part == "● REC":
                part = (
                    f'<span style="color:{COL["accent"]};font-weight:{WEIGHT["semibold"]}">'
                    "● REC</span>"
                )
            parts.append(part)
        self.meta.setText(" · ".join(parts))
        self.meta.setVisible(True)
        self.tier_chip.set_status(
            tier_label(recording.tier), "quiet", dot_color=tier_color(recording.tier)
        )
        self.tier_chip.setVisible(True)

        # A short archived session keeps the dashed outline; a live ring
        # draws its shape early and the meta line says it is settling.
        settled = summary.qualified or recording.live
        axes = perf_profile_axes(
            summary,
            target_fps=recording.target_fps,
            power_limit_w=recording.power_limit_w,
        )
        self.fingerprint.set_state(
            axes if settled else None,
            tier=recording.tier,
            warming_minutes=None if settled else summary.minutes,
        )
        tooltip = axes_tooltip(axes) if settled else warming_text(summary.minutes)
        self.fingerprint.setToolTip(tooltip)
        self.fingerprint.setAccessibleDescription(tooltip.replace("\n", ", "))
        self.dot_key.setVisible(settled)
        # Without the key below it, the fingerprint ends the card.
        self.fingerprint.bottom_radius = 0.0 if settled else RAD["panel"] - 1.0

        self.median_metric.set_value(
            summary.median_present_fps, summary.median_frametime_ms
        )
        self.low_metric.set_value(summary.low_1pct_fps, summary.p99_frametime_ms)
        self.target_row.set_value(
            f"{recording.target_fps:.0f} fps",
            "This game" if recording.target_is_game else "System-wide",
        )
        # Latency has no placeholder: the row exists only with a reading.
        has_latency = summary.latency_ms > 0
        self.latency_row.setVisible(has_latency)
        if has_latency:
            self.latency_row.set_value(
                latency_text(summary),
                "Estimate" if summary.latency_estimated else "Measured",
            )
            self.latency_row.setToolTip(
                "~ marks an estimate, as the overlay's LAT."
                if summary.latency_estimated
                else ""
            )
        rows = self.system_rows
        rows["power"].set_value(f"{summary.median_power_w} W")
        rows["clock"].set_value(f"{summary.median_clock_mhz} MHz")
        rows["load"].set_value(
            f"{summary.gpu_util_pct}% / {summary.cpu_peak_thread_pct}%"
        )
        rows["temp"].set_value(f"{summary.median_temperature_c:.0f} °C")
        rows["fan"].set_value(f"{summary.median_fan_pct:.0f}%")
        self.figure.show_recording(recording, summary)

    def _back(self) -> None:
        self._on_back()

    def _enable_capture(self) -> None:
        if self._on_enable_capture is not None and self.game_key:
            self._on_enable_capture(self.game_key)
            self.refresh(force=True)

    def event(self, event):
        # The back arrow is drawn at the screen's pixel ratio.
        if event.type() == C.QEvent.Type.DevicePixelRatioChange:
            self.back_button.setIcon(_back_icon(self.devicePixelRatioF() or 1.0))
        return super().event(event)

    # ----- refresh cadence -----
    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()
        self.refresh()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()

    def _tick(self) -> None:
        """Never spend a decode nobody can see: a hidden page has no timer,
        and a minimized window skips the tick."""
        window = self.window()
        if not self.isVisible() or (window is not None and window.isMinimized()):
            return
        self.refresh()


def page_stylesheet() -> str:
    c, t, w, r = COL, TYPE, WEIGHT, RAD
    return f"""
        QWidget#perfProfilePage, QWidget#perfContent, QWidget#perfDetail,
        QScrollArea#perfScroll {{ background: {c["window"]}; }}
        QScrollArea#perfScroll {{ border: none; }}
        QLabel#perfTitle {{ font-size: {t["title"]}px; font-weight: {w["bold"]}; color: {c["ink"]}; }}
        QLabel#perfMeta, QLabel#perfCrumb {{ font-size: {t["caption"]}px; color: {c["secondary"]}; }}
        QFrame#perfCard {{ background: {c["panel"]}; border: 1px solid {c["border"]}; border-radius: {r["panel"]}px; }}
        QWidget#perfCardHead, QWidget#perfMetrics, QWidget#perfLegend,
        QWidget#perfLiveControls {{ background: transparent; }}
        QLabel#perfCardTitle {{ font-size: {t["section"]}px; font-weight: {w["semibold"]}; color: {c["ink"]}; background: transparent; }}
        QLabel#perfCardNote, QLabel#perfMetricLabel {{ font-size: {t["caption"]}px; color: {c["secondary"]}; background: transparent; }}
        QLabel#perfMetricNumber {{ font-size: {t["title"]}px; font-weight: {w["bold"]}; color: {c["ink"]}; background: transparent; }}
        QLabel#perfMetricSub {{ font-size: {t["caption"]}px; color: {c["quiet"]}; background: transparent; }}
        QFrame#perfStatRow {{ background: transparent; border: none; border-top: 1px solid {c["divider"]}; border-radius: 0; }}
        QLabel#perfStatTitle {{ color: {c["secondary"]}; background: transparent; border: none; }}
        QLabel#perfStatValue {{ background: transparent; border: none; }}
        QLabel#perfEmptyText {{ color: {c["secondary"]}; background: transparent; }}
        QPushButton#perfBack, QPushButton#perfBack:focus {{ border: 1px solid {c["border"]}; padding: 6px 12px 6px 10px; }}
        QPushButton#perfBack:hover {{ border-color: {c["quiet"]}; }}
        QPushButton#perfBack[keyboardFocus="true"]:focus {{ border: 2px solid {c["focus"]}; padding: 5px 11px 5px 9px; }}
        QWidget#perfDotKey {{ border-top: 1px solid {c["divider"]}; }}
        {figure_stylesheet()}
    """
