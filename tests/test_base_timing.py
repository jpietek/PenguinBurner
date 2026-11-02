"""Fast original-marker transport on the shared monotonic clock."""
import os
import struct
import subprocess
import time

from overlay.telemetry.base_timing import (
    BUFFER_MAGIC,
    BUFFER_SIZE,
    RING_SLOTS,
    SAMPLES_OFFSET,
    TIMES_OFFSET,
    BaseFramePublisher,
    QpcClockMapper,
    choose_clock_base,
    monotonic_from_qpc,
    write_base_frame,
)

HEADER = struct.Struct("<6I2Q")
NOW = 10_000_000_000


def header(buffer) -> dict[str, int]:
    names = ("sequence", "magic", "pid", "index", "count", "reserved", "frame_id", "last_us")
    return dict(zip(names, HEADER.unpack_from(buffer)))


def sample(buffer, slot: int) -> tuple[int, int]:
    interval = struct.unpack_from("<Q", buffer, SAMPLES_OFFSET + 8 * slot)[0]
    end_us = struct.unpack_from("<Q", buffer, TIMES_OFFSET + 8 * slot)[0]
    return interval, end_us


def test_layout_matches_the_native_buffer_abi():
    # Fixed across 32/64-bit layers: static_asserts in base_timing.cpp.
    assert BUFFER_MAGIC == 0x50424231 and BUFFER_MAGIC.to_bytes(4, "little") == b"1BBP"
    assert HEADER.size == 40 and struct.calcsize("<6I") == 24  # frame_id offset
    assert SAMPLES_OFFSET == 40 and TIMES_OFFSET == 40 + 8 * 512
    assert RING_SLOTS == 512 and BUFFER_SIZE == 8232
    buffer = bytearray(BUFFER_SIZE)
    assert write_base_frame(buffer, 1, NOW, 42)
    assert header(buffer) == {"sequence": 2, "magic": BUFFER_MAGIC, "pid": 42, "index": 0,
                              "count": 0, "reserved": 0, "frame_id": 1, "last_us": NOW}


def test_sequence_ends_even_and_advances_once_per_accepted_publish():
    buffer = bytearray(BUFFER_SIZE)
    assert write_base_frame(buffer, 1, NOW, 42)
    assert header(buffer)["sequence"] == 2  # 0 -> 1 while writing -> 2.
    assert write_base_frame(buffer, 2, NOW + 8_000, 42)
    assert header(buffer)["sequence"] == 4
    assert not write_base_frame(buffer, 2, NOW + 16_000, 42)  # Dropped: untouched.
    assert header(buffer)["sequence"] == 4
    struct.pack_into("<I", buffer, 0, 7)  # Left odd by an interrupted writer: resume even.
    assert write_base_frame(buffer, 3, NOW + 16_000, 42)
    assert header(buffer)["sequence"] == 10
    struct.pack_into("<I", buffer, 0, 0xFFFF_FFFE)
    assert write_base_frame(buffer, 4, NOW + 24_000, 42)
    assert header(buffer)["sequence"] == 0  # u32 wrap like the native writer.


def test_ring_records_intervals_and_their_end_times():
    buffer = bytearray(BUFFER_SIZE)
    clock = NOW
    for frame, delta in enumerate((0, 30_000, 30_000, 90_000, 30_000), 1):
        clock += delta
        assert write_base_frame(buffer, frame, clock, 42)
    state = header(buffer)
    assert (state["index"], state["count"], state["frame_id"], state["last_us"]) == (4, 4, 5, clock)
    assert [sample(buffer, slot) for slot in range(4)] == [
        (30_000, NOW + 30_000), (30_000, NOW + 60_000), (90_000, NOW + 150_000), (30_000, NOW + 180_000)]


def test_ring_drops_other_pids_and_non_advancing_frames_or_times():
    buffer = bytearray(BUFFER_SIZE)
    assert write_base_frame(buffer, 10, NOW, 42)
    assert write_base_frame(buffer, 11, NOW + 8_000, 42)
    assert not write_base_frame(buffer, 12, NOW + 16_000, 43)  # Other pid, no gap.
    assert not write_base_frame(buffer, 11, NOW + 16_000, 42)  # Frame id did not advance.
    assert not write_base_frame(buffer, 9, NOW + 16_000, 42)
    assert not write_base_frame(buffer, 12, NOW + 8_000, 42)  # Timestamp did not advance.
    assert not write_base_frame(buffer, 12, NOW + 4_000, 42)
    assert not write_base_frame(buffer, 0, NOW + 16_000, 42)
    assert not write_base_frame(buffer, 12, 0, 42)
    assert not write_base_frame(buffer, 12, NOW + 16_000, 0)
    assert not write_base_frame(buffer, 12, NOW + 16_000, 2**32)
    assert header(buffer)["count"] == 1 and header(buffer)["frame_id"] == 11


def test_gap_over_two_seconds_rebinds_the_buffer_and_empties_the_ring():
    buffer = bytearray(BUFFER_SIZE)
    for frame in range(1, 4):
        assert write_base_frame(buffer, frame, NOW + 8_000 * frame, 42)
    assert header(buffer)["count"] == 2
    # Exactly two seconds is still the same session; a microsecond more resets.
    assert write_base_frame(buffer, 4, NOW + 24_000 + 2_000_000, 42)
    assert header(buffer)["count"] == 3
    assert write_base_frame(buffer, 1, NOW + 24_000 + 2_000_000 + 2_000_001, 43)
    state = header(buffer)
    assert (state["pid"], state["index"], state["count"], state["frame_id"]) == (43, 0, 0, 1)
    assert state["last_us"] == NOW + 24_000 + 2_000_000 + 2_000_001
    assert write_base_frame(buffer, 2, state["last_us"] + 8_000, 43)
    assert header(buffer)["count"] == 1 and sample(buffer, 0) == (8_000, state["last_us"] + 8_000)


def test_ring_wraps_at_512_and_count_saturates():
    buffer = bytearray(BUFFER_SIZE)
    for frame in range(1, 1 + 512 + 3):
        assert write_base_frame(buffer, frame, NOW + 1_000 * frame, 42)
    state = header(buffer)
    assert (state["index"], state["count"]) == (2, 512)
    assert sample(buffer, 1) == (1_000, NOW + 515_000)  # Newest overwrote slot 1.
    assert sample(buffer, 2) == (1_000, NOW + 4_000)  # Oldest surviving sample.


def test_monotonic_mapping_enforces_the_window_per_base():
    # Upstream Wine: QPC is CLOCK_MONOTONIC; RAW runs 360 ms ahead (offset negative).
    assert monotonic_from_qpc(NOW - 3_000, NOW, -360_000, raw=False) == NOW - 3_000
    # Proton: QPC is CLOCK_MONOTONIC_RAW, 360 ms ahead of CLOCK_MONOTONIC.
    assert monotonic_from_qpc(NOW + 360_000 - 3_000, NOW, -360_000, raw=True) == NOW - 3_000
    # Window: at most 500 us ahead of now, at most 2 s behind.
    assert monotonic_from_qpc(NOW + 500, NOW, 0, raw=False) == NOW + 500
    assert monotonic_from_qpc(NOW + 501, NOW, 0, raw=False) is None
    assert monotonic_from_qpc(NOW - 2_000_000, NOW, 0, raw=False) == NOW - 2_000_000
    assert monotonic_from_qpc(NOW - 2_000_001, NOW, 0, raw=False) is None
    assert monotonic_from_qpc(0, NOW, 0, raw=False) is None
    assert monotonic_from_qpc(0, NOW, 0, raw=True) is None


def test_clock_base_decision_prefers_the_candidate_just_behind_now():
    assert choose_clock_base(NOW - 3_000, NOW, -360_000) is False
    assert choose_clock_base(NOW + 360_000 - 3_000, NOW, -360_000) is True
    # RAW behind MONOTONIC (offset positive): the stale-looking monotonic
    # candidate loses to the one just behind now.
    assert choose_clock_base(NOW - 50_000 - 3_000, NOW, 50_000) is True
    assert choose_clock_base(NOW - 3_000, NOW, 50_000) is False
    # Neither base fits: stale FIFO, far future, or zero.
    assert choose_clock_base(NOW - 3_000_000, NOW, 0) is None
    assert choose_clock_base(NOW + 50_000, NOW, 0) is None
    assert choose_clock_base(0, NOW, 0) is None


def test_clock_base_sticks_per_pid_instead_of_flapping_with_a_small_offset():
    # Fresh boot: RAW is 1.5 ms behind MONOTONIC, less than the marker delivery
    # delay, so "closest to now" cannot tell the bases apart and would flip
    # between them sample by sample, jittering intervals by 1.5 ms. Decide once
    # and keep it: a constant offset cancels out of the published intervals.
    offset = 1_500
    mapper = QpcClockMapper()
    assert mapper.map(42, NOW - 3_000, NOW, offset) == NOW - 1_500
    assert mapper.base(42) is True
    previous = NOW - 1_500
    intervals = []
    for step, delay in enumerate((1_000, 3_000, 1_000, 3_000), 1):
        now = NOW + 8_000 * step
        mapped = mapper.map(42, now - delay, now, offset)
        assert mapped is not None and mapped == now - delay + offset
        intervals.append(mapped - previous)
        previous = mapped
    assert mapper.base(42) is True
    assert intervals == [10_000, 6_000, 10_000, 6_000]  # Exact marker spacing, no base jitter.
    # An unusually quick delivery lands ahead of now on the chosen base: it is
    # dropped rather than remapped through the other base.
    now = NOW + 8_000 * 5
    assert mapper.map(42, now - 600, now, offset) is None
    assert mapper.base(42) is True
    now += 8_000
    assert mapper.map(42, now - 3_000, now, offset) == now - 1_500
    # A second process on the other base is decided independently.
    assert mapper.map(43, NOW - 3_000, NOW, -360_000) == NOW - 3_000
    assert mapper.base(43) is False and mapper.base(42) is True


def test_clock_base_is_redecided_after_eight_consecutive_misses():
    mapper = QpcClockMapper()
    assert mapper.map(42, NOW - 3_000, NOW, -360_000) == NOW - 3_000
    assert mapper.base(42) is False
    # The pid now belongs to a Proton process: monotonic candidates are 360 ms
    # in the future and dropped, but one late marker must not flip the base.
    for step in range(1, 8):
        now = NOW + 8_000 * step
        assert mapper.map(42, now + 360_000 - 3_000, now, -360_000) is None
        assert mapper.base(42) is False
    now = NOW + 8_000 * 8
    assert mapper.map(42, now + 360_000 - 3_000, now, -360_000) == now - 3_000
    assert mapper.base(42) is True
    # A fitting sample clears the miss streak.
    mapper2 = QpcClockMapper()
    assert mapper2.map(42, NOW - 3_000, NOW, -360_000) == NOW - 3_000
    for step in range(1, 8):
        now = NOW + 8_000 * step
        assert mapper2.map(42, now + 50_000, now, -360_000) is None
    now = NOW + 8_000 * 8
    assert mapper2.map(42, now - 3_000, now, -360_000) == now - 3_000
    for step in range(9, 16):
        now = NOW + 8_000 * step
        assert mapper2.map(42, now + 50_000, now, -360_000) is None
    assert mapper2.base(42) is False
    # Samples that fit no base leave the pid undecided.
    assert QpcClockMapper().map(42, NOW - 5_000_000, NOW, 0) is None


def test_publish_writes_only_samples_on_the_host_monotonic_clock(tmp_path):
    publisher = BaseFramePublisher(tmp_path / "latency.sock", "700")
    path = tmp_path / "base-timing.700.bin"
    assert publisher.path == path
    now = time.monotonic_ns() // 1000
    publisher.publish(1, now - 3_000_000, 42)  # Older than the 2 s window on both bases.
    publisher.publish(2, now + 5_000_000, 42)  # 5 s in the future on both bases.
    publisher.publish(3, 0, 42)
    assert publisher.mapping is None and not path.exists()  # Nothing mapped yet.
    publisher.publish(4, now - 100_000, 42)  # CLOCK_MONOTONIC base decides the pid.
    publisher.publish(5, now - 92_000, 42)
    assert publisher.mapping is not None and path.stat().st_size == 8232
    state = header(publisher.mapping)
    assert state["magic"] == BUFFER_MAGIC and state["pid"] == 42 and state["count"] == 1
    assert state["frame_id"] == 5 and abs(state["last_us"] - (now - 92_000)) <= 2_000
    assert abs(sample(publisher.mapping, 0)[0] - 8_000) <= 2_000
    publisher.close()
    assert publisher.mapping is None and not path.exists()


def test_publisher_ignores_non_numeric_sessions_and_foreign_files(tmp_path):
    assert BaseFramePublisher(tmp_path / "latency.sock", "").path is None
    assert BaseFramePublisher(tmp_path / "latency.sock", "../etc").path is None
    publisher = BaseFramePublisher(tmp_path / "latency.sock", "700")
    now = time.monotonic_ns() // 1000
    publisher.publish(1, now - 1_000, 42)
    assert publisher.path is not None and publisher.path.exists()
    # Another bridge replaced the file: close() leaves the newcomer alone.
    publisher.path.unlink()
    publisher.path.write_bytes(b"other")
    publisher.close()
    assert publisher.path.read_bytes() == b"other"


def test_native_reader_decodes_the_python_publisher(tmp_path, build_overlay_probe):
    socket = tmp_path / "latency.sock"
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
    # Publish after compiling so the samples are inside the reader's window.
    publisher = BaseFramePublisher(socket, "700")
    clock = time.monotonic_ns() // 1000 - 180000
    for frame, delta in enumerate((0, 30000, 30000, 90000, 30000), 1):
        clock += delta
        publisher.publish(frame, clock, 42)
    assert publisher.mapping is not None and header(publisher.mapping)["count"] == 4
    try:
        subprocess.run([str(binary)], check=True, env={**os.environ,
            "PENGUIN_BURNER_TELEMETRY_SESSION": "700", "PENGUIN_BURNER_LATENCY_SOCKET": str(socket)})
    finally:
        publisher.close()
    assert not (tmp_path / "base-timing.700.bin").exists()
