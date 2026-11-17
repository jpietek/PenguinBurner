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
function navigate(t) {
  tab = t;
  report = false;
  render();
  window.scrollTo(0, 0);
  M.focus({ preventScroll: true });
}
auditBtn.onclick = () => navigate("UX audit");
function render() {
  shellTools();
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
