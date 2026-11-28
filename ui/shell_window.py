"""The PenguinBurner window: header, window status, tabs and pages.

Each page reads and writes through its backend owner; the window wires the
pages to each other and keeps Appearance in the runtime config's ``[ui]``
section.
"""

from __future__ import annotations

import sys

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

import ui.design.style as design
from cli.runtime_config_file import (
    ui_strings_from_runtime_config,
    ui_strings_to_runtime_config,
)
from ui.assets import asset_image_path
from ui.components.elided_label import ElidedLabel
from ui.constants import APP_DISPLAY_NAME
from ui.design.style import COL, COPY, GAP, glyph_icon, info_icon, tab_icon
from ui.design.tokens import AppearanceSettings
from ui.dialogs import about
from ui.dialogs.adaptive_game_picker import AdaptiveGame, AdaptiveGamePicker
from ui.dialogs.appearance import AppearanceDialog
from ui.features.overlay.overlay_page import OverlayPage
from ui.features.profiles.profiles_shell import PROFILES_TAB, ProfilesTab
from ui.features.tuning.auto_uv_page import AutoUvPage
from ui.startup_checks import run_startup_checks

# (tab label, painted tab glyph)
PAGES = (
    ("Auto UV", "auto-uv"),
    ("Profiles", "profiles"),
    ("Game Library", "game-library"),
    ("In-Game Overlay", "overlay"),
)
OVERLAY_PAGE = 3
# AppearanceSettings field -> its [ui] key.
APPEARANCE_KEYS = {
    "theme": "appearance_theme",
    "accent": "appearance_accent",
    "font": "appearance_font",
    "font_family": "appearance_font_family",
}
LIBRARY_TAB = 2


def scan_profile_id(profiles: list[dict], candidate_id) -> str:
    """The saved profile a scan candidate became ("" when none matches)."""
    candidate_id = str(candidate_id or "").strip()
    if not candidate_id:
        return ""
    return next(
        (
            str(p.get("profile_id") or "")
            for p in profiles
            if str(p.get("candidate_id") or "") == candidate_id
        ),
        "",
    )


def load_appearance() -> AppearanceSettings:
    stored = ui_strings_from_runtime_config(APPEARANCE_KEYS.values())
    values = {
        field: stored[key] for field, key in APPEARANCE_KEYS.items() if key in stored
    }
    return AppearanceSettings(**values).normalized()


def save_appearance(settings: AppearanceSettings) -> None:
    ui_strings_to_runtime_config(
        {key: getattr(settings, field) for field, key in APPEARANCE_KEYS.items()}
    )


def label(text: str, role: str) -> W.QLabel:
    widget = W.QLabel(text)
    widget.setObjectName(role)
    widget.setWordWrap(False)
    return widget


def shell_section(name: str) -> W.QWidget:
    section = W.QWidget()
    section.setObjectName(name)
    section.setAttribute(C.Qt.WidgetAttribute.WA_StyledBackground, True)
    return section


def header_icon_button(icon: G.QIcon, name: str, slot) -> W.QToolButton:
    """A flat, icon-only header utility (Appearance, About): same size and
    look for both, with the name as tooltip and accessible name."""
    tool = W.QToolButton()
    tool.setObjectName("headerUtility")
    tool.setIcon(icon)
    tool.setIconSize(C.QSize(18, 18))
    tool.setFixedSize(34, 34)
    tool.setToolTip(name)
    tool.setAccessibleName(name)
    tool.clicked.connect(slot)
    return tool


class ShellWindow(W.QWidget):
    def __init__(self, *, gpu_index: int | None = None):
        super().__init__()
        self.gpu_index = gpu_index
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.resize(1230, 810)
        page = W.QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)
        # Window status sources, highest priority first; pages report here.
        self.status_sources: dict[str, dict | None] = dict.fromkeys(
            ("scan", "profilesBusy", "appearance", "library", "profiles")
        )

        header = shell_section("shellHeader")
        top = self.header_layout = W.QHBoxLayout(header)
        top.setContentsMargins(GAP["lg"], GAP["sm"], GAP["lg"], GAP["xs"])
        top.setSpacing(GAP["sm"])
        logo = W.QLabel()
        logo.setObjectName("brandLogo")
        logo.setPixmap(
            G.QIcon(str(asset_image_path("penguin-burner.png"))).pixmap(30, 30)
        )
        top.addWidget(logo)
        top.addWidget(label(APP_DISPLAY_NAME, "brand"))
        top.addStretch()
        self.header_progress = W.QProgressBar()
        self.header_progress.setObjectName("headerProgress")
        self.header_progress.setRange(0, 100)
        self.header_progress.setFixedWidth(260)
        self.header_progress.hide()
        top.addWidget(self.header_progress)
        # Page actions are inserted before this divider as pages arrive.
        divider = W.QFrame()
        divider.setObjectName("headerDivider")
        divider.setFixedSize(1, 22)
        top.addWidget(divider)
        self.appearance_button = header_icon_button(
            glyph_icon("appearance", "secondary", 18), "Appearance", self.show_appearance
        )
        self.about_button = header_icon_button(
            info_icon(COL["secondary"]), "About PenguinBurner", self.show_about
        )
        top.addWidget(self.appearance_button)
        top.addWidget(self.about_button)
        page.addWidget(header)

        band = self.status_band = shell_section("statusBand")
        lines = W.QVBoxLayout(band)
        lines.setContentsMargins(GAP["lg"], 0, GAP["lg"], GAP["xs"])
        lines.setSpacing(0)
        self.status_running = ElidedLabel("statusRunning")
        self.status_detail = ElidedLabel("statusDetail", copyable=True)
        for line in (self.status_running, self.status_detail):
            lines.addWidget(line)
        page.addWidget(band)

        strip = W.QFrame()
        strip.setObjectName("tabStrip")
        strip_layout = W.QHBoxLayout(strip)
        strip_layout.setContentsMargins(GAP["sm"], 0, GAP["sm"], 0)
        self.tabs = W.QTabBar()
        self.tabs.setDrawBase(False)
        self.tabs.setDocumentMode(True)
        self.tabs.setIconSize(C.QSize(20, 20))
        self.tabs.setExpanding(False)
        for text, icon in PAGES:
            self.tabs.addTab(tab_icon(icon), text)
        strip_layout.addWidget(self.tabs)
        strip_layout.addStretch()
        page.addWidget(strip)

        self.stack = W.QStackedWidget()
        self.header_divider = divider
        # Wired pages re-read live state here after the root service changed.
        self.page_refreshers: list = []
        # Pages report whether a command, verification or scan is running;
        # the window is never rebuilt under one.
        self.busy_checks: list = []
        self.pages = [self.build_page(index) for index in range(len(PAGES))]
        for widget in self.pages:
            self.stack.addWidget(widget)
        page.addWidget(self.stack, 1)
        self.tabs.currentChanged.connect(self.stack.setCurrentIndex)
        self.tabs.currentChanged.connect(lambda _index: self.show_status())
        self.connect_pages()
        self.show_status()

    def build_page(self, index: int) -> W.QWidget:
        """The page for tab ``index``."""
        if index == LIBRARY_TAB:
            from ui.features.library.library_tab import GameLibraryTab

            self.library_tab = GameLibraryTab()
            self.page_refreshers.append(lambda: self.library_tab.refresh())
            self.library_tab.library.status_changed.connect(
                lambda status: self.set_global_status(status, "library")
            )
            return self.library_tab
        if index == OVERLAY_PAGE:
            self.overlay_page = OverlayPage()
            return self.overlay_page
        if index == PROFILES_TAB:
            self.profiles_tab = ProfilesTab(self)
            self.page_refreshers.append(self.profiles_tab.refresh)
            self.busy_checks.append(lambda: self.profiles_tab.page.busy())
            return self.profiles_tab.page
        if index == 0:
            self.auto_uv_page = AutoUvPage(gpu_index=self.gpu_index)
            self.auto_uv_page.attach_to_shell(self)
            self.page_refreshers.append(self.auto_uv_page.refresh_live_state)
            # A final-candidate choice is a modal step a rebuild would tear down.
            self.busy_checks.append(self.auto_uv_page.blocks_rebuild)
            return self.auto_uv_page
        raise ValueError(f"no page for tab {index}")

    def connect_pages(self) -> None:
        """Cross-page wiring, for the pages that are built."""
        library = getattr(self, "library_tab", None)
        overlay = getattr(self, "overlay_page", None)
        profiles = getattr(self, "profiles_tab", None)
        if library is not None and overlay is not None:
            # The overlay settings show the game picked in Game Library.
            library.library.selected_game_changed.connect(
                lambda game: overlay.panel.set_game(game)
            )
        if overlay is not None and profiles is not None:
            # The HUD preview's tier row shows what runs now.
            def show_running_tier(*_args) -> None:
                overlay.panel.set_running_tier(profiles.page.running_tier())

            profiles.page.status_changed.connect(show_running_tier)
            self.tabs.currentChanged.connect(
                lambda index: index == OVERLAY_PAGE and show_running_tier()
            )
            show_running_tier()
        if library is not None and profiles is not None:
            # Adaptive per game needs a tier profile on that game's GPU.
            profiles.page.adaptive_tiers_changed.connect(
                lambda _tiers: library.library.set_profile_summaries(None)
            )
            profiles.page.adaptive_requested.connect(self.pick_adaptive_game)
        scan = getattr(self, "auto_uv_page", None)
        if scan is not None and profiles is not None:
            page = profiles.page
            # Profiles → Verify runs on the Auto UV page, like a scan.
            page.verify_requested.connect(self.verify_profile)
            scan.profile_verified.connect(page.verification_finished)
            # A scan or verification locks Apply, Delete, Restore and Verify;
            # a running Profiles command locks Setup.
            scan.busy_changed.connect(page.set_busy)
            # One header Import Afterburner; it lands on Profiles.
            scan.import_requested.connect(profiles.import_afterburner)
            page.status_changed.connect(
                lambda status: scan.set_other_busy(bool((status or {}).get("busy")))
            )
            # The scan keeps the Silent fan curve choice Profiles shows (the
            # saved choice, a running or a boot profile's curve).
            scan.silent_fan_checked = page.silent_fan_enabled
            scan.profile_saved.connect(self.show_scan_profile)
            scan.scan_finished.connect(lambda _summary: self.profiles_changed())
            scan.scan_completed.connect(self.apply_scan_profile)
            page.setup_requested.connect(self.setup_from_profiles)

    # ---------- cross-page actions ----------

    def profiles_changed(self) -> None:
        """A scan saved or verified a profile: Profiles and Game Library's
        Adaptive gate re-read the saved profiles."""
        profiles = getattr(self, "profiles_tab", None)
        if profiles is not None:
            profiles.source.refresh()
            profiles.page.reload()
        library = getattr(self, "library_tab", None)
        if library is not None:
            library.library.set_profile_summaries(None)

    def show_scan_profile(self, result: dict) -> None:
        """A tier's verified profile was saved: show it, on the scanned GPU."""
        page = self.profiles_tab.page
        self.profiles_changed()
        gpu_index = result.get("gpu_index")
        if gpu_index is not None:
            page.set_target_gpu(int(gpu_index))
        profile_id = scan_profile_id(page.profiles, result.get("candidate_id"))
        if profile_id:
            page.show_new_profile(profile_id)

    def apply_scan_profile(self, summary: dict) -> None:
        """A completed scan applies its profile through the daemon once the
        scan process has fully released."""

        def apply() -> None:
            page = self.profiles_tab.page
            self.profiles_changed()
            gpu_index = summary.get("gpu_index")
            if gpu_index is not None:
                page.set_target_gpu(int(gpu_index))
            profile_id = scan_profile_id(page.profiles, summary.get("candidate_id"))
            if profile_id:
                page.show_new_profile(profile_id)
            else:
                profile_id = page.lead_id()
            if not profile_id:
                return
            # A scan must never turn off a fan curve the user had running.
            if summary.get("silent_fan") and not page.silent_fan_enabled():
                page.set_silent_fan(True)
            page.apply_profile(profile_id)

        C.QTimer.singleShot(0, self, apply)

    def setup_from_profiles(self) -> None:
        """Profiles' empty state: Setup Auto Undervolt on the Auto UV tab."""
        scan = self.auto_uv_page
        if scan.setup_button.isEnabled():
            C.QTimer.singleShot(0, scan, scan.setup)

    def verify_profile(self, profile_id: str, label: str, row: dict, minutes: int) -> None:
        profile = self.profiles_tab.page.profile(profile_id)
        if profile is None:
            return
        started = self.auto_uv_page.start_profile_verification(
            profile,
            label=label,
            duration_s=max(1, int(minutes)) * 60,
            gpu_index=row.get("gpu_index"),
        )
        if not started:
            # Another run holds the GPU: nothing ran, the profile is unchanged.
            self.profiles_tab.page.verification_finished(profile_id, False, stopped=True)

    def pick_adaptive_game(self) -> None:
        """Profiles → Use Adaptive for a game: the game and FPS picker, then
        Game Library with that game wrapped on Adaptive. The picker lists
        games once the launchers have been read."""
        self.library_tab.library.when_games_listed(self.open_adaptive_picker)

    def open_adaptive_picker(self) -> None:
        library = self.library_tab
        selected, eligible = library.library.adaptive_picker_state()
        games = [AdaptiveGame(key, name, int(fps)) for key, name, fps in eligible]
        picker = AdaptiveGamePicker(games, selected, self)
        if picker.exec() != W.QDialog.DialogCode.Accepted:
            return
        key, fps = picker.choice()
        self.tabs.setCurrentIndex(LIBRARY_TAB)
        library.back_to_library()
        library.library.apply_adaptive(key, fps)

    def add_header_action(self, widget: W.QWidget) -> None:
        """A page's header action (Setup, Stop, Import), placed before the
        divider in the order pages add them."""
        layout = self.header_layout
        layout.insertWidget(layout.indexOf(self.header_divider), widget)

    def refresh_pages(self) -> None:
        """Re-read live state after the root service changed (wired pages
        reload here)."""
        for refresh in self.page_refreshers:
            refresh()

    # ---------- window status ----------

    def set_global_status(self, status: dict | None, source: str = "scan") -> None:
        """Two-line window status above the tabs; None clears that source."""
        status = status or {}
        self.status_sources[source] = (
            status if status.get("running") or status.get("detail") else None
        )
        self.show_status()

    def show_status(self) -> None:
        # On the Auto UV tab its own card shows the scan, right over the plot.
        on_scan_page = self.tabs.currentIndex() == 0
        source, status = next(
            (
                (k, v)
                for k, v in self.status_sources.items()
                if v and not (k == "scan" and on_scan_page)
            ),
            ("", {}),
        )
        if on_scan_page and self.status_sources.get("scan"):
            # A scan runs: the profile lines would describe a suspended GPU.
            self.status_band.setVisible(False)
            return
        running, detail = status.get("running"), status.get("detail")
        # Nothing reports until a page is wired; an empty band is hidden.
        self.status_band.setVisible(bool(running or detail))
        tone = status.get("tone") or ""
        prefix = ""
        if source in ("scan", "profilesBusy", "library") or tone:
            dot = {
                "error": COL["loss"],
                "loss": COL["loss"],
                "warn": COL["warning"],
                "warning": COL["warning"],
            }.get(tone, COL["accent"])
            prefix = f'<span style="color:{dot}">●</span>&nbsp;'
        self.status_running.setVisible(bool(running))
        self.status_detail.setVisible(bool(detail))
        self.status_running.set_full_text(running or "", prefix)
        self.status_detail.set_full_text(detail or "")
        self.status_band.setToolTip("\n".join(filter(None, (running, detail))))

    def set_header_progress(self, text: str | None, pct: int = 0, tooltip: str = ""):
        """Header progress bar, e.g. "Downloading dependencies"; None hides it."""
        if text is None:
            self.header_progress.hide()
            return
        self.header_progress.setValue(max(0, min(100, int(pct))))
        self.header_progress.setFormat(str(text))
        self.header_progress.setToolTip(tooltip or text)
        self.header_progress.show()

    # ---------- header utilities ----------

    def show_about(self) -> None:
        about.show_about_dialog(
            QtCore=C,
            QtGui=G,
            QtWidgets=W,
            parent=self,
            purpose_text=COPY["aboutPurpose"],
        )

    def show_appearance(self) -> None:
        dialog = AppearanceDialog(design.APPEARANCE, self)
        dialog.applied.connect(self.apply_appearance)
        dialog.open()
        self.appearance_dialog = dialog

    def is_busy(self) -> bool:
        return any(check() for check in self.busy_checks)

    def apply_appearance(self, settings: AppearanceSettings) -> None:
        try:
            save_appearance(settings)
        except Exception as exc:  # noqa: BLE001 - a corrupt config is refused
            W.QMessageBox.warning(
                self, "Appearance", f"Could not save the appearance setting:\n{exc}"
            )
            return
        if self.is_busy():
            self.set_global_status(
                {
                    "running": "Appearance saved",
                    "detail": "It applies when the current action finishes.",
                },
                "appearance",
            )
        rebuild_when_idle(self, settings)


# The live window, so Apply and a desktop colour-scheme change can rebuild it.
CURRENT: dict[str, ShellWindow | None] = {"window": None}


def rebuild_window(
    window: ShellWindow, settings: AppearanceSettings | None = None
) -> ShellWindow:
    """Restyle the app and rebuild the window on the same tab and geometry.

    Pages set their own style sheets, chart pens and painted icons when they
    are built, so a rebuild is what puts every one in the new theme; pages
    read live state, so nothing is lost.
    """
    design.install_theme(W.QApplication.instance(), settings)
    replacement = ShellWindow(gpu_index=window.gpu_index)
    replacement.setWindowIcon(window.windowIcon())
    replacement.setGeometry(window.geometry())
    replacement.tabs.setCurrentIndex(window.tabs.currentIndex())
    if window.isVisible():
        # Show the new window before closing the old one, so closing the
        # last window never quits the app.
        replacement.show()
        window.close()
    window.deleteLater()
    CURRENT["window"] = replacement
    return replacement


def rebuild_when_idle(
    window: ShellWindow, settings: AppearanceSettings | None = None
) -> None:
    """Rebuild now, or once nothing runs: a rebuild deletes the pages and
    with them a running Apply, verification or scan."""
    if window.is_busy():
        C.QTimer.singleShot(1000, window, lambda: rebuild_when_idle(window, settings))
        return
    rebuild_window(window, settings)


def follow_desktop_scheme(_scheme=None) -> None:
    """Auto theme: rebuild when the desktop switches light/dark."""
    window = CURRENT["window"]
    if window is None or design.APPEARANCE.theme != "auto":
        return
    if design.resolved_mode(design.APPEARANCE) != design.MODE["current"]:
        rebuild_when_idle(window)


def open_shell_window(app: W.QApplication, *, gpu_index: int | None) -> ShellWindow:
    design.install_theme(app, load_appearance())
    G.QGuiApplication.styleHints().colorSchemeChanged.connect(follow_desktop_scheme)
    window = ShellWindow(gpu_index=gpu_index)
    CURRENT["window"] = window
    # Startup checks run after the window paints; a rebuild for a new
    # Appearance does not run them again.
    C.QTimer.singleShot(0, lambda: startup_checks(CURRENT["window"]))
    return window


def startup_checks(window: ShellWindow | None) -> None:
    if window is None:
        return
    run_startup_checks(
        QtWidgets=W,
        parent=window,
        log=lambda text: print(text, end="", file=sys.stderr),
        on_daemon_updated=window.refresh_pages,
    )
