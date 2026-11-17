// Example NVML facts match scripts/render-auto-uv-setup.py. Targets, clock
// bounds and power defaults use the same RTX 5080 policy as scan_tuning.py.
const setupGPU = {
    name: 'NVIDIA GeForce RTX 5080',
    voltage: [725, 1240],
    power: [300, 400],
    stockPower: 360,
    memoryClock: [0, 2000],
    presets: {
        Efficiency: {mv: 850, clock: 2800, clockRange: [2380, 2800], watts: 300},
        Balanced: {mv: 900, clock: 2800, clockRange: [2800, 2950], watts: 360},
        Performance: {mv: 925, clock: 2950, clockRange: [2800, 3098], watts: 360},
    },
};

function buildInitialSetup() {
    const perTier = {};
    for (const [tier, preset] of Object.entries(setupGPU.presets)) {
        perTier[tier] = {mv: preset.mv, clock: preset.clock, watts: preset.watts, memory: 0};
    }
    return {scope: 'Full scan', tier: 'Balanced', settings: perTier};
}

function defaultVerificationMinutes() {
    return 5; // ui.constants.DEFAULT_FINAL_VERIFICATION_DURATION_S / 60
}

function openSetup() {
    draft = structuredClone(settings);
    setupDialog();
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
    panel.querySelector('.uvBody').scrollTop = priorScroll;
}

function setupRanges(tier) {
    return {mv: setupGPU.voltage, clock: setupGPU.presets[tier].clockRange,
        memory: setupGPU.memoryClock, watts: setupGPU.power};
}

function uvNumber(key, label, unit, help) {
    const [lo, hi] = setupRanges(draft.tier)[key];
    const value = draft.settings[draft.tier][key];
    const range = key === 'watts'
        ? `<input type="range" id="uvPowerRange" min="${lo}" max="${hi}" value="${value}"
            aria-label="Power limit slider" oninput="changePowerRange(this.value)">`
        : '';
    return `<div class="uvField"><div><label for="uv-${key}">${label}</label>
        <p id="hint-${key}">${help}</p></div><div class="uvInputGroup">${range}
        <div class="uvNumber"><input type="number" id="uv-${key}" name="${key}"
            min="${lo}" max="${hi}" step="1" value="${value}" required
            aria-describedby="hint-${key}" oninput="setAdvanced('${key}',this.value)">
        <span>${unit}</span></div></div></div>`;
}

function pickButtons(values, current, action, groupLabel, withEstimates = false) {
    return `<div class="uvPicks" role="group" aria-label="${groupLabel}">` +
        values.map((v, i) => `<button type="button" id="${action}-${i}"
            aria-pressed="${v === current}" onclick="${action}('${v}')">
            ${v}${withEstimates ? `<small>${est(v.toLowerCase())}</small>` : ''}
        </button>`).join('') + '</div>';
}

function setupDialog(focusId) {
    const full = draft.scope === 'Full scan';
    const tier = draft.tier;
    const preset = setupGPU.presets[tier];
    const fields = uvNumber('mv', 'Voltage target', 'mV',
        `725–1240 mV · default ${preset.mv} mV. The curve can rise above this target.`)
        + uvNumber('clock', 'Core clock target', 'MHz',
            `${preset.clockRange.join('–')} MHz · default ${preset.clock} MHz.`)
        + uvNumber('memory', 'Memory clock offset', 'MHz',
            `0–2000 MHz · <output id="uvMemoryRate">${draft.settings[tier].memory * 2} MT/s</output>` +
            (full && tier !== 'Efficiency' ? ' · Shared by Balanced and Performance.' : ''))
        + uvNumber('watts', 'Power limit', 'W', `300–400 W · preset ${preset.watts} W.`);
    uvDialog('Setup Auto Undervolt', 'Configure the scan for the selected graphics card.',
        `<section class="uvGroup"><h3>Graphics card</h3><div class="uvGPU">
        <b>GPU 0 · ${setupGPU.name}</b>
        <span>Board power 300–400 W · stock 360 W</span></div>
        <details class="uvLimits"><summary>GPU limits</summary>
        <dl><dt>Graphics clock</dt><dd>210–3150 MHz</dd>
        <dt>Supported memory clock</dt><dd>15,001 MHz</dd>
        <dt>Voltage target range</dt><dd>725–1240 mV</dd>
        <dt>Memory transfer offset</dt><dd>0–4000 MT/s</dd></dl></details></section>
        <section class="uvGroup"><h3>Scan scope</h3>
        ${pickButtons(['Full scan', 'Selected profile'], draft.scope, 'setScope', 'Scan scope')}
        <p class="uvNote">${full ? 'Scan Efficiency → Balanced → Performance.' : 'Scan only the selected preset.'}</p>
        </section><section class="uvGroup"><h3 id="uvPresetHeading">Auto-UV preset</h3>
        ${pickButtons(tiers, tier, 'setSetupTier', 'Auto-UV preset', true)}
        <p class="uvNote">${full ? 'Choose a preset to edit its settings.' : 'Choose the tier to scan.'}</p>
        </section><section class="uvGroup"><h3>${tier} settings</h3>
        <form id="uvSettings" onsubmit="event.preventDefault()">${fields}</form></section>`,
        `<div><strong>${est(full ? 'adaptive' : tier.toLowerCase())}</strong><small>Scan estimate · final verification is additional</small></div>
        <div class="uvActions"><button type="button" onclick="closePanel()">Cancel</button>
        <button type="button" class="start" onclick="acceptSetup()">Start Auto Undervolt</button></div>`, focusId);
}

function setScope(value) {
    if (!document.getElementById('uvSettings').reportValidity()) return;
    draft.scope = value;
    if (value === 'Full scan') draft.settings.Performance.memory = draft.settings.Balanced.memory;
    setupDialog('setScope-' + (value === 'Full scan' ? 0 : 1));
}

function setSetupTier(tier) {
    if (!document.getElementById('uvSettings').reportValidity()) return;
    draft.tier = tier;
    setupDialog('setSetupTier-' + tiers.indexOf(tier));
}

function setAdvanced(key, value) {
    // Keep invalid intermediate edits in the input, never in the scan configuration.
    const input = document.getElementById('uv-' + key);
    if (!input.validity.valid || value === '') return;
    const number = Number(value);
    draft.settings[draft.tier][key] = number;
    if (key === 'watts') document.getElementById('uvPowerRange').value = value;
    if (key !== 'memory') return;
    document.getElementById('uvMemoryRate').textContent = (number * 2) + ' MT/s';
    if (draft.scope === 'Full scan' && draft.tier !== 'Efficiency') {
        draft.settings[draft.tier === 'Balanced' ? 'Performance' : 'Balanced'].memory = number;
    }
}

function changePowerRange(value) {
    document.getElementById('uv-watts').value = value;
    setAdvanced('watts', value);
}

function acceptSetup() {
    if (document.getElementById('uvSettings').reportValidity()) beginScan();
}

let proposedVerification = {};
let runningVerification = {};
let candidateTier = '';
let verificationPlan = null;
let choicesSaved = false;

function tierCandidates(tier) {
    return plan.slice(0, completed).map((row, index) => ({row, index}))
        .filter(({row}) => row.tier === tier && row.passed)
        .sort((a, b) => tier === 'Performance'
            ? b.row.fps - a.row.fps
            : (b.row.fps / b.row.power) - (a.row.fps / a.row.power));
}

function suggestedCandidate(candidates) {
    if (!candidates.length) return -1;
    // Full-scan backend supplies the final confirmed point; single-tier
    // ranking uses highest FPS/W or highest FPS for Performance.
    return settings.scope === 'Full scan'
        ? Math.max(...candidates.map(c => c.index)) : candidates[0].index;
}

function verifySetup() {
    const availableTiers = activeTiers().filter(tier => tierCandidates(tier).length);
    if (!availableTiers.length) return;
    proposedVerification = {};
    for (const tier of availableTiers) {
        const prior = verificationPlan === plan ? runningVerification[tier] : null;
        proposedVerification[tier] = prior ? {...prior} : {
            index: suggestedCandidate(tierCandidates(tier)), minutes: defaultVerificationMinutes(),
        };
    }
    candidateTier = availableTiers[0];
    verificationDialog();
}

function candidateOption(option, pick, suggestion) {
    const r = option.row;
    return `<tr onclick="if(!event.target.closest('label,input')) this.querySelector('input').click()"><th scope="row"><label><input type="radio" name="uvCandidate"
        value="${option.index}" ${pick.index === option.index ? 'checked' : ''}
        onchange="proposedVerification[candidateTier].index=Number(this.value)">
        <span>${r.mv} mV${option.index === suggestion ? '<small>Suggested</small>' : ''}</span></label></th>
        <td data-label="Target MHz">${r.target}</td><td data-label="Effective MHz">${Math.round(r.meas)}</td><td data-label="FPS">${r.fps.toFixed(2)}</td>
        <td data-label="Power W">${r.power.toFixed(1)}</td><td data-label="FPS/W">${(r.fps / r.power).toFixed(4)}</td></tr>`;
}

function verificationDialog(focusId) {
    const tier = candidateTier;
    const pick = proposedVerification[tier];
    const candidates = tierCandidates(tier);
    const suggestion = suggestedCandidate(candidates);
    const included = Object.keys(proposedVerification);
    const selector = included.length > 1 ? pickButtons(included, tier, 'chooseCandidateTier', 'Tier to configure') : '';
    uvDialog('Final verification', `GPU 0 · ${setupGPU.name}`,
        `<section class="uvGroup"><h3>${included.length > 1 ? 'Profiles to verify' : 'Profile to verify'}</h3>${selector || '<strong>' + tier + '</strong>'}
        <p class="uvNote">Choose a passing candidate and verification duration for each tier.</p></section>
        <section class="uvGroup"><h3>${tier} candidates</h3>
        <div class="uvCandidates" tabindex="0" role="region" aria-label="${tier} passing candidates">
        <table><thead><tr><th scope="col">Voltage</th><th scope="col">Target MHz</th><th scope="col">Effective MHz</th>
        <th scope="col">FPS</th><th scope="col">Power W</th><th scope="col">FPS/W</th></tr></thead>
        <tbody>${candidates.map(c => candidateOption(c, pick, suggestion)).join('')}</tbody></table></div>
        <div class="uvField"><div><label for="uvDuration">${tier} duration</label>
        <p>1–60 minutes per profile</p></div><div class="uvNumber">
        <input type="number" id="uvDuration" min="1" max="60" required step="1" value="${pick.minutes}"
            oninput="setCandidateDuration(this)"><span>min</span></div></div></section>`,
        `<div><strong id="uvTotalTime">${verificationTimeText()}</strong><small>Final verification · accelerated playback</small></div>
        <div class="uvActions"><button type="button" onclick="closePanel()">Cancel</button>
        <button type="button" class="start" onclick="startVerify()">Start verification</button></div>`, focusId);
}

function verificationTimeText() {
    const choices = Object.values(proposedVerification);
    const total = choices.reduce((sum, choice) => sum + choice.minutes, 0);
    return total + ' min total · ' + choices.length + (choices.length === 1 ? ' profile' : ' profiles');
}

function setCandidateDuration(input) {
    if (!input.validity.valid) return;
    proposedVerification[candidateTier].minutes = Number(input.value);
    document.getElementById('uvTotalTime').textContent = verificationTimeText();
}

function chooseCandidateTier(tier) {
    if (!document.getElementById('uvDuration').reportValidity()) return;
    candidateTier = tier;
    verificationDialog('chooseCandidateTier-' + Object.keys(proposedVerification).indexOf(tier));
}

function startVerify() {
    if (!document.getElementById('uvDuration').reportValidity()) return;
    runningVerification = structuredClone(proposedVerification);
    verificationPlan = plan;
    choicesSaved = false;
    verifyMinutes = Object.values(runningVerification).reduce((sum, c) => sum + c.minutes, 0);
    for (const tier of Object.keys(runningVerification)) {
        const row = selectedVerificationCandidate(tier);
        log(`${tier} · ${row.mv} mV @ ${row.target} MHz · ${selectedVerificationMinutes(tier)} min verification`);
    }
    ticks = 0;
    stage = 'verifying';
    closePanel();
    render();
    armTimer();
}

function selectedVerificationCandidate(tier) {
    return verificationPlan === plan ? plan[runningVerification[tier]?.index] : undefined;
}

function selectedVerificationMinutes(tier) {
    return verificationPlan === plan ? runningVerification[tier]?.minutes : undefined;
}

function verificationSummary() {
    if (verificationPlan !== plan) return '';
    return Object.keys(runningVerification).map(tier => {
        const row = selectedVerificationCandidate(tier);
        return `${tier} · ${row.mv} mV @ ${row.target} MHz · ${selectedVerificationMinutes(tier)} min`;
    }).join(' / ');
}

function saveVerifiedCandidate(tier, row, memoryClockMHz, durationMinutes) {
    const id = profiles.length;
    profiles.push([tier, row.mv, row.target, row.fps, row.power, row.fps / row.power]);
    profileMeta.push({
        tier, origin: tiers.indexOf(tier), valid: true, source: 'Auto UV', fromVerification: true,
        mem: Number(memoryClockMHz), durationMinutes, meas: row.meas, vf: structuredClone(row.points),
        date: 'Just now', deleted: false,
    });
    order.unshift(id);
    if (!verified.includes(tier)) verified.push(tier);
    return id;
}

function commitVerifiedChoices() {
    if (verificationPlan !== plan || choicesSaved) return;
    choicesSaved = true;
    let first;
    for (const tier of Object.keys(runningVerification)) {
        const id = saveVerifiedCandidate(tier, selectedVerificationCandidate(tier),
            settings.settings[tier].memory, selectedVerificationMinutes(tier));
        first ??= id;
    }
    if (first !== undefined) pi = first;
}
