"""Faugus runner selection through the shared compatibility picker."""
from __future__ import annotations

import json
from pathlib import Path

from common.atomic_write import atomic_write_text
from integrations.launchers.compatibility import (
    CompatibilitySelection,
    CompatibilityTool,
)
from integrations.launchers.host_process import run_on_host

from . import compat_probe
from .config_store import FaugusConfigError, read_games_document
from .library import NATIVE_RUNNER, STEAM_RUNNER, InstalledFaugusGame
from .paths import faugus_installation, games_path


class FaugusCompatibility:
    def __init__(self, home: Path | None = None) -> None:
        self.home = home

    def discover(self) -> tuple[CompatibilityTool, ...]:
        request = {"home": str(self.home or Path.home()), "system": self.home is None}
        if self.home is not None:
            records = compat_probe.discover(request)
        else:
            installation = faugus_installation(self.home)
            if installation.command() is None:
                raise FaugusConfigError("The selected Faugus installation is unavailable. Rescan after installing it.")
            result = run_on_host(
                installation.python_command("-c", Path(compat_probe.__file__).read_text(), json.dumps(request)),
                capture=True, timeout=10,
            )
            if result is None or result.returncode:
                raise FaugusConfigError("Could not discover Faugus's Proton versions. Check the selected installation, then Rescan.")
            records = json.loads(result.stdout)
        if not isinstance(records, list) or not all(
            isinstance(item, list) and len(item) == 2 and all(isinstance(v, str) for v in item)
            for item in records
        ):
            raise FaugusConfigError("Faugus returned an invalid compatibility tool list.")
        return tuple(CompatibilityTool(value, label) for value, label in records)

    def _entry(self, game: InstalledFaugusGame, document: list[dict]) -> dict:
        for entry in document:
            if str(entry.get("gameid") or "").strip() == game.game_id:
                return entry
        raise FaugusConfigError(f"{game.game_id} is not in the Faugus library.")

    def selection(self, game: InstalledFaugusGame) -> CompatibilitySelection:
        entry = self._entry(game, read_games_document(self.home, strict=True))
        runner = entry.get("runner", "")
        if not isinstance(runner, str):
            raise FaugusConfigError("Invalid Faugus runner setting; fix it in Faugus first.")
        return CompatibilitySelection(
            runner, runner.replace("Proton-GE", "GE-Proton"), "UMU-Proton Latest",
            supported=runner not in (NATIVE_RUNNER, STEAM_RUNNER),
        )

    def write(self, game: InstalledFaugusGame, tool: CompatibilityTool | None) -> None:
        document = read_games_document(self.home, strict=True)
        entry = self._entry(game, document)
        if entry.get("runner") in (NATIVE_RUNNER, STEAM_RUNNER):
            raise FaugusConfigError("This game does not use a Faugus Proton runner.")
        entry["runner"] = tool.value if tool is not None else ""
        atomic_write_text(games_path(self.home), json.dumps(document, indent=4, ensure_ascii=False), durable=True)
