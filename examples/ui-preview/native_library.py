"""Native library and overlay views with session-local game and telemetry fixtures."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from integrations.launchers.library import (
    FIELD_CHOICE,
    FIELD_MULTILINE,
    FIELD_TEXT,
    GROUP_IN_GAME,
    LauncherField,
    LauncherWriteState,
    LibraryGame,
    library_bulk_actions,
)
from overlay.config import default_overlay_config, set_overlay_item_enabled
from overlay.shortcuts import SHORTCUTS
from ui.assets import asset_image_path
from ui.components import game_library_panel as library_ui
from ui.components import overlay_config as overlay_ui
from ui.components.game_library_panel import GameLibraryPanel, game_key
from ui.components.overlay_preview import CANVAS_H, CANVAS_W, overlay_preview_class
from ui.components.preference_rows import preference_row
from ui.components.toggle_switch import make_toggle_switch

GAMES = (
    ("steam", "1091500", "Cyberpunk 2077", "Proton Experimental", 40, True),
    (
        "steam",
        "2531310",
        "The Last of Us Part II Remastered",
        "Proton Experimental",
        23,
        True,
    ),
    ("steam", "1888930", "The Last of Us Part I", "Proton Experimental", 61, True),
    ("steam", "782330", "DOOM Eternal", "Proton Experimental", 15, True),
    ("heroic", "control", "Control", "Epic Games · Wine-GE", 19, False),
    ("lutris", "dishonored", "Dishonored", "Wine", 7, True),
    ("faugus", "hollow-knight", "Hollow Knight", "Proton", 6, False),
)
DEFAULT_TARGET_FPS = 60
KEYS = [f"{launcher}:{ident}" for launcher, ident, *_ in GAMES]
NAMES = {"steam": "Steam", "heroic": "Heroic", "lutris": "Lutris", "faugus": "Faugus"}
COMMANDS = {
    "steam": "Steam launch options — %command% is where the game goes",
    "heroic": "Wrapper command in the Heroic settings",
    "lutris": "prefix_command in the Lutris config",
    "faugus": "Launch arguments in the Faugus game settings",
}
DESIGN = json.loads(Path(__file__).with_name("pburn-design.json").read_text())
c = SimpleNamespace(**DESIGN["colors"])
t = SimpleNamespace(**DESIGN["typography"])
sp = SimpleNamespace(**DESIGN["spacing"])
r = SimpleNamespace(**DESIGN["radii"])
FONTS = f"""
QLabel, QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QPushButton, QToolButton, QMenu, QListWidget {{
    font-family: "{t.family}"; font-size: {t.body}px; color: {c.ink};
}}
QLabel#gameTitle, QLabel#overlayPageTitle {{ font-size: {t.title}px; font-weight: 700; color: {c.ink}; }}
QLabel#libraryPaneTitle, QLabel#prefGroupHeading, QLabel#historyHeading {{
    font-size: {t.section}px; font-weight: 600; color: {c.ink};
}}
QLabel#prefRowTitle {{ font-size: {t.body}px; color: {c.ink}; }}
QLabel#prefRowTitle:disabled, QLabel#prefRowSubtitle:disabled {{ color: {c.quiet}; }}
QLabel#prefRowSubtitle, QLabel#gameMetadata, QLabel#overlayMuted, QLabel#overlaySampleValue,
QLabel#libraryLabel, QLabel#libraryStatus, QLabel#gameTargetFollow, QLabel#overlayMetricCount,
QLabel#overlayPreviewCaption, QLabel#libraryPlaceholder {{ font-size: {t.caption}px; color: {c.secondary}; }}
QWidget#overlayConfigPanel, QWidget#overlayPage, QFrame#gameDetailsPane {{ background: {c.window}; }}
QFrame#libraryPane, QFrame#prefGroupCard, QFrame#overlayPreviewCard, QFrame#historyEntry {{
    background: {c.panel}; border: 1px solid {c.border}; border-radius: {r.panel}px;
}}
QFrame#prefRow[hasSeparator="true"] {{ border-top: 1px solid {c.divider}; }}
QComboBox, QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QPushButton, QToolButton {{
    background: {c.control}; border: 1px solid {c.border}; border-radius: {r.control}px; padding: {sp.xs}px {sp.sm}px;
}}
QPushButton#historyOpen, QPushButton#gamePlayButton, QPushButton#overlayLivePreviewButton {{
    background: {c.primary}; color: {c.onPrimary}; font-size: {t.body}px; padding: 6px {sp.md}px;
}}
QPushButton#historyOpen:hover, QPushButton#gamePlayButton:hover, QPushButton#overlayLivePreviewButton:hover {{
    background: {c.primaryHover};
}}
QPushButton#overlayEditMetrics {{ background: transparent; color: {c.accent}; border: none; font-size: {t.caption}px; }}
QPushButton#gamePlayButton[playState="running"] {{ background: {c.raised}; color: {c.loss}; }}
QPushButton:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ color: {c.quiet}; }}
QComboBox:focus, QPushButton:focus, QLineEdit:focus, QPlainTextEdit:focus, QToolButton:focus {{ border-color: {c.focus}; }}
QListWidget#gameList {{ background: {c.panel}; border: none; }}
QListWidget#gameList::item:selected, QComboBox QAbstractItemView::item:selected, QMenu::item:selected {{
    background: {c.selection}; color: {c.onPrimary};
}}
QDialog {{ background: {c.window}; color: {c.ink}; }}
"""


def normalize_group_headings(widget):
    """Restore normal casing and tracking to section-sized native headings."""
    for label in widget.findChildren(W.QLabel, "prefGroupHeading"):
        label.setText(str(label.property("headingText") or label.text()))
        font = label.font()
        font.setLetterSpacing(G.QFont.SpacingType.AbsoluteSpacing, 0)
        label.setFont(font)


@dataclass
class Setting:
    enabled: bool = True
    overlay: bool = True
    mode: str = "stock"
    target_fps: float | None = None
    telemetry: bool = True
    gpu_uuid: str = ""
    compatibility: str = ""
    command: str | None = None


class Source:
    can_launch = True

    def __init__(self, name):
        self.launcher_id = name
        self.display_name = NAMES[name]
        self.icon_asset = f"tab-{name}.png"
        self.rows = {}
        for i, (launcher, ident, title, runner, hours, wrapped) in enumerate(GAMES):
            if launcher == name:
                setting = Setting(enabled=wrapped, telemetry=i != 2)
                cover = Path(__file__).with_name("artwork") / f"{ident}cover.webp"
                self.rows[ident] = LibraryGame(
                    launcher,
                    ident,
                    title,
                    last_played=1000 - i,
                    installed_at=2000 + i,
                    playtime_hours=hours,
                    subtitle=runner,
                    enabled=wrapped,
                    wrapped=wrapped,
                    art_path=cover
                    if cover.is_file()
                    else asset_image_path(self.icon_asset),
                    overlay=True,
                    detail=SimpleNamespace(setting=setting),
                )

    def available(self):
        return True

    def games(self):
        return list(self.rows.values())

    def bulk_actions(self):
        return library_bulk_actions()

    def write_state(self):
        return LauncherWriteState(summary="Example library")

    def fields(self, game):
        s = game.detail.setting
        default = {
            "steam": "Steam default",
            "heroic": "Heroic default",
            "lutris": "Lutris default",
            "faugus": "Faugus default",
        }[self.launcher_id]
        choices = (
            ("", default),
            ("proton-exp", "Proton Experimental"),
            ("wine-ge", "Wine-GE"),
        )
        wrapper = "PENGUIN_BURNER --pb-overlay=" + ("1" if s.overlay else "0")
        command = s.command
        if command is None:
            command = wrapper if s.enabled else ""
            if self.launcher_id == "steam":
                command = (command + " %command%").strip()
            elif s.enabled:
                command += " --pb-game-id=" + game_key(game)
        return (
            LauncherField(
                "compat_tool",
                FIELD_CHOICE,
                "Compatibility tool",
                "Choose the runner used on the next launch.",
                "set_compatibility",
                s.compatibility,
                choices,
            ),
            LauncherField(
                self.launcher_id + "_command",
                FIELD_MULTILINE if self.launcher_id == "steam" else FIELD_TEXT,
                "Command",
                COMMANDS[self.launcher_id],
                "set_command",
                command,
            ),
        )

    def edit(self, game, method, value):
        attr = {
            "set_game_enabled": "enabled",
            "set_game_overlay": "overlay",
            "set_game_mode": "mode",
            "set_game_target_fps": "target_fps",
            "set_game_gpu": "gpu_uuid",
            "set_game_telemetry": "telemetry",
            "set_compatibility": "compatibility",
            "set_command": "command",
        }[method]
        setting = game.detail.setting
        setattr(setting, attr, value)
        self.rows[game.game_id] = replace(
            game,
            enabled=setting.enabled,
            wrapped=setting.enabled,
            overlay=setting.overlay,
        )


class FixturePanel(GameLibraryPanel):
    def __init__(self, owner):
        self.owner = owner
        self.eligible = False
        self.playing = ""
        super().__init__(
            QtCore=C,
            QtGui=G,
            QtWidgets=W,
            sources=[Source(name) for name in NAMES],
            gpu_choices=lambda: (),
            adaptive_available=lambda *_: self.eligible,
        )
        self._apply_gpu_choices(
            [SimpleNamespace(name="GeForce RTX 5080", uuid="GPU-demo-5080")]
        )
        self._scanned = True
        self._reload_games()
        self.select_filters()
        self.splitter.setSizes([350, 800])

    def _build_library(self):
        pane = super()._build_library()
        self.search = W.QLineEdit()
        self.search.setAccessibleName("Search games")
        self.search.setPlaceholderText("Search games")
        self.search.setClearButtonEnabled(True)
        self.launcher_filter = W.QComboBox()
        self.launcher_filter.setAccessibleName("Launcher filter")
        self.launcher_filter.addItem("All launchers", "")
        for name, title in NAMES.items():
            self.launcher_filter.addItem(title, name)
        self.wrap_filter = W.QComboBox()
        self.wrap_filter.setAccessibleName("Wrapper filter")
        for key, title in [("", "All games"), ("on", "Wrapped"), ("off", "Unwrapped")]:
            self.wrap_filter.addItem(title, key)
        filters = W.QHBoxLayout()
        filters.addWidget(self.launcher_filter, 1)
        filters.addWidget(self.wrap_filter, 1)
        pane.layout().insertWidget(1, self.search)
        pane.layout().insertLayout(2, filters)
        self.search.textChanged.connect(self.select_filters)
        self.launcher_filter.currentIndexChanged.connect(self.select_filters)
        self.wrap_filter.currentIndexChanged.connect(self.select_filters)
        return pane

    def _build_settings(self):
        with patch.object(
            library_ui, "adaptive_target_fps_from_env", return_value=DEFAULT_TARGET_FPS
        ):
            pane = super()._build_settings()
        page = pane.findChild(W.QWidget, "gameSettingsPage").layout()
        self.history_row = W.QFrame()
        self.history_row.setObjectName("historyEntry")
        box = W.QHBoxLayout(self.history_row)
        box.setContentsMargins(14, 8, 14, 8)
        column = W.QVBoxLayout()
        column.setSpacing(2)
        heading = W.QLabel("Performance history")
        heading.setObjectName("historyHeading")
        self.history_summary = W.QLabel()
        self.history_summary.setWordWrap(True)
        self.history_summary.setObjectName("prefRowSubtitle")
        column.addWidget(heading)
        column.addWidget(self.history_summary)
        box.addLayout(column, 1)
        self.history_button = W.QPushButton("View history")
        self.history_button.setAccessibleName("View performance history")
        self.history_button.clicked.connect(self.open_history)
        self.capture_switch = make_toggle_switch(
            QtCore=C, QtGui=G, QtWidgets=W, object_name="captureTelemetry"
        )
        preference_row(
            QtCore=C,
            QtWidgets=W,
            rows_layout=self._field_rows[GROUP_IN_GAME],
            title="Capture telemetry",
            subtitle="Record performance during this game's next play session.",
            control=self.capture_switch,
        )
        self.capture_switch.toggled.connect(self._capture_changed)
        self.history_button.setObjectName("historyOpen")
        box.addWidget(self.history_button)
        page.insertWidget(1, self.history_row)
        return pane

    def _capture_changed(self, enabled):
        if not self._syncing:
            self._write(self._selected_game(), "set_game_telemetry", enabled)

    def _sync_target_controls(self, adaptive):
        with patch.object(
            library_ui, "adaptive_target_fps_from_env", return_value=DEFAULT_TARGET_FPS
        ):
            super()._sync_target_controls(adaptive)

    def select_filters(self, *_):
        name = self.search.text().casefold()
        launcher = self.launcher_filter.currentData()
        wrapped = self.wrap_filter.currentData()
        visible = []
        for i in range(self.game_list.count()):
            item = self.game_list.item(i)
            key = item.data(C.Qt.ItemDataRole.UserRole)
            game = self._game_for_key(key)
            if game is None:
                item.setHidden(True)
                continue
            show = (
                not launcher or game.launcher == launcher
            ) and name in game.name.casefold()
            show = show and (not wrapped or game.enabled == (wrapped == "on"))
            item.setHidden(not show)
            if show:
                visible.append(key)
        if visible and self._selected_key not in visible:
            self._select_key(visible[0])
        elif not visible:
            self.game_list.clearSelection()
            self._selected_key = ""
            self._sync_selection(None)
        self.placeholder_label.setVisible(not visible)
        if not visible:
            self.placeholder_label.setText("No games match these filters.")
        self.status_label.setText(f"{len(visible)} of {len(self._games)} games")
        if not visible:
            self.history_row.hide()

    def _refresh_list(self):
        super()._refresh_list()
        self.select_filters()

    def _sync_history_entry(self, game):
        self.history_row.setVisible(bool(game))
        if not game:
            return
        i = KEYS.index(game_key(game))
        has = i < 2
        if has:
            self.history_summary.setText(
                [
                    "Latest recording · 24 min   ·   87 FPS median   ·   61 FPS 1% low",
                    "Latest recording · 3 min   ·   72 FPS median   ·   53 FPS 1% low",
                ][i]
            )
        else:
            msg = (
                "Wrap this game to record its next session."
                if not game.enabled
                else "Capture telemetry is off."
                if not game.detail.setting.telemetry
                else "Ready to record on the next launch."
            )
            self.history_summary.setText("No recording yet · " + msg)
        with C.QSignalBlocker(self.capture_switch):
            self.capture_switch.setChecked(bool(game.detail.setting.telemetry))
        self.history_button.setVisible(has)
        self.history_button.setEnabled(has)
        self.history_button.setToolTip("Open the latest recording." if has else "")

    def open_history(self):
        if self._selected_key in KEYS[:2]:
            self.owner.open_history.emit(KEYS.index(self._selected_key))

    def rescan(self, *, deep=True, quiet=False):
        self._flush_pending_field_edit()
        self._reload_games()
        self.select_filters()
        self._sync_status("Example library refreshed.")

    def _select_key(self, key):
        previous = self._selected_key
        super()._select_key(key)
        if previous != self._selected_key:
            self.details_scroll.verticalScrollBar().setValue(0)

    def _sync_selection(self, game):
        with patch.object(
            library_ui, "adaptive_target_fps_from_env", return_value=DEFAULT_TARGET_FPS
        ):
            super()._sync_selection(game)
        self._sync_history_entry(game)
        self.owner.selected_game_changed.emit(game)

    def _sync_play_button(self, game):
        self.play_button.setVisible(bool(game))
        if game:
            active = game_key(game) == self.playing
            self.play_button.setProperty("playState", "running" if active else "idle")
            self.play_button.setText("Stop" if active else "Play")
            self.play_button.setEnabled(not self.playing or active)
            self.play_button.setToolTip("Simulate a play session in the preview.")
            self.play_button.style().unpolish(self.play_button)
            self.play_button.style().polish(self.play_button)

    def _play_stop_clicked(self, _checked: bool = False):
        game = self._selected_game()
        if game:
            self.playing = "" if self.playing else game_key(game)
            self._sync_play_button(game)
            self._sync_status(
                (
                    "Preview session started: "
                    if self.playing
                    else "Preview session stopped: "
                )
                + game.name
            )

    def _write(self, game, method, *args):
        if game is not None:
            self._by_launcher[game.launcher].edit(game, method, args[0])
            self._reload_games()
            self._sync_selection(self._selected_game())
            self.select_filters()
        return True

    _write_async = _write

    def _bulk_apply(self, action, sources):
        scope = self._bulk_scope(action, sources)
        if not scope or not self._confirm(
            "All games", f"{action.label} in the example library ({len(scope)} games)?"
        ):
            return
        self._flush_pending_field_edit()
        method = (
            "set_game_overlay" if action.affects == "overlay" else "set_game_enabled"
        )
        for game in scope:
            self._by_launcher[game.launcher].edit(game, method, action.value)
        self.rescan()


class LibraryPage(W.QWidget):
    selected_game_changed = C.Signal(object)
    open_history = C.Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = FixturePanel(self)
        self.setStyleSheet(FONTS)
        layout.addWidget(self.panel.widget)
        normalize_group_headings(self.panel.widget)

    def return_from_history(self):
        if self.panel.history_button.isVisible():
            self.panel.history_button.setFocus(C.Qt.FocusReason.OtherFocusReason)

    def set_adaptive_available(self, available):
        self.panel.eligible = bool(available)
        self.panel._sync_selection(self.panel._selected_game())

    def select_game(self, index):
        if index not in range(len(KEYS)):
            return
        p = self.panel
        for i in range(p.game_list.count()):
            item = p.game_list.item(i)
            if item.data(C.Qt.ItemDataRole.UserRole) == KEYS[index] and item.isHidden():
                p.search.clear()
                p.launcher_filter.setCurrentIndex(0)
                p.wrap_filter.setCurrentIndex(0)
                break
        p._select_key(KEYS[index])

    def adaptive_picker_state(self):
        """Mirror the selected game and each game's effective target into Profiles."""
        panel = self.panel
        selected = (
            KEYS.index(panel._selected_key) if panel._selected_key in KEYS else None
        )
        targets = []
        for launcher, ident, *_ in GAMES:
            game = panel._by_launcher[launcher].rows[ident]
            targets.append(game.detail.setting.target_fps or DEFAULT_TARGET_FPS)
        return selected, targets

    def apply_adaptive(self, index, target_fps):
        self.set_adaptive_available(True)
        self.select_game(index)
        p = self.panel
        game = p._selected_game()
        if game is not None:
            source = p._by_launcher[game.launcher]
            for method, value in [
                ("set_game_enabled", True),
                ("set_game_mode", "adaptive"),
                ("set_game_target_fps", target_fps),
            ]:
                source.edit(source.rows[game.game_id], method, value)
            p._reload_games()
            p._sync_selection(p._selected_game())


class GameArtworkPreview(overlay_preview_class(C, G, W)):
    """Keep the app's native HUD painter, replacing its 16:9 scenery only."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.artwork = G.QPixmap()

    def set_artwork(self, artwork):
        self.artwork = artwork
        self.update()

    def _paint_scene(self, painter):
        if self.artwork.isNull():
            super()._paint_scene(painter)
            return
        frame = C.QRectF(0, 0, CANVAS_W, CANVAS_H)
        source = C.QRectF(self.artwork.rect())
        scale = max(frame.width() / source.width(), frame.height() / source.height())
        source.setSize(C.QSizeF(frame.width() / scale, frame.height() / scale))
        source.moveCenter(C.QRectF(self.artwork.rect()).center())
        painter.save()
        painter.setRenderHint(G.QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(frame, self.artwork, source)
        shade = G.QLinearGradient(0, 0, 0, CANVAS_H)
        shade.setColorAt(0, G.QColor(19, 32, 25, 102))
        shade.setColorAt(1, G.QColor(19, 25, 22, 187))
        painter.fillRect(frame, shade)
        painter.restore()


class FixtureOverlay(overlay_ui.OverlayConfigPanel):
    def __init__(self):
        self.sample_dialog = None
        self.art_path = None
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
        self.timer.stop()

    def set_game(self, game):
        path = None
        if game is not None and game.launcher == "steam":
            candidate = Path(__file__).with_name("artwork") / f"{game.game_id}art.webp"
            if candidate.is_file():
                path = candidate
        name = (
            game.name
            if game is not None and path is not None
            else "Illustrative preview"
        )
        caption = self.widget.findChild(W.QLabel, "overlayPreviewCaption")
        if caption is not None:
            caption.setText(f"{name} · sample readings")
        self.preview.setAccessibleName(f"{name} · overlay preview")
        if self.sample_dialog is not None:
            self.sample_dialog.setWindowTitle(self.preview.accessibleName())
        if path == self.art_path:
            return
        self.art_path = path
        art = G.QPixmap(str(path)) if path else G.QPixmap()
        self.preview.set_artwork(art)
        if self.sample_dialog is not None:
            self.sample_canvas.set_artwork(art)

    def _update_setting(self, setter, value):
        if not self._syncing:
            self.config = setter(self.config, value)
            self._save()

    def _set_item_enabled(self, item_id, enabled):
        if self._syncing:
            return
        after = set_overlay_item_enabled(self.config, item_id, bool(enabled))
        if after.enabled_item_ids == self.config.enabled_item_ids and not enabled:
            self._show_status("Keep at least one overlay item enabled.")
        self.config = after
        self._save()

    def _save(self):
        self._sync_widgets()
        self.refresh_preview()

    def refresh_preview(self):
        values = dict(overlay_ui.SAMPLE_OVERLAY_VALUES)
        self.preview.set_config(self.config)
        self.preview.set_values(values)
        if self.sample_dialog is not None:
            self.sample_canvas.set_config(self.config)
            self.sample_canvas.set_values(values)
        for key, label in self.item_value_labels.items():
            label.setText(overlay_ui._sample_text(key, values))

    def _toggle_live_preview(self):
        if self.sample_dialog is not None:
            self.sample_dialog.close()
            return
        dlg = W.QDialog(self.widget)
        dlg.setWindowTitle(
            self.preview.accessibleName() or "Live preview · sample readings"
        )
        layout = W.QVBoxLayout(dlg)
        self.sample_canvas = GameArtworkPreview()
        self.sample_canvas.set_artwork(self.preview.artwork)
        self.sample_canvas.setMaximumSize(16777215, 16777215)
        self.sample_canvas.setMinimumSize(560, 315)
        dlg.resize(900, 515)
        layout.addWidget(self.sample_canvas)
        dlg.finished.connect(self._preview_closed)
        self.sample_dialog = dlg
        self.live_button.setText(overlay_ui.LIVE_PREVIEW_STOP)
        self.refresh_preview()
        dlg.show()

    def _preview_closed(self, *_):
        self.sample_dialog = None
        self.live_button.setText(overlay_ui.LIVE_PREVIEW_START)

    def _show_shortcuts(self):
        dlg = W.QDialog(self.widget)
        dlg.setWindowTitle("Keyboard shortcuts")
        form = W.QFormLayout(dlg)
        for item in SHORTCUTS:
            form.addRow(item.label, W.QLabel("Alt+" + item.key))
        button = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Close)
        button.rejected.connect(dlg.close)
        form.addRow(button)
        self._shortcuts_dialog = dlg
        dlg.show()


class OverlayPage(W.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = FixtureOverlay()
        self.setStyleSheet(FONTS)
        layout.addWidget(self.panel.widget)
        normalize_group_headings(self.panel.widget)
