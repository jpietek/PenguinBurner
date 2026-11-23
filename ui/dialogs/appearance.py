"""Appearance: colour theme, accent colour and font in one place.

Opened from the header's palette button. Every choice updates the sample
at the bottom straight away; **Apply** hands the new ``AppearanceSettings``
to the window, which saves it in ``[ui]`` and restyles the app.
Nothing here touches the GPU, the root service or a game.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from ui.components.preference_rows import preference_group, preference_row
from ui.design.style import (
    BUNDLED_FAMILY,
    GAP,
    RAD,
    TYPE,
    WEIGHT,
    effective_accent,
    qcolor,
    system_accent,
    system_font_family,
    system_prefers_light,
)
from ui.design.tokens import (
    SYSTEM_ACCENT,
    AppearanceSettings,
    accent_presets,
    accent_roles,
    resolve_colors,
    resolve_mode,
)

THEME_CHOICES = (("auto", "Auto"), ("dark", "Dark"), ("light", "Light"))
FONT_CHOICES = (("custom", "Custom"), ("system", "System"))
SWATCH = 28
CONTROL_WIDTH = 260


def swatch_icon(color: str, size: int = 18) -> G.QIcon:
    """A filled accent disc with a hairline edge, so a light swatch still
    reads on a light card."""
    icon = G.QIcon()
    for ratio in (1, 2, 3):
        pixmap = G.QPixmap(size * ratio, size * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(C.Qt.GlobalColor.transparent)
        painter = G.QPainter(pixmap)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        painter.setPen(G.QPen(qcolor("border"), 1))
        painter.setBrush(G.QColor(color))
        painter.drawEllipse(C.QRectF(0.5, 0.5, size - 1, size - 1))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


class Segmented(W.QWidget):
    """A row of exclusive buttons; ``value()`` is the checked choice."""

    changed = C.Signal(str)

    def __init__(self, choices, value: str, name: str, parent=None):
        super().__init__(parent)
        row = W.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(GAP["xs"])
        self.group = W.QButtonGroup(self)
        self.buttons: dict[str, W.QPushButton] = {}
        for key, text in choices:
            button = W.QPushButton(text)
            button.setProperty("segment", True)
            button.setCheckable(True)
            button.setChecked(key == value)
            button.setAccessibleName(f"{name}: {text}")
            button.clicked.connect(lambda _c=False, k=key: self.changed.emit(k))
            self.group.addButton(button)
            self.buttons[key] = button
            row.addWidget(button)

    def value(self) -> str:
        return next(k for k, b in self.buttons.items() if b.isChecked())

    def set_value(self, value: str) -> None:
        self.buttons[value].setChecked(True)


class AccentPicker(W.QWidget):
    """System (the desktop's accent, the default), preset swatches and
    Custom…; the selected swatch carries a ring."""

    changed = C.Signal(str)

    def __init__(self, value: str, parent=None):
        super().__init__(parent)
        row = W.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(GAP["xs"])
        self.group = W.QButtonGroup(self)
        self.system_button = W.QPushButton("System")
        self.system_button.setProperty("segment", True)
        self.system_button.setCheckable(True)
        self.system_button.setIconSize(C.QSize(12, 12))
        self.system_button.setToolTip("Follow the desktop accent colour")
        self.system_button.setAccessibleName("Accent: System")
        self.system_button.clicked.connect(lambda: self.changed.emit(SYSTEM_ACCENT))
        self.group.addButton(self.system_button)
        row.addWidget(self.system_button)
        row.addSpacing(GAP["xs"])
        self.swatches: dict[str, W.QToolButton] = {}
        for name, label, _swatch in accent_presets():
            self.swatches[name] = self._swatch(label, name)
            row.addWidget(self.swatches[name])
        # The custom swatch appears once a colour is picked.
        self.custom_swatch = self._swatch("Custom colour", "")
        self.custom_swatch.hide()
        row.addWidget(self.custom_swatch)
        row.addSpacing(GAP["xs"])
        self.custom_button = W.QPushButton("Custom…")
        self.custom_button.setObjectName("accentCustom")
        self.custom_button.clicked.connect(self.pick_custom)
        row.addWidget(self.custom_button)
        self.mode = "dark"
        self._value = ""
        self.set_value(value)

    def _swatch(self, label: str, value: str) -> W.QToolButton:
        swatch = W.QToolButton()
        swatch.setObjectName("accentSwatch")
        swatch.setCheckable(True)
        swatch.setFixedSize(SWATCH, SWATCH)
        swatch.setIconSize(C.QSize(18, 18))
        swatch.setToolTip(label)
        swatch.setAccessibleName(f"Accent: {label}")
        swatch.clicked.connect(
            lambda _c=False, s=swatch: self.changed.emit(s.property("accent"))
        )
        swatch.setProperty("accent", value)
        self.group.addButton(swatch)
        return swatch

    def value(self) -> str:
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value
        if value == SYSTEM_ACCENT:
            self.system_button.setChecked(True)
        elif value in self.swatches:
            self.swatches[value].setChecked(True)
        else:
            self.custom_swatch.setProperty("accent", value)
            self.custom_swatch.setToolTip(f"Custom colour {value}")
            self.custom_swatch.show()
            self.custom_swatch.setChecked(True)
        self.repaint_swatches()

    def set_mode(self, mode: str) -> None:
        """Swatches show the accent as the chosen theme will draw it."""
        self.mode = mode
        self.repaint_swatches()

    def repaint_swatches(self) -> None:
        system = accent_roles(effective_accent(SYSTEM_ACCENT), self.mode)["accent"]
        self.system_button.setIcon(swatch_icon(system, 12))
        for swatch in [*self.swatches.values(), self.custom_swatch]:
            accent = swatch.property("accent")
            if accent:
                swatch.setIcon(swatch_icon(accent_roles(accent, self.mode)["accent"]))

    def pick_custom(self) -> None:
        start = accent_roles(effective_accent(self._value), self.mode)["accent"]
        color = W.QColorDialog.getColor(G.QColor(start), self, "Accent colour")
        if color.isValid():
            value = color.name().upper()
            self.set_value(value)
            self.changed.emit(value)


class AppearanceSample(W.QWidget):
    """A miniature window drawn in the pending theme, accent and font: tab
    strip, a profile row with its primary action, a switch and a link."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("appearanceSample")
        self.setMinimumSize(420, 156)
        self.setSizePolicy(W.QSizePolicy.Policy.Expanding, W.QSizePolicy.Policy.Fixed)
        self.colors: dict[str, str] = {}
        self.family = BUNDLED_FAMILY
        self.setAccessibleName("Appearance sample")

    def show_settings(self, colors: dict[str, str], family: str) -> None:
        self.colors, self.family = colors, family
        self.update()

    def _font(self, size: int, weight: int = 400) -> G.QFont:
        font = G.QFont(self.family)
        font.setPixelSize(size)
        font.setWeight(G.QFont.Weight(weight))
        return font

    def paintEvent(self, _event):
        if not self.colors:
            return
        c = self.colors
        painter = G.QPainter(self)
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        frame = C.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(G.QPen(G.QColor(c["border"]), 1))
        painter.setBrush(G.QColor(c["window"]))
        painter.drawRoundedRect(frame, RAD["panel"], RAD["panel"])
        # Header + tab strip on panel.
        strip = C.QRectF(frame.left() + 1, frame.top() + 1, frame.width() - 2, 40)
        painter.setPen(C.Qt.PenStyle.NoPen)
        painter.setBrush(G.QColor(c["panel"]))
        painter.drawRoundedRect(strip, RAD["panel"] - 1, RAD["panel"] - 1)
        painter.fillRect(
            strip.adjusted(0, strip.height() / 2, 0, 0), G.QColor(c["panel"])
        )
        painter.setPen(G.QColor(c["divider"]))
        painter.drawLine(strip.bottomLeft(), strip.bottomRight())
        x = strip.left() + GAP["lg"]
        for index, text in enumerate(("Auto UV", "Profiles", "Game Library")):
            font = self._font(TYPE["caption"] + 1, WEIGHT["semibold"])
            width = G.QFontMetrics(font).horizontalAdvance(text)
            painter.setFont(font)
            painter.setPen(G.QColor(c["ink" if index == 1 else "secondary"]))
            painter.drawText(
                C.QRectF(x, strip.top(), width, strip.height() - 3),
                C.Qt.AlignmentFlag.AlignCenter,
                text,
            )
            if index == 1:
                painter.fillRect(
                    C.QRectF(x - 4, strip.bottom() - 3, width + 8, 3),
                    G.QColor(c["accent"]),
                )
            x += width + GAP["xl"]
        # A selected profile row: selection tint, accent edge, tier dot.
        row = C.QRectF(
            frame.left() + GAP["lg"],
            strip.bottom() + GAP["md"],
            frame.width() - 2 * GAP["lg"],
            40,
        )
        painter.fillRect(row, G.QColor(c["selection"]))
        painter.fillRect(
            C.QRectF(row.left(), row.top(), 2, row.height()), G.QColor(c["accent"])
        )
        painter.setPen(C.Qt.PenStyle.NoPen)
        painter.setBrush(G.QColor(c["balanced"]))
        painter.drawEllipse(C.QPointF(row.left() + 16, row.center().y()), 4, 4)
        painter.setPen(G.QColor(c["ink"]))
        painter.setFont(self._font(TYPE["body"], WEIGHT["semibold"]))
        painter.drawText(
            C.QRectF(row.left() + 28, row.top(), 200, row.height() / 2 + 4),
            C.Qt.AlignmentFlag.AlignLeft | C.Qt.AlignmentFlag.AlignBottom,
            "Balanced",
        )
        painter.setPen(G.QColor(c["secondary"]))
        painter.setFont(self._font(TYPE["caption"]))
        painter.drawText(
            C.QRectF(row.left() + 28, row.center().y() + 2, 220, row.height() / 2),
            C.Qt.AlignmentFlag.AlignLeft | C.Qt.AlignmentFlag.AlignTop,
            "−45 W · 98% of stock FPS",
        )
        painter.setPen(G.QColor(c["gain"]))
        painter.drawText(
            row.adjusted(0, 0, -GAP["md"], 0),
            C.Qt.AlignmentFlag.AlignRight | C.Qt.AlignmentFlag.AlignVCenter,
            "Verified",
        )
        # Controls: a switch that is on, a link, the secondary and primary actions.
        base = row.bottom() + GAP["md"]
        track = C.QRectF(frame.left() + GAP["lg"], base + 7, 32, 18)
        painter.setPen(G.QPen(G.QColor(c["primaryBorder"]), 1))
        painter.setBrush(G.QColor(c["primary"]))
        painter.drawRoundedRect(track, 9, 9)
        painter.setPen(C.Qt.PenStyle.NoPen)
        painter.setBrush(G.QColor(c["onPrimary"]))
        painter.drawEllipse(C.QPointF(track.right() - 9, track.center().y()), 6, 6)
        painter.setPen(G.QColor(c["ink"]))
        painter.setFont(self._font(TYPE["caption"] + 1))
        painter.drawText(
            C.QRectF(track.right() + GAP["sm"], base, 120, 32),
            C.Qt.AlignmentFlag.AlignVCenter,
            "Silent fan curve",
        )
        link_font = self._font(TYPE["caption"] + 1)
        link_font.setUnderline(True)
        painter.setFont(link_font)
        painter.setPen(G.QColor(c["accent"]))
        painter.drawText(
            C.QRectF(track.right() + 128, base, 110, 32),
            C.Qt.AlignmentFlag.AlignVCenter,
            "Perf Profile",
        )
        button_font = self._font(TYPE["caption"] + 1, WEIGHT["semibold"])
        painter.setFont(button_font)
        right = frame.right() - GAP["lg"]
        for text, fill, edge, ink in (
            ("Apply", "primary", "primaryBorder", "onPrimary"),
            ("Verify…", "control", "border", "ink"),
        ):
            width = G.QFontMetrics(button_font).horizontalAdvance(text) + 2 * GAP["md"]
            rect = C.QRectF(right - width, base, width, 30)
            painter.setPen(G.QPen(G.QColor(c[edge]), 1))
            painter.setBrush(G.QColor(c[fill]))
            painter.drawRoundedRect(rect, RAD["control"], RAD["control"])
            painter.setPen(G.QColor(c[ink]))
            painter.drawText(rect, C.Qt.AlignmentFlag.AlignCenter, text)
            right = rect.left() - GAP["sm"]
        painter.end()


class AppearanceDialog(W.QDialog):
    """Theme, accent and font; ``applied`` carries the new settings."""

    applied = C.Signal(object)

    def __init__(self, current: AppearanceSettings, parent=None):
        super().__init__(parent)
        self.setObjectName("appearanceDialog")
        self.setWindowTitle("Appearance")
        self.setModal(True)
        self.settings = current.normalized()
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(GAP["xl"], GAP["lg"], GAP["xl"], GAP["lg"])
        layout.setSpacing(GAP["sm"])
        title = W.QLabel("Appearance")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        layout.addSpacing(GAP["xs"])

        colors, colors_rows = preference_group(QtWidgets=W)
        self.theme = Segmented(THEME_CHOICES, self.settings.theme, "Theme")
        self.theme.changed.connect(lambda v: self._update(theme=v))
        self.theme_row = preference_row(
            QtWidgets=W,
            QtCore=C,
            rows_layout=colors_rows,
            title="Theme",
            subtitle=self._theme_subtitle(),
            control=self.theme,
        )
        self.accent = AccentPicker(self.settings.accent)
        self.accent.changed.connect(lambda v: self._update(accent=v))
        self.accent_row = preference_row(
            QtWidgets=W,
            QtCore=C,
            rows_layout=colors_rows,
            title="Accent colour",
            subtitle=self._accent_subtitle(),
            control=self.accent,
        )
        layout.addWidget(colors)

        fonts, font_rows = preference_group(QtWidgets=W)
        self.font_mode = Segmented(FONT_CHOICES, self.settings.font, "Font")
        self.font_mode.changed.connect(lambda v: self._update(font=v))
        self.font_row = preference_row(
            QtWidgets=W,
            QtCore=C,
            rows_layout=font_rows,
            title="Font",
            subtitle=self._font_subtitle(),
            control=self.font_mode,
        )
        self.family = W.QFontComboBox()
        self.family.setObjectName("appearanceFontFamily")
        self.family.setAccessibleName("Custom font")
        self.family.setFixedWidth(CONTROL_WIDTH)
        self.family.setFontFilters(W.QFontComboBox.FontFilter.ScalableFonts)
        self.family.setCurrentFont(G.QFont(self.settings.font_family))
        self.family.currentFontChanged.connect(
            lambda font: self._update(font_family=font.family())
        )
        self.family_row = preference_row(
            QtWidgets=W,
            QtCore=C,
            rows_layout=font_rows,
            title="Custom font",
            subtitle=f"{BUNDLED_FAMILY} ships with PenguinBurner.",
            control=self.family,
        )
        layout.addSpacing(GAP["sm"])
        layout.addWidget(fonts)

        layout.addSpacing(GAP["md"])
        # Labelled, so the miniature window reads as a preview of these
        # choices rather than as part of the dialog.
        preview_title = W.QLabel("Preview")
        preview_title.setObjectName("appearancePreviewTitle")
        layout.addWidget(preview_title)
        preview_note = W.QLabel(
            "How PenguinBurner will look. Nothing changes until Apply."
        )
        preview_note.setObjectName("caption")
        layout.addWidget(preview_note)
        layout.addSpacing(GAP["xs"])
        self.sample = AppearanceSample()
        layout.addWidget(self.sample)
        layout.addSpacing(GAP["sm"])

        foot = W.QHBoxLayout()
        foot.setSpacing(GAP["sm"])
        self.defaults_button = W.QPushButton("Restore defaults")
        self.defaults_button.clicked.connect(self.restore_defaults)
        foot.addWidget(self.defaults_button)
        foot.addStretch(1)
        cancel = W.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.apply_button = W.QPushButton("Apply")
        self.apply_button.setObjectName("nextAction")
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self._apply)
        foot.addWidget(cancel)
        foot.addWidget(self.apply_button)
        layout.addLayout(foot)
        self.setMinimumWidth(640)
        self._refresh()

    def _mode(self) -> str:
        return resolve_mode(self.settings.theme, system_prefers_light())

    def _theme_subtitle(self) -> str:
        if self.settings.theme != "auto":
            return "Auto follows your desktop's light or dark setting."
        return f"Follows your desktop, now {self._mode()}."

    def _accent_subtitle(self) -> str:
        if self.settings.accent != SYSTEM_ACCENT:
            return "Buttons, links, selection and the current tab."
        if system_accent() is None:
            return "No desktop accent; PenguinBurner green is used."
        return "Follows your desktop's accent colour."

    def _font_subtitle(self) -> str:
        if self.settings.font == "system":
            return f"Uses your desktop font, {system_font_family()}."
        return "Looks the same on every desktop."

    def _update(self, **changes) -> None:
        self.settings = replace(self.settings, **changes).normalized()
        self._refresh()

    def _refresh(self) -> None:
        mode = self._mode()
        self.accent.set_mode(mode)
        self.theme_row.property("subtitleLabel").setText(self._theme_subtitle())
        self.font_row.property("subtitleLabel").setText(self._font_subtitle())
        self.accent_row.property("subtitleLabel").setText(self._accent_subtitle())
        custom = self.settings.font == "custom"
        self.family.setEnabled(custom)
        self.family_row.property("titleLabel").setEnabled(custom)
        self.family_row.property("subtitleLabel").setEnabled(custom)
        family = self.settings.font_family if custom else system_font_family()
        self.sample.show_settings(
            resolve_colors(mode, effective_accent(self.settings.accent)), family
        )
        if self.isVisible():
            C.QTimer.singleShot(0, self._fit_height)

    def showEvent(self, event):
        super().showEvent(event)
        C.QTimer.singleShot(0, self._fit_height)

    def _fit_height(self) -> None:
        """Wrapped subtitles only know their height once the dialog has its
        width; Qt does not fold that into a top-level's minimum, so grow to
        the layout's height-for-width and the Preview never meets the
        buttons."""
        layout = self.layout()
        if layout is None:
            return
        needed = layout.totalHeightForWidth(self.width())
        if needed > self.height():
            self.setMinimumHeight(needed)
            self.resize(self.width(), needed)

    def restore_defaults(self) -> None:
        self.settings = AppearanceSettings()
        self.theme.set_value(self.settings.theme)
        self.accent.set_value(self.settings.accent)
        self.font_mode.set_value(self.settings.font)
        with C.QSignalBlocker(self.family):
            self.family.setCurrentFont(G.QFont(self.settings.font_family))
        self._refresh()

    def _apply(self) -> None:
        self.applied.emit(self.settings)
        self.accept()
