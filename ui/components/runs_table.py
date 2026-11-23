from __future__ import annotations

from collections.abc import Callable

from auto_uv.session.scan_events import probe_decision_label, probe_reason_tooltip
from auto_uv.session.scan_runs import (
    format_duration_compact,
    format_float,
    format_int,
    format_oc_progress,
    format_oc_progress_for_payload,
    format_signed_mhz,
    is_active_decision,
    is_base_baseline,
    measured_clock_value,
    oc_from_measured_baseline,
    payload_oc_progress,
    payload_progress,
    perf_cap_reason_text,
    perf_cap_reason_tooltip,
    probe_key,
    progress_label,
    progress_time_text,
    to_float,
)
from auto_uv.session.scan_runs import (
    row_state as scan_row_state,
)

from .. import theme
from .table_sizing import set_header_fit_column_widths


class RunsTable:
    COLUMNS = (
        "Run",
        "mV",
        "Tgt MHz",
        "OC MHz",
        "Meas MHz",
        "FPS",
        "Power W",
        "Temp C",
        "Fan %",
        "Cap",
        "FPS/W",
        "Result",
        "Status",
    )
    TARGET_MHZ_COLUMN = 2
    OC_MHZ_COLUMN = 3
    MEASURED_MHZ_COLUMN = 4
    FPS_COLUMN = 5
    POWER_COLUMN = 6
    TEMP_COLUMN = 7
    FAN_COLUMN = 8
    PERF_CAP_COLUMN = 9
    FPSW_COLUMN = 10
    DECISION_COLUMN = 11
    STATUS_COLUMN = 12
    # Keep two more rows than the old five-row panel while giving the plot a
    # little more default height. The table still follows the newest run.
    MIN_VISIBLE_ROWS = 7

    def __init__(self, *, QtCore, QtGui, QtWidgets):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.base_baseline: dict | None = None
        self._progress_by_probe: dict[tuple[str, str], dict] = {}
        self._oc_progress_by_probe: dict[tuple[str, str], tuple[int, int]] = {}
        self._progress_bar_class = _clickable_progress_bar_class(QtWidgets)
        self._selected_candidate_id = ""
        self.on_candidate_selection_changed: Callable[[str | None], None] | None = None
        self.widget = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.widget.setHorizontalHeaderLabels(self.COLUMNS)
        self.widget.horizontalHeader().setStretchLastSection(True)
        self.widget.verticalHeader().setVisible(False)
        self.widget.setAlternatingRowColors(False)
        self.widget.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.widget.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.widget.setSortingEnabled(False)
        self.widget.cellClicked.connect(self._handle_cell_clicked)
        # Content-derived height floor: the table always shows at least
        # MIN_VISIBLE_ROWS runs. Everything scales with the style's real row
        # and header metrics, so the floor stays correct across DPI/themes.
        header_height = self.widget.horizontalHeader().sizeHint().height()
        row_height = max(1, self.widget.verticalHeader().defaultSectionSize())
        frame_height = 2 * self.widget.frameWidth()
        self.widget.setMinimumHeight(
            header_height + frame_height + row_height * self.MIN_VISIBLE_ROWS
        )
        # Floors are deliberately small: real column widths come from the wider
        # of each header label and its widest expected value (samples below), so
        # every column fits its contents instead of reserving fixed dead space.
        set_header_fit_column_widths(
            self.widget,
            {
                0: 34,
                1: 44,
                self.TARGET_MHZ_COLUMN: 52,
                self.OC_MHZ_COLUMN: 52,
                self.MEASURED_MHZ_COLUMN: 56,
                self.FPS_COLUMN: 56,
                self.POWER_COLUMN: 64,
                self.TEMP_COLUMN: 52,
                self.FAN_COLUMN: 48,
                self.PERF_CAP_COLUMN: 60,
                self.FPSW_COLUMN: 64,
                self.DECISION_COLUMN: 64,
                self.STATUS_COLUMN: 150,
            },
            samples={
                self.TARGET_MHZ_COLUMN: "3180",
                self.OC_MHZ_COLUMN: "-389",
                self.MEASURED_MHZ_COLUMN: "2760.24",
                self.FPS_COLUMN: "120.00 (-12.34%)",
                self.POWER_COLUMN: "390.00 (-29.66%)",
                self.TEMP_COLUMN: "100.00",
                self.FAN_COLUMN: "100.00",
                self.PERF_CAP_COLUMN: "sw-power",
                self.FPSW_COLUMN: "0.2500 (+28.34%)",
                self.DECISION_COLUMN: "discarded",
            },
            QtCore=QtCore,
            padding=18,
        )

    def clear(self) -> None:
        self.base_baseline = None
        self._progress_by_probe.clear()
        self._oc_progress_by_probe.clear()
        self._selected_candidate_id = ""
        self.widget.clearSelection()
        self.widget.setRowCount(0)

    def add_probe_start(self, payload: dict) -> None:
        row = self.widget.rowCount()
        self.widget.insertRow(row)
        self._write_row(row, payload, running=True)

    def _scrolled_to_tail(self) -> bool:
        """Whether the view is pinned to the newest row.

        The one-row tolerance also recovers from scrollToItem's off-by-one:
        under per-item scrolling with a fractional row visible it could land
        one row short, leaving the newest pending row just below the fold.
        """
        bar = self.widget.verticalScrollBar()
        return int(bar.value()) >= int(bar.maximum()) - 1

    def record_candidate_curve(self, payload: dict) -> None:
        if is_base_baseline(payload):
            row = self._find_row_by_probe_key(probe_key(payload))
            if row is not None:
                row_state = (
                    "running"
                    if is_active_decision(self._cell_text(row, self.DECISION_COLUMN))
                    else scan_row_state(payload, running=False)
                )
                self._set_base_baseline_oc_cell(row, row_state=row_state)
            return
        oc_progress = oc_from_measured_baseline(
            payload,
            self.base_baseline,
        )
        if oc_progress is None:
            oc_progress = payload_oc_progress(payload)
        if oc_progress is None:
            return
        key = probe_key(payload)
        self._oc_progress_by_probe[key] = oc_progress
        row = self._find_row_by_probe_key(key)
        if row is not None:
            row_state = (
                "running"
                if is_active_decision(self._cell_text(row, self.DECISION_COLUMN))
                else scan_row_state(payload, running=False)
            )
            self._set_oc_cell(
                row,
                oc_progress,
                row_state=row_state,
            )

    def update_probe_progress(self, payload: dict) -> None:
        row = self._find_running_row(payload)
        if row is None:
            return
        progress = self._merged_progress(probe_key(payload), payload_progress(payload))
        self._progress_by_probe[probe_key(payload)] = progress
        self._set_perf_cap_cell(row, payload, row_state="running")
        self._set_status_progress_cell(
            row,
            progress=progress,
            running=True,
            row_state="running",
        )

    def mark_running_rows_stopping(self) -> None:
        for row in self._active_rows():
            self._set_decision_cell(row, "Stopping", row_state="warning")
            self._apply_row_state(row, "warning")
            self._set_status_progress_cell(
                row,
                progress=self._row_progress(row, label="Stopping"),
                running=True,
                row_state="warning",
            )

    def mark_running_rows_stopped(
        self,
        *,
        label: str = "Stopped",
        decision: str | None = None,
        row_state: str = "error",
    ) -> None:
        # The Decision cell follows ``decision`` (default: ``label``) so an
        # aborted/stopped run is not mislabelled "Failed".
        decision_text = decision if decision is not None else label
        for row in self._active_rows():
            self._set_decision_cell(row, decision_text, row_state=row_state)
            self._apply_row_state(row, row_state)
            self._set_status_progress_cell(
                row,
                progress=self._row_progress(row, label=label),
                running=False,
                row_state=row_state,
            )

    def add_probe_result(self, payload: dict) -> None:
        if is_base_baseline(payload):
            self.base_baseline = dict(payload)
        row = self._find_running_row(payload)
        if row is None:
            row = self.widget.rowCount()
            self.widget.insertRow(row)
        self._write_row(row, payload, running=False)

    def _write_row(self, row: int, payload: dict, *, running: bool) -> None:
        row_state = scan_row_state(payload, running=running)
        values = self._row_values(row, payload, running=running)
        if len(values) != len(self.COLUMNS):
            raise RuntimeError(
                f"runs table row has {len(values)} values for {len(self.COLUMNS)} columns"
            )
        for column, value in enumerate(values):
            item = self.widget_item(str(value), row_state=row_state, column=column)
            self.widget.setItem(row, column, item)
        self._paint_delta_cell(
            row,
            self.FPS_COLUMN,
            to_float(payload.get("fps")),
            higher_is_better=True,
            bold=False,
            label="FPS",
        )
        self._paint_delta_cell(
            row,
            self.POWER_COLUMN,
            to_float(payload.get("power_w")),
            higher_is_better=False,
            bold=True,
            label="Power W",
        )
        self._paint_fpsw_cell(row, payload)
        self._set_perf_cap_cell(row, payload, row_state=row_state)
        key = probe_key(payload)
        progress = self._merged_progress(key, payload_progress(payload))
        if not running:
            label = progress_label(payload)
            if label:
                progress = dict(progress)
                progress["label"] = label
        else:
            self._progress_by_probe[key] = dict(progress)
        self._set_status_progress_cell(
            row,
            progress=progress,
            running=running,
            row_state=row_state,
        )
        self._set_probe_reason_tooltips(row, payload)
        self._sync_selected_row(row)
        self._scroll_to_latest(row)

    def _row_values(self, row: int, payload: dict, *, running: bool) -> list[str]:
        decision = "Running" if running else probe_decision_label(payload)
        return [
            f"{int(row) + 1}.",
            format_int(payload.get("voltage_mv")),
            format_int(payload.get("clock_mhz")),
            format_oc_progress_for_payload(
                payload,
                self._oc_progress_for_payload(payload),
            ),
            format_float(measured_clock_value(payload)),
            self._metric_text_with_delta(payload.get("fps"), "fps"),
            self._metric_text_with_delta(payload.get("power_w"), "power_w"),
            format_float(payload.get("temp_c")),
            format_float(payload.get("fan_pct")),
            perf_cap_reason_text(
                payload.get("perf_cap_reason"),
                payload.get("hw_power_brake_samples"),
            ),
            self._metric_text_with_delta(
                payload.get("efficiency_fps_per_w"),
                "efficiency_fps_per_w",
            ),
            str(decision),
            "",
        ]

    def _merged_progress(self, key: tuple[str, str], incoming: dict) -> dict:
        progress = dict(self._progress_by_probe.get(key, {}))
        for field in ("elapsed_s", "target_duration_s"):
            value = incoming.get(field)
            if value is not None:
                progress[field] = value
        if incoming.get("label"):
            progress["label"] = incoming["label"]
        else:
            progress.setdefault("label", "")
        progress.setdefault("elapsed_s", None)
        progress.setdefault("target_duration_s", None)
        return progress

    def _oc_progress_for_payload(self, payload: dict) -> tuple[int, int] | None:
        measured_oc = oc_from_measured_baseline(payload, self.base_baseline)
        if measured_oc is not None:
            return measured_oc
        cached_oc = self._oc_progress_by_probe.get(probe_key(payload))
        if cached_oc is not None:
            return cached_oc
        payload_oc = payload_oc_progress(payload)
        if payload_oc is not None:
            return payload_oc
        return oc_from_measured_baseline(payload, self.base_baseline)

    def _metric_text_with_delta(self, value, baseline_key: str) -> str:
        value_text = format_float(value, precision=4 if baseline_key == "efficiency_fps_per_w" else 2)
        if not value_text:
            return ""
        delta_text = self._delta_text(value, baseline_key)
        if not delta_text:
            return value_text
        return f"{value_text} ({delta_text})"

    def _delta_text(
        self,
        value,
        baseline_key: str,
    ) -> str:
        if self.base_baseline is None:
            return ""
        if is_base_baseline(self.base_baseline):
            baseline = to_float(self.base_baseline.get(baseline_key))
        else:
            baseline = None
        current = to_float(value)
        if current is None or baseline is None or baseline == 0.0:
            return ""
        if current == baseline:
            return "ref"
        raw_delta_pct = ((current - baseline) / baseline) * 100.0
        return f"{raw_delta_pct:+.2f}%"

    def _paint_delta_cell(
        self,
        row: int,
        column: int,
        value: float | None,
        *,
        higher_is_better: bool,
        bold: bool,
        label: str,
    ) -> None:
        item = self.widget.item(row, column)
        if item is None or self.base_baseline is None or value is None:
            return
        baseline_key = "fps" if higher_is_better else "power_w"
        baseline = to_float(self.base_baseline.get(baseline_key))
        if baseline is None or baseline == 0.0:
            return
        delta_pct = ((float(value) - baseline) / baseline) * 100.0
        if abs(delta_pct) < 0.05:
            return

        if higher_is_better:
            if delta_pct > 0.05:
                color = theme.GOOD
            elif delta_pct >= -1.5:
                color = theme.WARNING
            else:
                color = theme.ERROR
        elif delta_pct <= -3.0:
            color = theme.GOOD
        elif delta_pct <= 2.0:
            color = theme.WARNING
        else:
            color = theme.ERROR
        item.setForeground(self.QtGui.QColor(color))
        if bold:
            item.setFont(_bold_font(item))
        base_text = format_float(baseline)
        if base_text:
            item.setToolTip(f"{label} {delta_pct:+.2f}% vs base {base_text}")

    def _paint_fpsw_cell(self, row: int, payload: dict) -> None:
        item = self.widget.item(row, self.FPSW_COLUMN)
        value = to_float(payload.get("efficiency_fps_per_w"))
        if item is None or value is None or self.base_baseline is None:
            return
        baseline = to_float(self.base_baseline.get("efficiency_fps_per_w"))
        if baseline is None or baseline == 0.0:
            return
        if is_base_baseline(payload):
            item.setToolTip("Base FPS/W reference")
            return
        delta_pct = ((float(value) - baseline) / baseline) * 100.0
        if delta_pct > 0.5:
            color = theme.GOOD
        elif delta_pct >= -0.5:
            color = theme.WARNING
        else:
            color = theme.ERROR
        item.setForeground(self.QtGui.QColor(color))
        item.setFont(_bold_font(item))
        item.setToolTip(f"FPS/W {delta_pct:+.2f}% vs base")

    def _set_perf_cap_cell(self, row: int, payload: dict, *, row_state: str) -> None:
        reason_text = perf_cap_reason_text(
            payload.get("perf_cap_reason"),
            payload.get("hw_power_brake_samples"),
        )
        item = self.widget.item(row, self.PERF_CAP_COLUMN)
        if item is None:
            item = self.widget_item(
                reason_text,
                row_state=row_state,
                column=self.PERF_CAP_COLUMN,
            )
            self.widget.setItem(row, self.PERF_CAP_COLUMN, item)
        elif reason_text:
            item.setText(reason_text)
        tooltip = perf_cap_reason_tooltip(
            payload.get("perf_cap_reason"),
            payload.get("hw_power_brake_samples"),
        )
        if tooltip:
            item.setToolTip(tooltip)

    def _set_status_progress_cell(
        self,
        row: int,
        *,
        progress: dict | None,
        running: bool,
        row_state: str,
    ) -> None:
        elapsed = to_float((progress or {}).get("elapsed_s"))
        target = to_float((progress or {}).get("target_duration_s"))
        label = str((progress or {}).get("label") or "").strip()
        bar = self._make_progress_bar(row, self.STATUS_COLUMN)
        bar.setTextVisible(True)
        bar.setFixedHeight(18)

        fill = _row_colors(row_state)["accent"]
        if running and row_state == "running":
            fill = theme.RUNNING
        ratio = None
        if target is not None and float(target) > 0.0:
            shown_elapsed = min(max(0.0, float(elapsed or 0.0)), float(target))
            ratio = min(1.0, shown_elapsed / float(target))
            if not running and row_state in {"good", "baseline", "neutral"}:
                ratio = 1.0
                shown_elapsed = float(target)
            bar.setRange(0, 1000)
            bar.setValue(round(ratio * 1000.0))
            time_text = progress_time_text(shown_elapsed, target)
            bar.setFormat(f"{label} {time_text}" if label else time_text)
            bar.setToolTip(
                f"Stability progress: {format_duration_compact(shown_elapsed)} / "
                f"{format_duration_compact(target)}"
            )
        elif running:
            self.widget.removeCellWidget(row, self.STATUS_COLUMN)
            item = self.widget_item(
                label or "Running",
                row_state=row_state,
                column=self.STATUS_COLUMN,
            )
            item.setToolTip("Waiting for stability progress timing")
            self.widget.setItem(row, self.STATUS_COLUMN, item)
            return
        else:
            bar.setRange(0, 1000)
            ratio = 1.0
            bar.setValue(1000)
            bar.setFormat(f"{label} 100%" if label else "100%")
            bar.setToolTip("Stability probe finished")

        bar.setStyleSheet(
            _progress_bar_stylesheet(fill, text_color=_progress_text_color(fill, ratio))
        )
        self.widget.setCellWidget(row, self.STATUS_COLUMN, bar)

    def _find_running_row(self, payload: dict) -> int | None:
        key = probe_key(payload)
        for row in range(self.widget.rowCount() - 1, -1, -1):
            if not is_active_decision(self._cell_text(row, self.DECISION_COLUMN)):
                continue
            if self._row_probe_key(row) == key:
                return row
        return None

    def _find_row_by_probe_key(self, key: tuple[str, str]) -> int | None:
        for row in range(self.widget.rowCount() - 1, -1, -1):
            if self._row_probe_key(row) == key:
                return row
        return None

    def _row_probe_key(self, row: int) -> tuple[str, str]:
        return (self._cell_text(row, 1), self._cell_text(row, self.TARGET_MHZ_COLUMN))

    def _cell_text(self, row: int, column: int) -> str:
        item = self.widget.item(row, column)
        return "" if item is None else item.text()

    def _active_rows(self) -> list[int]:
        return [
            row
            for row in range(self.widget.rowCount())
            if is_active_decision(self._cell_text(row, self.DECISION_COLUMN))
        ]

    def _row_progress(self, row: int, *, label: str) -> dict:
        item = self.widget.item(row, 0)
        probe_key = None
        if item is not None:
            voltage = self._cell_text(row, 1)
            clock = self._cell_text(row, self.TARGET_MHZ_COLUMN)
            probe_key = (voltage, clock)
        progress = (
            dict(self._progress_by_probe.get(probe_key, {}))
            if probe_key is not None
            else {}
        )
        progress["label"] = str(label)
        return progress

    def _set_decision_cell(self, row: int, text: str, *, row_state: str) -> None:
        item = self.widget_item(
            str(text),
            row_state=row_state,
            column=self.DECISION_COLUMN,
        )
        self.widget.setItem(row, self.DECISION_COLUMN, item)

    def _set_probe_reason_tooltips(self, row: int, payload: dict) -> None:
        tooltip = probe_reason_tooltip(payload)
        if not tooltip:
            return
        decision_item = self.widget.item(row, self.DECISION_COLUMN)
        if decision_item is not None:
            decision_item.setToolTip(tooltip)
        status_widget = self.widget.cellWidget(row, self.STATUS_COLUMN)
        if status_widget is not None:
            status_widget.setToolTip(tooltip)
            return
        status_item = self.widget.item(row, self.STATUS_COLUMN)
        if status_item is not None:
            status_item.setToolTip(tooltip)

    def _apply_row_state(self, row: int, row_state: str) -> None:
        colors = _row_colors(row_state)
        for column in range(self.widget.columnCount()):
            item = self.widget.item(row, column)
            if item is None:
                continue
            if column == self.DECISION_COLUMN:
                item.setBackground(self.QtGui.QColor(theme.SURFACE_BG))
                item.setForeground(self.QtGui.QColor(theme.TEXT))
                continue
            item.setBackground(self.QtGui.QColor(colors["background"]))
            item.setForeground(self.QtGui.QColor(colors["foreground"]))

    def selected_candidate_id(self) -> str | None:
        if self._selected_candidate_id:
            return self._selected_candidate_id
        selected = self.widget.selectionModel().selectedRows()
        if not selected:
            return None
        row = int(selected[-1].row())
        return self._row_candidate_id(row)

    def _handle_cell_clicked(self, row: int, _column: int) -> None:
        candidate_id = self._row_candidate_id(row)
        if not candidate_id:
            return
        if candidate_id == self._selected_candidate_id:
            self._selected_candidate_id = ""
            self.widget.clearSelection()
        else:
            self._selected_candidate_id = candidate_id
            self.widget.selectRow(row)
        self._emit_candidate_selection_changed()

    def _emit_candidate_selection_changed(self) -> None:
        callback = self.on_candidate_selection_changed
        if callback is not None:
            callback(self._selected_candidate_id or None)

    def _sync_selected_row(self, row: int) -> None:
        if not self._selected_candidate_id:
            return
        if self._row_candidate_id(row) == self._selected_candidate_id:
            self.widget.selectRow(row)

    def _make_progress_bar(self, row: int, column: int):
        return self._progress_bar_class(
            on_row_click=self._row_click_callback(row, column)
        )

    def _row_click_callback(self, row: int, column: int):
        return lambda: self._handle_cell_clicked(row, column)

    def _row_candidate_id(self, row: int) -> str | None:
        voltage = self._cell_text(row, 1).strip()
        clock = self._cell_text(row, self.TARGET_MHZ_COLUMN).strip()
        if not voltage or not clock:
            return None
        return f"{voltage}mv-{clock}mhz"

    def _scroll_to_latest(self, row: int) -> None:
        # Follow only the newest (tail) row, and only while the user hasn't
        # scrolled up to inspect earlier runs. scrollToBottom is exact under
        # per-item scrolling, where scrollToItem(PositionAtBottom) can land
        # one row short and hide the pending row below the fold.
        if row != self.widget.rowCount() - 1:
            return
        if not self._scrolled_to_tail():
            return
        self.widget.scrollToBottom()

    def _set_oc_cell(
        self,
        row: int,
        oc_progress: tuple[int, int] | None,
        *,
        row_state: str,
    ) -> None:
        item = self.widget_item(
            format_oc_progress(oc_progress),
            row_state=row_state,
            column=self.OC_MHZ_COLUMN,
        )
        if oc_progress is not None:
            oc_mhz, _limit_mhz = oc_progress
            item.setToolTip(
                "Target clock delta from measured baseline: "
                f"{format_signed_mhz(int(oc_mhz))}"
            )
        self.widget.setItem(row, self.OC_MHZ_COLUMN, item)

    def _set_base_baseline_oc_cell(self, row: int, *, row_state: str) -> None:
        item = self.widget_item(
            "-",
            row_state=row_state,
            column=self.OC_MHZ_COLUMN,
        )
        item.setToolTip("Baseline reference run; OC offset starts after this row.")
        self.widget.setItem(row, self.OC_MHZ_COLUMN, item)

    def widget_item(self, text: str, *, row_state: str, column: int):
        item = self.QtWidgets.QTableWidgetItem(text)
        if column in {
            1,
            self.TARGET_MHZ_COLUMN,
            self.OC_MHZ_COLUMN,
            self.MEASURED_MHZ_COLUMN,
            self.FPS_COLUMN,
            self.POWER_COLUMN,
            self.TEMP_COLUMN,
            self.FAN_COLUMN,
            self.FPSW_COLUMN,
        }:
            item.setTextAlignment(
                self.QtCore.Qt.AlignRight | self.QtCore.Qt.AlignVCenter
            )
        colors = _row_colors(row_state)
        item.setBackground(self.QtGui.QColor(colors["background"]))
        item.setForeground(self.QtGui.QColor(colors["foreground"]))
        if column == self.DECISION_COLUMN:
            item.setBackground(self.QtGui.QColor(theme.SURFACE_BG))
            item.setForeground(self.QtGui.QColor(theme.TEXT))
        return item


def _row_colors(row_state: str) -> dict[str, str]:
    if row_state == "good":
        return {
            "background": theme.ROW_GOOD_BG,
            "foreground": theme.GOOD_TEXT,
            "accent": theme.GOOD_BRIGHT,
        }
    if row_state == "warning":
        return {
            "background": theme.ROW_WARNING_BG,
            "foreground": theme.WARNING_TEXT,
            "accent": theme.WARNING,
        }
    if row_state == "error":
        return {
            "background": theme.ROW_ERROR_BG,
            "foreground": theme.ERROR_TEXT,
            "accent": theme.ERROR,
        }
    if row_state == "running":
        return {
            "background": theme.ROW_RUNNING_BG,
            "foreground": theme.RUNNING_TEXT,
            "accent": theme.RUNNING,
        }
    if row_state == "baseline":
        return {
            "background": theme.ROW_BASELINE_BG,
            "foreground": theme.BASELINE_TEXT,
            "accent": theme.BASELINE,
        }
    return {
        "background": theme.SURFACE_BG,
        "foreground": theme.TEXT,
        "accent": theme.TEXT,
    }


def _bold_font(item):
    font = item.font()
    font.setBold(True)
    return font


def _progress_text_color(fill: str, ratio: float | None) -> str:
    if ratio is None or float(ratio) < 0.995:
        return theme.TEXT_PROGRESS
    return theme.TEXT_ON_ERROR if fill.lower() == theme.ERROR else theme.TEXT_ON_LIGHT


def _progress_bar_stylesheet(fill: str, *, text_color: str = theme.TEXT_PROGRESS) -> str:
    return (
        "QProgressBar {"
        f"background: {theme.WINDOW_BG};"
        f"border: 1px solid {theme.BORDER};"
        "border-radius: 3px;"
        f"color: {text_color};"
        "font-size: 11px;"
        "font-weight: 800;"
        "text-align: center;"
        "}"
        f"QProgressBar::chunk {{ background: {fill}; border-radius: 2px; }}"
    )


def _clickable_progress_bar_class(QtWidgets):
    class RowClickableProgressBar(QtWidgets.QProgressBar):
        def __init__(self, *, on_row_click=None):
            super().__init__()
            self._on_row_click = on_row_click

        def mousePressEvent(self, event) -> None:
            if self._on_row_click is not None:
                self._on_row_click()
            super().mousePressEvent(event)

    return RowClickableProgressBar
