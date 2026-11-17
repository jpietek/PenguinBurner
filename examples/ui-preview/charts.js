// The canvas and its external legend use the same traces and line styles.
function currentVerificationTier() {
  const configured = activeTiers().filter((t) =>
    selectedVerificationCandidate(t),
  );
  return configured[
    Math.min(configured.length - 1, Math.floor((ticks * configured.length) / 9))
  ];
}

function stockClockAt(mv) {
  const base = sample.base;
  const hi = base.findIndex(([voltage]) => voltage >= mv);
  if (hi < 0) return base.at(-1)[1];
  if (hi === 0) return base[0][1];
  const [loV, loClock] = base[hi - 1],
    [hiV, hiClock] = base[hi];
  return loClock + ((hiClock - loClock) * (mv - loV)) / (hiV - loV);
}

function visibleCurves() {
  const colors = design.colors;
  const traces = [
    {
      label: "Stock",
      points: sample.base,
      color: colors.baseCurve,
      width: 1.8,
      dash: [7, 4],
    },
  ];
  for (const tier of activeTiers()) {
    const last = plan.findLastIndex((r) => r.tier === tier);
    if (last < 0 || completed <= last) continue;
    const passing = tierCandidates(tier);
    const chosen =
      selectedVerificationCandidate(tier) || plan[suggestedCandidate(passing)];
    if (chosen)
      traces.push({
        label: tier,
        points: chosen.points,
        color: colors[tier.toLowerCase()],
        width: 2.4,
      });
  }
  const live =
    stage === "verifying"
      ? selectedVerificationCandidate(currentVerificationTier())
      : plan[completed];
  if ((stage === "running" || stage === "verifying") && live) {
    traces.push({
      label: "Live candidate",
      points: live.points,
      color: colors.candidateCurve,
      width: 2.2,
      live,
    });
  }
  if (selected >= 0 && plan[selected]) {
    traces.push({
      label: "Selected run " + (selected + 1),
      points: plan[selected].points,
      color: colors.selectedCurve,
      width: 3,
      dash: [7, 5],
    });
  }
  return traces;
}

function curveLegend() {
  const items = visibleCurves();
  if (completed > 1)
    items.splice(1, 0, {
      label: "Previous runs",
      color: design.colors.curveHistory,
    });
  return items
    .map(
      (t) =>
        '<span><i style="border-color:' +
        t.color +
        ";border-top-style:" +
        (t.dash ? "dashed" : "solid") +
        '"></i>' +
        t.label +
        "</span>",
    )
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

function plotVF() {
  const canvas = document.getElementById("vf");
  if (!canvas) return;
  const { ctx, w, h } = canvasContext(canvas);
  const left = 61,
    right = w - 24,
    top = 16,
    bottom = h - 39,
    colors = design.colors;
  const span = 400 / zoomRange,
    lo = Math.max(
      800,
      Math.min(1200 - span, (plan[selected]?.mv || 1000) - span / 2),
    );
  const x = (v) => left + ((right - left) * (v - lo)) / span,
    y = (v) => bottom - ((bottom - top) * (v - 1800)) / 1400;
  ctx.fillStyle = colors.plotSurface;
  ctx.fillRect(0, 0, w, h);
  ctx.font = "11px " + design.typography.family + ", system-ui, sans-serif";
  ctx.fillStyle = colors.secondary;
  ctx.textAlign = "center";
  for (let v = 800; v <= 1200; v += 10) {
    const px = x(v);
    if (px < left || px > right) continue;
    ctx.strokeStyle = v % 50 ? colors.gridMinor : colors.gridMajor;
    strokePath(ctx, [
      [px, top],
      [px, bottom],
    ]);
    if (v % 50 === 0) ctx.fillText(v, px, bottom + 15);
  }
  ctx.textAlign = "right";
  for (let mhz = 1800; mhz <= 3200; mhz += 100) {
    ctx.strokeStyle = mhz % 200 ? colors.gridMinor : colors.gridMajor;
    strokePath(ctx, [
      [left, y(mhz)],
      [right, y(mhz)],
    ]);
    if (mhz % 200 === 0) ctx.fillText(mhz, left - 7, y(mhz) + 3);
  }
  ctx.textAlign = "center";
  ctx.fillText("Voltage (mV)", (left + right) / 2, h - 4);
  ctx.save();
  ctx.translate(13, h / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText("Clock (MHz)", 0, 0);
  ctx.restore();
  ctx.save();
  ctx.beginPath();
  ctx.rect(left - 1, top - 2, right - left + 2, bottom - top + 4);
  ctx.clip();
  const project = (points) => points.map(([v, m]) => [x(v), y(m)]);
  ctx.globalAlpha = 0.55;
  ctx.strokeStyle = colors.curveHistory;
  plan.slice(1, completed).forEach((r) => strokePath(ctx, project(r.points)));
  ctx.globalAlpha = 1;
  for (const trace of visibleCurves()) {
    ctx.strokeStyle = trace.color;
    ctx.lineWidth = trace.width;
    ctx.setLineDash(trace.dash || []);
    const plotted = project(trace.points);
    strokePath(ctx, plotted);
    if (trace.live) {
      ctx.setLineDash([]);
      ctx.fillStyle = trace.color;
      for (const [px, py] of plotted) {
        ctx.beginPath();
        ctx.arc(px, py, 2, 0, 2 * Math.PI);
        ctx.fill();
      }
      const px = x(trace.live.mv),
        py = y(trace.live.meas + Math.sin(ticks * 1.9) * 5);
      ctx.lineWidth = 1;
      ctx.strokeStyle = colors.warning + "88";
      ctx.fillStyle = colors.warning;
      strokePath(ctx, [
        [px, top],
        [px, bottom],
      ]);
      strokePath(ctx, [
        [left, py],
        [right, py],
      ]);
      ctx.beginPath();
      ctx.arc(px, py, 4, 0, 2 * Math.PI);
      ctx.fill();
    }
  }
  ctx.restore();
}

function drawFingerprint(canvas) {
  const { ctx, w, h } = canvasContext(canvas),
    cx = w / 2,
    cy = h * 0.5,
    r = Math.min(w * 0.26, h * 0.31);
  const game = g(),
    colors = design.colors;
  const axes = [
    ["PWR ↓", "224 W", 224 / 360],
    ["GPU", "96%", 0.96],
    ["FPS ↑", game.fps + " FPS", Math.min(1, game.fps / game.target)],
    ["1% LOW ↑", game.low + " FPS", game.low / game.fps],
    ["CPU ↓", "72%", 0.72],
  ];
  const vertex = (i, f) => [
    cx + r * f * Math.sin((i * Math.PI * 2) / 5),
    cy - r * f * Math.cos((i * Math.PI * 2) / 5),
  ];
  function polygon(fractions) {
    ctx.beginPath();
    fractions.forEach((f, i) => {
      const [px, py] = vertex(i, f);
      if (i) ctx.lineTo(px, py);
      else ctx.moveTo(px, py);
    });
    ctx.closePath();
  }
  ctx.strokeStyle = colors.gridMajor;
  for (const f of [0.25, 0.5, 0.75, 1]) {
    polygon(axes.map(() => f));
    ctx.stroke();
  }
  for (let i = 0; i < 5; i++) strokePath(ctx, [[cx, cy], vertex(i, 1)]);
  polygon(axes.map((a) => a[2]));
  ctx.fillStyle = colors.balanced + "30";
  ctx.strokeStyle = colors.balanced;
  ctx.lineWidth = 2;
  ctx.fill();
  ctx.stroke();
  axes.forEach(([caption, value, fraction], i) => {
    const [px, py] = vertex(i, fraction);
    ctx.beginPath();
    ctx.arc(px, py, 3, 0, 2 * Math.PI);
    ctx.fillStyle = colors.balanced;
    ctx.fill();
    const [tx, ty] = vertex(i, 1.22);
    ctx.textAlign = i === 0 ? "center" : i < 3 ? "left" : "right";
    ctx.font = "600 11px " + design.typography.family + ", sans-serif";
    ctx.fillStyle = colors.secondary;
    ctx.fillText(caption, tx, ty);
    ctx.font = "600 12px " + design.typography.family + ", sans-serif";
    ctx.fillStyle = colors.ink;
    ctx.fillText(value, tx, ty + 15);
  });
}

function drawFrametime(canvas) {
  const { ctx, w, h } = canvasContext(canvas);
  const x0 = 45,
    x1 = w - 18,
    y0 = 10,
    y1 = h - 18;
  const py = (v) => y1 - ((y1 - y0) * v) / 35,
    colors = design.colors;
  ctx.font = "11px " + design.typography.family + ", sans-serif";
  ctx.fillStyle = colors.secondary;
  ctx.strokeStyle = colors.gridMajor;
  for (const tick of [30, 20, 10]) {
    ctx.fillText(tick + " ms", 5, py(tick) + 3);
    strokePath(ctx, [
      [x0, py(tick)],
      [x1, py(tick)],
    ]);
  }
  const points = Array.from({ length: 200 }, (_, i) => [
    x0 + ((x1 - x0) * i) / 199,
    py(
      1000 / g().fps +
        1.1 * Math.sin(i * 0.5) +
        1.6 * Math.sin(i * 2.1) +
        ((i + idx) % 47 === 0 ? 11 : 0),
    ),
  ]);
  ctx.strokeStyle = colors.accent;
  ctx.lineWidth = 2;
  strokePath(ctx, points);
  ctx.lineTo(x1, y1);
  ctx.lineTo(x0, y1);
  ctx.closePath();
  ctx.fillStyle = colors.accent + "17";
  ctx.fill();
}

function draw() {
  plotVF();
  const fingerprint = document.querySelector("[data-chart=fingerprint]");
  const frametime = document.querySelector("[data-chart=time]");
  if (fingerprint) drawFingerprint(fingerprint);
  if (frametime) drawFrametime(frametime);
}
