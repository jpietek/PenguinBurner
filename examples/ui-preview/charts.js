// Shared chart spec for every canvas plot: plotSurface background, minor and
// major grid, an axis line in `border`, rounded 12px secondary tick labels and
// an external legend built from real line swatches.
function chartTheme() {
  const c = design.colors,
    family = design.typography.family + ", system-ui, sans-serif",
    size = design.typography.caption;
  return {
    surface: c.plotSurface,
    gridMinor: c.gridMinor,
    gridMajor: c.gridMajor,
    axis: c.border,
    text: c.secondary,
    ink: c.ink,
    quiet: c.quiet,
    font: size + "px " + family,
    fontStrong: design.weights.semibold + " " + size + "px " + family,
    crosshair: withAlpha(c.ink, 0.45),
    badgeFill: c.raised,
    badgeStroke: c.border,
    dimAlpha: 0.35,
    tiers: Object.fromEntries(
      ["Efficiency", "Balanced", "Performance"].map((t) => [
        t,
        c[t.toLowerCase()],
      ]),
    ),
  };
}

function withAlpha(hex, alpha) {
  return (
    hex.slice(0, 7) +
    Math.round(Math.max(0, Math.min(1, alpha)) * 255)
      .toString(16)
      .padStart(2, "0")
  );
}

// A 1/2/5 × 10^n step so tick labels are round numbers.
function niceStep(span, count = 5) {
  const raw = Math.abs(span) / Math.max(1, count),
    power = 10 ** Math.floor(Math.log10(raw || 1)),
    unit = raw / power;
  return (unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 5 ? 5 : 10) * power;
}

function niceTicks(lo, hi, count = 5, step = niceStep(hi - lo, count)) {
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step)
    ticks.push(Math.round(v * 1e6) / 1e6);
  return ticks;
}

// Paint the surface, grid, axis line, tick labels and axis titles.
// box: {left, right, top, bottom}; x/y map data to pixels.
// axes: {x: {lo, hi, minor, major, format, title}, y: {...}}
function drawChartFrame(ctx, w, h, box, x, y, axes) {
  const t = chartTheme();
  ctx.fillStyle = t.surface;
  ctx.fillRect(0, 0, w, h);
  ctx.lineWidth = 1;
  ctx.setLineDash([]);
  const lines = (axis, draw) => {
    if (!axis) return;
    if (axis.minor) {
      ctx.strokeStyle = t.gridMinor;
      niceTicks(axis.lo, axis.hi, 0, axis.minor).forEach((v) => draw(v));
    }
    ctx.strokeStyle = t.gridMajor;
    niceTicks(axis.lo, axis.hi, 5, axis.major).forEach((v) => draw(v));
  };
  lines(axes.x, (v) =>
    strokePath(ctx, [
      [Math.round(x(v)) + 0.5, box.top],
      [Math.round(x(v)) + 0.5, box.bottom],
    ]),
  );
  lines(axes.y, (v) =>
    strokePath(ctx, [
      [box.left, Math.round(y(v)) + 0.5],
      [box.right, Math.round(y(v)) + 0.5],
    ]),
  );
  ctx.strokeStyle = t.axis;
  strokePath(ctx, [
    [box.left + 0.5, box.top],
    [box.left + 0.5, box.bottom + 0.5],
    [box.right, box.bottom + 0.5],
  ]);
  ctx.font = t.font;
  ctx.fillStyle = t.text;
  if (axes.x) {
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (const v of niceTicks(axes.x.lo, axes.x.hi, 5, axes.x.major))
      ctx.fillText((axes.x.format || String)(v), x(v), box.bottom + 6);
    if (axes.x.title)
      ctx.fillText(axes.x.title, (box.left + box.right) / 2, box.bottom + 24);
  }
  if (axes.y) {
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (const v of niceTicks(axes.y.lo, axes.y.hi, 5, axes.y.major))
      ctx.fillText((axes.y.format || String)(v), box.left - 7, y(v));
    if (axes.y.title) {
      ctx.save();
      ctx.translate(12, (box.top + box.bottom) / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.textAlign = "center";
      ctx.fillText(axes.y.title, 0, 0);
      ctx.restore();
    }
  }
  ctx.textBaseline = "alphabetic";
}

// A value badge (raised fill, border outline, ink text) pinned to an axis.
function drawBadge(ctx, text, px, py, align = "center") {
  const t = chartTheme();
  ctx.font = t.font;
  const w = ctx.measureText(text).width + 12,
    h = 20,
    left = align === "left" ? px : align === "right" ? px - w : px - w / 2;
  ctx.fillStyle = t.badgeFill;
  ctx.strokeStyle = t.badgeStroke;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(Math.round(left) + 0.5, Math.round(py - h / 2) + 0.5, w, h, 4);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = t.ink;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, left + w / 2, py + 0.5);
  ctx.textBaseline = "alphabetic";
}

// External legend entry with a real line swatch (solid, dashed or dotted).
function legendItem(label, color, { dash = false, dot = false, fill } = {}) {
  return (
    '<span class="legendItem"><i class="swatch' +
    (dash ? " dashed" : "") +
    (dot ? " dot" : "") +
    (fill ? " area" : "") +
    '" style="--swatch:' +
    color +
    (fill ? ";--swatch-fill:" + fill : "") +
    '" aria-hidden="true"></i>' +
    label +
    "</span>"
  );
}

function tierColor(tier) {
  return chartTheme().tiers[tier] || design.colors.candidateCurve;
}

// The V/F canvas and its external legend share one list of traces.
function visibleCurves() {
  const colors = design.colors;
  const traces = [
    {
      label: "Stock",
      points: sample.base,
      color: colors.baseCurve,
      width: 1.5,
      dash: [6, 4],
      stock: true,
    },
  ];
  if (profileShown) {
    traces.push({
      label: profileShown.label,
      points: profileShown.row.points,
      color: tierColor(profileShown.row.tier),
      width: 2.4,
      live: profileJob ? profileShown.row : null,
    });
    return traces;
  }
  for (const tier of tiers) {
    const index = tierTrace[tier];
    if (index == null || !plan[index]) continue;
    traces.push({
      label: tier + " tier",
      points: plan[index].points,
      color: tierColor(tier),
      width: 2.4,
    });
  }
  const live = runs[activeRun];
  if (live && live.kind !== "baseline") {
    traces.push({
      label: "Live candidate",
      points: plan[live.plan].points,
      color: colors.candidateCurve,
      width: 2.2,
      live: plan[live.plan],
    });
  } else if (live) {
    traces.push({ label: "", points: [], color: "", live: plan[live.plan], hidden: true });
  }
  const chosen = runs[selected];
  if (chosen && chosen.kind !== "baseline") {
    // No extra hue: the selected run keeps its tier colour and gains weight
    // and a halo while the other non-stock traces recede.
    const row = plan[chosen.plan];
    traces.push({
      label: "Run " + (selected + 1) + " (selected)",
      points: row.points,
      color: tierColor(row.tier),
      width: 3,
      selected: true,
    });
  }
  return traces;
}

function curveLegend() {
  const items = visibleCurves().filter((t) => !t.hidden);
  if (!profileShown && runs.some((r, i) => r.kind === "probe" && i !== activeRun))
    items.splice(1, 0, {
      label: "Previous runs",
      color: design.colors.curveHistory,
    });
  // The selected run shares its tier's hue, so its swatch carries the
  // same thick line and plotSurface halo as the trace.
  return items
    .map((t) => {
      const item = legendItem(uvText(t.label), t.color, { dash: !!t.dash, dot: !!t.live });
      return t.selected ? item.replace('class="swatch', 'class="swatch selected') : item;
    })
    .join("");
}

function canvasContext(canvas) {
  const w = canvas.clientWidth,
    h = canvas.clientHeight,
    dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  return { ctx, w, h };
}

function strokePath(ctx, points) {
  ctx.beginPath();
  points.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.stroke();
}

// Autoscale to the data, like the app's pyqtgraph CurvePlot. Wheel zooms
// both axes around the pointer, drag pans, double-click resets.
function plotDataBounds() {
  const curves = [sample.base, ...plan.slice(1).map((r) => r.points)];
  if (profileShown) curves.push(profileShown.row.points);
  let x0 = Infinity,
    x1 = -Infinity,
    y0 = Infinity,
    y1 = -Infinity;
  for (const points of curves)
    for (const [v, m] of points) {
      x0 = Math.min(x0, v);
      x1 = Math.max(x1, v);
      y0 = Math.min(y0, m);
      y1 = Math.max(y1, m);
    }
  const pad = (y1 - y0) * 0.06;
  return { x: [x0, x1], y: [Math.floor((y0 - pad) / 100) * 100, Math.ceil((y1 + pad) / 100) * 100] };
}
let vfGeom = null,
  vfPan = null;
function plotView() {
  return vfView || plotDataBounds();
}
function zoomPlot(factor, fx = 0.5, fy = 0.5) {
  const view = plotView(),
    zoom = ([lo, hi], f) => {
      const span = Math.max(1e-6, (hi - lo) * factor),
        at = lo + (hi - lo) * f;
      return [at - span * f, at - span * f + span];
    };
  vfView = { x: zoom(view.x, fx), y: zoom(view.y, 1 - fy) };
  plotVF();
}
function wheelPlot(ev) {
  ev.preventDefault();
  if (!vfGeom) return;
  const { box } = vfGeom,
    rect = ev.currentTarget.getBoundingClientRect(),
    fx = (ev.clientX - rect.left - box.left) / (box.right - box.left),
    fy = (ev.clientY - rect.top - box.top) / (box.bottom - box.top);
  zoomPlot(ev.deltaY < 0 ? 1 / 1.15 : 1.15, Math.max(0, Math.min(1, fx)), Math.max(0, Math.min(1, fy)));
}
function panPlot(dxPx, dyPx) {
  const view = plotView(),
    { box } = vfGeom,
    sx = (view.x[1] - view.x[0]) / (box.right - box.left),
    sy = (view.y[1] - view.y[0]) / (box.bottom - box.top);
  vfView = {
    x: [view.x[0] - dxPx * sx, view.x[1] - dxPx * sx],
    y: [view.y[0] + dyPx * sy, view.y[1] + dyPx * sy],
  };
  plotVF();
}
function panStart(ev) {
  if (ev.button !== 0 || !vfGeom) return;
  const canvas = ev.currentTarget;
  canvas.setPointerCapture(ev.pointerId);
  vfPan = { x: ev.clientX, y: ev.clientY };
  canvas.classList.add("panning");
  const move = (e) => {
    panPlot(e.clientX - vfPan.x, e.clientY - vfPan.y);
    vfPan = { x: e.clientX, y: e.clientY };
  };
  canvas.addEventListener("pointermove", move);
  canvas.addEventListener(
    "pointerup",
    () => {
      canvas.removeEventListener("pointermove", move);
      canvas.classList.remove("panning");
      vfPan = null;
    },
    { once: true },
  );
}
function resetPlotView() {
  vfView = null;
  plotVF();
}
function plotKey(ev) {
  const step = 24,
    keys = {
      "+": () => zoomPlot(1 / 1.15),
      "=": () => zoomPlot(1 / 1.15),
      "-": () => zoomPlot(1.15),
      0: resetPlotView,
      ArrowLeft: () => panPlot(step, 0),
      ArrowRight: () => panPlot(-step, 0),
      ArrowUp: () => panPlot(0, step),
      ArrowDown: () => panPlot(0, -step),
    };
  if (!vfGeom || !keys[ev.key]) return;
  ev.preventDefault();
  keys[ev.key]();
}

function plotVF() {
  // The log pane shares the plot's render pass (see syncLog).
  syncLog();
  const canvas = document.getElementById("vf");
  if (!canvas) return (vfGeom = null);
  const { ctx, w, h } = canvasContext(canvas);
  const theme = chartTheme(),
    box = { left: 64, right: w - 18, top: 14, bottom: h - 46 },
    colors = design.colors,
    view = plotView(),
    [x0, x1] = view.x,
    [y0, y1] = view.y;
  const x = (v) => box.left + ((box.right - box.left) * (v - x0)) / (x1 - x0),
    y = (v) => box.bottom - ((box.bottom - box.top) * (v - y0)) / (y1 - y0);
  vfGeom = { box };
  const xMajor = niceStep(x1 - x0, Math.max(3, Math.round((box.right - box.left) / 110))),
    yMajor = niceStep(y1 - y0, 8);
  drawChartFrame(ctx, w, h, box, x, y, {
    x: { lo: x0, hi: x1, minor: xMajor / 5, major: xMajor, title: "Voltage (mV)" },
    y: { lo: y0, hi: y1, minor: yMajor / 2, major: yMajor, title: "Clock (MHz)" },
  });
  ctx.save();
  ctx.beginPath();
  ctx.rect(box.left, box.top - 2, box.right - box.left + 2, box.bottom - box.top + 2);
  ctx.clip();
  const project = (points) => points.map(([v, m]) => [x(v), y(m)]);
  const traces = visibleCurves().filter((t) => !t.hidden),
    emphasis = traces.some((t) => t.selected);
  ctx.lineWidth = 1;
  ctx.globalAlpha = emphasis ? theme.dimAlpha : 0.6;
  ctx.strokeStyle = colors.curveHistory;
  if (!profileShown)
    runs.forEach((run, i) => {
      if (run.kind === "probe" && i !== activeRun)
        strokePath(ctx, project(plan[run.plan].points));
    });
  for (const trace of traces) {
    const plotted = project(trace.points);
    ctx.globalAlpha = emphasis && !trace.stock && !trace.selected ? theme.dimAlpha : 1;
    ctx.setLineDash(trace.dash || []);
    if (trace.selected) {
      ctx.strokeStyle = colors.plotSurface;
      ctx.lineWidth = 5 + trace.width;
      strokePath(ctx, plotted);
    }
    ctx.strokeStyle = trace.color;
    ctx.lineWidth = trace.width;
    strokePath(ctx, plotted);
    ctx.setLineDash([]);
    if (trace.live) {
      ctx.fillStyle = trace.color;
      for (const [px, py] of plotted) {
        ctx.beginPath();
        ctx.arc(px, py, 2, 0, 2 * Math.PI);
        ctx.fill();
      }
    }
  }
  ctx.globalAlpha = 1;
  const probe = visibleCurves().find((t) => t.live)?.live;
  let crosshair = null;
  if (probe) {
    // The probe's target point is its own ring; the live measured load
    // point carries the neutral crosshair.
    const tx = x(probe.mv),
      ty = y(probe.target),
      mv = probe.mv + Math.round(Math.sin(ticks * 1.3) * 3),
      mhz = probe.meas + Math.sin(ticks * 1.9) * 5;
    ctx.strokeStyle = colors.ink;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(tx, ty, 6, 0, 2 * Math.PI);
    ctx.stroke();
    crosshair = { px: x(mv), py: y(mhz), mv, mhz };
    ctx.lineWidth = 1;
    ctx.strokeStyle = theme.crosshair;
    strokePath(ctx, [
      [Math.round(crosshair.px) + 0.5, box.top],
      [Math.round(crosshair.px) + 0.5, box.bottom],
    ]);
    strokePath(ctx, [
      [box.left, Math.round(crosshair.py) + 0.5],
      [box.right, Math.round(crosshair.py) + 0.5],
    ]);
    ctx.fillStyle = colors.candidateCurve;
    ctx.strokeStyle = colors.plotSurface;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(crosshair.px, crosshair.py, 4.5, 0, 2 * Math.PI);
    ctx.fill();
    ctx.stroke();
  }
  ctx.restore();
  if (crosshair) {
    // Badges stay pinned to the axes while the view pans.
    const clampTo = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    drawBadge(ctx, Math.round(crosshair.mhz) + " MHz", box.left + 4, clampTo(crosshair.py, box.top + 10, box.bottom - 10), "left");
    drawBadge(ctx, crosshair.mv + " mV", clampTo(crosshair.px, box.left + 30, box.right - 30), box.bottom - 12);
  }
}

function draw() {
  plotVF();
  const fingerprint = document.querySelector("[data-chart=fingerprint]");
  const frametime = document.querySelector("[data-chart=time]");
  if (fingerprint) drawFingerprint(fingerprint);
  if (frametime) drawFrametime(frametime);
}
