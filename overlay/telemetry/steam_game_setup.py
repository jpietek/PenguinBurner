from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from integrations.steam.vdf import quoted_tokens

from .steam_launch_check import (
    PENGUIN_BURNER_WRAPPER,
)

EXCLUDED_INSTALLED_APP_NAME_PREFIXES = (
    "Proton ",
    "Steam Linux Runtime",
    "Steamworks Common Redistributables",
)

INGAME_LATENCY_TOKENS = ("PB_INGAME_LATENCY=1",)
OVERLAY_TOKENS = ("PB_OVERLAY=1",)


@dataclass(frozen=True)
class SteamGamePreset:
    key: str
    name: str
    app_id: str


def game_preset(
    app_id: str,
    *,
    name: str | None = None,
) -> SteamGamePreset:
    """Build a game descriptor for an arbitrary Steam app id."""
    return SteamGamePreset(
        key=f"app-{app_id}",
        name=name or app_id,
        app_id=app_id,
    )


def build_game_launch_options(
    preset: SteamGamePreset,
    *,
    ingame_latency: bool = False,
    overlay: bool = False,
) -> str:
    # PENGUIN_BURNER is both the PATH wrapper name and the Flatpak host-layer
    # enable token. The optional in-game latency token enables dxvk-nvapi marker
    # parsing for explicit tests.
    tokens: list[str] = []
    if overlay:
        tokens.extend(OVERLAY_TOKENS)
    if ingame_latency:
        tokens.extend(INGAME_LATENCY_TOKENS)
    tokens.append(PENGUIN_BURNER_WRAPPER)
    tokens.append("%command%")
    return " ".join(tokens)


def installed_steam_game_presets(home: Path | None = None) -> tuple[SteamGamePreset, ...]:
    home = Path.home() if home is None else home
    presets: list[SteamGamePreset] = []
    seen_app_ids: set[str] = set()
    for steamapps_dir in default_steamapps_dirs(home):
        for manifest in sorted(steamapps_dir.glob("appmanifest_*.acf")):
            data = _manifest_fields(manifest)
            app_id = data.get("appid", "").strip()
            name = data.get("name", "").strip()
            if not app_id or not name or app_id in seen_app_ids:
                continue
            if _is_excluded_installed_app(name):
                continue
            seen_app_ids.add(app_id)
            presets.append(
                SteamGamePreset(
                    key=f"installed-{app_id}",
                    name=name,
                    app_id=app_id,
                )
            )
    return tuple(presets)


def default_steamapps_dirs(home: Path | None = None) -> tuple[Path, ...]:
    home = Path.home() if home is None else home
    candidates = [
        home / ".local" / "share" / "Steam" / "steamapps",
        home / ".steam" / "root" / "steamapps",
        home / ".steam" / "steam" / "steamapps",
    ]
    for base in tuple(candidates):
        candidates.extend(_library_steamapps_dirs(base / "libraryfolders.vdf"))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen or not path.is_dir():
            continue
        seen.add(resolved)
        unique.append(path)
    return tuple(unique)


def _library_steamapps_dirs(path: Path) -> tuple[Path, ...]:
    data = _manifest_fields(path)
    paths = []
    for value in data.get_all("path"):
        if value:
            paths.append(Path(value).expanduser() / "steamapps")
    return tuple(paths)


def _manifest_fields(path: Path) -> _ManifestFields:
    fields = _ManifestFields()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return fields
    for line in lines:
        tokens = quoted_tokens(line)
        if len(tokens) >= 2:
            fields.add(tokens[0], tokens[1])
    return fields


class _ManifestFields(dict[str, str]):
    def __init__(self) -> None:
        super().__init__()
        self._all: dict[str, list[str]] = {}

    def add(self, key: str, value: str) -> None:
        self.setdefault(key, value)
        self._all.setdefault(key, []).append(value)

    def get_all(self, key: str) -> tuple[str, ...]:
        return tuple(self._all.get(key, ()))


def _is_excluded_installed_app(name: str) -> bool:
    return name.startswith(EXCLUDED_INSTALLED_APP_NAME_PREFIXES)
