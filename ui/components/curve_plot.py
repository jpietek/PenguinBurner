from __future__ import annotations

from .. import theme


class CurvePlot:
    def __init__(
        self,
        *,
        QtWidgets,
        pg,
        x_label: str,
        x_units: str = "",
        y_label: str,
        y_units: str,
        x_range: tuple[float, float] | None = None,
        y_range: tuple[float, float] | None = None,
        source_name: str = "Base",
        candidate_name: str = "Candidate",
        show_source: bool = True,
        source_color: str = theme.PLOT_SOURCE,
        source_alpha: int = 255,
        candidate_color: str = theme.PLOT_CANDIDATE,
    ):
        self.pg = pg
        self._x_label = str(x_label)
        self._x_units = str(x_units)
        self._y_label = str(y_label)
        self._y_units = str(y_units)
        self._source_color = str(source_color)
        self._source_alpha = max(0, min(255, int(source_alpha)))
        self._candidate_color = str(candidate_color)
        self._manual_selection_color = theme.PLOT_MANUAL_SELECTION
        self.source_curve = None
        self.candidate_curve = None
        self.probe_marker = None
        self.probe_vline = None
        self.probe_hline = None
        self.probe_x_axis_badge = None
        self.probe_y_axis_badge = None
        self.comparison_curves = []
        self.previous_curves = []
        self._highlight_overlay_curve = None
        self._source_points: list[tuple[float, float]] = []
        self._candidate_points: list[tuple[float, float]] = []
        self._last_candidate_points: list[tuple[float, float]] = []
        self._curve_points_by_id: dict[str, list[tuple[float, float]]] = {}
        self._last_candidate_curve_id = ""
        self._highlighted_curve_id = ""
        self._last_live_probe_values: tuple[float, float] | None = None
        self._point_selection_enabled = False
        if pg is None:
            widget = QtWidgets.QPlainTextEdit()
            widget.setReadOnly(True)
            widget.setPlainText("pyqtgraph is not installed.")
            self.widget = widget
            return

        plot = pg.PlotWidget()
        plot.setMenuEnabled(False)
        if hasattr(pg, "setConfigOptions"):
            pg.setConfigOptions(antialias=True)
        plot.setBackground(theme.WINDOW_BG)
        plot.showGrid(x=True, y=True, alpha=0.22)
        plot.setLabel("bottom", x_label, units=x_units)
        plot.setLabel("left", y_label, units=y_units)
        _disable_axis_si_prefix(plot, "bottom")
        _disable_axis_si_prefix(plot, "left")
        if x_range is not None:
            plot.setXRange(float(x_range[0]), float(x_range[1]), padding=0.0)
        if y_range is not None:
            plot.setYRange(float(y_range[0]), float(y_range[1]), padding=0.0)
        plot.addLegend(offset=(-24, -54))
        source_brush = self._curve_color(self._source_color, self._source_alpha)
        if show_source:
            self.source_curve = plot.plot(
                [],
                [],
                pen=pg.mkPen(source_brush, width=1),
                symbol="o",
                symbolBrush=source_brush,
                symbolSize=5,
                name=str(source_name),
            )
        self.candidate_curve = plot.plot(
            [],
            [],
            pen=self._candidate_curve_pen(),
            symbol="o",
            symbolBrush=self._candidate_color,
            symbolSize=6,
            name=str(candidate_name),
        )
        self.probe_marker = plot.plot(
            [],
            [],
            pen=None,
            symbol="o",
            symbolPen=pg.mkPen(pg.mkColor(255, 229, 177, 235), width=1),
            symbolBrush=pg.mkBrush(pg.mkColor(240, 184, 76, 230)),
            symbolSize=10,
        )
        probe_line_pen = pg.mkPen(pg.mkColor(240, 184, 76, 135), width=1)
        self.probe_vline = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=probe_line_pen,
        )
        self.probe_hline = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=probe_line_pen,
        )
        plot.addItem(self.probe_vline, ignoreBounds=True)
        plot.addItem(self.probe_hline, ignoreBounds=True)
        self.probe_vline.hide()
        self.probe_hline.hide()
        badge_border = pg.mkPen(pg.mkColor(240, 184, 76, 120), width=1)
        badge_fill = pg.mkColor(17, 20, 24, 175)
        self.probe_x_axis_badge = pg.TextItem(
            "",
            color=theme.PLOT_BADGE,
            anchor=(0.5, 1.0),
            border=badge_border,
            fill=badge_fill,
        )
        self.probe_y_axis_badge = pg.TextItem(
            "",
            color=theme.PLOT_BADGE,
            anchor=(0.0, 0.5),
            border=badge_border,
            fill=badge_fill,
        )
        plot.addItem(self.probe_x_axis_badge, ignoreBounds=True)
        plot.addItem(self.probe_y_axis_badge, ignoreBounds=True)
        self.probe_x_axis_badge.hide()
        self.probe_y_axis_badge.hide()
        plot.getViewBox().sigRangeChanged.connect(
            lambda *_args: self._update_axis_probe_badges()
        )
        plot.scene().sigMouseClicked.connect(self._handle_curve_click)
        self.plot = plot
        self.widget = plot

    def enable_point_selection(self, enabled: bool = True) -> None:
        self._point_selection_enabled = bool(enabled)

    def set_source_points(self, points: list[tuple[float, float]]) -> None:
        self._source_points = _normalize_points(points)
        if self.source_curve is None:
            return
        self.source_curve.setData(
            [point[0] for point in self._source_points],
            [point[1] for point in self._source_points],
        )

    def add_comparison_points(
        self,
        points: list[tuple[float, float]],
        *,
        name: str = "Comparison",
        color: str = theme.PLOT_COMPARISON,
        alpha: int = 135,
        width: int = 1,
        z_value: float = -5.0,
    ) -> None:
        if self.pg is None or not hasattr(self, "plot"):
            return
        normalized = _normalize_points(points)
        if not normalized:
            return
        comparison_color = self._curve_color(color, alpha)
        trace = self.plot.plot(
            [point[0] for point in normalized],
            [point[1] for point in normalized],
            pen=self.pg.mkPen(comparison_color, width=max(1, int(width))),
            symbol=None,
            name=str(name),
        )
        if hasattr(trace, "setZValue"):
            trace.setZValue(float(z_value))
        self.comparison_curves.append(trace)

    def clear(self) -> None:
        self._source_points = []
        self._candidate_points = []
        self._last_candidate_points = []
        self._curve_points_by_id = {}
        self._last_candidate_curve_id = ""
        self._highlighted_curve_id = ""
        if self.source_curve is not None:
            self.source_curve.setData([], [])
        if self.candidate_curve is not None:
            self.candidate_curve.setData([], [])
        if self.pg is None or not hasattr(self, "plot"):
            return
        for trace in self.comparison_curves:
            self.plot.removeItem(trace)
        self.comparison_curves = []
        for trace in self.previous_curves:
            self.plot.removeItem(_previous_trace_item(trace))
        self.previous_curves = []
        self._remove_highlight_overlay()
        self.clear_load_markers()

    def set_candidate_points(
        self,
        points: list[tuple[float, float]],
        *,
        remember_previous: bool = True,
        curve_id: str | None = None,
    ) -> None:
        if self.candidate_curve is None:
            return
        normalized = _normalize_points(points)
        normalized_curve_id = _normalize_curve_id(curve_id)
        self._candidate_points = normalized
        if normalized_curve_id:
            self._curve_points_by_id[normalized_curve_id] = list(normalized)
        if (
            remember_previous
            and self._last_candidate_points
            and (
                normalized != self._last_candidate_points
                or normalized_curve_id != self._last_candidate_curve_id
            )
        ):
            self.add_previous_points(
                self._last_candidate_points,
                curve_id=self._last_candidate_curve_id,
            )
        self._last_candidate_points = normalized
        self._last_candidate_curve_id = normalized_curve_id
        self.candidate_curve.setData(
            [point[0] for point in normalized],
            [point[1] for point in normalized],
        )
        self._apply_curve_highlights()

    def add_previous_points(
        self,
        points: list[tuple[float, float]],
        *,
        curve_id: str | None = None,
    ) -> None:
        if self.pg is None or not hasattr(self, "plot"):
            return
        if not points:
            return
        normalized_curve_id = _normalize_curve_id(curve_id)
        trace = self.plot.plot(
            [point[0] for point in points],
            [point[1] for point in points],
            pen=self._previous_curve_pen(
                highlighted=self._curve_is_highlighted(normalized_curve_id)
            ),
            symbol=None,
        )
        self.previous_curves.append(
            {
                "curve_id": normalized_curve_id,
                "item": trace,
            }
        )
        while len(self.previous_curves) > 12:
            old_trace = self.previous_curves.pop(0)
            self.plot.removeItem(_previous_trace_item(old_trace))

    def set_highlighted_curve(self, curve_id: str | None) -> None:
        self._highlighted_curve_id = _normalize_curve_id(curve_id)
        self._apply_curve_highlights()

    def _apply_curve_highlights(self) -> None:
        if self.pg is None:
            return
        highlighted_curve_is_visible = False
        if self.candidate_curve is not None:
            self.candidate_curve.setPen(self._candidate_curve_pen())
            # Keep the live candidate curve light green; draw manual selection as
            # a darker overlay instead.
        for trace in self.previous_curves:
            item = _previous_trace_item(trace)
            if item is None:
                continue
            previous_highlighted = self._curve_is_highlighted(
                _previous_trace_curve_id(trace)
            )
            item.setPen(self._previous_curve_pen(highlighted=previous_highlighted))
            highlighted_curve_is_visible = highlighted_curve_is_visible or (
                previous_highlighted
            )
        self._sync_highlight_overlay(
            highlighted_curve_is_visible=highlighted_curve_is_visible
        )

    def _curve_is_highlighted(self, curve_id: str | None) -> bool:
        normalized_curve_id = _normalize_curve_id(curve_id)
        return bool(
            self._highlighted_curve_id
            and normalized_curve_id
            and normalized_curve_id == self._highlighted_curve_id
        )

    def _candidate_curve_pen(self):
        if self.pg is None:
            return None
        return self.pg.mkPen(self._candidate_color, width=2)

    def _curve_color(self, color, alpha: int):
        value = self.pg.mkColor(color)
        value.setAlpha(max(0, min(255, int(alpha))))
        return value

    def _previous_curve_pen(self, *, highlighted: bool):
        if self.pg is None:
            return None
        if highlighted:
            color = self.pg.mkColor(self._manual_selection_color)
            color.setAlpha(235)
            return self.pg.mkPen(color, width=3)
        color = self.pg.mkColor(self._candidate_color)
        color.setAlpha(72)
        return self.pg.mkPen(color, width=1)

    def _manual_selection_curve_pen(self):
        if self.pg is None:
            return None
        color = self.pg.mkColor(self._manual_selection_color)
        color.setAlpha(235)
        return self.pg.mkPen(color, width=4)

    def _sync_highlight_overlay(self, *, highlighted_curve_is_visible: bool) -> None:
        if self.pg is None or not hasattr(self, "plot"):
            return
        self._remove_highlight_overlay()
        if not self._highlighted_curve_id or highlighted_curve_is_visible:
            return
        points = self._curve_points_by_id.get(self._highlighted_curve_id)
        if not points:
            return
        self._highlight_overlay_curve = self.plot.plot(
            [point[0] for point in points],
            [point[1] for point in points],
            pen=self._manual_selection_curve_pen(),
            symbol=None,
        )

    def _remove_highlight_overlay(self) -> None:
        if self._highlight_overlay_curve is None:
            return
        if hasattr(self, "plot"):
            self.plot.removeItem(self._highlight_overlay_curve)
        self._highlight_overlay_curve = None

    def clear_load_markers(self) -> None:
        if self.probe_marker is not None:
            self.probe_marker.setData([], [])
        if self.probe_vline is not None:
            self.probe_vline.hide()
        if self.probe_hline is not None:
            self.probe_hline.hide()
        self._last_live_probe_values = None
        self._hide_axis_probe_badges()
        self._restore_axis_labels()

    def set_target_point(self, voltage, clock) -> None:
        _set_probe_marker(self.probe_marker, voltage, clock)

    def set_crosshair_point(self, voltage, clock) -> None:
        if _set_probe_lines(self.probe_vline, self.probe_hline, voltage, clock):
            self._set_axis_probe_badges(voltage, clock)
        else:
            self._last_live_probe_values = None
            self._hide_axis_probe_badges()

    def set_selected_point(self, voltage, clock) -> None:
        self.set_target_point(voltage, clock)
        self.set_crosshair_point(voltage, clock)

    def set_probe_marker(self, payload: dict) -> None:
        voltage, clock = _probe_marker_values(
            payload,
            prefer_measured=False,
        )
        self.set_target_point(voltage, clock)

    def set_live_load_marker(self, payload: dict) -> None:
        voltage, clock = _probe_marker_values(payload, prefer_measured=True)
        self.set_crosshair_point(voltage, clock)

    def set_load_markers(self, payload: dict) -> None:
        self.set_live_load_marker(payload)

    def _handle_curve_click(self, event) -> None:
        if not self._point_selection_enabled or not hasattr(self, "plot"):
            return
        scene_pos = event.scenePos()
        if not self.plot.sceneBoundingRect().contains(scene_pos):
            return
        view_pos = self.plot.plotItem.vb.mapSceneToView(scene_pos)
        point = _nearest_curve_point(
            float(view_pos.x()),
            float(view_pos.y()),
            [*self._candidate_points, *self._source_points],
            self.plot.viewRange(),
        )
        if point is None:
            return
        self.set_selected_point(point[0], point[1])
        if hasattr(event, "accept"):
            event.accept()

    def _restore_axis_labels(self) -> None:
        if not hasattr(self, "plot"):
            return
        self.plot.setLabel("bottom", self._x_label, units=self._x_units)
        self.plot.setLabel("left", self._y_label, units=self._y_units)

    def _set_axis_probe_badges(self, voltage, clock) -> None:
        if not hasattr(self, "plot"):
            return
        try:
            voltage_value = float(voltage)
            clock_value = float(clock)
        except (TypeError, ValueError):
            self._last_live_probe_values = None
            self._hide_axis_probe_badges()
            return
        self._last_live_probe_values = (voltage_value, clock_value)
        self._update_axis_probe_badges()

    def _update_axis_probe_badges(self) -> None:
        if not hasattr(self, "plot") or self._last_live_probe_values is None:
            return
        if self.probe_x_axis_badge is None or self.probe_y_axis_badge is None:
            return
        voltage_value, clock_value = self._last_live_probe_values
        (x_min, x_max), (y_min, y_max) = self.plot.viewRange()
        x_span = max(1.0, float(x_max) - float(x_min))
        y_span = max(1.0, float(y_max) - float(y_min))
        x_badge_y = float(y_min) + y_span * 0.035
        y_badge_x = float(x_min) + x_span * 0.015
        self.probe_x_axis_badge.setText(
            _axis_value_badge_text(voltage_value, self._x_units)
        )
        self.probe_x_axis_badge.setPos(float(voltage_value), x_badge_y)
        self.probe_x_axis_badge.show()
        self.probe_y_axis_badge.setText(
            _axis_value_badge_text(clock_value, self._y_units)
        )
        self.probe_y_axis_badge.setPos(y_badge_x, float(clock_value))
        self.probe_y_axis_badge.show()

    def _hide_axis_probe_badges(self) -> None:
        if self.probe_x_axis_badge is not None:
            self.probe_x_axis_badge.hide()
        if self.probe_y_axis_badge is not None:
            self.probe_y_axis_badge.hide()


def _disable_axis_si_prefix(plot, axis_name: str) -> None:
    axis = plot.getAxis(axis_name)
    if hasattr(axis, "enableAutoSIPrefix"):
        axis.enableAutoSIPrefix(False)


def _set_probe_marker(marker, voltage, clock) -> None:
    if marker is None:
        return
    try:
        voltage_value = float(voltage)
        clock_value = float(clock)
    except (TypeError, ValueError):
        marker.setData([], [])
        return
    marker.setData([voltage_value], [clock_value])


def _set_probe_lines(vline, hline, voltage, clock) -> bool:
    try:
        voltage_value = float(voltage)
        clock_value = float(clock)
    except (TypeError, ValueError):
        if vline is not None:
            vline.hide()
        if hline is not None:
            hline.hide()
        return False
    if vline is not None:
        vline.setPos(voltage_value)
        vline.show()
    if hline is not None:
        hline.setPos(clock_value)
        hline.show()
    return True


def _normalize_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    normalized = []
    for point in points:
        try:
            normalized.append((float(point[0]), float(point[1])))
        except (IndexError, TypeError, ValueError):
            continue
    return normalized


def _normalize_curve_id(curve_id: str | None) -> str:
    return str(curve_id or "").strip()


def _previous_trace_item(trace):
    if isinstance(trace, dict):
        return trace.get("item")
    return trace


def _previous_trace_curve_id(trace) -> str:
    if isinstance(trace, dict):
        return _normalize_curve_id(trace.get("curve_id"))
    return ""


def _nearest_curve_point(
    x_value: float,
    y_value: float,
    points: list[tuple[float, float]],
    view_range,
    *,
    max_normalized_distance: float = 0.04,
) -> tuple[float, float] | None:
    normalized = _normalize_points(points)
    if not normalized:
        return None
    try:
        (x_min, x_max), (y_min, y_max) = view_range
        x_span = max(1.0, float(x_max) - float(x_min))
        y_span = max(1.0, float(y_max) - float(y_min))
    except (TypeError, ValueError):
        x_span = 1.0
        y_span = 1.0
    best_point = None
    best_distance = None
    for point in normalized:
        distance = (
            ((float(point[0]) - float(x_value)) / x_span) ** 2
            + ((float(point[1]) - float(y_value)) / y_span) ** 2
        ) ** 0.5
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_point = point
    if best_distance is None or best_distance > float(max_normalized_distance):
        return None
    return best_point


def _axis_value_badge_text(value: float, units: str) -> str:
    unit_text = f" {units}" if str(units).strip() else ""
    return f"{value:.0f}{unit_text}"


def _probe_marker_values(payload: dict, *, prefer_measured: bool) -> tuple[object, object]:
    if prefer_measured:
        voltage = _first_present(
            payload,
            "measured_voltage_mv",
            "avg_voltage_mv",
        )
        clock = _first_present(
            payload,
            "measured_clock_mhz",
            "avg_core_clock_mhz",
        )
    else:
        voltage = _first_present(
            payload,
            "voltage_mv",
            "measured_voltage_mv",
            "avg_voltage_mv",
        )
        clock = _first_present(
            payload,
            "clock_mhz",
            "measured_clock_mhz",
            "avg_core_clock_mhz",
        )
    return voltage, clock


def _first_present(payload: dict, *keys: str):
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None
