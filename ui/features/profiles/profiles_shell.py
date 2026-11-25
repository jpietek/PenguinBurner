"""The Profiles tab in the window: the page over the real source, its
Afterburner import flow and its window status.

Cross-page hooks the window wires (ui/shell_window.py):
  import_afterburner()    <- the header's Import Afterburner (Auto UV page)
  page.verify_requested   -> the Auto UV page runs the verification
  page.set_busy(bool)     <- a scan or verification running there
  page.setup_requested    -> the Auto UV tab
  page.adaptive_requested -> the Adaptive game picker, then Game Library
  page.adaptive_tiers_changed(list) -> Game Library's Adaptive availability
"""

from __future__ import annotations

import sys

from PySide6 import QtWidgets as W

from ui.daemon_setup import ensure_daemon_ready_for_privileged_action
from ui.features.profiles.profiles_page import ProfilesPage
from ui.features.profiles.runtime_profiles_source import RuntimeProfilesSource

AUTO_UV_TAB, PROFILES_TAB = 0, 1


def log_to_stderr(text: str) -> None:
    print(text, end="", file=sys.stderr)


def profiles_status_sources(status: dict) -> dict[str, dict | None]:
    """A running Profiles command is the busy line; otherwise the page's
    idle "what runs now / at startup" line."""
    if status.get("busy"):
        return {"profilesBusy": status}
    return {"profilesBusy": None, "profiles": status or None}


class ProfilesTab:
    """Owns the page and its source for a window with ``set_global_status``,
    ``tabs`` and ``gpu_index``."""

    def __init__(self, window, *, source=None):
        self.window = window
        self.source = source or RuntimeProfilesSource(
            parent=window, ensure_daemon_ready=self.ensure_daemon_ready
        )
        self.page = ProfilesPage(
            self.source,
            ensure_daemon_ready=self.ensure_daemon_ready,
            gpu_index=window.gpu_index,
        )
        page = self.page
        page.status_changed.connect(self.show_status)
        page.import_requested.connect(self.import_afterburner)
        page.setup_requested.connect(lambda: window.tabs.setCurrentIndex(AUTO_UV_TAB))
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

    def import_afterburner(self) -> None:
        """Import lands on the Profiles tab with the new row selected."""
        self.window.tabs.setCurrentIndex(PROFILES_TAB)
        self.page.run_afterburner_import()

    def refresh(self) -> None:
        """After the root service changed: re-read everything."""
        self.source.refresh()
        self.page.refresh()
