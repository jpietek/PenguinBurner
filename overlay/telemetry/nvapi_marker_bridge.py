"""Bridge NVAPI-shim Reflex marker lines into the latency receiver.

The drop-in NVAPI shim (overlay/shim_deploy.py) writes one line per Reflex
marker to the launch's marker FIFO, reusing the wire format the old
dxvk-nvapi marker-log fork established. The PenguinBurner wrapper also routes
stderr into the same FIFO, so every marker line is drained by this bridge
without writing a Proton log to disk.

This bridge tails that stream, pairs SIMULATION_START (NV marker 0) with
PRESENT_END (NV marker 5) by frame id, and sends the resulting
``sim_to_present_us`` span to the PenguinBurner latency socket as a
``marker-proxy`` timing sample. When the title also emits INPUT_SAMPLE
(NV marker 6) -- the full Reflex PCL instrumentation, present in some titles
but not all -- the bridge additionally pairs it with the
present to report ``input_to_present_us`` (the true input-to-present Reflex
lag) and ``input_to_oob_present_us`` (input through the frame-generation hold). The existing receiver -> overlay-state
publisher -> Vulkan overlay path then surfaces it as ``latency_ms`` with no
downstream changes.

When frame generation is active the application's PRESENT_END happens *before*
the generated-frame pacing displays the frame, so ``sim_to_present_us`` cannot
see the frame-generation hold (it reads unrealistically low, e.g. ~18 ms at a
40 fps base). To capture that, the bridge also pairs each base frame's
SIMULATION_START with the first OUT_OF_BAND_PRESENT_END (NV marker 12) that
occurs at or after that frame's PRESENT_END, and emits the wider
``sim_to_oob_present_us`` span. That displayed-present span is the closest
software-measurable proxy to click-to-photon: it spans real timestamps from
input/simulation up to the actual hand-off to the display, omitting only the
two unmeasurable ends (peripheral input and physical scanout/panel).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from .sockets import latency_socket_path, latency_socket_paths
from .. import shim_deploy as _shim_deploy

# NV_LATENCY_MARKER_TYPE values named on the shim's marker lines.
NV_MARKER_SIMULATION_START = 0
NV_MARKER_PRESENT_END = 5
NV_MARKER_INPUT_SAMPLE = 6
NV_MARKER_OUT_OF_BAND_PRESENT_START = 11
NV_MARKER_OUT_OF_BAND_PRESENT_END = 12
NV_FRAMEGEN_MARKERS = {
    NV_MARKER_OUT_OF_BAND_PRESENT_START,
    NV_MARKER_OUT_OF_BAND_PRESENT_END,
}

# The shim's marker-line wire format (inherited from the dxvk-nvapi
# marker-log fork so the parser survived the source swap unchanged).
_MARKER_LOG_RE = re.compile(
    r"^\d+\.\d+:([0-9a-fA-F]+):[0-9a-fA-F]+:"
    r"latency-marker:[^:]+:qpcUs=(\d+)\s+.*\bframeID=(\d+)\s+"
    r"markerType=(\w+)(?:\s+markerValue=(\d+))?"
)
_MARKER_NAMES = {
    "SIMULATION_START": NV_MARKER_SIMULATION_START,
    "PRESENT_END": NV_MARKER_PRESENT_END,
    "INPUT_SAMPLE": NV_MARKER_INPUT_SAMPLE,
    "OUT_OF_BAND_PRESENT_START": NV_MARKER_OUT_OF_BAND_PRESENT_START,
    "OUT_OF_BAND_PRESENT_END": NV_MARKER_OUT_OF_BAND_PRESENT_END,
}


class _GameOutputPassthrough:
    """Hand the game's own stderr back to whoever launched it.

    The wrapper redirects the game's fd 2 into the marker FIFO, so every
    Proton/wine diagnostic ends up here instead of in the launcher's log --
    Lutris, Steam, or a terminal all go silent for the whole session. The
    drainer is deliberately spawned *before* that redirect, so its own stderr
    is still the launcher's; writing there restores what the redirect took.

    Only lines that are not PenguinBurner markers are forwarded. Marker lines
    exist solely because we asked dxvk-nvapi to emit them, so echoing them
    would add noise the user never had.

    Forwarding is best effort: once the launcher's pipe is gone, further
    writes are pointless, so it latches off. Draining must continue either way
    -- it is what keeps a full pipe from freezing the game.
    """

    def __init__(self, stream=None) -> None:
        self._stream = stream
        self._failed = False

    def forward(self, line: str) -> None:
        if self._failed:
            return
        stream = sys.stderr if self._stream is None else self._stream
        try:
            stream.write(line if line.endswith("\n") else line + "\n")
            stream.flush()
        except (OSError, ValueError):
            self._failed = True

    @property
    def failed(self) -> bool:
        return self._failed


def _parse_line_with_pid(line: str):
    """Return (frame, nv_marker, t_us, source_pid) for a marker line."""
    m = _MARKER_LOG_RE.search(line)
    if m:
        marker = _MARKER_NAMES.get(m.group(4))
        if marker is None:
            return None
        return int(m.group(3)), marker, int(m.group(2)), int(m.group(1), 16)
    return None

# A frame id whose PRESENT_END never arrives must not leak forever; keep only a
# small window of pending SIMULATION_START timestamps.
_MAX_PENDING = 512

# Detached-drainer bail-out for launches that die before any writer connects to
# the FIFO (see _follow_fifo): how long the launching session must be gone, with
# no traffic ever seen, before the drainer gives up. Generous on purpose -- the
# cost of waiting is one idle process, the cost of leaving early is a game that
# could still fill the pipe unread.
_NO_WRITER_GRACE_S = 60.0

# An out-of-band (frame-generation) present this much later than a base frame's
# PRESENT_END is treated as a different frame's display, not this one's. Bounds a
# mis-pairing to a sane click-to-photon magnitude (log timestamps are ms-grained).
_MAX_OOB_LAG_US = 200_000
TELEMETRY_SESSION_ENV = "PENGUIN_BURNER_TELEMETRY_SESSION"

_FifoIdentity = tuple[int, int]
_SignalHandler = int | Callable[[int, FrameType | None], object]
_SignalMask = set[int | signal.Signals]
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


def _socket_targets(env: dict[str, str] | None = None) -> list[Path]:
    env = dict(os.environ) if env is None else env
    try:
        targets = list(latency_socket_paths(env))
    except Exception:
        targets = []
    primary = latency_socket_path(env)
    if primary not in targets:
        targets.insert(0, primary)
    return targets


def _send_sample(sock: socket.socket, targets: list[Path], sample: dict) -> None:
    line = json.dumps(sample, separators=(",", ":")).encode("utf-8")
    for target in targets:
        try:
            sock.sendto(line, str(target))
            return
        except OSError:
            continue


def default_log_path(env: dict[str, str] | None = None) -> Path:
    paths = latency_socket_paths(env)
    return paths[-1].with_name("nvapi-trace.fifo")


def _stop_requested(stop_event: threading.Event | None) -> bool:
    return stop_event is not None and stop_event.is_set()


def _wait_or_stopped(
    stop_event: threading.Event | None,
    poll_interval_s: float,
) -> bool:
    if stop_event is not None:
        return stop_event.wait(poll_interval_s)
    time.sleep(poll_interval_s)
    return False


def _resolve_oob_present(
    sock: socket.socket,
    targets: list[Path],
    awaiting_oob: list[tuple[int, int, int, int]],
    oob_present_end_us: int,
    pid: int,
    session_id: str,
) -> int:
    """Emit the frame-generation-inclusive span for one displayed base frame.

    ``awaiting_oob`` holds (frame, input_us, sim_us, present_end_us) for base
    frames whose application present has completed but whose generated-frame
    display present has not yet been seen, in present order (``input_us`` is 0
    when the game emits no INPUT_SAMPLE marker). This out-of-band PRESENT_END
    resolves the oldest base frame whose app-present completed at or before it,
    one-to-one, so the span is not inflated by the extra generated presents.
    Returns the number of samples emitted (0 or 1).
    """
    while awaiting_oob:
        frame, input_us, sim_us, present_end_us = awaiting_oob[0]
        if present_end_us > oob_present_end_us:
            # Base frame presented after this out-of-band present; a later one
            # displays it. List is in present order, so nothing older remains.
            break
        awaiting_oob.pop(0)
        if oob_present_end_us - present_end_us > _MAX_OOB_LAG_US:
            # Too far apart to be this frame's display; drop without an OOB span.
            continue
        sample = {
            "v": 1,
            "type": "timing",
            "measurement": "marker-proxy",
            "source": "nvapi-marker-log",
            "pid": pid,
            "present_id": frame,
            "quality": "reflex-markers",
            "marker_bits": 1,
            "sim_to_present_us": present_end_us - sim_us,
            "sim_to_oob_present_us": oob_present_end_us - sim_us,
            # The displayed-present span feeds the latency ladder only. Frame
            # generation is inferred from cadence by the receiver, not asserted
            # here -- out-of-band presents occur with frame gen off too.
            "framegen_active": False,
        }
        if session_id:
            sample["session_id"] = session_id
        if input_us and oob_present_end_us > input_us:
            # Game emits INPUT_SAMPLE: input -> displayed present is the widest
            # input-anchored span (full Reflex input lag through the FG hold).
            sample["input_to_present_us"] = present_end_us - input_us
            sample["input_to_oob_present_us"] = oob_present_end_us - input_us
        _send_sample(sock, targets, sample)
        return 1
    return 0


def _follow_fifo(
    path: Path,
    *,
    poll_interval_s: float,
    stop_event: threading.Event | None = None,
    session_alive_fn=None,
    session_quiesced_fn=None,
):
    """Yield lines from a named pipe (in-memory trace stream).

    Opened read-only/non-blocking so the bridge never blocks waiting for a
    writer; lines are drained as the game produces them. On writer close
    (game exit) readline returns EOF and we reopen.

    ``session_alive_fn`` scopes a detached drainer's lifetime. The drainer must
    keep draining as long as *any* real game process holds the FIFO's write side
    (a wrapped game can outlive the wrapper session that launched it), so the
    plain session check ends the watch only where no writer can be feeding the
    pipe:

    - at EOF or open failure -- writers existed and are all gone, or the FIFO
      itself is gone;
    - on the quiet-select path, ONLY before any traffic has ever been seen and
      only after the session has been gone for a sustained grace period. A FIFO
      reader gets *no* poll event until a writer has connected at least once,
      so a launch that dies before routing its stderr (failed exec) would
      otherwise park the drainer forever. Once a single byte or EOF has been
      observed, writers demonstrably existed and the EOF path is reliable.

    ``session_quiesced_fn`` is narrower: Steam's reaper can keep the write side
    open after the game exits because PenguinBurner's own helpers are still its
    children. In that specific process-tree shape, the quiet path exits so the
    reaper can finish closing the app.
    """

    def _session_over() -> bool:
        return session_alive_fn is not None and not session_alive_fn()

    def _session_quiesced() -> bool:
        return session_quiesced_fn is not None and session_quiesced_fn()

    had_traffic = False
    # A writer connected and then every writer closed. From that point a quiet
    # select means "nobody is left to write", not "the writer is idle" -- so
    # once the session is gone too there is nothing left to wait for.
    writers_closed = False
    session_over_since: float | None = None
    while not _stop_requested(stop_event):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            if _session_over():
                return
            _wait_or_stopped(stop_event, poll_interval_s)
            continue
        pending = ""
        try:
            while not _stop_requested(stop_event):
                readable, _writable, _errors = select.select(
                    [fd], [], [], poll_interval_s
                )
                if not readable:
                    if _session_quiesced():
                        return
                    if not _session_over():
                        session_over_since = None
                        continue
                    if writers_closed:
                        # Every writer has closed and the launching session is
                        # gone, so no byte can ever arrive. Exiting promptly
                        # matters beyond tidiness: this process holds the
                        # launcher's output pipe, and Lutris waits on that pipe
                        # for EOF to decide the game has finished. Lingering
                        # here left the game "running" until stopped by hand.
                        return
                    if had_traffic:
                        # Writers exist and are merely idle -- a wrapped game
                        # can outlive the session that launched it.
                        continue
                    now = time.monotonic()
                    if session_over_since is None:
                        session_over_since = now
                    elif now - session_over_since >= _NO_WRITER_GRACE_S:
                        return  # session gone and no writer ever connected
                    continue
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                had_traffic = True
                if not chunk:
                    # Writer closed (game exited): reopen and wait for the next run.
                    writers_closed = True
                    break
                writers_closed = False
                pending += chunk.decode("utf-8", errors="replace")
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    yield line + "\n"
        finally:
            os.close(fd)
        if _session_over() or _session_quiesced():
            return  # no writers left and the launching session is gone
        _wait_or_stopped(stop_event, poll_interval_s)


def _follow(
    path: Path,
    *,
    poll_interval_s: float,
    from_start: bool,
    stop_event: threading.Event | None = None,
    session_alive_fn=None,
    session_quiesced_fn=None,
):
    """Yield lines from a FIFO (in-memory) or a growing/rotating file."""
    try:
        if path.exists() and stat.S_ISFIFO(path.stat().st_mode):
            yield from _follow_fifo(
                path,
                poll_interval_s=poll_interval_s,
                stop_event=stop_event,
                session_alive_fn=session_alive_fn,
                session_quiesced_fn=session_quiesced_fn,
            )
            return
    except OSError:
        pass
    handle = None
    inode = None
    # A user-pointed SHIM_OUTPUT file can grow fast. If we ever fall this far
    # behind the write head, jump to live instead of slogging through stale
    # backlog -- the overlay only needs *current* latency, so a gap is fine
    # and staying current is what prevents the fallback stall.
    max_lag_bytes = 4 * 1024 * 1024
    while not _stop_requested(stop_event):
        try:
            if handle is None:
                handle = path.open("r", encoding="utf-8", errors="replace")
                inode = os.fstat(handle.fileno()).st_ino
                if not from_start:
                    handle.seek(0, os.SEEK_END)
            line = handle.readline()
            if line:
                yield line
                continue
            # Caught up to EOF: check rotation, then skip-to-live if lagging.
            try:
                st = path.stat()
                if st.st_ino != inode:
                    handle.close()
                    handle = None
                    continue
                if st.st_size - handle.tell() > max_lag_bytes:
                    handle.seek(max(0, st.st_size - max_lag_bytes // 2))
                    handle.readline()  # discard partial line
                    continue
            except OSError:
                handle.close()
                handle = None
                continue
            _wait_or_stopped(stop_event, poll_interval_s)
        except OSError:
            if handle is not None:
                handle.close()
            handle = None
            _wait_or_stopped(stop_event, poll_interval_s)
    if handle is not None:
        handle.close()


def run(
    log_path: Path,
    *,
    poll_interval_s: float = 0.25,
    from_start: bool = False,
    env: dict[str, str] | None = None,
    pid: int | None = None,
    stop_event: threading.Event | None = None,
    session_alive_fn=None,
    session_quiesced_fn=None,
    passthrough: _GameOutputPassthrough | None = None,
) -> None:
    env = dict(os.environ) if env is None else env
    # The wrapper redirected the game's stderr into this FIFO, so the launcher
    # sees nothing unless the non-marker lines are handed back.
    passthrough = _GameOutputPassthrough() if passthrough is None else passthrough
    targets = _socket_targets(env)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    # Never block on a slow/full receiver: unix datagram sendto() BLOCKS when
    # the destination queue is full (unlike UDP), which would stop the drain
    # and let the FIFO back up into the game. Full queue -> drop the sample.
    sock.setblocking(False)
    pid = os.getpid() if pid is None else pid
    session_id = str(env.get(TELEMETRY_SESSION_ENV) or "").strip()

    try:
        pending_sim: dict[int, int] = {}
        pending_input: dict[int, int] = {}
        framegen_marker_frames: dict[int, int] = {}
        order: list[int] = []
        input_order: list[int] = []
        framegen_order: list[int] = []
        framegen_active_until_us = 0
        # The shim sees the application's original Reflex frame IDs before
        # DXVK/Vulkan and frame generation. Consecutive unique SIMULATION_START
        # markers therefore provide the canonical pre-frame-generation cadence.
        last_simulation_start: dict[int, tuple[int, int]] = {}
        # Base frames awaiting their out-of-band (frame-generation) display present,
        # in present order: (frame, input_us, sim_us, present_end_us).
        awaiting_oob: list[tuple[int, int, int, int]] = []
        for line in _follow(
            log_path,
            poll_interval_s=poll_interval_s,
            from_start=from_start,
            stop_event=stop_event,
            session_alive_fn=session_alive_fn,
            session_quiesced_fn=session_quiesced_fn,
        ):
            parsed = _parse_line_with_pid(line)
            if parsed is None:
                # Not one of ours: it is the game's own diagnostic output,
                # which belongs in the launcher's log.
                passthrough.forward(line)
                continue
            frame, marker, t_us, source_pid = parsed
            sample_pid = source_pid if source_pid is not None else pid
            if marker in NV_FRAMEGEN_MARKERS:
                if frame not in framegen_marker_frames:
                    framegen_order.append(frame)
                framegen_marker_frames[frame] = t_us
                framegen_active_until_us = max(
                    framegen_active_until_us, t_us + 3_000_000
                )
                while len(framegen_order) > _MAX_PENDING:
                    framegen_marker_frames.pop(framegen_order.pop(0), None)
                if marker == NV_MARKER_OUT_OF_BAND_PRESENT_END:
                    _resolve_oob_present(
                        sock,
                        targets,
                        awaiting_oob,
                        t_us,
                        sample_pid,
                        session_id,
                    )
                continue
            if marker == NV_MARKER_INPUT_SAMPLE:
                # Emitted by titles with full Reflex PCL markers, right before
                # SIMULATION_START. Anchors the true input-to-present lag.
                if frame not in pending_input:
                    input_order.append(frame)
                pending_input[frame] = t_us
                while len(input_order) > _MAX_PENDING:
                    pending_input.pop(input_order.pop(0), None)
            elif marker == NV_MARKER_SIMULATION_START:
                previous = last_simulation_start.get(sample_pid)
                if previous is not None:
                    previous_frame, previous_us = previous
                    if frame != previous_frame and t_us > previous_us:
                        base_sample = {
                            "v": 1,
                            "type": "timing",
                            "measurement": "base-frame-marker-pacing",
                            "source": "nvapi-marker-log",
                            "pid": sample_pid,
                            "base_frame_id": frame,
                            "quality": "nvapi-base-frame-marker",
                            "base_frame_frametime_us": t_us - previous_us,
                            "marker": NV_MARKER_SIMULATION_START,
                            "marker_name": "simulation-start",
                        }
                        if session_id:
                            base_sample["session_id"] = session_id
                        _send_sample(sock, targets, base_sample)
                # Advance the cadence baseline only forward: a late-flushed
                # older line must not rewind it and synthesize a multi-frame
                # frametime on the next genuine marker.
                if previous is None or (frame != previous[0] and t_us > previous[1]):
                    last_simulation_start[sample_pid] = (frame, t_us)
                # Pairing stays last-wins (like INPUT_SAMPLE above): a re-run
                # simulation tick re-anchors sim_to_present for that frame.
                if frame not in pending_sim:
                    order.append(frame)
                pending_sim[frame] = t_us
                while len(order) > _MAX_PENDING:
                    pending_sim.pop(order.pop(0), None)
            elif marker == NV_MARKER_PRESENT_END:
                sim_us = pending_sim.pop(frame, None)
                if sim_us is None or t_us <= sim_us:
                    pending_input.pop(frame, None)
                    continue
                input_us = pending_input.pop(frame, 0)
                span_us = t_us - sim_us
                framegen_marker_us = framegen_marker_frames.pop(frame, 0)
                # Recent out-of-band present activity means a displayed-present marker
                # is expected for this frame. It is NOT evidence of frame generation:
                # Reflex emits out-of-band presents with frame gen off, so the bridge
                # never asserts framegen_active. The receiver decides frame generation
                # from the displayed-vs-base cadence ratio instead.
                oob_present_recent = (
                    bool(framegen_marker_us) or t_us <= framegen_active_until_us
                )
                sample = {
                    "v": 1,
                    "type": "timing",
                    "measurement": "marker-proxy",
                    "source": "nvapi-marker-log",
                    "pid": sample_pid,
                    "present_id": frame,
                    "quality": "reflex-markers",
                    "marker_bits": 1,
                    "sim_to_present_us": span_us,
                    "framegen_active": False,
                }
                if session_id:
                    sample["session_id"] = session_id
                if input_us and t_us > input_us:
                    # Full Reflex input lag: input sample -> application present.
                    sample["input_to_present_us"] = t_us - input_us
                _send_sample(sock, targets, sample)
                if oob_present_recent:
                    # Wait for the displayed (out-of-band) present to emit the wider
                    # sim_to_oob_present_us span for the click-to-photon proxy.
                    awaiting_oob.append((frame, input_us, sim_us, t_us))
                    while len(awaiting_oob) > _MAX_PENDING:
                        awaiting_oob.pop(0)
    finally:
        sock.close()


def spawn_detached_drainer(
    env: dict[str, str],
    log_path: Path,
    *,
    session_pid: int | None = None,
) -> "subprocess.Popen | None":
    """Launch this module as a detached per-game FIFO drainer.

    The wrapper spawns this right before it execs into Proton, so the FIFO
    always has a reader for exactly as long as it can have writers -- with the
    app closed, samples are simply dropped at the (dead) latency socket instead
    of backing up in the pipe and freezing the game (see docs/nvapi-shim.md,
    "Known hazard"). Must be spawned *before* the wrapper redirects its stderr
    into the FIFO: the drainer inherits the console stderr, and must never hold
    the FIFO's write side (that would keep its own EOF from ever firing).
    Returns the Popen, or None when the spawn fails.
    """
    argv = [
        sys.executable,
        "-m",
        "overlay.telemetry.nvapi_marker_bridge",
        "--log",
        str(log_path),
        "--cleanup",
    ]
    if session_pid is not None:
        argv.append(f"--session-pid={session_pid}")
    try:
        return subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sys.stderr,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bridge dxvk-nvapi marker records into the latency socket."
        )
    )
    parser.add_argument("--log", type=Path, default=default_log_path())
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument(
        "--from-start",
        action="store_true",
        help="Process the whole log instead of only new lines.",
    )
    parser.add_argument(
        "--session-pid",
        type=int,
        default=None,
        help=(
            "Exit once this process is gone and the FIFO has no writers "
            "or once Steam's reaper has no non-PenguinBurner children "
            "(detached per-game drainer mode)."
        ),
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Unlink the FIFO after the watch ends.",
    )
    args = parser.parse_args(argv)
    session_alive_fn = None
    session_quiesced_fn = None
    session_fd = None
    if args.session_pid is not None:
        session_fd = _shim_deploy.open_session_fd(args.session_pid)
        liveness = _shim_deploy.SessionLiveness(args.session_pid, session_fd)
        session_alive_fn = liveness.session_alive
        session_quiesced_fn = liveness.steam_reaper_quiesced
    cleanup_identity = _fifo_identity(args.log) if args.cleanup else None
    installed_handlers: list[tuple[int, _SignalHandler]] = []
    try:
        # A launcher stopping the game kills this drainer along with the rest
        # of the session, so termination is the ordinary way it ends. Keep the
        # handlers scoped to the cleanup-owning invocation: main() is also
        # called in-process by tests and must not change its caller's signals.
        if args.cleanup:
            _install_termination_handlers(installed_handlers)
        run(
            args.log,
            poll_interval_s=args.poll_interval,
            from_start=args.from_start,
            session_alive_fn=session_alive_fn,
            session_quiesced_fn=session_quiesced_fn,
        )
    finally:
        cleanup_signal_mask = (
            _block_termination_signals() if installed_handlers else None
        )
        try:
            if session_fd is not None:
                try:
                    os.close(session_fd)
                except OSError:
                    pass
            # In the finally, not after the try: a drainer that is signalled or
            # raises would otherwise leave its FIFO behind, one per game
            # session. Only unlink the same FIFO this invocation started with;
            # a replaced path belongs to something else.
            if args.cleanup:
                unlink_fifo(
                    args.log, expected_identity=cleanup_identity
                )
        finally:
            _restore_signal_handlers(installed_handlers)
            _restore_signal_mask(cleanup_signal_mask)
    return 0


def _fifo_identity(path: Path) -> _FifoIdentity | None:
    """Identify a real FIFO at path without following a symlink."""
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISFIFO(info.st_mode):
        return None
    return info.st_dev, info.st_ino


def unlink_fifo(
    path: Path, *, expected_identity: _FifoIdentity | None
) -> bool:
    """Atomically capture and remove only the expected per-launch FIFO."""
    if expected_identity is None:
        try:
            path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    try:
        quarantine_dir = Path(
            tempfile.mkdtemp(
                prefix=".penguin-burner-fifo-cleanup-", dir=path.parent
            )
        )
    except OSError:
        return False
    quarantined = quarantine_dir / "entry"
    try:
        if not _quarantine_can_restore_entries(quarantine_dir):
            return False
        try:
            # rename() removes the public name and captures whatever occupied
            # it as one atomic directory operation. Validation and deletion
            # then happen only under our freshly created private directory, so
            # a replacement at the public path is never unlinked by cleanup.
            os.rename(path, quarantined)
        except FileNotFoundError:
            return True
        except OSError:
            return False

        if _fifo_identity(quarantined) != expected_identity:
            _restore_quarantined_entry(quarantined, path)
            return False
        try:
            quarantined.unlink()
        except OSError:
            _restore_quarantined_entry(quarantined, path)
            return False
        return True
    finally:
        try:
            quarantine_dir.rmdir()
        except OSError:
            # An entry that could not be restored is preserved here rather
            # than deleted. The random directory name makes it recoverable.
            pass


def _restore_quarantined_entry(quarantined: Path, destination: Path) -> bool:
    """Restore any entry without overwriting a new destination."""
    return _rename_noreplace(quarantined, destination)


def _rename_noreplace(source: Path, destination: Path) -> bool:
    """Atomically rename source only when destination does not exist."""
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
    except (AttributeError, OSError):
        return False
    return (
        renameat2(
            _AT_FDCWD,
            os.fsencode(source),
            _AT_FDCWD,
            os.fsencode(destination),
            _RENAME_NOREPLACE,
        )
        == 0
    )


def _quarantine_can_restore_entries(quarantine_dir: Path) -> bool:
    """Confirm this filesystem supports atomic no-overwrite restoration."""
    probe = quarantine_dir / "restore-probe"
    moved_probe = quarantine_dir / "restore-probe-moved"
    try:
        probe.mkdir()
    except OSError:
        return False
    restored_safely = _rename_noreplace(probe, moved_probe)
    for path in (probe, moved_probe):
        try:
            path.rmdir()
        except OSError:
            pass
    return restored_safely


def _raise_system_exit(
    signal_number: int, _frame: FrameType | None
) -> None:
    raise SystemExit(128 + int(signal_number))


def _install_termination_handlers(
    installed_handlers: list[tuple[int, _SignalHandler]],
) -> None:
    for number in _termination_signal_numbers():
        try:
            previous_handler = signal.getsignal(number)
        except (OSError, ValueError):
            # Not the main thread, or the platform refuses this signal: the
            # sweep on the next launch remains the backstop.
            continue
        if previous_handler is None:
            continue
        # Record first so a signal delivered immediately after signal.signal()
        # still unwinds through main's finally with enough state to restore it.
        installed_handlers.append((number, previous_handler))
        try:
            signal.signal(number, _raise_system_exit)
        except (OSError, ValueError):
            installed_handlers.pop()


def _restore_signal_handlers(
    installed_handlers: list[tuple[int, _SignalHandler]],
) -> None:
    for number, previous_handler in reversed(installed_handlers):
        try:
            signal.signal(number, previous_handler)
        except (OSError, ValueError):
            continue


def _termination_signal_numbers() -> tuple[int, ...]:
    return tuple(
        number
        for name in ("SIGTERM", "SIGINT", "SIGHUP")
        if (number := getattr(signal, name, None)) is not None
    )


def _block_termination_signals() -> _SignalMask | None:
    try:
        return signal.pthread_sigmask(
            signal.SIG_BLOCK, _termination_signal_numbers()
        )
    except (AttributeError, OSError, ValueError):
        return None


def _restore_signal_mask(previous_mask: _SignalMask | None) -> None:
    if previous_mask is None:
        return
    try:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    except (AttributeError, OSError, ValueError):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
