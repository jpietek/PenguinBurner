// Auto UV scan flow: a simulated backend that replays sample.json through
// the same per-tier sequence as auto_uv/main_loop.py. A full scan runs
// Efficiency -> Balanced -> Performance; each tier's descent opens its own
// candidate dialog, runs that tier's final verification and saves the
// profile before the next tier starts.
const tiers = ["Efficiency", "Balanced", "Performance"];
const tierColors = tiers.map((t) => design.colors[t.toLowerCase()]);
const stock = {
  tier: "Stock",
  mv: 1000,
  target: 2762,
  meas: 2734,
  fps: 63.4,
  power: 341,
  temp: 67,
  fan: 41,
  passed: true,
  points: sample.base,
};
// Failure facts for the sample's failed probes, worded like ui/models.py.
const PROBE_FAILURES = {
  "Efficiency:840:2430": {
    label: "Clock too low",
    kind: "low-clock",
    severity: "recoverable",
    reason: "core clock 2402 MHz fell below the 2420 MHz floor",
  },
  "Balanced:840:2585": {
    label: "Average FPS low",
    kind: "fps-regression",
    severity: "recoverable",
    reason: "average FPS 62.30 is 1.7% below stock (limit 1.5%)",
  },
  "Performance:920:3010": {
    label: "Nvidia Xid fail",
    kind: "nvidia-xid",
    severity: "critical",
    reason: "NVRM: Xid 13, graphics exception during the probe",
  },
};
const FINAL_FAILURE = {
  label: "Vulkan device lost",
  kind: "fatal-output",
  severity: "critical",
  reason: "final verification ended early",
  fatal: ["VK_ERROR_DEVICE_LOST"],
};
const PROBE_TICKS = 4,
  FINAL_TICKS = 9,
  PROFILE_TICKS = 12,
  LOG_LIMIT = 6000;
const initialSetup = buildInitialSetup();
let settings = structuredClone(initialSetup),
  draft,
  stage = "idle", // coarse, read by the shell: running | verifying are busy
  phase = "", // download | probe | choice | final | stopping | resuming | profile
  plan = [],
  completed = 0,
  ticks = 0,
  selected = -1,
  logLines = [],
  timer,
  verified = [],
  useExamples = false,
  note = "",
  runs = [],
  activeRun = -1,
  tierState = {},
  tierTrace = {},
  tierSaved = {},
  finalJob = null,
  profileJob = null,
  profileShown = null,
  lastScan = null,
  resumeTier = null, // a tier whose descent finished but was never verified
  failedOnce = false,
  anyDiscarded = false,
  downloadPct = 0,
  headerStage = "",
  candidateText = "",
  statusText = "",
  preScanSilentFan = false,
  lastSavedProfile = -1,
  statusOwned = false,
  logEpoch = 0,
  logFollow = true,
  logScroll = 0,
  uvSplit = 64,
  vfView = null;
const dlg = () => document.getElementById("panel"),
  closePanel = () => dlg().close();
function showPanel(content) {
  dlg().innerHTML = content;
  if (!dlg().open) dlg().showModal();
}
const dismiss =
  '<div class="dialogfoot"><button onclick="closePanel()">Close</button></div>';
function info(label, description) {
  showPanel(
    "<h2>" + label + '</h2><p class="sub">' + description + "</p>" + dismiss,
  );
}
function est(key) {
  return sample.est[key].join("–") + " min";
}
function setupKey(s) {
  return JSON.stringify([
    s.gpu ?? 0,
    s.scope,
    s.scope === "Full scan" ? s.settings : [s.tier, s.settings[s.tier]],
  ]);
}
function isFull() {
  return settings.scope === "Full scan";
}
function activeTiers() {
  return isFull() ? tiers : [settings.tier];
}
function scanBusy() {
  return stage === "running" || stage === "verifying";
}
function log(text) {
  logLines.push("[" + new Date().toLocaleTimeString("en-GB") + "] " + text);
  if (logLines.length > LOG_LIMIT) {
    logLines.splice(0, logLines.length - LOG_LIMIT);
    logEpoch++;
  }
}
function resetLog() {
  logLines = [];
  logEpoch++;
  logFollow = true;
}
function probeText(row) {
  return row.mv + "mV @ " + row.target + "MHz";
}
// The live scan and the #verification / #scan-results fragments share these
// log lines, so every log reads like the same scan-stream.
function probeStartLine(index) {
  const row = plan[index];
  return (
    "probe_start " +
    (index ? row.tier.toLowerCase() + " candidate " : "stock baseline ") +
    probeText(row)
  );
}
function probeResultLine(row, failure) {
  return (
    "probe_result " +
    probeText(row) +
    " · " +
    (failure
      ? "fail · " + failure.label + " · " + failure.reason
      : "pass · " + row.fps.toFixed(2) + " FPS · " + row.power.toFixed(2) + " W")
  );
}
function tierStartedLine(tier) {
  return "tier_started: " + tier.toLowerCase() + " (" + (tiers.indexOf(tier) + 1) + "/3)";
}
function scanCommand() {
  const mode = isFull() ? "adaptive" : settings.tier.toLowerCase();
  return (
    "$ penguin-burner --auto-uv --auto-uv-mode " +
    mode +
    " --gpu-index " +
    (settings.gpu ?? 0) +
    " --scan-stream"
  );
}
function memoryStatus() {
  const tier = isFull() ? "Efficiency" : settings.tier;
  const mhz = settings.settings[tier].memory || 0;
  return mhz
    ? "Memory offset: +" + mhz + " MHz memory clock"
    : "Memory offset: none";
}
function failureFor(row) {
  return (
    PROBE_FAILURES[row.tier + ":" + row.mv + ":" + row.target] || {
      label: "Average FPS low",
      kind: "fps-regression",
      severity: "recoverable",
      reason: "average FPS below the stock floor",
    }
  );
}
function capFor(row) {
  if (row.power < 305) return { text: "none", samples: 0 };
  const samples = row.tier === "Performance" && row.target === 2980 ? 3 : 0;
  return { text: samples ? "sw-power+hw-power-brake" : "sw-power", samples };
}

// ---------- Scan lifecycle ----------

function beginScan() {
  const key = setupKey(draft),
    resume = lastScan && lastScan.key === key;
  settings = structuredClone(draft);
  closePanel();
  tab = "Auto UV";
  // Remember the silent-fan intent before the scan resets the GPU to stock.
  preScanSilentFan =
    typeof silentFanCurve !== "undefined" &&
    !!(silentFanCurve || (runtime && runtime.fan));
  if (resume) return resumeScan();
  if (appState.simulate.crashRecovery) {
    appState.simulate.crashRecovery = false;
    return startCrashRecovery();
  }
  startFreshScan();
}
function freshPlan() {
  return [
    stock,
    ...sample.rows.filter((r) => isFull() || r.tier === settings.tier),
  ];
}
function resetScanState() {
  plan = freshPlan();
  completed = 0;
  runs = [];
  activeRun = -1;
  selected = -1;
  tierState = isFull()
    ? Object.fromEntries(tiers.map((t) => [t, "pending"]))
    : {};
  tierTrace = {};
  tierSaved = {};
  finalJob = null;
  profileShown = null;
  failedOnce = false;
  anyDiscarded = false;
  lastScan = null;
  resumeTier = null;
  vfView = null;
}
function startFreshScan() {
  resetScanState();
  stage = "running";
  log(scanCommand());
  log("Requested settings " + JSON.stringify(requestedSettings(settings)));
  headerStage = "Starting";
  candidateText = "Writing to main Auto-UV profile store";
  statusText = memoryStatus();
  if (appState.simulate.dependencyDownload) {
    phase = "download";
    downloadPct = 0;
    headerStage = "Downloading dependencies";
    candidateText = "";
    log("dependency_progress: fetching Q2RTX-headless and the CUDA workload");
    setHeaderProgress(
      "Downloading dependencies 0%",
      0,
      "Q2RTX-headless 1.8.1 · 0 / 540 MB",
    );
  } else {
    startProbe();
  }
  publishStatus();
  render();
  armTimer();
}
// The first tier whose descent finished but which was neither saved nor
// skipped: resume reopens its candidate choice instead of skipping it.
function pendingChoiceTier(scanPlan, done, saved, skipped) {
  return (
    tiers.find((tier) => {
      const last = scanPlan.findLastIndex((row) => row.tier === tier);
      return last > 0 && last < done && !saved[tier] && !skipped.includes(tier);
    }) || null
  );
}
function resumeScan() {
  const saved = lastScan;
  plan = saved.plan;
  completed = saved.completed;
  tierSaved = saved.tierSaved;
  tierTrace = {};
  for (const tier of Object.keys(tierSaved))
    tierTrace[tier] = tierSaved[tier].index;
  // Finished probes and final verifications come back as saved measurements.
  runs = saved.runs.map((run) => ({ ...run, restored: true }));
  resumeTier = saved.pending;
  const focus = resumeTier || plan[completed].tier;
  if (isFull()) {
    tierState = {};
    for (const tier of tiers) {
      const position = tiers.indexOf(tier) - tiers.indexOf(focus);
      tierState[tier] = tierSaved[tier]
        ? "complete"
        : saved.skipped.includes(tier) || position < 0
          ? "skipped"
          : position === 0
            ? "active"
            : "pending";
    }
  }
  activeRun = -1;
  selected = -1;
  finalJob = null;
  profileShown = null;
  lastScan = null;
  log(scanCommand());
  log(
    "scan_resumed: restored " +
      completed +
      " saved measurements for GPU " +
      (settings.gpu ?? 0),
  );
  const safe = plan
    .slice(1, completed)
    .findLast((row) => row.passed && row.tier === focus);
  stage = "running";
  phase = "resuming";
  ticks = 0;
  headerStage = "Resuming";
  candidateText = safe ? probeText(safe) + " (saved)" : "";
  statusText = resumeTier
    ? "Restored previous scan measurements. " + resumeTier + " still needs its final verification."
    : "Restored previous scan measurements and curves. Continuing from saved progress.";
  publishStatus();
  render();
  armTimer();
}
function startCrashRecovery() {
  resetScanState();
  const tier = isFull() ? "Balanced" : settings.tier,
    crashed = plan.findIndex((r) => r.tier === tier && !r.passed),
    saved = plan
      .map((row, index) => ({ row, index }))
      .filter(
        ({ row, index }) => row.tier === tier && row.passed && index < crashed,
      );
  stage = "running";
  phase = "choice";
  log(scanCommand());
  log(
    "crash-recovery: the previous run ended during the probe at " +
      probeText(plan[crashed]),
  );
  headerStage = "Starting";
  candidateText = "";
  statusText = "A previous Auto-UV run ended during a GPU probe.";
  if (isFull()) tierState[tier] = "active";
  publishStatus();
  render();
  openCandidateDialog({
    reason: "previous-crash",
    tier,
    records: saved.map(({ index }) => candidateRecord(index)),
    defaultId: saved.at(-1).index,
    decision: {
      mv: plan[crashed].mv,
      target: plan[crashed].target,
      text: "probe interrupted (GPU stopped responding)",
    },
  });
}
function armTimer() {
  clearInterval(timer);
  timer = setInterval(tick, 700);
}
function tick() {
  ticks++;
  if (phase === "download") {
    downloadPct = Math.min(100, downloadPct + 20);
    setHeaderProgress(
      "Downloading dependencies " + downloadPct + "%",
      downloadPct,
      "Q2RTX-headless 1.8.1 · " +
        Math.round(5.4 * downloadPct) +
        " / 540 MB",
    );
    if (downloadPct >= 100) {
      setHeaderProgress(null);
      appState.simulate.dependencyDownload = false;
      log("dependency_progress: dependencies are ready");
      startProbe();
    }
  } else if (phase === "probe") {
    runs[activeRun].elapsed = (SHORT_PROBE_S * ticks) / PROBE_TICKS;
    if (ticks >= PROBE_TICKS) finishProbe();
  } else if (phase === "final") {
    const run = runs[activeRun];
    run.elapsed = (run.duration * ticks) / FINAL_TICKS;
    if (finalJob.failAt && ticks >= finalJob.failAt) failFinal();
    else if (ticks >= FINAL_TICKS) finishFinal();
  } else if (phase === "stopping") {
    finishStop();
  } else if (phase === "resuming") {
    const tier = resumeTier;
    resumeTier = null;
    if (tier) descentDone(tier);
    else startProbe();
  } else if (phase === "profile") {
    profileTick();
  } else {
    clearInterval(timer);
  }
  refresh();
}
function refresh() {
  publishStatus();
  if (tab === "Auto UV") refreshScan();
  else shellTools();
}
function publishStatus() {
  if (scanBusy()) {
    statusOwned = true;
    setGlobalStatus({
      running:
        profileJob ? headerStage : "Auto UV · " + headerStage,
      detail: [candidateText, statusText].filter(Boolean).join(" · "),
    });
  } else if (statusOwned) {
    statusOwned = false;
    setGlobalStatus(null);
  }
}
function tierStarted(tier) {
  const position = tiers.indexOf(tier) + 1;
  tierState[tier] = "active";
  const balanced = tierSaved.Balanced;
  statusText =
    tier === "Performance" && balanced
      ? "Performance reuses the Balanced downsweep (" +
        probeText(plan[balanced.index]) +
        ") — starting the Auto-OC climb."
      : "Scanning " + tier + " profile (" + position + "/3).";
  log(tierStartedLine(tier));
}
function startProbe() {
  const row = plan[completed],
    previous = plan[completed - 1];
  if (isFull() && tiers.includes(row.tier) && previous?.tier !== row.tier)
    tierStarted(row.tier);
  else if (completed === 0) statusText = memoryStatus();
  runs.push({
    kind: completed ? "probe" : "baseline",
    plan: completed,
    state: "running",
    elapsed: 0,
    duration: SHORT_PROBE_S,
    cap: capFor(row),
  });
  activeRun = runs.length - 1;
  phase = "probe";
  stage = "running";
  ticks = 0;
  headerStage = completed ? "Undervolting Candidates Sweep" : "Baseline";
  candidateText = probeText(row);
  log(probeStartLine(completed));
}
function finishProbe() {
  const run = runs[activeRun],
    row = plan[run.plan];
  if (appState.simulate.processFailure && run.plan >= 3)
    return processFailed();
  run.state = row.passed ? "pass" : "fail";
  run.failure = row.passed ? null : failureFor(row);
  run.elapsed = SHORT_PROBE_S;
  log(probeResultLine(row, run.failure));
  completed++;
  activeRun = -1;
  const next = plan[completed];
  if (tiers.includes(row.tier) && next?.tier !== row.tier)
    descentDone(row.tier);
  else if (next) startProbe();
  else finishScan();
}
function descentDone(tier) {
  const candidates = tierCandidates(tier);
  if (!candidates.length) {
    tierSkip(tier, "no stable candidate found");
    return continueAfterTier(tier);
  }
  const pair = comparisonPair(tier);
  const records = candidates.map(({ index }) =>
    candidateRecord(index, pair?.includes(index) ? { comparisonRuns: 2 } : {}),
  );
  phase = "choice";
  if (pair)
    log(
      "performance comparison: " +
        pair.map((i) => probeText(plan[i])).join(" vs ") +
        " · 2 matched runs each",
    );
  log("final_choice_request: " + tier.toLowerCase());
  statusText = "Choose the " + tier + " candidate for final verification.";
  openCandidateDialog({
    reason: pair
      ? "performance-comparison"
      : isFull()
        ? "adaptive-" + tier.toLowerCase()
        : "",
    tier,
    records,
    defaultId: pair ? pair[0] : suggestedCandidate(candidates),
  });
  refresh();
}
// select_final_candidate's three outcomes for every request reason.
function handleChoice(request, action, index, minutes) {
  const { reason, tier } = request,
    recovery =
      reason === "previous-crash" || reason === "final-verification-failed";
  if (action === "select") {
    log(
      tier +
        " candidate " +
        probeText(plan[index]) +
        " · " +
        minutes +
        " min verification",
    );
    tierTrace[tier] = index;
    log("tier_confirmed: " + tier.toLowerCase() + " " + probeText(plan[index]));
    return startFinal(
      tier,
      index,
      minutes,
      reason === "previous-crash" ? "resume" : "final",
      reason === "previous-crash" || reason === "user-stop" ? "finish" : "next",
    );
  }
  if (action === "abort") return endScan("aborted");
  if (recovery) {
    log("Starting a new scan from scratch");
    return startFreshScan();
  }
  // Discard: a single tier saves nothing; a full scan skips this tier.
  anyDiscarded = true;
  log("final_choice_discarded: " + tier.toLowerCase());
  if (reason === "user-stop" || !isFull()) return endScan("discarded");
  tierSkip(tier, "discarded");
  continueAfterTier(tier);
  refresh();
  armTimer();
}
function tierSkip(tier, reason) {
  const next = tiers[tiers.indexOf(tier) + 1];
  tierState[tier] = "skipped";
  statusText =
    tier +
    " tier skipped: " +
    reason +
    (next && isFull() ? ". Continuing with " + next + "." : "");
  log("tier_skipped: " + tier.toLowerCase() + " · " + reason);
}
function startFinal(tier, index, minutes, kind = "final", then = "next") {
  const row = plan[index];
  runs.push({
    kind,
    plan: index,
    tier,
    state: "running",
    elapsed: 0,
    duration: minutes * 60,
    cap: capFor(row),
  });
  activeRun = runs.length - 1;
  finalJob = {
    tier,
    index,
    minutes,
    kind,
    then,
    failAt:
      kind === "final" &&
      appState.simulate.finalVerificationFailure &&
      !failedOnce
        ? 5
        : 0,
  };
  if (isFull()) tierState[tier] = "active";
  stage = "verifying";
  phase = "final";
  ticks = 0;
  headerStage = kind === "resume" ? "Resume verification" : "Final verification";
  candidateText = probeText(row);
  statusText =
    "Verifying " + tier + " for " + fmtDuration(minutes * 60) + ". Accelerated replay.";
  log(
    "probe_start " + (kind === "resume" ? "resume-verify " : "final ") + probeText(row),
  );
  refresh();
  armTimer();
}
function finishFinal() {
  const run = runs[activeRun],
    { tier, index, minutes, then } = finalJob,
    row = plan[index];
  run.state = "pass";
  run.elapsed = run.duration;
  const id = saveVerifiedCandidate(
    tier,
    row,
    settings.settings[tier].memory,
    minutes,
  );
  lastSavedProfile = id;
  pi = id;
  tierSaved[tier] = { index, minutes, id };
  tierState[tier] = "complete";
  activeRun = -1;
  finalJob = null;
  stage = "running";
  log("final_result: " + tier + " · " + probeText(row) + " · profile saved");
  const next = isFull() && then === "next" && tiers[tiers.indexOf(tier) + 1];
  statusText =
    tier +
    " profile complete." +
    (next ? " Continuing with " + next + "." : " All selected profiles are complete.");
  if (then === "finish") finishScan();
  else continueAfterTier(tier);
}
function failFinal() {
  const run = runs[activeRun],
    { tier, index, minutes } = finalJob,
    row = plan[index];
  failedOnce = true;
  run.state = "fail";
  run.failure = {
    ...FINAL_FAILURE,
    reason: FINAL_FAILURE.reason + " after " + fmtDuration(run.elapsed),
  };
  activeRun = -1;
  finalJob = null;
  stage = "running";
  phase = "choice";
  delete tierTrace[tier];
  log(
    "final verification failed: " + probeText(row) + " · " + FINAL_FAILURE.fatal[0],
  );
  const remaining = tierCandidates(tier).filter((c) => c.index !== index);
  if (!remaining.length) return processFailed();
  statusText = "Final verification failed. Choose a safer candidate.";
  openCandidateDialog({
    reason: "final-verification-failed",
    tier,
    records: remaining.map(({ index: i }) => candidateRecord(i)),
    defaultId: remaining[0].index,
    minutes,
    decision: {
      mv: row.mv,
      target: row.target,
      text: "failed final verification (" + FINAL_FAILURE.label + ")",
    },
  });
}
function continueAfterTier(tier) {
  const next = isFull() && tiers[tiers.indexOf(tier) + 1];
  if (!next) return finishScan();
  if (completed < plan.length) return startProbe();
  // Every probe was already replayed: go straight to the next tier's choice.
  tierStarted(next);
  descentDone(next);
}
function finishScan() {
  endScan(Object.keys(tierSaved).length ? "complete" : anyDiscarded ? "discarded" : "stopped");
}
function markRunning(state) {
  for (const run of runs) if (run.state === "running" || run.state === "stopping") run.state = state;
}
function markUnfinishedTiers(terminal) {
  for (const tier of Object.keys(tierState)) {
    if (tierState[tier] === "active") tierState[tier] = terminal;
    else if (tierState[tier] === "pending") tierState[tier] = "not-run";
  }
}
const OUTCOMES = {
  complete: ["Complete", "Final verification complete.", 0],
  stopped: ["Stopped", "Auto-UV stopped.", 1],
  aborted: ["Aborted", "Auto-UV aborted by user.", 1],
  discarded: [
    "Discarded",
    "Final verification discarded. No Auto-UV profile was saved.",
    0,
  ],
  failed: ["Error", "Auto-UV failed.", 1],
};
function endScan(outcome) {
  clearInterval(timer);
  const [label, text, exitCode] = OUTCOMES[outcome];
  if (outcome === "stopped") markRunning("stopped");
  else if (outcome === "aborted") markRunning("aborted");
  else if (outcome === "discarded") markRunning("discarded");
  else if (outcome === "failed") markRunning("failed");
  markUnfinishedTiers(outcome === "failed" ? "failed" : "stopped");
  phase = "";
  activeRun = -1;
  finalJob = null;
  stage = outcome;
  headerStage = label;
  candidateText = "";
  statusText = text;
  setHeaderProgress(null);
  log(
    "Auto-UV process " +
      (exitCode ? "stopped" : "finished") +
      ": exit_code=" +
      exitCode,
  );
  // A discarded choice is final; otherwise a verified-but-unsaved tier
  // keeps the scan resumable even when every probe already ran.
  const skipped = tiers.filter((t) => tierState[t] === "skipped"),
    pending =
      outcome === "discarded"
        ? null
        : pendingChoiceTier(plan, completed, tierSaved, skipped);
  lastScan =
    outcome !== "complete" && completed > 0 && (completed < plan.length || pending)
      ? {
          key: setupKey(settings),
          plan,
          completed,
          tierSaved,
          skipped,
          pending,
          runs: structuredClone(
            runs.filter((run) => run.state === "pass" || run.state === "fail"),
          ),
        }
      : null;
  publishStatus();
  if (outcome === "complete") return applyVerifiedProfile();
  if (tab === "Auto UV") refreshScan();
  else render();
  if (outcome === "failed") {
    showErrorDetails(
      "Auto-UV failed",
      "Auto-UV scan stopped unexpectedly. The full error details can be copied for troubleshooting.",
      processFailureDetails(
        "Auto-UV scan",
        exitCode,
        "NormalExit",
        logLines.slice(-80).join("\n"),
      ),
    );
  }
}
function processFailed() {
  appState.simulate.processFailure = false;
  log("Traceback (most recent call last):");
  log('  File "auto_uv/main_loop.py", line 1311, in run_adaptive_scan');
  log("RuntimeError: Q2RTX exited with status 139 (SIGSEGV)");
  endScan("failed");
}
// A finished scan switches to Profiles and applies the new profile through
// the Profiles apply action (startup too when Apply on startup is ticked),
// restoring the silent-fan intent captured before the scan.
function applyVerifiedProfile() {
  const id = lastSavedProfile,
    ok = id >= 0 && !!profiles[id];
  useExamples = false;
  if (ok) {
    pi = id;
    // Point Profiles at the scanned GPU so its apply is not refused.
    const gpu = profileMeta[id]?.gpu;
    if (typeof profileTarget !== "undefined" && gpu != null) profileTarget = gpu;
    if (typeof silentFanCurve !== "undefined") silentFanCurve = preScanSilentFan;
    if (typeof selectProfiles === "function") selectProfiles([id]);
    log("Applying " + profiles[id][0] + " · " + probeText({ mv: profiles[id][1], target: profiles[id][2] }));
  }
  navigate("Profiles");
  if (ok && typeof applyProfile === "function") applyProfile(id);
}
function stopScan() {
  if (phase === "profile") return stopProfileVerification();
  if (phase === "download") {
    log("Stopped by user");
    return endScan("stopped");
  }
  if (phase !== "probe" && phase !== "final") return;
  runs[activeRun].state = "stopping";
  finalJob && (finalJob.stopped = true);
  phase = "stopping";
  ticks = 0;
  headerStage = "Stopping";
  statusText = "Stopping Auto-UV.";
  log("Stop requested by user");
  refresh();
}
function finishStop() {
  const run = runs[activeRun];
  run.state = "stopped";
  activeRun = -1;
  const tier = plan[run.plan].tier;
  if (
    run.kind === "probe" &&
    tiers.includes(tier) &&
    tierCandidates(tier).length
  ) {
    phase = "choice";
    statusText = "Auto-UV was stopped. Choose a stable candidate or discard.";
    const candidates = tierCandidates(tier);
    openCandidateDialog({
      reason: "user-stop",
      tier,
      records: candidates.map(({ index }) => candidateRecord(index)),
      defaultId: candidates[0].index,
    });
    return;
  }
  endScan("stopped");
}

// ---------- Profile verification (Profiles → Verify) ----------

function startProfileVerification(profileIndex, minutes) {
  if (scanBusy() || !profiles[profileIndex]) return;
  ensureDaemonReady(
    () => beginProfileVerification(profileIndex, minutes),
    "Verifying an Auto-UV profile",
  );
}
function beginProfileVerification(profileIndex, minutes) {
  const p = profiles[profileIndex],
    meta = profileMeta[profileIndex] || {},
    points =
      meta.vf ||
      (typeof profileCurve === "function"
        ? profileCurve("vf", profileIndex)
        : sample.base),
    label = p[0] + " · " + probeText({ mv: p[1], target: p[2] });
  profileJob = {
    index: profileIndex,
    label,
    row: {
      tier: meta.tier || p[0],
      mv: p[1],
      target: p[2],
      meas: meta.meas || p[2] - 22,
      points,
    },
    duration: Math.max(1, Math.min(MAX_FINAL_MINUTES, minutes)) * 60,
    elapsed: 0,
    fail: !!appState.simulate.processFailure,
    priorStage: stage,
  };
  profileShown = profileJob;
  stage = "verifying";
  phase = "profile";
  ticks = 0;
  headerStage = "Profile verification";
  candidateText = label;
  statusText = "Verifying " + label + " with Q2RTX.";
  vfView = null;
  log(
    "$ penguin-burner --verify-profile " +
      p[0].toLowerCase() +
      " --duration " +
      profileJob.duration +
      " --gpu-index " +
      appState.selectedGpu,
  );
  setHeaderProgress(profileProgressText(), 0, statusText);
  armTimer();
  navigate("Auto UV");
}
function profileProgressText() {
  return (
    "Verifying profile " +
    fmtDuration(profileJob.elapsed) +
    " / " +
    fmtDuration(profileJob.duration)
  );
}
function profileTick() {
  const job = profileJob;
  job.elapsed = Math.min(job.duration, (job.duration * ticks) / PROFILE_TICKS);
  setHeaderProgress(
    profileProgressText(),
    (100 * job.elapsed) / job.duration,
    statusText,
  );
  if (job.fail && ticks >= 6) return endProfileVerification("failed");
  if (ticks >= PROFILE_TICKS) endProfileVerification("passed");
}
function stopProfileVerification() {
  headerStage = "Stopping";
  statusText = "Stopping profile verification.";
  phase = "profileStopping";
  refresh();
  setTimeout(() => endProfileVerification("stopped"), 500);
}
function endProfileVerification(result) {
  clearInterval(timer);
  const job = profileJob;
  profileJob = null;
  phase = "";
  stage = job.priorStage;
  log(
    "Profile verification finished: exit_code=" + (result === "passed" ? 0 : 1),
  );
  candidateText = "";
  if (result === "passed") {
    headerStage = "Idle";
    statusText = "Profile verification complete.";
    setHeaderProgress(
      "Verifying profile " + fmtDuration(job.duration) + " / " + fmtDuration(job.duration),
      100,
      statusText,
    );
    setTimeout(() => {
      if (!profileJob) setHeaderProgress(null);
    }, 2500);
  } else {
    setHeaderProgress(null);
    headerStage = result === "stopped" ? "Idle" : "Error";
    statusText =
      result === "stopped"
        ? "Profile verification stopped."
        : "Profile verification failed.";
  }
  if (result === "failed") appState.simulate.processFailure = false;
  // Profiles binds a legacy profile to the target GPU and promotes a pass.
  if (typeof profileVerificationFinished === "function")
    profileVerificationFinished(job.index, result === "passed");
  else if (result === "passed" && profileMeta[job.index])
    profileMeta[job.index].valid = true;
  refresh();
  if (result === "failed")
    showErrorDetails(
      "Profile verification failed",
      "Profile verification stopped unexpectedly. The full error details can be copied for troubleshooting.",
      processFailureDetails(
        "Profile verification",
        1,
        "NormalExit",
        logLines.slice(-80).join("\n"),
      ),
    );
}

// ---------- Fragments (#verification, #scan-results) ----------

// guide.js seeds plan/completed directly; derive the rows, tier states and
// log the live flow would have produced, so table, strip and log agree.
function ensureFragmentState() {
  if (runs.length || !completed || profileJob) return;
  const probe = (i) => ({
    kind: i ? "probe" : "baseline",
    plan: i,
    state: plan[i].passed ? "pass" : "fail",
    failure: plan[i].passed ? null : failureFor(plan[i]),
    elapsed: SHORT_PROBE_S,
    duration: SHORT_PROBE_S,
    cap: capFor(plan[i]),
  });
  if (stage === "choice") {
    // Mid full scan: Efficiency's descent just finished.
    completed = plan.findLastIndex((r) => r.tier === activeTiers()[0]) + 1;
    runs = plan.slice(0, completed).map((_, i) => probe(i));
    tierState = isFull()
      ? Object.fromEntries(tiers.map((t, i) => [t, i ? "pending" : "active"]))
      : {};
    stage = "running";
    phase = "choice";
    headerStage = "Undervolting Candidates Sweep";
    statusText = "Choose the " + activeTiers()[0] + " candidate for final verification.";
    seedFragmentLog();
    return;
  }
  for (let i = 0; i < completed; i++) {
    runs.push(probe(i));
    const tier = plan[i].tier;
    if (stage === "complete" && tiers.includes(tier) && plan[i + 1]?.tier !== tier) {
      const index = suggestedCandidate(tierCandidates(tier));
      tierTrace[tier] = index;
      tierState[tier] = "complete";
      const minutes = defaultVerificationMinutes(tier);
      runs.push({
        kind: "final",
        plan: index,
        tier,
        state: "pass",
        elapsed: minutes * 60,
        duration: minutes * 60,
        cap: capFor(plan[index]),
      });
    }
  }
  if (stage === "complete") {
    headerStage = "Complete";
    statusText = "Final verification complete.";
  }
  seedFragmentLog();
}
// Replays the scan-stream lines for the derived runs (completed runs only).
function seedFragmentLog() {
  resetLog();
  log(scanCommand());
  log("Requested settings " + JSON.stringify(requestedSettings(settings)));
  for (const run of runs) {
    const row = plan[run.plan];
    if (run.kind === "final") {
      log("final_choice_request: " + run.tier.toLowerCase());
      log(
        run.tier + " candidate " + probeText(row) + " · " + run.duration / 60 + " min verification",
      );
      log("tier_confirmed: " + run.tier.toLowerCase() + " " + probeText(row));
      log("probe_start final " + probeText(row));
      log("final_result: " + run.tier + " · " + probeText(row) + " · profile saved");
      continue;
    }
    if (isFull() && tiers.includes(row.tier) && plan[run.plan - 1]?.tier !== row.tier)
      log(tierStartedLine(row.tier));
    log(probeStartLine(run.plan));
    log(probeResultLine(row, run.failure));
  }
  if (stage === "complete") log("Auto-UV process finished: exit_code=0");
}
function verifySetup() {
  ensureFragmentState();
  if (phase !== "choice") return;
  const tier = tiers.find((t) => tierState[t] === "active") || activeTiers()[0];
  descentDone(tier);
}

// ---------- View ----------

function stageTone() {
  if (stage === "failed" || headerStage === "Error") return "error";
  if (["stopped", "aborted"].includes(stage) || phase === "stopping") return "warning";
  if (stage === "complete") return "good";
  if (scanBusy()) return "running";
  return "idle";
}
function statusBlock() {
  const idle = !plan.length && !profileShown;
  const title = idle ? "Ready to scan" : headerStage || "Idle",
    detail = idle
      ? "No Auto UV runs in this session. Choose a GPU and scan scope to get started."
      : statusText;
  return (
    '<div class="uvStage"><span class="uvDot ' +
    stageTone() +
    '" aria-hidden="true"></span><h2>' +
    uvText(title) +
    "</h2>" +
    (candidateText
      ? '<span class="uvCandidateLabel" title="Current candidate. Select to copy.">' +
        uvText(candidateText) +
        "</span>"
      : "") +
    '</div><p class="uvStatusText">' +
    uvText(detail) +
    "</p>"
  );
}
const TIER_LABELS = {
  pending: "Pending",
  active: "Scanning",
  complete: "✓ Complete",
  skipped: "Skipped",
  stopped: "Stopped",
  failed: "Failed",
  "not-run": "Not run",
};
function tierStrip() {
  if (!isFull() || !Object.keys(tierState).length) return "";
  return (
    '<div class="uvTiers" role="list" aria-label="Full scan progress"><span class="uvTiersTitle">Full scan</span>' +
    tiers
      .map((t, i) => {
        const state = tierState[t] || "pending";
        const label =
          state === "active" && phase === "final" && finalJob?.tier === t
            ? "Final verification"
            : TIER_LABELS[state];
        return (
          '<div role="listitem" class="uvTier ts-' +
          state +
          '" style="--tier:' +
          tierColors[i] +
          '"><b>' +
          (i + 1) +
          ". " +
          t +
          "</b><small>" +
          label +
          "</small></div>"
        );
      })
      .join("") +
    "</div>"
  );
}
function logPane() {
  return (
    '<div class="uvLogPane"><div class="uvLogHead"><h3 id="uvLogTitle">Log</h3><span id="uvLogCount">' +
    logLines.length +
    ' lines</span><span class="copymsg" id="copied" role="status">' +
    uvText(note) +
    '</span><button id="copyFullLog" onclick="copyLogs()" ' +
    (logLines.length ? "" : "disabled") +
    '>Copy logs</button></div><div class="uvLogBody"><pre id="uvLog" tabindex="0" aria-labelledby="uvLogTitle" onscroll="logScrolled(this)" data-count="' +
    logLines.length +
    '" data-epoch="' +
    logEpoch +
    '">' +
    logHtml(logLines) +
    "</pre></div></div>"
  );
}
function scanView() {
  ensureFragmentState();
  const idle = !plan.length && !profileShown;
  const gpu = settings.gpu ?? appState.selectedGpu ?? 0;
  return (
    '<div class="uvView" data-mode="' +
    (idle ? "idle" : "active") +
    '"><div class="titlebar"><div><h1>Auto UV</h1><p>GPU ' +
    gpu +
    " · " +
    uvText(gpuFacts(gpu).name) +
    '</p></div></div><section class="card uvScanCard"><div class="uvStatus" id="uvStatus">' +
    statusBlock() +
    '</div><div id="uvTiers">' +
    tierStrip() +
    "</div>" +
    (idle
      ? '<div class="plotbox empty"><h3>Voltage / frequency curve</h3><small>The base curve appears when the scan starts measuring.</small></div>'
      : '<div class="uvSplit" id="uvSplit" style="--split:' +
        uvSplit +
        '%"><div class="uvPlotPane"><div class="plotbox"><canvas id="vf" tabindex="0" aria-label="Voltage-frequency plot. Scroll to zoom, drag to pan, double-click to reset." onwheel="wheelPlot(event)" onpointerdown="panStart(event)" ondblclick="resetPlotView()" onkeydown="plotKey(event)"></canvas></div><div class="legend" id="uvLegend">' +
        curveLegend() +
        '</div></div><div class="uvSash" role="separator" tabindex="0" aria-orientation="vertical" aria-label="Resize plot and log" aria-valuemin="35" aria-valuemax="80" aria-valuenow="' +
        uvSplit +
        '" onpointerdown="sashStart(event)" onkeydown="sashKey(event)"></div>' +
        logPane() +
        "</div>") +
    '</section><section class="card">' +
    head("Undervolting runs", '<span id="uvRunsMeta">' + runsMeta() + "</span>") +
    '<div class="scanTable"><table><thead><tr>' +
    [
      ["Run", ""],
      ["Voltage", "mV"],
      ["Target clock", "MHz"],
      ["Core offset", "MHz"],
      ["Measured clock", "MHz"],
      ["Frame rate", "FPS"],
      ["Board power", "W"],
      ["Temperature", "°C"],
      ["Fan speed", "%"],
      ["Power cap", "reason"],
      ["Efficiency", "FPS/W"],
      ["Result", ""],
      ["Status", ""],
    ]
      .map(
        ([l, u]) =>
          '<th scope="col"' +
          (l === "Core offset"
            ? ' title="Target clock delta from measured baseline"'
            : "") +
          ">" +
          l +
          (["Frame rate", "Board power", "Efficiency"].includes(l)
            ? '<span class="metricpair units"><small>' +
              u +
              "</small><small>Δ stock</small></span>"
            : "<small>" + u + "</small>") +
          "</th>",
      )
      .join("") +
    '</tr></thead><tbody id="uvRuns">' +
    runRows() +
    "</tbody></table></div>" +
    '<p class="emptymsg" id="uvRunsEmpty"' +
    (runs.length ? " hidden" : "") +
    ">No runs yet</p></section></div>"
  );
}
function runsMeta() {
  if (!plan.length) return "Measurements and results appear here during a scan.";
  const done = runs.filter((r) => r.state !== "running" && r.state !== "stopping").length;
  return settings.scope + " · " + done + (done === 1 ? " completed run" : " completed runs");
}
function deltaText(value, baseline, lowerBetter) {
  const pct = (value / baseline - 1) * 100,
    score = lowerBetter ? -pct : pct;
  const tone = Math.abs(pct) < 1 ? "flat" : score > 0 ? "gain" : "loss";
  const label = (pct > 0 ? "+" : "") + pct.toFixed(1) + "%";
  return (
    '<small class="delta ' +
    tone +
    '" title="Change from measured stock baseline">' +
    label +
    "</small>"
  );
}
function metricPair(value, delta) {
  return (
    '<span class="metricpair"><span class="reading">' +
    value +
    "</span>" +
    (delta || '<small class="delta blank" aria-hidden="true"></small>') +
    "</span>"
  );
}
function failureTooltip(run) {
  const f = run.failure;
  if (!f) {
    if (run.restored) return "Saved measurement: passed";
    if (run.kind === "baseline") return "Baseline reference run";
    return "";
  }
  return [
    (run.restored ? "Saved measurement: " : "") + f.reason,
    "kind: " + f.kind,
    "severity: " + f.severity,
    f.fatal ? "fatal output: " + f.fatal.join(", ") : "",
    "log: ~/.local/state/penguin-burner/auto-uv/run-" +
      String(runs.indexOf(run) + 1).padStart(4, "0") +
      ".log",
  ]
    .filter(Boolean)
    .join("\n");
}
const RESULT = {
  running: ["Running", "pending running"],
  stopping: ["Stopping", "pending"],
  stopped: ["Stopped", "pending"],
  aborted: ["Aborted", "pending"],
  discarded: ["Discarded", "muted"],
  failed: ["Failed", "bad"],
};
function rowTone(run) {
  if (run.state === "running") return "running";
  if (["stopping", "stopped", "aborted"].includes(run.state)) return "warning";
  if (run.state === "discarded") return "neutral";
  if (run.state === "failed") return "error";
  if (run.state === "fail")
    return run.failure?.severity === "recoverable" ? "warning" : "error";
  return run.kind === "baseline" ? "baseline" : "good";
}
function statusCell(run) {
  const total = run.duration,
    pct = Math.max(0, Math.min(100, (100 * run.elapsed) / total)),
    stageName =
      run.kind === "final"
        ? "Final · "
        : run.kind === "resume"
          ? "Resume · "
          : "";
  const text =
    run.state === "fail"
      ? run.failure.label
      : run.state === "running" || run.state === "pass"
        ? stageName + fmtDuration(run.elapsed) + " / " + fmtDuration(total)
        : (RESULT[run.state]?.[0] || run.state);
  return (
    '<div class="runprog tone-' +
    rowTone(run) +
    '" style="--pct:' +
    pct.toFixed(1) +
    '%"><span>' +
    uvText(text) +
    "</span></div>"
  );
}
function runRows() {
  return runs
    .map((run, i) => {
      const r = plan[run.plan],
        measured = run.state === "pass" || run.state === "fail",
        isBase = run.kind === "baseline",
        oc = Math.round(r.target - stock.meas),
        delta = (n, b, lowerBetter = false) =>
          !isBase && measured ? deltaText(n, b, lowerBetter) : "",
        val = (n, d = 2) => (measured ? n.toFixed(d) : "—"),
        [resultText, resultClass] =
          run.state === "pass"
            ? ["Pass", "good"]
            : run.state === "fail"
              ? ["Failed", run.failure?.severity === "recoverable" ? "pending" : "bad"]
              : RESULT[run.state],
        tip = failureTooltip(run),
        cap = run.cap || capFor(r),
        capTip =
          "NVML clocks throttle reason: " +
          cap.text +
          (cap.samples
            ? "\nhw-power-brake on " +
              cap.samples +
              " samples: the board's power delivery limited the clock, not the configured power limit"
            : ""),
        live =
          run.state === "running" &&
          Math.round(r.meas + Math.sin(ticks * 1.9) * 5);
      return (
        '<tr class="tone-' +
        rowTone(run) +
        (run.kind === "final" || run.kind === "resume" ? " final" : "") +
        (run.restored ? " restored" : "") +
        (i === selected ? " selected" : "") +
        '" onclick="chooseRun(' +
        i +
        ')"' +
        (isBase ? ' title="Baseline reference run"' : "") +
        '><td><button id="run' +
        i +
        '" aria-pressed="' +
        (i === selected) +
        '" aria-label="Run ' +
        (i + 1) +
        (run.kind === "final" ? ", final verification" : "") +
        '">' +
        (i + 1) +
        "</button></td><td>" +
        r.mv +
        "</td><td>" +
        r.target +
        '</td><td title="Target clock delta from measured baseline">' +
        (isBase ? "-" : (oc > 0 ? "+" : "") + oc) +
        "</td><td>" +
        (measured ? r.meas.toFixed(2) : live ? live.toFixed(0) : "—") +
        "</td><td>" +
        metricPair(val(r.fps), delta(r.fps, stock.fps)) +
        "</td><td>" +
        metricPair(val(r.power), delta(r.power, stock.power, true)) +
        "</td><td>" +
        val(r.temp) +
        "</td><td>" +
        val(r.fan) +
        '</td><td title="' +
        uvText(measured || run.state === "running" ? capTip : "") +
        '">' +
        (measured || run.state === "running"
          ? cap.text.replaceAll("+", "+<wbr>")
          : "—") +
        "</td><td" +
        (isBase ? ' title="Base FPS/W reference"' : "") +
        ">" +
        metricPair(
          val(r.fps / r.power, 4),
          delta(r.fps / r.power, stock.fps / stock.power),
        ) +
        '</td><td class="' +
        resultClass +
        '" title="' +
        uvText(tip) +
        '">' +
        resultText +
        '</td><td title="' +
        uvText(
          [
            run.kind === "final"
              ? "Final verification"
              : run.kind === "resume"
                ? "Resume verification"
                : "",
            tip,
          ]
            .filter(Boolean)
            .join("\n"),
        ) +
        '">' +
        statusCell(run) +
        "</td></tr>"
      );
    })
    .join("");
}
function refreshScan() {
  shellTools();
  const view = M.querySelector(".uvView"),
    idle = !plan.length && !profileShown;
  if (!view || view.dataset.mode !== (idle ? "idle" : "active")) {
    M.innerHTML = scanView();
    plotVF();
    return;
  }
  document.getElementById("uvStatus").innerHTML = statusBlock();
  document.getElementById("uvTiers").innerHTML = tierStrip();
  const legend = document.getElementById("uvLegend");
  if (legend) legend.innerHTML = curveLegend();
  document.getElementById("uvRunsMeta").textContent = runsMeta();
  const table = document.querySelector(".scanTable"),
    tail =
      table.scrollHeight - table.scrollTop - table.clientHeight < 10,
    top = table.scrollTop,
    active = document.activeElement?.id;
  document.getElementById("uvRuns").innerHTML = runRows();
  document.getElementById("uvRunsEmpty").hidden = runs.length > 0;
  table.scrollTop = tail ? table.scrollHeight : top;
  if (active?.startsWith("run"))
    document.getElementById(active)?.focus({ preventScroll: true });
  plotVF();
}
// Append-only log pane: follows the tail unless the reader scrolled up.
// plotVF runs it with every render pass, so a freshly built pane lands on
// the tail (or the reader's saved position).
function syncLog() {
  const pre = document.getElementById("uvLog");
  if (!pre) return;
  const fresh = !pre.dataset.synced;
  pre.dataset.synced = "1";
  const count = document.getElementById("uvLogCount");
  if (count) count.textContent = logLines.length + (logLines.length === 1 ? " line" : " lines");
  const copy = document.getElementById("copyFullLog");
  if (copy) {
    copy.disabled = !logLines.length;
    copy.title = "Copy all " + logLines.length + " lines";
  }
  const shown = Number(pre.dataset.count || 0);
  if (pre.dataset.epoch !== String(logEpoch) || shown > logLines.length) {
    pre.innerHTML = logHtml(logLines);
  } else if (shown < logLines.length) {
    pre.insertAdjacentHTML("beforeend", logHtml(logLines.slice(shown)));
  }
  pre.dataset.count = logLines.length;
  pre.dataset.epoch = logEpoch;
  if (logFollow) pre.scrollTop = pre.scrollHeight;
  else if (fresh) pre.scrollTop = logScroll;
}
// One block per line, so a wrapped line hangs under its own timestamp.
function logHtml(lines) {
  return lines.map((line) => '<span class="uvLogLine">' + uvText(line) + "</span>").join("");
}
function logScrolled(pre) {
  logFollow = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 8;
  logScroll = pre.scrollTop;
}
async function copyLogs() {
  const lines = [...logLines];
  const status = document.getElementById("copied");
  try {
    await navigator.clipboard.writeText(lines.join("\n"));
    note = "Copied " + lines.length + " lines";
  } catch {
    const pre = document.getElementById("uvLog");
    if (pre) getSelection().selectAllChildren(pre);
    note = "Press Ctrl+C to copy the selected log";
  }
  if (status) status.textContent = note;
  setTimeout(() => {
    note = "";
    const s = document.getElementById("copied");
    if (s) s.textContent = "";
  }, 2400);
}
function chooseRun(i) {
  selected = selected === i ? -1 : i;
  refreshScan();
  document.getElementById("run" + i)?.focus({ preventScroll: true });
}
// Plot/log splitter: drag or use the arrow keys on the sash.
function setSplit(pct) {
  uvSplit = Math.round(Math.max(35, Math.min(80, pct)));
  const split = document.getElementById("uvSplit");
  if (!split) return;
  split.style.setProperty("--split", uvSplit + "%");
  split.querySelector(".uvSash").setAttribute("aria-valuenow", uvSplit);
  plotVF();
}
function sashStart(event) {
  const sash = event.currentTarget,
    box = sash.parentElement.getBoundingClientRect();
  sash.setPointerCapture(event.pointerId);
  const move = (e) => setSplit((100 * (e.clientX - box.left)) / box.width);
  sash.addEventListener("pointermove", move);
  sash.addEventListener(
    "pointerup",
    () => sash.removeEventListener("pointermove", move),
    { once: true },
  );
}
function sashKey(event) {
  const step = { ArrowLeft: -2, ArrowRight: 2 }[event.key];
  if (step) setSplit(uvSplit + step);
  else if (event.key === "Home") setSplit(35);
  else if (event.key === "End") setSplit(80);
  else return;
  event.preventDefault();
}

// Simulated knobs this area adds to the shared prototype controls.
if (typeof appState !== "undefined") {
  appState.simulate.processFailure ??= false;
  appState.simulate.noGpu ??= false;
}
if (typeof prototypeToggles !== "undefined") {
  prototypeToggles.push(
    [
      "processFailure",
      "Fail the next Auto UV or profile verification process",
      () => appState.simulate.processFailure,
    ],
    ["noGpu", "No NVIDIA GPU in Auto UV setup", () => appState.simulate.noGpu],
  );
}
