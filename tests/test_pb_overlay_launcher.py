from __future__ import annotations

from overlay import launcher
from overlay.config import OVERLAY_CONFIG_ENV
from overlay.config import OverlayConfig
from overlay.config import save_overlay_config
from overlay.native_layer import NATIVE_LAYER_DIR_ENV
from overlay.native_layer import NATIVE_LAYER_LIBRARY
from overlay.native_layer import NATIVE_LAYER_MANIFEST
from overlay.state import (
    OVERLAY_ENABLE_ENV_ALIAS,
    OVERLAY_ENABLE_ENV,
    OVERLAY_STATE_ENV,
)


def test_flatpak_session_uses_daemon_host_pid(monkeypatch, tmp_path):
    from runtime import daemon_client
    from integrations.launchers import runtime_profile

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FLATPAK_ID", "com.heroicgameslauncher.hgl")
    monkeypatch.setattr(daemon_client, "client_host_pid", lambda: 98765)
    monkeypatch.setattr(launcher, "configure_penguin_burner_environment", lambda *a, **k: False)
    profiles = []
    monkeypatch.setattr(runtime_profile, "apply_game_key_profile", lambda key, **kw: profiles.append((key, kw)))
    launched = []
    monkeypatch.setattr(launcher.os, "execvpe", lambda exe, args, env: launched.append(env))
    launcher.main(["--pb-overlay=0", "--pb-game-id=heroic:test", "game"])
    assert launched[0][launcher.TELEMETRY_SESSION_ENV] == "98765"
    assert profiles == [("heroic:test", {"watch_pid": 98765})]


def test_pb_overlay_launcher_execs_with_layer_environment(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setenv("HOME", str(tmp_path))
    state_path = tmp_path / "overlay-state.txt"
    monkeypatch.setenv(OVERLAY_STATE_ENV, str(state_path))
    monkeypatch.setenv(OVERLAY_CONFIG_ENV, str(tmp_path / "overlay.toml"))
    native_layer_dir = _fake_native_layer_dir(tmp_path)
    monkeypatch.setenv(NATIVE_LAYER_DIR_ENV, str(native_layer_dir))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("LD_PRELOAD", "steam-overlay.so")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/steam/runtime")
    monkeypatch.setenv("PRESSURE_VESSEL_RUNTIME", "steamrt")

    def fake_execvpe(file, args, env):
        calls.append((file, args, env))
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)

    try:
        launcher.main(["game", "--arg"])
    except RuntimeError:
        pass

    file, args, env = calls[0]
    assert file == "game"
    assert args == ["game", "--arg"]
    assert env["PENGUIN_BURNER"] == "1"
    assert env["PENGUIN_BURNER_LATENCY_LAYER"] == "1"
    assert env[OVERLAY_ENABLE_ENV] == "auto"
    assert env["PENGUIN_BURNER_LATENCY_SOCKET"].endswith(
        "/.cache/penguin-burner/latency.sock"
    )
    assert env["DXVK_NVAPI_VKREFLEX"] == "1"
    assert env["PROTON_ENABLE_NVAPI"] == "1"
    assert env["PROTON_HIDE_NVIDIA_GPU"] == "0"
    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert "DXVK_NVAPI_LOG_LEVEL" not in env
    assert "PROTON_LOG" not in env
    assert "VK_LAYER_PENGUINBURNER_latency" in env["VK_LOADER_LAYERS_ENABLE"]
    assert "VK_LAYER_DXVK_NVAPI_reflex" not in env["VK_LOADER_LAYERS_ENABLE"]
    assert str(native_layer_dir) in env["VK_ADD_IMPLICIT_LAYER_PATH"]
    assert env[OVERLAY_STATE_ENV] == str(state_path)
    assert env[launcher.TELEMETRY_SESSION_ENV] == str(launcher.os.getpid())


def test_main_overwrites_spoofed_telemetry_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(OVERLAY_CONFIG_ENV, str(tmp_path / "overlay.toml"))
    monkeypatch.setenv(launcher.TELEMETRY_SESSION_ENV, "some-other-game")
    game_envs = []

    def fake_execvpe(_file, _args, env):
        game_envs.append(dict(env))
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)
    try:
        launcher.main(["game"])
    except RuntimeError:
        pass

    assert game_envs[0][launcher.TELEMETRY_SESSION_ENV] == str(
        launcher.os.getpid()
    )


def test_configure_environment_arms_live_overlay_config_by_default(tmp_path) -> None:
    env = {OVERLAY_CONFIG_ENV: str(tmp_path / "overlay.toml")}
    launcher.configure_penguin_burner_environment(env)

    assert env[OVERLAY_ENABLE_ENV] == "auto"


def test_configure_environment_uses_live_overlay_config_when_global_config_exists(
    tmp_path,
) -> None:
    path = tmp_path / "overlay.toml"
    save_overlay_config(OverlayConfig(enabled=True), path)
    env = {OVERLAY_CONFIG_ENV: str(path)}

    launcher.configure_penguin_burner_environment(env)

    assert env[OVERLAY_ENABLE_ENV] == "auto"
    assert env[OVERLAY_CONFIG_ENV] == str(path)


def test_configure_environment_accepts_short_overlay_alias() -> None:
    env = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        OVERLAY_ENABLE_ENV_ALIAS: "1",
    }
    launcher.configure_penguin_burner_environment(env)

    assert env[OVERLAY_ENABLE_ENV] == "1"


def test_configure_environment_keeps_explicit_overlay_env_over_alias() -> None:
    env = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        OVERLAY_ENABLE_ENV: "0",
        OVERLAY_ENABLE_ENV_ALIAS: "1",
    }
    launcher.configure_penguin_burner_environment(env)

    assert env[OVERLAY_ENABLE_ENV] == "0"


def test_configure_environment_no_marker_output_without_prefix() -> None:
    # Overlay on (default) -> in-game latency on, but with no prefix to front the
    # shim it falls through and sets NO dxvk-nvapi trace/marker-log env: latency
    # degrades to the Vulkan layer's own marker tap, never the heavy log.
    env = {OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml"}
    launcher.configure_penguin_burner_environment(env)
    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert "DXVK_NVAPI_LOG_LEVEL" not in env


def test_overlay_on_defaults_ingame_latency_on() -> None:
    # Enabling the overlay defaults the latency meter on (no explicit flag).
    env = {OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml"}
    launcher.configure_penguin_burner_environment(env)
    assert launcher.ingame_latency_enabled(env)
    assert env["PENGUIN_BURNER_LATENCY_DISPLAY"] == "1"


def test_overlay_disabled_defaults_ingame_latency_off() -> None:
    env = {OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml", "PB_OVERLAY": "0"}
    launcher.configure_penguin_burner_environment(env)
    assert not launcher.ingame_latency_enabled(env)
    assert "PENGUIN_BURNER_LATENCY_DISPLAY" not in env


def test_explicit_ingame_latency_survives_the_overlay_being_off() -> None:
    # The documented way to keep markers (and so the shim, and so adaptive's
    # pacing signal) while running without the HUD: ask for latency by name.
    env = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        "PB_OVERLAY": "0",
        "PB_INGAME_LATENCY": "1",
    }
    launcher.configure_penguin_burner_environment(env)
    assert launcher.ingame_latency_enabled(env)


def test_configure_environment_passes_user_dxvk_nvapi_log_env_through() -> None:
    # The old trace escape is gone: a user-set DXVK_NVAPI_LOG_LEVEL no longer
    # disables the shim; the launcher just leaves the user's env untouched.
    env = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        "PB_INGAME_LATENCY": "1",
        "DXVK_NVAPI_LOG_LEVEL": "trace",
    }

    launcher.configure_penguin_burner_environment(env)

    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert env["DXVK_NVAPI_LOG_LEVEL"] == "trace"


def test_shim_deploy_never_suppresses_the_layer_marker_tap(monkeypatch) -> None:
    # Deploying the shim must not mute the native layer's own marker samples:
    # the daemon prefers shim samples by source and needs the layer as the
    # fallback when a deployed shim never streams (game-local nvapi64.dll,
    # 32-bit titles). No suppression env may be set.
    monkeypatch.setattr(launcher, "deploy_nvapi_shim", lambda _env: True)
    env = {launcher.MARKER_FIFO_ENV: "/tmp/nvapi-trace.700.fifo"}

    assert launcher._configure_dxvk_nvapi_marker_output(env)
    assert "PENGUIN_BURNER_NVAPI_SHIM_ACTIVE" not in env


def test_explicit_ingame_latency_zero_overrides_latency_alias() -> None:
    env = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        "PENGUIN_BURNER_INGAME_LATENCY": "0",
        "PB_INGAME_LATENCY": "1",
    }

    launcher.configure_penguin_burner_environment(env)

    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert "DXVK_NVAPI_LOG_LEVEL" not in env
    assert "PENGUIN_BURNER_LATENCY_DISPLAY" not in env
    assert "PENGUIN_BURNER_LATENCY_INJECT_PRESENT_ID" not in env


def test_ingame_latency_also_enables_display_tail() -> None:
    # Explicitly disabled -> no display tail (overrides the overlay default-on).
    off = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        "PB_INGAME_LATENCY": "0",
    }
    launcher.configure_penguin_burner_environment(off)
    assert "PENGUIN_BURNER_LATENCY_DISPLAY" not in off
    assert "PENGUIN_BURNER_LATENCY_INJECT_PRESENT_ID" not in off

    on = {
        OVERLAY_CONFIG_ENV: "/tmp/does-not-exist-pb-overlay.toml",
        "PB_INGAME_LATENCY": "1",
    }
    launcher.configure_penguin_burner_environment(on)
    assert on["PENGUIN_BURNER_LATENCY_DISPLAY"] == "1"
    assert on["PENGUIN_BURNER_LATENCY_INJECT_PRESENT_ID"] == "1"
    assert "PENGUIN_BURNER_LATENCY_DEBUG_FLOW" not in on


def test_configure_environment_keeps_global_latency_config_full_path(tmp_path) -> None:
    path = tmp_path / "overlay.toml"
    save_overlay_config(
        OverlayConfig(enabled=True, enabled_item_ids=("base_fps", "latency_ms")),
        path,
    )
    env = {OVERLAY_CONFIG_ENV: str(path)}

    launcher.configure_penguin_burner_environment(env)

    assert env[OVERLAY_ENABLE_ENV] == "auto"
    assert "PENGUIN_BURNER_INGAME_LATENCY" not in env
    assert "PB_INGAME_LATENCY" not in env
    assert "DXVK_NVAPI_LATENCY_MARKER_LOG" not in env
    assert "DXVK_NVAPI_LOG_LEVEL" not in env


def test_pb_overlay_launcher_strips_mangohud_when_overlay_enabled(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    state_path = tmp_path / "overlay-state.txt"
    monkeypatch.setenv(OVERLAY_STATE_ENV, str(state_path))
    monkeypatch.setenv(OVERLAY_CONFIG_ENV, str(tmp_path / "overlay.toml"))
    monkeypatch.setenv("PB_OVERLAY", "1")
    monkeypatch.setenv(
        "LD_PRELOAD",
        "steam-overlay.so:/run/host/usr/lib64/mangohud/libMangoHud_shim.so",
    )
    monkeypatch.setenv("MANGOHUD", "1")
    monkeypatch.setenv("MANGOHUD_CONFIG", "fps")
    monkeypatch.setenv("MANGOAPP_CONFIG", "fps")

    def fake_execvpe(file, args, env):
        calls.append((file, args, env))
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)

    try:
        launcher.main(["game"])
    except RuntimeError:
        pass

    env = calls[0][2]
    assert env["LD_PRELOAD"] == "steam-overlay.so"
    assert "MANGOHUD" not in env
    assert "MANGOHUD_CONFIG" not in env
    assert "MANGOAPP_CONFIG" not in env


def test_pb_overlay_launcher_keeps_mangohud_when_overlay_disabled(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    monkeypatch.setenv(OVERLAY_STATE_ENV, str(tmp_path / "overlay-state.txt"))
    monkeypatch.setenv(OVERLAY_CONFIG_ENV, str(tmp_path / "overlay.toml"))
    # Explicit overlay-off (what the Steam tab writes for an unchecked overlay
    # toggle) is the one launch mode where MangoHud must survive.
    monkeypatch.setenv("PB_OVERLAY", "0")
    monkeypatch.setenv("MANGOHUD", "1")
    monkeypatch.setenv("MANGOHUD_CONFIG", "fps")

    def fake_execvpe(file, args, env):
        calls.append((file, args, env))
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)

    try:
        launcher.main(["game"])
    except RuntimeError:
        pass

    env = calls[0][2]
    assert env["MANGOHUD"] == "1"
    assert env["MANGOHUD_CONFIG"] == "fps"


def test_main_arms_refront_watcher_when_shim_active(monkeypatch, tmp_path) -> None:
    """A real launch with the shim chosen spawns the detached re-front watcher
    that survives the exec and outlasts Proton's nvapi64.dll clobber."""
    _write_prefix_nvapi(tmp_path)  # stock real nvapi64.dll present
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    (shim_dir / "nvapi64.dll").write_bytes(b"MZ [pb-nvapi-shim] forwarder")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PENGUIN_BURNER_NVAPI_SHIM_DIR", str(shim_dir))
    monkeypatch.setenv("PB_INGAME_LATENCY", "1")
    monkeypatch.setenv("STEAM_COMPAT_DATA_PATH", str(tmp_path))
    monkeypatch.setenv(OVERLAY_CONFIG_ENV, str(tmp_path / "overlay.toml"))

    spawned = []
    monkeypatch.setattr(launcher, "spawn_refront_watcher", lambda env: spawned.append(env))
    drained = []
    monkeypatch.setattr(
        launcher,
        "spawn_detached_drainer",
        lambda env, path, session_pid=None: drained.append((path, session_pid)),
    )

    def fake_execvpe(file, args, env):
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)

    try:
        launcher.main(["game"])
    except RuntimeError:
        pass

    assert len(spawned) == 1
    sys32 = tmp_path / "pfx/drive_c/windows/system32"
    assert (sys32 / "nvapi64-pb.dll").is_file()  # shim deployed (real parked)
    # The per-game FIFO drainer is armed with the wrapper pid (= the future
    # Proton session) and the FIFO exists before the game could write to it.
    assert drained == [(launcher.trace_fifo_path(dict(launcher.os.environ)), launcher.os.getpid())]
    assert drained[0][0].is_fifo()


def test_flatpak_bootstrap_path_is_limited_to_detached_helpers(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(OVERLAY_CONFIG_ENV, str(tmp_path / "overlay.toml"))
    monkeypatch.setenv(
        launcher.SESSION_HELPER_PYTHONPATH_ENV, "/flatpak/site-packages"
    )
    monkeypatch.setenv("PYTHONPATH", "/original/pythonpath")
    monkeypatch.setenv("PENGUIN_BURNER_INGAME_LATENCY", "1")
    monkeypatch.setattr(
        launcher, "configure_penguin_burner_environment", lambda env, **_kwargs: True
    )
    helper_envs = []
    monkeypatch.setattr(
        launcher, "spawn_refront_watcher", lambda env: helper_envs.append(dict(env))
    )
    monkeypatch.setattr(
        launcher,
        "spawn_detached_drainer",
        lambda env, _path, session_pid=None: helper_envs.append(dict(env)),
    )
    game_envs = []

    def fake_execvpe(_file, _args, env):
        game_envs.append(dict(env))
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)

    try:
        launcher.main(["game"])
    except RuntimeError:
        pass

    assert len(helper_envs) == 2
    for helper_env in helper_envs:
        assert helper_env["PYTHONPATH"] == (
            f"/flatpak/site-packages{launcher.os.pathsep}/original/pythonpath"
        )
        assert launcher.SESSION_HELPER_PYTHONPATH_ENV not in helper_env
    assert game_envs[0]["PYTHONPATH"] == "/original/pythonpath"
    assert launcher.SESSION_HELPER_PYTHONPATH_ENV not in game_envs[0]


def test_main_does_not_arm_watcher_without_ingame_latency(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(OVERLAY_CONFIG_ENV, str(tmp_path / "overlay.toml"))
    spawned = []
    monkeypatch.setattr(launcher, "spawn_refront_watcher", lambda env: spawned.append(env))
    drained = []
    monkeypatch.setattr(
        launcher,
        "spawn_detached_drainer",
        lambda env, path, session_pid=None: drained.append(path),
    )

    def fake_execvpe(file, args, env):
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)

    try:
        launcher.main(["game"])
    except RuntimeError:
        pass

    assert spawned == []  # no prefix -> no shim -> no re-front watcher
    # In-game latency defaults on with the overlay, so stderr is routed into
    # the FIFO even without the shim -- and a routed FIFO always needs its
    # drainer, or plain game stderr could fill the pipe and stall the game.
    assert len(drained) == 1


def test_main_spawns_no_drainer_when_latency_opted_out(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(OVERLAY_CONFIG_ENV, str(tmp_path / "overlay.toml"))
    monkeypatch.setenv("PENGUIN_BURNER_INGAME_LATENCY", "0")
    drained = []
    monkeypatch.setattr(
        launcher,
        "spawn_detached_drainer",
        lambda env, path, session_pid=None: drained.append(path),
    )

    def fake_execvpe(file, args, env):
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)

    try:
        launcher.main(["game"])
    except RuntimeError:
        pass

    assert drained == []  # latency off -> stderr untouched -> nothing to drain


def test_trace_fifo_path_is_per_launch() -> None:
    p = launcher.trace_fifo_path({"HOME": "/home/jp"})
    assert str(p) == (
        f"/home/jp/.cache/penguin-burner/nvapi-trace.{launcher.os.getpid()}.fifo"
    )


def test_trace_fifo_path_honors_env_pin() -> None:
    """Once the wrapper pins the path into the env, every consumer (shim wine
    path, drainer --log, stderr route) resolves the same file."""
    pinned = "/tmp/somewhere/nvapi-trace.1234.fifo"
    env = {"HOME": "/home/jp", launcher.MARKER_FIFO_ENV: pinned}
    assert str(launcher.trace_fifo_path(env)) == pinned


def test_sweep_removes_only_readerless_fifos(tmp_path) -> None:
    import os

    env = {launcher.MARKER_FIFO_ENV: str(tmp_path / "nvapi-trace.1.fifo")}
    stale = tmp_path / "nvapi-trace.2.fifo"
    live = tmp_path / "nvapi-trace.3.fifo"
    legacy = tmp_path / "nvapi-trace.fifo"
    plain = tmp_path / "nvapi-trace.4.fifo.txt"
    for fifo in (stale, live, legacy):
        os.mkfifo(fifo)
    plain.write_text("not a fifo")
    reader = os.open(live, os.O_RDONLY | os.O_NONBLOCK)  # a live drainer
    try:
        launcher._sweep_stale_marker_fifos(env)
    finally:
        os.close(reader)

    assert not stale.exists()  # readerless leftover reaped
    assert not legacy.exists()  # old fixed-name FIFO reaped too
    assert live.exists()  # another launch's drainer keeps its FIFO
    assert plain.exists()  # non-FIFOs are never touched


def _fake_native_layer_dir(tmp_path):
    path = tmp_path / "native-layer"
    path.mkdir()
    (path / NATIVE_LAYER_MANIFEST).write_text("{}", encoding="utf-8")
    (path / NATIVE_LAYER_LIBRARY).write_bytes(b"")
    return path


def _write_prefix_nvapi(tmp_path) -> None:
    dll = tmp_path / "pfx/drive_c/windows/system32/nvapi64.dll"
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b"prefix-nvapi stock real")


def test_steam_game_profile_failure_never_blocks_launch(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(OVERLAY_STATE_ENV, str(tmp_path / "overlay-state.txt"))
    monkeypatch.setenv(OVERLAY_CONFIG_ENV, str(tmp_path / "overlay.toml"))
    import integrations.steam.game_runtime as game_runtime

    def broken_apply(env, **kwargs):
        raise RuntimeError("daemon exploded")

    monkeypatch.setattr(game_runtime, "apply_game_runtime_profile", broken_apply)
    calls = []

    def fake_execvpe(file, args, env):
        calls.append((file, args, env))
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)
    try:
        launcher.main(["game"])
    except RuntimeError:
        pass
    assert calls, "the game must exec even when the profile apply fails"
    assert "skipped" in capsys.readouterr().err


def test_wrapper_consumes_pb_overlay_flag(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(OVERLAY_STATE_ENV, str(tmp_path / "overlay-state.txt"))
    monkeypatch.setenv(OVERLAY_CONFIG_ENV, str(tmp_path / "overlay.toml"))
    monkeypatch.setenv("MANGOHUD", "1")
    calls = []

    def fake_execvpe(file, args, env):
        calls.append((file, args, env))
        raise RuntimeError("stop")

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)

    # --pb-overlay=0: flag consumed, overlay off, MangoHud preserved.
    try:
        launcher.main(["--pb-overlay=0", "game", "--arg"])
    except RuntimeError:
        pass
    file, args, env = calls[0]
    assert (file, args) == ("game", ["game", "--arg"])
    assert env[OVERLAY_ENABLE_ENV] == "0"
    assert env["MANGOHUD"] == "1"

    # --pb-overlay=1: overlay on, MangoHud stripped.
    try:
        launcher.main(["--pb-overlay=1", "game"])
    except RuntimeError:
        pass
    file, args, env = calls[1]
    assert (file, args) == ("game", ["game"])
    assert env[OVERLAY_ENABLE_ENV] == "1"
    assert "MANGOHUD" not in env

    # Only flags and no command -> usage error, no exec.
    assert launcher.main(["--pb-overlay=1"]) == 2
    assert len(calls) == 2
