const ov = {
  enabled: true,
  position: "Top right",
  layout: "HUD",
  graph: "Base frames (pre-FG)",
  font: "Sans",
  scale: "1",
  opacity: "80",
  preset: "Custom",
  interval: "1",
  live: false,
};
let customItems = [...sample.defaults],
  editSection = "";
const ovValues = {
  base_fps: "87 FPS",
  fg_fps: "174 FPS",
  latency_ms: "19 ms",
  clock_mhz: "2640 MHz",
  voltage_mv: "850 mV",
  power_w: "224 W",
  profile: "Balanced",
  gpu_util_pct: "96%",
  cpu_peak_thread_pct: "72%",
  frametime_graph: "▂▃▂▅▂▂▃▄▂▅",
  gpu_name: "GeForce RTX 5080",
  fps_1pct_low: "61 FPS",
  temperature_c: "64 °C",
  resolution: "2560 × 1440",
};
const presetKeys = {
  Small: "minimal",
  Medium: "standard",
  Large: "detailed",
  Biggest: "full",
};
function visibleItems() {
  return ov.preset === "Custom"
    ? customItems
    : sample.presets[presetKeys[ov.preset]];
}
function selectControl(k, label, items) {
  return (
    '<select aria-label="' +
    label +
    '" onchange="ov[\'' +
    k +
    "']=this.value;render()\">" +
    items
      .map(
        (v) =>
          "<option " + (ov[k] == v ? "selected" : "") + ">" + v + "</option>",
      )
      .join("") +
    "</select>"
  );
}
function changeMetric(k, on) {
  customItems = [...new Set(visibleItems())].filter((m) => m !== k);
  if (on) customItems.push(k);
  ov.preset = "Custom";
  render();
}
function overlay() {
  const ctrl = (k, l, values) => row(l, "", selectControl(k, l, values)),
    keys = visibleItems();
  const sections = sample.sections
    .map(
      ([s, items]) =>
        '<section class="card ovgroup">' +
        head(
          s,
          items.filter((k) => keys.includes(k)).length + " selected",
          "<button onclick=\"editSection=editSection==='" +
            s +
            "'?'':'" +
            s +
            "';render()\">" +
            (editSection === s ? "Done" : "Edit") +
            "</button>",
        ) +
        (items
          .filter((k) => editSection === s || keys.includes(k))
          .map((k) =>
            row(
              sample.metrics[k],
              "",
              editSection === s
                ? '<input type="checkbox" aria-label="' +
                    sample.metrics[k] +
                    '" ' +
                    (keys.includes(k) ? "checked" : "") +
                    " onchange=\"changeMetric('" +
                    k +
                    "',this.checked)\">"
                : "<span>" + (ovValues[k] || "—") + "</span>",
            ),
          )
          .join("") || '<div class="row muted">No metrics selected.</div>') +
        "</section>",
    )
    .join("");
  const pos =
    (ov.position.startsWith("Top") ? "top:8px;" : "bottom:8px;") +
    (ov.position.endsWith("right")
      ? "right:8px"
      : ov.position.endsWith("left")
        ? "left:8px"
        : "left:38%");
  const hud =
    '<div class="hud" style="' +
    pos +
    ";background:rgba(10,15,12," +
    ov.opacity / 100 +
    ");font-family:" +
    (ov.font === "Sans" ? "Inter,sans-serif" : "monospace") +
    ";font-size:" +
    Math.round(12 * Number(ov.scale)) +
    'px">' +
    keys
      .map(
        (k) =>
          "<span><small>" +
          (ov.layout === "Table"
            ? sample.metrics[k]
            : k === "base_fps"
              ? "FPS"
              : sample.metrics[k]) +
          "</small> <b>" +
          (ovValues[k] || "—") +
          "</b></span>",
      )
      .join("") +
    "</div>";
  return (
    '<div class="titlebar"><div><h1>In-Game Overlay</h1><p>Choose how your performance overlay looks.</p></div><div class="tools"><button onclick="shortcutDialog()">Keyboard shortcuts…</button><button class="track" role="switch" aria-label="Enable overlay" aria-checked="' +
    ov.enabled +
    '" onclick="ov.enabled=!ov.enabled;render()"><i></i></button></div></div><div class="section2"><div><section class="card">' +
    head(
      "Preview",
      ov.live
        ? "Expanded sample preview"
        : "Illustrative preview · sample readings",
    ) +
    '<div class="hudarea" style="height:' +
    (ov.live ? 290 : 180) +
    "px;" +
    (assets[g().id + "art"]
      ? "background-image:linear-gradient(#13201966,#131916bb),url(" +
        assets[g().id + "art"] +
        ")"
      : "") +
    '">' +
    (ov.enabled ? hud : "") +
    '</div><div class="row"><small>Preview uses the settings below.</small><button onclick="ov.live=!ov.live;render()">' +
    (ov.live ? "Close preview" : "Preview overlay") +
    '</button></div></section><section class="card ovgroup">' +
    head("Appearance", "") +
    ctrl("position", "Position", [
      "Top left",
      "Top center",
      "Top right",
      "Bottom left",
      "Bottom right",
    ]) +
    ctrl("scale", "Scale", ["0.5", "0.75", "1", "1.5", "2"]) +
    row(
      "Background opacity",
      "",
      '<label><input type="range" aria-label="Background opacity" min="0" max="100" step="5" value="' +
        ov.opacity +
        '" onchange="ov.opacity=this.value;render()"> ' +
        ov.opacity +
        "%</label>",
    ) +
    ctrl("layout", "Layout", ["HUD", "Table"]) +
    ctrl("graph", "Frame timing graphs", [
      "Base frames (pre-FG)",
      "Displayed frames (post-FG)",
      "Both graphs",
    ]) +
    ctrl("font", "Font", ["Pixel (bitmap)", "Mono", "Sans"]) +
    '</section><section class="card ovgroup">' +
    head("Behaviour", "") +
    ctrl("preset", "Preset", [
      "Small",
      "Medium",
      "Large",
      "Biggest",
      "Custom",
    ]) +
    ctrl(
      "interval",
      "Refresh interval (s)",
      Array.from({ length: 10 }, (_, i) => i + 1),
    ) +
    "</section></div><div>" +
    sections +
    "</div></div>"
  );
}
function shortcutDialog() {
  showPanel(
    '<h2>Keyboard shortcuts</h2><p class="sub">Default desktop bindings</p>' +
      [
        ["Toggle overlay", "Alt+X"],
        ["Cycle preset", "Alt+C"],
        ["Cycle frame timing graphs", "Alt+G"],
        ["Cycle opacity", "Alt+O"],
        ["Decrease / increase scale", "Alt+[ / Alt+]"],
        ["Previous / next position", "Alt+&lt; / Alt+&gt;"],
      ]
        .map(([l, k]) => row(l, "", "<kbd>" + k + "</kbd>"))
        .join("") +
      dismiss,
  );
}
