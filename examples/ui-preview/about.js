// About. The daemon keeps a persistent energy-saved counter. Once it has
// counted any active time, the totals replace the purpose sentence. The dialog
// polls it once a second, and it climbs while a tuned profile or game runs.
const aboutCounter = { seconds: 7 * 3600 + 24 * 60 + 12, wattSeconds: 192.7 * 3600 };
const ABOUT_SPONSOR_URL = "https://github.com/sponsors/jpietek",
  ABOUT_ISSUES_URL = "https://github.com/jpietek/PenguinBurner/issues",
  ABOUT_AUTHORS = [
    ["Jan Pietek", "https://github.com/jpietek"],
    ["Michał Ernst (Ernold)", "https://github.com/Ernold11"],
  ];
let aboutTimer = null;
function aboutSavings() {
  // daemon_status() fails without the service, so About keeps the purpose text.
  if (appState.daemon === "missing" || aboutCounter.seconds <= 0) return null;
  return aboutCounter;
}
function aboutTick() {
  if (appState.runningProfile != null || appState.runningGame != null) {
    aboutCounter.seconds += 1;
    aboutCounter.wattSeconds += 47;
  }
}
function runtimeText(seconds) {
  const days = Math.floor(seconds / 86400),
    hours = Math.floor(seconds / 3600) % 24,
    minutes = Math.floor(seconds / 60) % 60;
  return [
    days ? days + "d" : "",
    days || hours ? hours + "h" : "",
    days || hours || minutes ? minutes + "m" : "",
    (seconds % 60) + "s",
  ]
    .filter(Boolean)
    .join(" ");
}
function energyText(wattSeconds) {
  const wh = wattSeconds / 3600;
  if (wh >= 1000000) return (wh / 1000000).toFixed(2) + " MWh";
  if (wh >= 1000) return (wh / 1000).toFixed(2) + " kWh";
  return wh.toFixed(wh >= 100 ? 1 : 2) + " Wh";
}
function aboutView() {
  const data = aboutSavings(),
    link = (url) =>
      '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + "</a>";
  showPanel(`<section class="aboutView"><img src="${assets["penguin-burner"]}" alt="">
    <h2 id="aboutTitle">PenguinBurner</h2><p class="aboutVersion">Version ${esc(appVersion)}</p>
    ${
      data
        ? '<div class="aboutReadings"><div>Total runtime<strong id="aboutRuntime"></strong></div><div>Energy saved<strong id="aboutEnergy"></strong></div></div>'
        : '<p class="aboutPurpose">' + esc(design.copy.aboutPurpose) + "</p>"
    }
    <div class="aboutLinks"><p>Like PenguinBurner? You can support it on GitHub.<br>${link(ABOUT_SPONSOR_URL)}</p>
    <p>Found a problem? Report it here.<br>${link(ABOUT_ISSUES_URL)}</p>
    <p class="aboutAuthors">Authors<br>${ABOUT_AUTHORS.map(
      ([name, url]) =>
        '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + esc(name) + "</a>",
    ).join(" · ")}</p></div>
    <div class="dialogfoot"><button autofocus onclick="closePanel()">OK</button></div></section>`);
  const panel = dlg();
  panel.setAttribute("aria-labelledby", "aboutTitle");
  function update() {
    const runtime = document.getElementById("aboutRuntime");
    if (!runtime) return;
    runtime.textContent = runtimeText(aboutCounter.seconds);
    document.getElementById("aboutEnergy").textContent = energyText(aboutCounter.wattSeconds);
  }
  update();
  clearInterval(aboutTimer);
  aboutTimer = data && setInterval(() => (aboutTick(), update()), 1000);
  panel.addEventListener(
    "close",
    () => {
      clearInterval(aboutTimer);
      panel.removeAttribute("aria-labelledby");
    },
    { once: true },
  );
}
