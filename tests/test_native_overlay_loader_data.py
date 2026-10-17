from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def loader_data_probe(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build_loader_data_probe(tmp_path_factory.mktemp("loader-data"))


@pytest.mark.parametrize("callback_first", [False, True])
def test_device_creation_discovers_and_uses_loader_callback(
    loader_data_probe: Path, callback_first: bool
) -> None:
    assert _run_probe(loader_data_probe, 0, callback_first) == (
        "ready=1 allocated=3 stamped=3 live=14"
    )


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_loader_callback_failure_releases_all_overlay_resources(
    loader_data_probe: Path, fail_at: int
) -> None:
    assert _run_probe(loader_data_probe, fail_at, True) == (
        f"ready=0 allocated=3 stamped={fail_at} live=0"
    )


def test_device_creation_without_loader_callback(loader_data_probe: Path) -> None:
    assert _run_probe(loader_data_probe, -1, False) == (
        "ready=1 allocated=3 stamped=0 live=14"
    )


def test_estimated_latency_glyph_is_visible(loader_data_probe: Path) -> None:
    def glyph(character: str) -> list[int]:
        result = subprocess.run(
            [str(loader_data_probe), "--glyph", character],
            check=True, text=True, capture_output=True, timeout=10,
        )
        return [int(row) for row in result.stdout.split()]

    estimate = glyph("~")
    assert len(estimate) == 7
    assert any(estimate)
    assert estimate != glyph("-")
    assert estimate != glyph(" ")


def _run_probe(binary: Path, fail_at: int, callback_first: bool) -> str:
    result = subprocess.run(
        [str(binary), str(fail_at), str(int(callback_first))],
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PB_OVERLAY": "1",
            "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(binary.parent / "no-override"),
            "XDG_RUNTIME_DIR": str(binary.parent),
        },
    )
    return result.stdout.strip()


def _build_loader_data_probe(tmp_path: Path) -> Path:
    compiler = shutil.which("c++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    repo_root = Path(__file__).resolve().parents[1]
    source = tmp_path / "loader_data_probe.cpp"
    source.write_text(_PROBE_SOURCE, encoding="utf-8")
    output = tmp_path / "loader_data_probe"
    layer_src = repo_root / "overlay/native/latency_layer/src"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-I",
            str(layer_src),
            str(source),
            str(layer_src / "overlay_render.cpp"),
            str(layer_src / "overlay_text.cpp"),
            str(layer_src / "latency_state.cpp"),
            str(layer_src / "latency_layer.cpp"),
            str(layer_src / "telemetry_events.cpp"),
            str(layer_src / "marker_timing.cpp"),
            "-o",
            str(output),
        ],
        check=True,
        cwd=repo_root,
    )
    return output


_PROBE_SOURCE = r'''
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <unordered_set>
#include "latency_layer_internal.h"

namespace pblayer {
std::array<uint8_t, 7> overlay_glyph_rows(char ch);
VKAPI_ATTR VkResult VKAPI_CALL layer_create_device(
    VkPhysicalDevice, const VkDeviceCreateInfo*, const VkAllocationCallbacks*, VkDevice*);
bool init_overlay_resources(
    const DeviceContext& device_context,
    VkSwapchainKHR swapchain,
    SwapchainContext& swapchain_context,
    uint32_t queue_family_index);
}

namespace {

constexpr uint32_t kImageCount = 3;

uint32_t g_allocated = 0;
uint32_t g_stamped = 0;
uint64_t g_next_handle = 1;
int g_fail_at = 0;
VkDevice g_device = VK_NULL_HANDLE;
std::vector<VkCommandBuffer> g_buffers;
std::unordered_set<uint64_t> g_live_objects;

template <typename Handle>
Handle fake_handle() {
    return reinterpret_cast<Handle>(g_next_handle++);
}

VKAPI_ATTR VkResult VKAPI_CALL set_loader_data(VkDevice device, void* object) {
    assert(device == g_device);
    assert(object == g_buffers.at(g_stamped));
    ++g_stamped;
    return static_cast<int>(g_stamped) == g_fail_at
        ? VK_ERROR_INITIALIZATION_FAILED : VK_SUCCESS;
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL next_device_proc(VkDevice, const char*) {
    return nullptr;
}

VKAPI_ATTR VkResult VKAPI_CALL next_create_device(
    VkPhysicalDevice, const VkDeviceCreateInfo* info,
    const VkAllocationCallbacks*, VkDevice* device) {
    // The layer must advance only the link entry before passing it downstream.
    auto* link = pblayer::find_layer_chain_entry(info);
    assert(link && link->u.pLayerInfo == nullptr);
    *device = g_device;
    return VK_SUCCESS;
}

pblayer::DeviceContext create_device(bool callback_first) {
    VkInstance instance = fake_handle<VkInstance>();
    VkPhysicalDevice physical = fake_handle<VkPhysicalDevice>();
    g_device = fake_handle<VkDevice>();
    pblayer::InstanceContext instance_context{};
    instance_context.create_device = next_create_device;
    pblayer::g_instances[instance] = instance_context;
    pblayer::g_physical_devices[physical] = instance;

    VkLayerDeviceLink next{};
    next.pfnNextGetDeviceProcAddr = next_device_proc;
    VkLayerDeviceCreateInfo link{};
    link.sType = VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO;
    link.function = VK_LAYER_LINK_INFO;
    link.u.pLayerInfo = &next;
    VkLayerDeviceCreateInfo callback{};
    callback.sType = VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO;
    callback.function = VK_LOADER_DATA_CALLBACK;
    callback.u.pfnSetDeviceLoaderData = set_loader_data;
    VkPhysicalDeviceFeatures2 unrelated{};
    unrelated.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;
    if (g_fail_at == -1) {
        unrelated.pNext = &link;
    } else if (callback_first) {
        unrelated.pNext = &callback;
        callback.pNext = &link;
    } else {
        unrelated.pNext = &link;
        link.pNext = &callback;
    }
    VkDeviceCreateInfo info{};
    info.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
    info.pNext = &unrelated;
    VkDevice device = VK_NULL_HANDLE;
    assert(pblayer::layer_create_device(physical, &info, nullptr, &device) == VK_SUCCESS);
    auto context = pblayer::g_devices.at(device);
    assert(context.set_device_loader_data == (g_fail_at == -1 ? nullptr : set_loader_data));
    return context;
}

VKAPI_ATTR VkResult VKAPI_CALL get_swapchain_images(
    VkDevice, VkSwapchainKHR, uint32_t* count, VkImage* images) {
    if (images == nullptr) {
        *count = kImageCount;
        return VK_SUCCESS;
    }
    for (uint32_t i = 0; i < *count; ++i) {
        images[i] = fake_handle<VkImage>();
    }
    return VK_SUCCESS;
}

VKAPI_ATTR VkResult VKAPI_CALL allocate_command_buffers(
    VkDevice,
    const VkCommandBufferAllocateInfo* info,
    VkCommandBuffer* buffers) {
    for (uint32_t i = 0; i < info->commandBufferCount; ++i) {
        buffers[i] = fake_handle<VkCommandBuffer>();
        g_buffers.push_back(buffers[i]);
        ++g_allocated;
    }
    return VK_SUCCESS;
}

// Creation entry points the overlay only needs to succeed for.
template <typename Handle, typename Info>
VKAPI_ATTR VkResult VKAPI_CALL create_object(
    VkDevice, const Info*, const VkAllocationCallbacks*, Handle* handle) {
    *handle = fake_handle<Handle>();
    assert(g_live_objects.insert(pblayer::handle_to_u64(*handle)).second);
    return VK_SUCCESS;
}

template <typename Handle>
VKAPI_ATTR void VKAPI_CALL destroy_object(
    VkDevice, Handle handle, const VkAllocationCallbacks*) {
    assert(g_live_objects.erase(pblayer::handle_to_u64(handle)) == 1);
}

VKAPI_ATTR VkResult VKAPI_CALL wait_for_fences(
    VkDevice, uint32_t, const VkFence*, VkBool32, uint64_t) {
    return VK_SUCCESS;
}

VKAPI_ATTR VkResult VKAPI_CALL reset_fences(
    VkDevice, uint32_t, const VkFence*) {
    return VK_SUCCESS;
}

VKAPI_ATTR VkResult VKAPI_CALL get_fence_status(VkDevice, VkFence) {
    return VK_SUCCESS;
}

VKAPI_ATTR VkResult VKAPI_CALL reset_command_buffer(
    VkCommandBuffer, VkCommandBufferResetFlags) {
    return VK_SUCCESS;
}

VKAPI_ATTR VkResult VKAPI_CALL begin_command_buffer(
    VkCommandBuffer, const VkCommandBufferBeginInfo*) {
    return VK_SUCCESS;
}

VKAPI_ATTR VkResult VKAPI_CALL end_command_buffer(VkCommandBuffer) {
    return VK_SUCCESS;
}

VKAPI_ATTR void VKAPI_CALL cmd_begin_render_pass(
    VkCommandBuffer, const VkRenderPassBeginInfo*, VkSubpassContents) {}

VKAPI_ATTR void VKAPI_CALL cmd_end_render_pass(VkCommandBuffer) {}

VKAPI_ATTR void VKAPI_CALL cmd_clear_attachments(
    VkCommandBuffer,
    uint32_t,
    const VkClearAttachment*,
    uint32_t,
    const VkClearRect*) {}

VKAPI_ATTR VkResult VKAPI_CALL queue_submit(
    VkQueue, uint32_t, const VkSubmitInfo*, VkFence) {
    return VK_SUCCESS;
}

pblayer::DeviceContext make_device_context(bool callback_first) {
    auto context = create_device(callback_first);
    context.get_swapchain_images_khr = get_swapchain_images;
    context.queue_submit = queue_submit;
    context.create_image_view = create_object<VkImageView, VkImageViewCreateInfo>;
    context.destroy_image_view = destroy_object<VkImageView>;
    context.create_render_pass =
        create_object<VkRenderPass, VkRenderPassCreateInfo>;
    context.destroy_render_pass = destroy_object<VkRenderPass>;
    context.create_framebuffer =
        create_object<VkFramebuffer, VkFramebufferCreateInfo>;
    context.destroy_framebuffer = destroy_object<VkFramebuffer>;
    context.create_command_pool =
        create_object<VkCommandPool, VkCommandPoolCreateInfo>;
    context.destroy_command_pool = destroy_object<VkCommandPool>;
    context.allocate_command_buffers = allocate_command_buffers;
    context.reset_command_buffer = reset_command_buffer;
    context.begin_command_buffer = begin_command_buffer;
    context.end_command_buffer = end_command_buffer;
    context.cmd_begin_render_pass = cmd_begin_render_pass;
    context.cmd_end_render_pass = cmd_end_render_pass;
    context.cmd_clear_attachments = cmd_clear_attachments;
    context.create_semaphore =
        create_object<VkSemaphore, VkSemaphoreCreateInfo>;
    context.destroy_semaphore = destroy_object<VkSemaphore>;
    context.create_fence = create_object<VkFence, VkFenceCreateInfo>;
    context.destroy_fence = destroy_object<VkFence>;
    context.get_fence_status = get_fence_status;
    context.reset_fences = reset_fences;
    context.wait_for_fences = wait_for_fences;
    return context;
}

pblayer::SwapchainContext make_swapchain_context() {
    pblayer::SwapchainContext context{};
    context.image_format = VK_FORMAT_B8G8R8A8_UNORM;
    context.image_extent = {1920, 1080};
    context.image_array_layers = 1;
    context.image_usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    return context;
}

}  // namespace

int main(int argc, char** argv) {
    assert(argc == 3);
    if (std::string(argv[1]) == "--glyph") {
        for (const auto row : pblayer::overlay_glyph_rows(argv[2][0])) {
            std::printf("%u ", static_cast<unsigned>(row));
        }
        return 0;
    }
    g_fail_at = std::atoi(argv[1]);
    pblayer::DeviceContext device_context = make_device_context(std::atoi(argv[2]) != 0);
    pblayer::SwapchainContext swapchain_context = make_swapchain_context();
    const bool ready = pblayer::init_overlay_resources(
        device_context,
        fake_handle<VkSwapchainKHR>(),
        swapchain_context,
        0);
    if (g_fail_at > 0) {
        assert(!ready && !swapchain_context.overlay.ready);
        assert(swapchain_context.overlay.command_buffers.empty());
        assert(swapchain_context.overlay.command_pool == VK_NULL_HANDLE);
    }
    std::printf(
        "ready=%d allocated=%u stamped=%u live=%zu\n",
        ready ? 1 : 0,
        g_allocated,
        g_stamped,
        g_live_objects.size());
    pblayer::destroy_overlay_resources(device_context, swapchain_context);
    assert(g_live_objects.empty());
    return 0;
}
'''
