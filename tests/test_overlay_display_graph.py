"""Graph selection is live and display samples never reuse Present-call data."""
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
    for mode in ('display', 'both', 'present'):
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
    assert(display_timing_surface_stage({}, proc, {}, {}) == 0);
    supported_stage = VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT;
    assert(display_timing_surface_stage({}, proc, {}, {}) == supported_stage);
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
    for (unsigned i = 1; i <= 125; ++i)
        record_display_timestamp(s, 3000000000ull + i * 10000000ull, 3024001 + i * 10000);
    assert(s.count == 120 && s.index == 5);
    for (auto us : s.samples) assert(us == 10000);
}
''')
    output = tmp_path / 'probe'
    subprocess.run([compiler, '-std=c++17', '-ffunction-sections', '-fdata-sections',
                    '-Wl,--gc-sections', '-I', str(source), str(harness),
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
                    '-Wl,--gc-sections', '-I', str(source), str(harness),
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
                    '-Wl,--gc-sections', '-I', str(source), str(harness),
                    str(source / 'display_timing.cpp'), '-o', str(output)], check=True)
    subprocess.run([str(output)], check=True)
