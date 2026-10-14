// PenguinBurner Vulkan latency layer -- shared internal interface.
//
// Implementation is split across translation units (latency_state.cpp,
// overlay_text.cpp, overlay_render.cpp, telemetry_events.cpp,
// marker_timing.cpp, latency_layer.cpp); they share state through the
// pblayer namespace. The whole namespace is hidden in the .so
// (-fvisibility=hidden); only the three exported vk* entry points are public.
#ifndef PENGUINBURNER_LATENCY_LAYER_INTERNAL_H
#define PENGUINBURNER_LATENCY_LAYER_INTERNAL_H

#define VK_NO_PROTOTYPES

#include <vulkan/vk_layer.h>
#include <vulkan/vulkan.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <fstream>
#include <future>
#include <cctype>
#include <cinttypes>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

namespace pblayer {

constexpr const char* kLayerName = "VK_LAYER_PENGUINBURNER_latency";
constexpr const char* kSocketEnv = "PENGUIN_BURNER_LATENCY_SOCKET";
constexpr const char* kMasterEnableEnv = "PENGUIN_BURNER";
constexpr const char* kEnableEnv = "PENGUIN_BURNER_LATENCY_LAYER";
constexpr const char* kOverlayEnableEnv = "PENGUIN_BURNER_OVERLAY";
constexpr const char* kOverlayEnableEnvAlias = "PB_OVERLAY";
constexpr const char* kOverlayStateEnv = "PENGUIN_BURNER_OVERLAY_STATE";
constexpr const char* kOverlayOverrideEnv = "PENGUIN_BURNER_OVERLAY_OVERRIDE";
constexpr const char* kOverlayConfigEnv = "PENGUIN_BURNER_OVERLAY_CONFIG";
constexpr const char* kTelemetrySessionEnv =
    "PENGUIN_BURNER_TELEMETRY_SESSION";
constexpr const char* kRecoveryResetEnv = "PENGUIN_BURNER_LATENCY_RECOVERY_RESET";
constexpr const char* kDebugFlowEnv = "PENGUIN_BURNER_LATENCY_DEBUG_FLOW";
constexpr const char* kQueryTimingsEnv = "PENGUIN_BURNER_LATENCY_QUERY_TIMINGS";
// Opt-in, default off: measure the present->scanout tail via VK_KHR_present_wait.
// Read-only -- only ever waits on present IDs the app/DXVK already supplied; it
// never injects present IDs or enables extensions the app did not request.
constexpr const char* kDisplayLatencyEnv = "PENGUIN_BURNER_LATENCY_DISPLAY";
// Narrow opt-in: when the app enabled VK_KHR_present_id but does not stamp a
// present ID itself, prepend our own VkPresentIdKHR so vkWaitForPresentKHR has
// something to key on. This deliberately mutates only the present chain (never
// device creation) and only when the app has not already chained a
// VkPresentIdKHR. Requires PENGUIN_BURNER_LATENCY_DISPLAY too.
constexpr const char* kInjectPresentIdEnv =
    "PENGUIN_BURNER_LATENCY_INJECT_PRESENT_ID";
constexpr uint64_t kStaleRecoveryDuplicateThreshold = 240;
constexpr uint64_t kStaleRecoveryDuplicateInterval = 600;

template <typename Handle>
uint64_t handle_to_u64(Handle handle) {
    if constexpr (std::is_pointer_v<Handle>) {
        return static_cast<uint64_t>(reinterpret_cast<uintptr_t>(handle));
    } else {
        return static_cast<uint64_t>(handle);
    }
}

// The loader hands a layer several entries of the same chain struct, told
// apart by ``function``: VK_LAYER_LINK_INFO carries the next layer's proc
// addresses, and VK_LOADER_DATA_CALLBACK carries the callback that stamps
// loader data onto dispatchable objects the layer creates itself.
template <typename CreateInfo>
auto find_layer_chain_entry(
    CreateInfo* create_info,
    VkLayerFunction function = VK_LAYER_LINK_INFO) {
    using LayerInfo = std::conditional_t<
        std::is_same_v<std::remove_const_t<CreateInfo>, VkInstanceCreateInfo>,
        VkLayerInstanceCreateInfo,
        VkLayerDeviceCreateInfo>;

    auto* item = reinterpret_cast<LayerInfo*>(
        const_cast<void*>(static_cast<const void*>(create_info->pNext)));
    while (item) {
        if (item->sType == std::conditional_t<
                    std::is_same_v<std::remove_const_t<CreateInfo>, VkInstanceCreateInfo>,
                    std::integral_constant<VkStructureType, VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO>,
                    std::integral_constant<VkStructureType, VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO>>::value
            && item->function == function) {
            return item;
        }
        item = reinterpret_cast<LayerInfo*>(const_cast<void*>(item->pNext));
    }
    return static_cast<LayerInfo*>(nullptr);
}

class TelemetrySocket {
public:
    void send_line(const char* line, size_t length) {
        init_once();
        if (fd_ < 0 || paths_.empty()) {
            return;
        }

        for (const std::string& path : paths_) {
            sockaddr_un address{};
            address.sun_family = AF_UNIX;
            if (path.size() >= sizeof(address.sun_path)) {
                continue;
            }
            std::memcpy(address.sun_path, path.c_str(), path.size() + 1);

            ssize_t result = ::sendto(
                fd_,
                line,
                length,
                MSG_DONTWAIT | MSG_NOSIGNAL,
                reinterpret_cast<sockaddr*>(&address),
                static_cast<socklen_t>(
                    offsetof(sockaddr_un, sun_path) + path.size() + 1));
            if (result >= 0) {
                return;
            }
            if (errno == ENOENT || errno == ECONNREFUSED) {
                missed_receiver_count_.fetch_add(1, std::memory_order_relaxed);
            }
        }
    }

private:
    void add_path(std::string path) {
        if (path.empty()) {
            return;
        }
        if (std::find(paths_.begin(), paths_.end(), path) == paths_.end()) {
            paths_.push_back(std::move(path));
        }
    }

    void init_once() {
        std::call_once(init_flag_, [&]() {
            const char* explicit_path = std::getenv(kSocketEnv);
            if (explicit_path && explicit_path[0]) {
                add_path(explicit_path);
            }
            if (const char* runtime_dir = std::getenv("XDG_RUNTIME_DIR")) {
                if (runtime_dir[0]) {
                    add_path(
                        std::string(runtime_dir)
                        + "/penguin-burner/latency.sock");
                }
            }
            if (const char* home = std::getenv("HOME")) {
                if (home[0]) {
                    add_path(
                        std::string(home)
                        + "/.cache/penguin-burner/latency.sock");
                }
            }
            if (paths_.empty()) {
                char fallback[128]{};
                std::snprintf(
                    fallback,
                    sizeof(fallback),
                    "/tmp/penguin-burner-latency-%ld.sock",
                    static_cast<long>(::getuid()));
                add_path(fallback);
            }

            fd_ = ::socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        });
    }

    std::once_flag init_flag_{};
    int fd_ = -1;
    std::vector<std::string> paths_{};
    std::atomic<uint64_t> missed_receiver_count_{0};
};

struct DeviceTelemetryFlags {
    bool advertised = false;
    bool requested = false;
    bool functions_available = false;
    uint64_t marker_count = 0;
};

struct FlowSnapshot {
    bool advertised = false;
    bool requested = false;
    bool functions_available = false;
    bool present_wait_advertised = false;
    bool present_wait_requested = false;
    bool present_id_advertised = false;
    bool present_id_requested = false;
    bool swapchain_latency_mode = false;
    uint64_t marker_count = 0;
    uint64_t live_swapchain_count = 0;
    uint64_t present_count = 0;
    uint64_t last_vulkan_present_id = 0;
    uint64_t latest_marker_present_id = 0;
    uint64_t last_input_sample_present_id = 0;
    uint64_t last_simulation_present_id = 0;
    uint64_t last_render_submit_present_id = 0;
    uint64_t last_present_marker_present_id = 0;
    uint64_t last_oob_render_submit_present_id = 0;
    uint64_t last_oob_present_present_id = 0;
    uint64_t last_driver_report_present_id = 0;
    uint64_t driver_report_duplicate_count = 0;
};

struct MarkerTiming {
    uint32_t bits = 0;
    uint64_t input_sample_us = 0;
    uint64_t sim_start_us = 0;
    uint64_t sim_end_us = 0;
    uint64_t render_submit_start_us = 0;
    uint64_t render_submit_end_us = 0;
    uint64_t present_start_us = 0;
    uint64_t present_end_us = 0;
    uint64_t oob_present_end_us = 0;
    int64_t present_marker_lag_us = 0;
    uint32_t emitted_metric_bits = 0;
};

struct MarkerCounts {
    uint64_t simulation_start = 0;
    uint64_t simulation_end = 0;
    uint64_t render_submit_start = 0;
    uint64_t render_submit_end = 0;
    uint64_t present_start = 0;
    uint64_t present_end = 0;
    uint64_t input_sample = 0;
    uint64_t out_of_band_render_submit_start = 0;
    uint64_t out_of_band_render_submit_end = 0;
    uint64_t out_of_band_present_start = 0;
    uint64_t out_of_band_present_end = 0;
};

struct QueueContext {
    VkDevice device = VK_NULL_HANDLE;
    uint32_t queue_family_index = UINT32_MAX;
    uint32_t queue_index = 0;
    VkQueueFlags queue_flags = 0;
    uint64_t present_count = 0;
    uint64_t out_of_band_notify_count = 0;
};

struct InstanceContext {
    PFN_vkGetInstanceProcAddr get_instance_proc_addr = nullptr;
    PFN_vkDestroyInstance destroy_instance = nullptr;
    PFN_vkCreateDevice create_device = nullptr;
    PFN_vkEnumeratePhysicalDevices enumerate_physical_devices = nullptr;
    PFN_vkEnumerateDeviceExtensionProperties enumerate_device_extension_properties =
        nullptr;
    PFN_vkGetPhysicalDeviceProperties get_physical_device_properties = nullptr;
    PFN_vkGetPhysicalDeviceMemoryProperties
        get_physical_device_memory_properties = nullptr;
    PFN_vkGetPhysicalDeviceQueueFamilyProperties
        get_physical_device_queue_family_properties = nullptr;
    std::string application_name = "unknown";
    std::string engine_name = "unknown";
};

struct OverlayPipelineCache {
    std::mutex mutex;
    VkPipelineCache handle = VK_NULL_HANDLE;
    bool attempted = false;
};

struct DeviceContext {
    VkDevice device = VK_NULL_HANDLE;
    VkPhysicalDevice physical_device = VK_NULL_HANDLE;
    PFN_vkGetDeviceProcAddr get_device_proc_addr = nullptr;
    // Stamps loader data onto command buffers this layer allocates for the
    // overlay. Without it a layer above us is handed a dispatchable handle it
    // cannot look up.
    PFN_vkSetDeviceLoaderData set_device_loader_data = nullptr;
    PFN_vkDestroyDevice destroy_device = nullptr;
    PFN_vkDeviceWaitIdle device_wait_idle = nullptr;
    PFN_vkGetDeviceQueue get_device_queue = nullptr;
    PFN_vkGetDeviceQueue2 get_device_queue2 = nullptr;
    PFN_vkCreateSwapchainKHR create_swapchain_khr = nullptr;
    PFN_vkDestroySwapchainKHR destroy_swapchain_khr = nullptr;
    PFN_vkGetSwapchainImagesKHR get_swapchain_images_khr = nullptr;
    PFN_vkQueuePresentKHR queue_present_khr = nullptr;
    PFN_vkQueueSubmit queue_submit = nullptr;
    PFN_vkCreateImageView create_image_view = nullptr;
    PFN_vkDestroyImageView destroy_image_view = nullptr;
    PFN_vkCreateRenderPass create_render_pass = nullptr;
    PFN_vkDestroyRenderPass destroy_render_pass = nullptr;
    PFN_vkCreateFramebuffer create_framebuffer = nullptr;
    PFN_vkDestroyFramebuffer destroy_framebuffer = nullptr;
    PFN_vkCreateCommandPool create_command_pool = nullptr;
    PFN_vkDestroyCommandPool destroy_command_pool = nullptr;
    PFN_vkAllocateCommandBuffers allocate_command_buffers = nullptr;
    PFN_vkResetCommandBuffer reset_command_buffer = nullptr;
    PFN_vkBeginCommandBuffer begin_command_buffer = nullptr;
    PFN_vkEndCommandBuffer end_command_buffer = nullptr;
    PFN_vkCmdBeginRenderPass cmd_begin_render_pass = nullptr;
    PFN_vkCmdEndRenderPass cmd_end_render_pass = nullptr;
    PFN_vkCmdClearAttachments cmd_clear_attachments = nullptr;
    PFN_vkCreateShaderModule create_shader_module = nullptr;
    PFN_vkDestroyShaderModule destroy_shader_module = nullptr;
    PFN_vkCreatePipelineLayout create_pipeline_layout = nullptr;
    PFN_vkDestroyPipelineLayout destroy_pipeline_layout = nullptr;
    PFN_vkCreateGraphicsPipelines create_graphics_pipelines = nullptr;
    PFN_vkCreatePipelineCache create_pipeline_cache = nullptr;
    PFN_vkDestroyPipelineCache destroy_pipeline_cache = nullptr;
    std::shared_ptr<OverlayPipelineCache> overlay_pipeline_cache;
    PFN_vkDestroyPipeline destroy_pipeline = nullptr;
    PFN_vkCreateBuffer create_buffer = nullptr;
    PFN_vkDestroyBuffer destroy_buffer = nullptr;
    PFN_vkGetBufferMemoryRequirements get_buffer_memory_requirements = nullptr;
    PFN_vkAllocateMemory allocate_memory = nullptr;
    PFN_vkFreeMemory free_memory = nullptr;
    PFN_vkBindBufferMemory bind_buffer_memory = nullptr;
    PFN_vkMapMemory map_memory = nullptr;
    PFN_vkUnmapMemory unmap_memory = nullptr;
    PFN_vkCmdBindPipeline cmd_bind_pipeline = nullptr;
    PFN_vkCmdBindVertexBuffers cmd_bind_vertex_buffers = nullptr;
    PFN_vkCmdSetViewport cmd_set_viewport = nullptr;
    PFN_vkCmdSetScissor cmd_set_scissor = nullptr;
    PFN_vkCmdDraw cmd_draw = nullptr;
    PFN_vkCreateImage create_image = nullptr;
    PFN_vkDestroyImage destroy_image = nullptr;
    PFN_vkGetImageMemoryRequirements get_image_memory_requirements = nullptr;
    PFN_vkBindImageMemory bind_image_memory = nullptr;
    PFN_vkCreateSampler create_sampler = nullptr;
    PFN_vkDestroySampler destroy_sampler = nullptr;
    PFN_vkCreateDescriptorSetLayout create_descriptor_set_layout = nullptr;
    PFN_vkDestroyDescriptorSetLayout destroy_descriptor_set_layout = nullptr;
    PFN_vkCreateDescriptorPool create_descriptor_pool = nullptr;
    PFN_vkDestroyDescriptorPool destroy_descriptor_pool = nullptr;
    PFN_vkAllocateDescriptorSets allocate_descriptor_sets = nullptr;
    PFN_vkUpdateDescriptorSets update_descriptor_sets = nullptr;
    PFN_vkCmdBindDescriptorSets cmd_bind_descriptor_sets = nullptr;
    PFN_vkCmdCopyBufferToImage cmd_copy_buffer_to_image = nullptr;
    PFN_vkCmdPipelineBarrier cmd_pipeline_barrier = nullptr;
    PFN_vkCreateSemaphore create_semaphore = nullptr;
    PFN_vkDestroySemaphore destroy_semaphore = nullptr;
    PFN_vkCreateFence create_fence = nullptr;
    PFN_vkDestroyFence destroy_fence = nullptr;
    PFN_vkGetFenceStatus get_fence_status = nullptr;
    PFN_vkResetFences reset_fences = nullptr;
    PFN_vkWaitForFences wait_for_fences = nullptr;
    PFN_vkSetLatencySleepModeNV set_latency_sleep_mode_nv = nullptr;
    PFN_vkLatencySleepNV latency_sleep_nv = nullptr;
    PFN_vkSetLatencyMarkerNV set_latency_marker_nv = nullptr;
    PFN_vkGetLatencyTimingsNV get_latency_timings_nv = nullptr;
    PFN_vkQueueNotifyOutOfBandNV queue_notify_out_of_band_nv = nullptr;
    PFN_vkWaitForPresentKHR wait_for_present_khr = nullptr;
    VkPhysicalDeviceProperties physical_device_properties{};
    VkPhysicalDeviceMemoryProperties physical_device_memory_properties{};
    std::vector<VkQueueFamilyProperties> queue_family_properties;
    bool low_latency_extension_advertised = false;
    bool low_latency_extension_requested = false;
    bool low_latency_functions_available = false;
    // VK_KHR_present_wait / VK_KHR_present_id availability, recorded read-only at
    // device creation. Display-latency capture only activates when the app itself
    // enabled both (the *_requested flags).
    bool present_wait_advertised = false;
    bool present_wait_requested = false;
    bool present_id_advertised = false;
    bool present_id_requested = false;
    uint64_t latest_marker_present_id = 0;
    uint64_t marker_count = 0;
    bool has_latency_sleep_mode_info = false;
    VkLatencySleepModeInfoNV latency_sleep_mode_info{};
    uint64_t latency_sleep_mode_set_count = 0;
    uint64_t latency_sleep_mode_reapply_count = 0;
    uint64_t latency_sleep_count = 0;
    MarkerCounts marker_counts{};
    uint64_t last_input_sample_present_id = 0;
    uint64_t last_simulation_present_id = 0;
    uint64_t last_render_submit_present_id = 0;
    uint64_t last_present_marker_present_id = 0;
    uint64_t last_oob_render_submit_present_id = 0;
    uint64_t last_oob_present_present_id = 0;
    std::unordered_map<uint64_t, uint32_t> marker_bits_by_present_id;
    std::unordered_map<uint64_t, MarkerTiming> marker_timings_by_present_id;
    std::vector<uint64_t> marker_order;
};

struct BaseMarkerState {
    uint64_t frame_id = 0;
    uint64_t timestamp_us = 0;
};

// What the layer can answer about the game's device and swapchain without
// asking the daemon anything: the driver handed all of it over at instance and
// swapchain creation. Built once per swapchain, since none of it changes.
struct OverlayDeviceFacts {
    std::string gpu_name;
    std::string driver_version;
    std::string resolution;
    std::string present_mode;
};

// One blended quad corner. Positions are already in clip space: the CPU knows
// the swapchain extent when it builds them, so the shaders need no uniform and
// the pipeline needs no descriptor set.
struct OverlayVertex {
    float x = 0.0f;
    float y = 0.0f;
    float r = 0.0f;
    float g = 0.0f;
    float b = 0.0f;
    float a = 0.0f;
    // Atlas coordinates for a glyph; a negative u marks a solid quad, so the
    // panel and the text share one pipeline.
    float u = -1.0f;
    float v = -1.0f;
};

// Which face the overlay draws its text in. Pixel is the original 5x8 bitmap
// and the only one the clear-rect renderer can draw at all; the other two come
// from the signed-distance atlas and need the pipeline.
// Defined in the generated overlay_font.h; only ever passed by pointer here.
struct OverlayFontAtlas;

enum class OverlayFont : uint32_t {
    Pixel = 0,
    Mono = 1,
    Sans = 2,
};

// Sans, matching DEFAULT_OVERLAY_FONT in overlay/config.py. Every fixed-pitch
// face draws a slashed or dotted zero, which is the first mark to close up once
// the text is only a few pixels tall; the proportional face carries tabular
// figures, so its digits still hold a column.
constexpr OverlayFont kDefaultOverlayFont = OverlayFont::Sans;

// A horizontal slice of the panel. A rounded rectangle is built from
// non-overlapping bands rather than an overlapping stack, because two
// translucent quads on top of each other blend twice and show the seam.
struct OverlayRectBand {
    int32_t x = 0;
    int32_t y = 0;
    uint32_t width = 0;
    uint32_t height = 0;
};

// Room for the busiest readout plus its graph. Anything past this is dropped
// rather than grown mid-present.
constexpr uint32_t kOverlayMaxQuads = 8192;
constexpr uint32_t kOverlayVerticesPerQuad = 6;

struct OverlayReadout;

struct OverlayClearRect {
    int32_t x, y;
    uint32_t width, height;
    VkClearColorValue color;
};

struct OverlayGraphPlacement {
    float x = 0, y = 0, width = 0, height = 0, scale = 1;
    bool area = false;
    std::size_t vertex_offset = 0;
};

struct OverlayGeometryCache {
    std::shared_ptr<const OverlayReadout> readout;
    VkExtent2D extent{};
    double scale = 0, opacity = 0;
    uint32_t position = 0;
    OverlayFont font = OverlayFont::Pixel;
    bool blended = false, graph_visible = false;
    std::vector<OverlayVertex> vertices;
    std::vector<OverlayVertex> graph_tail;
    std::vector<OverlayClearRect> clear_rects;
    std::size_t static_count = 0;
    OverlayGraphPlacement graph;
    uint64_t generation = 0;
};

struct OverlayPipelineResources {
    // Blended-quad pipeline. Every one of these may be absent: when creation
    // fails at any step the overlay keeps drawing with cleared rectangles, so
    // a driver that cannot give us a pipeline costs the look, not the HUD.
    bool blended_ready = false;
    VkPipelineLayout pipeline_layout = VK_NULL_HANDLE;
    VkPipeline pipeline = VK_NULL_HANDLE;
    std::vector<VkBuffer> vertex_buffers;
    std::vector<VkDeviceMemory> vertex_memories;
    std::vector<void*> vertex_mappings;
    // Signed-distance font atlas. Absent means the overlay falls back to the
    // bitmap face, exactly as it does when the whole pipeline is absent.
    bool atlas_ready = false;
    bool atlas_uploaded = false;
    VkImage atlas_image = VK_NULL_HANDLE;
    VkDeviceMemory atlas_memory = VK_NULL_HANDLE;
    VkImageView atlas_view = VK_NULL_HANDLE;
    VkSampler atlas_sampler = VK_NULL_HANDLE;
    VkDescriptorSetLayout descriptor_set_layout = VK_NULL_HANDLE;
    VkDescriptorPool descriptor_pool = VK_NULL_HANDLE;
    VkDescriptorSet descriptor_set = VK_NULL_HANDLE;
    VkBuffer atlas_staging = VK_NULL_HANDLE;
    VkDeviceMemory atlas_staging_memory = VK_NULL_HANDLE;
};

struct OverlayRenderResources : OverlayPipelineResources {
    bool attempted = false;
    bool ready = false;
    uint32_t queue_family_index = UINT32_MAX;
    VkRenderPass render_pass = VK_NULL_HANDLE;
    VkCommandPool command_pool = VK_NULL_HANDLE;
    std::vector<VkImage> images;
    std::vector<VkImageView> image_views;
    std::vector<VkFramebuffer> framebuffers;
    std::vector<VkCommandBuffer> command_buffers;
    std::vector<VkSemaphore> signal_semaphores;
    std::vector<VkFence> submit_fences;
    std::vector<uint8_t> submit_pending;
    bool blended_attempted = false;
    OverlayGeometryCache geometry;
    std::vector<uint64_t> vertex_generations;
    std::vector<VkPipelineStageFlags> wait_stages;
};

struct SwapchainContext {
    VkDevice device = VK_NULL_HANDLE;
    bool swapchain_latency_mode = false;
    VkFormat image_format = VK_FORMAT_UNDEFINED;
    VkExtent2D image_extent{};
    uint32_t image_array_layers = 1;
    VkImageUsageFlags image_usage = 0;
    VkPresentModeKHR present_mode = VK_PRESENT_MODE_FIFO_KHR;
    OverlayDeviceFacts device_facts{};
    uint64_t last_gpu_render_end_us = 0;
    // Monotonic counter for present IDs we inject (present-id injection path).
    uint64_t last_injected_present_id = 0;
    uint64_t timing_sample_count = 0;
    uint64_t driver_timing_sample_count = 0;
    uint64_t last_driver_report_present_id = 0;
    uint64_t last_emitted_present_id = 0;
    uint64_t duplicate_driver_report_count = 0;
    uint64_t present_count = 0;
    uint64_t last_vulkan_present_id = 0;
    uint64_t last_present_us = 0;
    uint64_t timing_unavailable_count = 0;
    uint64_t timing_empty_count = 0;
    uint64_t latency_recovery_attempt_count = 0;
    uint64_t last_latency_recovery_duplicate_count = 0;
    std::array<uint64_t, 120> present_frametimes{};
    uint32_t present_frametime_index = 0;
    uint32_t present_frametime_count = 0;
    // Whole-session frametime accumulators. The ring above only spans its last
    // 120 presents -- about a second at 120 FPS -- so session min/avg/max need
    // their own running totals rather than a longer buffer.
    uint64_t session_frametime_count = 0;
    uint64_t session_frametime_sum_us = 0;
    uint64_t session_frametime_min_us = 0;
    uint64_t session_frametime_max_us = 0;
    BaseMarkerState out_of_band_present_start{};
    BaseMarkerState present_start{};
    BaseMarkerState render_submit_start{};
    OverlayRenderResources overlay{};
    // Only teardown waits for these workers. Present polls and skips drawing
    // until resources are ready; workers never access the live context.
    std::future<OverlayRenderResources> overlay_preparation;
    std::future<OverlayPipelineResources> pipeline_preparation;
    uint64_t overlay_readout_time_us = 0;
    std::shared_ptr<const OverlayReadout> overlay_readout;
};

// What the layer measured about presentation, as opposed to what the daemon
// published about the GPU. None of this can come from overlay-state.txt: the
// frametimes only exist inside the game process.
struct OverlayFrameStats {
    // Median over the ring; the headline FPS the overlay has always shown.
    uint64_t median_fps = 0;
    uint64_t median_us = 0;
    // FPS at the mean frametime of the ring's worst 1% of frames.
    uint64_t low_1pct_fps = 0;
    // Frametime 97% of the ring's frames came in under.
    uint64_t p97_frametime_us = 0;
    uint64_t session_min_fps = 0;
    uint64_t session_avg_fps = 0;
    uint64_t session_max_fps = 0;
    uint32_t sample_count = 0;
};

// A chronological snapshot of the present ring, taken under the swapchain lock
// so the draw path never reads the ring while a present updates it.
struct OverlayGraph {
    std::array<uint64_t, 120> samples{};
    uint32_t count = 0;
    // The rule drawn across the bars. The window's own median, not a target
    // frame rate: the layer has no way to learn the target, and "how far do
    // the spikes sit above typical" is the question the graph answers anyway.
    uint64_t median_us = 0;
};

// How urgently one cell of the readout should read. Colour is a layer-side
// concern only: the desktop preview shows the same text in one colour, so the
// two stay in agreement on what is written, not on how it is painted.
enum class OverlayCellLevel : uint8_t {
    Normal = 0,
    Warn = 1,
    Alert = 2,
    // Labels and the footer sit back from the values they belong to.
    Dim = 3,
    // A value sitting where you want it -- a cool card, a real undervolt.
    Good = 4,
    // The tier actually running, which is the one thing only this tool knows.
    Tier = 5,
};

// What a row is, rather than what it holds. The designed layout needs more
// than a grid: a headline frame rate, rules between groups, a quieter footer.
enum class OverlayRowKind : uint8_t {
    Value = 0,
    Hero = 1,
    Rule = 2,
    Foot = 3,
};

// How the readout is arranged. Mirrors OVERLAY_LAYOUTS in overlay/config.py.
enum class OverlayLayout : uint32_t {
    Hud = 0,
    Table = 1,
};

constexpr OverlayLayout kDefaultOverlayLayout = OverlayLayout::Hud;

// A run of characters in the readout that is not drawn in the default colour.
struct OverlayTextSpan {
    uint32_t line = 0;
    uint32_t column = 0;
    uint32_t length = 0;
    OverlayCellLevel level = OverlayCellLevel::Normal;
};

// One cell of the readout, before any padding.
struct OverlayReadoutCell {
    std::string text;
    OverlayCellLevel level = OverlayCellLevel::Normal;
};

struct OverlayReadoutRow {
    OverlayRowKind kind = OverlayRowKind::Value;
    std::string label;
    std::vector<OverlayReadoutCell> cells;
};

struct OverlayReadout {
    // The padded form. This is what the desktop preview compares against and
    // what the parity harness diffs, so it stays byte for byte what it was.
    std::string text;
    std::vector<OverlayTextSpan> highlights;
    // The same content unpadded. The renderer lays columns out from measured
    // widths instead, because space padding only lines up in a fixed-pitch
    // face and one of the three is not.
    std::vector<OverlayReadoutRow> rows;
};

struct OverlayGpuState {
    bool available = false;
    bool framegen_active = false;
    std::string present_fps;
    std::string framegen_fps;
    std::string latency_ms;
    std::string display_latency_ms;
    std::string clock_mhz;
    std::string voltage_mv;
    std::string power_w;
    std::string gpu_util_pct;
    std::string cpu_util_pct;
    std::string cpu_peak_thread_pct;
    std::string fan_pct;
    std::string temperature_c;
    std::string hotspot_temperature_c;
    std::string uv_offset_mv;
    std::string vram_used_mb;
    std::string vram_total_mb;
    std::string mem_clock_mhz;
    std::string ram_used_mb;
    std::string ram_total_mb;
    std::string cpu_temp_c;
    std::string cpu_power_w;
    std::string profile_id;
    // No default tier: an absent or empty published tier means stock GPU
    // state, and compact_profile_tier() renders that as DEF. Defaulting to
    // a real tier here made the HUD claim BAL on an untuned card.
    std::string profile_tier;
};

// Where the HUD box sits in the swapchain image. The four corners plus
// top-centre are what overlay.toml can name; anything else falls back to the
// historical top-right.
enum class OverlayPosition : uint32_t {
    TopLeft = 0,
    TopCenter = 1,
    TopRight = 2,
    BottomLeft = 3,
    BottomRight = 4,
};

constexpr OverlayPosition kDefaultOverlayPosition = OverlayPosition::TopRight;
// What 1x means: the resolution-adaptive size times this. At 0.75, 1x at 1440p
// draws ~16 px capitals, MangoHud's default text size. Kept in step with
// OVERLAY_SCALE_REFERENCE in overlay/config.py.
constexpr double kOverlayScaleReference = 0.75;

struct OverlayTextConfig {
    bool enabled = true;
    std::vector<std::string> items;
    // User-selected overlay scale multiplier from overlay.toml. 1.0 keeps the
    // resolution-adaptive size; 0.5/2.0 shrink/grow it. The Python UI only
    // writes 0.5/1.0/2.0, but the value is clamped on read for safety.
    double scale = 1.0;
    OverlayPosition position = kDefaultOverlayPosition;
    // Panel opacity, 0.0 to 1.0. Only the blended renderer can honour it: a
    // cleared rectangle writes its colour straight into the image.
    double opacity = 1.0;
    // false forces the historical clear-rect renderer even where a pipeline
    // would work, as a way out if blending upsets a particular game.
    bool blended_renderer = true;
    OverlayFont font = kDefaultOverlayFont;
    OverlayLayout layout = kDefaultOverlayLayout;
};

// ---- Shared global state (defined in latency_state.cpp / marker_timing.cpp) ----
extern TelemetrySocket g_socket;
extern std::mutex g_mutex;
extern std::unordered_map<VkInstance, InstanceContext> g_instances;
extern std::unordered_map<VkPhysicalDevice, VkInstance> g_physical_devices;
extern std::unordered_map<VkDevice, DeviceContext> g_devices;
extern std::unordered_map<VkQueue, QueueContext> g_queues;
extern std::unordered_map<VkSwapchainKHR, SwapchainContext> g_swapchains;
extern std::atomic<bool> g_reported_negotiate;
extern std::atomic<bool> g_reported_get_instance_proc_addr;
extern std::atomic<bool> g_reported_get_device_proc_addr;
extern std::atomic<bool> g_reported_create_instance_enter;
extern std::mutex g_overlay_config_mutex;
// Latest user overlay scale multiplier, published by overlay_text_config() so
// the present-path draw can read it without threading config through every call.
extern std::atomic<double> g_overlay_user_scale;
// Latest overlay corner, published alongside the scale so the present-path
// draw can read it without threading config through every call.
extern std::atomic<uint32_t> g_overlay_position;
// Whether the frametime graph is switched on, published with the rest of the
// config so the present-path draw can read it without taking the config lock.
extern std::atomic<bool> g_overlay_graph_enabled;
// Panel opacity and renderer choice, published with the rest of the config so
// the present-path draw never takes the config lock.
extern std::atomic<double> g_overlay_opacity;
extern std::atomic<bool> g_overlay_blended_renderer;
// Selected text face, published with the rest of the config.
extern std::atomic<uint32_t> g_overlay_font;
extern std::atomic<uint32_t> g_overlay_layout;

// ---- Cross-translation-unit function declarations ----
    bool env_flag_enabled(const char* name);
    const char* telemetry_session_id();
    bool debug_flow_enabled();
    bool display_latency_enabled();
    bool inject_present_id_enabled();
    bool should_report_counter(uint64_t count);
    bool timing_queries_enabled();
    uint64_t now_monotonic_us();
    uint64_t elapsed_us(uint64_t start_us, uint64_t end_us);
    std::string trim_ascii(std::string value);
    OverlayPosition parse_overlay_position(const std::string& value);
    OverlayFont parse_overlay_font(const std::string& value);
    OverlayLayout parse_overlay_layout(const std::string& value);
    VkOffset2D overlay_text_origin( const VkExtent2D& extent, uint32_t box_width, uint32_t box_height, uint32_t margin, OverlayPosition position);
    bool overlay_enabled();
    OverlayFrameStats overlay_frame_stats_locked( SwapchainContext& context, uint64_t present_frametime_us);
    OverlayGraph overlay_graph_locked(const SwapchainContext& context, uint64_t median_us);
    uint64_t overlay_graph_span_us(const OverlayGraph& graph, uint64_t worst_us);
    uint32_t overlay_graph_bar_height( uint64_t sample_us, uint64_t span_us, uint32_t height);
    std::string overlay_fit_line(const std::string& line, uint32_t max_chars);
    float overlay_glyph_advance( const OverlayFontAtlas* atlas, char ch, float scale);
    float overlay_measure( const std::string& text, const OverlayFontAtlas* atlas, float scale);
    OverlayLayout overlay_layout();
    VkClearColorValue overlay_level_color(OverlayCellLevel level);
    const OverlayFontAtlas* overlay_font_atlas(OverlayFont font);
    float overlay_ndc_x(int32_t x, uint32_t width);
    float overlay_ndc_y(int32_t y, uint32_t height);
    std::vector<OverlayRectBand> overlay_rounded_panel_bands( int32_t x, int32_t y, uint32_t width, uint32_t height, uint32_t radius);
    double overlay_opacity();
    bool overlay_blended_renderer_enabled();
    OverlayFont overlay_font();
    bool overlay_graph_enabled();
    OverlayFrameStats overlay_frame_stats_from_samples( const uint64_t* samples, uint32_t count, uint64_t session_count, uint64_t session_sum_us, uint64_t session_min_us, uint64_t session_max_us);
    uint64_t overlay_fps_from_frametime_us(uint64_t frametime_us);
    OverlayGpuState read_overlay_gpu_state();
    OverlayReadout build_overlay_readout( const OverlayFrameStats& stats, const OverlayDeviceFacts& facts, const OverlayGpuState& state, uint64_t now_us);
    std::string overlay_driver_version_text(uint32_t version, uint32_t vendor_id);
    std::string overlay_present_mode_text(VkPresentModeKHR mode);
    std::string overlay_resolution_text(const VkExtent2D& extent);
    std::string build_overlay_text( const OverlayFrameStats& stats, const OverlayDeviceFacts& facts, const OverlayGpuState& state, uint64_t now_us);
    const std::shared_ptr<const OverlayReadout>& cached_overlay_readout( SwapchainContext& context, const OverlayFrameStats& stats, uint64_t now_us);
    void destroy_overlay_resources( const DeviceContext& device_context, SwapchainContext& swapchain_context);
    VkSemaphore submit_overlay_locked( const DeviceContext& device_context, VkSwapchainKHR swapchain, SwapchainContext& swapchain_context, const QueueContext& queue_context, VkQueue queue, uint32_t image_index, const VkPresentInfoKHR* present_info, const std::shared_ptr<const OverlayReadout>& readout, const OverlayGraph& graph);
    uint64_t live_swapchain_count_locked(VkDevice device);
    DeviceTelemetryFlags device_telemetry_flags(VkDevice device);
    bool swapchain_latency_mode_enabled(const VkSwapchainCreateInfoKHR* create_info);
    uint64_t present_id_for_swapchain( const VkPresentInfoKHR* present_info, uint32_t swapchain_index);
    bool present_info_has_present_id_struct(const VkPresentInfoKHR* present_info);
    void send_status_event( const char* event, VkDevice device, VkSwapchainKHR swapchain, uint64_t count, DeviceTelemetryFlags flags);
    void send_swapchain_event( const char* event, VkDevice device, VkSwapchainKHR swapchain, const VkSwapchainCreateInfoKHR* create_info, uint64_t live_swapchain_count, bool latency_mode_enabled, DeviceTelemetryFlags flags);
    void send_latency_sleep_mode_event( const char* event, VkDevice device, VkSwapchainKHR swapchain, uint64_t count, VkResult result, bool has_sleep_mode_info, const VkLatencySleepModeInfoNV& sleep_mode_info, uint64_t duplicate_count, uint64_t present_count, DeviceTelemetryFlags flags);
    FlowSnapshot flow_snapshot(VkDevice device, VkSwapchainKHR swapchain);
    void send_flow_state_event( const char* event, VkDevice device, VkSwapchainKHR swapchain, VkQueue queue, uint64_t count, VkResult result, int queue_type, uint64_t sleep_value, const FlowSnapshot& snapshot);
    void send_marker_coverage_event( VkDevice device, VkSwapchainKHR swapchain, uint64_t count, const MarkerCounts& marker_counts, DeviceTelemetryFlags flags);
    void send_marker_timing_sample( VkDevice device, VkSwapchainKHR swapchain, uint64_t present_id, const MarkerTiming& timing, uint64_t sample_count, DeviceTelemetryFlags flags);
    void send_status_event_once(const char* event, std::atomic<bool>& reported);
    bool extension_requested( const VkDeviceCreateInfo* create_info, const char* extension_name);
    bool extension_advertised( const InstanceContext& instance, VkPhysicalDevice physical_device, const char* extension_name);
    uint32_t marker_timing_metric_bits(const MarkerTiming& timing);
    void store_queue( VkDevice device, VkQueue queue, uint32_t queue_family_index, uint32_t queue_index);
    void record_marker( VkDevice device, VkSwapchainKHR swapchain, const VkSetLatencyMarkerInfoNV* marker_info);
    void observe_latency_marker( VkDevice device, VkSwapchainKHR swapchain, const VkSetLatencyMarkerInfoNV* marker_info);
    void send_present_pacing_sample( VkDevice device, VkSwapchainKHR swapchain, uint64_t present_frametime_us, uint64_t present_count);
    void send_inject_debug_event( VkDevice device, VkSwapchainKHR swapchain, uint64_t present_count, bool chain_has_present_id, bool present_id_at_head, bool want_inject, uint64_t injected_present_id);
    void display_latency_enqueue( VkDevice device, PFN_vkWaitForPresentKHR wait_fn, VkSwapchainKHR swapchain, uint64_t present_id, uint64_t submit_us);
    void display_latency_stop_device(VkDevice device);
    bool reapply_latency_sleep_mode( const char* event, VkDevice device, VkSwapchainKHR swapchain, uint64_t duplicate_count, uint64_t present_count);
    void query_latency_timing(VkDevice device, VkSwapchainKHR swapchain);

}  // namespace pblayer

#endif  // PENGUINBURNER_LATENCY_LAYER_INTERNAL_H
