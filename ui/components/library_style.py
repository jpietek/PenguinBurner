"""Shared look of the Game Library and In-Game Overlay pages.

Base rules for both pages in the current theme, plus the two helpers that put
a preference group's heading inside its card, as on every other page.
"""

from __future__ import annotations

from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from ui.design.style import COL, GAP, RAD, TYPE, control_icons


class _Roles:
    """Attribute access to a design dict that reads the current value, so
    rules built after an Appearance change pick up the new theme."""

    def __init__(self, roles: dict):
        self._roles = roles

    def __getattr__(self, name):
        try:
            return self._roles[name]
        except KeyError:
            raise AttributeError(name) from None


c = _Roles(COL)
t = _Roles(TYPE)
sp = _Roles(GAP)
r = _Roles(RAD)


def library_stylesheet() -> str:
    """Game Library and In-Game Overlay base rules in the current theme."""
    ICONS = control_icons()
    return f"""
QLabel, QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QPushButton, QToolButton, QMenu {{
    font-family: "{t.family}"; font-size: {t.body}px; color: {c.ink};
}}
/* No colour here: each row's foreground (ink, or secondary when unwrapped)
   comes from the item, as in the real panel. */
QListWidget#gameList {{ font-family: "{t.family}"; font-size: {t.body}px; }}
QLabel#overlayPageTitle, QLabel#libraryPageTitle {{
    font-size: {t.title}px; font-weight: 700; color: {c.ink};
}}
/* The selected game is the detail's heading, under the page title. */
QLabel#gameTitle {{ font-size: {t.section}px; font-weight: 700; color: {c.ink}; }}
QLabel#libraryPaneTitle, QLabel#prefGroupHeading, QLabel#historyHeading, QLabel#cardHeading {{
    font-size: {t.section}px; font-weight: 600; color: {c.ink};
}}
QLabel#prefRowTitle {{ font-size: {t.body}px; color: {c.ink}; }}
QLabel#prefRowTitle:disabled, QLabel#prefRowSubtitle:disabled {{ color: {c.quiet}; }}
QLabel#prefRowSubtitle, QLabel#gameMetadata, QLabel#overlayMuted, QLabel#overlaySampleValue,
QLabel#libraryLabel, QLabel#libraryStatus, QLabel#gameTargetFollow, QLabel#overlayMetricCount,
QLabel#overlayPreviewCaption, QLabel#libraryPlaceholder {{ font-size: {t.caption}px; color: {c.secondary}; }}
QWidget#overlayConfigPanel, QWidget#overlayPage, QWidget#overlayGrid, QFrame#gameDetailsPane {{ background: {c.window}; }}
QWidget#cardHead {{ background: transparent; }}
QFrame#libraryPane, QFrame#prefGroupCard, QFrame#overlayPreviewCard, QFrame#historyEntry {{
    background: {c.panel}; border: 1px solid {c.border}; border-radius: {r.panel}px;
}}
QFrame#prefRow[hasSeparator="true"] {{ border-top: 1px solid {c.divider}; }}
QComboBox, QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QPushButton, QToolButton {{
    background: {c.control}; border: 1px solid {c.border}; border-radius: {r.control}px; padding: 6px {sp.md}px;
}}
QPushButton:hover, QToolButton:hover, QComboBox:hover {{ background: {c.controlHover}; border-color: {c.quiet}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {c.raised}; }}
/* One filled primary per view: Play. History and Live preview are secondary. */
QPushButton#gamePlayButton {{
    background: {c.primary}; border-color: {c.primaryBorder}; color: {c.onPrimary}; font-weight: 600;
}}
QPushButton#gamePlayButton:hover {{ background: {c.primaryHover}; border-color: {c.primaryBorder}; }}
QPushButton#gamePlayButton:pressed {{ background: {c.primaryPressed}; }}
QPushButton#gamePlayButton[playState="running"], QPushButton#gamePlayButton[playState="stopping"] {{
    background: {c.control}; border-color: {c.loss}; color: {c.loss};
}}
QPushButton#gamePlayButton:disabled {{ background: {c.control}; border-color: {c.divider}; color: {c.quiet}; }}
QPushButton#gamePerfProfileButton {{ font-weight: 600; }}
QPushButton#libraryWriteAction {{ border-color: {c.accent}; }}
QLabel#gameLaunchWarning {{ font-size: {t.caption}px; color: {c.loss}; }}
QLabel#gameCompatibility {{ font-size: {t.caption}px; color: {c.secondary}; }}
QLabel#libraryLoadingTitle {{ font-size: {t.section}px; font-weight: 600; color: {c.ink}; }}
QLabel#libraryLoadingSources {{ font-size: {t.caption}px; color: {c.secondary}; }}
QPushButton#overlayEditMetrics {{
    background: {c.control}; color: {c.ink}; border: 1px solid {c.border}; font-size: {t.caption}px; padding: 4px {sp.sm}px;
}}
QPushButton:disabled, QToolButton:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QLineEdit:disabled {{
    background: {c.control}; color: {c.quiet}; border-color: {c.divider};
}}
QComboBox:focus, QLineEdit:focus, QPlainTextEdit:focus, QToolButton:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 2px solid {c.focus};
}}
QPushButton:focus {{ border: 2px solid {c.focus}; padding: 5px {sp.md - 1}px; }}
QListWidget#gameList {{ background: {c.panel}; border: none; }}
QListWidget#gameList::item:selected, QComboBox QAbstractItemView::item:selected, QMenu::item:selected {{
    background: {c.selection}; color: {c.ink};
}}
QListWidget#gameList::item:selected {{ border-left: 2px solid {c.accent}; }}
QDialog {{ background: {c.window}; color: {c.ink}; }}
QFrame#libraryFilters {{ background: {c.raised}; border-radius: {r.panel}px; }}
QLabel#libraryFilterLabel {{ font-size: {t.caption}px; color: {c.secondary}; }}
QPushButton#libraryFiltersButton[expanded="true"] {{ border-color: {c.accent}; }}
/* The list tools row is caption-sized, as in the browser, so Sort, Filters
   and All games fit the default list width without clipping. */
QComboBox#librarySort, QPushButton#libraryFiltersButton, QToolButton#libraryAllGamesButton,
QFrame#libraryFilters QComboBox, QFrame#libraryFilters QPushButton {{
    font-size: {t.caption}px; padding-top: 4px; padding-bottom: 4px;
}}
QComboBox#librarySort, QFrame#libraryFilters QComboBox {{ padding-left: {sp.sm}px; }}
QFrame#libraryFilters QPushButton {{ padding-left: {sp.sm}px; padding-right: {sp.sm}px; }}
QPushButton#libraryFiltersButton {{ padding-left: {sp.sm}px; padding-right: {sp.sm}px; }}
QPushButton#libraryFiltersButton:focus {{ padding-left: {sp.sm - 1}px; padding-right: {sp.sm - 1}px; padding-top: 3px; padding-bottom: 3px; }}
QToolButton#libraryAllGamesButton {{ padding-left: {sp.sm}px; }}
/* Token sub-controls: the chevron the combo boxes use, never Fusion chrome. */
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 24px; border: none; background: transparent; }}
QComboBox::down-arrow {{ image: url({ICONS["chevron-down"]}); width: 10px; height: 5px; }}
QComboBox::down-arrow:disabled {{ image: url({ICONS["chevron-down-quiet"]}); }}
QComboBox::down-arrow:on {{ image: url({ICONS["chevron-up"]}); }}
QToolButton#libraryAllGamesButton {{ padding-right: 26px; }}
QToolButton#libraryAllGamesButton::menu-indicator {{
    image: url({ICONS["chevron-down"]}); width: 10px; height: 5px;
    subcontrol-origin: padding; subcontrol-position: center right; right: 10px;
}}
QToolButton#libraryAllGamesButton::menu-indicator:disabled {{ image: url({ICONS["chevron-down-quiet"]}); }}
QSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 22px; border: none; background: transparent; margin-top: 3px; }}
QSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 22px; border: none; background: transparent; margin-bottom: 3px; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {c.controlHover}; border-radius: {r.control - 1}px; }}
QSpinBox::up-arrow {{ image: url({ICONS["chevron-up"]}); width: 8px; height: 4px; }}
QSpinBox::down-arrow {{ image: url({ICONS["chevron-down"]}); width: 8px; height: 4px; }}
QSpinBox::up-arrow:disabled, QSpinBox::up-arrow:off {{ image: url({ICONS["chevron-up-quiet"]}); }}
QSpinBox::down-arrow:disabled, QSpinBox::down-arrow:off {{ image: url({ICONS["chevron-down-quiet"]}); }}
"""


def normalize_group_headings(widget):
    """Restore normal casing and tracking to section-sized native headings."""
    for label in widget.findChildren(W.QLabel, "prefGroupHeading"):
        label.setText(str(label.property("headingText") or label.text()))
        font = label.font()
        font.setLetterSpacing(G.QFont.SpacingType.AbsoluteSpacing, 0)
        label.setFont(font)


def heading_into_card(group, title_name="cardHeading"):
    """Move a preference group's heading (above its card) into the card's
    first row: one heading style, 17 px/600 inside every card."""
    outer = group.layout()
    card = group.findChild(W.QFrame, "prefGroupCard")
    if outer is None or card is None or outer.count() < 2:
        return None
    first = outer.itemAt(0)
    if first is None or first.widget() is card:
        return None
    item = outer.takeAt(0)
    head = W.QWidget()
    head.setObjectName("cardHead")
    row = W.QHBoxLayout(head)
    row.setContentsMargins(16, 14, 16, 10)
    row.setSpacing(8)
    if item.widget() is not None:
        row.addWidget(item.widget())
        row.addStretch(1)
    elif item.layout() is not None:
        row.addLayout(item.layout())
    for label in head.findChildren(W.QLabel, "prefGroupHeading"):
        label.setObjectName(title_name)
        label.setText(str(label.property("headingText") or label.text()))
    card.layout().insertWidget(0, head)
    return head
