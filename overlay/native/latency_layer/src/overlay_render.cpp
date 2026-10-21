// In-present-path Vulkan overlay rendering (bitmap glyphs).
#include "latency_layer_internal.h"

#include <cmath>
#include "overlay_glyphs.h"
#include "overlay_font.h"
#include "overlay_shaders.h"

namespace pblayer {
VkClearColorValue overlay_color(float r, float g, float b, float a) {
    VkClearColorValue value{};
    value.float32[0] = r;
    value.float32[1] = g;
    value.float32[2] = b;
    value.float32[3] = a;
    return value;
}

std::array<uint8_t, kOverlayGlyphHeight> overlay_glyph_rows(char ch) {
    // Printable ASCII only; anything else (control bytes, UTF-8 continuation
    // bytes from a name we did not format) draws blank rather than indexing
    // off the end of the table.
    const unsigned char code = static_cast<unsigned char>(ch);
    if (code < static_cast<unsigned char>(kOverlayGlyphFirst)
        || code > static_cast<unsigned char>(kOverlayGlyphLast)) {
        return {};
    }
    return kOverlayGlyphs[code - static_cast<unsigned char>(kOverlayGlyphFirst)];
}

OverlayFont overlay_font() {
    return static_cast<OverlayFont>(g_overlay_font.load(std::memory_order_relaxed));
}

// The atlas for the selected face, or null when the overlay should draw its
// original bitmap glyphs.
const OverlayFontAtlas* overlay_font_atlas(OverlayFont font) {
    switch (font) {
        case OverlayFont::Mono:
            return &kOverlayFontMono;
        case OverlayFont::Sans:
            return &kOverlayFontSans;
        case OverlayFont::Pixel:
        default:
            return nullptr;
    }
}

// Both atlases sit side by side in one image, so switching face costs nothing
// at draw time and the descriptor never has to be rewritten.
constexpr uint32_t kOverlayAtlasFaces = 2;

uint32_t overlay_atlas_face_offset(OverlayFont font) {
    return font == OverlayFont::Sans ? kOverlayFontAtlasHeight : 0u;
}

// Atlas metrics are in cell pixels. The mapping is by cap height, not line
// height: the ascent carries room for accents nothing here draws, so matching
// on it rendered the type a third smaller than the bitmap face it replaced --
// small enough to read as blur rather than as small.
float overlay_atlas_scale(const OverlayFontAtlas& atlas, float scale) {
    if (!(atlas.cap_height > 0.0f)) {
        return 1.0f;
    }
    // The bitmap face puts its capitals in the cell's first seven rows.
    return static_cast<float>(kOverlayGlyphHeight - 1) * scale / atlas.cap_height;
}

// Advance for one glyph. A proportional face gives every character its own,
// which is the whole reason the columns cannot be padded with spaces.
float overlay_glyph_advance(
    const OverlayFontAtlas* atlas,
    char ch,
    float scale) {
    if (!atlas) {
        return static_cast<float>(kOverlayGlyphWidth + 1u) * scale;
    }
    const unsigned char code = static_cast<unsigned char>(ch);
    if (code < static_cast<unsigned char>(kOverlayFontFirst)
        || code > static_cast<unsigned char>(kOverlayFontLast)) {
        return 0.0f;
    }
    const OverlayFontGlyph& glyph =
        atlas->glyphs[code - static_cast<unsigned char>(kOverlayFontFirst)];
    return glyph.advance * overlay_atlas_scale(*atlas, scale);
}

float overlay_measure(
    const std::string& text,
    const OverlayFontAtlas* atlas,
    float scale) {
    float width = 0.0f;
    for (char ch : text) {
        width += overlay_glyph_advance(atlas, ch, scale);
    }
    if (!atlas && !text.empty()) {
        // The bitmap face charges no gap after the last glyph.
        width -= scale;
    }
    return width;
}

// The designed layout's palette, straight from the mockup.
VkClearColorValue overlay_level_color(OverlayCellLevel level) {
    switch (level) {
        case OverlayCellLevel::Dim:
            return overlay_color(0.604f, 0.651f, 0.698f, 1.0f);
        case OverlayCellLevel::Good:
            return overlay_color(0.498f, 0.827f, 0.761f, 1.0f);
        case OverlayCellLevel::Warn:
            return overlay_color(1.0f, 0.761f, 0.400f, 1.0f);
        case OverlayCellLevel::Alert:
            return overlay_color(1.0f, 0.561f, 0.486f, 1.0f);
        case OverlayCellLevel::Tier:
            return overlay_color(1.0f, 0.725f, 0.467f, 1.0f);
        case OverlayCellLevel::Normal:
        default:
            return overlay_color(0.925f, 0.941f, 0.953f, 1.0f);
    }
}

OverlayLayout overlay_layout() {
    return static_cast<OverlayLayout>(
        g_overlay_layout.load(std::memory_order_relaxed));
}

// How many characters of a cell fit in the width left, so a row that runs out
// of room ends in a marker rather than simply stopping.
uint32_t overlay_chars_within(
    const std::string& text,
    float budget,
    const OverlayFontAtlas* atlas,
    float scale) {
    float used = 0.0f;
    uint32_t count = 0;
    for (char ch : text) {
        const float advance = overlay_glyph_advance(atlas, ch, scale);
        if (used + advance > budget) {
            break;
        }
        used += advance;
        ++count;
    }
    return count;
}

// A row wider than the image used to be cut mid-cell with nothing to show for
// it -- the old single-line readout silently lost seven fields that way. What
// is left now ends in '>' so the reader knows the row goes on.
std::string overlay_fit_line(const std::string& line, uint32_t max_chars) {
    if (line.size() <= max_chars) {
        return line;
    }
    if (!max_chars) {
        return "";
    }
    return line.substr(0, max_chars - 1) + ">";
}

std::vector<std::string> overlay_split_lines(const std::string& text) {
    std::vector<std::string> lines;
    std::size_t start = 0;
    while (true) {
        const std::size_t end = text.find('\n', start);
        if (end == std::string::npos) {
            lines.push_back(text.substr(start));
            return lines;
        }
        lines.push_back(text.substr(start, end - start));
        start = end + 1;
    }
}

uint32_t overlay_base_resolution_scale(uint32_t height) {
    // Snap the display height to the nearest standard tier (1080p/1440p/4K)
    // rather than assuming a 16:9 width, then map it to the bitmap-font pixel
    // scale. Keying off height keeps ultrawide and other odd resolutions on the
    // right tier (e.g. 3440x1440 stays 1440p, not 4K).
    struct Tier {
        uint32_t height;
        uint32_t scale;
    };
    static constexpr Tier kTiers[] = {
        {1080u, 2u},
        {1440u, 3u},
        {2160u, 4u},
    };
    uint32_t best_scale = kTiers[0].scale;
    uint32_t best_diff = height > kTiers[0].height
        ? height - kTiers[0].height
        : kTiers[0].height - height;
    for (const Tier& tier : kTiers) {
        const uint32_t diff = height > tier.height
            ? height - tier.height
            : tier.height - height;
        if (diff < best_diff) {
            best_diff = diff;
            best_scale = tier.scale;
        }
    }
    return best_scale;
}

// The pixel scale before it is forced onto whole numbers. The bitmap face
// still needs an integer -- a 5x8 cell can only be magnified by whole pixels --
// but the distance-field faces scale continuously, so rounding for their sake
// throws away every size between one step and the next.
float overlay_pixel_scale(const VkExtent2D& extent) {
    const uint32_t base = overlay_base_resolution_scale(extent.height);
    const double user_scale = g_overlay_user_scale.load(std::memory_order_relaxed);
    double scaled = static_cast<double>(base) * kOverlayScaleReference * user_scale;
    if (!(scaled > 0.5)) {
        scaled = 0.5;
    }
    if (scaled > 8.0) {
        scaled = 8.0;
    }
    return static_cast<float>(scaled);
}

uint32_t overlay_resolution_scale(const VkExtent2D& extent) {
    const uint32_t base = overlay_base_resolution_scale(extent.height);
    const double user_scale = g_overlay_user_scale.load(std::memory_order_relaxed);
    // Round to the nearest integer font scale (base values are positive, so a
    // simple +0.5 truncation rounds correctly), then keep at least 1 pixel.
    const double scaled =
        static_cast<double>(base) * kOverlayScaleReference * user_scale;
    long rounded = static_cast<long>(scaled + 0.5);
    if (rounded < 1) {
        return 1u;
    }
    if (rounded > 8) {
        return 8u;
    }
    return static_cast<uint32_t>(rounded);
}

double overlay_opacity() {
    return g_overlay_opacity.load(std::memory_order_relaxed);
}

bool overlay_blended_renderer_enabled() {
    return g_overlay_blended_renderer.load(std::memory_order_relaxed);
}

// Pixel edges map to clip space, not pixel centres: a rectangle covers whole
// pixels, so its boundary is what has to land on the grid.
float overlay_ndc_x(int32_t x, uint32_t width) {
    return width ? (static_cast<float>(x) / static_cast<float>(width)) * 2.0f - 1.0f
                 : -1.0f;
}

float overlay_ndc_y(int32_t y, uint32_t height) {
    return height ? (static_cast<float>(y) / static_cast<float>(height)) * 2.0f - 1.0f
                  : -1.0f;
}

// A rounded rectangle as non-overlapping horizontal bands. Stacking
// translucent quads instead would blend the overlap twice and draw a seam
// across the panel.
std::vector<OverlayRectBand> overlay_rounded_panel_bands(
    int32_t x,
    int32_t y,
    uint32_t width,
    uint32_t height,
    uint32_t radius) {
    std::vector<OverlayRectBand> bands;
    if (!width || !height) {
        return bands;
    }
    if (!radius || radius * 2 > width || radius * 2 > height) {
        bands.push_back(OverlayRectBand{x, y, width, height});
        return bands;
    }
    for (uint32_t row = 0; row < radius; ++row) {
        const uint32_t offset = radius - row;
        // Integer circle: the widest inset whose corner pixel still sits
        // inside the radius.
        uint32_t inset = 0;
        while (inset < radius) {
            const uint32_t dx = radius - inset;
            if (dx * dx + offset * offset <= radius * radius) {
                break;
            }
            ++inset;
        }
        const uint32_t band_width = width - inset * 2;
        bands.push_back(OverlayRectBand{
            x + static_cast<int32_t>(inset), y + static_cast<int32_t>(row),
            band_width, 1});
        bands.push_back(OverlayRectBand{
            x + static_cast<int32_t>(inset),
            y + static_cast<int32_t>(height - row - 1),
            band_width,
            1});
    }
    bands.push_back(OverlayRectBand{
        x,
        y + static_cast<int32_t>(radius),
        width,
        height - radius * 2});
    return bands;
}

VkOffset2D overlay_text_origin(
    const VkExtent2D& extent,
    uint32_t box_width,
    uint32_t box_height,
    uint32_t margin,
    OverlayPosition position) {
    // Pure corner maths: clipping the box to the image stays with the caller.
    const int32_t inset = static_cast<int32_t>(margin);
    const int32_t far_x =
        static_cast<int32_t>(extent.width) - static_cast<int32_t>(box_width) - inset;
    const int32_t center_x =
        (static_cast<int32_t>(extent.width) - static_cast<int32_t>(box_width)) / 2;
    const int32_t far_y =
        static_cast<int32_t>(extent.height) - static_cast<int32_t>(box_height) - inset;
    int32_t x = inset;
    int32_t y = inset;
    switch (position) {
        case OverlayPosition::TopLeft:
            break;
        case OverlayPosition::TopCenter:
            x = center_x;
            break;
        case OverlayPosition::TopRight:
            x = far_x;
            break;
        case OverlayPosition::BottomLeft:
            y = far_y;
            break;
        case OverlayPosition::BottomRight:
            x = far_x;
            y = far_y;
            break;
    }
    // A box wider or taller than the image it sits in still starts inside it.
    return VkOffset2D{std::max(x, 0), std::max(y, 0)};
}

void overlay_clear_rect(
    const DeviceContext& device_context,
    VkCommandBuffer command_buffer,
    const VkExtent2D& extent,
    int32_t x,
    int32_t y,
    uint32_t width,
    uint32_t height,
    VkClearColorValue color) {
    if (!device_context.cmd_clear_attachments || !width || !height) {
        return;
    }
    if (x >= static_cast<int32_t>(extent.width)
        || y >= static_cast<int32_t>(extent.height)) {
        return;
    }
    if (x < 0) {
        const uint32_t clipped = static_cast<uint32_t>(-x);
        if (clipped >= width) {
            return;
        }
        width -= clipped;
        x = 0;
    }
    if (y < 0) {
        const uint32_t clipped = static_cast<uint32_t>(-y);
        if (clipped >= height) {
            return;
        }
        height -= clipped;
        y = 0;
    }
    width = std::min<uint32_t>(
        width,
        static_cast<uint32_t>(static_cast<int32_t>(extent.width) - x));
    height = std::min<uint32_t>(
        height,
        static_cast<uint32_t>(static_cast<int32_t>(extent.height) - y));
    VkClearAttachment attachment{};
    attachment.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    attachment.colorAttachment = 0;
    attachment.clearValue.color = color;
    VkClearRect rect{};
    rect.rect.offset = {x, y};
    rect.rect.extent = {width, height};
    rect.baseArrayLayer = 0;
    rect.layerCount = 1;
    device_context.cmd_clear_attachments(command_buffer, 1, &attachment, 1, &rect);
}

// Every rectangle the overlay draws goes through here, so the two renderers
// differ in one place instead of at each call site.
struct OverlayQuadSink {
    const DeviceContext* device_context = nullptr;
    VkCommandBuffer command_buffer = VK_NULL_HANDLE;
    VkExtent2D extent{};
    // Null means the historical path: one cleared rectangle per call.
    std::vector<OverlayVertex>* vertices = nullptr;
    // Null means the bitmap face; the clear-rect path can draw nothing else.
    const OverlayFontAtlas* atlas = nullptr;
    OverlayFont font = OverlayFont::Pixel;
};

void overlay_fill_quad(
    OverlayQuadSink& sink,
    float x0,
    float y0,
    float x1,
    float y1,
    VkClearColorValue color,
    float u0,
    float v0,
    float u1,
    float v1) {
    if (!sink.vertices) {
        return;
    }
    const std::size_t capacity =
        static_cast<std::size_t>(kOverlayMaxQuads) * kOverlayVerticesPerQuad;
    if (sink.vertices->size() + kOverlayVerticesPerQuad > capacity) {
        return;
    }
    // Round to the pixel grid rather than truncating. Truncating floors each
    // edge independently, so a quad's width drifts by a pixel with its phase
    // and the same glyph samples the atlas differently in different places --
    // which is what made the type look unevenly soft rather than simply small.
    const float nx0 = overlay_ndc_x(
        static_cast<int32_t>(std::lround(x0)), sink.extent.width);
    const float nx1 = overlay_ndc_x(
        static_cast<int32_t>(std::lround(x1)), sink.extent.width);
    const float ny0 = overlay_ndc_y(
        static_cast<int32_t>(std::lround(y0)), sink.extent.height);
    const float ny1 = overlay_ndc_y(
        static_cast<int32_t>(std::lround(y1)), sink.extent.height);
    const float r = color.float32[0];
    const float g = color.float32[1];
    const float b = color.float32[2];
    const float a = color.float32[3];
    const OverlayVertex top_left{nx0, ny0, r, g, b, a, u0, v0};
    const OverlayVertex top_right{nx1, ny0, r, g, b, a, u1, v0};
    const OverlayVertex bottom_left{nx0, ny1, r, g, b, a, u0, v1};
    const OverlayVertex bottom_right{nx1, ny1, r, g, b, a, u1, v1};
    sink.vertices->push_back(top_left);
    sink.vertices->push_back(bottom_left);
    sink.vertices->push_back(top_right);
    sink.vertices->push_back(top_right);
    sink.vertices->push_back(bottom_left);
    sink.vertices->push_back(bottom_right);
}

void overlay_fill_rect(
    OverlayQuadSink& sink,
    int32_t x,
    int32_t y,
    uint32_t width,
    uint32_t height,
    VkClearColorValue color) {
    if (!width || !height) {
        return;
    }
    if (!sink.vertices) {
        overlay_clear_rect(
            *sink.device_context,
            sink.command_buffer,
            sink.extent,
            x,
            y,
            width,
            height,
            color);
        return;
    }
    const std::size_t capacity =
        static_cast<std::size_t>(kOverlayMaxQuads) * kOverlayVerticesPerQuad;
    if (sink.vertices->size() + kOverlayVerticesPerQuad > capacity) {
        return;
    }
    const float x0 = overlay_ndc_x(x, sink.extent.width);
    const float x1 = overlay_ndc_x(
        x + static_cast<int32_t>(width), sink.extent.width);
    const float y0 = overlay_ndc_y(y, sink.extent.height);
    const float y1 = overlay_ndc_y(
        y + static_cast<int32_t>(height), sink.extent.height);
    const float r = color.float32[0];
    const float g = color.float32[1];
    const float b = color.float32[2];
    const float a = color.float32[3];
    const OverlayVertex top_left{x0, y0, r, g, b, a};
    const OverlayVertex top_right{x1, y0, r, g, b, a};
    const OverlayVertex bottom_left{x0, y1, r, g, b, a};
    const OverlayVertex bottom_right{x1, y1, r, g, b, a};
    sink.vertices->push_back(top_left);
    sink.vertices->push_back(bottom_left);
    sink.vertices->push_back(top_right);
    sink.vertices->push_back(top_right);
    sink.vertices->push_back(bottom_left);
    sink.vertices->push_back(bottom_right);
}

// One glyph from the distance-field atlas. The quad covers the whole cell --
// the field needs the padding around the outline to antialias against -- and
// the pen advances by the font's own metric rather than a fixed cell.
void overlay_draw_atlas_glyph(
    OverlayQuadSink& sink,
    const OverlayFontAtlas& atlas,
    OverlayFont font,
    char ch,
    float pen_x,
    float line_top,
    float atlas_scale,
    VkClearColorValue color) {
    const unsigned char code = static_cast<unsigned char>(ch);
    if (code < static_cast<unsigned char>(kOverlayFontFirst)
        || code > static_cast<unsigned char>(kOverlayFontLast)) {
        return;
    }
    const OverlayFontGlyph& glyph =
        atlas.glyphs[code - static_cast<unsigned char>(kOverlayFontFirst)];
    const float cell = static_cast<float>(kOverlayFontCell);
    // One integer cell size for every glyph on the row: a size that changes
    // with the pen's fraction would magnify the atlas differently per glyph.
    const float size = static_cast<float>(std::lround(cell * atlas_scale));
    // The pen keeps its fraction so spacing stays true; only the drawn corner
    // is snapped, so every glyph samples the atlas at the same phase.
    const float x0 = static_cast<float>(std::lround(pen_x));
    // The cell is taller than the line box because the field needs padding to
    // antialias into; centre it so both faces sit on the same rows.
    const float y0 = static_cast<float>(std::lround(
        line_top - (size - atlas.line_height * atlas_scale) * 0.5f));
    const float sheet_width = static_cast<float>(kOverlayFontAtlasWidth);
    const float sheet_height =
        static_cast<float>(kOverlayFontAtlasHeight * kOverlayAtlasFaces);
    const float u0 = glyph.cell_x / sheet_width;
    const float u1 = (glyph.cell_x + cell) / sheet_width;
    const float offset = static_cast<float>(overlay_atlas_face_offset(font));
    const float v0 = (glyph.cell_y + offset) / sheet_height;
    const float v1 = (glyph.cell_y + offset + cell) / sheet_height;
    overlay_fill_quad(sink, x0, y0, x0 + size, y0 + size, color, u0, v0, u1, v1);
}

void overlay_draw_glyph(
    OverlayQuadSink& sink,
    char ch,
    int32_t x,
    int32_t y,
    uint32_t scale,
    VkClearColorValue color) {
    const auto rows = overlay_glyph_rows(ch);
    for (uint32_t row = 0; row < rows.size(); ++row) {
        const uint8_t bits = rows[row];
        uint32_t col = 0;
        while (col < kOverlayGlyphWidth) {
            const uint32_t bit = kOverlayGlyphWidth - 1u - col;
            if (!(bits & (1u << bit))) {
                ++col;
                continue;
            }
            const uint32_t start_col = col;
            while (col < kOverlayGlyphWidth
                && (bits & (1u << (kOverlayGlyphWidth - 1u - col)))) {
                ++col;
            }
            overlay_fill_rect(
                sink,
                x + static_cast<int32_t>(start_col * scale),
                y + static_cast<int32_t>(row * scale),
                (col - start_col) * scale,
                scale,
                color);
        }
    }
}

// Vertical scale for the bars. Anchoring purely on the worst frame would turn
// a perfectly smooth run into amplified noise, so a smooth window is measured
// against twice its median instead and the bars sit at about half height.
uint64_t overlay_graph_span_us(const OverlayGraph& graph, uint64_t worst_us) {
    const uint64_t floor_us = graph.median_us * 2;
    return std::max<uint64_t>(std::max(worst_us, floor_us), 1);
}

// Bar height for one sample. Never zero: a frame that was measured has to
// leave a mark, or a fast stretch reads as missing data.
uint32_t overlay_graph_bar_height(
    uint64_t sample_us,
    uint64_t span_us,
    uint32_t height) {
    if (!span_us || !height) {
        return 0;
    }
    uint64_t bar = (sample_us * height + span_us / 2) / span_us;
    if (!bar) {
        bar = 1;
    }
    if (bar > height) {
        bar = height;
    }
    return static_cast<uint32_t>(bar);
}

void overlay_draw_graph(
    OverlayQuadSink& sink,
    const OverlayGraph& graph,
    int32_t x,
    int32_t y,
    uint32_t width,
    uint32_t height,
    uint32_t scale) {
    if (!graph.count || !width || !height) {
        return;
    }
    // One bar per stride, drawn half as wide: without the gap a steady run
    // fills in solid and stops reading as a chart at all.
    const uint32_t bar_width = scale;
    const uint32_t stride = bar_width * 2;
    const uint32_t capacity = width / stride;
    if (!capacity) {
        return;
    }
    // Newest frame at the right edge; older frames fall off the left.
    const uint32_t bars = std::min(graph.count, capacity);
    const uint32_t first = graph.count - bars;
    uint64_t worst_us = 0;
    for (uint32_t i = first; i < graph.count; ++i) {
        worst_us = std::max(worst_us, graph.samples[i]);
    }
    const uint64_t span_us = overlay_graph_span_us(graph, worst_us);
    const int32_t bottom = y + static_cast<int32_t>(height);
    const VkClearColorValue normal = overlay_color(1.0f, 1.0f, 1.0f, 1.0f);
    const VkClearColorValue spike = overlay_color(1.0f, 0.62f, 0.16f, 1.0f);
    for (uint32_t i = 0; i < bars; ++i) {
        const uint64_t sample = graph.samples[first + i];
        const uint32_t bar_height =
            overlay_graph_bar_height(sample, span_us, height);
        overlay_fill_rect(
            sink,
            x + static_cast<int32_t>(i * stride),
            bottom - static_cast<int32_t>(bar_height),
            bar_width,
            bar_height,
            // A frame that took twice the median is a stutter worth spotting
            // without reading the numbers.
            graph.median_us && sample >= graph.median_us * 2 ? spike : normal);
    }
    // The median rule last, so it reads on top of the bars.
    const uint32_t rule_height = scale;
    uint32_t rule_offset = static_cast<uint32_t>(
        (graph.median_us * height + span_us / 2) / span_us);
    if (rule_offset > height) {
        rule_offset = height;
    }
    overlay_fill_rect(
        sink,
        x,
        bottom - static_cast<int32_t>(std::max(rule_offset, rule_height)),
        bars * stride,
        rule_height,
        overlay_color(0.35f, 0.72f, 0.62f, 1.0f));
}

// The trace wants sub-pixel positions: a frametime line stepped onto whole
// pixels reads as a staircase rather than a slope. The integer helpers stay as
// they are -- every rectangle in the panel still wants the pixel grid.
float overlay_ndc_x_f(float x, uint32_t width) {
    return width ? (x / static_cast<float>(width)) * 2.0f - 1.0f : -1.0f;
}

float overlay_ndc_y_f(float y, uint32_t height) {
    return height ? (y / static_cast<float>(height)) * 2.0f - 1.0f : -1.0f;
}

// Alpha for a point at `y` in a box running from `top` down `height`: full at
// the top, nothing at the baseline. The fill is one vertical gradient across
// the whole box rather than a shade per column, so the alpha has to follow the
// absolute height -- taking it from each column's own drop to the baseline
// would shade a flat stretch differently from a spike beside it.
float overlay_area_alpha(float y, float top, float height, float peak) {
    if (!(height > 0.0f)) {
        return 0.0f;
    }
    float t = (y - top) / height;
    t = std::max(0.0f, std::min(1.0f, t));
    return peak * (1.0f - t);
}

// One column of the filled area, as a trapezoid whose top edge follows the
// trace and whose bottom edge sits on the baseline.
void overlay_push_area_column(
    OverlayQuadSink& sink,
    float x0,
    float x1,
    float y_left,
    float y_right,
    float y_base,
    float box_top,
    float box_height,
    VkClearColorValue color,
    float peak) {
    if (!sink.vertices) {
        return;
    }
    const std::size_t capacity =
        static_cast<std::size_t>(kOverlayMaxQuads) * kOverlayVerticesPerQuad;
    if (sink.vertices->size() + kOverlayVerticesPerQuad > capacity) {
        return;
    }
    const float r = color.float32[0];
    const float g = color.float32[1];
    const float b = color.float32[2];
    const float nx0 = overlay_ndc_x_f(x0, sink.extent.width);
    const float nx1 = overlay_ndc_x_f(x1, sink.extent.width);
    const float nyl = overlay_ndc_y_f(y_left, sink.extent.height);
    const float nyr = overlay_ndc_y_f(y_right, sink.extent.height);
    const float nyb = overlay_ndc_y_f(y_base, sink.extent.height);
    const float al = overlay_area_alpha(y_left, box_top, box_height, peak);
    const float ar = overlay_area_alpha(y_right, box_top, box_height, peak);
    const OverlayVertex top_left{nx0, nyl, r, g, b, al};
    const OverlayVertex top_right{nx1, nyr, r, g, b, ar};
    const OverlayVertex base_left{nx0, nyb, r, g, b, 0.0f};
    const OverlayVertex base_right{nx1, nyb, r, g, b, 0.0f};
    sink.vertices->push_back(top_left);
    sink.vertices->push_back(base_left);
    sink.vertices->push_back(top_right);
    sink.vertices->push_back(top_right);
    sink.vertices->push_back(base_left);
    sink.vertices->push_back(base_right);
}

// Where one sample sits in the graph box. The trace uses nine tenths of the
// height so the worst frame in the window still has a little air above it.
float overlay_graph_point_y(
    uint64_t sample_us,
    uint64_t span_us,
    float y,
    float height) {
    if (!span_us) {
        return y + height;
    }
    float t = static_cast<float>(sample_us) / static_cast<float>(span_us);
    t = std::max(0.0f, std::min(1.0f, t));
    return y + height - t * height * 0.9f;
}

// The filled trace the design asks of the blended pipeline: an area under the
// frametime line fading out towards the baseline, with the line itself drawn
// over it. The clear-rect path can express neither the fade nor the slope --
// it has one solid rectangle per call -- so it keeps the bars.
void overlay_draw_area_graph(
    OverlayQuadSink& sink,
    const OverlayGraph& graph,
    float x,
    float y,
    float width,
    float height,
    float thickness) {
    if (!sink.vertices || graph.count < 2 || !(width > 1.0f)
        || !(height > 1.0f)) {
        return;
    }
    uint64_t worst_us = 0;
    for (uint32_t i = 0; i < graph.count; ++i) {
        worst_us = std::max(worst_us, graph.samples[i]);
    }
    const uint64_t span_us = overlay_graph_span_us(graph, worst_us);
    const float base = y + height;
    const uint32_t points = graph.count;
    const float step = width / static_cast<float>(points - 1);
    const VkClearColorValue trace = overlay_color(0.498f, 0.827f, 0.761f, 1.0f);

    for (uint32_t i = 0; i + 1 < points; ++i) {
        const float x0 = x + static_cast<float>(i) * step;
        const float x1 = x + static_cast<float>(i + 1) * step;
        const float y_left =
            overlay_graph_point_y(graph.samples[i], span_us, y, height);
        const float y_right =
            overlay_graph_point_y(graph.samples[i + 1], span_us, y, height);
        overlay_push_area_column(
            sink, x0, x1, y_left, y_right, base, y, height, trace, 0.42f);
    }

    // The line over the fill. Drawn as a vertical span per column rather than
    // a rotated quad per segment: the span covers the whole rise between two
    // samples, so a steep spike stays connected instead of breaking into
    // separate dashes the way a fixed-width quad would.
    for (uint32_t i = 0; i + 1 < points; ++i) {
        const float x0 = x + static_cast<float>(i) * step;
        const float x1 = x + static_cast<float>(i + 1) * step;
        const float y_left =
            overlay_graph_point_y(graph.samples[i], span_us, y, height);
        const float y_right =
            overlay_graph_point_y(graph.samples[i + 1], span_us, y, height);
        const float top = std::min(y_left, y_right);
        const float bottom = std::max(y_left, y_right) + thickness;
        overlay_fill_quad(
            sink, x0, top, std::max(x1, x0 + thickness), bottom, trace,
            -1.0f, -1.0f, -1.0f, -1.0f);
    }
}

// One drawn row of the designed layout: what it is, what it holds, and where
// its pieces land once the label column is measured.
struct OverlayDrawnRow {
    OverlayRowKind kind = OverlayRowKind::Value;
    float width = 0.0f;
    float height = 0.0f;
};

void overlay_draw_row_text(
    OverlayQuadSink& sink,
    const std::string& text,
    float pen_x,
    float line_top,
    float scale,
    VkClearColorValue color) {
    float glyph_x = pen_x;
    for (char ch : text) {
        if (ch != ' ') {
            if (sink.atlas) {
                overlay_draw_atlas_glyph(
                    sink,
                    *sink.atlas,
                    sink.font,
                    ch,
                    glyph_x,
                    line_top,
                    overlay_atlas_scale(*sink.atlas, scale),
                    color);
            } else {
                overlay_draw_glyph(
                    sink,
                    ch,
                    static_cast<int32_t>(std::lround(glyph_x)),
                    static_cast<int32_t>(std::lround(line_top)),
                    std::max(1u, static_cast<uint32_t>(std::lround(scale))),
                    color);
            }
        }
        glyph_x += overlay_glyph_advance(sink.atlas, ch, scale);
    }
}

void overlay_draw_text(
    OverlayQuadSink& sink,
    const OverlayReadout& readout,
    const OverlayGraph& graph) {
    const VkExtent2D& extent = sink.extent;
    if (readout.rows.empty() || extent.width < 64 || extent.height < 32) {
        return;
    }
    // The bitmap face magnifies by whole pixels only -- a 5x8 cell has no
    // meaning at 1.5x -- so it keeps the rounded scale. The distance-field
    // faces are resampled from a signed distance, so they read cleanly at any
    // size, and rounding them to the same steps threw away every size between
    // one step and the next.
    const float scale = sink.atlas
        ? overlay_pixel_scale(extent)
        : static_cast<float>(overlay_resolution_scale(extent));
    const uint32_t margin = static_cast<uint32_t>(std::lround(5.0f * scale));
    // The row box follows the face actually drawing. Taking it from the bitmap
    // cell left the atlas faces with rows shorter than their own em, so
    // ascenders and descenders of neighbouring rows nearly touched and the
    // whole panel read as cramped.
    const uint32_t line_height = sink.atlas
        ? static_cast<uint32_t>(std::lround(
              sink.atlas->line_height * overlay_atlas_scale(*sink.atlas, scale)))
        : static_cast<uint32_t>(std::lround(kOverlayGlyphHeight * scale));
    // Spacing follows the mockup's proportions rather than the old grid's:
    // about 0.45 em of padding, a little under 0.2 em between rows.
    const uint32_t padding_y = static_cast<uint32_t>(std::lround(line_height * 0.42f));
    const uint32_t padding = static_cast<uint32_t>(std::lround(line_height * 0.52f));
    const uint32_t lead = static_cast<uint32_t>(std::lround(line_height * 0.16f));
    const uint32_t rule_height = static_cast<uint32_t>(std::lround(line_height * 0.32f));
    // The headline number is drawn at twice the body size, with its unit on
    // the same baseline beside it.
    const float hero_scale = scale * 2.0f;
    // Same correction as the body rows: the headline's box has to follow the
    // face drawing it, or the number overflows into the row beneath.
    const uint32_t hero_height = sink.atlas
        ? static_cast<uint32_t>(std::lround(
              sink.atlas->line_height
              * overlay_atlas_scale(*sink.atlas, hero_scale)))
        : static_cast<uint32_t>(std::lround(kOverlayGlyphHeight * hero_scale));
    // The footer keeps the body size and steps back in colour instead. Half a
    // size down was unreadable at the sizes a HUD is actually drawn at.
    const float foot_scale = scale;
    const uint32_t foot_height = line_height;
    // Wide enough that "2820 MHz" and "925 mV" read as two values rather than
    // one run of characters -- the mockup gives this about 0.45 em.
    const float gap = static_cast<float>(line_height) * 0.44f;

    // Labels share a column so the values beside them line up.
    // A floor as well as a maximum: a readout whose labels are all short still
    // wants its values off the left edge, the way the mockup's column does.
    // The mockup reserves about 3.1 em for the label column, so the values
    // start in the same place whatever the labels happen to say.
    float label_width = static_cast<float>(line_height) * 2.6f;
    bool has_labels = false;
    for (const OverlayReadoutRow& row : readout.rows) {
        if (row.kind == OverlayRowKind::Value && !row.label.empty()) {
            has_labels = true;
            label_width = std::max(
                label_width, overlay_measure(row.label, sink.atlas, scale));
        }
    }
    if (!has_labels) {
        label_width = 0.0f;
    }

    std::vector<OverlayDrawnRow> drawn;
    float content_width = 0.0f;
    float content_height = 0.0f;
    for (const OverlayReadoutRow& row : readout.rows) {
        OverlayDrawnRow entry;
        entry.kind = row.kind;
        if (row.kind == OverlayRowKind::Rule) {
            entry.height = static_cast<float>(rule_height);
        } else if (row.kind == OverlayRowKind::Hero) {
            entry.height = static_cast<float>(hero_height);
            entry.width = overlay_measure(row.cells[0].text, sink.atlas, hero_scale);
            if (row.cells.size() > 1) {
                entry.width += gap
                    + overlay_measure(row.cells[1].text, sink.atlas, scale);
            }
        } else if (row.kind == OverlayRowKind::Foot) {
            entry.height = static_cast<float>(foot_height);
            entry.width =
                overlay_measure(row.cells[0].text, sink.atlas, foot_scale);
        } else {
            entry.height = static_cast<float>(line_height);
            entry.width = label_width > 0.0f ? label_width + gap : 0.0f;
            for (std::size_t index = 0; index < row.cells.size(); ++index) {
                if (index) {
                    entry.width += gap;
                }
                entry.width +=
                    overlay_measure(row.cells[index].text, sink.atlas, scale);
            }
        }
        content_width = std::max(content_width, entry.width);
        content_height += entry.height + static_cast<float>(lead);
        drawn.push_back(entry);
    }
    if (content_height > 0.0f) {
        content_height -= static_cast<float>(lead);
    }
    // A trace needs width to say anything. Rows alone can leave the panel
    // narrower than that -- a minimal readout is a handful of short values --
    // so the graph claims a floor of its own, the way the design does.
    if (overlay_graph_enabled() && graph.count) {
        content_width = std::max(content_width, static_cast<float>(line_height) * 10.0f);
    }

    // Round up, never down: a width a fraction short of the content takes the
    // last cell of the widest row with it, and does so silently.
    uint32_t text_width = static_cast<uint32_t>(std::ceil(content_width));
    if (text_width + padding * 2 + margin * 2 > extent.width) {
        text_width = extent.width > margin * 2 + padding * 2
            ? extent.width - margin * 2 - padding * 2
            : 0;
    }
    if (!text_width) {
        return;
    }
    const bool draw_graph = overlay_graph_enabled() && graph.count;
    const uint32_t graph_height = draw_graph ? line_height * 2 : 0;
    // The designed layout reads the trace as part of the headline -- how the
    // frame rate is behaving, right under what it currently is -- so on the
    // blended path it sits under the hero rather than at the foot of the
    // panel. The clear-rect path keeps it at the bottom: its bars are a row of
    // solid rectangles and read as a footnote wherever they are put.
    const bool graph_after_hero = draw_graph && sink.vertices != nullptr;
    if (graph_after_hero) {
        content_height += static_cast<float>(graph_height + lead);
    }
    const uint32_t text_height = static_cast<uint32_t>(std::ceil(content_height));
    const uint32_t box_width = text_width + padding * 2;
    const uint32_t box_height = text_height + padding_y * 2
        + (draw_graph && !graph_after_hero ? graph_height + lead : 0);
    const VkOffset2D origin = overlay_text_origin(
        extent,
        box_width,
        box_height,
        margin,
        static_cast<OverlayPosition>(
            g_overlay_position.load(std::memory_order_relaxed)));
    const int32_t x0 = origin.x;
    const int32_t y0 = origin.y;

    const float panel_alpha = sink.vertices
        ? static_cast<float>(overlay_opacity())
        : 1.0f;
    const VkClearColorValue panel_color =
        overlay_color(0.039f, 0.051f, 0.067f, panel_alpha);
    const uint32_t radius = sink.vertices
        ? static_cast<uint32_t>(std::lround(line_height * 0.36f))
        : 0;
    for (const OverlayRectBand& band : overlay_rounded_panel_bands(
             x0, y0, box_width, box_height, radius)) {
        overlay_fill_rect(
            sink, band.x, band.y, band.width, band.height, panel_color);
    }
    if (sink.vertices) {
        // A hairline picks the panel off a bright scene; on the clear-rect
        // path there is no alpha to draw it with.
        const VkClearColorValue edge = overlay_color(1.0f, 1.0f, 1.0f, 0.13f);
        const uint32_t hair = std::max(
            1u, static_cast<uint32_t>(std::lround(scale * 0.5f)));
        overlay_fill_rect(sink, x0 + static_cast<int32_t>(radius), y0,
                          box_width - radius * 2, hair, edge);
        overlay_fill_rect(sink, x0 + static_cast<int32_t>(radius),
                          y0 + static_cast<int32_t>(box_height - hair),
                          box_width - radius * 2, hair, edge);
        overlay_fill_rect(sink, x0, y0 + static_cast<int32_t>(radius), hair,
                          box_height - radius * 2, edge);
        overlay_fill_rect(sink, x0 + static_cast<int32_t>(box_width - hair),
                          y0 + static_cast<int32_t>(radius), hair,
                          box_height - radius * 2, edge);
    }

    const float left = static_cast<float>(x0 + static_cast<int32_t>(padding));
    const float right = left + static_cast<float>(text_width);
    float y = static_cast<float>(y0 + static_cast<int32_t>(padding_y));
    for (std::size_t index = 0; index < readout.rows.size(); ++index) {
        const OverlayReadoutRow& row = readout.rows[index];
        const OverlayDrawnRow& entry = drawn[index];
        if (row.kind == OverlayRowKind::Rule) {
            overlay_fill_rect(
                sink,
                static_cast<int32_t>(left),
                static_cast<int32_t>(std::lround(y + scale)),
                text_width,
                std::max(1u, static_cast<uint32_t>(std::lround(scale))),
                sink.vertices ? overlay_color(1.0f, 1.0f, 1.0f, 0.12f)
                              : overlay_color(0.227f, 0.255f, 0.282f, 1.0f));
        } else if (row.kind == OverlayRowKind::Hero) {
            overlay_draw_row_text(
                sink,
                row.cells[0].text,
                left,
                y,
                hero_scale,
                overlay_color(1.0f, 1.0f, 1.0f, 1.0f));
            if (row.cells.size() > 1) {
                // The unit sits on the headline's baseline, not its top.
                overlay_draw_row_text(
                    sink,
                    row.cells[1].text,
                    left
                        + overlay_measure(row.cells[0].text, sink.atlas, hero_scale)
                        + gap,
                    y + static_cast<float>(hero_height - line_height),
                    scale,
                    overlay_level_color(row.cells[1].level));
            }
        } else if (row.kind == OverlayRowKind::Foot) {
            overlay_draw_row_text(
                sink,
                row.cells[0].text,
                left,
                y,
                foot_scale,
                overlay_level_color(row.cells[0].level));
        } else {
            if (!row.label.empty()) {
                overlay_draw_row_text(
                    sink,
                    row.label,
                    left,
                    y,
                    scale,
                    overlay_level_color(OverlayCellLevel::Dim));
            }
            float pen_x = label_width > 0.0f ? left + label_width + gap : left;
            for (const OverlayReadoutCell& cell : row.cells) {
                const float budget = right - pen_x;
                if (budget <= 0.0f) {
                    break;
                }
                std::string value = cell.text;
                if (overlay_measure(value, sink.atlas, scale) > budget) {
                    value = overlay_fit_line(
                        value,
                        overlay_chars_within(value, budget, sink.atlas, scale));
                }
                overlay_draw_row_text(
                    sink, value, pen_x, y, scale, overlay_level_color(cell.level));
                pen_x += overlay_measure(cell.text, sink.atlas, scale) + gap;
            }
        }
        y += entry.height + static_cast<float>(lead);
        if (graph_after_hero && row.kind == OverlayRowKind::Hero) {
            overlay_draw_area_graph(
                sink,
                graph,
                left,
                y,
                static_cast<float>(text_width),
                static_cast<float>(graph_height),
                std::max(1.0f, scale));
            y += static_cast<float>(graph_height + lead);
        }
    }
    if (draw_graph && !graph_after_hero) {
        overlay_draw_graph(
            sink,
            graph,
            x0 + static_cast<int32_t>(padding),
            y0 + static_cast<int32_t>(padding_y + text_height + lead),
            text_width,
            graph_height,
            std::max(1u, static_cast<uint32_t>(std::lround(scale))));
    }
}

// The loader writes its dispatch table into every dispatchable object it hands
// the application. A layer that allocates its own command buffers calls down
// the chain directly, which bypasses that trampoline, so the buffers come back
// still carrying the ICD's magic word (0x01CDC0DE) where the table belongs.
// The driver does not care -- which is why this went unnoticed -- but any layer
// below us that dispatches on that key cannot find the device and aborts. The
// loader's layer interface requires the creating layer to copy the pointer from
// the parent device, so that is what this does.
uint32_t overlay_memory_type_index(
    const VkPhysicalDeviceMemoryProperties& properties,
    uint32_t type_bits,
    VkMemoryPropertyFlags required) {
    for (uint32_t index = 0; index < properties.memoryTypeCount; ++index) {
        if (!(type_bits & (1u << index))) {
            continue;
        }
        if ((properties.memoryTypes[index].propertyFlags & required) == required) {
            return index;
        }
    }
    return UINT32_MAX;
}

bool overlay_pipeline_functions_available(const DeviceContext& context) {
    return context.create_shader_module && context.destroy_shader_module
        && context.create_pipeline_layout && context.destroy_pipeline_layout
        && context.create_graphics_pipelines && context.destroy_pipeline
        && context.create_buffer && context.destroy_buffer
        && context.get_buffer_memory_requirements && context.allocate_memory
        && context.free_memory && context.bind_buffer_memory
        && context.map_memory && context.unmap_memory
        && context.cmd_bind_pipeline && context.cmd_bind_vertex_buffers
        && context.cmd_set_viewport && context.cmd_set_scissor && context.cmd_draw;
}

void destroy_overlay_atlas(
    const DeviceContext& device_context,
    OverlayRenderResources& overlay) {
    if (overlay.descriptor_pool != VK_NULL_HANDLE
        && device_context.destroy_descriptor_pool) {
        device_context.destroy_descriptor_pool(
            device_context.device, overlay.descriptor_pool, nullptr);
        overlay.descriptor_pool = VK_NULL_HANDLE;
        overlay.descriptor_set = VK_NULL_HANDLE;
    }
    if (overlay.descriptor_set_layout != VK_NULL_HANDLE
        && device_context.destroy_descriptor_set_layout) {
        device_context.destroy_descriptor_set_layout(
            device_context.device, overlay.descriptor_set_layout, nullptr);
        overlay.descriptor_set_layout = VK_NULL_HANDLE;
    }
    if (overlay.atlas_sampler != VK_NULL_HANDLE && device_context.destroy_sampler) {
        device_context.destroy_sampler(
            device_context.device, overlay.atlas_sampler, nullptr);
        overlay.atlas_sampler = VK_NULL_HANDLE;
    }
    if (overlay.atlas_view != VK_NULL_HANDLE && device_context.destroy_image_view) {
        device_context.destroy_image_view(
            device_context.device, overlay.atlas_view, nullptr);
        overlay.atlas_view = VK_NULL_HANDLE;
    }
    if (overlay.atlas_image != VK_NULL_HANDLE && device_context.destroy_image) {
        device_context.destroy_image(
            device_context.device, overlay.atlas_image, nullptr);
        overlay.atlas_image = VK_NULL_HANDLE;
    }
    if (overlay.atlas_memory != VK_NULL_HANDLE && device_context.free_memory) {
        device_context.free_memory(
            device_context.device, overlay.atlas_memory, nullptr);
        overlay.atlas_memory = VK_NULL_HANDLE;
    }
    if (overlay.atlas_staging != VK_NULL_HANDLE && device_context.destroy_buffer) {
        device_context.destroy_buffer(
            device_context.device, overlay.atlas_staging, nullptr);
        overlay.atlas_staging = VK_NULL_HANDLE;
    }
    if (overlay.atlas_staging_memory != VK_NULL_HANDLE
        && device_context.free_memory) {
        device_context.free_memory(
            device_context.device, overlay.atlas_staging_memory, nullptr);
        overlay.atlas_staging_memory = VK_NULL_HANDLE;
    }
    overlay.atlas_ready = false;
    overlay.atlas_uploaded = false;
}

void destroy_overlay_pipeline(
    const DeviceContext& device_context,
    OverlayRenderResources& overlay) {
    for (std::size_t i = 0; i < overlay.vertex_buffers.size(); ++i) {
        if (i < overlay.vertex_mappings.size() && overlay.vertex_mappings[i]
            && i < overlay.vertex_memories.size()
            && overlay.vertex_memories[i] != VK_NULL_HANDLE
            && device_context.unmap_memory) {
            device_context.unmap_memory(
                device_context.device, overlay.vertex_memories[i]);
        }
        if (overlay.vertex_buffers[i] != VK_NULL_HANDLE
            && device_context.destroy_buffer) {
            device_context.destroy_buffer(
                device_context.device, overlay.vertex_buffers[i], nullptr);
        }
    }
    for (VkDeviceMemory memory : overlay.vertex_memories) {
        if (memory != VK_NULL_HANDLE && device_context.free_memory) {
            device_context.free_memory(device_context.device, memory, nullptr);
        }
    }
    overlay.vertex_buffers.clear();
    overlay.vertex_memories.clear();
    overlay.vertex_mappings.clear();
    destroy_overlay_atlas(device_context, overlay);
    if (overlay.pipeline != VK_NULL_HANDLE && device_context.destroy_pipeline) {
        device_context.destroy_pipeline(
            device_context.device, overlay.pipeline, nullptr);
        overlay.pipeline = VK_NULL_HANDLE;
    }
    if (overlay.pipeline_layout != VK_NULL_HANDLE
        && device_context.destroy_pipeline_layout) {
        device_context.destroy_pipeline_layout(
            device_context.device, overlay.pipeline_layout, nullptr);
        overlay.pipeline_layout = VK_NULL_HANDLE;
    }
    overlay.blended_ready = false;
}

bool overlay_atlas_functions_available(const DeviceContext& context) {
    return context.create_image && context.destroy_image
        && context.get_image_memory_requirements && context.bind_image_memory
        && context.create_image_view && context.destroy_image_view
        && context.create_sampler && context.destroy_sampler
        && context.create_descriptor_set_layout
        && context.destroy_descriptor_set_layout && context.create_descriptor_pool
        && context.destroy_descriptor_pool && context.allocate_descriptor_sets
        && context.update_descriptor_sets && context.cmd_bind_descriptor_sets
        && context.cmd_copy_buffer_to_image && context.cmd_pipeline_barrier;
}

// Best-effort, like the pipeline: a missing atlas leaves the bitmap face in
// charge rather than taking the HUD down with it.
bool init_overlay_atlas(
    const DeviceContext& device_context,
    OverlayRenderResources& overlay) {
    if (!overlay_atlas_functions_available(device_context)) {
        return false;
    }
    const uint32_t width = kOverlayFontAtlasWidth;
    const uint32_t height = kOverlayFontAtlasHeight * kOverlayAtlasFaces;
    const VkDeviceSize size = static_cast<VkDeviceSize>(width) * height;

    VkImageCreateInfo image_info{};
    image_info.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO;
    image_info.imageType = VK_IMAGE_TYPE_2D;
    image_info.format = VK_FORMAT_R8_UNORM;
    image_info.extent = {width, height, 1};
    image_info.mipLevels = 1;
    image_info.arrayLayers = 1;
    image_info.samples = VK_SAMPLE_COUNT_1_BIT;
    image_info.tiling = VK_IMAGE_TILING_OPTIMAL;
    image_info.usage =
        VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_SAMPLED_BIT;
    image_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    image_info.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    if (device_context.create_image(
            device_context.device, &image_info, nullptr, &overlay.atlas_image)
        != VK_SUCCESS) {
        return false;
    }
    VkMemoryRequirements requirements{};
    device_context.get_image_memory_requirements(
        device_context.device, overlay.atlas_image, &requirements);
    uint32_t type_index = overlay_memory_type_index(
        device_context.physical_device_memory_properties,
        requirements.memoryTypeBits,
        VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    if (type_index == UINT32_MAX) {
        type_index = overlay_memory_type_index(
            device_context.physical_device_memory_properties,
            requirements.memoryTypeBits,
            0);
    }
    if (type_index == UINT32_MAX) {
        return false;
    }
    VkMemoryAllocateInfo allocate_info{};
    allocate_info.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    allocate_info.allocationSize = requirements.size;
    allocate_info.memoryTypeIndex = type_index;
    if (device_context.allocate_memory(
            device_context.device, &allocate_info, nullptr, &overlay.atlas_memory)
        != VK_SUCCESS) {
        return false;
    }
    if (device_context.bind_buffer_memory && device_context.bind_image_memory(
            device_context.device, overlay.atlas_image, overlay.atlas_memory, 0)
        != VK_SUCCESS) {
        return false;
    }

    // Staging buffer holding both faces, copied into the image on the first
    // frame that draws.
    VkBufferCreateInfo staging_info{};
    staging_info.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    staging_info.size = size;
    staging_info.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
    staging_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    if (device_context.create_buffer(
            device_context.device, &staging_info, nullptr, &overlay.atlas_staging)
        != VK_SUCCESS) {
        return false;
    }
    VkMemoryRequirements staging_requirements{};
    device_context.get_buffer_memory_requirements(
        device_context.device, overlay.atlas_staging, &staging_requirements);
    const uint32_t staging_type = overlay_memory_type_index(
        device_context.physical_device_memory_properties,
        staging_requirements.memoryTypeBits,
        VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
            | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    if (staging_type == UINT32_MAX) {
        return false;
    }
    allocate_info.allocationSize = staging_requirements.size;
    allocate_info.memoryTypeIndex = staging_type;
    if (device_context.allocate_memory(
            device_context.device,
            &allocate_info,
            nullptr,
            &overlay.atlas_staging_memory)
        != VK_SUCCESS) {
        return false;
    }
    if (device_context.bind_buffer_memory(
            device_context.device,
            overlay.atlas_staging,
            overlay.atlas_staging_memory,
            0)
        != VK_SUCCESS) {
        return false;
    }
    void* mapping = nullptr;
    if (device_context.map_memory(
            device_context.device,
            overlay.atlas_staging_memory,
            0,
            VK_WHOLE_SIZE,
            0,
            &mapping)
        != VK_SUCCESS) {
        return false;
    }
    const VkDeviceSize face = static_cast<VkDeviceSize>(kOverlayFontAtlasWidth)
        * kOverlayFontAtlasHeight;
    std::memcpy(mapping, kOverlayFontMono.pixels, face);
    std::memcpy(
        static_cast<uint8_t*>(mapping) + face, kOverlayFontSans.pixels, face);
    device_context.unmap_memory(device_context.device, overlay.atlas_staging_memory);

    VkImageViewCreateInfo view_info{};
    view_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
    view_info.image = overlay.atlas_image;
    view_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
    view_info.format = VK_FORMAT_R8_UNORM;
    view_info.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    view_info.subresourceRange.levelCount = 1;
    view_info.subresourceRange.layerCount = 1;
    if (device_context.create_image_view(
            device_context.device, &view_info, nullptr, &overlay.atlas_view)
        != VK_SUCCESS) {
        return false;
    }

    VkSamplerCreateInfo sampler_info{};
    sampler_info.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
    sampler_info.magFilter = VK_FILTER_LINEAR;
    sampler_info.minFilter = VK_FILTER_LINEAR;
    sampler_info.mipmapMode = VK_SAMPLER_MIPMAP_MODE_NEAREST;
    sampler_info.addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    sampler_info.addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    sampler_info.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    sampler_info.borderColor = VK_BORDER_COLOR_FLOAT_TRANSPARENT_BLACK;
    if (device_context.create_sampler(
            device_context.device, &sampler_info, nullptr, &overlay.atlas_sampler)
        != VK_SUCCESS) {
        return false;
    }

    VkDescriptorSetLayoutBinding binding{};
    binding.binding = 0;
    binding.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    binding.descriptorCount = 1;
    binding.stageFlags = VK_SHADER_STAGE_FRAGMENT_BIT;
    VkDescriptorSetLayoutCreateInfo layout_info{};
    layout_info.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO;
    layout_info.bindingCount = 1;
    layout_info.pBindings = &binding;
    if (device_context.create_descriptor_set_layout(
            device_context.device,
            &layout_info,
            nullptr,
            &overlay.descriptor_set_layout)
        != VK_SUCCESS) {
        return false;
    }
    VkDescriptorPoolSize pool_size{};
    pool_size.type = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    pool_size.descriptorCount = 1;
    VkDescriptorPoolCreateInfo pool_info{};
    pool_info.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
    pool_info.maxSets = 1;
    pool_info.poolSizeCount = 1;
    pool_info.pPoolSizes = &pool_size;
    if (device_context.create_descriptor_pool(
            device_context.device, &pool_info, nullptr, &overlay.descriptor_pool)
        != VK_SUCCESS) {
        return false;
    }
    VkDescriptorSetAllocateInfo set_info{};
    set_info.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
    set_info.descriptorPool = overlay.descriptor_pool;
    set_info.descriptorSetCount = 1;
    set_info.pSetLayouts = &overlay.descriptor_set_layout;
    if (device_context.allocate_descriptor_sets(
            device_context.device, &set_info, &overlay.descriptor_set)
        != VK_SUCCESS) {
        return false;
    }
    VkDescriptorImageInfo image_binding{};
    image_binding.sampler = overlay.atlas_sampler;
    image_binding.imageView = overlay.atlas_view;
    image_binding.imageLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = overlay.descriptor_set;
    write.dstBinding = 0;
    write.descriptorCount = 1;
    write.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    write.pImageInfo = &image_binding;
    device_context.update_descriptor_sets(device_context.device, 1, &write, 0, nullptr);

    overlay.atlas_ready = true;
    return true;
}

// Best-effort: every failure here leaves blended_ready false and the overlay
// keeps drawing with cleared rectangles, so a driver that will not give us a
// pipeline costs the look rather than the HUD.
void init_overlay_pipeline(
    const DeviceContext& device_context,
    OverlayRenderResources& overlay,
    uint32_t image_count) {
    if (!overlay_pipeline_functions_available(device_context)
        || overlay.render_pass == VK_NULL_HANDLE) {
        return;
    }
    // The fragment shader samples the atlas, so the pipeline cannot exist
    // without it -- even for the bitmap face, which simply never reads it.
    if (!init_overlay_atlas(device_context, overlay)) {
        destroy_overlay_atlas(device_context, overlay);
        return;
    }

    VkShaderModule vertex_module = VK_NULL_HANDLE;
    VkShaderModule fragment_module = VK_NULL_HANDLE;
    VkShaderModuleCreateInfo module_info{};
    module_info.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
    module_info.codeSize = kOverlayVertexSpirv.size() * sizeof(uint32_t);
    module_info.pCode = kOverlayVertexSpirv.data();
    if (device_context.create_shader_module(
            device_context.device, &module_info, nullptr, &vertex_module)
        != VK_SUCCESS) {
        return;
    }
    module_info.codeSize = kOverlayFragmentSpirv.size() * sizeof(uint32_t);
    module_info.pCode = kOverlayFragmentSpirv.data();
    if (device_context.create_shader_module(
            device_context.device, &module_info, nullptr, &fragment_module)
        != VK_SUCCESS) {
        device_context.destroy_shader_module(
            device_context.device, vertex_module, nullptr);
        return;
    }

    VkPipelineLayoutCreateInfo layout_info{};
    layout_info.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
    layout_info.setLayoutCount = 1;
    layout_info.pSetLayouts = &overlay.descriptor_set_layout;
    bool ok = device_context.create_pipeline_layout(
                  device_context.device,
                  &layout_info,
                  nullptr,
                  &overlay.pipeline_layout)
        == VK_SUCCESS;

    if (ok) {
        std::array<VkPipelineShaderStageCreateInfo, 2> stages{};
        stages[0].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
        stages[0].stage = VK_SHADER_STAGE_VERTEX_BIT;
        stages[0].module = vertex_module;
        stages[0].pName = "main";
        stages[1].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
        stages[1].stage = VK_SHADER_STAGE_FRAGMENT_BIT;
        stages[1].module = fragment_module;
        stages[1].pName = "main";

        VkVertexInputBindingDescription binding{};
        binding.binding = 0;
        binding.stride = sizeof(OverlayVertex);
        binding.inputRate = VK_VERTEX_INPUT_RATE_VERTEX;
        std::array<VkVertexInputAttributeDescription, 3> attributes{};
        attributes[0].location = 0;
        attributes[0].binding = 0;
        attributes[0].format = VK_FORMAT_R32G32_SFLOAT;
        attributes[0].offset = offsetof(OverlayVertex, x);
        attributes[1].location = 1;
        attributes[1].binding = 0;
        attributes[1].format = VK_FORMAT_R32G32B32A32_SFLOAT;
        attributes[1].offset = offsetof(OverlayVertex, r);
        attributes[2].location = 2;
        attributes[2].binding = 0;
        attributes[2].format = VK_FORMAT_R32G32_SFLOAT;
        attributes[2].offset = offsetof(OverlayVertex, u);

        VkPipelineVertexInputStateCreateInfo vertex_input{};
        vertex_input.sType =
            VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
        vertex_input.vertexBindingDescriptionCount = 1;
        vertex_input.pVertexBindingDescriptions = &binding;
        vertex_input.vertexAttributeDescriptionCount =
            static_cast<uint32_t>(attributes.size());
        vertex_input.pVertexAttributeDescriptions = attributes.data();

        VkPipelineInputAssemblyStateCreateInfo assembly{};
        assembly.sType =
            VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO;
        assembly.topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;

        VkPipelineViewportStateCreateInfo viewport_state{};
        viewport_state.sType =
            VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO;
        viewport_state.viewportCount = 1;
        viewport_state.scissorCount = 1;

        VkPipelineRasterizationStateCreateInfo raster{};
        raster.sType =
            VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
        raster.polygonMode = VK_POLYGON_MODE_FILL;
        raster.cullMode = VK_CULL_MODE_NONE;
        raster.frontFace = VK_FRONT_FACE_COUNTER_CLOCKWISE;
        raster.lineWidth = 1.0f;

        VkPipelineMultisampleStateCreateInfo multisample{};
        multisample.sType =
            VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO;
        multisample.rasterizationSamples = VK_SAMPLE_COUNT_1_BIT;

        VkPipelineColorBlendAttachmentState blend{};
        blend.blendEnable = VK_TRUE;
        blend.srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;
        blend.dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
        blend.colorBlendOp = VK_BLEND_OP_ADD;
        blend.srcAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
        blend.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
        blend.alphaBlendOp = VK_BLEND_OP_ADD;
        blend.colorWriteMask = VK_COLOR_COMPONENT_R_BIT
            | VK_COLOR_COMPONENT_G_BIT | VK_COLOR_COMPONENT_B_BIT
            | VK_COLOR_COMPONENT_A_BIT;

        VkPipelineColorBlendStateCreateInfo blend_state{};
        blend_state.sType =
            VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;
        blend_state.attachmentCount = 1;
        blend_state.pAttachments = &blend;

        std::array<VkDynamicState, 2> dynamic_states = {
            VK_DYNAMIC_STATE_VIEWPORT, VK_DYNAMIC_STATE_SCISSOR};
        VkPipelineDynamicStateCreateInfo dynamic{};
        dynamic.sType = VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO;
        dynamic.dynamicStateCount =
            static_cast<uint32_t>(dynamic_states.size());
        dynamic.pDynamicStates = dynamic_states.data();

        VkGraphicsPipelineCreateInfo pipeline_info{};
        pipeline_info.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
        pipeline_info.stageCount = static_cast<uint32_t>(stages.size());
        pipeline_info.pStages = stages.data();
        pipeline_info.pVertexInputState = &vertex_input;
        pipeline_info.pInputAssemblyState = &assembly;
        pipeline_info.pViewportState = &viewport_state;
        pipeline_info.pRasterizationState = &raster;
        pipeline_info.pMultisampleState = &multisample;
        pipeline_info.pColorBlendState = &blend_state;
        pipeline_info.pDynamicState = &dynamic;
        pipeline_info.layout = overlay.pipeline_layout;
        pipeline_info.renderPass = overlay.render_pass;
        pipeline_info.subpass = 0;
        ok = device_context.create_graphics_pipelines(
                 device_context.device,
                 VK_NULL_HANDLE,
                 1,
                 &pipeline_info,
                 nullptr,
                 &overlay.pipeline)
            == VK_SUCCESS;
    }

    device_context.destroy_shader_module(
        device_context.device, fragment_module, nullptr);
    device_context.destroy_shader_module(
        device_context.device, vertex_module, nullptr);

    if (ok) {
        const VkDeviceSize buffer_size =
            static_cast<VkDeviceSize>(kOverlayMaxQuads)
            * kOverlayVerticesPerQuad * sizeof(OverlayVertex);
        overlay.vertex_buffers.assign(image_count, VK_NULL_HANDLE);
        overlay.vertex_memories.assign(image_count, VK_NULL_HANDLE);
        overlay.vertex_mappings.assign(image_count, nullptr);
        for (uint32_t i = 0; i < image_count && ok; ++i) {
            VkBufferCreateInfo buffer_info{};
            buffer_info.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
            buffer_info.size = buffer_size;
            buffer_info.usage = VK_BUFFER_USAGE_VERTEX_BUFFER_BIT;
            buffer_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
            if (device_context.create_buffer(
                    device_context.device,
                    &buffer_info,
                    nullptr,
                    &overlay.vertex_buffers[i])
                != VK_SUCCESS) {
                ok = false;
                break;
            }
            VkMemoryRequirements requirements{};
            device_context.get_buffer_memory_requirements(
                device_context.device,
                overlay.vertex_buffers[i],
                &requirements);
            const uint32_t type_index = overlay_memory_type_index(
                device_context.physical_device_memory_properties,
                requirements.memoryTypeBits,
                VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
                    | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
            if (type_index == UINT32_MAX) {
                ok = false;
                break;
            }
            VkMemoryAllocateInfo allocate_info{};
            allocate_info.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
            allocate_info.allocationSize = requirements.size;
            allocate_info.memoryTypeIndex = type_index;
            if (device_context.allocate_memory(
                    device_context.device,
                    &allocate_info,
                    nullptr,
                    &overlay.vertex_memories[i])
                != VK_SUCCESS) {
                ok = false;
                break;
            }
            if (device_context.bind_buffer_memory(
                    device_context.device,
                    overlay.vertex_buffers[i],
                    overlay.vertex_memories[i],
                    0)
                != VK_SUCCESS) {
                ok = false;
                break;
            }
            if (device_context.map_memory(
                    device_context.device,
                    overlay.vertex_memories[i],
                    0,
                    VK_WHOLE_SIZE,
                    0,
                    &overlay.vertex_mappings[i])
                != VK_SUCCESS) {
                ok = false;
                break;
            }
        }
    }

    if (!ok) {
        destroy_overlay_pipeline(device_context, overlay);
        return;
    }
    overlay.blended_ready = true;
}

bool overlay_device_functions_available(const DeviceContext& context) {
    return context.get_swapchain_images_khr && context.queue_submit
        && context.create_image_view && context.destroy_image_view
        && context.create_render_pass && context.destroy_render_pass
        && context.create_framebuffer && context.destroy_framebuffer
        && context.create_command_pool && context.destroy_command_pool
        && context.allocate_command_buffers && context.reset_command_buffer
        && context.begin_command_buffer && context.end_command_buffer
        && context.cmd_begin_render_pass && context.cmd_end_render_pass
        && context.cmd_clear_attachments && context.create_semaphore
        && context.destroy_semaphore && context.create_fence
        && context.destroy_fence && context.get_fence_status
        && context.reset_fences && context.wait_for_fences;
}

void destroy_overlay_resources(
    const DeviceContext& device_context,
    SwapchainContext& swapchain_context) {
    auto& overlay = swapchain_context.overlay;
    if (device_context.wait_for_fences) {
        std::vector<VkFence> pending;
        for (std::size_t i = 0; i < overlay.submit_fences.size(); ++i) {
            if (overlay.submit_fences[i] != VK_NULL_HANDLE
                && i < overlay.submit_pending.size()
                && overlay.submit_pending[i]) {
                pending.push_back(overlay.submit_fences[i]);
            }
        }
        if (!pending.empty()) {
            device_context.wait_for_fences(
                device_context.device,
                static_cast<uint32_t>(pending.size()),
                pending.data(),
                VK_TRUE,
                1000000000ull);
        }
    }
    for (VkFence fence : overlay.submit_fences) {
        if (fence != VK_NULL_HANDLE && device_context.destroy_fence) {
            device_context.destroy_fence(device_context.device, fence, nullptr);
        }
    }
    for (VkSemaphore semaphore : overlay.signal_semaphores) {
        if (semaphore != VK_NULL_HANDLE && device_context.destroy_semaphore) {
            device_context.destroy_semaphore(device_context.device, semaphore, nullptr);
        }
    }
    for (VkFramebuffer framebuffer : overlay.framebuffers) {
        if (framebuffer != VK_NULL_HANDLE && device_context.destroy_framebuffer) {
            device_context.destroy_framebuffer(device_context.device, framebuffer, nullptr);
        }
    }
    for (VkImageView image_view : overlay.image_views) {
        if (image_view != VK_NULL_HANDLE && device_context.destroy_image_view) {
            device_context.destroy_image_view(device_context.device, image_view, nullptr);
        }
    }
    destroy_overlay_pipeline(device_context, overlay);
    if (overlay.command_pool != VK_NULL_HANDLE
        && device_context.destroy_command_pool) {
        device_context.destroy_command_pool(device_context.device, overlay.command_pool, nullptr);
    }
    if (overlay.render_pass != VK_NULL_HANDLE
        && device_context.destroy_render_pass) {
        device_context.destroy_render_pass(device_context.device, overlay.render_pass, nullptr);
    }
    overlay = {};
}

bool init_overlay_resources(
    const DeviceContext& device_context,
    VkSwapchainKHR swapchain,
    SwapchainContext& swapchain_context,
    uint32_t queue_family_index) {
    auto& overlay = swapchain_context.overlay;
    if (overlay.ready && overlay.queue_family_index == queue_family_index) {
        return true;
    }
    // Resources are bound to the first queue family that presents this
    // swapchain; presents from other families skip the overlay instead of
    // destroying resources that may still be in flight.
    if (overlay.attempted) {
        return false;
    }

    // A disabled overlay must NOT consume the one-shot init: the live toggle
    // can enable it later in this same process, and resources then still need
    // their first (and only) initialization attempt.
    if (!overlay_enabled()) {
        return false;
    }

    overlay.attempted = true;
    overlay.queue_family_index = queue_family_index;
    if (!overlay_device_functions_available(device_context)
        || queue_family_index == UINT32_MAX
        || swapchain_context.image_format == VK_FORMAT_UNDEFINED
        || swapchain_context.image_extent.width == 0
        || swapchain_context.image_extent.height == 0
        || swapchain_context.image_array_layers != 1
        || !(swapchain_context.image_usage & VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT)) {
        return false;
    }

    uint32_t image_count = 0;
    VkResult result = device_context.get_swapchain_images_khr(
        device_context.device,
        swapchain,
        &image_count,
        nullptr);
    if (result != VK_SUCCESS || image_count == 0) {
        return false;
    }
    overlay.images.resize(image_count);
    result = device_context.get_swapchain_images_khr(
        device_context.device,
        swapchain,
        &image_count,
        overlay.images.data());
    if ((result != VK_SUCCESS && result != VK_INCOMPLETE) || image_count == 0) {
        destroy_overlay_resources(device_context, swapchain_context);
        return false;
    }
    overlay.images.resize(image_count);

    VkAttachmentDescription attachment{};
    attachment.format = swapchain_context.image_format;
    attachment.samples = VK_SAMPLE_COUNT_1_BIT;
    attachment.loadOp = VK_ATTACHMENT_LOAD_OP_LOAD;
    attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    attachment.initialLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    attachment.finalLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;

    VkAttachmentReference color_attachment{};
    color_attachment.attachment = 0;
    color_attachment.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = 1;
    subpass.pColorAttachments = &color_attachment;

    std::array<VkSubpassDependency, 2> dependencies{};
    dependencies[0].srcSubpass = VK_SUBPASS_EXTERNAL;
    dependencies[0].dstSubpass = 0;
    dependencies[0].srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependencies[0].dstStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependencies[0].dstAccessMask =
        VK_ACCESS_COLOR_ATTACHMENT_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    dependencies[0].dependencyFlags = VK_DEPENDENCY_BY_REGION_BIT;
    dependencies[1].srcSubpass = 0;
    dependencies[1].dstSubpass = VK_SUBPASS_EXTERNAL;
    dependencies[1].srcStageMask = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT;
    dependencies[1].dstStageMask = VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT;
    dependencies[1].srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    dependencies[1].dependencyFlags = VK_DEPENDENCY_BY_REGION_BIT;

    VkRenderPassCreateInfo render_pass_info{};
    render_pass_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_CREATE_INFO;
    render_pass_info.attachmentCount = 1;
    render_pass_info.pAttachments = &attachment;
    render_pass_info.subpassCount = 1;
    render_pass_info.pSubpasses = &subpass;
    render_pass_info.dependencyCount =
        static_cast<uint32_t>(dependencies.size());
    render_pass_info.pDependencies = dependencies.data();
    result = device_context.create_render_pass(
        device_context.device,
        &render_pass_info,
        nullptr,
        &overlay.render_pass);
    if (result != VK_SUCCESS) {
        destroy_overlay_resources(device_context, swapchain_context);
        return false;
    }

    overlay.image_views.resize(image_count, VK_NULL_HANDLE);
    overlay.framebuffers.resize(image_count, VK_NULL_HANDLE);
    for (uint32_t i = 0; i < image_count; ++i) {
        VkImageViewCreateInfo view_info{};
        view_info.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO;
        view_info.image = overlay.images[i];
        view_info.viewType = VK_IMAGE_VIEW_TYPE_2D;
        view_info.format = swapchain_context.image_format;
        view_info.components = {
            VK_COMPONENT_SWIZZLE_IDENTITY,
            VK_COMPONENT_SWIZZLE_IDENTITY,
            VK_COMPONENT_SWIZZLE_IDENTITY,
            VK_COMPONENT_SWIZZLE_IDENTITY,
        };
        view_info.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        view_info.subresourceRange.baseMipLevel = 0;
        view_info.subresourceRange.levelCount = 1;
        view_info.subresourceRange.baseArrayLayer = 0;
        view_info.subresourceRange.layerCount = 1;
        result = device_context.create_image_view(
            device_context.device,
            &view_info,
            nullptr,
            &overlay.image_views[i]);
        if (result != VK_SUCCESS) {
            destroy_overlay_resources(device_context, swapchain_context);
            return false;
        }

        VkFramebufferCreateInfo framebuffer_info{};
        framebuffer_info.sType = VK_STRUCTURE_TYPE_FRAMEBUFFER_CREATE_INFO;
        framebuffer_info.renderPass = overlay.render_pass;
        framebuffer_info.attachmentCount = 1;
        framebuffer_info.pAttachments = &overlay.image_views[i];
        framebuffer_info.width = swapchain_context.image_extent.width;
        framebuffer_info.height = swapchain_context.image_extent.height;
        framebuffer_info.layers = 1;
        result = device_context.create_framebuffer(
            device_context.device,
            &framebuffer_info,
            nullptr,
            &overlay.framebuffers[i]);
        if (result != VK_SUCCESS) {
            destroy_overlay_resources(device_context, swapchain_context);
            return false;
        }
    }

    VkCommandPoolCreateInfo pool_info{};
    pool_info.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    pool_info.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pool_info.queueFamilyIndex = queue_family_index;
    result = device_context.create_command_pool(
        device_context.device,
        &pool_info,
        nullptr,
        &overlay.command_pool);
    if (result != VK_SUCCESS) {
        destroy_overlay_resources(device_context, swapchain_context);
        return false;
    }

    overlay.command_buffers.resize(image_count, VK_NULL_HANDLE);
    VkCommandBufferAllocateInfo allocate_info{};
    allocate_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocate_info.commandPool = overlay.command_pool;
    allocate_info.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocate_info.commandBufferCount = image_count;
    result = device_context.allocate_command_buffers(
        device_context.device,
        &allocate_info,
        overlay.command_buffers.data());
    if (result != VK_SUCCESS) {
        destroy_overlay_resources(device_context, swapchain_context);
        return false;
    }
    // A command buffer is dispatchable, and the one the layer below us
    // returned carries its dispatch table, not the one a layer above us reads.
    // The loader's callback restores that before the handle travels back up.
    if (device_context.set_device_loader_data) {
        for (VkCommandBuffer command_buffer : overlay.command_buffers) {
            result = device_context.set_device_loader_data(
                device_context.device,
                command_buffer);
            if (result != VK_SUCCESS) {
                destroy_overlay_resources(device_context, swapchain_context);
                return false;
            }
        }
    }

    overlay.signal_semaphores.resize(image_count, VK_NULL_HANDLE);
    VkSemaphoreCreateInfo semaphore_info{};
    semaphore_info.sType = VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO;
    for (uint32_t i = 0; i < image_count; ++i) {
        result = device_context.create_semaphore(
            device_context.device,
            &semaphore_info,
            nullptr,
            &overlay.signal_semaphores[i]);
        if (result != VK_SUCCESS) {
            destroy_overlay_resources(device_context, swapchain_context);
            return false;
        }
    }

    overlay.submit_fences.resize(image_count, VK_NULL_HANDLE);
    overlay.submit_pending.assign(image_count, 0);
    VkFenceCreateInfo fence_info{};
    fence_info.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    for (uint32_t i = 0; i < image_count; ++i) {
        result = device_context.create_fence(
            device_context.device,
            &fence_info,
            nullptr,
            &overlay.submit_fences[i]);
        if (result != VK_SUCCESS) {
            destroy_overlay_resources(device_context, swapchain_context);
            return false;
        }
    }

    // Best effort and last: a missing pipeline leaves the clear-rect renderer
    // in charge, so it must not fail the resources that renderer needs.
    init_overlay_pipeline(device_context, overlay, image_count);

    overlay.ready = true;
    return true;
}

VkSemaphore submit_overlay_locked(
    const DeviceContext& device_context,
    VkSwapchainKHR swapchain,
    SwapchainContext& swapchain_context,
    const QueueContext& queue_context,
    VkQueue queue,
    uint32_t image_index,
    const VkPresentInfoKHR* present_info,
    const OverlayReadout& readout,
    const OverlayGraph& graph) {
    if (readout.text.empty() || !overlay_enabled()
        || !(queue_context.queue_flags & VK_QUEUE_GRAPHICS_BIT)
        || !present_info) {
        return VK_NULL_HANDLE;
    }
    if (!init_overlay_resources(
            device_context,
            swapchain,
            swapchain_context,
            queue_context.queue_family_index)) {
        return VK_NULL_HANDLE;
    }
    auto& overlay = swapchain_context.overlay;
    if (image_index >= overlay.command_buffers.size()
        || image_index >= overlay.framebuffers.size()
        || image_index >= overlay.signal_semaphores.size()
        || image_index >= overlay.submit_fences.size()
        || image_index >= overlay.submit_pending.size()) {
        return VK_NULL_HANDLE;
    }

    // The previous submit that used this image's command buffer must have
    // retired before the buffer is reset; skip the overlay for this frame
    // rather than stall the present path.
    VkFence fence = overlay.submit_fences[image_index];
    if (overlay.submit_pending[image_index]) {
        if (device_context.get_fence_status(device_context.device, fence)
            != VK_SUCCESS) {
            return VK_NULL_HANDLE;
        }
        overlay.submit_pending[image_index] = 0;
    }
    if (device_context.reset_fences(device_context.device, 1, &fence)
        != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }

    VkCommandBuffer command_buffer = overlay.command_buffers[image_index];
    VkResult result = device_context.reset_command_buffer(command_buffer, 0);
    if (result != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }

    VkCommandBufferBeginInfo begin_info{};
    begin_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    begin_info.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    result = device_context.begin_command_buffer(command_buffer, &begin_info);
    if (result != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }

    const bool blended_available =
        overlay.blended_ready && overlay_blended_renderer_enabled();
    if (blended_available && overlay.atlas_ready && !overlay.atlas_uploaded) {
        // Copying both faces in once, on the first frame that draws, keeps the
        // atlas out of device creation and off every later frame.
        VkImageMemoryBarrier barrier{};
        barrier.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
        barrier.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        barrier.image = overlay.atlas_image;
        barrier.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        barrier.subresourceRange.levelCount = 1;
        barrier.subresourceRange.layerCount = 1;
        barrier.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        device_context.cmd_pipeline_barrier(
            command_buffer,
            VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
            VK_PIPELINE_STAGE_TRANSFER_BIT,
            0,
            0,
            nullptr,
            0,
            nullptr,
            1,
            &barrier);

        VkBufferImageCopy region{};
        region.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
        region.imageSubresource.layerCount = 1;
        region.imageExtent = {
            kOverlayFontAtlasWidth,
            kOverlayFontAtlasHeight * kOverlayAtlasFaces,
            1};
        device_context.cmd_copy_buffer_to_image(
            command_buffer,
            overlay.atlas_staging,
            overlay.atlas_image,
            VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
            1,
            &region);

        barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        barrier.newLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        barrier.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
        device_context.cmd_pipeline_barrier(
            command_buffer,
            VK_PIPELINE_STAGE_TRANSFER_BIT,
            VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT,
            0,
            0,
            nullptr,
            0,
            nullptr,
            1,
            &barrier);
        overlay.atlas_uploaded = true;
    }

    VkRenderPassBeginInfo render_pass_info{};
    render_pass_info.sType = VK_STRUCTURE_TYPE_RENDER_PASS_BEGIN_INFO;
    render_pass_info.renderPass = overlay.render_pass;
    render_pass_info.framebuffer = overlay.framebuffers[image_index];
    render_pass_info.renderArea.offset = {0, 0};
    render_pass_info.renderArea.extent = swapchain_context.image_extent;
    device_context.cmd_begin_render_pass(
        command_buffer,
        &render_pass_info,
        VK_SUBPASS_CONTENTS_INLINE);
    const bool blended = blended_available
        && image_index < overlay.vertex_buffers.size()
        && overlay.vertex_mappings[image_index];
    // The bitmap face is the only one the clear-rect path can draw, and the
    // only one available until the atlas has actually landed in the image.
    const OverlayFont font = blended && overlay.atlas_uploaded
        ? overlay_font()
        : OverlayFont::Pixel;
    std::vector<OverlayVertex> vertices;
    OverlayQuadSink sink{
        &device_context,
        command_buffer,
        swapchain_context.image_extent,
        blended ? &vertices : nullptr,
        overlay_font_atlas(font),
        font};
    overlay_draw_text(sink, readout, graph);
    if (blended && !vertices.empty()) {
        // The fence for this image already retired above, so nothing is
        // reading the buffer we are about to overwrite.
        std::memcpy(
            overlay.vertex_mappings[image_index],
            vertices.data(),
            vertices.size() * sizeof(OverlayVertex));
        device_context.cmd_bind_pipeline(
            command_buffer, VK_PIPELINE_BIND_POINT_GRAPHICS, overlay.pipeline);
        device_context.cmd_bind_descriptor_sets(
            command_buffer,
            VK_PIPELINE_BIND_POINT_GRAPHICS,
            overlay.pipeline_layout,
            0,
            1,
            &overlay.descriptor_set,
            0,
            nullptr);
        VkViewport viewport{};
        viewport.width = static_cast<float>(swapchain_context.image_extent.width);
        viewport.height = static_cast<float>(swapchain_context.image_extent.height);
        viewport.maxDepth = 1.0f;
        device_context.cmd_set_viewport(command_buffer, 0, 1, &viewport);
        VkRect2D scissor{};
        scissor.extent = swapchain_context.image_extent;
        device_context.cmd_set_scissor(command_buffer, 0, 1, &scissor);
        VkDeviceSize vertex_offset = 0;
        device_context.cmd_bind_vertex_buffers(
            command_buffer,
            0,
            1,
            &overlay.vertex_buffers[image_index],
            &vertex_offset);
        device_context.cmd_draw(
            command_buffer, static_cast<uint32_t>(vertices.size()), 1, 0, 0);
    }
    device_context.cmd_end_render_pass(command_buffer);

    result = device_context.end_command_buffer(command_buffer);
    if (result != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }

    std::vector<VkPipelineStageFlags> wait_stages(
        present_info->waitSemaphoreCount,
        VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT);
    VkSemaphore signal = overlay.signal_semaphores[image_index];
    VkSubmitInfo submit_info{};
    submit_info.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submit_info.waitSemaphoreCount = present_info->waitSemaphoreCount;
    submit_info.pWaitSemaphores = present_info->pWaitSemaphores;
    submit_info.pWaitDstStageMask =
        wait_stages.empty() ? nullptr : wait_stages.data();
    submit_info.commandBufferCount = 1;
    submit_info.pCommandBuffers = &command_buffer;
    submit_info.signalSemaphoreCount = 1;
    submit_info.pSignalSemaphores = &signal;
    result = device_context.queue_submit(queue, 1, &submit_info, fence);
    if (result != VK_SUCCESS) {
        return VK_NULL_HANDLE;
    }
    overlay.submit_pending[image_index] = 1;
    return signal;
}

}  // namespace pblayer
