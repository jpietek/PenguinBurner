from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import stat
import sys

from .config import OVERLAY_CONFIG_ENV, default_overlay_config_path
from .native_layer import LATENCY_LAYER_NAME
from .native_layer import native_layer_dirs
from .shim_deploy import (
    deploy_nvapi_shim,
    nvapi_shim_artifact,
    prefix_system32,
    spawn_refront_watcher,
)
from .telemetry.nvapi_marker_bridge import spawn_detached_drainer
from .state import (
    OVERLAY_ENABLE_ENV_ALIAS,
    OVERLAY_ENABLE_ENV,
    OVERLAY_STATE_ENV,
    clear_overlay_override,
    overlay_state_path,
)
from .telemetry.steam_launch_check import PENGUIN_BURNER_WRAPPER
from .wrapper_tokens import LUTRIS_GAME_ID_ENV, LUTRIS_ID_FLAG_PREFIX

MASTER_ENABLE_ENV = "PENGUIN_BURNER"
LATENCY_ENABLE_ENV = "PENGUIN_BURNER_LATENCY_LAYER"
LATENCY_SOCKET_ENV = "PENGUIN_BURNER_LATENCY_SOCKET"
# User-facing toggle for extra in-game Reflex marker telemetry: the NVAPI shim
# is deployed into the Proton prefix and streams Reflex markers to the FIFO.
INGAME_LATENCY_ENV = "PENGUIN_BURNER_INGAME_LATENCY"
INGAME_LATENCY_ENV_ALIAS = "PB_INGAME_LATENCY"
# Display (present->scanout) latency is folded into the single in-game latency
# opt-in: when latency is on, the wrapper also enables the present-wait tail and
# the present-id injection that makes it work on vkd3d titles. These stay env
# vars (not user-facing tokens) so one launch-line flag covers the whole stack.
DISPLAY_LATENCY_ENV = "PENGUIN_BURNER_LATENCY_DISPLAY"
INJECT_PRESENT_ID_ENV = "PENGUIN_BURNER_LATENCY_INJECT_PRESENT_ID"
DXVK_NVAPI_ENABLE_ENV = "DXVK_NVAPI_VKREFLEX"
VK_LAYER_PATH_ENV = "VK_ADD_IMPLICIT_LAYER_PATH"
VK_LAYER_ENABLE_ENV = "VK_LOADER_LAYERS_ENABLE"
SESSION_HELPER_PYTHONPATH_ENV = "PENGUIN_BURNER_SESSION_HELPER_PYTHONPATH"
TELEMETRY_SESSION_ENV = "PENGUIN_BURNER_TELEMETRY_SESSION"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def ingame_latency_enabled(env: dict[str, str]) -> bool:
    for key in (INGAME_LATENCY_ENV, INGAME_LATENCY_ENV_ALIAS):
        if str(env.get(key) or "").strip().lower() in _FALSEY:
            return False
    for key in (INGAME_LATENCY_ENV, INGAME_LATENCY_ENV_ALIAS):
        if str(env.get(key) or "").strip().lower() in _TRUTHY:
            return True
    # No explicit setting: default the latency meter ON whenever the overlay is
    # enabled, so turning on the overlay just gives you latency too. Opt out with
    # PENGUIN_BURNER_INGAME_LATENCY=0 (or PB_INGAME_LATENCY=0).
    return _overlay_enabled(env)


def _overlay_enabled(env: dict[str, str]) -> bool:
    for key in (OVERLAY_ENABLE_ENV, OVERLAY_ENABLE_ENV_ALIAS):
        value = str(env.get(key) or "").strip().lower()
        if value in _FALSEY:
            return False
        if value:  # "1", "auto", etc.
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    env = dict(os.environ)
    session_helper_pythonpath = str(
        env.pop(SESSION_HELPER_PYTHONPATH_ENV, "") or ""
    ).strip()
    # Wine reports a Windows PID while the Vulkan layer reports the host PID.
    # Both producers inherit this wrapper/session identity so the daemon can
    # merge their complementary samples without mistaking them for two games.
    env[TELEMETRY_SESSION_ENV] = str(os.getpid())
    args = _consume_wrapper_flags(args, env)
    # Every launch starts from its own launch-time overlay setting: a live
    # override left behind by a previous session must not leak into this one.
    clear_overlay_override()
    if not args:
        print(
            f"Usage: {PENGUIN_BURNER_WRAPPER} %command%\n"
            f"Steam launch option example: {PENGUIN_BURNER_WRAPPER} %command%",
            file=sys.stderr,
        )
        return 2

    shim_active = configure_penguin_burner_environment(env, command_args=args)
    if _overlay_enabled(env):
        # Two overlays on one swapchain conflict, so MangoHud is stripped
        # unless the overlay is explicitly off (PB_OVERLAY=0 — what the Steam
        # tab writes for an unchecked overlay toggle). "auto" keeps the
        # historical strip: the config file may enable the overlay mid-game.
        _remove_mangohud_environment(env)
    _prepare_overlay_paths(env)
    _apply_game_profile(env)
    if shim_active:
        # Proton clobbers our pre-exec shim during prefix setup; this detached
        # watcher re-fronts it across that window and survives the exec below.
        # Spawn it before _route_trace_to_fifo so the watcher inherits the
        # console stderr -- not the marker FIFO that fd 2 is about to become,
        # which would feed its diagnostics into the bridge as bogus markers.
        spawn_refront_watcher(
            _session_helper_environment(env, session_helper_pythonpath)
        )
    if ingame_latency_enabled(env):
        # The FIFO must always have a drainer for as long as it can have
        # writers, or the 64 KB pipe fills and the shim's marker write (and any
        # stderr write) blocks the game. The drainer's lifetime is tied to this
        # pid (the exec below turns it into the Proton session) plus the FIFO's
        # writer count -- NOT to the app, which may be closed the whole time.
        # Same stderr caveat as the watcher: spawn before the redirect.
        _sweep_stale_marker_fifos(env)
        if _create_marker_fifo(env):
            spawn_detached_drainer(
                _session_helper_environment(env, session_helper_pythonpath),
                trace_fifo_path(env),
                session_pid=os.getpid(),
            )
        _route_trace_to_fifo(env)
    os.execvpe(args[0], args, env)
    return 127


def _session_helper_environment(
    env: dict[str, str], package_path: str
) -> dict[str, str]:
    """Give detached Python helpers the Flatpak package path without leaking
    it into Proton or the game process."""
    if not package_path:
        return env
    helper_env = dict(env)
    existing = str(helper_env.get("PYTHONPATH") or "").strip()
    helper_env["PYTHONPATH"] = (
        f"{package_path}{os.pathsep}{existing}" if existing else package_path
    )
    return helper_env


def _consume_wrapper_flags(args: list[str], env: dict[str, str]) -> list[str]:
    """Strip leading --pb-* wrapper flags off the command.

    The tabs inject the per-game overlay switch as a flag rather than a
    PB_OVERLAY=x env token, because launchers that exec their child directly
    (gamescope after its "--") cannot start an env assignment as a program.
    The flag carries the same meaning, so it lands in the same env vars.

    The Lutris tab also injects the game identity here. Steam publishes
    SteamAppId in the environment, but Lutris regenerates LUTRIS_GAME_UUID on
    every launch, so the id it stores its settings under has to travel as a
    flag we wrote ourselves.
    """
    while args and (
        args[0].startswith("--pb-overlay=")
        or args[0].startswith("--pb-ingame-latency=")
        or args[0].startswith(LUTRIS_ID_FLAG_PREFIX)
    ):
        flag = args.pop(0)
        name, _, value = flag.partition("=")
        value = value.strip()
        if name == "--pb-overlay":
            if value in _TRUTHY | _FALSEY:
                env[OVERLAY_ENABLE_ENV_ALIAS] = value
                env[OVERLAY_ENABLE_ENV] = value
        elif name == "--pb-ingame-latency":
            # Lands in the same env vars the assignment form sets, so
            # everything downstream reads one opt-in and not two.
            if value in _TRUTHY | _FALSEY:
                env[INGAME_LATENCY_ENV_ALIAS] = value
                env[INGAME_LATENCY_ENV] = value
        elif value:
            env[LUTRIS_GAME_ID_ENV] = value
    return args


def _apply_game_profile(env: dict[str, str]) -> None:
    # Per-game Auto-UV preset: the root daemon applies it and watches this PID
    # -- the exec below makes it the game session's PID, so the daemon restores
    # the standing profile when the game exits. Runs before exec, outside
    # pressure-vessel, so the daemon socket is reachable. Any failure is only
    # reported: it must never block a game launch.
    #
    # A launch belongs to exactly one launcher: the Lutris id only exists when
    # the Lutris tab wrote it onto argv, and Steam's app id only when Steam set
    # it in the environment.
    try:
        if env.get(LUTRIS_GAME_ID_ENV):
            from integrations.lutris.game_runtime import (
                apply_lutris_game_runtime_profile,
            )

            apply_lutris_game_runtime_profile(env)
            return
        from integrations.steam.game_runtime import apply_game_runtime_profile

        apply_game_runtime_profile(env)
    except Exception as error:
        print(
            f"penguin-burner: per-game profile apply skipped: {error}",
            file=sys.stderr,
        )


SHIM_OUTPUT_ENV = "PENGUIN_BURNER_SHIM_OUTPUT"
# The per-launch marker FIFO path, pinned into the env so the wrapper, the shim
# (via its Z:\ wine path) and the detached drainer all agree on one file.
MARKER_FIFO_ENV = "PENGUIN_BURNER_MARKER_FIFO"


def trace_fifo_path(env: dict[str, str]) -> Path:
    """The marker FIFO for this launch.

    One FIFO *per game session* (named by the wrapper's pid, which exec turns
    into the Proton session's): each launch gets its own drainer, so two
    concurrently wrapped games never share a pipe -- a shared FIFO with one
    reader per wrapper would have the readers stealing each other's lines.
    """
    explicit = str(env.get(MARKER_FIFO_ENV) or "").strip()
    if explicit:
        return Path(explicit)
    return _home_latency_socket_path(env).with_name(
        f"nvapi-trace.{os.getpid()}.fifo"
    )


def _fifo_wine_path(env: dict[str, str]) -> str:
    # Wine maps the unix filesystem root to Z:. The shim runs inside the prefix,
    # so the marker FIFO (created by the launcher on the host) is reachable at
    # its Z:\ path; opening it by path sidesteps the broken inherited fd 2.
    return "Z:" + str(trace_fifo_path(env)).replace("/", "\\")


def _create_marker_fifo(env: dict[str, str]) -> bool:
    """Create this launch's marker FIFO. False when the filesystem refuses."""
    fifo = trace_fifo_path(env)
    try:
        fifo.parent.mkdir(parents=True, exist_ok=True)
        if not fifo.exists():
            os.mkfifo(fifo, 0o600)
        return True
    except OSError:
        return False


def _sweep_stale_marker_fifos(env: dict[str, str]) -> None:
    """Unlink leftover per-launch marker FIFOs that no drainer reads anymore.

    The drainer unlinks its own FIFO on exit, including when it is signalled;
    this catches what is left when it cannot -- SIGKILL, a machine losing
    power. A non-blocking write-only open of a FIFO fails with ENXIO
    exactly when it has no reader -- a live drainer means skip it.
    """
    current = trace_fifo_path(env)
    try:
        siblings = list(current.parent.glob("nvapi-trace*.fifo"))
    except OSError:
        return
    for path in siblings:
        if path == current:
            continue
        try:
            if not stat.S_ISFIFO(path.stat().st_mode):
                continue
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:
            if error.errno == errno.ENXIO:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        os.close(fd)  # a reader exists: another launch's drainer is live


def _route_trace_to_fifo(env: dict[str, str]) -> None:
    """Route wine debug output (process stderr) into an in-memory FIFO.

    The shim writes markers to the FIFO directly; stderr is still routed there
    so marker lines that reach wine's debug output land in the same stream the
    bridge drains, living in the kernel pipe buffer (RAM), never on disk.
    Redirecting the inherited fd (not a path) avoids wine path translation.
    """
    fifo = trace_fifo_path(env)
    try:
        fifo.parent.mkdir(parents=True, exist_ok=True)
        if not fifo.exists():
            os.mkfifo(fifo, 0o600)
        # O_RDWR so the open never blocks even if the bridge has not opened the
        # read end yet (a FIFO opened write-only would block until a reader).
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        os.set_blocking(fd, True)
        os.dup2(fd, 2)
        if fd != 2:
            os.close(fd)
    except OSError:
        # Fall back silently to the inherited stderr; the overlay still shows
        # FPS/clocks, just no in-game latency.
        return


def configure_penguin_burner_environment(
    env: dict[str, str],
    *,
    command_args: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Populate the launch env. Return True when the NVAPI shim is the marker
    source (so the caller arms the re-front watcher)."""
    overlay_config_path = default_overlay_config_path(env)
    env.setdefault(OVERLAY_CONFIG_ENV, str(overlay_config_path))
    env.setdefault(MASTER_ENABLE_ENV, "1")
    env.setdefault(LATENCY_ENABLE_ENV, "1")
    _apply_overlay_enable_alias(env)
    env.setdefault(OVERLAY_ENABLE_ENV, "auto")
    env.setdefault(DXVK_NVAPI_ENABLE_ENV, "1")
    env.setdefault("PROTON_ENABLE_NVAPI", "1")
    env.setdefault("PROTON_HIDE_NVIDIA_GPU", "0")
    shim_active = False
    if ingame_latency_enabled(env):
        # Pin this launch's marker FIFO before anything derives from it (the
        # shim's SHIM_OUTPUT wine path, the drainer's --log, the stderr route).
        env.setdefault(MARKER_FIFO_ENV, str(trace_fifo_path(env)))
        shim_active = _configure_dxvk_nvapi_marker_output(
            env, command_args=command_args
        )
        # Fold the present->scanout display tail into the same opt-in. Both are
        # read-only/gated in the layer and inert where unsupported.
        env.setdefault(DISPLAY_LATENCY_ENV, "1")
        env.setdefault(INJECT_PRESENT_ID_ENV, "1")
    env.setdefault(LATENCY_SOCKET_ENV, str(_home_latency_socket_path(env)))
    env.setdefault(OVERLAY_STATE_ENV, str(overlay_state_path(env)))
    _prepend_layer_paths(env)
    _prepend_enabled_layers(env)
    return shim_active


def _configure_dxvk_nvapi_marker_output(
    env: dict[str, str],
    *,
    command_args: list[str] | tuple[str, ...] | None = None,
) -> bool:
    _ = command_args
    # The drop-in NVAPI shim taps the Reflex markers above vkd3d's owner-gate (so
    # it works under frame generation) and writes them to the marker FIFO the
    # bridge drains -- no trace, no marker-log, no dxvk-nvapi fork. When there is
    # no prefix to front, in-game latency falls back to the Vulkan layer's own
    # vkSetLatencyMarkerNV tap (which covers the single-swapchain / non-FG case).
    fronted = deploy_nvapi_shim(env) is not None
    if not fronted and (
        nvapi_shim_artifact(env) is None or prefix_system32(env) is None
    ):
        # Nothing to front, or nowhere to front it: the layer's own tap is the
        # marker source and no watcher would have anything to guard.
        return False

    # Reached with the shim either already fronted, or frontable but not right
    # now. The second case is a launch that landed inside prefix setup: umu and
    # Proton REMOVE system32\nvapi64.dll before copying their own dxvk-nvapi in,
    # and a wrapper running in that window finds nothing to park. Observed on
    # Lutris + umu, where every launch hit it. Treating that as "no shim" is
    # backwards -- it disarms the re-front watcher, which is the one thing that
    # would have fronted the DLL the moment it reappeared.
    #
    # The shim's default write to fd 2 silently fails in a GUI game process
    # (msvcrt's std fd 2 isn't a writable handle for our loaded DLL: _write(2)
    # returns EBADF with no syscall). Hand it the FIFO's wine path so it
    # _open()s a fresh, valid fd. A user-set SHIM_OUTPUT (debug file) wins.
    # Pinned in both cases: a shim fronted later by the watcher is loaded by the
    # same game process, which reads this env once, at start.
    env.setdefault(SHIM_OUTPUT_ENV, _fifo_wine_path(env))
    return True


def _apply_overlay_enable_alias(env: dict[str, str]) -> None:
    if OVERLAY_ENABLE_ENV in env:
        return
    value = str(env.get(OVERLAY_ENABLE_ENV_ALIAS) or "").strip()
    if value:
        env[OVERLAY_ENABLE_ENV] = value


def _remove_mangohud_environment(env: dict[str, str]) -> None:
    for key in tuple(env):
        upper = key.upper()
        if upper.startswith("MANGOHUD") or upper.startswith("MANGOAPP"):
            env.pop(key, None)

    preload = str(env.get("LD_PRELOAD") or "").strip()
    if not preload:
        return
    entries = [
        entry
        for entry in re.split(r"[:\s]+", preload)
        if entry and "mangohud" not in entry.lower()
    ]
    if entries:
        env["LD_PRELOAD"] = ":".join(entries)
    else:
        env.pop("LD_PRELOAD", None)


def _home_latency_socket_path(env: dict[str, str]) -> Path:
    home = str(env.get("HOME") or "").strip()
    if home and home != "/root":
        return Path(home).expanduser() / ".cache" / "penguin-burner" / "latency.sock"
    state_path = overlay_state_path(env)
    return state_path.with_name("latency.sock")


def _prepend_layer_paths(env: dict[str, str]) -> None:
    layer_paths = [str(path) for path in native_layer_dirs(env)]
    if layer_paths:
        _prepend_path_entries(env, VK_LAYER_PATH_ENV, layer_paths, separator=":")


def _prepend_enabled_layers(env: dict[str, str]) -> None:
    if native_layer_dirs(env):
        _prepend_path_entries(
            env, VK_LAYER_ENABLE_ENV, [LATENCY_LAYER_NAME], separator=","
        )


def _prepend_path_entries(
    env: dict[str, str],
    key: str,
    entries: list[str],
    *,
    separator: str,
) -> None:
    existing = [item for item in str(env.get(key) or "").split(separator) if item]
    merged: list[str] = []
    for item in [*entries, *existing]:
        if item not in merged:
            merged.append(item)
    if merged:
        env[key] = separator.join(merged)


def _prepare_overlay_paths(env: dict[str, str]) -> None:
    path = Path(str(env.get(OVERLAY_STATE_ENV) or "")).expanduser()
    if not str(path):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
