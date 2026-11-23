"""The PenguinBurner window: header, window status, tabs and pages.

Pages land here one at a time as each is wired to its backend owner; a tab
whose page is not wired yet offers the classic window instead of sample data.
Appearance is kept in the runtime config's ``[ui]`` section.
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
from ui.components.page_header import PAGE_MARGINS, PageHeader
from ui.constants import APP_DISPLAY_NAME
from ui.design.style import COL, COPY, GAP, glyph_icon, info_icon, tab_icon
from ui.design.tokens import AppearanceSettings
from ui.dialogs import about
from ui.dialogs.appearance import AppearanceDialog
from ui.features.overlay.overlay_page import OverlayPage
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
CLASSIC_FLAG = "--classic"
LIBRARY_TAB = 2


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


def open_classic_window() -> bool:
    """Start the classic window as its own process; it keeps its own look."""
    return C.QProcess.startDetached(sys.executable, ["-m", "ui", CLASSIC_FLAG])[0]


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


class ClassicPlaceholder(W.QWidget):
    """A tab whose page is not in the new window yet."""

    def __init__(self, title: str, open_classic=open_classic_window, parent=None):
        super().__init__(parent)
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(*PAGE_MARGINS)
        layout.setSpacing(GAP["md"])
        layout.addWidget(PageHeader(title))
        note = W.QLabel(
            f"{title} is not in the new window yet. "
            "Use the classic window for it until it arrives."
        )
        note.setObjectName("pageNote")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.open_button = W.QPushButton("Open classic window")
        self.open_button.clicked.connect(lambda: open_classic())
        row = W.QHBoxLayout()
        row.addWidget(self.open_button)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()


class ShellWindow(W.QWidget):
    def __init__(self, *, gpu_index: int | None = None, open_classic=open_classic_window):
        super().__init__()
        self.gpu_index = gpu_index
        self.open_classic = open_classic
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.resize(1230, 810)
        page = W.QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)
        # Window status sources, highest priority first; pages report here.
        self.status_sources: dict[str, dict | None] = dict.fromkeys(
            ("scan", "profilesBusy", "library", "profiles")
        )

        header = shell_section("shellHeader")
        top = self.header_layout = W.QHBoxLayout(header)
        top.setContentsMargins(GAP["lg"], GAP["md"], GAP["lg"], GAP["sm"])
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
        lines.setContentsMargins(GAP["lg"], 0, GAP["lg"], GAP["sm"] + 2)
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
        self.pages = [self.build_page(index) for index in range(len(PAGES))]
        for widget in self.pages:
            self.stack.addWidget(widget)
        page.addWidget(self.stack, 1)
        self.tabs.currentChanged.connect(self.stack.setCurrentIndex)
        self.show_status()

    def build_page(self, index: int) -> W.QWidget:
        """The page for tab ``index``. Each ported page adds its branch here;
        the rest offer the classic window."""
        if index == LIBRARY_TAB:
            from ui.features.library.library_tab import GameLibraryTab

            self.library_tab = GameLibraryTab()
            self.library_tab.library.status_changed.connect(
                lambda status: self.set_global_status(status, "library")
            )
            return self.library_tab
        if index == OVERLAY_PAGE:
            self.overlay_page = OverlayPage()
            return self.overlay_page
        return ClassicPlaceholder(PAGES[index][0], open_classic=self.open_classic)

    def add_header_action(self, widget: W.QWidget) -> None:
        """A page's header action (Setup, Stop, Import), placed before the
        divider in the order pages add them."""
        layout = self.header_layout
        layout.insertWidget(layout.indexOf(self.header_divider), widget)

    def refresh_pages(self) -> None:
        """Re-read live state after the root service changed (wired pages
        reload here)."""
        self.library_tab.refresh()

    # ---------- window status ----------

    def set_global_status(self, status: dict | None, source: str = "scan") -> None:
        """Two-line window status above the tabs; None clears that source."""
        status = status or {}
        self.status_sources[source] = (
            status if status.get("running") or status.get("detail") else None
        )
        self.show_status()

    def show_status(self) -> None:
        source, status = next(
            ((k, v) for k, v in self.status_sources.items() if v), ("", {})
        )
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

    def apply_appearance(self, settings: AppearanceSettings) -> None:
        try:
            save_appearance(settings)
        except Exception as exc:  # noqa: BLE001 - a corrupt config is refused
            W.QMessageBox.warning(
                self, "Appearance", f"Could not save the appearance setting:\n{exc}"
            )
            return
        rebuild_window(self, settings)


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
    replacement = ShellWindow(gpu_index=window.gpu_index, open_classic=window.open_classic)
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


def follow_desktop_scheme(_scheme=None) -> None:
    """Auto theme: rebuild when the desktop switches light/dark."""
    window = CURRENT["window"]
    if window is None or design.APPEARANCE.theme != "auto":
        return
    if design.resolved_mode(design.APPEARANCE) != design.MODE["current"]:
        rebuild_window(window)


def open_shell_window(app: W.QApplication, *, gpu_index: int | None) -> ShellWindow:
    design.install_theme(app, load_appearance())
    G.QGuiApplication.styleHints().colorSchemeChanged.connect(follow_desktop_scheme)
    window = ShellWindow(gpu_index=gpu_index)
    CURRENT["window"] = window
    # After the window paints, as the classic window does; a rebuild for a
    # new Appearance does not run them again.
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
