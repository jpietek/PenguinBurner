"""Graph selection is live and display samples never reuse Present-call data."""
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from overlay.config import OverlayConfig, load_overlay_config, save_overlay_config
from overlay.control import change_overlay_graph_mode


def test_graph_cycle_preserves_custom_items_and_appearance(tmp_path):
    path = tmp_path / 'overlay.toml'
    config = OverlayConfig(enabled=True, scale=0.75, enabled_item_ids=('base_fps', 'frametime_graph'))
    save_overlay_config(config, path)
    before = load_overlay_config(path)
    for mode in ('display', 'both', 'base'):
        result = change_overlay_graph_mode(config_path=path)
        assert result.as_json()['graph_mode'] == mode
        assert load_overlay_config(path) == replace(before, graph_mode=mode)
    with pytest.raises(ValueError):
        change_overlay_graph_mode('fake-smoothed', path)
    assert load_overlay_config(path) == before


def test_graph_selector_persists_both(qtbot, tmp_path):
    from PySide6 import QtCore, QtWidgets

    from ui.components.overlay_config import OverlayConfigPanel
    path = tmp_path / 'overlay.toml'
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=path)
    qtbot.addWidget(panel.widget)
    panel.graph_mode_combo.setCurrentIndex(2)
    assert load_overlay_config(path).graph_mode == 'both'
    assert panel.preview._config.graph_mode == 'both'


def test_native_display_timestamp_filter_and_ring(tmp_path):
    compiler = shutil.which('c++')
    if not compiler:
        pytest.skip('C++ compiler unavailable')
    source = Path('overlay/native/latency_layer/src').resolve()
    harness = tmp_path / 'display.cpp'
    harness.write_text(r'''
#include "latency_layer_internal.h"
#include <cassert>
using namespace pblayer;
#ifdef VK_EXT_present_timing
static uint32_t supported_stage = VK_PRESENT_STAGE_QUEUE_OPERATIONS_END_BIT_EXT;
static VkResult VKAPI_CALL caps(VkPhysicalDevice, const VkPhysicalDeviceSurfaceInfo2KHR*, VkSurfaceCapabilities2KHR* out) {
    auto* timing = static_cast<VkPresentTimingSurfaceCapabilitiesEXT*>(out->pNext);
    timing->presentTimingSupported = VK_TRUE;
    timing->presentStageQueries = supported_stage;
    static_cast<VkSurfaceCapabilitiesPresentId2KHR*>(timing->pNext)->presentId2Supported = VK_TRUE;
    return VK_SUCCESS;
}
static PFN_vkVoidFunction VKAPI_CALL proc(VkInstance, const char*) {
    return reinterpret_cast<PFN_vkVoidFunction>(caps);
}
#endif
int main() {
#ifdef VK_EXT_present_timing
    bool queue_only = false;
    assert(display_timing_surface_stage({}, proc, {}, {}, &queue_only) == 0 && queue_only);
    supported_stage = 0; // No stage at all: not an X11 symptom, no Wayland hint.
    assert(display_timing_surface_stage({}, proc, {}, {}, &queue_only) == 0 && !queue_only);
    supported_stage = VK_PRESENT_STAGE_QUEUE_OPERATIONS_END_BIT_EXT | VK_PRESENT_STAGE_REQUEST_DEQUEUED_BIT_EXT;
    assert(display_timing_surface_stage({}, proc, {}, {}, &queue_only) == 0 && !queue_only);
    supported_stage = VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT;
    assert(display_timing_surface_stage({}, proc, {}, {}, &queue_only) == supported_stage && !queue_only);
    supported_stage |= VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_VISIBLE_BIT_EXT;
    assert(display_timing_surface_stage({}, proc, {}, {}) == VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_VISIBLE_BIT_EXT);
#endif
    DisplayTimingState s{};
    record_display_timestamp(s, 1000000000, 1000);
    record_display_timestamp(s, 1010000000, 11000);
    record_display_timestamp(s, 1020000000, 21000);
    assert(s.count == 2 && s.samples[0] == 10000 && s.samples[1] == 10000);
    record_display_timestamp(s, 0, 22000); // discarded
    record_display_timestamp(s, 1020000000, 23000); // duplicate
    record_display_timestamp(s, 1015000000, 24000); // out of order
    assert(s.count == 2);
    record_display_timestamp(s, 3000000000, 3024001); // new window after stale feedback
    assert(s.count == 0);
    for (unsigned i = 1; i <= 517; ++i)
        record_display_timestamp(s, 3000000000ull + i * 10000000ull, 3024001 + i * 10000);
    assert(s.count == 512 && s.index == 5);
    for (auto us : s.samples) assert(us == 10000);
}
''')
    output = tmp_path / 'probe'
    subprocess.run([compiler, '-std=c++17', '-ffunction-sections', '-fdata-sections',
                    '-Wl,--gc-sections', '-I', str(source.parent / 'vendor/vulkan-headers/include'),
                    '-I', str(source), str(harness),
                    str(source / 'display_timing.cpp'), '-o', str(output)], check=True)
    subprocess.run([str(output)], check=True)


def test_native_passive_feedback_preserves_application_queue(tmp_path):
    compiler = shutil.which('c++')
    if not compiler:
        pytest.skip('C++ compiler unavailable')
    source = Path('overlay/native/latency_layer/src').resolve()
    harness = tmp_path / 'passive.cpp'
    harness.write_text(r'''
#include "latency_layer_internal.h"
#include <cassert>
namespace pblayer {
std::mutex g_mutex;
std::unordered_map<VkDevice, DeviceContext> g_devices;
std::unordered_map<VkSwapchainKHR, SwapchainContext> g_swapchains;
uint64_t now_monotonic_us() { return 1000000; }
bool display_debug_enabled() { return false; }
void display_debug_log(const char*, ...) {}
// SwapchainContext owns a worker; these probes never start one.
OverlayWorker::~OverlayWorker() {}
}
using namespace pblayer;
#ifdef VK_EXT_present_timing
static unsigned calls = 0;
static VkResult status = VK_SUCCESS;
static const VkPastPresentationTimingInfoEXT* expected_info;
static VkPastPresentationTimingPropertiesEXT* expected_results;
static VkResult VKAPI_CALL next(VkDevice, const VkPastPresentationTimingInfoEXT* info,
    VkPastPresentationTimingPropertiesEXT* results) {
    assert(info == expected_info && results == expected_results);
    assert(g_mutex.try_lock()); g_mutex.unlock();
    ++calls;
    return status;
}
#endif
int main() {
#ifdef VK_EXT_present_timing
    auto device = reinterpret_cast<VkDevice>(1);
    auto swapchain = reinterpret_cast<VkSwapchainKHR>(2);
    g_devices[device].get_past_timing = next;
    g_swapchains[swapchain].device = device;
    VkPastPresentationTimingInfoEXT info{}; info.swapchain = swapchain;
    VkPresentStageTimeEXT stage{};
    VkPastPresentationTimingEXT report{};
    report.reportComplete = VK_TRUE;
    report.timeDomainId = 42;
    report.presentStageCount = 1; report.pPresentStages = &stage;
    VkPastPresentationTimingPropertiesEXT results{};
    results.presentationTimingCount = 1; results.pPresentationTimings = &report;
    expected_info = &info; expected_results = &results;
    auto& s = g_swapchains[swapchain].display_timing;
    stage.stage = VK_PRESENT_STAGE_QUEUE_OPERATIONS_END_BIT_EXT;
    stage.time = 1000000000;
    assert(layer_get_past_presentation_timing_ext(device, &info, &results) == VK_SUCCESS);
    assert(!s.available && !s.count);
    stage.stage = VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT;
    layer_get_past_presentation_timing_ext(device, &info, &results);
    assert(s.available && s.passive && !s.count && s.domain_id == 42);
    stage.time += 10000000;
    status = VK_INCOMPLETE;
    assert(layer_get_past_presentation_timing_ext(device, &info, &results) == VK_INCOMPLETE);
    assert(s.count == 1 && s.samples[0] == 10000 && calls == 3);
    assert(report.pPresentStages == &stage && report.timeDomainId == 42);
    status = VK_ERROR_DEVICE_LOST;
    stage.time += 10000000;
    assert(layer_get_past_presentation_timing_ext(device, &info, &results) == status);
    assert(s.count == 1);
    status = VK_SUCCESS;
    report.reportComplete = VK_FALSE;
    layer_get_past_presentation_timing_ext(device, &info, &results);
    assert(s.count == 1);
    report.reportComplete = VK_TRUE;
    report.timeDomainId = 43;
    layer_get_past_presentation_timing_ext(device, &info, &results);
    assert(!s.count && s.domain_id == 43);
    stage.stage = VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_VISIBLE_BIT_EXT;
    stage.time += 10000000;
    layer_get_past_presentation_timing_ext(device, &info, &results);
    assert(!s.count && s.stage == stage.stage);
    results.pPresentationTimings = nullptr; // count-only query
    layer_get_past_presentation_timing_ext(device, &info, &results);
    assert(!s.count);
    // Passive state must never trigger a second driver read.
    s.pending = 1;
    unsigned before = calls;
    collect_display_timing(g_devices[device], swapchain, s);
    assert(calls == before);
#endif
}
''')
    output = tmp_path / 'probe'
    subprocess.run([compiler, '-std=c++17', '-ffunction-sections', '-fdata-sections',
                    '-Wl,--gc-sections', '-I', str(source.parent / 'vendor/vulkan-headers/include'),
                    '-I', str(source), str(harness),
                    str(source / 'display_timing.cpp'), '-o', str(output)], check=True)
    subprocess.run([str(output)], check=True)


def test_native_shared_feedback_counts_requests_and_skips_replaced_frames(tmp_path):
    """vkd3d off FIFO: our requests ride its polls, replaced frames never plot."""
    compiler = shutil.which('c++')
    if not compiler:
        pytest.skip('C++ compiler unavailable')
    source = Path('overlay/native/latency_layer/src').resolve()
    harness = tmp_path / 'shared.cpp'
    harness.write_text(r'''
#include "latency_layer_internal.h"
#include <cassert>
namespace pblayer {
std::mutex g_mutex;
std::unordered_map<VkDevice, DeviceContext> g_devices;
std::unordered_map<VkSwapchainKHR, SwapchainContext> g_swapchains;
uint64_t now_monotonic_us() { return 1000000; }
bool display_debug_enabled() { return false; }
void display_debug_log(const char*, ...) {}
// SwapchainContext owns a worker; these probes never start one.
OverlayWorker::~OverlayWorker() {}
}
using namespace pblayer;
#ifdef VK_EXT_present_timing
static unsigned calls = 0;
static VkResult VKAPI_CALL next(VkDevice, const VkPastPresentationTimingInfoEXT*,
    VkPastPresentationTimingPropertiesEXT* results) {
    ++calls;
    if (results) results->presentationTimingCount = 0;
    return VK_SUCCESS;
}
#endif
int main() {
#ifdef VK_EXT_present_timing
    auto device = reinterpret_cast<VkDevice>(1);
    auto swapchain = reinterpret_cast<VkSwapchainKHR>(2);
    g_devices[device].get_past_timing = next;
    g_swapchains[swapchain].device = device;
    auto& s = g_swapchains[swapchain].display_timing;
    s.shared = true; s.available = true; s.pending = 3;
    s.stage = VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT; s.domain_id = 7;
    VkPresentStageTimeEXT stage{};
    stage.stage = VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT;
    VkPastPresentationTimingEXT report{};
    report.reportComplete = VK_TRUE; report.timeDomainId = 7;
    report.presentStageCount = 1; report.pPresentStages = &stage;
    VkPastPresentationTimingPropertiesEXT results{};
    results.presentationTimingCount = 1; results.pPresentationTimings = &report;
    // Replaced frame: request answered, nothing displayed, nothing plotted.
    stage.time = 0;
    observe_display_timing(s, results, 1000000);
    assert(s.pending == 2 && s.count == 0 && s.last_observed_us == 1000000 && s.shared);
    stage.time = 1000000000; observe_display_timing(s, results, 1000000);
    stage.time = 1010000000; observe_display_timing(s, results, 1000000);
    assert(s.pending == 0 && s.count == 1 && s.samples[0] == 10000);
    // Two presents attributed to one scanout produce one interval, not two.
    observe_display_timing(s, results, 1000000);
    assert(s.count == 1 && s.pending == 0);
    // A domain change restarts the ring instead of mixing clocks.
    report.timeDomainId = 8; stage.time = 5000000000ull;
    observe_display_timing(s, results, 1000000);
    assert(s.count == 0 && s.domain_id == 8 && s.shared && s.available);
    // The app polls for us: no self-poll while its feedback keeps arriving.
    s.pending = 8;
    collect_display_timing(g_devices[device], swapchain, s);
    assert(calls == 0);
    // Only when it stopped polling and the request cap is near do we drain.
    s.last_observed_us = 1;
    collect_display_timing(g_devices[device], swapchain, s);
    assert(calls == 1);
#endif
}
''')
    output = tmp_path / 'probe'
    subprocess.run([compiler, '-std=c++17', '-ffunction-sections', '-fdata-sections',
                    '-Wl,--gc-sections', '-I', str(source.parent / 'vendor/vulkan-headers/include'),
                    '-I', str(source), str(harness),
                    str(source / 'display_timing.cpp'), '-o', str(output)], check=True)
    subprocess.run([str(output)], check=True)


def test_legacy_present_selection_becomes_base(tmp_path):
    from overlay.config import normalize_overlay_graph_mode
    assert normalize_overlay_graph_mode("present") == "base"
    path = tmp_path / "overlay.toml"
    result = change_overlay_graph_mode("present", path)
    assert result.graph_mode == "base"
    assert load_overlay_config(path).graph_mode == "base"



def test_native_present_fallback_base_priority_and_shared_clock(tmp_path, build_overlay_probe):
    """Top plot: bridge base frames first, Present calls as the fallback.

    Labels distinguish a source that never existed ("unavailable") from a gap
    after one was seen ("waiting"), and a failed re-calibration keeps the last
    good display-clock offset.
    """
    harness = tmp_path / "fast.cpp"
    harness.write_text(r"""
#include "latency_layer_internal.h"
#include <cassert>
#include <fcntl.h>
#include <sys/mman.h>
using namespace pblayer;
#ifdef VK_EXT_present_timing
static unsigned calibrations = 0;
static uint64_t deviation = 1000;
static VkResult VKAPI_CALL domains(VkDevice, VkSwapchainKHR, VkSwapchainTimeDomainPropertiesEXT* out, uint64_t*) {
    out->timeDomainCount = 1;
    if (out->pTimeDomains) {
        out->pTimeDomains[0] = VK_TIME_DOMAIN_PRESENT_STAGE_LOCAL_EXT;
        out->pTimeDomainIds[0] = 7;
    }
    return VK_SUCCESS;
}
static VkResult VKAPI_CALL calibrate(VkDevice, uint32_t count, const VkCalibratedTimestampInfoEXT* infos,
                                    uint64_t* times, uint64_t* error) {
    assert(count == 2 && infos[1].timeDomain == VK_TIME_DOMAIN_CLOCK_MONOTONIC_EXT);
    const auto* local = static_cast<const VkSwapchainCalibratedTimestampInfoEXT*>(infos[0].pNext);
    assert(local->timeDomainId == 7 && local->presentStage == VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT);
    ++calibrations;
    times[0] = 2000000000; times[1] = 3000000000; *error = deviation;
    return VK_SUCCESS;
}
#endif
int main(int argc, char** argv) {
    assert(argc == 2);
    const auto now = now_monotonic_us();
    SwapchainContext context;
    // Nothing measured yet: no source at all.
    auto graph = overlay_graph_locked(context);
    assert(graph.count == 0 && graph.source == OverlayGraphSource::None);
    assert(!graph.present_available && !graph.base_available && !graph.display_available);
    // Present calls are the fallback, on the shared axis. The statistics keep
    // every present; the plot keeps the two-second window.
    overlay_frame_stats_locked(context, 8000, now - 3000000);
    overlay_frame_stats_locked(context, 16000, now - 40000);
    overlay_frame_stats_locked(context, 24000, now - 16000);
    const auto stats = overlay_frame_stats_locked(context, 16000, now);
    assert(stats.sample_count == 4);
    graph = overlay_graph_locked(context);
    assert(graph.source == OverlayGraphSource::PresentCalls && graph.present_available);
    assert(graph.count == 3 && graph.median_us == 16000 && graph.sample_times_us[2] == now);
    assert(std::string(overlay_graph_title(false, graph.source, true)) == "Present calls");
    // The bridge's shared buffer, published through the layer's own ABI.
    const std::string dir = argv[1];
    const std::string path = dir + "/base-timing.700.bin";
    setenv("PENGUIN_BURNER_TELEMETRY_SESSION", "700", 1);
    setenv("PENGUIN_BURNER_LATENCY_SOCKET", (dir + "/latency.sock").c_str(), 1);
    const int fd = open(path.c_str(), O_RDWR | O_CREAT | O_EXCL, 0600);
    assert(fd >= 0);
    const int sized = ftruncate(fd, static_cast<off_t>(pb_base_timing_size()));
    assert(sized == 0);
    void* buffer = mmap(nullptr, pb_base_timing_size(), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    assert(buffer != MAP_FAILED);
    close(fd);
    auto reader = open_base_frame_reader();
    assert(reader);
    // Mapped but never published: not a source yet, the fallback stands.
    graph = overlay_graph_locked(context);
    assert(!read_base_frame_reader(reader, graph));
    assert(!graph.base_available && graph.source == OverlayGraphSource::PresentCalls && graph.count == 3);
    // One marker is a source without an interval: still the fallback.
    pb_base_timing_publish(buffer, 1, now - 180000, 42);
    graph = overlay_graph_locked(context);
    assert(!read_base_frame_reader(reader, graph));
    assert(graph.base_available && graph.source == OverlayGraphSource::PresentCalls && graph.count == 3);
    pb_base_timing_publish(buffer, 2, now - 150000, 42);
    pb_base_timing_publish(buffer, 2, now - 149000, 42); // Duplicate.
    pb_base_timing_publish(buffer, 1, now - 148000, 42); // Out of order.
    pb_base_timing_publish(buffer, 3, now - 120000, 42);
    pb_base_timing_publish(buffer, 4, now - 30000, 42); // Real spike retained.
    pb_base_timing_publish(buffer, 5, now, 42);
    // Base frames replace the Present fallback.
    graph = overlay_graph_locked(context);
    assert(read_base_frame_reader(reader, graph));
    assert(graph.source == OverlayGraphSource::BaseFrames && graph.present_available);
    assert(graph.count == 4 && graph.median_us == 30000 && graph.samples[2] == 90000);
    assert(std::string(overlay_graph_title(false, graph.source, false)) == "Base frames");
    assert(std::string(overlay_graph_title(false, graph.source, true)) == "Base frames (pre-FG)");
    assert(std::string(overlay_graph_title(true, graph.source, false)) == "Displayed");
    assert(std::string(overlay_graph_title(true, graph.source, true)) == "Displayed (post-FG)");
    // The newest marker may postdate the snapshot taken on the present thread
    // (CPU run-ahead plus the bridge's publish delay): still base frames, with
    // the future sample left out rather than the whole plot.
    graph = overlay_graph_locked(context);
    graph.end_time_us = now - 5000;
    assert(read_base_frame_reader(reader, graph));
    assert(graph.source == OverlayGraphSource::BaseFrames && graph.count == 3);
    assert(graph.sample_times_us[2] == now - 30000 && graph.median_us == 30000);
    // Displayed samples share the axis with the base frames.
    auto& display = context.display_timing;
    display.available = display.clock_valid = true;
    display.clock_offset_ns = 1000000000;
    record_display_timestamp(display, (now - 40000) * 1000 - 1000000000, now);
    record_display_timestamp(display, (now - 30000) * 1000 - 1000000000, now);
    graph = overlay_graph_locked(context);
    assert(read_base_frame_reader(reader, graph));
    assert(graph.display_available && graph.display_count == 1 && graph.display_median_us == 10000);
    assert(graph.sample_times_us[2] == graph.display_times_us[0]);
    OverlayGraph lower = graph;
    lower.count = graph.display_count;
    lower.sample_times_us = graph.display_times_us;
    assert(overlay_graph_sample_x(graph, 2, 1000) == overlay_graph_sample_x(lower, 0, 1000));
    // A gap in the marker stream (loading screen) falls back to Present
    // calls; the base source still exists, so an empty plot would wait.
    graph = overlay_graph_locked(context);
    graph.end_time_us += 3000000;
    assert(!read_base_frame_reader(reader, graph));
    assert(graph.base_available && graph.source == OverlayGraphSource::PresentCalls && graph.count == 3);
    // Display source seen but the clock never calibrated: the samples still
    // plot, anchored at the collection time and walked back through their own
    // intervals, instead of "waiting" over a full ring.
    display.clock_valid = false;
    record_display_timestamp(display, (now - 10000) * 1000 - 1000000000, now);
    graph = overlay_graph_locked(context);
    assert(graph.display_available && graph.display_count == 2);
    assert(graph.display_samples[0] == 10000 && graph.display_samples[1] == 20000);
    assert(graph.display_times_us[1] == now && graph.display_times_us[0] == now - 20000);
    assert(graph.display_median_us == 20000);
    // A stale display stream stays empty either way.
    display.last_sample_us = now - 3000000;
    graph = overlay_graph_locked(context);
    assert(graph.display_available && graph.display_count == 0);
    display.last_sample_us = now;
    munmap(buffer, pb_base_timing_size());
    unlink(path.c_str());
#ifdef VK_EXT_present_timing
    DeviceContext device;
    device.get_timing_domains = domains;
    device.get_calibrated_timestamps = calibrate;
    DisplayTimingState clock;
    clock.time_domain = VK_TIME_DOMAIN_PRESENT_STAGE_LOCAL_EXT;
    clock.domain_id = 7;
    clock.stage = VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT;
    calibrate_display_clock(device, {}, clock, 1000000);
    assert(clock.clock_valid && clock.clock_offset_ns == 1000000000 && calibrations == 1);
    calibrate_display_clock(device, {}, clock, 1500000);
    assert(calibrations == 1); // No per-frame queries.
    calibrate_display_clock(device, {}, clock, 3000001);
    assert(calibrations == 2);
    // A failed re-calibration keeps the last good offset instead of blanking the plot.
    deviation = 1000000;
    calibrate_display_clock(device, {}, clock, 5000002);
    assert(calibrations == 3 && clock.clock_valid && clock.clock_offset_ns == 1000000000);
    // Only a domain change invalidates it.
    clock.shared = true;
    VkPresentStageTimeEXT stage{};
    stage.stage = clock.stage; stage.time = 1;
    VkPastPresentationTimingEXT report{};
    report.reportComplete = VK_TRUE; report.timeDomainId = 8;
    report.timeDomain = VK_TIME_DOMAIN_PRESENT_STAGE_LOCAL_EXT;
    report.presentStageCount = 1; report.pPresentStages = &stage;
    VkPastPresentationTimingPropertiesEXT results{};
    results.presentationTimingCount = 1; results.pPresentationTimings = &report;
    observe_display_timing(clock, results, 5000003);
    assert(!clock.clock_valid && clock.domain_id == 8);
#endif
}
""")
    output = tmp_path / "fast"
    build_overlay_probe(harness, output, full_layer=True)
    subprocess.run([str(output), str(tmp_path)], check=True)



def test_native_worker_reopens_a_replaced_base_timing_file(tmp_path, build_overlay_probe):
    """A restarted bridge replaces the buffer file; the worker follows it.

    The reader maps an inode, so os.replace or an unlink by the new bridge
    would otherwise leave the layer on a dead buffer ("waiting") for the rest
    of the session. build_now() stats the path and reopens or drops the reader.
    """
    harness = tmp_path / "reopen.cpp"
    harness.write_text(r"""
#include "latency_layer_internal.h"
#include <cassert>
#include <fcntl.h>
#include <sys/mman.h>
using namespace pblayer;
static void* make_buffer(const std::string& file) {
    const int fd = open(file.c_str(), O_RDWR | O_CREAT | O_EXCL, 0600);
    assert(fd >= 0);
    const int sized = ftruncate(fd, static_cast<off_t>(pb_base_timing_size()));
    assert(sized == 0);
    void* buffer = mmap(nullptr, pb_base_timing_size(), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    assert(buffer != MAP_FAILED);
    close(fd);
    return buffer;
}
int main(int argc, char** argv) {
    assert(argc == 2);
    const std::string dir = argv[1];
    const std::string path = dir + "/base-timing.701.bin";
    setenv("PENGUIN_BURNER_TELEMETRY_SESSION", "701", 1);
    setenv("PENGUIN_BURNER_LATENCY_SOCKET", (dir + "/latency.sock").c_str(), 1);
    const auto now = now_monotonic_us();
    OverlayGraph graph{};
    graph.end_time_us = now;
    OverlayWorker worker(false);
    worker.post(OverlayFrameStats{}, graph, VkExtent2D{1920, 1080}, false, OverlayFont::Pixel);
    // No bridge yet: nothing to map, and nothing to re-validate.
    assert(!base_frame_reader_stale(nullptr));
    auto prepared = worker.build_now();
    assert(prepared && !prepared->base_reader);
    // The bridge appears: the next build maps it.
    void* first = make_buffer(path);
    pb_base_timing_publish(first, 1, now - 100000, 42);
    pb_base_timing_publish(first, 2, now - 50000, 42);
    pb_base_timing_publish(first, 3, now, 42);
    prepared = worker.build_now();
    assert(prepared->base_reader && !base_frame_reader_stale(prepared->base_reader));
    OverlayGraph read = graph;
    assert(read_base_frame_reader(prepared->base_reader, read));
    assert(read.count == 2 && read.samples[0] == 50000 && read.samples[1] == 50000);
    // The bridge restarts and replaces the file (new inode, other samples).
    const std::string temp = path + ".tmp";
    void* second = make_buffer(temp);
    pb_base_timing_publish(second, 1, now - 90000, 43);
    pb_base_timing_publish(second, 2, now - 60000, 43);
    pb_base_timing_publish(second, 3, now, 43);
    const int renamed = rename(temp.c_str(), path.c_str());
    assert(renamed == 0);
    const auto stale = prepared->base_reader;
    assert(base_frame_reader_stale(stale));
    // The old mapping still reads the old inode; the next build swaps it out.
    read = graph;
    assert(read_base_frame_reader(stale, read) && read.samples[1] == 50000);
    prepared = worker.build_now();
    assert(prepared->base_reader && prepared->base_reader != stale);
    assert(!base_frame_reader_stale(prepared->base_reader));
    read = graph;
    assert(read_base_frame_reader(prepared->base_reader, read));
    assert(read.count == 2 && read.samples[0] == 30000 && read.samples[1] == 60000);
    // The bridge goes away: the source does too, instead of a frozen buffer.
    const int unlinked = unlink(path.c_str());
    assert(unlinked == 0);
    assert(base_frame_reader_stale(prepared->base_reader));
    prepared = worker.build_now();
    assert(!prepared->base_reader);
    read = graph;
    assert(!read_base_frame_reader(prepared->base_reader, read) && !read.base_available);
    munmap(first, pb_base_timing_size());
    munmap(second, pb_base_timing_size());
}
""")
    output = tmp_path / "reopen"
    build_overlay_probe(harness, output, full_layer=True)
    subprocess.run([str(output), str(tmp_path)], check=True, env={
        **os.environ,
        "PENGUIN_BURNER_OVERLAY_CONFIG": str(tmp_path / "missing"),
        "PENGUIN_BURNER_OVERLAY_STATE": str(tmp_path / "missing"),
        "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(tmp_path / "missing"),
    })


def test_device_timing_features_do_not_imply_swapchain_ownership(tmp_path, build_overlay_probe):
    harness = tmp_path / 'ownership.cpp'
    harness.write_text(r'''
#include "latency_layer_internal.h"
#include <cassert>
namespace pblayer {
VKAPI_ATTR VkResult VKAPI_CALL layer_create_swapchain_khr(
    VkDevice, const VkSwapchainCreateInfoKHR*, const VkAllocationCallbacks*, VkSwapchainKHR*);
}
using namespace pblayer;
static VkSwapchainCreateFlagsKHR received;
static unsigned queues = 0;
static VkResult VKAPI_CALL caps(VkPhysicalDevice, const VkPhysicalDeviceSurfaceInfo2KHR*, VkSurfaceCapabilities2KHR* out) {
    auto* t = static_cast<VkPresentTimingSurfaceCapabilitiesEXT*>(out->pNext);
    t->presentTimingSupported = VK_TRUE;
    t->presentStageQueries = VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT;
    static_cast<VkSurfaceCapabilitiesPresentId2KHR*>(t->pNext)->presentId2Supported = VK_TRUE;
    return VK_SUCCESS;
}
static PFN_vkVoidFunction VKAPI_CALL proc(VkInstance, const char*) { return reinterpret_cast<PFN_vkVoidFunction>(caps); }
static VkResult VKAPI_CALL create(VkDevice, const VkSwapchainCreateInfoKHR* in, const VkAllocationCallbacks*, VkSwapchainKHR* out) {
    received = in->flags;
    *out = reinterpret_cast<VkSwapchainKHR>(5);
    return VK_SUCCESS;
}
static VkResult VKAPI_CALL domains(VkDevice, VkSwapchainKHR, VkSwapchainTimeDomainPropertiesEXT* out, uint64_t*) {
    out->timeDomainCount = 1;
    if (out->pTimeDomains) { out->pTimeDomains[0] = VK_TIME_DOMAIN_CLOCK_MONOTONIC_KHR; out->pTimeDomainIds[0] = 1; }
    return VK_SUCCESS;
}
static VkResult VKAPI_CALL queue(VkDevice, VkSwapchainKHR, uint32_t size) { assert(size == 64); ++queues; return VK_SUCCESS; }
static VkResult VKAPI_CALL past(VkDevice, const VkPastPresentationTimingInfoEXT*, VkPastPresentationTimingPropertiesEXT*) { return VK_SUCCESS; }
int main() {
    const char* extensions[] = {VK_EXT_PRESENT_TIMING_EXTENSION_NAME, VK_KHR_PRESENT_ID_2_EXTENSION_NAME};
    VkPhysicalDevicePresentId2FeaturesKHR id{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_2_FEATURES_KHR, nullptr, VK_TRUE};
    VkPhysicalDevicePresentTimingFeaturesEXT timing{};
    timing.sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_TIMING_FEATURES_EXT;
    timing.pNext = &id; timing.presentTiming = VK_TRUE;
    VkDeviceCreateInfo device_info{};
    device_info.pNext = &timing; device_info.enabledExtensionCount = 2; device_info.ppEnabledExtensionNames = extensions;
    assert(display_timing_features_enabled(&device_info));
    timing.presentTiming = VK_FALSE; assert(!display_timing_features_enabled(&device_info)); timing.presentTiming = VK_TRUE;
    id.presentId2 = VK_FALSE; assert(!display_timing_features_enabled(&device_info)); id.presentId2 = VK_TRUE;
    device_info.enabledExtensionCount = 1; assert(!display_timing_features_enabled(&device_info)); device_info.enabledExtensionCount = 2;
    auto instance = reinterpret_cast<VkInstance>(1);
    auto physical = reinterpret_cast<VkPhysicalDevice>(2);
    auto device = reinterpret_cast<VkDevice>(3);
    g_instances[instance].get_instance_proc_addr = proc;
    g_physical_devices[physical] = instance;
    auto& d = g_devices[device];
    d.device = device; d.physical_device = physical; d.create_swapchain_khr = create;
    d.display_timing_enabled = display_timing_features_enabled(&device_info);
    d.owns_display_timing = false; // Features enabled by the application.
    d.set_timing_queue = queue; d.get_timing_domains = domains; d.get_past_timing = past;
    VkSwapchainCreateInfoKHR info{};
    info.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR;
    info.imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT; info.imageExtent = {640, 480};
    info.presentMode = VK_PRESENT_MODE_FIFO_KHR;
    info.flags = VK_SWAPCHAIN_CREATE_PRESENT_ID_2_BIT_KHR;
    VkSwapchainKHR swapchain{};
    assert(layer_create_swapchain_khr(device, &info, nullptr, &swapchain) == VK_SUCCESS);
    assert(received & VK_SWAPCHAIN_CREATE_PRESENT_TIMING_BIT_EXT);
    assert(!(info.flags & VK_SWAPCHAIN_CREATE_PRESENT_TIMING_BIT_EXT)); // Caller preserved.
    assert(g_swapchains.at(swapchain).display_timing.available);
    assert(!g_swapchains.at(swapchain).display_timing.shared && queues == 1);
    info.flags |= VK_SWAPCHAIN_CREATE_PRESENT_TIMING_BIT_EXT;
    assert(layer_create_swapchain_khr(device, &info, nullptr, &swapchain) == VK_SUCCESS);
    assert(received == info.flags && g_swapchains.at(swapchain).display_timing.shared);
    assert(queues == 1); // Existing application's queue never resized.
    d.display_timing_enabled = false;
    info.flags = 0;
    assert(layer_create_swapchain_khr(device, &info, nullptr, &swapchain) == VK_SUCCESS);
    assert(received == 0 && !g_swapchains.at(swapchain).display_timing.available);
}
''')
    binary = tmp_path / 'ownership'
    build_overlay_probe(harness, binary, full_layer=True)
    subprocess.run([str(binary)], check=True)


def test_instance_injects_surface_capabilities2_when_application_did_not(tmp_path, build_overlay_probe):
    """Create through a next layer that requires live instances for lookups.

    Covers Mesa's pre-instance constraint, extension injection/pass-through,
    a consumed chain on retry, and propagation of application/driver errors.
    """
    harness = tmp_path / 'instance_caps2.cpp'
    harness.write_text(r"""
#include "latency_layer_internal.h"
#include <cassert>
namespace pblayer {
VKAPI_ATTR VkResult VKAPI_CALL layer_create_instance(
    const VkInstanceCreateInfo*, const VkAllocationCallbacks*, VkInstance*);
}
using namespace pblayer;
static const char* kCaps2 = VK_KHR_GET_SURFACE_CAPABILITIES_2_EXTENSION_NAME;
static bool reject_caps2 = false;
static bool missing_create = false;
static VkResult driver_result = VK_SUCCESS;
static VkLayerInstanceLink downstream{};
static VkInstance live_instance = VK_NULL_HANDLE;
static uintptr_t instance_id = 100;
static std::vector<std::vector<std::string>> creates;
static VkResult VKAPI_CALL create(const VkInstanceCreateInfo* info, const VkAllocationCallbacks*, VkInstance* out) {
    // Model a downstream layer consuming its link even on a failed attempt.
    // The retry must restore it before re-entering the lower layer.
    auto* chain = find_layer_chain_entry(info);
    assert(chain && chain->u.pLayerInfo == &downstream);
    chain->u.pLayerInfo = downstream.pNext;
    std::vector<std::string> names;
    for (uint32_t i = 0; i < info->enabledExtensionCount; ++i) names.push_back(info->ppEnabledExtensionNames[i]);
    creates.push_back(names);
    const bool has_caps2 = std::find(names.begin(), names.end(), kCaps2) != names.end();
    if (has_caps2 && reject_caps2) return VK_ERROR_EXTENSION_NOT_PRESENT;
    if (driver_result != VK_SUCCESS) return driver_result;
    live_instance = reinterpret_cast<VkInstance>(++instance_id);
    *out = live_instance;
    return VK_SUCCESS;
}
static PFN_vkVoidFunction VKAPI_CALL gipa(VkInstance instance, const char* name) {
    // Regression for #150: Mesa device-select crashes on the extension
    // enumeration lookup with a null instance, before vkCreateInstance.
    if (instance == VK_NULL_HANDLE) {
        assert(!std::strcmp(name, "vkCreateInstance"));
        return missing_create ? nullptr : reinterpret_cast<PFN_vkVoidFunction>(create);
    }
    assert(instance == live_instance);
    return nullptr;
}
static bool run(const char* const* app, uint32_t count, VkResult expected = VK_SUCCESS) {
    VkLayerInstanceLink link{};
    link.pfnNextGetInstanceProcAddr = gipa;
    link.pNext = &downstream;
    VkLayerInstanceCreateInfo chain{};
    chain.sType = VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO;
    chain.function = VK_LAYER_LINK_INFO;
    chain.u.pLayerInfo = &link;
    VkInstanceCreateInfo info{};
    info.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
    info.pNext = &chain;
    info.enabledExtensionCount = count;
    info.ppEnabledExtensionNames = app;
    std::vector<std::string> before;
    for (uint32_t i = 0; i < count; ++i) before.emplace_back(app[i]);
    creates.clear();
    live_instance = VK_NULL_HANDLE;
    const auto contexts_before = g_instances.size();
    VkInstance instance{};
    assert(layer_create_instance(&info, nullptr, &instance) == expected);
    // The caller's create info and extension array are never mutated.
    assert(info.enabledExtensionCount == count && info.ppEnabledExtensionNames == app);
    for (uint32_t i = 0; i < count; ++i) assert(before[i] == app[i]);
    if (expected != VK_SUCCESS) {
        assert(g_instances.size() == contexts_before);
        return false;
    }
    assert(g_instances.size() == contexts_before + 1);
    const bool enabled = g_instances.at(instance).surface_capabilities2_enabled;
    g_instances.erase(instance);
    return enabled;
}
int main() {
    const char* surface_only[] = {VK_KHR_SURFACE_EXTENSION_NAME};
    const char* with_caps2[] = {VK_KHR_SURFACE_EXTENSION_NAME, kCaps2};
    const std::vector<std::string> surface_list{VK_KHR_SURFACE_EXTENSION_NAME};
    const std::vector<std::string> extended{VK_KHR_SURFACE_EXTENSION_NAME, kCaps2};

    // Inject for native apps without any pre-instance extension query.
    assert(run(surface_only, 1));
    assert(creates.size() == 1 && creates[0] == extended);
    assert(run(nullptr, 0));
    assert(creates.size() == 1 && creates[0] == std::vector<std::string>{kCaps2});

    // Application request passes through without a duplicate.
    assert(run(with_caps2, 2));
    assert(creates.size() == 1 && creates[0] == extended);

    // Unsupported optional extension retries with the original list and chain.
    reject_caps2 = true;
    assert(!run(surface_only, 1));
    assert(creates.size() == 2 && creates[0] == extended && creates[1] == surface_list);
    assert(!run(nullptr, 0));
    assert(creates.size() == 2 && creates[1].empty());

    // Never remove an extension the application requested itself.
    run(with_caps2, 2, VK_ERROR_EXTENSION_NOT_PRESENT);
    assert(creates.size() == 1 && creates[0] == extended);

    // A failed retry is returned without publishing an instance context.
    driver_result = VK_ERROR_INCOMPATIBLE_DRIVER;
    run(surface_only, 1, driver_result);
    assert(creates.size() == 2);
    reject_caps2 = false;

    // Other failures are propagated immediately, without retry.
    for (VkResult error : {VK_ERROR_INCOMPATIBLE_DRIVER, VK_ERROR_INITIALIZATION_FAILED,
                          VK_ERROR_OUT_OF_HOST_MEMORY}) {
        driver_result = error;
        run(surface_only, 1, error);
        assert(creates.size() == 1);
    }
    driver_result = VK_SUCCESS;
    missing_create = true;
    run(surface_only, 1, VK_ERROR_INITIALIZATION_FAILED);
    assert(creates.empty());
    missing_create = false;

    // Pure helper: `inject` false leaves the create info untouched.
    VkInstanceCreateInfo plain{};
    plain.enabledExtensionCount = 1;
    plain.ppEnabledExtensionNames = surface_only;
    InstanceExtensionSetup setup;
    prepare_instance_surface_capabilities2(&plain, false, setup);
    assert(!setup.requested && !setup.injected);
    prepare_instance_surface_capabilities2(&plain, true, setup);
    assert(setup.injected && setup.info.enabledExtensionCount == 2
        && std::string(setup.info.ppEnabledExtensionNames[1]) == kCaps2
        && plain.enabledExtensionCount == 1 && plain.ppEnabledExtensionNames == surface_only);
}
""")
    binary = tmp_path / 'instance_caps2'
    build_overlay_probe(harness, binary, full_layer=True)
    subprocess.run([str(binary)], check=True)


def test_native_both_mode_collapses_when_displayed_source_is_absent(tmp_path, build_overlay_probe):
    """Both mode: no display stage -> one full-height plot, not an empty half.

    Queue-only feedback (NVIDIA on XWayland) keeps a single hint line; no
    present timing at all draws exactly what Base mode draws in the same box.
    A source that exists but has no sample in the window keeps both slots.
    """
    harness = tmp_path / "collapse.cpp"
    harness.write_text(r"""
#include <cassert>
#include <cmath>
#include "overlay_render.cpp"
using namespace pblayer;

static const float kX = 10, kY = 100, kWidth = 200, kHeight = 48, kScale = 1;

static OverlayGraph base_graph() {
    OverlayGraph graph{};
    graph.present_available = true;
    graph.source = OverlayGraphSource::PresentCalls;
    graph.count = 120;
    graph.median_us = 9000;
    for (uint32_t i = 0; i < graph.count; ++i) graph.samples[i] = 8000 + i * 100;
    return graph;
}

struct Drawn {
    std::vector<OverlayVertex> vertices;
    size_t bars = 0, glyphs = 0;
    float bar_bottom = -1.0f, glyph_bottom = -1.0f;
};

static Drawn draw(uint32_t mode, const OverlayGraph& graph) {
    g_overlay_graph_mode = mode;
    Drawn drawn;
    const VkExtent2D extent{2560, 1440};
    OverlayQuadSink sink{nullptr, VK_NULL_HANDLE, extent, &drawn.vertices,
        overlay_font_atlas(OverlayFont::Sans), OverlayFont::Sans, nullptr};
    overlay_draw_timing_graphs(sink, graph, kX, kY, kWidth, kHeight, kScale, true);
    for (const auto& v : drawn.vertices) {
        if (v.u < 0.0f) { ++drawn.bars; drawn.bar_bottom = std::max(drawn.bar_bottom, v.y); }
        else { ++drawn.glyphs; drawn.glyph_bottom = std::max(drawn.glyph_bottom, v.y); }
    }
    return drawn;
}

static bool near(float a, float b) { return std::fabs(a - b) < 0.01f; }

int main() {
    const float ndc_half = overlay_ndc_y(static_cast<int32_t>(kY + kHeight / 2), 1440);
    const float ndc_bottom = overlay_ndc_y(static_cast<int32_t>(kY + kHeight), 1440);

    // NVIDIA on XWayland: timing exists, but no display stage to plot.
    OverlayGraph queue_only = base_graph();
    queue_only.display_queue_only = true;
    auto layout = overlay_timing_graph_layout(2, queue_only, kHeight, kScale);
    assert(layout.plots == 1 && layout.displayed_hint);
    assert(overlay_graph_box_rows(2, queue_only) == 4); // one plot + hint row
    // Box rows: 3 for the plot + 1 for the hint, so the text row is a quarter.
    assert(near(layout.label_height, 12) && near(layout.slot_height, 36));
    assert(near(layout.plot_height, 36 - 12 - 3) && near(layout.hint_y, 36));
    const Drawn hinted = draw(2, queue_only);
    assert(hinted.bars && hinted.glyphs);
    assert(hinted.bar_bottom > ndc_half);
    // The hint line sits under the plot.
    assert(hinted.glyph_bottom > hinted.bar_bottom);

    // No present timing at all: no displayed plot, no displayed label.
    OverlayGraph none = base_graph();
    layout = overlay_timing_graph_layout(2, none, kHeight, kScale);
    assert(layout.plots == 1 && !layout.displayed_hint);
    assert(overlay_graph_box_rows(2, none) == 3); // sized exactly like Base mode
    assert(near(layout.label_height, 16) && near(layout.plot_height, 32));
    const Drawn plain = draw(2, none);
    assert(plain.bars == hinted.bars && plain.glyphs < hinted.glyphs);
    assert(plain.bar_bottom > hinted.bar_bottom && near(plain.bar_bottom, ndc_bottom));
    // Identical to what Base mode draws in the same box: one label, one plot.
    const Drawn base_mode = draw(0, none);
    assert(base_mode.glyphs == plain.glyphs && base_mode.bars == plain.bars);
    assert(near(base_mode.bar_bottom, plain.bar_bottom) && near(base_mode.glyph_bottom, plain.glyph_bottom));

    // Source seen, nothing in the window: both slots stay so a gap never
    // moves the top plot around.
    OverlayGraph waiting = base_graph();
    waiting.display_available = true;
    layout = overlay_timing_graph_layout(2, waiting, kHeight, kScale);
    assert(layout.plots == 2 && !layout.displayed_hint);
    assert(overlay_graph_box_rows(2, waiting) == 6);
    assert(near(layout.slot_height, 24) && near(layout.plot_height, 24 - 8 - 3));
    const Drawn held = draw(2, waiting);
    assert(held.bar_bottom < ndc_half);
    assert(held.glyphs > plain.glyphs); // "Displayed - waiting" is still written.
    // The hint row sits at the foot of its box (y = 36 in a 48 px box), one
    // 12 px row below where the waiting layout's second label starts (y = 24).
    const auto px = [](float ndc) { return (ndc + 1.0f) / 2.0f * 1440.0f; };
    assert(near(px(hinted.glyph_bottom) - px(held.glyph_bottom), 12.0f));

    // Displayed samples in hand: two plots, whatever the availability flag says.
    OverlayGraph sampled = base_graph();
    sampled.display_count = 2;
    sampled.display_samples[0] = sampled.display_samples[1] = 3000;
    sampled.display_median_us = 3000;
    layout = overlay_timing_graph_layout(2, sampled, kHeight, kScale);
    assert(layout.plots == 2 && !layout.displayed_hint);

    // Single-graph modes are untouched by the displayed flags.
    for (uint32_t mode : {0u, 1u}) {
        for (const auto* graph : {&queue_only, &none, &waiting}) {
            layout = overlay_timing_graph_layout(mode, *graph, kHeight, kScale);
            assert(layout.plots == 1 && !layout.displayed_hint);
            assert(near(layout.label_height, 16) && near(layout.plot_height, 32));
        }
    }
    // Display-only mode still names why its plot is empty.
    const Drawn display_only = draw(1, queue_only);
    assert(!display_only.bars && display_only.glyphs);
    std::puts("both-mode collapse passed");
}
""")
    binary = tmp_path / "collapse"
    build_overlay_probe(harness, binary, embedded=("overlay_render.cpp",))
    result = subprocess.run([str(binary)], check=True, capture_output=True, text=True,
                            env={**os.environ, "PB_OVERLAY": "1"})
    assert result.stdout.strip() == "both-mode collapse passed"
