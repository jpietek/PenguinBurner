from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from ..styles import curve_editor_legend_stylesheet


def nearest_curve_point(
    x_value: float,
    y_value: float,
    points: list[tuple[float, float]],
    view_range,
    *,
    max_normalized_distance: float = 0.04,
) -> tuple[float, float] | None:
    """Return the curve point closest to (x_value, y_value) in view-normalized space.

    Shared by the VF and fan curve editors. Returns None when no point lies
    within ``max_normalized_distance`` of the cursor.
    """
    if not points:
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
    for point in points:
        distance = (
            ((float(point[0]) - float(x_value)) / x_span) ** 2
            + ((float(point[1]) - float(y_value)) / y_span) ** 2
        ) ** 0.5
        if best_distance is None or distance < best_distance:
            best_point = point
            best_distance = distance
    if best_distance is None or best_distance > float(max_normalized_distance):
        return None
    return best_point


def install_curve_editor_shortcut_legend(
    *,
    QtWidgets,
    QtCore,
    plot_widget,
    parent,
    rows: tuple[tuple[str, str], ...],
    title: str = "Editor keys",
    object_prefix: str = "curveEditorShortcut",
):
    legend_overlay = QtWidgets.QFrame(plot_widget)
    legend_overlay.setObjectName(f"{object_prefix}Legend")
    styled_background = getattr(
        getattr(QtCore.Qt, "WidgetAttribute", QtCore.Qt),
        "WA_StyledBackground",
        None,
    )
    if styled_background is not None:
        legend_overlay.setAttribute(styled_background, True)
    legend_overlay.setStyleSheet(curve_editor_legend_stylesheet(object_prefix))
    legend_layout = QtWidgets.QVBoxLayout(legend_overlay)
    legend_layout.setContentsMargins(7, 5, 7, 6)
    legend_layout.setSpacing(4)
    legend_header = QtWidgets.QHBoxLayout()
    legend_header.setContentsMargins(0, 0, 0, 0)
    legend_header.setSpacing(5)
    legend_title = QtWidgets.QLabel(title)
    legend_title.setObjectName(f"{object_prefix}Title")
    legend_toggle = QtWidgets.QToolButton()
    legend_toggle.setObjectName(f"{object_prefix}Toggle")
    legend_toggle.setAutoRaise(True)
    legend_header.addWidget(legend_title)
    legend_header.addStretch(1)
    legend_header.addWidget(legend_toggle)
    legend_layout.addLayout(legend_header)
    legend_body = QtWidgets.QWidget()
    legend_grid = QtWidgets.QGridLayout(legend_body)
    legend_grid.setContentsMargins(0, 0, 0, 0)
    legend_grid.setHorizontalSpacing(7)
    legend_grid.setVerticalSpacing(3)
    split_at = (len(rows) + 1) // 2
    for index, (keys, text) in enumerate(rows):
        grid_row = index % split_at
        grid_col = 0 if index < split_at else 2
        key_label = QtWidgets.QLabel(keys)
        key_label.setObjectName(f"{object_prefix}Key")
        text_label = QtWidgets.QLabel(text)
        text_label.setObjectName(f"{object_prefix}Text")
        legend_grid.addWidget(key_label, grid_row, grid_col)
        legend_grid.addWidget(text_label, grid_row, grid_col + 1)
    legend_layout.addWidget(legend_body)
    legend_state = {"collapsed": False}

    def position_legend() -> None:
        legend_overlay.adjustSize()
        size = legend_overlay.sizeHint()
        legend_overlay.resize(size)
        margin = 12
        x_pos = int(margin)
        y_pos = max(
            int(margin),
            int(plot_widget.height()) - int(size.height()) - int(margin),
        )
        legend_overlay.move(x_pos, y_pos)
        legend_overlay.raise_()

    def sync_legend() -> None:
        collapsed = bool(legend_state["collapsed"])
        legend_body.setVisible(not collapsed)
        legend_title.setText("Keys" if collapsed else str(title))
        legend_toggle.setText("+" if collapsed else "-")
        legend_toggle.setToolTip(
            "Show curve editor shortcuts" if collapsed else "Hide curve editor shortcuts"
        )
        position_legend()

    def toggle_legend() -> None:
        legend_state["collapsed"] = not bool(legend_state["collapsed"])
        sync_legend()

    legend_toggle.clicked.connect(toggle_legend)
    resize_event_type = getattr(
        getattr(QtCore.QEvent, "Type", QtCore.QEvent),
        "Resize",
        None,
    )

    class CurveEditorLegendFilter(QtCore.QObject):
        def eventFilter(_filter_self, _watched, event) -> bool:
            if resize_event_type is not None and event.type() == resize_event_type:
                position_legend()
            return False

    legend_filter = CurveEditorLegendFilter(parent)
    plot_widget.installEventFilter(legend_filter)
    sync_legend()
    QtCore.QTimer.singleShot(0, position_legend)
    return legend_overlay, legend_filter


EditT = TypeVar("EditT")


class CurveEditHistory[EditT]:
    def __init__(
        self,
        *,
        clone: Callable[[EditT], EditT],
        equals: Callable[[EditT, EditT], bool],
        apply: Callable[[EditT], None],
        changed: Callable[[], None] | None = None,
        limit: int = 64,
    ):
        self._clone = clone
        self._equals = equals
        self._apply = apply
        self._changed = changed
        self._limit = max(1, int(limit))
        self.undo_stack: list[EditT] = []
        self.redo_stack: list[EditT] = []
        self.active_actions: dict[str, EditT] = {}

    def clear(self) -> None:
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.active_actions.clear()
        self._notify_changed()

    def push(self, value: EditT, *, clear_redo: bool = True) -> None:
        snapshot = self._clone(value)
        if self.undo_stack and self._equals(self.undo_stack[-1], snapshot):
            return
        self.undo_stack.append(snapshot)
        del self.undo_stack[: -self._limit]
        if clear_redo:
            self.redo_stack.clear()
        self._notify_changed()

    def begin_action(self, action_key: str, value: EditT) -> None:
        key = str(action_key)
        if key in self.active_actions:
            return
        snapshot = self._clone(value)
        self.active_actions[key] = snapshot
        self.push(snapshot)

    def finish_action(self, action_key: str, current_value: EditT) -> None:
        key = str(action_key)
        snapshot = self.active_actions.pop(key, None)
        if snapshot is None:
            return
        if self._equals(current_value, snapshot) and self.undo_stack:
            self.undo_stack.pop()
        self._notify_changed()

    def undo(self, current_value: EditT) -> None:
        if not self.undo_stack:
            return
        self.active_actions.clear()
        previous = self.undo_stack.pop()
        self.redo_stack.append(self._clone(current_value))
        del self.redo_stack[: -self._limit]
        self._apply(previous)
        self._notify_changed()

    def redo(self, current_value: EditT) -> None:
        if not self.redo_stack:
            return
        self.active_actions.clear()
        next_value = self.redo_stack.pop()
        self.undo_stack.append(self._clone(current_value))
        del self.undo_stack[: -self._limit]
        self._apply(next_value)
        self._notify_changed()

    def _notify_changed(self) -> None:
        if self._changed is not None:
            self._changed()
