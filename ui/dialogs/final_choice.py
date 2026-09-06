from __future__ import annotations

from auto_uv.scan_mode.auto_uv_mode import (
    AUTO_UV_MODE_PERFORMANCE,
    normalize_auto_uv_mode,
)
from ui.features.auto_uv.final_choice_ranking import FINAL_CHOICE_DEFAULT_SORT_COLUMN
from ui.features.auto_uv.final_choice_ranking import FINAL_CHOICE_FPS_SORT_COLUMN
from ui.features.auto_uv.final_choice_ranking import FINAL_CHOICE_FPSW_SORT_COLUMN
from ui.features.auto_uv.final_choice_ranking import FINAL_CHOICE_HIGHER_FIRST_COLUMNS
from ui.features.auto_uv.final_choice_ranking import FINAL_CHOICE_SORTABLE_COLUMNS
from ui.features.auto_uv.final_choice_ranking import best_final_choice_candidate_id
from ui.features.auto_uv.final_choice_ranking import candidate_oc_mhz
from ui.features.auto_uv.final_choice_ranking import candidate_short_duration_s
from ui.features.auto_uv.final_choice_ranking import final_choice_shows_oc_column
from ui.features.auto_uv.final_choice_ranking import final_choice_sort_column_for_mode
from ui.features.auto_uv.final_choice_ranking import final_choice_sort_values
from ui.features.auto_uv.final_choice_ranking import numeric_sort_value
from ui.features.auto_uv.final_choice_ranking import sort_candidates_for_final_choice

from ..constants import DEFAULT_FINAL_VERIFICATION_DURATION_S
from ..constants import MAX_FINAL_VERIFICATION_DURATION_S
from ..components.profile_list import _format_profile_metric_with_delta
from ..components.profile_list import _paint_profile_delta_item
from ..components.profile_list import _profile_base_metric
from ..components.table_sizing import set_header_fit_column_widths


FINAL_CHOICE_SORT_ROLE = 261
FINAL_CHOICE_COLUMNS = [
    "mV",
    "Target MHz",
    "OC MHz",
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
    request_reason: object = "",
) -> str:
    parts = []
    if is_default:
        reason = str(request_reason or "").strip().lower()
        if reason == "previous-crash":
            parts.append("Resume from here")
        elif reason == "final-verification-failed":
            parts.append("Next safer pick")
        elif reason.startswith("adaptive-"):
            parts.append("Tier suggestion")
        else:
            parts.append(
                "Highest FPS"
                if normalize_auto_uv_mode(auto_uv_mode) == AUTO_UV_MODE_PERFORMANCE
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
    precision = max(0, min(int(precision), 4))
    return str(int(round(number))) if precision <= 0 else f"{number:.{precision}f}"


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
    recovery_decision: dict | None = None,
) -> tuple[dict | None, int, str]:
    if not candidates:
        return None, int(default_duration_s), "discard"

    reason = str(request_reason or "").strip().lower()
    previous_crash = reason == "previous-crash"
    final_verification_failed = reason == "final-verification-failed"
    recovery_prompt = previous_crash or final_verification_failed
    requested_default_id = str(default_candidate_id or "").strip()
    candidates = (
        list(candidates)
        if previous_crash
        else sort_candidates_for_final_choice(list(candidates), auto_uv_mode)
    )
    by_id = {
        str(candidate.get("candidate_id", "")): candidate for candidate in candidates
    }
    # Crash resume and adaptive tiers preselect the backend's requested
    # candidate (the failed point / the tier's confirmed point), not the
    # mode-metric best.
    honors_requested_default = previous_crash or reason.startswith("adaptive-")
    if honors_requested_default and requested_default_id in by_id:
        default_candidate_id = requested_default_id
    else:
        default_candidate_id = (
            best_final_choice_candidate_id(candidates, auto_uv_mode)
            or requested_default_id
        )
    dialog = QtWidgets.QDialog(parent)
    if reason.startswith("adaptive-"):
        tier_name = reason.removeprefix("adaptive-").title()
        window_title = f"Tune {tier_name} profile candidate"
    elif previous_crash:
        window_title = "Resume Auto-UV Candidate"
    elif final_verification_failed:
        window_title = "Choose Safer Auto-UV Candidate"
    else:
        window_title = "Choose Final verification candidate"
    dialog.setWindowTitle(window_title)
    dialog.setMinimumWidth(900)
    dialog.setMinimumHeight(360)
    layout = QtWidgets.QVBoxLayout(dialog)

    label_text = final_choice_intro_text(
        auto_uv_mode,
        request_reason=request_reason,
        recovery_decision=recovery_decision,
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
            None
            if previous_crash and default_sort_column is None
            else final_choice_sort_column_for_mode(auto_uv_mode)
            if default_sort_column is None
            else int(default_sort_column)
        ),
        auto_uv_mode=auto_uv_mode,
        request_reason=request_reason,
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
    if not recovery_prompt:
        duration_layout = QtWidgets.QHBoxLayout()
        duration_layout.addWidget(QtWidgets.QLabel("Final verification duration"))
        duration_layout.addWidget(duration_spin)
        duration_layout.addStretch(1)
        layout.addLayout(duration_layout)

    buttons = QtWidgets.QDialogButtonBox()
    discard_button = buttons.addButton(
        "Start From Scratch" if recovery_prompt else "Discard",
        QtWidgets.QDialogButtonBox.DestructiveRole,
    )
    abort_button = None
    if recovery_prompt:
        abort_button = buttons.addButton(
            "Abort",
            QtWidgets.QDialogButtonBox.RejectRole,
        )
    if final_verification_failed:
        use_label = "Verify Selected"
    elif previous_crash:
        use_label = "Resume Selected"
    else:
        use_label = "Use Selected"
    use_button = buttons.addButton(
        use_label,
        QtWidgets.QDialogButtonBox.AcceptRole,
    )
    rejected_action = {"value": "abort" if recovery_prompt else "discard"}

    def handle_button(button) -> None:
        if button is discard_button:
            rejected_action["value"] = "discard"
            dialog.reject()
        elif button is abort_button:
            rejected_action["value"] = "abort"
            dialog.reject()
        elif button is use_button:
            dialog.accept()

    buttons.clicked.connect(handle_button)
    layout.addWidget(buttons)
    use_button.setDefault(True)

    if dialog.exec() != QtWidgets.QDialog.Accepted:
        return None, _seconds(duration_spin.value()), rejected_action["value"]
    selected_rows = table.selectionModel().selectedRows(0)
    selected_row = int(selected_rows[-1].row()) if selected_rows else table.currentRow()
    if selected_row < 0:
        selected_row = 0
    item = table.item(selected_row, 0)
    selected_id = str(item.data(QtCore.Qt.UserRole) or "") if item is not None else ""
    return by_id.get(selected_id), _seconds(duration_spin.value()), "select"


def final_choice_intro_text(
    auto_uv_mode: object,
    *,
    request_reason: object = "",
    recovery_decision: dict | None = None,
) -> str:
    reason = str(request_reason or "").strip().lower()
    if reason.startswith("adaptive-"):
        return (
            "The adaptive sweep is complete. This tier's confirmed candidate "
            "is selected for the long Final verification; pick another passed "
            "candidate to tune this tier's profile."
        )
    stopped = reason == "user-stop"
    previous_crash = reason == "previous-crash"
    final_verification_failed = reason == "final-verification-failed"
    metric_text = (
        "highest FPS"
        if normalize_auto_uv_mode(auto_uv_mode) == AUTO_UV_MODE_PERFORMANCE
        else "best FPS/W"
    )
    if previous_crash:
        detail = recovery_decision_text(recovery_decision)
        suffix = f" Previous decision: {detail}" if detail else ""
        return (
            "A previous Auto-UV run ended during a GPU probe. "
            "Choose a saved candidate to resume from a safer voltage bin, "
            "or start a new scan from scratch."
            f"{suffix}"
        )
    if final_verification_failed:
        detail = recovery_decision_text(recovery_decision)
        suffix = f" Failed candidate: {detail}." if detail else ""
        return (
            "Final verification failed. Choose another passed short-probe "
            "candidate at a safer voltage, or start a new scan from scratch. "
            f"The {metric_text} remaining candidate is selected by default."
            f"{suffix}"
        )
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
    default_sort_column: int | None = FINAL_CHOICE_DEFAULT_SORT_COLUMN,
    auto_uv_mode: object = "",
    request_reason: object = "",
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
            2: 88,
            3: 128,
            4: 154,
            5: 134,
            6: 92,
            7: 104,
            8: 128,
        },
        QtCore=QtCore,
        padding=34,
    )
    if not final_choice_shows_oc_column(auto_uv_mode):
        table.setColumnHidden(2, True)
    header.setStretchLastSection(True)

    for row, candidate in enumerate(candidates):
        candidate_id = str(candidate.get("candidate_id", ""))
        values = [
            candidate_number(candidate.get("candidate_voltage_mv"), precision=0),
            candidate_number(candidate.get("lock_clock_mhz"), precision=0),
            candidate_oc_text(candidate),
            candidate_number(candidate.get("avg_core_clock_mhz"), precision=2),
            _candidate_metric_text(
                candidate,
                "efficiency_fps_per_w",
                precision=4,
            ),
            _candidate_metric_text(candidate, "avg_fps", precision=2),
            _candidate_metric_text(
                candidate,
                "avg_power_w",
                precision=2,
                lower_is_better=True,
            ),
            _duration_label(candidate_short_duration_s(candidate)),
            candidate_status_text(
                candidate,
                candidate_id == default_candidate_id,
                auto_uv_mode=auto_uv_mode,
                request_reason=request_reason,
            ),
        ]
        sort_values = final_choice_sort_values(candidate)
        for column, value in enumerate(values):
            item = item_class(str(value))
            item.setData(QtCore.Qt.UserRole, candidate_id)
            item.setData(FINAL_CHOICE_SORT_ROLE, sort_values[column])
            if column < 7:
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
            if QtGui is not None and column == 6:
                _paint_profile_delta_item(
                    item,
                    QtGui,
                    candidate.get("avg_power_w"),
                    _profile_base_metric(candidate, "avg_power_w"),
                    label="Power W",
                    lower_is_better=True,
                )
            table.setItem(row, column, item)

    _connect_final_choice_sorting(
        QtCore=QtCore,
        table=table,
        item_class=item_class,
        default_sort_column=default_sort_column,
    )
    return table


def _candidate_metric_text(
    candidate: dict,
    metric: str,
    *,
    precision: int,
    lower_is_better: bool = False,
) -> str:
    return _format_profile_metric_with_delta(
        candidate.get(metric),
        _profile_base_metric(candidate, metric),
        precision=precision,
        lower_is_better=lower_is_better,
    )


def candidate_oc_text(candidate: dict) -> str:
    rounded = candidate_oc_mhz(candidate)
    if rounded is None:
        return ""
    return f"{rounded:+d}"


def recovery_decision_text(recovery_decision: dict | None) -> str:
    if not isinstance(recovery_decision, dict):
        return ""
    voltage = candidate_number(recovery_decision.get("candidate_voltage_mv"), precision=0)
    clock = candidate_number(recovery_decision.get("lock_clock_mhz"), precision=0)
    decision = str(recovery_decision.get("decision") or "").strip()
    prefix = f"{voltage}mV@{clock}MHz" if voltage and clock else ""
    if prefix and decision:
        return f"{prefix} - {decision}"
    return decision or prefix


def _connect_final_choice_sorting(
    *,
    QtCore,
    table,
    item_class,
    default_sort_column: int | None,
) -> None:
    header = table.horizontalHeader()
    sort_column = int(default_sort_column) if default_sort_column is not None else None
    sort_order = QtCore.Qt.DescendingOrder

    def apply_sort_indicator(column: int | None, order) -> None:
        signals_blocked = header.blockSignals(True)
        try:
            if column is None:
                header.setSortIndicatorShown(False)
            else:
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
    if default_sort_column is not None:
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
