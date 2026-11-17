// File input stays local. The same synthetic export exercises the desktop chooser and this browser view.
let afterburnerRows = [],
  importSelection = -1,
  importDirectory = "Example Afterburner",
  importMessage = "",
  importRequest = 0,
  importedPoint = -1;

function blobReader(hex, stride, counted) {
  const clean = hex.trim().replace(/\s+/g, "");
  if (!/^(?:[0-9a-f]{2})+$/i.test(clean) || clean.length < 24)
    throw Error("Invalid curve data");
  const view = new DataView(
    Uint8Array.from(clean.match(/../g), (token) => parseInt(token, 16)).buffer,
  );
  const count = counted
    ? view.getUint32(4, true)
    : Math.floor((view.byteLength - 12) / (stride * 4));
  if (count > (view.byteLength - 12) / (stride * 4))
    throw Error("Truncated curve data");
  const points = [];
  for (let i = 0; i < count; i++) {
    const row = Array.from({ length: stride }, (_, j) =>
      view.getFloat32(12 + 4 * (i * stride + j), true),
    );
    if (!counted && row.every((n) => n === 0)) break;
    if (!row.every(Number.isFinite)) throw Error("Invalid curve point");
    points.push(row);
  }
  return points;
}
function vfPoints(hex) {
  const rows = blobReader(hex, 3, false);
  const counts = new Map();
  for (const [, , offset] of rows)
    counts.set(offset, (counts.get(offset) || 0) + 1);
  const dominant = [...counts].sort((a, b) => b[1] - a[1])[0];
  let ceiling = Infinity;
  if (dominant?.[1] === rows.length - 1) {
    const i = rows.findIndex((r) => Math.abs(r[2] - dominant[0]) > 1e-6);
    if (
      i > 0 &&
      i < rows.length - 1 &&
      rows[i][1] <= Math.min(rows[i - 1][1], rows[i + 1][1]) - 100
    )
      ceiling = rows[i][1];
  }
  return rows.map(([v, m]) => [v, Math.min(m, ceiling)]);
}
function cfgSections(text) {
  const sections = new Map();
  let values = new Map();
  sections.set("", values);
  for (const original of text.split(/\r?\n/)) {
    const line = original.trim(),
      section = line.match(/^\[(.+)\]$/);
    if (section) {
      values = new Map();
      sections.set(section[1], values);
      continue;
    }
    const eq = line.indexOf("=");
    if (eq > 0 && !line.startsWith(";") && !line.startsWith("#"))
      values.set(
        line.slice(0, eq).trim().toLowerCase(),
        line.slice(eq + 1).trim(),
      );
  }
  return sections;
}
function targetOfCurve(points) {
  if (points.length < 4) return null;
  const clock = points.at(-1)[1];
  let start = points.length - 1;
  while (start > 0 && Math.abs(points[start - 1][1] - clock) <= 1) start--;
  return points.length - start >= 4
    ? { clock: Math.round(clock), mv: Math.round(points[start][0]) }
    : null;
}
function afterburnerEntries(files) {
  if (!Object.hasOwn(files, "MSIAfterburner.cfg"))
    throw Error(
      "Choose the Afterburner directory containing MSIAfterburner.cfg and Profiles.",
    );
  if (!Object.keys(files).some((path) => path.startsWith("Profiles/")))
    throw Error("The selected directory has no Profiles folder.");
  let fan;
  for (const name of ["Profiles/MSIAfterburner.cfg", "MSIAfterburner.cfg"]) {
    if (!Object.hasOwn(files, name)) continue;
    const settings = new Map(
      [...cfgSections(files[name]).values()].flatMap((values) => [...values]),
    );
    if (!settings.has("swautofancontrol")) continue;
    try {
      fan = blobReader(settings.get("swautofancontrolcurve") || "", 2, true);
    } catch {
      fan = undefined;
    }
    break;
  }
  const result = [];
  for (const [path, text] of Object.entries(files)) {
    if (!/^Profiles\/VEN_[^/]+\.cfg$/i.test(path)) continue;
    const device = path.split("/").at(-1);
    const entries = [];
    for (const [section, values] of cfgSections(text)) {
      const raw = values.get("vfcurve");
      if (!raw) continue;
      const builtin = ["defaults", "startup"].indexOf(section.toLowerCase());
      let points = [],
        error = "";
      try {
        points = vfPoints(raw);
      } catch (e) {
        error = e.message;
      }
      entries.push({ device, section, points, raw, builtin, error, fan });
    }
    const bases = entries
      .filter((e) => e.builtin >= 0 && !e.error)
      .sort((a, b) => a.builtin - b.builtin);
    for (const e of entries.filter((e) => e.builtin < 0)) {
      const target = targetOfCurve(e.points);
      Object.assign(e, target);
      if (e.error) e.status = e.error;
      else if (bases.some((b) => b.raw === e.raw))
        e.status = "Same as Defaults or Startup";
      else if (!target) e.status = "No flattened V/F tail";
      else {
        let voltage;
        for (const b of bases) {
          const point = [...b.points]
            .sort((a, c) => a[1] - c[1] || a[0] - c[0])
            .find((p) => p[1] + 1e-6 >= e.clock);
          if (point) {
            voltage = point[0];
            break;
          }
        }
        e.status =
          voltage === undefined
            ? "Missing reference curve"
            : voltage - e.mv >= 5
              ? "Ready"
              : "Target is not below the reference voltage";
      }
      result.push(e);
    }
  }
  return result.sort(
    (a, b) =>
      (b.status === "Ready") - (a.status === "Ready") ||
      a.device.localeCompare(b.device) ||
      a.section.localeCompare(b.section),
  );
}
function setImportSource(files, name) {
  importDirectory = name;
  importMessage = "";
  importedPoint = -1;
  afterburnerRows = [];
  try {
    afterburnerRows = afterburnerEntries(files);
    if (!afterburnerRows.length)
      importMessage = "No saved Afterburner V/F profiles found.";
  } catch (e) {
    importMessage = e.message;
  }
  importSelection = afterburnerRows.length ? 0 : -1;
}
async function chooseImportFolder(input) {
  if (!input.files.length) return;
  const request = ++importRequest,
    files = [...input.files];
  const name = files[0].webkitRelativePath.split("/")[0];
  try {
    const parts = await Promise.all(
      files.map(async (f) => {
        const path = f.webkitRelativePath.split("/").slice(1).join("/");
        if (
          !/^MSIAfterburner\.cfg$|^Profiles\/(VEN_[^/]+|MSIAfterburner)\.cfg$/i.test(
            path,
          )
        )
          return null;
        return [path, await f.text()];
      }),
    );
    if (request !== importRequest || !dlg().open) return;
    setImportSource(Object.fromEntries(parts.filter(Boolean)), name);
  } catch (e) {
    if (request !== importRequest) return;
    setImportSource({}, name);
    importMessage = e.message;
  }
  importDialog();
}
function openImport() {
  if (!afterburnerRows.length && !importMessage)
    setImportSource(exampleAfterburner, "Example Afterburner");
  importDialog();
  dlg().addEventListener(
    "close",
    () => {
      importRequest++;
    },
    { once: true },
  );
}
function selectImportRow(index) {
  importSelection = index;
  importedPoint = -1;
  importDialog();
  document
    .querySelector('[data-import-row="' + index + '"]')
    ?.focus({ preventScroll: true });
}
function importDialog() {
  const entry = afterburnerRows[importSelection];
  const rows = afterburnerRows
    .map(
      (
        r,
        i,
      ) => `<tr class="${i === importSelection ? "selected" : ""}" onclick="selectImportRow(${i})">
    <td title="${esc(r.device)}">${esc(r.device.split("&SUBSYS")[0])}</td><td><button data-import-row="${i}" aria-pressed="${i === importSelection}">${esc(r.section)}</button></td>
    <td>${r.mv ? r.mv + " mV · " + r.clock + " MHz" : "—"}</td><td>${esc(r.status)}</td></tr>`,
    )
    .join("");
  showPanel(`<section class="importDialog"><h2>Import Afterburner</h2>
    <div class="importSource"><div><small>Afterburner directory</small><strong>${esc(importDirectory)}</strong></div>
    <input hidden id="importFolder" type="file" webkitdirectory multiple onchange="chooseImportFolder(this)">
    <button onclick="document.getElementById('importFolder').click()">Choose directory…</button></div>
    <div class="importGrid"><div class="tableWrap"><table><thead><tr><th>Device profile</th><th>Profile</th><th>Target</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>
    <div><canvas id="importCurve" tabindex="0" role="img" aria-label="Imported voltage and frequency curve, use arrow keys to inspect points"></canvas><small id="importPoint"></small></div></div>
    <p class="sub" role="status">${esc(importMessage || (entry?.status === "Ready" ? "The selected curve will appear in Profiles." : entry?.status || ""))}</p>
    <div class="dialogfoot"><button onclick="closePanel()">Cancel</button><button class="primary" ${entry?.status === "Ready" ? "" : "disabled"} onclick="acceptImport()">Import</button></div></section>`);
  requestAnimationFrame(drawImportCurve);
}
function drawImportCurve() {
  const canvas = document.getElementById("importCurve"),
    entry = afterburnerRows[importSelection];
  if (!canvas || !entry?.points.length) return;
  const { ctx, w, h } = canvasContext(canvas),
    points = entry.points,
    colors = design.colors;
  const xs = points.map((p) => p[0]),
    ys = points.map((p) => p[1]),
    x0 = Math.min(...xs),
    x1 = Math.max(...xs),
    y0 = Math.min(...ys) - 50,
    y1 = Math.max(...ys) + 100;
  const x = (v) => 46 + ((w - 66) * (v - x0)) / Math.max(1, x1 - x0),
    y = (v) => h - 28 - ((h - 46) * (v - y0)) / Math.max(1, y1 - y0);
  ctx.fillStyle = colors.plotSurface;
  ctx.fillRect(0, 0, w, h);
  ctx.font = "11px system-ui";
  ctx.fillStyle = colors.secondary;
  ctx.strokeStyle = colors.gridMajor;
  for (let i = 0; i < 3; i++) {
    const mv = x0 + ((x1 - x0) * i) / 2,
      mhz = y0 + ((y1 - y0) * i) / 2;
    ctx.textAlign = "center";
    ctx.fillText(Math.round(mv), x(mv), h - 8);
    ctx.textAlign = "right";
    ctx.fillText(Math.round(mhz), 40, y(mhz) + 3);
    strokePath(ctx, [
      [46, y(mhz)],
      [w - 20, y(mhz)],
    ]);
  }
  ctx.strokeStyle = colors.candidateCurve;
  ctx.lineWidth = 2;
  strokePath(
    ctx,
    points.map(([v, m]) => [x(v), y(m)]),
  );
  if (importedPoint < 0)
    importedPoint = Math.max(
      0,
      points.findIndex((p) => p[0] >= entry.mv),
    );
  const [v, m] = points[importedPoint];
  ctx.fillStyle = colors.selectedCurve;
  ctx.beginPath();
  ctx.arc(x(v), y(m), 4, 0, 2 * Math.PI);
  ctx.fill();
  document.getElementById("importPoint").textContent =
    Math.round(v) + " mV · " + Math.round(m) + " MHz";
  canvas.onclick = (e) => {
    importedPoint = points.reduce(
      (nearest, p, i) =>
        Math.abs(x(p[0]) - e.offsetX) <
        Math.abs(x(points[nearest][0]) - e.offsetX)
          ? i
          : nearest,
      0,
    );
    drawImportCurve();
  };
  canvas.onkeydown = (e) => {
    if (["ArrowLeft", "ArrowRight"].includes(e.key)) {
      e.preventDefault();
      importedPoint = Math.max(
        0,
        Math.min(
          points.length - 1,
          importedPoint + (e.key === "ArrowRight" ? 1 : -1),
        ),
      );
      drawImportCurve();
    }
  };
}
function saveImportedProfile(entry) {
  const id = profiles.length;
  profiles.push([
    `MSI Afterburner ${entry.section} ${entry.clock} MHz ${entry.mv} mV`,
    entry.mv,
    entry.clock,
    null,
    null,
    null,
  ]);
  profileMeta.push({
    tier: "None",
    origin: 0,
    valid: true,
    source: "MSI Afterburner",
    imported: true,
    copy: true,
    vf: structuredClone(entry.points),
    fan: entry.fan?.length ? structuredClone(entry.fan) : undefined,
    mem: 0,
    date: "Just now",
    deleted: false,
  });
  order.unshift(id);
  pi = id;
  return id;
}
function acceptImport() {
  const e = afterburnerRows[importSelection];
  if (e?.status !== "Ready") return;
  saveImportedProfile(e);
  closePanel();
  navigate("Profiles");
}
