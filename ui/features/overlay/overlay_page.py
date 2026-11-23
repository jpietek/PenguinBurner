"""The In-Game Overlay page: the overlay settings panel in the card layout
every page shares, with the selected game's wide artwork behind the HUD
preview.

Everything the overlay tab does runs through ``OverlayConfigPanel`` as in
the classic window: overlay.toml loaded and saved on each explicit edit, the
1 s re-sync that picks up shortcut and CLI edits, the telemetry a wrapped game
publishes, the vkcube live preview and the Keyboard shortcuts dialog. This
module lays it out, and takes the selected game and the running tier from the
other pages (``set_game``, ``set_running_tier``).
"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from integrations.launchers.library import LibraryGame
from integrations.steam.library import game_hero_path
from integrations.steam.users import default_steam_root
from ui.components.card_style import (
    heading_into_card,
    library_stylesheet,
    normalize_group_headings,
)
from ui.components.overlay_config import (
    OverlayConfigPanel,
    _preview_values,
    _sample_text,
)
from ui.components.overlay_preview import (
    CANVAS_H,
    CANVAS_W,
    MAX_PREVIEW_H,
    overlay_preview_class,
)
from ui.components.page_header import PAGE_MARGINS
from ui.design.style import COL, GAP, RAD, TYPE, WEIGHT, focus_visible, qcolor

PAGE_TITLE = "In-Game Overlay"
CAPTION_SAMPLE = "Illustrative preview · sample readings"
CAPTION_LIVE = "Live readings from a wrapped game"
# The daemon publishes GPU and host readings with no game running; frame
# rates and latency then stay samples.
CAPTION_GPU = "Live GPU readings · sample frame rates"
SAVE_NOTE = "Changes save to overlay.toml right away, and running games update live."
KDE_OPENED = (
    "Opened KDE shortcut settings. PenguinBurner's overlay actions are listed there."
)
# Below this page width the metric groups stack under the settings column.
TWO_COLUMN_MIN_W = 880

c = COL


def page_stylesheet() -> str:
    return f"""
QLabel#overlayPreviewTitle, QLabel#overlayGroupTitle {{
    font-size: {TYPE["section"]}px; font-weight: {WEIGHT["semibold"]}; color: {c["ink"]};
}}
QWidget#overlayCardHead, QWidget#cardHead {{ background: transparent; }}
QLabel#overlayEmptyMetrics {{
    font-size: {TYPE["caption"]}px; color: {c["secondary"]}; padding: 2px 16px 16px;
}}
QLabel#overlayNote {{
    font-size: {TYPE["caption"]}px; color: {c["secondary"]}; padding: 10px 16px 14px;
    border-top: 1px solid {c["divider"]};
}}
QLabel#overlayConfigStatus {{
    background: {c["raised"]}; border: 1px solid {c["border"]};
    border-left: 2px solid {c["accent"]}; border-radius: {RAD["control"]}px;
    padding: 8px 12px; color: {c["ink"]};
}}
QLabel#overlayConfigStatus[error="true"] {{ color: {c["loss"]}; border-left-color: {c["loss"]}; }}
QToolButton#infoButton {{
    padding: 0; min-width: 20px; min-height: 20px; max-width: 20px; max-height: 20px;
    border: 1px solid {c["border"]}; border-radius: 10px; background: transparent;
    color: {c["secondary"]}; font-size: {TYPE["caption"]}px; font-weight: {WEIGHT["semibold"]};
}}
QToolButton#infoButton:hover {{ color: {c["ink"]}; border-color: {c["quiet"]}; background: {c["controlHover"]}; }}
QToolButton#infoButton:pressed, QToolButton#infoButton:checked {{
    color: {c["ink"]}; background: {c["selection"]}; border-color: {c["accent"]};
}}
"""


def dialog_stylesheet() -> str:
    return f"""
QDialog#overlayShortcutsDialog, QDialog#overlayShortcutsDialog QScrollArea,
QDialog#overlayShortcutsDialog QScrollArea > QWidget > QWidget {{ background: {c["window"]}; border: none; }}
QDialog#overlayShortcutsDialog QLabel {{ color: {c["ink"]}; font-size: {TYPE["body"]}px; }}
QLabel#overlayShortcutError {{
    background: {c["raised"]}; border: 1px solid {c["border"]}; border-left: 2px solid {c["loss"]};
    border-radius: {RAD["control"]}px; padding: 8px 12px; color: {c["loss"]};
}}
QDialog#overlayShortcutsDialog QLineEdit {{
    font-family: "DejaVu Sans Mono", monospace; font-size: {TYPE["caption"]}px;
    color: {c["secondary"]}; background: {c["raised"]};
}}
"""


class GameArtworkPreview(overlay_preview_class(C, G, W)):
    """Keep the app's native HUD painter, replacing its 16:9 scenery only."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.artwork = G.QPixmap()

    def set_artwork(self, artwork):
        self.artwork = artwork
        self.update()

    def _paint_scene(self, painter):
        frame = C.QRectF(0, 0, CANVAS_W, CANVAS_H)
        if self.artwork.isNull():
            # A token backdrop (the app's stand-in scene is purple-blue).
            backdrop = G.QLinearGradient(0, 0, 0, CANVAS_H)
            backdrop.setColorAt(0, qcolor("raised"))
            backdrop.setColorAt(1, qcolor("plotSurface"))
            painter.fillRect(frame, backdrop)
            return
        source = C.QRectF(self.artwork.rect())
        scale = max(frame.width() / source.width(), frame.height() / source.height())
        source.setSize(C.QSizeF(frame.width() / scale, frame.height() / scale))
        source.moveCenter(C.QRectF(self.artwork.rect()).center())
        painter.save()
        painter.setRenderHint(G.QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(frame, self.artwork, source)
        shade = G.QLinearGradient(0, 0, 0, CANVAS_H)
        shade.setColorAt(0, qcolor("window", 0.4))
        shade.setColorAt(1, qcolor("panel", 0.73))
        painter.fillRect(frame, shade)
        painter.restore()


def steam_hero_artwork(game: LibraryGame | None) -> Path | None:
    """The wide art Steam cached for a Steam game; other launchers have none."""
    if game is None or game.launcher != "steam":
        return None
    return game_hero_path(game.game_id, steam_root=default_steam_root())


def preview_caption(telemetry: dict[str, str]) -> str:
    if str(telemetry.get("present_fps") or "").strip():
        return CAPTION_LIVE
    return CAPTION_GPU if telemetry else CAPTION_SAMPLE


class OverlayPanel(OverlayConfigPanel):
    """The overlay settings panel in the page's card layout.

    ``running_tier`` is the tier the other pages report as running (Adaptive's
    live tier, the running profile's tier, or "Default" at stock). Empty means
    unknown: the HUD preview then keeps its sample tier, as the classic tab
    does. A tier published by a wrapped game always wins.
    """

    def __init__(self, *, config_path: str | Path | None = None):
        self.running_tier = ""
        self.art_path: Path | None = None
        self.game_name = ""
        super().__init__(QtCore=C, QtWidgets=W, config_path=config_path)
        self._restyle()

    def _preview_widget_class(self):
        return GameArtworkPreview

    # -- page structure ------------------------------------------------------

    def _restyle(self):
        """One heading style: title-case 17px/600 inside each bordered card."""
        root = self.widget
        title = root.findChild(W.QLabel, "overlayPageTitle")
        if title is not None:
            title.setText(PAGE_TITLE)
        card = root.findChild(W.QFrame, "overlayPreviewCard")
        heading = next(
            (
                label
                for label in card.findChildren(W.QLabel, "prefRowTitle")
                if label.text() == "Preview"
            ),
            None,
        )
        if heading is not None:
            heading.setObjectName("overlayPreviewTitle")
        card.layout().setContentsMargins(16, 14, 16, 16)
        card.layout().setSpacing(12)
        # Up to a 640x360 frame (centred by the painter), so the HUD reads at
        # Scale 100%.
        self.preview.setMaximumSize(16777215, MAX_PREVIEW_H)
        self.preview.setAccessibleName("Illustrative overlay preview")
        for group in [*self._settings_groups(), *self.metric_groups]:
            heading_into_card(group, "overlayGroupTitle")
        for info in self.metric_infos.values():
            info.setFixedSize(20, 20)
        # Rows share the card heading's 16 px edge.
        for row in self.page.findChildren(W.QFrame, "prefRow"):
            margins = row.layout().contentsMargins()
            row.layout().setContentsMargins(16, margins.top(), 16, margins.bottom())
        behaviour = self._settings_groups()[-1].findChild(W.QFrame, "prefGroupCard")
        note = W.QLabel(SAVE_NOTE)
        note.setObjectName("overlayNote")
        note.setWordWrap(True)
        behaviour.layout().addWidget(note)
        # One page structure on every tab: the header (title, subtitle,
        # actions) and the status line scroll with the cards, inside the
        # shared page margins.
        outer = root.layout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = outer.takeAt(0).layout()
        outer.removeWidget(self.status_label)
        scroll = self._options_scroll
        grid_page = scroll.takeWidget()
        content = W.QWidget()
        content.setObjectName("overlayPage")
        column = W.QVBoxLayout(content)
        column.setContentsMargins(*PAGE_MARGINS)
        column.setSpacing(GAP["md"])
        column.addLayout(header)
        column.addWidget(self.status_label)
        column.addSpacing(GAP["xs"])
        column.addWidget(grid_page, 1)
        grid_page.setObjectName("overlayGrid")
        self.grid.setContentsMargins(0, 0, 0, 0)
        scroll.setWidget(content)
        self._layout_columns = 0
        self._reflow_columns(max(root.width(), 1300))

    def _settings_groups(self):
        return [
            group
            for group in self.appearance.findChildren(W.QWidget)
            if group.parentWidget() is self.appearance
            and group.findChild(W.QFrame, "prefGroupCard") is not None
        ]

    def _reflow_columns(self, width):
        """A wide settings column beside the metric groups."""
        mode = 2 if width >= TWO_COLUMN_MIN_W else 1
        if mode == self._layout_columns:
            return
        self._layout_columns = mode
        for widget in (self.appearance, self.metrics_a, self.metrics_b):
            self.grid.removeWidget(widget)
        for i in range(3):
            self.grid.setColumnStretch(i, 0)
            self.grid.setRowStretch(i, 0)
        top = C.Qt.AlignmentFlag.AlignTop
        if mode == 2:
            self.grid.addWidget(self.appearance, 0, 0, 2, 1, top)
            self.grid.addWidget(self.metrics_a, 0, 1, top)
            self.grid.addWidget(self.metrics_b, 1, 1, top)
            self.grid.setColumnStretch(0, 13)
            self.grid.setColumnStretch(1, 10)
            # The second metric column takes the slack.
            self.grid.setRowStretch(1, 1)
        else:
            self.grid.addWidget(self.appearance, 0, 0, top)
            self.grid.addWidget(self.metrics_a, 1, 0, top)
            self.grid.addWidget(self.metrics_b, 2, 0, top)
            self.grid.setColumnStretch(0, 1)

    # -- what the other pages feed in -----------------------------------------

    def artwork_path(self, game: LibraryGame | None) -> Path | None:
        return steam_hero_artwork(game)

    def set_game(self, game: LibraryGame | None) -> None:
        """Game Library's selection: its wide artwork behind the preview."""
        path = self.artwork_path(game)
        self.game_name = game.name if game is not None and path is not None else ""
        self.preview.setAccessibleName(
            f"{self.game_name or 'Illustrative'} overlay preview"
        )
        if path == self.art_path:
            return
        self.art_path = path
        self.preview.set_artwork(G.QPixmap(str(path)) if path else G.QPixmap())

    def set_running_tier(self, tier: str | None) -> None:
        tier = str(tier or "")
        if tier != self.running_tier:
            self.running_tier = tier
            self.refresh_preview()

    # -- readings ---------------------------------------------------------------

    def _show_readings(self, telemetry: dict[str, str]) -> None:
        super()._show_readings(telemetry)
        caption = self.widget.findChild(W.QLabel, "overlayPreviewCaption")
        if caption is not None:
            caption.setText(preview_caption(telemetry))
        tier = self.running_tier
        if not tier or str(telemetry.get("profile_tier") or "").strip():
            return
        # The running tier is a real reading even with no game publishing;
        # the other sample readings stay as they are.
        values = _preview_values(telemetry)
        values["profile_tier"] = tier
        self.preview.set_values(values)
        self.item_value_labels["profile"].setText(
            _sample_text("profile", {**telemetry, "profile_tier": tier})
        )

    def _show_status(self, message: str, *, error: bool = False):
        super()._show_status(message, error=error)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    # -- keyboard shortcuts -----------------------------------------------------

    def _show_shortcuts(self):
        """The classic flow: KDE opens its own shortcut settings; otherwise,
        or when that fails, the shortcuts dialog."""
        before = getattr(self, "_shortcuts_dialog", None)
        super()._show_shortcuts()
        dialog = getattr(self, "_shortcuts_dialog", None)
        if dialog is None or dialog is before:
            self._show_status(KDE_OPENED)
            return
        dialog.setStyleSheet(focus_visible(dialog_stylesheet()))


class OverlayPage(W.QWidget):
    """The In-Game Overlay tab. ``panel`` exposes ``set_game`` and
    ``set_running_tier`` for the Game Library and Profiles pages."""

    def __init__(self, panel: OverlayPanel | None = None, parent=None):
        super().__init__(parent)
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = panel if panel is not None else OverlayPanel()
        self.setStyleSheet(focus_visible(library_stylesheet() + page_stylesheet()))
        layout.addWidget(self.panel.widget)
        normalize_group_headings(self.panel.widget)
        # The panel's 1 s re-sync runs only while this page is shown.
        self.panel.timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self.panel.refresh_preview()
        self.panel.timer.start(1000)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.panel.timer.stop()
