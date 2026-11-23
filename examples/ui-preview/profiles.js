// Profiles tab: the saved-profile table (sortable, multi-select), multi-GPU
// targeting, Apply and Apply on startup, Restore defaults, delete safety,
// Verify, Export LACT, memory offset and the V/F and fan curve editors.
// Sample data only: every daemon write is simulated in this session.

// Per-GPU facts the sample needs beyond GPU_CHOICES (shell.js).
const PROFILE_GPU_FACTS = [
  { bus: "01:00.0", factoryW: 360, memory: [0, 2000] },
  { bus: "02:00.0", factoryW: 200, memory: [0, 1500] },
];
// The example scan's stock run is every example profile's baseline.
const PROFILE_BASELINE = { mv: 1000, eff: 2734, fps: 63.4, power: 341 };
const DEFAULT_FAN_CURVE = [
  [30, 30],
  [45, 35],
  [55, 42],
  [65, 51],
  [75, 66],
  [85, 85],
  [90, 100],
];
const at = (y, mo, d, h, mi) => new Date(y, mo - 1, d, h, mi).getTime();

// Two extra examples: a profile bound to the second GPU (shown when the
// prototype has two graphics cards) and an unbound profile from 0.6.
profiles.push(
  ["Balanced", 875, 2550, 48.2, 168.4, 0.2862],
  ["2700 MHz 880 mV", 880, 2700, 63.9, 289.6, 0.2206],
);
order.push(3, 4);
const profileMeta = [
  { tier: "Efficiency", meas: 2439, mem: 0, saved: at(2026, 7, 13, 22, 28) },
  { tier: "Balanced", meas: 2595, mem: 1500, saved: at(2026, 7, 13, 22, 36) },
  { tier: "Performance", meas: 2912, mem: 1500, saved: at(2026, 7, 14, 0, 0) },
  {
    tier: "Balanced",
    meas: 2528,
    mem: 1000,
    saved: at(2026, 7, 2, 19, 12),
    gpu: 1,
    base: { mv: 1005, eff: 2640, fps: 51.0, power: 197 },
  },
  {
    tier: "Balanced",
    meas: 2681,
    mem: 0,
    saved: at(2026, 6, 2, 21, 4),
    gpu: null,
    legacy: true,
    noFan: true,
  },
].map((m, i) => ({
  origin: [0, 1, 2, 1, 1][i],
  valid: true,
  source: "Auto UV",
  gpu: 0,
  base: PROFILE_BASELINE,
  assigned: 0,
  deleted: false,
  ...m,
}));

let profileSel = new Set(),
  selAnchor = -1,
  profileSort = { key: "saved", desc: true },
  fullMetrics = false,
  silentFanCurve = false,
  profileTarget = 0, // Target GPU index, or null for "Choose target GPU…"
  runtime = null, // {gpu, id ('stock' or a profile index), fan, adaptive}
  bootByGpu = {}, // gpu index -> {id, fan}
  bootIntent = {}, // gpu index -> true while ticked with nothing saved yet
  mainGpu = null,
  profileCommand = "", // a simulated daemon command in flight
  profileNotice = null, // {text, tone} under the Apply row
  selectionCleared = false, // the user deselected every row on purpose
  editor = null;

const esc = (s) =>
  String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll('"', "&quot;");

// ---------- Model ----------
function normalizeProfiles() {
  // Profiles created elsewhere (final verification) carry only the scan
  // fields; give them a timestamp, a GPU binding and the stock baseline.
  profileMeta.forEach((m, i) => {
    if (m.saved === undefined) {
      m.saved = Date.now() + i;
      if (m.fromVerification) markPreferred(i);
    }
    if (m.gpu === undefined) m.gpu = appState.selectedGpu;
    if (m.base === undefined && m.source === "Auto UV") m.base = PROFILE_BASELINE;
    if (m.assigned === undefined) m.assigned = 0;
  });
}
function markPreferred(i) {
  profileMeta.forEach((m, j) => (m.preferred = j === i));
}
function multiGpu() {
  return appState.gpus.length > 1;
}
function targetGpu() {
  if (!multiGpu()) return 0;
  return profileTarget === null ? null : Math.min(profileTarget, appState.gpus.length - 1);
}
function gpuShort(i) {
  return "GPU " + i + " · " + (GPU_CHOICES[i] || GPU_CHOICES[0]).short;
}
function gpuColumnLabel(m) {
  return m.gpu === null
    ? "Unassigned (legacy)"
    : GPU_CHOICES[m.gpu].name.replace("NVIDIA ", "") + " (" + PROFILE_GPU_FACTS[m.gpu].bus + ")";
}
// Profiles the table shows: deleted rows never, examples only while the
// examples are on, and rows bound to another GPU are hidden by the target.
function available() {
  normalizeProfiles();
  const target = targetGpu();
  return profiles
    .map((_, i) => i)
    .filter((i) => {
      const m = profileMeta[i];
      if (!m || m.deleted) return false;
      if (!(useExamples || m.copy || m.fromVerification)) return false;
      if (m.gpu !== null && m.gpu >= appState.gpus.length) return false;
      return target === null || m.gpu === null || m.gpu === target;
    });
}
function profileMatchesTarget(i) {
  const m = profileMeta[i],
    target = targetGpu();
  if (target === null) return false;
  return m.gpu === null ? !multiGpu() : m.gpu === target;
}
function hasFanCurve(i) {
  const m = profileMeta[i];
  return !!(m.fan?.length || (m.source !== "MSI Afterburner" && !m.noFan));
}
function canEditMemory(i) {
  return Number.isFinite(profileMeta[i].mem);
}
function profilesBusy() {
  // appState.nativeScanBusy: the Qt mock's native Auto UV page is scanning.
  return stage === "running" || stage === "verifying" || !!profileCommand || !!appState.nativeScanBusy;
}
// Adaptive picks one profile per tier and GPU: an explicit assignment wins,
// otherwise the newest verified profile with that tier. Legacy profiles join
// the population on a one-GPU machine.
function tierWinners(gpu = targetGpu()) {
  const winners = {};
  const pool = profiles
    .map((_, i) => i)
    .filter((i) => {
      const m = profileMeta[i];
      if (!m || m.deleted || !m.valid || !tiers.includes(m.tier)) return false;
      if (!(useExamples || m.copy || m.fromVerification)) return false;
      return m.gpu === gpu || (m.gpu === null && !multiGpu());
    })
    .sort(
      (a, b) =>
        profileMeta[b].assigned - profileMeta[a].assigned ||
        profileMeta[b].saved - profileMeta[a].saved,
    );
  for (const i of pool) winners[profileMeta[i].tier] ??= i;
  return winners;
}
function tierLabel(i) {
  const m = profileMeta[i],
    gpu = m.gpu === null ? targetGpu() : m.gpu;
  return tiers.includes(m.tier) && tierWinners(gpu)[m.tier] === i ? m.tier : "";
}
function adaptiveTiers() {
  const winners = tierWinners(targetGpu() ?? appState.selectedGpu);
  return tiers.filter((t) => t in winners);
}
function profileName(i) {
  return profiles[i][0];
}

// ---------- Sorting ----------
const PROFILE_COLUMNS = [
  { key: "name", label: "Profile", left: true, compact: true },
  { key: "saved", label: "Saved", sort: true, left: true, compact: true },
  { key: "gpu", label: "GPU", sort: true, left: true },
  { key: "mv", label: "Voltage", sort: true },
  { key: "target", label: "Target clock", sort: true },
  { key: "eff", label: "Effective clock", sort: true },
  { key: "fpsw", label: "FPS/W", sort: true, compact: true },
  { key: "fps", label: "FPS", sort: true, compact: true },
  { key: "power", label: "Power", sort: true, compact: true },
  { key: "mem", label: "Memory" },
  { key: "tier", label: "Tier", left: true, compact: true },
  { key: "source", label: "Source", left: true },
];
function sortValue(i, key) {
  const p = profiles[i],
    m = profileMeta[i];
  const value = {
    saved: m.saved,
    gpu: gpuColumnLabel(m).toLowerCase(),
    mv: p[1],
    target: p[2],
    eff: m.meas,
    fpsw: p[5],
    fps: p[3],
    power: p[4],
  }[key];
  return typeof value === "string" || Number.isFinite(value) ? value : null;
}
// Missing values sort last in either direction; ties keep the newest first.
function sortedProfiles(ids, sort = profileSort) {
  return [...ids].sort((a, b) => {
    const x = sortValue(a, sort.key),
      y = sortValue(b, sort.key);
    if (x === null || y === null)
      return (x === null) - (y === null) || profileMeta[b].saved - profileMeta[a].saved;
    const c = x < y ? -1 : x > y ? 1 : 0;
    return (sort.desc ? -c : c) || profileMeta[b].saved - profileMeta[a].saved;
  });
}
function sortProfiles(key) {
  profileSort =
    profileSort.key === key
      ? { key, desc: !profileSort.desc }
      : { key, desc: key === "saved" };
  render();
  M.querySelector('th[data-sort="' + key + '"] button')?.focus({ preventScroll: true });
}

// ---------- Selection ----------
function selectedIds() {
  const shown = available();
  return sortedProfiles(shown).filter((i) => profileSel.has(i));
}
function selectProfiles(ids, lead = ids.at(-1)) {
  profileSel = new Set(ids);
  selectionCleared = !ids.length;
  if (lead !== undefined) pi = lead;
  profileNotice = null;
}
function chooseProfile(i) {
  selectProfiles([i]);
  selAnchor = i;
  render();
  focusProfileRow(i);
}
function focusProfileRow(i) {
  M.querySelector('[data-profile="' + i + '"]')?.focus({ preventScroll: true });
}
// Standard extended selection: click, Ctrl-click toggles, Shift-click
// selects the range from the anchor in the current sort order.
function rowClick(event, i) {
  if (event.target.closest("input")) return;
  const rows = sortedProfiles(available());
  if (event.shiftKey && rows.includes(selAnchor)) {
    const [a, b] = [rows.indexOf(selAnchor), rows.indexOf(i)].sort((x, y) => x - y);
    const range = rows.slice(a, b + 1);
    selectProfiles(
      event.ctrlKey || event.metaKey ? [...new Set([...profileSel, ...range])] : range,
      i,
    );
  } else if (event.ctrlKey || event.metaKey) {
    const next = new Set(profileSel);
    next.has(i) ? next.delete(i) : next.add(i);
    selectProfiles([...next], i);
    selAnchor = i;
  } else {
    selectProfiles([i]);
    selAnchor = i;
  }
  render();
  focusProfileRow(i);
}
function profileTableKey(event) {
  if (!event.target.closest("tbody")) return;
  const rows = sortedProfiles(available()),
    at = rows.indexOf(pi);
  const move = { ArrowDown: 1, ArrowUp: -1, Home: -rows.length, End: rows.length }[event.key];
  if (move !== undefined && rows.length) {
    event.preventDefault();
    const next = rows[Math.max(0, Math.min(rows.length - 1, at + move))];
    if (event.shiftKey && rows.includes(selAnchor)) {
      const [a, b] = [rows.indexOf(selAnchor), rows.indexOf(next)].sort((x, y) => x - y);
      selectProfiles(rows.slice(a, b + 1), next);
    } else {
      selectProfiles([next]);
      selAnchor = next;
    }
    render();
    focusProfileRow(next);
  } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
    event.preventDefault();
    selectProfiles(rows, pi);
    render();
    focusProfileRow(pi);
  } else if (event.key === "Delete") {
    event.preventDefault();
    deleteSelected();
  }
}

// ---------- Formatting ----------
function profileReading(value, precision, unit = "") {
  return Number.isFinite(value) ? value.toFixed(precision) + (unit ? " " + unit : "") : "";
}
function signedMemoryClock(mhz) {
  if (!Number.isFinite(mhz)) return "";
  return (mhz > 0 ? "+" : "") + Math.round(mhz) + " MHz";
}
function transferRate(mhz) {
  return "= " + (mhz * 2 > 0 ? "+" : "") + mhz * 2 + " MT/s transfer rate";
}
function deltaPercent(value, base) {
  return Number.isFinite(value) && Number.isFinite(base) && base ? ((value - base) / base) * 100 : null;
}
function signedPercent(d) {
  return (d > 0 ? "+" : d < 0 ? "−" : "") + Math.abs(d).toFixed(2) + "%";
}
function metricTooltip(label, value, base, precision) {
  const d = deltaPercent(value, base);
  if (d === null) return "";
  return (
    label +
    ": " +
    value.toFixed(precision) +
    "\n" +
    signedPercent(d) +
    " vs this scan's baseline: " +
    base.toFixed(precision) +
    ".\nBaselines differ between scans; these percentages do not compare profiles."
  );
}
function powerContext(i) {
  const m = profileMeta[i],
    watts = profiles[i][4],
    limit = m.gpu === null ? null : PROFILE_GPU_FACTS[m.gpu].factoryW,
    d = deltaPercent(watts, limit);
  if (!Number.isFinite(watts)) return { text: "", title: "" };
  if (d === null)
    return {
      text: watts.toFixed(2) + " W",
      title: "Factory/default power limit unavailable for this profile's GPU.",
    };
  return {
    text: watts.toFixed(2) + " W",
    delta: signedPercent(d),
    gain: d < 0,
    title:
      signedPercent(d) +
      " relative to this GPU's factory/default power limit (" +
      limit.toFixed(2) +
      " W).\nThis compares measured power to a power cap, not measured stock power savings.",
  };
}
function formatSaved(ts) {
  const d = new Date(ts),
    two = (n) => String(n).padStart(2, "0");
  return (
    d.getFullYear() + "-" + two(d.getMonth() + 1) + "-" + two(d.getDate()) + " " + two(d.getHours()) + ":" + two(d.getMinutes())
  );
}
function profileCell(i, key) {
  const p = profiles[i],
    m = profileMeta[i],
    base = m.base || {};
  const cell = (text, title = "", cls = "") =>
    '<td class="' +
    ("c-" + key + (cls ? " " + cls : "")) +
    '"' +
    (title ? ' title="' + esc(title) + '"' : "") +
    ">" +
    (text === "" ? '<span class="missing" aria-label="Not measured">—</span>' : text) +
    "</td>";
  switch (key) {
    case "saved":
      return cell(esc(formatSaved(m.saved)), "", "l");
    case "gpu":
      return cell(esc(gpuColumnLabel(m)), "", "l");
    case "mv":
      return cell(p[1] + " mV", metricTooltip("Voltage", p[1], base.mv, 0));
    case "target":
      return cell(p[2] + " MHz");
    case "eff":
      return cell(profileReading(m.meas, 0, "MHz"), metricTooltip("Effective clock", m.meas, base.eff, 2));
    case "fpsw":
      return cell(
        profileReading(p[5], 4),
        metricTooltip("FPS/W", p[5], base.fps && base.power ? base.fps / base.power : null, 4),
      );
    case "fps":
      return cell(profileReading(p[3], 2), metricTooltip("FPS", p[3], base.fps, 2));
    case "power": {
      const c = powerContext(i);
      return cell(
        c.text +
          (c.delta ? ' <span class="delta ' + (c.gain ? "gain" : "flat") + '">' + c.delta + "</span>" : ""),
        c.title,
      );
    }
    case "mem":
      return cell(
        canEditMemory(i) ? signedMemoryClock(m.mem) : "",
        canEditMemory(i) ? transferRate(m.mem) : "",
      );
    case "tier": {
      const t = tierLabel(i);
      return cell(
        t ? '<span class="tierTag" style="--tier:' + tierColor(t) + '">' + t + "</span>" : "&nbsp;",
        "",
        "l",
      );
    }
    case "source":
      return cell(esc(m.source), "", "l");
  }
  return "";
}

// ---------- Runner status (window status band) ----------
function profileStatusLabel(id, fan, adaptive) {
  return profileName(id) + (adaptive ? " (Adaptive)" : "") + (fan ? " · silent fan curve" : "");
}
// What runs now and what applies at startup, in the shell's idle summary.
// A running per-game Adaptive game shows both layers, like the real status.
// Both are read live, so a game that stops on another tab never leaves a
// stale line behind.
function runningProfileLabel() {
  const game = appState.runningGame != null ? games[appState.runningGame] : null,
    perGame = game && game.session?.state === "running" && game.mode === "Adaptive" && game.wrapped !== false;
  let running = null;
  if (runtime && runtime.id !== "stock" && profileMeta[runtime.id] && !profileMeta[runtime.id].deleted)
    running = profileStatusLabel(runtime.id, runtime.fan, runtime.adaptive);
  if (perGame) {
    const live = adaptiveTiers(),
      tier = game.liveTier || (live.includes("Balanced") ? "Balanced" : live[0]) || "Default";
    running = tier + " (Adaptive, per-game) · Standing: " + (running || "Default");
  }
  if (running && multiGpu() && runtime) running += " on GPU " + runtime.gpu;
  return running;
}
function bootProfileLabel() {
  const boot = bootByGpu[targetGpu() ?? appState.selectedGpu];
  return boot && boot.id !== "stock" && profileMeta[boot.id] && !profileMeta[boot.id].deleted
    ? profileStatusLabel(boot.id, boot.fan, false)
    : null;
}
for (const [key, get] of [
  ["runningProfile", runningProfileLabel],
  ["bootProfile", bootProfileLabel],
])
  Object.defineProperty(appState, key, { get, set() {}, enumerable: true, configurable: true });
// The native Qt mock hides the page's status band and draws its own; it
// listens for this console message to show the same two lines.
function postQtStatus() {
  if (!window.pbQtPreview) return;
  const s = appState.status || idleStatus();
  console.info("pb-qt-preview-status:" + JSON.stringify({ running: s.running, detail: s.detail }));
}
// A short-lived busy status while a simulated daemon command runs.
function runProfileCommand(running, detail, done, ms = 700) {
  profileCommand = running;
  setGlobalStatus({ running, detail });
  postQtStatus();
  render();
  setTimeout(() => {
    profileCommand = "";
    done();
    setGlobalStatus(null);
    postQtStatus();
    renderShellStatus();
    render();
  }, ms);
}
function startupChecked(gpu = targetGpu()) {
  return gpu !== null && !!(bootByGpu[gpu] || bootIntent[gpu]);
}

// ---------- View ----------
function emptyProfiles() {
  return (
    title("Profiles", "Saved undervolt profiles") +
    '<section class="card">' +
    head("No saved profiles yet", "Complete Auto UV and final verification to create your first profile.") +
    '<div class="row"><button class="start" onclick="navigate(\'Auto UV\');openSetup()">Setup Auto Undervolt</button><button onclick="useExamples=true;profileMeta.slice(0,5).forEach(m=>m.deleted=false);render()">Preview example profiles</button></div></section>'
  );
}
const trashIcon =
  '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M2.5 4h11M6 4V2.5h4V4M4 4l.7 9.5h6.6L12 4M6.6 6.5v5M9.4 6.5v5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';

function profileView() {
  postQtStatus();
  const ids = available();
  if (!profiles.some((_, i) => profileMeta[i] && !profileMeta[i].deleted && (useExamples || profileMeta[i].copy || profileMeta[i].fromVerification)))
    return emptyProfiles();
  for (const i of [...profileSel]) if (!ids.includes(i)) profileSel.delete(i);
  if (!profileSel.size && ids.length && !selectionCleared) {
    const first = ids.includes(pi) ? pi : sortedProfiles(ids)[0];
    profileSel.add(first);
    pi = first;
    selAnchor = first;
  }
  if (!profileSel.has(pi) && profileSel.size) pi = [...profileSel].at(-1);
  renderShellStatus();
  return (
    title("Profiles", "Apply, edit and verify saved undervolt profiles.") +
    profileTableCard(ids) +
    profileDetailCard() +
    adaptiveCard()
  );
}
function profileTableCard(ids) {
  const busy = profilesBusy(),
    target = targetGpu(),
    selected = selectedIds(),
    columns = PROFILE_COLUMNS.filter((c) => fullMetrics || c.compact),
    rows = sortedProfiles(ids),
    tabStop = rows.includes(pi) ? pi : rows[0];
  const header =
    "<tr>" +
    columns
      .map((c) => {
        const active = profileSort.key === c.key;
        return (
          '<th class="c-' +
          c.key +
          (c.left ? " l" : "") +
          '"' +
          (c.sort ? ' data-sort="' + c.key + '"' : "") +
          (c.sort ? ' aria-sort="' + (active ? (profileSort.desc ? "descending" : "ascending") : "none") + '"' : "") +
          ">" +
          (c.sort
            ? '<button type="button" onclick="sortProfiles(\'' +
              c.key +
              "')\">" +
              c.label +
              '<i class="sortArrow" aria-hidden="true"></i></button>'
            : c.label) +
          "</th>"
        );
      })
      .join("") +
    "</tr>";
  const body = rows
    .map((i) => {
      const m = profileMeta[i],
        isSel = profileSel.has(i),
        running = runtime && runtime.id === i,
        startup = Object.values(bootByGpu).some((b) => b.id === i);
      return (
        '<tr class="' +
        [isSel && "selected", m.preferred && "preferred"].filter(Boolean).join(" ") +
        '" aria-selected="' +
        isSel +
        '" onclick="rowClick(event,' +
        i +
        ')" oncontextmenu="openProfileMenu(event,' +
        i +
        ')">' +
        columns
          .map((c) =>
            c.key === "name"
              ? '<td class="c-name l"><button type="button" tabindex="' +
                (i === tabStop ? 0 : -1) +
                '" aria-haspopup="menu" data-profile="' +
                i +
                '">' +
                esc(profileName(i)) +
                "</button>" +
                (m.preferred ? pill("New", "newpill") : "") +
                (!m.valid ? pill("Needs verification", "warm") : "") +
                (running ? pill("Running") : "") +
                (startup ? pill("At startup", "muted") : "") +
                "</td>"
              : profileCell(i, c.key),
          )
          .join("") +
        "</tr>"
      );
    })
    .join("");
  const canDelete = !busy && selected.length > 0;
  const tools =
    '<div class="profileTools"><button type="button" aria-pressed="' +
    fullMetrics +
    '" onclick="fullMetrics=!fullMetrics;render()">All measurements</button>' +
    '<button type="button" class="iconButton" id="deleteProfiles" aria-label="Delete selected profiles" title="Delete selected profiles" ' +
    (canDelete ? "" : "disabled") +
    ' onclick="deleteSelected()">' +
    trashIcon +
    "</button>" +
    '<button type="button" id="restoreDefaults" title="Reset the GPU to stock now and at boot: clear core and memory offsets, release locked clocks, restore the factory V/F curve, and restore the default power limit." ' +
    (busy || target === null ? "disabled" : "") +
    ' onclick="restoreDefaults()">Restore defaults</button></div>';
  return (
    '<section class="card profilesCard">' +
    head(
      "Saved profiles",
      ids.length +
        (ids.length === 1 ? " profile" : " profiles") +
        (selected.length > 1 ? " · " + selected.length + " selected" : ""),
      tools,
    ) +
    (multiGpu() ? targetBar() : "") +
    '<div class="tableWrap"><table class="profileTable' + (fullMetrics ? " full" : "") + '" aria-label="Saved profiles" aria-multiselectable="true" onkeydown="profileTableKey(event)"><thead>' +
    header +
    "</thead><tbody>" +
    (body ||
      '<tr><td class="l emptyrow" colspan="' +
        columns.length +
        '">No saved profiles for this GPU.</td></tr>') +
    '</tbody></table></div><p class="tableHint">Ctrl-click selects several profiles and Shift-click selects a range. Press Delete or right-click to delete the selection. Per-game Adaptive targets are set in Game Library.</p></section>'
  );
}
function targetBar() {
  const target = targetGpu(),
    busy = profilesBusy(),
    hasBoot = target !== null && !!bootByGpu[target],
    mainTip =
      target === null
        ? "Choose a target NVIDIA GPU first."
        : !hasBoot
          ? "Apply a profile with Apply on startup first, then mark this NVIDIA GPU as the daemon's main monitored GPU."
          : mainGpu === target
            ? "This NVIDIA GPU owns daemon monitoring after boot. Untick to restore the default last-saved-GPU behavior."
            : "Make this NVIDIA GPU own daemon monitoring after boot. Manual Apply can still move monitoring for the current session.";
  return (
    '<div class="targetBar"><label>Target GPU <select id="profileTarget" title="Choose which GPU this Profiles action targets. Changing this selection does not modify hardware settings." onchange="setProfileTarget(this.value)"><option value=""' +
    (target === null ? " selected" : "") +
    ">Choose target GPU…</option>" +
    appState.gpus
      .map(
        (g, i) =>
          '<option value="' + i + '"' + (target === i ? " selected" : "") + ">" + esc(gpuShort(i)) + "</option>",
      )
      .join("") +
    '</select></label><label class="check" title="' +
    esc(mainTip) +
    '"><input type="checkbox" id="mainGpu" ' +
    (mainGpu !== null && mainGpu === target ? "checked " : "") +
    (busy || !hasBoot ? "disabled " : "") +
    'onchange="setMainGpu(this.checked)"> Main GPU</label><small>Changing the target does not change hardware settings.</small></div>'
  );
}
function profileDetailCard() {
  const selected = selectedIds();
  if (selected.length > 1)
    return (
      '<section class="card profileDetail">' +
      head(
        selected.length + " profiles selected",
        selected.map((i) => esc(profileName(i))).join(", "),
        '<button type="button" onclick="deleteSelected()" ' +
          (profilesBusy() ? "disabled" : "") +
          ">Delete " +
          selected.length +
          " profiles</button>",
      ) +
      "</section>"
    );
  if (!selected.length)
    return (
      '<section class="card profileDetail">' +
      head("No profile selected", "Select a profile to apply, edit or verify it.") +
      "</section>"
    );
  const i = selected[0],
    p = profiles[i],
    m = profileMeta[i],
    busy = profilesBusy(),
    target = targetGpu(),
    matches = profileMatchesTarget(i),
    where = target === null ? "the target GPU" : "GPU " + target,
    startup = startupChecked(),
    running = runtime && runtime.id === i && runtime.gpu === target,
    tier = tierLabel(i);
  const status = m.valid ? (m.imported ? "Imported" : "Verified") : "Needs verification";
  const applyBlock = !m.valid
    ? row(
        "Verify",
        "This edited profile must be verified before it can be applied.",
        '<button type="button" class="primary" ' + (busy ? "disabled" : "") + ' onclick="verifyProfile(pi)">Verify…</button>',
      )
    : row(
        "Apply",
        running
          ? esc(profileName(i)) + " is running on " + where + "."
          : !matches
            ? applyRefusal(i)
            : "Applies " + esc(profileName(i)) + " to " + where + (startup ? " now and at startup." : " for this session."),
        '<button type="button" class="primary" id="applyProfile" ' +
          (busy || !matches ? "disabled " : "") +
          'title="Apply the single selected profile now. Apply on startup controls whether it also becomes the boot profile." onclick="applyProfile(pi)">' +
          (multiGpu() && target !== null ? "Apply to GPU " + target : "Apply") +
          "</button>",
      );
  return (
    '<section class="card profileDetail">' +
    head(
      esc(p[0]),
      [m.source, tier && tier + " tier", m.gpu === null ? "Unassigned (legacy)" : gpuShort(m.gpu)]
        .filter(Boolean)
        .join(" · "),
      pill(status, m.valid ? "" : "warm"),
    ) +
    '<div class="metrics">' +
    metric("Core voltage", p[1], "mV") +
    metric("Target clock", p[2], "MHz") +
    metric("Effective clock", Number.isFinite(m.meas) ? Math.round(m.meas) : "—", Number.isFinite(m.meas) ? "MHz" : "") +
    '<div class="metric" title="' +
    (canEditMemory(i) ? esc(transferRate(m.mem)) : "") +
    '"><small>Memory offset</small><strong>' +
    (canEditMemory(i) ? (m.mem > 0 ? "+" : "") + m.mem : "—") +
    ' <span class="measure">' +
    (canEditMemory(i) ? "MHz" : "") +
    "</span></strong></div>" +
    '</div><div class="row profActions"><button type="button" onclick="openEditor(\'vf\')">Edit V/F curve</button><button type="button" ' +
    (hasFanCurve(i) ? "" : 'disabled title="No editable fan curve is available for this profile."') +
    ' onclick="openEditor(\'fan\')">Edit fan curve</button>' +
    (m.valid
      ? '<button type="button" ' + (busy ? "disabled" : "") + ' onclick="verifyProfile(pi)">Verify…</button>'
      : "") +
    '<button type="button" id="moreProfileActions" aria-haspopup="menu" aria-expanded="false" onclick="openProfileMenu(event,pi)">More actions</button></div>' +
    row(
      "Silent fan curve",
      hasFanCurve(i)
        ? "Apply also sets the saved fan curve."
        : "This profile has no saved fan curve.",
      switchHtml("Silent fan curve", silentFanCurve, "silentFanCurve=!silentFanCurve;profileNotice=null;render()", busy),
    ) +
    row(
      "Apply on startup",
      target === null
        ? "Choose a target GPU first."
        : startup
          ? "Apply also saves the profile for " + where + " at boot."
          : "Apply changes only the current session.",
      switchHtml("Apply on startup", startup, "toggleStartup()", busy || target === null),
    ) +
    applyBlock +
    (profileNotice
      ? '<p class="profileNotice ' + (profileNotice.tone || "") + '" role="status">' + esc(profileNotice.text) + "</p>"
      : '<p class="profileNotice" role="status"></p>') +
    "</section>"
  );
}
function switchHtml(label, on, action, disabled) {
  return (
    '<button type="button" class="track" role="switch" aria-label="' +
    label +
    '" aria-checked="' +
    on +
    '" ' +
    (disabled ? "disabled " : "") +
    'onclick="' +
    action +
    '"><i></i></button>'
  );
}
function adaptiveCard() {
  const available = adaptiveTiers();
  return (
    '<section class="card">' +
    head(
      "Adaptive for games",
      available.length
        ? "Available tiers · " + available.join(" · ")
        : "Verify a profile and assign a tier to enable Adaptive.",
      '<button type="button" ' + (available.length ? "" : "disabled") + ' onclick="adaptivePicker()">Use Adaptive for a game…</button>',
    ) +
    "</section>"
  );
}

// ---------- Target GPU, Main GPU and Apply on startup ----------
function setProfileTarget(value) {
  profileTarget = value === "" ? null : Number(value);
  if (profileTarget !== null) appState.selectedGpu = profileTarget;
  profileNotice = null;
  render();
  document.getElementById("profileTarget")?.focus({ preventScroll: true });
}
function setMainGpu(checked) {
  const target = targetGpu();
  if (target === null || !bootByGpu[target]) return render();
  mainGpu = checked ? target : null;
  render();
  document.getElementById("mainGpu")?.focus({ preventScroll: true });
}
// Ticking records intent: the next Apply writes the boot entry. Unticking
// clears this GPU's saved boot profile now, through the root service.
function toggleStartup() {
  const target = targetGpu();
  if (target === null) return;
  if (!startupChecked(target)) {
    bootIntent[target] = true;
    return render();
  }
  if (appState.daemon !== "ready")
    return info(
      "Apply on startup",
      "Could not clear the saved boot profile: the PenguinBurner root service is not available. The startup profile is unchanged.",
    );
  delete bootIntent[target];
  delete bootByGpu[target];
  if (mainGpu === target) mainGpu = null;
  postQtStatus();
  render();
}

// ---------- Apply and Restore defaults ----------
function applyRefusal(i) {
  const m = profileMeta[i];
  if (targetGpu() === null) return "Choose a target GPU before applying this profile.";
  if (!m.valid) return "This edited profile must be verified before it can be applied.";
  if (!profileMatchesTarget(i))
    return m.gpu === null
      ? "This legacy profile must be verified on the selected GPU before it can be applied."
      : "This profile belongs to a different GPU. Choose its GPU target.";
  if (silentFanCurve && !hasFanCurve(i)) return "No runtime-ready silent fan curve is available.";
  return "";
}
function applyProfile(i) {
  if (profilesBusy()) return;
  const refusal = applyRefusal(i);
  if (refusal) {
    profileNotice = { text: refusal, tone: "bad" };
    return render();
  }
  const target = targetGpu();
  ensureDaemonReady(
    () =>
      confirmGpuSwitch(target, () => {
        const m = profileMeta[i],
          startup = startupChecked(target),
          fan = silentFanCurve;
        if (m.gpu === null) m.gpu = target; // a legacy profile binds on first apply
        runProfileCommand(
          "Starting profile: " + profileName(i) + "; Autostart: " + (startup ? "Yes" : "No"),
          "Applying through the root service · " + gpuShort(target),
          () => {
            runtime = { gpu: target, id: i, fan, adaptive: false };
            if (startup) bootByGpu[target] = { id: i, fan };
            delete bootIntent[target];
            profileNotice = null;
          },
        );
      }),
    "Applying runtime profile",
  );
}
function confirmGpuSwitch(target, next) {
  if (!runtime || runtime.gpu === target || !multiGpu()) return next();
  showPanel(
    '<h2>Switch managed GPU</h2><p class="sub">Another GPU is currently managed. Switching stops monitoring that GPU. Its applied curve, memory offset, or power limit may remain until you restore it or the driver resets it.</p><p class="sub">Switch to the selected GPU?</p><div class="dialogfoot"><button type="button" onclick="closePanel()">No</button><button type="button" class="primary" id="switchGpu">Switch GPU</button></div>',
  );
  const go = document.getElementById("switchGpu");
  go.onclick = () => {
    closePanel();
    next();
  };
  go.focus();
}
// No confirmation: stock is the safe state. It applies now and at boot and
// leaves Apply on startup ticked.
function restoreDefaults(after, target = targetGpu()) {
  if (profilesBusy() || target === null) return;
  ensureDaemonReady(
    () =>
      runProfileCommand(
        "Restoring GPU to stock now and at boot",
        "The root service keeps running for fan control · " + gpuShort(target),
        () => {
          runtime = { gpu: target, id: "stock", fan: silentFanCurve, adaptive: false };
          bootByGpu[target] = { id: "stock", fan: false };
          bootIntent[target] = true;
          profileNotice = { text: "Restore defaults complete. The GPU runs stock settings now and at boot." };
          after?.(target);
        },
      ),
    "Restoring GPU to stock",
  );
}

// ---------- Delete ----------
function deleteConfirmationText(names, restoresStock, lastAdaptive) {
  const subject = !names.length
    ? "the selected profiles"
    : names.length === 1
      ? "Auto-UV profile " + names[0]
      : names.length + " selected profiles";
  let text = "Delete " + subject + "?";
  if (restoresStock && lastAdaptive)
    text +=
      names.length === 1
        ? " This is the last usable Adaptive Auto-UV profile. Deleting it will restore stock now and at boot."
        : " These are the last usable Adaptive Auto-UV profiles. Deleting them will restore stock now and at boot.";
  else if (restoresStock)
    text += " This profile is currently persisted on startup. Deleting it will restore stock now and at boot.";
  return text;
}
// The startup entries and the running profile are both checked: deleting
// either restores stock on that profile's GPU rather than leaving an
// orphaned curve applied. `gpu` is where the restore runs.
function deleteSafety(ids) {
  const target = targetGpu() ?? appState.selectedGpu,
    selected = new Set(ids),
    running = runtime && runtime.id !== "stock" && selected.has(runtime.id),
    bootGpu = Object.keys(bootByGpu).find((g) => selected.has(bootByGpu[g].id)),
    game = appState.runningGame != null ? games[appState.runningGame] : null,
    adaptive = (runtime && runtime.adaptive) || (game && game.mode === "Adaptive");
  const gpu = running ? runtime.gpu : bootGpu != null ? Number(bootGpu) : runtime ? runtime.gpu : target;
  if (adaptive) {
    const winners = Object.values(tierWinners(target));
    if (winners.some((i) => selected.has(i)) && !winners.some((i) => !selected.has(i)))
      return { restoreStock: true, lastAdaptive: true, gpu: runtime && runtime.adaptive ? runtime.gpu : target };
  }
  return { restoreStock: !!(running || bootGpu != null), lastAdaptive: false, gpu };
}
function markDeleted(ids) {
  ids.forEach((i) => {
    profileMeta[i].deleted = true;
    Object.keys(bootByGpu).forEach((g) => bootByGpu[g].id === i && delete bootByGpu[g]);
  });
  if (runtime && ids.includes(runtime.id)) runtime = null;
  profileSel.clear();
  const label = ids.length === 1 ? "profile" : "profiles";
  return "Deleted " + ids.length + " saved " + label + ".";
}
function deleteSelected(ids = selectedIds()) {
  if (profilesBusy() || !ids.length) return;
  const safety = deleteSafety(ids);
  showPanel(
    "<h2>Delete " +
      (ids.length === 1 ? "profile" : ids.length + " profiles") +
      '</h2><p class="sub">' +
      esc(deleteConfirmationText(ids.map(profileName), safety.restoreStock, safety.lastAdaptive)) +
      "</p>" +
      (ids.length > 1 ? '<ul class="deleteList">' + ids.map((i) => "<li>" + esc(profileName(i)) + "</li>").join("") + "</ul>" : "") +
      '<div class="dialogfoot"><button type="button" id="deleteCancel" onclick="closePanel()">No</button><button type="button" class="danger" id="deleteConfirm">Delete</button></div>',
  );
  document.getElementById("deleteCancel").focus();
  document.getElementById("deleteConfirm").onclick = () => {
    closePanel();
    if (!safety.restoreStock) {
      profileNotice = { text: markDeleted(ids) };
      return render();
    }
    // The profile goes only once stock is restored on its GPU. Declining
    // the root-service install leaves the profile and the GPU unchanged.
    restoreDefaults(
      (gpu) => (profileNotice = { text: markDeleted(ids) + " " + gpuShort(gpu) + " runs stock settings now and at boot." }),
      safety.gpu,
    );
    render();
  };
}

// ---------- Verify ----------
function verifyProfile(i = pi) {
  if (profilesBusy()) return;
  const m = profileMeta[i],
    target = targetGpu();
  if (target === null) return info("Profile verification", "Choose a target GPU before verifying this profile.");
  if (m.gpu !== null && m.gpu !== target)
    return info("Profile verification", "This profile belongs to a different GPU. Choose its GPU target.");
  showPanel(
    '<h2>Verify profile</h2><p class="sub">Verify ' +
      esc(profileName(i)) +
      '. Verification runs on the Auto UV tab with the stability workload, and you can stop it there.</p><div class="fields gap"><label>Verification duration (min)<input id="verifyMinutes" type="number" min="1" max="60" step="1" required value="' +
      defaultVerificationMinutes() +
      '"></label></div><div class="dialogfoot"><button type="button" onclick="closePanel()">Cancel</button><button type="button" class="primary" id="startVerify">Start verification</button></div>',
  );
  const minutes = document.getElementById("verifyMinutes");
  minutes.focus();
  minutes.select?.();
  document.getElementById("startVerify").onclick = () => {
    if (!minutes.reportValidity()) return;
    closePanel();
    // Auto UV checks the root service and runs the verification.
    startProfileVerification(i, Number(minutes.value));
  };
}
// Auto UV calls this when a profile verification ends. A pass makes the
// profile applicable and promotes it to the top of the table.
function profileVerificationFinished(i, passed) {
  const m = profileMeta[i];
  if (!m) return;
  if (passed) {
    m.valid = true;
    if (m.gpu === null) m.gpu = targetGpu() ?? appState.selectedGpu;
    markPreferred(i);
    selectProfiles([i]);
    selAnchor = i;
  }
  if (tab === "Profiles") render();
}

// ---------- Memory offset ----------
function memoryDialog(i = pi) {
  if (!canEditMemory(i)) return info("Edit memory offset", "No editable memory offset is available for this profile.");
  const target = targetGpu();
  if (target === null) return info("Edit memory offset", "Choose this profile's GPU before editing its memory offset.");
  const [lo, hi] = PROFILE_GPU_FACTS[profileMeta[i].gpu ?? target].memory;
  showPanel(
    '<h2>Edit memory offset</h2><p class="sub">' +
      esc(profileName(i)) +
      '. Saving creates an edited copy that needs Verify before Apply.</p><div class="fields gap memoryFields"><label>Memory clock offset (MHz)<input id="memval" type="number" min="' +
      lo +
      '" max="' +
      hi +
      '" step="25" required value="' +
      Math.max(lo, Math.min(hi, profileMeta[i].mem)) +
      '" oninput="memRate.textContent=transferRate(Number(this.value)||0)"></label><output id="memRate">' +
      transferRate(profileMeta[i].mem) +
      '</output></div><p class="sub">' +
      lo +
      " to " +
      hi +
      " MHz on " +
      gpuShort(profileMeta[i].gpu ?? target) +
      '.</p><div class="dialogfoot"><button type="button" onclick="closePanel()">Cancel</button><button type="button" class="primary" id="memSave">Save edited copy</button></div>',
  );
  const field = document.getElementById("memval");
  field.focus();
  document.getElementById("memSave").onclick = () => {
    if (!field.reportValidity()) return;
    const value = Number(field.value);
    closePanel();
    if (value === profileMeta[i].mem) return;
    const j = cloneEdit(i, "User edited");
    profileMeta[j].mem = value;
    profiles[j][0] = editedName(j);
    profileNotice = { text: "Saved an edited memory offset copy. Verify it before Apply." };
    render();
  };
}

// ---------- Export LACT ----------
const LACT_DIRECTORIES = [
  { path: "/etc/lact", gpuId: "10DE:2C02-1462:5311-0000:01:00.0" },
  { path: "~/.config/PenguinBurner", gpuId: "" },
];
function lactYaml(i, gpuId, includeFan) {
  const points = profileCurve("vf", i);
  let yaml = "# PenguinBurner export · " + profileName(i) + "\ngpus:\n  " + gpuId + ":\n    fan_control_enabled: " + includeFan + "\n";
  if (includeFan)
    yaml +=
      "    fan_control_settings:\n      mode: curve\n      curve:\n" +
      profileCurve("fan", i)
        .map(([x, y]) => "        " + x + ": " + (y / 100).toFixed(2))
        .join("\n") +
      "\n";
  return (
    yaml +
    "    gpu_vf_curve:\n" +
    points
      .map(([x, y], n) => "      " + n + ":\n        clockspeed: " + Math.round(y) + "\n        voltage: " + Math.round(x))
      .join("\n")
  );
}
function exportProfile(i = pi, chosen = 0, error = "") {
  if (!profileMeta[i].valid) return;
  const includeFan = silentFanCurve;
  showPanel(
    '<h2>Export LACT</h2><p class="sub">Write ' +
      esc(profileName(i)) +
      " to a LACT config.yaml. The silent fan curve is " +
      (includeFan ? "included" : "not included") +
      ', following the Silent fan curve switch.</p><div class="fields gap"><label>Export directory<select id="lactDir">' +
      LACT_DIRECTORIES.map(
        (d, n) => '<option value="' + n + '"' + (n === chosen ? " selected" : "") + ">" + esc(d.path) + "</option>",
      ).join("") +
      "</select></label></div>" +
      (error ? '<p class="dialogError" role="alert">' + esc(error) + "</p>" : "") +
      '<div class="dialogfoot"><button type="button" onclick="closePanel()">Cancel</button><button type="button" class="primary" id="lactGo">Export</button></div>',
  );
  document.getElementById("lactDir").focus();
  document.getElementById("lactGo").onclick = () => {
    const n = Number(document.getElementById("lactDir").value),
      dir = LACT_DIRECTORIES[n];
    if (!dir.gpuId)
      return exportProfile(
        i,
        n,
        "Could not detect the LACT GPU id. Start LACT once, or choose a directory that already contains config.yaml.",
      );
    if (includeFan && !hasFanCurve(i))
      return exportProfile(i, n, "No runtime-ready silent fan curve is available for this profile.");
    const yaml = lactYaml(i, dir.gpuId, includeFan),
      written = dir.path + "/config.yaml";
    showPanel(
      '<h2>Export LACT</h2><p class="sub">LACT profile successfully written: <code>' +
        esc(written) +
        "</code></p>" +
        (includeFan
          ? '<p class="sub">Warnings: fan auto_threshold is disabled because no zero-RPM temp was saved.</p>'
          : "") +
        '<pre class="lactPreview">' +
        esc(yaml) +
        '</pre><div class="dialogfoot"><button type="button" id="downloadDemo">Download sample YAML</button><button type="button" class="primary" onclick="closePanel()">Close</button></div>',
    );
    profileNotice = { text: "LACT profile written: " + written };
    document.getElementById("downloadDemo").onclick = () => {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([yaml], { type: "text/yaml" }));
      a.download = "sample-lact-config.yaml";
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    };
    render();
  };
}

// ---------- Curves and edited copies ----------
function profileCurve(kind, i) {
  const m = profileMeta[i];
  if (kind === "fan") return structuredClone(m.fan || (hasFanCurve(i) ? DEFAULT_FAN_CURVE : []));
  return structuredClone(m.vf || sample.rows.filter((r) => r.tier === tiers[m.origin]).at(-1).points);
}
// Fan readings recorded during the scan: temperature (°C) against fan (%).
function profileFanMeasurements(i) {
  const m = profileMeta[i];
  if (m.source === "MSI Afterburner" || !hasFanCurve(i)) return [];
  const row = sample.rows.filter((r) => r.tier === tiers[m.origin]).at(-1),
    curve = profileCurve("fan", i);
  const fanAt = (t) => {
    const k = Math.max(1, curve.findIndex((p) => p[0] >= t));
    const [a, b] = [curve[Math.min(k, curve.length - 1) - 1], curve[Math.min(k, curve.length - 1)]];
    return a[1] + ((b[1] - a[1]) * (t - a[0])) / Math.max(1, b[0] - a[0]);
  };
  return Array.from({ length: 28 }, (_, n) => {
    const t = row.temp - 14 + n * 0.75 + Math.sin(n * 2.3) * 1.2;
    return [Math.round(t * 10) / 10, Math.round((fanAt(t) + Math.sin(n * 1.7) * 2.4) * 10) / 10];
  });
}
function editedName(j) {
  const p = profiles[j],
    mem = profileMeta[j].mem;
  return "User edited " + p[2] + " MHz " + p[1] + " mV" + (mem ? " mem " + signedMemoryClock(mem) : "");
}
// Edits never touch the original: they save a user-edited copy. A fan-only
// copy keeps its parent's verification; V/F and memory copies need Verify.
function cloneEdit(i, source, keepVerification = false) {
  const j = profiles.length;
  profiles.push([...profiles[i]]);
  profileMeta.push({
    ...structuredClone(profileMeta[i]),
    copy: true,
    imported: false,
    valid: keepVerification && profileMeta[i].valid,
    source,
    assigned: 0,
    saved: Date.now(),
    base: profileMeta[i].base,
  });
  profiles[j][0] = editedName(j);
  order.unshift(j);
  markPreferred(j);
  selectProfiles([j]);
  selAnchor = j;
  return j;
}
function saveEditedCurve(kind, i, points, controls) {
  const j = cloneEdit(i, "User edited", kind === "fan");
  profileMeta[j][kind] = structuredClone(points);
  if (kind === "vf") {
    if (controls) profileMeta[j].controls = [...controls];
    profiles[j][2] = Math.round(
      points.reduce((a, b) => (Math.abs(b[0] - profiles[j][1]) < Math.abs(a[0] - profiles[j][1]) ? b : a))[1],
    );
    profiles[j][0] = editedName(j);
    profileNotice = { text: "Saved an edited curve copy. Verify it before Apply." };
  } else {
    profiles[j][0] = "User edited fan curve · " + profileName(i);
    profileNotice = { text: "Saved an edited fan curve copy. It keeps the profile's verification." };
  }
  render();
  return j;
}

// ---------- Curve editors ----------
// V/F: fixed voltage bins. Up/Down change clock, Left/Right move the
// selected target to the next voltage bin, Tab walks bins, Shift+Right
// selects a range, Ctrl+L flattens from the selection, Ctrl+Click adds a
// control point and dragging moves the target in both axes.
// Fan: points on a fixed 30–95 °C × 0–100 % plot with a measured trace.
const EDITOR_SHORTCUTS = {
  vf: [
    ["Click", "select bin"],
    ["Ctrl+Click", "new point"],
    ["Drag", "move selected bin"],
    ["Up/Down", "clock"],
    ["Left/Right", "voltage"],
    ["Tab / Shift+Tab", "next / previous bin"],
    ["Shift+Right", "select range"],
    ["Ctrl+L", "flatten here"],
    ["Ctrl+Z / Ctrl+Y", "undo / redo"],
  ],
  fan: [
    ["Click", "select dot"],
    ["Ctrl+Click", "new point"],
    ["Drag", "move dot"],
    ["Up/Down", "fan speed"],
    ["Left/Right", "temperature"],
    ["Tab / Shift+Tab", "next / previous"],
    ["Ctrl+Z / Ctrl+Y", "undo / redo"],
  ],
};
function newEditor(kind, i) {
  const points = profileCurve(kind, i).map(([x, y]) => [Math.round(x), Math.round(y)]);
  const fan = kind === "fan",
    anchor = profiles[i][1];
  let index = fan
    ? Math.floor(points.length / 2)
    : points.reduce((best, p, n) => (Math.abs(p[0] - anchor) < Math.abs(points[best][0] - anchor) ? n : best), 0);
  const clocks = [...points, ...(fan ? [] : sample.base)].map((p) => p[1]);
  return {
    kind,
    id: i,
    points,
    before: structuredClone(points),
    reference: fan ? null : sample.base,
    measured: fan ? profileFanMeasurements(i) : null,
    controls: new Set(profileMeta[i].controls || []),
    index,
    range: null,
    undo: [],
    redo: [],
    legend: false,
    xRange: fan ? [30, 95] : [points[0][0], points.at(-1)[0]],
    yRange: fan
      ? [0, 100]
      : [Math.floor((Math.min(...clocks) - 150) / 200) * 200, Math.ceil((Math.max(...clocks) + 150) / 200) * 200],
  };
}
function editorSnapshot(e) {
  return { points: structuredClone(e.points), index: e.index, range: e.range && [...e.range], controls: [...e.controls] };
}
function editorStash(e = editor) {
  e.undo.push(editorSnapshot(e));
  e.redo = [];
}
// Restoring fewer points (Revert after Ctrl+Click added one) keeps the
// selection inside the curve.
function editorRestore(e, s) {
  const last = s.points.length - 1;
  e.points = s.points;
  e.index = Math.max(0, Math.min(s.index, last));
  e.range = s.range && s.range.every((j) => j <= last) ? s.range : null;
  e.controls = new Set(s.controls);
}
function editorRevert(e = editor) {
  editorStash(e);
  editorRestore(e, {
    points: structuredClone(e.before),
    index: e.index,
    range: null,
    controls: profileMeta[e.id].controls || [],
  });
}
function editorUndo(e = editor) {
  if (!e.undo.length) return;
  e.redo.push(editorSnapshot(e));
  editorRestore(e, e.undo.pop());
}
function editorRedo(e = editor) {
  if (!e.redo.length) return;
  e.undo.push(editorSnapshot(e));
  editorRestore(e, e.redo.pop());
}
// Set one V/F bin and keep the curve monotonic: lower bins never exceed
// it and higher bins never fall below it.
function vfSet(e, j, clock) {
  const c = Math.round(Math.max(e.yRange[0], Math.min(e.yRange[1], clock)));
  e.points[j][1] = c;
  for (let k = 0; k < j; k++) e.points[k][1] = Math.min(e.points[k][1], c);
  for (let k = j + 1; k < e.points.length; k++) e.points[k][1] = Math.max(e.points[k][1], c);
}
function fanSet(e, j, temp, speed) {
  const prev = e.points[j - 1],
    next = e.points[j + 1];
  const t = Math.round(Math.max(prev ? prev[0] + 1 : 30, Math.min(next ? next[0] - 1 : 95, temp)));
  const s = Math.round(Math.max(prev ? prev[1] : 0, Math.min(next ? next[1] : 100, speed)));
  e.points[j] = [t, s];
}
function editorRangeIds(e) {
  if (!e.range) return [e.index];
  const [a, b] = e.range;
  return Array.from({ length: Math.abs(b - a) + 1 }, (_, n) => Math.min(a, b) + n);
}
function editorNudgeY(e, dy) {
  editorStash(e);
  if (e.kind === "fan") return fanSet(e, e.index, e.points[e.index][0], e.points[e.index][1] + dy);
  // A range moves together, then the bins around it stay monotonic.
  const ids = editorRangeIds(e),
    [lo, hi] = [ids[0], ids.at(-1)];
  for (const j of ids) e.points[j][1] = Math.round(Math.max(e.yRange[0], Math.min(e.yRange[1], e.points[j][1] + dy)));
  for (let k = 0; k < lo; k++) e.points[k][1] = Math.min(e.points[k][1], e.points[lo][1]);
  for (let k = hi + 1; k < e.points.length; k++) e.points[k][1] = Math.max(e.points[k][1], e.points[hi][1]);
}
function editorNudgeX(e, dx) {
  editorStash(e);
  if (e.kind === "fan") return fanSet(e, e.index, e.points[e.index][0] + dx, e.points[e.index][1]);
  const j = Math.max(0, Math.min(e.points.length - 1, e.index + dx));
  if (j === e.index) return;
  const clock = e.points[e.index][1];
  if (e.controls.delete(e.points[e.index][0])) e.controls.add(e.points[j][0]);
  e.index = j;
  e.range = null;
  vfSet(e, j, clock);
}
function editorFlatten(e) {
  editorStash(e);
  const c = e.points[e.index][1];
  for (let k = e.index + 1; k < e.points.length; k++) e.points[k][1] = c;
}
function editorExtendRange(e) {
  const [a, b] = e.range || [e.index, e.index];
  e.range = [a, Math.min(e.points.length - 1, b + 1)];
}
function editorAddPoint(e, x, y) {
  editorStash(e);
  if (e.kind === "fan") {
    const t = Math.round(Math.max(30, Math.min(95, x)));
    if (e.points.some((p) => p[0] === t)) return;
    let k = e.points.findIndex((p) => p[0] > t);
    if (k < 0) k = e.points.length;
    e.points.splice(k, 0, [t, 0]);
    e.index = k;
    fanSet(e, k, t, y);
  } else {
    const j = e.points.reduce((best, p, n) => (Math.abs(p[0] - x) < Math.abs(e.points[best][0] - x) ? n : best), 0);
    e.index = j;
    e.range = null;
    e.controls.add(e.points[j][0]);
    vfSet(e, j, y);
  }
}
function editorDirty(e = editor) {
  return JSON.stringify(e.points) !== JSON.stringify(e.before) || [...e.controls].join() !== (profileMeta[e.id].controls || []).join();
}
function openEditor(kind, i = pi) {
  if (kind === "fan" && !hasFanCurve(i)) return info("Edit fan curve", "No editable fan curve is available for this profile.");
  if (window.pbQtPreview) {
    console.info("pb-qt-preview-edit-" + kind + ":" + i);
    return;
  }
  editor = newEditor(kind, i);
  editorDialog();
}
function editorDialog() {
  const e = editor,
    fan = e.kind === "fan",
    t = chartTheme(),
    rows = EDITOR_SHORTCUTS[e.kind];
  showPanel(
    '<div class="curveEditor"><h2 id="editorTitle" tabindex="-1">Edit ' +
      (fan ? "fan" : "V/F") +
      ' curve</h2><p class="sub">' +
      esc(profileName(e.id)) +
      " · " +
      (fan ? "Saving creates a copy that keeps this profile's verification." : "Saved edits require Verify before Apply.") +
      '</p><div class="editorPlot"><canvas class="editplot" id="editplot" tabindex="0" aria-describedby="editorStatus" aria-label="' +
      (fan ? "Fan curve editor" : "V/F curve editor") +
      '. Arrow keys move the selected point."></canvas><div class="shortcutLegend" id="shortcutLegend"' +
      (e.legend ? "" : " hidden") +
      '><dl>' +
      rows.map(([k, v]) => "<dt><kbd>" + k + "</kbd></dt><dd>" + v + "</dd>").join("") +
      '</dl></div></div><div class="legend editorLegend">' +
      (fan
        ? markerLegendItem("Measured", t.quiet, "scatter")
        : legendItem("Reference", design.colors.baseCurve, { dash: true })) +
      legendItem("Before edit", design.colors.curveHistory) +
      legendItem("Edited draft", design.colors.accent) +
      markerLegendItem("Selected", design.colors.accent, "handle") +
      (fan ? "" : markerLegendItem("Control point", t.ink, "ring")) +
      '</div><div class="editorBar"><div class="editorFields">' +
      (fan
        ? '<label>Temperature (°C)<input id="pointX" type="number" min="30" max="95" step="1" onchange="editorField(\'x\',this.value)"></label><label>Fan speed (%)<input id="pointY" type="number" min="0" max="100" step="1" onchange="editorField(\'y\',this.value)"></label>'
        : '<label>Voltage<output id="pointX"></output></label><label>Clock (MHz)<input id="pointY" type="number" step="15" min="' +
          e.yRange[0] +
          '" max="' +
          e.yRange[1] +
          '" onchange="editorField(\'y\',this.value)"></label><button type="button" onclick="editorFlatten(editor);drawEditor()" title="Ctrl+L">Flatten from here</button>') +
      '</div><div class="editorButtons"><button type="button" onclick="editorUndo();drawEditor()" title="Undo last edit (Ctrl+Z)">Undo</button><button type="button" onclick="editorRedo();drawEditor()" title="Redo last undone edit (Ctrl+Y)">Redo</button><button type="button" onclick="editorRevert();drawEditor()">Revert</button><button type="button" id="legendToggle" aria-pressed="' +
      e.legend +
      '" onclick="toggleShortcutLegend()">Shortcuts</button></div></div><p class="editorStatus" id="editorStatus" role="status"></p><div class="dialogfoot"><button type="button" onclick="closePanel()">Cancel</button><button type="button" class="primary" id="editorSave">Save edited copy</button></div></div>',
  );
  document.getElementById("editorSave").onclick = () => {
    const ed = editor;
    closePanel();
    if (editorDirty(ed)) saveEditedCurve(ed.kind, ed.id, ed.points, ed.kind === "vf" ? [...ed.controls] : undefined);
  };
  document.getElementById("editorTitle").focus({ preventScroll: true });
  requestAnimationFrame(bindEditor);
}
// Point markers for the external legend (the line swatches come from charts.js).
function markerLegendItem(label, color, kind) {
  return (
    '<span class="legendItem"><i class="marker ' +
    kind +
    '" style="--swatch:' +
    color +
    '" aria-hidden="true"></i>' +
    label +
    "</span>"
  );
}
function toggleShortcutLegend() {
  editor.legend = !editor.legend;
  document.getElementById("shortcutLegend").hidden = !editor.legend;
  document.getElementById("legendToggle").setAttribute("aria-pressed", editor.legend);
}
function editorField(axis, value) {
  const e = editor,
    v = Number(value);
  if (!Number.isFinite(v)) return drawEditor();
  editorStash(e);
  if (e.kind === "fan")
    fanSet(e, e.index, axis === "x" ? v : e.points[e.index][0], axis === "y" ? v : e.points[e.index][1]);
  else vfSet(e, e.index, v);
  drawEditor();
}
function editorGeometry(w, h, e = editor) {
  const box = { left: 64, right: w - 20, top: 16, bottom: h - 46 },
    [x0, x1] = e.xRange,
    [y0, y1] = e.yRange;
  return {
    box,
    x: (v) => box.left + ((box.right - box.left) * (v - x0)) / (x1 - x0),
    y: (v) => box.bottom - ((box.bottom - box.top) * (v - y0)) / (y1 - y0),
    ix: (px) => x0 + ((px - box.left) * (x1 - x0)) / (box.right - box.left),
    iy: (py) => y0 + ((box.bottom - py) * (y1 - y0)) / (box.bottom - box.top),
  };
}
function drawEditor() {
  const c = document.getElementById("editplot");
  if (!c || !editor) return;
  const e = editor,
    fan = e.kind === "fan",
    t = chartTheme(),
    colors = design.colors,
    { ctx, w, h } = canvasContext(c),
    z = editorGeometry(w, h);
  drawChartFrame(ctx, w, h, z.box, z.x, z.y, {
    x: fan
      ? { lo: 30, hi: 95, minor: 5, major: 10, title: "Temperature (°C)" }
      : { lo: e.xRange[0], hi: e.xRange[1], minor: 10, major: 50, title: "Voltage (mV)" },
    y: fan
      ? { lo: 0, hi: 100, minor: 10, major: 20, title: "Fan speed (%)" }
      : { lo: e.yRange[0], hi: e.yRange[1], minor: 100, major: 200, title: "Clock (MHz)" },
  });
  ctx.save();
  ctx.beginPath();
  ctx.rect(z.box.left, z.box.top - 6, z.box.right - z.box.left + 6, z.box.bottom - z.box.top + 6);
  ctx.clip();
  const line = (points, color, width, dash = []) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash);
    strokePath(
      ctx,
      points.map(([x, y]) => [z.x(x), z.y(y)]),
    );
    ctx.setLineDash([]);
  };
  const dot = (x, y, r, fill, stroke) => {
    ctx.beginPath();
    ctx.arc(z.x(x), z.y(y), r, 0, 2 * Math.PI);
    if (fill) {
      ctx.fillStyle = fill;
      ctx.fill();
    }
    if (stroke) {
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
  };
  if (e.reference) line(e.reference, colors.baseCurve, 1.5, [6, 4]);
  if (e.measured) e.measured.forEach(([x, y]) => dot(x, y, 2.5, withAlpha(t.quiet, 0.8)));
  line(e.before, colors.curveHistory, 1.5);
  line(e.points, colors.accent, 2.2);
  const range = new Set(editorRangeIds(e));
  e.points.forEach(([x, y], n) => {
    if (fan) dot(x, y, n === e.index ? 0 : 3.5, colors.accent, colors.plotSurface);
    else if (range.has(n) && n !== e.index) dot(x, y, 4, colors.accent, colors.plotSurface);
    else if (n !== e.index) dot(x, y, 1.8, withAlpha(colors.accent, 0.7));
    if (!fan && e.controls.has(x)) dot(x, y, 5.5, null, t.ink);
  });
  ctx.restore();
  // The selected point: neutral crosshair, a ringed handle and axis badges.
  const [sx, sy] = e.points[e.index],
    px = z.x(sx),
    py = z.y(sy);
  ctx.lineWidth = 1;
  ctx.strokeStyle = t.crosshair;
  strokePath(ctx, [
    [Math.round(px) + 0.5, z.box.top],
    [Math.round(px) + 0.5, z.box.bottom],
  ]);
  strokePath(ctx, [
    [z.box.left, Math.round(py) + 0.5],
    [z.box.right, Math.round(py) + 0.5],
  ]);
  ctx.beginPath();
  ctx.arc(px, py, 6, 0, 2 * Math.PI);
  ctx.fillStyle = colors.ink;
  ctx.fill();
  ctx.lineWidth = 2;
  ctx.strokeStyle = colors.plotSurface;
  ctx.stroke();
  ctx.beginPath();
  ctx.arc(px, py, 9, 0, 2 * Math.PI);
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = colors.accent;
  ctx.stroke();
  drawBadge(ctx, sy + (fan ? " %" : " MHz"), z.box.left + 4, py, "left");
  drawBadge(ctx, sx + (fan ? " °C" : " mV"), px, z.box.bottom - 12);
  const xField = document.getElementById("pointX"),
    yField = document.getElementById("pointY");
  if (xField) fan ? (xField.value = sx) : (xField.textContent = sx + " mV");
  if (yField) yField.value = sy;
  const ids = editorRangeIds(e);
  document.getElementById("editorStatus").textContent =
    (ids.length > 1
      ? "Range " + e.points[ids[0]][0] + "–" + e.points[ids.at(-1)][0] + " mV selected. Up/Down shifts it."
      : (fan ? "Point " : "Bin ") + (e.index + 1) + " of " + e.points.length + " · " + sx + (fan ? " °C · " + sy + " %" : " mV · " + sy + " MHz")) +
    (editorDirty(e) ? " · Edited" : "");
}
function bindEditor() {
  const c = document.getElementById("editplot");
  if (!c) return;
  drawEditor();
  let drag = null;
  const pick = (ev) => {
    const z = editorGeometry(c.clientWidth, c.clientHeight);
    let best = 0,
      bestD = Infinity;
    editor.points.forEach(([x, y], n) => {
      const d = editor.kind === "fan" ? Math.hypot(z.x(x) - ev.offsetX, z.y(y) - ev.offsetY) : Math.abs(z.x(x) - ev.offsetX);
      if (d < bestD) [best, bestD] = [n, d];
    });
    return { z, best };
  };
  c.onpointerdown = (ev) => {
    const { z, best } = pick(ev);
    c.focus({ preventScroll: true });
    if (ev.ctrlKey || ev.metaKey) {
      editorAddPoint(editor, z.ix(ev.offsetX), z.iy(ev.offsetY));
      return drawEditor();
    }
    editor.index = best;
    editor.range = null;
    editorStash();
    drag = { start: structuredClone(editor.points), from: best, controls: new Set(editor.controls) };
    c.setPointerCapture(ev.pointerId);
    drawEditor();
  };
  c.onpointermove = (ev) => {
    if (!drag || !c.hasPointerCapture(ev.pointerId)) return;
    const z = editorGeometry(c.clientWidth, c.clientHeight),
      e = editor;
    e.points = structuredClone(drag.start);
    if (e.kind === "fan") fanSet(e, e.index, z.ix(ev.offsetX), z.iy(ev.offsetY));
    else {
      const j = pick(ev).best;
      e.controls = new Set(drag.controls);
      if (j !== drag.from && e.controls.delete(e.points[drag.from][0])) e.controls.add(e.points[j][0]);
      e.index = j;
      vfSet(e, j, z.iy(ev.offsetY));
    }
    drawEditor();
  };
  c.onpointerup = c.onpointercancel = () => {
    if (drag && JSON.stringify(drag.start) === JSON.stringify(editor.points)) editor.undo.pop();
    drag = null;
  };
  c.onkeydown = (ev) => {
    const e = editor,
      k = ev.key,
      mod = ev.ctrlKey || ev.metaKey;
    let handled = true;
    if (k === "ArrowUp" || k === "ArrowDown") editorNudgeY(e, (k === "ArrowUp" ? 1 : -1) * (e.kind === "fan" ? 1 : 15));
    else if (k === "ArrowRight" && ev.shiftKey && e.kind === "vf") editorExtendRange(e);
    else if (k === "ArrowLeft" || k === "ArrowRight") editorNudgeX(e, k === "ArrowLeft" ? -1 : 1);
    else if (k === "Tab") {
      const j = e.index + (ev.shiftKey ? -1 : 1);
      if (j < 0 || j >= e.points.length) return; // let focus leave the plot
      e.index = j;
      e.range = null;
    } else if (mod && k.toLowerCase() === "z") editorUndo(e);
    else if (mod && k.toLowerCase() === "y") editorRedo(e);
    else if (mod && k.toLowerCase() === "l" && e.kind === "vf") editorFlatten(e);
    else if (k === "?") toggleShortcutLegend();
    else if (k === "Escape" && e.range) e.range = null;
    else handled = false;
    if (handled) {
      ev.preventDefault();
      drawEditor();
    }
  };
}

// ---------- Adaptive for a game ----------
function adaptivePicker() {
  showPanel(
    '<h2>Use Adaptive for a game</h2><p class="sub">Choose a game and the target FPS before frame generation. Adaptive uses the verified tiers on its GPU.</p><div class="fields gap"><label>Game<select id="adaptiveGame" onchange="document.getElementById(\'adaptiveFps\').value=games[Number(this.value)].target">' +
      games
        .map((x, n) => '<option value="' + n + '" ' + (n === idx ? "selected" : "") + ">" + esc(x.name) + "</option>")
        .join("") +
      '</select></label><label>Target FPS<input id="adaptiveFps" type="number" min="1" max="500" required value="' +
      g().target +
      '"></label></div><div class="dialogfoot"><button type="button" onclick="closePanel()">Cancel</button><button type="button" class="primary" onclick="applyGameAdaptive()">Apply to game</button></div>',
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
