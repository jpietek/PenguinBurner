// Shared global state, env-flag gates, and leaf time/counter helpers.
#include "latency_layer_internal.h"
#include <cstdarg>
#include <unistd.h>

namespace pblayer {
bool env_flag_enabled(const char* name) {
    const char* value = std::getenv(name);
    if (!value || !*value) {
        return false;
    }
    return std::strcmp(value, "0") != 0 && std::strcmp(value, "false") != 0
        && std::strcmp(value, "False") != 0 && std::strcmp(value, "no") != 0
        && std::strcmp(value, "off") != 0;
}

const char* telemetry_session_id() {
    static const std::string session_id = [] {
        const char* value = std::getenv(kTelemetrySessionEnv);
        if (!value || !*value) {
            return std::string{};
        }
        std::string candidate(value);
        if (!std::all_of(
                candidate.begin(),
                candidate.end(),
                [](unsigned char ch) { return std::isdigit(ch) != 0; })) {
            return std::string{};
        }
        return candidate;
    }();
    return session_id.c_str();
}

bool debug_flow_enabled() {
    static const bool enabled = env_flag_enabled(kDebugFlowEnv);
    return enabled;
}

bool display_latency_enabled() {
    static const bool enabled = env_flag_enabled(kDisplayLatencyEnv);
    return enabled;
}

bool inject_present_id_enabled() {
    static const bool enabled = env_flag_enabled(kInjectPresentIdEnv);
    return enabled;
}

TelemetrySocket g_socket;

std::mutex g_mutex;

std::unordered_map<VkInstance, InstanceContext> g_instances;

std::unordered_map<VkPhysicalDevice, VkInstance> g_physical_devices;

std::unordered_map<VkDevice, DeviceContext> g_devices;

std::unordered_map<VkQueue, QueueContext> g_queues;

std::unordered_map<VkSwapchainKHR, SwapchainContext> g_swapchains;

std::atomic<bool> g_reported_negotiate{false};

std::atomic<bool> g_reported_get_instance_proc_addr{false};

std::atomic<bool> g_reported_get_device_proc_addr{false};

std::atomic<bool> g_reported_create_instance_enter{false};

std::mutex g_overlay_config_mutex;

// Latest user overlay scale multiplier, published by overlay_text_config() so
// the present-path draw can read it without threading config through every call.
std::atomic<double> g_overlay_user_scale{1.0};
std::atomic<uint32_t> g_overlay_position{
    static_cast<uint32_t>(kDefaultOverlayPosition)};
std::atomic<uint32_t> g_overlay_graph_mode{0};
std::atomic<bool> g_overlay_graph_enabled{false};
std::atomic<double> g_overlay_opacity{1.0};
std::atomic<bool> g_overlay_blended_renderer{true};
std::atomic<uint32_t> g_overlay_font{
    static_cast<uint32_t>(kDefaultOverlayFont)};
std::atomic<uint32_t> g_overlay_layout{
    static_cast<uint32_t>(kDefaultOverlayLayout)};

bool should_report_counter(uint64_t count) {
    return count <= 3 || (count % 600) == 0;
}

bool env_value_is_false(const char* value) {
    if (!value || !*value) {
        return false;
    }
    std::string text(value);
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return text == "0" || text == "false" || text == "no" || text == "off";
}

bool timing_queries_enabled() {
    return !env_value_is_false(std::getenv(kQueryTimingsEnv));
}

uint64_t now_monotonic_us() {
    using Clock = std::chrono::steady_clock;
    return static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(
            Clock::now().time_since_epoch())
            .count());
}

uint64_t elapsed_us(uint64_t start_us, uint64_t end_us) {
    if (!start_us || !end_us || end_us <= start_us) {
        return 0;
    }
    return end_us - start_us;
}


// Trace of the overlay/display-timing path, on only with PENGUIN_BURNER_LATENCY_DEBUG_FLOW.
// Writes ~/.cache/penguin-burner/display-timing.<pid>.log; callers keep it sparse.
bool display_debug_enabled() {
    static const bool enabled = [] {
        const char* value = std::getenv(kDebugFlowEnv);
        return value && *value && std::strcmp(value, "0") != 0;
    }();
    return enabled;
}

void display_debug_log(const char* fmt, ...) {
    if (!display_debug_enabled()) return;
    static std::mutex log_mutex;
    static FILE* file = nullptr;
    std::lock_guard<std::mutex> lock(log_mutex);
    if (!file) {
        const char* cache = std::getenv("XDG_CACHE_HOME");
        std::string dir = cache && *cache ? std::string(cache)
            : std::string(std::getenv("HOME") ? std::getenv("HOME") : "/tmp") + "/.cache";
        dir += "/penguin-burner";
        char path[512];
        std::snprintf(path, sizeof(path), "%s/display-timing.%ld.log", dir.c_str(), static_cast<long>(::getpid()));
        file = std::fopen(path, "a");
        if (!file) return;
    }
    std::fprintf(file, "%" PRIu64 " ", now_monotonic_us());
    va_list args;
    va_start(args, fmt);
    std::vfprintf(file, fmt, args);
    va_end(args);
    std::fputc('\n', file);
    std::fflush(file);
}

}  // namespace pblayer
