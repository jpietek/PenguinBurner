from __future__ import annotations

import struct
import subprocess
import threading
import time
from pathlib import Path

from overlay import shim_deploy
from overlay.launcher import _configure_dxvk_nvapi_marker_output


SHIM_BYTES = b"MZ fake nvapi64 [pb-nvapi-shim] forwarder\x00\x01\x02"
REAL_BYTES = b"MZ real dxvk-nvapi nvapi64.dll\x00\x01\x02"


def _make_artifact(tmp_path: Path) -> Path:
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    artifact = shim_dir / shim_deploy.SHIM_DLL_NAME
    artifact.write_bytes(SHIM_BYTES)
    return artifact


def _make_prefix(tmp_path: Path, *, with_nvapi: bool = True) -> Path:
    """Create a prefix; optionally seed the stock nvapi64.dll. Return data path."""
    data_path = tmp_path / "compatdata"
    system32 = data_path / "pfx" / "drive_c" / "windows" / "system32"
    system32.mkdir(parents=True)
    if with_nvapi:
        (system32 / shim_deploy.SHIM_DLL_NAME).write_bytes(REAL_BYTES)
    return data_path


def _env(tmp_path: Path, data_path: Path) -> dict[str, str]:
    return {
        shim_deploy.NVAPI_SHIM_DIR_ENV: str(tmp_path / "shim"),
        "STEAM_COMPAT_DATA_PATH": str(data_path),
    }


def _system32(data_path: Path) -> Path:
    return data_path / "pfx" / "drive_c" / "windows" / "system32"


def test_artifact_discovery_prefers_env_override(tmp_path: Path) -> None:
    artifact = _make_artifact(tmp_path)
    env = {shim_deploy.NVAPI_SHIM_DIR_ENV: str(artifact.parent)}
    assert shim_deploy.nvapi_shim_artifact(env) == artifact


def test_prefix_system32_resolves_when_present(tmp_path: Path) -> None:
    data_path = _make_prefix(tmp_path)
    assert shim_deploy.prefix_system32({"STEAM_COMPAT_DATA_PATH": str(data_path)}) == (
        _system32(data_path)
    )


def test_prefix_system32_none_without_path() -> None:
    assert shim_deploy.prefix_system32({}) is None


def test_deploy_fronts_stock_nvapi_and_parks_real(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    sys32 = _system32(data_path)

    result = shim_deploy.deploy_nvapi_shim(env)
    assert result == sys32 / shim_deploy.SHIM_DLL_NAME
    # nvapi64.dll is now the shim; the real dxvk-nvapi is parked alongside.
    assert (sys32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == SHIM_BYTES
    assert (sys32 / shim_deploy.REAL_SIDECAR_NAME).read_bytes() == REAL_BYTES


def test_restore_reverses_active_shim_fronting(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    sys32 = _system32(data_path)
    assert shim_deploy.deploy_nvapi_shim(env) is not None

    assert shim_deploy.restore_nvapi_shim(env) == sys32
    assert (sys32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == REAL_BYTES
    assert not (sys32 / shim_deploy.REAL_SIDECAR_NAME).exists()


def test_restore_removes_stale_sidecar_after_proton_resync(tmp_path: Path) -> None:
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    sys32 = _system32(data_path)
    sidecar = sys32 / shim_deploy.REAL_SIDECAR_NAME
    sidecar.write_bytes(b"older real")

    assert shim_deploy.restore_nvapi_shim(env) == sys32
    assert (sys32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == REAL_BYTES
    assert not sidecar.exists()


def test_restore_recovers_parked_real_when_front_dll_is_missing(tmp_path: Path) -> None:
    data_path = _make_prefix(tmp_path, with_nvapi=False)
    env = _env(tmp_path, data_path)
    sys32 = _system32(data_path)
    sidecar = sys32 / shim_deploy.REAL_SIDECAR_NAME
    sidecar.write_bytes(REAL_BYTES)

    assert shim_deploy.restore_nvapi_shim(env) == sys32
    assert (sys32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == REAL_BYTES
    assert not sidecar.exists()


def test_restore_all_scans_every_steam_library_prefix(tmp_path: Path) -> None:
    compat_data = (
        tmp_path
        / ".local/share/Steam/steamapps/compatdata/123"
    )
    system32 = compat_data / "pfx/drive_c/windows/system32"
    system32.mkdir(parents=True)
    (system32 / shim_deploy.SHIM_DLL_NAME).write_bytes(SHIM_BYTES)
    (system32 / shim_deploy.REAL_SIDECAR_NAME).write_bytes(REAL_BYTES)

    assert shim_deploy.restore_all_nvapi_shims(tmp_path) == (system32,)
    assert (system32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == REAL_BYTES
    assert not (system32 / shim_deploy.REAL_SIDECAR_NAME).exists()


def test_deploy_is_idempotent(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    sys32 = _system32(data_path)

    assert shim_deploy.deploy_nvapi_shim(env) is not None
    nvapi = sys32 / shim_deploy.SHIM_DLL_NAME
    sidecar = sys32 / shim_deploy.REAL_SIDECAR_NAME
    nvapi_mtime = nvapi.stat().st_mtime_ns
    sidecar_mtime = sidecar.stat().st_mtime_ns

    # Second deploy: shim already current, must not rewrite either file.
    assert shim_deploy.deploy_nvapi_shim(env) == nvapi
    assert nvapi.stat().st_mtime_ns == nvapi_mtime
    assert sidecar.stat().st_mtime_ns == sidecar_mtime
    assert sidecar.read_bytes() == REAL_BYTES  # real not clobbered by re-park


def test_deploy_updates_stale_shim(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path, with_nvapi=False)
    sys32 = _system32(data_path)
    # An older shim already installed, plus its parked real.
    (sys32 / shim_deploy.SHIM_DLL_NAME).write_bytes(b"old [pb-nvapi-shim] build")
    (sys32 / shim_deploy.REAL_SIDECAR_NAME).write_bytes(REAL_BYTES)
    env = _env(tmp_path, data_path)

    assert shim_deploy.deploy_nvapi_shim(env) is not None
    assert (sys32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == SHIM_BYTES
    assert (sys32 / shim_deploy.REAL_SIDECAR_NAME).read_bytes() == REAL_BYTES


def test_deploy_reheals_after_resync(tmp_path: Path) -> None:
    """Proton re-sync restored the stock nvapi64.dll over our shim; re-park + reinstall."""
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)  # stock nvapi64.dll present again
    sys32 = _system32(data_path)
    # A stale sidecar from a previous Proton version.
    (sys32 / shim_deploy.REAL_SIDECAR_NAME).write_bytes(b"older real")
    env = _env(tmp_path, data_path)

    assert shim_deploy.deploy_nvapi_shim(env) is not None
    assert (sys32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == SHIM_BYTES
    # Sidecar refreshed to the freshly-restored real.
    assert (sys32 / shim_deploy.REAL_SIDECAR_NAME).read_bytes() == REAL_BYTES


def test_deploy_skips_when_no_prefix(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    env = {shim_deploy.NVAPI_SHIM_DIR_ENV: str(tmp_path / "shim")}
    assert shim_deploy.deploy_nvapi_shim(env) is None


def test_deploy_skips_when_prefix_has_no_nvapi(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path, with_nvapi=False)
    env = _env(tmp_path, data_path)
    assert shim_deploy.deploy_nvapi_shim(env) is None


def test_deploy_guards_shim_without_sidecar(tmp_path: Path) -> None:
    """Our shim is installed but the parked real vanished: do not pretend it works."""
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path, with_nvapi=False)
    sys32 = _system32(data_path)
    (sys32 / shim_deploy.SHIM_DLL_NAME).write_bytes(SHIM_BYTES)  # shim, no sidecar
    env = _env(tmp_path, data_path)
    assert shim_deploy.deploy_nvapi_shim(env) is None


def test_deploy_respects_latency_disable_env(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    env[shim_deploy.NVAPI_LATENCY_DISABLE_ENV] = "1"
    assert shim_deploy.deploy_nvapi_shim(env) is None


def test_deploy_respects_legacy_shim_disable_env(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    env[shim_deploy.NVAPI_SHIM_DISABLE_ENV] = "1"
    assert shim_deploy.deploy_nvapi_shim(env) is None


def test_launcher_prefers_shim_over_trace(tmp_path: Path) -> None:
    """When the shim deploys, neither trace nor marker-log env is set."""
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)

    _configure_dxvk_nvapi_marker_output(env)

    assert "DXVK_NVAPI_LOG_LEVEL" not in env
    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert (_system32(data_path) / shim_deploy.REAL_SIDECAR_NAME).is_file()


def test_launcher_no_marker_output_without_prefix(tmp_path: Path) -> None:
    """No prefix -> shim skipped -> no dxvk-nvapi trace/marker-log env set;
    in-game latency degrades to the Vulkan layer's own marker tap."""
    _make_artifact(tmp_path)
    env = {shim_deploy.NVAPI_SHIM_DIR_ENV: str(tmp_path / "shim")}

    _configure_dxvk_nvapi_marker_output(env)

    assert "DXVK_NVAPI_LOG_LEVEL" not in env
    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env


def test_watch_and_refront_reinstalls_after_proton_clobber(tmp_path: Path) -> None:
    """Proton copies its stock nvapi64.dll over our shim mid-launch; the watcher
    re-fronts it (one iteration here) so the game still loads the shim."""
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    sys32 = _system32(data_path)
    nvapi = sys32 / shim_deploy.SHIM_DLL_NAME

    assert shim_deploy.deploy_nvapi_shim(env) is not None
    assert nvapi.read_bytes() == SHIM_BYTES

    # Proton's try_copy removes our shim and drops the stock real back in place.
    nvapi.write_bytes(REAL_BYTES)

    # duration_s=0 runs exactly one re-front pass, then returns.
    shim_deploy.watch_and_refront(env, duration_s=0.0, poll_s=0.0)

    assert nvapi.read_bytes() == SHIM_BYTES
    # The parked real is still the real dxvk-nvapi (not overwritten with itself
    # in a way that loses it), so the shim still has a forward target.
    assert (sys32 / shim_deploy.REAL_SIDECAR_NAME).read_bytes() == REAL_BYTES


def test_watch_seconds_default_is_session_scoped() -> None:
    """No env cap -> None: the watcher guards the whole Proton session."""
    assert shim_deploy.watch_seconds({}) is None
    assert shim_deploy.watch_seconds(
        {shim_deploy.NVAPI_SHIM_WATCH_SECONDS_ENV: "12.5"}
    ) == 12.5
    assert shim_deploy.watch_seconds(
        {shim_deploy.NVAPI_SHIM_WATCH_SECONDS_ENV: "bogus"}
    ) is None


def test_parse_inotify_events_decodes_names() -> None:
    name = b"nvapi64.dll\0\0\0\0\0"
    event = struct.pack("iIII", 1, shim_deploy._IN_CLOSE_WRITE, 0, len(name)) + name
    dir_event = struct.pack("iIII", 1, shim_deploy._IN_IGNORED, 0, 0)
    events = shim_deploy._parse_inotify_events(event + dir_event)
    assert events == [
        (shim_deploy._IN_CLOSE_WRITE, "nvapi64.dll"),
        (shim_deploy._IN_IGNORED, ""),
    ]


def _wait_readable(fd: int, timeout_s: float) -> bool:
    import select

    return bool(select.select([fd], [], [], timeout_s)[0])


def test_notifier_reports_nvapi_rewrite(tmp_path: Path) -> None:
    """A completed write of nvapi64.dll wakes the notifier; other files do not."""
    notifier = shim_deploy._Nvapi64Notifier(tmp_path)
    try:
        (tmp_path / "unrelated.dll").write_bytes(b"x")
        assert _wait_readable(notifier.fd, 0.5)
        assert notifier.drain() is False

        (tmp_path / shim_deploy.SHIM_DLL_NAME).write_bytes(REAL_BYTES)
        assert _wait_readable(notifier.fd, 0.5)
        assert notifier.drain() is True
    finally:
        notifier.close()


def test_watch_refronts_on_rewrite_event(tmp_path: Path) -> None:
    """Session-scoped watch: the watcher re-fronts as soon as Proton's rewrite
    of nvapi64.dll completes, and ends when the session process exits."""
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    nvapi = _system32(data_path) / shim_deploy.SHIM_DLL_NAME

    # Stand-in for the exec'd Proton session the watcher guards.
    session = subprocess.Popen(["sleep", "30"])
    watcher = threading.Thread(
        target=shim_deploy.watch_and_refront,
        args=(env,),
        kwargs={"session_pid": session.pid, "poll_s": 0.02},  # no duration cap
    )
    watcher.start()
    try:
        deadline = time.monotonic() + 2.0
        while nvapi.read_bytes() != SHIM_BYTES and time.monotonic() < deadline:
            time.sleep(0.01)
        assert nvapi.read_bytes() == SHIM_BYTES  # initial deploy landed

        nvapi.write_bytes(REAL_BYTES)  # Proton's per-launch clobber
        deadline = time.monotonic() + 2.0
        while nvapi.read_bytes() != SHIM_BYTES and time.monotonic() < deadline:
            time.sleep(0.01)
        assert nvapi.read_bytes() == SHIM_BYTES  # re-fronted well within 2s
    finally:
        session.kill()
        session.wait()
    # Session death must end the watch promptly (pidfd wakes the select).
    watcher.join(timeout=5.0)
    assert not watcher.is_alive()


def test_watch_exits_when_session_already_dead(tmp_path: Path, monkeypatch) -> None:
    """With no duration cap, a session that is already gone when the watcher
    starts must end the watch immediately (the startup race), not run forever."""
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)

    # No pidfd support and a dead pid: exercise the liveness-poll fallback.
    monkeypatch.setattr(shim_deploy, "open_session_fd", lambda _pid: None)

    def dead(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(shim_deploy.os, "kill", dead)
    monkeypatch.setattr(shim_deploy, "_SESSION_POLL_SECONDS", 0.05)

    start = time.monotonic()
    shim_deploy.watch_and_refront(env, session_pid=999999, poll_s=0.01)
    assert time.monotonic() - start < 2.0  # returned because the session is gone


def test_session_liveness_waits_for_real_steam_child_before_quiescing(
    monkeypatch,
) -> None:
    now = 10.0
    monkeypatch.setattr(
        shim_deploy, "session_alive", lambda _pid, _fd: True
    )
    monkeypatch.setattr(
        shim_deploy,
        "_proc_cmdline",
        lambda pid: (
            "/home/jp/.local/share/Steam/ubuntu12_32/reaper "
            "SteamLaunch AppId=1"
            if pid == 100
            else f"/usr/bin/python -m overlay.shim_deploy --session-pid=100 {pid}"
        ),
    )
    monkeypatch.setattr(shim_deploy, "_session_child_pids", lambda _pid: [201, 202])

    tracker = shim_deploy.SessionLiveness(
        100,
        None,
        now_fn=lambda: now,
        startup_grace_s=15.0,
    )

    assert tracker.steam_reaper_quiesced() is False


def test_session_liveness_quiesces_after_real_steam_child_exits(monkeypatch) -> None:
    children = iter(([200, 201, 202], [201, 202]))
    monkeypatch.setattr(
        shim_deploy, "session_alive", lambda _pid, _fd: True
    )
    monkeypatch.setattr(
        shim_deploy,
        "_proc_cmdline",
        lambda pid: (
            "/home/jp/.local/share/Steam/ubuntu12_32/reaper "
            "SteamLaunch AppId=1"
            if pid == 100
            else (
                "/usr/bin/python -m overlay.shim_deploy --session-pid=100"
                if pid == 201
                else (
                    "/usr/bin/python -m overlay.telemetry.nvapi_marker_bridge "
                    "--session-pid=100"
                    if pid == 202
                    else "/home/jp/.local/share/Steam/steamapps/common/Game/game.exe"
                )
            )
        ),
    )
    monkeypatch.setattr(shim_deploy, "_session_child_pids", lambda _pid: next(children))

    tracker = shim_deploy.SessionLiveness(100, None)

    assert tracker.steam_reaper_quiesced() is False
    assert tracker.steam_reaper_quiesced() is True


def test_session_liveness_does_not_quiesce_direct_game_launch(monkeypatch) -> None:
    monkeypatch.setattr(
        shim_deploy, "session_alive", lambda _pid, _fd: True
    )
    monkeypatch.setattr(
        shim_deploy,
        "_proc_cmdline",
        lambda pid: (
            "/games/Game/game.exe"
            if pid == 100
            else "/usr/bin/python -m overlay.shim_deploy --session-pid=100"
        ),
    )
    monkeypatch.setattr(shim_deploy, "_session_child_pids", lambda _pid: [201])

    tracker = shim_deploy.SessionLiveness(
        100,
        None,
        now_fn=lambda: 100.0,
        startup_grace_s=0.0,
    )

    assert tracker.steam_reaper_quiesced() is False


def test_spawn_refront_watcher_launches_detached_watch(
    tmp_path: Path, monkeypatch
) -> None:
    _make_artifact(tmp_path)
    prefix = _make_wine_prefix(tmp_path, umu_pfx_symlink=False)
    env = {
        shim_deploy.NVAPI_SHIM_DIR_ENV: str(tmp_path / "shim"),
        "WINEPREFIX": str(prefix),
    }
    captured: dict = {}

    class FakePopen:
        def __init__(self, argv, **kwargs) -> None:
            captured["argv"] = argv
            captured["kwargs"] = kwargs

    monkeypatch.setattr(shim_deploy.subprocess, "Popen", FakePopen)

    assert shim_deploy.spawn_refront_watcher(env) is not None
    assert captured["argv"] == [
        shim_deploy.sys.executable,
        "-m",
        "overlay.shim_deploy",
        "--watch",
        f"--session-pid={shim_deploy.os.getpid()}",
    ]
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["env"] is env


def test_spawn_refront_watcher_skips_when_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    env = _env(tmp_path, data_path)
    env[shim_deploy.NVAPI_LATENCY_DISABLE_ENV] = "1"

    def fail(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("watcher must not spawn when disabled")

    monkeypatch.setattr(shim_deploy.subprocess, "Popen", fail)
    assert shim_deploy.spawn_refront_watcher(env) is None


def test_spawn_refront_watcher_skips_without_prefix(
    tmp_path: Path, monkeypatch
) -> None:
    _make_artifact(tmp_path)
    env = {shim_deploy.NVAPI_SHIM_DIR_ENV: str(tmp_path / "shim")}  # no prefix

    def fail(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("watcher must not spawn without a prefix")

    monkeypatch.setattr(shim_deploy.subprocess, "Popen", fail)
    assert shim_deploy.spawn_refront_watcher(env) is None


def _make_wine_prefix(tmp_path: Path, *, umu_pfx_symlink: bool) -> Path:
    """A Lutris-style prefix: drive_c at the top, no compatdata wrapper."""
    prefix = tmp_path / "Games" / "some-game"
    system32 = prefix / "drive_c" / "windows" / "system32"
    system32.mkdir(parents=True)
    (system32 / shim_deploy.SHIM_DLL_NAME).write_bytes(REAL_BYTES)
    if umu_pfx_symlink:
        # umu drops this next to drive_c, so the Steam-shaped path also
        # resolves inside a Lutris prefix.
        (prefix / "pfx").symlink_to(".")
    return prefix


def test_prefix_system32_resolves_a_lutris_wine_prefix(tmp_path: Path) -> None:
    """Reading only STEAM_COMPAT_DATA_PATH left Lutris without the shim.

    No shim means no Reflex markers, so adaptive receives no pacing at all and
    holds whatever tier it started on.
    """
    prefix = _make_wine_prefix(tmp_path, umu_pfx_symlink=False)

    assert shim_deploy.prefix_system32({"WINEPREFIX": str(prefix)}) == (
        prefix / "drive_c" / "windows" / "system32"
    )


def test_prefix_system32_accepts_the_umu_pfx_symlink(tmp_path: Path) -> None:
    prefix = _make_wine_prefix(tmp_path, umu_pfx_symlink=True)

    resolved = shim_deploy.prefix_system32({"WINEPREFIX": str(prefix)})

    assert resolved is not None
    assert resolved.resolve() == (prefix / "drive_c" / "windows" / "system32").resolve()


def test_prefix_system32_prefers_the_steam_path_when_both_are_set(
    tmp_path: Path,
) -> None:
    """umu exports WINEPREFIX under Steam too; the Steam prefix stays canonical."""
    steam = _make_prefix(tmp_path / "steam")
    lutris = _make_wine_prefix(tmp_path / "lutris", umu_pfx_symlink=False)

    resolved = shim_deploy.prefix_system32(
        {"STEAM_COMPAT_DATA_PATH": str(steam), "WINEPREFIX": str(lutris)}
    )

    assert resolved == _system32(steam)


def test_prefix_system32_falls_through_a_steam_path_that_does_not_exist(
    tmp_path: Path,
) -> None:
    """A stale Steam variable must not hide a prefix that is really there."""
    lutris = _make_wine_prefix(tmp_path, umu_pfx_symlink=False)

    resolved = shim_deploy.prefix_system32(
        {"STEAM_COMPAT_DATA_PATH": str(tmp_path / "gone"), "WINEPREFIX": str(lutris)}
    )

    assert resolved == lutris / "drive_c" / "windows" / "system32"


# --- cleanup for prefixes no launcher scan can find ---------------------------


def _isolated_home(tmp_path: Path, monkeypatch) -> Path:
    """Point the register at a throwaway home, never the developer's own."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("PENGUIN_BURNER_HOME", str(home))
    return home


def test_deploy_records_the_prefix_it_fronted(tmp_path: Path, monkeypatch) -> None:
    _isolated_home(tmp_path, monkeypatch)
    artifact = _make_artifact(tmp_path)
    prefix = _make_wine_prefix(tmp_path, umu_pfx_symlink=False)
    system32 = prefix / "drive_c" / "windows" / "system32"

    shim_deploy.deploy_nvapi_shim(
        {
            "WINEPREFIX": str(prefix),
            shim_deploy.NVAPI_SHIM_DIR_ENV: str(artifact.parent),
        }
    )

    assert str(system32.resolve()) in shim_deploy._read_fronted_prefixes(
        shim_deploy.fronted_prefixes_path()
    )


def test_restore_all_cleans_a_prefix_no_scan_could_find(
    tmp_path: Path, monkeypatch
) -> None:
    """The whole point: a Lutris prefix lives wherever the user put it.

    Without the register, uninstall walked Steam's compatdata only, so a
    Lutris, Heroic or plain-wine prefix stayed fronted forever -- invisibly,
    because the front is a DLL swap inside the user's own game files.
    """
    _isolated_home(tmp_path, monkeypatch)
    artifact = _make_artifact(tmp_path)
    prefix = _make_wine_prefix(tmp_path, umu_pfx_symlink=False)
    system32 = prefix / "drive_c" / "windows" / "system32"
    shim_deploy.deploy_nvapi_shim(
        {
            "WINEPREFIX": str(prefix),
            shim_deploy.NVAPI_SHIM_DIR_ENV: str(artifact.parent),
        }
    )
    assert (system32 / shim_deploy.REAL_SIDECAR_NAME).is_file()

    restored = shim_deploy.restore_all_nvapi_shims(tmp_path / "no-steam-here")

    assert system32 in restored
    assert (system32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == REAL_BYTES
    assert not (system32 / shim_deploy.REAL_SIDECAR_NAME).exists()


def test_a_cleaned_prefix_leaves_the_register(tmp_path: Path, monkeypatch) -> None:
    _isolated_home(tmp_path, monkeypatch)
    artifact = _make_artifact(tmp_path)
    prefix = _make_wine_prefix(tmp_path, umu_pfx_symlink=False)
    env = {
        "WINEPREFIX": str(prefix),
        shim_deploy.NVAPI_SHIM_DIR_ENV: str(artifact.parent),
    }
    shim_deploy.deploy_nvapi_shim(env)

    shim_deploy.restore_nvapi_shim(env)

    assert shim_deploy._read_fronted_prefixes(shim_deploy.fronted_prefixes_path()) == []


def test_deploy_refuses_a_prefix_it_cannot_record(
    tmp_path: Path, monkeypatch
) -> None:
    """No record, no fronting.

    A prefix that cannot be recorded is one uninstall can never find again, and
    the swap is invisible to the user, so it would stay behind for good.
    Declining to deploy costs a session of marker latency; the alternative
    costs the user's game files.
    """
    _isolated_home(tmp_path, monkeypatch)
    artifact = _make_artifact(tmp_path)
    prefix = _make_wine_prefix(tmp_path, umu_pfx_symlink=False)
    system32 = prefix / "drive_c" / "windows" / "system32"
    monkeypatch.setattr(shim_deploy, "_write_fronted_prefixes", lambda *_a: False)

    placed = shim_deploy.deploy_nvapi_shim(
        {
            "WINEPREFIX": str(prefix),
            shim_deploy.NVAPI_SHIM_DIR_ENV: str(artifact.parent),
        }
    )

    assert placed is None
    assert (system32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == REAL_BYTES
    assert not (system32 / shim_deploy.REAL_SIDECAR_NAME).exists()


def test_deploy_refuses_to_overwrite_a_corrupt_prefix_register(
    tmp_path: Path, monkeypatch
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    artifact = _make_artifact(tmp_path)
    prefix = _make_wine_prefix(tmp_path, umu_pfx_symlink=False)
    system32 = prefix / "drive_c" / "windows" / "system32"
    register = shim_deploy.fronted_prefixes_path()
    register.parent.mkdir(parents=True, exist_ok=True)
    register.write_text("{broken", encoding="utf-8")

    placed = shim_deploy.deploy_nvapi_shim(
        {
            "WINEPREFIX": str(prefix),
            shim_deploy.NVAPI_SHIM_DIR_ENV: str(artifact.parent),
        }
    )

    assert placed is None
    assert register.read_text(encoding="utf-8") == "{broken"
    assert (system32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == REAL_BYTES
    assert not (system32 / shim_deploy.REAL_SIDECAR_NAME).exists()


def test_concurrent_prefix_registration_keeps_both_entries(
    tmp_path: Path, monkeypatch
) -> None:
    _isolated_home(tmp_path, monkeypatch)
    first_write_started = threading.Event()
    release_first_write = threading.Event()
    second_write_started = threading.Event()
    call_guard = threading.Lock()
    call_count = 0
    real_write = shim_deploy._write_fronted_prefixes

    def paused_first_write(path: Path, entries: list[str]) -> bool:
        nonlocal call_count
        with call_guard:
            call_count += 1
            this_call = call_count
        if this_call == 1:
            first_write_started.set()
            assert release_first_write.wait(timeout=2.0)
        else:
            second_write_started.set()
        return real_write(path, entries)

    monkeypatch.setattr(shim_deploy, "_write_fronted_prefixes", paused_first_write)
    results: list[bool] = []
    first = threading.Thread(
        target=lambda: results.append(
            shim_deploy.remember_fronted_prefix(tmp_path / "one" / "system32")
        )
    )
    second = threading.Thread(
        target=lambda: results.append(
            shim_deploy.remember_fronted_prefix(tmp_path / "two" / "system32")
        )
    )

    first.start()
    assert first_write_started.wait(timeout=2.0)
    second.start()
    try:
        assert not second_write_started.wait(timeout=0.1)
    finally:
        release_first_write.set()
        first.join(timeout=2.0)
        second.join(timeout=2.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert results == [True, True]
    assert set(
        shim_deploy._read_fronted_prefixes(shim_deploy.fronted_prefixes_path()) or []
    ) == {
        str((tmp_path / "one" / "system32").resolve()),
        str((tmp_path / "two" / "system32").resolve()),
    }


def test_the_register_keeps_one_entry_per_prefix(
    tmp_path: Path, monkeypatch
) -> None:
    """umu symlinks pfx -> ., so one directory answers to two paths."""
    _isolated_home(tmp_path, monkeypatch)
    artifact = _make_artifact(tmp_path)
    prefix = _make_wine_prefix(tmp_path, umu_pfx_symlink=True)
    shim_env = {shim_deploy.NVAPI_SHIM_DIR_ENV: str(artifact.parent)}

    shim_deploy.deploy_nvapi_shim({"WINEPREFIX": str(prefix), **shim_env})
    # Second launch of the same game, reached through the symlinked spelling.
    shim_deploy.deploy_nvapi_shim({"WINEPREFIX": str(prefix / "pfx"), **shim_env})

    assert (
        len(shim_deploy._read_fronted_prefixes(shim_deploy.fronted_prefixes_path()))
        == 1
    )


def test_restore_all_still_sweeps_steam_libraries(
    tmp_path: Path, monkeypatch
) -> None:
    """The register does not replace the sweep: it cannot know older installs."""
    _isolated_home(tmp_path, monkeypatch)
    compat_data = tmp_path / ".local/share/Steam/steamapps/compatdata/123"
    system32 = compat_data / "pfx/drive_c/windows/system32"
    system32.mkdir(parents=True)
    (system32 / shim_deploy.SHIM_DLL_NAME).write_bytes(SHIM_BYTES)
    (system32 / shim_deploy.REAL_SIDECAR_NAME).write_bytes(REAL_BYTES)

    assert shim_deploy.restore_all_nvapi_shims(tmp_path) == (system32,)
    assert (system32 / shim_deploy.SHIM_DLL_NAME).read_bytes() == REAL_BYTES


# --- a launch that lands inside prefix setup ---------------------------------


def test_marker_output_is_claimed_when_the_prefix_is_mid_setup(
    tmp_path: Path,
) -> None:
    """umu/Proton remove nvapi64.dll before copying their own dxvk-nvapi in.

    Observed on Lutris + umu (2026-08-24, every launch): at wrapper time the
    file was simply absent. Reporting "no shim" there disarms the re-front
    watcher -- the one thing that would front the DLL the moment it reappears --
    so the shim never returned for the rest of the session.
    """
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path, with_nvapi=False)
    env = _env(tmp_path, data_path)

    claimed = _configure_dxvk_nvapi_marker_output(env)

    assert claimed is True
    # The game reads its env once, at start, so the shim's output path has to be
    # pinned now even though the shim itself arrives later.
    assert env.get("PENGUIN_BURNER_SHIM_OUTPUT")


def test_marker_output_is_declined_without_a_prefix_to_front(
    tmp_path: Path,
) -> None:
    """The other side of it: no prefix at all is not a race, it is a fallback.

    Nothing would ever appear for a watcher to guard, so the Vulkan layer's own
    marker tap stays the source and no helper is spawned.
    """
    _make_artifact(tmp_path)
    env = {shim_deploy.NVAPI_SHIM_DIR_ENV: str(tmp_path / "shim")}

    assert _configure_dxvk_nvapi_marker_output(env) is False
    assert "PENGUIN_BURNER_SHIM_OUTPUT" not in env


def test_marker_output_is_declined_without_a_built_shim(tmp_path: Path) -> None:
    data_path = _make_prefix(tmp_path)
    env = {
        shim_deploy.NVAPI_SHIM_DIR_ENV: str(tmp_path / "absent-shim-dir"),
        "STEAM_COMPAT_DATA_PATH": str(data_path),
    }

    assert _configure_dxvk_nvapi_marker_output(env) is False


# --- never park a DLL that is still being written -----------------------------


def test_deploy_waits_while_the_stock_dll_is_still_being_written(
    tmp_path: Path, monkeypatch
) -> None:
    """A live failure: a 512 KiB fragment of a 1.8 MB DLL was parked.

    The sidecar is what every NVAPI call is forwarded into, so parking a
    half-written copy took the game down at startup. umu writes this file during
    prefix setup and a close notification does not prove the write finished.
    """
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    system32 = _system32(data_path)
    nvapi = system32 / shim_deploy.SHIM_DLL_NAME
    monkeypatch.setattr(shim_deploy, "_settled_size", lambda _path: None)

    placed = shim_deploy.deploy_nvapi_shim(_env(tmp_path, data_path))

    assert placed is None
    # Untouched: no shim installed, and crucially no sidecar written from a
    # partial file, which is what a later restore would put back.
    assert nvapi.read_bytes() == REAL_BYTES
    assert not (system32 / shim_deploy.REAL_SIDECAR_NAME).exists()


def test_settled_size_rejects_a_pause_before_more_growth() -> None:
    sizes = iter([100, 100, 200, 200])

    class PausingWriter:
        def stat(self):
            return type("Stat", (), {"st_size": next(sizes)})()

    assert shim_deploy._settled_size(PausingWriter(), checks=3, delay_s=0) is None


def test_settled_size_accepts_a_complete_stability_window() -> None:
    sizes = iter([100, 100, 100, 100])

    class FinishedWriter:
        def stat(self):
            return type("Stat", (), {"st_size": next(sizes)})()

    assert shim_deploy._settled_size(FinishedWriter(), checks=3, delay_s=0) == 100


def test_deploy_proceeds_once_the_size_settles(tmp_path: Path) -> None:
    _make_artifact(tmp_path)
    data_path = _make_prefix(tmp_path)
    system32 = _system32(data_path)

    placed = shim_deploy.deploy_nvapi_shim(_env(tmp_path, data_path))

    assert placed is not None
    assert (system32 / shim_deploy.REAL_SIDECAR_NAME).read_bytes() == REAL_BYTES
