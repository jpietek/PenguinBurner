"""Native library and overlay views with session-local game and telemetry fixtures."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from design import COL, GAP, RAD, TYPE, control_icons, focus_visible
from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W
from qt.components.page_header import PAGE_MARGINS
from qt.components.perf_entry import PerfEntryButton
from qt.components.perf_profile_plot import PerfProfileSource
from qt.components.switch_sync import set_switch

from integrations.launchers.compatibility import (
    CompatibilitySelection,
    CompatibilityTool,
    compatibility_field,
)
from integrations.launchers.library import (
    FIELD_CHOICE,
    FIELD_MULTILINE,
    FIELD_TEXT,
    GROUP_IN_GAME,
    WRITE_NEEDS_SETUP,
    LauncherField,
    LauncherWriteState,
    LibraryGame,
    library_bulk_actions,
)
from integrations.steam.launch_options import launch_options_problems
from ui.assets import asset_image_path
from ui.components import game_library_panel as library_ui
from ui.components.game_library_panel import GameLibraryPanel, game_key
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
    ("steam", "388880", "Oxenfree", "Native Linux", 4, True),
    ("steam", "1145350", "Hades II", "Proton Experimental", 0, False),
)
OPENGL_REASON = (
    "Game not compatible with Overlay or Adaptive: OpenGL detected. These "
    "features need Vulkan frame data. Fixed GPU profiles remain available."
)
# What each launcher reports beyond the row, matching library.js GAME_FACTS.
FACTS = {
    "1091500": {"effective": "Proton 9.0-4", "mode": "balanced"},
    "2531310": {"effective": "Proton Experimental", "mode": "efficiency"},
    "1888930": {"compat": "GE-Proton9-11"},
    # The same per-game target its Perf Profile ring recorded against.
    "782330": {"effective": "Proton 9.0-4", "target_fps": 144},
    # Control's sample Perf Profile ring was recorded on Balanced.
    "control": {
        "command": "gamemoderun",
        "inherited": "Heroic global settings",
        "mode": "balanced",
    },
    "388880": {"native": True, "overlay_reason": OPENGL_REASON},
    "1145350": {"effective": "Proton 9.0-4", "ready": False},
}
TOOLS = {
    "steam": (
        ("proton_experimental", "Proton Experimental"),
        ("proton_9", "Proton 9.0-4"),
        ("GE-Proton9-20", "GE-Proton9-20"),
    ),
    # The Flatpak Heroic lists only the tools inside its sandbox.
    "heroic": (
        ("Wine-GE-Proton8-26", "Wine-GE-Proton8-26"),
        ("GE-Proton9-20", "GE-Proton9-20"),
    ),
    "lutris": (
        ("wine-ge-8-26-x86_64", "wine-ge-8-26-x86_64"),
        ("lutris-GE-Proton8-26-x86_64", "lutris-GE-Proton8-26-x86_64"),
        ("system", "System Wine"),
    ),
    "faugus": (
        ("GE-Proton9-20", "GE-Proton9-20"),
        ("UMU-Proton-9.0-3", "UMU-Proton-9.0-3"),
    ),
}
GUIDANCE = {
    "steam": "Keeps Steam's current choice by default. Selecting another entry "
    "uses Steam's own compatibility-tool setting for this game.",
    "heroic": "Applies on the next launch. Use Play here to refresh Heroic's saved settings.",
    "lutris": "Applies on the next launch. Close Lutris's game settings before editing here.",
    "faugus": "Applies on the next launch. Close Faugus's game settings before editing here.",
}
# A game mode that records a Perf Profile ring, and the tier it records on.
# Adaptive picks its tier live; the sample session runs on Balanced.
RECORDING_TIERS = {
    "efficiency": "Efficiency",
    "balanced": "Balanced",
    "performance": "Performance",
    "adaptive": "Balanced",
}
# Prototype controls: what Steam's integration reports (integrations/steam/
# library_source.py SteamLibrarySource.write_state, same words).
STEAM_NOT_SET_UP = LauncherWriteState(
    state=WRITE_NEEDS_SETUP,
    summary="not set up",
    detail=(
        "Find your installed games and connect them to "
        "PenguinBurner for simple per-game profiles."
    ),
    note="Steam user: jp",
    action_label="Scan my Steam library",
    action="initialize",
)
STEAM_NEEDS_RESTART = LauncherWriteState(
    state=WRITE_NEEDS_SETUP,
    summary="read-only until initialized",
    detail=(
        "Restart Steam once to finish connecting per-game profiles. "
        "Your games and saved settings stay exactly as they are."
    ),
    note="Steam user: jp",
    action_label="Restart Steam to finish",
    action="restart",
    confirm="Cleanly shut down and relaunch the Steam client now?",
)
# Library launch outcomes the Prototype controls can pick for the next Play
# (ui/components/game_library_panel.py session states, same words).
LAUNCH_FAILED = "PenguinBurner could not execute the game's launch command."
LAUNCH_UNCONFIRMED = (
    "Launch request was not confirmed. Check the launcher before retrying."
)
PROFILE_UNCONFIRMED = "PenguinBurner connected; GPU profile application is unconfirmed."
SESSION_LOST = (
    "Session connection lost; game status will be confirmed after reconnecting."
)
EXTERNAL_NOTE = "Game process detected. PenguinBurner integration is not yet confirmed."
# integrations/steam/library_source.py: Proton needs the live Steam client.
STEAM_NEEDS_LIVE = (
    "Needs live apply: Proton is Steam's own setting, so the "
    "client has to be running and connected to change it."
)
HEROIC_FLATPAK_READY = (
    "Configured for Flatpak; Play refreshes Heroic's saved launch settings."
)
HEROIC_FLATPAK_SETUP = (
    "Flatpak setup required: turn Wrap this game off and on, then use Play."
)
# A sample scan takes this long, so the loading page and Scanning… show.
SAMPLE_SCAN_S = 1.2
DEFAULT_TARGET_FPS = 60
DETAILS_MIN_WIDTH = 460
KEYS = [f"{launcher}:{ident}" for launcher, ident, *_ in GAMES]
NAMES = {"steam": "Steam", "heroic": "Heroic", "lutris": "Lutris", "faugus": "Faugus"}
COMMANDS = {
    "steam": "Steam launch options — %command% is where the game goes",
    "heroic": "Wrapper command in the Heroic settings",
    "lutris": "prefix_command in the Lutris config",
    "faugus": "Launch arguments in the Faugus game settings",
}


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
    # Heroic Flatpak: the wrap switch wrote the sandbox wrapper path.
    flatpak_wrapper: bool = True


def result(message, ok=True):
    """The launcher manager's answer, shaped like its ApplyResult."""
    return SimpleNamespace(ok=ok, message=message)


class Source:
    """One sample launcher. It is its own manager: the real panel finds the
    setters by name, so every write goes through the panel's own code path."""

    def __init__(self, name):
        self.launcher_id = name
        self.display_name = NAMES[name]
        self.icon_asset = f"tab-{name}.png"
        # The Faugus config is present but its client is not installed here,
        # so the real panel hides Play for its games.
        self.can_launch = name != "faugus"
        self.manager = self
        self.panel = None
        self.rows = {}
        for i, (launcher, ident, title, runner, hours, wrapped) in enumerate(GAMES):
            if launcher == name:
                facts = FACTS.get(ident, {})
                setting = Setting(
                    enabled=wrapped,
                    telemetry=i != 2,
                    compatibility=facts.get("compat", ""),
                    mode=facts.get("mode", "stock"),
                    command=facts.get("command"),
                    target_fps=facts.get("target_fps"),
                )
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
                    ready=facts.get("ready", True),
                    art_path=cover
                    if cover.is_file()
                    else asset_image_path(self.icon_asset),
                    overlay=True,
                    overlay_supported=not facts.get("overlay_reason"),
                    overlay_unsupported_reason=facts.get("overlay_reason", ""),
                    detail=SimpleNamespace(setting=setting),
                )

    def available(self):
        """Asked again on every scan, as the real panel does."""
        return self._prototype("libraryScan", "normal") != "no-launchers"

    def refresh(self, deep=True):
        """Re-read the launcher, off the GUI thread.

        The sample Steam read takes the panel's sample_scan_delay, so the
        loading page and Scanning… feedback show; Prototype controls can make
        the Heroic read fail while the other launchers still list."""
        delay = getattr(self.panel, "sample_scan_delay", 0.0)
        if deep and delay and self.launcher_id == "steam":
            time.sleep(delay)
        if self.launcher_id == "heroic" and (
            self._prototype("libraryScan", "normal") == "heroic-error"
        ):
            raise RuntimeError("could not read the Heroic library cache (permission denied)")

    def _prototype(self, key, default):
        owner = getattr(self.panel, "owner", None)
        return getattr(owner, "prototype", {}).get(key, default)

    def games(self):
        if self._prototype("emptyLibrary", False):
            return []
        return list(self.rows.values())

    def bulk_actions(self):
        return library_bulk_actions()

    def write_state(self):
        if self.launcher_id != "steam":
            return LauncherWriteState()
        state = self._prototype("launcherWriteState", "ready")
        if state == "not-set-up":
            return STEAM_NOT_SET_UP
        if state == "needs-restart":
            return STEAM_NEEDS_RESTART
        return LauncherWriteState(summary="live apply", note="Steam user: jp")

    # The write-state actions the panel calls by name (off the GUI thread).
    def _write_ready(self, message):
        owner = getattr(self.panel, "owner", None)
        if owner is not None:
            owner.prototype["launcherWriteState"] = "ready"
        return result(message)

    def initialize(self):
        return self._write_ready(
            "Steam library connected. Per-game profiles apply live."
        )

    def restart(self):
        return self._write_ready("Steam restarted. Per-game profiles apply live.")

    def _compat_field(self, game):
        """The shared picker, filled the way each launcher adapter fills it."""
        s = game.detail.setting
        facts = FACTS.get(game.game_id, {})
        native = facts.get("native", False) and not s.compatibility
        default = f"{self.display_name} default"
        if self.launcher_id == "steam" and not s.compatibility:
            default = (
                f"Steam default ({facts['effective']})"
                if facts.get("effective")
                else "Native Linux — no compatibility tool"
                if native
                else "Steam default"
            )
        # Steam lists and changes Proton only through its live client.
        live = self.launcher_id != "steam" or self.write_state().ready
        return compatibility_field(
            CompatibilitySelection(
                value=s.compatibility, default_label=default, supported=not native
            ),
            tuple(CompatibilityTool(v, label) for v, label in TOOLS[self.launcher_id])
            if live
            else (),
            guidance=GUIDANCE[self.launcher_id],
            error="" if live else STEAM_NEEDS_LIVE,
        )

    def _command_subtitle(self, game, command):
        s = game.detail.setting
        facts = FACTS.get(game.game_id, {})
        if self.launcher_id == "heroic" and s.enabled:
            return HEROIC_FLATPAK_READY if s.flatpak_wrapper else HEROIC_FLATPAK_SETUP
        if facts.get("inherited") and command and not s.enabled:
            return f"wrapper_command — inherited from {facts['inherited']}"
        return COMMANDS[self.launcher_id]

    def fields(self, game):
        s = game.detail.setting
        wrapper = "PENGUIN_BURNER --pb-overlay=" + ("1" if s.overlay else "0")
        command = s.command
        if command is None or (s.enabled and "PENGUIN_BURNER" not in command):
            command = wrapper if s.enabled else ""
            if self.launcher_id == "steam":
                command = (command + " %command%").strip()
            elif s.enabled:
                command += " --pb-game-id=" + game_key(game)
        return (
            self._compat_field(game),
            LauncherField(
                self.launcher_id + "_command",
                FIELD_MULTILINE if self.launcher_id == "steam" else FIELD_TEXT,
                "Command",
                self._command_subtitle(game, command),
                "set_raw_launch_options"
                if self.launcher_id == "steam"
                else "set_game_command",
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
            "set_game_compat_tool": "compatibility",
            "set_raw_launch_options": "command",
            "set_game_command": "command",
        }[method]
        setting = game.detail.setting
        setattr(setting, attr, value)
        if attr == "command":
            setting.enabled = "PENGUIN_BURNER" in str(value)
            # A hand-typed Heroic wrapper lacks the Flatpak sandbox path.
            setting.flatpak_wrapper = False
        elif attr in ("enabled", "overlay"):
            setting.command = None
            if attr == "enabled":
                setting.flatpak_wrapper = True
        self.rows[game.game_id] = replace(
            game,
            enabled=setting.enabled,
            wrapped=setting.enabled,
            overlay=setting.overlay,
        )

    # -- manager setters the real panel calls by name -------------------------

    def _running(self, game_id):
        panel = self.panel
        return bool(
            panel
            and panel._tracked_state(f"{self.launcher_id}:{game_id}") == "running"
            and self.rows[game_id].enabled
        )

    def _saved(self):
        return (
            "Applied live."
            if self.launcher_id == "steam"
            else "Saved for the next launch."
        )

    def _write_problem(self, game_id):
        """Prototype controls: a failed save, or a save whose live update
        failed for the running game, in the managers' own words."""
        failure = self._prototype("libraryWrite", "ok")
        if failure == "save-fail":
            return result(
                f"{self.rows[game_id].name}: save failed "
                "(PermissionError: launcher settings are read-only)",
                ok=False,
            )
        if failure == "live-fail" and self._running(game_id):
            return result(
                "Setting saved; live update failed (the daemon did not answer).",
                ok=False,
            )
        return None

    def _set(self, game_id, method, value, live=""):
        problem = self._write_problem(game_id)
        if problem is not None and not problem.message.startswith("Setting saved"):
            return problem
        self.edit(self.rows[game_id], method, value)
        if problem is not None:
            return problem
        return result(live if live and self._running(game_id) else self._saved())

    def set_game_enabled(self, game_id, value):
        return self._set(game_id, "set_game_enabled", value)

    def set_game_overlay(self, game_id, value):
        return self._set(
            game_id,
            "set_game_overlay",
            value,
            f"Overlay {'show' if value else 'hide'} request sent live; "
            "loaded overlays update within one second.",
        )

    def set_game_mode(self, game_id, mode):
        problem = self._write_problem(game_id)
        if problem is not None and not problem.message.startswith("Setting saved"):
            return problem
        self.edit(self.rows[game_id], "set_game_mode", mode)
        if problem is not None:
            return problem
        if self._running(game_id):
            return result(
                f"{mode.title()} applied to the running game; verified with daemon."
            )
        return result("Mode saved for next launch; no active profile for this game.")

    def set_game_target_fps(self, game_id, value):
        return self._set(
            game_id,
            "set_game_target_fps",
            value,
            "Adaptive target applied to the running game.",
        )

    def set_game_gpu(self, game_id, value):
        return self._set(
            game_id,
            "set_game_gpu",
            value,
            "GPU saved; relaunch the game to change its GPU.",
        )

    def set_game_telemetry(self, game_id, value):
        return self._set(game_id, "set_game_telemetry", value)

    def set_game_compat_tool(self, game_id, value):
        self.edit(self.rows[game_id], "set_game_compat_tool", value)
        return result(f"Compatibility tool saved. {GUIDANCE[self.launcher_id]}")

    def set_raw_launch_options(self, game_id, text):
        problems = launch_options_problems(text.strip())
        if problems:
            return result("; ".join(problems), ok=False)
        return self._set(game_id, "set_raw_launch_options", text.strip())

    def set_game_command(self, game_id, text):
        return self._set(game_id, "set_game_command", text.strip())

    def _set_all(self, ids, method, value):
        for game_id in ids:
            self.edit(self.rows[game_id], method, value)
        count = len(ids)
        return result(f"{count} {'game' if count == 1 else 'games'} saved.")

    def set_all_games_enabled(self, ids, value):
        return self._set_all(ids, "set_game_enabled", value)

    def set_all_games_overlay(self, ids, value):
        return self._set_all(ids, "set_game_overlay", value)


WRAPPED_ROLE = int(C.Qt.ItemDataRole.UserRole) + 7


class GameRowDelegate(W.QStyledItemDelegate):
    """The list row as the panel draws it, plus a painted 6 px marker on the
    right edge: an accent dot when wrapped, a quiet ring when not (the real
    panel prefixes ●/○ glyphs)."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        wrapped = index.data(WRAPPED_ROLE)
        if wrapped is None:
            return
        painter.save()
        painter.setRenderHint(G.QPainter.RenderHint.Antialiasing)
        center = C.QPointF(option.rect.right() - 14, option.rect.center().y())
        if wrapped:
            painter.setPen(C.Qt.PenStyle.NoPen)
            painter.setBrush(G.QColor(c.accent))
        else:
            painter.setPen(G.QPen(G.QColor(c.quiet), 1.2))
            painter.setBrush(C.Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, 3, 3)
        painter.restore()


def find_in_layout(layout, widget):
    """(layout, index) of the item holding widget, searching nested layouts."""
    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item.widget() is widget:
            return layout, i
        if item.layout() is not None:
            found = find_in_layout(item.layout(), widget)
            if found:
                return found
    return None


class FixturePanel(GameLibraryPanel):
    """The real Game Library panel over sample launchers.

    Only what needs a backend is simulated: the launcher reads and the game
    session (Play, Stop). The scan threads, loading page, write-state gating
    and its fix-it action, writes, the All games menu, the Play button states
    and the status line are the panel's own code.
    """

    def __init__(self, owner):
        self.owner = owner
        self.eligible = False
        self._owns_status = False
        # Seconds the sample Steam read takes (0 while building, so the
        # window opens on a filled list; the first-scan replay sets it).
        self.sample_scan_delay = 0.0
        self._action_rescan = False
        self._last_status = ""
        super().__init__(
            QtCore=C,
            QtGui=G,
            QtWidgets=W,
            sources=[Source(name) for name in NAMES],
            gpu_choices=lambda: (),
            adaptive_available=lambda *_: self.eligible,
        )
        # The shared page margins; the list and details scroll in their panes.
        self.widget.layout().setContentsMargins(*PAGE_MARGINS)
        # Every sample launcher, installed (per Prototype controls) or not.
        self.sample_sources = {s.launcher_id: s for s in self._candidate_sources}
        for source in self._candidate_sources:
            source.panel = self
        self._apply_gpu_choices(
            [SimpleNamespace(name="GeForce RTX 5080", uuid="GPU-demo-5080")]
        )
        # The tab is built already scanned; the 30 s quiet refresh runs as
        # in the real window once a scan has happened.
        self._scanned = True
        self._library_timer.start()
        self._apply_library_heading()
        self._reload_games()
        self.select_filters()
        self.splitter.setSizes([370, 800])

    def _build_library(self):
        """Search, then Sort · Filters · All games, then the filters panel.

        Same layout as the browser: the three filters fold away behind a
        Filters button that counts the active ones.
        """
        pane = super()._build_library()
        self.game_list.setItemDelegate(GameRowDelegate(self.game_list))
        self.search = W.QLineEdit()
        self.search.setAccessibleName("Search games")
        self.search.setPlaceholderText("Search games…")
        self.search.setClearButtonEnabled(True)
        self.launcher_filter = self._filter_combo(
            "Launcher filter",
            [("", "All launchers")] + [(k, v) for k, v in NAMES.items()],
        )
        self.wrap_filter = self._filter_combo(
            "Wrapper filter",
            [("", "Any setup"), ("on", "Wrapped"), ("off", "Unwrapped")],
        )
        self.record_filter = self._filter_combo(
            "Recording filter",
            [("", "Any history"), ("on", "Recorded"), ("off", "No recording")],
        )
        self.filters_button = W.QPushButton("Filters")
        self.filters_button.setObjectName("libraryFiltersButton")
        self.filters_button.setProperty("expanded", False)
        self.filters_button.setAccessibleDescription("Show or hide the list filters")
        self.filters_button.clicked.connect(self.toggle_filters)
        self.sort_combo.setSizeAdjustPolicy(
            W.QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        found = find_in_layout(pane.layout(), self.sort_combo)
        assert found, "Sort is laid out in the list pane"
        actions, index = found
        actions.insertWidget(index + 1, self.filters_button)
        self.filters_panel = W.QFrame()
        self.filters_panel.setObjectName("libraryFilters")
        grid = W.QGridLayout(self.filters_panel)
        grid.setContentsMargins(sp.md, sp.sm, sp.md, sp.sm)
        grid.setHorizontalSpacing(sp.md)
        grid.setVerticalSpacing(sp.sm)
        for row, (title, combo) in enumerate(
            [
                ("Launcher", self.launcher_filter),
                ("Wrapper", self.wrap_filter),
                ("Recording", self.record_filter),
            ]
        ):
            label = W.QLabel(title)
            label.setObjectName("libraryFilterLabel")
            label.setBuddy(combo)
            grid.addWidget(label, row, 0)
            grid.addWidget(combo, row, 1, C.Qt.AlignmentFlag.AlignRight)
        clear = W.QPushButton("Clear filters")
        clear.setObjectName("libraryClearFilters")
        clear.clicked.connect(self.reset_filters)
        grid.addWidget(clear, 3, 0, 1, 2, C.Qt.AlignmentFlag.AlignLeft)
        grid.setColumnStretch(1, 1)
        self.filters_panel.hide()
        layout = pane.layout()
        layout.insertWidget(1, self.search)
        layout.insertWidget(layout.indexOf(self.game_list), self.filters_panel)
        self.search.textChanged.connect(self.select_filters)
        for combo in (self.launcher_filter, self.wrap_filter, self.record_filter):
            combo.currentIndexChanged.connect(self.select_filters)
        return pane

    @staticmethod
    def _filter_combo(name, items):
        combo = W.QComboBox()
        combo.setAccessibleName(name)
        combo.setSizeAdjustPolicy(W.QComboBox.SizeAdjustPolicy.AdjustToContents)
        for key, title in items:
            combo.addItem(title, key)
        return combo

    def toggle_filters(self):
        shown = not self.filters_panel.isVisible()
        self.filters_panel.setVisible(shown)
        self.filters_button.setProperty("expanded", shown)
        self.filters_button.style().unpolish(self.filters_button)
        self.filters_button.style().polish(self.filters_button)

    def reset_filters(self):
        with C.QSignalBlocker(self.search):
            self.search.clear()
        for combo in (self.launcher_filter, self.wrap_filter, self.record_filter):
            with C.QSignalBlocker(combo):
                combo.setCurrentIndex(0)
        self.select_filters()

    def fit_list_tools(self):
        """Size the list pane once the page style (caption-sized tools) is on:
        "Filters (3)" never squeezes the Sort combo, and the pane keeps the
        width its tools row needs."""
        pane = self.splitter.widget(0)
        for widget in [pane, *pane.findChildren(W.QWidget)]:
            widget.ensurePolished()
        # The three filter combos share one width, so their edges line up.
        combos = (self.launcher_filter, self.wrap_filter, self.record_filter)
        widest = max(combo.sizeHint().width() for combo in combos)
        for combo in combos:
            combo.setFixedWidth(widest)
        button = self.filters_button
        button.setMinimumWidth(
            button.fontMetrics().horizontalAdvance("Filters (3)") + 2 * sp.sm + 4
        )
        width = max(300, pane.minimumSizeHint().width())
        pane.setMinimumWidth(width)
        self.splitter.setSizes([width, 800])

    def _active_filter_count(self):
        return sum(
            bool(combo.currentData())
            for combo in (self.launcher_filter, self.wrap_filter, self.record_filter)
        )

    def _apply_library_heading(self):
        """The browser's page heading: a title, then the launchers it merged."""
        names = list(NAMES.values())
        joined = ", ".join(names[:-1]) + " and " + names[-1]
        self.library_label.setText(joined)
        header = find_in_layout(self.widget.layout(), self.library_label)
        assert header, "the launcher caption sits in the header row"
        row, index = header
        row.takeAt(index)
        heading = W.QVBoxLayout()
        heading.setSpacing(2)
        self.page_title = W.QLabel("Game Library")
        self.page_title.setObjectName("libraryPageTitle")
        heading.addWidget(self.page_title)
        heading.addWidget(self.library_label)
        row.insertLayout(index, heading)
        row.setAlignment(self.rescan_button, C.Qt.AlignmentFlag.AlignVCenter)

    def _build_settings(self):
        with patch.object(
            library_ui, "adaptive_target_fps_from_env", return_value=DEFAULT_TARGET_FPS
        ):
            pane = super()._build_settings()
        # A long title wraps; unwrapped, it set the page's minimum width and
        # pushed Play and the row controls past the card at narrow sizes.
        self.title_label.setWordWrap(True)
        self.title_label.setSizePolicy(
            W.QSizePolicy.Policy.Ignored, W.QSizePolicy.Policy.Preferred
        )
        self.title_label.setMinimumWidth(160)
        # "follows system-wide (60)" wraps rather than widening its row.
        self.target_follow_label.setWordWrap(True)
        self._details_pane = pane
        pane.setMinimumWidth(DETAILS_MIN_WIDTH)
        # PR #137's Perf Profile entry sits beside Play, in a secondary style,
        # with the game's own frametime sparkline as its icon.
        self.perf_button = PerfEntryButton(self.owner.perf_source)
        self.perf_button.open_requested.connect(self.owner.open_perf_profile)
        found = find_in_layout(
            self.play_button.parentWidget().layout(), self.play_button
        )
        assert found, "Play is laid out in the game header"
        hero, index = found
        hero.insertWidget(index, self.perf_button, 0, C.Qt.AlignmentFlag.AlignTop)
        self.capture_switch = make_toggle_switch(
            QtCore=C, QtGui=G, QtWidgets=W, object_name="captureTelemetry"
        )
        preference_row(
            QtCore=C,
            QtWidgets=W,
            rows_layout=self._field_rows[GROUP_IN_GAME],
            title="Capture telemetry",
            subtitle="Record frame timing and GPU measurements for the Perf Profile.",
            control=self.capture_switch,
        )
        self.capture_switch.toggled.connect(self._capture_changed)
        return pane

    def _build_field(self, field):
        widgets = super()._build_field(field)
        # A choice sizes to its longest entry ("Native Linux — no compatibility
        # tool"); the row subtitle takes the slack.
        if widgets is not None and field.kind == FIELD_CHOICE:
            widgets["control"].setSizeAdjustPolicy(
                W.QComboBox.SizeAdjustPolicy.AdjustToContents
            )
        return widgets

    def _capture_changed(self, enabled):
        if self._syncing:
            return
        game = self._selected_game()
        if not self._write(game, "set_game_telemetry", enabled) or game is None:
            return
        key = game_key(game)
        self.owner.capture_changed.emit(key, bool(enabled))
        self.perf_button.sync(key, capture=bool(enabled))

    def _sync_target_controls(self, adaptive):
        with patch.object(
            library_ui, "adaptive_target_fps_from_env", return_value=DEFAULT_TARGET_FPS
        ):
            super()._sync_target_controls(adaptive)

    def select_filters(self, *_):
        name = self.search.text().casefold()
        launcher = self.launcher_filter.currentData()
        wrapped = self.wrap_filter.currentData()
        recorded = self.record_filter.currentData()
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
            show = show and (
                not recorded or self.owner.has_recording(key) == (recorded == "on")
            )
            item.setHidden(not show)
            if show:
                visible.append(key)
        if visible and self._selected_key not in visible:
            self._select_key(visible[0])
        elif not visible:
            self.game_list.clearSelection()
            self._selected_key = ""
            self._sync_selection(None)
        # The real empty-library text stays; only a filter miss is ours.
        if self._games:
            self.placeholder_label.setVisible(not visible)
            self.placeholder_label.setText("No games match these filters.")
        count = self._active_filter_count()
        self.filters_button.setText(f"Filters ({count})" if count else "Filters")
        self.filters_button.setEnabled(bool(self._games))
        # Filtering changes no count the status line shows: keep its message.
        self._sync_status(self._last_status)

    def _sync_status(self, message: str = "") -> None:
        self._last_status = message
        super()._sync_status(message)

    @staticmethod
    def _row_label(game):
        # The wrapped marker is painted by GameRowDelegate, not a glyph.
        return game.name

    def _refresh_list(self):
        super()._refresh_list()
        # The real panel mutes unwrapped rows; in the token colours here.
        for i in range(self.game_list.count()):
            item = self.game_list.item(i)
            game = self._game_for_key(item.data(C.Qt.ItemDataRole.UserRole))
            if game is not None:
                item.setForeground(G.QColor(c.ink if game.enabled else c.secondary))
                item.setData(WRAPPED_ROLE, bool(game.enabled))
        self.select_filters()

    def rescan(self, *, deep=True, quiet=False):
        """The panel's own scan over the sample launchers. A deep, user-asked
        scan takes SAMPLE_SCAN_S; the quiet timer pass and the re-read after a
        write-state action land at once (so the action's message stands)."""
        slow = deep and not quiet and not self._action_rescan
        self.sample_scan_delay = SAMPLE_SCAN_S if slow else 0.0
        super().rescan(deep=deep, quiet=quiet)

    def _collect_write_action(self, first=True):
        self._action_rescan = True
        try:
            super()._collect_write_action(first)
        finally:
            self._action_rescan = False
        if self._action_thread is None:
            # Initialize / Restart Steam finished: the shell's Prototype
            # controls follow the launcher's new state.
            self.owner.prototype_changed.emit(
                "launcherWriteState",
                self.owner.prototype.get("launcherWriteState", "ready"),
            )

    def replay_first_scan(self):
        """Prototype controls: the first scan's loading page, as when the tab
        is opened for the first time."""
        if self._scan_active or self._write_busy:
            return
        self._games = ()
        self._refresh_list()
        self.rescan()

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
        setting = getattr(game.detail, "setting", None) if game else None
        if setting is not None:
            set_switch(self.capture_switch, bool(setting.telemetry))
        self.perf_button.sync(
            game_key(game) if game else "",
            capture=bool(setting.telemetry) if setting is not None else None,
        )
        self._fit_details()
        self.owner.selected_game_changed.emit(game)

    def _fit_details(self):
        """The splitter never squeezes the details below what their rows need;
        the list gives way instead, so nothing runs past the card edge."""
        page = self.details_scroll.widget()
        page.adjustSize()
        need = (
            page.minimumSizeHint().width()
            + self.details_scroll.verticalScrollBar().sizeHint().width()
        )
        self._details_pane.setMinimumWidth(max(DETAILS_MIN_WIDTH, need))

    # -- the simulated game session; the panel's own state machine renders it --

    def _play_game(self, game):
        """The sample launch. The panel's own Play / Retry routing calls this;
        Prototype controls pick how the next launch ends."""
        if self._launch_thread is not None or self._write_busy:
            return
        key = game_key(game)
        launcher = NAMES[game.launcher]
        outcome = self.owner.prototype.get("nextLaunch", "normal")
        self._pending_launches[key] = library_ui._PendingLaunch(time.monotonic())
        self._set_game_state(key, "launching")
        self._sync_status(f"{game.name}: preparing launch…")
        C.QTimer.singleShot(
            400,
            lambda: self._tracked_state(key) == "launching"
            and self._sync_status(f"{game.name}: launching via {launcher}…"),
        )
        C.QTimer.singleShot(1200, lambda: self._sample_launched(key, outcome))

    def _sample_launched(self, key, outcome):
        track = self._tracked.get(key)
        if track is None or track.state != "launching":
            return
        game = self._game_for_key(key)
        name = game.name if game is not None else key
        if outcome == "fails":
            track.state, track.warning = "stopped", LAUNCH_FAILED
            self._sync_play_button(self._selected_game())
            self._sync_status(f"{name}: launch failed.")
            return
        if outcome == "unconfirmed":
            track.state, track.note = "unconfirmed", LAUNCH_UNCONFIRMED
            self._ambiguous_launches.add(key)
            self._pending_launches.pop(key, None)
            self._sync_play_button(self._selected_game())
            self._sync_status(f"{name}: launch request sent; not confirmed yet.")
            return
        track.state = "running"
        track.confirmed = True
        track.note = PROFILE_UNCONFIRMED if outcome == "profile" else ""
        self._ambiguous_launches.discard(key)
        self._sync_play_button(self._selected_game())
        if game is not None:
            self._sync_status(f"{name}: launched via {NAMES[game.launcher]}.")
        self._publish_status(key)
        self.owner.session_changed(key, True)
        if outcome == "lost":
            C.QTimer.singleShot(2500, lambda: self._sample_session_lost(key))

    def _sample_session_lost(self, key):
        if self._tracked_state(key) == "running":
            self._session_keys.add(key)
            self._session_stream_lost()
            self._session_keys.discard(key)

    def start_outside(self):
        """Prototype controls: the selected game started from its own
        launcher, outside the wrapper. Observable, not ours to stop; it exits
        on its own after a while."""
        game = self._selected_game()
        if game is None or self._other_active_game():
            return
        key = game_key(game)
        if self._tracked_state(key) in library_ui._ACTIVE_GAME_STATES:
            return
        self._set_game_state(key, "external")
        self._tracked[key].note = EXTERNAL_NOTE
        self._sync_play_button(self._selected_game())
        self._sync_status(f"{game.name}: running outside PenguinBurner.")
        C.QTimer.singleShot(15000, lambda: self._sample_external_ended(key))

    def _sample_external_ended(self, key):
        if self._tracked_state(key) == "external":
            self._set_game_state(key, "stopped")
            game = self._game_for_key(key)
            self._sync_status(f"{game.name if game else key}: exited.")

    def _stop_game(self, game):
        key = game_key(game)
        launcher = NAMES[game.launcher]
        self._set_game_state(key, "stopping")
        self._publish_status(None)
        self._sync_status(f"{game.name}: requesting stop…")
        C.QTimer.singleShot(
            1500, lambda: self._sample_stopped(key, game.name, launcher)
        )

    def _sample_stopped(self, key, name, launcher):
        if self._tracked_state(key) == "stopping":
            self._set_game_state(key, "stopped")
            self._sync_status(f"{name}: Stop requested via {launcher}.")
            self.owner.session_changed(key, False)

    def _publish_status(self, key=None):
        """The window status band, as the browser's syncSessions sets it."""
        game = self._game_for_key(key) if key else None
        if game is None:
            if self._owns_status:
                self._owns_status = False
                self.owner.status_changed.emit(None)
            return
        setting = getattr(game.detail, "setting", None)
        mode = str(getattr(setting, "mode", None) or "stock").title()
        self._owns_status = True
        self.owner.status_changed.emit(
            {
                "running": f"Playing {game.name}",
                "detail": f"{mode} · GPU 0 · RTX 5080 · via {NAMES[game.launcher]}",
            }
        )

    _write_async = GameLibraryPanel._write


class LibraryPage(W.QWidget):
    selected_game_changed = C.Signal(object)
    # A game key ("<launcher>:<id>") whose Perf Profile the user opened.
    open_perf_profile = C.Signal(str)
    # {"running", "detail"} while a game plays, None after: the shell's
    # set_global_status, like the browser's setGlobalStatus.
    status_changed = C.Signal(object)
    # key, running, game mode ("stock", a tier or "adaptive") and its target
    # FPS: a sample session started or ended.
    game_session_changed = C.Signal(str, bool, str, int)
    # key, running, the tier the session records on ("" = it does not
    # record: Stock, unwrapped or Capture telemetry off). The host feeds its
    # perf source; the library never writes the source itself.
    recording_changed = C.Signal(str, bool, str)
    # key, enabled: the game's Capture telemetry setting changed.
    capture_changed = C.Signal(str, bool)
    # key, value: a launcher action changed a simulated state (Initialize or
    # Restart Steam made it "ready"); the host syncs Prototype controls.
    prototype_changed = C.Signal(str, object)

    def __init__(self, perf_source: PerfProfileSource, parent=None):
        super().__init__(parent)
        self.perf_source = perf_source
        # Prototype controls the sample launchers read; set_prototype
        # changes them.
        self.prototype = {
            "emptyLibrary": False,
            "launcherWriteState": "ready",
            "libraryScan": "normal",
            "libraryWrite": "ok",
            "nextLaunch": "normal",
        }
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.panel = FixturePanel(self)
        self.setStyleSheet(focus_visible(library_stylesheet()))
        layout.addWidget(self.panel.widget)
        normalize_group_headings(self.panel.widget)
        # Section headings sit inside their cards, as on every other page.
        for card in self.panel.widget.findChildren(W.QFrame, "prefGroupCard"):
            if card.parentWidget() is not None:
                heading_into_card(card.parentWidget())
        self.panel._restripe_groups()
        self.panel.fit_list_tools()
        # A live ring grows while its game plays: the sparkline follows, as
        # the real panel's game-state poll would refresh it. Runs only while
        # this page is shown and a session records.
        self.perf_timer = C.QTimer(self)
        self.perf_timer.setInterval(2000)
        self.perf_timer.timeout.connect(self._sync_live_perf)
        self._recording = False

    def has_recording(self, key):
        return bool(self.perf_source.has_recording(key))

    def selected_key(self):
        return self.panel._selected_key

    def return_from_perf_profile(self):
        """Back from the Perf Profile: the selection and filters never
        changed; focus returns to the entry without opening its peek."""
        button = self.panel.perf_button
        button.sync(self.panel._selected_key)
        if button.isVisible():
            button.setFocus(C.Qt.FocusReason.OtherFocusReason)

    def session_changed(self, key, running):
        game = self.panel._game_for_key(key)
        setting = getattr(getattr(game, "detail", None), "setting", None)
        mode = str(getattr(setting, "mode", "") or "stock")
        target = int(getattr(setting, "target_fps", 0) or DEFAULT_TARGET_FPS)
        # Recording rule: a wrapped game on a tier with Capture telemetry on.
        records = bool(setting and setting.enabled and setting.telemetry)
        tier = RECORDING_TIERS.get(mode, "") if records else ""
        self.recording_changed.emit(key, bool(running), tier)
        self._recording = bool(running and tier)
        self._sync_perf_timer()
        self.panel.perf_button.sync(self.panel._selected_key)
        self.panel.select_filters()
        self.game_session_changed.emit(key, bool(running), mode, target)

    def _sync_perf_timer(self):
        if self._recording and self.isVisible():
            if not self.perf_timer.isActive():
                self.perf_timer.start()
        else:
            self.perf_timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_perf_timer()
        if self._recording:
            self._sync_live_perf()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._sync_perf_timer()

    def _sync_live_perf(self):
        button = self.panel.perf_button
        if self.isVisible() and button.recording is not None and button.recording.live:
            button.sync(self.panel._selected_key)

    def set_prototype(self, state):
        """Prototype controls: library scan, Steam write state, launches,
        writes, an empty library and two GPUs."""
        changed = False
        for key in ("emptyLibrary", "launcherWriteState", "libraryScan"):
            if key in state and self.prototype.get(key) != state[key]:
                self.prototype[key] = state[key]
                changed = True
        for key in ("libraryWrite", "nextLaunch"):
            if key in state:
                self.prototype[key] = state[key]
        p = self.panel
        if "twoGpus" in state or "noGpu" in state:
            p._apply_gpu_choices(self._gpu_choices(state))
            changed = True
        if "libraryScan" in state:
            # Installed launchers change only with a scan, as in the app.
            p.rescan()
        elif changed:
            p._reload_games()
            p.select_filters()
            p._sync_selection(p._selected_game())

    def _gpu_choices(self, state):
        self._gpus = {**getattr(self, "_gpus", {}), **state}
        if self._gpus.get("noGpu"):
            return []
        gpus = [SimpleNamespace(name="GeForce RTX 5080", uuid="GPU-demo-5080")]
        if self._gpus.get("twoGpus"):
            gpus.append(SimpleNamespace(name="GeForce RTX 4070", uuid="GPU-demo-4070"))
        return gpus

    def prototype_action(self, name):
        """One-shot Prototype controls flows the library simulates."""
        if name == "libraryFirstScan":
            self.panel.replay_first_scan()
        elif name == "externalGame":
            self.panel.start_outside()

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
                p.reset_filters()
                break
        p._select_key(KEYS[index])

    def _sample_game(self, key):
        launcher, _, ident = key.partition(":")
        source = self.panel.sample_sources.get(launcher)
        return source.rows.get(ident) if source is not None else None

    def overlay_publishing(self, key):
        """Whether a running game publishes overlay telemetry: wrapped, its
        overlay on, and a renderer the Vulkan layer reaches."""
        game = self._sample_game(key)
        setting = getattr(getattr(game, "detail", None), "setting", None)
        return bool(
            game is not None
            and setting is not None
            and setting.enabled
            and setting.overlay
            and game.overlay_supported
        )

    def enable_capture(self, key):
        """The Perf Profile's "Turn on Capture telemetry": the same write as
        the game's own switch."""
        game = self._sample_game(key)
        if game is None:
            return
        p = self.panel
        source = p.sample_sources[game.launcher]
        source.edit(source.rows[game.game_id], "set_game_telemetry", True)
        self.capture_changed.emit(key, True)
        p._reload_games()
        p._sync_selection(p._selected_game())

    @staticmethod
    def adaptive_eligible(game):
        """The real panel's gate: Adaptive needs Vulkan frame data (no
        OpenGL) and a game its launcher can run."""
        return bool(game is not None and game.overlay_supported and game.ready)

    def adaptive_picker_state(self):
        """(selected key, [(key, name, effective target FPS)]) for the games
        Adaptive can run on, for Profiles' Use Adaptive for a game."""
        games = []
        for key in KEYS:
            game = self._sample_game(key)
            if game is not None and self.adaptive_eligible(game):
                target = game.detail.setting.target_fps or DEFAULT_TARGET_FPS
                games.append((key, game.name, int(target)))
        return self.panel._selected_key, games

    def apply_adaptive(self, index, target_fps):
        """Wrap the game on Adaptive with a per-game target. Refused (False)
        for a game the panel blocks Adaptive on."""
        if index not in range(len(KEYS)):
            return False
        game = self._sample_game(KEYS[index])
        if game is None or not self.adaptive_eligible(game):
            return False
        self.set_adaptive_available(True)
        self.select_game(index)
        p = self.panel
        source = p.sample_sources[game.launcher]
        for method, value in [
            ("set_game_enabled", True),
            ("set_game_mode", "adaptive"),
            ("set_game_target_fps", target_fps),
        ]:
            source.edit(source.rows[game.game_id], method, value)
        p._reload_games()
        p._sync_selection(p._selected_game())
        return True
