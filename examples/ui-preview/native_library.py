"""The Game Library page over session-local game and telemetry fixtures.

The page and its panel are the real ones (``ui.features.library``); only
the launchers and the game session are simulated here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6 import QtCore as C

from integrations.launchers.compatibility import (
    CompatibilitySelection,
    CompatibilityTool,
    compatibility_field,
)
from integrations.launchers.library import (
    FIELD_MULTILINE,
    FIELD_TEXT,
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
from ui.features.library import library_page
from ui.features.library.library_page import LibraryPanel

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
KEYS = [f"{launcher}:{ident}" for launcher, ident, *_ in GAMES]
NAMES = {"steam": "Steam", "heroic": "Heroic", "lutris": "Lutris", "faugus": "Faugus"}
COMMANDS = {
    "steam": "Steam launch options — %command% is where the game goes",
    "heroic": "Wrapper command in the Heroic settings",
    "lutris": "prefix_command in the Lutris config",
    "faugus": "Launch arguments in the Faugus game settings",
}


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


class FixturePanel(LibraryPanel):
    """The real Game Library page panel over sample launchers.

    Only what needs a backend is simulated: the launcher reads and the game
    session (Play, Stop). The layout, scan threads, loading page, write-state
    gating and its fix-it action, writes, the All games menu, the Play button
    states, session reporting and the status line are the page's own code.
    """

    def __init__(self, owner):
        self.eligible = False
        # Seconds the sample Steam read takes (0 while building, so the
        # window opens on a filled list; the first-scan replay sets it).
        self.sample_scan_delay = 0.0
        self._action_rescan = False
        super().__init__(
            owner,
            sources=[Source(name) for name in NAMES],
            gpu_choices=lambda: (),
            adaptive_available=lambda *_: self.eligible,
        )
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
        self._reload_games()
        self.select_filters()

    def _build_settings(self):
        with patch.object(
            library_ui, "adaptive_target_fps_from_env", return_value=DEFAULT_TARGET_FPS
        ):
            return super()._build_settings()

    def _sync_target_controls(self, adaptive):
        with patch.object(
            library_ui, "adaptive_target_fps_from_env", return_value=DEFAULT_TARGET_FPS
        ):
            super()._sync_target_controls(adaptive)

    def _sync_selection(self, game):
        with patch.object(
            library_ui, "adaptive_target_fps_from_env", return_value=DEFAULT_TARGET_FPS
        ):
            super()._sync_selection(game)

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
        # The page reports the session (and the window status) from here.
        self._sync_play_button(self._selected_game())
        if game is not None:
            self._sync_status(f"{name}: launched via {NAMES[game.launcher]}.")
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
        self._sync_status(f"{game.name}: requesting stop…")
        C.QTimer.singleShot(
            1500, lambda: self._sample_stopped(key, game.name, launcher)
        )

    def _sample_stopped(self, key, name, launcher):
        if self._tracked_state(key) == "stopping":
            self._set_game_state(key, "stopped")
            self._sync_status(f"{name}: Stop requested via {launcher}.")

    _write_async = GameLibraryPanel._write


class LibraryPage(library_page.LibraryPage):
    """The real page over the fixture panel, plus Prototype controls.

    Indexes (``select_game``, ``apply_adaptive``) count ``GAMES``.
    """

    # key, value: a launcher action changed a simulated state (Initialize or
    # Restart Steam made it "ready"); the host syncs Prototype controls.
    prototype_changed = C.Signal(str, object)

    def build_panel(self, **_options):
        # Prototype controls the sample launchers read; set_prototype
        # changes them.
        self.prototype = {
            "emptyLibrary": False,
            "launcherWriteState": "ready",
            "libraryScan": "normal",
            "libraryWrite": "ok",
            "nextLaunch": "normal",
        }
        return FixturePanel(self)

    def system_target_fps(self):
        return DEFAULT_TARGET_FPS

    def recording_mode(self, game):
        """The sample ring records on the tier's name; Adaptive picks its
        tier live and the sample session runs on Balanced."""
        return RECORDING_TIERS.get(super().recording_mode(game), "")

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
        if index in range(len(KEYS)):
            super().select_game(KEYS[index])

    def _sample_game(self, key):
        launcher, _, ident = key.partition(":")
        source = self.panel.sample_sources.get(launcher)
        return source.rows.get(ident) if source is not None else None

    def adaptive_eligible(self, game):
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
