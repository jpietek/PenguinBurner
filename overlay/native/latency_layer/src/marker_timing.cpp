// Reflex marker recording, driver-report timing, present->scanout waiter.
#include "latency_layer_internal.h"

namespace pblayer {
uint32_t marker_bit(VkLatencyMarkerNV marker) {
    switch (marker) {
        case VK_LATENCY_MARKER_SIMULATION_START_NV:
            return 1u << 0;
        case VK_LATENCY_MARKER_SIMULATION_END_NV:
            return 1u << 1;
        case VK_LATENCY_MARKER_RENDERSUBMIT_START_NV:
            return 1u << 2;
        case VK_LATENCY_MARKER_RENDERSUBMIT_END_NV:
            return 1u << 3;
        case VK_LATENCY_MARKER_PRESENT_START_NV:
            return 1u << 4;
        case VK_LATENCY_MARKER_PRESENT_END_NV:
            return 1u << 5;
        case VK_LATENCY_MARKER_INPUT_SAMPLE_NV:
            return 1u << 6;
        case VK_LATENCY_MARKER_OUT_OF_BAND_RENDERSUBMIT_START_NV:
            return 1u << 7;
        case VK_LATENCY_MARKER_OUT_OF_BAND_RENDERSUBMIT_END_NV:
            return 1u << 8;
        case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_START_NV:
            return 1u << 9;
        case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_END_NV:
            return 1u << 10;
        default:
            return 0;
    }
}

bool report_has_reflex_markers(const VkLatencyTimingsFrameReportNV& report) {
    return report.inputSampleTimeUs || report.simStartTimeUs || report.simEndTimeUs
        || report.renderSubmitStartTimeUs || report.renderSubmitEndTimeUs
        || report.presentStartTimeUs || report.presentEndTimeUs;
}

bool report_has_driver_timing(const VkLatencyTimingsFrameReportNV& report) {
    return report.driverStartTimeUs || report.driverEndTimeUs
        || report.osRenderQueueStartTimeUs || report.osRenderQueueEndTimeUs
        || report.gpuRenderStartTimeUs || report.gpuRenderEndTimeUs;
}

void store_queue(
    VkDevice device,
    VkQueue queue,
    uint32_t queue_family_index,
    uint32_t queue_index) {
    if (queue == VK_NULL_HANDLE) {
        return;
    }
    QueueContext queue_context{};
    queue_context.device = device;
    queue_context.queue_family_index = queue_family_index;
    queue_context.queue_index = queue_index;

    std::lock_guard lock(g_mutex);
    auto device_it = g_devices.find(device);
    if (device_it != g_devices.end()) {
        if (queue_family_index
            < device_it->second.queue_family_properties.size()) {
            const auto& properties =
                device_it->second.queue_family_properties[queue_family_index];
            queue_context.queue_flags = properties.queueFlags;
        }
    }
    g_queues[queue] = queue_context;
}

void record_marker(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkSetLatencyMarkerInfoNV* marker_info) {
    if (!marker_info || marker_info->presentID == 0) {
        return;
    }

    uint32_t bit = marker_bit(marker_info->marker);
    if (!bit) {
        return;
    }

    const uint64_t marker_time_us = now_monotonic_us();
    uint64_t marker_count = 0;
    uint64_t marker_sample_count = 0;
    bool send_marker_sample = false;
    DeviceTelemetryFlags flags{};
    MarkerTiming marker_timing{};
    MarkerCounts marker_counts{};
    {
        std::lock_guard lock(g_mutex);
        auto device_it = g_devices.find(device);
        if (device_it == g_devices.end()) {
            return;
        }

        auto& context = device_it->second;
        context.latest_marker_present_id = marker_info->presentID;
        uint32_t& bits = context.marker_bits_by_present_id[marker_info->presentID];
        if (!bits) {
            context.marker_order.push_back(marker_info->presentID);
            if (context.marker_order.size() > 256) {
                uint64_t stale = context.marker_order.front();
                context.marker_order.erase(context.marker_order.begin());
                context.marker_bits_by_present_id.erase(stale);
                context.marker_timings_by_present_id.erase(stale);
            }
        }
        bits |= bit;
        auto& timing = context.marker_timings_by_present_id[marker_info->presentID];
        timing.bits |= bit;
        switch (marker_info->marker) {
            case VK_LATENCY_MARKER_SIMULATION_START_NV:
                context.marker_counts.simulation_start++;
                context.last_simulation_present_id = marker_info->presentID;
                timing.sim_start_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_SIMULATION_END_NV:
                context.marker_counts.simulation_end++;
                context.last_simulation_present_id = marker_info->presentID;
                timing.sim_end_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_RENDERSUBMIT_START_NV:
                context.marker_counts.render_submit_start++;
                context.last_render_submit_present_id = marker_info->presentID;
                timing.render_submit_start_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_RENDERSUBMIT_END_NV:
                context.marker_counts.render_submit_end++;
                context.last_render_submit_present_id = marker_info->presentID;
                timing.render_submit_end_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_PRESENT_START_NV:
                context.marker_counts.present_start++;
                context.last_present_marker_present_id = marker_info->presentID;
                timing.present_start_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_PRESENT_END_NV:
                context.marker_counts.present_end++;
                context.last_present_marker_present_id = marker_info->presentID;
                timing.present_end_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_INPUT_SAMPLE_NV:
                context.marker_counts.input_sample++;
                context.last_input_sample_present_id = marker_info->presentID;
                timing.input_sample_us = marker_time_us;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_RENDERSUBMIT_START_NV:
                context.marker_counts.out_of_band_render_submit_start++;
                context.last_oob_render_submit_present_id = marker_info->presentID;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_RENDERSUBMIT_END_NV:
                context.marker_counts.out_of_band_render_submit_end++;
                context.last_oob_render_submit_present_id = marker_info->presentID;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_START_NV:
                context.marker_counts.out_of_band_present_start++;
                context.last_oob_present_present_id = marker_info->presentID;
                break;
            case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_END_NV:
                context.marker_counts.out_of_band_present_end++;
                context.last_oob_present_present_id = marker_info->presentID;
                timing.oob_present_end_us = marker_time_us;
                break;
            default:
                break;
        }

        marker_count = ++context.marker_count;
        flags = {
            context.low_latency_extension_advertised,
            context.low_latency_extension_requested,
            context.low_latency_functions_available,
            context.marker_count,
        };
        marker_counts = context.marker_counts;
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end()) {
            // Settles whether PRESENT_END means presented or enqueued: a
            // tight, mostly-positive distribution vs. the layer's own
            // vkQueuePresentKHR time means the marker fires at the real
            // present (see latency-meter plan, Phase 1 step 5).
            if (marker_info->marker == VK_LATENCY_MARKER_PRESENT_END_NV
                && swapchain_it->second.last_present_us) {
                timing.present_marker_lag_us =
                    static_cast<int64_t>(marker_time_us)
                    - static_cast<int64_t>(swapchain_it->second.last_present_us);
            }
        }
        uint32_t metric_bits = marker_timing_metric_bits(timing);
        if (metric_bits && metric_bits != timing.emitted_metric_bits) {
            timing.emitted_metric_bits = metric_bits;
            marker_timing = timing;
            send_marker_sample = true;

            if (swapchain_it != g_swapchains.end()) {
                auto& swapchain_context = swapchain_it->second;
                marker_sample_count = ++swapchain_context.timing_sample_count;
            }
        }
    }

    if (should_report_counter(marker_count)) {
        send_status_event(
            "latency-marker",
            device,
            swapchain,
            marker_count,
            flags);
        send_marker_coverage_event(
            device,
            swapchain,
            marker_count,
            marker_counts,
            flags);
    }
    if (send_marker_sample) {
        send_marker_timing_sample(
            device,
            swapchain,
            marker_info->presentID,
            marker_timing,
            marker_sample_count,
            flags);
    }
}

void send_timing_sample(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkLatencyTimingsFrameReportNV& report,
    uint32_t timing_count) {
    uint32_t marker_bits = 0;
    uint64_t gpu_frame_time_us = 0;
    uint64_t sample_count = 0;
    uint64_t driver_report_count = 0;
    uint64_t driver_report_duplicate_count = 0;
    bool advertised = false;
    bool requested = false;
    bool functions_available = false;

    {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end()) {
            auto& swapchain_context = swapchain_it->second;
            if (report.gpuRenderEndTimeUs && swapchain_context.last_gpu_render_end_us
                && report.gpuRenderEndTimeUs > swapchain_context.last_gpu_render_end_us) {
                gpu_frame_time_us =
                    report.gpuRenderEndTimeUs - swapchain_context.last_gpu_render_end_us;
            }
            if (report.gpuRenderEndTimeUs) {
                swapchain_context.last_gpu_render_end_us = report.gpuRenderEndTimeUs;
            }
            driver_report_count = ++swapchain_context.driver_timing_sample_count;
            if (report.presentID
                && report.presentID == swapchain_context.last_driver_report_present_id) {
                driver_report_duplicate_count =
                    ++swapchain_context.duplicate_driver_report_count;
            } else {
                swapchain_context.last_driver_report_present_id = report.presentID;
                swapchain_context.duplicate_driver_report_count = 0;
            }
            sample_count = ++swapchain_context.timing_sample_count;
        }

        auto device_it = g_devices.find(device);
        if (device_it != g_devices.end()) {
            auto& device_context = device_it->second;
            auto marker_it =
                device_context.marker_bits_by_present_id.find(report.presentID);
            if (marker_it != device_context.marker_bits_by_present_id.end()) {
                marker_bits = marker_it->second;
            }
            advertised = device_context.low_latency_extension_advertised;
            requested = device_context.low_latency_extension_requested;
            functions_available = device_context.low_latency_functions_available;
        }
    }

    uint64_t render_submit_us = 0;
    if (report.renderSubmitStartTimeUs && report.renderSubmitEndTimeUs
        && report.renderSubmitEndTimeUs > report.renderSubmitStartTimeUs) {
        render_submit_us =
            report.renderSubmitEndTimeUs - report.renderSubmitStartTimeUs;
    }

    uint64_t input_to_present_us = 0;
    if (report.inputSampleTimeUs && report.presentEndTimeUs
        && report.presentEndTimeUs > report.inputSampleTimeUs) {
        input_to_present_us = report.presentEndTimeUs - report.inputSampleTimeUs;
    }

    uint64_t render_present_us = 0;
    if (report.presentStartTimeUs && report.gpuRenderEndTimeUs
        && report.gpuRenderEndTimeUs > report.presentStartTimeUs) {
        render_present_us = report.gpuRenderEndTimeUs - report.presentStartTimeUs;
    }

    uint64_t gpu_render_us = 0;
    if (report.gpuRenderStartTimeUs && report.gpuRenderEndTimeUs
        && report.gpuRenderEndTimeUs > report.gpuRenderStartTimeUs) {
        gpu_render_us = report.gpuRenderEndTimeUs - report.gpuRenderStartTimeUs;
    }

    const bool has_reflex_report_timestamps = report_has_reflex_markers(report);
    const bool has_driver_timing = report_has_driver_timing(report);
    const char* quality = "present-frametime";
    if (input_to_present_us) {
        quality = "reflex-input-present";
    } else if (render_submit_us) {
        quality = "reflex-render-submit";
    } else if (has_reflex_report_timestamps) {
        quality = "reflex-markers";
    } else if (has_driver_timing) {
        quality = "driver-timing";
    } else if (marker_bits) {
        quality = "reflex-marker-presence";
    }

    char line[1280]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"timing\",\"measurement\":\"driver-report\","
        "\"pid\":%ld,"
        "\"session_id\":\"%s\","
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"present_id\":%" PRIu64 ",\"quality\":\"%s\","
        "\"timing_count\":%u,\"sample_count\":%" PRIu64 ","
        "\"driver_report_count\":%" PRIu64 ","
        "\"driver_report_duplicate_count\":%" PRIu64 ","
        "\"marker_bits\":%u,\"vk_nv_low_latency2_advertised\":%s,"
        "\"vk_nv_low_latency2_requested\":%s,"
        "\"vk_nv_low_latency2_functions\":%s,"
        "\"gpu_frame_time_us\":%" PRIu64 ","
        "\"gpu_render_us\":%" PRIu64 ","
        "\"render_submit_us\":%" PRIu64 ","
        "\"render_present_us\":%" PRIu64 ","
        "\"input_to_present_us\":%" PRIu64 ","
        "\"input_sample_us\":%" PRIu64 ","
        "\"sim_start_us\":%" PRIu64 ","
        "\"sim_end_us\":%" PRIu64 ","
        "\"render_submit_start_us\":%" PRIu64 ","
        "\"render_submit_end_us\":%" PRIu64 ","
        "\"present_start_us\":%" PRIu64 ","
        "\"present_end_us\":%" PRIu64 ","
        "\"driver_start_us\":%" PRIu64 ","
        "\"driver_end_us\":%" PRIu64 ","
        "\"os_render_queue_start_us\":%" PRIu64 ","
        "\"os_render_queue_end_us\":%" PRIu64 ","
        "\"gpu_render_start_us\":%" PRIu64 ","
        "\"gpu_render_end_us\":%" PRIu64 "}",
        static_cast<long>(::getpid()),
        telemetry_session_id(),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        report.presentID,
        quality,
        timing_count,
        sample_count,
        driver_report_count,
        driver_report_duplicate_count,
        marker_bits,
        advertised ? "true" : "false",
        requested ? "true" : "false",
        functions_available ? "true" : "false",
        gpu_frame_time_us,
        gpu_render_us,
        render_submit_us,
        render_present_us,
        input_to_present_us,
        report.inputSampleTimeUs,
        report.simStartTimeUs,
        report.simEndTimeUs,
        report.renderSubmitStartTimeUs,
        report.renderSubmitEndTimeUs,
        report.presentStartTimeUs,
        report.presentEndTimeUs,
        report.driverStartTimeUs,
        report.driverEndTimeUs,
        report.osRenderQueueStartTimeUs,
        report.osRenderQueueEndTimeUs,
        report.gpuRenderStartTimeUs,
        report.gpuRenderEndTimeUs);

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

// Present-to-present frametime. This is the only cadence signal that works for
// every Vulkan app regardless of Reflex/markers, so it is the lowest tier of the
// quality ladder (quality=present-frametime). It counts *presented* frames, so
// with frame generation active it reflects presented FPS, not the real rendered
// frame cadence -- callers must not treat it as input-bearing latency.
void send_present_pacing_sample(
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t present_frametime_us,
    uint64_t present_count) {
    char line[512]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"timing\",\"measurement\":\"present-pacing\","
        "\"pid\":%ld,"
        "\"session_id\":\"%s\","
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"quality\":\"present-frametime\","
        "\"present_count\":%" PRIu64 ","
        "\"present_frametime_us\":%" PRIu64 "}",
        static_cast<long>(::getpid()),
        telemetry_session_id(),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        present_count,
        present_frametime_us);

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

void send_display_latency_sample(
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t present_id,
    uint64_t display_latency_us) {
    char line[512]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"timing\",\"measurement\":\"display-latency\","
        "\"pid\":%ld,"
        "\"session_id\":\"%s\","
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"quality\":\"present-wait\","
        "\"present_id\":%" PRIu64 ","
        "\"display_latency_us\":%" PRIu64 "}",
        static_cast<long>(::getpid()),
        telemetry_session_id(),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        present_id,
        display_latency_us);

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

// Diagnostic: what the inject path decided for a present.
void send_inject_debug_event(
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t present_count,
    bool chain_has_present_id,
    bool present_id_at_head,
    bool want_inject,
    uint64_t injected_present_id) {
    char line[512]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"status\",\"event\":\"present-id-inject\",\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"count\":%" PRIu64 ","
        "\"chain_has_present_id\":%s,\"present_id_at_head\":%s,"
        "\"want_inject\":%s,\"injected_present_id\":%" PRIu64 "}",
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        present_count,
        chain_has_present_id ? "true" : "false",
        present_id_at_head ? "true" : "false",
        want_inject ? "true" : "false",
        injected_present_id);
    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

// Diagnostic: result of a vkWaitForPresentKHR call (so wait failures are visible).
void send_wait_result_debug(
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t present_id,
    int result,
    uint64_t display_latency_us) {
    char line[512]{};
    int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"status\",\"event\":\"present-wait-result\",\"pid\":%ld,"
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"present_id\":%" PRIu64 ",\"result\":%d,"
        "\"display_latency_us\":%" PRIu64 "}",
        static_cast<long>(::getpid()),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        present_id,
        result,
        display_latency_us);
    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

// Measures the present->scanout tail without touching the app's Vulkan work: a
// dedicated thread blocks in vkWaitForPresentKHR on present IDs the app already
// supplied, and timestamps when each present is latched/displayed. Owned per
// VkDevice in g_display_waiters and torn down before any swapchain/device the
// app destroys, so no wait is ever in flight across a destroy.
class DisplayLatencyWaiter {
public:
    DisplayLatencyWaiter(VkDevice device, PFN_vkWaitForPresentKHR wait_fn)
        : device_(device),
          wait_fn_(wait_fn),
          thread_([this]() {
              pthread_setname_np(pthread_self(), "pb-present-wait");
              run();
          }) {}

    ~DisplayLatencyWaiter() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stop_ = true;
        }
        cv_.notify_all();
        if (thread_.joinable()) {
            thread_.join();
        }
    }

    DisplayLatencyWaiter(const DisplayLatencyWaiter&) = delete;
    DisplayLatencyWaiter& operator=(const DisplayLatencyWaiter&) = delete;

    void enqueue(
        VkSwapchainKHR swapchain,
        uint64_t present_id,
        uint64_t submit_us) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (stop_ || disabled_) {
                return;
            }
            if (pending_.size() >= kMaxPending) {
                pending_.pop_front();
            }
            pending_.push_back(Pending{swapchain, present_id, submit_us});
        }
        cv_.notify_one();
    }

private:
    struct Pending {
        VkSwapchainKHR swapchain = VK_NULL_HANDLE;
        uint64_t present_id = 0;
        uint64_t submit_us = 0;
    };

    // Keep the backlog shallow: if the wait thread falls behind the present
    // cadence the oldest entries are dropped rather than reporting stale tails.
    static constexpr size_t kMaxPending = 8;
    // Bound each wait so a stalled swapchain (or teardown) cannot wedge the
    // thread; on timeout the sample is simply skipped.
    static constexpr uint64_t kWaitTimeoutNs = 200ull * 1000ull * 1000ull;
    // Present->scanout is a sub-frame tail; anything larger is a backlog/clock
    // artifact, not a real display latency, so discard it.
    static constexpr uint64_t kMaxPlausibleUs = 100ull * 1000ull;
    static constexpr uint64_t kErrorDisableThreshold = 16;

    void run() {
        for (;;) {
            Pending item;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                cv_.wait(lock, [this]() {
                    return stop_ || !pending_.empty();
                });
                if (stop_ && pending_.empty()) {
                    return;
                }
                if (disabled_) {
                    pending_.clear();
                    if (stop_) {
                        return;
                    }
                    continue;
                }
                item = pending_.front();
                pending_.pop_front();
            }

            const VkResult result = wait_fn_(
                device_, item.swapchain, item.present_id, kWaitTimeoutNs);
            const uint64_t now_us = now_monotonic_us();
            const uint64_t latency_us =
                now_us > item.submit_us ? now_us - item.submit_us : 0;
            // Diagnostic: surface the first handful of wait results (success or
            // failure) so we can see whether the wait path is the blocker.
            if (processed_count_ < 8 || (processed_count_ % 600) == 0) {
                send_wait_result_debug(
                    device_,
                    item.swapchain,
                    item.present_id,
                    static_cast<int>(result),
                    latency_us);
            }
            ++processed_count_;
            if (result == VK_SUCCESS) {
                if (latency_us > 0 && latency_us <= kMaxPlausibleUs) {
                    send_display_latency_sample(
                        device_,
                        item.swapchain,
                        item.present_id,
                        latency_us);
                }
                error_count_ = 0;
            } else if (
                result == VK_TIMEOUT || result == VK_SUBOPTIMAL_KHR
                || result == VK_ERROR_OUT_OF_DATE_KHR) {
                // Transient: no usable timestamp, but not a reason to give up.
            } else if (++error_count_ >= kErrorDisableThreshold) {
                std::lock_guard<std::mutex> lock(mutex_);
                disabled_ = true;
            }
        }
    }

    VkDevice device_;
    PFN_vkWaitForPresentKHR wait_fn_;
    std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<Pending> pending_;
    bool stop_ = false;
    bool disabled_ = false;
    uint64_t error_count_ = 0;
    uint64_t processed_count_ = 0;
    std::thread thread_;
};

std::mutex g_display_waiters_mutex;

std::unordered_map<VkDevice, std::unique_ptr<DisplayLatencyWaiter>>
    g_display_waiters;

void display_latency_enqueue(
    VkDevice device,
    PFN_vkWaitForPresentKHR wait_fn,
    VkSwapchainKHR swapchain,
    uint64_t present_id,
    uint64_t submit_us) {
    if (!wait_fn || !present_id) {
        return;
    }
    std::lock_guard<std::mutex> lock(g_display_waiters_mutex);
    std::unique_ptr<DisplayLatencyWaiter>& slot = g_display_waiters[device];
    if (!slot) {
        slot = std::make_unique<DisplayLatencyWaiter>(device, wait_fn);
    }
    slot->enqueue(swapchain, present_id, submit_us);
}

// Stop and join the device's wait thread, returning before the caller destroys
// the swapchain/device. Any in-flight wait targets a still-live swapchain (the
// app has not yet called the driver's destroy), so the join is bounded by the
// present completing or kWaitTimeoutNs. Recreated lazily on the next present.
void display_latency_stop_device(VkDevice device) {
    std::unique_ptr<DisplayLatencyWaiter> waiter;
    {
        std::lock_guard<std::mutex> lock(g_display_waiters_mutex);
        auto it = g_display_waiters.find(device);
        if (it == g_display_waiters.end()) {
            return;
        }
        waiter = std::move(it->second);
        g_display_waiters.erase(it);
    }
    // waiter destructor joins the thread here, outside the lock.
}

const char* latency_marker_name(VkLatencyMarkerNV marker) {
    switch (marker) {
        case VK_LATENCY_MARKER_RENDERSUBMIT_START_NV:
            return "rendersubmit-start";
        case VK_LATENCY_MARKER_PRESENT_START_NV:
            return "present-start";
        case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_START_NV:
            return "oob-present-start";
        default:
            return "other";
    }
}

bool is_base_frame_marker(VkLatencyMarkerNV marker) {
    return marker == VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_START_NV
        || marker == VK_LATENCY_MARKER_PRESENT_START_NV
        || marker == VK_LATENCY_MARKER_RENDERSUBMIT_START_NV;
}

BaseMarkerState* base_marker_state_for(
    SwapchainContext& context,
    VkLatencyMarkerNV marker) {
    switch (marker) {
        case VK_LATENCY_MARKER_OUT_OF_BAND_PRESENT_START_NV:
            return &context.out_of_band_present_start;
        case VK_LATENCY_MARKER_PRESENT_START_NV:
            return &context.present_start;
        case VK_LATENCY_MARKER_RENDERSUBMIT_START_NV:
            return &context.render_submit_start;
        default:
            return nullptr;
    }
}

void send_base_frame_marker_sample(
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t base_frame_id,
    uint64_t base_frame_frametime_us,
    VkLatencyMarkerNV marker) {
    char line[640]{};
    const int length = std::snprintf(
        line,
        sizeof(line),
        "{\"v\":1,\"type\":\"timing\","
        "\"measurement\":\"base-frame-marker-pacing\","
        "\"pid\":%ld,"
        "\"session_id\":\"%s\","
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"quality\":\"base-frame-marker\","
        "\"base_frame_id\":%" PRIu64 ","
        "\"base_frame_frametime_us\":%" PRIu64 ","
        "\"marker\":%d,"
        "\"marker_name\":\"%s\"}",
        static_cast<long>(::getpid()),
        telemetry_session_id(),
        handle_to_u64(device),
        handle_to_u64(swapchain),
        base_frame_id,
        base_frame_frametime_us,
        static_cast<int>(marker),
        latency_marker_name(marker));

    if (length > 0 && static_cast<size_t>(length) < sizeof(line)) {
        g_socket.send_line(line, static_cast<size_t>(length));
    }
}

void observe_latency_marker(
    VkDevice device,
    VkSwapchainKHR swapchain,
    const VkSetLatencyMarkerInfoNV* marker_info) {
    if (!marker_info || !marker_info->presentID
        || !is_base_frame_marker(marker_info->marker)) {
        return;
    }

    const uint64_t marker_time_us = now_monotonic_us();
    uint64_t base_frame_frametime_us = 0;
    {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it == g_swapchains.end()) {
            return;
        }

        auto* state = base_marker_state_for(swapchain_it->second, marker_info->marker);
        if (!state) {
            return;
        }

        if (state->frame_id == marker_info->presentID) {
            return;
        }

        if (state->timestamp_us && marker_time_us > state->timestamp_us) {
            base_frame_frametime_us =
                marker_time_us - state->timestamp_us;
        }

        state->frame_id = marker_info->presentID;
        state->timestamp_us = marker_time_us;
    }

    if (base_frame_frametime_us) {
        send_base_frame_marker_sample(
            device,
            swapchain,
            marker_info->presentID,
            base_frame_frametime_us,
            marker_info->marker);
    }
}

struct LatencySleepModeReplay {
    PFN_vkSetLatencySleepModeNV set_latency_sleep_mode = nullptr;
    VkLatencySleepModeInfoNV sleep_mode_info{};
    bool has_sleep_mode_info = false;
    uint64_t count = 0;
    DeviceTelemetryFlags flags{};
};

LatencySleepModeReplay load_latency_sleep_mode_replay(VkDevice device) {
    LatencySleepModeReplay replay{};
    std::lock_guard lock(g_mutex);
    auto device_it = g_devices.find(device);
    if (device_it == g_devices.end()) {
        return replay;
    }

    auto& context = device_it->second;
    replay.set_latency_sleep_mode = context.set_latency_sleep_mode_nv;
    replay.has_sleep_mode_info = context.has_latency_sleep_mode_info;
    if (!replay.set_latency_sleep_mode || !replay.has_sleep_mode_info) {
        return replay;
    }
    replay.sleep_mode_info = context.latency_sleep_mode_info;
    replay.count = ++context.latency_sleep_mode_reapply_count;
    replay.flags = {
        context.low_latency_extension_advertised,
        context.low_latency_extension_requested,
        context.low_latency_functions_available,
        context.marker_count,
    };
    return replay;
}

bool reapply_latency_sleep_mode(
    const char* event,
    VkDevice device,
    VkSwapchainKHR swapchain,
    uint64_t duplicate_count,
    uint64_t present_count) {
    LatencySleepModeReplay replay = load_latency_sleep_mode_replay(device);
    if (!replay.set_latency_sleep_mode || !replay.has_sleep_mode_info) {
        return false;
    }

    VkResult result =
        replay.set_latency_sleep_mode(device, swapchain, &replay.sleep_mode_info);
    send_latency_sleep_mode_event(
        event,
        device,
        swapchain,
        replay.count,
        result,
        true,
        replay.sleep_mode_info,
        duplicate_count,
        present_count,
        replay.flags);
    return result == VK_SUCCESS;
}

void maybe_recover_stale_latency_stream(VkDevice device, VkSwapchainKHR swapchain) {
    PFN_vkSetLatencySleepModeNV set_latency_sleep_mode = nullptr;
    VkLatencySleepModeInfoNV sleep_mode_info{};
    bool has_sleep_mode_info = false;
    uint64_t attempt_count = 0;
    uint64_t duplicate_count = 0;
    uint64_t present_count = 0;
    DeviceTelemetryFlags flags{};

    {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it == g_swapchains.end()) {
            return;
        }
        auto& swapchain_context = swapchain_it->second;
        duplicate_count = swapchain_context.duplicate_driver_report_count;
        if (duplicate_count < kStaleRecoveryDuplicateThreshold) {
            return;
        }
        if (swapchain_context.last_latency_recovery_duplicate_count
            && duplicate_count - swapchain_context.last_latency_recovery_duplicate_count
                < kStaleRecoveryDuplicateInterval) {
            return;
        }
        swapchain_context.last_latency_recovery_duplicate_count = duplicate_count;
        attempt_count = ++swapchain_context.latency_recovery_attempt_count;
        present_count = swapchain_context.present_count;

        auto device_it = g_devices.find(device);
        if (device_it != g_devices.end()) {
            const auto& device_context = device_it->second;
            set_latency_sleep_mode = device_context.set_latency_sleep_mode_nv;
            has_sleep_mode_info = device_context.has_latency_sleep_mode_info;
            sleep_mode_info = device_context.latency_sleep_mode_info;
            flags = {
                device_context.low_latency_extension_advertised,
                device_context.low_latency_extension_requested,
                device_context.low_latency_functions_available,
                device_context.marker_count,
            };
        }
    }

    send_flow_state_event(
        "latency-stream-stale",
        device,
        swapchain,
        VK_NULL_HANDLE,
        attempt_count,
        VK_NOT_READY,
        -1,
        0,
        flow_snapshot(device, swapchain));

    if (!set_latency_sleep_mode || !has_sleep_mode_info) {
        send_latency_sleep_mode_event(
            "latency-recovery-unavailable",
            device,
            swapchain,
            attempt_count,
            VK_ERROR_EXTENSION_NOT_PRESENT,
            has_sleep_mode_info,
            sleep_mode_info,
            duplicate_count,
            present_count,
            flags);
        return;
    }

    VkLatencySleepModeInfoNV disabled_sleep_mode_info = sleep_mode_info;
    disabled_sleep_mode_info.lowLatencyMode = VK_FALSE;
    disabled_sleep_mode_info.lowLatencyBoost = VK_FALSE;
    VkResult disable_result =
        set_latency_sleep_mode(device, swapchain, &disabled_sleep_mode_info);
    send_latency_sleep_mode_event(
        "latency-recovery-disable-sleep-mode",
        device,
        swapchain,
        attempt_count,
        disable_result,
        true,
        disabled_sleep_mode_info,
        duplicate_count,
        present_count,
        flags);

    if (env_flag_enabled(kRecoveryResetEnv)) {
        send_latency_sleep_mode_event(
            "latency-recovery-reset-sleep-mode-enter",
            device,
            swapchain,
            attempt_count,
            VK_NOT_READY,
            false,
            disabled_sleep_mode_info,
            duplicate_count,
            present_count,
            flags);
        VkResult reset_result = set_latency_sleep_mode(device, swapchain, nullptr);
        send_latency_sleep_mode_event(
            "latency-recovery-reset-sleep-mode",
            device,
            swapchain,
            attempt_count,
            reset_result,
            false,
            disabled_sleep_mode_info,
            duplicate_count,
            present_count,
            flags);
    }

    VkResult result = set_latency_sleep_mode(device, swapchain, &sleep_mode_info);
    send_latency_sleep_mode_event(
        "latency-recovery-reapply-sleep-mode",
        device,
        swapchain,
        attempt_count,
        result,
        true,
        sleep_mode_info,
        duplicate_count,
        present_count,
        flags);
}

void query_latency_timing(VkDevice device, VkSwapchainKHR swapchain) {
    PFN_vkGetLatencyTimingsNV get_latency_timings = nullptr;
    {
        std::lock_guard lock(g_mutex);
        auto device_it = g_devices.find(device);
        if (device_it == g_devices.end()) {
            return;
        }
        get_latency_timings = device_it->second.get_latency_timings_nv;
    }

    if (!get_latency_timings) {
        uint64_t count = 0;
        {
            std::lock_guard lock(g_mutex);
            auto swapchain_it = g_swapchains.find(swapchain);
            if (swapchain_it != g_swapchains.end()) {
                count = ++swapchain_it->second.timing_unavailable_count;
            }
        }
        if (should_report_counter(count)) {
            send_status_event(
                "latency-timing-unavailable",
                device,
                swapchain,
                count,
                device_telemetry_flags(device));
        }
        return;
    }

    VkGetLatencyMarkerInfoNV info{};
    info.sType = VK_STRUCTURE_TYPE_GET_LATENCY_MARKER_INFO_NV;
    get_latency_timings(device, swapchain, &info);
    if (info.timingCount == 0) {
        uint64_t count = 0;
        {
            std::lock_guard lock(g_mutex);
            auto swapchain_it = g_swapchains.find(swapchain);
            if (swapchain_it != g_swapchains.end()) {
                count = ++swapchain_it->second.timing_empty_count;
            }
        }
        if (should_report_counter(count)) {
            send_status_event(
                "latency-timing-empty",
                device,
                swapchain,
                count,
                device_telemetry_flags(device));
        }
        return;
    }

    constexpr uint32_t kMaxTimings = 64;
    const uint32_t timing_count = std::min(info.timingCount, kMaxTimings);
    std::array<VkLatencyTimingsFrameReportNV, kMaxTimings> reports{};
    for (uint32_t i = 0; i < timing_count; ++i) {
        reports[i].sType = VK_STRUCTURE_TYPE_LATENCY_TIMINGS_FRAME_REPORT_NV;
    }

    info.timingCount = timing_count;
    info.pTimings = reports.data();
    get_latency_timings(device, swapchain, &info);
    if (info.timingCount == 0) {
        uint64_t count = 0;
        {
            std::lock_guard lock(g_mutex);
            auto swapchain_it = g_swapchains.find(swapchain);
            if (swapchain_it != g_swapchains.end()) {
                count = ++swapchain_it->second.timing_empty_count;
            }
        }
        if (should_report_counter(count)) {
            send_status_event(
                "latency-timing-empty",
                device,
                swapchain,
                count,
                device_telemetry_flags(device));
        }
        return;
    }

    const uint32_t written_count = std::min(info.timingCount, timing_count);

    // The driver returns a ring of up to kMaxTimings frame reports, oldest
    // first. Emitting only the newest report re-latches the same value every
    // present once the Reflex ring stops advancing, which is what produced the
    // stuck constant latency. Instead, forward every report whose presentID is
    // newer than the last one we already emitted for this swapchain, so the
    // receiver sees genuine per-frame samples.
    std::vector<uint32_t> usable_indices;
    usable_indices.reserve(written_count);
    for (uint32_t index = 0; index < written_count; ++index) {
        const auto& report = reports[index];
        const bool usable = report.presentID || report_has_driver_timing(report)
            || report_has_reflex_markers(report);
        if (!usable) {
            continue;
        }
        usable_indices.push_back(index);
    }

    uint64_t last_emitted = 0;
    {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end()) {
            last_emitted = swapchain_it->second.last_emitted_present_id;
        }
    }

    bool has_newest = false;
    uint32_t newest_index = 0;
    uint64_t newest_present_id = 0;
    bool saw_lower_present_id = false;
    bool saw_higher_present_id = false;
    for (uint32_t index : usable_indices) {
        const uint64_t present_id = reports[index].presentID;
        if (!has_newest || present_id >= newest_present_id) {
            has_newest = true;
            newest_index = index;
            newest_present_id = present_id;
        }
        if (last_emitted && present_id && present_id < last_emitted) {
            saw_lower_present_id = true;
        }
        if (present_id > last_emitted) {
            saw_higher_present_id = true;
        }
    }

    // A frozen ring keeps its newest entry equal to last_emitted; that is
    // staleness, not a presentID reset. Treating it as a reset re-emits all
    // 64 stale reports every present (observed live at ~3.5k samples/s on
    // 2026-06-11) and defeats the receiver's duplicate detection because the
    // IDs cycle. Only call it a reset when even the newest ID went backwards.
    const bool reset_detected =
        last_emitted && saw_lower_present_id && !saw_higher_present_id
        && newest_present_id < last_emitted;
    const uint64_t emit_after = reset_detected ? 0 : last_emitted;
    uint64_t new_last_emitted = reset_detected ? 0 : last_emitted;
    bool emitted_fresh = false;
    for (uint32_t index : usable_indices) {
        const uint64_t present_id = reports[index].presentID;
        if (present_id && present_id <= emit_after) {
            continue;
        }
        send_timing_sample(device, swapchain, reports[index], written_count);
        emitted_fresh = true;
        if (present_id > new_last_emitted) {
            new_last_emitted = present_id;
        }
    }

    if (new_last_emitted != last_emitted || reset_detected) {
        std::lock_guard lock(g_mutex);
        auto swapchain_it = g_swapchains.find(swapchain);
        if (swapchain_it != g_swapchains.end()) {
            swapchain_it->second.last_emitted_present_id = new_last_emitted;
        }
    }

    // Nothing newer than last time: the Reflex ring has stopped advancing.
    // Re-emit the newest report once so the receiver's duplicate-report
    // detection can flag the stream as stale instead of us silently dropping
    // it (which would otherwise leave the meter showing a frozen value).
    if (!emitted_fresh && has_newest) {
        send_timing_sample(device, swapchain, reports[newest_index], written_count);
        maybe_recover_stale_latency_stream(device, swapchain);
    }
}

}  // namespace pblayer
