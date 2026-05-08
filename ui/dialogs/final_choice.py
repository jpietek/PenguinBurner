from __future__ import annotations

import math

from auto_uv3.scan_mode import AUTO_UV_MODE_PERFORMANCE

from ..constants import DEFAULT_FINAL_VERIFICATION_DURATION_S
from ..constants import MAX_FINAL_VERIFICATION_DURATION_S
from ..components.profile_list import _format_profile_metric_with_delta
from ..components.profile_list import _paint_profile_delta_item
from ..components.profile_list import _profile_base_metric
from ..components.table_sizing import set_header_fit_column_widths


FINAL_CHOICE_SORT_ROLE = 261
FINAL_CHOICE_FPSW_SORT_COLUMN = 3
FINAL_CHOICE_FPS_SORT_COLUMN = 4
FINAL_CHOICE_DEFAULT_SORT_COLUMN = FINAL_CHOICE_FPSW_SORT_COLUMN
FINAL_CHOICE_SORTABLE_COLUMNS = frozenset({2, 3, 4, 5})
FINAL_CHOICE_HIGHER_FIRST_COLUMNS = frozenset({2, 3, 4})
FINAL_CHOICE_COLUMNS = [
    "mV",
    "Target MHz",
    "Effective MHz",
    "FPS/W",
    "FPS",
    "Power W",
    "Short Check",
    "Status",
]


def candidate_status_text(
    candidate: dict,
    is_default: bool,
    *,
    auto_uv_mode: object = "",
) -> str:
    parts = []
    if is_default:
        parts.append(
            "Best FPS"
            if final_choice_is_performance_mode(auto_uv_mode)
            else "Best FPS/W"
        )
    parts.append(
        "Final stability verified"
        if bool(candidate.get("final_verified"))
        else "Passed short probe"
    )
    return " | ".join(parts)


def candidate_number(value, *, precision: int) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    precision = max(0, min(int(precision), 2))
    return str(int(round(number))) if precision <= 0 else f"{number:.{precision}f}"


def final_choice_sort_values(candidate: dict) -> list[float | str]:
    return [
        numeric_sort_value(candidate.get("candidate_voltage_mv")),
        numeric_sort_value(candidate.get("lock_clock_mhz")),
        numeric_sort_value(candidate.get("avg_core_clock_mhz")),
        numeric_sort_value(candidate.get("efficiency_fps_per_w")),
        numeric_sort_value(candidate.get("avg_fps")),
        numeric_sort_value(candidate.get("avg_power_w")),
        float(candidate_short_duration_s(candidate)),
        str(candidate_status_text(candidate, False)).casefold(),
    ]


def final_choice_sort_column_for_mode(auto_uv_mode: object) -> int:
    return (
        FINAL_CHOICE_FPS_SORT_COLUMN
        if final_choice_is_performance_mode(auto_uv_mode)
        else FINAL_CHOICE_FPSW_SORT_COLUMN
    )


def sort_candidates_for_final_choice(
    candidates: list[dict],
    auto_uv_mode: object,
) -> list[dict]:
    if final_choice_is_performance_mode(auto_uv_mode):
        return sorted(
            candidates,
            key=lambda candidate: (
                candidate_fps(candidate) is None,
                -float(candidate_fps(candidate) or 0.0),
                int(candidate.get("candidate_voltage_mv") or 99999),
                -int(candidate.get("lock_clock_mhz") or 0),
            ),
        )
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate_fpsw(candidate) is None,
            -float(candidate_fpsw(candidate) or 0.0),
            int(candidate.get("candidate_voltage_mv") or 99999),
            -int(candidate.get("lock_clock_mhz") or 0),
        ),
    )


def best_final_choice_candidate_id(candidates: list[dict], auto_uv_mode: object) -> str:
    metric = (
        candidate_fps
        if final_choice_is_performance_mode(auto_uv_mode)
        else candidate_fpsw
    )
    for candidate in candidates:
        if metric(candidate) is not None:
            return str(candidate.get("candidate_id", ""))
    return str(candidates[0].get("candidate_id", "")) if candidates else ""


def duration_minutes_for_control(seconds) -> int:
    try:
        duration_s = max(1, int(round(float(seconds))))
    except (TypeError, ValueError):
        duration_s = DEFAULT_FINAL_VERIFICATION_DURATION_S
    return max(1, min(MAX_FINAL_VERIFICATION_DURATION_S // 60, int(math.ceil(duration_s / 60.0))))


def candidate_short_duration_s(candidate: dict) -> int:
    try:
        duration_s = int(round(float(candidate.get("short_verification_duration_s"))))
    except (TypeError, ValueError):
        duration_s = 30
    return max(1, min(MAX_FINAL_VERIFICATION_DURATION_S, duration_s))


def numeric_sort_value(value) -> float | str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return float(number) if math.isfinite(number) else ""


def final_choice_is_performance_mode(auto_uv_mode: object) -> bool:
    return str(auto_uv_mode or "").strip().lower() == AUTO_UV_MODE_PERFORMANCE


def candidate_fpsw(candidate: dict) -> float | None:
    value = numeric_sort_value(candidate.get("efficiency_fps_per_w"))
    return None if value == "" else float(value)


def candidate_fps(candidate: dict) -> float | None:
    value = numeric_sort_value(candidate.get("avg_fps"))
    return None if value == "" else float(value)


def select_final_candidate(
    *,
    QtCore,
    QtGui=None,
    QtWidgets,
    parent,
    candidates: list[dict],
    default_candidate_id: str,
    default_duration_s: int = DEFAULT_FINAL_VERIFICATION_DURATION_S,
    max_duration_s: int = MAX_FINAL_VERIFICATION_DURATION_S,
    default_sort_column: int | None = None,
    auto_uv_mode: object = "",
    request_reason: object = "",
) -> tuple[dict | None, int, bool]:
    if not candidates:
        return None, int(default_duration_s), True

    candidates = sort_candidates_for_final_choice(list(candidates), auto_uv_mode)
    default_candidate_id = (
        best_final_choice_candidate_id(candidates, auto_uv_mode)
        or str(default_candidate_id or "").strip()
    )
    by_id = {
        str(candidate.get("candidate_id", "")): candidate for candidate in candidates
    }
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Choose Final verification candidate")
    dialog.setMinimumWidth(900)
    dialog.setMinimumHeight(360)
    layout = QtWidgets.QVBoxLayout(dialog)

    label_text = final_choice_intro_text(
        auto_uv_mode,
        request_reason=request_reason,
    )
    label = QtWidgets.QLabel(label_text)
    label.setWordWrap(True)
    layout.addWidget(label)

    table = create_final_choice_table(
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        candidates=candidates,
        default_candidate_id=default_candidate_id,
        default_sort_column=(
            final_choice_sort_column_for_mode(auto_uv_mode)
            if default_sort_column is None
            else int(default_sort_column)
        ),
        auto_uv_mode=auto_uv_mode,
    )
    table.doubleClicked.connect(dialog.accept)
    layout.addWidget(table)

    if default_candidate_id in by_id:
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if (
                item is not None
                and str(item.data(QtCore.Qt.UserRole)) == default_candidate_id
            ):
                table.clearSelection()
                table.selectRow(row)
                table.setCurrentCell(row, 0)
                break
    else:
        table.clearSelection()
        table.selectRow(0)
        table.setCurrentCell(0, 0)

    duration_spin = QtWidgets.QSpinBox()
    duration_spin.setRange(1, max(1, _minutes(max_duration_s)))
    duration_spin.setSuffix(" min")
    duration_spin.setValue(_minutes(default_duration_s))
    duration_layout = QtWidgets.QHBoxLayout()
    duration_layout.addWidget(QtWidgets.QLabel("Final verification duration"))
    duration_layout.addWidget(duration_spin)
    duration_layout.addStretch(1)
    layout.addLayout(duration_layout)

    buttons = QtWidgets.QDialogButtonBox()
    discard_button = buttons.addButton(
        "Discard",
        QtWidgets.QDialogButtonBox.DestructiveRole,
    )
    use_button = buttons.addButton("Use Selected", QtWidgets.QDialogButtonBox.AcceptRole)

    def handle_button(button) -> None:
        if button is discard_button:
            dialog.reject()
        elif button is use_button:
            dialog.accept()

    buttons.clicked.connect(handle_button)
    layout.addWidget(buttons)
    use_button.setDefault(True)

    if dialog.exec() != QtWidgets.QDialog.Accepted:
        return None, _seconds(duration_spin.value()), True
    selected_rows = table.selectionModel().selectedRows(0)
    selected_row = int(selected_rows[-1].row()) if selected_rows else table.currentRow()
    if selected_row < 0:
        selected_row = 0
    item = table.item(selected_row, 0)
    selected_id = str(item.data(QtCore.Qt.UserRole) or "") if item is not None else ""
    return by_id.get(selected_id), _seconds(duration_spin.value()), False


def final_choice_intro_text(auto_uv_mode: object, *, request_reason: object = "") -> str:
    stopped = str(request_reason or "").strip().lower() == "user-stop"
    if final_choice_is_performance_mode(auto_uv_mode):
        metric_text = "highest-FPS"
    else:
        metric_text = "best FPS/W"
    if stopped:
        return (
            "Auto-UV was stopped. Choose one of the previously stable candidates. "
            f"The {metric_text} candidate is selected for the long Final verification."
        )
    return (
        "The short candidate sweep is complete. "
        f"The {metric_text} passed candidate is selected for the long Final verification."
    )


def create_final_choice_table(
    *,
    QtCore,
    QtGui=None,
    QtWidgets,
    candidates: list[dict],
    default_candidate_id: str,
    default_sort_column: int = FINAL_CHOICE_DEFAULT_SORT_COLUMN,
    auto_uv_mode: object = "",
):
    item_class = _sortable_table_item_class(QtWidgets, FINAL_CHOICE_SORT_ROLE)
    table = QtWidgets.QTableWidget(len(candidates), len(FINAL_CHOICE_COLUMNS))
    table.setHorizontalHeaderLabels(FINAL_CHOICE_COLUMNS)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
    table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    table.setSortingEnabled(False)
    header = table.horizontalHeader()
    header.setHighlightSections(False)
    header.setSectionsClickable(True)
    set_header_fit_column_widths(
        table,
        {
            0: 62,
            1: 108,
            2: 128,
            3: 134,
            4: 134,
            5: 92,
            6: 104,
            7: 110,
        },
        QtCore=QtCore,
        padding=34,
    )
    header.setStretchLastSection(True)

    for row, candidate in enumerate(candidates):
        candidate_id = str(candidate.get("candidate_id", ""))
        values = [
            candidate_number(candidate.get("candidate_voltage_mv"), precision=0),
            candidate_number(candidate.get("lock_clock_mhz"), precision=0),
            candidate_number(candidate.get("avg_core_clock_mhz"), precision=2),
            _candidate_metric_text(
                candidate,
                "efficiency_fps_per_w",
                precision=2,
            ),
            _candidate_metric_text(candidate, "avg_fps", precision=2),
            candidate_number(candidate.get("avg_power_w"), precision=2),
            _duration_label(candidate_short_duration_s(candidate)),
            candidate_status_text(
                candidate,
                candidate_id == default_candidate_id,
                auto_uv_mode=auto_uv_mode,
            ),
        ]
        sort_values = final_choice_sort_values(candidate)
        for column, value in enumerate(values):
            item = item_class(str(value))
            item.setData(QtCore.Qt.UserRole, candidate_id)
            item.setData(FINAL_CHOICE_SORT_ROLE, sort_values[column])
            if column < 6:
                item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            if QtGui is not None and column == FINAL_CHOICE_FPSW_SORT_COLUMN:
                _paint_profile_delta_item(
                    item,
                    QtGui,
                    candidate.get("efficiency_fps_per_w"),
                    _profile_base_metric(candidate, "efficiency_fps_per_w"),
                    label="FPS/W",
                )
            if QtGui is not None and column == FINAL_CHOICE_FPS_SORT_COLUMN:
                _paint_profile_delta_item(
                    item,
                    QtGui,
                    candidate.get("avg_fps"),
                    _profile_base_metric(candidate, "avg_fps"),
                    label="FPS",
                )
            table.setItem(row, column, item)

    _connect_final_choice_sorting(
        QtCore=QtCore,
        table=table,
        item_class=item_class,
        default_sort_column=int(default_sort_column),
    )
    return table


def _candidate_metric_text(candidate: dict, metric: str, *, precision: int) -> str:
    return _format_profile_metric_with_delta(
        candidate.get(metric),
        _profile_base_metric(candidate, metric),
        precision=precision,
    )


def _connect_final_choice_sorting(
    *,
    QtCore,
    table,
    item_class,
    default_sort_column: int,
) -> None:
    header = table.horizontalHeader()
    sort_column = int(default_sort_column)
    sort_order = QtCore.Qt.DescendingOrder

    def apply_sort_indicator(column: int, order) -> None:
        signals_blocked = header.blockSignals(True)
        try:
            header.setSortIndicatorShown(True)
            header.setSortIndicator(int(column), order)
        finally:
            header.blockSignals(signals_blocked)

    def sort_table(column: int, order) -> None:
        nonlocal sort_column, sort_order
        if int(column) not in FINAL_CHOICE_SORTABLE_COLUMNS:
            apply_sort_indicator(sort_column, sort_order)
            return
        sort_column = int(column)
        sort_order = order
        apply_sort_indicator(sort_column, sort_order)
        item_class.sort_descending = order == QtCore.Qt.DescendingOrder
        table.sortItems(sort_column, sort_order)

    def sort_by_header_column(column: int) -> None:
        nonlocal sort_column, sort_order
        column = int(column)
        if column not in FINAL_CHOICE_SORTABLE_COLUMNS:
            apply_sort_indicator(sort_column, sort_order)
            return
        if sort_column == column:
            order = (
                QtCore.Qt.AscendingOrder
                if sort_order == QtCore.Qt.DescendingOrder
                else QtCore.Qt.DescendingOrder
            )
        else:
            order = (
                QtCore.Qt.DescendingOrder
                if column in FINAL_CHOICE_HIGHER_FIRST_COLUMNS
                else QtCore.Qt.AscendingOrder
            )
        sort_table(column, order)

    header.sectionClicked.connect(sort_by_header_column)
    sort_table(sort_column, sort_order)


def _sortable_table_item_class(QtWidgets, sort_role: int):
    class SortableTableWidgetItem(QtWidgets.QTableWidgetItem):
        sort_descending = False

        def __lt__(self, other):
            other_value = (
                other.data(sort_role) if hasattr(other, "data") else str(other.text())
            )
            return _sort_value_less(
                self.data(sort_role),
                other_value,
                descending=bool(self.sort_descending),
            )

    return SortableTableWidgetItem


def _sort_value_less(left, right, *, descending: bool = False) -> bool:
    left_missing = left in (None, "")
    right_missing = right in (None, "")
    if left_missing != right_missing:
        return left_missing if descending else right_missing
    if left_missing and right_missing:
        return False
    left_key = _sort_key(left)
    right_key = _sort_key(right)
    return left_key < right_key


def _sort_key(value) -> float | str:
    number = numeric_sort_value(value)
    if number != "":
        return float(number)
    return str(value).casefold()


def _duration_label(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    remaining_s = seconds % 60
    if remaining_s:
        return f"{minutes}min {remaining_s}s"
    return f"{minutes}min"


def _minutes(seconds: int) -> int:
    return max(1, int(round(int(seconds) / 60.0)))


def _seconds(minutes: int) -> int:
    return max(60, int(minutes) * 60)
