// The graphics preview supplies a session counter to the same two states as the desktop About dialog.
let aboutClockStart;
function previewSavings() {
  if (!logLines.length) return null;
  aboutClockStart ??= performance.now();
  const elapsed = Math.floor((performance.now() - aboutClockStart) / 1000);
  return {
    seconds: 7 * 3600 + 24 * 60 + elapsed,
    wattSeconds: 192.7 * 3600 + elapsed * 47,
  };
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
  const data = previewSavings();
  showPanel(`<section class="aboutView"><img src="${assets["penguin-burner"]}" alt="">
    <h2 id="aboutTitle">PenguinBurner</h2><p class="aboutVersion">Version ${esc(appVersion)}</p>
    ${
      data
        ? '<div class="aboutReadings"><div>Total runtime<strong id="aboutRuntime"></strong></div><div>Energy saved<strong id="aboutEnergy"></strong></div></div><small>Sample lifetime totals</small>'
        : '<p class="aboutPurpose">' + esc(design.copy.aboutPurpose) + '</p>'
    }
    <div class="aboutLinks"><a href="https://github.com/sponsors/jpietek" target="_blank" rel="noopener noreferrer">Support PenguinBurner on GitHub ↗</a>
    <a href="https://github.com/jpietek/PenguinBurner/issues" target="_blank" rel="noopener noreferrer">Report an issue ↗</a></div>
    <div class="dialogfoot"><button onclick="closePanel()">OK</button></div></section>`);
  const panel = dlg();
  panel.setAttribute("aria-labelledby", "aboutTitle");
  function update() {
    const current = previewSavings();
    if (current && document.getElementById("aboutRuntime")) {
      document.getElementById("aboutRuntime").textContent = runtimeText(
        current.seconds,
      );
      document.getElementById("aboutEnergy").textContent = energyText(
        current.wattSeconds,
      );
    }
  }
  update();
  const interval = data && setInterval(update, 1000);
  panel.addEventListener(
    "close",
    () => {
      clearInterval(interval);
      panel.removeAttribute("aria-labelledby");
    },
    { once: true },
  );
}
