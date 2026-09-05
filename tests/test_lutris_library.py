"""Reading the Lutris library out of pga.db, including the ways it can fail."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from integrations.lutris.library import lutris_game, read_lutris_games
from integrations.lutris.paths import (
    game_config_path,
    game_cover_path,
    lutris_config_root,
    lutris_installed,
    lutris_library_db,
    runner_config_path,
    system_config_path,
)

_SCHEMA = """
create table games (
    id INTEGER PRIMARY KEY, name TEXT, sortname TEXT, slug TEXT,
    installer_slug TEXT, parent_slug TEXT, platform TEXT, runner TEXT,
    executable TEXT, directory TEXT, updated DATETIME, lastplayed INTEGER,
    installed INTEGER, installed_at INTEGER, year INTEGER, configpath TEXT,
    has_custom_banner INTEGER, has_custom_icon INTEGER,
    has_custom_coverart_big INTEGER, playtime REAL, service TEXT,
    service_id TEXT, discord_id TEXT
)
"""


def _library(home: Path, rows: list[dict]) -> Path:
    db = lutris_library_db(home)
    db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db)
    try:
        connection.execute(_SCHEMA)
        for row in rows:
            keys = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            connection.execute(
                f"insert into games ({keys}) values ({marks})", tuple(row.values())
            )
        connection.commit()
    finally:
        connection.close()
    return db


def _row(**overrides) -> dict:
    row = {
        "id": 27,
        "name": "Test Game",
        "slug": "test-game",
        "runner": "wine",
        "platform": "Windows",
        "installed": 1,
        "lastplayed": 1000,
        "playtime": 2.5,
        "configpath": "test-game-1784393690",
        "directory": "/games/test-game",
    }
    row.update(overrides)
    return row


# -- reading ------------------------------------------------------------------


def test_reads_a_game_with_its_config_and_cover(tmp_path) -> None:
    _library(tmp_path, [_row()])
    cover = tmp_path / ".local/share/lutris/coverart/test-game.jpg"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"jpeg")

    (game,) = read_lutris_games(tmp_path)

    assert game.game_id == "27"
    assert game.display_name == "Test Game"
    assert game.is_wine is True
    assert game.ready is True
    assert game.config_path == game_config_path("test-game-1784393690", tmp_path)
    assert game.cover_path == cover


def test_uninstalled_games_are_left_out_unless_asked_for(tmp_path) -> None:
    """An uninstalled entry has no prefix to wrap, so it would only pad the list."""
    _library(tmp_path, [_row(), _row(id=28, slug="gone", installed=0)])

    assert [g.game_id for g in read_lutris_games(tmp_path)] == ["27"]
    assert len(read_lutris_games(tmp_path, include_uninstalled=True)) == 2


def test_games_are_ordered_by_most_recently_played(tmp_path) -> None:
    _library(
        tmp_path,
        [
            _row(id=1, slug="old", name="Old", lastplayed=10),
            _row(id=2, slug="new", name="New", lastplayed=99),
        ],
    )

    assert [g.game_id for g in read_lutris_games(tmp_path)] == ["2", "1"]


def test_a_game_without_a_config_name_is_not_ready(tmp_path) -> None:
    _library(tmp_path, [_row(configpath="")])

    (game,) = read_lutris_games(tmp_path)

    assert game.config_path is None
    assert game.ready is False


def test_lookup_by_id_finds_uninstalled_games_too(tmp_path) -> None:
    _library(tmp_path, [_row(installed=0)])

    assert lutris_game("27", tmp_path) is not None
    assert lutris_game("999", tmp_path) is None


# -- degrading -----------------------------------------------------------------


def test_a_missing_library_reads_as_empty(tmp_path) -> None:
    """A machine without Lutris must leave the tab empty, not raise."""
    assert read_lutris_games(tmp_path) == ()
    assert lutris_installed(tmp_path) is False


def test_a_corrupt_library_reads_as_empty(tmp_path) -> None:
    db = lutris_library_db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is not a database")

    assert read_lutris_games(tmp_path) == ()
    assert lutris_installed(tmp_path) is True


def test_an_older_schema_without_our_columns_reads_as_empty(tmp_path) -> None:
    db = lutris_library_db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db)
    connection.execute("create table games (id INTEGER PRIMARY KEY, name TEXT)")
    connection.commit()
    connection.close()

    assert read_lutris_games(tmp_path) == ()


# -- path safety ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "..", "../../etc/passwd", "a/b", "a\\b"])
def test_a_config_name_cannot_escape_the_config_directory(name, tmp_path) -> None:
    """configpath is spliced into a filename straight out of the database."""
    assert game_config_path(name, tmp_path) is None


def test_a_cover_slug_cannot_escape_the_art_directory(tmp_path) -> None:
    assert game_cover_path("../secret", tmp_path) is None


def test_a_cover_falls_back_to_the_banner(tmp_path) -> None:
    banner = tmp_path / ".local/share/lutris/banners/test-game.jpg"
    banner.parent.mkdir(parents=True, exist_ok=True)
    banner.write_bytes(b"jpeg")

    assert game_cover_path("test-game", tmp_path) == banner


# -- Lutris's own icon ---------------------------------------------------------


def test_the_lutris_icon_is_found_where_lutris_installs_it(tmp_path) -> None:
    """Shown in the tab instead of shipping a copy of someone's logo."""
    from integrations.lutris.paths import lutris_desktop_icon

    icon = tmp_path / "icons/hicolor/scalable/apps/net.lutris.Lutris.svg"
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_text("<svg/>", encoding="utf-8")

    assert lutris_desktop_icon(data_dirs=[tmp_path]) == icon


def test_a_scalable_icon_wins_over_a_fixed_size_one(tmp_path) -> None:
    from integrations.lutris.paths import lutris_desktop_icon

    for relative in (
        "icons/hicolor/scalable/apps/net.lutris.Lutris.svg",
        "icons/hicolor/128x128/apps/net.lutris.Lutris.png",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    assert lutris_desktop_icon(data_dirs=[tmp_path]).suffix == ".svg"


def test_earlier_data_dirs_win(tmp_path) -> None:
    from integrations.lutris.paths import lutris_desktop_icon

    first, second = tmp_path / "a", tmp_path / "b"
    for root in (first, second):
        path = root / "icons/hicolor/128x128/apps/net.lutris.Lutris.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    assert lutris_desktop_icon(data_dirs=[first, second]).is_relative_to(first)


def test_no_lutris_installed_means_no_icon(tmp_path) -> None:
    """The tab then uses PenguinBurner's own glyph, which is right there."""
    from integrations.lutris.paths import lutris_desktop_icon

    assert lutris_desktop_icon(data_dirs=[tmp_path]) is None


def test_the_fallback_glyph_ships_with_the_package() -> None:
    from ui.assets import asset_image_path

    assert asset_image_path("tab-lutris.png").is_file()


# -- where the configs live ---------------------------------------------------


def test_configs_prefer_the_legacy_config_dir_when_it_exists(tmp_path) -> None:
    """Upstream Lutris still reads ~/.config/lutris first.

    lutris/settings.py only falls back to the data dir when the legacy config
    dir does not exist -- the deprecation is a fallback, not a migration -- so
    writing the data-dir copy on such a host produces YAML Lutris never loads
    and the wrapper silently never applies.
    """
    legacy = tmp_path / ".config" / "lutris"
    legacy.mkdir(parents=True)

    assert game_config_path("cfg", tmp_path) == legacy / "games" / "cfg.yml"
    assert runner_config_path("wine", tmp_path) == legacy / "runners" / "wine.yml"
    assert system_config_path(tmp_path) == legacy / "system.yml"
    # The library database is DATA_DIR state either way, like upstream's PGA_DB.
    assert lutris_library_db(tmp_path) == tmp_path / ".local/share/lutris/pga.db"


def test_configs_fall_back_to_the_data_dir_without_a_legacy_dir(tmp_path) -> None:
    data = tmp_path / ".local" / "share" / "lutris"

    assert game_config_path("cfg", tmp_path) == data / "games" / "cfg.yml"
    assert runner_config_path("wine", tmp_path) == data / "runners" / "wine.yml"
    assert system_config_path(tmp_path) == data / "system.yml"


def test_default_paths_honor_the_xdg_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    monkeypatch.setattr(
        "integrations.lutris.paths.FLATPAK_INFO_PATH", tmp_path / "not-flatpak"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    (tmp_path / "cfg" / "lutris").mkdir(parents=True)

    assert lutris_config_root() == tmp_path / "cfg" / "lutris"
    assert lutris_library_db() == tmp_path / "data" / "lutris" / "pga.db"


def test_penguinburner_flatpak_reads_lutris_from_the_host_home(
    tmp_path, monkeypatch
) -> None:
    """The sandbox XDG roots belong to PenguinBurner, not host Lutris."""
    monkeypatch.setenv("FLATPAK_ID", "io.github.jpietek.PenguinBurner")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(
        "XDG_DATA_HOME",
        str(tmp_path / ".var/app/io.github.jpietek.PenguinBurner/data"),
    )
    monkeypatch.setenv(
        "XDG_CONFIG_HOME",
        str(tmp_path / ".var/app/io.github.jpietek.PenguinBurner/config"),
    )
    host_config = tmp_path / ".config" / "lutris"
    host_config.mkdir(parents=True)

    assert lutris_library_db() == tmp_path / ".local/share/lutris/pga.db"
    assert lutris_config_root() == host_config


def test_an_explicit_home_ignores_the_session_xdg_environment(
    tmp_path, monkeypatch
) -> None:
    """The test seam has to isolate: an exported XDG var must not leak in."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))

    assert lutris_library_db(tmp_path) == tmp_path / ".local/share/lutris/pga.db"
    assert lutris_config_root(tmp_path) == tmp_path / ".local/share/lutris"
