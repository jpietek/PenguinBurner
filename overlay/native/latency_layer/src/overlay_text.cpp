// Overlay configuration parsing and the compact overlay text string.
#include "latency_layer_internal.h"

namespace pblayer {
double parse_overlay_scale(const std::string& value) {
    const std::string text = trim_ascii(value);
    if (text.empty()) {
        return 1.0;
    }
    errno = 0;
    char* end = nullptr;
    const double parsed = std::strtod(text.c_str(), &end);
    if (end == text.c_str() || errno != 0 || !(parsed > 0.0)) {
        return 1.0;
    }
    // Guard against absurd values; the UI only offers 0.5/1.0/2.0.
    if (parsed < 0.25) {
        return 0.25;
    }
    if (parsed > 4.0) {
        return 4.0;
    }
    return parsed;
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

bool overlay_enabled() {
    // Launch-time default from the wrapper env, overridable LIVE by a small
    // runtime file the UI writes ("1"/"0"): the layer is always loaded, so
    // visibility can flip on a running game without a restart. The file is
    // re-checked at most once a second; the wrapper clears it at launch so a
    // stale override never leaks into the next game.
    static const bool env_default = !value_is_false(overlay_env_value());
    static std::mutex override_mutex;
    static std::chrono::steady_clock::time_point last_check{};
    static int override_state = -1;  // -1 unknown/absent, 0 off, 1 on

    std::lock_guard<std::mutex> lock(override_mutex);
    const auto now = std::chrono::steady_clock::now();
    if (last_check.time_since_epoch().count() == 0
        || now - last_check >= std::chrono::seconds(1)) {
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
    if (override_state >= 0) {
        return override_state == 1;
    }
    return env_default;
}

bool overlay_env_fallback_enabled() {
    static const bool enabled = value_is_true(overlay_env_value());
    return enabled;
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

const std::array<const char*, 13> kOverlayItemOrder = {
    "base_fps",
    "fg_fps",
    "latency_ms",
    "clock_mhz",
    "voltage_mv",
    "power_w",
    "profile",
    "gpu_util_pct",
    "cpu_util_pct",
    "cpu_peak_thread_pct",
    "fan_pct",
    "temperature_c",
    "uv_offset_mv",
};

OverlayTextConfig default_overlay_text_config(bool enabled = true) {
    return OverlayTextConfig{enabled, {
        "base_fps",
        "fg_fps",
        "latency_ms",
        "clock_mhz",
        "voltage_mv",
        "power_w",
        "profile",
    }};
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
    double scale = 1.0;
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
        }
    }
    std::fclose(file);
    // An explicit launch env (PB_OVERLAY=1) wins over the saved UI toggle; the
    // saved config only decides the wrapper's automatic/default overlay mode.
    if (fallback_enabled) {
        enabled = true;
    }
    OverlayTextConfig result = requested.empty()
        ? default_overlay_text_config(enabled)
        : OverlayTextConfig{enabled, normalize_overlay_items(requested), scale};
    result.scale = scale;
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
        g_overlay_user_scale.store(config.scale, std::memory_order_relaxed);
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
        } else if (key == "uv_offset_mv") {
            state.uv_offset_mv = value;
        } else if (key == "profile_id") {
            state.profile_id = value;
        } else if (key == "profile_tier" && !value.empty()) {
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

uint64_t median_fps_from_present_frametime_locked(
    SwapchainContext& context,
    uint64_t present_frametime_us) {
    if (!present_frametime_us) {
        return 0;
    }
    context.present_frametimes[context.present_frametime_index] =
        present_frametime_us;
    context.present_frametime_index =
        (context.present_frametime_index + 1)
        % static_cast<uint32_t>(context.present_frametimes.size());
    context.present_frametime_count = std::min<uint32_t>(
        context.present_frametime_count + 1,
        static_cast<uint32_t>(context.present_frametimes.size()));
    std::vector<uint64_t> values;
    values.reserve(context.present_frametime_count);
    for (uint32_t i = 0; i < context.present_frametime_count; ++i) {
        uint64_t value = context.present_frametimes[i];
        if (value) {
            values.push_back(value);
        }
    }
    if (values.empty()) {
        return 0;
    }
    std::sort(values.begin(), values.end());
    const uint64_t median = values[values.size() / 2];
    return median ? static_cast<uint64_t>((1000000ull + median / 2) / median) : 0;
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

// Render latency plus the optional present->scanout display tail, as one number.
// When the display field is absent (display-latency capture not enabled via the
// Steam params), this collapses to the render latency alone. A display tail
// with no marker latency renders nothing: scanout timing alone is not a
// latency figure (mirrors overlay_text.py).
std::string overlay_combined_latency_value(
    const std::string& render_ms,
    const std::string& display_ms) {
    if (overlay_value_missing(render_ms)) {
        return "";
    }
    const std::string render_text = trim_ascii(render_ms);
    errno = 0;
    char* render_end = nullptr;
    const long render = std::strtol(render_text.c_str(), &render_end, 10);
    if (errno != 0 || render_end == render_text.c_str()) {
        return "";
    }
    long total = render;
    if (!overlay_value_missing(display_ms)) {
        const std::string display_text = trim_ascii(display_ms);
        errno = 0;
        char* display_end = nullptr;
        const long display = std::strtol(display_text.c_str(), &display_end, 10);
        if (errno == 0 && display_end != display_text.c_str()) {
            total += display;
        }
    }
    return std::to_string(total);
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

void append_overlay_part(std::string& text, const std::string& part) {
    if (part.empty()) {
        return;
    }
    if (!text.empty()) {
        text += " ";
    }
    text += part;
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

std::string build_overlay_text(
    uint64_t fps,
    const OverlayGpuState& state,
    uint64_t now_us) {
    std::string text;
    const OverlayTextConfig config = overlay_text_config(now_us);
    if (!config.enabled) {
        return "";
    }
    for (const std::string& item : config.items) {
        if (item == "base_fps") {
            append_overlay_part(text, overlay_fps_text(fps, state));
        } else if (item == "fg_fps") {
            double output_fps = 0.0;
            if (state.framegen_active
                && overlay_parse_fps_value(state.framegen_fps, &output_fps)) {
                append_overlay_part(
                    text,
                    overlay_framegen_fps_text(fps, state) + " FG");
            }
        } else if (item == "clock_mhz") {
            append_overlay_part(text, overlay_value_or_na(state.clock_mhz) + " MHz");
        } else if (item == "voltage_mv") {
            append_overlay_part(text, overlay_value_or_na(state.voltage_mv) + " mV");
        } else if (item == "power_w") {
            append_overlay_part(text, overlay_value_or_na(state.power_w) + " W");
        } else if (item == "profile") {
            append_overlay_part(text, compact_profile_tier(state.profile_tier));
        } else if (item == "gpu_util_pct") {
            const std::string util = overlay_optional_value(state.gpu_util_pct, "%");
            if (!util.empty()) {
                append_overlay_part(text, "GPU " + util);
            }
        } else if (item == "cpu_util_pct") {
            const std::string util = overlay_optional_value(state.cpu_util_pct, "%");
            append_overlay_part(text, "CPU " + (util.empty() ? "--%" : util));
        } else if (item == "cpu_peak_thread_pct") {
            const std::string util =
                overlay_optional_value(state.cpu_peak_thread_pct, "%");
            append_overlay_part(text, "CPU-T " + (util.empty() ? "--%" : util));
        } else if (item == "fan_pct") {
            const std::string fan = overlay_optional_value(state.fan_pct, "%");
            if (!fan.empty()) {
                append_overlay_part(text, "FAN " + fan);
            }
        } else if (item == "temperature_c") {
            const std::string temp = overlay_optional_value(state.temperature_c, " C");
            if (!temp.empty()) {
                append_overlay_part(text, "T " + temp);
            }
        } else if (item == "latency_ms") {
            const std::string latency = overlay_optional_value(
                overlay_combined_latency_value(
                    state.latency_ms, state.display_latency_ms),
                " ms");
            if (!latency.empty()) {
                append_overlay_part(text, "LAT " + latency);
            }
        } else if (item == "uv_offset_mv") {
            const std::string uv = overlay_optional_value(
                overlay_signed_value(state.uv_offset_mv),
                " mV");
            if (!uv.empty()) {
                append_overlay_part(text, "UV " + uv);
            }
        }
    }
    return text.empty() ? "PB WAITING" : text;
}

std::string cached_overlay_text(uint64_t fps, uint64_t now_us) {
    static uint64_t last_read_us = 0;
    static std::string last_text;
    std::lock_guard lock(g_overlay_file_mutex);
    if (!last_read_us || now_us < last_read_us
        || now_us - last_read_us >= 250000) {
        last_text = build_overlay_text(fps, read_overlay_gpu_state(), now_us);
        last_read_us = now_us;
    }
    return last_text;
}

}  // namespace pblayer
