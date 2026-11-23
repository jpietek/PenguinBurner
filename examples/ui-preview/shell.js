const games = [
  {
    id: "1091500",
    name: "Cyberpunk 2077",
    mins: 24,
    fps: 87,
    low: 61,
    cap: true,
  },
  {
    id: "2531310",
    name: "The Last of Us Part II Remastered",
    mins: 3,
    fps: 72,
    low: 53,
    cap: true,
  },
  { id: "1888930", name: "The Last of Us Part I", mins: 0, cap: false },
  { id: "782330", name: "DOOM Eternal", mins: 0, cap: true },
];
const tabs = {
  "Auto UV": "tab-auto-uv",
  Profiles: "tab-profiles",
  "Game Library": "tab-game-library",
  "In-Game Overlay": "tab-overlay",
};
const profiles = [
  ["Efficiency", 850, 2460, 56.76, 232.9, 0.2437],
  ["Balanced", 850, 2639, 62.7, 267.15, 0.2347],
  ["Performance", 915, 2980, 66.8, 310.18, 0.2154],
];
let tab =
    location.hash === "#auto-uv"
      ? "Auto UV"
      : location.hash === "#design-system"
        ? "Design system"
        : "Game Library",
  idx = 0,
  report = location.hash === "#history",
  q = "",
  pi = 1,
  applied = -1,
  boot = -1,
  order = [0, 1, 2],
  sortKey = -1,
  asc = true,
  libScroll = 0,
  listScroll = 0;
const M = document.getElementById("main"),
  g = () => games[idx],
  metric = (label, v, unit = "") =>
    '<div class="metric"><small>' +
    label +
    "</small><strong>" +
    v +
    ' <span class="measure">' +
    unit +
    "</span></strong></div>",
  pill = (s, c = "") => '<span class="pill ' + c + '">' + s + "</span>",
  row = (title, desc, val) =>
    '<div class="row"><div><h3>' +
    title +
    "</h3><small>" +
    desc +
    "</small></div>" +
    val +
    "</div>",
  head = (title, desc, extra = "") =>
    '<div class="cardhead"><div><h2>' +
    title +
    "</h2><small>" +
    desc +
    "</small></div>" +
    extra +
    "</div>",
  title = (t, d) =>
    '<div class="titlebar"><div><h1>' + t + "</h1><p>" + d + "</p></div></div>";
logo.src = assets["penguin-burner"];

// Shared cross-area state. Areas read and write these fields and call the
// helpers below; none of them re-implements the shell.
const GPU_CHOICES = [
  { index: 0, name: "NVIDIA GeForce RTX 5080", short: "RTX 5080" },
  { index: 1, name: "NVIDIA GeForce RTX 4070", short: "RTX 4070" },
];
const appState = {
  gpus: GPU_CHOICES.slice(0, 1),
  selectedGpu: 0,
  daemon: "ready", // 'ready' | 'missing' | 'outdated'
  // Index of a game PenguinBurner launched wrapped and that now runs (tuned);
  // the overlay, status band and energy counter follow it.
  runningGame: null,
  // Index into games while one is launching, running, stopping, unconfirmed
  // or external: Play's one-game lock.
  launchLock: null,
  bootProfile: null, // profile name applied at startup, or null for stock
  runningProfile: null, // profile name running now, or null for stock
  launcherWriteState: "ready", // 'not-set-up' | 'needs-restart' | 'ready'
  emptyLibrary: false,
  simulate: {
    crashRecovery: false,
    finalVerificationFailure: false,
    dependencyDownload: false,
  },
  status: null, // {running, detail} while an area reports activity
  progress: null, // {label, pct, tooltip} for the header progress bar
};
const gpuLabel = (i = appState.selectedGpu) =>
  "GPU " + i + " · " + (appState.gpus[i] || appState.gpus[0]).short;

// Simulated root-service check (scan-stream-v1). Calls next() once the
// service is ready; declining leaves everything unchanged.
function ensureDaemonReady(next, action = "Auto UV") {
  if (appState.daemon === "ready") return next();
  const missing = appState.daemon === "missing";
  showPanel(
    '<h2>PenguinBurner hardware service</h2><p class="sub">' +
      esc(action) +
      (missing
        ? " needs the PenguinBurner root hardware service. It is not installed."
        : " needs a newer PenguinBurner root hardware service. The running service does not support scan streaming.") +
      '</p><p class="sub">PenguinBurner can ' +
      (missing ? "install" : "update") +
      ' it now. This may ask for your administrator password once.</p><div class="dialogfoot"><button onclick="closePanel()">Cancel</button><button class="primary" id="daemonGo">' +
      (missing ? "Install service" : "Update service") +
      "</button></div>",
  );
  const go = document.getElementById("daemonGo");
  go.onclick = () => {
    go.disabled = true;
    go.textContent = missing ? "Installing…" : "Updating…";
    setTimeout(() => {
      appState.daemon = "ready";
      closePanel();
      renderShellStatus();
      next();
    }, 600);
  };
  go.focus();
}

// Window-level two-line status above the tabs. Pass null to return to the
// idle summary of what is running and what applies at startup. An optional
// tone ('error' | 'warn') marks a failure instead of activity.
function setGlobalStatus(status) {
  appState.status = status && (status.running || status.detail) ? status : null;
  renderShellStatus();
}

// Header progress bar: setHeaderProgress("Downloading dependencies", 42, "…").
function setHeaderProgress(label, pct = 0, tooltip = "") {
  appState.progress =
    label == null
      ? null
      : { label, pct: Math.max(0, Math.min(100, pct)), tooltip };
  renderShellStatus();
}

// Range inputs draw their filled track from --fill (pburn.css). Kept in sync
// on input and whenever a page or dialog renders a slider.
function syncRangeFill(input) {
  const min = Number(input.min || 0),
    max = input.max === "" ? 100 : Number(input.max),
    pct = max > min ? ((Number(input.value) - min) * 100) / (max - min) : 0;
  input.style.setProperty("--fill", Math.max(0, Math.min(100, pct)) + "%");
}
if (typeof document !== "undefined" && typeof MutationObserver === "function") {
  document.addEventListener("input", (e) => {
    if (e.target.matches?.('input[type="range"]')) syncRangeFill(e.target);
  });
  new MutationObserver(() =>
    document.querySelectorAll('input[type="range"]').forEach(syncRangeFill),
  ).observe(document.body, { childList: true, subtree: true });
}

// Defined by the Auto UV area; this stub keeps Profiles working without it.
function startProfileVerification(profileIndex, minutes) {
  console.info(
    "startProfileVerification is not implemented",
    profileIndex,
    minutes,
  );
}

function idleStatus() {
  const daemon = {
    ready: "",
    missing: " · Root service not installed",
    outdated: " · Root service needs an update",
  }[appState.daemon];
  return {
    running: appState.runningProfile
      ? "Running " + appState.runningProfile
      : "Running stock settings",
    detail:
      (appState.bootProfile
        ? "At startup: " + appState.bootProfile
        : "At startup: stock settings") +
      " · " +
      gpuLabel() +
      daemon,
  };
}

function renderShellStatus() {
  const band = document.getElementById("globalStatus");
  if (band) {
    const s = appState.status || idleStatus(),
      [running, detail] = band.querySelectorAll("span");
    running.textContent = s.running || "";
    detail.textContent = s.detail || "";
    band.title = [s.running, s.detail].filter(Boolean).join("\n");
    const tone = appState.status?.tone;
    band.classList.toggle("busy", !!appState.status && !tone);
    band.classList.toggle("error", tone === "error");
    band.classList.toggle("warn", tone === "warn");
  }
  const bar = document.getElementById("headProgress");
  if (bar) {
    const p = appState.progress;
    bar.hidden = !p;
    if (p) {
      bar.querySelector("span").textContent = p.label;
      bar.querySelector("progress").value = p.pct;
      bar.title = p.tooltip || p.label;
    }
  }
}

// Prototype controls: simulated states the mock has no backend for.
const prototypeChoices = [
  ["daemon", "Root service", ["ready", "missing", "outdated"], ["Ready", "Not installed", "Outdated"]],
  ["launcherWriteState", "Launcher setup", ["ready", "not-set-up", "needs-restart"], ["Ready", "Steam not set up", "Steam needs restart"]],
];
const prototypeToggles = [
  ["twoGpus", "Two graphics cards", () => appState.gpus.length > 1],
  ["emptyLibrary", "Empty game library", () => appState.emptyLibrary],
  ["dependencyDownload", "Download dependencies on first scan", () => appState.simulate.dependencyDownload],
  ["crashRecovery", "Recover from a crash on the next scan", () => appState.simulate.crashRecovery],
  ["finalVerificationFailure", "Fail final verification", () => appState.simulate.finalVerificationFailure],
];
// One-shot actions for flows that otherwise only open from a URL fragment.
const prototypeActions = [
  ["Run startup checks", () => { closePanel(); runStartupChecks({ nvidiaGpu: false }); }],
  ["Show an error report", () => showErrorDetails(...sampleProcessError())],
];
function prototypeControls() {
  showPanel(
    '<h2>Prototype controls</h2><p class="sub">Simulate states the sample data cannot reach on its own.</p><div class="prefrows gap">' +
      prototypeChoices
        .map(([key, label, values, names]) =>
          row(
            label,
            "",
            '<select aria-label="' +
              label +
              '" onchange="setPrototype(\'' +
              key +
              "',this.value)\">" +
              values
                .map(
                  (v, i) =>
                    "<option value=\"" +
                    v +
                    '"' +
                    (appState[key] === v ? " selected" : "") +
                    ">" +
                    names[i] +
                    "</option>",
                )
                .join("") +
              "</select>",
          ),
        )
        .join("") +
      prototypeToggles
        .map(([key, label, on]) =>
          row(
            label,
            "",
            '<button class="track" role="switch" data-proto="' +
              key +
              '" aria-label="' +
              label +
              '" aria-checked="' +
              on() +
              '" onclick="setPrototype(\'' +
              key +
              "',!(" +
              "this.getAttribute('aria-checked')==='true'))\"><i></i></button>",
          ),
        )
        .join("") +
      "</div>" +
      '<div class="dialogfoot protoFoot">' +
      prototypeActions
        .map(([label], i) => '<button onclick="prototypeActions[' + i + '][1]()">' + label + "</button>")
        .join("") +
      '<button onclick="closePanel()">Close</button></div>',
  );
}
function setPrototype(key, value) {
  if (key === "twoGpus") {
    appState.gpus = GPU_CHOICES.slice(0, value ? 2 : 1);
    appState.selectedGpu = Math.min(appState.selectedGpu, appState.gpus.length - 1);
  } else if (key in appState.simulate) appState.simulate[key] = value;
  else appState[key] = value;
  render();
  prototypeControls();
  document
    .querySelector('#panel [data-proto="' + key + '"]')
    ?.focus({ preventScroll: true });
}
function navigate(t) {
  tab = t;
  report = false;
  render();
  window.scrollTo(0, 0);
  M.focus({ preventScroll: true });
}
auditBtn.onclick = () => navigate("UX audit");
protoBtn.onclick = prototypeControls;
function render() {
  document.body.dataset.tab = tab;
  shellTools();
  renderShellStatus();
  document.querySelector("nav").innerHTML = Object.entries(tabs)
    .map(
      ([t, i]) =>
        "<button " +
        (tab === t ? 'aria-current="page"' : "") +
        ' onclick="navigate(this.innerText)"><img alt="" src="' +
        assets[i] +
        '">' +
        t +
        "</button>",
    )
    .join("");
  M.innerHTML =
    tab === "Game Library"
      ? report
        ? historyView()
        : library()
      : tab === "Profiles"
        ? profileView()
        : tab === "Auto UV"
          ? scanView()
          : tab === "UX audit"
            ? audit()
            : tab === "Design system"
              ? designSystem()
              : overlay();
  if (tab === "Game Library" && !report) {
    document.querySelector("#search").value = q;
    filterList();
  }
  requestAnimationFrame(draw);
}
