// Host-side probe for the layer's overlay text builder.
//
// It links the same translation units the Vulkan layer does and prints what
// build_overlay_text() would draw, so the C++ formatter can be driven from a
// test with no GPU, no Vulkan loader and no game. The overlay config and the
// daemon state file are read from their usual environment overrides, which is
// what lets a test feed identical inputs to this binary and to the Python
// formatter in overlay/overlay_text.py and diff the two.
//
// Build (from the repository root):
//   c++ -std=c++17 -I overlay/native/latency_layer/src \
//       overlay/native/latency_layer/tools/overlay_text_probe.cpp \
//       overlay/native/latency_layer/src/overlay_text.cpp \
//       overlay/native/latency_layer/src/latency_state.cpp \
//       overlay/native/latency_layer/src/overlay_render.cpp \
//       -o overlay_text_probe
//
// Usage: overlay_text_probe [median_fps] [low_1pct_fps] [p97_us]
//                           [session_min_fps] [session_avg_fps]
//                           [session_max_fps]
//        overlay_text_probe --glyphs TEXT
//        overlay_text_probe --origin WIDTH HEIGHT BOX_W BOX_H MARGIN
//        overlay_text_probe --frame-stats US,US,...
//        overlay_text_probe --graph US,US,...
//        overlay_text_probe --graph-bars HEIGHT US,US,...
//        overlay_text_probe --fit-line MAX_CHARS TEXT
//        overlay_text_probe --highlights [stats...]
//        overlay_text_probe --driver-version VERSION VENDOR_ID
//        overlay_text_probe --panel-bands X Y WIDTH HEIGHT RADIUS
//        overlay_text_probe --ndc X Y WIDTH HEIGHT
//        overlay_text_probe --measure FONT SCALE TEXT
//        overlay_text_probe --hud-rows [stats...]
//
// --origin prints the box origin the layer would place for the position in
// the overlay config, so a test can drive the real parser and corner maths.
//
// --frame-stats runs a list of present frametimes through the layer's own
// percentile and session maths and prints the result, so the statistics can
// be tested without a swapchain.
//
// --graph pushes the frametimes through the ring exactly as presents do and
// prints the chronological snapshot the renderer would draw, which is what
// pins the wrap-around ordering.
//
// --graph-bars prints the bar height each frametime would draw at, which is
// the shape of the graph in numbers: a compositor that pins a test app to a
// fixed rate cannot produce the variance a screenshot would need.
//
// --highlights prints the readout followed by the character runs the layer
// would paint in a warning colour, as "line column length level".
//
// The device facts the layer reads from Vulkan come from PB_PROBE_GPU_NAME,
// PB_PROBE_DRIVER_VERSION, PB_PROBE_RESOLUTION and PB_PROBE_PRESENT_MODE --
// a seam in this tool only, since the real layer takes them from the driver
// at swapchain creation and they never reach a file.
//
// --panel-bands prints the non-overlapping horizontal slices the blended
// renderer builds a rounded panel from, and --ndc the clip-space corner it
// maps a pixel to. Both are pure, which is what makes the pipeline's geometry
// testable on a machine with no working GPU.
//
// --measure prints the advance of each character and the width of the whole
// string, which is what the measured column layout is built on.
//
// --hud-rows prints the designed layout as rows -- kind, label, then each
// cell's colour role and text -- which is what the renderer places.
//
// --glyphs prints one line per character -- the byte, then the seven 5-bit
// rows the layer would draw for it -- so a test can assert the shipped font
// matches its generator without a swapchain.
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <string>

#include "latency_layer_internal.h"
#include "overlay_glyphs.h"

namespace pblayer {
std::array<uint8_t, kOverlayGlyphHeight> overlay_glyph_rows(char ch);
OverlayTextConfig overlay_text_config(uint64_t now_us);
}

namespace {
uint64_t arg_u64(int argc, char** argv, int index) {
    return index < argc ? std::strtoull(argv[index], nullptr, 10) : 0ull;
}
}  // namespace

namespace {
int print_glyphs(const std::string& text) {
    for (char ch : text) {
        const auto rows = pblayer::overlay_glyph_rows(ch);
        std::printf("%d", static_cast<int>(static_cast<unsigned char>(ch)));
        for (uint8_t row : rows) {
            std::printf(" %02x", static_cast<unsigned>(row));
        }
        std::printf("\n");
    }
    return 0;
}
std::vector<uint64_t> parse_us_list(const std::string& list) {
    std::vector<uint64_t> samples;
    std::size_t start = 0;
    while (start <= list.size()) {
        const std::size_t end = list.find(',', start);
        const std::string token = list.substr(
            start, end == std::string::npos ? std::string::npos : end - start);
        if (!token.empty()) {
            samples.push_back(std::strtoull(token.c_str(), nullptr, 10));
        }
        if (end == std::string::npos) {
            break;
        }
        start = end + 1;
    }
    return samples;
}

int print_frame_stats(const std::string& list) {
    const std::vector<uint64_t> samples = parse_us_list(list);
    uint64_t session_count = 0;
    uint64_t session_sum_us = 0;
    uint64_t session_min_us = 0;
    uint64_t session_max_us = 0;
    for (uint64_t sample : samples) {
        if (!sample) {
            continue;
        }
        ++session_count;
        session_sum_us += sample;
        if (!session_min_us || sample < session_min_us) {
            session_min_us = sample;
        }
        if (sample > session_max_us) {
            session_max_us = sample;
        }
    }
    const pblayer::OverlayFrameStats stats =
        pblayer::overlay_frame_stats_from_samples(
            samples.data(),
            static_cast<uint32_t>(samples.size()),
            session_count,
            session_sum_us,
            session_min_us,
            session_max_us);
    std::printf(
        "median_fps=%" PRIu64 " low_1pct_fps=%" PRIu64 " p97_frametime_us=%" PRIu64
        " session_min_fps=%" PRIu64 " session_avg_fps=%" PRIu64
        " session_max_fps=%" PRIu64 " sample_count=%u\n",
        stats.median_fps,
        stats.low_1pct_fps,
        stats.p97_frametime_us,
        stats.session_min_fps,
        stats.session_avg_fps,
        stats.session_max_fps,
        stats.sample_count);
    return 0;
}

int print_graph(const std::string& list) {
    pblayer::SwapchainContext context{};
    pblayer::OverlayFrameStats stats{};
    for (uint64_t sample : parse_us_list(list)) {
        stats = pblayer::overlay_frame_stats_locked(context, sample);
    }
    const pblayer::OverlayGraph graph = pblayer::overlay_graph_locked(context, stats.median_us);
    std::printf("count=%u median_us=%" PRIu64 " samples=", graph.count, graph.median_us);
    for (uint32_t i = 0; i < graph.count; ++i) {
        std::printf("%s%" PRIu64, i ? "," : "", graph.samples[i]);
    }
    std::printf("\n");
    return 0;
}

int print_graph_bars(uint32_t height, const std::string& list) {
    pblayer::OverlayGraph graph{};
    const std::vector<uint64_t> samples = parse_us_list(list);
    graph.count = static_cast<uint32_t>(
        std::min<std::size_t>(samples.size(), graph.samples.size()));
    uint64_t worst_us = 0;
    for (uint32_t i = 0; i < graph.count; ++i) {
        graph.samples[i] = samples[i];
        worst_us = std::max(worst_us, samples[i]);
    }
    std::vector<uint64_t> sorted(samples.begin(), samples.begin() + graph.count);
    std::sort(sorted.begin(), sorted.end());
    if (!sorted.empty()) {
        graph.median_us = sorted[sorted.size() / 2];
    }
    const uint64_t span_us = pblayer::overlay_graph_span_us(graph, worst_us);
    std::printf("span_us=%" PRIu64 " heights=", span_us);
    for (uint32_t i = 0; i < graph.count; ++i) {
        std::printf(
            "%s%u",
            i ? "," : "",
            pblayer::overlay_graph_bar_height(graph.samples[i], span_us, height));
    }
    std::printf("\n");
    return 0;
}

int print_fit_line(uint32_t max_chars, const std::string& line) {
    std::printf("%s\n", pblayer::overlay_fit_line(line, max_chars).c_str());
    return 0;
}

std::string env_or_empty(const char* name) {
    const char* value = std::getenv(name);
    return value ? value : "";
}

pblayer::OverlayDeviceFacts probe_device_facts() {
    pblayer::OverlayDeviceFacts facts;
    facts.gpu_name = env_or_empty("PB_PROBE_GPU_NAME");
    facts.driver_version = env_or_empty("PB_PROBE_DRIVER_VERSION");
    facts.resolution = env_or_empty("PB_PROBE_RESOLUTION");
    facts.present_mode = env_or_empty("PB_PROBE_PRESENT_MODE");
    return facts;
}

int print_driver_version(const char* version, const char* vendor) {
    std::printf(
        "%s\n",
        pblayer::overlay_driver_version_text(
            static_cast<uint32_t>(std::strtoul(version, nullptr, 0)),
            static_cast<uint32_t>(std::strtoul(vendor, nullptr, 0)))
            .c_str());
    return 0;
}

int print_panel_bands(char** argv) {
    const auto value = [&argv](int index) {
        return static_cast<int32_t>(std::strtol(argv[index], nullptr, 10));
    };
    const std::vector<pblayer::OverlayRectBand> bands =
        pblayer::overlay_rounded_panel_bands(
            value(0),
            value(1),
            static_cast<uint32_t>(value(2)),
            static_cast<uint32_t>(value(3)),
            static_cast<uint32_t>(value(4)));
    for (const pblayer::OverlayRectBand& band : bands) {
        std::printf("%d %d %u %u\n", band.x, band.y, band.width, band.height);
    }
    return 0;
}

int print_ndc(char** argv) {
    std::printf(
        "%.6f %.6f\n",
        pblayer::overlay_ndc_x(
            static_cast<int32_t>(std::strtol(argv[0], nullptr, 10)),
            static_cast<uint32_t>(std::strtoul(argv[2], nullptr, 10))),
        pblayer::overlay_ndc_y(
            static_cast<int32_t>(std::strtol(argv[1], nullptr, 10)),
            static_cast<uint32_t>(std::strtoul(argv[3], nullptr, 10))));
    return 0;
}

int print_measure(const std::string& font_name, float scale, const std::string& text) {
    const pblayer::OverlayFont font = pblayer::parse_overlay_font(font_name);
    const pblayer::OverlayFontAtlas* atlas = pblayer::overlay_font_atlas(font);
    float sum = 0.0f;
    for (char ch : text) {
        const float advance = pblayer::overlay_glyph_advance(atlas, ch, scale);
        std::printf("advance %d %.4f\n", static_cast<int>(ch), advance);
        sum += advance;
    }
    std::printf(
        "measured %.4f summed %.4f\n",
        pblayer::overlay_measure(text, atlas, scale),
        sum);
    return 0;
}

int print_hud_rows(const pblayer::OverlayReadout& readout) {
    for (const pblayer::OverlayReadoutRow& row : readout.rows) {
        std::printf("row %u %s", static_cast<unsigned>(row.kind), row.label.c_str());
        for (const pblayer::OverlayReadoutCell& cell : row.cells) {
            std::printf(
                "|%u:%s", static_cast<unsigned>(cell.level), cell.text.c_str());
        }
        std::printf("\n");
    }
    return 0;
}

int print_origin(char** argv) {
    VkExtent2D extent{};
    extent.width = static_cast<uint32_t>(std::strtoul(argv[0], nullptr, 10));
    extent.height = static_cast<uint32_t>(std::strtoul(argv[1], nullptr, 10));
    const uint32_t box_width = static_cast<uint32_t>(std::strtoul(argv[2], nullptr, 10));
    const uint32_t box_height = static_cast<uint32_t>(std::strtoul(argv[3], nullptr, 10));
    const uint32_t margin = static_cast<uint32_t>(std::strtoul(argv[4], nullptr, 10));
    const pblayer::OverlayTextConfig config = pblayer::overlay_text_config(1000000);
    const VkOffset2D origin = pblayer::overlay_text_origin(
        extent, box_width, box_height, margin, config.position);
    std::printf("%d %d\n", origin.x, origin.y);
    return 0;
}
}  // namespace

int main(int argc, char** argv) {
    if (argc > 1 && std::string(argv[1]) == "--config-watch") {
        uint64_t now_us;
        while (std::cin >> now_us) {
            const auto config = pblayer::overlay_text_config(now_us);
            const auto origin = pblayer::overlay_text_origin(
                VkExtent2D{1280, 720}, 200, 100, 10, config.position);
            std::cout << config.items.size() << ' ' << config.scale << ' '
                      << origin.x << ' ' << origin.y << std::endl;
        }
        return 0;
    }

    if (argc > 1 && std::string(argv[1]) == "--visibility-watch") {
        std::string command;
        while (std::getline(std::cin, command)) {
            std::cout << pblayer::overlay_enabled() << std::endl;
        }
        return 0;
    }
    if (argc > 2 && std::string(argv[1]) == "--glyphs") {
        return print_glyphs(argv[2]);
    }
    if (argc > 6 && std::string(argv[1]) == "--origin") {
        return print_origin(argv + 2);
    }
    if (argc > 4 && std::string(argv[1]) == "--measure") {
        return print_measure(
            argv[2], std::strtof(argv[3], nullptr), argv[4]);
    }
    if (argc > 6 && std::string(argv[1]) == "--panel-bands") {
        return print_panel_bands(argv + 2);
    }
    if (argc > 5 && std::string(argv[1]) == "--ndc") {
        return print_ndc(argv + 2);
    }
    if (argc > 3 && std::string(argv[1]) == "--driver-version") {
        return print_driver_version(argv[2], argv[3]);
    }
    if (argc > 2 && std::string(argv[1]) == "--frame-stats") {
        return print_frame_stats(argv[2]);
    }
    if (argc > 2 && std::string(argv[1]) == "--graph") {
        return print_graph(argv[2]);
    }
    if (argc > 3 && std::string(argv[1]) == "--fit-line") {
        return print_fit_line(
            static_cast<uint32_t>(std::strtoul(argv[2], nullptr, 10)), argv[3]);
    }
    if (argc > 3 && std::string(argv[1]) == "--graph-bars") {
        return print_graph_bars(
            static_cast<uint32_t>(std::strtoul(argv[2], nullptr, 10)), argv[3]);
    }
    const bool want_highlights =
        argc > 1 && std::string(argv[1]) == "--highlights";
    const bool want_hud_rows =
        argc > 1 && std::string(argv[1]) == "--hud-rows";
    const int base = (want_highlights || want_hud_rows) ? 1 : 0;
    pblayer::OverlayFrameStats stats{};
    stats.median_fps = arg_u64(argc, argv, base + 1);
    stats.low_1pct_fps = arg_u64(argc, argv, base + 2);
    stats.p97_frametime_us = arg_u64(argc, argv, base + 3);
    stats.session_min_fps = arg_u64(argc, argv, base + 4);
    stats.session_avg_fps = arg_u64(argc, argv, base + 5);
    stats.session_max_fps = arg_u64(argc, argv, base + 6);
    const pblayer::OverlayGpuState state = pblayer::read_overlay_gpu_state();
    const pblayer::OverlayReadout readout = pblayer::build_overlay_readout(
        stats, probe_device_facts(), state, 1000000);
    if (want_hud_rows) {
        return print_hud_rows(readout);
    }
    std::cout << readout.text << "\n";
    if (want_highlights) {
        for (const pblayer::OverlayTextSpan& span : readout.highlights) {
            std::printf(
                "highlight %u %u %u %u\n",
                span.line,
                span.column,
                span.length,
                static_cast<unsigned>(span.level));
        }
    }
    return 0;
}
