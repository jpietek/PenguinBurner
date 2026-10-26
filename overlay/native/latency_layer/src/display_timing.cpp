// Display timing uses driver-reported scanout/visible timestamps, never waiter
// wakeups or smoothed CPU calls. Unsupported surfaces retain an explicit gap.
#include "latency_layer_internal.h"
#include <cstdarg>
#include <unistd.h>

namespace pblayer {
// Trace of the display-timing path, on only with PENGUIN_BURNER_LATENCY_DEBUG_FLOW.
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
void record_display_timestamp(DisplayTimingState& state, uint64_t timestamp_ns, uint64_t now_us) {
    if (!timestamp_ns) return; // A discarded presentation has no display timestamp.
    if (state.last_sample_us && now_us - state.last_sample_us > 2000000) {
        state.count = state.index = 0;
        state.last_timestamp_ns = 0;
    }
    if (timestamp_ns <= state.last_timestamp_ns) return;
    if (state.last_timestamp_ns) {
        const uint64_t delta = (timestamp_ns - state.last_timestamp_ns) / 1000;
        if (delta) {
            state.samples[state.index] = delta;
            state.index = (state.index + 1) % state.samples.size();
            state.count = std::min<uint32_t>(state.count + 1, state.samples.size());
        }
    }
    state.last_timestamp_ns = timestamp_ns;
    state.last_sample_us = now_us;
}

#ifdef VK_EXT_present_timing
static const VkBaseInStructure* chain_entry(const void* chain, VkStructureType type) {
    auto* p = static_cast<const VkBaseInStructure*>(chain);
    for (; p; p = p->pNext) if (p->sType == type) return p;
    return nullptr;
}

void prepare_display_timing_device(VkInstance instance, const InstanceContext& context,
    VkPhysicalDevice physical, const VkDeviceCreateInfo* original, DisplayTimingDeviceSetup& setup) {
    if (!context.surface_capabilities2_enabled) return;
    // Do not consume timing queues owned by the game or another layer.
    if (extension_requested(original, VK_EXT_PRESENT_TIMING_EXTENSION_NAME)
        || chain_entry(original->pNext, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_TIMING_FEATURES_EXT)) return;
    const char* required[] = {VK_EXT_PRESENT_TIMING_EXTENSION_NAME,
        VK_KHR_PRESENT_ID_2_EXTENSION_NAME, VK_KHR_CALIBRATED_TIMESTAMPS_EXTENSION_NAME};
    for (auto* name : required) if (!extension_advertised(context, physical, name)) return;
    auto get_features = reinterpret_cast<PFN_vkGetPhysicalDeviceFeatures2>(
        context.get_instance_proc_addr(instance, "vkGetPhysicalDeviceFeatures2"));
    if (!get_features || !context.get_instance_proc_addr(instance, "vkGetPhysicalDeviceSurfaceCapabilities2KHR")) return;
    VkPhysicalDevicePresentId2FeaturesKHR supported_id{};
    supported_id.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_2_FEATURES_KHR;
    VkPhysicalDevicePresentTimingFeaturesEXT supported{};
    supported.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_TIMING_FEATURES_EXT;
    supported.pNext = &supported_id;
    VkPhysicalDeviceFeatures2 features{};
    features.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2;
    features.pNext = &supported;
    get_features(physical, &features);
    if (!supported.presentTiming || !supported_id.presentId2) return;
    auto* existing_id = reinterpret_cast<const VkPhysicalDevicePresentId2FeaturesKHR*>(
        chain_entry(original->pNext, VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_2_FEATURES_KHR));
    if (existing_id && !existing_id->presentId2) return;
    setup.info = *original;
    if (original->enabledExtensionCount)
        setup.extensions.assign(original->ppEnabledExtensionNames,
            original->ppEnabledExtensionNames + original->enabledExtensionCount);
    for (auto* name : required)
        if (!extension_requested(original, name)) setup.extensions.push_back(name);
    setup.timing.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_TIMING_FEATURES_EXT;
    setup.id.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_2_FEATURES_KHR;
    setup.timing.presentTiming = VK_TRUE;
    setup.timing.pNext = const_cast<void*>(original->pNext);
    if (!existing_id) {
        setup.id.presentId2 = VK_TRUE;
        setup.id.pNext = const_cast<void*>(original->pNext);
        setup.timing.pNext = &setup.id;
    }
    setup.info.pNext = &setup.timing;
    setup.info.enabledExtensionCount = setup.extensions.size();
    setup.info.ppEnabledExtensionNames = setup.extensions.data();
    setup.enabled = true;
}

uint32_t display_timing_surface_stage(VkInstance instance, PFN_vkGetInstanceProcAddr get_proc,
    VkPhysicalDevice physical, VkSurfaceKHR surface) {
    if (!get_proc) return 0;
    auto get_caps = reinterpret_cast<PFN_vkGetPhysicalDeviceSurfaceCapabilities2KHR>(
        get_proc(instance, "vkGetPhysicalDeviceSurfaceCapabilities2KHR"));
    if (!get_caps) return 0;
    VkPhysicalDeviceSurfaceInfo2KHR info{};
    info.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SURFACE_INFO_2_KHR;
    info.surface = surface;
    VkPresentTimingSurfaceCapabilitiesEXT timing{};
    timing.sType = VK_STRUCTURE_TYPE_PRESENT_TIMING_SURFACE_CAPABILITIES_EXT;
    VkSurfaceCapabilitiesPresentId2KHR id_caps{};
    id_caps.sType = VK_STRUCTURE_TYPE_SURFACE_CAPABILITIES_PRESENT_ID_2_KHR;
    timing.pNext = &id_caps;
    VkSurfaceCapabilities2KHR caps{};
    caps.sType = VK_STRUCTURE_TYPE_SURFACE_CAPABILITIES_2_KHR;
    caps.pNext = &timing;
    if (get_caps(physical, &info, &caps) != VK_SUCCESS || !timing.presentTimingSupported || !id_caps.presentId2Supported) return 0;
    if (timing.presentStageQueries & VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_VISIBLE_BIT_EXT)
        return VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_VISIBLE_BIT_EXT;
    if (timing.presentStageQueries & VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT)
        return VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT;
    return 0; // GPU queue completion is not display timing.
}

void init_display_timing(const DeviceContext& device, VkSwapchainKHR swapchain, DisplayTimingState& state) {
    if (!state.stage || !device.set_timing_queue || !device.get_timing_domains || !device.get_past_timing) return;
    VkSwapchainTimeDomainPropertiesEXT domains{};
    domains.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_TIME_DOMAIN_PROPERTIES_EXT;
    uint64_t counter = 0;
    if (device.get_timing_domains(device.device, swapchain, &domains, &counter) != VK_SUCCESS
        || !domains.timeDomainCount || domains.timeDomainCount > 64) return;
    std::vector<VkTimeDomainKHR> types(domains.timeDomainCount);
    std::vector<uint64_t> ids(domains.timeDomainCount);
    domains.pTimeDomains = types.data();
    domains.pTimeDomainIds = ids.data();
    if (device.get_timing_domains(device.device, swapchain, &domains, &counter) != VK_SUCCESS) return;
    // Intervals stay inside a single domain; no CPU clock conversion is needed.
    state.domain_id = ids[0];
    for (uint32_t i = 0; i < domains.timeDomainCount; ++i)
        if (types[i] == VK_TIME_DOMAIN_CLOCK_MONOTONIC_KHR) state.domain_id = ids[i];
    // Shared: the app sized the queue; leave it alone.
    state.available = state.shared || device.set_timing_queue(device.device, swapchain, 64) == VK_SUCCESS;
}

void collect_display_timing(const DeviceContext& device, VkSwapchainKHR swapchain, DisplayTimingState& state) {
    if (!state.available || state.passive || !state.pending) return;
    if (state.shared) {
        // The app polls for us; drain only if it stopped and the cap is near.
        const uint64_t now = now_monotonic_us();
        if (state.pending < 8 || (state.last_observed_us && now - state.last_observed_us < 500000)) return;
    }
    std::array<VkPastPresentationTimingEXT, 64> timings{};
    std::array<VkPresentStageTimeEXT, 64> stages{};
    for (size_t i = 0; i < timings.size(); ++i) {
        timings[i].sType = VK_STRUCTURE_TYPE_PAST_PRESENTATION_TIMING_EXT;
        timings[i].presentStageCount = 1;
        timings[i].pPresentStages = &stages[i];
    }
    VkPastPresentationTimingInfoEXT info{};
    info.sType = VK_STRUCTURE_TYPE_PAST_PRESENTATION_TIMING_INFO_EXT;
    info.swapchain = swapchain;
    VkPastPresentationTimingPropertiesEXT results{};
    results.sType = VK_STRUCTURE_TYPE_PAST_PRESENTATION_TIMING_PROPERTIES_EXT;
    results.presentationTimingCount = timings.size();
    results.pPresentationTimings = timings.data();
    const VkResult result = device.get_past_timing(device.device, &info, &results);
    if (result != VK_SUCCESS && result != VK_INCOMPLETE) {
        state.available = false;
        state.count = state.index = 0;
        return;
    }
    for (uint32_t i = 0; i < results.presentationTimingCount; ++i) {
        if (!timings[i].reportComplete) continue;
        if (state.pending) --state.pending;
        if (timings[i].timeDomainId != state.domain_id) {
            state.last_timestamp_ns = 0;
            state.count = state.index = 0;
            continue;
        }
        if (stages[i].stage == state.stage)
            record_display_timestamp(state, stages[i].time, now_monotonic_us());
    }
}

void observe_display_timing(DisplayTimingState& state,
    const VkPastPresentationTimingPropertiesEXT& results, uint64_t now_us) {
    if (!results.pPresentationTimings) return;
    for (uint32_t i = 0; i < results.presentationTimingCount; ++i) {
        const auto& report = results.pPresentationTimings[i];
        if (!report.reportComplete || !report.pPresentStages) continue;
        const VkPresentStageTimeEXT* selected = nullptr;
        for (uint32_t j = 0; j < report.presentStageCount; ++j) {
            const auto& stage = report.pPresentStages[j];
            if (stage.stage == VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_VISIBLE_BIT_EXT) {
                selected = &stage;
                break;
            }
            if (stage.stage == VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT) selected = &stage;
        }
        if (state.shared) {
            if (state.pending) --state.pending;
            state.last_observed_us = now_us;
            state.available = true;
            if (!selected) continue;
            if (state.domain_id != report.timeDomainId || state.stage != selected->stage) {
                state.count = state.index = 0;
                state.last_timestamp_ns = 0;
                state.domain_id = report.timeDomainId;
                state.stage = selected->stage;
            }
            // A replaced present reports time 0 and is skipped.
            record_display_timestamp(state, selected->time, now_us);
            continue;
        }
        if (!selected || !selected->time) continue;
        // Never subtract timestamps across clock domains or presentation stages.
        if (!state.passive || state.domain_id != report.timeDomainId || state.stage != selected->stage) {
            state = DisplayTimingState{};
            state.passive = true;
            state.domain_id = report.timeDomainId;
            state.stage = selected->stage;
        }
        state.available = true;
        record_display_timestamp(state, selected->time, now_us);
    }
}

VKAPI_ATTR VkResult VKAPI_CALL layer_get_past_presentation_timing_ext(VkDevice device,
    const VkPastPresentationTimingInfoEXT* info, VkPastPresentationTimingPropertiesEXT* results) {
    PFN_vkGetPastPresentationTimingEXT next = nullptr;
    bool owns_timing = false;
    {
        std::lock_guard lock(g_mutex);
        auto it = g_devices.find(device);
        if (it != g_devices.end()) {
            next = it->second.get_past_timing;
            owns_timing = it->second.owns_display_timing;
        }
    }
    if (!next) {
        static std::atomic<uint64_t> missing{0};
        if (++missing <= 5) display_debug_log("past-timing: next entry point missing (device %p)", reinterpret_cast<void*>(device));
        return VK_ERROR_EXTENSION_NOT_PRESENT;
    }
    // Forward the exact request once. Do not drain, resize, or replace the
    // application's queue; only observe the feedback it already requested.
    const VkResult result = next(device, info, results);
    static std::atomic<uint64_t> calls{0};
    const uint64_t call = ++calls;
    const bool verbose = call <= 12 || call % 600 == 0;
    if (verbose) {
        display_debug_log("past-timing call=%" PRIu64 " result=%d owns=%d count=%u swapchain=%p",
            call, static_cast<int>(result), owns_timing ? 1 : 0,
            results ? results->presentationTimingCount : 0u, info ? reinterpret_cast<void*>(info->swapchain) : nullptr);
        if (results && results->pPresentationTimings) {
            for (uint32_t i = 0; i < std::min(results->presentationTimingCount, 3u); ++i) {
                const auto& r = results->pPresentationTimings[i];
                display_debug_log("  report id=%" PRIu64 " complete=%d stages=%u stage0=0x%x time0=%" PRIu64 " domain=%d domainId=%" PRIu64 " target=%" PRIu64,
                    r.presentId, r.reportComplete ? 1 : 0, r.presentStageCount,
                    r.pPresentStages && r.presentStageCount ? r.pPresentStages[0].stage : 0u,
                    r.pPresentStages && r.presentStageCount ? r.pPresentStages[0].time : 0ull,
                    static_cast<int>(r.timeDomain), r.timeDomainId, r.targetTime);
            }
        }
    }
    if (!owns_timing && info && results && (result == VK_SUCCESS || result == VK_INCOMPLETE)) {
        const uint64_t lock_begin_us = display_debug_enabled() ? now_monotonic_us() : 0;
        std::lock_guard lock(g_mutex);
        if (lock_begin_us) {
            // This runs on the app's own polling thread; report how long it waited on us.
            static uint64_t wait_max = 0, wait_sum = 0, waits = 0;
            const uint64_t waited = now_monotonic_us() - lock_begin_us;
            wait_max = std::max(wait_max, waited); wait_sum += waited;
            if (++waits % 600 == 0) { display_debug_log("intercept g_mutex wait avg=%.1fus max=%" PRIu64 "us", wait_sum / 600.0, wait_max); wait_sum = wait_max = 0; }
        }
        auto it = g_swapchains.find(info->swapchain);
        if (it != g_swapchains.end() && it->second.device == device) {
            observe_display_timing(it->second.display_timing, *results, now_monotonic_us());
            if (verbose) display_debug_log("  observed available=%d count=%u stage=0x%x last_ts=%" PRIu64,
                it->second.display_timing.available ? 1 : 0, it->second.display_timing.count,
                it->second.display_timing.stage, it->second.display_timing.last_timestamp_ns);
        } else if (verbose) {
            display_debug_log("  swapchain lookup failed found=%d", it != g_swapchains.end() ? 1 : 0);
        }
    }
    return result;
}
#endif
} // namespace pblayer
