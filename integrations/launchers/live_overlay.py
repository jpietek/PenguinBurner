"""Required live overlay behavior for every Game Library launcher."""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable

from .host_process import run_on_host, running_in_flatpak
from .wrapper_manager import ApplyResult

# Host-side and stdlib-only so PB's Flatpak can inspect native and sandboxed
# games. Return only game identities, never environment values or credentials.
_WRAPPED_GAMES = """
import json, os, sys
from pathlib import Path
keys = set()
for proc in Path(sys.argv[1]).iterdir():
    if not proc.name.isdecimal():
        continue
    try:
        if proc.stat().st_uid != os.getuid():
            continue
        env = dict(item.split(b'=', 1) for item in (proc / 'environ').read_bytes().split(b'\\0') if b'=' in item)
        if not (env.get(b'PENGUIN_BURNER_SESSION_ID') or
                env.get(b'PENGUIN_BURNER_TELEMETRY_SESSION') == proc.name.encode()):
            continue
        if (proc / 'stat').read_text().rsplit(')', 1)[1].split()[0] == 'Z':
            continue
        key = env.get(b'PENGUIN_BURNER_GAME_KEY', b'').decode()
        if not key:
            app = next((env[name].decode() for name in (b'SteamAppId', b'STEAM_COMPAT_APP_ID', b'SteamGameId') if env.get(name)), '')
            key = 'steam:' + app if app.isdecimal() else ''
        if key:
            keys.add(key)
    except (OSError, UnicodeError):
        continue
print(json.dumps(sorted(keys)))
"""


def wrapped_game_keys() -> frozenset[str] | None:
    """Live wrapper identities, independent of GPU-profile application."""
    python = (os.environ.get("PENGUIN_BURNER_HOST_PYTHON") or "/usr/bin/python3"
              if running_in_flatpak() else sys.executable)
    result = run_on_host([python, "-c", _WRAPPED_GAMES, "/proc"], capture=True)
    if result is None or result.returncode:
        return None
    try:
        keys = json.loads(result.stdout)
        return frozenset(keys) if isinstance(keys, list) and all(isinstance(k, str) for k in keys) else None
    except ValueError:
        return None


class LiveOverlaySource:
    """Launcher contract: inherit both follow-ups; supply settings and liveness.

    Profile follow-ups may extend after_setting_write via super(). Bulk results
    must list only successfully saved ids in applied_game_ids.
    """

    launcher_id: str

    @property
    def non_game_ids(self) -> frozenset[str]:
        """Known store-client entries, excluded from the game-session guard."""
        return getattr(getattr(self, "manager", None), "non_game_ids", frozenset())

    def saved_overlay(self, game_id: str) -> bool:
        raise NotImplementedError

    def running_game_ids(self) -> frozenset[str] | None:
        raise NotImplementedError

    def after_setting_write(self, game_id: str, setter: str) -> object | None:
        if setter == "set_game_overlay":
            return self._reapply_overlay((game_id,))
        return None

    def after_bulk_write(self, setter: str, result) -> ApplyResult | None:
        if setter == "set_all_games_overlay":
            return self._reapply_overlay(result.applied_game_ids)
        return None

    def _reapply_overlay(self, game_ids: Iterable[str]) -> ApplyResult | None:
        ids = set(game_ids)
        if not ids:
            return None
        running = self.running_game_ids()
        if running is None:
            return ApplyResult(False, "Overlay saved, but running games could not be checked for a live update.")
        selected = ids.intersection(running)
        if not selected:
            return None
        wrapped = wrapped_game_keys()
        if wrapped is None:
            return ApplyResult(False, "Overlay saved, but the running wrapper could not be checked for a live update.")
        confirmed = {game_id for game_id in selected if f"{self.launcher_id}:{game_id}" in wrapped}
        messages = []
        if confirmed:
            from overlay.state import write_overlay_override

            values = {self.saved_overlay(game_id) for game_id in confirmed}
            # The existing native-layer override is shared by running games.
            # Never pick an arbitrary winner if callers supply mixed choices.
            if len(values) != 1:
                return ApplyResult(False, "Overlay saved; conflicting live visibility choices require a relaunch.")
            enabled = values.pop()
            if not write_overlay_override(enabled):
                return ApplyResult(False, "Overlay saved, but live visibility update failed.")
            messages.append(
                f"Overlay {'show' if enabled else 'hide'} request sent live; "
                "loaded overlays update within one second."
            )
        unconfirmed = selected - confirmed
        if unconfirmed:
            messages.append("Overlay saved; PenguinBurner is unconfirmed for a running game. "
                            "Close it, then relaunch with Play to load the overlay.")
        return ApplyResult(not unconfirmed, " ".join(messages))
