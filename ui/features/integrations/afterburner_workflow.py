from __future__ import annotations

from pathlib import Path

from integrations.afterburner.profile_import import (
    persist_afterburner_import_selection,
)
from ui.dialogs.afterburner_import import select_afterburner_import


class AfterburnerImportWorkflow:
    def __init__(
        self,
        *,
        QtCore,
        QtGui,
        QtWidgets,
        pg,
        parent,
        tabs,
        profiles_tab_index: int,
        profile_list,
        controls,
        log_view,
        workflow_running,
        load_profiles,
        show_error,
    ):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self.pg = pg
        self.parent = parent
        self.tabs = tabs
        self.profiles_tab_index = int(profiles_tab_index)
        self.profile_list = profile_list
        self.controls = controls
        self.log_view = log_view
        self.workflow_running = workflow_running
        self.load_profiles = load_profiles
        self.show_error = show_error

    def run(self) -> None:
        if self.workflow_running():
            self.controls.set_status_text(
                "Finish the current PenguinBurner action before importing Afterburner."
            )
            return
        try:
            entry = select_afterburner_import(
                QtCore=self.QtCore,
                QtGui=self.QtGui,
                QtWidgets=self.QtWidgets,
                pg=self.pg,
                parent=self.parent,
            )
        except Exception as exc:  # noqa: BLE001
            self.show_error("Import Afterburner", f"Afterburner import failed:\n{exc}")
            return
        if entry is None:
            return
        try:
            result = persist_afterburner_import_selection(entry)
        except Exception as exc:  # noqa: BLE001
            self.show_error("Import Afterburner", f"Afterburner import failed:\n{exc}")
            return
        # Imported Afterburner state is represented as one selectable profile row.
        message = (
            "Imported Afterburner profile "
            f"{result['section']} from {Path(result['device_profile_relative_path']).name}."
        )
        self.controls.set_status_text(message)
        self.log_view.append(
            "\n"
            + message
            + f"\nSaved profile: {result['profile_path']}\n"
        )
        self.load_profiles()
        self.tabs.setCurrentIndex(self.profiles_tab_index)
        self.profile_list.select_profile(str(result.get("profile_id", "")))
        self.profile_list.table.setFocus()
        self.QtWidgets.QMessageBox.information(self.parent, "Import Afterburner", message)
