// Markerless frame-cycle estimates with calibrated GPU completion.
#include "latency_layer_internal.h"
#include <cmath>

namespace pblayer {
bool frame_completion_enabled(const DeviceContext& device) {
    return device.get_calibrated_timestamps && timing_queries_enabled()
        && !env_value_is_false(std::getenv("PB_INGAME_LATENCY"));
}

// The previous Present entry is a frame-cycle proxy, not an engine input event.
// Pair it with this frame's GPU completion, using one calibrated clock pair.
// Reading the query on image reuse must not add the readback delay to latency.
uint64_t calibrated_frame_latency_us(const FrameCompletionSample& sample,
    uint64_t gpu_ticks, uint64_t calibrated_ticks, uint64_t calibrated_cpu_ns,
    uint64_t deviation_ns, double period_ns, uint32_t valid_bits) {
    if (!sample.presented || !sample.cycle_start_us || sample.present_us <= sample.cycle_start_us
        || sample.present_us - sample.cycle_start_us > 1000000 || deviation_ns > 500000
        || !std::isfinite(period_ns) || period_ns <= 0 || !valid_bits || valid_bits > 64) return 0;
    const uint64_t mask = UINT64_MAX >> (64 - valid_bits);
    const uint64_t age_ticks = (calibrated_ticks - gpu_ticks) & mask;
    if (age_ticks > (mask >> 1)) return 0;
    const long double age_ns = static_cast<long double>(age_ticks) * period_ns;
    if (age_ns > 3000000000.0L || age_ns >= calibrated_cpu_ns) return 0;
    const long double end_us = (calibrated_cpu_ns - age_ns) / 1000.0L;
    if (end_us < sample.present_us || end_us <= sample.cycle_start_us) return 0;
    const auto latency = end_us - sample.cycle_start_us;
    return latency <= 1000000.0L ? static_cast<uint64_t>(latency) : 0;
}

void collect_frame_completion(const DeviceContext& device, VkSwapchainKHR swapchain,
    OverlayRenderResources& overlay, uint32_t image_index) {
    if (!overlay.frame_queries || image_index >= overlay.frame_samples.size()) return;
    const auto sample = overlay.frame_samples[image_index];
    overlay.frame_samples[image_index] = {};
    if (!sample.presented || !sample.cycle_start_us) return;
    uint64_t gpu_ticks = 0;
    // Called after the slot's submit fence retired. Never use WAIT_BIT.
    if (device.get_query_pool_results(device.device, overlay.frame_queries, image_index, 1,
        sizeof(gpu_ticks), &gpu_ticks, sizeof(gpu_ticks), VK_QUERY_RESULT_64_BIT) != VK_SUCCESS) return;
    const VkCalibratedTimestampInfoEXT infos[] = {
        {VK_STRUCTURE_TYPE_CALIBRATED_TIMESTAMP_INFO_EXT, nullptr, VK_TIME_DOMAIN_DEVICE_EXT},
        {VK_STRUCTURE_TYPE_CALIBRATED_TIMESTAMP_INFO_EXT, nullptr, VK_TIME_DOMAIN_CLOCK_MONOTONIC_EXT},
    };
    uint64_t clocks[2]{}, deviation = 0;
    if (device.get_calibrated_timestamps(device.device, 2, infos, clocks, &deviation) != VK_SUCCESS) return;
    const uint64_t latency = calibrated_frame_latency_us(sample, gpu_ticks, clocks[0], clocks[1],
        deviation, device.physical_device_properties.limits.timestampPeriod,
        device.queue_family_properties[overlay.queue_family_index].timestampValidBits);
    if (!latency) return;
    char line[640]{};
    const int size = std::snprintf(line, sizeof(line),
        "{\"v\":1,\"type\":\"timing\",\"measurement\":\"frame-cycle-gpu\","
        "\"source\":\"vulkan-overlay\",\"pid\":%ld,\"session_id\":\"%s\","
        "\"device\":\"0x%016" PRIx64 "\",\"swapchain\":\"0x%016" PRIx64 "\","
        "\"present_count\":%" PRIu64 ",\"frame_cycle_gpu_us\":%" PRIu64 "}",
        static_cast<long>(::getpid()), telemetry_session_id(), handle_to_u64(device.device),
        handle_to_u64(swapchain), sample.present_count, latency);
    if (size > 0 && static_cast<size_t>(size) < sizeof(line)) g_socket.send_line(line, size);
}

} // namespace pblayer
