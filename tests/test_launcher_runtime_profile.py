"""The wrapper side: turning a --pb-game-id flag into a daemon profile apply."""

from __future__ import annotations

from types import SimpleNamespace

from integrations.launchers import runtime_profile
from integrations.launchers.runtime_profile import (
    apply_game_key_profile,
    game_setting,
)
from integrations.lutris.settings import LutrisGameSetting, store_lutris_game_setting
from overlay.launcher import _consume_wrapper_flags
from overlay.wrapper_tokens import GAME_KEY_ENV
from profiles import game_profile
from profiles.game_profile import GAME_MODE_ADAPTIVE, GAME_MODE_STOCK
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR


def _one_gpu(monkeypatch, uuid="GPU-1"):
    monkeypatch.setattr(
        runtime_profile.DaemonGpuClient,
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


def _stored(tmp_path, setting) -> str:
    settings = tmp_path / "settings.json"
    store_lutris_game_setting("27", setting, path=settings)
    return settings


def _argv(key: str, settings_path):
    setting = game_setting(key, settings_path=settings_path)
    return None if setting is None else runtime_profile.profile_argv(setting)


# -- the flag ------------------------------------------------------------------


def test_the_wrapper_reads_the_game_key_off_argv() -> None:
    """Lutris and Heroic publish no stable id, so the tab writes ours on."""
    env: dict[str, str] = {}

    rest = _consume_wrapper_flags(
        ["--pb-overlay=1", "--pb-game-id=lutris:27", "game-performance", "wine"], env
    )

    assert env[GAME_KEY_ENV] == "lutris:27"
    assert env["PB_OVERLAY"] == "1"
    assert rest == ["game-performance", "wine"]


def test_the_flag_earlier_versions_wrote_still_names_a_lutris_game() -> None:
    """Configured prefix_commands out there still carry --pb-lutris-id."""
    env: dict[str, str] = {}

    _consume_wrapper_flags(["--pb-overlay=0", "--pb-lutris-id=27", "wine"], env)

    assert env[GAME_KEY_ENV] == "lutris:27"


def test_a_launch_without_the_flag_carries_no_identity() -> None:
    env: dict[str, str] = {}

    _consume_wrapper_flags(["--pb-overlay=0", "wine"], env)

    assert GAME_KEY_ENV not in env


def test_an_unknown_launcher_resolves_to_nothing(tmp_path) -> None:
    assert game_setting("nowhere:27", settings_path=tmp_path / "x.json") is None
    assert game_setting("", settings_path=tmp_path / "x.json") is None


# -- resolution ----------------------------------------------------------------


def test_argv_resolves_the_stored_setting_for_that_game(tmp_path, monkeypatch) -> None:
    _one_gpu(monkeypatch)
    _profiles(monkeypatch)
    settings = _stored(
        tmp_path,
        LutrisGameSetting(enabled=True, mode=GAME_MODE_ADAPTIVE, target_fps=120.0),
    )

    argv = _argv("lutris:27", settings)

    assert argv is not None
    assert "--adaptive-auto-uv" in argv
    assert argv[argv.index("--adaptive-target-fps") + 1] == "120"


def test_a_stock_setting_pins_factory_state(tmp_path, monkeypatch) -> None:
    _one_gpu(monkeypatch)
    settings = _stored(tmp_path, LutrisGameSetting(enabled=True, mode=GAME_MODE_STOCK))

    assert _argv("lutris:27", settings)[:2] == [
        "--auto-uv-profile",
        STOCK_PROFILE_SELECTOR,
    ]


def test_a_disabled_setting_resolves_to_nothing(tmp_path, monkeypatch) -> None:
    _one_gpu(monkeypatch)
    settings = _stored(tmp_path, LutrisGameSetting(enabled=False))

    assert _argv("lutris:27", settings) is None


def test_a_game_with_no_stored_setting_resolves_to_nothing(tmp_path) -> None:
    assert _argv("lutris:27", tmp_path / "absent.json") is None


# -- soft failure --------------------------------------------------------------


def _adaptive_game(tmp_path, monkeypatch):
    _one_gpu(monkeypatch)
    _profiles(monkeypatch)
    return _stored(
        tmp_path, LutrisGameSetting(enabled=True, mode=GAME_MODE_ADAPTIVE)
    )


def test_an_unreachable_daemon_never_blocks_the_launch(
    tmp_path, monkeypatch, capsys
) -> None:
    settings = _adaptive_game(tmp_path, monkeypatch)
    from runtime import daemon_client

    def boom(*_args, **_kwargs):
        raise OSError("socket is not there")

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", boom)

    applied = apply_game_key_profile(
        "lutris:27", settings_path=settings, watch_pid=1234
    )

    assert applied is False
    assert "profile apply skipped" in capsys.readouterr().err


def test_a_daemon_refusal_is_reported_not_claimed(
    tmp_path, monkeypatch, capsys
) -> None:
    settings = _adaptive_game(tmp_path, monkeypatch)
    from runtime import daemon_client

    monkeypatch.setattr(
        daemon_client,
        "start_game_runtime_profile",
        lambda *a, **k: {"ignored": True, "reason": "another game owns the runtime"},
    )

    applied = apply_game_key_profile(
        "lutris:27", settings_path=settings, watch_pid=1234
    )

    assert applied is False
    assert "another game owns the runtime" in capsys.readouterr().err


def test_the_daemon_id_is_the_namespaced_game_key(tmp_path, monkeypatch) -> None:
    """Lutris game 27 and Steam app 27 are different games to the daemon."""
    settings = _adaptive_game(tmp_path, monkeypatch)
    seen: dict[str, object] = {}
    from runtime import daemon_client

    def record(argv, *, watch_pid, app_id, timeout_s):
        seen.update(app_id=app_id, watch_pid=watch_pid)
        return {"started": True}

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", record)

    assert apply_game_key_profile("lutris:27", settings_path=settings, watch_pid=99)
    assert seen == {"app_id": "lutris:27", "watch_pid": 99}
