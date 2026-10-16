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


@pytest.mark.parametrize("mode", ["ready", "failure", "destroy", "solid", "pipeline", "pipeline_destroy"])
def test_present_does_not_wait_for_resource_preparation(
    loader_data_probe: Path, mode: str,
) -> None:
    assert _run_probe(loader_data_probe, 1 if mode == "failure" else 0, True, mode) == (
        "async initialization and cleanup passed"
    )


def _run_probe(
    binary: Path, fail_at: int, callback_first: bool, mode: str | None = None,
) -> str:
    result = subprocess.run(
        [str(binary), str(fail_at), str(int(callback_first)), *([mode] if mode else [])],
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
VKAPI_ATTR VkResult VKAPI_CALL layer_create_device(
    VkPhysicalDevice, const VkDeviceCreateInfo*, const VkAllocationCallbacks*, VkDevice*);
VKAPI_ATTR void VKAPI_CALL layer_destroy_device(VkDevice, const VkAllocationCallbacks*);
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
bool g_block_init = false;
bool g_block_pipeline = false;
uint32_t g_cache_creations = 0;
std::unordered_map<VkDeviceMemory, std::vector<uint8_t>> g_memory;
std::promise<void> g_init_entered;
std::promise<void> g_release_init;
std::shared_future<void> g_release = g_release_init.get_future().share();
const std::thread::id g_present_thread = std::this_thread::get_id();

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
        if (g_block_init) {
            assert(std::this_thread::get_id() != g_present_thread);
            g_init_entered.set_value();
            g_release.wait();
        }
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

template <typename Handle>
VKAPI_ATTR void VKAPI_CALL memory_requirements(VkDevice, Handle, VkMemoryRequirements* out) {
    *out = {4 * 1024 * 1024, 4, 1};
}

VKAPI_ATTR VkResult VKAPI_CALL allocate_memory(
    VkDevice device, const VkMemoryAllocateInfo* info, const VkAllocationCallbacks* allocator,
    VkDeviceMemory* memory) {
    auto result = create_object<VkDeviceMemory>(device, info, allocator, memory);
    g_memory[*memory].resize(info->allocationSize);
    return result;
}

VKAPI_ATTR void VKAPI_CALL free_memory(VkDevice device, VkDeviceMemory memory,
    const VkAllocationCallbacks* allocator) {
    assert(g_memory.erase(memory) == 1);
    destroy_object(device, memory, allocator);
}

VKAPI_ATTR VkResult VKAPI_CALL create_cache(VkDevice device,
    const VkPipelineCacheCreateInfo* info, const VkAllocationCallbacks* allocator,
    VkPipelineCache* cache) {
    ++g_cache_creations;
    return create_object(device, info, allocator, cache);
}

VKAPI_ATTR VkResult VKAPI_CALL create_pipeline(VkDevice device, VkPipelineCache cache,
    uint32_t count, const VkGraphicsPipelineCreateInfo* info,
    const VkAllocationCallbacks* allocator, VkPipeline* pipeline) {
    assert(std::this_thread::get_id() != g_present_thread);
    assert(cache != VK_NULL_HANDLE && count == 1);
    if (g_block_pipeline) {
        g_init_entered.set_value();
        g_release.wait();
    }
    return create_object(device, info, allocator, pipeline);
}

void enable_pipeline(pblayer::DeviceContext& c) {
    c.physical_device_memory_properties.memoryTypeCount = 1;
    c.physical_device_memory_properties.memoryTypes[0].propertyFlags =
        VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT | VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
        | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    c.create_pipeline_cache = create_cache;
    c.destroy_pipeline_cache = destroy_object<VkPipelineCache>;
    c.create_shader_module = create_object<VkShaderModule, VkShaderModuleCreateInfo>;
    c.destroy_shader_module = destroy_object<VkShaderModule>;
    c.create_pipeline_layout = create_object<VkPipelineLayout, VkPipelineLayoutCreateInfo>;
    c.destroy_pipeline_layout = destroy_object<VkPipelineLayout>;
    c.create_graphics_pipelines = create_pipeline;
    c.destroy_pipeline = destroy_object<VkPipeline>;
    c.create_buffer = create_object<VkBuffer, VkBufferCreateInfo>;
    c.destroy_buffer = destroy_object<VkBuffer>;
    c.get_buffer_memory_requirements = memory_requirements<VkBuffer>;
    c.allocate_memory = allocate_memory;
    c.free_memory = free_memory;
    c.bind_buffer_memory = [](VkDevice, VkBuffer, VkDeviceMemory, VkDeviceSize) { return VK_SUCCESS; };
    c.map_memory = [](VkDevice, VkDeviceMemory m, VkDeviceSize, VkDeviceSize, VkMemoryMapFlags, void** p) {
        *p = g_memory.at(m).data(); return VK_SUCCESS;
    };
    c.unmap_memory = [](VkDevice, VkDeviceMemory) {};
    c.cmd_bind_pipeline = [](VkCommandBuffer, VkPipelineBindPoint, VkPipeline) {};
    c.cmd_bind_vertex_buffers = [](VkCommandBuffer, uint32_t, uint32_t, const VkBuffer*, const VkDeviceSize*) {};
    c.cmd_set_viewport = [](VkCommandBuffer, uint32_t, uint32_t, const VkViewport*) {};
    c.cmd_set_scissor = [](VkCommandBuffer, uint32_t, uint32_t, const VkRect2D*) {};
    c.cmd_draw = [](VkCommandBuffer, uint32_t, uint32_t, uint32_t, uint32_t) {};
    c.create_image = create_object<VkImage, VkImageCreateInfo>;
    c.destroy_image = destroy_object<VkImage>;
    c.get_image_memory_requirements = memory_requirements<VkImage>;
    c.bind_image_memory = [](VkDevice, VkImage, VkDeviceMemory, VkDeviceSize) { return VK_SUCCESS; };
    c.create_sampler = create_object<VkSampler, VkSamplerCreateInfo>;
    c.destroy_sampler = destroy_object<VkSampler>;
    c.create_descriptor_set_layout = create_object<VkDescriptorSetLayout, VkDescriptorSetLayoutCreateInfo>;
    c.destroy_descriptor_set_layout = destroy_object<VkDescriptorSetLayout>;
    c.create_descriptor_pool = create_object<VkDescriptorPool, VkDescriptorPoolCreateInfo>;
    c.destroy_descriptor_pool = destroy_object<VkDescriptorPool>;
    c.allocate_descriptor_sets = [](VkDevice, const VkDescriptorSetAllocateInfo*, VkDescriptorSet* set) {
        *set = fake_handle<VkDescriptorSet>(); return VK_SUCCESS;
    };
    c.update_descriptor_sets = [](VkDevice, uint32_t, const VkWriteDescriptorSet*, uint32_t, const VkCopyDescriptorSet*) {};
    c.cmd_bind_descriptor_sets = [](VkCommandBuffer, VkPipelineBindPoint, VkPipelineLayout,
        uint32_t, uint32_t, const VkDescriptorSet*, uint32_t, const uint32_t*) {};
    c.cmd_copy_buffer_to_image = [](VkCommandBuffer, VkBuffer, VkImage, VkImageLayout, uint32_t, const VkBufferImageCopy*) {};
    c.cmd_pipeline_barrier = [](VkCommandBuffer, VkPipelineStageFlags, VkPipelineStageFlags,
        VkDependencyFlags, uint32_t, const VkMemoryBarrier*, uint32_t,
        const VkBufferMemoryBarrier*, uint32_t, const VkImageMemoryBarrier*) {};
}

pblayer::SwapchainContext make_swapchain_context() {
    pblayer::SwapchainContext context{};
    context.image_format = VK_FORMAT_B8G8R8A8_UNORM;
    context.image_extent = {1920, 1080};
    context.image_array_layers = 1;
    context.image_usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    return context;
}

void test_async(pblayer::DeviceContext& device, const std::string& mode) {
    const bool pipeline_mode = mode == "pipeline" || mode == "pipeline_destroy";
    if (pipeline_mode) enable_pipeline(device);
    auto context = make_swapchain_context();
    const auto swapchain = fake_handle<VkSwapchainKHR>();
    const auto queue = fake_handle<VkQueue>();
    const auto semaphore = fake_handle<VkSemaphore>();
    uint32_t image = 0;
    VkPresentInfoKHR present{};
    present.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR;
    present.swapchainCount = 1;
    present.pSwapchains = &swapchain;
    present.pImageIndices = &image;
    present.waitSemaphoreCount = 1;
    present.pWaitSemaphores = &semaphore;
    pblayer::QueueContext queue_context{};
    queue_context.queue_family_index = 0;
    queue_context.queue_flags = VK_QUEUE_GRAPHICS_BIT;
    auto text = std::make_shared<pblayer::OverlayReadout>();
    text->text = "hotspot 79 C";
    text->rows = {{pblayer::OverlayRowKind::Value, "hotspot", {{"79 C"}}}};
    const auto submit = [&] {
        return pblayer::submit_overlay_locked(device, swapchain, context,
            queue_context, queue, image, &present, text, {});
    };
    if (mode == "solid") pblayer::g_overlay_blended_renderer = false;
    g_block_init = !pipeline_mode;
    g_block_pipeline = pipeline_mode;
    assert(submit() == VK_NULL_HANDLE);
    assert(g_init_entered.get_future().wait_for(std::chrono::seconds(2))
        == std::future_status::ready);
    for (int i = 0; i < 20; ++i) {
        assert(submit() == VK_NULL_HANDLE);
        assert(present.pWaitSemaphores == &semaphore);
    }
    if (mode == "destroy" || mode == "pipeline_destroy") {
        auto teardown = std::async(std::launch::async, [&] {
            pblayer::destroy_overlay_resources(device, context);
        });
        assert(teardown.wait_for(std::chrono::milliseconds(10)) == std::future_status::timeout);
        g_release_init.set_value();
        teardown.get();
    } else {
        g_release_init.set_value();
        context.overlay_preparation.wait();
        const auto signal = submit();
        if (mode == "failure") {
            assert(signal == VK_NULL_HANDLE && !context.overlay.ready);
            assert(context.overlay.attempted && !context.overlay_preparation.valid());
            assert(submit() == VK_NULL_HANDLE && !context.overlay_preparation.valid());
        } else {
            assert(signal != VK_NULL_HANDLE && context.overlay.ready);
            assert(context.overlay.submit_pending[0]);
            if (pipeline_mode) {
                assert(context.overlay.blended_ready && context.overlay.atlas_uploaded);
                auto* mapping = static_cast<uint8_t*>(context.overlay.vertex_mappings[0]);
                const auto first_byte = mapping[0];
                mapping[0] ^= 0xff;
                assert(submit() != VK_NULL_HANDLE && mapping[0] != first_byte);
                // A reused image skips the upload; another image still gets its own copy.
                image = 1;
                assert(submit() != VK_NULL_HANDLE);
                assert(context.overlay.vertex_generations[0] == context.overlay.vertex_generations[1]);
                image = 0;
                text = std::make_shared<pblayer::OverlayReadout>(*text);
                text->rows[0].cells[0].text = "83 C";
                assert(submit() != VK_NULL_HANDLE && mapping[0] == first_byte);
                assert(context.overlay.vertex_generations[0] > context.overlay.vertex_generations[1]);
            }
            if (mode == "solid") {
                assert(!context.overlay.blended_attempted);
                pblayer::g_overlay_blended_renderer = true;
                assert(submit() != VK_NULL_HANDLE);
                assert(context.pipeline_preparation.valid());
                context.pipeline_preparation.wait();
                // This stub driver has no blended pipeline functions: fallback stays usable.
                assert(submit() != VK_NULL_HANDLE);
                assert(context.overlay.blended_attempted && !context.overlay.blended_ready);
            }
        }
        pblayer::destroy_overlay_resources(device, context);
    }
    if (pipeline_mode) {
        assert(g_cache_creations == 1 && g_live_objects.size() == 1 && g_memory.empty());
        if (mode == "pipeline") {
            // Swapchain recreation reuses the device's cache and frees its own resources.
            g_block_pipeline = false;
            g_stamped = 0;
            g_buffers.clear();
            context = make_swapchain_context();
            assert(submit() == VK_NULL_HANDLE);
            context.overlay_preparation.wait();
            assert(submit() != VK_NULL_HANDLE && g_cache_creations == 1);
            pblayer::destroy_overlay_resources(device, context);
        }
        pblayer::g_devices[g_device] = device;
        pblayer::layer_destroy_device(g_device, nullptr);
    }
    assert(g_live_objects.empty() && g_memory.empty());
    std::puts("async initialization and cleanup passed");
}

}  // namespace

int main(int argc, char** argv) {
    assert(argc == 3 || argc == 4);
    g_fail_at = std::atoi(argv[1]);
    pblayer::DeviceContext device_context = make_device_context(std::atoi(argv[2]) != 0);
    if (argc == 4) {
        test_async(device_context, argv[3]);
        return 0;
    }
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
