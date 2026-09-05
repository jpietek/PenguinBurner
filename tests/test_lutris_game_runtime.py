"""The wrapper side: turning a --pb-lutris-id flag into a daemon profile apply."""

from __future__ import annotations

from types import SimpleNamespace

from integrations.lutris import game_runtime
from integrations.lutris.game_runtime import (
    apply_lutris_game_runtime_profile,
    game_id_from_env,
    lutris_app_id,
    lutris_runtime_profile_argv,
)
from integrations.lutris.settings import LutrisGameSetting, store_lutris_game_setting
from overlay.launcher import _consume_wrapper_flags
from overlay.wrapper_tokens import LUTRIS_GAME_ID_ENV
from profiles import game_profile
from profiles.game_profile import GAME_MODE_ADAPTIVE, GAME_MODE_STOCK
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR


def _one_gpu(monkeypatch, uuid="GPU-1"):
    monkeypatch.setattr(
        game_runtime.DaemonGpuClient,
        "discover_identities",
        staticmethod(lambda: [SimpleNamespace(uuid=uuid, index=0)]),
    )


def _profiles(monkeypatch, profile_id="perf-1"):
    monkeypatch.setattr(game_profile, "read_auto_uv_profiles", list)
    monkeypatch.setattr(
        game_profile,
        "resolve_profile_tier_profiles",
        lambda _profiles, **_kwargs: {"performance": {"profile_id": profile_id}},
    )


# -- the flag ------------------------------------------------------------------


def test_the_wrapper_reads_the_game_id_off_argv(tmp_path) -> None:
    """Lutris publishes no stable id, so the tab writes ours onto the command."""
    env: dict[str, str] = {}

    rest = _consume_wrapper_flags(
        ["--pb-overlay=1", "--pb-lutris-id=27", "game-performance", "wine"], env
    )

    assert env[LUTRIS_GAME_ID_ENV] == "27"
    assert env["PB_OVERLAY"] == "1"
    assert rest == ["game-performance", "wine"]


def test_a_launch_without_the_flag_carries_no_lutris_identity() -> None:
    env: dict[str, str] = {}

    _consume_wrapper_flags(["--pb-overlay=0", "wine"], env)

    assert LUTRIS_GAME_ID_ENV not in env


def test_only_a_numeric_id_counts() -> None:
    assert game_id_from_env({LUTRIS_GAME_ID_ENV: "27"}) == "27"
    assert game_id_from_env({LUTRIS_GAME_ID_ENV: "not-an-id"}) == ""
    assert game_id_from_env({}) == ""


def test_the_daemon_id_is_namespaced_away_from_steam_app_ids() -> None:
    """Lutris game 27 and Steam app 27 are different games."""
    assert lutris_app_id("27") == "lutris:27"
    assert lutris_app_id("") == ""


# -- resolution ----------------------------------------------------------------


def test_argv_resolves_the_stored_setting_for_that_game(tmp_path, monkeypatch) -> None:
    _one_gpu(monkeypatch)
    _profiles(monkeypatch)
    settings = tmp_path / "settings.json"
    store_lutris_game_setting(
        "27",
        LutrisGameSetting(enabled=True, mode=GAME_MODE_ADAPTIVE, target_fps=120.0),
        path=settings,
    )

    resolved = lutris_runtime_profile_argv(
        {LUTRIS_GAME_ID_ENV: "27"}, settings_path=settings
    )

    assert resolved is not None
    argv, app_id = resolved
    assert app_id == "lutris:27"
    assert "--adaptive-auto-uv" in argv
    assert argv[argv.index("--adaptive-target-fps") + 1] == "120"


def test_a_stock_setting_pins_factory_state(tmp_path, monkeypatch) -> None:
    _one_gpu(monkeypatch)
    settings = tmp_path / "settings.json"
    store_lutris_game_setting(
        "27", LutrisGameSetting(enabled=True, mode=GAME_MODE_STOCK), path=settings
    )

    resolved = lutris_runtime_profile_argv(
        {LUTRIS_GAME_ID_ENV: "27"}, settings_path=settings
    )

    assert resolved is not None
    assert resolved[0][:2] == ["--auto-uv-profile", STOCK_PROFILE_SELECTOR]


def test_a_disabled_setting_resolves_to_nothing(tmp_path, monkeypatch) -> None:
    _one_gpu(monkeypatch)
    settings = tmp_path / "settings.json"
    store_lutris_game_setting(
        "27", LutrisGameSetting(enabled=False), path=settings
    )

    assert (
        lutris_runtime_profile_argv(
            {LUTRIS_GAME_ID_ENV: "27"}, settings_path=settings
        )
        is None
    )


def test_a_game_with_no_stored_setting_resolves_to_nothing(tmp_path) -> None:
    assert (
        lutris_runtime_profile_argv(
            {LUTRIS_GAME_ID_ENV: "27"}, settings_path=tmp_path / "absent.json"
        )
        is None
    )


# -- soft failure --------------------------------------------------------------


def test_an_unreachable_daemon_never_blocks_the_launch(
    tmp_path, monkeypatch, capsys
) -> None:
    _one_gpu(monkeypatch)
    _profiles(monkeypatch)
    settings = tmp_path / "settings.json"
    store_lutris_game_setting(
        "27", LutrisGameSetting(enabled=True, mode=GAME_MODE_ADAPTIVE), path=settings
    )
    from runtime import daemon_client

    def boom(*_args, **_kwargs):
        raise OSError("socket is not there")

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", boom)

    applied = apply_lutris_game_runtime_profile(
        {LUTRIS_GAME_ID_ENV: "27"}, settings_path=settings, watch_pid=1234
    )

    assert applied is False
    assert "profile apply skipped" in capsys.readouterr().err


def test_a_daemon_refusal_is_reported_not_claimed(
    tmp_path, monkeypatch, capsys
) -> None:
    _one_gpu(monkeypatch)
    _profiles(monkeypatch)
    settings = tmp_path / "settings.json"
    store_lutris_game_setting(
        "27", LutrisGameSetting(enabled=True, mode=GAME_MODE_ADAPTIVE), path=settings
    )
    from runtime import daemon_client

    monkeypatch.setattr(
        daemon_client,
        "start_game_runtime_profile",
        lambda *a, **k: {"ignored": True, "reason": "another game owns the runtime"},
    )

    applied = apply_lutris_game_runtime_profile(
        {LUTRIS_GAME_ID_ENV: "27"}, settings_path=settings, watch_pid=1234
    )

    assert applied is False
    assert "another game owns the runtime" in capsys.readouterr().err
