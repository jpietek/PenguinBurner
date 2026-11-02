"""Fast original-marker transport on the shared monotonic clock."""
import os
import shutil
import struct
import subprocess
import time
from pathlib import Path

import pytest

from overlay.telemetry.base_timing import BaseFramePublisher, monotonic_from_qpc


def test_qpc_maps_from_either_wine_clock_base():
    now = 10_000_000_000
    # Upstream Wine: QPC is CLOCK_MONOTONIC; RAW runs 360 ms ahead (offset negative).
    assert monotonic_from_qpc(now - 3_000, now, -360_000) == now - 3_000
    # Proton: QPC is CLOCK_MONOTONIC_RAW, 360 ms ahead of CLOCK_MONOTONIC.
    assert monotonic_from_qpc(now + 360_000 - 3_000, now, -360_000) == now - 3_000
    # RAW behind MONOTONIC (offset positive): the stale-looking monotonic
    # candidate loses to the one just behind now, and a future one is rejected.
    assert monotonic_from_qpc(now - 50_000 - 3_000, now, 50_000) == now - 3_000
    assert monotonic_from_qpc(now - 3_000, now, 50_000) == now - 3_000
    # Neither base fits: stale FIFO, far future, or zero.
    assert monotonic_from_qpc(now - 3_000_000, now, 0) is None
    assert monotonic_from_qpc(now + 50_000, now, 0) is None
    assert monotonic_from_qpc(0, now, 0) is None


def test_publish_drops_samples_off_the_host_monotonic_clock(tmp_path, monkeypatch):
    # A sample must map just behind now from one of the two Wine clock bases.
    # Anything else (a stale FIFO, a far-future stamp) is dropped before the
    # native publisher is even opened.
    calls: list[tuple[int, int, int]] = []
    publisher = BaseFramePublisher(tmp_path / "latency.sock", "700")
    monkeypatch.setattr(publisher, "_open", lambda: True)
    monkeypatch.setattr(publisher, "library", type("Library", (), {
        "pb_base_timing_publish": staticmethod(lambda _p, f, t, pid: calls.append((f, t, pid)))})())
    now = time.monotonic_ns() // 1000
    raw_to_monotonic = now - time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW) // 1000
    publisher.publish(1, now - 3_000_000, 42)  # Older than the 2 s window on both bases.
    publisher.publish(2, now + 5_000_000, 42)  # 5 s in the future on both bases.
    publisher.publish(3, 0, 42)
    publisher.publish(4, now - 100_000, 42)  # CLOCK_MONOTONIC base.
    publisher.publish(5, now - 100_000 - raw_to_monotonic, 42)  # CLOCK_MONOTONIC_RAW base.
    assert [call[0] for call in calls] == [4, 5]
    assert all(abs(call[1] - (now - 100_000)) <= 2_000 for call in calls)
    assert calls[0][2] == 42


@pytest.fixture(scope="module")
def timing_library(tmp_path_factory):
    if not shutil.which("cmake") or not shutil.which("c++"):
        pytest.skip("native build toolchain unavailable")
    root = Path(__file__).resolve().parents[1]
    build = tmp_path_factory.mktemp("base-timing-library")
    subprocess.run(["cmake", "-S", str(root / "overlay/native/latency_layer"), "-B", str(build),
                    "-DCMAKE_BUILD_TYPE=Release"], check=True)
    subprocess.run(["cmake", "--build", str(build), "-j2"], check=True)
    return build / "libVkLayer_penguinburner_latency.so"


def test_shared_graph_preserves_raw_spikes_and_native_clock(tmp_path, timing_library, build_overlay_probe):
    socket = tmp_path / "latency.sock"
    publisher = BaseFramePublisher(socket, "700", library_path=timing_library)
    clock = time.monotonic_ns() // 1000 - 180000
    for frame, delta in enumerate((0, 30000, 30000, 90000, 30000), 1):
        clock += delta
        publisher.publish(frame, clock, 42)
    assert publisher.mapping is not None
    header = struct.unpack_from("<6I2Q", publisher.mapping)
    assert header[0] % 2 == 0 and header[4] == 4
    publisher.publish(5, clock, 42)  # Duplicate is ignored.
    assert struct.unpack_from("<6I2Q", publisher.mapping)[4] == 4
    harness = tmp_path / "reader.cpp"
    harness.write_text(r'''
#include "latency_layer_internal.h"
#include <cassert>
using namespace pblayer;
int main() {
    auto reader = open_base_frame_reader();
    assert(reader);
    OverlayGraph graph;
    graph.end_time_us = now_monotonic_us();
    assert(read_base_frame_reader(reader, graph));
    assert(graph.count == 4 && graph.median_us == 30000 && graph.samples[2] == 90000);
    assert(graph.sample_times_us[3] - graph.sample_times_us[0] == 150000);
    graph.end_time_us += 3000000;
    assert(!read_base_frame_reader(reader, graph));
}
''')
    binary = tmp_path / "reader"
    build_overlay_probe(harness, binary)
    # Compilation can take longer than the graph's freshness limit. Refresh
    # the same measured intervals against the current monotonic clock.
    publisher.close()
    publisher = BaseFramePublisher(socket, "700", library_path=timing_library)
    clock = time.monotonic_ns() // 1000 - 180000
    for frame, delta in enumerate((0, 30000, 30000, 90000, 30000), 1):
        clock += delta
        publisher.publish(frame, clock, 42)
    try:
        subprocess.run([str(binary)], check=True, env={**os.environ,
            "PENGUIN_BURNER_TELEMETRY_SESSION": "700", "PENGUIN_BURNER_LATENCY_SOCKET": str(socket)})
    finally:
        publisher.close()
    assert not (tmp_path / "base-timing.700.bin").exists()
