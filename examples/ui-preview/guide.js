const findings = [
  [
    "Game Library",
    "game-library",
    "Keep the library layout",
    "Game artwork and the title make the selection clear. Keep Play prominent, along with the existing launcher, sort and bulk actions. The prototype adds search to exercise returning to a filtered list.",
    "Move history under the game",
    "The current Perf Profile badge opens a permanent tab. Put the Perf Profile button beside Play, with a hover peek of median FPS, 1% low and power. Preserve the selected game and keyboard focus on return.",
  ],
  [
    "Profiles",
    "profiles-management",
    "Make profile comparison the default",
    "Date leads a 12-column grid. Repeated GPU and source values consume the space needed for profile names and comparable measurements.",
    "Put application scope beside the choice",
    "Keep sortable FPS, watts and FPS/W visible. Put clocks and provenance in the selection details. Show current-session and startup actions separately. Retain multi-select deletion and advanced columns in the production view.",
  ],
  [
    "Auto UV",
    "auto-uv-scan",
    "Keep the scan plot and all 13 columns",
    "Give the base, candidate, previous runs and confirmed tier curves the full plot width. Keep row selection connected to its curve and retain measured values and stock deltas.",
    "Start empty and keep Copy logs visible",
    "Setup Auto Undervolt opens GPU, scope, preset and advanced settings. Start begins the scan. Start empty; the log pane beside the plot follows the scan and Copy logs copies every line.",
  ],
  [
    "In-Game Overlay",
    "overlay",
    "Keep the preview beside its controls",
    "Grouped settings and a labeled preview make the effect of each choice visible. The separate live preview action distinguishes the example from a running overlay.",
    "Reuse its measurement layout",
    "Carry the aligned labels, units and clear group headings over to Profiles and Auto UV. Use the same title, section, row-label and caption sizes as Game Library.",
  ],
];
function shot(key) {
  document.getElementById("shotImg").src = assets[key];
  document.getElementById("zoom").showModal();
}
function audit() {
  return (
    title(
      "UX audit",
      "Based on the installed Qt workflow and repository screenshots. Click a screenshot to inspect the existing design.",
    ) +
    '<div class="audit">' +
    findings
      .map(
        ([t, img, a, b, c, d]) =>
          '<section class="card"><button class="shot" aria-label="Inspect existing ' +
          t +
          ' screen" onclick="shot(\'' +
          img +
          '\')"><img alt="Existing ' +
          t +
          ' screen" src="' +
          assets[img] +
          '"></button><article><small>' +
          t +
          "</small><h2>" +
          a +
          "</h2><p>" +
          b +
          "</p><h3>" +
          c +
          "</h3><p>" +
          d +
          "</p><button onclick=\"navigate('" +
          t +
          "')\">Open " +
          t +
          " →</button></article></section>",
      )
      .join("") +
    "</div>"
  );
}
function designSystem() {
  const c = design.colors,
    t = design.typography;
  const sw = (name, key) =>
    '<span><i class="chip" style="background:' +
    c[key] +
    '"></i>' +
    name +
    "</span>";
  return (
    title(
      "PBurn design system",
      "Shared roles for the desktop interface and its HTML prototype.",
    ) +
    '<div class="sys">' +
    '<section class="card"><h2>Surfaces and emphasis</h2><p>Charcoal sets the background. One raised surface groups related controls. Green marks the primary action, selection and keyboard focus.</p><div class="swatches">' +
    [
      ["Window", "window"],
      ["Panel", "panel"],
      ["Raised row", "raised"],
      ["Action", "primary"],
      ["Focus", "focus"],
    ]
      .map((v) => sw(...v))
      .join("") +
    "</div></section>" +
    '<section class="card"><h2>Type and spacing</h2><div class="guideRow"><h1>Page title</h1><small>' +
    t.title +
    ' px</small></div><div class="guideRow"><h2>Section title</h2><small>' +
    t.section +
    ' px</small></div><div class="guideRow"><b>Setting or action</b><small>' +
    t.body +
    ' px</small></div><div class="guideRow"><small>Units and supporting text</small><small>' +
    t.caption +
    " px</small></div><p>Spacing steps: 4, 8, 12, 16 and 24 px. Align numeric values on the right and use tabular digits.</p></section>" +
    '<section class="card"><h2>Controls</h2><p>A switch changes a preference. An action button starts a task. Show the selected GPU or game beside any action whose scope depends on it.</p>' +
    row(
      "Capture telemetry",
      "Example preference",
      '<button role="switch" aria-label="Example capture telemetry" class="track" aria-checked="true" onclick="this.setAttribute(\'aria-checked\',this.getAttribute(\'aria-checked\')!==\'true\')"><i></i></button>',
    ) +
    '<div class="editorControls"><button class="primary" onclick="navigate(\'Auto UV\');ensureDaemonReady(openSetup)">Setup Auto Undervolt</button><button onclick="navigate(\'Profiles\')">View profiles</button></div></section>' +
    '<section class="card"><h2>Measurements and curves</h2><p>Use green/red on the signed difference from stock. Lower power is favorable. Measured values and signed percentages each have a fixed alignment. Absolute measurements stay neutral; a change under 1% is gray.</p><div class="guideRow"><b>Board power</b><span>267 W <span style="color:' +
    c.gain +
    '">−21.7%</span></span></div><div class="guideRow"><b>Frame rate</b><span>62.6 FPS <span style="color:' +
    c.loss +
    '">−1.3%</span></span></div><p>The legend sits outside the graph. The same tier keeps its color on the scan, fingerprint and verification screens.</p><div class="swatches">' +
    [
      ["Base", "baseCurve"],
      ["Candidate", "candidateCurve"],
      ["Efficiency", "efficiency"],
      ["Balanced", "balanced"],
      ["Performance", "performance"],
    ]
      .map((v) => sw(...v))
      .join("") +
    "</div></section>" +
    '<section class="card"><h2>Game Library and Overlay</h2><p>Keep artwork in the game list, the game title and Play action together, and switches aligned at the right of grouped rows. Keep metric editing alongside the Overlay preview. History opens and returns inside the same library selection.</p><button onclick="navigate(\'Game Library\')">Open Game Library</button></section>' +
    '<section class="card"><h2>Auto UV and Profiles</h2><p>Give the V/F plot the available width. Keep the runs table dense enough to compare candidates, with written-out headings and units below. In Profiles, separate selecting a candidate, verifying an edit, applying it and saving it for startup.</p><button onclick="navigate(\'Auto UV\')">Open Auto UV</button></section></div>'
  );
}
// Startup checks run once the window has painted: a missing NVIDIA driver
// gets a plain warning, then an incompatible running service is offered an
// update. A missing service is left alone until a privileged action needs it.
function runStartupChecks({ nvidiaGpu = true } = {}) {
  const daemonCheck = () => {
    if (appState.daemon === "outdated") ensureDaemonReady(() => {}, "This version of PenguinBurner");
  };
  if (nvidiaGpu) return daemonCheck();
  showPanel(
    '<div class="errhead"><span class="erricon warn" aria-hidden="true">!</span><div><h2 id="startupTitle">No NVIDIA GPU detected</h2>' +
      '<p class="sub">PenguinBurner tunes NVIDIA GPUs and could not find one on this system. No NVIDIA kernel driver is loaded.</p>' +
      '<p class="sub">Install a supported NVIDIA card and the proprietary driver, then restart PenguinBurner. Undervolt scans and profile applies will not work until then.</p></div></div>' +
      '<div class="dialogfoot"><button id="startupOk">OK</button></div>',
  );
  const ok = document.getElementById("startupOk");
  ok.onclick = () => {
    closePanel();
    daemonCheck();
  };
  ok.focus();
}
// Global error reporting: the first line goes to the status band and the
// log, and the dialog carries copyable details (exit code, status, log tail).
function showErrorDetails(title, message, details = "") {
  const first = String(message).split("\n")[0],
    copy = [title, "", message, ...(details ? ["", details] : [])].join("\n"),
    owned = { running: title, detail: first, tone: "error" };
  logLines.push("[" + new Date().toLocaleTimeString("en-GB") + "] " + title + ": " + message);
  if (!appState.status) setGlobalStatus(owned);
  showPanel(
    '<div class="errhead"><span class="erricon" aria-hidden="true">!</span><div><h2 id="errTitle">' + esc(title) + '</h2><p class="sub">' + esc(message) +
      '</p></div></div><textarea class="errdetails" id="errDetails" readonly aria-label="Error details" wrap="off"></textarea>' +
      '<div class="dialogfoot"><span class="copymsg" id="errCopied" role="status"></span><button id="errCopy">Copy error</button><button id="errOk">OK</button></div>',
  );
  const field = document.getElementById("errDetails");
  field.value = copy;
  document.getElementById("errOk").onclick = closePanel;
  document.getElementById("errCopy").onclick = async () => {
    try {
      await navigator.clipboard.writeText(copy);
      document.getElementById("errCopied").textContent = "Copied";
    } catch {
      field.focus();
      field.select();
    }
  };
  dlg().addEventListener(
    "close",
    () => appState.status === owned && setGlobalStatus(null),
    { once: true },
  );
  document.getElementById("errOk").focus();
}
function sampleProcessError() {
  const tail = Array.from(
    { length: 80 },
    (_, i) =>
      "[21:14:" + String(i % 60).padStart(2, "0") + "] " +
      (i < 79 ? "Run 14 · Balanced · 850 mV @ 2640 MHz · Q2RTX frame " + (4120 + i * 30) : "Q2RTX exited: VK_ERROR_DEVICE_LOST"),
  ).join("\n");
  return [
    "Auto UV stopped",
    "The Auto UV scan stopped unexpectedly. The full error details can be copied for troubleshooting.",
    "Action: Auto UV scan\nExit code: 1\nExit status: CrashExit\n\nRecent logs:\n" + tail,
  ];
}
function shellTools() {
  const busy = stage === "running" || stage === "verifying";
  document.getElementById("shellTools").innerHTML = `
    <button class="start" ${busy ? "disabled" : ""} onclick="ensureDaemonReady(openSetup)">Setup Auto Undervolt</button>
    <button ${busy ? "" : "disabled"} onclick="stopScan()">Stop</button>
    <button ${busy ? "disabled" : ""} onclick="openImport()">Import Afterburner</button>`;
}
if (["#scan-results", "#verification"].includes(location.hash)) {
  tab = "Auto UV";
  stage = "complete";
  plan = [stock, ...sample.rows];
  completed = plan.length;
  if (location.hash === "#verification") {
    stage = "choice";
  } else {
    for (const t of tiers) {
      const options = tierCandidates(t);
      const row = plan[suggestedCandidate(options)];
      saveVerifiedCandidate(
        t,
        row,
        settings.settings[t].memory,
        defaultVerificationMinutes(),
      );
    }
  }
}
if (location.hash === "#profiles") {
  useExamples = true;
  tab = "Profiles";
}
if (location.hash === "#overlay") tab = "In-Game Overlay";
if (location.hash === "#overlay-live") tab = "In-Game Overlay";
if (location.hash === "#setup") tab = "Auto UV";
addEventListener("resize", draw);
render();
if (location.hash === "#setup") openSetup();
if (location.hash === "#verification") verifySetup();
if (location.hash === "#about") aboutView();
if (location.hash === "#overlay-live") ovToggleLive();
if (location.hash === "#startup-checks") {
  appState.daemon = "outdated";
  render();
  runStartupChecks({ nvidiaGpu: false });
}
if (location.hash === "#error-details") showErrorDetails(...sampleProcessError());
