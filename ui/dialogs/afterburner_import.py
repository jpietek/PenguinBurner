from __future__ import annotations

from pathlib import Path

from integrations.afterburner.profile_import import (
    afterburner_profile_entries,
    configured_afterburner_root,
    entry_curve_points,
)

from ..components.curve_plot import CurvePlot
from ..components.table_sizing import set_header_fit_column_widths


def select_afterburner_import(
    *,
    QtCore,
    QtGui,
    QtWidgets,
    pg,
    parent,
) -> dict | None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Import Afterburner")
    dialog.resize(1040, 560)
    layout = QtWidgets.QVBoxLayout(dialog)

    directory_row = QtWidgets.QHBoxLayout()
    directory_label = QtWidgets.QLabel("Afterburner directory")
    directory_edit = QtWidgets.QLineEdit(configured_afterburner_root())
    browse_button = QtWidgets.QToolButton()
    standard_pixmap = getattr(QtWidgets.QStyle, "StandardPixmap", QtWidgets.QStyle)
    browse_button.setIcon(
        dialog.style().standardIcon(standard_pixmap.SP_DirOpenIcon)
    )
    browse_button.setToolTip("Choose Afterburner Directory")
    browse_button.setAccessibleName("Choose Afterburner Directory")
    directory_row.addWidget(directory_label)
    directory_row.addWidget(directory_edit, 1)
    directory_row.addWidget(browse_button)

    table = QtWidgets.QTableWidget(0, 4)
    table.setHorizontalHeaderLabels(
        ["Device Profile", "Afterburner Profile", "Target", "Status"]
    )
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
    table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    table.setSortingEnabled(False)
    set_header_fit_column_widths(
        table,
        {
            0: 260,
            1: 150,
            2: 145,
            3: 220,
        },
        QtCore=QtCore,
        padding=32,
    )
    table.horizontalHeader().setStretchLastSection(True)

    preview_plot = CurvePlot(
        QtWidgets=QtWidgets,
        pg=pg,
        x_label="Voltage",
        x_units="mV",
        y_label="Clock",
        y_units="MHz",
        source_name="Base",
        candidate_name="Imported",
        show_source=False,
    )
    preview_plot.enable_point_selection(True)

    splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
    splitter.addWidget(table)
    splitter.addWidget(preview_plot.widget)
    splitter.setSizes([470, 570])

    status_label = QtWidgets.QLabel("")
    status_label.setWordWrap(True)
    buttons = QtWidgets.QDialogButtonBox()
    import_button = buttons.addButton("Import", QtWidgets.QDialogButtonBox.AcceptRole)
    buttons.addButton(QtWidgets.QDialogButtonBox.Cancel)
    import_button.setEnabled(False)

    layout.addLayout(directory_row)
    layout.addWidget(splitter, 1)
    layout.addWidget(status_label)
    layout.addWidget(buttons)

    entries: list[dict] = []
    chosen: dict[str, dict | None] = {"entry": None}
    role = 257

    def selected_entry() -> dict | None:
        rows = table.selectionModel().selectedRows()
        if not rows:
            return None
        item = table.item(int(rows[-1].row()), 0)
        if item is None:
            return None
        try:
            index = int(item.data(role))
        except (TypeError, ValueError):
            return None
        return entries[index] if 0 <= index < len(entries) else None

    def sync_selection_state() -> None:
        entry = selected_entry()
        importable = bool(entry and entry.get("importable"))
        import_button.setEnabled(importable)
        preview_plot.clear()
        if entry:
            points = entry_curve_points(entry)
            preview_plot.set_candidate_points(points, remember_previous=False)
            if entry.get("target_voltage_mv") and entry.get("target_clock_mhz"):
                preview_plot.set_selected_point(
                    entry.get("target_voltage_mv"),
                    entry.get("target_clock_mhz"),
                )
        status_label.setText(str(entry.get("status", "")) if entry and not importable else "")

    def add_cell(row: int, column: int, text: str, entry_index: int) -> None:
        item = QtWidgets.QTableWidgetItem(str(text))
        # Store the source entry index once so every cell can recover the row payload.
        item.setData(role, int(entry_index))
        if not entries[entry_index].get("importable"):
            item.setForeground(QtGui.QColor("#7f8794"))
        table.setItem(row, column, item)

    def populate_profiles() -> None:
        root_text = str(directory_edit.text()).strip()
        entries.clear()
        table.setRowCount(0)
        import_button.setEnabled(False)
        chosen["entry"] = None
        if not root_text:
            status_label.setText("Choose an MSI Afterburner directory.")
            return
        try:
            entries.extend(afterburner_profile_entries(root_text))
        except Exception as exc:  # noqa: BLE001
            status_label.setText(str(exc))
            return
        if not entries:
            status_label.setText(
                "No saved Afterburner V/F profiles were found in that directory."
            )
            return
        for entry_index, entry in enumerate(entries):
            row = table.rowCount()
            table.insertRow(row)
            add_cell(row, 0, entry["device_profile_name"], entry_index)
            add_cell(row, 1, entry["section"], entry_index)
            add_cell(row, 2, entry["target"], entry_index)
            add_cell(row, 3, entry["status"], entry_index)
            if entry.get("importable"):
                for column in range(table.columnCount()):
                    font = table.item(row, column).font()
                    font.setBold(True)
                    table.item(row, column).setFont(font)
        first_importable_row = next(
            (row for row, entry in enumerate(entries) if bool(entry.get("importable"))),
            None,
        )
        if first_importable_row is not None:
            table.selectRow(first_importable_row)
            status_label.setText(
                "Select one Afterburner profile to import into PenguinBurner."
            )
        else:
            status_label.setText(
                "Afterburner profiles were found, but none are importable."
            )
        sync_selection_state()

    def browse_directory() -> None:
        selected = QtWidgets.QFileDialog.getExistingDirectory(
            dialog,
            "Choose Afterburner Directory",
            str(directory_edit.text()).strip() or str(Path.home()),
        )
        if selected:
            directory_edit.setText(selected)
            populate_profiles()

    def accept_import() -> None:
        entry = selected_entry()
        if not entry or not entry.get("importable"):
            status_label.setText("Select one importable Afterburner profile.")
            return
        chosen["entry"] = dict(entry)
        dialog.accept()

    browse_button.clicked.connect(browse_directory)
    directory_edit.editingFinished.connect(populate_profiles)
    table.itemSelectionChanged.connect(sync_selection_state)
    buttons.accepted.connect(accept_import)
    buttons.rejected.connect(dialog.reject)
    populate_profiles()

    if dialog.exec() != QtWidgets.QDialog.Accepted:
        return None
    entry = chosen.get("entry")
    return dict(entry) if isinstance(entry, dict) else None
