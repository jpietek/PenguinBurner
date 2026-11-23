"""Native Qt In-Game Overlay page for the UI preview.

The real page (``ui/features/overlay/overlay_page.py``) runs under patch
with an in-memory overlay.toml, simulated telemetry and a simulated
live-preview window: the same 1 s reload, the same '-' for missing sensors,
the same status line, the real Keyboard shortcuts dialog (desktop from
Prototype controls) and a vkcube window. Telemetry is live only while the
library plays a wrapped game with its overlay on; otherwise the panel shows
its sample readings.
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
from ui.components.overlay_preview import CANVAS_H, CANVAS_W, overlay_preview_class
from ui.design.style import COL
from ui.dialogs import overlay_shortcuts as shortcuts_ui
from ui.features.overlay import overlay_page
from ui.features.overlay.overlay_page import OverlayPanel

CONFIG_PATH = "~/.config/penguin-burner/overlay.toml"
SAVE_FAILED = (
    "Overlay config save failed: [Errno 13] Permission denied: '" + CONFIG_PATH + "'"
)
KEEP_ONE = "Keep at least one overlay item enabled."
LIVE_RUNNING = overlay_ui.LIVE_PREVIEW_RUNNING
LIVE_TITLE = "vkcube · PenguinBurner overlay"
# The real page's captions and status lines, which the mock tests read here.
CAPTION_SAMPLE = overlay_page.CAPTION_SAMPLE
CAPTION_LIVE = overlay_page.CAPTION_LIVE
KDE_OPENED = overlay_page.KDE_OPENED
GNOME_DONE = (
    "Overlay shortcuts are ready in GNOME Settings. Existing key assignments were "
    "preserved. Choose an available key for: Cycle background opacity. Its default "
    "key is already in use."
)
DESKTOPS = ("kde", "kde-fail", "gnome", "other", "other-flatpak")
FLATPAK_CLI = "flatpak run --command=penguin-burner-cli io.github.jpietek.PenguinBurner"

c = COL


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


class FixtureOverlay(OverlayPanel):
    """The real page panel with an in-memory overlay.toml and simulated runtime."""

    def __init__(self):
        self.sample_dialog = None
        self.sample_canvas = None
        self.tick = 0
        # A wrapped game with its overlay on is publishing telemetry (the
        # library's session sets this); otherwise the panel shows samples.
        self.publishing = False
        self.save_failure = False
        self.vkcube_missing = False
        self.desktop = "kde"
        with patch.object(
            overlay_ui,
            "load_overlay_config",
            return_value=default_overlay_config(enabled=True),
        ):
            super().__init__()
        self.running_tier = "Default"
        # `disk` stands for overlay.toml: shortcuts and the CLI write it, and
        # the panel's 1 s timer pulls it back into the controls.
        self.disk = self.config
        self.refresh_preview()

    # -- simulated state -----------------------------------------------------

    def set_prototype(self, state):
        """Overlay switches from Prototype controls."""
        self.vkcube_missing = bool(state.get("livePreviewMissing", self.vkcube_missing))
        self.save_failure = bool(state.get("overlaySaveFailure", self.save_failure))
        if state.get("overlayDesktop") in DESKTOPS:
            self.desktop = state["overlayDesktop"]
        self.refresh_preview()

    def artwork_path(self, game):
        """The sample's bundled wide art in place of Steam's cache."""
        if game is not None and game.launcher == "steam":
            candidate = Path(__file__).with_name("artwork") / f"{game.game_id}art.webp"
            if candidate.is_file():
                return candidate
        return None

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
        self._show_status(LIVE_RUNNING if self.sample_dialog is not None else "")
        self._sync_widgets()
        self.refresh_preview(advance=False)

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
        self._show_readings(self.telemetry())

    def _show_readings(self, telemetry):
        super()._show_readings(telemetry)
        if self.sample_canvas is not None:
            self.sample_canvas.set_config(self.config)
            self.sample_canvas.set_values(self.preview._values)

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
        self._clear_live_preview_status()

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
        if dialog is not None and dialog is not before:
            sample_shortcuts_dialog(dialog, kind)


def sample_shortcuts_dialog(dialog, desktop):
    """Restyle the real dialog, and answer its desktop actions with sample
    results instead of starting host processes."""
    dialog.setProperty("sampleReady", True)
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


class OverlayPage(overlay_page.OverlayPage):
    def __init__(self, parent=None):
        super().__init__(FixtureOverlay(), parent)
