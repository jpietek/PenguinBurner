"""Qt-free orchestration for the Steam tab.

Owns the reconciliation between three stores: the installed library
(manifests), PenguinBurner's per-account game settings, and Steam's per-game
launch options (live via CDP while Steam runs, localconfig on disk when it
does not). The UI panel stays a thin view over this.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from common.flatpak_wrappers import ensure_host_integration
from overlay.telemetry.steam_launch_check import (
    launch_options_from_localconfig,
    rewrite_launch_options,
)
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR

from .cdp import (
    SteamAppDetails,
    SteamCdpClient,
    SteamCdpError,
    cdp_available,
    cdp_marker_present,
    ensure_cdp_marker,
)
from .launch_options import (
    inject_launch_options,
    injection_state,
    launch_options_problems,
    remove_injection,
)
from .library import InstalledSteamGame, installed_steam_games
from .process import running_steam_game_ids, steam_running
from .settings import (
    SteamGameSetting,
    load_steam_game_settings,
    store_steam_game_setting,
)
from profiles.game_profile import (
    GAME_MODE_ADAPTIVE,
    GAME_MODE_DEFAULT,
    GAME_MODE_NONE,
    game_mode_uses_latency_markers,
    normalize_game_mode,
    normalize_game_target_fps,
)
from .users import SteamUser, active_steam_user


@dataclass(frozen=True)
class SteamGameRow:
    game: InstalledSteamGame
    setting: SteamGameSetting
    launch_options: str


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    message: str
    launch_options: str = ""


class SteamIntegrationManager:
    def __init__(
        self,
        *,
        home: Path | None = None,
        settings_path: str | Path | None = None,
    ) -> None:
        self._home = home
        self._settings_path = settings_path
        self._launch_options: dict[str, str] = {}
        self._compat_tools: dict[str, tuple[tuple[str, str], ...]] = {}
        # "This Steam session cannot answer" is a global fact, remembered so
        # browsing a game list does not pay a fresh 5s CDP attempt per row.
        # Cleared on the next deep refresh, when Steam may have come back.
        self._compat_tools_unavailable = False
        self._app_details: dict[str, SteamAppDetails] = {}

    # -- status ------------------------------------------------------------

    def active_user(self) -> SteamUser | None:
        return active_steam_user(self._home)

    def marker_present(self) -> bool:
        return cdp_marker_present(self._home)

    def steam_running(self) -> bool:
        return steam_running()

    def cdp_ready(self) -> bool:
        return cdp_available(timeout_s=1.0)

    def running_game_ids(self) -> frozenset[str] | None:
        """Every running Steam game's app id in one subprocess (for polling).

        None means the check failed (distinct from an empty set = no games).
        """
        return running_steam_game_ids()

    def stop_game(self, app_id: str) -> ApplyResult:
        """Ask the running Steam client to shut the game down cleanly.

        Steam exposes no stop equivalent of ``-applaunch``; TerminateApp over
        the CDP channel is the client's own Stop button, so stopping needs
        live apply to be initialized.
        """
        # isdecimal, not isdigit: it admits exactly the strings int() accepts,
        # so terminate_app's int() below can never raise past this guard.
        if not str(app_id).isdecimal():
            return ApplyResult(False, "invalid app id")
        try:
            with SteamCdpClient(timeout_s=3.0) as client:
                if not client.terminate_app_supported():
                    return ApplyResult(
                        False, "this Steam build does not expose TerminateApp"
                    )
                client.terminate_app(app_id)
                return ApplyResult(True, "Stop requested via Steam.")
        except SteamCdpError as error:
            if not self.steam_running():
                return ApplyResult(False, "Steam is not running")
            if not self.cdp_ready():
                return ApplyResult(
                    False,
                    "stopping needs live apply; initialize the Steam "
                    "integration (one Steam restart) first",
                )
            return ApplyResult(False, f"stop failed: {error}")

    def initialize(self) -> ApplyResult:
        if not ensure_cdp_marker(self._home):
            return ApplyResult(False, "Steam installation not found")
        if self.cdp_ready():
            return ApplyResult(True, "Live apply already active.")
        return ApplyResult(
            True,
            "Marker created. Restart Steam once to activate live apply.",
        )

    # -- rows ----------------------------------------------------------------

    def refresh(
        self,
        *,
        read_launch_options: bool = True,
    ) -> tuple[SteamGameRow, ...]:
        games = installed_steam_games(self._home)
        if read_launch_options:
            games = self._read_all_app_details(games)
            # Steam may have come back (or gone away) since the last deep
            # pass; let the next compat-tool question ask it again.
            self._compat_tools_unavailable = False
        else:
            games = self._merge_cached_app_details(games)
        user = self.active_user()
        settings = (
            load_steam_game_settings(self._settings_path).get(user.account_id, {})
            if user is not None
            else {}
        )
        if self._adopt_wrapped_games(games, settings):
            settings = (
                load_steam_game_settings(self._settings_path).get(user.account_id, {})
                if user is not None
                else {}
            )
        return tuple(
            SteamGameRow(
                game=game,
                setting=settings.get(game.app_id, SteamGameSetting()),
                launch_options=self._launch_options.get(game.app_id, ""),
            )
            for game in games
        )

    def _adopt_wrapped_games(
        self,
        games: tuple[InstalledSteamGame, ...],
        settings: dict[str, SteamGameSetting],
    ) -> bool:
        """Recreate the settings entry of a game that already runs our wrapper.

        The wrapper in a launch line IS user intent, durably recorded in
        Steam's own config. After a reinstall or a migrated home the settings
        file is gone while the wrapper still runs; without adoption such a game
        shows "Not wrapped" and launches with no per-game profile behind the
        wrapper. This repairs our own bookkeeping -- it never writes to Steam
        -- and only touches wrapped games with no entry, so a scan of an
        already-consistent library writes nothing.
        """
        if self.active_user() is None:
            return False
        adopted = False
        for game in games:
            if game.app_id in settings:
                continue
            current = self._launch_options.get(game.app_id, "")
            state = injection_state(current)
            if not state.wrapped:
                continue
            self._store(
                game.app_id,
                SteamGameSetting(
                    enabled=True,
                    mode=GAME_MODE_ADAPTIVE,
                    overlay=state.overlay,
                    ingame_latency=state.ingame_latency,
                    original_launch_options=remove_injection(current),
                    injected_launch_options=current,
                ),
            )
            adopted = True
        return adopted

    def _read_all_app_details(
        self,
        games: tuple[InstalledSteamGame, ...],
    ) -> tuple[InstalledSteamGame, ...]:
        """Read Steam's live per-app facts, including its effective Proton.

        The config.vdf mapping only records explicit overrides. Steam's API is
        authoritative for the runtime it actually selected through defaults.
        """
        try:
            with SteamCdpClient(timeout_s=3.0) as client:
                fresh_details = client.app_details_many(game.app_id for game in games)
        except SteamCdpError:
            fresh_details = {}
        if fresh_details:
            self._app_details.update(fresh_details)
            self._launch_options.update(
                {
                    app_id: details.launch_options
                    for app_id, details in fresh_details.items()
                }
            )
            return self._merge_cached_app_details(games)
        if self.steam_running():
            # localconfig on disk is stale while Steam runs; keep the cache.
            return self._merge_cached_app_details(games)
        user = self.active_user()
        if user is None:
            return self._merge_cached_app_details(games)
        try:
            text = user.localconfig_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return self._merge_cached_app_details(games)
        self._launch_options = {
            game.app_id: launch_options_from_localconfig(text, game.app_id) or ""
            for game in games
        }
        return self._merge_cached_app_details(games)

    def _merge_cached_app_details(
        self,
        games: tuple[InstalledSteamGame, ...],
    ) -> tuple[InstalledSteamGame, ...]:
        return tuple(
            self._game_with_app_details(game, self._app_details.get(game.app_id))
            for game in games
        )

    @staticmethod
    def _game_with_app_details(
        game: InstalledSteamGame,
        details: SteamAppDetails | None,
    ) -> InstalledSteamGame:
        if details is None:
            return game
        return replace(
            game,
            effective_compat_tool=details.compat_tool_name,
            effective_compat_tool_display=details.compat_tool_display_name,
            effective_compat_tool_priority=details.compat_tool_priority,
            effective_platforms=details.platforms,
        )

    # -- mutations -----------------------------------------------------------

    def set_game_mode(self, app_id: str, mode: str) -> ApplyResult:
        mode = normalize_game_mode(mode)
        setting = self._setting(app_id)
        return self._apply(app_id, replace(setting, mode=mode))

    def set_game_gpu(self, app_id: str, gpu_uuid: str) -> ApplyResult:
        setting = self._setting(app_id)
        return self._apply(
            app_id,
            replace(setting, gpu_uuid=str(gpu_uuid or "").strip()),
        )

    def set_game_enabled(self, app_id: str, enabled: bool) -> ApplyResult:
        setting = self._setting(app_id)
        return self._apply(
            app_id,
            replace(
                setting,
                enabled=bool(enabled),
                overlay=setting.overlay if enabled else False,
            ),
        )

    def set_game_overlay(self, app_id: str, overlay: bool) -> ApplyResult:
        setting = self._setting(app_id)
        return self._apply(app_id, replace(setting, overlay=bool(overlay)))

    def set_game_target_fps(self, app_id: str, target_fps: float | None) -> ApplyResult:
        setting = self._setting(app_id)
        return self._apply(
            app_id,
            replace(setting, target_fps=normalize_game_target_fps(target_fps)),
        )

    def set_all_games_enabled(self, app_ids, enabled: bool) -> ApplyResult:
        """Bulk wrapper toggle. Enabling keeps each game's overlay flag (off
        unless the user opted in per game); disabling restores each game's
        original launch options."""
        return self._apply_to_games(
            app_ids,
            lambda app_id: self.set_game_enabled(app_id, bool(enabled)),
            "enabled" if enabled else "disabled",
        )

    def set_all_games_overlay(self, app_ids, overlay: bool) -> ApplyResult:
        return self._apply_to_games(
            app_ids,
            lambda app_id: self.set_game_overlay(app_id, bool(overlay)),
            "overlay shown" if overlay else "overlay hidden",
            live_overlay=True,
        )

    def _apply_to_games(self, app_ids, action, label: str, *, live_overlay: bool = False) -> ApplyResult:
        ids = [str(app_id) for app_id in app_ids]
        failed: list[str] = []
        for app_id in ids:
            result = action(app_id)
            if not result.ok:
                failed.append(app_id)
        changed = len(ids) - len(failed)
        # One daemon-status probe instead of one per game: only games the
        # daemon is currently watching can pick the change up live.
        applied = set(ids) - set(failed)
        live_problems: list[str] = []
        if live_overlay:
            for app_id in (self.running_game_ids() or frozenset()) & applied:
                result = self.hot_reapply_overlay(app_id)
                if result is not None and not result.ok:
                    live_problems.append(result.message)
        else:
            for app_id in self._watched_running_app_ids() & applied:
                self.hot_reapply(app_id)
        games_word = "game" if changed == 1 else "games"
        message = f"PenguinBurner {label} for {changed} {games_word}."
        if live_problems:
            message += " " + " ".join(live_problems)
        if failed:
            return ApplyResult(
                False,
                f"{message} Failed for {len(failed)}: {', '.join(failed[:5])}.",
            )
        return ApplyResult(not live_problems, message)

    def _watched_running_app_ids(self) -> frozenset[str]:
        from runtime.daemon_client import daemon_status

        try:
            status = daemon_status(timeout_s=1.0)
        except Exception:
            return frozenset()
        watched = (status.get("game_runtime") or {}).get("watched") or []
        return frozenset(
            str(entry.get("app_id"))
            for entry in watched
            if entry.get("app_id")
        )

    def available_compat_tools(self, app_id: str) -> tuple[tuple[str, str], ...]:
        if app_id in self._compat_tools:
            return self._compat_tools[app_id]
        if self._compat_tools_unavailable:
            return ()
        try:
            with SteamCdpClient(timeout_s=5.0) as client:
                if not client.compat_tool_selection_supported():
                    self._compat_tools_unavailable = True
                    return ()
                tools = client.available_compat_tools(app_id)
        except SteamCdpError:
            self._compat_tools_unavailable = True
            return ()
        self._compat_tools[app_id] = tools
        return tools

    def set_game_compat_tool(self, app_id: str, tool_name: str) -> ApplyResult:
        """Change Proton through Steam itself; empty restores Steam Default."""
        if not str(app_id).isdigit():
            return ApplyResult(False, "invalid app id")
        tool_name = str(tool_name).strip()
        available = {name for name, _display in self.available_compat_tools(app_id)}
        if tool_name and tool_name not in available:
            return ApplyResult(False, "selected compatibility tool is unavailable")
        try:
            with SteamCdpClient(timeout_s=5.0) as client:
                if not client.compat_tool_selection_supported():
                    return ApplyResult(False, "this Steam build cannot change Proton live")
                client.specify_compat_tool(app_id, tool_name)
                details = client.app_details(app_id)
        except SteamCdpError as error:
            return ApplyResult(False, f"Proton change failed: {error}")
        if details is not None:
            self._app_details[app_id] = details
            self._launch_options[app_id] = details.launch_options
        return ApplyResult(
            True,
            "Using Steam default compatibility tool."
            if not tool_name
            else f"Compatibility tool changed to {tool_name}.",
        )

    def set_raw_launch_options(self, app_id: str, text: str) -> ApplyResult:
        """User-edited launch string: validate, write verbatim, resync setting."""
        text = text.strip()
        problems = launch_options_problems(text)
        if problems:
            return ApplyResult(False, "; ".join(problems))
        write = self._write_launch_options(app_id, text)
        if not write.ok:
            return write
        setting = self._setting(app_id)
        state = injection_state(text)
        if state.wrapped:
            self._store(
                app_id,
                replace(
                    setting,
                    overlay=state.overlay,
                    # Injection deliberately leaves the latency flag out while
                    # the overlay is on (the wrapper runs the markers anyway),
                    # so its absence on such a line says nothing about the
                    # stored opt-in; only an overlay-off line speaks for it.
                    ingame_latency=(
                        setting.ingame_latency
                        if state.overlay
                        else state.ingame_latency
                    ),
                    original_launch_options=(
                        setting.original_launch_options
                        if setting.active
                        else remove_injection(text)
                    ),
                    injected_launch_options=text,
                ),
            )
        elif setting.active:
            # The user removed our wrapper by hand; the preset is inert now.
            self._store(
                app_id,
                replace(
                    setting,
                    enabled=False,
                    mode=GAME_MODE_ADAPTIVE,
                    overlay=False,
                    original_launch_options=text,
                    injected_launch_options="",
                ),
            )
        return write

    def hot_reapply(self, app_id: str) -> ApplyResult | None:
        """Push a changed preset onto the game if it is running right now.

        The daemon reports which game sessions it is watching; re-issuing the
        request with the same watch PID swaps the active profile in place.
        None means the game is not running (nothing to do).
        """
        from runtime.daemon_client import daemon_status, start_game_runtime_profile

        from drivers.nvidia.daemon_gpu import DaemonGpuClient

        from profiles.game_profile import game_gpu_target, profile_argv_for_setting

        try:
            status = daemon_status(timeout_s=1.0)
        except Exception:
            return None
        watched = (status.get("game_runtime") or {}).get("watched") or []
        pids = [
            int(entry["pid"])
            for entry in watched
            if str(entry.get("app_id")) == str(app_id) and entry.get("pid")
        ]
        if not pids:
            return None
        setting = self._setting(app_id)
        argv = profile_argv_for_setting(setting)
        profile_mode_requested = setting.enabled and setting.mode not in {
            GAME_MODE_DEFAULT,
            GAME_MODE_NONE,
        }
        if profile_mode_requested:
            try:
                identities = list(DaemonGpuClient.discover_identities())
            except Exception as error:
                return ApplyResult(False, f"could not detect target GPU: {error}")
            target = game_gpu_target(setting, identities)
            if target is None:
                return ApplyResult(False, "the saved target GPU is not currently available")
            gpu_uuid, gpu_index = target
            argv = profile_argv_for_setting(
                setting,
                gpu_index=gpu_index,
                gpu_uuid=gpu_uuid,
                include_legacy_profiles=len(identities) == 1,
            )
        if argv is None:
            game_runtime = status.get("game_runtime") or {}
            standing_mode = str(game_runtime.get("standing_runtime_mode") or "")
            standing_profile_id = str(
                game_runtime.get("standing_profile_id") or ""
            ).strip()
            if standing_mode == "adaptive":
                argv = [
                    "--auto-uv-profile",
                    standing_profile_id or "latest",
                    "--adaptive-auto-uv",
                ]
            elif standing_mode == "static" and standing_profile_id:
                argv = ["--auto-uv-profile", standing_profile_id]
            elif standing_mode == "stock":
                argv = ["--auto-uv-profile", STOCK_PROFILE_SELECTOR]
            else:
                return ApplyResult(False, "standing Default profile is unavailable")
        try:
            result = start_game_runtime_profile(
                argv,
                watch_pid=pids[0],
                app_id=app_id,
            )
        except Exception as error:
            if "not a running process" in str(error):
                # The game exited within the daemon's restore grace window.
                return ApplyResult(True, "Game just exited; applies on next launch.")
            return ApplyResult(False, f"live profile re-apply failed: {error}")
        if bool(result.get("ignored")) or not bool(result.get("started", True)):
            reason = str(result.get("reason") or "daemon did not start the profile")
            return ApplyResult(False, f"live profile re-apply skipped: {reason}")
        return ApplyResult(True, "Profile re-applied to the running game.")

    def hot_reapply_overlay(self, app_id: str) -> ApplyResult | None:
        """Update the native layer only when this game's session is running."""
        from overlay.state import write_overlay_override

        running = self.running_game_ids()
        if running is None or str(app_id) not in running:
            return None
        enabled = self._setting(app_id).overlay
        if not write_overlay_override(enabled):
            return ApplyResult(False, "Overlay saved, but live visibility update failed.")
        return ApplyResult(True, f"Overlay switched {'on' if enabled else 'off'} live.")

    def _apply(self, app_id: str, setting: SteamGameSetting) -> ApplyResult:
        # Marker capture is an Adaptive prerequisite, not a second preference
        # users must keep in sync with the selected mode. The overlay still
        # controls only whether the HUD is drawn.
        setting = replace(
            setting,
            ingame_latency=game_mode_uses_latency_markers(setting.mode),
        )
        current = self._launch_options.get(app_id)
        if current is None:
            # No cache entry means this game's last read failed (a per-app CDP
            # timeout leaves the others populated). Composing a new line from
            # "" would overwrite whatever the user really has in Steam, and
            # recording "" as the original destroys the restore path -- so
            # refuse rather than guess.
            return ApplyResult(
                False,
                "this game's current launch options could not be read; "
                "rescan and try again",
            )
        if setting.active:
            try:
                ensure_host_integration()
            except (OSError, RuntimeError) as error:
                return ApplyResult(
                    False,
                    f"PenguinBurner Steam integration repair failed: {error}",
                )
            desired = inject_launch_options(
                current,
                overlay=setting.overlay,
                ingame_latency=setting.ingame_latency,
            )
            # The stored original is a snapshot taken when the wrapper last
            # went in, and it is only still the original while the wrapper is
            # still there. On an unwrapped line the live command *is* the
            # original: the user may have edited it in Steam since, or taken
            # our wrapper out by hand. Preferring a stale snapshot over it
            # restores an older command -- and if that snapshot was itself
            # recorded from a bad read, hands the user back a broken one.
            original = (
                setting.original_launch_options
                if setting.original_launch_options and injection_state(current).wrapped
                else remove_injection(current)
            )
        else:
            desired = remove_injection(
                current,
                stored_original=setting.original_launch_options or None,
                stored_injected=setting.injected_launch_options or None,
            )
            original = setting.original_launch_options
        message = "Applied."
        if desired != current:
            write = self._write_launch_options(app_id, desired)
            if not write.ok:
                return write
            message = write.message
        if setting.active:
            self._store(
                app_id,
                replace(
                    setting,
                    original_launch_options=original,
                    injected_launch_options=desired,
                ),
            )
        else:
            # Disabled is a durable per-game choice. Keep the preselected mode
            # while restoring the original command and removing our wrapper.
            self._store(
                app_id,
                replace(
                    setting,
                    original_launch_options=desired,
                    injected_launch_options="",
                ),
            )
        return ApplyResult(True, message, launch_options=desired)

    # -- helpers -------------------------------------------------------------

    def _store(self, app_id: str, setting: SteamGameSetting) -> None:
        user = self.active_user()
        store_steam_game_setting(
            user.account_id if user is not None else "",
            app_id,
            setting,
            path=self._settings_path,
            display_name=user.display_name if user is not None else "",
        )

    def _setting(self, app_id: str) -> SteamGameSetting:
        user = self.active_user()
        if user is None:
            return SteamGameSetting()
        return (
            load_steam_game_settings(self._settings_path)
            .get(user.account_id, {})
            .get(app_id, SteamGameSetting())
        )

    def _write_launch_options(self, app_id: str, value: str) -> ApplyResult:
        try:
            with SteamCdpClient(timeout_s=3.0) as client:
                if client.set_app_launch_options(app_id, value):
                    self._launch_options[app_id] = value
                    return ApplyResult(True, "Applied live.", launch_options=value)
                return ApplyResult(
                    False, "Steam did not accept the launch options (read-back mismatch)"
                )
        except SteamCdpError:
            pass
        if self.steam_running():
            return ApplyResult(
                False,
                "Steam is running without live apply. Initialize the Steam "
                "integration (one Steam restart) or close Steam and retry.",
            )
        user = self.active_user()
        if user is None:
            return ApplyResult(False, "No Steam account found")
        # Steam was checked immediately above and is not running. Read the
        # file back, but do not impose the generic delayed-clobber watch: an
        # absent client has nothing left to flush and every toggle otherwise
        # pays that fixed latency.
        result = rewrite_launch_options(
            app_id=app_id,
            launch_options=value,
            config_paths=[user.localconfig_path],
            verify_delay_s=0.0,
        )
        if not result.persisted:
            return ApplyResult(False, "Failed to write localconfig.vdf")
        self._launch_options[app_id] = value
        return ApplyResult(True, "Applied to Steam config.", launch_options=value)
