// Game Perf Profile (PR #137) as a Game Library drill-down: deterministic
// sample frame rings, the library entry with its hover peek, the full view,
// and its fingerprint and frametime charts. Shared chart helpers
// (canvasContext, strokePath, chartTheme, drawChartFrame, drawBadge,
// legendItem, withAlpha) live in charts.js.

// ---------- Constants (PR #137 game_perf_profile_panel.py) ----------
const PERF = {
  refreshMs: 2000,
  liveRefreshMs: 1000,
  columns: 300,
  minColumnFrames: 200,
  stutterRatio: 1.6,
  mildMs: 16.7,
  severeMs: 33.3,
  hitchRatio: 2,
  // Overview dots grade from warning at the hitch threshold to severe here.
  hitchWorstMs: 100,
  qualifyMinutes: 5,
  ringMinutes: 30,
  fallbackPowerW: 350,
  fallbackTargetFps: 60,
  windows: [10, 30, 60],
};
const PERF_OVERVIEW_CAPTION =
  "line: median frametime · band: up to the 1% worst frames · dots: hitches over 33 ms";
const PERF_LIVE_CAPTION =
  "flat & low = smooth · stutter needles yellow -> red by severity (red ~30 ms) · lines: median & 1%-low (p99)";
const PERF_EMPTY_HINT =
  "Select a game in the Game Library tab and click its Perf Profile button.\nTelemetry is recorded while a game's PenguinBurner profile runs.";
const PERF_UNAVAILABLE =
  "Game Perf Profile unavailable — no telemetry recorded for this game yet. Wrap the game, pick a mode other than Stock, keep Capture telemetry on, and play it.";
const PERF_TIERS = ["Efficiency", "Balanced", "Performance"];
const PERF_POWER_LIMITS = { 0: 360, 1: 200 };

// ---------- Pure helpers ----------
const perfClamp01 = (v) => Math.min(1, Math.max(0, v));
const perfSum = (values) => values.reduce((a, b) => a + b, 0);

// Floor-indexed percentile of a sorted array, as runtime/frame_history.py.
function perfPercentile(sorted, fraction) {
  return sorted[Math.min(sorted.length - 1, Math.floor(fraction * sorted.length))];
}

// Bucket a frame ring into overview columns of at least 200 frames:
// {x: minutes relative to now (newest = 0), p50, p99, max}.
function densifyFrametimes(frames, columns = PERF.columns) {
  if (!frames.length) return [];
  const total = perfSum(frames),
    bucket = Math.max(1, Math.floor(frames.length / columns), PERF.minColumnFrames),
    out = [];
  let elapsed = 0;
  for (let start = 0; start < frames.length; start += bucket) {
    const chunk = Float64Array.from(frames.slice(start, start + bucket)).sort();
    elapsed += perfSum(chunk);
    out.push({
      x: (elapsed - total) / 60000,
      p50: chunk[chunk.length >> 1],
      p99: chunk[Math.min(chunk.length - 1, Math.floor(chunk.length * 0.99))],
      max: chunk[chunk.length - 1],
    });
  }
  return out;
}

// A column hitches when its worst frame is over 33 ms and twice its own
// median, so a 60 fps cap in a faster game is not one long stutter.
function hitchPoints(cols) {
  return cols.filter(
    (c) => c.max >= Math.max(PERF.severeMs, c.p50 * PERF.hitchRatio),
  );
}

// The last window at full resolution: x is the frame's start in seconds ago.
function liveTail(frames, windowMs) {
  const xs = [],
    values = [];
  let elapsed = 0;
  for (let i = frames.length - 1; i >= 0; i--) {
    elapsed += frames[i];
    if (elapsed > windowMs && values.length) break;
    xs.push(-elapsed / 1000);
    values.push(frames[i]);
  }
  return { xs: xs.reverse(), values: values.reverse() };
}

function frametimeCeiling(p50, p99) {
  return Math.ceil(Math.max(20, p50 * 2.4, p99 * 1.85) / 5) * 5;
}
function frametimeStep(ceiling) {
  return ceiling <= 25 ? 5 : ceiling <= 50 ? 10 : 20;
}
// Unsigned "minutes ago" ticks: the smallest round step giving six or fewer.
function overviewStep(spanMinutes) {
  return (
    [0.1, 0.2, 0.5, 1, 2, 5, 10, 15].find((s) => spanMinutes / s <= 6) || 15
  );
}
function liveStep(windowS) {
  return windowS <= 10 ? 2 : windowS <= 30 ? 5 : 10;
}

function mixHex(a, b, t) {
  const pa = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16)),
    pb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  return (
    "#" +
    pa
      .map((v, i) =>
        Math.round(v + (pb[i] - v) * perfClamp01(t))
          .toString(16)
          .padStart(2, "0"),
      )
      .join("")
      .toUpperCase()
  );
}
// Live needles: warning at 16.7 ms to severe at 33.3 ms, from the true value.
function stutterColor(ms) {
  const c = design.colors;
  return mixHex(
    c.warning,
    c.severe,
    (ms - PERF.mildMs) / (PERF.severeMs - PERF.mildMs),
  );
}
// Overview hitch dots (all over 33 ms): graded on to 100 ms.
function hitchColor(ms) {
  const c = design.colors;
  return mixHex(
    c.warning,
    c.severe,
    (ms - PERF.severeMs) / (PERF.hitchWorstMs - PERF.severeMs),
  );
}

function clippedCaption(caption, worstMs, ceilingMs) {
  return worstMs > ceilingMs
    ? caption + " · worst frame " + Math.round(worstMs) + " ms (clipped)"
    : caption;
}

// Floor, not round: 4.6 min must not read "5 of 5" while the gate is shut.
function warmingText(minutes) {
  return (
    "Warming up · " +
    Math.floor(minutes) +
    " of " +
    PERF.qualifyMinutes +
    " min captured"
  );
}

function latencyText(latency) {
  return (latency.estimated ? "~" : "") + Math.round(latency.ms) + " ms";
}

function perfMetaParts(rec, summary) {
  const parts = [];
  if (rec.live) parts.push("● REC");
  parts.push(
    (summary.minutes < 10
      ? summary.minutes.toFixed(1)
      : Math.round(summary.minutes).toString()) + " min captured",
  );
  parts.push(summary.frames.toLocaleString("en-US") + " frames");
  if (!summary.qualified)
    parts.push("settling · fingerprint at " + PERF.qualifyMinutes + " min");
  if (summary.framegen) parts.push("frame-gen seen");
  return parts;
}
function perfMetaText(rec, summary) {
  return perfMetaParts(rec, summary).join(" · ");
}

function perfTierLabel(tier) {
  return PERF_TIERS.includes(tier) ? tier : "Untuned";
}
function perfTierColor(tier) {
  return PERF_TIERS.includes(tier)
    ? design.colors[tier.toLowerCase()]
    : design.colors.quiet;
}

// The five spokes, each against its own scale (PR #137 perf_profile_axes).
function perfSpokes(summary, targetFps, powerLimitW) {
  const limit = powerLimitW > 0 ? powerLimitW : PERF.fallbackPowerW,
    target = targetFps > 0 ? targetFps : PERF.fallbackTargetFps,
    hw = summary.hw,
    ratio = summary.fps > 0 ? summary.low / summary.fps : 0,
    pct = Math.round(ratio * 100) + "%";
  return [
    {
      code: "PWR",
      arrow: "↓",
      label: "Power draw",
      text: hw.power + " W",
      fraction: perfClamp01(hw.power / limit),
      band: [0, 0.6],
    },
    {
      code: "GPU",
      arrow: "",
      label: "GPU load",
      text: hw.gpu + "%",
      fraction: perfClamp01(hw.gpu / 100),
      band: null,
    },
    {
      code: "FPS",
      arrow: "↑",
      label: "Median FPS",
      text: Math.round(summary.fps) + " fps",
      fraction: perfClamp01(summary.fps / target),
      band: [0.95, 1],
    },
    {
      code: "LOW",
      arrow: "↑",
      label: "Fluency floor",
      text: pct + " of median",
      spoke: pct,
      fraction: perfClamp01(ratio),
      band: [0.85, 1],
    },
    {
      code: "CPU",
      arrow: "↓",
      label: "CPU hot thread",
      text: hw.cpu + "%",
      fraction: perfClamp01(hw.cpu / 100),
      band: [0, 0.8],
    },
  ];
}
// true inside the spoke's fine band, false outside, null without one.
function axisInBand(axis) {
  return axis.band
    ? axis.fraction >= axis.band[0] && axis.fraction <= axis.band[1]
    : null;
}

// ---------- Deterministic sample recordings ----------
function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function perfSeed(text) {
  let h = 2166136261;
  for (const ch of String(text)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return h >>> 0;
}

// fps/lowFps set the body and its 1% tail; hitches are shader or traversal
// stalls; menu is a [startS, lengthS, cappedFps] stretch; stutterEvery adds
// mild 20-30 ms frames that only the Live view singles out.
const PERF_SAMPLES = {
  // Long recording with real hitches and one clipped stall.
  1091500: {
    mode: "Balanced",
    tier: "Balanced",
    minutes: 24,
    fps: 87,
    lowFps: 61,
    drift: 0.05,
    hitchEvery: 88,
    hitchMs: [35, 96],
    bigHitch: [610, 182],
    stutterEvery: 9,
    framegen: true,
    latency: { ms: 28, estimated: false },
    hw: { power: 224, clock: 2640, gpu: 96, cpu: 72, temp: 64, fan: 42 },
  },
  // Short archived session: graph and stats show, the fingerprint waits.
  2531310: {
    mode: "Efficiency",
    tier: "Efficiency",
    minutes: 3.6,
    fps: 72,
    lowFps: 54,
    drift: 0.04,
    hitchEvery: 70,
    hitchMs: [38, 58],
    earlyHitches: [4, 9, 15, 31],
    stutterEvery: 14,
    hw: { power: 168, clock: 2385, gpu: 91, cpu: 64, temp: 58, fan: 34 },
  },
  // A fast game with a 60 fps capped menu stretch; #history-live replays it
  // as a session that is still recording.
  782330: {
    mode: "Performance",
    tier: "Performance",
    minutes: 7.4,
    fps: 158,
    lowFps: 118,
    drift: 0.06,
    menu: [150, 45, 60],
    hitchEvery: 160,
    hitchMs: [34, 46],
    stutterEvery: 6,
    latency: { ms: 22, estimated: true },
    hw: { power: 288, clock: 2925, gpu: 98, cpu: 58, temp: 69, fan: 55 },
  },
};
const PERF_TIER_POWER = { Efficiency: 172, Balanced: 224, Performance: 292 };

// A fresh recording for a game the user starts in the prototype.
function perfFreshSpec(x) {
  const seed = perfSeed(x.id),
    tier =
      x.mode === "Adaptive"
        ? "Balanced"
        : PERF_TIERS.includes(x.mode)
          ? x.mode
          : null,
    fps = 70 + (seed % 70);
  return {
    tier,
    minutes: 0,
    fps,
    lowFps: Math.round(fps * 0.72),
    drift: 0.05,
    hitchEvery: 45,
    hitchMs: [36, 60],
    stutterEvery: 8,
    hw: {
      power: PERF_TIER_POWER[tier] || 250,
      clock: 2500 + (seed % 300),
      gpu: 88 + (seed % 11),
      cpu: 50 + (seed % 30),
      temp: 60,
      fan: 38,
    },
  };
}

function perfRecording(spec, id) {
  const rng = mulberry32(perfSeed(id)),
    ratio = spec.fps / spec.lowFps;
  const rec = {
    spec,
    tier: spec.tier || null,
    frames: [],
    totalMs: 0,
    clockMs: 0,
    rng,
    // Tail spikes U(lo, hi) at 2.5% so the 1% low lands near lowFps.
    spikeHi: Math.max(1.2, (ratio - 1.12) / 0.6 + 1.12),
    nextHitch: spec.hitchEvery * (0.4 + rng()),
    nextStutter: (spec.stutterEvery || 1e9) * (0.5 + rng()),
    early: [...(spec.earlyHitches || [])],
    bigDone: !spec.bigHitch,
    burst: 0,
    burstMs: 0,
    live: false,
    framegen: !!spec.framegen,
    latency: spec.latency || null,
    version: 0,
    cache: {},
  };
  perfAppend(rec, spec.minutes * 60000);
  return rec;
}

function perfNextFrame(rec) {
  const s = rec.spec,
    r = rec.rng,
    t = rec.clockMs / 1000;
  if (rec.burst > 0) {
    rec.burst--;
    return rec.burstMs * (0.7 + 0.3 * r());
  }
  if (s.menu && t >= s.menu[0] && t < s.menu[0] + s.menu[1]) {
    // A capped menu is smooth; stalls due inside it move past its end.
    const end = s.menu[0] + s.menu[1];
    rec.nextHitch = Math.max(rec.nextHitch, end + s.hitchEvery * 0.5);
    rec.nextStutter = Math.max(rec.nextStutter, end + 2);
    return 1000 / s.menu[2] + (r() - 0.5) * 0.3;
  }
  if (!rec.bigDone && t >= s.bigHitch[0]) {
    rec.bigDone = true;
    return s.bigHitch[1];
  }
  if (rec.early.length && t >= rec.early[0]) {
    rec.early.shift();
    return s.hitchMs[0] + r() * (s.hitchMs[1] - s.hitchMs[0]);
  }
  if (t >= rec.nextHitch) {
    rec.nextHitch = t + s.hitchEvery * (0.5 + r());
    const ms = s.hitchMs[0] + r() * r() * (s.hitchMs[1] - s.hitchMs[0]);
    if (r() < 0.35) {
      rec.burst = 1;
      rec.burstMs = ms * 0.6;
    }
    return ms;
  }
  if (t >= rec.nextStutter) {
    rec.nextStutter = t + s.stutterEvery * (0.5 + r());
    return 19 + r() * 12;
  }
  const base =
    (1000 / s.fps) *
    (1 + s.drift * Math.sin(t / 53) + s.drift * 0.5 * Math.sin(t / 11.3));
  if (r() < 0.025) return base * (1.12 + r() * (rec.spikeHi - 1.12));
  return base * (1 + (r() + r() + r() - 1.5) * 0.07);
}

// Record durationMs more play; the ring keeps the newest 30 minutes.
function perfAppend(rec, durationMs) {
  const end = rec.clockMs + durationMs;
  while (rec.clockMs < end) {
    const ms = perfNextFrame(rec);
    rec.frames.push(ms);
    rec.totalMs += ms;
    rec.clockMs += ms;
  }
  const cap = PERF.ringMinutes * 60000;
  if (rec.totalMs > cap) {
    let drop = 0,
      ms = 0;
    while (rec.totalMs - ms > cap) ms += rec.frames[drop++];
    rec.frames.splice(0, drop);
    rec.totalMs -= ms;
  }
  rec.version++;
}

// Hardware readings drift a little while a session records.
function perfHardware(rec) {
  const hw = rec.spec.hw,
    n = rec.clockMs / 1000;
  if (!rec.live) return { ...hw };
  const wobble = (period, amp) => Math.round(amp * Math.sin(n / period));
  return {
    power: hw.power + wobble(17, 4),
    clock: hw.clock + wobble(23, 15),
    gpu: Math.min(100, hw.gpu + wobble(13, 1)),
    cpu: hw.cpu + wobble(9, 3),
    temp: hw.temp + wobble(41, 1),
    fan: hw.fan + wobble(37, 1),
  };
}

function perfSummary(rec) {
  if (!rec || !rec.frames.length) return null;
  if (rec.cache.summaryAt === rec.version) return rec.cache.summary;
  const sorted = Float64Array.from(rec.frames).sort(),
    p50 = perfPercentile(sorted, 0.5),
    p99 = perfPercentile(sorted, 0.99),
    minutes = rec.totalMs / 60000;
  const summary = {
    frames: rec.frames.length,
    minutes,
    qualified: minutes >= PERF.qualifyMinutes,
    p50,
    p99,
    fps: 1000 / p50,
    low: 1000 / p99,
    framegen: rec.framegen,
    latency: rec.latency && rec.latency.ms > 0 ? rec.latency : null,
    hw: perfHardware(rec),
  };
  rec.cache.summary = summary;
  rec.cache.summaryAt = rec.version;
  return summary;
}
function perfColumns(rec) {
  if (rec.cache.colsAt !== rec.version) {
    rec.cache.cols = densifyFrametimes(rec.frames);
    rec.cache.colsAt = rec.version;
  }
  return rec.cache.cols;
}

const perfRecords = {};
const perfRecord = (x) => (x ? perfRecords[x.id] || null : null);

// Recording follows the library's launch lifecycle: a wrapped session that
// reached "running" (games started outside PenguinBurner do not record).
function perfIsRunning(x) {
  return x.session ? x.session.state === "running" : !!x.running;
}
const perfCanRecord = (x) => x.wrapped && x.cap && x.mode !== "Stock";

// The library reads these fields for its rows, filters and peek; keep them
// in step with the ring.
function perfSyncGame(x) {
  const rec = perfRecord(x),
    s = perfSummary(rec);
  if (!s) return;
  x.mins = Math.round(s.minutes * 10) / 10;
  x.fps = Math.round(s.fps);
  x.low = Math.round(s.low);
  x.power = s.hw.power;
  x.latency_ms = s.latency ? Math.round(s.latency.ms) : 0;
  x.recTier = rec.tier || "";
}

function initPerfSamples() {
  games.forEach((x) => {
    const spec = PERF_SAMPLES[x.id];
    if (!spec) return;
    perfRecords[x.id] = perfRecording(spec, x.id);
    x.mode = spec.mode;
    x.cap = true;
    perfSyncGame(x);
  });
  // #history-live: DOOM Eternal is still recording, in the Live zoom.
  if (typeof location !== "undefined" && location.hash === "#history-live") {
    const i = games.findIndex((x) => x.id === "782330");
    if (i < 0) return;
    const x = games[i];
    if (x.session) Object.assign(x.session, { state: "running", pending: false });
    else x.running = true;
    perfRecords[x.id].live = true;
    if (typeof syncSessions === "function") syncSessions();
    idx = i;
    report = true;
    perfLive = true;
  }
}

// ---------- API for the library's Perf Profile button and peek ----------
// library.js owns the button beside Play and its popover; these keep it on
// the same ring, spokes and copy as the full view.

// {enabled, peek, live, tip}: disabled until a ring exists, warming under
// 5 min, peek only once the fingerprint has qualified.
function perfEntryInfo(x) {
  const rec = perfRecord(x),
    s = perfSummary(rec);
  if (!rec)
    return { enabled: false, peek: false, live: false, tip: PERF_UNAVAILABLE };
  if (!s)
    return {
      enabled: true,
      peek: false,
      live: rec.live,
      tip: "Game Perf Profile — waiting for the first frames",
    };
  if (!s.qualified)
    return {
      enabled: true,
      peek: false,
      live: rec.live,
      tip: "Game Perf Profile — " + warmingText(s.minutes),
    };
  return {
    enabled: true,
    peek: true,
    live: rec.live,
    tip: "Game Perf Profile — click to open the full view.",
  };
}

// Peek rows: Median, 1% low, Power draw, and Latency only when measured.
function perfPeekRows(x) {
  const s = perfSummary(perfRecord(x));
  if (!s) return [];
  const rows = [
    ["Median", Math.round(s.fps) + " fps · " + s.p50.toFixed(1) + " ms"],
    ["1% low", Math.round(s.low) + " fps · " + s.p99.toFixed(1) + " ms"],
    ["Power draw", s.hw.power + " W"],
  ];
  if (s.latency) rows.push(["Latency", latencyText(s.latency)]);
  return rows;
}

// The 124 px peek fingerprint: spoke captions only, no values.
function drawPerfPeek(canvas, x) {
  const rec = perfRecord(x),
    s = perfSummary(rec);
  drawPerfFingerprint(canvas, {
    summary: s && s.qualified ? s : null,
    tier: rec?.tier,
    target: perfTarget(x),
    detailed: false,
  });
}

// Bumps on every recorded second and on start/stop, so a poll can tell
// whether the button needs a refresh.
function perfRecordingVersion(x) {
  const rec = perfRecord(x);
  return rec ? rec.version + (rec.live ? 0.5 : 0) : -1;
}

// ---------- Drill-down view ----------
let perfLive = false,
  perfWindow = 10,
  perfProbe = null,
  perfViewKind = "",
  perfLastRefresh = 0;

// The per-game target only while the override is on, as the library row.
function perfTarget(x) {
  const system =
    typeof SYSTEM_TARGET_FPS !== "undefined" ? SYSTEM_TARGET_FPS : PERF.fallbackTargetFps;
  return x.override ? Number(x.target) || system : system;
}
function perfPowerLimit() {
  const gpu = typeof appState !== "undefined" ? appState.selectedGpu : 0;
  return PERF_POWER_LIMITS[gpu] || PERF.fallbackPowerW;
}

function openReport() {
  libScroll = scrollY;
  listScroll = document.querySelector(".list")?.scrollTop || 0;
  report = true;
  perfProbe = null;
  render();
  scrollTo(0, 0);
  document.querySelector("#back").focus();
}
function back() {
  report = false;
  render();
  const list = document.querySelector(".list");
  if (list) list.scrollTop = listScroll;
  scrollTo(0, libScroll);
  document.querySelector("#view")?.focus({ preventScroll: true });
}

function perfViewKindFor(x) {
  if (!x) return "nogame";
  return perfSummary(perfRecord(x)) ? "detail" : "norecord";
}

function perfStatRows(rows) {
  return (
    "<dl>" +
    rows
      .map(
        ([k, v, sub, hint]) =>
          "<div" +
          (hint ? ' title="' + hint + '"' : "") +
          "><dt>" +
          k +
          "</dt><dd><b>" +
          v +
          "</b>" +
          (sub ? "<small>" + sub + "</small>" : "") +
          "</dd></div>",
      )
      .join("") +
    "</dl>"
  );
}
function perfFrameRateRows(x, s) {
  const big = (label, fps, ms) =>
    '<div class="perfmetric"><small>' +
    label +
    "</small><strong>" +
    Math.round(fps) +
    ' <span class="measure">fps</span></strong><small>' +
    ms.toFixed(1) +
    " ms</small></div>";
  const rows = [
    ["Target", perfTarget(x) + " fps", x.override ? "This game" : "System-wide"],
  ];
  if (s.latency)
    rows.push([
      "Latency",
      latencyText(s.latency),
      s.latency.estimated ? "Estimate" : "Measured",
      s.latency.estimated ? "~ marks an estimate, as the overlay's LAT." : "",
    ]);
  return (
    '<div class="perfmetrics">' +
    big("Median", s.fps, s.p50) +
    big("1% low", s.low, s.p99) +
    "</div>" +
    perfStatRows(rows)
  );
}
function perfSystemRows(s) {
  const hw = s.hw;
  return perfStatRows([
    ["Power draw", hw.power + " W"],
    ["GPU clock", hw.clock + " MHz"],
    ["GPU / busiest CPU thread", hw.gpu + "% / " + hw.cpu + "%"],
    ["GPU temperature", hw.temp + " °C"],
    ["Fan", hw.fan + "%"],
  ]);
}
function perfMetaHtml(rec, s) {
  return perfMetaParts(rec, s)
    .map((p) => (p === "● REC" ? '<span class="rec">● REC</span>' : p))
    .join(" · ");
}
function perfChip(rec) {
  return (
    '<span class="tierchip" id="perfChip" style="--tier:' +
    perfTierColor(rec.tier) +
    '" title="Profile tier while this was recorded">' +
    perfTierLabel(rec.tier).toUpperCase() +
    "</span>"
  );
}

function perfControls() {
  return (
    // The select sits before the switch, so showing it never moves the
    // switch from under the pointer.
    '<div class="perfcontrols"><select id="perfWindowSelect" aria-label="Live window"' +
    (perfLive ? "" : " hidden") +
    ' onchange="setPerfWindow(this.value)">' +
    PERF.windows
      .map(
        (w) =>
          '<option value="' +
          w +
          '"' +
          (w === perfWindow ? " selected" : "") +
          ">" +
          w +
          " s</option>",
      )
      .join("") +
    '</select><label for="perfLiveBtn">Live</label><button class="track" id="perfLiveBtn" role="switch" aria-checked="' +
    perfLive +
    '" onclick="setPerfLive(!perfLive)"><i></i></button></div>'
  );
}

function historyView() {
  const x = g(),
    kind = perfViewKindFor(x);
  perfViewKind = kind;
  perfLastRefresh = Date.now();
  const backline =
    '<div class="backline"><button id="back" onclick="back()">← Back to game</button><small>Game Library / Perf Profile</small></div>';
  if (kind !== "detail") {
    const text =
      kind === "nogame"
        ? PERF_EMPTY_HINT
        : "No telemetry captured for " +
          esc(x.name) +
          " yet.\nPlay it with PenguinBurner enabled to record a window.";
    if (typeof requestAnimationFrame === "function")
      requestAnimationFrame(() => {
        const c = document.querySelector("[data-perf-empty]");
        if (c) drawPerfFingerprint(c, { summary: null, badge: true, muted: true });
      });
    return (
      '<div class="perfview" id="perfView" data-kind="' +
      kind +
      '">' +
      (x ? backline : "") +
      '<div class="card perfempty">' +
      (kind === "nogame"
        ? '<canvas data-perf-empty aria-hidden="true"></canvas>'
        : "") +
      "<p>" +
      text.replace("\n", "<br>") +
      "</p></div></div>"
    );
  }
  const rec = perfRecord(x),
    s = perfSummary(rec);
  return (
    '<div class="perfview" id="perfView" data-kind="detail" data-refresh="' +
    perfRefreshMs(rec) +
    '">' +
    backline +
    '<div class="perfhead"><div><h1>' +
    esc(x.name) +
    '</h1><p class="perfmeta" id="perfMeta">' +
    perfMetaHtml(rec, s) +
    "</p></div>" +
    perfChip(rec) +
    "</div>" +
    '<div class="perfgridwrap"><div class="perfgrid">' +
    '<section class="card perfcard fingercard">' +
    head("Fingerprint", "Each spoke on its own scale") +
    '<div class="fingerwrap"><canvas class="finger" data-chart="fingerprint" role="img"></canvas></div><div class="legend fingerkey">' +
    perfDotKey() +
    "</div></section>" +
    '<section class="card perfcard">' +
    head("Frame rate", "Window medians") +
    '<div class="perfstats" id="perfFrameRate">' +
    perfFrameRateRows(x, s) +
    "</div></section>" +
    '<section class="card perfcard">' +
    head("GPU and system", "Window medians") +
    '<div class="perfstats" id="perfSystem">' +
    perfSystemRows(s) +
    "</div></section></div></div>" +
    '<section class="card perfcard frametimecard">' +
    head("Frametime", "Every frame, in milliseconds", perfControls()) +
    '<div class="plotbox"><canvas class="graph" data-chart="time" role="img" tabindex="0"></canvas></div><div class="legend perflegend" id="perfLegend"></div></section>' +
    "</div>"
  );
}

function perfDotKey() {
  const c = design.colors,
    dot = (label, color, hollow) =>
      '<span class="legendItem"><i class="swatch dotonly' +
      (hollow ? " hollow" : "") +
      '" style="--swatch:' +
      color +
      '" aria-hidden="true"></i>' +
      label +
      "</span>";
  return (
    dot("In range", c.accent) +
    dot("Out of range", c.warning, true) +
    dot("No range", c.quiet)
  );
}

function setPerfLive(on) {
  perfLive = !!on;
  perfProbe = null;
  const btn = document.getElementById("perfLiveBtn"),
    sel = document.getElementById("perfWindowSelect"),
    view = document.getElementById("perfView");
  if (btn) btn.setAttribute("aria-checked", perfLive);
  if (sel) sel.hidden = !perfLive;
  if (view) view.dataset.refresh = perfRefreshMs(perfRecord(g()));
  const canvas = document.querySelector("[data-chart=time]");
  if (canvas) drawFrametime(canvas);
}
function setPerfWindow(value) {
  perfWindow = PERF.windows.includes(Number(value)) ? Number(value) : 10;
  perfProbe = null;
  const sel = document.getElementById("perfWindowSelect");
  if (sel) sel.value = String(perfWindow);
  const canvas = document.querySelector("[data-chart=time]");
  if (canvas) drawFrametime(canvas);
}

// 1 s only where it shows: a recording ring in the Live zoom.
function perfRefreshMs(rec) {
  return rec && rec.live && perfLive ? PERF.liveRefreshMs : PERF.refreshMs;
}

function refreshPerfView() {
  const view = document.getElementById("perfView"),
    x = g();
  if (!view) return;
  perfLastRefresh = Date.now();
  const kind = perfViewKindFor(x);
  if (kind !== perfViewKind) {
    const backFocused = document.activeElement?.id === "back";
    render();
    if (backFocused) document.querySelector("#back")?.focus({ preventScroll: true });
    return;
  }
  if (kind !== "detail") return;
  const rec = perfRecord(x),
    s = perfSummary(rec);
  view.dataset.refresh = perfRefreshMs(rec);
  document.getElementById("perfMeta").innerHTML = perfMetaHtml(rec, s);
  document.getElementById("perfFrameRate").innerHTML = perfFrameRateRows(x, s);
  document.getElementById("perfSystem").innerHTML = perfSystemRows(s);
  const finger = document.querySelector("[data-chart=fingerprint]"),
    time = document.querySelector("[data-chart=time]");
  if (finger) drawFingerprint(finger);
  if (time) drawFrametime(time);
}

// ---------- Recording heartbeat (1 Hz) ----------
// Games whose ring started, stopped or appeared since the last poll event.
const perfChanged = new Set();
function perfHeartbeat() {
  games.forEach((x) => {
    const running = perfIsRunning(x);
    let rec = perfRecord(x);
    if (running && !rec && perfCanRecord(x)) {
      rec = perfRecords[x.id] = perfRecording(perfFreshSpec(x), x.id);
      perfChanged.add(x);
    }
    if (!rec) return;
    const live = running && perfCanRecord(x);
    if (live !== rec.live) {
      rec.live = live;
      rec.version++;
      perfChanged.add(x);
    }
    if (live) {
      perfAppend(rec, 1000);
      perfSyncGame(x);
    }
  });
  // Recording continues in the background; the screen only refreshes while
  // it is visible.
  if (document.hidden || tab !== "Game Library") return;
  if (report) {
    const due = perfRefreshMs(perfRecord(g()));
    if (Date.now() - perfLastRefresh >= due - 100) refreshPerfView();
  } else if (Date.now() - perfLastRefresh >= PERF.refreshMs - 100) {
    // The library's game-state poll: one event per 2 s while a ring records.
    perfLastRefresh = Date.now();
    const x = g();
    if (perfRecord(x)?.live || perfChanged.has(x))
      document.dispatchEvent(
        new CustomEvent("perfrecording", { detail: { index: idx } }),
      );
    perfChanged.clear();
  }
}

// ---------- Fingerprint ----------
// Half the widest two-line spoke label ("87 fps", "LOW ↑") at 12px.
const PERF_LABEL_HALF = 24;
// How far a two-line label's centre sits past the vertex gap along its
// spoke: the box's support in that direction, so no corner meets the dot.
function perfLabelReach(cos, sin, lineH) {
  return Math.abs(cos) * PERF_LABEL_HALF + Math.abs(sin) * lineH;
}

// opts: {summary|null, tier, target, detailed=true, badge=false, muted=false,
//        warming=minutes}
function drawPerfFingerprint(canvas, opts) {
  const { ctx, w, h } = canvasContext(canvas),
    c = design.colors,
    theme = chartTheme(),
    size = Math.min(w, h),
    cx = w / 2,
    lineH = 15,
    labels = !opts.badge,
    detailed = labels && opts.detailed !== false,
    dotR = labels ? Math.min(4.5, Math.max(3, size * 0.026)) : 0,
    gap = labels ? Math.max(2, size * 0.02) + dotR : 0,
    angle = (i) => -Math.PI / 2 + (i * 2 * Math.PI) / 5;
  let r, cy;
  if (detailed) {
    // Two-line labels sit above the top spoke and below the two lower ones
    // (sin 54°), so the radar is taller above its centre than below: fit
    // that real extent instead of a symmetric circle. Side labels (cos 18°)
    // bound it by width.
    const low = Math.sin(0.3 * Math.PI),
      lowCos = Math.cos(0.3 * Math.PI),
      side = Math.cos(0.1 * Math.PI),
      sideSin = Math.sin(0.1 * Math.PI),
      lowReach = perfLabelReach(lowCos, low, lineH),
      sideReach = perfLabelReach(side, sideSin, lineH),
      byHeight = (h - (1 + low) * gap - 2 * lineH - low * lowReach - lineH) / (1 + low),
      byWidth = (w / 2 - PERF_LABEL_HALF) / side - gap - sideReach;
    r = Math.min(byHeight, byWidth);
    const above = r + gap + 2 * lineH,
      below = low * (r + gap + lowReach) + lineH;
    cy = (h - above - below) / 2 + above;
  } else {
    // The compact peek keeps 12px codes legible by giving them a fixed band
    // at the top and bottom and tucking side codes above their spokes.
    r = labels ? size / 2 - lineH - 7 : size * 0.42;
    cy = h / 2;
  }
  const reach = (f) => r * (0.1 + 0.9 * perfClamp01(f));
  // A canvas inside a hidden container has no size yet: nothing to draw.
  if (r < 8) return null;
  if (labels) {
    ctx.fillStyle = theme.surface;
    ctx.fillRect(0, 0, w, h);
  }
  ctx.lineWidth = 1;
  if (!opts.summary) {
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = opts.muted ? c.divider : c.border;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, 2 * Math.PI);
    ctx.stroke();
    ctx.setLineDash([]);
    if (opts.warming != null && labels) {
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.font = theme.fontStrong;
      ctx.fillStyle = c.secondary;
      ctx.fillText("Warming up", cx, cy - 9);
      ctx.font = theme.font;
      ctx.fillStyle = c.quiet;
      ctx.fillText(
        Math.floor(opts.warming) + " of " + PERF.qualifyMinutes + " min captured",
        cx,
        cy + 9,
      );
      ctx.textBaseline = "alphabetic";
    }
    return;
  }
  const axes = perfSpokes(opts.summary, opts.target, perfPowerLimit()),
    color = perfTierColor(opts.tier);
  ctx.strokeStyle = labels ? c.gridMajor : c.border;
  for (const f of labels ? [0.25, 0.5, 0.75, 1] : [1]) {
    ctx.beginPath();
    ctx.arc(cx, cy, reach(f), 0, 2 * Math.PI);
    ctx.stroke();
  }
  const points = axes.map((a, i) => [
    cx + Math.cos(angle(i)) * reach(a.fraction),
    cy + Math.sin(angle(i)) * reach(a.fraction),
  ]);
  if (labels) {
    ctx.font = theme.font;
    axes.forEach((axis, i) => {
      const cos = Math.cos(angle(i)),
        sin = Math.sin(angle(i));
      ctx.strokeStyle = c.gridMajor;
      strokePath(ctx, [
        [cx, cy],
        [cx + cos * r, cy + sin * r],
      ]);
      if (!detailed) {
        ctx.font = theme.font;
        ctx.fillStyle = c.secondary;
        ctx.textBaseline = "top";
        const width = ctx.measureText(axis.code).width,
          side = Math.abs(cos) > 0.5,
          ex = cx + cos * r,
          ey = cy + sin * r;
        ctx.textAlign = "left";
        // A small inset keeps side captions off the canvas edge.
        const inset = 3,
          left = side
            ? cos > 0
              ? w - width - inset
              : inset
            : Math.min(Math.max(ex - width / 2, inset), w - width - inset);
        const top = side
          ? ey - lineH - 3
          : sin < 0
            ? Math.max(0, ey - lineH - 4)
            : Math.min(h - lineH, ey + 4);
        ctx.fillText(axis.code, left, top);
        return;
      }
      const lines = [axis.arrow ? axis.code + " " + axis.arrow : axis.code];
      lines.push(axis.spoke || axis.text);
      // Push the label box out until its nearest edge clears the vertex dot.
      const dist = r + gap + perfLabelReach(cos, sin, lineH),
        tx = cx + cos * dist;
      let top = cy + sin * dist - (lineH * lines.length) / 2;
      top = Math.min(Math.max(top, 0), h - lineH * lines.length);
      lines.forEach((line, row) => {
        ctx.font = row ? theme.fontStrong : theme.font;
        const width = ctx.measureText(line).width,
          left = Math.min(Math.max(tx - width / 2, 0), w - width);
        ctx.fillStyle = row ? c.ink : c.secondary;
        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.fillText(line, left, top + row * lineH);
      });
    });
    ctx.textBaseline = "alphabetic";
  }
  ctx.beginPath();
  points.forEach(([px, py], i) => (i ? ctx.lineTo(px, py) : ctx.moveTo(px, py)));
  ctx.closePath();
  ctx.fillStyle = withAlpha(color, labels ? 0.18 : 0.3);
  ctx.strokeStyle = color;
  ctx.lineWidth = labels ? 2.4 : 1.6;
  ctx.lineJoin = "round";
  ctx.fill();
  ctx.stroke();
  if (labels) {
    // A 2.5 px surface halo lifts each dot off any tier stroke; out of
    // range is hollow, so it reads by shape as well as hue (warning on the
    // amber Performance stroke would otherwise vanish).
    axes.forEach((axis, i) => {
      const band = axisInBand(axis),
        [px, py] = points[i];
      ctx.fillStyle = theme.surface;
      ctx.beginPath();
      ctx.arc(px, py, dotR + 2.5, 0, 2 * Math.PI);
      ctx.fill();
      ctx.beginPath();
      if (band === false) {
        ctx.arc(px, py, dotR - 0.5, 0, 2 * Math.PI);
        ctx.lineWidth = 2;
        ctx.strokeStyle = c.warning;
        ctx.stroke();
      } else {
        ctx.arc(px, py, dotR, 0, 2 * Math.PI);
        ctx.fillStyle = band ? c.accent : c.quiet;
        ctx.fill();
      }
    });
  }
  return axes;
}

// The view's fingerprint (charts.js draw() calls this on every render).
function drawFingerprint(canvas) {
  const x = g(),
    rec = perfRecord(x),
    s = perfSummary(rec);
  if (!s) return;
  // A short archived session keeps the dashed outline; a live ring draws
  // its shape early and the meta line says it is settling.
  const settled = s.qualified || rec.live;
  const axes = perfSpokes(s, perfTarget(x), perfPowerLimit());
  drawPerfFingerprint(canvas, {
    summary: settled ? s : null,
    tier: rec.tier,
    target: perfTarget(x),
    warming: settled ? null : s.minutes,
  });
  canvas.closest(".fingercard")?.querySelector(".fingerkey")?.toggleAttribute("hidden", !settled);
  const tip = settled
    ? axes.map((a) => a.label + ": " + a.text).join("\n")
    : warmingText(s.minutes);
  canvas.title = tip;
  canvas.setAttribute(
    "aria-label",
    settled
      ? perfTierLabel(rec.tier) + " fingerprint. " + tip.replaceAll("\n", ", ") + "."
      : tip,
  );
}

// ---------- Frametime ----------
// The verbatim PR #137 caption, split at " · " so each part carries its
// real swatch: the legend reads exactly as the caption.
function perfSwatch(cls, color, fill) {
  return (
    '<i class="swatch ' +
    cls +
    '"' +
    (color
      ? ' style="--swatch:' + color + (fill ? ";--swatch-fill:" + fill : "") + '"'
      : "") +
    ' aria-hidden="true"></i>'
  );
}
function perfLegendHtml(caption) {
  const c = design.colors,
    swatches = {
      "line: median frametime": perfSwatch("", c.accent),
      "band: up to the 1% worst frames": perfSwatch("band", c.accent, withAlpha(c.accent, 0.16)),
      "dots: hitches over 33 ms":
        perfSwatch("dotonly", c.warning) + perfSwatch("dotonly", c.severe),
      "stutter needles yellow -> red by severity (red ~30 ms)": perfSwatch("grad"),
      "lines: median & 1%-low (p99)":
        perfSwatch("dashed", c.secondary) + perfSwatch("dashed fine", c.quiet),
    };
  return caption
    .split(" · ")
    .map(
      (part, i) =>
        (i ? '<span class="sep"> · </span>' : "") +
        '<span class="legendItem' +
        (part.startsWith("worst frame") ? " clipnote" : "") +
        '">' +
        (swatches[part] || "") +
        part +
        "</span>",
    )
    .join("");
}

// PR #137 gives the plot the rest of the window height. The figure leads the
// view, so it fills the viewport below its own top and legend, keeping a
// 56 px strip for the summary cards' headings as a cue to scroll. Measured
// from the document, so the browser's in-page shell header and the Qt
// window's native one both work.
const PERF_PLOT_MIN = 240,
  PERF_PLOT_MAX = 480,
  PERF_PLOT_PEEK = 56;
function perfPlotHeight(viewportH, plotTop, belowPlot) {
  return Math.round(
    Math.min(
      PERF_PLOT_MAX,
      Math.max(PERF_PLOT_MIN, viewportH - plotTop - belowPlot - PERF_PLOT_PEEK),
    ),
  );
}
function perfFitPlot(canvas) {
  const card = canvas.closest?.(".frametimecard");
  if (!card || typeof innerHeight !== "number") return;
  const rect = canvas.getBoundingClientRect(),
    height =
      perfPlotHeight(
        innerHeight,
        rect.top + scrollY,
        card.getBoundingClientRect().bottom - rect.bottom,
      ) + "px";
  if (canvas.style.height !== height) canvas.style.height = height;
}

function drawFrametime(canvas) {
  const x = g(),
    rec = perfRecord(x),
    s = perfSummary(rec);
  if (!s) return;
  perfFitPlot(canvas);
  const { ctx, w, h } = canvasContext(canvas),
    c = design.colors,
    theme = chartTheme(),
    box = { left: 54, right: w - 96, top: 14, bottom: h - 46 },
    ceiling = frametimeCeiling(s.p50, s.p99),
    yTop = ceiling * 1.04,
    y = (v) => box.bottom - ((box.bottom - box.top) * Math.min(v, yTop)) / yTop;
  let lo, hi, step, title, model;
  if (perfLive) {
    const tail = liveTail(rec.frames, perfWindow * 1000);
    lo = -perfWindow * 1.03;
    hi = perfWindow * 0.01;
    step = liveStep(perfWindow);
    title = "Seconds ago";
    model = { mode: "live", tail };
  } else {
    const cols = perfColumns(rec),
      x0 = cols[0].x,
      span = Math.abs(x0) || 1;
    lo = x0 - span * 0.01;
    hi = span * 0.01;
    step = overviewStep(span);
    title = "Minutes ago";
    model = { mode: "overview", cols };
  }
  const X = (v) => box.left + ((box.right - box.left) * (v - lo)) / (hi - lo);
  const fmt = (v) => String(Math.round(Math.abs(v) * 10) / 10);
  drawChartFrame(ctx, w, h, box, X, y, {
    x: { lo, hi, major: step, format: fmt, title },
    y: { lo: 0, hi: ceiling, minor: frametimeStep(ceiling) / 2, major: frametimeStep(ceiling), title: "Frametime (ms)" },
  });
  ctx.save();
  ctx.beginPath();
  ctx.rect(box.left + 1, box.top - 8, box.right - box.left, box.bottom - box.top + 8);
  ctx.clip();
  let worst = 0,
    hitchCount = 0;
  if (model.mode === "overview") {
    const cols = model.cols;
    // Band from the median up to p99, then the median line.
    ctx.beginPath();
    cols.forEach((col, i) =>
      i ? ctx.lineTo(X(col.x), y(col.p50)) : ctx.moveTo(X(col.x), y(col.p50)),
    );
    for (let i = cols.length - 1; i >= 0; i--) ctx.lineTo(X(cols[i].x), y(cols[i].p99));
    ctx.closePath();
    ctx.fillStyle = withAlpha(c.accent, 0.16);
    ctx.fill();
    ctx.strokeStyle = c.accent;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = "round";
    if (cols.length > 1) strokePath(ctx, cols.map((col) => [X(col.x), y(col.p50)]));
    else {
      ctx.fillStyle = c.accent;
      ctx.beginPath();
      ctx.arc(X(cols[0].x), y(cols[0].p50), 2.5, 0, 2 * Math.PI);
      ctx.fill();
    }
    perfReferenceLines(ctx, box, y, s, w);
    const hitches = hitchPoints(cols);
    hitchCount = hitches.length;
    for (const col of hitches) {
      worst = Math.max(worst, col.max);
      const t = perfClamp01((col.max - PERF.severeMs) / (PERF.hitchWorstMs - PERF.severeMs)),
        px = X(col.x),
        py = y(Math.min(col.max, ceiling));
      ctx.fillStyle = hitchColor(col.max);
      ctx.strokeStyle = theme.surface;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(px, py, 3 + 1.5 * t, 0, 2 * Math.PI);
      ctx.fill();
      ctx.stroke();
    }
  } else {
    const { xs, values } = model.tail,
      pts = xs.map((v, i) => [X(v), y(values[i])]);
    ctx.beginPath();
    ctx.moveTo(pts[0][0], y(0));
    pts.forEach(([px, py]) => ctx.lineTo(px, py));
    ctx.lineTo(pts.at(-1)[0], y(0));
    ctx.closePath();
    ctx.fillStyle = withAlpha(c.accent, 0.1);
    ctx.fill();
    ctx.strokeStyle = c.accent;
    ctx.lineWidth = 1.4;
    ctx.lineJoin = "round";
    strokePath(ctx, pts);
    const threshold = s.p50 * PERF.stutterRatio,
      needle = Math.max(1.5, ((box.right - box.left) * perfWindow * 0.003) / (hi - lo));
    values.forEach((v, i) => {
      if (v < threshold) return;
      hitchCount++;
      worst = Math.max(worst, v);
      ctx.fillStyle = stutterColor(v);
      const top = y(Math.min(v, ceiling));
      ctx.fillRect(X(xs[i]) - needle / 2, top, needle, y(0) - top);
    });
    perfReferenceLines(ctx, box, y, s, w);
  }
  ctx.restore();
  perfReferenceLabels(ctx, box, y, s);
  perfDrawProbe(ctx, box, X, y, model, ceiling);
  canvas._perf = { box, X, lo, hi, model };
  const caption = clippedCaption(
    perfLive ? PERF_LIVE_CAPTION : PERF_OVERVIEW_CAPTION,
    worst,
    ceiling,
  );
  canvas.dataset.caption = caption;
  const legend = document.getElementById("perfLegend");
  if (legend) legend.innerHTML = perfLegendHtml(caption);
  canvas.setAttribute(
    "aria-label",
    (perfLive
      ? "Live frametime, last " + perfWindow + " seconds: "
      : "Frametime overview, last " + Math.round(Math.abs(lo)) + " minutes: ") +
      "median " +
      s.p50.toFixed(1) +
      " ms, p99 " +
      s.p99.toFixed(1) +
      " ms, " +
      hitchCount +
      (perfLive ? " stutter frames" : " hitches") +
      (worst ? ", worst " + Math.round(worst) + " ms" : "") +
      ". Use the arrow keys to read values.",
  );
}

function perfReferenceLines(ctx, box, y, s) {
  const c = design.colors;
  ctx.lineWidth = 1;
  [
    [s.p50, c.secondary, [5, 4]],
    [s.p99, c.quiet, [2, 3]],
  ].forEach(([v, color, dash]) => {
    ctx.strokeStyle = color;
    ctx.setLineDash(dash);
    const py = Math.round(y(v)) + 0.5;
    strokePath(ctx, [
      [box.left, py],
      [box.right, py],
    ]);
  });
  ctx.setLineDash([]);
}
// Right-edge labels in the gutter, nudged apart when the lines are close.
function perfReferenceLabels(ctx, box, y, s) {
  const c = design.colors,
    theme = chartTheme();
  let m = y(s.p50),
    p = y(s.p99);
  if (m - p < 16) {
    const mid = (m + p) / 2;
    m = mid + 8;
    p = mid - 8;
  }
  ctx.font = theme.font;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillStyle = c.secondary;
  ctx.fillText("Median " + s.p50.toFixed(1), box.right + 8, m);
  ctx.fillStyle = c.quiet;
  ctx.fillText("p99 " + s.p99.toFixed(1), box.right + 8, p);
  ctx.textBaseline = "alphabetic";
}

function perfProbeText(model, i) {
  if (model.mode === "overview") {
    const col = model.cols[i];
    return (
      Math.abs(col.x).toFixed(1) +
      " min ago · median " +
      col.p50.toFixed(1) +
      " · p99 " +
      col.p99.toFixed(1) +
      " · worst " +
      col.max.toFixed(0) +
      " ms"
    );
  }
  return (
    Math.abs(model.tail.xs[i]).toFixed(2) +
    " s ago · " +
    model.tail.values[i].toFixed(1) +
    " ms"
  );
}
function perfProbeCount(model) {
  return model.mode === "overview" ? model.cols.length : model.tail.xs.length;
}
function perfDrawProbe(ctx, box, X, y, model, ceiling) {
  if (perfProbe == null) return;
  const n = perfProbeCount(model);
  if (!n) return;
  const i = Math.min(n - 1, Math.max(0, perfProbe)),
    xv = model.mode === "overview" ? model.cols[i].x : model.tail.xs[i],
    yv = model.mode === "overview" ? model.cols[i].p50 : model.tail.values[i],
    px = Math.round(X(xv)) + 0.5,
    theme = chartTheme();
  ctx.strokeStyle = theme.crosshair;
  ctx.lineWidth = 1;
  strokePath(ctx, [
    [px, box.top],
    [px, box.bottom],
  ]);
  ctx.fillStyle = design.colors.candidateCurve;
  ctx.strokeStyle = design.colors.plotSurface;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(px, y(Math.min(yv, ceiling)), 4, 0, 2 * Math.PI);
  ctx.fill();
  ctx.stroke();
  const text = perfProbeText(model, i);
  ctx.font = theme.font;
  const half = ctx.measureText(text).width / 2 + 6,
    bx = Math.min(Math.max(px, box.left + half + 2), box.right - half - 2);
  // Frametimes never reach 0 ms, so the bottom strip is always free; the
  // top row is where clipped hitch dots sit.
  drawBadge(ctx, text, bx, box.bottom - 14);
}

function perfProbeAt(canvas, clientX) {
  const geo = canvas._perf;
  if (!geo) return null;
  const rect = canvas.getBoundingClientRect(),
    px = clientX - rect.left;
  if (px < geo.box.left || px > geo.box.right) return null;
  const v = geo.lo + ((px - geo.box.left) * (geo.hi - geo.lo)) / (geo.box.right - geo.box.left),
    xs = geo.model.mode === "overview" ? geo.model.cols.map((c) => c.x) : geo.model.tail.xs;
  let best = 0;
  for (let i = 1; i < xs.length; i++)
    if (Math.abs(xs[i] - v) < Math.abs(xs[best] - v)) best = i;
  return best;
}

if (typeof document !== "undefined") {
  document.addEventListener("pointermove", (e) => {
    const canvas = e.target.closest?.("[data-chart=time]");
    if (!canvas) return;
    const i = perfProbeAt(canvas, e.clientX);
    if (i !== perfProbe) {
      perfProbe = i;
      drawFrametime(canvas);
    }
  });
  document.addEventListener("pointerout", (e) => {
    const canvas = e.target.closest?.("[data-chart=time]");
    if (canvas && perfProbe != null && document.activeElement !== canvas) {
      perfProbe = null;
      drawFrametime(canvas);
    }
  });
  document.addEventListener("keydown", (e) => {
    const canvas = e.target.closest?.("[data-chart=time]");
    if (!canvas || !canvas._perf) return;
    const n = perfProbeCount(canvas._perf.model),
      big = Math.max(1, Math.round(n / 20));
    const moves = { ArrowLeft: -1, ArrowRight: 1, PageUp: -big, PageDown: big };
    if (e.key in moves) {
      perfProbe = Math.min(n - 1, Math.max(0, (perfProbe ?? n - 1) + moves[e.key]));
    } else if (e.key === "Home") perfProbe = 0;
    else if (e.key === "End") perfProbe = n - 1;
    else if (e.key === "Escape") perfProbe = null;
    else return;
    e.preventDefault();
    drawFrametime(canvas);
  });
  document.addEventListener("focusout", (e) => {
    if (e.target.matches?.("[data-chart=time]") && perfProbe != null) {
      perfProbe = null;
      drawFrametime(e.target);
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && tab === "Game Library" && report) refreshPerfView();
  });
}

initPerfSamples();
if (typeof setInterval === "function" && typeof document !== "undefined")
  setInterval(perfHeartbeat, 1000);
