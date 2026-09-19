"""Reading the Heroic library out of its per-store caches."""

from __future__ import annotations

import hashlib
import json

from integrations.heroic.library import read_heroic_games
from integrations.heroic.paths import (
    ART_CARD_QUERY,
    game_art_path,
    game_config_path,
    heroic_config_root,
    heroic_installed,
)

ART_URL = "https://cdn.example/art/redacted.jpg"


def _heroic(tmp_path, *, flatpak: bool = False):
    root = (
        tmp_path / ".var/app/com.heroicgameslauncher.hgl/config/heroic"
        if flatpak
        else tmp_path / ".config/heroic"
    )
    (root / "store_cache").mkdir(parents=True)
    (root / "store").mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"defaultSettings": {}}))
    return root


def _library(root, games, *, name="legendary_library.json", key="library"):
    (root / "store_cache" / name).write_text(json.dumps({key: games}))


def _installed(root, runner, payload):
    """Write a store backend's installed.json, in that backend's own shape."""
    relative = {
        "legendary": "legendaryConfig/legendary/installed.json",
        "gog": "gog_store/installed.json",
        "nile": "nile_config/nile/installed.json",
    }[runner]
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _installed_legendary(root, *app_names, **fields):
    """The common case: mark these Epic games installed, as legendary does."""
    _installed(root, "legendary", {name: {"app_name": name, **fields} for name in app_names})


def _game(app_name="Turkey", **overrides):
    game = {
        "app_name": app_name,
        "title": "Borderlands",
        "runner": "legendary",
        "is_installed": True,
        "install": {"platform": "Windows", "install_path": "/games/bl"},
        "art_square": ART_URL,
    }
    game.update(overrides)
    return game


def test_a_machine_without_heroic_lists_nothing(tmp_path) -> None:
    """The tab explains itself instead of showing an empty list."""
    assert heroic_installed(tmp_path) is False
    assert read_heroic_games(tmp_path) == ()


def test_every_store_is_read_and_dlc_is_not_a_game(tmp_path) -> None:
    """Heroic lists DLC beside its game but never launches it on its own."""
    root = _heroic(tmp_path)
    _library(root, [_game(), _game("dlc-1", install={"is_dlc": True})])
    _library(root, [_game("1454", runner="gog")], name="gog_library.json", key="games")
    _installed_legendary(root, "Turkey", "dlc-1")
    _installed(root, "gog", {"installed": [{"appName": "1454"}]})
    (root / "sideload_apps").mkdir()
    (root / "sideload_apps" / "library.json").write_text(
        json.dumps({"games": [_game("side-1", runner="sideload")]})
    )

    games = read_heroic_games(tmp_path)

    assert {game.game_id for game in games} == {"Turkey", "1454", "side-1"}
    assert {game.runner_label for game in games} == {"Epic", "GOG", "Sideloaded"}


def test_uninstalled_games_have_nothing_to_wrap(tmp_path) -> None:
    root = _heroic(tmp_path)
    _library(root, [_game(is_installed=False)])

    assert read_heroic_games(tmp_path) == ()
    assert len(read_heroic_games(tmp_path, include_uninstalled=True)) == 1


def test_the_store_backend_decides_installed_not_the_cached_flag(tmp_path) -> None:
    """Heroic derives the cached flag from these files when it refreshes.

    Between refreshes the flag is stale, so a game installed a minute ago
    still reads false there -- and was invisible in the tab until its next
    library sync.
    """
    root = _heroic(tmp_path)
    _library(root, [_game("1584866499", runner="gog", is_installed=False)],
             name="gog_library.json", key="games")
    _installed(root, "gog", {"installed": [
        {"appName": "1584866499", "install_path": "/games/beat-cop",
         "platform": "windows", "is_dlc": False}
    ]})

    (game,) = read_heroic_games(tmp_path)

    assert game.installed is True
    assert game.install_path == "/games/beat-cop"


def test_each_store_backend_spells_its_installed_list_differently(tmp_path) -> None:
    """An object keyed by app name, a list under "installed", a bare list."""
    root = _heroic(tmp_path)
    _library(root, [
        _game("Turkey", is_installed=False),
        _game("1584866499", runner="gog", is_installed=False),
        _game("amzn1.adg.product.x", runner="nile", is_installed=False),
    ])
    _installed(root, "legendary", {"Turkey": {"app_name": "Turkey",
                                             "install_path": "/g/bl",
                                             "platform": "Windows"}})
    _installed(root, "gog", {"installed": [{"appName": "1584866499",
                                            "install_path": "/g/bc"}]})
    _installed(root, "nile", [{"id": "amzn1.adg.product.x", "path": "/g/az"}])

    games = read_heroic_games(tmp_path)

    assert {game.game_id for game in games} == {
        "Turkey", "1584866499", "amzn1.adg.product.x"
    }
    assert {game.install_path for game in games} == {"/g/bl", "/g/bc", "/g/az"}


def test_the_installed_platform_wins_over_the_platforms_the_store_sells(
    tmp_path,
) -> None:
    """A game with a Linux build installed for Windows runs under Proton.

    Reading is_linux_native instead would send the overlay's OpenGL probe
    after a Proton game, and mark it unsupported on what it found.
    """
    root = _heroic(tmp_path)
    _library(root, [_game("1584866499", runner="gog", is_installed=False,
                          is_linux_native=True)],
             name="gog_library.json", key="games")
    _installed(root, "gog", {"installed": [{"appName": "1584866499",
                                            "platform": "windows"}]})

    (game,) = read_heroic_games(tmp_path)

    assert (game.platform, game.is_native) == ("windows", False)


def test_dlc_the_store_recorded_is_still_not_a_game(tmp_path) -> None:
    root = _heroic(tmp_path)
    _library(root, [_game("redist", runner="gog", is_installed=True)],
             name="gog_library.json", key="games")
    _installed(root, "gog", {"installed": [{"appName": "redist",
                                            "is_dlc": True}]})

    assert read_heroic_games(tmp_path) == ()


def test_a_sideloaded_game_has_no_store_and_speaks_for_itself(tmp_path) -> None:
    """Nothing installed it for Heroic, so its entry is the only record."""
    root = _heroic(tmp_path)
    (root / "sideload_apps").mkdir()
    (root / "sideload_apps" / "library.json").write_text(
        json.dumps({"games": [_game("side-1", runner="sideload")]})
    )

    (game,) = read_heroic_games(tmp_path)

    assert game.game_id == "side-1"


def test_playtime_comes_from_the_session_record_in_hours(tmp_path) -> None:
    """Heroic counts minutes; the merged library compares hours."""
    root = _heroic(tmp_path)
    _library(root, [_game()])
    _installed_legendary(root, "Turkey")
    (root / "store" / "timestamp.json").write_text(
        json.dumps(
            {
                "Turkey": {
                    "lastPlayed": "2025-03-04T16:26:15.569Z",
                    "totalPlayed": 90,
                }
            }
        )
    )

    (game,) = read_heroic_games(tmp_path)

    assert game.playtime_hours == 1.5
    assert game.last_played == 1741105575


def test_an_unplayed_game_reports_nothing_rather_than_guessing(tmp_path) -> None:
    root = _heroic(tmp_path)
    _library(root, [_game()])
    _installed_legendary(root, "Turkey")
    (root / "store" / "timestamp.json").write_text(json.dumps({"Turkey": {}}))

    (game,) = read_heroic_games(tmp_path)

    assert (game.last_played, game.playtime_hours) == (0, 0.0)


def test_a_half_written_cache_leaves_the_tab_empty_not_broken(tmp_path) -> None:
    root = _heroic(tmp_path)
    (root / "store_cache" / "legendary_library.json").write_text("{not json")

    assert read_heroic_games(tmp_path) == ()


def test_art_is_the_image_heroic_already_downloaded(tmp_path) -> None:
    """Nothing is fetched: a library list must not go to the network.

    Epic art is cached under the sized URL the card asked for, GOG art under
    the plain one, so both spellings are tried.
    """
    root = _heroic(tmp_path)
    cache = root / "images-cache"
    cache.mkdir()
    sized = hashlib.sha256(f"{ART_URL}{ART_CARD_QUERY}".encode()).hexdigest()
    (cache / sized).write_bytes(b"\xff\xd8jpeg")

    assert game_art_path("Turkey", (ART_URL,), tmp_path) == cache / sized

    plain = hashlib.sha256(ART_URL.encode()).hexdigest()
    (cache / sized).unlink()
    (cache / plain).write_bytes(b"\xff\xd8jpeg")

    assert game_art_path("Turkey", (ART_URL,), tmp_path) == cache / plain
    assert game_art_path("Turkey", ("https://nothing/here.jpg",), tmp_path) is None


def test_a_downloaded_game_icon_wins_over_the_card_art(tmp_path) -> None:
    root = _heroic(tmp_path)
    (root / "icons").mkdir()
    icon = root / "icons" / "Turkey.jpg"
    icon.write_bytes(b"\xff\xd8jpeg")

    assert game_art_path("Turkey", (ART_URL,), tmp_path) == icon


def test_the_flatpak_tree_is_found_when_the_native_one_is_absent(tmp_path) -> None:
    root = _heroic(tmp_path, flatpak=True)

    assert heroic_config_root(tmp_path) == root
    assert heroic_installed(tmp_path) is True


def test_an_app_name_can_never_escape_the_config_directory(tmp_path) -> None:
    """The name is a store's string spliced into a filename."""
    for unusable in ("", "  ", "..", "../../etc/passwd", "a/b"):
        assert game_config_path(unusable, tmp_path) is None
    assert game_config_path("Turkey", tmp_path) is not None
