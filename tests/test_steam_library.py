from dataclasses import replace
from pathlib import Path

from integrations.steam.library import (
    game_icon_path,
    installed_steam_games,
)


def _write_manifest(
    steamapps: Path,
    app_id: str,
    name: str,
    *,
    state_flags: int = 4,
    last_played: str = "0",
    last_updated: str = "0",
) -> None:
    (steamapps / f"appmanifest_{app_id}.acf").write_text(
        "\n".join(
            [
                '"AppState"',
                "{",
                f'\t"appid"\t\t"{app_id}"',
                f'\t"name"\t\t"{name}"',
                f'\t"StateFlags"\t\t"{state_flags}"',
                f'\t"LastUpdated"\t\t"{last_updated}"',
                f'\t"LastPlayed"\t\t"{last_played}"',
                f'\t"installdir"\t\t"{name}"',
                "}",
            ]
        ),
        encoding="utf-8",
    )


def _steam_home(tmp_path: Path) -> Path:
    root = tmp_path / ".local" / "share" / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    return root


def test_scans_ready_games_and_filters_tools(tmp_path: Path) -> None:
    root = _steam_home(tmp_path)
    steamapps = root / "steamapps"
    _write_manifest(steamapps, "10", "Zeta Game")
    _write_manifest(steamapps, "20", "Alpha Game", state_flags=6)
    _write_manifest(steamapps, "30", "Proton 10.0")
    _write_manifest(steamapps, "40", "Steam Linux Runtime 4.0")
    _write_manifest(steamapps, "50", "Updating Game", state_flags=1026 | 1)
    games = installed_steam_games(tmp_path)
    # Sorted by name; StateFlags 6 (update queued, bit 4 set) still counts as
    # installed, uninstalling bit 1 does not, tools/runtimes are excluded.
    assert [game.name for game in games] == ["Alpha Game", "Zeta Game"]
    assert all(game.ready for game in games)


def test_reads_compat_tool_mapping(tmp_path: Path) -> None:
    root = _steam_home(tmp_path)
    _write_manifest(root / "steamapps", "10", "Windows Game")
    _write_manifest(root / "steamapps", "20", "Native Game")
    (root / "config" / "config.vdf").write_text(
        "\n".join(
            [
                '"InstallConfigStore"',
                "{",
                '\t"Software"',
                "\t{",
                '\t\t"Valve"',
                "\t\t{",
                '\t\t\t"Steam"',
                "\t\t\t{",
                '\t\t\t\t"CompatToolMapping"',
                "\t\t\t\t{",
                '\t\t\t\t\t"10"',
                "\t\t\t\t\t{",
                '\t\t\t\t\t\t"name"\t\t"proton_experimental"',
                "\t\t\t\t\t}",
                "\t\t\t\t}",
                "\t\t\t}",
                "\t\t}",
                "\t}",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    games = {game.app_id: game for game in installed_steam_games(tmp_path)}
    assert games["10"].is_proton and games["10"].compat_tool == "proton_experimental"
    assert not games["20"].is_proton
    assert not games["20"].runtime_known
    assert not games["20"].is_native_linux

    steam_reported_native = replace(games["20"], effective_compat_tool="")
    assert steam_reported_native.runtime_known
    assert steam_reported_native.is_native_linux


def test_reads_last_played_timestamp_from_manifest(tmp_path: Path) -> None:
    root = _steam_home(tmp_path)
    _write_manifest(
        root / "steamapps", "10", "Recently Played", last_played="1783870852"
    )
    _write_manifest(root / "steamapps", "20", "Never Played")

    games = {game.app_id: game for game in installed_steam_games(tmp_path)}

    assert games["10"].last_played == 1783870852
    assert games["20"].last_played == 0


def test_reads_last_updated_timestamp_from_manifest(tmp_path: Path) -> None:
    root = _steam_home(tmp_path)
    _write_manifest(
        root / "steamapps", "10", "Recently Installed", last_updated="1783870852"
    )
    _write_manifest(root / "steamapps", "20", "No Update Time")

    games = {game.app_id: game for game in installed_steam_games(tmp_path)}

    assert games["10"].last_updated == 1783870852
    assert games["20"].last_updated == 0


def test_invalid_last_played_timestamp_is_treated_as_never_played(
    tmp_path: Path,
) -> None:
    root = _steam_home(tmp_path)
    _write_manifest(root / "steamapps", "10", "Broken Date", last_played="nope")

    assert installed_steam_games(tmp_path)[0].last_played == 0


def test_icon_prefers_small_library_cache_jpg(tmp_path: Path) -> None:
    root = _steam_home(tmp_path)
    cache = root / "appcache" / "librarycache" / "10"
    cache.mkdir(parents=True)
    (cache / "aaaa.jpg").write_bytes(b"x" * 500)
    (cache / "header.jpg").write_bytes(b"x" * 40000)
    (cache / "huge.jpg").write_bytes(b"x" * 200000)
    icon = game_icon_path("10", steam_root=root)
    assert icon is not None and icon.name == "aaaa.jpg"


def test_icon_supports_legacy_flat_layout(tmp_path: Path) -> None:
    root = _steam_home(tmp_path)
    cache = root / "appcache" / "librarycache"
    cache.mkdir(parents=True)
    (cache / "10_icon.jpg").write_bytes(b"x" * 500)
    icon = game_icon_path("10", steam_root=root)
    assert icon is not None and icon.name == "10_icon.jpg"


def test_icon_missing_yields_none(tmp_path: Path) -> None:
    root = _steam_home(tmp_path)
    assert game_icon_path("10", steam_root=root) is None
    assert game_icon_path("10", steam_root=None) is None
