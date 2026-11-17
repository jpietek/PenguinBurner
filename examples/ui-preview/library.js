let sortLib = "Recently played",
  silentFan = false;
const launchers = ["Steam", "Heroic", "Lutris", "Faugus"];
const launcherDetails = [
  ["Control", "Heroic", "Epic"],
  ["Dishonored", "Lutris", "Wine"],
  ["Hollow Knight", "Faugus", "Proton"],
];
launcherDetails.forEach(([name, launcher, runner], i) =>
  games.push({
    name,
    id: "demo-" + launcher.toLowerCase(),
    launcher,
    runner,
    mins: 0,
    cap: true,
  }),
);
games.forEach((game, i) =>
  Object.assign(game, {
    launcher: game.launcher || "Steam",
    runner: game.runner || "Proton",
    wrapped: i !== 4 && i !== 6,
    mode: "Stock",
    overlay: true,
    target: 60,
    override: false,
    compat: "Launcher default",
    running: false,
    latency: false,
    played: [40, 23, 61, 15, 3, 14, 5][i],
    command: null,
  }),
);
let filtersOpen = false,
  launcherFilter = "All launchers",
  wrappedFilter = "Any setup",
  recordFilter = "Any history",
  panelScroll = 0,
  rescanNote = "";
function selectOptions(label, items, value, handler) {
  return (
    '<select aria-label="' +
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
  const list = games
    .map((x, i) => [x, i])
    .filter(
      ([x]) =>
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
    ${art ? `<img class="launcherMark" src="${launcherIcon(game)}" title="${game.launcher}" alt="">` : ""}</span>`;
}
function gameList() {
  return (
    matches()
      .map(
        ([game, i]) => `<button class="game ${idx === i ? "active" : ""}"
    aria-pressed="${idx === i}" onclick="selectGame(${i})">${gameArtwork(game)}<span><strong>${game.name}</strong>
    <small>${game.launcher} · ${game.played} h</small></span></button>`,
      )
      .join("") || '<div class="emptymsg">No matching games</div>'
  );
}
function filterCount() {
  return (
    Number(launcherFilter !== "All launchers") +
    Number(wrappedFilter !== "Any setup") +
    Number(recordFilter !== "Any history")
  );
}
function listTools() {
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
    ) +
    '</label><button aria-expanded="' +
    filtersOpen +
    '" onclick="filtersOpen=!filtersOpen;render()">Filters' +
    (filterCount() ? " (" + filterCount() + ")" : "") +
    '</button><button onclick="bulkDialog()">All games</button></div>' +
    (filtersOpen
      ? '<div class="filterbody">' +
        [
          [
            "Launcher",
            ["All launchers", ...launchers],
            launcherFilter,
            "launcherFilter",
          ],
          [
            "Wrapper",
            ["Any setup", "Wrapped", "Unwrapped"],
            wrappedFilter,
            "wrappedFilter",
          ],
          [
            "Recording",
            ["Any history", "Recorded", "No recording"],
            recordFilter,
            "recordFilter",
          ],
        ]
          .map(
            ([l, vs, v, k]) =>
              "<label>" +
              l +
              selectOptions(l, vs, v, k + "=this.value;updateFilters()") +
              "</label>",
          )
          .join("") +
        '<button onclick="resetFilters()">Clear filters</button></div>'
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
  if (match.length && !match.some(([, i]) => i === idx)) idx = match[0][1];
  render();
  document.querySelector(".list").scrollTop = scroll;
  if (searching) {
    let ip = document.querySelector("#search");
    ip.focus({ preventScroll: true });
    ip.setSelectionRange(caret, caret);
  }
}
function bulkDialog() {
  showPanel(
    '<h2>All games</h2><div class="shortlist gap">' +
      [
        "Enable PenguinBurner for all games",
        "Disable PenguinBurner for all games",
        "Show overlay for wrapped games",
        "Hide overlay for wrapped games",
      ]
        .map(
          (s, i) =>
            '<button onclick="confirmBulk(' +
            i +
            ',this.innerText)">' +
            s +
            "</button>",
        )
        .join("") +
      "</div>" +
      dismiss,
  );
}
function confirmBulk(i, l) {
  showPanel(
    "<h2>" +
      l +
      '?</h2><p class="sub">Applies to this demo library.</p><div class="dialogfoot"><button onclick="closePanel()">Cancel</button><button onclick="applyBulk(' +
      i +
      ')">Confirm</button></div>',
  );
}
function applyBulk(i) {
  games.forEach((x) => {
    if (i < 2) x.wrapped = !i;
    else if (x.wrapped) x.overlay = i === 2;
  });
  closePanel();
  updateFilters();
}
function selectedSetting(k, v) {
  g()[k] = v;
  updateFilters();
  document
    .querySelector('[data-switch="' + k + '"]')
    ?.focus({ preventScroll: true });
}
function gameToggle(k, label) {
  return (
    '<button class="track" data-switch="' +
    k +
    '" role="switch" aria-label="' +
    label +
    '" aria-checked="' +
    g()[k] +
    '" onclick="selectedSetting(\'' +
    k +
    "',!g()['" +
    k +
    "'])\"><i></i></button>"
  );
}
function group(name, body) {
  return (
    '<section class="prefgroup"><h2>' +
    name +
    '</h2><div class="prefrows">' +
    body +
    "</div></section>"
  );
}
function defaultCmd(x) {
  return x.wrapped
    ? "PENGUIN_BURNER " +
        (x.overlay ? "--pb-overlay=1" : "--pb-overlay=0") +
        (!x.overlay && x.latency ? " --pb-ingame-latency=1" : "") +
        (x.launcher === "Steam"
          ? " %command%"
          : " --pb-game-id=" + x.launcher.toLowerCase() + ":" + x.id)
    : x.launcher === "Steam"
      ? "%command%"
      : "";
}
function commandDialog() {
  const x = g();
  showPanel(
    '<h2>Edit launch command</h2><p class="sub">' +
      x.launcher +
      " · " +
      esc(x.name) +
      '</p><textarea id="rawCmd" rows="4" aria-label="Launch command"></textarea><div class="dialogfoot"><button onclick="closePanel()">Cancel</button><button class="primary" onclick="g().command=document.getElementById(\'rawCmd\').value;closePanel();render()">Save command</button></div>',
  );
  document.querySelector("#rawCmd").value = x.command ?? defaultCmd(x);
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
function settingsGroup() {
  const x = g();
  let canUse = adaptiveTiers().length > 0,
    modeList = ["Adaptive", ...tiers, "Stock"];
  const modeSelect =
    '<select aria-label="Saved Auto-UV mode" onchange="g().mode=this.value;render()">' +
    modeList
      .map(
        (t) =>
          "<option " +
          (x.mode === t ? "selected" : "") +
          " " +
          (t === "Adaptive" && !canUse ? "disabled" : "") +
          ">" +
          t +
          "</option>",
      )
      .join("") +
    "</select>";
  return (
    group(
      "PenguinBurner",
      row(
        "Wrap this game",
        "Configure the PenguinBurner wrapper for the next launch.",
        gameToggle("wrapped", "Wrap this game"),
      ),
    ) +
    (x.wrapped
      ? group(
          "Tuning",
          row(
            "Saved Auto-UV mode",
            canUse
              ? "Adaptive switches tiers from live frame pacing."
              : "Adaptive needs at least one verified tier.",
            modeSelect,
          ) +
            (x.mode === "Adaptive"
              ? row(
                  "Per-game target",
                  x.override
                    ? "Target frame rate before frame generation."
                    : "Follows the system-wide target.",
                  '<div class="tools">' +
                    (x.override
                      ? '<label><input aria-label="Target FPS" type="number" min="1" max="500" value="' +
                        x.target +
                        '" onchange="g().target=Math.min(500,Math.max(1,Number(this.value)));render()"> FPS</label>'
                      : "") +
                    gameToggle("override", "Override system target") +
                    "</div>",
                )
              : "") +
            row(
              "Graphics card",
              "Target GPU for this game.",
              "<span>GPU 0 · RTX 5080</span>",
            ),
        ) +
        group(
          "In game",
          row(
            "Overlay",
            "Draw the performance overlay during play.",
            gameToggle("overlay", "Show overlay"),
          ) +
            row(
              "Capture telemetry",
              "Record frame timing and GPU measurements.",
              gameToggle("cap", "Capture telemetry"),
            ) +
            row(
              "In-game latency",
              "Use supported latency markers during play.",
              gameToggle("latency", "In-game latency"),
            ),
        )
      : "") +
    group(
      "Launch command",
      row(
        "Compatibility tool",
        "Applies on the next launch.",
        selectOptions(
          "Compatibility tool",
          [
            "Launcher default",
            ...(x.launcher === "Lutris"
              ? ["Wine GE", "System Wine"]
              : ["Proton Experimental", "GE-Proton"]),
          ],
          x.compat,
          "g().compat=this.value;render()",
        ),
      ) +
        row(
          "Command",
          x.launcher === "Steam"
            ? "Steam launch options"
            : x.launcher + " game configuration",
          '<div class="command"><code>' +
            esc(x.command ?? defaultCmd(x)) +
            '</code><button onclick="commandDialog()">Edit</button></div>',
        ),
    )
  );
}
function historyEntry(x) {
  return (
    '<section class="prefgroup"><h2>Performance history</h2><div class="prefrows historyentry"><div><strong>' +
    (x.mins ? "Latest recording" : "No recording yet") +
    "</strong><small>" +
    (x.mins ? x.mins + " min of recorded play" : readiness(x)[0]) +
    "</small></div>" +
    (x.mins
      ? '<div class="histnums"><span><b>' +
        x.fps +
        "</b> FPS <small>Median</small></span><span><b>" +
        x.low +
        '</b> FPS <small>1% low</small></span></div><button id="view" class="primary" onclick="openReport()">View history →</button>'
      : !x.cap && x.wrapped
        ? '<button onclick="capture()">Enable capture</button>'
        : "") +
    "</div></section>"
  );
}
function filterList() {
  document.querySelector(".list").innerHTML = gameList();
}
function selectGame(i) {
  idx = i;
  render();
  document.querySelector(".game.active")?.focus({ preventScroll: true });
}
function library() {
  let x = g(),
    list = matches();
  return (
    '<div class="libheading"><div><h1>Game Library</h1><p>Steam, Heroic, Lutris and Faugus</p></div><div class="tools"><small role="status">' +
    rescanNote +
    '</small><button onclick="rescanDemo()">Rescan</button></div></div><div class="layout classic"><aside class="side"><div class="sidehead"><h2>Games</h2><small>' +
    list.length +
    " of " +
    games.length +
    '</small></div><input id="search" aria-label="Search games" placeholder="Search games…" oninput="q=this.value;updateFilters(true)">' +
    listTools() +
    '<div class="list"></div></aside>' +
    (list.length
      ? '<div class="gamepane"><div class="gamehero"><div><h2>' +
        x.name +
        '</h2><small class="launcherLine"><img alt="" src="' +
        launcherIcon(x) +
        '">' +
        x.launcher +
        " · " +
        x.runner +
        " · " +
        x.played +
        ' h played</small></div><button class="primary" title="Simulate launch or stop" onclick="g().running=!g().running;render()">' +
        (x.running ? "Stop" : "Play") +
        "</button></div>" +
        historyEntry(x) +
        settingsGroup() +
        "</div>"
      : '<div class="gamepane emptymsg">No games match these filters. <button onclick="resetFilters()">Clear filters</button></div>') +
    "</div>"
  );
}
function rescanDemo() {
  rescanNote = "Sample library refreshed · " + games.length + " games";
  render();
}
function capture() {
  g().cap = !g().cap;
  render();
  document.querySelector("[data-switch=cap]")?.focus({ preventScroll: true });
}
function openReport() {
  libScroll = scrollY;
  listScroll = document.querySelector(".list").scrollTop;
  report = true;
  render();
  scrollTo(0, 0);
  document.querySelector("#back").focus();
}
function back() {
  report = false;
  render();
  document.querySelector(".list").scrollTop = listScroll;
  scrollTo(0, libScroll);
  document.querySelector("#view").focus({ preventScroll: true });
}
function historyView() {
  const x = g(),
    full = x.mins >= 5;
  const fingerprint =
    '<section class="card fingerprint">' +
    head(
      "Performance fingerprint",
      full
        ? "Five measures across this recording."
        : "Gathering the first five minutes.",
    ) +
    (full
      ? '<canvas class="finger" data-chart="fingerprint" role="img" aria-label="Balanced fingerprint, 224 W, 96% GPU, 87 FPS median, 61 FPS 1% low and 72% busiest CPU thread."></canvas>'
      : '<div class="warming"><progress max="5" value="' +
        x.mins +
        '"></progress><small>' +
        x.mins +
        " of 5 minutes recorded</small></div>") +
    "</section>";
  const summary =
    '<section class="card">' +
    head("Recording summary", x.mins + " minutes of play · Balanced tier") +
    '<div class="metrics">' +
    metric("Median", x.fps, "FPS") +
    metric("1% low", x.low, "FPS") +
    metric("Target", 60, "FPS") +
    '</div><div class="stats">' +
    [
      ["GPU power", full ? "224 W" : "208 W"],
      ["GPU clock", full ? "2640 MHz" : "2580 MHz"],
      ["GPU temperature", full ? "64 °C" : "61 °C"],
      ["Fan", full ? "42%" : "38%"],
      ["GPU / busiest CPU thread", full ? "96% / 72%" : "93% / 68%"],
    ]
      .map(([k, v]) => "<div><small>" + k + "</small><b>" + v + "</b></div>")
      .join("") +
    "</div></section>";
  return (
    '<div class="backline"><button id="back" onclick="back()">← Back to game</button><small>Game Library / Performance history</small></div>' +
    title(
      x.name,
      "Latest recording · " + x.mins + " minutes of recorded play",
    ) +
    '<div class="reporthead">' +
    fingerprint +
    summary +
    '</div><section class="card gap">' +
    head("Frametime", "Median frame duration by interval · milliseconds") +
    '<canvas class="graph" data-chart="time" aria-label="Illustrative recorded frametime trend"></canvas><div class="axes"><span>−' +
    x.mins +
    " min</span><span>End of recording</span></div></section>"
  );
}
