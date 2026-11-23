// Game Library: the merged launcher list, each game's settings, the launch
// lifecycle and the Perf Profile entry. The mock has no backend, so launcher
// reads, writes and sessions are simulated with short timers. Copy follows the
// real panel (ui/components/game_library_panel.py and the launcher adapters).
let sortLib = "Recently played",
  silentFan = false;
const launchers = ["Steam", "Heroic", "Lutris", "Faugus"];
// Registry order, as the real empty state names them.
const LAUNCHER_NAMES = ["Steam", "Lutris", "Heroic", "Faugus Launcher"];
const SYSTEM_TARGET_FPS = 60,
  MIN_TARGET_FPS = 15,
  MAX_TARGET_FPS = 1000,
  STEAM_USER = "jp";
const ACTIVE_STATES = [
  "launching",
  "running",
  "external",
  "stopping",
  "unconfirmed",
];
// Real timings (5 s settle, 120 s stall) compressed so the mock stays usable.
const LIB_TIMING = {
  confirm: 1200,
  settle: 1600,
  stall: 4000,
  stop: 1400,
  write: 450,
  scan: 1100,
  firstScan: 900,
  action: 1500,
  sessionLost: 2600,
  quietRefresh: 30000,
};
const OPENGL_REASON =
  "Game not compatible with Overlay or Adaptive: OpenGL detected. These features need Vulkan frame data. Fixed GPU profiles remain available.";
const PERF_HINT = "Click Perf Profile to open the full view";

[
  ["Control", "control", "Heroic", "Epic"],
  ["Dishonored", "dishonored", "Lutris", "Wine"],
  ["Hollow Knight", "hollow-knight", "Faugus", "Proton"],
  ["Oxenfree", "388880", "Steam", "Native Linux"],
  ["Hades II", "1145350", "Steam", "Proton"],
].forEach(([name, id, launcher, runner]) =>
  games.push({
    name,
    id: launcher === "Steam" ? id : "demo-" + launcher.toLowerCase(),
    launcher,
    runner,
    mins: 0,
    cap: true,
  }),
);
// Per-game facts a launcher reports. Indices match the native fixture.
const GAME_FACTS = [
  { effective: "Proton 9.0-4" },
  { effective: "Proton Experimental" },
  { compat: "GE-Proton9-11" },
  { effective: "Proton 9.0-4" },
  { baseCommand: "gamemoderun", inherited: "Heroic global settings" },
  {},
  {},
  { native: true, overlayOk: false },
  { effective: "Proton 9.0-4", ready: false },
];
games.forEach((game, i) =>
  Object.assign(
    game,
    {
      launcher: game.launcher || "Steam",
      runner: game.runner || "Proton",
      wrapped: ![4, 6, 8].includes(i),
      mode: "Stock",
      overlay: true,
      target: SYSTEM_TARGET_FPS,
      override: false,
      compat: "",
      played: [40, 23, 61, 15, 3, 14, 5, 4, 0][i] ?? 0,
      command: null,
      baseCommand: null,
      inherited: "",
      ready: true,
      native: false,
      overlayOk: true,
      gpu: "",
      flatpakReady: true,
      effective: "",
      power: 0,
      latency_ms: 0,
      recTier: "",
      session: { state: "stopped", note: "", warning: "", pending: false },
    },
    GAME_FACTS[i] || {},
  ),
);

// Prototype controls for states the sample data cannot reach by itself.
// Extends the shell's lists; shell.js applies and re-renders them.
Object.assign(appState, {
  launchOutcome: appState.launchOutcome || "starts",
  libraryScan: appState.libraryScan || "ok",
  heroicInstall: appState.heroicInstall || "flatpak",
  externalGame: !!appState.externalGame,
});
(function extendPrototypeControls() {
  const writeChoice = prototypeChoices.find(
    (c) => c[0] === "launcherWriteState",
  );
  if (writeChoice && !writeChoice[2].includes("stopped")) {
    writeChoice[2].push("stopped");
    writeChoice[3].push("Steam stopped");
  }
  const add = (list, entry) =>
    list.some((c) => c[0] === entry[0]) || list.push(entry);
  add(prototypeChoices, [
    "libraryScan",
    "Library scan",
    ["ok", "heroic-error", "no-launcher"],
    ["All launchers read", "Heroic read fails", "No launcher found"],
  ]);
  add(prototypeChoices, [
    "heroicInstall",
    "Heroic installation",
    ["flatpak", "native"],
    ["Flatpak", "Native package"],
  ]);
  add(prototypeChoices, [
    "launchOutcome",
    "Next game launch",
    ["starts", "profile", "session", "fails", "unconfirmed"],
    [
      "Starts normally",
      "Profile unconfirmed",
      "Session connection lost",
      "Launch command fails",
      "Launch not confirmed",
    ],
  ]);
  add(prototypeToggles, [
    "externalGame",
    "The Last of Us Part I runs outside PenguinBurner",
    () => appState.externalGame,
  ]);
})();

let filtersOpen = false,
  launcherFilter = "All launchers",
  wrappedFilter = "Any setup",
  recordFilter = "Any history",
  panelScroll = 0;
const lib = {
  scan: "initial", // initial | idle | scanning
  message: "",
  busy: false,
  action: false, // a launcher write action (Steam setup or restart) is running
  draft: null, // {index, value} while a command edit is unsaved
  menu: null,
  side: null, // splitter width in px, null for the default
  ownsStatus: false,
};
try {
  lib.side = Number(localStorage.getItem("pb-library-side")) || null;
} catch {
  lib.side = null;
}

// ---------- Pure model helpers ----------

function libTimer(fn, ms) {
  return setTimeout(fn, ms);
}
function launcherAvailable(launcher) {
  if (appState.libraryScan === "no-launcher") return false;
  return !(appState.libraryScan === "heroic-error" && launcher === "Heroic");
}
function libraryGames() {
  return appState.emptyLibrary
    ? []
    : games.filter((x) => launcherAvailable(x.launcher));
}
function availableLaunchers() {
  return launchers.filter(launcherAvailable);
}
function joinNames(names) {
  return names.length < 2
    ? names.join("")
    : names.slice(0, -1).join(", ") + " and " + names.at(-1);
}
function libraryPlaceholder() {
  if (!availableLaunchers().length)
    return (
      "No game launcher found. PenguinBurner looks for " +
      LAUNCHER_NAMES.join(", ") +
      ". Install one, then press Rescan."
    );
  if (!libraryGames().length)
    return "A launcher is installed but has no games yet. Install one, then press Rescan.";
  return "";
}
// Whether the launcher would take a write now (Steam holds its launch options
// in memory while the client runs). Other launchers write their own files.
function writeState(launcher) {
  if (launcher !== "Steam")
    return { ready: true, live: false, note: "", summary: "" };
  const note = "Steam user: " + STEAM_USER;
  return (
    {
      "not-set-up": {
        ready: false,
        summary: "not set up",
        note,
        action: "Scan my Steam library",
        detail:
          "Find your installed games and connect them to PenguinBurner for simple per-game profiles.",
      },
      "needs-restart": {
        ready: false,
        summary: "read-only until initialized",
        note,
        action: "Restart Steam to finish",
        detail:
          "Restart Steam once to finish connecting per-game profiles. Your games and saved settings stay exactly as they are.",
        confirm: "Cleanly shut down and relaunch the Steam client now?",
      },
      stopped: { ready: true, summary: "Steam stopped", note },
    }[appState.launcherWriteState] || {
      ready: true,
      live: true,
      summary: "live apply",
      note,
    }
  );
}
function libraryStatusText(list, message, state) {
  const total = list.length,
    configured = list.filter((x) => x.wrapped).length;
  return [
    total + (total === 1 ? " game" : " games"),
    configured + " configured",
    state?.note,
    state?.summary,
    message,
  ]
    .filter(Boolean)
    .join(" · ");
}
function gameMetadata(x) {
  const launcher =
    x.launcher === "Heroic" && appState.heroicInstall === "flatpak"
      ? "Heroic (Flatpak)"
      : x.launcher;
  return [launcher, x.runner, x.played ? x.played.toFixed(1) + " h played" : ""]
    .filter(Boolean)
    .join(" · ");
}
function gameTooltip(x) {
  return [x.name, gameMetadata(x), x.ready ? "" : "Not installed"]
    .filter(Boolean)
    .join("\n");
}
function canLaunch(x) {
  // The Faugus config is present but its client is not installed here.
  return x.launcher !== "Faugus";
}
function isActive(x) {
  return ACTIVE_STATES.includes(x.session.state);
}
function otherActiveGame(i) {
  const other = games.find((x, j) => j !== i && isActive(x));
  return other ? other.name : "";
}
// The Play button's text, state and tooltip for what has been observed.
function playView(x, i) {
  const s = x.session,
    blocking = otherActiveGame(i);
  let view;
  if (s.state === "launching" && s.stalled)
    view = { text: "Play", enabled: !blocking, state: "idle" };
  else if (s.state === "launching" || s.pending)
    view = { text: "Starting…", enabled: false, state: "starting" };
  else if (s.state === "running")
    view = { text: "Stop", enabled: true, state: "running" };
  else if (s.state === "external")
    view = { text: "Running", enabled: false, state: "external" };
  else if (s.state === "unconfirmed")
    view = { text: "Play", enabled: !blocking, state: "idle" };
  else if (s.state === "stopping")
    view = { text: "Stopping…", enabled: true, state: "stopping" };
  else view = { text: "Play", enabled: !blocking, state: "idle" };
  view.enabled = view.enabled && !lib.busy;
  view.tip =
    blocking && !isActive(x)
      ? blocking + " is still running. Stop it before launching another game."
      : s.note;
  return view;
}
const STEAM_TOOLS = [
  ["proton_experimental", "Proton Experimental"],
  ["proton_9", "Proton 9.0-4"],
  ["GE-Proton9-20", "GE-Proton9-20"],
];
const COMPAT = {
  Steam: {
    guidance:
      "Keeps Steam's current choice by default. Selecting another entry uses Steam's own compatibility-tool setting for this game.",
  },
  Heroic: {
    guidance:
      "Applies on the next launch. Use Play here to refresh Heroic's saved settings.",
  },
  Lutris: {
    guidance:
      "Applies on the next launch. Close Lutris's game settings before editing here.",
    tools: [
      ["wine-ge-8-26-x86_64", "wine-ge-8-26-x86_64"],
      ["lutris-GE-Proton8-26-x86_64", "lutris-GE-Proton8-26-x86_64"],
      ["system", "System Wine"],
    ],
  },
  Faugus: {
    guidance:
      "Applies on the next launch. Close Faugus's game settings before editing here.",
    tools: [
      ["GE-Proton9-20", "GE-Proton9-20"],
      ["UMU-Proton-9.0-3", "UMU-Proton-9.0-3"],
    ],
  },
};
function compatTools(launcher) {
  if (launcher === "Steam") return STEAM_TOOLS;
  if (launcher === "Heroic")
    // A Flatpak Heroic only sees the tools inside its own sandbox.
    return appState.heroicInstall === "flatpak"
      ? [
          ["Wine-GE-Proton8-26", "Wine-GE-Proton8-26"],
          ["GE-Proton9-20", "GE-Proton9-20"],
        ]
      : [
          ["Wine-GE-Proton8-26", "Wine-GE-Proton8-26"],
          ["GE-Proton9-20", "GE-Proton9-20"],
          ["proton_experimental", "Proton Experimental (Steam)"],
        ];
  return COMPAT[launcher].tools;
}
// The shared compatibility picker: default names the effective tool, a saved
// tool that is gone stays visible as "(not listed)".
function compatField(x) {
  const steam = x.launcher === "Steam",
    native = x.native && !x.compat,
    live = !steam || writeState("Steam").live;
  const defaultLabel = steam
    ? x.compat
      ? "Steam default"
      : x.effective
        ? "Steam default (" + x.effective + ")"
        : native
          ? "Native Linux — no compatibility tool"
          : "Steam default"
    : x.launcher + " default";
  const tools = steam && !live ? [] : compatTools(x.launcher);
  const choices = [["", defaultLabel], ...tools];
  if (x.compat && !choices.some(([v]) => v === x.compat))
    choices.push([x.compat, x.compat + " (not listed)"]);
  const error = live
    ? ""
    : "Needs live apply: Proton is Steam's own setting, so the client has to be running and connected to change it.";
  return {
    choices,
    value: x.compat,
    enabled: !native && !error,
    subtitle: native
      ? "This game does not use Wine or Proton."
      : error || COMPAT[x.launcher].guidance,
  };
}
function defaultCmd(x) {
  const steam = x.launcher === "Steam";
  if (!x.wrapped) return x.baseCommand ?? (steam ? "%command%" : "");
  return (
    "PENGUIN_BURNER " +
    (x.overlay ? "--pb-overlay=1" : "--pb-overlay=0") +
    (steam
      ? " %command%"
      : " --pb-game-id=" + x.launcher.toLowerCase() + ":" + x.id)
  );
}
function commandText(x) {
  return x.command ?? defaultCmd(x);
}
function commandSubtitle(x) {
  if (x.launcher === "Heroic" && x.wrapped && appState.heroicInstall === "flatpak")
    return x.flatpakReady
      ? "Configured for Flatpak; Play refreshes Heroic's saved launch settings."
      : "Flatpak setup required: turn Wrap this game off and on, then use Play.";
  const inherited = !x.wrapped && x.inherited && commandText(x);
  return {
    Steam: "Steam launch options — %command% is where the game goes",
    Heroic: inherited
      ? "wrapper_command — inherited from " + x.inherited
      : "Wrapper command in the Heroic settings",
    Lutris: inherited
      ? "prefix_command — inherited from " + x.inherited
      : "prefix_command in the Lutris config",
    Faugus: "Launch arguments in the Faugus game settings",
  }[x.launcher];
}
// Steam validates launch options before it writes them.
function commandProblems(launcher, text) {
  if (launcher !== "Steam") return [];
  const problems = [];
  if ((text.match(/%command%/g) || []).length > 1)
    problems.push("more than one %command% token");
  let quote = "";
  for (let k = 0; k < text.length; k++) {
    const ch = text[k];
    if (ch === "\\" && quote !== "'") k++;
    else if (quote ? ch === quote : ch === '"' || ch === "'")
      quote = quote ? "" : ch;
  }
  if (quote) problems.push("unbalanced quotes");
  return problems;
}
function clampTarget(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return SYSTEM_TARGET_FPS;
  return Math.min(
    MAX_TARGET_FPS,
    Math.max(MIN_TARGET_FPS, Math.round(n / 5) * 5),
  );
}
const BULK_ACTIONS = [
  {
    label: "Enable PenguinBurner for all games",
    affects: "wrapped",
    value: true,
    confirm:
      'Add the PenguinBurner wrapper to the launch command of {count} {games}?\n\nSaved overlay choices are preserved, and MangoHud is disabled in wrapped games. "Disable PenguinBurner for all games" restores each game\'s own launch command.',
  },
  {
    label: "Disable PenguinBurner for all games",
    affects: "wrapped",
    value: false,
    confirm:
      "Remove the PenguinBurner wrapper from {count} {games} and restore their own launch command?",
  },
  {
    label: "Show In-Game overlay for enabled games",
    affects: "overlay",
    value: true,
    enabledOnly: true,
    confirm: "Show the In-Game overlay in {count} PenguinBurner-enabled {games}?",
  },
  {
    label: "Hide In-Game overlay for enabled games",
    affects: "overlay",
    value: false,
    enabledOnly: true,
    confirm: "Hide the In-Game overlay in {count} {games}?",
  },
];
function bulkCandidates(action, list) {
  return list.filter(
    (x) =>
      (x.wrapped || !action.enabledOnly) &&
      (action.affects !== "overlay" || !action.value || x.overlayOk),
  );
}
// Only launchers that would take a write now: a Steam that is not set up or
// waits for a restart keeps its games as they are, like the per-game switch.
function bulkScope(action, list = libraryGames()) {
  return bulkCandidates(action, list).filter((x) => writeState(x.launcher).ready);
}
// Launchers left out of a bulk change that would have changed their games.
function bulkSkipped(action, list = libraryGames()) {
  const skipped = new Map();
  bulkCandidates(action, list)
    .filter((x) => !!x[action.affects] !== action.value && !writeState(x.launcher).ready)
    .forEach((x) => skipped.set(x.launcher, writeState(x.launcher)));
  return [...skipped];
}
function bulkWouldChange(action, list) {
  return bulkScope(action, list).some((x) => !!x[action.affects] !== action.value);
}
function bulkConfirmText(action, count) {
  return action.confirm
    .replace("{count}", count)
    .replace("{games}", count === 1 ? "game" : "games");
}
function modeLabel(mode) {
  return mode === "Stock" ? "Stock (factory GPU state)" : mode;
}
// The Perf Profile entry follows the recording ring history.js keeps
// (perfEntryInfo); this fallback only covers a build without it.
function perfState(x) {
  if (typeof perfEntryInfo === "function") return perfEntryInfo(x);
  return x.mins
    ? { enabled: true, peek: false, live: false, tip: "Game Perf Profile — click to open the full view." }
    : { enabled: false, peek: false, live: false, tip: "Game Perf Profile unavailable — no telemetry recorded for this game yet." };
}

// ---------- Lists, filters and sorting ----------

function selectOptions(label, items, value, handler, id) {
  return (
    '<select id="' +
    id +
    '" aria-label="' +
    label +
    '" onchange="' +
    handler +
    '">' +
    items
      .map(
        (v) =>
          "<option " + (value === v ? "selected" : "") + ">" + v + "</option>",
      )
      .join("") +
    "</select>"
  );
}
function matches() {
  const shown = libraryGames();
  const list = games
    .map((x, i) => [x, i])
    .filter(
      ([x]) =>
        shown.includes(x) &&
        x.name.toLowerCase().includes(q.toLowerCase()) &&
        (launcherFilter === "All launchers" || x.launcher === launcherFilter) &&
        (wrappedFilter === "Any setup" ||
          x.wrapped === (wrappedFilter === "Wrapped")) &&
        (recordFilter === "Any history" ||
          !!x.mins === (recordFilter === "Recorded")),
    );
  if (sortLib === "Alphabetical")
    list.sort(([a], [b]) => a.name.localeCompare(b.name));
  if (sortLib === "Launcher")
    list.sort(
      ([a], [b]) =>
        a.launcher.localeCompare(b.launcher) || a.name.localeCompare(b.name),
    );
  if (sortLib === "Most played") list.sort(([a], [b]) => b.played - a.played);
  if (sortLib === "Recently installed") list.reverse();
  return list;
}
function launcherIcon(game) {
  return assets["tab-" + game.launcher.toLowerCase()];
}
function gameArtwork(game) {
  const art = assets[game.id + "cover"];
  return `<span class="gameArt"><img alt="" class="cover" src="${art || launcherIcon(game)}">
    ${art ? `<img class="launcherMark" src="${launcherIcon(game)}" alt="">` : ""}</span>`;
}
function gameList() {
  return matches()
    .map(([game, i]) => {
      const live = isActive(game) ? '<i class="livedot" title="Running"></i>' : "";
      return `<button class="game ${idx === i ? "active" : ""} ${game.wrapped ? "" : "unwrapped"}"
    data-game="${i}" aria-pressed="${idx === i}" title="${esc(gameTooltip(game))}" aria-label="${esc(game.name)}, ${game.wrapped ? "wrapped" : "not wrapped"}${game.ready ? "" : ", not installed"}"
    onclick="selectGame(${i})">${gameArtwork(game)}<span class="gametext"><strong><span class="wrapmark" aria-hidden="true">${game.wrapped ? "●" : "○"}</span>${esc(game.name)}${live}</strong>
    <small>${game.launcher} · ${game.played} h</small></span></button>`;
    })
    .join("") ||
    (libraryGames().length
      ? '<div class="emptymsg">No matching games</div>'
      : "");
}
function filterCount() {
  return (
    Number(launcherFilter !== "All launchers") +
    Number(wrappedFilter !== "Any setup") +
    Number(recordFilter !== "Any history")
  );
}
function listTools() {
  const empty = !libraryGames().length;
  return (
    '<div class="bulk"><label>Sort ' +
    selectOptions(
      "Library sort",
      [
        "Recently played",
        "Most played",
        "Alphabetical",
        "Recently installed",
        "Launcher",
      ],
      sortLib,
      "sortLib=this.value;updateFilters()",
      "libSort",
    ) +
    '</label><button id="filtersBtn" aria-expanded="' +
    filtersOpen +
    '" onclick="filtersOpen=!filtersOpen;render()"' +
    (empty ? " disabled" : "") +
    ">Filters" +
    (filterCount() ? " (" + filterCount() + ")" : "") +
    '</button><button id="allGames" class="menubtn" aria-haspopup="menu" aria-expanded="' +
    !!lib.menu +
    '" onclick="openBulkMenu(this)"' +
    (empty ? " disabled" : lib.busy ? ' aria-disabled="true"' : "") +
    ">All games</button></div>" +
    (filtersOpen && !empty
      ? '<div class="filterbody">' +
        [
          [
            "Launcher",
            ["All launchers", ...availableLaunchers()],
            launcherFilter,
            "launcherFilter",
            "filterLauncher",
          ],
          [
            "Wrapper",
            ["Any setup", "Wrapped", "Unwrapped"],
            wrappedFilter,
            "wrappedFilter",
            "filterWrapper",
          ],
          [
            "Recording",
            ["Any history", "Recorded", "No recording"],
            recordFilter,
            "recordFilter",
            "filterRecording",
          ],
        ]
          .map(
            ([l, vs, v, k, id]) =>
              "<label>" +
              l +
              selectOptions(l, vs, v, k + "=this.value;updateFilters()", id) +
              "</label>",
          )
          .join("") +
        '<button id="clearFilters" onclick="resetFilters()">Clear filters</button></div>'
      : "")
  );
}
function resetFilters() {
  launcherFilter = "All launchers";
  wrappedFilter = "Any setup";
  recordFilter = "Any history";
  q = "";
  updateFilters();
}
function updateFilters(searching = false) {
  const scroll = document.querySelector(".list")?.scrollTop || 0,
    caret = document.querySelector("#search")?.selectionStart || 0;
  const match = matches();
  if (match.length && !match.some(([, i]) => i === idx)) {
    if (!flushCommand()) return render();
    idx = match[0][1];
  }
  render();
  document.querySelector(".list").scrollTop = scroll;
  if (searching) {
    let ip = document.querySelector("#search");
    ip.focus({ preventScroll: true });
    ip.setSelectionRange(caret, caret);
  }
}
function filterList() {
  const list = document.querySelector(".list"),
    key = focusKey(document.activeElement),
    row = key && key.startsWith(".game") && list.contains(document.activeElement);
  list.innerHTML = gameList();
  lib.listSig = listSignature();
  if (row) list.querySelector(key)?.focus({ preventScroll: true });
}
// A selector that finds the focused control again after a re-render.
function focusKey(el) {
  if (!el || el === document.body) return null;
  if (el.id) return "#" + el.id;
  if (el.dataset?.switch) return '[data-switch="' + el.dataset.switch + '"]';
  if (el.dataset?.game) return '.game[data-game="' + el.dataset.game + '"]';
  return null;
}
// Re-render, keeping the list scroll and the focused control (and its caret).
// A caller may name the control its result belongs to; that control takes
// focus back only while focus is still on it or has fallen to the page, so a
// late write result never pulls focus from where the user has moved on to.
function libRefresh(focus) {
  const list = document.querySelector(".list")?.scrollTop || 0,
    active = document.activeElement,
    idle = !active || active === document.body,
    own = focusKey(active),
    key = focus && (idle || active.matches?.(focus)) ? focus : own,
    caret =
      key && key === own && "selectionStart" in active
        ? [active.selectionStart, active.selectionEnd]
        : null;
  render();
  const l = document.querySelector(".list");
  if (l) l.scrollTop = list;
  const target = key && document.querySelector(key);
  if (target && target !== document.activeElement) {
    target.focus({ preventScroll: true });
    if (caret && "setSelectionRange" in target) {
      try {
        target.setSelectionRange(...caret);
      } catch {
        /* number inputs have no caret */
      }
    }
  }
}

// ---------- All games bulk menu ----------

function closeBulkMenu(restore = true) {
  if (!lib.menu) return;
  lib.menu.remove();
  lib.menu = null;
  const button = document.getElementById("allGames");
  button?.setAttribute("aria-expanded", "false");
  if (restore) button?.focus({ preventScroll: true });
}
function openBulkMenu(button) {
  if (lib.menu) return closeBulkMenu();
  if (lib.busy) return;
  const list = libraryGames();
  const menu = document.createElement("div");
  menu.className = "contextMenu libmenu";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "All games");
  menu.tabIndex = -1;
  BULK_ACTIONS.forEach((action, k) => {
    const b = document.createElement("button");
    b.setAttribute("role", "menuitem");
    b.textContent = action.label;
    // A direction that would change nothing is a readout, not an action.
    b.disabled = !bulkWouldChange(action, list);
    if (k === 2) b.className = "startGroup";
    b.onclick = () => {
      closeBulkMenu(false);
      confirmBulk(k);
    };
    menu.append(b);
  });
  document.body.append(menu);
  lib.menu = menu;
  button.setAttribute("aria-expanded", "true");
  const rect = button.getBoundingClientRect();
  menu.style.width = "324px";
  menu.style.left =
    Math.max(4, Math.min(rect.left, innerWidth - menu.offsetWidth - 5)) + "px";
  menu.style.top =
    Math.max(4, Math.min(rect.bottom + 3, innerHeight - menu.offsetHeight - 5)) +
    "px";
  (menu.querySelector("button:not(:disabled)") || menu).focus({
    preventScroll: true,
  });
  menu.onkeydown = (e) => {
    const items = [...menu.querySelectorAll("button:not(:disabled)")],
      current = items.indexOf(document.activeElement);
    const next = {
      ArrowDown: (current + 1) % items.length,
      ArrowUp: (current - 1 + items.length) % items.length,
      Home: 0,
      End: items.length - 1,
    }[e.key];
    if (next !== undefined && items.length) {
      e.preventDefault();
      items[next].focus();
    }
    if (e.key === "Escape" || e.key === "Tab") {
      if (e.key === "Escape") e.preventDefault();
      closeBulkMenu(e.key === "Escape");
    }
  };
}
document.addEventListener("pointerdown", (e) => {
  if (lib.menu && !e.target.closest?.(".libmenu,#allGames")) closeBulkMenu(false);
});
window.addEventListener("resize", () => closeBulkMenu(false));
function confirmBulk(k) {
  const action = BULK_ACTIONS[k],
    scope = bulkScope(action);
  if (!scope.length || !flushCommand()) return;
  if (
    action.affects === "wrapped" &&
    action.value &&
    appState.gpus.length > 1 &&
    scope.some((x) => x.gpu === "")
  ) {
    setLibraryMessage("Choose a Game GPU for each game before enabling all games.");
    return;
  }
  const skipped = bulkSkipped(action)
    .map(([launcher, ws]) => "Skipped: " + launcher + " (" + ws.summary + "). Use " + ws.action + " first.")
    .join(" ");
  const text = (bulkConfirmText(action, scope.length) + (skipped ? "\n\n" + skipped : ""))
    .split("\n\n")
    .map((p) => '<p class="sub">' + esc(p) + "</p>")
    .join("");
  showPanel(
    "<h2>All games</h2>" +
      text +
      '<div class="dialogfoot"><button onclick="closePanel();document.getElementById(\'allGames\')?.focus()">Cancel</button><button class="primary" id="bulkGo" onclick="applyBulk(' +
      k +
      ')">' +
      esc(action.label.replace(/ for .*/, "")) +
      "</button></div>",
  );
  document.getElementById("bulkGo")?.focus();
}
function applyBulk(k) {
  const action = BULK_ACTIONS[k],
    scope = bulkScope(action),
    skipped = bulkSkipped(action);
  closePanel();
  lib.busy = true;
  setLibraryMessage("Saving changes for " + scope.length + " games…");
  libTimer(() => {
    const per = {};
    scope.forEach((x) => {
      if (action.affects === "wrapped") setWrapped(x, action.value);
      else x.overlay = action.value;
      x.command = null;
      per[x.launcher] = (per[x.launcher] || 0) + 1;
    });
    lib.busy = false;
    setLibraryMessage(
      Object.entries(per)
        .map(
          ([launcher, n]) =>
            launcher.toLowerCase() +
            ": " +
            n +
            (n === 1 ? " game" : " games") +
            (launcher === "Steam" && writeState("Steam").live
              ? " applied live"
              : " saved"),
        )
        .concat(skipped.map(([launcher, ws]) => launcher.toLowerCase() + ": skipped (" + ws.summary + ")"))
        .join("; "),
      "#allGames",
    );
  }, LIB_TIMING.write * 2);
}

// ---------- Settings writes ----------

function setLibraryMessage(message, focus, soft = false) {
  lib.message = message;
  if (tab !== "Game Library" || report) return;
  const line = document.querySelector(".libstatus");
  // A soft update leaves the page alone: a blur-time save must not replace
  // the element the pointer is about to click.
  if (soft && line)
    line.textContent = libraryStatusText(
      libraryGames(),
      message,
      writeState(g().launcher),
    );
  else libRefresh(focus);
}
function setWrapped(x, on) {
  x.wrapped = on;
  x.command = null;
  if (on && x.launcher === "Heroic") x.flatpakReady = true;
}
// Every launcher write: "saving…", then the launcher's own result.
function writeSetting(x, apply, result, focus, soft = false) {
  apply();
  lib.busy = true;
  setLibraryMessage(x.name + ": saving…", focus, soft);
  libTimer(() => {
    lib.busy = false;
    setLibraryMessage(typeof result === "function" ? result() : result, focus);
  }, LIB_TIMING.write);
}
function savedResult(x) {
  if (x.launcher !== "Steam") return x.name + ": saved.";
  return writeState("Steam").live ? "Applied live." : "Applied to Steam config.";
}
function runningWrapped(x) {
  return x.session.state === "running" && x.wrapped;
}
function setWrap(on) {
  const x = g();
  if (on && appState.gpus.length > 1 && x.gpu === "") {
    setLibraryMessage(
      "Choose a Game GPU before enabling PenguinBurner.",
      '[data-switch="wrapped"]',
    );
    return;
  }
  writeSetting(x, () => setWrapped(x, on), () => savedResult(x), '[data-switch="wrapped"]');
}
function setMode(mode) {
  const x = g();
  writeSetting(
    x,
    () => (x.mode = mode),
    () =>
      runningWrapped(x)
        ? modeLabel(mode).split(" ")[0] +
          " applied to the running game; verified with daemon."
        : "Mode saved for next launch; no active profile for this game.",
    "#gameMode",
  );
}
function setOverride(on) {
  const x = g();
  writeSetting(
    x,
    () => (x.override = on),
    () =>
      runningWrapped(x) && x.mode === "Adaptive"
        ? "Adaptive target applied to the running game."
        : savedResult(x),
    '[data-switch="override"]',
  );
}
function setTarget(input) {
  const x = g(),
    value = clampTarget(input.value);
  writeSetting(
    x,
    () => (x.target = value),
    () =>
      runningWrapped(x)
        ? "Adaptive target applied to the running game."
        : savedResult(x),
    "#gameTarget",
  );
}
function setGpu(value) {
  const x = g();
  writeSetting(
    x,
    () => (x.gpu = value),
    () =>
      runningWrapped(x)
        ? "GPU saved; relaunch the game to change its GPU."
        : savedResult(x),
    "#gameGpu",
  );
}
function setOverlay(on) {
  const x = g();
  writeSetting(
    x,
    () => {
      x.overlay = on;
      x.command = null;
    },
    () =>
      runningWrapped(x)
        ? "Overlay " +
          (on ? "show" : "hide") +
          " request sent live; loaded overlays update within one second."
        : savedResult(x),
    '[data-switch="overlay"]',
  );
}
function selectedSetting(k, v) {
  if (k === "wrapped") return setWrap(v);
  if (k === "override") return setOverride(v);
  if (k === "overlay") return setOverlay(v);
  const x = g();
  writeSetting(
    x,
    () => (x[k] = v),
    () => savedResult(x),
    '[data-switch="' + k + '"]',
  );
}
function setCompat(value) {
  const x = g();
  writeSetting(
    x,
    () => (x.compat = value),
    () => "Compatibility tool saved. " + COMPAT[x.launcher].guidance,
    "#gameCompat",
  );
}
function capture() {
  selectedSetting("cap", !g().cap);
}

// ---------- Inline launch command ----------

function commandInput(el) {
  lib.draft = { index: idx, value: el.value };
}
function commandKey(e, el) {
  const multiline = el.tagName === "TEXTAREA";
  if (e.key === "Enter" && (!multiline || e.ctrlKey)) {
    e.preventDefault();
    flushCommand(true);
  }
  if (e.key === "Escape" && lib.draft) {
    lib.draft = null;
    libRefresh("#gameCommand");
  }
}
// Save an unsaved command edit. A refused save keeps the draft and the
// selection, and reports why. fromBlur saves without re-rendering.
function flushCommand(keepFocus = false, fromBlur = false) {
  const d = lib.draft;
  if (!d) return true;
  const x = games[d.index],
    value = d.value.trim(),
    focus = keepFocus ? "#gameCommand" : null;
  if (value === commandText(x).trim()) {
    lib.draft = null;
    return true;
  }
  const problems = commandProblems(x.launcher, value);
  if (problems.length) {
    setLibraryMessage(
      x.name + ": save failed (" + problems.join("; ") + ")",
      "#gameCommand",
      fromBlur,
    );
    document.getElementById("gameCommand")?.setAttribute("aria-invalid", "true");
    return false;
  }
  lib.draft = null;
  writeSetting(
    x,
    () => {
      x.command = value;
      // After a hand edit the command is the truth for the wrapper switch.
      x.wrapped = value.includes("PENGUIN_BURNER");
      x.inherited = "";
    },
    () => savedResult(x),
    focus,
    fromBlur,
  );
  return true;
}

// ---------- Launch lifecycle ----------

function syncSessions() {
  const outside = games[2];
  if (appState.externalGame && !isActive(outside)) {
    outside.session = {
      state: "external",
      note: "Game process detected. PenguinBurner integration is not yet confirmed.",
      warning: "",
      pending: false,
    };
  } else if (!appState.externalGame && outside.session.state === "external") {
    outside.session = { state: "stopped", note: "", warning: "", pending: false };
  }
  // launchLock: the one game launching, running or stopping (Play's lock).
  // runningGame: a game PenguinBurner launched wrapped and that now runs, the
  // one the overlay, the status band and the energy counter follow.
  const active = games.findIndex(isActive),
    tuned = games.findIndex((x) => x.session.state === "running" && x.session.tuned);
  appState.launchLock = active < 0 ? null : active;
  appState.runningGame = tuned < 0 ? null : tuned;
  const x = games[active];
  if (x && (x.session.state === "running" || x.session.state === "external")) {
    setGlobalStatus(
      x.session.state === "external"
        ? {
            running: x.name + " is running",
            detail: "Started outside PenguinBurner · not tuned",
          }
        : {
            running: "Playing " + x.name,
            // A tuned Adaptive game shows its live tier and the standing
            // profile (profiles.js runningProfileLabel).
            detail:
              ((appState.runningGame === active && x.mode === "Adaptive" && appState.runningProfile) ||
                modeLabel(x.mode).split(" ")[0]) +
              " · " +
              gpuLabel(x.gpu === "" ? appState.selectedGpu : Number(x.gpu)) +
              " · via " +
              x.launcher,
          },
    );
    lib.ownsStatus = true;
  } else if (lib.ownsStatus) {
    lib.ownsStatus = false;
    setGlobalStatus(null);
  }
}
function sessionTimer(x, fn, ms) {
  (x.session.timers ||= []).push(libTimer(fn, ms));
}
function clearSession(x) {
  (x.session.timers || []).forEach(clearTimeout);
  x.session.timers = [];
}
function sessionChanged(x, message) {
  if (message) lib.message = x.name + ": " + message;
  syncSessions();
  if (tab === "Game Library" && !report) libRefresh();
}
function playClicked() {
  const x = g(),
    i = idx;
  // Starting… and the like are aria-disabled, so focus stays on the button.
  if (!canLaunch(x) || !playView(x, i).enabled) return;
  const s = x.session.state;
  if (s === "running" || s === "stopping") stopGame(x);
  else if (s === "unconfirmed" || (s === "launching" && x.session.stalled))
    retryLaunch(i);
  else if (!isActive(x) && !otherActiveGame(i)) playGame(x);
}
function retryLaunch(i) {
  showPanel(
    '<h2>Retry launch</h2><p class="sub">The previous game\'s status is unknown. Launching again may start a second instance. Retry anyway?</p><div class="dialogfoot"><button onclick="closePanel();document.getElementById(\'playBtn\')?.focus()">Cancel</button><button class="primary" id="retryGo">Retry launch</button></div>',
  );
  const go = document.getElementById("retryGo");
  go.onclick = () => {
    closePanel();
    playGame(games[i]);
    document.getElementById("playBtn")?.focus({ preventScroll: true });
  };
  go.focus();
}
function playGame(x) {
  clearSession(x);
  // The wrapper is read at launch; toggling it later applies next time.
  x.session = { state: "launching", note: "", warning: "", pending: true, timers: [], tuned: x.wrapped };
  if (x.launcher === "Heroic" && x.wrapped) x.flatpakReady = true;
  sessionChanged(x, "preparing launch…");
  sessionTimer(x, () => sessionChanged(x, "launching via " + x.launcher + "…"), 400);
  const outcome = appState.launchOutcome;
  if (outcome === "fails") {
    sessionTimer(
      x,
      () => {
        x.session = {
          state: "stopped",
          note: "",
          warning: "PenguinBurner could not execute the game's launch command.",
          pending: false,
        };
        sessionChanged(x, "launch failed.");
      },
      LIB_TIMING.confirm,
    );
    return;
  }
  if (outcome === "unconfirmed") {
    sessionTimer(
      x,
      () => {
        Object.assign(x.session, {
          state: "unconfirmed",
          stalled: true,
          pending: false,
          note: "Launch request was not confirmed. Check the launcher before retrying.",
        });
        sessionChanged(x, "launch status unknown.");
      },
      LIB_TIMING.stall,
    );
    return;
  }
  sessionTimer(
    x,
    () => {
      Object.assign(x.session, {
        state: "running",
        note:
          outcome === "profile"
            ? "PenguinBurner connected; GPU profile application is unconfirmed."
            : "",
      });
      sessionChanged(x);
    },
    LIB_TIMING.confirm,
  );
  sessionTimer(
    x,
    () => {
      x.session.pending = false;
      sessionChanged(x, "running.");
    },
    LIB_TIMING.confirm + LIB_TIMING.settle,
  );
  if (outcome === "session")
    sessionTimer(
      x,
      () => {
        x.session.note =
          "Session connection lost; game status will be confirmed after reconnecting.";
        sessionChanged(x, "session connection lost.");
      },
      LIB_TIMING.confirm + LIB_TIMING.settle + LIB_TIMING.sessionLost,
    );
}
function stopGame(x) {
  const again = x.session.state === "stopping";
  if (!again) clearSession(x);
  x.session.state = "stopping";
  sessionChanged(x, again ? "requesting stop again…" : "requesting stop…");
  sessionTimer(
    x,
    () => sessionChanged(x, "Stop requested via " + x.launcher + "."),
    500,
  );
  sessionTimer(
    x,
    () => {
      clearSession(x);
      x.session = { state: "stopped", note: "", warning: "", pending: false };
      sessionChanged(x, "exited.");
    },
    LIB_TIMING.stop,
  );
}

// ---------- Launcher write action, scanning, quiet refresh ----------

function runWriteAction() {
  const state = writeState(g().launcher);
  if (!state.action || lib.action || !flushCommand()) return;
  const go = () => {
    lib.action = true;
    setLibraryMessage(
      state.action === "Scan my Steam library"
        ? "Connecting the Steam library…"
        : "Restarting Steam…",
    );
    libTimer(() => {
      lib.action = false;
      appState.launcherWriteState = "ready";
      rescan("Steam is ready for live apply.");
    }, LIB_TIMING.action);
  };
  if (!state.confirm) return go();
  showPanel(
    "<h2>" +
      state.action +
      '</h2><p class="sub">' +
      state.confirm +
      '</p><div class="dialogfoot"><button onclick="closePanel()">Cancel</button><button class="primary" id="writeGo">Restart Steam</button></div>',
  );
  const b = document.getElementById("writeGo");
  b.onclick = () => {
    closePanel();
    go();
  };
  b.focus();
}
function rescan(message = "") {
  if (lib.busy || lib.scan !== "idle" || !flushCommand()) return;
  lib.scan = "scanning";
  libRefresh("#rescanBtn");
  libTimer(() => {
    lib.scan = "idle";
    lib.message =
      message ||
      (appState.libraryScan === "heroic-error"
        ? "Heroic: could not read the library (legendary_library.json: Expecting value)"
        : "");
    if (tab === "Game Library" && !report) libRefresh("#rescanBtn");
  }, LIB_TIMING.scan);
}
function rescanDemo() {
  rescan();
}
// The first time the library is shown it reads every launcher.
libTimer(() => {
  if (lib.scan !== "initial") return;
  lib.scan = "idle";
  if (tab === "Game Library" && !report) libRefresh();
}, LIB_TIMING.firstScan);
// A quiet re-read every 30 s; it leaves an unsaved command edit alone.
setInterval(() => {
  if (tab !== "Game Library" || report || lib.scan !== "idle" || lib.menu) return;
  const list = document.querySelector(".list"),
    sig = listSignature();
  if (!list || sig === lib.listSig) return;
  const top = list.scrollTop;
  filterList();
  list.scrollTop = top;
}, LIB_TIMING.quietRefresh);
function listSignature() {
  return (
    idx +
    ":" +
    matches()
      .map(([x, i]) => i + (x.wrapped ? "w" : "") + (isActive(x) ? "a" : ""))
      .join(",")
  );
}

// ---------- Selection and splitter ----------

function selectGame(i) {
  if (lib.scan === "initial") lib.scan = "idle";
  if (i !== idx && !flushCommand()) {
    // The draft belongs to the current game; keep the selection on it.
    document.querySelector("#gameCommand")?.focus({ preventScroll: true });
    return;
  }
  hidePerfPeek();
  idx = i;
  render();
  document.querySelector(".game.active")?.focus({ preventScroll: true });
}
function sideWidth(width) {
  const layout = document.querySelector(".classic");
  const max = Math.max(260, (layout?.clientWidth || 1200) - 440);
  return Math.round(Math.min(max, Math.max(240, width)));
}
function setSide(width) {
  lib.side = sideWidth(width);
  document
    .querySelector(".classic")
    ?.style.setProperty("--libSide", lib.side + "px");
  const handle = document.querySelector(".splitter");
  handle?.setAttribute("aria-valuenow", lib.side);
  try {
    localStorage.setItem("pb-library-side", String(lib.side));
  } catch {
    /* per-viewer convenience only */
  }
}
function splitterDown(e) {
  const handle = e.currentTarget,
    left = document.querySelector(".classic").getBoundingClientRect().left;
  handle.setPointerCapture(e.pointerId);
  handle.onpointermove = (m) => setSide(m.clientX - left - 8);
  handle.onpointerup = () => (handle.onpointermove = null);
}
function splitterKey(e) {
  const width = lib.side || document.querySelector(".side").offsetWidth;
  const step = e.shiftKey ? 64 : 16;
  const next = {
    ArrowLeft: width - step,
    ArrowRight: width + step,
    Home: 240,
    End: 10000,
  }[e.key];
  if (next !== undefined) {
    e.preventDefault();
    setSide(next);
  }
}

// ---------- Perf Profile entry and its peek ----------
// PR #137's entry: same shape as Play in a secondary style. Hover or keyboard
// focus peeks the fingerprint once it has qualified; a click opens the view.

function perfButton(x) {
  const p = perfState(x);
  return (
    '<button id="view" class="perfbtn" type="button" title="' +
    esc(p.tip) +
    '"' +
    (p.peek ? ' aria-describedby="perfPeek"' : "") +
    (p.enabled ? "" : ' aria-disabled="true"') +
    ' onclick="perfClicked()" onmouseenter="showPerfPeek()" onmouseleave="hidePerfPeek()" onfocus="perfFocus(this)" onblur="hidePerfPeek()" onmousedown="hidePerfPeek()" onkeydown="if(event.key===\'Escape\')hidePerfPeek()">' +
    perfIcon() +
    "Perf Profile" +
    (p.live ? '<i class="livedot" title="Recording"></i><span class="vh"> · recording</span>' : "") +
    "</button>"
  );
}
function perfIcon() {
  return '<svg aria-hidden="true" viewBox="0 0 20 20" width="18" height="18"><path d="M10 2.5 17.1 7.7 14.4 16H5.6L2.9 7.7Z" fill="none" stroke="currentColor" stroke-width="1.3" opacity=".55"/><path d="M10 5.6 14.6 8.6 12.4 13.4H7.4L6.1 8.9Z" fill="currentColor" fill-opacity=".28" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
}
function perfClicked() {
  hidePerfPeek();
  if (perfState(g()).enabled) openReport();
}
function perfFocus(button) {
  // Keyboard focus peeks; focus restored after a re-render does not.
  if (button.matches(":focus-visible")) showPerfPeek();
}
function showPerfPeek() {
  const peek = document.getElementById("perfPeek"),
    x = g();
  if (!peek || !perfState(x).peek || typeof perfPeekRows !== "function") return;
  peek.innerHTML =
    '<div class="perfpeekbody"><canvas class="perfpeekfinger" aria-hidden="true"></canvas><dl>' +
    perfPeekRows(x)
      .map(([k, v]) => "<div><dt>" + k + "</dt><dd>" + v + "</dd></div>")
      .join("") +
    "</dl></div><p>" +
    PERF_HINT +
    "</p>";
  peek.hidden = false;
  drawPerfPeek(peek.querySelector("canvas"), x);
}
function hidePerfPeek() {
  const peek = document.getElementById("perfPeek");
  if (peek) peek.hidden = true;
}
// history.js announces each recorded poll; refresh only the button so the
// page, focus and an open peek stay put.
function refreshPerfButton() {
  const old = document.getElementById("view");
  if (!old || tab !== "Game Library" || report) return;
  const focused = document.activeElement === old,
    peekOpen = !document.getElementById("perfPeek")?.hidden;
  old.outerHTML = perfButton(g());
  const button = document.getElementById("view");
  if (focused) button.focus({ preventScroll: true });
  if (peekOpen) showPerfPeek();
}
document.addEventListener("perfrecording", refreshPerfButton);

// ---------- Views ----------

function group(name, body, opts = {}) {
  const tag = opts.gated ? "fieldset" : "section";
  return (
    "<" +
    tag +
    ' class="prefgroup"' +
    (opts.disabled ? " disabled" : "") +
    (opts.tip ? ' title="' + esc(opts.tip) + '"' : "") +
    "><h2>" +
    name +
    '</h2><div class="prefrows">' +
    body +
    "</div></" +
    tag +
    ">"
  );
}
function gameToggle(k, label, disabled = false, tip = "") {
  return (
    '<button class="track" data-switch="' +
    k +
    '" role="switch" aria-label="' +
    label +
    '" aria-checked="' +
    !!g()[k] +
    '"' +
    (disabled ? " disabled" : "") +
    (tip ? ' title="' + esc(tip) + '"' : "") +
    " onclick=\"selectedSetting('" +
    k +
    "',!g()['" +
    k +
    "'])\"><i></i></button>"
  );
}
function readiness(x) {
  return !x.wrapped
    ? [
        "Wrap this game to record",
        "Enable the wrapper in the game settings below.",
      ]
    : !x.cap
      ? [
          "Capture telemetry is off",
          "Enable capture to record your next play session.",
        ]
      : [
          "Ready for the next launch",
          "Play with PenguinBurner enabled to record performance.",
        ];
}
function playButton(x) {
  if (!canLaunch(x))
    return '<small class="nolaunch">Faugus Launcher is not installed here</small>';
  const v = playView(x, idx);
  return (
    '<button id="playBtn" data-state="' +
    v.state +
    '" class="' +
    (v.state === "idle" ? "primary" : "playstate") +
    '"' +
    (v.enabled ? "" : ' aria-disabled="true"') +
    (v.tip ? ' title="' + esc(v.tip) + '"' : "") +
    ' onclick="playClicked()">' +
    (v.state === "starting" || v.state === "stopping"
      ? '<span class="spinner" aria-hidden="true"></span>'
      : "") +
    v.text +
    "</button>"
  );
}
function settingsGroups(x) {
  const ws = writeState(x.launcher),
    writable = ws.ready,
    gated = !(x.wrapped && writable),
    canAdaptive = adaptiveTiers().length > 0 && x.overlayOk,
    adaptive = x.mode === "Adaptive" && x.overlayOk,
    compat = compatField(x),
    multiGpu = appState.gpus.length > 1,
    draft = lib.draft && lib.draft.index === idx ? lib.draft.value : null,
    cmd = draft ?? commandText(x),
    steam = x.launcher === "Steam";
  const modeSelect =
    '<select id="gameMode" aria-label="Saved Auto-UV mode" onchange="setMode(this.value)"' +
    (canAdaptive ? "" : ' title="' + esc(x.overlayOk ? "Adaptive needs at least one verified Auto-UV profile; run an Auto-UV scan first." : OPENGL_REASON) + '"') +
    ">" +
    ["Adaptive", ...tiers, "Stock"]
      .map(
        (t) =>
          '<option value="' +
          t +
          '"' +
          (x.mode === t ? " selected" : "") +
          (t === "Adaptive" && !canAdaptive ? " disabled" : "") +
          ">" +
          modeLabel(t) +
          "</option>",
      )
      .join("") +
    "</select>";
  const target =
    '<div class="targetctl">' +
    (x.override
      ? '<label class="fps"><input id="gameTarget" aria-label="Per-game target FPS" type="number" min="' +
        MIN_TARGET_FPS +
        '" max="' +
        MAX_TARGET_FPS +
        '" step="5" value="' +
        x.target +
        '"' +
        (adaptive ? "" : " disabled") +
        ' onchange="setTarget(this)"> FPS</label>'
      : '<span class="follow">follows system-wide (' + SYSTEM_TARGET_FPS + ")</span>") +
    gameToggle("override", "Per-game target", !adaptive) +
    "</div>";
  const gpuSelect =
    '<select id="gameGpu" aria-label="Graphics card" onchange="setGpu(this.value)"' +
    (writable ? "" : " disabled") +
    '><option value="">Auto (single GPU)</option>' +
    appState.gpus
      .map(
        (gpu, k) =>
          '<option value="' +
          k +
          '"' +
          (String(x.gpu) === String(k) ? " selected" : "") +
          ">" +
          gpu.name +
          "</option>",
      )
      .join("") +
    "</select>";
  const field = steam
    ? '<textarea id="gameCommand" rows="2" spellcheck="false" aria-label="Command"'
    : '<input id="gameCommand" type="text" spellcheck="false" aria-label="Command"';
  const commandControl =
    field +
    (writable ? "" : " disabled") +
    ' oninput="commandInput(this)" onchange="flushCommand(false,true)" onkeydown="commandKey(event,this)"' +
    (steam ? ">" + esc(cmd) + "</textarea>" : ' value="' + esc(cmd) + '">');
  return (
    group(
      "PenguinBurner",
      row(
        "Wrap this game",
        "Adds the PenguinBurner wrapper to the launch command. Takes effect on the next launch.",
        gameToggle("wrapped", "Wrap this game", !writable),
      ) +
        (multiGpu
          ? row("Graphics card", "Which GPU this game's profile applies to.", gpuSelect)
          : ""),
      { tip: writable ? "" : ws.detail },
    ) +
    group(
      "Tuning",
      row(
        "Saved Auto-UV mode",
        canAdaptive
          ? "Adaptive switches tiers from live frame pacing. Changes apply live to a wrapped game."
          : x.overlayOk
            ? "Adaptive needs at least one verified tier."
            : "Adaptive needs Vulkan frame data.",
        modeSelect,
      ) +
        row("Per-game target", "Off, this game follows the system-wide Adaptive target.", target),
      { gated: true, disabled: gated },
    ) +
    group(
      "In game",
      row(
        "Overlay",
        x.overlayOk
          ? "Draw the PenguinBurner overlay in this game."
          : "Needs Vulkan frame data.",
        gameToggle("overlay", "Overlay", !x.overlayOk, x.overlayOk ? "" : OPENGL_REASON),
      ) +
        row(
          "Capture telemetry",
          "Record frame timing and GPU measurements for the Perf Profile.",
          gameToggle("cap", "Capture telemetry"),
        ),
      { gated: true, disabled: gated },
    ) +
    group(
      "Launch command",
      row(
        "Compatibility tool",
        compat.subtitle,
        '<select id="gameCompat" aria-label="Compatibility tool" onchange="setCompat(this.value)"' +
          (compat.enabled && writable ? "" : " disabled") +
          ">" +
          compat.choices
            .map(
              ([v, l]) =>
                '<option value="' +
                esc(v) +
                '"' +
                (v === compat.value ? " selected" : "") +
                ">" +
                esc(l) +
                "</option>",
            )
            .join("") +
          "</select>",
      ) +
        '<div class="row stack' +
        (draft !== null ? " drafting" : "") +
        '"><div><h3><label for="gameCommand">Command</label></h3><small>' +
        esc(commandSubtitle(x)) +
        "</small></div>" +
        commandControl +
        '<small class="cmdhint">' +
        (draft !== null
          ? "Unsaved · " + (steam ? "Ctrl+Enter" : "Enter") + " or leave the field to save, Esc to discard"
          : steam
            ? "Saved when you leave the field or press Ctrl+Enter."
            : "Saved when you leave the field or press Enter.") +
        "</small></div>",
    )
  );
}
function gamePane(x) {
  const ws = writeState(x.launcher);
  return (
    '<div class="gamepane"><div class="gamehero"><div class="herotext"><h2>' +
    esc(x.name) +
    '</h2><small class="launcherLine"><img alt="" src="' +
    launcherIcon(x) +
    '">' +
    esc(gameMetadata(x)) +
    (x.ready ? "" : ' · <span class="notready">Not installed</span>') +
    '</small></div><div class="heroactions">' +
    perfButton(x) +
    playButton(x) +
    '<div class="perfpeek" id="perfPeek" role="tooltip" hidden></div></div></div>' +
    (x.session.warning
      ? '<p class="launchwarn" role="alert">' + esc(x.session.warning) + "</p>"
      : "") +
    (x.overlayOk ? "" : '<p class="compatnote">' + OPENGL_REASON + "</p>") +
    (!ws.ready
      ? '<p class="writenote"><b>Steam is read-only for now.</b> ' + esc(ws.detail) + "</p>"
      : "") +
    settingsGroups(x) +
    "</div>"
  );
}
function libraryHeading() {
  const names = availableLaunchers(),
    ws = writeState(g()?.launcher),
    scanning = lib.scan === "scanning";
  return (
    '<div class="libheading"><div><h1>Game Library</h1><p>' +
    (names.length ? joinNames(names) : "No launcher found") +
    '</p></div><div class="tools">' +
    (ws.action && libraryGames().includes(g())
      ? '<button class="attention" id="writeAction" title="' +
        esc(ws.detail) +
        '"' +
        (lib.action ? ' aria-disabled="true"' : "") +
        ' onclick="runWriteAction()">' +
        (lib.action ? '<span class="spinner" aria-hidden="true"></span>' : "") +
        ws.action +
        "</button>"
      : "") +
    '<button id="rescanBtn" title="Re-read every launcher\'s library and each game\'s current launch command. Press this after changing a game in ' +
    (names.join(", ") || "your launcher") +
    '."' +
    (scanning || lib.busy || lib.scan === "initial" ? ' aria-disabled="true"' : "") +
    ' onclick="rescan()">' +
    (scanning ? '<span class="spinner" aria-hidden="true"></span>Scanning…' : "Rescan") +
    "</button></div></div>"
  );
}
function library() {
  syncSessions();
  const shown = libraryGames(),
    list = matches(),
    placeholder = libraryPlaceholder();
  if (shown.length && !shown.includes(g()))
    idx = list.length ? list[0][1] : games.indexOf(shown[0]);
  const x = g(),
    loading = lib.scan === "initial",
    ws = writeState(shown.includes(x) ? x.launcher : "");
  const sideStyle = lib.side ? ' style="--libSide:' + lib.side + 'px"' : "";
  const pane = !shown.length
    ? '<div class="gamepane emptystate"><div><img alt="" src="' +
      assets["tab-game-library"] +
      '"><h2>' +
      (availableLaunchers().length ? "No games yet" : "Install a game launcher") +
      "</h2><p>" +
      placeholder +
      "</p></div></div>"
    : list.length
      ? gamePane(x)
      : '<div class="gamepane emptymsg">No games match these filters. <button id="clearFiltersPane" onclick="resetFilters()">Clear filters</button></div>';
  return (
    libraryHeading() +
    (loading
      ? '<div class="loadingpane" role="status"><span class="spinner big" aria-hidden="true"></span><h2>Loading installed games…</h2><p>' +
        joinNames(availableLaunchers()) +
        "</p></div>"
      : "") +
    '<div class="layout classic"' +
    sideStyle +
    (loading ? " hidden" : "") +
    '><aside class="side"><div class="sidehead"><h2>Games</h2><small>' +
    list.length +
    " of " +
    shown.length +
    '</small></div><input id="search" aria-label="Search games" placeholder="Search games…" oninput="q=this.value;updateFilters(true)"' +
    (shown.length ? "" : " disabled") +
    ">" +
    listTools() +
    '<div class="list"></div></aside><div class="splitter" id="libSplitter" role="separator" tabindex="0" aria-orientation="vertical" aria-label="Resize the game list" aria-valuemin="240" aria-valuenow="' +
    (lib.side || 340) +
    '" onpointerdown="splitterDown(event)" onkeydown="splitterKey(event)"></div>' +
    pane +
    '</div><p class="libstatus" role="status">' +
    esc(libraryStatusText(shown, lib.message, ws)) +
    "</p>"
  );
}
