from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

from overlay.telemetry.nvapi_marker_bridge import (
    NV_MARKER_INPUT_SAMPLE,
    NV_MARKER_OUT_OF_BAND_PRESENT_END,
    NV_MARKER_SIMULATION_START,
    _parse_line_with_pid,
    run,
)


def test_parse_line_accepts_input_sample_marker() -> None:
    assert _parse_line_with_pid(
        "123.456:2a:0:latency-marker:pb:"
        "qpcUs=123456000 frameID=42 markerType=INPUT_SAMPLE"
    ) == (42, NV_MARKER_INPUT_SAMPLE, 123456000, 0x2A)


def test_parse_line_rejects_stock_dxvk_nvapi_trace_lines() -> None:
    # Stock DXVK_NVAPI_LOG_LEVEL=trace output is no longer a marker source;
    # only the shim's latency-marker wire format parses.
    assert _parse_line_with_pid(
        "123.456:trace:nvapi64:NvAPI_D3D_SetLatencyMarker "
        "({version=1,frameID=42,markerType=SIMULATION_START,rsvd})"
    ) is None


def test_parse_line_accepts_dxvk_nvapi_marker_only_log() -> None:
    assert _parse_line_with_pid(
        "123.456:1abc:2def:latency-marker:nvapi64:"
        "qpcUs=987654321 api=d3d frameID=42 markerType=SIMULATION_START "
        "markerValue=0"
    ) == (42, NV_MARKER_SIMULATION_START, 987654321, 0x1ABC)


def test_parse_line_accepts_dxvk_nvapi_marker_only_async_log() -> None:
    assert _parse_line_with_pid(
        "123.456:1abc:2def:latency-marker:nvapi64:"
        "qpcUs=987654321 api=d3d12_async frameID=42 "
        "markerType=OUT_OF_BAND_PRESENT_END markerValue=12 presentFrameID=77"
    ) == (42, NV_MARKER_OUT_OF_BAND_PRESENT_END, 987654321, 0x1ABC)


def test_bridge_uses_marker_only_log_process_id(monkeypatch, tmp_path) -> None:
    import overlay.telemetry.nvapi_marker_bridge as bridge

    lines = iter(
        (
            "123.456:0000002a:2def:latency-marker:nvapi64:"
            "qpcUs=1000000 api=d3d frameID=7 markerType=SIMULATION_START "
            "markerValue=0",
            "123.466:0000002a:2def:latency-marker:nvapi64:"
            "qpcUs=1010000 api=d3d frameID=7 markerType=PRESENT_END "
            "markerValue=5",
        )
    )
    samples = []

    monkeypatch.setattr(
        bridge,
        "_follow",
        lambda _path, poll_interval_s, from_start, stop_event=None,
        session_alive_fn=None, session_quiesced_fn=None: lines,
    )
    monkeypatch.setattr(
        bridge,
        "_send_sample",
        lambda _sock, _targets, sample: samples.append(sample),
    )

    run(tmp_path / "trace.fifo", env={}, pid=999)

    assert len(samples) == 1
    assert samples[0]["pid"] == 42
    assert samples[0]["sim_to_present_us"] == 10000


def test_bridge_emits_unique_simulation_start_cadence_with_session(
    monkeypatch, tmp_path
) -> None:
    import overlay.telemetry.nvapi_marker_bridge as bridge

    lines = iter(
        (
            "1.000:0000002a:0:latency-marker:pb:qpcUs=1000000 frameID=7 "
            "markerType=SIMULATION_START",
            "1.001:0000002a:0:latency-marker:pb:qpcUs=1001000 frameID=7 "
            "markerType=SIMULATION_START",
            "1.020:0000002a:0:latency-marker:pb:qpcUs=1020000 frameID=8 "
            "markerType=SIMULATION_START",
            "1.040:0000002a:0:latency-marker:pb:qpcUs=1040000 frameID=9 "
            "markerType=SIMULATION_START",
        )
    )
    samples = []
    monkeypatch.setattr(
        bridge,
        "_follow",
        lambda _path, poll_interval_s, from_start, stop_event=None,
        session_alive_fn=None, session_quiesced_fn=None: lines,
    )
    monkeypatch.setattr(
        bridge,
        "_send_sample",
        lambda _sock, _targets, sample: samples.append(sample),
    )

    run(
        tmp_path / "trace.fifo",
        env={bridge.TELEMETRY_SESSION_ENV: "700"},
        pid=999,
    )

    assert [s["base_frame_id"] for s in samples] == [8, 9]
    assert [s["base_frame_frametime_us"] for s in samples] == [20000, 20000]
    assert all(s["pid"] == 42 for s in samples)
    assert all(s["session_id"] == "700" for s in samples)
    assert all(s["source"] == "nvapi-marker-log" for s in samples)


def test_bridge_does_not_mark_framegen_from_oob_present_markers(monkeypatch, tmp_path) -> None:
    # Reflex out-of-band present markers are emitted with frame generation OFF, so
    # the bridge must never assert frame generation from them -- it emits the span
    # only and leaves the on/off decision to the receiver's cadence check.
    import overlay.telemetry.nvapi_marker_bridge as bridge

    lines = iter(
        (
            "1.000:2a:0:latency-marker:pb:qpcUs=1000000 frameID=7 "
            "markerType=SIMULATION_START",
            "1.005:2a:0:latency-marker:pb:qpcUs=1005000 frameID=7 "
            "markerType=OUT_OF_BAND_PRESENT_END",
            "1.010:2a:0:latency-marker:pb:qpcUs=1010000 frameID=7 "
            "markerType=PRESENT_END",
        )
    )
    samples = []

    monkeypatch.setattr(
        bridge,
        "_follow",
        lambda _path, poll_interval_s, from_start, stop_event=None,
        session_alive_fn=None, session_quiesced_fn=None: lines,
    )
    monkeypatch.setattr(
        bridge,
        "_send_sample",
        lambda _sock, _targets, sample: samples.append(sample),
    )

    run(tmp_path / "trace.fifo", env={}, pid=123)

    assert len(samples) == 1
    assert samples[0]["sim_to_present_us"] == 10000
    assert samples[0]["framegen_active"] is False
    assert "framegen_frame_count" not in samples[0]


def test_bridge_emits_oob_present_span_in_present_order(monkeypatch, tmp_path) -> None:
    # Realistic frame-gen order: the application PRESENT_END precedes the
    # out-of-band display present that shows the frame. A prior out-of-band present
    # primes the expectation so the base frame is paired with its later display
    # present, yielding the wider sim_to_oob_present_us span (still not flagged as
    # frame generation -- the receiver decides that from cadence).
    import overlay.telemetry.nvapi_marker_bridge as bridge

    lines = iter(
        (
            "0.950:2a:0:latency-marker:pb:qpcUs=950000 frameID=6 "
            "markerType=OUT_OF_BAND_PRESENT_END",
            "1.000:2a:0:latency-marker:pb:qpcUs=1000000 frameID=7 "
            "markerType=SIMULATION_START",
            "1.010:2a:0:latency-marker:pb:qpcUs=1010000 frameID=7 "
            "markerType=PRESENT_END",
            "1.060:2a:0:latency-marker:pb:qpcUs=1060000 frameID=7 "
            "markerType=OUT_OF_BAND_PRESENT_END",
        )
    )
    samples = []

    monkeypatch.setattr(
        bridge,
        "_follow",
        lambda _path, poll_interval_s, from_start, stop_event=None,
        session_alive_fn=None, session_quiesced_fn=None: lines,
    )
    monkeypatch.setattr(
        bridge,
        "_send_sample",
        lambda _sock, _targets, sample: samples.append(sample),
    )

    run(tmp_path / "trace.fifo", env={}, pid=123)

    oob_samples = [s for s in samples if "sim_to_oob_present_us" in s]
    assert len(oob_samples) == 1
    assert oob_samples[0]["present_id"] == 7
    assert oob_samples[0]["sim_to_oob_present_us"] == 60000
    assert oob_samples[0]["sim_to_present_us"] == 10000
    assert oob_samples[0]["framegen_active"] is False


def test_repeated_simulation_start_reanchors_pairing_without_rewinding_cadence(
    monkeypatch, tmp_path
) -> None:
    # A re-run simulation tick for the SAME frame re-anchors sim_to_present
    # (last-wins, like INPUT_SAMPLE), and a late-flushed OLDER line must not
    # rewind the cadence baseline into a synthetic multi-frame frametime.
    import overlay.telemetry.nvapi_marker_bridge as bridge

    lines = iter(
        (
            "1.000:2a:0:latency-marker:pb:qpcUs=1000000 frameID=7 "
            "markerType=SIMULATION_START",
            # Same frame re-emitted 5ms later: pairing anchor moves forward.
            "1.005:2a:0:latency-marker:pb:qpcUs=1005000 frameID=7 "
            "markerType=SIMULATION_START",
            # Late-flushed old line from an earlier frame: ignored as baseline.
            "1.006:2a:0:latency-marker:pb:qpcUs=900000 frameID=6 "
            "markerType=SIMULATION_START",
            "1.020:2a:0:latency-marker:pb:qpcUs=1020000 frameID=7 "
            "markerType=PRESENT_END",
        )
    )
    samples = []

    monkeypatch.setattr(
        bridge,
        "_follow",
        lambda _path, poll_interval_s, from_start, stop_event=None,
        session_alive_fn=None, session_quiesced_fn=None: lines,
    )
    monkeypatch.setattr(
        bridge,
        "_send_sample",
        lambda _sock, _targets, sample: samples.append(sample),
    )

    run(tmp_path / "trace.fifo", env={}, pid=123)

    timing = [s for s in samples if "sim_to_present_us" in s]
    assert len(timing) == 1
    # Anchored at the re-emitted 1005000, not the first 1000000.
    assert timing[0]["sim_to_present_us"] == 15000


def test_bridge_reports_input_to_present_when_input_marker_present(monkeypatch, tmp_path) -> None:
    # Title with full Reflex PCL markers (e.g. Quake II RTX): INPUT_SAMPLE pairs
    # with PRESENT_END to give the true input-to-present Reflex lag.
    import overlay.telemetry.nvapi_marker_bridge as bridge

    lines = iter(
        (
            "1.000:2a:0:latency-marker:pb:qpcUs=1000000 frameID=7 "
            "markerType=INPUT_SAMPLE",
            "1.002:2a:0:latency-marker:pb:qpcUs=1002000 frameID=7 "
            "markerType=SIMULATION_START",
            "1.030:2a:0:latency-marker:pb:qpcUs=1030000 frameID=7 "
            "markerType=PRESENT_END",
        )
    )
    samples = []

    monkeypatch.setattr(
        bridge,
        "_follow",
        lambda _path, poll_interval_s, from_start, stop_event=None,
        session_alive_fn=None, session_quiesced_fn=None: lines,
    )
    monkeypatch.setattr(
        bridge,
        "_send_sample",
        lambda _sock, _targets, sample: samples.append(sample),
    )

    run(tmp_path / "trace.fifo", env={}, pid=123)

    assert len(samples) == 1
    # input -> present (30ms) is wider than sim -> present (28ms): the input lag.
    assert samples[0]["input_to_present_us"] == 30000
    assert samples[0]["sim_to_present_us"] == 28000


def test_drainer_exits_when_session_dead_and_no_writers(tmp_path, monkeypatch) -> None:
    """Detached-drainer lifetime: the launching session died before any writer
    ever connected (e.g. failed exec); after the no-writer grace, run() returns
    instead of tailing a pipe that can never fill."""
    import os

    import overlay.telemetry.nvapi_marker_bridge as bridge

    monkeypatch.setattr(bridge, "_NO_WRITER_GRACE_S", 0.05)
    fifo = tmp_path / "nvapi-trace.1.fifo"
    os.mkfifo(fifo)

    bridge.run(fifo, poll_interval_s=0.01, session_alive_fn=lambda: False)
    # Returned promptly (a hang here would fail the test by timeout).


def test_drainer_keeps_draining_while_writer_exists(tmp_path) -> None:
    """A dead session does not stop the drain while any writer holds the FIFO
    (a wrapped game can outlive the wrapper session that launched it)."""
    import os
    import threading
    import time

    import overlay.telemetry.nvapi_marker_bridge as bridge

    fifo = tmp_path / "nvapi-trace.1.fifo"
    os.mkfifo(fifo)
    samples = []

    def send(_sock, _targets, sample):
        samples.append(sample)

    thread = threading.Thread(
        target=lambda: bridge.run(
            fifo,
            poll_interval_s=0.01,
            session_alive_fn=lambda: False,  # session died immediately
            env={},
        ),
        daemon=True,
    )
    import unittest.mock

    with unittest.mock.patch.object(bridge, "_send_sample", send):
        writer = os.open(fifo, os.O_RDWR)  # the surviving game's stderr fd
        thread.start()
        os.write(
            writer,
            b"123.456:2a:0:latency-marker:pb:qpcUs=1000000 frameID=7 "
            b"markerType=SIMULATION_START\n"
            b"123.466:2a:0:latency-marker:pb:qpcUs=1010000 frameID=7 "
            b"markerType=PRESENT_END\n",
        )
        deadline = time.monotonic() + 2.0
        while not samples and time.monotonic() < deadline:
            time.sleep(0.01)
        assert samples, "markers must be drained while the game lives"
        assert samples[0]["sim_to_present_us"] == 10000
        os.close(writer)  # last writer gone -> EOF -> session check -> exit
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_drainer_exits_when_steam_reaper_quiesces_after_traffic(tmp_path) -> None:
    """Steam's reaper keeps the FIFO write side open after the game is gone; the
    drainer must still exit once the reaper has only PB helper children left."""
    import os
    import threading
    import time

    import overlay.telemetry.nvapi_marker_bridge as bridge

    fifo = tmp_path / "nvapi-trace.1.fifo"
    os.mkfifo(fifo)

    quiesced = threading.Event()
    thread = threading.Thread(
        target=lambda: bridge.run(
            fifo,
            poll_interval_s=0.01,
            session_alive_fn=lambda: True,
            session_quiesced_fn=lambda: quiesced.is_set(),
            env={},
        ),
        daemon=True,
    )
    writer = os.open(fifo, os.O_RDWR)  # Steam reaper's inherited stderr fd
    try:
        thread.start()
        os.write(
            writer,
            b"123.456:2a:0:latency-marker:pb:qpcUs=1000000 frameID=7 "
            b"markerType=SIMULATION_START\n",
        )
        time.sleep(0.05)
        quiesced.set()
        thread.join(timeout=2.0)
        assert not thread.is_alive()
    finally:
        os.close(writer)


def test_drainer_main_cleans_up_fifo(tmp_path, monkeypatch) -> None:
    """--cleanup unlinks the per-launch FIFO once the watch ends."""
    import os

    import overlay.telemetry.nvapi_marker_bridge as bridge

    monkeypatch.setattr(bridge, "_NO_WRITER_GRACE_S", 0.05)
    fifo = tmp_path / "nvapi-trace.9.fifo"
    os.mkfifo(fifo)
    # A pid that cannot exist: the session is dead from the first check, and
    # no writer ever connects, so the watch ends after the grace.
    rc = bridge.main(
        ["--log", str(fifo), "--session-pid", "2147483646", "--cleanup",
         "--poll-interval", "0.01"]
    )
    assert rc == 0
    assert not fifo.exists()


def test_spawn_detached_drainer_argv(monkeypatch, tmp_path) -> None:
    import sys

    import overlay.telemetry.nvapi_marker_bridge as bridge

    captured = {}

    class FakePopen:
        def __init__(self, argv, **kwargs) -> None:
            captured["argv"] = argv
            captured["kwargs"] = kwargs

    monkeypatch.setattr(bridge.subprocess, "Popen", FakePopen)
    fifo = tmp_path / "nvapi-trace.5.fifo"
    env = {"HOME": str(tmp_path)}

    assert bridge.spawn_detached_drainer(env, fifo, session_pid=41) is not None
    assert captured["argv"] == [
        sys.executable,
        "-m",
        "overlay.telemetry.nvapi_marker_bridge",
        "--log",
        str(fifo),
        "--cleanup",
        "--session-pid=41",
    ]
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["env"] is env


# -- the drainer owns its FIFO -------------------------------------------------


def test_the_drainer_unlinks_its_fifo_when_signalled(tmp_path) -> None:
    """A launcher stopping the game kills the drainer, so termination is the
    ordinary way it ends. Leaving cleanup outside the finally left one stale
    FIFO in ~/.cache per game session."""
    import errno
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path as _Path

    fifo = tmp_path / "nvapi-trace.test.fifo"
    os.mkfifo(fifo, 0o600)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_Path(__file__).resolve().parents[1])

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "overlay.telemetry.nvapi_marker_bridge",
            "--log",
            str(fifo),
            "--cleanup",
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    writer_fd = None
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and process.poll() is None:
            try:
                writer_fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as error:
                if error.errno != errno.ENXIO:
                    raise
                time.sleep(0.01)
                continue
            break
        assert writer_fd is not None, "drainer never opened the FIFO"
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)
    finally:
        if writer_fd is not None:
            os.close(writer_fd)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    assert process.returncode == 128 + signal.SIGTERM
    assert not fifo.exists()


def test_drainer_main_restores_termination_handlers(tmp_path, monkeypatch) -> None:
    import os
    import signal

    import pytest

    import overlay.telemetry.nvapi_marker_bridge as bridge

    fifo = tmp_path / "nvapi-trace.handlers.fifo"
    os.mkfifo(fifo, 0o600)

    def fail_run(*_args, **_kwargs) -> None:
        raise RuntimeError("watch failed")

    monkeypatch.setattr(bridge, "run", fail_run)

    signal_numbers = [
        number
        for name in ("SIGTERM", "SIGINT", "SIGHUP")
        if (number := getattr(signal, name, None)) is not None
    ]
    previous_handlers = {
        number: signal.getsignal(number) for number in signal_numbers
    }

    def sentinel_handler(_signal_number, _frame) -> None:
        return None

    try:
        for number in signal_numbers:
            signal.signal(number, sentinel_handler)

        with pytest.raises(RuntimeError, match="watch failed"):
            bridge.main(["--log", str(fifo), "--cleanup"])
        assert all(
            signal.getsignal(number) is sentinel_handler
            for number in signal_numbers
        )
    finally:
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)

    assert not fifo.exists()


def test_drainer_main_without_cleanup_does_not_install_handlers(
    tmp_path, monkeypatch
) -> None:
    import pytest

    import overlay.telemetry.nvapi_marker_bridge as bridge

    monkeypatch.setattr(bridge, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bridge,
        "_install_termination_handlers",
        lambda _installed_handlers: pytest.fail(
            "non-cleanup invocation changed signal handlers"
        ),
    )

    assert bridge.main(["--log", str(tmp_path / "markers.log")]) == 0


def test_drainer_main_cleans_up_when_handler_installation_exits(
    tmp_path, monkeypatch
) -> None:
    import os

    import pytest

    import overlay.telemetry.nvapi_marker_bridge as bridge

    fifo = tmp_path / "nvapi-trace.install-signal.fifo"
    os.mkfifo(fifo, 0o600)

    def exit_during_install(_installed_handlers) -> None:
        raise SystemExit(143)

    monkeypatch.setattr(
        bridge, "_install_termination_handlers", exit_during_install
    )

    with pytest.raises(SystemExit, match="143"):
        bridge.main(["--log", str(fifo), "--cleanup"])

    assert not fifo.exists()


# The scenario below asserts on process-global signal delivery: a SIGTERM
# raised at the test process has to reach the handler that was installed before
# cleanup started. That is not a property a single interpreter can hold on
# behalf of a whole suite -- anything else already loaded may have touched
# termination handling at the C level, where Python's `signal` module cannot
# see it and pytest cannot undo it. A live QApplication is enough to reproduce
# it, and Qt is loaded by every GUI test in this suite.
#
# So the scenario runs in a fresh interpreter. The child does exactly what the
# in-process version did and prints its findings; the parent asserts on those.
_DEFERRED_SIGNAL_CHILD = """
import os, signal, sys
from pathlib import Path

import overlay.telemetry.nvapi_marker_bridge as bridge

fifo = Path(sys.argv[1])
os.mkfifo(fifo, 0o600)
delivered = []
real_rename = os.rename


def previous_signal_handler(signal_number, _frame):
    delivered.append(signal_number)


def exit_run(*_args, **_kwargs):
    raise SystemExit(143)


def signal_after_capture(source, destination):
    real_rename(source, destination)
    if Path(source) == fifo:
        os.kill(os.getpid(), signal.SIGTERM)


signal.signal(signal.SIGTERM, previous_signal_handler)
bridge.run = exit_run
bridge.os.rename = signal_after_capture

exit_code = None
try:
    bridge.main(["--log", str(fifo), "--cleanup"])
except SystemExit as exc:
    exit_code = exc.code
finally:
    bridge.os.rename = real_rename

leftovers = sorted(p.name for p in fifo.parent.glob(".penguin-burner-fifo-cleanup-*"))
print(f"exit={exit_code}")
print(f"delivered={delivered}")
print(f"fifo_exists={fifo.exists()}")
print(f"leftovers={leftovers}")
"""


def test_drainer_cleanup_defers_a_repeated_termination_signal(tmp_path) -> None:
    """A signal arriving mid-cleanup waits for it, then reaches the old handler."""
    completed = subprocess.run(
        [sys.executable, "-c", _DEFERRED_SIGNAL_CHILD, str(tmp_path / "trace.fifo")],
        capture_output=True,
        text=True,
        timeout=60,
        # The child's own exit code is part of what this asserts, so a raising
        # run() would hide the interesting failure behind a CalledProcessError.
        check=False,
        cwd=Path(__file__).resolve().parent.parent,
    )

    assert completed.returncode == 0, completed.stderr
    report = dict(
        line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line
    )
    # The drainer's own exit survives: deferring a signal must not swallow it.
    assert report["exit"] == "143"
    # Deferred, not dropped, and handed to the handler that was there before.
    assert report["delivered"] == f"[{int(signal.SIGTERM)}]"
    assert report["fifo_exists"] == "False"
    assert report["leftovers"] == "[]"


def test_unlink_fifo_leaves_a_regular_file_alone(tmp_path) -> None:
    """--log may point at a real log file; cleanup must not delete data."""
    from overlay.telemetry.nvapi_marker_bridge import unlink_fifo

    regular = tmp_path / "not-a-fifo.log"
    regular.write_text("markers", encoding="utf-8")

    assert unlink_fifo(regular, expected_identity=None) is False
    assert regular.exists()


def test_unlink_fifo_leaves_a_symlink_to_a_fifo_alone(tmp_path) -> None:
    import os

    from overlay.telemetry.nvapi_marker_bridge import (
        _fifo_identity,
        unlink_fifo,
    )

    target = tmp_path / "target.fifo"
    link = tmp_path / "marker.fifo"
    os.mkfifo(target, 0o600)
    link.symlink_to(target)

    assert _fifo_identity(link) is None
    assert unlink_fifo(link, expected_identity=None) is False
    assert link.is_symlink()
    assert target.exists()


def test_unlink_fifo_refuses_a_replacement_fifo(tmp_path) -> None:
    import os

    from overlay.telemetry.nvapi_marker_bridge import (
        _fifo_identity,
        unlink_fifo,
    )

    fifo = tmp_path / "marker.fifo"
    replacement = tmp_path / "replacement.fifo"
    os.mkfifo(fifo, 0o600)
    expected_identity = _fifo_identity(fifo)
    os.mkfifo(replacement, 0o600)
    os.replace(replacement, fifo)

    assert expected_identity is not None
    assert unlink_fifo(fifo, expected_identity=expected_identity) is False
    assert fifo.exists()


def test_unlink_fifo_does_not_capture_without_atomic_restore_support(
    tmp_path, monkeypatch
) -> None:
    import os

    import overlay.telemetry.nvapi_marker_bridge as bridge

    fifo = tmp_path / "marker.fifo"
    os.mkfifo(fifo, 0o600)
    expected_identity = bridge._fifo_identity(fifo)
    monkeypatch.setattr(
        bridge, "_quarantine_can_restore_entries", lambda _path: False
    )

    assert expected_identity is not None
    assert (
        bridge.unlink_fifo(fifo, expected_identity=expected_identity)
        is False
    )
    assert bridge._fifo_identity(fifo) == expected_identity
    assert not list(tmp_path.glob(".penguin-burner-fifo-cleanup-*"))


def test_unlink_fifo_restores_a_replacement_directory(tmp_path) -> None:
    import os

    from overlay.telemetry.nvapi_marker_bridge import (
        _fifo_identity,
        unlink_fifo,
    )

    fifo = tmp_path / "marker.fifo"
    os.mkfifo(fifo, 0o600)
    expected_identity = _fifo_identity(fifo)
    fifo.unlink()
    fifo.mkdir()
    payload = fifo / "keep.txt"
    payload.write_text("user data", encoding="utf-8")

    assert expected_identity is not None
    assert unlink_fifo(fifo, expected_identity=expected_identity) is False
    assert fifo.is_dir()
    assert payload.read_text(encoding="utf-8") == "user data"
    assert not list(tmp_path.glob(".penguin-burner-fifo-cleanup-*"))


def test_unlink_fifo_restores_a_replacement_that_wins_before_capture(
    tmp_path, monkeypatch
) -> None:
    import os
    from pathlib import Path

    import overlay.telemetry.nvapi_marker_bridge as bridge

    fifo = tmp_path / "marker.fifo"
    os.mkfifo(fifo, 0o600)
    expected_identity = bridge._fifo_identity(fifo)
    real_rename = os.rename

    def replace_then_rename(source, destination) -> None:
        source_path = Path(source)
        if source_path == fifo:
            source_path.unlink()
            source_path.write_text("replacement", encoding="utf-8")
        real_rename(source, destination)

    monkeypatch.setattr(bridge.os, "rename", replace_then_rename)

    assert expected_identity is not None
    assert (
        bridge.unlink_fifo(fifo, expected_identity=expected_identity)
        is False
    )
    assert fifo.read_text(encoding="utf-8") == "replacement"
    assert not list(tmp_path.glob(".penguin-burner-fifo-cleanup-*"))


def test_unlink_fifo_preserves_a_replacement_created_after_capture(
    tmp_path, monkeypatch
) -> None:
    import os
    from pathlib import Path

    import overlay.telemetry.nvapi_marker_bridge as bridge

    fifo = tmp_path / "marker.fifo"
    os.mkfifo(fifo, 0o600)
    expected_identity = bridge._fifo_identity(fifo)
    real_rename = os.rename

    def rename_then_replace(source, destination) -> None:
        real_rename(source, destination)
        if Path(source) == fifo:
            fifo.write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(bridge.os, "rename", rename_then_replace)

    assert expected_identity is not None
    assert bridge.unlink_fifo(fifo, expected_identity=expected_identity) is True
    assert fifo.read_text(encoding="utf-8") == "replacement"
    assert not list(tmp_path.glob(".penguin-burner-fifo-cleanup-*"))


def test_unlink_fifo_removes_the_matching_fifo(tmp_path) -> None:
    import os

    from overlay.telemetry.nvapi_marker_bridge import (
        _fifo_identity,
        unlink_fifo,
    )

    fifo = tmp_path / "marker.fifo"
    os.mkfifo(fifo, 0o600)
    expected_identity = _fifo_identity(fifo)

    assert expected_identity is not None
    assert unlink_fifo(fifo, expected_identity=expected_identity) is True
    assert not fifo.exists()


def test_unlink_fifo_is_idempotent(tmp_path) -> None:
    from overlay.telemetry.nvapi_marker_bridge import unlink_fifo

    assert unlink_fifo(
        tmp_path / "gone.fifo", expected_identity=(123, 456)
    ) is True


class _FakeStream:
    """Stands in for the launcher's stderr."""

    def __init__(self, *, fail: bool = False) -> None:
        self.written: list[str] = []
        self.flushes = 0
        self._fail = fail

    def write(self, text: str) -> int:
        if self._fail:
            raise OSError("launcher pipe is gone")
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        self.flushes += 1


def _run_bridge_over(monkeypatch, tmp_path, lines, passthrough):
    import overlay.telemetry.nvapi_marker_bridge as bridge

    samples: list[dict] = []
    monkeypatch.setattr(
        bridge,
        "_follow",
        lambda _path, poll_interval_s, from_start, stop_event=None,
        session_alive_fn=None, session_quiesced_fn=None: iter(lines),
    )
    monkeypatch.setattr(
        bridge,
        "_send_sample",
        lambda _sock, _targets, sample: samples.append(sample),
    )
    run(tmp_path / "trace.fifo", env={}, pid=999, passthrough=passthrough)
    return samples


_SIM_LINE = (
    "123.456:0000002a:2def:latency-marker:nvapi64:"
    "qpcUs=1000000 api=d3d frameID=7 markerType=SIMULATION_START markerValue=0"
)
_PRESENT_LINE = (
    "123.466:0000002a:2def:latency-marker:nvapi64:"
    "qpcUs=1010000 api=d3d frameID=7 markerType=PRESENT_END markerValue=5"
)


def test_bridge_does_not_echo_its_own_marker_lines(monkeypatch, tmp_path) -> None:
    """Markers exist only because we asked for them; echoing them adds noise."""
    from overlay.telemetry.nvapi_marker_bridge import _GameOutputPassthrough

    stream = _FakeStream()
    _run_bridge_over(
        monkeypatch, tmp_path, [_SIM_LINE, _PRESENT_LINE], _GameOutputPassthrough(stream)
    )

    assert stream.written == []


def test_bridge_gives_game_output_back_to_the_launcher(monkeypatch, tmp_path) -> None:
    """The wrapper steals the game's stderr; the drainer must hand it back.

    Without this the whole Proton/wine log vanishes from Lutris, Steam, or a
    terminal for as long as the wrapper is in the launch command.
    """
    from overlay.telemetry.nvapi_marker_bridge import _GameOutputPassthrough

    stream = _FakeStream()
    lines = [
        "wine: ordinal not found\n",
        _SIM_LINE,
        "err:module:import_dll Library missing\n",
        _PRESENT_LINE,
    ]

    samples = _run_bridge_over(
        monkeypatch, tmp_path, lines, _GameOutputPassthrough(stream)
    )

    assert stream.written == [
        "wine: ordinal not found\n",
        "err:module:import_dll Library missing\n",
    ]
    # Still bridged the markers it was there to bridge.
    assert len(samples) == 1


def test_passthrough_keeps_lines_line_terminated() -> None:
    from overlay.telemetry.nvapi_marker_bridge import _GameOutputPassthrough

    stream = _FakeStream()
    passthrough = _GameOutputPassthrough(stream)

    passthrough.forward("has newline\n")
    passthrough.forward("no newline")

    assert stream.written == ["has newline\n", "no newline\n"]
    assert stream.flushes == 2


def test_passthrough_latches_off_when_the_launcher_pipe_dies() -> None:
    from overlay.telemetry.nvapi_marker_bridge import _GameOutputPassthrough

    stream = _FakeStream(fail=True)
    passthrough = _GameOutputPassthrough(stream)

    passthrough.forward("anything\n")
    assert passthrough.failed is True

    # A dead launcher must not keep costing a failed write per drained line;
    # draining itself has to continue, or a full pipe freezes the game.
    stream._fail = False
    passthrough.forward("later\n")
    assert stream.written == []


def _drain_fifo(fifo, *, session_alive, timeout_s=5.0):
    """Run _follow_fifo in a thread and report whether it returned."""
    import threading

    from overlay.telemetry.nvapi_marker_bridge import _follow_fifo

    lines: list[str] = []
    finished = threading.Event()

    def run():
        # Consumed one at a time on purpose: the tests assert on what has
        # arrived while the generator is still running, so it must not be
        # drained in one go.
        lines.extend(
            _follow_fifo(fifo, poll_interval_s=0.02, session_alive_fn=session_alive)
        )
        finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return lines, finished, thread


def test_drainer_exits_once_writers_closed_and_the_session_is_gone(tmp_path) -> None:
    """It holds the launcher's output pipe, which Lutris waits on for EOF.

    Reopening after the game's EOF used to leave it spinning forever: with
    traffic already seen, the quiet path never re-checked whether the session
    had ended, so the launcher showed the game as still running until stopped
    by hand.
    """
    import os
    import time

    fifo = tmp_path / "trace.fifo"
    os.mkfifo(fifo, 0o600)
    alive = {"value": True}

    lines, finished, thread = _drain_fifo(fifo, session_alive=lambda: alive["value"])

    # A writer connects, emits, then closes -- the game running and exiting.
    writer = os.open(fifo, os.O_WRONLY)
    os.write(writer, b"err:module:import_dll something\n")
    time.sleep(0.2)
    os.close(writer)
    # The session is still alive for a moment after the writer goes, which is
    # what pushed the drainer into the reopen path.
    time.sleep(0.2)
    alive["value"] = False

    assert finished.wait(timeout=5.0), "the drainer must exit once nothing can write"
    thread.join(timeout=1.0)
    assert lines == ["err:module:import_dll something\n"]


def test_drainer_keeps_draining_while_a_writer_is_merely_idle(tmp_path) -> None:
    """A wrapped game can outlive the session that launched it."""
    import os
    import time

    fifo = tmp_path / "trace.fifo"
    os.mkfifo(fifo, 0o600)

    lines, finished, thread = _drain_fifo(fifo, session_alive=lambda: False)

    writer = os.open(fifo, os.O_WRONLY)
    try:
        os.write(writer, b"first\n")
        time.sleep(0.4)
        # Session already reported gone, writer still open and quiet: the
        # drainer must stay for the game that is still holding the pipe.
        assert not finished.is_set()
        os.write(writer, b"second\n")
        time.sleep(0.2)
        assert lines == ["first\n", "second\n"]
    finally:
        os.close(writer)

    assert finished.wait(timeout=5.0)
    thread.join(timeout=1.0)


def test_drainer_keeps_a_reconnected_writer_after_the_session_ends(tmp_path) -> None:
    """An earlier EOF must not make a later live writer look closed."""
    import os
    import time

    fifo = tmp_path / "trace.fifo"
    os.mkfifo(fifo, 0o600)
    alive = {"value": True}
    lines, finished, thread = _drain_fifo(
        fifo, session_alive=lambda: alive["value"]
    )

    first = os.open(fifo, os.O_WRONLY)
    os.write(first, b"first\n")
    os.close(first)
    deadline = time.monotonic() + 2.0
    while lines != ["first\n"] and time.monotonic() < deadline:
        time.sleep(0.01)
    # Give the reader time to observe EOF and reopen before the next writer.
    time.sleep(0.1)

    second = os.open(fifo, os.O_WRONLY)
    try:
        os.write(second, b"second\n")
        deadline = time.monotonic() + 2.0
        while lines != ["first\n", "second\n"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lines == ["first\n", "second\n"]

        alive["value"] = False
        time.sleep(0.1)
        assert not finished.is_set(), "the reconnected writer is still open"
    finally:
        os.close(second)

    assert finished.wait(timeout=5.0)
    thread.join(timeout=1.0)
