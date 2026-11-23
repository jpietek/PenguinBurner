"""The selected profile's card, and the Adaptive for games card.

Both are views: the page hands them a frozen state object and listens to
their signals. Nothing here reads the data source.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from design import GAP
from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from qt.components.profile_style import (
    Pill,
    TierChip,
    divider,
    repolish,
    text_label,
)
from qt.components.switch_sync import set_switch
from ui.components.toggle_switch import make_toggle_switch


@dataclass(frozen=True)
class Metric:
    label: str
    value: str
    unit: str = ""
    tooltip: str = ""


@dataclass(frozen=True)
class DetailState:
    """Everything the detail card shows for the current selection."""

    mode: str = "none"  # "single" | "multi" | "none"
    title: str = "No profile selected"
    subtitle: str = "Select a profile to apply, edit or verify it."
    status: tuple[str, str] = ("", "")
    metrics: tuple[Metric, ...] = ()
    edit_vf_reason: str = ""
    show_fan_button: bool = False
    verify_reason: str = ""
    more_enabled: bool = True
    silent_fan_checked: bool = False
    silent_fan_enabled: bool = True
    silent_fan_subtitle: str = ""
    startup_checked: bool = False
    startup_enabled: bool = True
    startup_subtitle: str = ""
    startup_profile: str = ""
    startup_profile_subtitle: str = ""
    session_subtitle: str = ""
    session_tone: str = ""
    primary_text: str = "Apply"
    primary_action: str = "apply"  # "apply" | "verify"
    primary_enabled: bool = False
    primary_tooltip: str = ""
    # The selected profile already runs: its action is a secondary Re-apply.
    primary_secondary: bool = False
    notice: tuple[str, str] = ("", "")
    selected_names: tuple[str, ...] = field(default_factory=tuple)
    delete_enabled: bool = False


def card() -> tuple[W.QFrame, W.QVBoxLayout]:
    frame = W.QFrame()
    frame.setObjectName("profilesCard")
    layout = W.QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    return frame, layout


class SettingRow(W.QWidget):
    """Title, one-sentence subtitle and one control on the right."""

    def __init__(self, title: str, control: W.QWidget | None = None, parent=None):
        super().__init__(parent)
        layout = W.QHBoxLayout(self)
        layout.setContentsMargins(GAP["lg"], 10, GAP["lg"], 10)
        layout.setSpacing(GAP["lg"])
        text = W.QVBoxLayout()
        text.setSpacing(2)
        self.title = text_label(title, "profilesRowTitle")
        self.subtitle = text_label("", "profilesRowSubtitle", wrap=True)
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        layout.addLayout(text, 1)
        self.control = control
        if control is not None:
            layout.addWidget(control, 0, C.Qt.AlignmentFlag.AlignVCenter)

    def set_subtitle(self, text: str, tone: str = "") -> None:
        self.subtitle.setText(text)
        self.subtitle.setVisible(bool(text))
        if self.subtitle.property("tone") != tone:
            self.subtitle.setProperty("tone", tone)
            repolish(self.subtitle)


class MetricTile(W.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.label = text_label("", "metricLabel")
        value_row = W.QHBoxLayout()
        value_row.setSpacing(4)
        self.value = text_label("", "metricValue")
        self.unit = text_label("", "metricUnit")
        value_row.addWidget(self.value, 0, C.Qt.AlignmentFlag.AlignBaseline)
        value_row.addWidget(self.unit, 0, C.Qt.AlignmentFlag.AlignBaseline)
        value_row.addStretch()
        layout.addWidget(self.label)
        layout.addLayout(value_row)

    def set_metric(self, metric: Metric) -> None:
        self.label.setText(metric.label)
        self.value.setText(metric.value)
        self.unit.setText(metric.unit)
        self.unit.setVisible(bool(metric.unit))
        self.setToolTip(metric.tooltip)
        self.setAccessibleName(f"{metric.label} {metric.value} {metric.unit}".strip())


class ProfileDetailCard(W.QFrame):
    """Apply, edit and verify the selected profile."""

    edit_vf = C.Signal()
    edit_fan = C.Signal()
    verify = C.Signal()
    primary = C.Signal(str)
    silent_fan_toggled = C.Signal(bool)
    startup_toggled = C.Signal(bool)
    delete_selection = C.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("profilesCard")
        self.state = DetailState()
        outer = W.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        head = W.QHBoxLayout()
        head.setContentsMargins(GAP["lg"] + 4, GAP["lg"], GAP["lg"] + 4, GAP["md"])
        titles = W.QVBoxLayout()
        titles.setSpacing(2)
        self.title = text_label("", "profilesTitle")
        self.title.setTextInteractionFlags(
            C.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.subtitle = text_label("", "profilesCaption", wrap=True)
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)
        self.status = Pill()
        head.addWidget(self.status, 0, C.Qt.AlignmentFlag.AlignTop)
        self.delete_button = W.QPushButton("Delete")
        self.delete_button.setObjectName("profilesDanger")
        self.delete_button.clicked.connect(self.delete_selection)
        head.addWidget(self.delete_button, 0, C.Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)

        # Single-profile body: metrics and actions | settings, then session.
        self.body = W.QWidget()
        body = W.QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(divider())
        columns = W.QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(0)
        left = W.QVBoxLayout()
        left.setContentsMargins(GAP["lg"] + 4, GAP["lg"], GAP["lg"], GAP["lg"])
        left.setSpacing(GAP["lg"])
        tiles = W.QHBoxLayout()
        tiles.setSpacing(GAP["xl"] + 4)
        self.tiles = [MetricTile() for _ in range(4)]
        for tile in self.tiles:
            tiles.addWidget(tile)
        tiles.addStretch()
        left.addLayout(tiles)
        actions = W.QHBoxLayout()
        actions.setSpacing(GAP["sm"])
        self.edit_vf_button = W.QPushButton("Edit V/F curve")
        self.edit_vf_button.clicked.connect(self.edit_vf)
        self.edit_fan_button = W.QPushButton("Edit fan curve")
        self.edit_fan_button.clicked.connect(self.edit_fan)
        self.verify_button = W.QPushButton("Verify…")
        self.verify_button.clicked.connect(self.verify)
        self.more_button = W.QPushButton("More actions")
        self.more_button.setObjectName("profilesMenuButton")
        for button in (
            self.edit_vf_button,
            self.edit_fan_button,
            self.verify_button,
            self.more_button,
        ):
            actions.addWidget(button)
        actions.addStretch()
        left.addLayout(actions)
        left.addStretch()
        # Current session: what Apply does, beside the one filled button.
        self.primary_button = W.QPushButton("Apply")
        self.primary_button.setObjectName("profilesPrimary")
        self.primary_button.setAutoDefault(False)
        self.primary_button.clicked.connect(
            lambda: self.primary.emit(self.state.primary_action)
        )
        session = W.QFrame()
        session.setObjectName("profilesSession")
        session_row = W.QHBoxLayout(session)
        session_row.setContentsMargins(
            GAP["md"], GAP["sm"] + 2, GAP["md"], GAP["sm"] + 2
        )
        session_row.setSpacing(GAP["lg"])
        session_text = W.QVBoxLayout()
        session_text.setSpacing(2)
        self.session_title = text_label("Current session", "profilesRowTitle")
        self.session_subtitle = text_label("", "profilesRowSubtitle", wrap=True)
        self.notice = text_label("", "profilesNotice", wrap=True)
        self.notice.setAccessibleName("Profile status")
        session_text.addWidget(self.session_title)
        session_text.addWidget(self.session_subtitle)
        session_text.addWidget(self.notice)
        session_row.addLayout(session_text, 1)
        session_row.addWidget(self.primary_button, 0, C.Qt.AlignmentFlag.AlignVCenter)
        left.addWidget(session)
        columns.addLayout(left, 3)
        columns.addWidget(divider(vertical=True))
        right = W.QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.silent_switch = make_toggle_switch(
            QtCore=C, QtGui=G, QtWidgets=W, object_name="silentFanSwitch"
        )
        self.silent_switch.setAccessibleName("Silent fan curve")
        self.silent_switch.toggled.connect(self._silent_toggled)
        self.silent_row = SettingRow("Silent fan curve", self.silent_switch)
        self.startup_switch = make_toggle_switch(
            QtCore=C,
            QtGui=G,
            QtWidgets=W,
            object_name="applyOnStartupSwitch",
            tooltip=(
                "When ticked, Apply also saves the profile for this GPU at boot. "
                "Unticking clears this GPU's saved boot profile immediately, and "
                "Apply then changes only the current session."
            ),
        )
        self.startup_switch.setAccessibleName("Apply on startup")
        self.startup_switch.toggled.connect(self._startup_toggled)
        self.startup_row = SettingRow("Apply on startup", self.startup_switch)
        self.boot_value = text_label("", "profilesValue")
        self.boot_row = SettingRow("Startup profile", self.boot_value)
        for i, row in enumerate((self.silent_row, self.startup_row, self.boot_row)):
            if i:
                right.addWidget(divider())
            right.addWidget(row, 1)
        columns.addLayout(right, 2)
        body.addLayout(columns)
        outer.addWidget(self.body)
        self.set_state(DetailState())

    # ---------------- state ----------------

    def set_more_menu(self, menu: W.QMenu) -> None:
        self.more_button.setMenu(menu)

    def set_state(self, state: DetailState) -> None:
        self.state = state
        single = state.mode == "single"
        self.title.setText(state.title)
        self.subtitle.setText(state.subtitle)
        self.status.setVisible(single and bool(state.status[0]))
        if single:
            self.status.set_status(*state.status)
        self.delete_button.setVisible(state.mode == "multi")
        self.delete_button.setText(f"Delete {len(state.selected_names)} profiles")
        self.delete_button.setEnabled(state.delete_enabled)
        self.body.setVisible(single)
        if not single:
            return
        for tile, metric in zip(self.tiles, state.metrics, strict=False):
            tile.set_metric(metric)
        for tile in self.tiles[len(state.metrics) :]:
            tile.hide()
        self.edit_vf_button.setEnabled(not state.edit_vf_reason)
        self.edit_vf_button.setToolTip(state.edit_vf_reason)
        self.edit_fan_button.setVisible(state.show_fan_button)
        self.verify_button.setVisible(state.primary_action != "verify")
        self.verify_button.setEnabled(not state.verify_reason)
        self.verify_button.setToolTip(
            state.verify_reason or "Run a stability check on this profile."
        )
        self.more_button.setEnabled(state.more_enabled)
        self._set_checked(self.silent_switch, state.silent_fan_checked)
        self.silent_switch.setEnabled(state.silent_fan_enabled)
        self.silent_row.set_subtitle(state.silent_fan_subtitle)
        self._set_checked(self.startup_switch, state.startup_checked)
        self.startup_switch.setEnabled(state.startup_enabled)
        self.startup_row.set_subtitle(state.startup_subtitle)
        self.boot_value.setText(state.startup_profile)
        self.boot_row.set_subtitle(state.startup_profile_subtitle)
        self.session_subtitle.setText(state.session_subtitle)
        self.session_subtitle.setVisible(bool(state.session_subtitle))
        if self.session_subtitle.property("tone") != state.session_tone:
            self.session_subtitle.setProperty("tone", state.session_tone)
            repolish(self.session_subtitle)
        self.primary_button.setText(state.primary_text)
        self.primary_button.setEnabled(state.primary_enabled)
        self.primary_button.setToolTip(state.primary_tooltip)
        role = "secondary" if state.primary_secondary else ""
        if self.primary_button.property("role") != role:
            self.primary_button.setProperty("role", role)
            repolish(self.primary_button)
        text, tone = state.notice
        self.notice.setText(text)
        self.notice.setVisible(bool(text))
        if self.notice.property("tone") != tone:
            self.notice.setProperty("tone", tone)
            repolish(self.notice)

    @staticmethod
    def _set_checked(switch, checked: bool) -> None:
        set_switch(switch, checked)

    def _silent_toggled(self, checked: bool) -> None:
        self.silent_fan_toggled.emit(bool(checked))

    def _startup_toggled(self, checked: bool) -> None:
        self.startup_toggled.emit(bool(checked))


class AdaptiveCard(W.QFrame):
    """Which tiers Adaptive can use, and the way into per-game Adaptive."""

    use_adaptive = C.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("profilesCard")
        layout = W.QHBoxLayout(self)
        layout.setContentsMargins(
            GAP["lg"] + 4, GAP["md"] + 2, GAP["lg"] + 4, GAP["md"] + 2
        )
        layout.setSpacing(GAP["lg"])
        text = W.QVBoxLayout()
        text.setSpacing(4)
        text.addWidget(text_label("Adaptive for games", "profilesTitle"))
        line = W.QHBoxLayout()
        line.setSpacing(GAP["lg"])
        self.caption = text_label("", "profilesCaption")
        line.addWidget(self.caption)
        self.chips_box = W.QWidget()
        self.chips = W.QHBoxLayout(self.chips_box)
        self.chips.setContentsMargins(0, 0, 0, 0)
        self.chips.setSpacing(GAP["lg"])
        line.addWidget(self.chips_box)
        line.addStretch()
        text.addLayout(line)
        layout.addLayout(text, 1)
        self.button = W.QPushButton("Use Adaptive for a game…")
        self.button.setToolTip(
            "Per-game Adaptive targets a frame rate and switches between the "
            "tiers below. Set it up per game in Game Library."
        )
        self.button.clicked.connect(self.use_adaptive)
        layout.addWidget(self.button, 0, C.Qt.AlignmentFlag.AlignVCenter)
        self.tiers: list[str] = []
        self.set_tiers([])

    def set_tiers(self, tiers: list[str], *, target_chosen: bool = True) -> None:
        if list(tiers) != self.tiers or not self.chips.count():
            while self.chips.count():
                item = self.chips.takeAt(0)
                widget = item.widget() if item is not None else None
                if widget is not None:
                    widget.hide()
                    widget.setParent(None)
                    widget.deleteLater()
            for tier in tiers:
                self.chips.addWidget(TierChip(tier))
        self.tiers = list(tiers)
        if tiers:
            self.caption.setText("Adaptive switches between these verified tiers:")
        elif not target_chosen:
            self.caption.setText("Choose a target GPU to see its Adaptive tiers.")
        else:
            self.caption.setText(
                "Verify a profile and assign a tier to enable Adaptive."
            )
        self.chips_box.setVisible(bool(tiers))
        self.button.setEnabled(bool(tiers))
