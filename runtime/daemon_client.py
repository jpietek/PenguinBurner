from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import sys
from typing import Any


DEFAULT_DAEMON_SOCKET = "/run/penguin-burnerd.sock"

# Overrides the daemon socket for the milestone-B client wrappers below (GPU
# writes, profile verification, profile deletion). Production leaves it unset
# (the daemon always listens on DEFAULT_DAEMON_SOCKET); tests point it at a
# temp socket, and a daemon-spawned child could inherit it for a nonstandard
# --socket setup.
DAEMON_SOCKET_ENV = "PENGUIN_BURNER_DAEMON_SOCKET"

# GPU writes ride slow NVML/NVAPI driver calls (supported-clock enumeration,
# VF-curve get-mutate-set) and serialize under the daemon's backend mutex, so
# they get a far larger budget than the 3 s control-plane default.
GPU_WRITE_TIMEOUT_S = 30.0
GPU_READ_TIMEOUT_S = 10.0
DAEMON_PROTOCOL_MAJOR = 2


class DaemonCompatibilityError(RuntimeError):
    """The reachable daemon cannot serve this client's required protocol."""


def client_host_pid() -> int:
    """Our PID as seen by the root daemon, even inside a PID namespace."""
    result = daemon_request("client_identity", socket_path=_resolved_socket_path(None))
    pid = result.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RuntimeError("PenguinBurner daemon did not return a valid host PID")
    return pid


def _resolved_socket_path(socket_path: str | Path | None) -> str | Path:
    if socket_path is not None:
        return socket_path
    return os.environ.get(DAEMON_SOCKET_ENV, "").strip() or DEFAULT_DAEMON_SOCKET


def daemon_request(
    method: str,
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    return daemon_payload_request(
        {"method": str(method)},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def daemon_payload_request(
    request: dict[str, Any],
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(float(timeout_s))
            client.connect(str(socket_path))
            client.sendall(
                (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
            )
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
    except FileNotFoundError as exc:
        raise RuntimeError(f"PenguinBurner daemon socket not found: {socket_path}") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to connect to PenguinBurner daemon: {exc}") from exc

    line = b"".join(chunks).split(b"\n", 1)[0].decode("utf-8", errors="replace")
    if not line:
        raise RuntimeError("PenguinBurner daemon returned an empty response")
    response = json.loads(line)
    if not isinstance(response, dict):
        raise RuntimeError("PenguinBurner daemon returned an invalid response")
    if not response.get("ok"):
        raise RuntimeError(
            str(response.get("error") or "PenguinBurner daemon request failed")
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("PenguinBurner daemon returned an invalid result")
    return result


def daemon_stream_request(
    request: dict[str, Any],
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(float(timeout_s))
            client.connect(str(socket_path))
            client.settimeout(None)
            client.sendall(
                (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
            )
            buffer = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    if buffer.strip():
                        yield _decode_response_line(buffer)
                    return
                buffer += chunk
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    if raw_line.strip():
                        yield _decode_response_line(raw_line)
    except FileNotFoundError as exc:
        raise RuntimeError(f"PenguinBurner daemon socket not found: {socket_path}") from exc
    except OSError as exc:
        raise RuntimeError(f"failed to connect to PenguinBurner daemon: {exc}") from exc


def daemon_status(
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    return daemon_request("status", socket_path=socket_path, timeout_s=timeout_s)


def require_daemon_capabilities(
    *required: str,
    socket_path: str | Path | None = None,
    timeout_s: float = 3.0,
    expected_version: str | None = None,
) -> dict[str, Any]:
    """Return status only when the requested compatibility contract matches."""
    status = daemon_status(
        socket_path=_resolved_socket_path(socket_path),
        timeout_s=timeout_s,
    )
    raw_protocol_major = status.get("protocol_major")
    if raw_protocol_major is None:
        raise DaemonCompatibilityError(
            "PenguinBurner hardware service predates the versioned protocol"
        )
    try:
        protocol_major = int(raw_protocol_major)
    except (TypeError, ValueError) as exc:
        raise DaemonCompatibilityError(
            "PenguinBurner hardware service predates the versioned protocol"
        ) from exc
    if protocol_major != DAEMON_PROTOCOL_MAJOR:
        raise DaemonCompatibilityError(
            "PenguinBurner hardware service protocol mismatch: "
            f"client={DAEMON_PROTOCOL_MAJOR}, daemon={protocol_major}"
        )
    expected_release = str(expected_version or "").strip()
    daemon_release = str(status.get("version") or "").strip()
    if expected_release and daemon_release != expected_release:
        raise DaemonCompatibilityError(
            "PenguinBurner hardware service release mismatch: "
            f"app={expected_release}, daemon={daemon_release or 'unknown'}"
        )
    available = {
        str(item)
        for item in status.get("capabilities", [])
        if str(item).strip()
    }
    missing = sorted({str(item) for item in required if str(item)} - available)
    if missing:
        raise DaemonCompatibilityError(
            "PenguinBurner hardware service is missing required capabilities: "
            + ", ".join(missing)
        )
    return status


def gpu_capabilities(
    gpu_index: int,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_READ_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_capabilities",
        gpu_index,
        {},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_telemetry(
    gpu_index: int,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_READ_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_telemetry",
        gpu_index,
        {},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_vf_snapshot(
    gpu_index: int,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_READ_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_vf_snapshot",
        gpu_index,
        {},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def probe_power_limit_support(
    gpu_index: int,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = 1.0,
) -> dict[str, Any]:
    return daemon_payload_request(
        {"method": "probe_power_limit_support", "gpu_index": int(gpu_index)},
        socket_path=_resolved_socket_path(socket_path),
        timeout_s=timeout_s,
    )


def apply_runtime_spec(
    spec: dict[str, Any],
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 45.0,
) -> dict[str, Any]:
    return daemon_payload_request(
        {"method": "apply_runtime_spec", "spec": dict(spec)},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def apply_runtime_intent(
    intent: dict[str, Any],
    *,
    persist_on_startup: bool = False,
    clear_boot: bool = False,
    socket_path: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve and apply a user runtime intent through the root daemon.

    ``persist_on_startup`` saves the applied spec as the boot profile;
    ``clear_boot`` removes any saved boot profile so the apply is
    session-only. With neither flag the boot entry is left untouched.
    """
    from runtime.runtime_spec import build_runtime_spec_from_intent

    resolved_socket = _resolved_socket_path(socket_path)
    spec = build_runtime_spec_from_intent(intent, socket_path=resolved_socket)
    result = apply_runtime_spec(spec, socket_path=resolved_socket)
    if persist_on_startup:
        set_boot_runtime_spec(spec, socket_path=resolved_socket)
    elif clear_boot:
        gpu = spec.get("gpu") if isinstance(spec, dict) else None
        gpu_uuid = (
            str(gpu.get("uuid") or "").strip() if isinstance(gpu, dict) else ""
        )
        clear_boot_runtime_spec(gpu_uuid=gpu_uuid, socket_path=resolved_socket)
    return result


def set_boot_runtime_spec(
    spec: dict[str, Any],
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    return daemon_payload_request(
        {"method": "set_boot_runtime_spec", "spec": dict(spec)},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def clear_boot_runtime_spec(
    *,
    gpu_uuid: str = "",
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    request: dict[str, Any] = {"method": "clear_boot_runtime_spec"}
    selected_uuid = str(gpu_uuid or "").strip()
    if selected_uuid:
        request["gpu_uuid"] = selected_uuid
    return daemon_payload_request(
        request,
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def set_boot_main_gpu(
    gpu_uuid: str,
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    return daemon_payload_request(
        {"method": "set_boot_main_gpu", "gpu_uuid": str(gpu_uuid or "").strip()},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def boot_runtime_spec(
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    return daemon_request(
        "boot_runtime_spec",
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def start_game_runtime_profile(
    argv: list[str],
    *,
    watch_pid: int,
    app_id: str = "",
    socket_path: str | Path | None = None,
    timeout_s: float = 45.0,
) -> dict[str, Any]:
    """Resolve legacy profile argv as the unprivileged user, then apply it.

    The Rust daemon receives only an immutable RuntimeSpec; it never reads the
    user's mutable profile/config files and it never revives the removed Python
    daemon implementation.
    """
    from runtime.runtime_spec import (
        build_runtime_spec_from_intent,
        runtime_intent_from_argv,
    )

    spec = build_runtime_spec_from_intent(
        runtime_intent_from_argv(argv),
        socket_path=socket_path,
    )
    return start_game_runtime_spec(
        spec,
        watch_pid=watch_pid,
        app_id=app_id,
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def start_game_runtime_spec(
    spec: dict[str, Any],
    *,
    watch_pid: int,
    app_id: str = "",
    socket_path: str | Path | None = None,
    timeout_s: float = 45.0,
) -> dict[str, Any]:
    return daemon_payload_request(
        {
            "method": "start_game_runtime_profile",
            "spec": dict(spec),
            "watch_pid": int(watch_pid),
            "app_id": str(app_id),
        },
        socket_path=_resolved_socket_path(socket_path),
        timeout_s=timeout_s,
    )


def stop_runtime_profile(
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    return daemon_request(
        "stop_runtime_profile",
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


# --- GPU write RPCs (milestone B) ------------------------------------------
#
# Thin wrappers over the Rust daemon's gpu_* methods (burnerd/src/gpu_rpc.rs).
# The daemon relays the backend driver's exact error text in the {"ok":false,
# "error":...} envelope, which daemon_payload_request raises verbatim as the
# RuntimeError message -- consumers pattern-match those strings, so nothing may
# rephrase them.


def _gpu_request(
    method: str,
    gpu_index,
    extra: dict[str, Any],
    *,
    socket_path: str | Path | None,
    timeout_s: float,
) -> dict[str, Any]:
    return daemon_payload_request(
        {"method": method, "gpu_index": int(gpu_index), **extra},
        socket_path=_resolved_socket_path(socket_path),
        timeout_s=timeout_s,
    )


def gpu_apply_vf_offsets(
    gpu_index,
    offsets,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    """Apply a VF plan of ``(index, offset_khz)`` pairs.

    The daemon owns the NVAPI get-mutate-set cycle: it reads the live control
    struct, overwrites ``freq_offset_khz`` for exactly the listed indices
    (preserving every non-listed point), and sets the result.
    """
    plan = [[int(index), int(offset_khz)] for index, offset_khz in offsets]
    return _gpu_request(
        "gpu_apply_vf_offsets",
        gpu_index,
        {"offsets": plan},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_apply_power_limit(
    gpu_index,
    power_limit_w,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_apply_power_limit",
        gpu_index,
        {"power_limit_w": int(power_limit_w)},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_apply_clock_offsets(
    gpu_index,
    *,
    gpc_clk_vf_offset_mhz=None,
    mem_clk_vf_offset_mhz=None,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if gpc_clk_vf_offset_mhz is not None:
        extra["gpc_clk_vf_offset_mhz"] = int(gpc_clk_vf_offset_mhz)
    if mem_clk_vf_offset_mhz is not None:
        extra["mem_clk_vf_offset_mhz"] = int(mem_clk_vf_offset_mhz)
    return _gpu_request(
        "gpu_apply_clock_offsets",
        gpu_index,
        extra,
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_apply_locked_core_clock(
    gpu_index,
    clock_mhz,
    *,
    prefer_not_above: bool = True,
    snap_to_supported: bool = True,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_apply_locked_core_clock",
        gpu_index,
        {
            "clock_mhz": int(clock_mhz),
            "prefer_not_above": bool(prefer_not_above),
            "snap_to_supported": bool(snap_to_supported),
        },
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_apply_locked_core_clock_range(
    gpu_index,
    min_clock_mhz,
    max_clock_mhz,
    *,
    prefer_max_not_above: bool = True,
    snap_to_supported: bool = True,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_apply_locked_core_clock_range",
        gpu_index,
        {
            "min_mhz": int(min_clock_mhz),
            "max_mhz": int(max_clock_mhz),
            "prefer_max_not_above": bool(prefer_max_not_above),
            "snap_to_supported": bool(snap_to_supported),
        },
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_reset_locked_core_clocks(
    gpu_index,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_reset_locked_core_clocks",
        gpu_index,
        {},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_reset_locked_memory_clocks(
    gpu_index,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_reset_locked_memory_clocks",
        gpu_index,
        {},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_reset_defaults(
    gpu_index,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_reset_defaults",
        gpu_index,
        {},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_reset_fans(
    gpu_index,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_reset_fans",
        gpu_index,
        {},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def gpu_enable_persistence_mode(
    gpu_index,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = GPU_WRITE_TIMEOUT_S,
) -> dict[str, Any]:
    return _gpu_request(
        "gpu_enable_persistence_mode",
        gpu_index,
        {},
        socket_path=socket_path,
        timeout_s=timeout_s,
    )


def delete_auto_uv_profiles(
    profile_paths,
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Ask the root daemon to delete saved Auto-UV profile files.

    The daemon canonicalizes and prefix-enforces every path against the
    effective user's ``~/.config/PenguinBurner/auto-uv-profiles`` dir; any
    rejected path fails the whole request with nothing deleted.
    """
    return daemon_payload_request(
        {
            "method": "delete_auto_uv_profiles",
            "paths": [str(path) for path in profile_paths],
        },
        socket_path=_resolved_socket_path(socket_path),
        timeout_s=timeout_s,
    )


def stop_profile_verification(
    *,
    socket_path: str | Path | None = None,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    return daemon_request(
        "stop_profile_verification",
        socket_path=_resolved_socket_path(socket_path),
        timeout_s=timeout_s,
    )


def stream_profile_verification(
    options: dict[str, Any],
    *,
    socket_path: str | Path | None = None,
    stdout=None,
    stderr=None,
) -> int:
    """Run a daemon-side profile verification, relaying its output lines.

    Mirrors stream_auto_uv_scan: SIGINT converts into a cooperative
    stop_profile_verification request instead of killing the stream.
    """
    socket_path = _resolved_socket_path(socket_path)
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    def _request_stop(_signum, _frame):
        try:
            stop_profile_verification(socket_path=socket_path, timeout_s=1.0)
        except Exception as exc:
            print(
                f"warning: failed to request profile verification stop: {exc}",
                file=stderr,
                flush=True,
            )

    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _request_stop)
    exit_code = 1
    try:
        for payload in daemon_stream_request(
            {"method": "start_profile_verification", "options": options},
            socket_path=socket_path,
        ):
            if not payload.get("ok"):
                print(
                    str(payload.get("error") or "profile verification daemon request failed"),
                    file=stderr,
                    flush=True,
                )
                return 1
            line = payload.get("line")
            if isinstance(line, str):
                print(line, end="", file=stdout, flush=True)
                continue
            if payload.get("control") == "finished":
                exit_code = int(payload.get("exit_code", 1))
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
    return int(exit_code)


def stream_auto_uv_scan(
    options: dict[str, Any],
    *,
    socket_path: str | Path = DEFAULT_DAEMON_SOCKET,
    stdout=None,
    stderr=None,
) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr

    def _request_stop(_signum, _frame):
        try:
            daemon_request("stop_auto_uv_scan", socket_path=socket_path, timeout_s=1.0)
        except Exception as exc:
            print(f"warning: failed to request Auto-UV stop: {exc}", file=stderr, flush=True)

    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _request_stop)
    exit_code = 1
    try:
        for payload in daemon_stream_request(
            {"method": "start_auto_uv_scan", "options": options},
            socket_path=socket_path,
        ):
            if not payload.get("ok"):
                print(str(payload.get("error") or "Auto-UV daemon request failed"), file=stderr, flush=True)
                return 1
            line = payload.get("line")
            if isinstance(line, str):
                print(line, end="", file=stdout, flush=True)
                continue
            if payload.get("control") == "finished":
                exit_code = int(payload.get("exit_code", 1))
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
    return int(exit_code)


def migrate_legacy_boot_intent(*, socket_path=None) -> int:
    """Replay a 0.6.x last-runtime.json boot profile onto the new daemon.

    Runs host-side (as root) from the elevated daemon install/migration
    script, after the new daemon is reachable — the flatpak sandbox cannot
    see host /var/lib, so this recovery cannot happen client-side. The
    legacy file is consumed on success or when there is nothing worth
    recovering; a failed apply keeps it for the next repair attempt and
    never fails the daemon install itself.
    """
    import shlex

    from runtime.runtime_spec import runtime_intent_from_argv
    from runtime.support.runtime_service import (
        LAST_RUNTIME_STATE_PATH,
        read_legacy_last_runtime_argv,
    )

    argv = read_legacy_last_runtime_argv()
    if not argv:
        LAST_RUNTIME_STATE_PATH.unlink(missing_ok=True)
        return 0
    try:
        intent = runtime_intent_from_argv(argv)
        apply_runtime_intent(intent, persist_on_startup=True, socket_path=socket_path)
    except Exception as error:
        print(
            "warning: could not migrate the 0.6.x apply-on-startup profile "
            f"({shlex.join(argv)}): {error}",
            file=sys.stderr,
            flush=True,
        )
        return 0
    LAST_RUNTIME_STATE_PATH.unlink(missing_ok=True)
    print(
        f"Migrated 0.6.x apply-on-startup profile: {shlex.join(argv)}",
        flush=True,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PenguinBurner daemon client")
    parser.add_argument(
        "--socket",
        default=None,
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("stop-auto-uv")
    subparsers.add_parser("stop-runtime-profile")
    subparsers.add_parser("stop-profile-verification")
    start = subparsers.add_parser("start-auto-uv")
    start.add_argument("options_json")
    runtime = subparsers.add_parser("apply-runtime-intent")
    runtime.add_argument("--boot", action="store_true")
    runtime.add_argument("--clear-boot", action="store_true")
    runtime.add_argument("intent_json")
    subparsers.add_parser("migrate-legacy-boot-intent")
    runtime_spec = subparsers.add_parser("apply-runtime-spec")
    runtime_spec.add_argument("spec_json")
    boot_spec = subparsers.add_parser("set-boot-runtime-spec")
    boot_spec.add_argument("spec_json")
    main_gpu = subparsers.add_parser("set-boot-main-gpu")
    main_gpu.add_argument("--gpu-uuid", default="")
    subparsers.add_parser("boot-runtime-spec")
    clear_boot = subparsers.add_parser("clear-boot-runtime-spec")
    clear_boot.add_argument("--gpu-uuid", default="")
    verify = subparsers.add_parser("start-profile-verification")
    verify.add_argument("options_json")
    delete = subparsers.add_parser("delete-auto-uv-profiles")
    delete.add_argument("paths_json")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    # No explicit --socket: honor the env override, else the default socket.
    args.socket = _resolved_socket_path(args.socket)

    try:
        if args.command == "status":
            print(
                json.dumps(daemon_status(socket_path=args.socket), indent=2),
                flush=True,
            )
            return 0
        if args.command == "boot-runtime-spec":
            print(
                json.dumps(boot_runtime_spec(socket_path=args.socket), indent=2),
                flush=True,
            )
            return 0
        if args.command == "set-boot-main-gpu":
            print(
                json.dumps(
                    set_boot_main_gpu(args.gpu_uuid, socket_path=args.socket),
                    indent=2,
                ),
                flush=True,
            )
            return 0
        if args.command == "stop-auto-uv":
            print(
                json.dumps(
                    daemon_request("stop_auto_uv_scan", socket_path=args.socket),
                    indent=2,
                ),
                flush=True,
            )
            return 0
        if args.command == "stop-runtime-profile":
            print(
                json.dumps(stop_runtime_profile(socket_path=args.socket), indent=2),
                flush=True,
            )
            return 0
        if args.command == "stop-profile-verification":
            print(
                json.dumps(
                    stop_profile_verification(socket_path=args.socket),
                    indent=2,
                ),
                flush=True,
            )
            return 0
        if args.command == "start-auto-uv":
            options = json.loads(args.options_json)
            if not isinstance(options, dict):
                raise RuntimeError("Auto-UV options JSON must be an object")
            return stream_auto_uv_scan(options, socket_path=args.socket)
        if args.command == "start-profile-verification":
            options = json.loads(args.options_json)
            if not isinstance(options, dict):
                raise RuntimeError("profile verification options JSON must be an object")
            return stream_profile_verification(options, socket_path=args.socket)
        if args.command == "delete-auto-uv-profiles":
            paths = json.loads(args.paths_json)
            if not isinstance(paths, list) or not all(
                isinstance(item, str) for item in paths
            ):
                raise RuntimeError("profile paths JSON must be a string list")
            result = delete_auto_uv_profiles(paths, socket_path=args.socket)
            print(json.dumps(result, indent=2), flush=True)
            deleted = result.get("deleted")
            count = len(deleted) if isinstance(deleted, list) else 0
            label = "profile" if count == 1 else "profiles"
            print(f"Deleted {count} saved Auto-UV {label}.", flush=True)
            return 0
        if args.command == "apply-runtime-intent":
            intent = json.loads(args.intent_json)
            result = apply_runtime_intent(
                intent,
                persist_on_startup=bool(args.boot),
                clear_boot=bool(args.clear_boot),
                socket_path=args.socket,
            )
            print(
                json.dumps(result, indent=2),
                flush=True,
            )
            return 0
        if args.command == "migrate-legacy-boot-intent":
            return migrate_legacy_boot_intent(socket_path=args.socket)
        if args.command in {"apply-runtime-spec", "set-boot-runtime-spec"}:
            spec = json.loads(args.spec_json)
            if not isinstance(spec, dict):
                raise RuntimeError("runtime spec JSON must be an object")
            result = (
                apply_runtime_spec(spec, socket_path=args.socket)
                if args.command == "apply-runtime-spec"
                else set_boot_runtime_spec(spec, socket_path=args.socket)
            )
            print(json.dumps(result, indent=2), flush=True)
            return 0
        if args.command == "clear-boot-runtime-spec":
            print(
                json.dumps(
                    clear_boot_runtime_spec(
                        gpu_uuid=args.gpu_uuid,
                        socket_path=args.socket,
                    ),
                    indent=2,
                ),
                flush=True,
            )
            return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    return 2


def _decode_response_line(raw_line: bytes) -> dict[str, Any]:
    payload = json.loads(raw_line.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError("PenguinBurner daemon returned an invalid response")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
