from pathlib import Path
from types import SimpleNamespace

import pytest

from profiles import game_profile

import integrations.steam.game_runtime as game_runtime
from integrations.steam.game_runtime import (
    apply_game_runtime_profile,
    game_account_id,
    game_app_id,
    game_runtime_profile_argv,
)
from profiles.game_profile import (
    game_gpu_target,
    profile_argv_for_setting,
)
from integrations.steam.settings import (
    SteamGameSetting,
    store_steam_game_setting,
)
from profiles.game_profile import (
    GAME_MODE_NONE,
)
from integrations.steam.users import STEAMID64_BASE


ACCOUNT_ID = "78675700"


@pytest.fixture()
def steam_home(tmp_path: Path) -> Path:
    root = tmp_path / ".local" / "share" / "Steam"
    (root / "steamapps").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "userdata" / ACCOUNT_ID / "config").mkdir(parents=True)
    (root / "config" / "loginusers.vdf").write_text(
        '"users"\n{\n\t"%d"\n\t{\n\t\t"AccountName"\t\t"jan_pietek"\n'
        '\t\t"PersonaName"\t\t"jan.pietek"\n\t\t"MostRecent"\t\t"1"\n'
        '\t\t"Timestamp"\t\t"1"\n\t}\n}\n' % (STEAMID64_BASE + int(ACCOUNT_ID)),
        encoding="utf-8",
    )
    return tmp_path


def test_game_app_id_prefers_steam_app_id() -> None:
    assert game_app_id({"SteamAppId": "1089130"}) == "1089130"
    assert game_app_id({"STEAM_COMPAT_APP_ID": "42"}) == "42"
    assert game_app_id({"SteamAppId": "not-a-number"}) == ""
    assert game_app_id({}) == ""


def test_game_account_id_matches_steam_user_env(steam_home: Path) -> None:
    assert (
        game_account_id({"SteamUser": "jan_pietek"}, home=steam_home) == ACCOUNT_ID
    )
    # Unknown login name falls back to the active account.
    assert game_account_id({"SteamUser": "somebody"}, home=steam_home) == ACCOUNT_ID
    assert game_account_id({}, home=steam_home) == ACCOUNT_ID


def test_profile_argv_for_default_is_none() -> None:
    assert profile_argv_for_setting(SteamGameSetting(enabled=True, mode="default")) is None
    assert profile_argv_for_setting(SteamGameSetting(enabled=True, mode=GAME_MODE_NONE)) is None


def test_profile_argv_for_explicit_stock_pins_factory_state() -> None:
    argv = profile_argv_for_setting(SteamGameSetting(enabled=True, mode="stock"))
    assert argv == ["--auto-uv-profile", "__stock__"]


def _stub_adaptive_profiles(monkeypatch) -> None:
    monkeypatch.setattr(game_profile, "read_auto_uv_profiles", lambda: [])
    monkeypatch.setattr(
        game_profile,
        "resolve_profile_tier_profiles",
        lambda profiles, **kwargs: {
            "efficiency": {"profile_id": "efficiency-profile"},
            "balanced": {"profile_id": "balanced-profile"},
            "performance": {"profile_id": "performance-profile"},
        },
    )
    monkeypatch.setattr(
        game_runtime.DaemonGpuClient,
        "discover_identities",
        classmethod(
            lambda cls: [
                SimpleNamespace(index=0, uuid="GPU-primary", name="RTX Test")
            ]
        ),
    )


def test_profile_argv_for_adaptive(monkeypatch) -> None:
    _stub_adaptive_profiles(monkeypatch)
    argv = profile_argv_for_setting(SteamGameSetting(enabled=True, mode="adaptive"))
    assert argv == [
        "--auto-uv-profile",
        "performance-profile",
        "--adaptive-auto-uv",
    ]


def test_profile_argv_for_adaptive_includes_per_game_target_fps(monkeypatch) -> None:
    _stub_adaptive_profiles(monkeypatch)
    argv = profile_argv_for_setting(
        SteamGameSetting(enabled=True, mode="adaptive", target_fps=120.0)
    )
    assert argv == [
        "--auto-uv-profile",
        "performance-profile",
        "--adaptive-auto-uv",
        "--adaptive-target-fps",
        "120",
    ]


def test_profile_argv_for_non_adaptive_ignores_target_fps() -> None:
    argv = profile_argv_for_setting(
        SteamGameSetting(enabled=True, mode="stock", target_fps=120.0)
    )
    assert argv == ["--auto-uv-profile", "__stock__"]


def test_profile_argv_for_fixed_tier_resolves_profile(monkeypatch) -> None:
    monkeypatch.setattr(game_profile, "read_auto_uv_profiles", lambda: [])
    monkeypatch.setattr(
        game_profile,
        "resolve_profile_tier_profiles",
        lambda profiles: {"balanced": {"profile_id": "profile-123"}},
    )
    argv = profile_argv_for_setting(SteamGameSetting(enabled=True, mode="balanced"))
    assert argv == ["--auto-uv-profile", "profile-123"]


def test_profile_argv_for_unresolved_tier_is_none(monkeypatch) -> None:
    monkeypatch.setattr(game_profile, "read_auto_uv_profiles", lambda: [])
    monkeypatch.setattr(
        game_profile,
        "resolve_profile_tier_profiles",
        lambda profiles: {"balanced": None},
    )
    assert profile_argv_for_setting(
        SteamGameSetting(enabled=True, mode="balanced")
    ) is None


def test_game_runtime_profile_argv_reads_setting(
    steam_home: Path, tmp_path: Path, monkeypatch
) -> None:
    _stub_adaptive_profiles(monkeypatch)
    settings_path = tmp_path / "steam-game-settings.json"
    store_steam_game_setting(
        ACCOUNT_ID,
        "1089130",
        SteamGameSetting(enabled=True, mode="adaptive", overlay=True),
        path=settings_path,
    )
    env = {"SteamAppId": "1089130", "SteamUser": "jan_pietek"}
    resolved = game_runtime_profile_argv(
        env, home=steam_home, settings_path=settings_path
    )
    assert resolved is not None
    argv, app_id = resolved
    assert app_id == "1089130"
    assert "--adaptive-auto-uv" in argv


def test_game_runtime_profile_argv_keeps_legacy_profile_on_single_gpu(
    steam_home: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings_path = tmp_path / "steam-game-settings.json"
    store_steam_game_setting(
        ACCOUNT_ID,
        "1089130",
        SteamGameSetting(enabled=True, mode="balanced"),
        path=settings_path,
    )
    monkeypatch.setattr(
        game_profile,
        "read_auto_uv_profiles",
        lambda: [
            {
                "profile_id": "legacy-balanced",
                "final_verified": True,
                "profile_tier": "balanced",
            }
        ],
    )
    monkeypatch.setattr(
        game_runtime.DaemonGpuClient,
        "discover_identities",
        classmethod(
            lambda cls: [
                SimpleNamespace(index=0, uuid="GPU-only", name="RTX Test")
            ]
        ),
    )

    resolved = game_runtime_profile_argv(
        {"SteamAppId": "1089130", "SteamUser": "jan_pietek"},
        home=steam_home,
        settings_path=settings_path,
    )

    assert resolved == (
        ["--auto-uv-profile", "legacy-balanced", "--gpu-index", "0"],
        "1089130",
    )


def test_game_runtime_profile_argv_none_without_setting(
    steam_home: Path, tmp_path: Path
) -> None:
    env = {"SteamAppId": "1089130", "SteamUser": "jan_pietek"}
    assert (
        game_runtime_profile_argv(
            env,
            home=steam_home,
            settings_path=tmp_path / "steam-game-settings.json",
        )
        is None
    )


def test_apply_calls_daemon_with_own_pid(
    steam_home: Path, tmp_path: Path, monkeypatch
) -> None:
    settings_path = tmp_path / "steam-game-settings.json"
    store_steam_game_setting(
        ACCOUNT_ID,
        "1089130",
        SteamGameSetting(enabled=True, mode="adaptive"),
        path=settings_path,
    )
    calls: list[dict] = []

    import runtime.daemon_client as daemon_client

    _stub_adaptive_profiles(monkeypatch)

    def fake_start(argv, *, watch_pid, app_id="", **kwargs):
        calls.append({"argv": argv, "watch_pid": watch_pid, "app_id": app_id})
        return {"started": True}

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", fake_start)
    env = {"SteamAppId": "1089130", "SteamUser": "jan_pietek"}
    assert apply_game_runtime_profile(
        env, home=steam_home, settings_path=settings_path
    )
    import os

    assert calls == [
        {
            "argv": [
                "--auto-uv-profile",
                "performance-profile",
                "--adaptive-auto-uv",
                "--gpu-index",
                "0",
            ],
            "watch_pid": os.getpid(),
            "app_id": "1089130",
        }
    ]


def test_apply_accepts_host_wrapper_pid(
    steam_home: Path, tmp_path: Path, monkeypatch
) -> None:
    settings_path = tmp_path / "steam-game-settings.json"
    store_steam_game_setting(
        ACCOUNT_ID,
        "1089130",
        SteamGameSetting(enabled=True, mode="adaptive"),
        path=settings_path,
    )
    calls = []

    import runtime.daemon_client as daemon_client

    _stub_adaptive_profiles(monkeypatch)

    monkeypatch.setattr(
        daemon_client,
        "start_game_runtime_profile",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )

    assert apply_game_runtime_profile(
        {"SteamAppId": "1089130", "SteamUser": "jan_pietek"},
        home=steam_home,
        settings_path=settings_path,
        watch_pid=4242,
    )
    assert calls[0][1]["watch_pid"] == 4242


def test_flatpak_runtime_helper_passes_explicit_game_identity(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(
        game_runtime,
        "apply_game_runtime_profile",
        lambda env, **kwargs: seen.append((env, kwargs)) or True,
    )

    assert (
        game_runtime.main(
            [
                "--watch-pid",
                "4242",
                "--app-id",
                "1089130",
                "--account-name",
                "jan_pietek",
            ]
        )
        == 0
    )
    assert seen[0][0]["SteamAppId"] == "1089130"
    assert seen[0][0]["SteamUser"] == "jan_pietek"
    assert seen[0][1]["watch_pid"] == 4242


def test_apply_soft_fails_when_daemon_unreachable(
    steam_home: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    _stub_adaptive_profiles(monkeypatch)
    settings_path = tmp_path / "steam-game-settings.json"
    store_steam_game_setting(
        ACCOUNT_ID,
        "1089130",
        SteamGameSetting(enabled=True, mode="adaptive"),
        path=settings_path,
    )
    import runtime.daemon_client as daemon_client

    def fake_start(*args, **kwargs):
        raise RuntimeError("daemon socket not found")

    monkeypatch.setattr(daemon_client, "start_game_runtime_profile", fake_start)
    env = {"SteamAppId": "1089130", "SteamUser": "jan_pietek"}
    assert not apply_game_runtime_profile(
        env, home=steam_home, settings_path=settings_path
    )
    assert "skipped" in capsys.readouterr().err


def test_apply_reports_when_another_game_owns_runtime(
    steam_home: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    _stub_adaptive_profiles(monkeypatch)
    settings_path = tmp_path / "steam-game-settings.json"
    store_steam_game_setting(
        ACCOUNT_ID,
        "1089130",
        SteamGameSetting(enabled=True, mode="adaptive"),
        path=settings_path,
    )
    import runtime.daemon_client as daemon_client

    monkeypatch.setattr(
        daemon_client,
        "start_game_runtime_profile",
        lambda *args, **kwargs: {
            "started": False,
            "ignored": True,
            "reason": "first-game-runtime-active",
        },
    )

    assert not apply_game_runtime_profile(
        {"SteamAppId": "1089130", "SteamUser": "jan_pietek"},
        home=steam_home,
        settings_path=settings_path,
    )
    assert "first-game-runtime-active" in capsys.readouterr().err


def test_game_gpu_target_requires_choice_with_multiple_gpus() -> None:
    identities = [
        SimpleNamespace(index=0, uuid="GPU-a"),
        SimpleNamespace(index=1, uuid="GPU-b"),
    ]

    assert game_gpu_target(SteamGameSetting(), identities) is None
    assert game_gpu_target(
        SteamGameSetting(gpu_uuid="GPU-b"), identities
    ) == ("GPU-b", 1)


def test_profile_argv_filters_tiers_by_gpu_uuid(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(game_profile, "read_auto_uv_profiles", lambda: [])
    monkeypatch.setattr(
        game_profile,
        "resolve_profile_tier_profiles",
        lambda profiles, *, gpu_uuid="", **_kwargs: seen.append(gpu_uuid)
        or {"balanced": {"profile_id": "profile-b"}},
    )

    argv = profile_argv_for_setting(
        SteamGameSetting(enabled=True, mode="balanced", gpu_uuid="GPU-b"),
        gpu_index=2,
    )

    assert seen == ["GPU-b"]
    assert argv == ["--auto-uv-profile", "profile-b", "--gpu-index", "2"]


def test_profile_argv_keeps_legacy_tiers_on_unambiguous_single_gpu(
    monkeypatch,
) -> None:
    legacy = {
        "profile_id": "legacy-balanced",
        "final_verified": True,
        "profile_tier": "balanced",
    }
    monkeypatch.setattr(game_profile, "read_auto_uv_profiles", lambda: [legacy])

    argv = profile_argv_for_setting(
        SteamGameSetting(enabled=True, mode="balanced"),
        gpu_index=0,
        gpu_uuid="GPU-only",
        include_legacy_profiles=True,
    )

    assert argv == [
        "--auto-uv-profile",
        "legacy-balanced",
        "--gpu-index",
        "0",
    ]


def test_client_resolves_and_applies_game_spec_on_the_same_socket(monkeypatch) -> None:
    import runtime.daemon_client as daemon_client
    import runtime.runtime_spec as runtime_spec

    calls: list[tuple[str, object]] = []
    spec = {"format_version": 1}

    def fake_build(intent, *, socket_path=None):
        calls.append(("build", socket_path))
        assert intent["profile_selector"] == "profile-9"
        return spec

    def fake_start(
        resolved_spec,
        *,
        watch_pid,
        app_id="",
        socket_path=None,
        timeout_s=45.0,
    ):
        calls.append(("apply", socket_path))
        assert resolved_spec == spec
        assert (watch_pid, app_id, timeout_s) == (4242, "10", 12.0)
        return {"started": True}

    monkeypatch.setattr(runtime_spec, "build_runtime_spec_from_intent", fake_build)
    monkeypatch.setattr(daemon_client, "start_game_runtime_spec", fake_start)

    result = daemon_client.start_game_runtime_profile(
        ["--auto-uv-profile", "profile-9"],
        watch_pid=4242,
        app_id="10",
        socket_path="/tmp/test-burnerd.sock",
        timeout_s=12.0,
    )

    assert result == {"started": True}
    assert calls == [
        ("build", "/tmp/test-burnerd.sock"),
        ("apply", "/tmp/test-burnerd.sock"),
    ]


def test_launch_steam_game_validates_app_id(monkeypatch) -> None:
    import integrations.steam.process as process

    monkeypatch.setattr(process, "running_in_flatpak", lambda: False)
    monkeypatch.setattr(process.shutil, "which", lambda name: "/usr/bin/steam")
    launched = []
    monkeypatch.setattr(
        process.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append(command),
    )
    from integrations.steam.process import launch_steam_game

    assert launch_steam_game("3606110")
    assert launched == [["/usr/bin/steam", "-applaunch", "3606110"]]
    assert not launch_steam_game("rm -rf /")
    assert len(launched) == 1


def test_running_steam_game_ids_batches_one_pgrep(monkeypatch) -> None:
    import integrations.steam.process as process

    monkeypatch.setattr(process, "running_in_flatpak", lambda: False)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return process.subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "4242 reaper SteamLaunch AppId=3606110 -- /game/a\n"
                "4243 reaper SteamLaunch AppId=228980 -- /game/b\n"
                "9 pgrep -af [S]teamLaunch AppId=\n"  # our own query, ignored
            ),
            stderr="",
        )

    monkeypatch.setattr(process.subprocess, "run", fake_run)

    # One subprocess returns every running game's id.
    assert process.running_steam_game_ids() == frozenset({"3606110", "228980"})
    assert calls == [["/usr/bin/pgrep", "-af", r"[S]teamLaunch AppId="]]

    # returncode 1 = ran fine, no games -> empty set (NOT a failure).
    monkeypatch.setattr(
        process.subprocess,
        "run",
        lambda command, **kwargs: process.subprocess.CompletedProcess(
            command, 1, stdout="", stderr=""
        ),
    )
    assert process.running_steam_game_ids() == frozenset()

    # A timeout / non-0/1 exit is "couldn't tell" -> None, so a poller holds
    # state instead of reading it as every game having exited.
    def boom(command, **kwargs):
        raise process.subprocess.TimeoutExpired(command, 3.0)

    monkeypatch.setattr(process.subprocess, "run", boom)
    assert process.running_steam_game_ids() is None


def test_flatpak_steam_process_control_runs_on_host(monkeypatch) -> None:
    import integrations.steam.process as process

    monkeypatch.setattr(process, "running_in_flatpak", lambda: True)
    monkeypatch.setattr(
        process.shutil,
        "which",
        lambda name: "/usr/bin/flatpak-spawn" if name == "flatpak-spawn" else None,
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-3:] == ["/usr/bin/sh", "-c", "command -v steam"]:
            return process.subprocess.CompletedProcess(
                command, 0, stdout="/usr/bin/steam\n", stderr=""
            )
        return process.subprocess.CompletedProcess(command, 0)

    launched = []
    monkeypatch.setattr(process.subprocess, "run", fake_run)
    monkeypatch.setattr(
        process.subprocess,
        "Popen",
        lambda command, **kwargs: launched.append((command, kwargs)),
    )

    assert process.steam_running()
    assert process.steam_available()
    assert process.launch_steam_game("3606110")

    assert calls[0][0] == [
        "/usr/bin/flatpak-spawn",
        "--host",
        "--directory=/tmp",
        "/usr/bin/pgrep",
        "-x",
        "steam",
    ]
    assert launched[0][0] == [
        "/usr/bin/flatpak-spawn",
        "--host",
        "--directory=/tmp",
        "/usr/bin/steam",
        "-applaunch",
        "3606110",
    ]


def test_flatpak_steam_control_fails_closed_without_host_bridge(monkeypatch) -> None:
    import integrations.steam.process as process

    monkeypatch.setattr(process, "running_in_flatpak", lambda: True)
    monkeypatch.setattr(process.shutil, "which", lambda _name: None)

    assert not process.steam_running()
    assert not process.steam_available()
    assert not process.launch_steam_game("3606110")
