"""The wrapper side: turning a --pb-game-id flag into a daemon profile apply."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from integrations.launchers import runtime_profile
from integrations.launchers.game_settings import LauncherGameSetting
from integrations.launchers.runtime_profile import (
    apply_game_key_profile,
    game_setting,
)
from integrations.lutris.settings import LUTRIS_GAME_SETTINGS_STORE
from overlay.launcher import _consume_wrapper_flags
from overlay.wrapper_tokens import GAME_KEY_ENV
from profiles import game_profile
from profiles.game_profile import GAME_MODE_ADAPTIVE, GAME_MODE_STOCK
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR


@pytest.fixture
def live_adaptive(monkeypatch):
    from runtime import daemon_client

    _one_gpu(monkeypatch)
    _profiles(monkeypatch)
    status = {
        "active_job": {"runtime_mode": "adaptive", "gpu_uuid": "GPU-1"},
        "game_runtime": {
            "active": True,
            "watched": [{"pid": 4242, "app_id": "heroic:27"}],
        },
    }
    calls = []
    monkeypatch.setattr(daemon_client, "daemon_status", lambda **kw: status)

    def start(argv, **kwargs):
        calls.append((argv, kwargs))
        return {"started": True}

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", start)
    return status, calls


@pytest.mark.parametrize("launcher", ["heroic", "lutris", "faugus"])
@pytest.mark.parametrize("target", [90.0, None])
def test_live_target_reuses_owner_and_resolves_saved_target(live_adaptive, launcher, target):
    status, calls = live_adaptive
    key = f"{launcher}:27"
    status["game_runtime"]["watched"][0]["app_id"] = key
    setting = LauncherGameSetting(enabled=True, mode="adaptive", target_fps=target)
    result = runtime_profile.hot_reapply_game_profile(key, setting)
    assert result is not None and result.ok
    argv, kwargs = calls[0]
    assert kwargs == {"watch_pid": 4242, "app_id": key, "timeout_s": 45.0}
    assert "--adaptive-auto-uv" in argv
    if target is None:
        assert "--adaptive-target-fps" not in argv
    else:
        assert argv[argv.index("--adaptive-target-fps") + 1] == "90"


@pytest.mark.parametrize("case", ["other-launcher", "unwatched", "overridden", "static", "gpu", "disabled"])
def test_live_target_does_not_take_over_another_runtime(live_adaptive, case):
    status, calls = live_adaptive
    setting = LauncherGameSetting(enabled=case != "disabled", mode="adaptive",
                                  gpu_uuid="GPU-other" if case == "gpu" else "")
    if case == "other-launcher":
        status["game_runtime"]["watched"][0]["app_id"] = "steam:27"
    elif case == "unwatched":
        status["game_runtime"]["watched"] = []
    elif case == "overridden":
        status["game_runtime"]["active"] = False
    elif case == "static":
        status["active_job"]["runtime_mode"] = "static"
    result = runtime_profile.hot_reapply_game_profile("heroic:27", setting)
    assert result is None or not result.ok
    assert calls == []


@pytest.mark.parametrize("failure", ["offline", "exited", "ignored", "empty", "profiles"])
def test_live_target_reports_failure_without_claiming_apply(live_adaptive, monkeypatch, failure):
    from runtime import daemon_client

    def fail(**kwargs):
        raise RuntimeError("daemon unavailable")

    if failure == "offline":
        monkeypatch.setattr(daemon_client, "daemon_status", fail)
    elif failure == "profiles":
        monkeypatch.setattr(runtime_profile, "profile_argv", lambda setting: None)
    else:
        def start(*args, **kwargs):
            if failure == "exited":
                raise RuntimeError("not a running process")
            return {"ignored": True, "reason": "first-game-runtime-active"} if failure == "ignored" else {}
        monkeypatch.setattr(daemon_client, "start_game_runtime_profile", start)
    result = runtime_profile.hot_reapply_game_profile(
        "heroic:27", LauncherGameSetting(enabled=True, mode="adaptive", target_fps=90)
    )
    assert result is not None and not result.ok
    assert "Target saved" in result.message


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
    LUTRIS_GAME_SETTINGS_STORE.store("27", setting, path=settings)
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
        LauncherGameSetting(enabled=True, mode=GAME_MODE_ADAPTIVE, target_fps=120.0),
    )

    argv = _argv("lutris:27", settings)

    assert argv is not None
    assert "--adaptive-auto-uv" in argv
    assert argv[argv.index("--adaptive-target-fps") + 1] == "120"


def test_a_stock_setting_pins_factory_state(tmp_path, monkeypatch) -> None:
    _one_gpu(monkeypatch)
    settings = _stored(tmp_path, LauncherGameSetting(enabled=True, mode=GAME_MODE_STOCK))

    assert _argv("lutris:27", settings)[:2] == [
        "--auto-uv-profile",
        STOCK_PROFILE_SELECTOR,
    ]


def test_a_disabled_setting_resolves_to_nothing(tmp_path, monkeypatch) -> None:
    _one_gpu(monkeypatch)
    settings = _stored(tmp_path, LauncherGameSetting(enabled=False))

    assert _argv("lutris:27", settings) is None


def test_a_game_with_no_stored_setting_resolves_to_nothing(tmp_path) -> None:
    assert _argv("lutris:27", tmp_path / "absent.json") is None


# -- soft failure --------------------------------------------------------------


def _adaptive_game(tmp_path, monkeypatch):
    _one_gpu(monkeypatch)
    _profiles(monkeypatch)
    return _stored(
        tmp_path, LauncherGameSetting(enabled=True, mode=GAME_MODE_ADAPTIVE)
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


@pytest.mark.parametrize("launcher", ["heroic", "lutris", "faugus"])
@pytest.mark.parametrize("mode", ["performance", "adaptive", "stock"])
def test_live_mode_switches_and_verifies_the_same_game(live_adaptive, monkeypatch, launcher, mode):
    from runtime import daemon_client

    status, calls = live_adaptive
    key = f"{launcher}:27"
    status["game_runtime"]["watched"][0]["app_id"] = key
    original_watch = dict(status["game_runtime"]["watched"][0])
    status["active_job"]["runtime_mode"] = "static" if mode == "adaptive" else "adaptive"
    def apply(argv, **kwargs):
        calls.append((argv, kwargs))
        status["active_job"].update(
            runtime_mode=mode if mode in ("adaptive", "stock") else "static",
            profile_id=argv[1],
        )
        return {"started": True}
    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", apply)
    result = runtime_profile.hot_reapply_game_profile(
        key, LauncherGameSetting(enabled=True, mode=mode), mode_change=True,
    )
    assert result.ok and "verified" in result.message
    assert status["game_runtime"]["watched"] == [original_watch]
    assert calls[0][1]["watch_pid"] == original_watch["pid"]
    assert ("--adaptive-auto-uv" in calls[0][0]) == (mode == "adaptive")


@pytest.mark.parametrize("failure", ["mode", "profile", "owner", "overridden", "offline"])
def test_live_mode_does_not_claim_success_without_readback(live_adaptive, monkeypatch, failure):
    from runtime import daemon_client

    status, _calls = live_adaptive
    reads = []
    def read(**kwargs):
        reads.append(True)
        if failure == "offline" and len(reads) > 1:
            raise RuntimeError("daemon offline")
        return status
    monkeypatch.setattr(daemon_client, "daemon_status", read)
    def apply(*args, **kwargs):
        status["active_job"].update(runtime_mode="static", profile_id="perf-1")
        if failure == "mode":
            status["active_job"]["runtime_mode"] = "adaptive"
        elif failure == "profile":
            status["active_job"]["profile_id"] = "other"
        elif failure == "owner":
            status["game_runtime"]["watched"] = []
        elif failure == "overridden":
            status["game_runtime"]["active"] = False
        return {"started": True}
    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", apply)
    result = runtime_profile.hot_reapply_game_profile(
        "heroic:27", LauncherGameSetting(enabled=True, mode="performance"), mode_change=True,
    )
    assert not result.ok
    assert "Mode saved" in result.message


@pytest.mark.parametrize("case", ["other-game", "overridden", "gpu", "disabled", "missing-profile"])
def test_live_mode_never_applies_outside_its_active_profile(live_adaptive, monkeypatch, case):
    status, calls = live_adaptive
    setting = LauncherGameSetting(enabled=case != "disabled", mode="performance",
                                  gpu_uuid="GPU-other" if case == "gpu" else "")
    if case == "other-game":
        status["game_runtime"]["watched"][0]["app_id"] = "lutris:27"
    elif case == "overridden":
        status["game_runtime"]["active"] = False
    elif case == "missing-profile":
        monkeypatch.setattr(runtime_profile, "profile_argv", lambda setting: None)
    result = runtime_profile.hot_reapply_game_profile("heroic:27", setting, mode_change=True)
    assert calls == []
    if result is not None:
        assert "verified" not in result.message


@pytest.mark.parametrize("mode", ["adaptive", "performance", "stock"])
def test_faugus_saved_profile_reaches_daemon(tmp_path, monkeypatch, mode):
    from integrations.faugus.settings import FAUGUS_GAME_SETTINGS_STORE
    from runtime import daemon_client

    _one_gpu(monkeypatch)
    _profiles(monkeypatch)
    path = tmp_path / "faugus.json"
    setting = LauncherGameSetting(enabled=True, mode=mode, target_fps=90)
    FAUGUS_GAME_SETTINGS_STORE.store("27", setting, path=path)
    calls = []
    monkeypatch.setattr(
        daemon_client, "start_game_runtime_profile",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or {"started": True},
    )
    assert game_setting("faugus:27", settings_path=path) == setting
    assert apply_game_key_profile("faugus:27", settings_path=path, watch_pid=1234)
    argv, kwargs = calls[0]
    assert kwargs == {"watch_pid": 1234, "app_id": "faugus:27", "timeout_s": 45.0}
    assert ("--adaptive-auto-uv" in argv) == (mode == "adaptive")
    if mode == "adaptive":
        assert argv[argv.index("--adaptive-target-fps") + 1] == "90"
    else:
        assert argv[:2] == ["--auto-uv-profile", STOCK_PROFILE_SELECTOR if mode == "stock" else "perf-1"]
