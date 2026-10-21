from __future__ import annotations

import logging
import queue
import threading

from integrations.launchers.host_process import start_on_host
from overlay.gnome_shortcuts import GnomeShortcutSetupResult, setup_gnome_shortcuts
from overlay.shortcuts import (
    SHORTCUT_ACTIONS,
    SHORTCUT_DEFAULTS,
    shortcut_command,
    shortcut_desktop,
    shortcut_instructions,
    shortcut_settings_command,
)


def overlay_shortcuts_dialog(*, QtCore, QtWidgets, parent, error=""):
    desktop = shortcut_desktop()
    kde = desktop == "kde"
    gnome = desktop == "gnome"
    dialog = QtWidgets.QDialog(parent)
    dialog.setObjectName("overlayShortcutsDialog")
    dialog.setWindowTitle("Overlay keyboard shortcuts")
    dialog.setMinimumWidth(570)
    dialog.resize(680, 440 if kde or gnome else 640)
    outer_layout = QtWidgets.QVBoxLayout(dialog)
    scroll = QtWidgets.QScrollArea()
    scroll.setObjectName("overlayShortcutScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
    content = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(content)
    layout.setSizeConstraint(QtWidgets.QLayout.SetMinimumSize)
    scroll.setWidget(content)
    outer_layout.addWidget(scroll)
    if error:
        error_label = QtWidgets.QLabel(error)
        error_label.setObjectName("overlayShortcutError")
        error_label.setWordWrap(True)
        layout.addWidget(error_label)
    intro = QtWidgets.QLabel(
        "Alt+X shows or hides the overlay. "
        "Alt+C cycles Small → Medium → Large → Biggest → Small. "
        "Alt+[ / Alt+] changes Scale; Alt+< / Alt+> moves the overlay. "
        "You can change any binding in your desktop settings. "
        "This works on Wayland and X11, even with PenguinBurner closed."
    )
    if gnome:
        intro.setText("Global overlay shortcuts work on Wayland and X11, even with PenguinBurner closed.")
    intro.setWordWrap(True)
    layout.addWidget(intro)
    instructions = QtWidgets.QLabel(shortcut_instructions())
    instructions.setWordWrap(True)
    layout.addWidget(instructions)

    for index, (label, action) in enumerate(SHORTCUT_ACTIONS):
        if action in SHORTCUT_DEFAULTS:
            label = f"{label} (default: {SHORTCUT_DEFAULTS[action]})"
        elif kde:
            label = f"{label} (unassigned)"
        layout.addWidget(QtWidgets.QLabel(label))
        if kde or gnome:
            continue
        row = QtWidgets.QHBoxLayout()
        command = QtWidgets.QLineEdit(shortcut_command(action))
        command.setReadOnly(True)
        command.setObjectName(f"overlayShortcutCommand{index}")
        row.addWidget(command, 1)
        copy = QtWidgets.QPushButton("Copy command")
        copy.setObjectName(f"overlayShortcutCopy{index}")
        copy.clicked.connect(
            lambda _checked=False, field=command: QtWidgets.QApplication.clipboard().setText(
                field.text()
            )
        )
        row.addWidget(copy)
        layout.addLayout(row)

    note = QtWidgets.QLabel(
        "The game must be launched with PenguinBurner's overlay layer. "
        "Changes appear live. Scale stops at 50% and 200%; positions wrap. "
        "Custom fields remain available in the Preset selector. "
        "Opening this dialog does not assign or replace any shortcuts."
    )
    note.setWordWrap(True)
    if gnome:
        note.setText("Setup adds missing actions and opens GNOME Settings.")
        outer_layout.addWidget(note)
    else:
        layout.addWidget(note)
    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
    buttons.rejected.connect(dialog.close)
    settings_command = shortcut_settings_command()
    if settings_command:
        settings = buttons.addButton(
            "Open shortcut settings", QtWidgets.QDialogButtonBox.ActionRole
        )
        settings.setObjectName("overlayShortcutSettings")

        def open_settings():
            if not start_on_host(settings_command):
                note.setText("Could not open shortcut settings. " + shortcut_instructions())

        settings.clicked.connect(open_settings)
    if gnome:
        setup = buttons.addButton(
            "Set up GNOME shortcuts", QtWidgets.QDialogButtonBox.ActionRole
        )
        setup.setObjectName("overlayShortcutSetup")
        results = queue.Queue()
        timer = QtCore.QTimer(dialog)
        timer.setInterval(50)

        def finished_setup():
            try:
                result = results.get_nowait()
            except queue.Empty:
                return
            timer.stop()
            setup.setEnabled(True)
            setup.setText("Set up GNOME shortcuts")
            note.setText(result.message)
            if result.success and settings_command and not start_on_host(settings_command):
                note.setText(result.message + " Could not open GNOME shortcut settings.")

        def run_setup():
            # The worker owns no Qt widgets, so closing the dialog is safe.
            try:
                result = setup_gnome_shortcuts()
            except Exception as exc:
                logging.getLogger(__name__).exception("GNOME shortcut setup failed")
                result = GnomeShortcutSetupResult(
                    success=False, message=f"Could not set up GNOME shortcuts: {exc}"
                )
            results.put(result)

        def begin_setup():
            setup.setEnabled(False)
            setup.setText("Setting up…")
            note.setText("Registering overlay shortcuts in GNOME…")
            threading.Thread(target=run_setup, daemon=True).start()
            timer.start()

        timer.timeout.connect(finished_setup)
        dialog.finished.connect(timer.stop)
        setup.clicked.connect(begin_setup)
    outer_layout.addWidget(buttons)
    dialog.setWindowModality(QtCore.Qt.WindowModal)
    return dialog
