"""The Heroic side of the shared wrapper write path, and its library adapter."""

from __future__ import annotations

import json

import pytest

from integrations.heroic.library_source import HeroicLibrarySource
from integrations.heroic.manager import HeroicIntegrationManager
from integrations.heroic.settings import load_heroic_game_settings
from integrations.launchers import wrapper_manager
from profiles.game_profile import GAME_MODE_ADAPTIVE

WRAPPER_FLAG = "--pb-game-id=heroic:Turkey"


@pytest.fixture(autouse=True)
def _no_host_repair(monkeypatch):
    """Outside a Flatpak this is a no-op; here it must never touch the host."""
    monkeypatch.setattr(wrapper_manager, "ensure_host_integration", lambda: None)


def _home(
    tmp_path,
    *,
    defaults=("game-performance",),
    game=None,
    platform="Windows",
    app_names=("Turkey",),
):
    root = tmp_path / ".config" / "heroic"
    (root / "GamesConfig").mkdir(parents=True, exist_ok=True)
    (root / "store_cache").mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(
        json.dumps(
            {
                "defaultSettings": {
                    "wrapperOptions": [{"exe": exe, "args": ""} for exe in defaults]
                }
            }
        )
    )
    (root / "store_cache" / "legendary_library.json").write_text(
        json.dumps(
            {
                "library": [
                    {
                        "app_name": app_name,
                        "title": "Borderlands",
                        "runner": "legendary",
                        "is_installed": True,
                    }
                    for app_name in app_names
                ]
            }
        )
    )
    # What is installed, and where, is the store backend's record -- Heroic
    # only copies it into the cached library when it next refreshes.
    installed = root / "legendaryConfig" / "legendary" / "installed.json"
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(
        json.dumps(
            {
                app_name: {
                    "app_name": app_name,
                    "platform": platform,
                    "install_path": "/g/bl",
                }
                for app_name in app_names
            }
        )
    )
    if game is not None:
        (root / "GamesConfig" / "Turkey.json").write_text(json.dumps(game))
    return tmp_path


def _manager(tmp_path, **kwargs) -> HeroicIntegrationManager:
    manager = HeroicIntegrationManager(
        home=_home(tmp_path, **kwargs),
        settings_path=tmp_path / "heroic-game-settings.json",
    )
    manager.refresh()
    return manager


def _wrappers(tmp_path) -> list[dict]:
    path = tmp_path / ".config" / "heroic" / "GamesConfig" / "Turkey.json"
    return json.loads(path.read_text())["Turkey"].get("wrapperOptions", [])


def _stored(tmp_path):
    return load_heroic_game_settings(tmp_path / "heroic-game-settings.json")["Turkey"]


def test_rows_carry_what_the_game_actually_launches_through(tmp_path) -> None:
    manager = _manager(tmp_path)

    (row,) = manager.rows()

    assert row.command == "game-performance"
    assert row.inherited is True
    assert row.source_label == "Heroic global settings"
    assert row.wrapped is False


def test_enabling_keeps_the_wrappers_the_game_inherited(tmp_path) -> None:
    """Writing the game level replaces the global rows, so they come along.

    Adaptive also asks for latency markers, and the opt-in has to be set
    before the wrapper is the thing running -- so it rides an `env` prefix,
    which Heroic can start because it spawns wrappers as plain argv.
    """
    manager = _manager(tmp_path)

    assert manager.set_game_enabled("Turkey", True).ok

    assert _wrappers(tmp_path) == [
        {
            "exe": "env",
            "args": (
                f"PB_INGAME_LATENCY=1 PENGUIN_BURNER --pb-overlay=0 {WRAPPER_FLAG}"
            ),
        },
        {"exe": "game-performance", "args": ""},
    ]
    assert _stored(tmp_path).original_command == "game-performance"


def test_disabling_lets_the_global_wrappers_take_over_again(tmp_path) -> None:
    """A game we found inheriting must be left inheriting, not frozen."""
    manager = _manager(tmp_path)
    manager.set_game_enabled("Turkey", True)

    assert manager.set_game_enabled("Turkey", False).ok

    assert _wrappers(tmp_path) == []
    row = manager.row("Turkey")
    assert row is not None
    assert (row.command, row.inherited) == ("game-performance", True)


def test_a_games_own_wrappers_are_restored_rather_than_dropped(tmp_path) -> None:
    manager = _manager(tmp_path, game={"Turkey": {"wrapperOptions": [{"exe": "mangohud", "args": ""}]}})
    manager.set_game_enabled("Turkey", True)

    assert manager.set_game_enabled("Turkey", False).ok

    assert _wrappers(tmp_path) == [{"exe": "mangohud", "args": ""}]


def test_the_overlay_switch_rewrites_only_our_own_row(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.set_game_enabled("Turkey", True)

    assert manager.set_game_overlay("Turkey", True).ok

    rows = _wrappers(tmp_path)
    # Overlay on, so the wrapper turns the markers on by itself and the env
    # prefix goes away with them.
    assert rows[0] == {
        "exe": "PENGUIN_BURNER",
        "args": f"--pb-overlay=1 {WRAPPER_FLAG}",
    }
    assert rows[1:] == [{"exe": "game-performance", "args": ""}]


def test_a_hand_edit_re_reads_the_toggles_from_what_landed(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.set_game_enabled("Turkey", True)

    assert manager.set_game_wrapper_command("Turkey", "mangohud").ok

    stored = _stored(tmp_path)
    assert stored.enabled is False
    assert stored.original_command == "mangohud"


def test_settings_land_in_heroics_own_file(tmp_path) -> None:
    """App names and Lutris ids must not share one map."""
    manager = _manager(tmp_path)

    manager.set_game_mode("Turkey", GAME_MODE_ADAPTIVE)

    assert (tmp_path / "heroic-game-settings.json").is_file()


def test_the_library_adapter_describes_the_game_the_tab_draws(tmp_path) -> None:
    source = HeroicLibrarySource(_manager(tmp_path), home=tmp_path)
    source.refresh()

    (game,) = source.games()

    assert (game.launcher, game.game_id, game.name) == ("heroic", "Turkey", "Borderlands")
    assert game.subtitle == "Epic"
    assert game.overlay_supported is True  # Proton translates everything to Vulkan
    (field,) = source.fields(game)
    assert field.key == "wrapper_command"
    assert field.setter == "set_game_wrapper_command"
    assert "inherited from Heroic global settings" in field.subtitle


def test_a_linux_native_game_is_the_only_one_the_overlay_can_miss(
    tmp_path, monkeypatch
) -> None:
    """Proton translates everything to Vulkan; a native build need not be.

    The probe walks the install directory, so it must run on the scan worker
    and be answered from the cache afterwards -- never on the GUI thread.
    """
    from integrations.heroic import library_source as heroic_source

    source = HeroicLibrarySource(_manager(tmp_path), home=tmp_path)
    probed: list[str] = []
    monkeypatch.setattr(
        heroic_source,
        "overlay_support",
        lambda **kwargs: probed.append(str(kwargs.get("directory"))) or (False, "OpenGL"),
    )

    source.refresh()
    assert probed == []  # a Windows game never reaches the probe

    _home(tmp_path, platform="linux")
    source.refresh()

    (game,) = source.games()
    assert (game.overlay_supported, game.overlay_unsupported_reason) == (
        False,
        "OpenGL",
    )
    assert probed == ["/g/bl"]

    # The view reads the cached fact; a cheap tick never probes disk again.
    source.refresh(deep=False)
    source.games()
    assert probed == ["/g/bl"]


def test_the_global_wrappers_are_read_once_a_scan_not_once_a_game(
    tmp_path, monkeypatch
) -> None:
    """Every game without wrappers of its own falls back to that one file.

    Reading it per game turned a library scan -- which the tab repeats on a
    timer -- into one full parse of Heroic's whole settings block per title.
    """
    import integrations.heroic.manager as manager_module

    home = _home(tmp_path, app_names=("Turkey", "Eel", "Wren"))
    manager = HeroicIntegrationManager(
        home=home, settings_path=tmp_path / "heroic-game-settings.json"
    )
    reads: list[bool] = []
    original = manager_module.read_global_entries
    monkeypatch.setattr(
        manager_module,
        "read_global_entries",
        lambda where: reads.append(True) or original(where),
    )

    rows = manager.refresh()

    assert len(rows) == 3
    assert reads == [True]
    assert {row.command for row in rows} == {"game-performance"}
