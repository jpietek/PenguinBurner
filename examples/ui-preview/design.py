"""Read the preview's shared design roles and apply them to Qt graphics."""

import json
import re
from pathlib import Path

from PySide6 import QtCore as C
from PySide6 import QtGui as G

from ui import theme
from ui.components.curve_plot import CurvePlot
from ui.styles import STYLESHEET

SPEC = json.loads(Path(__file__).with_name("pburn-design.json").read_text())
COL = SPEC["colors"]
TYPE = SPEC["typography"]
GAP = SPEC["spacing"]
RAD = SPEC["radii"]


class DemoCurvePlot(CurvePlot):
    def __init__(self, **kwargs):
        kwargs["source_color"] = COL["baseCurve"]
        kwargs["candidate_color"] = COL["candidateCurve"]
        super().__init__(**kwargs)
        if (
            self.candidate_curve is None
            or self.probe_marker is None
            or self.probe_vline is None
            or self.probe_hline is None
        ):
            return
        self._manual_selection_color = COL["selectedCurve"]
        self.plot.setBackground(COL["plotSurface"])
        self.plot.showGrid(x=True, y=True, alpha=0.65)
        if self.source_curve is not None:
            self.source_curve.setPen(
                self.pg.mkPen(COL["baseCurve"], width=1.8, style=C.Qt.PenStyle.DashLine)
            )
            self.source_curve.setSymbol(None)
        self.candidate_curve.setZValue(5)
        self.candidate_curve.setSymbolPen(self.pg.mkPen(COL["candidateCurve"]))
        self.candidate_curve.setSymbolSize(4)
        self.probe_marker.setZValue(9)
        for side in ("left", "bottom"):
            axis = self.plot.getAxis(side)
            axis.setTextPen(COL["secondary"])
            axis.setPen(COL["gridMinor"])
            axis.setTickPen(COL["gridMajor"])
            axis.label.setDefaultTextColor(G.QColor(COL["secondary"]))
            axis.setLabel(color=COL["secondary"])
        warning = G.QColor(COL["warning"])
        self.probe_marker.setSymbolBrush(warning)
        self.probe_marker.setSymbolPen(warning)
        warning.setAlpha(150)
        self.probe_vline.setPen(warning)
        self.probe_hline.setPen(warning)

    def _candidate_curve_pen(self):
        return self.pg.mkPen(COL["candidateCurve"], width=2.3)

    def _previous_curve_pen(self, *, highlighted):
        if highlighted:
            return self._manual_selection_curve_pen()
        return self.pg.mkPen(self._curve_color(COL["curveHistory"], 140), width=1)

    def _manual_selection_curve_pen(self):
        return self.pg.mkPen(
            COL["selectedCurve"], width=3, style=C.Qt.PenStyle.DashLine
        )

    def _apply_curve_highlights(self):
        super()._apply_curve_highlights()
        for trace in self.previous_curves:
            selected = self._curve_is_highlighted(trace["curve_id"])
            trace["item"].setZValue(8 if selected else -9)
        if self._highlight_overlay_curve is not None:
            self._highlight_overlay_curve.setZValue(8)


def install_theme(app):
    font = G.QFont(TYPE["family"])
    font.setPixelSize(TYPE["body"])
    app.setFont(font)
    palette = app.palette()
    for key, token in {
        "Window": "window",
        "Base": "panel",
        "AlternateBase": "raised",
        "Button": "control",
        "Text": "ink",
        "WindowText": "ink",
        "ButtonText": "ink",
        "Highlight": "selection",
        "HighlightedText": "onPrimary",
        "Link": "accent",
        "LinkVisited": "accent",
        "PlaceholderText": "quiet",
        "ToolTipBase": "raised",
        "ToolTipText": "ink",
        "Accent": "focus",
    }.items():
        role = getattr(G.QPalette.ColorRole, key, None)
        if role is not None:
            palette.setColor(role, G.QColor(COL[token]))
    app.setPalette(palette)
    # Preserve the native widgets' individual selectors and layout rules.
    native = {
        "WINDOW_BG": "window",
        "SURFACE_BG": "panel",
        "SURFACE_ALT_BG": "raised",
        "CONTROL_BG": "control",
        "CONTROL_HOVER_BG": "raised",
        "BORDER": "divider",
        "BORDER_STRONG": "border",
        "TEXT": "ink",
        "TEXT_STRONG": "ink",
        "TEXT_MUTED": "secondary",
        "TEXT_DISABLED": "quiet",
        "PROFILE_SELECTED_BG": "selection",
        "TOOLTIP_BG": "raised",
        "LINK": "accent",
        "PRIMARY_BUTTON_BG": "primary",
        "PRIMARY_BUTTON_BORDER": "primaryHover",
        "PRIMARY_BUTTON_HOVER_BORDER": "focus",
        "PRIMARY_BUTTON_TEXT": "onPrimary",
        "AUTO_UV_BUTTON_BG": "primary",
        "AUTO_UV_BUTTON_BORDER": "primaryHover",
        "AUTO_UV_BUTTON_TEXT": "onPrimary",
        "AUTO_UV_BUTTON_HOVER_BORDER": "focus",
        "AUTO_UV_BUTTON_PRESSED_BG": "selection",
        "GOOD": "gain",
        "WARNING": "warning",
        "ERROR": "loss",
        "ABOUT_BUTTON_BG": "control",
        "ABOUT_BUTTON_BORDER": "border",
    }
    substitutions = {
        getattr(theme, key).lower(): COL[token] for key, token in native.items()
    }
    # A few production controls have literal hover colors in their own rules.
    substitutions.update(
        {
            literal: COL[role]
            for literal, role in {
                "#7f93ad": "accent",
                "#9fb1c7": "focus",
                "#2f3844": "raised",
                "#252a31": "control",
                "#5b6675": "border",
                "#c7eaff": "border",
                "#e7f7ff": "focus",
                "#fff0f0": "focus",
            }.items()
        }
    )
    base = re.sub(
        r"#[0-9A-Fa-f]{6}\b",
        lambda match: substitutions.get(match[0].lower(), match[0]),
        STYLESHEET,
    )
    # Full scan includes all three profiles. The active button selects which
    # profile's settings are visible, independently of that inclusion state.
    preset_rules = []
    for tier in ("efficiency", "balanced", "performance"):
        selector = (
            f'QPushButton#autoUvPresetButton[presetId="{tier}"][scanIncluded="true"]'
        )
        preset_rules.append(f"""
            {selector} {{background:{COL["control"]}; color:{COL["ink"]}; border-color:{COL["border"]}}}
            {selector}:hover {{background:{COL["raised"]}; border-color:{COL["secondary"]}}}
            {selector}:checked {{background:{COL["selection"]}; border-color:{COL["accent"]}}}
            {selector}:focus {{border-color:{COL["focus"]}}}
        """)
    base += "\n".join(preset_rules)
    app.setStyleSheet(
        base
        + f"""
        QWidget{{font-family:"{TYPE["family"]}";font-size:{TYPE["body"]}px;color:{COL["ink"]}}}
        QLabel#brand{{font-size:20px;font-weight:700;padding:4px 8px}}
        QLabel#pageTitle{{font-size:{TYPE["title"]}px;font-weight:700}}
        QLabel#sectionTitle{{font-size:{TYPE["section"]}px;font-weight:650}}
        QLabel#caption{{font-size:{TYPE["caption"]}px;color:{COL["secondary"]}}}
        QFrame#scanLogStrip{{background:{COL["panel"]};border:1px solid {COL["border"]};border-radius:{RAD["control"]}px}}
        QLabel#latestLog{{font-size:{TYPE["caption"]}px;color:{COL["quiet"]};padding:1px 0}}
        QPushButton#copyFullLog{{font-size:{TYPE["caption"]}px;color:{COL["secondary"]};background:transparent;border:none;border-left:1px solid {COL["divider"]};border-radius:0;padding:3px 8px}}
        QPushButton#copyFullLog:hover{{background:{COL["raised"]};color:{COL["ink"]}}}
        QPushButton#copyFullLog:focus{{border:1px solid {COL["focus"]}}}
        QLabel#curveLegend{{font-size:{TYPE["caption"]}px;color:{COL["secondary"]};padding:3px 8px}}
        QPushButton{{padding:6px 11px;background:{COL["control"]};border:1px solid {COL["border"]};border-radius:{RAD["control"]}px}}
        QPushButton:hover{{background:{COL["raised"]};border-color:{COL["secondary"]}}}
        QPushButton#setupScan,QPushButton#nextAction,QDialogButtonBox QPushButton:default{{background:{COL["primary"]};border-color:{COL["primaryHover"]};color:{COL["onPrimary"]};font-weight:650}}
        QPushButton#setupScan:hover,QPushButton#nextAction:hover{{background:{COL["primaryHover"]}}}
        QPushButton#setupScan:disabled,QPushButton:disabled{{background:{COL["raised"]};color:{COL["quiet"]};border-color:{COL["divider"]}}}
        QPushButton#aboutPreview{{background:transparent;color:{COL["secondary"]};border:1px solid transparent;padding:4px 6px}}
        QPushButton#aboutPreview:hover{{color:{COL["ink"]};background:{COL["raised"]}}}
        QPushButton:focus,QToolButton:focus,QComboBox:focus,QSpinBox:focus,QLineEdit:focus,QTableWidget:focus,QListWidget:focus{{border:1px solid {COL["focus"]}}}
        QMenu::item:selected,QComboBox QAbstractItemView::item:selected,QListView::item:selected,QTreeView::item:selected{{background:{COL["selection"]};color:{COL["onPrimary"]}}}
        QComboBox QAbstractItemView::item:hover,QListView::item:hover,QTreeView::item:hover{{background:{COL["raised"]};color:{COL["ink"]}}}
        QLineEdit,QPlainTextEdit,QTextEdit{{selection-background-color:{COL["selection"]};selection-color:{COL["onPrimary"]}}}
        QTabBar::tab{{padding:9px 14px;color:{COL["secondary"]};font-size:{TYPE["body"]}px;background:{COL["panel"]};border-bottom:2px solid transparent}}
        QTabBar::tab:selected{{color:{COL["ink"]};background:{COL["selection"]};border-bottom-color:{COL["accent"]}}}
        QTabBar::tab:focus{{border-bottom-color:{COL["focus"]}}}
        QTableWidget{{font-size:{TYPE["table"]}px;selection-background-color:{COL["selection"]};selection-color:{COL["onPrimary"]}}}
        QTableWidget#scanRunsTable{{background:{COL["panel"]};border:1px solid {COL["border"]}}}
        QTableWidget#scanRunsTable::item{{padding:1px 3px;border-bottom:1px solid {COL["divider"]}}}
        QTableWidget#scanRunsTable QHeaderView::section{{font-size:{TYPE["table"]}px;font-weight:500;background:{COL["raised"]};color:{COL["secondary"]};border:none;border-bottom:1px solid {COL["border"]};padding:4px}}
        QHeaderView::section{{font-size:{TYPE["table"]}px;padding:4px}}
    """
    )
