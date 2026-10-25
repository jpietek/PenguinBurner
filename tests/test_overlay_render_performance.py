"""Exercise the actual CPU renderer; no timing thresholds or GPU required."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def render_cache_probe(tmp_path_factory: pytest.TempPathFactory) -> Path:
    compiler = shutil.which("c++")
    if compiler is None:
        pytest.skip("C++ compiler unavailable")
    root = Path(__file__).resolve().parents[1]
    src = root / "overlay/native/latency_layer/src"
    target = tmp_path_factory.mktemp("overlay-cache")
    probe = target / "probe.cpp"
    probe.write_text(_SOURCE, encoding="utf-8")
    binary = target / "probe"
    subprocess.run([
        compiler, "-std=c++17", "-O2", "-I", str(src), str(probe),
        str(src / "overlay_text.cpp"), str(src / "latency_state.cpp"), str(src / "frame_timing.cpp"),
        "-o", str(binary),
    ], check=True)
    return binary


@pytest.mark.parametrize("renderer", ["sans", "mono", "pixel", "solid"])
@pytest.mark.parametrize("graph", [False, True])
def test_cached_geometry_preserves_output_and_has_no_warm_frame_allocations(
    render_cache_probe: Path, renderer: str, graph: bool,
) -> None:
    result = subprocess.run(
        [str(render_cache_probe), renderer, str(int(graph))],
        check=True, capture_output=True, text=True, timeout=10,
        env={**os.environ,
             "PENGUIN_BURNER_OVERLAY_CONFIG": str(render_cache_probe.parent / "missing"),
             "PENGUIN_BURNER_OVERLAY_STATE": str(render_cache_probe.parent / "missing"),
             "PENGUIN_BURNER_OVERLAY_OVERRIDE": str(render_cache_probe.parent / "missing"),
             "PB_OVERLAY": "1"},
    )
    assert result.stdout.strip() == "cache parity, invalidation, graph and allocations passed"


_SOURCE = r'''
#include <cassert>
#include <new>
#include <cstdlib>
static bool counting = false;
static size_t allocations = 0;
void* operator new(std::size_t n) {
    if (counting) ++allocations;
    if (void* p = std::malloc(n ? n : 1)) return p;
    throw std::bad_alloc();
}
void* operator new[](std::size_t n) { return ::operator new(n); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }
void operator delete[](void* p, std::size_t) noexcept { std::free(p); }
#include "overlay_render.cpp"
using namespace pblayer;

int main(int argc, char** argv) {
    assert(argc == 3);
    const bool blended = std::string(argv[1]) != "solid";
    OverlayFont font = blended ? parse_overlay_font(argv[1]) : OverlayFont::Pixel;
    const bool graph_on = std::atoi(argv[2]);
    g_overlay_graph_enabled = graph_on;
    OverlayGraph graph{};
    graph.count = 120;
    graph.median_us = 9000;
    for (uint32_t i = 0; i < graph.count; ++i) graph.samples[i] = 8000 + i * 100;
    auto mutable_readout = std::make_shared<OverlayReadout>();
    mutable_readout->text = "119 fps hotspot 79 C";
    mutable_readout->rows = {
        {OverlayRowKind::Hero, "", {{"119"}, {"fps", OverlayCellLevel::Dim}}},
        {OverlayRowKind::Value, "hotspot", {{"79 C"}}},
        {OverlayRowKind::Value, "gpu", {{"2820 MHz"}, {"925 mV"}}},
        {OverlayRowKind::Rule, "", {}},
        {OverlayRowKind::Foot, "", {{"RTX 5070 Ti | 2560x1440"}}},
    };
    std::shared_ptr<const OverlayReadout> readout = mutable_readout;
    OverlayGeometryCache cache;
    VkExtent2D extent{2560, 1440};
    const auto verify = [&] {
        const auto& cached = cached_overlay_geometry(cache, readout, graph, extent, blended, font);
        std::vector<OverlayVertex> vertices;
        std::vector<OverlayClearRect> rects;
        OverlayQuadSink direct{nullptr, VK_NULL_HANDLE, extent,
            blended ? &vertices : nullptr, overlay_font_atlas(font), font,
            blended ? nullptr : &rects};
        overlay_draw_text(direct, *readout, graph);
        assert(cached.vertices.size() == vertices.size());
        if (!vertices.empty()) {
            assert(std::memcmp(cached.vertices.data(), vertices.data(),
                vertices.size() * sizeof(OverlayVertex)) == 0);
        }
        assert(cached.clear_rects.size() == rects.size());
        for (size_t i = 0; i < rects.size(); ++i) {
            const auto& a = cached.clear_rects[i];
            const auto& b = rects[i];
            assert(a.x == b.x && a.y == b.y && a.width == b.width && a.height == b.height);
            assert(std::memcmp(&a.color, &b.color, sizeof(a.color)) == 0);
        }
    };
    verify();
    const auto generation = cache.generation;
    const auto* data = blended ? static_cast<const void*>(cache.vertices.data())
                              : static_cast<const void*>(cache.clear_rects.data());
    allocations = 0;
    counting = true;
    for (int i = 0; i < 500; ++i) {
        cached_overlay_geometry(cache, readout, graph, extent, blended, font);
    }
    counting = false;
    assert(allocations == 0);
    assert(data == (blended ? static_cast<const void*>(cache.vertices.data())
                           : static_cast<const void*>(cache.clear_rects.data())));
    assert(graph_on || generation == cache.generation);
    graph.samples[119] = 48000;
    graph.count = 1; verify();
    graph.count = 30; verify();
    graph.count = 120; verify();
    g_overlay_user_scale = 1.5; verify();
    g_overlay_opacity = 0.4; verify();
    g_overlay_position = static_cast<uint32_t>(OverlayPosition::BottomLeft); verify();
    extent = {3840, 2160}; verify();
    if (blended) { font = OverlayFont::Mono; verify(); }
    auto updated = std::make_shared<OverlayReadout>(*readout);
    updated->rows[1].cells[0].text = "83 C";
    readout = updated; verify();
    g_overlay_graph_enabled = !graph_on; verify();
    g_overlay_graph_enabled = false;
    // Per-swapchain snapshots stay stable between polls and do not leak device facts.
    SwapchainContext context, other;
    context.device_facts.gpu_name = "first";
    other.device_facts.gpu_name = "second";
    auto stats = overlay_frame_stats_locked(context, 10000);
    const auto first = cached_overlay_readout(context, stats, 1000000);
    assert(cached_overlay_readout(other, stats, 1000000) != first);
    allocations = 0;
    counting = true;
    for (int i = 0; i < 500; ++i) {
        stats = overlay_frame_stats_locked(context, 8000 + i);
        const auto trace = overlay_graph_locked(context, stats.median_us);
        assert(trace.median_us == stats.median_us);
        assert(cached_overlay_readout(context, stats, 1100000) == first);
    }
    counting = false;
    assert(allocations == 0);
    assert(cached_overlay_readout(context, stats, 1250000) != first);
    std::puts("cache parity, invalidation, graph and allocations passed");
}
'''
