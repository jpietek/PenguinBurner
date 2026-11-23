"""Native Qt In-Game Overlay page for the UI preview.

The real ``OverlayConfigPanel`` runs under patch with an in-memory
overlay.toml, simulated telemetry and a simulated live-preview window: the
same 1 s reload, the same '-' for missing sensors, the same status line, the
real Keyboard shortcuts dialog (desktop from Prototype controls) and a vkcube
window. Telemetry is live only while the library plays a wrapped game with
its overlay on; otherwise the panel shows its sample readings.
"""

from __future__ import annotations

import math
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from overlay import shortcuts as overlay_shortcuts
from overlay.config import (
    default_overlay_config,
    next_overlay_detail,
    set_overlay_detail,
    set_overlay_item_enabled,
)
from overlay.gnome_shortcuts import GnomeShortcutSetupResult
from ui.components import overlay_config as overlay_ui
from ui.components.library_style import (
    heading_into_card,
    library_stylesheet,
    normalize_group_headings,
)
from ui.components.overlay_preview import (
    CANVAS_H,
    CANVAS_W,
    MAX_PREVIEW_H,
    overlay_preview_class,
)
from ui.components.page_header import PAGE_MARGINS
from ui.design.style import COL, GAP, RAD, TYPE, WEIGHT, focus_visible, qcolor
from ui.dialogs import overlay_shortcuts as shortcuts_ui

CONFIG_PATH = "~/.config/penguin-burner/overlay.toml"
SAVE_FAILED = (
    "Overlay config save failed: [Errno 13] Permission denied: '" + CONFIG_PATH + "'"
)
KEEP_ONE = "Keep at least one overlay item enabled."
LIVE_RUNNING = "Live preview running. It follows these settings as you change them."
LIVE_TITLE = "vkcube · PenguinBurner overlay"
CAPTION_SAMPLE = "Illustrative preview · sample readings"
CAPTION_LIVE = "Live readings from a wrapped game"
KDE_OPENED = (
    "Opened KDE shortcut settings. PenguinBurner's overlay actions are listed there."
)
GNOME_DONE = (
    "Overlay shortcuts are ready in GNOME Settings. Existing key assignments were "
    "preserved. Choose an available key for: Cycle background opacity. Its default "
    "key is already in use."
)
DESKTOPS = ("kde", "kde-fail", "gnome", "other", "other-flatpak")
FLATPAK_CLI = "flatpak run --command=penguin-burner-cli io.github.jpietek.PenguinBurner"

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


def _round(value):
    """JavaScript Math.round, so both renderers simulate the same readings."""
    return math.floor(value + 0.5)


def sample_telemetry(tick, tier="Default"):
    """overlay.js ovTelemetry(): a wrapped game publishing overlay telemetry.

    The hotspot and DLSS 5 sensors are absent, so those rows read '-'.
    """

    def w(amp, period, phase=0):
        return _round(amp * math.sin((tick + phase) * 2 * math.pi / period))

    return {
        "present_fps": str(60 + w(3, 7)),
        "framegen_fps": str(120 + w(6, 7)),
        "framegen_active": "1",
        "latency_ms": str(41 + w(2, 5)),
        "latency_avg_ms": str(39 + w(2, 5, 1)),
        "clock_mhz": str(2820 + w(15, 6)),
        "voltage_mv": "925",
        "power_w": str(318 + w(9, 5)),
        "profile_tier": tier,
        "gpu_util_pct": str(96 + w(2, 4)),
        "cpu_util_pct": str(32 + w(4, 6)),
        "cpu_peak_thread_pct": str(93 + w(4, 3)),
        "fan_pct": "62",
        "temperature_c": str(68 + w(3, 9)),
        "hotspot_temperature_c": "",
        "uv_offset_mv": "-75",
        "mem_clock_mhz": "10501",
        "vram_used_mb": str(9626 + w(90, 8)),
        "vram_total_mb": "16376",
        "ram_used_mb": str(14540 + w(120, 11)),
        "ram_total_mb": "32768",
        "cpu_temp_c": str(62 + w(3, 10)),
        "cpu_power_w": str(88 + w(6, 4)),
    }


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


# A unit cube: eight corners and the six faces between them.
CUBE = [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]
FACES = (
    (0, 1, 3, 2),
    (4, 5, 7, 6),
    (0, 1, 5, 4),
    (2, 3, 7, 6),
    (0, 2, 6, 4),
    (1, 3, 7, 5),
)


class VkcubeCanvas(overlay_preview_class(C, G, W)):
    """The live preview's vkcube scene: a spinning primary cube on plotSurface,
    with the overlay drawn by the app's own HUD painter on top."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMaximumSize(16777215, 16777215)
        self.angle = 35.0
        self.spin = C.QTimer(self)
        self.spin.setInterval(40)
        self.spin.timeout.connect(self._advance)
        self.spin.start()

    def _advance(self):
        self.angle = (self.angle + 1.6) % 360
        self.update()

    def heightForWidth(self, width):
        return round(width * CANVAS_H / CANVAS_W)

    def _paint_scene(self, painter):
        frame = C.QRectF(0, 0, CANVAS_W, CANVAS_H)
        glow = G.QRadialGradient(CANVAS_W / 2, CANVAS_H * 0.45, CANVAS_W * 0.55)
        glow.setColorAt(0, G.QColor(c["control"]))
        glow.setColorAt(0.75, G.QColor(c["plotSurface"]))
        glow.setColorAt(1, G.QColor(c["plotSurface"]))
        painter.fillRect(frame, glow)
        yaw, tilt = math.radians(self.angle), math.radians(-24)
        size, depth = CANVAS_W * 0.1, CANVAS_W * 1.25
        points = []
        for x, y, z in CUBE:
            x, z = (
                x * math.cos(yaw) + z * math.sin(yaw),
                -x * math.sin(yaw) + z * math.cos(yaw),
            )
            y, z = (
                y * math.cos(tilt) - z * math.sin(tilt),
                y * math.sin(tilt) + z * math.cos(tilt),
            )
            k = depth / (depth + z * size)
            points.append((CANVAS_W / 2 + x * size * k, CANVAS_H / 2 + y * size * k, z))
        painter.save()
        painter.setOpacity(0.92)
        pen = G.QPen(G.QColor(c["primaryBorder"]), 6)
        pen.setJoinStyle(C.Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        for face in sorted(FACES, key=lambda f: -sum(points[i][2] for i in f)):
            polygon = G.QPolygonF([C.QPointF(points[i][0], points[i][1]) for i in face])
            box = polygon.boundingRect()
            fill = G.QLinearGradient(box.topLeft(), box.bottomRight())
            fill.setColorAt(0, G.QColor(c["primaryHover"]))
            fill.setColorAt(1, G.QColor(c["primaryPressed"]))
            painter.setBrush(fill)
            painter.drawPolygon(polygon)
        painter.restore()


class FixtureOverlay(overlay_ui.OverlayConfigPanel):
    """The real panel with an in-memory overlay.toml and simulated runtime."""

    def __init__(self):
        self.sample_dialog = None
        self.sample_canvas = None
        self.art_path = None
        self.game_name = ""
        self.tick = 0
        # A wrapped game with its overlay on is publishing telemetry (the
        # library's session sets this); otherwise the panel shows samples.
        self.publishing = False
        self.running_tier = "Default"
        self.save_failure = False
        self.vkcube_missing = False
        self.desktop = "kde"
        with (
            patch.object(
                overlay_ui,
                "load_overlay_config",
                return_value=default_overlay_config(enabled=True),
            ),
            patch.object(
                overlay_ui, "overlay_preview_class", return_value=GameArtworkPreview
            ),
        ):
            super().__init__(QtCore=C, QtWidgets=W)
        # `disk` stands for overlay.toml: shortcuts and the CLI write it, and
        # the panel's 1 s timer pulls it back into the controls.
        self.disk = self.config
        self._restyle()
        self.refresh_preview()

    # -- page structure ------------------------------------------------------

    def _restyle(self):
        """One heading style: title-case 17px/600 inside each bordered card."""
        root = self.widget
        title = root.findChild(W.QLabel, "overlayPageTitle")
        if title is not None:
            title.setText("In-Game Overlay")
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
        # Scale 100%; overlay.css sizes the browser frame the same way.
        self.preview.setMaximumSize(16777215, MAX_PREVIEW_H)
        for group in [*self._settings_groups(), *self.metric_groups]:
            self._heading_into_card(group)
        for info in self.metric_infos.values():
            info.setFixedSize(20, 20)
        # Rows share the card heading's 16 px edge, as in overlay.css.
        for row in self.page.findChildren(W.QFrame, "prefRow"):
            margins = row.layout().contentsMargins()
            row.layout().setContentsMargins(16, margins.top(), 16, margins.bottom())
        behaviour = self._settings_groups()[-1].findChild(W.QFrame, "prefGroupCard")
        note = W.QLabel(
            "Changes save to overlay.toml right away, and running games update live."
        )
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

    @staticmethod
    def _heading_into_card(group):
        heading_into_card(group, "overlayGroupTitle")

    def _reflow_columns(self, width):
        """A wide settings column beside the metric groups, like overlay.css."""
        mode = 2 if width >= 880 else 1
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
            # The second metric column takes the slack, like `auto 1fr`.
            self.grid.setRowStretch(1, 1)
        else:
            self.grid.addWidget(self.appearance, 0, 0, top)
            self.grid.addWidget(self.metrics_a, 1, 0, top)
            self.grid.addWidget(self.metrics_b, 2, 0, top)
            self.grid.setColumnStretch(0, 1)

    # -- simulated state -----------------------------------------------------

    def set_prototype(self, state):
        """Overlay switches from Prototype controls."""
        self.vkcube_missing = bool(state.get("livePreviewMissing", self.vkcube_missing))
        self.save_failure = bool(state.get("overlaySaveFailure", self.save_failure))
        if state.get("overlayDesktop") in DESKTOPS:
            self.desktop = state["overlayDesktop"]
        self.refresh_preview()

    def set_game(self, game):
        path = None
        if game is not None and game.launcher == "steam":
            candidate = Path(__file__).with_name("artwork") / f"{game.game_id}art.webp"
            if candidate.is_file():
                path = candidate
        self.game_name = game.name if game is not None and path is not None else ""
        self.preview.setAccessibleName(
            f"{self.game_name or 'Illustrative'} overlay preview"
        )
        if path == self.art_path:
            return
        self.art_path = path
        self.preview.set_artwork(G.QPixmap(str(path)) if path else G.QPixmap())

    def set_publishing(self, publishing):
        """A wrapped game with its overlay on started (True) or ended."""
        if self.publishing != bool(publishing):
            self.publishing = bool(publishing)
            self.refresh_preview(advance=False)

    def press_cycle_shortcut(self):
        """Alt+C in a game: the CLI rewrites overlay.toml's preset; the
        panel's 1 s re-sync pulls it into the controls."""
        self.disk = set_overlay_detail(self.disk, next_overlay_detail(self.disk.detail))

    def telemetry(self):
        return sample_telemetry(self.tick, self.running_tier) if self.publishing else {}

    # -- saving --------------------------------------------------------------

    def _update_setting(self, setter, value):
        if not self._syncing:
            self.config = setter(self.config, value)
            self._save()

    def _set_item_enabled(self, item_id, enabled):
        if self._syncing:
            return
        after = set_overlay_item_enabled(self.config, item_id, bool(enabled))
        if after.enabled_item_ids == self.config.enabled_item_ids and not enabled:
            self._show_status(KEEP_ONE)
            self._sync_widgets()
            return
        self.config = after
        self._save()

    def _save(self):
        if self.save_failure:
            self.config = self.disk
            self._sync_widgets()
            self._show_status(SAVE_FAILED, error=True)
            self.preview.set_config(self.config)
            return
        self.disk = self.config
        if self.status_label.text() != LIVE_RUNNING or self.sample_dialog is None:
            self._show_status("")
        self._sync_widgets()
        self.refresh_preview(advance=False)

    def _show_status(self, message, *, error=False):
        super()._show_status(message, error=error)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    # -- the 1 s refresh -----------------------------------------------------

    def refresh_preview(self, advance=True):
        if not hasattr(self, "disk"):
            # The base constructor's first refresh, before the fixture is ready.
            return
        if advance:
            self.tick += 1
        if self.disk != self.config:
            self.config = self.disk
            self._sync_widgets()
        telemetry = self.telemetry()
        values = overlay_ui._preview_values(telemetry)
        self.preview.set_config(self.config)
        self.preview.set_values(values)
        if self.sample_canvas is not None:
            self.sample_canvas.set_config(self.config)
            self.sample_canvas.set_values(values)
        for key, label in self.item_value_labels.items():
            label.setText(overlay_ui._sample_text(key, telemetry))
        caption = self.widget.findChild(W.QLabel, "overlayPreviewCaption")
        if caption is not None:
            caption.setText(CAPTION_LIVE if self.publishing else CAPTION_SAMPLE)

    # -- live preview: a separate vkcube window running the real layer --------

    def _toggle_live_preview(self):
        if self.sample_dialog is not None:
            self.sample_dialog.close()
            return
        if self.vkcube_missing:
            self._show_status(overlay_ui.MISSING_EXECUTABLE_MESSAGE)
            return
        dlg = W.QDialog(self.widget)
        dlg.setObjectName("overlayLiveWindow")
        dlg.setWindowTitle(LIVE_TITLE)
        dlg.setAttribute(C.Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = W.QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        self.sample_canvas = VkcubeCanvas(dlg)
        self.sample_canvas.setAccessibleName(LIVE_TITLE)
        self.sample_canvas.setMinimumSize(560, 315)
        layout.addWidget(self.sample_canvas)
        dlg.resize(560, 315)
        dlg.finished.connect(self._preview_closed)
        self.sample_dialog = dlg
        self.live_button.setText(overlay_ui.LIVE_PREVIEW_STOP)
        self._show_status(LIVE_RUNNING)
        self.refresh_preview(advance=False)
        dlg.show()

    def _preview_closed(self, *_):
        self.sample_dialog = None
        self.sample_canvas = None
        self.live_button.setText(overlay_ui.LIVE_PREVIEW_START)
        if self.status_label.text() == LIVE_RUNNING:
            self._show_status("")

    # -- keyboard shortcuts --------------------------------------------------

    def _show_shortcuts(self):
        """The real handler and dialog, with the desktop from Prototype
        controls. On KDE it opens KDE shortcut settings directly; the dialog
        appears only when that fails, or on other desktops. Nothing here
        starts a host process or writes desktop settings."""
        desktop = self.desktop
        kind = "kde" if desktop.startswith("kde") else desktop.split("-")[0]
        with ExitStack() as stack:
            for target in (overlay_ui, shortcuts_ui, overlay_shortcuts):
                stack.enter_context(
                    patch.object(target, "shortcut_desktop", return_value=kind)
                )
            stack.enter_context(
                patch.object(overlay_ui, "repair_user_desktop_entry", return_value=None)
            )
            stack.enter_context(
                patch.object(
                    overlay_ui, "start_on_host", return_value=desktop == "kde"
                )
            )
            stack.enter_context(
                patch.object(
                    overlay_shortcuts,
                    "running_in_flatpak",
                    return_value=desktop == "other-flatpak",
                )
            )
            stack.enter_context(
                patch.object(overlay_shortcuts.shutil, "which", return_value=None)
            )
            before = getattr(self, "_shortcuts_dialog", None)
            super()._show_shortcuts()
        dialog = getattr(self, "_shortcuts_dialog", None)
        if dialog is None or dialog is before:
            if desktop == "kde":
                self._show_status(KDE_OPENED)
            return
        sample_shortcuts_dialog(dialog, kind)


def sample_shortcuts_dialog(dialog, desktop):
    """Restyle the real dialog, and answer its desktop actions with sample
    results instead of starting host processes."""
    dialog.setProperty("sampleReady", True)
    dialog.setStyleSheet(focus_visible(dialog_stylesheet()))
    labels = dialog.findChildren(W.QLabel)
    note = next(
        (
            label
            for label in reversed(labels)
            if label.text().startswith(("The game must", "Setup adds"))
        ),
        None,
    )
    settings = dialog.findChild(W.QPushButton, "overlayShortcutSettings")
    if settings is not None and note is not None:
        settings.clicked.disconnect()
        settings.clicked.connect(
            lambda: note.setText(
                "Opened GNOME Settings → Keyboard → Custom Shortcuts."
                if desktop == "gnome"
                else "Opened KDE shortcut settings."
            )
        )
    setup = dialog.findChild(W.QPushButton, "overlayShortcutSetup")
    if setup is not None and note is not None:
        setup.clicked.disconnect()
        result = GnomeShortcutSetupResult(success=True, message=GNOME_DONE)

        def finish():
            setup.setEnabled(True)
            setup.setText("Set up GNOME shortcuts")
            note.setText(result.message)

        def begin():
            setup.setEnabled(False)
            setup.setText("Setting up…")
            note.setText("Registering overlay shortcuts in GNOME…")
            C.QTimer.singleShot(900, dialog, finish)

        setup.clicked.connect(begin)


class OverlayPage(W.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = FixtureOverlay()
        self.setStyleSheet(focus_visible(library_stylesheet() + page_stylesheet()))
        layout.addWidget(self.panel.widget)
        normalize_group_headings(self.panel.widget)
        # The panel's 1 s re-sync runs only while this page is shown.
        self.panel.timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self.panel.refresh_preview(advance=False)
        self.panel.timer.start(1000)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.panel.timer.stop()
