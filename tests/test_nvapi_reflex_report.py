from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from overlay.telemetry.reflex_report import REPORT_PREFIX, parse_reflex_report


@pytest.fixture(scope="module")
def captured_report(tmp_path_factory: pytest.TempPathFactory) -> str:
    compiler = shutil.which("x86_64-w64-mingw32-g++")
    wine = shutil.which("wine")
    wineserver = shutil.which("wineserver")
    if not compiler or not wine or not wineserver:
        pytest.skip("NVAPI report probe requires MinGW and Wine")
    root = tmp_path_factory.mktemp("reflex-report")
    source_dir = Path(__file__).resolve().parents[1] / "overlay/native/nvapi_shim/src"
    source = root / "probe.cpp"
    source.write_text(PROBE, encoding="utf-8")
    binary = root / "probe.exe"
    subprocess.run([compiler, "-std=c++17", "-O2", "-static", "-I", str(source_dir),
                    str(source), "-o", str(binary)], check=True, capture_output=True)
    env = {**os.environ, "WINEPREFIX": str(root / "prefix"), "WINEDEBUG": "-all",
           "WINEDLLOVERRIDES": "mscoree,mshtml=", "DISPLAY": "", "WAYLAND_DISPLAY": ""}
    try:
        subprocess.run([wine, str(binary)], cwd=root, env=env, check=True,
                       capture_output=True, timeout=90)
        return (root / "reports.log").read_text(encoding="utf-8").strip()
    finally:
        subprocess.run([wineserver, "-k"], env=env, check=False,
                       capture_output=True, timeout=10)


def test_shim_preserves_call_and_transports_latest_driver_endpoints(captured_report: str) -> None:
    # The native probe checks forwarding, unchanged output/status, null and
    # unknown-version buffers, empty histories, failed calls, and truncation.
    sample = parse_reflex_report(captured_report, "session-123")
    assert sample is not None
    assert sample["present_id"] == 82
    assert sample["pid"] == 42
    assert sample["device"] == "abc"
    assert sample["input_sample_us"] == 1000
    assert sample["gpu_render_start_us"] == 2000
    assert sample["gpu_render_end_us"] == 17000
    assert sample["session_id"] == "session-123"
    assert sample["measurement"] == "driver-report"
    assert "present_count" not in sample  # Timing reports are not new presents.


@pytest.mark.parametrize("field,value", [
    ("pid", 0), ("present_id", -1), ("input_sample_us", True),
    ("gpu_render_end_us", 1.5), ("gpu_render_end_us", 2**63),
    ("device", "not-hex"), ("device", "f" * 17),
])
def test_reject_malformed_report(captured_report: str, field: str, value: object) -> None:
    data = json.loads(captured_report.removeprefix(REPORT_PREFIX))
    data[field] = value
    assert parse_reflex_report(REPORT_PREFIX + json.dumps(data), "") is None


@pytest.mark.parametrize("line", ["unrelated", REPORT_PREFIX + "{", REPORT_PREFIX + "[]",
                                REPORT_PREFIX + "{}", REPORT_PREFIX + "null"])
def test_reject_nonreport(line: str) -> None:
    assert parse_reflex_report(line, "") is None


def test_bridge_routes_reports_without_inventing_input_or_gpu_times(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, captured_report: str,
) -> None:
    import overlay.telemetry.nvapi_marker_bridge as bridge

    data = json.loads(captured_report.removeprefix(REPORT_PREFIX))
    data.update(input_sample_us=0, gpu_render_end_us=0, session_id="untrusted", v=999)
    lines = iter([REPORT_PREFIX + json.dumps(data), REPORT_PREFIX + "broken"])
    samples: list[dict[str, object]] = []
    monkeypatch.setattr(bridge, "_follow", lambda *_args, **_kwargs: lines)
    monkeypatch.setattr(bridge, "_send_sample", lambda _sock, _targets, sample: samples.append(sample))
    bridge.run(tmp_path / "unused", env={"PENGUIN_BURNER_TELEMETRY_SESSION": "123"})
    assert len(samples) == 1
    assert samples[0]["session_id"] == "123"
    assert samples[0]["v"] == 1
    assert samples[0]["input_sample_us"] == samples[0]["gpu_render_end_us"] == 0


PROBE = r'''
#include "nvapi_shim.cpp"
#include <cassert>
#include <limits>
unsigned calls = 0;
int result = 0;
void* expected = nullptr;
NvAPI_Status __cdecl real_latency(void* device, void* data) {
    assert(device == reinterpret_cast<void*>(0xabc) && data == expected);
    ++calls;
    return result;
}
unsigned poll_calls = 0;
NvAPI_Status __cdecl polling_driver(void* device, void* data) {
    assert(device == reinterpret_cast<void*>(0xabc));
    auto* report = static_cast<pb_reflex::LatencyResults*>(data);
    assert(report->version == pb_reflex::kVersion);
    ++poll_calls;
    report->frames[0].frame_id = 100 + poll_calls;
    report->frames[0].input_sample = 2000;
    report->frames[0].gpu_end = 7000;
    poll_latency_report(device, 999999); // Reentrant callbacks cannot query again.
    return result;
}
void* __cdecl polling_query(NvU32 id) {
    assert(id == kIdGetLatency);
    return reinterpret_cast<void*>(&polling_driver);
}
NvAPI_Status __cdecl marker_driver(void*, NvLatencyMarkerParams*) { return result; }
int main() {
    InitializeCriticalSection(&g_emit_lock);
    g_out_fd = _open("reports.log", _O_CREAT | _O_TRUNC | _O_WRONLY | _O_BINARY, _S_IREAD | _S_IWRITE);
    assert(g_out_fd >= 0);
    g_pid = 42;
    g_real_get_latency = real_latency;
    pb_reflex::LatencyResults report{};
    expected = &report;
    auto call = [&] { return wrap_get_latency(reinterpret_cast<void*>(0xabc), expected); };
    report.version = pb_reflex::kVersion;
    assert(call() == 0); // Empty warm-up ring.
    report.frames[1].frame_id = 1;
    auto& newest = report.frames[63];
    newest.frame_id = 82;
    newest.input_sample = 1000;
    newest.gpu_start = 2000;
    newest.gpu_end = 17000;
    result = -1;
    assert(call() == -1); // A failed call must never publish populated memory.
    result = 0;
    report.version = 0;
    assert(call() == 0);
    report.version = pb_reflex::kVersion;
    const auto before = report;
    assert(call() == 0);
    assert(std::memcmp(&before, &report, sizeof(report)) == 0);
    expected = nullptr;
    assert(call() == 0);
    assert(calls == 5);
    _close(g_out_fd);
    g_out_fd = _open("polls.log", _O_CREAT | _O_TRUNC | _O_WRONLY | _O_BINARY, _S_IREAD | _S_IWRITE);
    g_real_get_latency = polling_driver;
    void* device = reinterpret_cast<void*>(0xabc);
    poll_latency_report(nullptr, 1000);
    assert(poll_calls == 0);
    poll_latency_report(device, 1000);
    poll_latency_report(device, 1099);
    assert(poll_calls == 1);
    poll_latency_report(device, 1100);
    assert(poll_calls == 2);
    const auto length_before_failure = _tell(g_out_fd);
    result = -1;
    poll_latency_report(device, 1200);
    assert(poll_calls == 3 && _tell(g_out_fd) == length_before_failure);
    result = 0;
    g_real_get_latency = nullptr;
    g_real_qi = polling_query;
    poll_latency_report(device, 1300);
    assert(poll_calls == 4); // Reporting works even if the game never queried this API.
    g_real_set_marker = marker_driver;
    NvLatencyMarkerParams marker{};
    marker.version = sizeof(marker) | (1u << 16);
    marker.markerType = 5;
    g_next_report_poll = 0;
    result = -1;
    assert(wrap_set_latency_marker(device, &marker) == -1 && poll_calls == 4);
    result = 0;
    marker.markerType = 0;
    assert(wrap_set_latency_marker(device, &marker) == 0 && poll_calls == 4);
    marker.markerType = 5;
    marker.version = 0;
    assert(wrap_set_latency_marker(device, &marker) == 0 && poll_calls == 4);
    marker.version = sizeof(marker) | (1u << 16);
    assert(wrap_set_latency_marker(device, &marker) == 0 && poll_calls == 5);
    _close(g_out_fd);
    char tiny[8];
    assert(pb_reflex::format_latest(report, 42, 0xabc, tiny, sizeof(tiny)) == 0);
    auto* words = reinterpret_cast<uint64_t*>(&newest);
    for (size_t i = 0; i < 14; ++i) words[i] = std::numeric_limits<uint64_t>::max();
    char maximum[pb_reflex::kLineBytes];
    const int length = pb_reflex::format_latest(report, 42, 0xabc, maximum, sizeof(maximum));
    assert(length > 0 && maximum[length - 1] == '\n');
    return 0;
}
'''
