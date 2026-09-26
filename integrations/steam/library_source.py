"""Steam seen through the launcher contract the game library tab speaks.

A thin read-only face over SteamIntegrationManager. Every write still goes
through that manager: it is the only thing that knows how to splice our wrapper
into %command% and how to take it back out again.
"""

from __future__ import annotations

from pathlib import Path

from integrations.launchers.compatibility import (
    CompatibilitySelection,
    CompatibilityTool,
    compatibility_field,
)
from integrations.launchers.desktop_icons import desktop_icon
from integrations.launchers.library import (
    FIELD_MULTILINE,
    GROUP_COMMAND,
    WRITE_NEEDS_SETUP,
    WRITE_READY,
    LauncherBulkAction,
    LauncherField,
    LauncherWriteState,
    LibraryGame,
    library_bulk_actions,
)
from integrations.launchers.live_overlay import LiveOverlaySource

from .identity import STEAM_LAUNCHER_ID
from .launch_options import injection_state
from .library import steam_playtime_hours
from .manager import SteamGameRow, SteamIntegrationManager
from .process import launch_steam_game, restart_steam
from .users import default_steam_root, steam_installation


class SteamLibrarySource(LiveOverlaySource):
    launcher_id = STEAM_LAUNCHER_ID
    display_name = "Steam"
    #: Shipped fallback, used when the machine has no Steam icon of its own.
    icon_asset = "tab-steam.png"
    #: The icon Steam installs itself, preferred over our copy.
    desktop_icon_name = "steam"
    #: Steam exposes an API for starting a game; the tab offers Play for it.
    can_launch = True

    def watch_paths(self) -> tuple[Path, ...]:
        from overlay.telemetry.steam_game_setup import default_steamapps_dirs

        root = default_steam_root(self._home) or (self._home or Path.home()) / ".local/share/Steam"
        return (root / "config", root / "userdata",
                *root.glob("userdata/*/config"),
                *root.glob("userdata/*/config/localconfig.vdf"),
                *default_steamapps_dirs(self._home),
                *(row.game.steamapps_dir for row in self._rows))
    _LIVE_PROFILE_SETTERS = frozenset(
        {
            "set_game_enabled",
            "set_game_mode",
            "set_game_target_fps",
        }
    )

    def __init__(
        self,
        manager: SteamIntegrationManager | None = None,
        *,
        home: Path | None = None,
        settings_path: str | Path | None = None,
    ) -> None:
        self.manager = manager or SteamIntegrationManager(
            home=home,
            settings_path=settings_path,
        )
        self._home = home
        self._rows: tuple[SteamGameRow, ...] = ()
        self._playtime: dict[str, float] = {}
        # Probed during refresh, which the tab runs off the GUI thread: pgrep
        # plus a CDP connect is seconds of work in the worst case, and every
        # selection change would otherwise pay it again.
        self._probe: dict[str, object] = {}

    def desktop_icon(self):
        """Steam's own installed icon, or None when it has none here."""
        return desktop_icon(self.desktop_icon_name, self._home)

    def available(self) -> bool:
        """Whether a Steam installation exists here.

        The install directory, not the `steam` binary on PATH: a library is
        worth listing and configuring whether or not the client can be
        launched, and PATH is the wrong question inside a Flatpak anyway.
        """
        return default_steam_root(self._home) is not None

    def refresh(self, *, deep: bool = True) -> None:
        # A scan reads; the one write it may do is the manager re-adopting a
        # wrapped game whose settings entry is missing -- repairing our own
        # bookkeeping to match what Steam's config already says, never
        # touching Steam or a consistent library.
        #
        # The shallow pass skips reading every app's details back over CDP and
        # merges what the last deep pass cached instead. New installs and fresh
        # LastPlayed values still land, which is all the tab's timer needs.
        self._rows = self.manager.refresh(read_launch_options=deep)
        self._playtime = steam_playtime_hours(self._home)
        self._probe = self._probe_state()

    def _probe_state(self) -> dict[str, object]:
        """The Steam-side facts every later question is answered from."""
        user = self.manager.active_user()
        return {
            "marker": bool(self.manager.marker_present()),
            "running": bool(self.manager.steam_running()),
            "cdp_ready": bool(self.manager.cdp_ready()),
            "user": getattr(user, "display_name", "") if user is not None else "",
        }

    def games(self) -> tuple[LibraryGame, ...]:
        return tuple(self._library_game(row) for row in self._rows)

    def _library_game(self, row: SteamGameRow) -> LibraryGame:
        return LibraryGame(
            launcher=self.launcher_id,
            game_id=row.game.app_id,
            name=row.game.name,
            subtitle=str(row.game.runtime_label or ""),
            last_played=int(row.game.last_played or 0),
            installed_at=int(row.game.last_updated or 0),
            playtime_hours=float(self._playtime.get(row.game.app_id, 0.0)),
            art_path=row.game.art_path,
            ready=bool(row.game.ready),
            wrapped=bool(injection_state(row.launch_options).wrapped),
            enabled=bool(row.setting.enabled),
            overlay=bool(row.setting.overlay),
            detail=row,
        )

    # -- launcher fields -----------------------------------------------------

    def fields(self, game: LibraryGame) -> tuple[LauncherField, ...]:
        row = game.detail
        if not isinstance(row, SteamGameRow):
            return ()
        return (
            self._compat_tool_field(row),
            self._launch_options_field(row),
        )

    def _compat_tool_field(self, row: SteamGameRow) -> LauncherField:
        """Steam's live compatibility settings, rendered by the shared picker."""
        live = bool(self._probe.get("cdp_ready"))
        # A game Steam calls native has nothing to pick a Proton for -- unless
        # the user already forced one, in which case they keep the way back.
        native = bool(getattr(row.game, "is_native_linux", False)) and not row.game.compat_tool
        choices = self.manager.available_compat_tools(row.game.app_id) if live else ()
        return compatibility_field(
            CompatibilitySelection(
                value=str(row.game.compat_tool or ""),
                default_label=self._compat_default_label(row),
                supported=not native,
            ),
            tuple(CompatibilityTool(value, label) for value, label in choices),
            guidance=(
                "Keeps Steam's current choice by default. Selecting another "
                "entry uses Steam's own compatibility-tool setting for this game."
            ),
            error=("" if live else "Needs live apply: Proton is Steam's own setting, so the "
                   "client has to be running and connected to change it."),
        )

    @staticmethod
    def _compat_default_label(row: SteamGameRow) -> str:
        """Describe Steam's effective default without changing its override."""
        if row.game.compat_tool:
            return "Steam default"
        effective = str(getattr(row.game, "effective_compat_tool_label", "") or "")
        if effective:
            return f"Steam default ({effective})"
        if getattr(row.game, "is_native_linux", False):
            return "Native Linux — no compatibility tool"
        return "Steam default"

    def _launch_options_field(self, row: SteamGameRow) -> LauncherField:
        return LauncherField(
            key="launch_options",
            kind=FIELD_MULTILINE,
            title="Command",
            subtitle="Steam launch options — %command% is where the game goes",
            setter="set_raw_launch_options",
            value=str(row.launch_options or ""),
            group=GROUP_COMMAND,
        )

    def write_state(self) -> LauncherWriteState:
        """Whether Steam will take a setting now, and what to press if not.

        Two things can stand in the way, and each has one button behind it: the
        integration was never set up, or it was but Steam has been running since
        before it was -- in which case Steam holds the launch options in memory
        and would write ours back out on exit.
        """
        probe = self._probe or self._probe_state()
        note = f"Steam user: {probe['user']}" if probe.get("user") else "Steam user: —"
        if not probe.get("marker"):
            return LauncherWriteState(
                state=WRITE_NEEDS_SETUP,
                summary="not set up",
                detail=(
                    "Find your installed games and connect them to "
                    "PenguinBurner for simple per-game profiles."
                ),
                note=note,
                action_label="Scan my Steam library",
                action="initialize",
            )
        if probe.get("running") and not probe.get("cdp_ready"):
            return LauncherWriteState(
                state=WRITE_NEEDS_SETUP,
                summary="read-only until initialized",
                detail=(
                    "Restart Steam once to finish connecting per-game profiles. "
                    "Your games and saved settings stay exactly as they are."
                ),
                note=note,
                action_label="Restart Steam to finish",
                action="restart",
                confirm="Cleanly shut down and relaunch the Steam client now?",
            )
        return LauncherWriteState(
            state=WRITE_READY,
            summary="live apply" if probe.get("running") else "Steam stopped",
            note=note,
        )

    def restart(self):
        """Restart the Steam client. Named here because write_state points at it."""
        return restart_steam(home=self._home)

    def bulk_actions(self) -> tuple[LauncherBulkAction, ...]:
        return library_bulk_actions()

    def saved_overlay(self, game_id: str) -> bool:
        return self.manager.game_setting(game_id).overlay

    def after_setting_write(self, game_id: str, setter: str):
        """Push profile changes into a Steam game that is already running.

        Changing the target GPU still requires a relaunch, while overlay
        visibility is separate from the daemon profile. The remaining common
        profile controls can be re-issued to the daemon in place.
        """
        if setter not in self._LIVE_PROFILE_SETTERS:
            return super().after_setting_write(game_id, setter)
        return self.manager.hot_reapply(game_id)

    # -- launching -----------------------------------------------------------

    def launch(self, game_id: str) -> tuple[bool, str]:
        """Ask Steam to start a game. Returns (started, what to tell the user)."""
        try:
            steam_installation(self._home).ensure_integration()
        except (OSError, RuntimeError) as error:
            return False, f"FAILED to repair the PenguinBurner Steam integration ({error})"
        if launch_steam_game(game_id, home=self._home):
            return True, "launching via Steam…"
        return False, "FAILED to launch (steam not available)"

    def stop(self, game_id: str) -> tuple[bool, str]:
        result = self.manager.stop_game(game_id)
        if getattr(result, "ok", False):
            return True, "stopping…"
        return False, f"FAILED to stop ({getattr(result, 'message', '')})"

    def running_game_ids(self) -> frozenset[str] | None:
        """Which of this launcher's games are running, or None if unknowable.

        None is not "nothing is running": it means the check itself failed, and
        the caller must hold every state rather than read a stalled probe as
        every game having exited.
        """
        return self.manager.running_game_ids()

    def observed_processes(self) -> dict[str, tuple[int, ...]] | None:
        from .process import running_steam_processes

        return running_steam_processes()
