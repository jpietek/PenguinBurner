function emptyProfiles() {
  return (
    title("Profiles", "Saved undervolt profiles · GeForce RTX 5080") +
    '<section class="card">' +
    head(
      "No saved profiles yet",
      "Complete Auto UV and final verification to create your first profile.",
    ) +
    '<div class="row"><button class="start" onclick="navigate(\'Auto UV\');openSetup()">Setup Auto Undervolt</button><button onclick="useExamples=true;profileMeta.slice(0,3).forEach(m=>m.deleted=false);render()">Preview example profiles</button></div></section>'
  );
}

function profileReading(value, precision, unit) {
  return Number.isFinite(value) ? value.toFixed(precision) + " " + unit : "—";
}
const profileMeta = profiles.map((p, i) => ({
  tier: p[0],
  origin: i,
  valid: true,
  source: "Auto UV",
  mem: i === 0 ? 0 : 3000,
  date: ["13 Jul · 22:28", "13 Jul · 22:36", "14 Jul · 00:00"][i],
  deleted: false,
}));
let appliedFan = false,
  bootFan = false,
  fullMetrics = false,
  manageProfiles = false,
  marked = new Set(),
  editor = null;
const esc = (s) =>
  String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll('"', "&quot;");
function available() {
  return order.filter(
    (i) =>
      !profileMeta[i].deleted &&
      (useExamples || profileMeta[i].copy || profileMeta[i].fromVerification),
  );
}
function adaptiveTiers() {
  return [
    ...new Set(
      available()
        .filter((i) => profileMeta[i].valid && profileMeta[i].tier !== "None")
        .map((i) => profileMeta[i].tier),
    ),
  ];
}
function profileView() {
  const ids = available();
  if (!ids.length) return emptyProfiles();
  if (!ids.includes(pi)) pi = ids[0];
  const p = profiles[pi],
    m = profileMeta[pi],
    isApplied = applied === pi && appliedFan === silentFan,
    active =
      applied < 0
        ? "Stock"
        : profiles[applied][0] + (appliedFan ? " · silent fan" : "");
  const table =
    '<div class="tableWrap"><table><thead><tr><th>Profile</th>' +
    ["Frame rate", "Board power", "Efficiency"]
      .map(
        (v, i) =>
          '<th><button aria-label="Sort by ' +
          v +
          '" onclick="sortProfiles(' +
          (i + 3) +
          ')">' +
          v +
          " " +
          (sortKey === i + 3 ? (asc ? "↑" : "↓") : "↕") +
          "</button></th>",
      )
      .join("") +
    (fullMetrics
      ? [
          "Voltage",
          "Target clock",
          "Effective clock",
          "Memory offset",
          "Tier",
          "Source",
          "Saved",
          "GPU",
        ]
          .map((v) => "<th>" + v + "</th>")
          .join("")
      : "") +
    "</tr></thead><tbody>" +
    ids
      .map((i) => {
        let v = profiles[i],
          m = profileMeta[i];
        return (
          '<tr class="' +
          (pi === i ? "selected" : "") +
          '" onclick="chooseProfile(' +
          i +
          ')" oncontextmenu="openProfileMenu(event,' +
          i +
          ')"><td>' +
          (manageProfiles
            ? '<input type="checkbox" aria-label="Mark ' +
              esc(v[0]) +
              '" ' +
              (marked.has(i) ? "checked" : "") +
              ' onclick="event.stopPropagation();mark(' +
              i +
              ',this.checked)"> '
            : "") +
          '<button aria-haspopup="menu" data-profile="' +
          i +
          '" aria-pressed="' +
          (pi === i) +
          '">' +
          esc(v[0]) +
          "</button>" +
          (!m.valid
            ? pill("Needs verification", "warm")
            : i === applied
              ? pill("Applied")
              : "") +
          "</td><td>" +
          profileReading(v[3], 2, "FPS") +
          "</td><td>" +
          profileReading(v[4], 2, "W") +
          "</td><td>" +
          profileReading(v[5], 4, "FPS/W") +
          "</td>" +
          (fullMetrics
            ? [
                v[1] + " mV",
                v[2] + " MHz",
                profileReading(
                  m.meas ?? (m.imported ? null : [2439, 2595, 2912][m.origin]),
                  0,
                  "MHz",
                ),
                m.mem + " MHz",
                m.tier,
                m.source,
                m.date,
                "RTX 5080",
              ]
                .map((z) => "<td>" + esc(z) + "</td>")
                .join("")
            : "") +
          "</tr>"
        );
      })
      .join("") +
    "</tbody></table></div>";
  return (
    title("Profiles", "GPU 0 · GeForce RTX 5080 · Current session: " + esc(active)) +
    '<section class="card">' +
    head(
      "Saved profiles",
      ids.length + (useExamples ? " example profiles" : " saved profiles"),
      '<div class="profileTools"><button aria-pressed="' +
        fullMetrics +
        '" onclick="fullMetrics=!fullMetrics;render()">All measurements</button><button aria-pressed="' +
        manageProfiles +
        '" onclick="manageProfiles=!manageProfiles;marked.clear();render()">' +
        (manageProfiles ? "Done" : "Select multiple") +
        "</button></div>",
    ) +
    table +
    (manageProfiles
      ? '<div class="row"><small>' +
        marked.size +
        " selected</small><button " +
        (marked.size ? "" : "disabled") +
        ' onclick="confirmDelete([...marked])">Delete selected</button></div>'
      : "") +
    '</section><section class="card">' +
    head(
      esc(p[0]) + " profile",
      m.source + " · " + m.tier + " tier",
      pill(
        m.valid ? (m.imported ? "Imported" : "Verified") : "Needs verification",
        m.valid ? "" : "warm",
      ),
    ) +
    '<div class="metrics">' +
    metric("Core voltage", p[1], "mV") +
    metric("Target clock", p[2], "MHz") +
    metric("Memory offset", m.mem, "MHz") +
    '</div><div class="row profActions"><button onclick="openEditor(\'vf\')">Edit V/F curve</button><button onclick="openEditor(\'fan\')">Edit fan curve</button><button id="moreProfileActions" aria-haspopup="menu" aria-expanded="false" onclick="openProfileMenu(event,pi)">More actions</button></div>' +
    row(
      "Silent fan curve",
      "Include the saved fan curve when you apply this profile.",
      '<button class="track" role="switch" aria-label="Silent fan curve" aria-checked="' +
        silentFan +
        '" onclick="silentFan=!silentFan;render()"><i></i></button>',
    ) +
    row(
      "Current session",
      esc(active) + " is active.",
      m.valid
        ? '<button class="primary" ' +
            (isApplied
              ? "disabled"
              : 'onclick="applied=pi;appliedFan=silentFan;render()"') +
            ">" +
            (isApplied
              ? "Currently applied"
              : "Apply " + esc(p[0]) + (silentFan ? " + silent fan" : "")) +
            "</button>"
        : '<button class="primary" onclick="verifyProfile()">Verify edited profile</button>',
    ) +
    row(
      "Startup profile",
      boot < 0
        ? "Stock at startup."
        : esc(profiles[boot][0]) + (bootFan ? " · silent fan" : "") + " at startup.",
      "<button " +
        (!m.valid ? "disabled" : "") +
        ' onclick="setBoot()">' +
        (boot === pi && bootFan === silentFan
          ? "Clear startup profile"
          : "Use at startup") +
        "</button>",
    ) +
    '</section><section class="card">' +
    head(
      "Adaptive for games",
      adaptiveTiers().length
        ? "Available tiers · " + adaptiveTiers().join(" · ")
        : "Verify a profile and assign a tier to enable Adaptive.",
      "<button " +
        (adaptiveTiers().length ? "" : "disabled") +
        ' onclick="adaptivePicker()">Use Adaptive for a game…</button>',
    ) +
    "</section>"
  );
}
function setBoot() {
  if (boot === pi && bootFan === silentFan) {
    boot = -1;
    bootFan = false;
  } else {
    boot = pi;
    bootFan = silentFan;
  }
  render();
}
function chooseProfile(i) {
  pi = i;
  render();
  M.querySelector(".selected button")?.focus({ preventScroll: true });
}
function mark(i, on) {
  on ? marked.add(i) : marked.delete(i);
  render();
}
function confirmDelete(ids) {
  showPanel(
    "<h2>Delete " +
      ids.length +
      " saved " +
      (ids.length === 1 ? "profile" : "profiles") +
      '?</h2><p class="sub">' +
      ids.map((i) => esc(profiles[i][0])).join(", ") +
      '</p><div class="dialogfoot"><button onclick="closePanel()">Cancel</button><button id="deleteConfirm">Delete</button></div>',
  );
  document.querySelector("#deleteConfirm").onclick = () => {
    ids.forEach((i) => {
      profileMeta[i].deleted = true;
      if (applied === i) applied = -1;
      if (boot === i) boot = -1;
    });
    marked.clear();
    manageProfiles = false;
    closePanel();
    render();
  };
}
function adaptivePicker() {
  showPanel(
    '<h2>Use Adaptive for a game</h2><p class="sub">Choose a game and the target FPS before frame generation. Adaptive uses the verified tiers on its GPU.</p><div class="fields gap"><label>Game<select id="adaptiveGame" onchange="document.getElementById(\'adaptiveFps\').value=games[Number(this.value)].target">' +
      games
        .map(
          (x, i) =>
            '<option value="' +
            i +
            '" ' +
            (i === idx ? "selected" : "") +
            ">" +
            esc(x.name) +
            "</option>",
        )
        .join("") +
      '</select></label><label>Target FPS<input id="adaptiveFps" type="number" min="1" max="500" required value="' +
      g().target +
      '"></label></div><div class="dialogfoot"><button onclick="closePanel()">Cancel</button><button class="primary" onclick="applyGameAdaptive()">Apply to game</button></div>',
  );
}
function applyGameAdaptive() {
  const fps = document.getElementById("adaptiveFps");
  if (!fps.reportValidity()) return;
  idx = Number(document.getElementById("adaptiveGame").value);
  Object.assign(g(), {
    wrapped: true,
    mode: "Adaptive",
    target: Number(fps.value),
    override: true,
  });
  q = "";
  launcherFilter = "All launchers";
  wrappedFilter = "Any setup";
  recordFilter = "Any history";
  closePanel();
  navigate("Game Library");
}
function restoreDialog() {
  showPanel(
    '<h2>Restore defaults?</h2><p class="sub">Return this GPU to stock for the current session and startup.</p><div class="dialogfoot"><button onclick="closePanel()">Cancel</button><button onclick="applied=boot=-1;appliedFan=bootFan=false;closePanel();render()">Restore defaults</button></div>',
  );
}
function verifyProfile() {
  showPanel(
    "<h2>Verify " +
      esc(profiles[pi][0]) +
      '</h2><p class="sub">Run a simulated stability check before applying the edited curve.</p><div class="dialogfoot"><button onclick="closePanel()">Cancel</button><button class="primary" onclick="verifyDemoProfile()">Start verification</button></div>',
  );
}
function verifyDemoProfile() {
  const id = pi;
  showPanel(
    "<h2>Verifying " +
      esc(profiles[id][0]) +
      '</h2><p class="sub">Replaying the sample verification workload…</p><progress></progress>',
  );
  setTimeout(() => {
    profileMeta[id].valid = true;
    if (dlg().open) closePanel();
    render();
  }, 1500);
}
function memoryDialog() {
  showPanel(
    '<h2>Edit memory offset</h2><div class="fields gap"><label>Memory offset (MHz)<input id="memval" type="number" value="' +
      profileMeta[pi].mem +
      '" required step="1"></label></div><div class="dialogfoot"><button onclick="closePanel()">Cancel</button><button onclick="if(memval.reportValidity()){let i=cloneEdit(pi,\'Memory edit\');profileMeta[i].mem=Number(memval.value);closePanel();render()}">Save edited copy</button></div>',
  );
}
function exportProfile() {
  const p = profiles[pi],
    points = profileCurve("vf", pi);
  let yaml =
    "# Demo LACT export · " +
    p[0] +
    '\ngpus:\n  "demo-nvidia-gpu":\n    fan_control_enabled: ' +
    silentFan +
    "\n";
  if (silentFan)
    yaml +=
      "    fan_control_settings:\n      mode: curve\n      curve:\n" +
      profileCurve("fan", pi)
        .map(([x, y]) => "        " + x + ": " + (y / 100).toFixed(2))
        .join("\n") +
      "\n";
  yaml +=
    "    gpu_vf_curve:\n" +
    points
      .map(
        ([x, y], i) =>
          "      " +
          i +
          ":\n        clockspeed: " +
          Math.round(y) +
          "\n        voltage: " +
          Math.round(x),
      )
      .join("\n");
  showPanel(
    '<h2>Export LACT</h2><p class="sub">Sample configuration for ' +
      esc(p[0]) +
      '.</p><pre style="max-height:38vh;overflow:auto">' +
      esc(yaml) +
      '</pre><div class="dialogfoot"><button onclick="closePanel()">Close</button><button id="downloadDemo">Download sample YAML</button></div>',
  );
  document.getElementById("downloadDemo").onclick = () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([yaml], { type: "text/yaml" }));
    a.download = "sample-lact-config.yaml";
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
}
function profileCurve(kind, i) {
  const m = profileMeta[i];
  return structuredClone(
    m[kind] ||
      (kind === "fan"
        ? [
            [30, 30],
            [45, 35],
            [55, 42],
            [65, 51],
            [75, 66],
            [85, 85],
            [90, 100],
          ]
        : sample.rows.filter((r) => r.tier === tiers[m.origin]).at(-1).points),
  );
}
function cloneEdit(i, source) {
  let j = profiles.length;
  profiles.push([...profiles[i]]);
  profiles[j][0] =
    (profileMeta[i].imported ? profiles[i][0] : tiers[profileMeta[i].origin]) +
    " edit " +
    (j - 2);
  profileMeta.push({
    ...structuredClone(profileMeta[i]),
    copy: true,
    valid: false,
    source,
    date: "Just now",
  });
  order.unshift(j);
  pi = j;
  return j;
}
function saveEditedCurve(kind, i, points) {
  let j = cloneEdit(i, kind === "vf" ? "V/F edit" : "Fan edit");
  profileMeta[j][kind] = structuredClone(points);
  if (kind === "vf")
    profiles[j][2] = Math.round(
      points.reduce((a, b) =>
        Math.abs(b[0] - profiles[j][1]) < Math.abs(a[0] - profiles[j][1])
          ? b
          : a,
      )[1],
    );
  render();
  return j;
}
function openEditor(kind) {
  if (window.pbQtPreview) {
    console.info("pb-qt-preview-edit-" + kind + ":" + pi);
    return;
  }
  const points = profileCurve(kind, pi);
  editor = {
    kind,
    id: pi,
    points,
    before: structuredClone(points),
    index: Math.floor(points.length / 2),
    undo: [],
    redo: [],
  };
  editorDialog();
}
function editorDialog() {
  const e = editor,
    fan = e.kind === "fan",
    p = e.points[e.index];
  showPanel(
    "<h2>Edit " +
      (fan ? "fan" : "V/F") +
      ' curve</h2><p class="sub">' +
      esc(profiles[e.id][0]) +
      ' · Click or drag a point. Arrow keys select and adjust it.</p><canvas class="editplot" id="editplot" tabindex="0" aria-label="Editable curve"></canvas><div class="legend"><span>Gray · before edit</span><span class="good">Green · draft</span></div><div class="editorControls"><button onclick="shiftPoint(-1)">←</button><b id="pointlabel"></b><button onclick="shiftPoint(1)">→</button><label>' +
      (fan ? "Fan speed (%)" : "Clock (MHz)") +
      ' <input id="pointY" type="number" onchange="pointInput(this.value)" value="' +
      Math.round(p[1]) +
      '"></label><button onclick="undoPoint()">Undo</button><button onclick="redoPoint()">Redo</button><button onclick="revertPoints()">Revert</button></div><div class="dialogfoot"><button onclick="closePanel()">Cancel</button><button class="primary" onclick="if(JSON.stringify(editor.before)!==JSON.stringify(editor.points)){saveEditedCurve(editor.kind,editor.id,editor.points)}closePanel()">Save edited copy</button></div>',
  );
  requestAnimationFrame(bindEditor);
}
function stash() {
  editor.undo.push(structuredClone(editor.points));
  editor.redo = [];
}
function shiftPoint(n) {
  editor.index = Math.max(
    0,
    Math.min(editor.points.length - 1, editor.index + n),
  );
  drawEditor();
}
function pointInput(v, remember = true) {
  if (remember) stash();
  editor.points[editor.index][1] = Math.round(
    Math.max(
      editor.kind === "fan" ? 0 : 1200,
      Math.min(editor.kind === "fan" ? 100 : 3600, Number(v)),
    ),
  );
  drawEditor();
}
function undoPoint() {
  if (editor.undo.length) {
    editor.redo.push(editor.points);
    editor.points = editor.undo.pop();
    drawEditor();
  }
}
function redoPoint() {
  if (editor.redo.length) {
    editor.undo.push(editor.points);
    editor.points = editor.redo.pop();
    drawEditor();
  }
}
function revertPoints() {
  stash();
  editor.points = structuredClone(editor.before);
  drawEditor();
}
function geometry(c) {
  const e = editor,
    fan = e.kind === "fan",
    w = c.clientWidth,
    h = c.clientHeight,
    lo = e.before[0][0],
    hi = e.before.at(-1)[0],
    a = fan ? 0 : 1200,
    b = fan ? 100 : 3600;
  return {
    w,
    h,
    lo,
    hi,
    a,
    b,
    x: (v) => 48 + ((w - 62) * (v - lo)) / (hi - lo),
    y: (v) => h - 30 - ((h - 48) * (v - a)) / (b - a),
    inv: (y) => a + ((h - 30 - y) * (b - a)) / (h - 48),
  };
}
function drawEditor() {
  const c = document.getElementById("editplot");
  if (!c) return;
  const e = editor,
    z = geometry(c),
    d = devicePixelRatio || 1;
  c.width = z.w * d;
  c.height = z.h * d;
  const a = c.getContext("2d");
  a.scale(d, d);
  a.font = "11px Inter";
  a.fillStyle = "#acb6bd";
  for (let j = 0; j <= 4; j++) {
    let n = z.a + ((z.b - z.a) * j) / 4,
      y = z.y(n);
    a.fillText(n, 6, y + 3);
    a.strokeStyle = "#343c41";
    a.beginPath();
    a.moveTo(46, y);
    a.lineTo(z.w - 12, y);
    a.stroke();
  }
  a.fillText(z.lo + (e.kind === "fan" ? " °C" : " mV"), 36, z.h - 8);
  a.fillText(z.hi + (e.kind === "fan" ? " °C" : " mV"), z.w - 54, z.h - 8);
  function trace(ps, col) {
    a.strokeStyle = col;
    a.lineWidth = 2;
    a.beginPath();
    ps.forEach(([v, m], i) =>
      i ? a.lineTo(z.x(v), z.y(m)) : a.moveTo(z.x(v), z.y(m)),
    );
    a.stroke();
  }
  trace(e.before, "#879199");
  trace(e.points, "#9de0b6");
  e.points.forEach(([x, y], i) => {
    a.fillStyle = i === e.index ? "#ffbe72" : "#9de0b6";
    a.beginPath();
    a.arc(z.x(x), z.y(y), i === e.index ? 5 : 2, 0, 7);
    a.fill();
  });
  const pt = e.points[e.index];
  pointlabel.textContent =
    Math.round(pt[0]) + (e.kind === "fan" ? " °C" : " mV");
  pointY.value = Math.round(pt[1]);
}
function bindEditor() {
  const c = document.getElementById("editplot");
  if (!c) return;
  drawEditor();
  c.onpointerdown = (ev) => {
    const z = geometry(c);
    editor.index = editor.points.reduce(
      (best, p, i) =>
        Math.abs(z.x(p[0]) - ev.offsetX) <
        Math.abs(z.x(editor.points[best][0]) - ev.offsetX)
          ? i
          : best,
      0,
    );
    c.setPointerCapture(ev.pointerId);
    c.focus();
    stash();
    drawEditor();
  };
  c.onpointermove = (ev) => {
    if (c.hasPointerCapture(ev.pointerId))
      pointInput(geometry(c).inv(ev.offsetY), false);
  };
  c.onkeydown = (ev) => {
    if (ev.key.startsWith("Arrow")) {
      ev.preventDefault();
      if (ev.key === "ArrowLeft" || ev.key === "ArrowRight")
        shiftPoint(ev.key === "ArrowLeft" ? -1 : 1);
      else
        pointInput(
          editor.points[editor.index][1] +
            (ev.key === "ArrowUp" ? 1 : -1) * (editor.kind === "fan" ? 1 : 15),
        );
    } else if (ev.ctrlKey && ev.key === "z") {
      ev.preventDefault();
      undoPoint();
    } else if (ev.ctrlKey && ev.key === "y") {
      ev.preventDefault();
      redoPoint();
    }
  };
}

function sortProfiles(k) {
  asc = sortKey === k ? !asc : false;
  sortKey = k;
  order.sort((a, b) => (profiles[a][k] - profiles[b][k]) * (asc ? 1 : -1));
  render();
  M.querySelectorAll("th button")[k - 3].focus({ preventScroll: true });
}
