/* overlay.js: the In-Game Overlay page. The model half mirrors overlay/metrics.py,
 * overlay/overlay_text.py and ui/components/overlay_config.py, so the preview draws
 * the same rows the in-game layer draws. Regenerate the snapshot below from those
 * modules when their catalog changes. */

// Snapshot of overlay/metrics.py: telemetry key, units, table/HUD labels,
// missing-value rules and the long description behind each 'i' button.
const OV_SPECS = {
  base_fps: { key: "present_fps", unit: "FPS",
    info: "Exact base present FPS published by PenguinBurner. This is the pre-frame-generation present_fps headline, not the output FG cadence." },
  fg_fps: { key: "framegen_fps", unit: "FG",
    info: "Frame-generation output FPS. It is shown only when PenguinBurner marks frame generation as active." },
  latency_ms: { key: "latency_ms", unit: "ms",
    info: "Average frame latency over the last 3 seconds, from input/simulation or render submission to the driver-reported GPU completion. A ~ prefix marks an estimate. Without Reflex markers, the Vulkan overlay pairs the previous Present call with this frame's GPU completion, including GPU queueing. That frame-cycle proxy can miss engine/input work, especially with asynchronous rendering or frame generation. P95 is shown instead with older daemons that do not publish the average. This is not measured mouse-to-screen latency; input sampling waits, presentation and monitor response are not fully covered. Driver timing accuracy under Proton/frame generation still needs validation." },
  clock_mhz: { key: "clock_mhz", unit: "MHz", missing: "n/a", sensor: true,
    info: "Current GPU graphics clock in MHz." },
  voltage_mv: { key: "voltage_mv", unit: "mV", missing: "n/a", sensor: true,
    info: "Current live GPU voltage in mV." },
  power_w: { key: "power_w", unit: "W", missing: "n/a", sensor: true,
    info: "Current GPU board power draw in watts." },
  profile: { key: "profile_tier",
    info: "Active profile tier reported by the runtime." },
  fps_1pct_low: { key: "fps_1pct_low", table: "1% low", example: true,
    info: "Frame rate of the slowest one per cent of recent frames, averaged. This is the number that moves when an undervolt starts costing you stutter: a tier that holds 1% low while dropping voltage is a good tier, and the headline FPS will not tell you that. Measured inside the game by the overlay layer over its last 120 presents, so it is blank until a wrapped game is running." },
  frametime_p97: { key: "frametime_p97", unit: "ms", table: "p97", example: true,
    info: "The frametime 97% of recent frames came in under, in milliseconds. A tail measure like the 1% low, expressed as time rather than rate: useful when you care how long the worst frames took, not how many frames per second that implies. Measured in-game by the overlay layer." },
  fps_session: { key: "fps_session", table: "min/avg/max", example: true,
    info: "Slowest, average and fastest frame rate since the game started. The average is taken over the session's total frame time, so a burst of cheap frames cannot drag it upwards. The extremes are single frames and will look spiky -- one alt-tab or shader compile sets the minimum for the rest of the session." },
  frametime_graph: { key: "frametime_graph",
    info: "Draws the last frames as bars under the readout, newest on the right, with a rule across the window's median frametime. Bars that reach twice the median turn amber, so a stutter is visible without reading any number. The bars are scaled against the worse of the window's slowest frame or twice its median, which keeps a smooth run looking flat instead of amplifying noise. Drawn by the overlay layer inside the game." },
  gpu_name: { key: "gpu_name", example: true,
    info: "The GPU the game is actually rendering on, as its driver reports it. Read inside the game by the overlay layer, so on a multi-GPU machine it shows the card the game picked rather than the one the desktop app is tuning." },
  driver_version: { key: "driver_version", table: "driver", hud: "driver", example: true,
    info: "Graphics driver version, decoded the way the card's vendor packs it. Read inside the game by the overlay layer." },
  resolution: { key: "resolution", example: true,
    info: "Size of the game's Vulkan presentation surface. Upscaling may render the scene at a different internal resolution." },
  present_mode: { key: "present_mode", example: true,
    info: "Presentation mode requested when the game creates its Vulkan swapchain. FIFO waits for display refresh; Mailbox keeps the newest pending frame; Immediate does not wait for refresh. This readout does not change VSync or establish the monitor's final VRR behavior. Per-present mode changes may not be reflected." },
  nr_gpu_ms: { key: "nr_gpu_ms", unit: "ms", table: "dlss5 nr", missing: "-", sensor: true, noMissingUnit: true,
    info: "Average GPU elapsed time per DLSS 5 neural-rendering model evaluation, in milliseconds. This excludes surrounding injector work and is not added input latency. Enable before launching the game; restart an already running game to start capture. Missing measurements display a dash." },
  gpu_util_pct: { key: "gpu_util_pct", unit: "%", table: "GPU", sensor: true,
    info: "Current GPU utilization (0-100%) from NVML. Missing data is hidden." },
  cpu_util_pct: { key: "cpu_util_pct", unit: "%", table: "CPU", tableMissing: "--", sensor: true,
    info: "Current game process CPU usage from procfs, normalized to total system CPU capacity so it stays in the 0-100% range. Missing process data is hidden." },
  cpu_peak_thread_pct: { key: "cpu_peak_thread_pct", unit: "%", table: "CPU-T", hud: "peak", tableMissing: "--", sensor: true,
    info: "Busiest game thread over the last overlay interval, normalized to one logical CPU. Values near 100% indicate a likely single-thread or main thread bottleneck. This is not tied to one fixed core because the scheduler may migrate the thread." },
  fan_pct: { key: "fan_pct", unit: "%", table: "Fan", hud: "fan", sensor: true,
    info: "Current reported GPU fan speed percentage. Missing fan data is hidden." },
  temperature_c: { key: "temperature_c", unit: "C", table: "T", sensor: true,
    info: "Current GPU temperature in C. Missing temperature data is hidden." },
  hotspot_temperature_c: { key: "hotspot_temperature_c", unit: "C", table: "HOT", sensor: true,
    info: "Hottest GPU sensor temperature in C, when supported by the GPU and driver. Missing hotspot data is hidden." },
  uv_offset_mv: { key: "uv_offset_mv", unit: "mV", table: "UV",
    info: "Current voltage delta from the stock VF point for the nearest active operating point. Missing VF data is hidden." },
  mem_clock_mhz: { key: "mem_clock_mhz", unit: "MHz", table: "MEM", sensor: true,
    info: "Current memory clock from NVML, published by the daemon beside the core clock. Hidden when the card does not report one." },
  vram: { key: "vram_used_mb", table: "VRAM",
    info: "Video memory in use and the card's total, as the driver counts it -- so it includes whatever else is on the GPU, not just this game. Published by the daemon; hidden when the card reports no memory info." },
  ram: { key: "ram_used_mb", table: "RAM",
    info: "System memory in use and installed. \"Used\" is total minus available, not total minus free: Linux lends nearly all idle memory to the page cache, so total-minus-free would read as a full machine at all times." },
  cpu_temp_c: { key: "cpu_temp_c", unit: "C", table: "CPU", sensor: true,
    info: "CPU package temperature, read by the daemon from the kernel's hwmon sensors. Colours later than the GPU's: a CPU is expected to run hotter. Hidden on a box whose sensor driver we do not recognise -- a missing row is honest, a wrong number is not." },
  cpu_power_w: { key: "cpu_power_w", unit: "W", table: "CPU", sensor: true,
    info: "CPU package power, averaged over the interval since the last refresh from the kernel's RAPL energy counter. The first reading after a start has no interval to average over, so it is blank. Hidden entirely on a box without RAPL." },
};
const OV_ITEM_IDS = ["nr_gpu_ms", "base_fps", "fg_fps", "latency_ms", "clock_mhz", "voltage_mv", "power_w", "profile", "fps_1pct_low", "frametime_p97", "fps_session", "frametime_graph", "gpu_name", "driver_version", "resolution", "present_mode", "gpu_util_pct", "cpu_util_pct", "cpu_peak_thread_pct", "fan_pct", "temperature_c", "hotspot_temperature_c", "uv_offset_mv", "mem_clock_mhz", "vram", "ram", "cpu_temp_c", "cpu_power_w"];
const OV_HUD_ROWS = [
  ["hero", "", ["base_fps"]],
  ["value", "fg", ["fg_fps"]],
  ["value", "lat", ["latency_ms"]],
  ["value", "dlss5 nr", ["nr_gpu_ms"]],
  ["value", "tier", ["profile"]],
  ["rule", "", []],
  ["value", "gpu", ["clock_mhz", "voltage_mv"]],
  ["value", "", ["power_w", "temperature_c"]],
  ["value", "hotspot", ["hotspot_temperature_c"]],
  ["value", "load", ["gpu_util_pct", "fan_pct"]],
  ["value", "mem", ["mem_clock_mhz"]],
  ["value", "uv", ["uv_offset_mv"]],
  ["rule", "", []],
  ["value", "1% low", ["fps_1pct_low"]],
  ["value", "p97", ["frametime_p97"]],
  ["value", "session", ["fps_session"]],
  ["value", "vram", ["vram"]],
  ["value", "cpu", ["cpu_util_pct", "cpu_temp_c"]],
  ["value", "", ["cpu_peak_thread_pct", "cpu_power_w"]],
  ["value", "ram", ["ram"]],
  ["foot", "", ["gpu_name", "driver_version", "resolution", "present_mode"]],
];
const OV_ROW_GROUPS = [
  ["base_fps", "fg_fps", "latency_ms", "nr_gpu_ms", "profile"],
  ["fps_1pct_low", "frametime_p97", "fps_session"],
  ["frametime_graph"],
  ["clock_mhz", "voltage_mv", "uv_offset_mv"],
  ["power_w", "temperature_c", "hotspot_temperature_c", "fan_pct"],
  ["gpu_util_pct", "cpu_util_pct", "cpu_peak_thread_pct"],
  ["mem_clock_mhz", "vram", "ram"],
  ["cpu_temp_c", "cpu_power_w"],
  ["gpu_name", "driver_version", "resolution", "present_mode"],
];
// overlay_text.SAMPLE_OVERLAY_VALUES: the demo readings both renderers share.
const OV_SAMPLE = {"nr_gpu_ms": "2.20", "present_fps": "60", "fps_1pct_low": "47", "frametime_p97": "21.3", "fps_session_min": "38", "fps_session_avg": "58", "fps_session_max": "61", "gpu_name": "GPU model name", "driver_version": "580.76", "resolution": "2560x1440", "present_mode": "fifo", "framegen_fps": "120", "framegen_active": "1", "clock_mhz": "2820", "voltage_mv": "925", "power_w": "318", "profile_tier": "Balanced", "gpu_util_pct": "97", "cpu_util_pct": "32", "cpu_peak_thread_pct": "98", "fan_pct": "62", "temperature_c": "67", "hotspot_temperature_c": "79", "latency_ms": "40", "display_latency_ms": "", "uv_offset_mv": "-75", "mem_clock_mhz": "10501", "vram_used_mb": "9626", "vram_total_mb": "16376", "ram_used_mb": "14540", "ram_total_mb": "32768", "cpu_temp_c": "61", "cpu_power_w": "88"};
// overlay/shortcuts.py SHORTCUTS: label, CLI flag, Alt+key.
const OV_SHORTCUTS = [
  ["Toggle overlay", "--overlay-toggle", "X"],
  ["Cycle overlay preset", "--overlay-detail-next", "C"],
  ["Cycle frame timing graphs", "--overlay-graph-next", "G"],
  ["Cycle background opacity", "--overlay-opacity-next", "O"],
  ["Decrease overlay Scale", "--overlay-scale-down", "["],
  ["Increase overlay Scale", "--overlay-scale-up", "]"],
  ["Previous overlay position", "--overlay-position-prev", "<"],
  ["Next overlay position", "--overlay-position-next", ">"],
];

// ---------- Model: overlay/config.py, overlay_text.py, overlay_config.py ----------
const OV_POSITIONS = [
  ["top_left", "Top left"],
  ["top_center", "Top center"],
  ["top_right", "Top right"],
  ["bottom_left", "Bottom left"],
  ["bottom_right", "Bottom right"],
];
const OV_LAYOUTS = [
  ["hud", "HUD"],
  ["table", "Table"],
];
const OV_GRAPHS = [
  ["base", "Base frames (pre-FG)"],
  ["display", "Displayed frames (post-FG)"],
  ["both", "Both graphs"],
];
const OV_FONTS = [
  ["pixel", "Pixel (bitmap)"],
  ["mono", "Mono"],
  ["sans", "Sans"],
];
const OV_PRESETS = [
  ["minimal", "Small"],
  ["standard", "Medium"],
  ["detailed", "Large"],
  ["full", "Biggest"],
  ["custom", "Custom"],
];
const OV_PRESET_CYCLE = ["minimal", "standard", "detailed", "full"];
const OV_SCALES = [0.5, 0.75, 1, 1.5, 2];
const OV_OPACITY = { min: 20, max: 100, step: 5, page: 10, initial: 85 };
const OV_OPACITY_CYCLE = [50, 60, 70, 75, 80, 85, 90, 95, 100];
const OV_INTERVAL = { min: 1, max: 10 };
const OV_LEVEL = { normal: 0, warn: 1, alert: 2, dim: 3, good: 4, tier: 5 };
const OV_LEVEL_CLASS = ["", "warn", "alert", "dim", "good", "tier"];
const OV_MISSING_EXECUTABLE =
  "The live preview needs vkcube, from the vulkan-tools package. Install it and the button will work; everything else on this tab is unaffected.";
const OV_KEEP_ONE = "Keep at least one overlay item enabled.";
const OV_CONFIG_PATH = "~/.config/penguin-burner/overlay.toml";

function ovDefaultConfig() {
  return {
    enabled: true,
    items: [...sample.defaults],
    preset: "custom",
    interval: 1,
    scale: 1,
    position: "top_right",
    opacity: OV_OPACITY.initial,
    font: "sans",
    layout: "hud",
    graph: "base",
  };
}
const ovStr = (v) => String(v ?? "").trim();
const ovOrdered = (ids) => {
  const wanted = new Set(ids);
  const ordered = OV_ITEM_IDS.filter((k) => wanted.has(k));
  return ordered.length ? ordered : ["base_fps"];
};
// A preset owns the field list while it is selected; Custom keeps the saved set.
function ovEnabledIds(cfg) {
  return ovOrdered(
    cfg.preset === "custom" ? cfg.items : sample.presets[cfg.preset] || [],
  );
}
// set_overlay_item_enabled: never drops the last item, and an edit selects Custom.
function ovWithItem(cfg, id, on) {
  const current = new Set(ovEnabledIds(cfg));
  if (on) current.add(id);
  else if (current.size > 1) current.delete(id);
  return { ...cfg, items: ovOrdered([...current]), preset: "custom" };
}
function ovNextPreset(preset) {
  const i = OV_PRESET_CYCLE.indexOf(preset);
  return OV_PRESET_CYCLE[i < 0 ? 0 : (i + 1) % OV_PRESET_CYCLE.length];
}
function ovNextOpacity(percent) {
  return OV_OPACITY_CYCLE.find((p) => p > percent) ?? OV_OPACITY_CYCLE[0];
}
function ovStepScale(scale, step) {
  const i = OV_SCALES.indexOf(scale);
  return OV_SCALES[Math.max(0, Math.min(OV_SCALES.length - 1, i + step))];
}
function ovStepChoice(choices, value, step) {
  const i = choices.findIndex(([id]) => id === value);
  return choices[(i + step + choices.length) % choices.length][0];
}
// What each global shortcut does to overlay.toml (penguin-burner-cli --overlay-*).
function ovApplyShortcut(cfg, key) {
  const k = { ",": "<", ".": ">" }[key] || key.toUpperCase();
  const next = { ...cfg };
  if (k === "X") next.enabled = !cfg.enabled;
  else if (k === "C") next.preset = ovNextPreset(cfg.preset);
  else if (k === "G") next.graph = ovStepChoice(OV_GRAPHS, cfg.graph, 1);
  else if (k === "O") next.opacity = ovNextOpacity(cfg.opacity);
  else if (k === "[") next.scale = ovStepScale(cfg.scale, -1);
  else if (k === "]") next.scale = ovStepScale(cfg.scale, 1);
  else if (k === "<") next.position = ovStepChoice(OV_POSITIONS, cfg.position, -1);
  else if (k === ">") next.position = ovStepChoice(OV_POSITIONS, cfg.position, 1);
  else return null;
  return next;
}

function ovFpsNumber(v) {
  let t = ovStr(v).toLowerCase();
  if (!t || t === "n/a") return null;
  if (t.endsWith("fps")) t = t.slice(0, -3).trim();
  const n = Number(t);
  return t && Number.isFinite(n) && n > 0 ? n : null;
}
function ovMsNumber(v) {
  let t = ovStr(v);
  if (!t || t.toLowerCase() === "n/a") return null;
  if (t.toLowerCase().endsWith(" ms")) t = t.slice(0, -3).trim();
  const n = Number(t);
  return t && Number.isFinite(n) ? Math.round(n) : null;
}
function ovFrameLatency(values) {
  const n = ovMsNumber(ovStr(values.latency_avg_ms) || values.latency_ms);
  if (n == null) return "";
  return (values.latency_source === "input-to-gpu" ? "" : "~") + n;
}
function ovCompactTier(v) {
  const t = ovStr(v),
    lower = t.toLowerCase();
  if (!t || lower === "stock" || lower === "default") return "DEF";
  return (
    { balanced: "BAL", efficiency: "EFF", performance: "PERF" }[lower] ||
    t.slice(0, 6).toUpperCase()
  );
}
function ovSigned(v) {
  let t = ovStr(v);
  if (!t || t.toLowerCase() === "n/a") return "";
  t = t.replace(/\s*mv$/i, "");
  const n = Number(t);
  if (!t || !Number.isFinite(n)) return t;
  const r = Math.round(n);
  return (r >= 0 ? "+" : "") + r;
}
function ovGib(used, total) {
  const u = Number(ovStr(used)),
    t = Number(ovStr(total));
  if (!ovStr(total) || !(t > 0) || !ovStr(used) || !Number.isFinite(u)) return "";
  return (u / 1024).toFixed(1) + " / " + Math.round(t / 1024);
}
function ovSession(values) {
  const avg = ovStr(values.fps_session_avg);
  if (!avg || avg.toLowerCase() === "n/a") return "";
  return (
    (ovStr(values.fps_session_min) || "0") +
    "/" +
    avg +
    "/" +
    (ovStr(values.fps_session_max) || "0")
  );
}
// metric_value(): one metric resolved for the settings, HUD or table view.
function ovMetric(id, values, view = "table") {
  const spec = OV_SPECS[id];
  if (!spec || id === "frametime_graph") return { value: "", unit: "", label: "" };
  let raw = ovStr(values[spec.key]);
  if (raw.toLowerCase() === "n/a") raw = "";
  let unit = spec.unit || "",
    label = (view === "hud" ? spec.hud : spec.table) || "";
  if (id === "base_fps") {
    raw = ovFpsNumber(values.present_fps) != null ? ovStr(values.present_fps) : "n/a";
    unit = view === "hud" ? "" : "FPS";
  } else if (id === "fg_fps") {
    const on = ["1", "true", "yes", "on"].includes(ovStr(values.framegen_active).toLowerCase());
    raw =
      (view === "settings" || on) && ovFpsNumber(values.framegen_fps) != null
        ? ovStr(values.framegen_fps).replace(/\s*fps$/i, "")
        : "";
    unit = view === "hud" ? "fps" : "FG";
  } else if (id === "latency_ms") {
    raw = ovFrameLatency(values);
    label = view === "hud" ? "" : ovStr(values.latency_avg_ms) ? "LAT AVG" : "LAT P95";
  } else if (id === "profile") {
    raw = view !== "table" ? raw || "Default" : ovCompactTier(raw);
  } else if (id === "uv_offset_mv") {
    raw = ovSigned(raw);
  } else if (id === "vram" || id === "ram") {
    raw = ovGib(values[id + "_used_mb"], values[id + "_total_mb"]);
    unit = "GiB";
  } else if (id === "fps_session") {
    raw = ovSession(values);
  } else if (id === "gpu_name" && view !== "settings") {
    raw = raw.replace(/^NVIDIA\s+/, "").replace(/^GeForce\s+/, "");
  } else if (id === "fps_1pct_low" && view === "hud") {
    unit = "fps";
  }
  const suffix = unit === "%" ? unit : unit ? " " + unit : "";
  if (suffix && raw.endsWith(suffix)) raw = raw.slice(0, -suffix.length);
  if (!raw && view !== "settings") {
    raw = (view === "table" ? spec.tableMissing : "") || spec.missing || "";
    if (raw && spec.noMissingUnit) unit = "";
  }
  return { value: raw, unit, label };
}
function ovMetricText(m, labeled = false) {
  if (!m.value) return "";
  const suffix = m.unit === "%" ? m.unit : m.unit ? " " + m.unit : "";
  return (labeled && m.label ? m.label + " " : "") + m.value + suffix;
}
// format_settings_metric(): the value column beside each metric row.
function ovSettingsText(id, values) {
  const m = ovMetric(id, values, "settings");
  if (!m.value) return "-";
  let { value, unit } = m;
  if (["base_fps", "fg_fps", "fps_1pct_low", "fps_session"].includes(id)) {
    unit = "FPS";
    value = value.replaceAll("/", " / ");
  }
  if (unit === "C") unit = "°C";
  if (["MHz", "mV", "W"].includes(unit) && id !== "uv_offset_mv" && /^[+-]?\d+$/.test(value))
    value = Number(value).toLocaleString("en-US");
  if (id === "uv_offset_mv" && value.startsWith("-")) value = "−" + value.slice(1);
  if (id === "profile")
    value =
      { perf: "Performance", performance: "Performance", bal: "Balanced", balanced: "Balanced", eff: "Efficiency", efficiency: "Efficiency" }[
        value.toLowerCase()
      ] || value;
  if (id === "resolution") value = value.replaceAll("x", " × ");
  if (id === "present_mode")
    value =
      { fifo: "FIFO", "fifo-relaxed": "FIFO relaxed", mailbox: "Mailbox", immediate: "Immediate" }[value] || value;
  return ovMetricText({ value, unit });
}
function ovLevel(id, values) {
  if (id === "profile") return OV_LEVEL.tier;
  if (id === "uv_offset_mv") return OV_LEVEL.good;
  if (id === "temperature_c" || id === "cpu_temp_c") {
    const [warn, alert] = id === "cpu_temp_c" ? [85, 95] : [72, 80];
    const c = ovMsNumber(values[id]);
    if (c == null) return OV_LEVEL.normal;
    return c >= alert ? OV_LEVEL.alert : c >= warn ? OV_LEVEL.warn : OV_LEVEL.good;
  }
  return OV_LEVEL.normal;
}
// format_overlay_hud_rows(): hero, labelled value rows, rules and the footer.
function ovHudRows(values, ids) {
  const enabled = new Set(ids),
    rows = [];
  for (const [kind, label, items] of OV_HUD_ROWS) {
    if (kind === "rule") {
      if (rows.length && rows.at(-1).kind !== "rule") rows.push({ kind, label: "", cells: [] });
      continue;
    }
    const cells = [];
    for (const id of items) {
      if (!enabled.has(id)) continue;
      const text = ovMetricText(ovMetric(id, values, "hud"), true);
      if (text) cells.push({ text, level: ovLevel(id, values) });
    }
    if (!cells.length) continue;
    if (kind === "hero") cells.push({ text: "fps", level: OV_LEVEL.dim });
    if (kind === "foot") {
      rows.push({
        kind,
        label: "",
        cells: [{ text: cells.map((c) => c.text).join(" | "), level: OV_LEVEL.dim }],
      });
      continue;
    }
    rows.push({ kind, label, cells });
  }
  while (rows.length && rows.at(-1).kind === "rule") rows.pop();
  return rows;
}
// format_overlay_rows(): the plain Table grid, columns padded to line up.
function ovTableLines(values, ids) {
  const enabled = new Set(ids),
    rows = OV_ROW_GROUPS.map((group) =>
      group
        .filter((id) => enabled.has(id))
        .map((id) => ovMetricText(ovMetric(id, values, "table"), true))
        .filter(Boolean),
    ).filter((cells) => cells.length),
    widths = [];
  rows.forEach((cells) => cells.forEach((c, i) => (widths[i] = Math.max(widths[i] || 0, c.length))));
  const lines = rows.map((cells) => cells.map((c, i) => c.padEnd(widths[i])).join(" ").trimEnd());
  return lines.length ? lines : ["PB waiting"];
}
const ovLive = (telemetry, key) => {
  const v = ovStr(telemetry[key]);
  return v.toLowerCase() === "n/a" ? "" : v;
};
// _sample_text(): game-local readings use examples; a missing live sensor shows '-'.
function ovSampleText(id, telemetry) {
  if (id === "frametime_graph") return "Graph";
  const spec = OV_SPECS[id];
  let live = ovLive(telemetry, spec.key);
  if (id === "latency_ms") live = ovLive(telemetry, "latency_avg_ms") || live;
  if (spec.example || (!live && ["base_fps", "fg_fps", "latency_ms"].includes(id)))
    return ovSettingsText(id, OV_SAMPLE);
  return live ? ovSettingsText(id, telemetry) : "-";
}
// _preview_values(): live values win over the demo set; a lost hotspot stays hidden.
function ovPreviewValues(telemetry) {
  const values = { ...OV_SAMPLE };
  for (const [k, v] of Object.entries(telemetry)) if (ovStr(v)) values[k] = ovStr(v);
  if (Object.keys(telemetry).length) values.hotspot_temperature_c = ovStr(telemetry.hotspot_temperature_c);
  return values;
}
// Simulated read_overlay_state() while a wrapped game publishes telemetry. The
// hotspot and DLSS 5 sensors are absent, so their rows read '-'.
function ovTelemetry(tick, tier = "Balanced") {
  const w = (amp, period, phase = 0) =>
    Math.round(amp * Math.sin(((tick + phase) * 2 * Math.PI) / period));
  return {
    present_fps: String(60 + w(3, 7)),
    framegen_fps: String(120 + w(6, 7)),
    framegen_active: "1",
    latency_ms: String(41 + w(2, 5)),
    latency_avg_ms: String(39 + w(2, 5, 1)),
    clock_mhz: String(2820 + w(15, 6)),
    voltage_mv: "925",
    power_w: String(318 + w(9, 5)),
    profile_tier: tier,
    gpu_util_pct: String(96 + w(2, 4)),
    cpu_util_pct: String(32 + w(4, 6)),
    cpu_peak_thread_pct: String(93 + w(4, 3)),
    fan_pct: "62",
    temperature_c: String(68 + w(3, 9)),
    hotspot_temperature_c: "",
    uv_offset_mv: "-75",
    mem_clock_mhz: "10501",
    vram_used_mb: String(9626 + w(90, 8)),
    vram_total_mb: "16376",
    ram_used_mb: String(14540 + w(120, 11)),
    ram_total_mb: "32768",
    cpu_temp_c: String(62 + w(3, 10)),
    cpu_power_w: String(88 + w(6, 4)),
  };
}

// ---------- Page state ----------
// `ovDisk` stands for overlay.toml. Every edit saves it at once so a running
// game updates live; a shortcut or CLI change writes it directly and the
// 1 s refresh pulls it back into the controls.
let ov = ovDefaultConfig(),
  ovDisk = structuredClone(ov),
  ovEditing = new Set(),
  ovInfoOpen = "",
  ovStatus = null, // {text, error}
  ovTimer = null,
  ovTick = 0,
  ovWindow = null;

// Overlay simulations, registered with the shared Prototype controls dialog.
if (typeof appState !== "undefined") {
  appState.overlayDesktop ??= "kde";
  Object.assign(appState.simulate, {
    overlayTelemetry: true,
    livePreviewMissing: false,
    overlaySaveFailure: false,
    ...appState.simulate,
  });
  if (typeof prototypeChoices !== "undefined")
    prototypeChoices.push([
      "overlayDesktop",
      "Desktop for overlay shortcuts",
      ["kde", "kde-fail", "gnome", "other"],
      ["KDE Plasma", "KDE, settings fail to open", "GNOME", "Other (Sway, Hyprland…)"],
    ]);
  if (typeof prototypeToggles !== "undefined")
    prototypeToggles.push(
      ["overlayTelemetry", "Wrapped game publishing overlay telemetry", () => appState.simulate.overlayTelemetry],
      ["livePreviewMissing", "Live preview tool (vkcube) missing", () => appState.simulate.livePreviewMissing],
      ["overlaySaveFailure", "Fail overlay config saves", () => appState.simulate.overlaySaveFailure],
    );
}

const ovTab = () => typeof tab !== "undefined" && tab === "In-Game Overlay";
function ovPublishing() {
  return appState.runningGame != null || !!appState.simulate.overlayTelemetry;
}
// The bare tier the runtime publishes as profile_tier: never the status band's
// label ("Balanced · silent fan curve"), and "Default" on stock settings.
function ovRunningTier() {
  const game = appState.runningGame != null ? games[appState.runningGame] : null;
  if (game && game.mode === "Adaptive" && game.wrapped !== false) {
    const live = typeof adaptiveTiers === "function" ? adaptiveTiers() : [];
    return game.liveTier || (live.includes("Balanced") ? "Balanced" : live[0]) || "Default";
  }
  const known = typeof profiles !== "undefined" ? profiles : [],
    meta = typeof profileMeta !== "undefined" ? profileMeta : [];
  let id = null;
  if (typeof runtime !== "undefined" && runtime) id = runtime.id;
  else if (appState.runningProfile)
    id = known.findIndex((p) => p[0] === appState.runningProfile);
  if (id == null || id === "stock" || id < 0 || !known[id] || meta[id]?.deleted) return "Default";
  const tier = meta[id]?.tier;
  return ["Efficiency", "Balanced", "Performance"].includes(tier) ? tier : known[id][0];
}
function ovCurrentTelemetry() {
  return ovPublishing() ? ovTelemetry(ovTick, ovRunningTier()) : {};
}
function ovCaption() {
  if (!ovPublishing()) return "Illustrative preview · sample readings";
  const game = appState.runningGame != null ? games[appState.runningGame]?.name : "";
  return "Live readings" + (game ? " · " + game : " from a wrapped game");
}

// ---------- Saving and external sync ----------
function ovSave(next, { full = true } = {}) {
  if (appState.simulate.overlaySaveFailure) {
    ovStatus = {
      text: "Overlay config save failed: [Errno 13] Permission denied: '" + OV_CONFIG_PATH + "'",
      error: true,
    };
    ovRerender();
    return false;
  }
  ov = next;
  ovDisk = structuredClone(next);
  if (ovStatus && !ovStatus.live) ovStatus = null;
  if (full) ovRerender();
  else ovPaint();
  return true;
}
function ovSet(key, value, opts) {
  return ovSave({ ...ov, [key]: value }, opts);
}
function ovSetItem(id, on) {
  const next = ovWithItem(ov, id, on);
  if (!on && next.items.includes(id)) {
    ovStatus = { text: OV_KEEP_ONE };
    ovRerender();
    return;
  }
  ovSave(next);
}
function ovToggleEdit(section) {
  if (ovEditing.has(section)) ovEditing.delete(section);
  else ovEditing.add(section);
  ovRerender();
}
function ovToggleInfo(id) {
  ovInfoOpen = ovInfoOpen === id ? "" : id;
  ovRerender();
}
// The 1 s timer: reload overlay.toml, then feed live telemetry to the
// preview, the metric values and the live preview window.
function ovEnsureTimer() {
  if (ovTimer != null || typeof setInterval !== "function") return;
  ovTimer = setInterval(ovOnTick, 1000);
  // The Pixel face samples the text on a 2 px grid, like the bitmap font.
  document.body.insertAdjacentHTML(
    "beforeend",
    '<svg width="0" height="0" style="position:absolute" aria-hidden="true"><filter id="ovPixel" x="0" y="0" width="1" height="1"><feFlood x="0" y="0" width="1" height="1"/><feComposite width="2" height="2"/><feTile result="grid"/><feComposite in="SourceGraphic" in2="grid" operator="in"/><feMorphology operator="dilate" radius="1"/></filter></svg>',
  );
}
function ovOnTick() {
  ovTick++;
  if (JSON.stringify(ovDisk) !== JSON.stringify(ov)) {
    ov = structuredClone(ovDisk);
    if (ovTab()) ovRerender();
    else ovPaintWindow();
    return;
  }
  if (ovTab()) ovPaint();
  else ovPaintWindow();
}
// Global shortcuts reach overlay.toml through penguin-burner-cli even with the
// app in the background. The prototype accepts them on the Overlay tab.
function ovShortcutKey(event) {
  if (!ovTab() || !event.altKey || event.ctrlKey || event.metaKey) return;
  if (document.querySelector("dialog[open]")) return;
  const next = ovApplyShortcut(ovDisk, event.key);
  if (!next) return;
  event.preventDefault();
  ovDisk = next;
}
if (typeof addEventListener === "function") addEventListener("keydown", ovShortcutKey);

// ---------- Rendering ----------
function ovRerender() {
  if (!ovTab()) return ovPaintWindow();
  const focused = document.activeElement?.id;
  render();
  if (focused) document.getElementById(focused)?.focus({ preventScroll: true });
}
function ovStatusHtml() {
  if (!ovStatus) return "";
  const role = ovStatus.error ? "alert" : "status";
  return '<p class="ovstatus' + (ovStatus.error ? " error" : "") + '" role="' + role + '">' + esc(ovStatus.text) + "</p>";
}
// The status line follows ovStatus on every paint, so a save that clears it
// (a slider move) also clears the message on screen.
function ovPaintStatus() {
  const slot = document.getElementById("ovStatusSlot"),
    key = ovStatus ? JSON.stringify(ovStatus) : "";
  if (!slot || slot.dataset.status === key) return;
  slot.dataset.status = key;
  slot.innerHTML = ovStatusHtml();
}
// Repaint what the 1 s refresh updates, without rebuilding the controls.
function ovPaint() {
  ovPaintStatus();
  const telemetry = ovCurrentTelemetry(),
    area = document.getElementById("ovArea");
  if (area) {
    area.innerHTML = ov.enabled ? ovHud(ovPreviewValues(telemetry), ov) : "";
    ovFit(area);
  }
  for (const id of OV_ITEM_IDS) {
    const cell = document.getElementById("ovv-" + id);
    if (cell) cell.textContent = ovSampleText(id, telemetry);
  }
  const caption = document.getElementById("ovCaption");
  if (caption) caption.textContent = ovCaption();
  ovPaintWindow();
}
function ovGraph(cfg) {
  const labels = { base: ["Base frames"], display: ["Displayed"], both: ["Base frames", "Displayed"] }[cfg.graph],
    samples = [8.3, 8.4, 8.2, 8.6, 8.3, 8.5, 9.1, 8.4, 8.3, 8.2, 8.8, 8.4, 8.3, 8.5, 8.2, 8.4, 12.6, 16.9, 11.2, 8.9, 8.4, 8.3, 8.5, 8.2, 8.4, 8.3, 8.6, 8.4, 8.2, 8.5, 8.3, 8.4, 8.7, 8.3, 8.2, 8.5, 8.4, 8.3, 9.4, 8.4, 8.3, 8.5],
    sorted = [...samples].sort((a, b) => a - b),
    span = Math.max(sorted.at(-1), 2 * sorted[samples.length >> 1]),
    pts = samples.map((v, i) => [(i * 100) / (samples.length - 1), 40 - (v / span) * 36]),
    line = pts.map(([x, y], i) => (i ? "L" : "M") + x.toFixed(2) + " " + y.toFixed(2)).join(" ");
  return labels
    .map(
      (label) =>
        '<div class="hudgraph"><span class="dim">' +
        label +
        ' (example)</span><svg viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true"><path class="area" d="' +
        line +
        ' L100 40 L0 40 Z"/><path class="line" d="' +
        line +
        '"/></svg></div>',
    )
    .join("");
}
// The HUD drawn from the same typed rows as the layer: hero frame rate, dim
// group labels, rules between groups, a footer device line and the graph.
function ovHud(values, cfg) {
  const ids = ovEnabledIds(cfg),
    graph = ids.includes("frametime_graph") ? ovGraph(cfg) : "",
    pos = cfg.position.split("_"),
    style =
      "--hud-alpha:" + cfg.opacity / 100 + ";--hud-scale:" + cfg.scale,
    cls = "hud font-" + cfg.font + " at-" + pos[0] + " at-" + pos[1];
  const cell = (c) => '<span class="' + OV_LEVEL_CLASS[c.level] + '">' + esc(c.text) + "</span>";
  let body = "";
  if (cfg.layout === "table") {
    body = graph + ovTableLines(values, ids).map((l) => '<div class="hudline">' + esc(l) + "</div>").join("");
    return '<div class="' + cls + ' table" style="' + style + '">' + body + "</div>";
  }
  const rows = ovHudRows(values, ids);
  let graphDone = !graph;
  if (!rows.some((r) => r.kind === "hero")) body += graph, (graphDone = true);
  for (const row of rows) {
    if (row.kind === "rule") body += '<div class="hudrule"></div>';
    else if (row.kind === "hero") {
      body += '<div class="hudhero"><b>' + esc(row.cells[0].text) + '</b><span class="dim">' + esc(row.cells[1].text) + "</span></div>";
      if (!graphDone) (body += graph), (graphDone = true);
    } else if (row.kind === "foot") body += '<div class="hudfoot dim">' + esc(row.cells[0].text) + "</div>";
    else body += '<span class="dim hudlab">' + esc(row.label) + '</span><span class="hudcells">' + row.cells.map(cell).join("") + "</span>";
  }
  return '<div class="' + cls + '" style="' + style + '">' + body + "</div>";
}
// ui/components/overlay_preview.py sizing: the layer's 1x cap height at 1440p
// (14 px x the 0.75 scale reference), magnified 3x on a 2560 px wide stand-in
// frame. Both renderers size the HUD from these, so Scale reads the same.
const OV_CANVAS_W = 2560,
  OV_BASE_CAP_PX = 14 * 0.75,
  OV_PREVIEW_MAGNIFY = 3,
  ovCapRatios = {};
// Cap height per em of the HUD face, measured like the painter's _face().
function ovCapRatio(hud) {
  const cs = getComputedStyle(hud),
    font = cs.fontWeight + " 100px " + cs.fontFamily;
  if (!(font in ovCapRatios)) {
    const ctx = document.createElement("canvas").getContext?.("2d");
    let ratio = 0;
    if (ctx) {
      ctx.font = font;
      ratio = ctx.measureText("H").actualBoundingBoxAscent / 100;
    }
    ovCapRatios[font] = ratio > 0.4 && ratio < 1 ? ratio : 0.71;
  }
  return ovCapRatios[font];
}
// Shrink only when the panel would leave the frame, like _fitted_magnification.
function ovFit(area) {
  const hud = area.querySelector(".hud");
  if (!hud) return;
  const scale = Number(hud.style.getPropertyValue("--hud-scale") || 1),
    cap = (area.clientWidth / OV_CANVAS_W) * OV_BASE_CAP_PX * scale * OV_PREVIEW_MAGNIFY,
    base = cap / ovCapRatio(hud);
  hud.style.fontSize = base + "px";
  const fit = Math.min(1, (0.92 * area.clientWidth) / hud.offsetWidth, (0.92 * area.clientHeight) / hud.offsetHeight);
  if (fit < 1) hud.style.fontSize = base * fit + "px";
  // A 2 px sampling grid reads as a bitmap face only at a legible size.
  hud.classList.toggle("pixelated", base * Math.min(1, fit) >= 12);
}
function ovRow(id, title, tooltip, control) {
  return (
    '<div class="row"><label for="' + id + '" title="' + esc(tooltip) + '">' + title + "</label>" + control + "</div>"
  );
}
function ovSelect(id, key, choices, tooltip) {
  return (
    '<select id="' + id + '" title="' + esc(tooltip) + '" onchange="ovSet(\'' + key + "',this.value)\">" +
    choices.map(([v, l]) => '<option value="' + v + '"' + (ov[key] === v ? " selected" : "") + ">" + l + "</option>").join("") +
    "</select>"
  );
}
function ovSlider(id, min, max, step, value, text, oninput, tooltip, extra = "") {
  return (
    '<span class="ovslider" title="' + esc(tooltip) + '"><input type="range" id="' + id + '" min="' + min + '" max="' + max + '" step="' + step + '" value="' + value +
    '" aria-valuetext="' + text + '" title="' + text + '" oninput="' + oninput + '"' + extra + '><output for="' + id + '" id="' + id + 'Value">' + text + "</output></span>"
  );
}
function ovSlideScale(input) {
  ovSet("scale", OV_SCALES[Number(input.value)], { full: false });
  ovSliderText(input, Math.round(ov.scale * 100) + "%");
}
function ovSlideOpacity(input) {
  ovSet("opacity", Number(input.value), { full: false });
  ovSliderText(input, ov.opacity + "%");
}
function ovSliderText(input, text) {
  input.setAttribute("aria-valuetext", text);
  input.title = text;
  document.getElementById(input.id + "Value").textContent = text;
  if (typeof syncRangeFill === "function") syncRangeFill(input);
}
// QSlider page step: PageUp/PageDown move the opacity 10%.
function ovOpacityKey(event) {
  const dir = { PageUp: 1, PageDown: -1 }[event.key];
  if (!dir) return;
  event.preventDefault();
  event.target.value = Math.max(OV_OPACITY.min, Math.min(OV_OPACITY.max, ov.opacity + dir * OV_OPACITY.page));
  ovSlideOpacity(event.target);
}
function ovSetInterval(input) {
  const n = Math.round(Number(input.value));
  const clamped = Math.max(OV_INTERVAL.min, Math.min(OV_INTERVAL.max, Number.isFinite(n) ? n : 1));
  input.value = clamped;
  ovSet("interval", clamped);
}
function ovMetricRow(id, editing, telemetry) {
  const label = sample.metrics[id],
    info = OV_SPECS[id].info,
    value = '<span class="ovvalue" id="ovv-' + id + '" title="' + esc(info) + '">' + esc(ovSampleText(id, telemetry)) + "</span>";
  if (!editing)
    return '<div class="row ovmetric"><span class="ovlabel" title="' + esc(info) + '">' + label + "</span>" + value + "</div>";
  const on = ovEnabledIds(ov).includes(id),
    open = ovInfoOpen === id;
  return (
    '<div class="row ovmetric editing"><input type="checkbox" id="ovc-' + id + '"' + (on ? " checked" : "") +
    " onchange=\"ovSetItem('" + id + "',this.checked)\"><label for=\"ovc-" + id + '" title="' + esc(info) + '">' + label + "</label>" + value +
    '<button class="ovinfo" id="ovi-' + id + '" aria-label="About ' + esc(label) + '" aria-expanded="' + open + '" aria-controls="ovd-' + id +
    "\" onclick=\"ovToggleInfo('" + id + "')\">i</button></div>" +
    (open ? '<p class="ovinfotext" id="ovd-' + id + '">' + esc(info) + "</p>" : "")
  );
}
function ovSection([title, items], telemetry) {
  const editing = ovEditing.has(title),
    keys = ovEnabledIds(ov),
    selected = items.filter((k) => keys.includes(k)),
    slug = title.toLowerCase().replace(/\W+/g, "-"),
    shown = editing ? items : selected;
  return (
    '<section class="card ovgroup" aria-labelledby="ovh-' + slug + '"><div class="cardhead ovhead"><h2 id="ovh-' + slug + '">' + title +
    '</h2><small class="ovcount">' + selected.length + ' selected</small><button class="ovedit" id="ove-' + slug + '" aria-label="' +
    (editing ? "Finish editing " : "Edit ") + title.toLowerCase() + " metrics\" onclick=\"ovToggleEdit('" + title + "')\">" +
    (editing ? "Done" : "Edit") + "</button></div>" +
    (shown.map((k) => ovMetricRow(k, editing, telemetry)).join("") || '<div class="row ovempty">No metrics selected.</div>') +
    "</section>"
  );
}
function overlay() {
  ovEnsureTimer();
  const telemetry = ovCurrentTelemetry(),
    sections = sample.sections,
    // The frame shows the running game's art, or the library selection.
    shown = appState.runningGame != null ? games[appState.runningGame] || g() : g(),
    art = assets[shown.id + "art"];
  const status =
    '<div id="ovStatusSlot" data-status="' + esc(ovStatus ? JSON.stringify(ovStatus) : "") + '">' + ovStatusHtml() + "</div>";
  const preview =
    '<section class="card ovpreview"><div class="cardhead"><h2>Preview</h2></div><div class="hudarea" id="ovArea" role="img" aria-label="' +
    esc(shown.name) + ' overlay preview"' + (art ? ' style="--art:url(' + art + ')"' : "") + "></div>" +
    '<div class="ovpreviewfoot"><small id="ovCaption">' + ovCaption() + '</small><button id="ovLive" title="Open a Vulkan window with the real overlay, following these settings live." onclick="ovToggleLive()">' +
    (ovWindow ? "Stop preview" : "Live preview") + "</button></div></section>";
  const appearance =
    '<section class="card ovgroup ovsettings"><div class="cardhead"><h2>Appearance</h2></div>' +
    ovRow("ovPosition", "Position", "Position of the overlay within the game's image.", ovSelect("ovPosition", "position", OV_POSITIONS, "Position of the overlay within the game's image.")) +
    ovRow("ovScale", "Scale", "Overlay scale, on top of automatic sizing for the game's resolution.",
      ovSlider("ovScale", 0, OV_SCALES.length - 1, 1, OV_SCALES.indexOf(ov.scale), Math.round(ov.scale * 100) + "%", "ovSlideScale(this)", "Overlay scale, on top of automatic sizing for the game's resolution.")) +
    ovRow("ovOpacity", "Background opacity", "Opacity of the background panel; text stays fully opaque.",
      ovSlider("ovOpacity", OV_OPACITY.min, OV_OPACITY.max, OV_OPACITY.step, ov.opacity, ov.opacity + "%", "ovSlideOpacity(this)", "Opacity of the background panel; text stays fully opaque.", ' onkeydown="ovOpacityKey(event)"')) +
    ovRow("ovLayout", "Layout", "HUD groups the readout around the frame rate. Table uses a plain grid.", ovSelect("ovLayout", "layout", OV_LAYOUTS, "HUD groups the readout around the frame rate. Table uses a plain grid.")) +
    ovRow("ovGraph", "Frame timing graphs", OV_GRAPH_TIP, ovSelect("ovGraph", "graph", OV_GRAPHS, OV_GRAPH_TIP)) +
    ovRow("ovFont", "Font", "Typeface used by the in-game overlay.", ovSelect("ovFont", "font", OV_FONTS, "Typeface used by the in-game overlay.")) +
    "</section>";
  const behaviour =
    '<section class="card ovgroup ovsettings"><div class="cardhead"><h2>Behaviour</h2></div>' +
    ovRow("ovPreset", "Preset", OV_PRESET_TIP, ovSelect("ovPreset", "preset", OV_PRESETS, OV_PRESET_TIP)) +
    ovRow("ovInterval", "Refresh interval", "How often the runtime refreshes overlay telemetry.",
      '<span class="ovspin" title="How often the runtime refreshes overlay telemetry."><input type="number" id="ovInterval" min="' + OV_INTERVAL.min + '" max="' + OV_INTERVAL.max +
      '" step="1" value="' + ov.interval + '" onchange="ovSetInterval(this)"><span aria-hidden="true">s</span></span>') +
    '<p class="ovnote">Changes save to overlay.toml right away, and running games update live.</p></section>';
  const page =
    '<div class="titlebar"><div><h1>In-Game Overlay</h1><p>Choose how your performance overlay looks.</p></div><div class="tools">' +
    '<button id="ovShortcuts" onclick="ovShortcuts()">Keyboard shortcuts…</button><span class="ovstate" aria-hidden="true">' + (ov.enabled ? "On" : "Off") +
    '</span><button class="track" role="switch" id="ovEnabled" aria-label="In-game overlay" title="Show the overlay in wrapped games." aria-checked="' + ov.enabled +
    "\" onclick=\"ovSet('enabled',!ov.enabled)\"><i></i></button></div></div>" + status +
    '<div class="ovgrid"><div class="ovcol ovcol-a">' + preview + appearance + behaviour + '</div><div class="ovcol ovcol-b">' +
    ovSection(sections[0], telemetry) + ovSection(sections[2], telemetry) + '</div><div class="ovcol ovcol-c">' +
    ovSection(sections[1], telemetry) + ovSection(sections[3], telemetry) + "</div></div>";
  requestAnimationFrame(ovPaint);
  return page;
}
const OV_GRAPH_TIP =
  "Base frames uses real pre-FG markers. Displayed frames uses driver presentation timestamps, including generated frames when exposed by the driver. Both shows two unsmoothed plots. Unavailable feedback is labeled, never replaced by Present-call timing. Restart an existing game after updating the layer.";
const OV_PRESET_TIP =
  "Small, Medium, Large or Biggest. The preset shortcut cycles these four. Scale is independent. Custom restores your saved field selection; editing a metric selection updates it.";

// ---------- Live preview: a separate vkcube window running the real layer ----------
function ovToggleLive() {
  if (ovWindow) return ovCloseWindow();
  if (appState.simulate.livePreviewMissing) {
    ovStatus = { text: OV_MISSING_EXECUTABLE };
    return ovRerender();
  }
  const win = document.createElement("section");
  win.className = "ovwindow";
  win.setAttribute("aria-label", "Live preview window");
  win.innerHTML =
    '<header><span>vkcube · PenguinBurner overlay</span><button aria-label="Close live preview window" title="Close">×</button></header>' +
    '<div class="ovwinbody"><div class="ovcube" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i></div><div class="ovwinhud"></div></div>';
  win.querySelector("button").onclick = ovCloseWindow;
  win.addEventListener("keydown", (e) => e.key === "Escape" && ovCloseWindow());
  ovDragWindow(win);
  document.body.append(win);
  ovWindow = win;
  ovStatus = { text: "Live preview running. It follows these settings as you change them.", live: true };
  ovRerender();
}
// Closing from the window's own title bar resets the button too.
function ovCloseWindow() {
  ovWindow?.remove();
  ovWindow = null;
  if (ovStatus?.live) ovStatus = null;
  ovRerender();
}
function ovPaintWindow() {
  const host = ovWindow?.querySelector(".ovwinhud");
  if (!host) return;
  host.innerHTML = ov.enabled ? ovHud(ovPreviewValues(ovCurrentTelemetry()), ov) : "";
  ovFit(host);
}
function ovDragWindow(win) {
  const bar = win.querySelector("header");
  bar.addEventListener("pointerdown", (e) => {
    if (e.target.closest("button")) return;
    const box = win.getBoundingClientRect(),
      dx = e.clientX - box.left,
      dy = e.clientY - box.top;
    bar.setPointerCapture(e.pointerId);
    const move = (m) => {
      win.style.left = Math.max(0, Math.min(innerWidth - 80, m.clientX - dx)) + "px";
      win.style.top = Math.max(0, Math.min(innerHeight - 40, m.clientY - dy)) + "px";
      win.style.right = win.style.bottom = "auto";
    };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", () => bar.removeEventListener("pointermove", move), { once: true });
  });
}

// ---------- Keyboard shortcuts ----------
const OV_SHORTCUT_INTRO =
  "Alt+X shows or hides the overlay. Alt+C cycles Small → Medium → Large → Biggest → Small. Alt+[ / Alt+] changes Scale; Alt+< / Alt+> moves the overlay. You can change any binding in your desktop settings. This works on Wayland and X11, even with PenguinBurner closed.";
const OV_SHORTCUT_NOTE =
  "The game must be launched with PenguinBurner's overlay layer. Changes appear live. Scale stops at 50% and 200%; positions wrap. Custom fields remain available in the Preset selector. Opening this dialog does not assign or replace any shortcuts.";
const OV_SHORTCUT_HELP = {
  kde: "PenguinBurner's overlay actions are provided in KDE shortcut settings. Choose a key beside an action to change it. Default keys apply unless you have assigned your own.",
  gnome:
    "Set up all overlay shortcuts automatically. Existing shortcuts and your customized bindings are preserved. If a default key is already used, its new action is left unassigned for you to choose a free key.",
  other:
    "Add a global shortcut in your desktop settings and paste a command below. On Sway use bindsym with exec; on Hyprland use bind with exec.",
};
// On KDE the button repairs the .desktop entry and opens KDE shortcut
// settings directly; the dialog appears only when that fails.
function ovShortcuts() {
  const desktop = appState.overlayDesktop;
  if (desktop === "kde") {
    ovStatus = { text: "Opened KDE shortcut settings. PenguinBurner's overlay actions are listed there." };
    return ovRerender();
  }
  ovShortcutDialog(
    desktop === "kde-fail"
      ? "Could not open KDE shortcut settings. Try again below, or open System Settings → Keyboard → Shortcuts and select PenguinBurner."
      : "",
  );
}
function ovShortcutDialog(error) {
  const desktop = appState.overlayDesktop === "kde-fail" ? "kde" : appState.overlayDesktop,
    commands = desktop === "other",
    gnome = desktop === "gnome";
  const list = OV_SHORTCUTS.map(
    ([label, flag, key], i) =>
      '<div class="ovkey"><span>' + label + "</span><kbd>Alt+" + esc(key) + "</kbd>" +
      (commands
        ? '<div class="ovcmd"><input readonly id="ovCmd' + i + '" aria-label="Command for ' + label + '" value="penguin-burner-cli ' + flag +
          '"><button onclick="ovCopyCommand(' + i + ')">Copy command</button></div>'
        : "") +
      "</div>",
  ).join("");
  showPanel(
    '<h2 id="ovKeysTitle">Overlay keyboard shortcuts</h2>' +
      (error ? '<p class="ovstatus error" role="alert">' + error + "</p>" : "") +
      '<p class="sub">' + (gnome ? "Global overlay shortcuts work on Wayland and X11, even with PenguinBurner closed." : OV_SHORTCUT_INTRO) + "</p>" +
      '<p class="sub">' + OV_SHORTCUT_HELP[desktop] + '</p><div class="ovkeys">' + list + "</div>" +
      '<p class="sub" id="ovKeysNote" role="status">' + (gnome ? "Setup adds missing actions and opens GNOME Settings." : OV_SHORTCUT_NOTE) + "</p>" +
      '<div class="dialogfoot">' +
      (gnome ? '<button id="ovGnomeSetup" onclick="ovGnomeSetup(this)">Set up GNOME shortcuts</button>' : "") +
      (desktop !== "other" ? '<button onclick="ovOpenShortcutSettings()">Open shortcut settings</button>' : "") +
      '<button autofocus onclick="closePanel()">Close</button></div>',
  );
  dlg().setAttribute("aria-labelledby", "ovKeysTitle");
  dlg().addEventListener("close", () => dlg().removeAttribute("aria-labelledby"), { once: true });
}
function ovOpenShortcutSettings() {
  document.getElementById("ovKeysNote").textContent =
    appState.overlayDesktop === "gnome" ? "Opened GNOME Settings → Keyboard → Custom Shortcuts." : "Opened KDE shortcut settings.";
}
function ovGnomeSetup(button) {
  const note = document.getElementById("ovKeysNote");
  button.disabled = true;
  button.textContent = "Setting up…";
  note.textContent = "Registering overlay shortcuts in GNOME…";
  setTimeout(() => {
    if (!button.isConnected) return;
    button.disabled = false;
    button.textContent = "Set up GNOME shortcuts";
    note.textContent =
      "Overlay shortcuts are ready in GNOME Settings. Existing key assignments were preserved. Choose an available key for: Cycle background opacity. Its default key is already in use.";
  }, 900);
}
async function ovCopyCommand(i) {
  const field = document.getElementById("ovCmd" + i),
    button = field.nextElementSibling;
  try {
    await navigator.clipboard.writeText(field.value);
    button.textContent = "Copied";
  } catch {
    field.select();
    button.textContent = "Press Ctrl+C";
  }
  setTimeout(() => button.isConnected && (button.textContent = "Copy command"), 1500);
}
