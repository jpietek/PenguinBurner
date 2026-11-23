"""The Game Library's Perf Profile entry: sparkline icon, button and peek.

After PR #137 (``origin/build-pr-137`` @ 611e6d3,
``ui/components/game_perf_profile_badge.py`` and ``GamePerfProfilePeek`` in
``ui/components/game_perf_profile.py``): one button beside Play for every
game. Until a ring exists it looks disabled and says how to record one; hover
or keyboard focus peeks the fingerprint and the numbers that matter, Esc
closes the peek, and a click opens the full view. The icon is that game's own
frametime ribbon in miniature.
"""

from __future__ import annotations

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from ui.components.perf_fingerprint import fingerprint_pixmap, perf_profile_axes
from ui.components.perf_profile_plot import (
    PerfProfileSource,
    PerfRecording,
    densify_frametimes,
    frametime_ceiling,
    hitch_points,
    latency_text,
    percentile,
    warming_text,
)
from ui.design.style import COL, GAP, RAD, TYPE, WEIGHT, focus_visible, qcolor

SPARK_WIDTH = 44
SPARK_HEIGHT = 18
# Room between the sparkline and the label; Qt's own icon gap is ~4 px.
ICON_GAP = 4
PEEK_FINGERPRINT = 124
PERF_UNAVAILABLE = (
    "Game Perf Profile unavailable — no telemetry recorded for this game yet. "
    "Wrap the game, pick a mode other than Stock, keep Capture telemetry on, "
    "and play it."
)
PERF_CAPTURE_OFF = (
    "Game Perf Profile unavailable — Capture telemetry is off for this game. "
    "Turn it on in the game's settings, then play it to record."
)
PEEK_HINT = "Click Perf Profile to open the full view"
_KEYBOARD_REASONS = (
    C.Qt.FocusReason.TabFocusReason,
    C.Qt.FocusReason.BacktabFocusReason,
    C.Qt.FocusReason.ShortcutFocusReason,
)


def perf_sparkline_pixmap(
    summary_or_columns,
    *,
    width: int = SPARK_WIDTH,
    height: int = SPARK_HEIGHT,
    dpr: float = 1.0,
    live: bool = False,
) -> G.QPixmap:
    """A miniature frametime ribbon: the median line over its p99 band in
    accent, a severe dot at the worst hitch, a live dot at the newest end.

    ``summary_or_columns`` is a ``PerfRecording``, a densified
    ``(xs, p50s, p99s, maxes)`` tuple, or ``None``/empty for a game with no
    recording, which draws a flat dashed quiet line.
    """
    columns: tuple[tuple[float, ...], ...]
    if isinstance(summary_or_columns, PerfRecording):
        live = live or summary_or_columns.live
        # Two pixels a column keeps the line smooth at this size.
        columns = densify_frametimes(
            summary_or_columns.frametimes_ms, columns=max(2, width // 2)
        )
    else:
        columns = summary_or_columns or ((), (), (), ())
    columns = tuple(columns)
    xs, medians, p99s, maxes = columns
    ratio = max(1.0, float(dpr))
    pixmap = G.QPixmap(round(width * ratio), round(height * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(C.Qt.GlobalColor.transparent)
    painter = G.QPainter(pixmap)
    try:
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing, True)
        if len(xs) < 2:
            pen = G.QPen(qcolor("quiet"), 1.2)
            pen.setDashPattern([3, 2.5])
            painter.setPen(pen)
            painter.drawLine(C.QPointF(1, height / 2), C.QPointF(width - 1, height / 2))
            return pixmap
        # Scaled to the ribbon, not the full figure's ceiling: at 18 px the
        # shape is the information. The top row is kept for the hitch dot.
        ordered_p99 = sorted(p99s)
        ceiling = max(
            percentile(ordered_p99, 0.9) * 1.25,
            max(medians) * 1.3,
            1.0,
        )
        ceiling = min(
            ceiling,
            frametime_ceiling(sorted(medians)[len(medians) // 2], ordered_p99[-1]),
        )
        # A sparkline baseline sits under the lowest median, not at 0 ms.
        floor = min(medians) * 0.7
        lo, span = xs[0], (xs[-1] - xs[0]) or 1.0
        pad = 2.5
        top = 5.5 if live or hitch_points(xs, medians, maxes)[0] else 2.0

        def px(x: float) -> float:
            return pad + (width - 2 * pad) * (x - lo) / span

        def py(v: float) -> float:
            return (
                height
                - 1.5
                - (height - 1.5 - top) * (min(v, ceiling) - floor) / (ceiling - floor)
            )

        band = G.QPolygonF(
            [C.QPointF(px(x), py(v)) for x, v in zip(xs, medians, strict=True)]
            + [
                C.QPointF(px(x), py(v))
                for x, v in reversed(list(zip(xs, p99s, strict=True)))
            ]
        )
        painter.setPen(C.Qt.PenStyle.NoPen)
        painter.setBrush(qcolor("accent", 0.34))
        painter.drawPolygon(band)
        line = G.QPen(qcolor("accent"), 1.3)
        line.setJoinStyle(C.Qt.PenJoinStyle.RoundJoin)
        painter.setPen(line)
        painter.setBrush(C.Qt.BrushStyle.NoBrush)
        painter.drawPolyline(
            G.QPolygonF(
                [C.QPointF(px(x), py(v)) for x, v in zip(xs, medians, strict=True)]
            )
        )
        hitch_x, hitch_y = hitch_points(xs, medians, maxes)
        if hitch_x:
            worst = max(range(len(hitch_y)), key=hitch_y.__getitem__)
            painter.setPen(C.Qt.PenStyle.NoPen)
            painter.setBrush(qcolor("severe"))
            painter.drawEllipse(C.QPointF(px(hitch_x[worst]), 2.4), 2.2, 2.2)
        if live:
            # Recording now: a dot on the newest end of the ribbon.
            end = C.QPointF(width - 2.6, py(medians[-1]))
            painter.setPen(G.QPen(qcolor("control"), 1.2))
            painter.setBrush(qcolor("focus"))
            painter.drawEllipse(end, 2.5, 2.5)
    finally:
        painter.end()
    return pixmap


def _icon_with_gap(spark: G.QPixmap) -> G.QPixmap:
    ratio = spark.devicePixelRatio()
    pixmap = G.QPixmap(
        round((SPARK_WIDTH + ICON_GAP) * ratio), round(SPARK_HEIGHT * ratio)
    )
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(C.Qt.GlobalColor.transparent)
    painter = G.QPainter(pixmap)
    painter.drawPixmap(0, 0, spark)
    painter.end()
    return pixmap


def keyboard_focus_ring(widget: W.QWidget) -> None:
    """Show the focus ring only for keyboard focus (Tab, Backtab, shortcut),
    never after programmatic focus such as returning from the full view:
    sets the ``keyboardFocus`` property the style sheets select on."""

    class _Filter(C.QObject):
        def eventFilter(self, watched, event):
            kind = event.type()
            if kind in (C.QEvent.Type.FocusIn, C.QEvent.Type.FocusOut):
                keyboard = (
                    kind == C.QEvent.Type.FocusIn
                    and event.reason() in _KEYBOARD_REASONS
                )
                if watched.property("keyboardFocus") != keyboard:
                    watched.setProperty("keyboardFocus", keyboard)
                    watched.style().unpolish(watched)
                    watched.style().polish(watched)
            return False

    widget.setProperty("keyboardFocus", False)
    widget._keyboard_focus_filter = _Filter(widget)  # type: ignore[attr-defined]
    widget.installEventFilter(widget._keyboard_focus_filter)  # type: ignore[attr-defined]


class PerfPeek(W.QFrame):
    """The hover/focus popover: a 124 px fingerprint beside Median, 1% low,
    Power draw and (only when measured) Latency. A frameless tool-tip window;
    clicking the Perf Profile button, not this popover, opens the view."""

    def __init__(self, parent=None):
        super().__init__(
            parent,
            C.Qt.WindowType.ToolTip
            | C.Qt.WindowType.FramelessWindowHint
            | C.Qt.WindowType.NoDropShadowWindowHint,
        )
        # The rounded panel is painted in paintEvent; the window corners
        # outside its radius stay transparent instead of showing a square.
        self.setAttribute(C.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setObjectName("perfPeek")
        self.setStyleSheet(focus_visible(peek_stylesheet()))
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(GAP["md"], GAP["md"] - 2, GAP["md"], GAP["md"] - 2)
        layout.setSpacing(GAP["sm"])
        self.body = W.QWidget()
        self.body.setObjectName("perfPeekBody")
        body = W.QHBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(GAP["md"])
        self.fingerprint = W.QLabel()
        self.fingerprint.setFixedSize(PEEK_FINGERPRINT, PEEK_FINGERPRINT)
        body.addWidget(self.fingerprint, 0)
        stats = W.QGridLayout()
        stats.setHorizontalSpacing(18)
        stats.setVerticalSpacing(GAP["xs"] + 2)
        self.keys: dict[str, W.QLabel] = {}
        self.values: dict[str, W.QLabel] = {}
        for row, title in enumerate(("Median", "1% low", "Power draw", "Latency")):
            key = W.QLabel(title)
            key.setObjectName("perfPeekKey")
            value = W.QLabel("")
            value.setObjectName("perfPeekValue")
            value.setAlignment(
                C.Qt.AlignmentFlag.AlignRight | C.Qt.AlignmentFlag.AlignVCenter
            )
            stats.addWidget(key, row + 1, 0)
            stats.addWidget(value, row + 1, 1)
            self.keys[title], self.values[title] = key, value
        stats.setRowStretch(0, 1)
        stats.setRowStretch(5, 1)
        body.addLayout(stats, 1)
        layout.addWidget(self.body)
        self.message = W.QLabel("")
        self.message.setObjectName("perfPeekMessage")
        self.message.setTextFormat(C.Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setFixedWidth(300)
        layout.addWidget(self.message)
        self.hint = W.QLabel(PEEK_HINT)
        self.hint.setObjectName("perfPeekHint")
        layout.addWidget(self.hint)

    def paintEvent(self, _event):
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        painter.setPen(G.QPen(qcolor("border"), 1))
        painter.setBrush(qcolor("raised"))
        radius = RAD["panel"]
        painter.drawRoundedRect(
            C.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius
        )
        painter.end()

    def set_recording(
        self, recording: PerfRecording | None, *, message: str = "", dpr: float = 1.0
    ) -> None:
        """Fill for a recording; with none, show only ``message`` (how to record)."""
        summary = recording.summary if recording is not None else None
        self.body.setVisible(summary is not None)
        self.hint.setVisible(summary is not None)
        self.message.setVisible(summary is None or not summary.qualified)
        if recording is None or summary is None:
            self.message.setText(message or PERF_UNAVAILABLE)
            self.adjustSize()
            return
        settled = summary.qualified or recording.live
        axes = perf_profile_axes(
            summary,
            target_fps=recording.target_fps,
            power_limit_w=recording.power_limit_w,
        )
        self.fingerprint.setPixmap(
            fingerprint_pixmap(
                axes if settled else None,
                tier=recording.tier,
                size=PEEK_FINGERPRINT,
                labels=True,
                detailed=False,
                device_pixel_ratio=dpr,
            )
        )
        self.message.setText(warming_text(summary.minutes))
        self.values["Median"].setText(
            f"{summary.median_present_fps:.0f} fps · {summary.median_frametime_ms:.1f} ms"
        )
        self.values["1% low"].setText(
            f"{summary.low_1pct_fps:.0f} fps · {summary.p99_frametime_ms:.1f} ms"
        )
        self.values["Power draw"].setText(f"{summary.median_power_w} W")
        has_latency = summary.latency_ms > 0
        self.values["Latency"].setText(latency_text(summary) if has_latency else "")
        self.values["Latency"].setVisible(has_latency)
        self.keys["Latency"].setVisible(has_latency)
        self.adjustSize()

    def show_at(self, anchor: C.QPoint) -> None:
        """Open below-right-aligned to ``anchor`` (the button's bottom-right),
        kept on screen."""
        self.adjustSize()
        hint = self.sizeHint()
        position = C.QPoint(anchor.x() - hint.width(), anchor.y() + 6)
        screen = G.QGuiApplication.screenAt(anchor) or G.QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            position.setX(
                min(max(position.x(), area.left() + 8), area.right() - hint.width() - 8)
            )
            position.setY(min(position.y(), area.bottom() - hint.height() - 8))
        self.move(position)
        self.show()


class PerfEntryButton(W.QPushButton):
    """ "Perf Profile" beside Play, with the game's sparkline as its icon.

    ``sync(game_key)`` reads the source. Without a ring the button stays
    focusable but looks disabled, and its peek says how to record one; with a
    ring, hover or keyboard focus opens the peek and a click emits
    ``open_requested(game_key)``.
    """

    open_requested = C.Signal(str)

    def __init__(self, source: PerfProfileSource, parent=None):
        super().__init__("Perf Profile", parent)
        self.setObjectName("gamePerfProfileButton")
        self.setCursor(C.Qt.CursorShape.PointingHandCursor)
        self.setIconSize(C.QSize(SPARK_WIDTH + ICON_GAP, SPARK_HEIGHT))
        self.setStyleSheet(focus_visible(entry_stylesheet()))
        keyboard_focus_ring(self)
        self.source = source
        self.game_key = ""
        self.recording: PerfRecording | None = None
        self._message = ""
        self._capture: bool | None = None
        self._peek: PerfPeek | None = None
        # Opened by keyboard focus (stays while focused) or by hover (closes
        # when the pointer leaves, even after a click focused the button).
        self._peek_from_keyboard = False
        self.clicked.connect(self._open)
        self.sync("")

    @property
    def peek(self) -> PerfPeek:
        if self._peek is None:
            self._peek = PerfPeek(self)
        return self._peek

    @property
    def has_recording(self) -> bool:
        return self.recording is not None and self.recording.summary is not None

    def sync(self, game_key: str = "", *, capture: bool | None = None) -> None:
        """Refresh for the selected game (call again on each game-state poll).

        ``capture`` is the game's own Capture telemetry setting, as the
        library reads it; ``None`` asks the perf source.
        """
        if game_key != self.game_key or capture is not None:
            self._capture = capture
        self.game_key = game_key or ""
        self.setVisible(bool(self.game_key))
        self.recording = self.source.record(self.game_key) if self.game_key else None
        recording = self.recording
        summary = recording.summary if recording is not None else None
        self.setProperty("perfState", "ready" if summary else "empty")
        self.setProperty("perfLive", bool(recording and recording.live))
        self.style().unpolish(self)
        self.style().polish(self)
        self.setIcon(
            G.QIcon(
                _icon_with_gap(
                    perf_sparkline_pixmap(recording, dpr=self.devicePixelRatioF())
                )
            )
        )
        if summary is None:
            capture = self._capture
            if capture is None:
                capture = (
                    self.source.capture_enabled(self.game_key)
                    if self.game_key
                    else True
                )
            self._message = PERF_UNAVAILABLE if capture else PERF_CAPTURE_OFF
            description = self._message
        elif not summary.qualified:
            self._message = ""
            description = f"Game Perf Profile — {warming_text(summary.minutes)}"
        else:
            self._message = ""
            description = "Game Perf Profile — click to open the full view."
        if recording is not None and recording.live:
            description += " Recording now."
        self.setAccessibleName("Perf Profile")
        self.setAccessibleDescription(description)
        if self._peek is not None and self._peek.isVisible():
            self._fill_peek()

    def _fill_peek(self) -> None:
        self.peek.set_recording(
            self.recording, message=self._message, dpr=self.devicePixelRatioF()
        )

    def show_peek(self) -> None:
        if not self.game_key or not self.isVisible():
            return
        self._fill_peek()
        self.peek.show_at(self.mapToGlobal(self.rect().bottomRight()))

    def hide_peek(self) -> None:
        self._peek_from_keyboard = False
        if self._peek is not None:
            self._peek.hide()

    def _open(self) -> None:
        self.hide_peek()
        if self.has_recording:
            self.open_requested.emit(self.game_key)

    # ----- events -----
    def enterEvent(self, event):
        super().enterEvent(event)
        self.show_peek()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        # A hover peek closes with the pointer, even when a click focused the
        # button; one opened by keyboard focus stays until focus leaves.
        if not (self._peek_from_keyboard and self.hasFocus()):
            self.hide_peek()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        # Keyboard focus peeks; focus restored after Back does not.
        if event.reason() in _KEYBOARD_REASONS:
            self.show_peek()
            self._peek_from_keyboard = True

    def event(self, event):
        # The sparkline is drawn at the screen's pixel ratio: redraw it when
        # the window moves to a screen with another scale.
        if event.type() == C.QEvent.Type.DevicePixelRatioChange:
            self.sync(self.game_key)
        return super().event(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.hide_peek()

    def mousePressEvent(self, event):
        self.hide_peek()
        super().mousePressEvent(event)

    def hideEvent(self, event):
        self.hide_peek()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if (
            event.key() == C.Qt.Key.Key_Escape
            and self._peek is not None
            and self._peek.isVisible()
        ):
            self.hide_peek()
            event.accept()
            return
        super().keyPressEvent(event)


def entry_stylesheet() -> str:
    """The empty state uses the one disabled style; it stays focusable."""
    c = COL
    return f"""
        QPushButton#gamePerfProfileButton {{ font-weight: {WEIGHT["semibold"]}; padding: 6px 12px 6px 10px; }}
        QPushButton#gamePerfProfileButton:focus {{ border: 1px solid {c["border"]}; padding: 6px 12px 6px 10px; }}
        QPushButton#gamePerfProfileButton[perfState="empty"],
        QPushButton#gamePerfProfileButton[perfState="empty"]:hover,
        QPushButton#gamePerfProfileButton[perfState="empty"]:focus {{
            background: {c["control"]}; color: {c["quiet"]}; border: 1px solid {c["divider"]};
        }}
        QPushButton#gamePerfProfileButton[keyboardFocus="true"]:focus {{
            border: 2px solid {c["focus"]}; padding: 5px 11px 5px 9px;
        }}
    """


def peek_stylesheet() -> str:
    c, t, w = COL, TYPE, WEIGHT
    return f"""
        QFrame#perfPeek {{ background: transparent; border: none; }}
        QWidget#perfPeekBody {{ background: transparent; }}
        QLabel#perfPeekKey {{ color: {c["secondary"]}; font-size: {t["caption"]}px; background: transparent; border: none; }}
        QLabel#perfPeekValue {{ color: {c["ink"]}; font-size: {t["caption"]}px; font-weight: {w["semibold"]}; background: transparent; border: none; }}
        QLabel#perfPeekMessage {{ color: {c["secondary"]}; font-size: {t["caption"]}px; background: transparent; border: none; }}
        QLabel#perfPeekHint {{ color: {c["quiet"]}; font-size: {t["caption"]}px; background: transparent; border: none; border-top: 1px solid {c["divider"]}; padding-top: 6px; }}
    """
