// Auto UV dialogs and their pure models: setup, final-candidate choice and
// error details. The scan flow that opens them lives in auto-uv.js.
//
// Example NVML facts match scripts/render-auto-uv-setup.py. Targets, clock
// bounds and power defaults use the same RTX 5080 policy as scan_tuning.py.
// The second card has no target table, so its voltage and clock targets stay
// on "Auto" (ui/dialogs/scan_tuning.py special value).
const GPU_FACTS = [
    {
        name: 'NVIDIA GeForce RTX 5080',
        voltage: [725, 1240],
        power: [300, 400],
        stockPower: 360,
        clocksNow: {core: 210, memory: 405},
        memoryClocks: [15001],
        coreSteps: [210, 3150],
        memoryClock: [0, 2000],
        targets: true,
        presets: {
            Efficiency: {mv: 850, clock: 2800, clockRange: [2380, 2800], watts: 300},
            Balanced: {mv: 900, clock: 2800, clockRange: [2800, 2950], watts: 360},
            Performance: {mv: 925, clock: 2950, clockRange: [2800, 3098], watts: 360},
        },
    },
    {
        name: 'NVIDIA GeForce RTX 4070',
        voltage: [700, 1100],
        power: [150, 220],
        stockPower: 200,
        clocksNow: {core: 210, memory: 405},
        memoryClocks: [10501],
        coreSteps: [210, 3105],
        memoryClock: [0, 1500],
        targets: false,
        presets: {
            Efficiency: {mv: null, clock: null, clockRange: [210, 3105], watts: 170},
            Balanced: {mv: null, clock: null, clockRange: [210, 3105], watts: 200},
            Performance: {mv: null, clock: null, clockRange: [210, 3105], watts: 200},
        },
    },
];
let setupGPU = GPU_FACTS[0];
// Spin-box steps from scan_tuning.py: 5 mV, 15 MHz core, 25 MHz memory.
const SETUP_STEPS = {mv: 5, clock: 15, memory: 25, watts: 1};
const SETUP_TOOLTIPS = {
    presets: {
        Efficiency: 'Deepest undervolt: selects the highest measured FPS per watt among stable candidates, with 2 rising tail bins.',
        Balanced: 'Try to maintain baseline clock while lowering the voltage; the tail of the curve goes 2 V/F bins up.',
        Performance: 'Use the 2-bin tail curve, then run the Performance Auto-OC ladder toward the configured voltage and clock targets.',
    },
    mv: 'Voltage target for this tier after the normal voltage sweep. The rising tail can operate above this anchor; it is not a strict voltage limit.',
    clock: 'Clock target for this tier. A lower custom target is tested after the voltage sweep; the two-bin tail adds nominal boost headroom. The scan may choose a lower tested clock if the target is unsafe.',
    memory: 'Optional memory clock offset applied while this profile scans and saved with its final profile. The NVML transfer-rate offset (MT/s) is twice the clock and is shown alongside. Higher values can be unstable.',
    memoryShared: ' In a full scan, Balanced and Performance share this value so Performance can reuse the Balanced downsweep.',
    watts: "Power limit applied to this profile's stock baseline, descent, final verification and saved profile. Efficiency caps below stock; Balanced and Performance keep the stock budget.",
    start: 'Resume an unfinished scan when the GPU and settings match; otherwise start a new scan.',
};
// ui/features/auto_uv defaults: Efficiency 1 min, Balanced 3 min, Performance 5 min.
const FINAL_MINUTES = {Efficiency: 1, Balanced: 3, Performance: 5};
const MAX_FINAL_MINUTES = 60;
const SHORT_PROBE_S = 30;

function uvText(value) {
    return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
}

// ui/components/scan_controls.py _format_duration_compact.
function fmtDuration(seconds) {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    if (total < 60) return total + 's';
    const minutes = Math.floor(total / 60), rest = total % 60;
    if (minutes < 60) return rest ? `${minutes}min ${rest}s` : `${minutes}min`;
    const hours = Math.floor(minutes / 60), mins = minutes % 60;
    return mins ? `${hours}h ${mins}min` : `${hours}h`;
}

function gpuFacts(index) {
    return GPU_FACTS[index] || GPU_FACTS[0];
}

function buildInitialSetup(gpu = 0) {
    const facts = gpuFacts(gpu);
    const perTier = {};
    const edited = {};
    for (const [tier, preset] of Object.entries(facts.presets)) {
        perTier[tier] = {mv: preset.mv, clock: preset.clock, watts: preset.watts, memory: 0};
        edited[tier] = {};
    }
    return {scope: 'Full scan', tier: 'Balanced', gpu, settings: perTier, edited};
}

function defaultVerificationMinutes(tier) {
    return FINAL_MINUTES[tier] ?? 5; // ui.constants.DEFAULT_FINAL_VERIFICATION_DURATION_S / 60
}

// Untouched defaults are sent as automatic, never as hard targets.
function requestedSettings(setup) {
    const out = {};
    for (const [tier, values] of Object.entries(setup.settings)) {
        out[tier] = {};
        for (const [key, value] of Object.entries(values)) {
            const auto = (key === 'mv' || key === 'clock') && (!setup.edited?.[tier]?.[key] || value == null);
            out[tier][key] = auto ? 'auto' : value;
        }
    }
    return out;
}

function openSetup() {
    if (scanBusy()) return;
    ensureDaemonReady(() => {
        draft = structuredClone(settings);
        draft.gpu = Math.min(appState.selectedGpu, appState.gpus.length - 1);
        if (draft.gpu !== settings.gpu) regearSetup(draft.gpu);
        setupGPU = gpuFacts(draft.gpu);
        setupDialog();
    }, 'Setting up Auto-UV');
}

function uvDialog(titleText, subtitle, contents, footer, focusId) {
    const panel = dlg();
    const wasOpen = panel.open;
    const priorScroll = wasOpen ? panel.querySelector('.uvBody')?.scrollTop || 0 : 0;
    panel.setAttribute('aria-labelledby', 'uvTitle');
    showPanel(`<div class="uvDialog">
        <header class="uvHeader"><h2 id="uvTitle" tabindex="-1">${titleText}</h2>
        <p>${subtitle}</p></header><div class="uvBody">${contents}</div>
        <footer class="uvFooter">${footer}</footer></div>`);
    if (!wasOpen) {
        panel.addEventListener('close', () => {
            if (!panel.open) panel.removeAttribute('aria-labelledby');
        }, {once: true});
    }
    const focus = document.getElementById(focusId || 'uvTitle');
    if (focus) focus.focus({preventScroll:true});
    const body = panel.querySelector('.uvBody');
    if (body) body.scrollTop = priorScroll;
}

function setupRanges(tier) {
    return {mv: setupGPU.voltage, clock: setupGPU.presets[tier].clockRange,
        memory: setupGPU.memoryClock, watts: setupGPU.power};
}

function uvNumber(key, label, unit, help) {
    const [lo, hi] = setupRanges(draft.tier)[key];
    const value = draft.settings[draft.tier][key];
    const auto = (key === 'mv' || key === 'clock') && !setupGPU.targets;
    const range = key === 'watts'
        ? `<input type="range" id="uvPowerRange" min="${lo}" max="${hi}" value="${value}"
            aria-label="Power limit slider" oninput="changePowerRange(this.value)">`
        : '';
    const tooltip = SETUP_TOOLTIPS[key] + (key === 'memory' && draft.scope === 'Full scan' && draft.tier !== 'Efficiency' ? SETUP_TOOLTIPS.memoryShared : '');
    return `<div class="uvField"><div><label for="uv-${key}" title="${uvText(tooltip)}">${label}</label>
        <p id="hint-${key}">${help}</p></div><div class="uvInputGroup">${range}
        <div class="uvNumber"><input type="number" id="uv-${key}" name="${key}"
            min="${lo}" max="${hi}" step="${SETUP_STEPS[key]}" value="${value ?? ''}" ${auto ? 'placeholder="Auto"' : 'required'}
            title="${uvText(tooltip)}" aria-describedby="hint-${key}" oninput="setAdvanced('${key}',this.value)">
        <span>${unit}</span></div></div></div>`;
}

function pickButtons(values, current, action, groupLabel, withEstimates = false, tooltips = {}) {
    return `<div class="uvPicks" role="group" aria-label="${groupLabel}">` +
        values.map((v, i) => `<button type="button" id="${action}-${i}"
            aria-pressed="${v === current}" onclick="${action}('${v}')"${tooltips[v] ? ` title="${uvText(tooltips[v])}"` : ''}>
            ${v}${withEstimates ? `<small>${est(v.toLowerCase())}</small>` : ''}
        </button>`).join('') + '</div>';
}

// ui/features/tuning/tuning.py auto_uv_nvml_info_text, one row per fact.
function nvmlRows(facts) {
    const [lo, hi] = facts.power;
    return [
        ['Power limit', `current ${facts.stockPower} W · default ${facts.stockPower} W · range ${lo}–${hi} W`],
        ['Clocks now', `core ${facts.clocksNow.core} MHz · memory ${facts.clocksNow.memory} MHz`],
        ['Supported memory clocks', facts.memoryClocks.join(', ') + ' MHz'],
        ['Supported core range', `${facts.coreSteps[0]}–${facts.coreSteps.at(-1)} MHz (${facts.coreSteps.length} steps)`],
        ['Power management', 'enabled'],
        ['Fixed power-limit writes', 'supported'],
    ];
}

function setupGpuSection(noGpu) {
    if (noGpu) {
        return `<section class="uvGroup"><h3>Graphics card</h3><div class="uvGPU uvNoGpu" role="alert">
            <b>No NVIDIA GPU detected.</b><span>Close this dialog, check the GPU driver and PenguinBurner hardware service, then open Setup Auto Undervolt again.</span></div></section>`;
    }
    const facts = setupGPU;
    const picker = appState.gpus.length > 1
        ? `<label class="uvGpuPick"><select id="uvGpu" aria-label="Graphics card" onchange="changeSetupGpu(Number(this.value))">${
            appState.gpus.map((g, i) => `<option value="${i}"${i === draft.gpu ? ' selected' : ''}>GPU ${i} · ${uvText(gpuFacts(i).name)}</option>`).join('')
        }</select></label>`
        : `<b>GPU ${draft.gpu} · ${uvText(facts.name)}</b>`;
    return `<section class="uvGroup"><h3>Graphics card</h3><div class="uvGPU">${picker}
        <span>Board power ${facts.power[0]}–${facts.power[1]} W · stock ${facts.stockPower} W</span></div>
        <details class="uvLimits"><summary>GPU limits</summary><dl>${
            nvmlRows(facts).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')
        }<dt>Voltage target range</dt><dd>${facts.voltage[0]}–${facts.voltage[1]} mV</dd>
        <dt>Memory transfer offset</dt><dd>0–${facts.memoryClock[1] * 2} MT/s</dd></dl></details></section>`;
}

function setupDialog(focusId) {
    const noGpu = !!appState.simulate.noGpu;
    const full = draft.scope === 'Full scan';
    const tier = draft.tier;
    const preset = setupGPU.presets[tier];
    const targetHelp = (key, unit, range) => setupGPU.targets
        ? `${range} ${unit} · default ${preset[key]} ${unit}.`
        : `Auto · this GPU has no target table. Type a value to set one.`;
    const fields = uvNumber('mv', 'Voltage target', 'mV',
        targetHelp('mv', 'mV', setupGPU.voltage.join('–')) + (setupGPU.targets ? ' The curve can rise above this target.' : ''))
        + uvNumber('clock', 'Core clock target', 'MHz', targetHelp('clock', 'MHz', preset.clockRange.join('–')))
        + `<p class="uvCaution">${uvText(design.copy.targetCaution)} Defaults suit most GPUs.</p>`
        + uvNumber('memory', 'Memory clock offset', 'MHz',
            `0–${setupGPU.memoryClock[1]} MHz · <output id="uvMemoryRate">${(draft.settings[tier].memory || 0) * 2} MT/s</output>` +
            (full && tier !== 'Efficiency' ? ' · Shared by Balanced and Performance.' : ''))
        + uvNumber('watts', 'Power limit', 'W', `${setupGPU.power.join('–')} W · preset ${preset.watts} W.`);
    uvDialog('Setup Auto Undervolt', design.copy.setupNote,
        setupGpuSection(noGpu) +
        `<fieldset class="uvGroups"${noGpu ? ' disabled' : ''}>
        <section class="uvGroup"><h3>Scan scope</h3>
        ${pickButtons(['Full scan', 'Selected profile'], draft.scope, 'setScope', 'Scan scope')}
        <p class="uvNote">${full ? 'Scan Efficiency → Balanced → Performance. Each profile is verified and saved before the next starts.' : 'Scan only the selected preset.'}</p>
        </section><section class="uvGroup"><h3 id="uvPresetHeading">Auto-UV preset</h3>
        ${pickButtons(tiers, tier, 'setSetupTier', 'Auto-UV preset', true, SETUP_TOOLTIPS.presets)}
        <p class="uvNote">${full ? 'Choose a preset to edit its settings.' : 'Choose the tier to scan.'}</p>
        </section><section class="uvGroup"><h3>${tier} settings</h3>
        <form id="uvSettings" onsubmit="event.preventDefault()">${fields}</form></section></fieldset>`,
        `<div><strong>${est(full ? 'adaptive' : tier.toLowerCase())}</strong><small>Scan estimate · final verification is additional</small></div>
        <div class="uvActions"><button type="button" onclick="closePanel()">Cancel</button>
        <button type="button" class="start" id="uvStart" title="${uvText(SETUP_TOOLTIPS.start)}" ${noGpu ? 'disabled' : ''} onclick="acceptSetup()">Start Auto Undervolt</button></div>`, focusId);
}

function setupValid() {
    const form = document.getElementById('uvSettings');
    return !form || form.reportValidity();
}

function setScope(value) {
    if (!setupValid()) return;
    draft.scope = value;
    if (value === 'Full scan') draft.settings.Performance.memory = draft.settings.Balanced.memory;
    setupDialog('setScope-' + (value === 'Full scan' ? 0 : 1));
}

function setSetupTier(tier) {
    if (!setupValid()) return;
    draft.tier = tier;
    setupDialog('setSetupTier-' + tiers.indexOf(tier));
}

function clamp(value, [lo, hi]) {
    return Math.max(lo, Math.min(hi, value));
}

// Re-read the selected card's limits: untouched fields take its defaults,
// edited ones keep their value inside the new range.
function regearSetup(gpu) {
    const facts = gpuFacts(gpu);
    draft.gpu = gpu;
    draft.edited ??= {};
    for (const tier of tiers) {
        const preset = facts.presets[tier];
        const values = draft.settings[tier];
        const edited = draft.edited[tier] ??= {};
        const ranges = {mv: facts.voltage, clock: preset.clockRange, memory: facts.memoryClock, watts: facts.power};
        for (const key of ['mv', 'clock', 'watts', 'memory']) {
            if (!edited[key]) values[key] = key === 'memory' ? 0 : preset[key];
            else if (values[key] != null) values[key] = clamp(values[key], ranges[key]);
        }
    }
}

function changeSetupGpu(gpu) {
    regearSetup(gpu);
    setupGPU = gpuFacts(gpu);
    setupDialog('uvGpu');
}

function setAdvanced(key, value) {
    // Keep invalid intermediate edits in the input, never in the scan configuration.
    const input = document.getElementById('uv-' + key);
    if (value === '' && (key === 'mv' || key === 'clock') && !setupGPU.targets) {
        draft.settings[draft.tier][key] = null;
        if (draft.edited?.[draft.tier]) draft.edited[draft.tier][key] = false;
        return;
    }
    if (!input.validity.valid || value === '') return;
    const number = Number(value);
    draft.settings[draft.tier][key] = number;
    draft.edited ??= {};
    (draft.edited[draft.tier] ??= {})[key] = true;
    if (key === 'watts') {
        const range = document.getElementById('uvPowerRange');
        range.value = value;
        // A typed value moves the thumb; keep the filled track with it.
        if (typeof syncRangeFill === 'function') syncRangeFill(range);
    }
    if (key !== 'memory') return;
    document.getElementById('uvMemoryRate').textContent = (number * 2) + ' MT/s';
    if (draft.scope === 'Full scan' && draft.tier !== 'Efficiency') {
        const other = draft.tier === 'Balanced' ? 'Performance' : 'Balanced';
        draft.settings[other].memory = number;
        (draft.edited[other] ??= {}).memory = true;
    }
}

function changePowerRange(value) {
    document.getElementById('uv-watts').value = value;
    setAdvanced('watts', value);
}

function acceptSetup() {
    if (appState.simulate.noGpu || !setupValid()) return;
    appState.selectedGpu = draft.gpu;
    beginScan();
}

// ---------- Final candidate model (ui/dialogs/final_choice.py) ----------

function tierCandidates(tier) {
    return plan.slice(0, completed).map((row, index) => ({row, index}))
        .filter(({row}) => row.tier === tier && row.passed)
        .sort((a, b) => tier === 'Performance'
            ? b.row.fps - a.row.fps
            : (b.row.fps / b.row.power) - (a.row.fps / a.row.power));
}

function suggestedCandidate(candidates) {
    if (!candidates.length) return -1;
    // Full-scan backend supplies the tier's confirmed point; single-tier
    // ranking uses highest FPS/W or highest FPS for Performance.
    return settings.scope === 'Full scan'
        ? Math.max(...candidates.map(c => c.index)) : candidates[0].index;
}

// Performance candidates within half a percent are compared twice each
// (PR #137 performance-comparison request).
function comparisonPair(tier) {
    if (tier !== 'Performance') return null;
    const [first, second] = tierCandidates(tier);
    if (!first || !second) return null;
    return Math.abs(first.row.fps / second.row.fps - 1) < 0.005 ? [first.index, second.index] : null;
}

function candidateRecord(index, extra = {}) {
    const row = plan[index];
    return {
        id: index, mv: row.mv, target: row.target, oc: Math.round(row.target - stock.meas),
        meas: row.meas, fps: row.fps, power: row.power, fpsw: row.fps / row.power,
        shortS: SHORT_PROBE_S, comparisonRuns: 0, finalVerified: false, ...extra,
    };
}

function candidateStatus(record, isDefault, tier, reason) {
    const parts = [];
    if (isDefault) {
        if (reason === 'previous-crash') parts.push('Resume from here');
        else if (reason === 'final-verification-failed') parts.push('Next safer pick');
        else if (reason === 'performance-comparison') parts.push('Comparison winner');
        else if (reason.startsWith('adaptive-')) parts.push('Tier suggestion');
        else parts.push(tier === 'Performance' ? 'Highest FPS' : 'Best FPS/W');
    }
    if (record.comparisonRuns) parts.push('Mean of 2 matched runs');
    parts.push(record.finalVerified ? 'Final stability verified' : 'Passed short probe');
    return parts.join(' · ');
}

function choiceTitle(request) {
    const reason = request.reason;
    if (reason.startsWith('adaptive-')) return `Tune ${request.tier} profile candidate`;
    if (reason === 'previous-crash') return 'Resume Auto-UV Candidate';
    if (reason === 'final-verification-failed') return 'Choose Safer Auto-UV Candidate';
    return 'Choose Final verification candidate';
}

function decisionText(decision) {
    if (!decision) return '';
    return `${decision.mv}mV@${decision.target}MHz - ${decision.text}`;
}

function choiceIntro(request) {
    const reason = request.reason;
    const metric = request.tier === 'Performance' ? 'highest FPS' : 'best FPS/W';
    if (reason === 'performance-comparison') {
        return 'Two close Performance candidates were compared twice each with equal 30s Q2RTX + 5s CUDA runs. Their FPS, clock and power show the two-run means. The comparison winner is selected; other rows retain their earlier short probes. Long Final verification is still required.';
    }
    if (reason.startsWith('adaptive-')) {
        return "The adaptive sweep is complete. This tier's confirmed candidate is selected for the long Final verification; pick another passed candidate to tune this tier's profile.";
    }
    if (reason === 'previous-crash') {
        const detail = decisionText(request.decision);
        return 'A previous Auto-UV run ended during a GPU probe. Choose a saved candidate to resume from a safer voltage bin, or start a new scan from scratch.' + (detail ? ` Previous decision: ${detail}` : '');
    }
    if (reason === 'final-verification-failed') {
        const detail = decisionText(request.decision);
        return `Final verification failed. Choose another passed short-probe candidate at a safer voltage, or start a new scan from scratch. The ${metric} remaining candidate is selected by default.` + (detail ? ` Failed candidate: ${detail}.` : '');
    }
    if (reason === 'user-stop') {
        return `Auto-UV was stopped. Choose one of the previously stable candidates. The ${metric} candidate is selected for the long Final verification.`;
    }
    return `The short candidate sweep is complete. The ${metric} passed candidate is selected for the long Final verification.`;
}

// FINAL_CHOICE_COLUMNS minus the radio. Sortable: OC, Effective, FPS/W, FPS, Power.
const CHOICE_COLUMNS = [
    {key: 'mv', label: 'mV'},
    {key: 'target', label: 'Target MHz'},
    {key: 'oc', label: 'OC MHz', sort: 'desc', performanceOnly: true},
    {key: 'meas', label: 'Effective MHz', sort: 'desc'},
    {key: 'fpsw', label: 'FPS/W', sort: 'desc'},
    {key: 'fps', label: 'FPS', sort: 'desc'},
    {key: 'power', label: 'Power W', sort: 'asc'},
    {key: 'shortS', label: 'Short Check'},
    {key: 'status', label: 'Status'},
];

function defaultChoiceSort(request) {
    if (request.reason === 'previous-crash') return null;
    return {key: request.tier === 'Performance' ? 'fps' : 'fpsw', desc: true};
}

function sortedRecords(records, sort) {
    if (!sort) return records.slice();
    const sign = sort.desc ? -1 : 1;
    return records.slice().sort((a, b) => sign * (a[sort.key] - b[sort.key]) || a.mv - b.mv);
}

function bestRecord(records, tier) {
    const key = tier === 'Performance' ? 'fps' : 'fpsw';
    return records.reduce((best, r) => (!best || r[key] > best[key] ? r : best), null);
}

// Build the dialog model the backend's final_choice_request would carry.
function choiceModel(request) {
    const records = request.records.map(r => ({...r}));
    const honors = request.reason === 'previous-crash' || request.reason.startsWith('adaptive-')
        || request.reason === 'performance-comparison';
    const requested = records.find(r => r.id === request.defaultId);
    const fallback = bestRecord(records, request.tier);
    const defaultId = (honors && requested ? requested : fallback)?.id ?? -1;
    for (const r of records) r.status = candidateStatus(r, r.id === defaultId, request.tier, request.reason);
    const recovery = request.reason === 'previous-crash' || request.reason === 'final-verification-failed';
    return {
        title: choiceTitle(request), intro: choiceIntro(request), records, defaultId, recovery,
        showOc: request.tier === 'Performance',
        minutes: Math.min(MAX_FINAL_MINUTES, request.minutes ?? defaultVerificationMinutes(request.tier)),
        discardLabel: recovery ? 'Start From Scratch' : 'Discard',
        acceptLabel: request.reason === 'final-verification-failed' ? 'Verify Selected'
            : request.reason === 'previous-crash' ? 'Resume Selected' : 'Use Selected',
        rejectAction: recovery ? 'abort' : 'discard',
    };
}

let choiceState = null; // {request, model, pick, minutes, sort}

function openCandidateDialog(request) {
    const model = choiceModel(request);
    choiceState = {request, model, pick: model.defaultId, minutes: model.minutes, sort: defaultChoiceSort(request)};
    dlg().oncancel = event => {
        // Escape is the dialog's reject: Discard, or Abort for recovery prompts.
        event.preventDefault();
        resolveChoice(choiceState?.model.rejectAction || 'discard');
    };
    candidateDialog();
}

function deltaPct(value, base, lowerBetter = false) {
    const pct = (value / base - 1) * 100;
    const tone = Math.abs(pct) < 1 ? 'flat' : (lowerBetter ? -pct : pct) > 0 ? 'gain' : 'loss';
    return `<small class="delta ${tone}" title="Change from measured stock baseline">${pct > 0 ? '+' : ''}${pct.toFixed(1)}%</small>`;
}

function choiceCell(record, column) {
    switch (column.key) {
    case 'mv': return `${record.mv}`;
    case 'target': return `${record.target}`;
    case 'oc': return `<span title="Target clock delta from measured baseline">${record.oc > 0 ? '+' : ''}${record.oc}</span>`;
    case 'meas': return record.meas.toFixed(2);
    case 'fpsw': return `<span class="metricpair"><span class="reading">${record.fpsw.toFixed(4)}</span>${deltaPct(record.fpsw, stock.fps / stock.power)}</span>`;
    case 'fps': return `<span class="metricpair"><span class="reading">${record.fps.toFixed(2)}</span>${deltaPct(record.fps, stock.fps)}</span>`;
    case 'power': return `<span class="metricpair"><span class="reading">${record.power.toFixed(2)}</span>${deltaPct(record.power, stock.power, true)}</span>`;
    case 'shortS': return record.comparisonRuns ? `${record.comparisonRuns} × 35s` : fmtDuration(record.shortS);
    default: return uvText(record.status);
    }
}

function candidateDialog(focusId) {
    const {request, model, sort} = choiceState;
    const columns = CHOICE_COLUMNS.filter(c => !c.performanceOnly || model.showOc);
    const rows = sortedRecords(model.records, sort);
    const head = columns.map(c => {
        const active = sort?.key === c.key;
        const aria = active ? ` aria-sort="${sort.desc ? 'descending' : 'ascending'}"` : '';
        const label = c.sort
            ? `<button type="button" id="uvSort-${c.key}" onclick="sortChoice('${c.key}')">${c.label}<span class="uvSortArrow" aria-hidden="true">${active && !sort.desc ? '↑' : '↓'}</span></button>`
            : c.label;
        return `<th scope="col" class="col-${c.key}${c.sort ? ' sortable' : ''}${active ? ' sorted' : ''}"${aria}>${label}</th>`;
    }).join('');
    const body = rows.map(r => `<tr class="${r.id === choiceState.pick ? 'picked' : ''}" ondblclick="pickChoice(${r.id});acceptChoice()"
        onclick="if(!event.target.closest('input')) pickChoice(${r.id})">
        <td class="uvPick"><input type="radio" name="uvCandidate" id="uvCand-${r.id}" value="${r.id}" aria-label="${r.mv} mV at ${r.target} MHz"
            ${r.id === choiceState.pick ? 'checked' : ''} onchange="pickChoice(${r.id})"></td>
        ${columns.map(c => `<td data-label="${c.label}" class="col-${c.key}">${choiceCell(r, c)}</td>`).join('')}</tr>`).join('');
    const duration = model.recovery ? '' : `<div class="uvField uvDuration"><div><label for="uvDuration">Final verification duration</label>
        <p>1–${MAX_FINAL_MINUTES} min · ${request.tier} default ${defaultVerificationMinutes(request.tier)} min</p></div>
        <div class="uvNumber"><input type="number" id="uvDuration" min="1" max="${MAX_FINAL_MINUTES}" required step="1" value="${choiceState.minutes}"
            oninput="setChoiceDuration(this)"><span>min</span></div></div>`;
    uvDialog(model.title, `GPU ${settings.gpu ?? 0} · ${uvText(gpuFacts(settings.gpu ?? 0).name)} · ${request.tier}`,
        `<p class="uvIntro">${uvText(model.intro)}</p>
        <div class="uvCandidates" tabindex="0" role="region" aria-label="${request.tier} passing candidates">
        <table><thead><tr><th scope="col" class="uvPick"><span class="visually-hidden">Pick</span></th>${head}</tr></thead>
        <tbody>${body}</tbody></table></div>${duration}`,
        `<div class="uvActions"><button type="button" class="danger" id="uvDiscard" onclick="resolveChoice('discard')">${model.discardLabel}</button>${
            model.recovery ? '<button type="button" id="uvAbort" onclick="resolveChoice(\'abort\')">Abort</button>' : ''}</div>
        <div class="uvActions"><button type="button" class="start" id="uvAccept" onclick="acceptChoice()">${model.acceptLabel}</button></div>`,
        focusId);
}

function sortChoice(key) {
    const column = CHOICE_COLUMNS.find(c => c.key === key);
    if (!column?.sort) return;
    const sort = choiceState.sort;
    choiceState.sort = sort?.key === key ? {key, desc: !sort.desc} : {key, desc: column.sort === 'desc'};
    candidateDialog('uvSort-' + key);
}

function pickChoice(id) {
    if (!choiceState) return;
    choiceState.pick = id;
    for (const row of document.querySelectorAll('.uvCandidates tbody tr')) {
        const input = row.querySelector('input');
        const on = Number(input.value) === id;
        input.checked = on;
        row.classList.toggle('picked', on);
    }
}

function setChoiceDuration(input) {
    if (!input.validity.valid || input.value === '') return;
    choiceState.minutes = Number(input.value);
}

function acceptChoice() {
    const field = document.getElementById('uvDuration');
    if (field && !field.reportValidity()) return;
    resolveChoice('select');
}

// The dialog's three outcomes, as select_final_candidate returns them.
function resolveChoice(action, id = choiceState?.pick, minutes = choiceState?.minutes) {
    if (!choiceState) return;
    const {request, model} = choiceState;
    choiceState = null;
    const panel = dlg();
    panel.oncancel = null;
    closePanel();
    const record = model.records.find(r => r.id === id);
    handleChoice(request, action === 'select' && record ? 'select' : action === 'select' ? 'discard' : action,
        record?.id ?? -1, Math.max(1, Math.min(MAX_FINAL_MINUTES, Number(minutes) || model.minutes)));
}

// ---------- Error details (ui/dialogs/error_details.py process_failure_details) ----------

function processFailureDetails(action, exitCode, exitStatus, logTail) {
    const lines = [`Action: ${action}`, `Exit code: ${exitCode}`, `Exit status: ${exitStatus}`];
    if (logTail) lines.push('', 'Recent logs:', logTail);
    return lines.join('\n');
}

// The dialog itself is the shared showErrorDetails(title, message, details)
// global error reporter (guide.js).

// ---------- Verified profiles ----------

// gpu: the scanned GPU (the native Qt scan passes its own setup's index).
function saveVerifiedCandidate(tier, row, memoryClockMHz, durationMinutes, gpu = settings.gpu ?? appState.selectedGpu) {
    const id = profiles.length;
    profiles.push([tier, row.mv, row.target, row.fps, row.power, row.fps / row.power]);
    profileMeta.push({
        tier, origin: tiers.indexOf(tier), valid: true, source: 'Auto UV', fromVerification: true,
        mem: Number(memoryClockMHz), durationMinutes, meas: row.meas, vf: structuredClone(row.points),
        date: 'Just now', deleted: false, gpu: Number(gpu ?? 0),
    });
    order.unshift(id);
    if (!verified.includes(tier)) verified.push(tier);
    return id;
}
