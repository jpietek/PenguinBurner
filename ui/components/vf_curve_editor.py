from __future__ import annotations

from collections.abc import Callable
from typing import Any

from curve_editors.uv.vf_curve_manual_editor import (
    ManualCurveEdit,
    manual_add_curve_point_edit,
    manual_drag_anchor_edit,
    manual_flatten_from_existing_point,
    manual_nudge_selected_frequency,
    manual_nudge_selected_voltage,
    manual_offset_selected_range,
    manual_select_adjacent_point,
    manual_select_curve_point,
    manual_select_range_to_right,
    manual_tune_single_point_edit,
)
from profiles.uv.profile_vf_curve import curve_points_from_values

from .. import theme
from .curve_editor import (
    CurveEditHistory,
    install_curve_editor_shortcut_legend,
    nearest_curve_point,
)
from .curve_plot import CurvePlot


def vf_curve_editor_shortcut_legend_rows() -> tuple[tuple[str, str], ...]:
    return (
        ("Click", "select bin"),
        ("Ctrl+Click", "new point"),
        ("Drag", "move selected bin"),
        ("Up/Down", "clock"),
        ("Left/Right", "voltage"),
        ("Shift+Right", "select range"),
        ("Ctrl+L", "flatten here"),
        ("Ctrl+Z / Ctrl+Y", "undo / redo"),
    )


def open_vf_curve_editor_dialog(
    *,
    QtCore,
    QtGui,
    QtWidgets,
    pg,
    parent,
    plan: list[dict],
    base_points: list[tuple[float, float]],
    anchor: tuple[int, int],
    save_callback: Callable[[ManualCurveEdit], str | None],
    control_voltage_mvs: tuple[int, ...] | list[int] = (),
) -> bool:
    if pg is None:
        QtWidgets.QMessageBox.information(
            parent,
            "Edit VF Curve",
            "pyqtgraph is not installed, so the curve editor is unavailable.",
        )
        return False

    original_anchor_voltage_mv, original_anchor_clock_mhz = anchor

    def initial_manual_edit() -> ManualCurveEdit:
        return ManualCurveEdit(
            plan=[dict(item) for item in plan],
            anchor_voltage_mv=int(original_anchor_voltage_mv),
            anchor_clock_mhz=int(original_anchor_clock_mhz),
            edit_kind="unchanged",
            selected_voltage_mv=int(original_anchor_voltage_mv),
            selected_clock_mhz=int(original_anchor_clock_mhz),
            control_voltage_mvs=tuple(int(value) for value in control_voltage_mvs),
        )

    current_edit = {"value": initial_manual_edit()}
    syncing_target = {"active": False}
    syncing_point_handles = {"active": False}
    point_handle_items: dict[int, Any] = {}

    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Edit VF Curve")
    dialog.resize(900, 640)
    layout = QtWidgets.QVBoxLayout(dialog)
    initial_curve_points = curve_points_from_values(plan)
    plot = CurvePlot(
        QtWidgets=QtWidgets,
        pg=pg,
        x_label="Voltage",
        x_units="mV",
        y_label="Clock",
        y_units="MHz",
        source_name="Reference",
        candidate_name="Edited draft",
        show_source=bool(base_points),
        source_color=theme.BASELINE,
        source_alpha=204,
        candidate_color=theme.PLOT_CANDIDATE,
    )
    if base_points:
        plot.set_source_points(base_points)
    plot.add_comparison_points(
        initial_curve_points,
        name="Before edit",
        color=theme.PLOT_COMPARISON,
        alpha=145,
        width=1,
    )
    plot.set_candidate_points(initial_curve_points, remember_previous=False)
    plot.set_selected_point(original_anchor_voltage_mv, original_anchor_clock_mhz)
    _style_passive_vf_bin_symbols(
        pg,
        getattr(plot, "source_curve", None),
        color=theme.BASELINE,
        alpha=140,
    )
    _style_passive_vf_bin_symbols(
        pg,
        getattr(plot, "candidate_curve", None),
        color=theme.PLOT_CANDIDATE,
        alpha=150,
    )
    layout.addWidget(plot.widget, 1)

    legend_overlay, legend_filter = install_curve_editor_shortcut_legend(
        QtWidgets=QtWidgets,
        QtCore=QtCore,
        plot_widget=plot.widget,
        parent=dialog,
        rows=vf_curve_editor_shortcut_legend_rows(),
    )
    dialog._curve_editor_shortcut_legend = legend_overlay
    dialog._curve_editor_shortcut_legend_filter = legend_filter

    target_item = pg.TargetItem(
        pos=(float(original_anchor_voltage_mv), float(original_anchor_clock_mhz)),
        size=14,
        symbol="o",
        pen=pg.mkPen(pg.mkColor(255, 229, 177, 240), width=1),
        brush=pg.mkBrush(pg.mkColor(240, 184, 76, 235)),
        movable=True,
    )
    plot.plot.addItem(target_item)
    selected_range_overlay = plot.plot.plot(
        [],
        [],
        pen=None,
        symbol="o",
        symbolPen=pg.mkPen(pg.mkColor(255, 229, 177, 245), width=1),
        symbolBrush=pg.mkBrush(pg.mkColor(240, 184, 76, 225)),
        symbolSize=8,
    )
    if hasattr(selected_range_overlay, "setZValue"):
        selected_range_overlay.setZValue(8)

    status_label = QtWidgets.QLabel()
    status_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    layout.addWidget(status_label)

    button_row = QtWidgets.QHBoxLayout()
    revert_button = QtWidgets.QPushButton("Revert")
    undo_button = QtWidgets.QPushButton("Undo")
    redo_button = QtWidgets.QPushButton("Redo")
    save_button = QtWidgets.QPushButton("Save")
    cancel_button = QtWidgets.QPushButton("Cancel")
    undo_button.setToolTip("Undo last edit (Ctrl+Z)")
    redo_button.setToolTip("Redo last undone edit (Ctrl+Y)")
    standard_pixmap = getattr(QtWidgets.QStyle, "StandardPixmap", QtWidgets.QStyle)
    save_icon_id = getattr(standard_pixmap, "SP_DialogSaveButton", None)
    if save_icon_id is not None:
        save_button.setIcon(dialog.style().standardIcon(save_icon_id))
    for widget in (revert_button, undo_button, redo_button):
        button_row.addWidget(widget)
    button_row.addStretch(1)
    button_row.addWidget(save_button)
    button_row.addWidget(cancel_button)
    layout.addLayout(button_row)

    def clone_edit(edit: ManualCurveEdit) -> ManualCurveEdit:
        return ManualCurveEdit(
            plan=[dict(item) for item in edit.plan],
            anchor_voltage_mv=int(edit.anchor_voltage_mv),
            anchor_clock_mhz=int(edit.anchor_clock_mhz),
            edit_kind=str(edit.edit_kind),
            selected_voltage_mv=edit.selected_voltage_mv,
            selected_clock_mhz=edit.selected_clock_mhz,
            selected_range_start_voltage_mv=edit.selected_range_start_voltage_mv,
            control_voltage_mvs=edit.control_voltage_mvs,
        )

    def edit_signature(edit: ManualCurveEdit):
        return (
            int(edit.anchor_voltage_mv),
            int(edit.anchor_clock_mhz),
            str(edit.edit_kind),
            edit.selected_voltage_mv,
            edit.selected_clock_mhz,
            edit.selected_range_start_voltage_mv,
            tuple(int(value) for value in edit.control_voltage_mvs),
            tuple(
                (
                    int(point.get("index", 0)),
                    int(point.get("voltage_mv", 0)),
                    int(point.get("base_mhz", 0)),
                    int(point.get("target_mhz", 0)),
                    int(point.get("new_offset_mhz", 0)),
                )
                for point in edit.plan
            ),
        )

    def edits_equal(left: ManualCurveEdit, right: ManualCurveEdit) -> bool:
        return edit_signature(left) == edit_signature(right)

    def update_history_buttons() -> None:
        undo_button.setEnabled(bool(history.undo_stack))
        redo_button.setEnabled(bool(history.redo_stack))

    def set_preview(
        edit: ManualCurveEdit,
        *,
        move_target: bool = True,
        record_undo: bool = False,
    ) -> None:
        if record_undo and not edits_equal(current_edit["value"], edit):
            history.push(current_edit["value"])
        current_edit["value"] = edit
        plot.set_candidate_points(curve_points_from_values(edit.plan), remember_previous=False)
        update_selected_range_overlay(edit)
        selected_voltage_mv = edit.selected_voltage_mv or edit.anchor_voltage_mv
        selected_clock_mhz = edit.selected_clock_mhz or edit.anchor_clock_mhz
        plot.set_selected_point(selected_voltage_mv, selected_clock_mhz)
        update_status(edit)
        if move_target:
            snap_target(edit)
        refresh_point_handles(edit)

    history = CurveEditHistory(
        clone=clone_edit,
        equals=edits_equal,
        apply=lambda edit: set_preview(edit, record_undo=False),
        changed=update_history_buttons,
    )

    def update_status(edit: ManualCurveEdit) -> None:
        selected_voltage_mv = edit.selected_voltage_mv or edit.anchor_voltage_mv
        selected_clock_mhz = edit.selected_clock_mhz or edit.anchor_clock_mhz
        if edit.selected_range_start_voltage_mv is not None:
            status_label.setText(
                "Selected range: "
                f"{int(edit.selected_range_start_voltage_mv)} mV -> flat tail. "
                f"Start bin: {int(selected_voltage_mv)} mV / "
                f"{int(selected_clock_mhz)} MHz. "
                f"Flat max: {int(edit.anchor_clock_mhz)} MHz."
            )
            return
        if edit.edit_kind == "add-point":
            prefix = "Added point"
        elif edit.edit_kind == "single-point":
            prefix = "Edited point"
        else:
            prefix = "Selected bin"
        status_label.setText(
            f"{prefix}: {int(selected_voltage_mv)} mV / "
            f"{int(selected_clock_mhz)} MHz. Saved edits require Verify before Apply."
        )

    def selected_range_points(edit: ManualCurveEdit) -> list[tuple[float, float]]:
        if edit.selected_range_start_voltage_mv is None:
            return []
        start_voltage = int(edit.selected_range_start_voltage_mv)
        points = []
        for raw in edit.plan:
            try:
                voltage_mv = int(raw["voltage_mv"])
                clock_mhz = int(raw["target_mhz"])
            except (KeyError, TypeError, ValueError):
                continue
            if voltage_mv < start_voltage:
                continue
            if voltage_mv >= int(edit.anchor_voltage_mv):
                clock_mhz = int(edit.anchor_clock_mhz)
            points.append((float(voltage_mv), float(clock_mhz)))
        return sorted(points)

    def update_selected_range_overlay(edit: ManualCurveEdit) -> None:
        points = selected_range_points(edit)
        selected_range_overlay.setData(
            [point[0] for point in points],
            [point[1] for point in points],
        )

    def snap_target(edit: ManualCurveEdit) -> None:
        syncing_target["active"] = True
        try:
            target_item.setPos(float(edit.anchor_voltage_mv), float(edit.anchor_clock_mhz))
        finally:
            syncing_target["active"] = False

    def target_position() -> tuple[float, float]:
        pos = target_item.pos()
        return float(pos.x()), float(pos.y())

    def on_target_position_changed(*_args) -> None:
        if syncing_target["active"]:
            return
        requested_voltage_mv, requested_clock_mhz = target_position()
        current_value = current_edit["value"]
        if current_value.selected_range_start_voltage_mv is not None:
            edit = manual_offset_selected_range(
                current_value,
                reference_voltage_mv=float(current_value.anchor_voltage_mv),
                requested_clock_mhz=requested_clock_mhz,
            )
        else:
            edit = manual_drag_anchor_edit(
                current_value.plan,
                anchor_voltage_mv=int(current_value.anchor_voltage_mv),
                anchor_clock_mhz=int(current_value.anchor_clock_mhz),
                requested_voltage_mv=requested_voltage_mv,
                requested_clock_mhz=requested_clock_mhz,
                control_voltage_mvs=current_value.control_voltage_mvs,
            )
        if not edits_equal(edit, current_value):
            history.begin_action("target", current_value)
        set_preview(edit, move_target=True)

    def on_target_position_finished(*_args) -> None:
        history.finish_action("target", current_edit["value"])
        snap_target(current_edit["value"])

    def point_handle_values(edit: ManualCurveEdit) -> dict[int, int]:
        values = {}
        control_voltage_mvs = {int(value) for value in edit.control_voltage_mvs}
        anchor_voltage_mv = int(edit.anchor_voltage_mv)
        selected_voltage_mv = (
            None
            if edit.selected_voltage_mv is None
            else int(edit.selected_voltage_mv)
        )
        for raw in edit.plan:
            try:
                voltage_mv = int(raw["voltage_mv"])
                clock_mhz = int(raw["target_mhz"])
            except (KeyError, TypeError, ValueError):
                continue
            if voltage_mv == anchor_voltage_mv:
                continue
            is_control = (
                voltage_mv < anchor_voltage_mv
                and voltage_mv in control_voltage_mvs
            )
            if is_control or voltage_mv == selected_voltage_mv:
                values[voltage_mv] = clock_mhz
        return dict(sorted(values.items()))

    def selectable_curve_points(edit: ManualCurveEdit) -> list[tuple[float, float]]:
        points = []
        for raw in edit.plan:
            try:
                voltage_mv = int(raw["voltage_mv"])
                clock_mhz = int(raw["target_mhz"])
            except (KeyError, TypeError, ValueError):
                continue
            if voltage_mv == int(edit.anchor_voltage_mv):
                clock_mhz = int(edit.anchor_clock_mhz)
            points.append((float(voltage_mv), float(clock_mhz)))
        return sorted(points)

    def point_item(entry: Any) -> Any:
        return entry.get("item") if isinstance(entry, dict) else entry

    def style_point_handle(item, *, selected: bool) -> None:
        if item is None:
            return
        if selected:
            item.setPen(pg.mkPen(pg.mkColor(255, 229, 177, 230), width=1))
            item.setBrush(pg.mkBrush(pg.mkColor(240, 184, 76, 225)))
        else:
            item.setPen(pg.mkPen(pg.mkColor(210, 226, 235, 190), width=1))
            item.setBrush(pg.mkBrush(pg.mkColor(94, 243, 140, 110)))

    def point_handle_is_highlighted(edit: ManualCurveEdit, voltage_mv: int) -> bool:
        if (
            edit.selected_range_start_voltage_mv is not None
            and int(voltage_mv) >= int(edit.selected_range_start_voltage_mv)
        ):
            return True
        return bool(
            edit.selected_voltage_mv is not None
            and int(voltage_mv) == int(edit.selected_voltage_mv)
        )

    def add_point_handle(voltage_mv: int, clock_mhz: int) -> None:
        item = pg.TargetItem(
            pos=(float(voltage_mv), float(clock_mhz)),
            size=11,
            symbol="o",
            pen=pg.mkPen(pg.mkColor(210, 226, 235, 190), width=1),
            brush=pg.mkBrush(pg.mkColor(94, 243, 140, 150)),
            movable=True,
        )
        if hasattr(item, "setZValue"):
            item.setZValue(6)
        item_mouse_click_event = item.mouseClickEvent

        def on_item_mouse_click_event(
            event,
            point_voltage_mv=int(voltage_mv),
            original_mouse_click=item_mouse_click_event,
        ) -> None:
            select_curve_point(float(point_voltage_mv))
            original_mouse_click(event)

        item.mouseClickEvent = on_item_mouse_click_event

        def on_changed(*_args, point_voltage_mv=int(voltage_mv)) -> None:
            on_point_handle_position_changed(point_voltage_mv)

        def on_finished(*_args, point_voltage_mv=int(voltage_mv)) -> None:
            history.finish_action(f"point:{int(point_voltage_mv)}", current_edit["value"])
            snap_point_handle(point_voltage_mv)

        item.sigPositionChanged.connect(on_changed)
        item.sigPositionChangeFinished.connect(on_finished)
        plot.plot.addItem(item)
        point_handle_items[int(voltage_mv)] = {"item": item}

    def refresh_point_handles(edit: ManualCurveEdit) -> None:
        values = point_handle_values(edit)
        for voltage_mv in list(point_handle_items):
            if voltage_mv not in values:
                item = point_item(point_handle_items.pop(voltage_mv))
                if item is not None:
                    plot.plot.removeItem(item)
        for voltage_mv, clock_mhz in values.items():
            if voltage_mv not in point_handle_items:
                add_point_handle(voltage_mv, clock_mhz)
        syncing_point_handles["active"] = True
        try:
            for voltage_mv, clock_mhz in values.items():
                item = point_item(point_handle_items.get(voltage_mv))
                if item is None:
                    continue
                item.setPos(float(voltage_mv), float(clock_mhz))
                style_point_handle(
                    item,
                    selected=point_handle_is_highlighted(edit, voltage_mv),
                )
        finally:
            syncing_point_handles["active"] = False

    def snap_point_handle(voltage_mv: int) -> None:
        values = point_handle_values(current_edit["value"])
        item = point_item(point_handle_items.get(int(voltage_mv)))
        clock_mhz = values.get(int(voltage_mv))
        if item is None or clock_mhz is None:
            return
        syncing_point_handles["active"] = True
        try:
            item.setPos(float(voltage_mv), float(clock_mhz))
        finally:
            syncing_point_handles["active"] = False

    def on_point_handle_position_changed(voltage_mv: int) -> None:
        if syncing_point_handles["active"]:
            return
        item = point_item(point_handle_items.get(int(voltage_mv)))
        if item is None:
            return
        pos = item.pos()
        current_value = current_edit["value"]
        if (
            current_value.selected_range_start_voltage_mv is not None
            and int(voltage_mv) >= int(current_value.selected_range_start_voltage_mv)
        ):
            edit = manual_offset_selected_range(
                current_value,
                reference_voltage_mv=int(voltage_mv),
                requested_clock_mhz=float(pos.y()),
            )
        else:
            edit = manual_tune_single_point_edit(
                current_value.plan,
                anchor_voltage_mv=int(current_value.anchor_voltage_mv),
                anchor_clock_mhz=int(current_value.anchor_clock_mhz),
                point_voltage_mv=int(voltage_mv),
                requested_voltage_mv=float(pos.x()),
                requested_clock_mhz=float(pos.y()),
                control_voltage_mvs=current_value.control_voltage_mvs,
            )
        if not edits_equal(edit, current_value):
            history.begin_action(f"point:{int(voltage_mv)}", current_value)
        set_preview(edit, move_target=False)

    def select_curve_point(voltage_mv: float) -> None:
        history.active_actions.clear()
        edit = manual_select_curve_point(current_edit["value"], voltage_mv=voltage_mv)
        set_preview(edit, move_target=True, record_undo=False)

    def select_adjacent_curve_point(direction: int) -> None:
        history.active_actions.clear()
        edit = manual_select_adjacent_point(current_edit["value"], direction=direction)
        set_preview(edit, move_target=True, record_undo=False)

    def select_range_to_right() -> None:
        history.active_actions.clear()
        edit = manual_select_range_to_right(current_edit["value"])
        set_preview(edit, move_target=True, record_undo=False)

    def lock_selected_curve_point() -> None:
        history.active_actions.clear()
        selected_voltage_mv = (
            current_edit["value"].selected_voltage_mv
            if current_edit["value"].selected_voltage_mv is not None
            else current_edit["value"].anchor_voltage_mv
        )
        edit = manual_flatten_from_existing_point(
            current_edit["value"].plan,
            anchor_voltage_mv=int(current_edit["value"].anchor_voltage_mv),
            clicked_voltage_mv=float(selected_voltage_mv),
            control_voltage_mvs=current_edit["value"].control_voltage_mvs,
        )
        set_preview(edit, record_undo=True)

    def nudge_selected_frequency(direction: int) -> None:
        history.active_actions.clear()
        edit = manual_nudge_selected_frequency(current_edit["value"], direction=direction)
        set_preview(edit, move_target=True, record_undo=True)

    def nudge_selected_voltage(direction: int) -> None:
        history.active_actions.clear()
        edit = manual_nudge_selected_voltage(current_edit["value"], direction=direction)
        set_preview(edit, move_target=True, record_undo=True)

    def nearest_curve_point_at_scene_pos(scene_pos):
        if not plot.plot.sceneBoundingRect().contains(scene_pos):
            return None
        view_pos = plot.plot.plotItem.vb.mapSceneToView(scene_pos)
        return nearest_curve_point(
            float(view_pos.x()),
            float(view_pos.y()),
            selectable_curve_points(current_edit["value"]),
            plot.plot.viewRange(),
            max_normalized_distance=0.08,
        )

    def create_curve_point_at_scene_pos(scene_pos) -> bool:
        if not plot.plot.sceneBoundingRect().contains(scene_pos):
            return False
        view_pos = plot.plot.plotItem.vb.mapSceneToView(scene_pos)
        history.active_actions.clear()
        edit = manual_add_curve_point_edit(
            current_edit["value"],
            requested_voltage_mv=float(view_pos.x()),
            requested_clock_mhz=float(view_pos.y()),
        )
        set_preview(edit, move_target=True, record_undo=True)
        return True

    def event_has_keyboard_modifier(event, modifier_name: str) -> bool:
        modifier_enum = getattr(QtCore.Qt, "KeyboardModifier", QtCore.Qt)
        modifier = getattr(modifier_enum, str(modifier_name), None)
        modifiers_attr = getattr(event, "modifiers", None)
        if modifier is None or not callable(modifiers_attr):
            return False
        return bool(modifiers_attr() & modifier)

    def on_plot_clicked(event) -> None:
        if event_has_keyboard_modifier(event, "ControlModifier"):
            if create_curve_point_at_scene_pos(event.scenePos()) and hasattr(event, "accept"):
                event.accept()
            return
        point = nearest_curve_point_at_scene_pos(event.scenePos())
        if point is None:
            return
        select_curve_point(point[0])
        if hasattr(event, "accept"):
            event.accept()

    def revert_edit() -> None:
        history.clear()
        set_preview(initial_manual_edit(), record_undo=False)
        update_history_buttons()

    key_press_event_type = getattr(
        getattr(QtCore.QEvent, "Type", QtCore.QEvent),
        "KeyPress",
        None,
    )
    key_enum = getattr(QtCore.Qt, "Key", QtCore.Qt)
    modifier_enum = getattr(QtCore.Qt, "KeyboardModifier", QtCore.Qt)
    key_left = getattr(key_enum, "Key_Left", None)
    key_right = getattr(key_enum, "Key_Right", None)
    key_up = getattr(key_enum, "Key_Up", None)
    key_down = getattr(key_enum, "Key_Down", None)
    key_tab = getattr(key_enum, "Key_Tab", None)
    key_backtab = getattr(key_enum, "Key_Backtab", None)
    key_l = getattr(key_enum, "Key_L", None)
    key_z = getattr(key_enum, "Key_Z", None)
    key_y = getattr(key_enum, "Key_Y", None)
    control_modifier = getattr(modifier_enum, "ControlModifier", None)
    shift_modifier = getattr(modifier_enum, "ShiftModifier", None)
    alt_modifier = getattr(modifier_enum, "AltModifier", None)

    def event_has_modifier(event, modifier) -> bool:
        return False if modifier is None else bool(event.modifiers() & modifier)

    def key_event_belongs_to_dialog() -> bool:
        focus_widget = QtWidgets.QApplication.focusWidget()
        if focus_widget is None:
            return bool(dialog.isActiveWindow())
        return bool(focus_widget is dialog or dialog.isAncestorOf(focus_widget))

    class VfCurveEditorKeyFilter(QtCore.QObject):
        def eventFilter(_filter_self, _watched, event) -> bool:
            if key_press_event_type is None or event.type() != key_press_event_type:
                return False
            if not dialog.isVisible() or not key_event_belongs_to_dialog():
                return False
            key = event.key()
            has_ctrl = event_has_modifier(event, control_modifier)
            has_shift = event_has_modifier(event, shift_modifier)
            has_alt = event_has_modifier(event, alt_modifier)
            if has_ctrl and not has_alt and key == key_l:
                lock_selected_curve_point()
            elif has_ctrl and not has_alt and key == key_z:
                history.undo(current_edit["value"])
            elif has_ctrl and not has_alt and key == key_y:
                history.redo(current_edit["value"])
            elif not has_ctrl and has_shift and not has_alt and key == key_right:
                select_range_to_right()
            elif not has_ctrl and not has_shift and not has_alt and key == key_up:
                nudge_selected_frequency(1)
            elif not has_ctrl and not has_shift and not has_alt and key == key_down:
                nudge_selected_frequency(-1)
            elif not has_ctrl and not has_shift and not has_alt and key == key_left:
                nudge_selected_voltage(-1)
            elif not has_ctrl and not has_shift and not has_alt and key == key_right:
                nudge_selected_voltage(1)
            elif not has_ctrl and not has_alt and key == key_tab:
                select_adjacent_curve_point(-1 if has_shift else 1)
            elif not has_ctrl and not has_alt and key == key_backtab:
                select_adjacent_curve_point(-1)
            else:
                return False
            if hasattr(event, "accept"):
                event.accept()
            return True

    def save_edit() -> None:
        try:
            message = save_callback(current_edit["value"])
        except Exception as exc:  # noqa: BLE001
            QtWidgets.QMessageBox.critical(
                dialog,
                "Edit VF Curve",
                f"Failed to save edited curve: {exc}",
            )
            return
        if message:
            status_label.setText(str(message))
        dialog.accept()

    target_item.sigPositionChanged.connect(on_target_position_changed)
    target_item.sigPositionChangeFinished.connect(on_target_position_finished)
    plot.plot.scene().sigMouseClicked.connect(on_plot_clicked)
    refresh_point_handles(current_edit["value"])
    revert_button.clicked.connect(revert_edit)
    undo_button.clicked.connect(lambda: history.undo(current_edit["value"]))
    redo_button.clicked.connect(lambda: history.redo(current_edit["value"]))
    shortcut_context = getattr(
        getattr(QtCore.Qt, "ShortcutContext", QtCore.Qt),
        "WidgetWithChildrenShortcut",
        None,
    )
    shortcuts = []
    for sequence, callback in (
        ("Ctrl+Z", lambda: history.undo(current_edit["value"])),
        ("Ctrl+Y", lambda: history.redo(current_edit["value"])),
        ("Up", lambda: nudge_selected_frequency(1)),
        ("Down", lambda: nudge_selected_frequency(-1)),
        ("Left", lambda: nudge_selected_voltage(-1)),
        ("Right", lambda: nudge_selected_voltage(1)),
        ("Tab", lambda: select_adjacent_curve_point(1)),
        ("Shift+Tab", lambda: select_adjacent_curve_point(-1)),
        ("Ctrl+L", lock_selected_curve_point),
        ("Shift+Right", select_range_to_right),
    ):
        shortcut = QtGui.QShortcut(QtGui.QKeySequence(sequence), dialog)
        if shortcut_context is not None:
            shortcut.setContext(shortcut_context)
        shortcut.activated.connect(callback)
        shortcuts.append(shortcut)
    dialog._curve_editor_shortcuts = shortcuts
    dialog._curve_editor_state = {
        "current_edit": current_edit,
        "point_handle_items": point_handle_items,
        "select_curve_point": select_curve_point,
    }
    save_button.clicked.connect(save_edit)
    cancel_button.clicked.connect(dialog.reject)
    set_preview(current_edit["value"], record_undo=False)
    update_history_buttons()
    app_instance = QtWidgets.QApplication.instance()
    key_filter = VfCurveEditorKeyFilter(dialog)
    if app_instance is not None:
        app_instance.installEventFilter(key_filter)
    dialog._curve_editor_key_filter = key_filter
    try:
        return bool(dialog.exec() == QtWidgets.QDialog.Accepted)
    finally:
        if app_instance is not None:
            app_instance.removeEventFilter(key_filter)


def _style_passive_vf_bin_symbols(pg, item, *, color: str, alpha: int) -> None:
    if pg is None or item is None:
        return
    symbol_color = pg.mkColor(color)
    symbol_color.setAlpha(max(0, min(255, int(alpha))))
    set_symbol = getattr(item, "setSymbol", None)
    if callable(set_symbol):
        set_symbol("o")
    set_symbol_size = getattr(item, "setSymbolSize", None)
    if callable(set_symbol_size):
        set_symbol_size(4)
    set_symbol_brush = getattr(item, "setSymbolBrush", None)
    if callable(set_symbol_brush):
        set_symbol_brush(pg.mkBrush(symbol_color))
    set_symbol_pen = getattr(item, "setSymbolPen", None)
    if callable(set_symbol_pen):
        set_symbol_pen(pg.mkPen(symbol_color, width=1))
