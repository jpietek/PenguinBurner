"""Prototype controls: simulate states the sample data cannot reach.

A mock-only harness (it has no place in the real window): the footer's
**Prototype controls** opens ``PrototypeControlsDialog`` over one
``PrototypeState``. Each change emits ``changed(key, value)``; the host
forwards it to the pages that simulate it (Auto UV, Profiles, Game Library,
In-Game Overlay). One-shot flows run through ``action_requested(name)``.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from ui.components.preference_rows import preference_row
from ui.components.toggle_switch import make_toggle_switch
from ui.design.style import GAP

DAEMON_STATES = (
    ("ready", "Ready"),
    ("missing", "Not installed"),
    ("outdated", "Outdated"),
)
LAUNCHER_STATES = (
    ("ready", "Ready"),
    ("not-set-up", "Steam not set up"),
    ("needs-restart", "Steam needs restart"),
)
OVERLAY_DESKTOPS = (
    ("kde", "KDE Plasma"),
    ("kde-fail", "KDE Plasma, settings fail to open"),
    ("gnome", "GNOME"),
    ("other", "Other desktop"),
    ("other-flatpak", "Other desktop, Flatpak install"),
)
LIBRARY_SCANS = (
    ("normal", "Every launcher reads"),
    ("heroic-error", "Heroic read fails"),
    ("no-launchers", "No launcher installed"),
)
LIBRARY_WRITES = (
    ("ok", "Saves work"),
    ("save-fail", "Saves fail"),
    ("live-fail", "Saved; live update fails"),
)
NEXT_LAUNCHES = (
    ("normal", "Starts normally"),
    ("fails", "Launch command fails"),
    ("unconfirmed", "Launch is not confirmed"),
    ("profile", "GPU profile unconfirmed"),
    ("lost", "Session connection lost"),
)
# Right-column controls share one width so their left edges line up.
CONTROL_WIDTH = 220


@dataclass
class PrototypeState:
    """Every simulation switch; keys match ScanPage.prototype and the
    overlay panel's set_prototype."""

    daemon: str = "ready"
    launcherWriteState: str = "ready"
    libraryScan: str = "normal"
    libraryWrite: str = "ok"
    nextLaunch: str = "normal"
    overlayDesktop: str = "kde"
    twoGpus: bool = False
    noGpu: bool = False
    emptyLibrary: bool = False
    dependencyDownload: bool = False
    crashRecovery: bool = False
    finalVerificationFailure: bool = False
    processFailure: bool = False
    performanceComparison: bool = False
    overlaySaveFailure: bool = False
    livePreviewMissing: bool = False

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


CHOICES = (
    (
        "daemon",
        "Root service",
        "Setup, Apply and Verify ask to install or update it.",
        DAEMON_STATES,
    ),
    (
        "launcherWriteState",
        "Steam setup",
        "Game Library gates Steam settings until this is ready.",
        LAUNCHER_STATES,
    ),
    (
        "libraryScan",
        "Library scan",
        "What the launchers report on the next scan.",
        LIBRARY_SCANS,
    ),
    (
        "libraryWrite",
        "Library saves",
        "How per-game setting writes end.",
        LIBRARY_WRITES,
    ),
    (
        "nextLaunch",
        "Next launch",
        "How the next Play in Game Library ends.",
        NEXT_LAUNCHES,
    ),
    (
        "overlayDesktop",
        "Desktop",
        "Keyboard shortcuts follows this desktop.",
        OVERLAY_DESKTOPS,
    ),
)
TOGGLES = (
    (
        "twoGpus",
        "Two graphics cards",
        "Adds an RTX 4070 to setup, Profiles and Game Library.",
    ),
    (
        "noGpu",
        "No graphics card",
        "Startup warns; setup, Profiles and Game Library have no GPU.",
    ),
    ("emptyLibrary", "Empty game library", "No launcher reports a game."),
    (
        "dependencyDownload",
        "Download dependencies on next scan",
        "Header progress while Q2RTX downloads.",
    ),
    (
        "crashRecovery",
        "Recover from a crash on next scan",
        "The next scan opens the crash recovery choice.",
    ),
    (
        "finalVerificationFailure",
        "Fail final verification",
        "The next final verification ends early.",
    ),
    (
        "processFailure",
        "Fail the next scan, verification or Profiles command",
        "Shows the copyable error report.",
    ),
    (
        "performanceComparison",
        "Compare close Performance candidates",
        "The next Performance choice compares its two best twice.",
    ),
    (
        "overlaySaveFailure",
        "Fail overlay saves",
        "overlay.toml writes report an error.",
    ),
    (
        "livePreviewMissing",
        "vkcube missing",
        "Live preview explains why it cannot start.",
    ),
)
ACTIONS = (
    ("startupChecks", "Run startup checks"),
    ("errorReport", "Show an error report"),
    ("libraryFirstScan", "Restart library (first scan)"),
    ("externalGame", "Start game outside PenguinBurner"),
    ("overlayShortcut", "Press Alt+C in game"),
)


class PrototypeControlsDialog(W.QDialog):
    """Non-modal panel over a shared ``PrototypeState``."""

    changed = C.Signal(str, object)
    action_requested = C.Signal(str)

    def __init__(self, state: PrototypeState, parent=None):
        super().__init__(parent)
        self.setObjectName("prototypeControls")
        self.setWindowTitle("Prototype controls")
        self.state = state
        self.combos: dict[str, W.QComboBox] = {}
        self.switches: dict[str, W.QAbstractButton] = {}
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(GAP["xl"], GAP["lg"], GAP["xl"], GAP["lg"])
        layout.setSpacing(GAP["sm"])
        title = W.QLabel("Prototype controls")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        note = W.QLabel("Simulate states the sample data cannot reach on its own.")
        note.setObjectName("caption")
        layout.addWidget(note)
        layout.addSpacing(GAP["sm"])

        card = W.QFrame()
        card.setObjectName("prefGroupCard")
        rows = W.QVBoxLayout(card)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(0)
        for key, text, subtitle, options in CHOICES:
            combo = W.QComboBox()
            combo.setAccessibleName(text)
            combo.setFixedWidth(CONTROL_WIDTH)
            for value, name in options:
                combo.addItem(name, value)
            combo.setCurrentIndex(max(0, combo.findData(getattr(state, key))))
            combo.currentIndexChanged.connect(
                lambda _i, k=key, box=combo: self._set(k, box.currentData())
            )
            self.combos[key] = combo
            preference_row(
                QtWidgets=W,
                QtCore=C,
                rows_layout=rows,
                title=text,
                subtitle=subtitle,
                control=combo,
            )
        for key, text, subtitle in TOGGLES:
            switch = make_toggle_switch(
                QtCore=C, QtGui=G, QtWidgets=W, checked=bool(getattr(state, key))
            )
            switch.setAccessibleName(text)
            switch.toggled.connect(lambda on, k=key: self._set(k, bool(on)))
            self.switches[key] = switch
            preference_row(
                QtWidgets=W,
                QtCore=C,
                rows_layout=rows,
                title=text,
                subtitle=subtitle,
                control=switch,
            )
        scroll = W.QScrollArea()
        scroll.setObjectName("prototypeScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(C.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(card)
        layout.addWidget(scroll, 1)

        actions = W.QGridLayout()
        actions.setHorizontalSpacing(GAP["sm"])
        actions.setVerticalSpacing(GAP["sm"])
        self.action_buttons: dict[str, W.QPushButton] = {}
        for index, (name, text) in enumerate(ACTIONS):
            action = W.QPushButton(text)
            action.clicked.connect(
                lambda _c=False, n=name: self.action_requested.emit(n)
            )
            self.action_buttons[name] = action
            actions.addWidget(action, index // 2, index % 2)
        layout.addSpacing(GAP["xs"])
        layout.addLayout(actions)
        foot = W.QHBoxLayout()
        foot.addStretch()
        close = W.QPushButton("Close")
        close.clicked.connect(self.close)
        foot.addWidget(close)
        layout.addLayout(foot)
        self.resize(620, 720)

    def _set(self, key: str, value) -> None:
        if getattr(self.state, key) == value:
            return
        setattr(self.state, key, value)
        self.changed.emit(key, value)

    def sync(self) -> None:
        """Re-read the state after a page consumed a one-shot switch. The
        state already holds each value, so ``_set`` emits nothing."""
        for key, combo in self.combos.items():
            combo.setCurrentIndex(max(0, combo.findData(getattr(self.state, key))))
        for key, switch in self.switches.items():
            switch.setChecked(bool(getattr(self.state, key)))
