const tiers = ["Efficiency", "Balanced", "Performance"];
const tierColors = tiers.map((t) => design.colors[t.toLowerCase()]);
const stock = {
  tier: "Stock",
  mv: 1000,
  target: 2762,
  meas: 2734,
  fps: 63.4,
  power: 341,
  temp: 67,
  fan: 41,
  passed: true,
  points: sample.base,
};
const initialSetup = buildInitialSetup();
let settings = structuredClone(initialSetup),
  draft,
  stage = "idle",
  plan = [],
  completed = 0,
  ticks = 0,
  selected = -1,
  logLines = [],
  timer,
  verifyMinutes = defaultVerificationMinutes(),
  verified = [],
  useExamples = false,
  previousKey = "",
  note = "",
  zoomRange = 1;
const dlg = () => document.getElementById("panel"),
  closePanel = () => dlg().close();
function showPanel(content) {
  dlg().innerHTML = content;
  if (!dlg().open) dlg().showModal();
}
const dismiss =
  '<div class="dialogfoot"><button onclick="closePanel()">Close</button></div>';
function info(label, description) {
  showPanel(
    "<h2>" + label + '</h2><p class="sub">' + description + "</p>" + dismiss,
  );
}
function est(key) {
  return sample.est[key].join("–") + " min";
}
function setupKey(s) {
  return JSON.stringify([
    s.scope,
    s.scope === "Full scan" ? s.settings : [s.tier, s.settings[s.tier]],
  ]);
}
function log(text) {
  logLines.push("[" + new Date().toLocaleTimeString("en-GB") + "] " + text);
}
function beginScan() {
  const resume =
    stage === "stopped" &&
    completed < plan.length &&
    previousKey === setupKey(draft);
  tab = "Auto UV";
  settings = structuredClone(draft);
  previousKey = setupKey(settings);
  if (!resume) {
    plan = [
      stock,
      ...sample.rows.filter(
        (r) => settings.scope === "Full scan" || r.tier === settings.tier,
      ),
    ];
    completed = 0;
    selected = -1;
    logLines = [];
    zoomRange = 1;
  }
  ticks = 0;
  note = "";
  stage = "running";
  closePanel();
  log(
    resume
      ? "Resume matching example scan"
      : "Starting " + settings.scope + " · GPU 0 · RTX 5080",
  );
  log("Requested settings " + JSON.stringify(settings.settings));
  log(
    resume
      ? "Continuing from saved example runs"
      : "Reading the stock voltage / frequency curve",
  );
  render();
  armTimer();
}
function armTimer() {
  clearInterval(timer);
  timer = setInterval(() => {
    ticks++;
    if (stage === "running" && ticks >= 4) {
      const r = plan[completed];
      log(
        r.tier +
          " · " +
          r.mv +
          " mV @ " +
          r.target +
          " MHz · " +
          (r.passed
            ? "Pass · " + r.fps + " FPS · " + r.power + " W"
            : "Failed · average FPS low"),
      );
      completed++;
      ticks = 0;
      if (completed === plan.length) {
        clearInterval(timer);
        stage = "choice";
        log("Ready for final verification");
      }
    } else if (stage === "verifying" && ticks >= 9) {
      clearInterval(timer);
      stage = "complete";
      commitVerifiedChoices();
      log("Final verification passed · " + verificationSummary());
      if (tab === "Profiles") render();
    }
    if (tab === "Auto UV") refreshScan();
    else shellTools();
  }, 700);
}
function activeTiers() {
  return settings.scope === "Full scan" ? tiers : [settings.tier];
}
function stopScan() {
  clearInterval(timer);
  stage = stage === "verifying" ? "verifyStopped" : "stopped";
  log("Stopped at user request");
  note = "";
  render();
}
async function copyLogs() {
  const lines = [...logLines];
  try {
    await navigator.clipboard.writeText(lines.join("\n"));
    note = "Copied all " + lines.length + " lines";
  } catch {
    showPanel(
      '<h2>Full scan log</h2><textarea id="fullLog" aria-label="Full scan log" readonly rows="12"></textarea>' +
        dismiss,
    );
    const field = document.getElementById("fullLog");
    field.value = lines.join("\n");
    field.select();
    return;
  }
  const status = document.getElementById("copied");
  if (status) status.textContent = note;
}
function scanView() {
  const busy = ["running", "verifying"].includes(stage),
    idle = stage === "idle",
    r = plan[completed],
    names = {
      idle: "Ready to scan",
      stopped: "Scan stopped",
      choice: "Ready for final verification",
      verifying: "Final verification",
      verifyStopped: "Verification stopped",
      complete: "Scan complete",
      running:
        completed === 0
          ? "Measuring stock baseline"
          : "Testing " + (r?.tier || "") + " candidate",
    };
  const detail = idle
    ? "No Auto UV runs in this session. Choose a GPU and scan scope to get started."
    : stage === "running"
      ? r.mv +
        " mV @ " +
        r.target +
        " MHz · " +
        ticks * 5 +
        " / 20 s · " +
        Math.round(r.meas + Math.sin(ticks * 1.9) * 5) +
        " MHz measured"
      : stage === "complete"
        ? (verificationSummary() || activeTiers().join(" · ")) + " verified"
        : stage === "stopped"
          ? "Recorded runs are available below. Start with matching settings to resume."
          : stage === "verifying"
            ? currentVerificationTier() +
              " · " +
              selectedVerificationMinutes(currentVerificationTier()) +
              " min · accelerated replay"
            : "RTX 5080 · " + settings.scope;
  return (
    '<div class="titlebar"><div><h1>Auto UV</h1><p>GPU 0 · NVIDIA GeForce RTX 5080</p></div></div><section class="card">' +
    head(names[stage], detail) +
    logStrip() +
    (["choice", "verifyStopped"].includes(stage) ||
    (stage === "stopped" &&
      settings.scope !== "Full scan" &&
      tierCandidates(settings.tier).length > 0)
      ? '<div class="verifybar"><button class="start" onclick="verifySetup()">Set up final verification</button></div>'
      : stage === "complete"
        ? '<div class="verifybar"><button onclick="useExamples=false;navigate(\'Profiles\')">View verified profiles →</button></div>'
        : "") +
    (idle
      ? '<div class="plotbox empty"><h3>Voltage / frequency curve</h3><small>The base curve appears when the scan starts measuring.</small></div>'
      : '<div class="plotbox"><canvas id="vf" tabindex="0" aria-label="Voltage-frequency plot. Scroll to zoom, double-click to reset." onwheel="wheelPlot(event)" ondblclick="zoomRange=1;plotVF()"></canvas></div><div class="legend">' +
        curveLegend() +
        "</div>") +
    '</section><section class="card">' +
    head(
      "Undervolting runs",
      idle
        ? "Measurements and results appear here during a scan."
        : settings.scope + " · " + completed + " completed runs",
    ) +
    (!idle && settings.scope === "Full scan"
      ? '<div class="tiers">' +
        tiers
          .map((t) => {
            const tierRows = plan.filter((r) => r.tier === t),
              done = plan
                .slice(0, completed)
                .filter((r) => r.tier === t).length,
              state =
                done === tierRows.length ? "done" : r?.tier === t ? "now" : "";
            return (
              '<div class="' +
              state +
              '">' +
              t +
              " · " +
              (state === "done"
                ? "Complete"
                : state === "now"
                  ? stage === "running"
                    ? "Running"
                    : "Stopped"
                  : "Waiting") +
              "</div>"
            );
          })
          .join("") +
        "</div>"
      : "") +
    '<div class="scanTable"><table><thead><tr>' +
    [
      ["Run", ""],
      ["Voltage", "mV"],
      ["Target clock", "MHz"],
      ["Core offset", "MHz"],
      ["Measured clock", "MHz"],
      ["Frame rate", "FPS"],
      ["Board power", "W"],
      ["Temperature", "°C"],
      ["Fan speed", "%"],
      ["Power cap reason", ""],
      ["Efficiency", "FPS/W"],
      ["Result", ""],
      ["Status", ""],
    ]
      .map(
        ([l, u]) =>
          '<th scope="col">' +
          l +
          (["Frame rate", "Board power", "Efficiency"].includes(l)
            ? '<span class="metricpair units"><small>' +
              u +
              "</small><small>Δ stock</small></span>"
            : "<small>" + u + "</small>") +
          "</th>",
      )
      .join("") +
    "</tr></thead><tbody>" +
    runRows() +
    "</tbody></table></div>" +
    (idle ? '<p class="emptymsg">No runs yet</p>' : "") +
    "</section>"
  );
}
function logStrip() {
  if (!logLines.length) return "";
  const line = esc(logLines.at(-1));
  return `<div class="lastlog"><span>Latest log</span><samp title="${line}">${line}</samp>
    <span class="copymsg" id="copied" role="status">${esc(note)}</span>
    <button id="copyFullLog" title="Copy all ${logLines.length} lines" onclick="copyLogs()">Copy full log</button></div>`;
}
function deltaText(value, baseline, lowerBetter) {
  const pct = (value / baseline - 1) * 100,
    score = lowerBetter ? -pct : pct;
  const tone = Math.abs(pct) < 1 ? "flat" : score > 0 ? "gain" : "loss";
  const label = (pct > 0 ? "+" : "") + pct.toFixed(1) + "%";
  return (
    '<small class="delta ' +
    tone +
    '" title="Change from measured stock baseline">' +
    label +
    "</small>"
  );
}
function metricPair(value, delta) {
  return (
    '<span class="metricpair"><span class="reading">' +
    value +
    "</span>" +
    (delta || '<small class="delta blank" aria-hidden="true"></small>') +
    "</span>"
  );
}
function runRows() {
  return plan
    .slice(0, completed + (stage === "running" || stage === "stopped" ? 1 : 0))
    .map((r, i) => {
      const done = i < completed,
        oc = i ? Math.round(r.target - stockClockAt(r.mv)) : 0,
        delta = (n, b, lowerBetter = false) =>
          i && done ? deltaText(n, b, lowerBetter) : "",
        val = (n, d = 2) => (done ? n.toFixed(d) : "—"),
        result = done
          ? r.passed
            ? "Pass"
            : "Failed"
          : stage === "running"
            ? "Running"
            : "Stopped";
      return (
        '<tr class="' +
        (i === selected ? "selected" : "") +
        '" onclick="chooseRun(' +
        i +
        ')"><td><button id="run' +
        i +
        '" aria-pressed="' +
        (i === selected) +
        '">' +
        (i + 1) +
        "</button></td><td>" +
        r.mv +
        "</td><td>" +
        r.target +
        "</td><td>" +
        (oc > 0 ? "+" : "") +
        oc +
        "</td><td>" +
        val(r.meas) +
        "</td><td>" +
        metricPair(val(r.fps), delta(r.fps, stock.fps)) +
        "</td><td>" +
        metricPair(val(r.power), delta(r.power, stock.power, true)) +
        "</td><td>" +
        val(r.temp) +
        "</td><td>" +
        val(r.fan) +
        "</td><td>" +
        (done ? "none" : "—") +
        "</td><td>" +
        metricPair(
          val(r.fps / r.power, 4),
          delta(r.fps / r.power, stock.fps / stock.power),
        ) +
        '</td><td class="' +
        (!done ? "pending" : r.passed ? "good" : "bad") +
        '">' +
        result +
        "</td><td>" +
        (done
          ? r.passed
            ? "20 / 20 s"
            : "Average FPS low"
          : ticks * 5 + " / 20 s") +
        "</td></tr>"
      );
    })
    .join("");
}
function refreshScan() {
  shellTools();
  const sc = document.querySelector(".scanTable"),
    top = sc?.scrollTop || 0,
    left = sc?.scrollLeft || 0,
    tail = !sc || sc.scrollHeight - sc.scrollTop - sc.clientHeight < 10,
    active = document.activeElement?.id;
  M.innerHTML = scanView();
  const next = document.querySelector(".scanTable");
  next.scrollTop = tail ? next.scrollHeight : top;
  next.scrollLeft = left;
  if (active) document.getElementById(active)?.focus({ preventScroll: true });
  plotVF();
}
function chooseRun(i) {
  selected = selected === i ? -1 : i;
  refreshScan();
  document.getElementById("run" + i)?.focus({ preventScroll: true });
}
function wheelPlot(ev) {
  ev.preventDefault();
  zoomRange = Math.max(
    1,
    Math.min(4, zoomRange * (ev.deltaY < 0 ? 1.15 : 1 / 1.15)),
  );
  plotVF();
}
