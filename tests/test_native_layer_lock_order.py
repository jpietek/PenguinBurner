"""The layer must never call down the Vulkan chain while holding g_mutex.

The loader calls into the layer under its own lock (vkEnumeratePhysicalDevices
inside vkCreateInstance) while another thread resolves device or instance
functions through the layer, which reach the loader's lock. Observed live as a
startup hang in Assassin's Creed Shadows (amd_ags vkCreateInstance racing
vkd3d's vkCreateDevice).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def lock_order_probe(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = shutil.which("c++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    tmp_path = tmp_path_factory.mktemp("lock-order")
    repo_root = Path(__file__).resolve().parents[1]
    layer_src = repo_root / "overlay/native/latency_layer/src"
    source = tmp_path / "lock_order_probe.cpp"
    source.write_text(_PROBE_SOURCE, encoding="utf-8")
    output = tmp_path / "lock_order_probe"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-pthread",
            "-I",
            str(layer_src),
            str(source),
            *(str(layer_src / name) for name in (
                "overlay_render.cpp",
                "overlay_text.cpp",
                "latency_state.cpp",
                "frame_timing.cpp",
                "latency_layer.cpp",
                "telemetry_events.cpp",
                "marker_timing.cpp",
            )),
            "-o",
            str(output),
        ],
        check=True,
        cwd=repo_root,
    )
    return output


@pytest.mark.parametrize("resolver", ["device", "instance"])
def test_proc_addr_forwarding_does_not_hold_layer_lock(
    lock_order_probe: Path, resolver: str
) -> None:
    result = subprocess.run(
        [str(lock_order_probe), resolver],
        text=True,
        capture_output=True,
        timeout=20,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(lock_order_probe.parent / "none"),
            "XDG_RUNTIME_DIR": str(lock_order_probe.parent),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "resolved=1 enumerated=1"


_PROBE_SOURCE = r'''
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <thread>
#include "latency_layer_internal.h"

extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(
    VkInstance, const char*);
extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(
    VkDevice, const char*);

namespace pblayer {
VKAPI_ATTR VkResult VKAPI_CALL layer_enumerate_physical_devices(
    VkInstance, uint32_t*, VkPhysicalDevice*);
}

namespace {
std::mutex g_loader;  // stands in for the Vulkan loader's own lock
std::mutex g_steps;
std::condition_variable g_cv;
bool g_loader_held = false;
bool g_resolver_inside = false;

void wait_for(bool& flag) {
    std::unique_lock lock(g_steps);
    g_cv.wait(lock, [&] { return flag; });
}

void set(bool& flag) {
    {
        std::lock_guard lock(g_steps);
        flag = true;
    }
    g_cv.notify_all();
}

PFN_vkVoidFunction next_resolve() {
    set(g_resolver_inside);
    std::lock_guard loader(g_loader);
    return reinterpret_cast<PFN_vkVoidFunction>(&next_resolve);
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL next_gdpa(VkDevice, const char*) {
    return next_resolve();
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL next_gipa(VkInstance, const char*) {
    return next_resolve();
}

VKAPI_ATTR VkResult VKAPI_CALL next_enumerate(
    VkInstance, uint32_t* count, VkPhysicalDevice*) {
    *count = 0;
    return VK_SUCCESS;
}
}  // namespace

int main(int argc, char** argv) {
    const bool device_mode = argc > 1 && std::strcmp(argv[1], "device") == 0;
    auto instance = reinterpret_cast<VkInstance>(uintptr_t{0x10});
    auto device = reinterpret_cast<VkDevice>(uintptr_t{0x20});
    pblayer::InstanceContext instance_context{};
    instance_context.enumerate_physical_devices = next_enumerate;
    instance_context.get_instance_proc_addr = next_gipa;
    pblayer::g_instances[instance] = instance_context;
    pblayer::DeviceContext device_context{};
    device_context.get_device_proc_addr = next_gdpa;
    pblayer::g_devices[device] = device_context;

    bool resolved = false;
    bool enumerated = false;
    // Loader thread: holds its lock and calls into the layer, as the loader
    // does for vkEnumeratePhysicalDevices during vkCreateInstance.
    std::thread loader([&] {
        std::lock_guard held(g_loader);
        set(g_loader_held);
        wait_for(g_resolver_inside);
        uint32_t count = 0;
        enumerated = pblayer::layer_enumerate_physical_devices(
            instance, &count, nullptr) == VK_SUCCESS;
    });
    // Resolver thread: forwards an unknown name down the chain.
    std::thread resolver([&] {
        wait_for(g_loader_held);
        resolved = device_mode
            ? vkGetDeviceProcAddr(device, "vkUnknownProbeEXT") != nullptr
            : vkGetInstanceProcAddr(instance, "vkUnknownProbeEXT") != nullptr;
    });
    std::thread watchdog([] {
        std::this_thread::sleep_for(std::chrono::seconds(5));
        std::puts("deadlock: layer held g_mutex while calling down the chain");
        std::fflush(stdout);
        std::_Exit(3);
    });
    watchdog.detach();
    loader.join();
    resolver.join();
    std::printf("resolved=%d enumerated=%d\n", resolved, enumerated);
    return 0;
}
'''
