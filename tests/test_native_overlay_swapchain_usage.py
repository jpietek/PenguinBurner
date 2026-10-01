"""Exercise the native entry point with storage-only DXVK swapchains."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def swapchain_probe(tmp_path_factory):
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    root = Path(__file__).resolve().parents[1] / "overlay/native/latency_layer/src"
    directory = tmp_path_factory.mktemp("swapchain-usage")
    source = directory / "probe.cpp"
    source.write_text(PROBE)
    binary = directory / "probe"
    subprocess.run([compiler, "-std=c++17", "-I", str(root), str(source),
                    *(str(p) for p in sorted(root.glob("*.cpp"))), "-o", str(binary)], check=True)
    return binary


@pytest.mark.parametrize("scenario", ["storage", "color", "unsupported", "query-failed", "missing-query", "shared"])
@pytest.mark.parametrize("visible", ["0", "1"])
def test_swapchain_usage_preserves_game_and_supports_live_overlay(swapchain_probe, scenario, visible):
    subprocess.run([str(swapchain_probe), scenario], check=True, timeout=10,
                   env={**os.environ, "PB_OVERLAY": visible,
                        "PENGUIN_BURNER_LATENCY_SOCKET": str(swapchain_probe.parent / "absent.sock")})


PROBE = r'''
#include "latency_layer_internal.h"
#include <cassert>
#include <string>
namespace pblayer {
VKAPI_ATTR VkResult VKAPI_CALL layer_create_swapchain_khr(
    VkDevice, const VkSwapchainCreateInfoKHR*, const VkAllocationCallbacks*, VkSwapchainKHR*);
}
namespace {
std::string scenario;
VkImageUsageFlags received_usage = 0;
const void* received_chain = nullptr;
uint32_t queries = 0;
VKAPI_ATTR VkResult VKAPI_CALL capabilities(VkPhysicalDevice physical, VkSurfaceKHR surface,
                                           VkSurfaceCapabilitiesKHR* caps) {
    assert(physical == reinterpret_cast<VkPhysicalDevice>(2));
    assert(surface == reinterpret_cast<VkSurfaceKHR>(4));
    ++queries;
    caps->supportedUsageFlags = VK_IMAGE_USAGE_STORAGE_BIT;
    if (scenario != "unsupported") caps->supportedUsageFlags |= VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    return scenario == "query-failed" ? VK_ERROR_SURFACE_LOST_KHR : VK_SUCCESS;
}
VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL instance_proc(VkInstance instance, const char* name) {
    assert(instance == reinterpret_cast<VkInstance>(1));
    assert(std::string(name) == "vkGetPhysicalDeviceSurfaceCapabilitiesKHR");
    return scenario == "missing-query" ? nullptr : reinterpret_cast<PFN_vkVoidFunction>(capabilities);
}
VKAPI_ATTR VkResult VKAPI_CALL create(VkDevice device, const VkSwapchainCreateInfoKHR* info,
                                    const VkAllocationCallbacks*, VkSwapchainKHR* swapchain) {
    assert(device == reinterpret_cast<VkDevice>(3));
    received_usage = info->imageUsage;
    received_chain = info->pNext;
    assert(info->imageExtent.width == 3840 && info->imageExtent.height == 2160);
    *swapchain = reinterpret_cast<VkSwapchainKHR>(5);
    return VK_SUCCESS;
}
}
int main(int argc, char** argv) {
    assert(argc == 2);
    scenario = argv[1];
    auto instance = reinterpret_cast<VkInstance>(1);
    auto physical = reinterpret_cast<VkPhysicalDevice>(2);
    auto device = reinterpret_cast<VkDevice>(3);
    pblayer::InstanceContext instance_context{};
    instance_context.get_instance_proc_addr = instance_proc;
    pblayer::g_instances[instance] = instance_context;
    pblayer::g_physical_devices[physical] = instance;
    pblayer::DeviceContext device_context{};
    device_context.physical_device = physical;
    device_context.create_swapchain_khr = create;
    pblayer::g_devices[device] = device_context;
    VkDeviceGroupSwapchainCreateInfoKHR chain{};
    chain.sType = VK_STRUCTURE_TYPE_DEVICE_GROUP_SWAPCHAIN_CREATE_INFO_KHR;
    VkSwapchainCreateInfoKHR info{};
    info.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR;
    info.pNext = &chain;
    info.surface = reinterpret_cast<VkSurfaceKHR>(4);
    info.imageExtent = {3840, 2160};
    info.imageArrayLayers = 1;
    info.imageFormat = VK_FORMAT_B8G8R8A8_UNORM;
    info.presentMode = scenario == "shared" ? VK_PRESENT_MODE_SHARED_DEMAND_REFRESH_KHR : VK_PRESENT_MODE_FIFO_KHR;
    info.imageUsage = VK_IMAGE_USAGE_STORAGE_BIT;
    if (scenario == "color") info.imageUsage |= VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    const auto original = info.imageUsage;
    VkSwapchainKHR swapchain = VK_NULL_HANDLE;
    assert(pblayer::layer_create_swapchain_khr(device, &info, nullptr, &swapchain) == VK_SUCCESS);
    const auto expected = scenario == "storage" ? original | VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT : original;
    assert(received_usage == expected);
    assert(info.imageUsage == original && info.pNext == &chain && received_chain == &chain);
    assert(pblayer::g_swapchains.at(swapchain).image_usage == expected);
    if (scenario == "color" || scenario == "shared" || scenario == "missing-query") assert(queries == 0);
}
'''
