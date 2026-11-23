"""The Profiles tab in the window: the page over the real source, its header
actions (Import Afterburner, Stop verification) and its window status.

Cross-page hooks the window wires as other pages arrive:
  page.verify_requested   -> ProfileVerificationRunner here until the Auto UV
                             page owns verification
  page.set_busy(bool)     <- a scan or verification running elsewhere
  page.setup_requested    -> the Auto UV tab
  page.adaptive_requested -> the Game Library tab (its Adaptive picker)
  page.adaptive_tiers_changed(list) -> Game Library's Adaptive availability
"""

from __future__ import annotations

import sys

from PySide6 import QtCore as C
from PySide6 import QtWidgets as W

from ui.daemon_setup import ensure_daemon_ready_for_privileged_action
from ui.design.style import glyph_icon
from ui.features.profiles.profiles_page import ProfilesPage
from ui.features.profiles.runtime_profiles_source import RuntimeProfilesSource
from ui.features.profiles.verification_runner import ProfileVerificationRunner

AUTO_UV_TAB, PROFILES_TAB, GAME_LIBRARY_TAB = 0, 1, 2


def log_to_stderr(text: str) -> None:
    print(text, end="", file=sys.stderr)


def header_button(text: str, icon: str, tone: str, slot) -> W.QPushButton:
    button = W.QPushButton(text)
    button.setIcon(glyph_icon(icon, tone))
    button.setIconSize(C.QSize(16, 16))
    button.clicked.connect(slot)
    return button


def profiles_status_sources(status: dict) -> dict[str, dict | None]:
    """A running Profiles command is the busy line; otherwise the page's
    idle "what runs now / at startup" line."""
    if status.get("busy"):
        return {"profilesBusy": status}
    return {"profilesBusy": None, "profiles": status or None}


class ProfilesTab:
    """Owns the page, its source and the verification runner for a window
    with ``add_header_action``, ``set_global_status``,
    ``set_header_progress``, ``tabs`` and ``gpu_index``."""

    def __init__(self, window, *, source=None, verifier=None):
        self.window = window
        self.source = source or RuntimeProfilesSource(
            parent=window, ensure_daemon_ready=self.ensure_daemon_ready
        )
        self.page = ProfilesPage(
            self.source,
            ensure_daemon_ready=self.ensure_daemon_ready,
            gpu_index=window.gpu_index,
        )
        self.verifier = verifier or ProfileVerificationRunner(
            self.source.profiles, parent=window
        )
        self.import_button = header_button(
            "Import Afterburner", "import", "secondary", self.import_afterburner
        )
        self.stop_button = header_button(
            "Stop", "stop", "loss", self.verifier.stop
        )
        self.stop_button.setToolTip("Stop profile verification")
        self.stop_button.hide()
        window.add_header_action(self.stop_button)
        window.add_header_action(self.import_button)

        page, verifier = self.page, self.verifier
        page.status_changed.connect(self.show_status)
        page.verify_requested.connect(self.verify)
        page.import_requested.connect(self.import_afterburner)
        page.setup_requested.connect(lambda: window.tabs.setCurrentIndex(AUTO_UV_TAB))
        page.adaptive_requested.connect(
            lambda: window.tabs.setCurrentIndex(GAME_LIBRARY_TAB)
        )
        verifier.progress_changed.connect(
            lambda text, pct, tip: window.set_header_progress(text, pct, tip)
        )
        verifier.status_changed.connect(
            lambda status: window.set_global_status(status, "scan")
        )
        verifier.running_changed.connect(self.verification_running)
        verifier.finished.connect(page.verification_finished)
        self.show_status(page.status())

    def ensure_daemon_ready(self, action_label: str) -> bool:
        return ensure_daemon_ready_for_privileged_action(
            QtWidgets=W,
            parent=self.window,
            log=log_to_stderr,
            action_label=action_label,
        )

    def show_status(self, status: dict) -> None:
        for source, value in profiles_status_sources(dict(status or {})).items():
            self.window.set_global_status(value, source)

    def verify(self, profile_id: str, label: str, row: dict, minutes: int) -> None:
        self.verifier.start(profile_id, label, row, minutes)

    def verification_running(self, running: bool) -> None:
        self.stop_button.setVisible(running)
        self.import_button.setEnabled(not running)
        self.page.set_busy(running)

    def import_afterburner(self) -> None:
        """Import lands on the Profiles tab with the new row selected."""
        self.window.tabs.setCurrentIndex(PROFILES_TAB)
        self.page.run_afterburner_import()

    def refresh(self) -> None:
        """After the root service changed: re-read everything."""
        self.source.refresh()
        self.page.refresh()
