from pathlib import Path
import json

from integrations.steam.settings import (
    SteamGameSetting,
    load_steam_game_settings,
    remove_steam_game_setting,
    steam_game_setting,
    store_steam_game_setting,
)
from profiles.game_profile import (
    GAME_MODE_ADAPTIVE,
    GAME_MODE_DEFAULT,
    GAME_MODE_NONE,
    GAME_MODE_STOCK,
    game_mode_uses_latency_markers,
    normalize_game_mode,
    normalize_game_target_fps,
)


def test_store_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "steam-game-settings.json"
    setting = SteamGameSetting(
        mode=GAME_MODE_ADAPTIVE,
        overlay=True,
        original_launch_options="gamemoderun %command%",
        injected_launch_options="gamemoderun PB_OVERLAY=1 PENGUIN_BURNER %command%",
    )
    store_steam_game_setting("78675700", "1089130", setting, path=path)
    assert steam_game_setting("78675700", "1089130", path=path) == setting


def test_accounts_are_isolated(tmp_path: Path) -> None:
    path = tmp_path / "steam-game-settings.json"
    jan = SteamGameSetting(mode="balanced")
    marzena = SteamGameSetting(mode="efficiency", overlay=True)
    store_steam_game_setting("78675700", "1089130", jan, path=path)
    store_steam_game_setting("1255210572", "1089130", marzena, path=path)
    assert steam_game_setting("78675700", "1089130", path=path) == jan
    assert steam_game_setting("1255210572", "1089130", path=path) == marzena


def test_remove_setting_drops_empty_account(tmp_path: Path) -> None:
    path = tmp_path / "steam-game-settings.json"
    store_steam_game_setting("78675700", "1089130", SteamGameSetting(), path=path)
    remove_steam_game_setting("78675700", "1089130", path=path)
    assert load_steam_game_settings(path) == {}


def test_corrupt_file_loads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "steam-game-settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_steam_game_settings(path) == {}


def test_normalize_game_mode_accepts_tier_aliases() -> None:
    assert normalize_game_mode("adaptive") == GAME_MODE_ADAPTIVE
    assert normalize_game_mode("stock") == GAME_MODE_STOCK
    assert normalize_game_mode("eff") == "efficiency"
    assert normalize_game_mode("perf") == "performance"
    assert normalize_game_mode("none") == GAME_MODE_NONE
    assert normalize_game_mode("bogus") == GAME_MODE_DEFAULT
    assert normalize_game_mode(None) == GAME_MODE_DEFAULT


def test_only_adaptive_mode_uses_hidden_latency_markers() -> None:
    assert game_mode_uses_latency_markers(GAME_MODE_ADAPTIVE)
    assert not game_mode_uses_latency_markers("balanced")
    assert not game_mode_uses_latency_markers(GAME_MODE_STOCK)


def test_setting_active_property() -> None:
    assert not SteamGameSetting().active
    assert SteamGameSetting(enabled=True).active


def test_target_fps_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "steam-game-settings.json"
    setting = SteamGameSetting(enabled=True, target_fps=120.0)
    store_steam_game_setting("78675700", "1089130", setting, path=path)
    assert steam_game_setting("78675700", "1089130", path=path) == setting


def test_gpu_uuid_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "steam-game-settings.json"
    setting = SteamGameSetting(enabled=True, gpu_uuid="GPU-stable-a")
    store_steam_game_setting("78675700", "1089130", setting, path=path)

    assert steam_game_setting("78675700", "1089130", path=path) == setting
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["format_version"] == 2


def test_target_fps_stays_unset_by_default(tmp_path: Path) -> None:
    path = tmp_path / "steam-game-settings.json"
    store_steam_game_setting("78675700", "1089130", SteamGameSetting(), path=path)
    loaded = steam_game_setting("78675700", "1089130", path=path)
    assert loaded is not None and loaded.target_fps is None
    assert "target_fps" not in path.read_text(encoding="utf-8")


def test_normalize_game_target_fps_bounds() -> None:
    assert normalize_game_target_fps(None) is None
    assert normalize_game_target_fps("bogus") is None
    assert normalize_game_target_fps(float("nan")) is None
    assert normalize_game_target_fps(0.5) is None
    assert normalize_game_target_fps(14) is None
    assert normalize_game_target_fps(2000) is None
    assert normalize_game_target_fps(15) == 15.0
    assert normalize_game_target_fps("120") == 120.0
    assert normalize_game_target_fps(60) == 60.0


def test_legacy_hidden_mode_loads_as_adaptive(tmp_path: Path) -> None:
    path = tmp_path / "steam-game-settings.json"
    path.write_text(
        json.dumps(
            {
                "accounts": {
                    "78675700": {
                        "games": {
                            "1089130": {
                                "enabled": True,
                                "mode": "default",
                                "injected_launch_options": (
                                    "PENGUIN_BURNER --pb-overlay=0 %command%"
                                ),
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    setting = steam_game_setting("78675700", "1089130", path=path)

    assert setting is not None
    assert setting.enabled is True
    assert setting.mode == GAME_MODE_ADAPTIVE
