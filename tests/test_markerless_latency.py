"""Calibrated frame completion: delayed readback is not added to latency."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def frame_probe(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    root = Path(__file__).resolve().parents[1] / "overlay/native/latency_layer/src"
    directory = tmp_path_factory.mktemp("frame-completion")
    source = directory / "probe.cpp"
    source.write_text(PROBE)
    binary = directory / "probe"
    subprocess.run([compiler, "-std=c++17", "-I", str(root), str(source),
                    *(str(p) for p in sorted(root.glob("*.cpp"))), "-o", str(binary)], check=True)
    return binary


@pytest.mark.parametrize("mode", ["valid", "delayed", "wrap", "unready", "bad-calibration", "failed-present"])
def test_calibrated_completion_transport(frame_probe: Path, tmp_path: Path, mode: str) -> None:
    path = tmp_path / "latency.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(str(path))
        receiver.settimeout(0.1)
        subprocess.run([str(frame_probe), mode], check=True, timeout=10, env={
            **os.environ, "PENGUIN_BURNER_LATENCY_SOCKET": str(path),
            "PENGUIN_BURNER_TELEMETRY_SESSION": "12345",
        })
        if mode in {"valid", "delayed", "wrap"}:
            sample = json.loads(receiver.recv(4096))
            assert sample["measurement"] == "frame-cycle-gpu"
            assert sample["session_id"] == "12345"
            assert sample["frame_cycle_gpu_us"] == 22000
            assert sample["present_count"] == 11
            assert "present_frametime_us" not in sample
        with pytest.raises(TimeoutError):
            receiver.recv(4096)


PROBE = r'''
#include "latency_layer_internal.h"
#include <cassert>
#include <limits>
using namespace pblayer;
std::string mode;
unsigned reads = 0, calibrations = 0;
VKAPI_ATTR VkResult VKAPI_CALL query(VkDevice, VkQueryPool, uint32_t first, uint32_t count,
    size_t size, void* out, VkDeviceSize stride, VkQueryResultFlags flags) {
    ++reads;
    assert(first == 0 && count == 1 && size == 8 && stride == 8);
    assert(flags == VK_QUERY_RESULT_64_BIT); // No blocking WAIT_BIT.
    *static_cast<uint64_t*>(out) = mode == "wrap" ? (100ull - 28000000) & ((1ull << 36) - 1) : 4972000000;
    return mode == "unready" ? VK_NOT_READY : VK_SUCCESS;
}
VKAPI_ATTR VkResult VKAPI_CALL calibration(VkDevice, uint32_t count,
    const VkCalibratedTimestampInfoEXT* info, uint64_t* out, uint64_t* deviation) {
    ++calibrations;
    assert(count == 2 && info[0].timeDomain == VK_TIME_DOMAIN_DEVICE_EXT
        && info[1].timeDomain == VK_TIME_DOMAIN_CLOCK_MONOTONIC_EXT);
    out[0] = mode == "wrap" ? 100 : mode == "delayed" ? 5100000000 : 5000000000;
    out[1] = mode == "delayed" ? 1150000000 : 1050000000;
    *deviation = mode == "bad-calibration" ? 500001 : 200;
    return VK_SUCCESS;
}
int main(int argc, char** argv) {
    assert(argc == 2);
    mode = argv[1];
    FrameCompletionSample frame{1000000, 1016000, 11, true};
    assert(calibrated_frame_latency_us(frame, 4972000000, 5000000000, 1050000000, 200, 1, 64) == 22000);
    assert(calibrated_frame_latency_us(frame, 5000000001, 5000000000, 1050000000, 200, 1, 64) == 0);
    assert(calibrated_frame_latency_us(frame, 4900000000, 5000000000, 1050000000, 200, 1, 64) == 0);
    for (auto bits : {0u, 65u}) assert(calibrated_frame_latency_us(frame, 4972000000, 5000000000, 1050000000, 200, 1, bits) == 0);
    for (auto period : {0.0, -1.0, std::numeric_limits<double>::quiet_NaN()})
        assert(calibrated_frame_latency_us(frame, 4972000000, 5000000000, 1050000000, 200, period, 64) == 0);
    auto paused = frame; paused.present_us += 2000000;
    assert(calibrated_frame_latency_us(paused, 4972000000, 5000000000, 1050000000, 200, 1, 64) == 0);
    DeviceContext device{};
    device.get_query_pool_results = query;
    device.get_calibrated_timestamps = calibration;
    device.physical_device_properties.limits.timestampPeriod = 1;
    device.queue_family_properties.resize(1);
    device.queue_family_properties[0].timestampValidBits = mode == "wrap" ? 36 : 64;
    OverlayRenderResources resources{};
    resources.queue_family_index = 0;
    resources.frame_queries = reinterpret_cast<VkQueryPool>(1);
    frame.presented = mode != "failed-present";
    resources.frame_samples = {frame};
    collect_frame_completion(device, VK_NULL_HANDLE, resources, 0);
    collect_frame_completion(device, VK_NULL_HANDLE, resources, 0); // A slot emits once.
    assert(reads == (mode == "failed-present" ? 0 : 1));
    assert(calibrations == (mode == "failed-present" || mode == "unready" ? 0 : 1));
}
'''
