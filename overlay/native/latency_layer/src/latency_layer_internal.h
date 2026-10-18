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
    PFN_vkGetPhysicalDeviceQueueFamilyProperties
        get_physical_device_queue_family_properties = nullptr;
    std::string application_name = "unknown";
    std::string engine_name = "unknown";
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

struct OverlayRenderResources {
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
};

struct SwapchainContext {
    VkDevice device = VK_NULL_HANDLE;
    bool swapchain_latency_mode = false;
    VkFormat image_format = VK_FORMAT_UNDEFINED;
    VkExtent2D image_extent{};
    uint32_t image_array_layers = 1;
    VkImageUsageFlags image_usage = 0;
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
    BaseMarkerState out_of_band_present_start{};
    BaseMarkerState present_start{};
    BaseMarkerState render_submit_start{};
    OverlayRenderResources overlay{};
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
    std::string uv_offset_mv;
    std::string profile_id;
    std::string profile_tier = "Balanced";
};

struct OverlayTextConfig {
    bool enabled = true;
    std::vector<std::string> items;
    // User-selected overlay scale multiplier from overlay.toml. 1.0 keeps the
    // resolution-adaptive size; 0.5/2.0 shrink/grow it. The Python UI only
    // writes 0.5/1.0/2.0, but the value is clamped on read for safety.
    double scale = 1.0;
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
extern std::mutex g_overlay_file_mutex;
// Latest user overlay scale multiplier, published by overlay_text_config() so
// the present-path draw can read it without threading config through every call.
extern std::atomic<double> g_overlay_user_scale;

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
    bool overlay_enabled();
    uint64_t median_fps_from_present_frametime_locked( SwapchainContext& context, uint64_t present_frametime_us);
    std::string cached_overlay_text(uint64_t fps, uint64_t now_us);
    void destroy_overlay_resources( const DeviceContext& device_context, SwapchainContext& swapchain_context);
    VkSemaphore submit_overlay_locked( const DeviceContext& device_context, VkSwapchainKHR swapchain, SwapchainContext& swapchain_context, const QueueContext& queue_context, VkQueue queue, uint32_t image_index, const VkPresentInfoKHR* present_info, const std::string& text);
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
