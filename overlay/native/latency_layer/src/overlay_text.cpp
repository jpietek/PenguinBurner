// Overlay configuration parsing and the compact overlay text string.
#include "latency_layer_internal.h"
#include "overlay_metrics.h"

#include <cmath>

namespace pblayer {
double parse_overlay_scale(const std::string& value) {
    const std::string text = trim_ascii(value);
    if (text.find_first_not_of("0123456789.eE+-") != std::string::npos) return kDEFAULT_OVERLAY_SCALE;
    errno = 0;
    char* end = nullptr;
    const double parsed = std::strtod(text.c_str(), &end);
    if (end == text.c_str() || *end != '\0' || errno != 0 || !std::isfinite(parsed) || parsed <= 0.0) {
        return kDEFAULT_OVERLAY_SCALE;
    }
    return std::clamp(parsed, kMIN_OVERLAY_SCALE, kMAX_OVERLAY_SCALE);
}

double parse_overlay_opacity(const std::string& value) {
    const std::string text = trim_ascii(value);
    if (text.find_first_not_of("0123456789.eE+-") != std::string::npos) return kDEFAULT_OVERLAY_OPACITY;
    errno = 0;
    char* end = nullptr;
    const double parsed = std::strtod(text.c_str(), &end);
    if (end == text.c_str() || *end != '\0' || errno != 0 || !std::isfinite(parsed)) {
        return kDEFAULT_OVERLAY_OPACITY;
    }
    return std::clamp(parsed, kMIN_OVERLAY_OPACITY, kMAX_OVERLAY_OPACITY);
}

std::string unquote_lower(const std::string& value) {
    std::string text = trim_ascii(value);
    while (!text.empty() && (text.front() == '"' || text.front() == '\'')) {
        text.erase(text.begin());
    }
    while (!text.empty() && (text.back() == '"' || text.back() == '\'')) {
        text.pop_back();
    }
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return text;
}

OverlayLayout parse_overlay_layout(const std::string& value) {
    return unquote_lower(value) == "table" ? OverlayLayout::Table : OverlayLayout::Hud;
}

OverlayFont parse_overlay_font(const std::string& value) {
    const std::string text = unquote_lower(value);
    if (text == "pixel") {
        return OverlayFont::Pixel;
    }
    if (text == "mono") {
        return OverlayFont::Mono;
    }
    return kDefaultOverlayFont;
}

OverlayPosition parse_overlay_position(const std::string& value) {
    std::string text = trim_ascii(value);
    // overlay.toml quotes the value; the UI only ever writes the five names
    // below, and anything else keeps the historical top-right corner.
    while (!text.empty() && (text.front() == '"' || text.front() == '\'')) {
        text.erase(text.begin());
    }
    while (!text.empty() && (text.back() == '"' || text.back() == '\'')) {
        text.pop_back();
    }
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    if (text == "top_left") {
        return OverlayPosition::TopLeft;
    }
    if (text == "top_center") {
        return OverlayPosition::TopCenter;
    }
    if (text == "bottom_left") {
        return OverlayPosition::BottomLeft;
    }
    if (text == "bottom_right") {
        return OverlayPosition::BottomRight;
    }
    return OverlayPosition::TopRight;
}

bool value_is_true(const std::string& value) {
    std::string text = trim_ascii(value);
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return text == "1" || text == "true" || text == "yes" || text == "on";
}

bool value_is_false(const std::string& value) {
    std::string text = trim_ascii(value);
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return text == "0" || text == "false" || text == "no" || text == "off";
}

std::string overlay_runtime_path(const char* env_name, const char* file_name);

std::string overlay_env_value() {
    if (const char* value = std::getenv(kOverlayEnableEnv)) {
        return trim_ascii(value);
    }
    if (const char* value = std::getenv(kOverlayEnableEnvAlias)) {
        return trim_ascii(value);
    }
    return "";
}

static int overlay_visibility_override() {
    // Launch-time default from the wrapper env, overridable LIVE by a small
    // runtime file the UI writes ("1"/"0"): the layer is always loaded, so
    // visibility can flip on a running game without a restart. The file is
    // re-checked every 50 ms; the wrapper clears it at launch so a
    // stale override never leaks into the next game.
    static std::mutex override_mutex;
    static std::chrono::steady_clock::time_point last_check{};
    static int override_state = -1;  // -1 unknown/absent, 0 off, 1 on

    std::lock_guard<std::mutex> lock(override_mutex);
    const auto now = std::chrono::steady_clock::now();
    if (last_check.time_since_epoch().count() == 0
        || now - last_check >= std::chrono::milliseconds(50)) {
        last_check = now;
        override_state = -1;
        static const std::string override_path = overlay_runtime_path(
            kOverlayOverrideEnv, "overlay-override");
        std::ifstream stream(override_path);
        if (stream.good()) {
            std::string value;
            std::getline(stream, value);
            if (value_is_true(value)) {
                override_state = 1;
            } else if (value_is_false(value)) {
                override_state = 0;
            }
        }
    }
    return override_state;
}

bool overlay_enabled() {
    static const bool env_default = !value_is_false(overlay_env_value());
    const int override_state = overlay_visibility_override();
    return override_state >= 0 ? override_state == 1 : env_default;
}

bool overlay_env_fallback_enabled() {
    static const bool enabled = value_is_true(overlay_env_value());
    const int override_state = overlay_visibility_override();
    return override_state >= 0 ? override_state == 1 : enabled;
}

std::string overlay_runtime_path(const char* env_name, const char* file_name) {
    if (const char* explicit_path = std::getenv(env_name)) {
        if (explicit_path[0]) {
            return explicit_path;
        }
    }
    // Inside Steam's pressure-vessel container XDG_RUNTIME_DIR only holds
    // forwarded sockets; the home directory is shared with the host.
    if (const char* home = std::getenv("HOME")) {
        if (home[0] && std::strcmp(home, "/root") != 0) {
            return std::string(home) + "/.cache/penguin-burner/" + file_name;
        }
    }
    if (const char* runtime_dir = std::getenv("XDG_RUNTIME_DIR")) {
        if (runtime_dir[0]) {
            return std::string(runtime_dir) + "/penguin-burner/" + file_name;
        }
    }
    char fallback[160]{};
    std::snprintf(
        fallback,
        sizeof(fallback),
        "/tmp/penguin-burner-%s-%ld.txt",
        file_name,
        static_cast<long>(::getuid()));
    return fallback;
}

std::string trim_ascii(std::string value) {
    while (!value.empty()
        && std::isspace(static_cast<unsigned char>(value.front()))) {
        value.erase(value.begin());
    }
    while (!value.empty()
        && std::isspace(static_cast<unsigned char>(value.back()))) {
        value.pop_back();
    }
    return value;
}

std::string profile_voltage_mv_from_id(const std::string& profile_id) {
    std::string text = profile_id;
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    std::size_t pos = text.find("mv");
    while (pos != std::string::npos) {
        std::size_t start = pos;
        while (start > 0
            && std::isdigit(static_cast<unsigned char>(text[start - 1]))) {
            --start;
        }
        if (start < pos) {
            return text.substr(start, pos - start);
        }
        pos = text.find("mv", pos + 2);
    }
    return "";
}

OverlayTextConfig default_overlay_text_config(bool enabled = true) {
    return OverlayTextConfig{enabled, kOverlayDefaultItems};
}

bool overlay_item_known(const std::string& item) {
    for (const char* known_item : kOverlayItemOrder) {
        if (item == known_item) {
            return true;
        }
    }
    return false;
}

std::string overlay_config_path() {
    if (const char* explicit_path = std::getenv(kOverlayConfigEnv)) {
        if (explicit_path[0]) {
            return explicit_path;
        }
    }
    if (const char* home = std::getenv("HOME")) {
        if (home[0] && std::strcmp(home, "/root") != 0) {
            const std::string preferred =
                std::string(home) + "/.config/penguin-burner/overlay.toml";
            if (FILE* file = std::fopen(preferred.c_str(), "r")) {
                std::fclose(file);
                return preferred;
            }
            return std::string(home) + "/.config/PenguinBurner/overlay.toml";
        }
    }
    return "";
}

std::vector<std::string> parse_overlay_items_line(const std::string& line) {
    std::vector<std::string> items;
    const std::size_t start = line.find('[');
    const std::size_t end = line.find(']', start == std::string::npos ? 0 : start);
    if (start == std::string::npos || end == std::string::npos || end <= start) {
        return items;
    }
    std::string current;
    bool in_quote = false;
    for (std::size_t index = start + 1; index < end; ++index) {
        const char ch = line[index];
        if (ch == '"') {
            if (in_quote) {
                const std::string item = trim_ascii(current);
                if (overlay_item_known(item)
                    && std::find(items.begin(), items.end(), item) == items.end()) {
                    items.push_back(item);
                }
                current.clear();
            }
            in_quote = !in_quote;
            continue;
        }
        if (in_quote) {
            current.push_back(ch);
        }
    }
    return items;
}

std::vector<std::string> normalize_overlay_items(
    const std::vector<std::string>& requested) {
    std::vector<std::string> items;
    for (const char* ordered_item : kOverlayItemOrder) {
        const std::string item(ordered_item);
        if (std::find(requested.begin(), requested.end(), item) != requested.end()) {
            items.push_back(item);
        }
    }
    if (items.empty()) {
        items.push_back("base_fps");
    }
    return items;
}

OverlayTextConfig read_overlay_text_config() {
    if (!overlay_enabled()) {
        return default_overlay_text_config(false);
    }
    const bool fallback_enabled = overlay_env_fallback_enabled();
    const std::string path = overlay_config_path();
    if (path.empty()) {
        return default_overlay_text_config(fallback_enabled);
    }
    FILE* file = std::fopen(path.c_str(), "r");
    if (!file) {
        return default_overlay_text_config(fallback_enabled);
    }
    bool enabled = fallback_enabled;
    double scale = kDEFAULT_OVERLAY_SCALE;
    OverlayPosition position = kDefaultOverlayPosition;
    double opacity = kDEFAULT_OVERLAY_OPACITY;
    bool blended_renderer = true;
    OverlayFont font = kDefaultOverlayFont;
    OverlayLayout layout = kDefaultOverlayLayout;
    uint32_t graph_mode = 0;
    std::vector<std::string> requested;
    char line_buffer[512]{};
    while (std::fgets(line_buffer, sizeof(line_buffer), file)) {
        std::string line(line_buffer);
        const std::size_t sep = line.find('=');
        if (sep == std::string::npos) {
            continue;
        }
        const std::string key = trim_ascii(line.substr(0, sep));
        const std::string value = trim_ascii(line.substr(sep + 1));
        if (key == "items") {
            requested = parse_overlay_items_line(line);
        } else if (key == "enabled") {
            enabled = value_is_true(value);
        } else if (key == "scale") {
            scale = parse_overlay_scale(value);
        } else if (key == "position") {
            position = parse_overlay_position(value);
        } else if (key == "opacity") {
            opacity = parse_overlay_opacity(value);
        } else if (key == "renderer") {
            blended_renderer = unquote_lower(value) != "clear_rect";
        } else if (key == "font") {
            font = parse_overlay_font(value);
        } else if (key == "graph_mode") {
            const auto mode = unquote_lower(value);
            graph_mode = mode == "both" ? 2 : mode == "display" ? 1 : 0;
        } else if (key == "layout") {
            layout = parse_overlay_layout(value);
        }
    }
    std::fclose(file);
    // An explicit launch env or live visibility override wins over the saved
    // UI toggle; config only decides the automatic/default overlay mode.
    if (fallback_enabled) {
        enabled = true;
    }
    OverlayTextConfig result = requested.empty()
        ? default_overlay_text_config(enabled)
        : OverlayTextConfig{enabled, normalize_overlay_items(requested)};
    result.scale = scale;
    result.position = position;
    result.opacity = opacity;
    result.blended_renderer = blended_renderer;
    result.font = font;
    result.layout = layout;
    result.graph_mode = graph_mode;
    return result;
}

OverlayTextConfig overlay_text_config(uint64_t now_us) {
    static uint64_t last_read_us = 0;
    static OverlayTextConfig config = default_overlay_text_config(false);
    std::lock_guard lock(g_overlay_config_mutex);
    if (!last_read_us || now_us < last_read_us
        || now_us - last_read_us >= 250000) {
        config = read_overlay_text_config();
        last_read_us = now_us;
        g_overlay_graph_mode.store(config.graph_mode, std::memory_order_relaxed);
        g_overlay_user_scale.store(config.scale, std::memory_order_relaxed);
        g_overlay_position.store(
            static_cast<uint32_t>(config.position), std::memory_order_relaxed);
        g_overlay_opacity.store(config.opacity, std::memory_order_relaxed);
        g_overlay_blended_renderer.store(
            config.blended_renderer, std::memory_order_relaxed);
        g_overlay_font.store(
            static_cast<uint32_t>(config.font), std::memory_order_relaxed);
        g_overlay_layout.store(
            static_cast<uint32_t>(config.layout), std::memory_order_relaxed);
        g_overlay_graph_enabled.store(
            std::find(
                config.items.begin(), config.items.end(), "frametime_graph")
                != config.items.end(),
            std::memory_order_relaxed);
    }
    return config;
}

OverlayGpuState read_overlay_gpu_state() {
    OverlayGpuState state{};
    const std::string path =
        overlay_runtime_path(kOverlayStateEnv, "overlay-state.txt");
    FILE* file = std::fopen(path.c_str(), "r");
    if (!file) {
        return state;
    }
    state.available = true;
    char line[512]{};
    while (std::fgets(line, sizeof(line), file)) {
        std::string text(line);
        const std::size_t sep = text.find('=');
        if (sep == std::string::npos) {
            continue;
        }
        const std::string key = trim_ascii(text.substr(0, sep));
        const std::string value = trim_ascii(text.substr(sep + 1));
        if (key == "present_fps") {
            state.present_fps = value;
        } else if (key == "framegen_fps") {
            state.framegen_fps = value;
        } else if (key == "framegen_active") {
            state.framegen_active = value_is_true(value);
        } else if (key == "clock_mhz") {
            state.clock_mhz = value;
        } else if (key == "latency_ms") {
            state.latency_ms = value;
        } else if (key == "nr_gpu_ms") {
            state.nr_gpu_ms = value;
        } else if (key == "latency_avg_ms") {
            state.latency_avg_ms = value;
        } else if (key == "latency_source") {
            state.latency_source = value;
        } else if (key == "display_latency_ms") {
            state.display_latency_ms = value;
        } else if (key == "voltage_mv") {
            state.voltage_mv = value;
        } else if (key == "power_w") {
            state.power_w = value;
        } else if (key == "gpu_util_pct") {
            state.gpu_util_pct = value;
        } else if (key == "cpu_util_pct") {
            state.cpu_util_pct = value;
        } else if (key == "cpu_peak_thread_pct") {
            state.cpu_peak_thread_pct = value;
        } else if (key == "fan_pct") {
            state.fan_pct = value;
        } else if (key == "temperature_c") {
            state.temperature_c = value;
        } else if (key == "hotspot_temperature_c") {
            state.hotspot_temperature_c = value;
        } else if (key == "uv_offset_mv") {
            state.uv_offset_mv = value;
        } else if (key == "vram_used_mb") {
            state.vram_used_mb = value;
        } else if (key == "vram_total_mb") {
            state.vram_total_mb = value;
        } else if (key == "mem_clock_mhz") {
            state.mem_clock_mhz = value;
        } else if (key == "ram_used_mb") {
            state.ram_used_mb = value;
        } else if (key == "ram_total_mb") {
            state.ram_total_mb = value;
        } else if (key == "cpu_temp_c") {
            state.cpu_temp_c = value;
        } else if (key == "cpu_power_w") {
            state.cpu_power_w = value;
        } else if (key == "profile_id") {
            state.profile_id = value;
        } else if (key == "profile_tier") {
            state.profile_tier = value;
        }
    }
    std::fclose(file);
    const std::string requested_voltage_mv =
        profile_voltage_mv_from_id(state.profile_id);
    if (!requested_voltage_mv.empty()) {
        state.voltage_mv = requested_voltage_mv;
    }
    return state;
}

uint64_t overlay_fps_from_frametime_us(uint64_t frametime_us) {
    return frametime_us
        ? static_cast<uint64_t>((1000000ull + frametime_us / 2) / frametime_us)
        : 0;
}

// Nearest-rank percentile over an ascending-sorted buffer: the smallest value
// at or below which `percentile` per cent of the samples fall. With the ring's
// 120 samples the 99th percentile is the second-worst frame, which is what
// makes it a tail measure rather than an average.
uint64_t overlay_percentile_us(
    const uint64_t* sorted, std::size_t size, unsigned percentile) {
    if (!size) {
        return 0;
    }
    std::size_t rank = (size * percentile + 99) / 100;  // ceil(size * p / 100)
    if (rank == 0) {
        rank = 1;
    }
    if (rank > size) {
        rank = size;
    }
    return sorted[rank - 1];
}

// Mean frametime of the worst `percent` per cent of frames, always at least
// one frame. A plain percentile is the wrong tool for a "1% low": with exactly
// one bad frame in a hundred, the 99th percentile is still a good frame and
// the stutter disappears. Averaging the tail reports it.
uint64_t overlay_worst_percent_mean_us(
    const uint64_t* sorted, std::size_t size, unsigned percent) {
    if (!size) {
        return 0;
    }
    std::size_t take = (size * percent + 99) / 100;
    if (take == 0) {
        take = 1;
    }
    if (take > size) {
        take = size;
    }
    uint64_t sum = 0;
    for (std::size_t i = size - take; i < size; ++i) {
        sum += sorted[i];
    }
    return (sum + take / 2) / take;
}

OverlayFrameStats overlay_frame_stats_from_samples(
    const uint64_t* samples,
    uint32_t count,
    uint64_t session_count,
    uint64_t session_sum_us,
    uint64_t session_min_us,
    uint64_t session_max_us) {
    OverlayFrameStats stats{};
    // The live ring is bounded at 120. Keep its sort off the heap; larger
    // host-side inputs still retain the formatter probe's original contract.
    std::array<uint64_t, 120> scratch{};
    std::vector<uint64_t> overflow;
    if (count > scratch.size()) {
        overflow.resize(count);
    }
    uint64_t* values = overflow.empty() ? scratch.data() : overflow.data();
    for (uint32_t i = 0; i < count; ++i) {
        if (samples[i]) {
            values[stats.sample_count++] = samples[i];
        }
    }
    if (stats.sample_count) {
        std::sort(values, values + stats.sample_count);
        stats.median_us = values[stats.sample_count / 2];
        stats.median_fps = overlay_fps_from_frametime_us(stats.median_us);
        stats.low_1pct_fps = overlay_fps_from_frametime_us(
            overlay_worst_percent_mean_us(values, stats.sample_count, 1));
        stats.p97_frametime_us = overlay_percentile_us(values, stats.sample_count, 97);
    }
    if (session_count && session_sum_us) {
        // Average over the whole session, not over the ring: a mean of
        // per-frame rates would weight cheap frames far too heavily.
        stats.session_avg_fps = static_cast<uint64_t>(
            (1000000ull * session_count + session_sum_us / 2) / session_sum_us);
        // A long frame is a low rate, so the extremes swap round.
        stats.session_min_fps = overlay_fps_from_frametime_us(session_max_us);
        stats.session_max_fps = overlay_fps_from_frametime_us(session_min_us);
    }
    return stats;
}

// Oldest sample first. The ring's index points at the next slot to write, so
// a full ring reads [index, end) then [begin, index).
OverlayGraph overlay_graph_locked(const SwapchainContext& context, uint64_t median_us) {
    OverlayGraph graph{};
    const uint32_t size = static_cast<uint32_t>(context.present_frametimes.size());
    const uint32_t count = std::min(context.present_frametime_count, size);
    const uint32_t start = count < size
        ? 0u
        : context.present_frametime_index % size;
    for (uint32_t i = 0; i < count; ++i) {
        const uint64_t sample = context.present_frametimes[(start + i) % size];
        if (sample) {
            graph.samples[graph.count++] = sample;
        }
    }
    graph.median_us = graph.count ? median_us : 0;
    const auto& display = context.display_timing;
    graph.display_available = display.available;
    const uint64_t now = now_monotonic_us();
    if (display.last_sample_us && now >= display.last_sample_us && now - display.last_sample_us < 2000000) {
        graph.display_count = display.count;
        const uint32_t first = display.count == display.samples.size() ? display.index : 0;
        for (uint32_t i = 0; i < display.count; ++i)
            graph.display_samples[i] = display.samples[(first + i) % display.samples.size()];
        auto sorted = graph.display_samples;
        std::sort(sorted.begin(), sorted.begin() + graph.display_count);
        if (graph.display_count) graph.display_median_us = sorted[graph.display_count / 2];
    }
    return graph;
}

bool overlay_graph_enabled() {
    return g_overlay_graph_enabled.load(std::memory_order_relaxed);
}

OverlayFrameStats overlay_frame_stats_locked(
    SwapchainContext& context,
    uint64_t present_frametime_us) {
    if (present_frametime_us) {
        context.present_frametimes[context.present_frametime_index] =
            present_frametime_us;
        context.present_frametime_index =
            (context.present_frametime_index + 1)
            % static_cast<uint32_t>(context.present_frametimes.size());
        context.present_frametime_count = std::min<uint32_t>(
            context.present_frametime_count + 1,
            static_cast<uint32_t>(context.present_frametimes.size()));
        ++context.session_frametime_count;
        context.session_frametime_sum_us += present_frametime_us;
        if (!context.session_frametime_min_us
            || present_frametime_us < context.session_frametime_min_us) {
            context.session_frametime_min_us = present_frametime_us;
        }
        if (present_frametime_us > context.session_frametime_max_us) {
            context.session_frametime_max_us = present_frametime_us;
        }
    }
    return overlay_frame_stats_from_samples(
        context.present_frametimes.data(),
        context.present_frametime_count,
        context.session_frametime_count,
        context.session_frametime_sum_us,
        context.session_frametime_min_us,
        context.session_frametime_max_us);
}

std::string overlay_value_or_na(const std::string& value) {
    return value.empty() ? "n/a" : value;
}

bool overlay_value_missing(const std::string& value) {
    std::string text = trim_ascii(value);
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return text.empty() || text == "n/a";
}

std::string overlay_optional_value(const std::string& value, const char* suffix) {
    if (overlay_value_missing(value)) {
        return "";
    }
    std::string text = trim_ascii(value);
    std::string lower = text;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    std::string suffix_text(suffix);
    std::string suffix_lower = suffix_text;
    std::transform(
        suffix_lower.begin(),
        suffix_lower.end(),
        suffix_lower.begin(),
        [](unsigned char ch) {
            return static_cast<char>(std::tolower(ch));
        });
    if (lower.size() >= suffix_lower.size()
        && lower.rfind(suffix_lower) == lower.size() - suffix_lower.size()) {
        return text;
    }
    return text + suffix_text;
}

// Use the selected frame span without adding an unrelated presentation wait.
// Without an input marker the span is an estimate of responsiveness.
std::string overlay_frame_latency_value(
    const std::string& latency_ms,
    const std::string& source) {
    if (overlay_value_missing(latency_ms)) {
        return "";
    }
    const std::string value = trim_ascii(latency_ms);
    errno = 0;
    char* end = nullptr;
    const long latency = std::strtol(value.c_str(), &end, 10);
    if (errno != 0 || end == value.c_str()) {
        return "";
    }
    return std::string(source == "input-to-gpu" ? "" : "~") + std::to_string(latency);
}

std::string overlay_signed_value(const std::string& value) {
    if (overlay_value_missing(value)) {
        return "";
    }
    std::string text = trim_ascii(value);
    std::string lower = text;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    if (lower.size() >= 2 && lower.rfind("mv") == lower.size() - 2) {
        text = trim_ascii(text.substr(0, text.size() - 2));
    }
    char* end = nullptr;
    const long parsed = std::strtol(text.c_str(), &end, 10);
    if (end == text.c_str()) {
        return text;
    }
    char signed_text[64]{};
    std::snprintf(signed_text, sizeof(signed_text), "%+ld", parsed);
    return signed_text;
}

std::string overlay_fps_value_text(const std::string& raw_value, uint64_t fallback_fps) {
    std::string value = trim_ascii(raw_value);
    if (!value.empty()) {
        std::string lower = value;
        std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
            return static_cast<char>(std::tolower(ch));
        });
        if (lower.size() >= 3 && lower.rfind("fps") == lower.size() - 3) {
            return value;
        }
        return value + " FPS";
    }
    if (fallback_fps) {
        char fps_text[64]{};
        std::snprintf(fps_text, sizeof(fps_text), "%" PRIu64 " FPS", fallback_fps);
        return fps_text;
    }
    return "n/a FPS";
}

// Frametimes are measured in microseconds but read as milliseconds; one
// decimal is the resolution a player can act on.
std::string overlay_frametime_ms_text(uint64_t frametime_us) {
    char text[32]{};
    std::snprintf(
        text, sizeof(text), "%.1f", static_cast<double>(frametime_us) / 1000.0);
    return text;
}

std::string overlay_fps_text(uint64_t fps, const OverlayGpuState& state) {
    return overlay_fps_value_text(state.present_fps, fps);
}

std::string overlay_framegen_fps_text(uint64_t fps, const OverlayGpuState& state) {
    std::string value = trim_ascii(state.framegen_fps);
    if (!value.empty()) {
        std::string lower = value;
        std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
            return static_cast<char>(std::tolower(ch));
        });
        if (lower.size() >= 3 && lower.rfind("fps") == lower.size() - 3) {
            value = trim_ascii(value.substr(0, value.size() - 3));
        }
        return value.empty() ? "n/a" : value;
    }
    if (fps) {
        char fps_text[64]{};
        std::snprintf(fps_text, sizeof(fps_text), "%" PRIu64, fps);
        return fps_text;
    }
    return "n/a";
}

bool overlay_parse_fps_value(const std::string& raw_value, double* out) {
    std::string value = trim_ascii(raw_value);
    if (value.empty()) {
        return false;
    }
    std::string lower = value;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    if (lower == "n/a") {
        return false;
    }
    if (lower.size() >= 3 && lower.rfind("fps") == lower.size() - 3) {
        value = trim_ascii(value.substr(0, value.size() - 3));
    }
    char* end = nullptr;
    const double parsed = std::strtod(value.c_str(), &end);
    if (end == value.c_str() || parsed <= 0.0) {
        return false;
    }
    *out = parsed;
    return true;
}

std::string compact_profile_tier(const std::string& value) {
    // No tier means stock/default GPU state (Restore Defaults, per-game
    // Stock/Default): show DEF, never masquerade as a tuned tier.
    const std::string trimmed = trim_ascii(value);
    if (trimmed.empty() || trimmed == "stock" || trimmed == "Stock"
        || trimmed == "default" || trimmed == "Default") {
        return "DEF";
    }
    const std::string tier = trimmed;
    std::string lower = tier;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    if (lower == "balanced") {
        return "BAL";
    }
    if (lower == "efficiency") {
        return "EFF";
    }
    if (lower == "performance") {
        return "PERF";
    }
    std::string compact = tier.size() > 6 ? tier.substr(0, 6) : tier;
    std::transform(compact.begin(), compact.end(), compact.begin(), [](unsigned char ch) {
        return static_cast<char>(std::toupper(ch));
    });
    return compact;
}

// Power is not coloured: overlay-state.txt does not publish the board limit
// it would be measured against.
OverlayCellLevel overlay_value_level(
    const std::string& item,
    const OverlayGpuState& state) {
    if (item == "profile") {
        return OverlayCellLevel::Tier;
    }
    if (item == "uv_offset_mv") {
        return OverlayCellLevel::Good;
    }
    const bool is_cpu = item == "cpu_temp_c";
    if (item != "temperature_c" && !is_cpu) {
        return OverlayCellLevel::Normal;
    }
    const std::string value =
        trim_ascii(is_cpu ? state.cpu_temp_c : state.temperature_c);
    if (value.empty()) {
        return OverlayCellLevel::Normal;
    }
    errno = 0;
    char* end = nullptr;
    const long celsius = std::strtol(value.c_str(), &end, 10);
    if (end == value.c_str() || errno != 0) {
        return OverlayCellLevel::Normal;
    }
    const long alert = is_cpu ? kALERT_CPU_TEMPERATURE_C
                              : kALERT_TEMPERATURE_C;
    const long warn =
        is_cpu ? kWARN_CPU_TEMPERATURE_C : kWARN_TEMPERATURE_C;
    if (celsius >= alert) {
        return OverlayCellLevel::Alert;
    }
    if (celsius >= warn) {
        return OverlayCellLevel::Warn;
    }
    return OverlayCellLevel::Good;
}

// One metric with no label of its own: the designed layout puts the label in
// its own dim column, so the value arrives bare.
// Drop the vendor words the driver prefixes every model name with. The driver
// reports "NVIDIA GeForce RTX 5070 Ti"; in a HUD footer the first two words are
// the same on every card this will ever run on and they cost real width, since
// the footer is what sets the panel's width. Kept identical to
// `short_gpu_name` in overlay/overlay_text.py -- the parity harness drives both
// from one case table and fails if they disagree.
std::string overlay_short_gpu_name(const std::string& value) {
    std::string name = trim_ascii(value);
    for (const char* prefix : {"NVIDIA ", "GeForce "}) {
        const std::size_t length = std::char_traits<char>::length(prefix);
        if (name.size() >= length && name.compare(0, length, prefix) == 0) {
            name = trim_ascii(name.substr(length));
        }
    }
    return name;
}

// "9.4 / 16 GiB" from two mebibyte counts. The used figure carries a decimal
// because it is what moves; the total does not, because a card or a board does
// not grow one mid-game. Kept identical to `_gibibyte_pair` in
// overlay/overlay_text.py -- the parity harness fails if they disagree.
std::string overlay_gibibyte_pair(
    const std::string& used_mb,
    const std::string& total_mb) {
    const std::string used_text = trim_ascii(used_mb);
    const std::string total_text = trim_ascii(total_mb);
    if (used_text.empty() || total_text.empty()) {
        return "";
    }
    errno = 0;
    char* end = nullptr;
    const long total = std::strtol(total_text.c_str(), &end, 10);
    if (end == total_text.c_str() || errno != 0 || total <= 0) {
        return "";
    }
    errno = 0;
    end = nullptr;
    const long used = std::strtol(used_text.c_str(), &end, 10);
    if (end == used_text.c_str() || errno != 0) {
        return "";
    }
    char buffer[64];
    // lround on the total matches Python's round-half-even only away from .5,
    // which a mebibyte total never lands on: totals are whole gibibytes plus
    // the slice the firmware reserves.
    std::snprintf(
        buffer,
        sizeof(buffer),
        "%.1f / %ld GiB",
        static_cast<double>(used) / 1024.0,
        std::lround(static_cast<double>(total) / 1024.0));
    return std::string(buffer);
}

bool overlay_sensor_text(const std::string& item, const OverlayGpuState& state,
                         bool table, std::string& result) {
    for (const auto& spec : kOverlayMetrics) {
        if (item != spec.id) continue;
        const std::string suffix = std::string(spec.unit) == "%" ? "%" : std::string(" ") + spec.unit;
        result = overlay_optional_value(state.*spec.field, suffix.c_str());
        if (result.empty()) {
            const char* missing = table && *spec.table_missing ? spec.table_missing : spec.missing;
            if (*missing) result = std::string(missing) + (spec.missing_unit ? suffix : "");
        }
        const char* label = table ? spec.table_label : spec.hud_label;
        if (!result.empty() && *label) result = std::string(label) + " " + result;
        return true;
    }
    return false;
}

std::string overlay_item_value(
    const std::string& item,
    const OverlayFrameStats& stats,
    const OverlayDeviceFacts& facts,
    const OverlayGpuState& state) {
    std::string sensor;
    if (overlay_sensor_text(item, state, false, sensor)) return sensor;
    if (item == "base_fps") {
        const std::string published = trim_ascii(state.present_fps);
        if (!published.empty()) {
            return published;
        }
        return stats.median_fps ? std::to_string(stats.median_fps) : "n/a";
    }
    if (item == "fg_fps") {
        double output_fps = 0.0;
        if (state.framegen_active
            && overlay_parse_fps_value(state.framegen_fps, &output_fps)) {
            return overlay_framegen_fps_text(stats.median_fps, state) + " fps";
        }
        return "";
    }
    if (item == "latency_ms") {
        return overlay_optional_value(
            overlay_frame_latency_value(
                trim_ascii(state.latency_avg_ms).empty() ? state.latency_ms : state.latency_avg_ms,
                state.latency_source),
            " ms");
    }
    if (item == "profile") {
        // The designed layout has room for the tier's real name.
        const std::string tier = trim_ascii(state.profile_tier);
        return tier.empty() ? "Default" : tier;
    }
    if (item == "uv_offset_mv") {
        return overlay_optional_value(
            overlay_signed_value(state.uv_offset_mv), " mV");
    }
    if (item == "vram") {
        return overlay_gibibyte_pair(state.vram_used_mb, state.vram_total_mb);
    }
    if (item == "ram") {
        return overlay_gibibyte_pair(state.ram_used_mb, state.ram_total_mb);
    }
    if (item == "fps_1pct_low") {
        return stats.low_1pct_fps
            ? std::to_string(stats.low_1pct_fps) + " fps"
            : "";
    }
    if (item == "frametime_p97") {
        return stats.p97_frametime_us
            ? overlay_frametime_ms_text(stats.p97_frametime_us) + " ms"
            : "";
    }
    if (item == "fps_session") {
        return stats.session_avg_fps
            ? std::to_string(stats.session_min_fps) + "/"
                + std::to_string(stats.session_avg_fps) + "/"
                + std::to_string(stats.session_max_fps)
            : "";
    }
    if (item == "gpu_name") {
        return overlay_short_gpu_name(facts.gpu_name);
    }
    if (item == "driver_version") {
        return facts.driver_version.empty() ? "" : "driver " + facts.driver_version;
    }
    if (item == "resolution") {
        return facts.resolution;
    }
    if (item == "present_mode") {
        return facts.present_mode;
    }
    return "";
}

std::string overlay_item_cell(
    const std::string& item,
    const OverlayFrameStats& stats,
    const OverlayDeviceFacts& facts,
    const OverlayGpuState& state) {
    std::string sensor;
    if (overlay_sensor_text(item, state, true, sensor)) return sensor;
    const uint64_t fps = stats.median_fps;
    if (item == "gpu_name") {
        return overlay_short_gpu_name(facts.gpu_name);
    }
    if (item == "driver_version") {
        return facts.driver_version.empty()
            ? ""
            : "driver " + facts.driver_version;
    }
    if (item == "resolution") {
        return facts.resolution;
    }
    if (item == "present_mode") {
        return facts.present_mode;
    }
    if (item == "base_fps") {
        return overlay_fps_text(fps, state);
    }
    if (item == "fps_1pct_low") {
        // Hidden until the ring has measured something; a 0 here would read as
        // a catastrophic stutter rather than as "no data yet".
        return stats.low_1pct_fps
            ? "1% low " + std::to_string(stats.low_1pct_fps)
            : "";
    }
    if (item == "frametime_p97") {
        return stats.p97_frametime_us
            ? "p97 " + overlay_frametime_ms_text(stats.p97_frametime_us) + " ms"
            : "";
    }
    if (item == "frametime_graph") {
        // Drawn by the renderer, not written into the readout.
        return "";
    }
    if (item == "fps_session") {
        return stats.session_avg_fps
            ? "min/avg/max " + std::to_string(stats.session_min_fps) + "/"
                + std::to_string(stats.session_avg_fps) + "/"
                + std::to_string(stats.session_max_fps)
            : "";
    }
    if (item == "fg_fps") {
        double output_fps = 0.0;
        if (state.framegen_active
            && overlay_parse_fps_value(state.framegen_fps, &output_fps)) {
            return overlay_framegen_fps_text(fps, state) + " FG";
        }
        return "";
    }
    if (item == "profile") {
        return compact_profile_tier(state.profile_tier);
    }
    if (item == "latency_ms") {
        const std::string latency = overlay_optional_value(
            overlay_frame_latency_value(
                trim_ascii(state.latency_avg_ms).empty() ? state.latency_ms : state.latency_avg_ms,
                state.latency_source),
            " ms");
        return latency.empty() ? "" : std::string(trim_ascii(state.latency_avg_ms).empty() ? "LAT P95 " : "LAT AVG ") + latency;
    }
    if (item == "vram") {
        const std::string pair =
            overlay_gibibyte_pair(state.vram_used_mb, state.vram_total_mb);
        return pair.empty() ? "" : "VRAM " + pair;
    }
    if (item == "ram") {
        const std::string pair =
            overlay_gibibyte_pair(state.ram_used_mb, state.ram_total_mb);
        return pair.empty() ? "" : "RAM " + pair;
    }
    if (item == "uv_offset_mv") {
        const std::string uv = overlay_optional_value(
            overlay_signed_value(state.uv_offset_mv), " mV");
        return uv.empty() ? "" : "UV " + uv;
    }
    return "";
}

// Vendors pack driverVersion differently and Vulkan does not normalise it.
// NVIDIA's proprietary driver uses its own 10/8/8/6-bit scheme, which a
// generic decode turns into nonsense for the exact card this tool exists for.
// Every Mesa driver (Intel ANV, AMD RADV) uses VK_MAKE_API_VERSION; Intel's
// 18/14-bit packing exists only in its Windows driver.
// shortcut: Mesa's NVK also reports vendor 0x10DE and decodes wrong here;
// switch to VkPhysicalDeviceDriverProperties if NVK is ever supported.
std::string overlay_driver_version_text(uint32_t version, uint32_t vendor_id) {
    char text[64]{};
    if (vendor_id == 0x10DE) {  // NVIDIA
        std::snprintf(
            text,
            sizeof(text),
            "%u.%u",
            (version >> 22) & 0x3ff,
            (version >> 14) & 0x0ff);
        return text;
    }
    std::snprintf(
        text,
        sizeof(text),
        "%u.%u.%u",
        version >> 22,
        (version >> 12) & 0x3ff,
        version & 0xfff);
    return text;
}

std::string overlay_present_mode_text(VkPresentModeKHR mode) {
    switch (mode) {
        case VK_PRESENT_MODE_IMMEDIATE_KHR:
            return "immediate";
        case VK_PRESENT_MODE_MAILBOX_KHR:
            return "mailbox";
        case VK_PRESENT_MODE_FIFO_KHR:
            return "fifo";
        case VK_PRESENT_MODE_FIFO_RELAXED_KHR:
            return "fifo-relaxed";
        case VK_PRESENT_MODE_SHARED_DEMAND_REFRESH_KHR:
            return "shared-demand";
        case VK_PRESENT_MODE_SHARED_CONTINUOUS_REFRESH_KHR:
            return "shared-continuous";
        default:
            return "";
    }
}

std::string overlay_resolution_text(const VkExtent2D& extent) {
    if (!extent.width || !extent.height) {
        return "";
    }
    char text[32]{};
    std::snprintf(text, sizeof(text), "%ux%u", extent.width, extent.height);
    return text;
}

// Pad every column to its widest cell so the rows read as a table. The overlay
// font is fixed-pitch, which is what lets plain spaces do the aligning. The
// character offsets of any cell that is not drawn in the default colour fall
// out of the same pass, because only this function knows where padding put it.
OverlayReadout align_overlay_columns(
    const std::vector<std::vector<std::string>>& rows,
    const std::vector<std::vector<OverlayCellLevel>>& levels) {
    std::vector<std::size_t> widths;
    for (const std::vector<std::string>& cells : rows) {
        if (widths.size() < cells.size()) {
            widths.resize(cells.size(), 0);
        }
        for (std::size_t column = 0; column < cells.size(); ++column) {
            widths[column] = std::max(widths[column], cells[column].size());
        }
    }
    OverlayReadout readout;
    for (std::size_t row = 0; row < rows.size(); ++row) {
        const std::vector<std::string>& cells = rows[row];
        OverlayReadoutRow unpadded;
        unpadded.cells.reserve(cells.size());
        for (std::size_t column = 0; column < cells.size(); ++column) {
            unpadded.cells.push_back(
                OverlayReadoutCell{cells[column], levels[row][column]});
        }
        readout.rows.push_back(std::move(unpadded));
        std::string line;
        for (std::size_t column = 0; column < cells.size(); ++column) {
            if (column) {
                line += ' ';
            }
            const uint32_t start = static_cast<uint32_t>(line.size());
            line += cells[column];
            if (levels[row][column] != OverlayCellLevel::Normal) {
                readout.highlights.push_back(OverlayTextSpan{
                    static_cast<uint32_t>(row),
                    start,
                    static_cast<uint32_t>(cells[column].size()),
                    levels[row][column]});
            }
            if (cells[column].size() < widths[column]) {
                line.append(widths[column] - cells[column].size(), ' ');
            }
        }
        while (!line.empty() && line.back() == ' ') {
            line.pop_back();
        }
        if (!readout.text.empty()) {
            readout.text += '\n';
        }
        readout.text += line;
    }
    return readout;
}

// The designed layout, built from the shared row table. A row whose items are
// all switched off is dropped, and so is a rule with nothing after it.
OverlayReadout build_overlay_hud_readout(
    const OverlayFrameStats& stats,
    const OverlayDeviceFacts& facts,
    const OverlayGpuState& state,
    const OverlayTextConfig& config) {
    OverlayReadout readout;
    for (const OverlayHudRow& source : kOverlayHudRows) {
        if (source.kind == OverlayRowKind::Rule) {
            if (!readout.rows.empty()
                && readout.rows.back().kind != OverlayRowKind::Rule) {
                OverlayReadoutRow rule;
                rule.kind = OverlayRowKind::Rule;
                readout.rows.push_back(std::move(rule));
            }
            continue;
        }
        OverlayReadoutRow row;
        row.kind = source.kind;
        row.label = source.label;
        for (const char* item_id : source.items) {
            const std::string item(item_id);
            if (std::find(config.items.begin(), config.items.end(), item)
                == config.items.end()) {
                continue;
            }
            std::string value = overlay_item_value(item, stats, facts, state);
            if (!value.empty()) {
                row.cells.push_back(
                    OverlayReadoutCell{value, overlay_value_level(item, state)});
            }
        }
        if (row.cells.empty()) {
            continue;
        }
        if (source.kind == OverlayRowKind::Hero) {
            // The unit is its own cell: the renderer draws the number at twice
            // the body size and drops "fps" onto the same baseline beside it.
            row.cells.push_back(OverlayReadoutCell{"fps", OverlayCellLevel::Dim});
        } else if (source.kind == OverlayRowKind::Foot) {
            std::string joined;
            for (const OverlayReadoutCell& cell : row.cells) {
                if (!joined.empty()) {
                    joined += " | ";
                }
                joined += cell.text;
            }
            row.cells.assign(1, OverlayReadoutCell{joined, OverlayCellLevel::Dim});
        }
        readout.rows.push_back(std::move(row));
    }
    while (!readout.rows.empty()
        && readout.rows.back().kind == OverlayRowKind::Rule) {
        readout.rows.pop_back();
    }
    if (readout.rows.empty()) {
        return readout;
    }

    // The text form the desktop preview shows and the parity harness diffs.
    std::size_t label_width = 0;
    for (const OverlayReadoutRow& row : readout.rows) {
        if (row.kind != OverlayRowKind::Rule) {
            label_width = std::max(label_width, row.label.size());
        }
    }
    std::vector<std::string> lines;
    for (const OverlayReadoutRow& row : readout.rows) {
        if (row.kind == OverlayRowKind::Rule) {
            lines.push_back("");
            continue;
        }
        std::string body;
        for (const OverlayReadoutCell& cell : row.cells) {
            if (!body.empty()) {
                body += " ";
            }
            body += cell.text;
        }
        if (row.kind == OverlayRowKind::Hero || row.kind == OverlayRowKind::Foot
            || !label_width) {
            lines.push_back(body);
            continue;
        }
        std::string line = row.label;
        line.append(label_width - row.label.size(), ' ');
        line += " ";
        line += body;
        while (!line.empty() && line.back() == ' ') {
            line.pop_back();
        }
        lines.push_back(line);
    }
    std::size_t width = 0;
    for (const std::string& line : lines) {
        width = std::max(width, line.size());
    }
    for (std::size_t index = 0; index < lines.size(); ++index) {
        if (lines[index].empty()) {
            lines[index] = std::string(width, '-');
        }
        if (!readout.text.empty()) {
            readout.text += '\n';
        }
        readout.text += lines[index];
    }
    return readout;
}

OverlayReadout build_overlay_readout(
    const OverlayFrameStats& stats,
    const OverlayDeviceFacts& facts,
    const OverlayGpuState& state,
    uint64_t now_us) {
    const OverlayTextConfig config = overlay_text_config(now_us);
    if (!config.enabled) {
        return {};
    }
    if (config.layout == OverlayLayout::Hud) {
        OverlayReadout hud = build_overlay_hud_readout(stats, facts, state, config);
        if (!hud.rows.empty()) {
            return hud;
        }
        OverlayReadout waiting;
        waiting.text = "PB waiting";
        OverlayReadoutRow row;
        row.cells.push_back(OverlayReadoutCell{waiting.text});
        waiting.rows.push_back(std::move(row));
        return waiting;
    }
    std::vector<std::vector<std::string>> rows;
    std::vector<std::vector<OverlayCellLevel>> levels;
    for (const std::vector<const char*>& group : kOverlayRowGroups) {
        std::vector<std::string> cells;
        std::vector<OverlayCellLevel> cell_levels;
        for (const char* item_id : group) {
            const std::string item(item_id);
            if (std::find(config.items.begin(), config.items.end(), item)
                == config.items.end()) {
                continue;
            }
            std::string cell = overlay_item_cell(item, stats, facts, state);
            if (!cell.empty()) {
                cells.push_back(std::move(cell));
                cell_levels.push_back(overlay_value_level(item, state));
            }
        }
        if (!cells.empty()) {
            rows.push_back(std::move(cells));
            levels.push_back(std::move(cell_levels));
        }
    }
    if (rows.empty()) {
        OverlayReadout waiting;
        waiting.text = "PB waiting";
        OverlayReadoutRow row;
        row.cells.push_back(OverlayReadoutCell{waiting.text});
        waiting.rows.push_back(std::move(row));
        return waiting;
    }
    return align_overlay_columns(rows, levels);
}

std::string build_overlay_text(
    const OverlayFrameStats& stats,
    const OverlayDeviceFacts& facts,
    const OverlayGpuState& state,
    uint64_t now_us) {
    return build_overlay_readout(stats, facts, state, now_us).text;
}


}  // namespace pblayer
