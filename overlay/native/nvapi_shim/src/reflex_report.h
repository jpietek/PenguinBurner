#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdio>

namespace pb_reflex {

// NV_LATENCY_RESULT_PARAMS_V1, NVIDIA/nvapi nvapi.h. The V1 reserved tail
// gained named fields over time without changing this ABI's 240-byte stride.
struct FrameReport {
    uint64_t frame_id;
    uint64_t input_sample;
    uint64_t sim_start;
    uint64_t sim_end;
    uint64_t submit_start;
    uint64_t submit_end;
    uint64_t present_start;
    uint64_t present_end;
    uint64_t driver_start;
    uint64_t driver_end;
    uint64_t queue_start;
    uint64_t queue_end;
    uint64_t gpu_start;
    uint64_t gpu_end;
    uint32_t gpu_active_us;
    uint32_t gpu_frame_us;
    uint64_t camera_constructed;
    uint32_t cross_adapter_us;
    uint32_t ai_frame_us;
    uint8_t reserved[104];
};

struct LatencyResults {
    uint32_t version;
    FrameReport frames[64];
    uint8_t reserved[32];
};
static_assert(sizeof(FrameReport) == 240);
static_assert(offsetof(FrameReport, gpu_end) == 104);
static_assert(offsetof(LatencyResults, frames) == 8);
static_assert(sizeof(LatencyResults) == 15400);
constexpr uint32_t kVersion = sizeof(LatencyResults) | (1u << 16);
constexpr size_t kLineBytes = 1024;

// GetLatency returns a rolling history, oldest first. Emit only the newest
// nonempty report; the receiver deduplicates repeated frame IDs per device.
inline int format_latest(const LatencyResults& results, uint32_t pid,
                         uintptr_t device, char* out, size_t capacity) {
    if (results.version != kVersion) return 0;
    for (int i = 63; i >= 0; --i) {
        const auto& r = results.frames[i];
        if (!r.frame_id) continue;
        const int length = std::snprintf(out, capacity,
            "[pb-reflex-report] {\"pid\":%u,\"device\":\"%llx\","
            "\"present_id\":%llu,\"input_sample_us\":%llu,"
            "\"sim_start_us\":%llu,\"sim_end_us\":%llu,"
            "\"render_submit_start_us\":%llu,\"render_submit_end_us\":%llu,"
            "\"present_start_us\":%llu,\"present_end_us\":%llu,"
            "\"driver_start_us\":%llu,\"driver_end_us\":%llu,"
            "\"os_render_queue_start_us\":%llu,\"os_render_queue_end_us\":%llu,"
            "\"gpu_render_start_us\":%llu,\"gpu_render_end_us\":%llu}\n",
            static_cast<unsigned>(pid), static_cast<unsigned long long>(device),
            static_cast<unsigned long long>(r.frame_id),
            static_cast<unsigned long long>(r.input_sample),
            static_cast<unsigned long long>(r.sim_start),
            static_cast<unsigned long long>(r.sim_end),
            static_cast<unsigned long long>(r.submit_start),
            static_cast<unsigned long long>(r.submit_end),
            static_cast<unsigned long long>(r.present_start),
            static_cast<unsigned long long>(r.present_end),
            static_cast<unsigned long long>(r.driver_start),
            static_cast<unsigned long long>(r.driver_end),
            static_cast<unsigned long long>(r.queue_start),
            static_cast<unsigned long long>(r.queue_end),
            static_cast<unsigned long long>(r.gpu_start),
            static_cast<unsigned long long>(r.gpu_end));
        return length > 0 && static_cast<size_t>(length) < capacity ? length : 0;
    }
    return 0;
}
}  // namespace pb_reflex
