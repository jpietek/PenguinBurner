const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const preview = path.resolve(__dirname, '..');
const dialogs = fs.readFileSync(path.join(preview, 'auto-uv-dialogs.js'), 'utf8');
const flow = fs.readFileSync(path.join(preview, 'auto-uv.js'), 'utf8');
const sample = JSON.parse(fs.readFileSync(path.join(preview, 'sample.json'), 'utf8'));
const design = JSON.parse(fs.readFileSync(path.join(preview, '..', '..', 'ui', 'design', 'pburn-design.json'), 'utf8'));
// build_preview.py hands the frozen prototype one flat colour set, the
// shipped dark look; resolve the same roles here.
design.colors = Object.assign({}, design.themes.dark, design.accents.penguinburner.dark);
const tiers = ['Efficiency', 'Balanced', 'Performance'];

// Run the page's Auto UV scripts against stubbed shell boundaries: the
// shared API (status band, header progress, daemon check, error dialog,
// navigation) is recorded, timers are driven by calling tick().
function createPreview() {
    const panel = {
        open: false, oncancel: null, innerHTML: '',
        showModal() { this.open = true; }, close() { this.open = false; },
        setAttribute() {}, removeAttribute() {}, addEventListener() {}, querySelector: () => null,
    };
    const state = {
        design, sample, structuredClone, console,
        profiles: [], profileMeta: [], order: [], pi: -1, applied: -1, boot: -1,
        appState: {
            gpus: [{index: 0}], selectedGpu: 0, daemon: 'ready', status: null, runningProfile: null, bootProfile: null,
            simulate: {crashRecovery: false, finalVerificationFailure: false, dependencyDownload: false},
        },
        tab: 'Auto UV', navigated: [], statuses: [], progress: [], errors: [],
        panel,
        document: {getElementById: id => (id === 'panel' ? panel : null), querySelectorAll: () => [], activeElement: null},
        M: {querySelector: () => null, innerHTML: ''},
        setInterval: () => 1, clearInterval() {}, setTimeout() {},
        render() {}, shellTools() {}, plotVF() {}, curveLegend: () => '', head: () => '',
        navigate(t) { state.tab = t; state.navigated.push(t); },
        applies: [], applyProfile(i) { state.applies.push(i); },
        setGlobalStatus(s) { state.statuses.push(s); },
        setHeaderProgress(label, pct) { state.progress.push([label, pct]); },
        ensureDaemonReady(next) { next(); },
        showErrorDetails(...args) { state.errors.push(args); },
    };
    vm.createContext(state);
    vm.runInContext(dialogs, state);
    vm.runInContext(flow, state);
    return state;
}

const get = (state, expr) => vm.runInContext(expr, state);

function startScan(state, scope = 'Full scan', tier = 'Balanced') {
    get(state, 'draft = structuredClone(settings)');
    get(state, `draft.scope = ${JSON.stringify(scope)}; draft.tier = ${JSON.stringify(tier)}`);
    state.beginScan();
}

// Tick until the backend asks for a candidate (or the run ends).
function runToChoice(state, limit = 200) {
    for (let i = 0; i < limit && get(state, 'phase') !== 'choice' && get(state, 'phase') !== ''; i++) state.tick();
    return get(state, 'choiceState');
}

function runFinal(state) {
    for (let i = 0; i < 20 && get(state, 'phase') === 'final'; i++) state.tick();
}

test('full-scan memory mirrors Balanced and Performance while power stays per tier', () => {
    const state = createPreview();
    const elements = {
        uvSettings: {reportValidity: () => true},
        'uv-memory': {validity: {valid: true}},
        'uv-watts': {validity: {valid: true}},
        uvMemoryRate: {},
        uvPowerRange: {},
    };
    state.document = {getElementById: id => elements[id] || (id === 'panel' ? state.panel : null)};
    get(state, 'draft = buildInitialSetup()');
    state.setAdvanced('memory', '250');
    assert.equal(elements.uvMemoryRate.textContent, '500 MT/s');
    assert.equal(get(state, 'draft.settings.Performance.memory'), 250);
    assert.equal(get(state, 'draft.settings.Efficiency.memory'), 0);
    state.changePowerRange('365');
    assert.equal(get(state, 'draft.settings.Balanced.watts'), 365);
    assert.equal(get(state, 'draft.settings.Performance.watts'), 360);

    state.setScope('Selected profile');
    state.setSetupTier('Performance');
    state.setAdvanced('memory', '300');
    assert.equal(get(state, 'draft.settings.Balanced.memory'), 250);
    state.setScope('Full scan');
    assert.equal(get(state, 'draft.settings.Performance.memory'), 250);
    assert.equal(get(state, 'settings.settings.Balanced.memory'), 0);
});

test('untouched targets are sent as automatic; a second GPU re-ranges unedited fields', () => {
    const state = createPreview();
    get(state, 'draft = buildInitialSetup()');
    get(state, 'draft.settings.Balanced.watts = 395; draft.edited.Balanced.watts = true');
    const requested = state.requestedSettings(get(state, 'draft'));
    assert.equal(requested.Balanced.mv, 'auto');
    assert.equal(requested.Balanced.clock, 'auto');
    state.regearSetup(1);
    assert.equal(get(state, 'draft.settings.Efficiency.watts'), 170);
    assert.equal(get(state, 'draft.settings.Balanced.watts'), 220, 'edited power is clamped, not reset');
    assert.equal(get(state, 'draft.settings.Balanced.mv'), null, 'no target table: Auto');
});

test('full and single-tier suggestions choose passing, completed probes', () => {
    const state = createPreview();
    get(state, 'plan = structuredClone(sample.rows); completed = plan.length');
    const candidates = state.tierCandidates('Performance');
    assert.ok(candidates.every(({row}) => row.tier === 'Performance' && row.passed));
    assert.equal(candidates.length, 4);
    assert.equal(state.suggestedCandidate(candidates), 14);
    get(state, 'settings.scope = "Selected profile"');
    assert.equal(state.suggestedCandidate(candidates), 13);
    get(state, 'completed = 12');
    assert.equal(state.tierCandidates('Performance').length, 2);
    assert.equal(state.suggestedCandidate(state.tierCandidates('Efficiency')), 4);
});

test('candidate dialog model follows final_choice.py per request reason', () => {
    const state = createPreview();
    get(state, 'plan = [stock, ...structuredClone(sample.rows)]; completed = plan.length');
    assert.deepEqual(tiers.map(t => state.defaultVerificationMinutes(t)), [1, 3, 5]);
    const records = state.tierCandidates('Balanced').map(c => state.candidateRecord(c.index));
    assert.equal(records[0].oc, get(state, 'plan')[records[0].id].target - 2734);

    const adaptive = state.choiceModel({reason: 'adaptive-balanced', tier: 'Balanced', records, defaultId: 10});
    assert.equal(adaptive.title, 'Tune Balanced profile candidate');
    assert.equal(adaptive.defaultId, 10);
    assert.equal(adaptive.minutes, 3);
    assert.equal(adaptive.discardLabel, 'Discard');
    assert.equal(adaptive.acceptLabel, 'Use Selected');
    assert.equal(adaptive.showOc, false);
    assert.match(adaptive.records.find(r => r.id === 10).status, /^Tier suggestion · Passed short probe$/);

    const failed = state.choiceModel({reason: 'final-verification-failed', tier: 'Balanced', records, defaultId: 10, minutes: 75,
        decision: {mv: 850, target: 2639, text: 'failed final verification (Vulkan device lost)'}});
    assert.equal(failed.title, 'Choose Safer Auto-UV Candidate');
    assert.equal(failed.acceptLabel, 'Verify Selected');
    assert.equal(failed.discardLabel, 'Start From Scratch');
    assert.equal(failed.rejectAction, 'abort');
    assert.equal(failed.recovery, true);
    assert.equal(failed.minutes, 60, 'capped at the 60 min maximum');
    assert.match(failed.intro, /Failed candidate: 850mV@2639MHz - failed final verification/);
    assert.match(failed.records.find(r => r.id === failed.defaultId).status, /^Next safer pick/);

    const crash = state.choiceModel({reason: 'previous-crash', tier: 'Balanced', records, defaultId: 7});
    assert.equal(crash.title, 'Resume Auto-UV Candidate');
    assert.equal(crash.acceptLabel, 'Resume Selected');
    assert.equal(crash.defaultId, 7);
    assert.equal(state.defaultChoiceSort({reason: 'previous-crash', tier: 'Balanced'}), null, 'backend order');

    const perf = state.tierCandidates('Performance').map(c => state.candidateRecord(c.index));
    const single = state.choiceModel({reason: '', tier: 'Performance', records: perf, defaultId: -1});
    assert.equal(single.title, 'Choose Final verification candidate');
    assert.equal(single.showOc, true);
    assert.equal(single.defaultId, 14, 'highest FPS');
    assert.equal(JSON.stringify(state.defaultChoiceSort({reason: '', tier: 'Performance'})), '{"key":"fps","desc":true}');
    const sorted = state.sortedRecords(single.records, {key: 'power', desc: false});
    assert.ok(sorted.every((r, i) => !i || sorted[i - 1].power <= r.power));
});

test('full scan interleaves descent, choice, final verification and save per tier', () => {
    const state = createPreview();
    startScan(state);
    assert.equal(get(state, 'stage'), 'running');
    let choice = runToChoice(state);
    assert.equal(choice.model.title, 'Tune Efficiency profile candidate');
    assert.ok(get(state, 'runs').every(r => ['Stock', 'Efficiency'].includes(get(state, 'plan')[r.plan].tier)),
        'Balanced has not started before Efficiency is chosen');
    assert.equal(get(state, 'tierState.Efficiency'), 'active');
    assert.equal(get(state, 'tierState.Balanced'), 'pending');

    state.resolveChoice('select', choice.pick, 2);
    assert.equal(get(state, 'phase'), 'final');
    const finalRun = get(state, 'runs').at(-1);
    assert.equal(finalRun.kind, 'final');
    assert.equal(finalRun.duration, 120);
    runFinal(state);
    assert.equal(state.profiles.length, 1, 'Efficiency is saved before Balanced scans');
    assert.equal(state.profileMeta[0].durationMinutes, 2);
    assert.equal(get(state, 'tierState.Efficiency'), 'complete');
    assert.equal(get(state, 'tierState.Balanced'), 'active');

    choice = runToChoice(state);
    assert.equal(choice.model.title, 'Tune Balanced profile candidate');
    state.resolveChoice('discard');
    assert.equal(get(state, 'tierState.Balanced'), 'skipped');
    assert.ok(get(state, 'logLines').some(l => l.endsWith('tier_skipped: balanced · discarded')));
    assert.equal(get(state, 'tierState.Performance'), 'active');

    choice = runToChoice(state);
    assert.equal(choice.request.reason, 'performance-comparison');
    assert.ok(choice.model.records.some(r => r.comparisonRuns === 2));
    state.resolveChoice('select');
    runFinal(state);
    assert.equal(get(state, 'stage'), 'complete');
    assert.equal(state.profiles.length, 2);
    assert.deepEqual(state.navigated, ['Profiles']);
    assert.deepEqual(state.applies, [1], 'the new profile is applied');
    assert.equal(get(state, 'pi'), 1);
    assert.ok(get(state, 'logLines').some(l => l.endsWith('Auto-UV process finished: exit_code=0')));
});

test('single-tier discard saves nothing', () => {
    const state = createPreview();
    startScan(state, 'Selected profile', 'Balanced');
    const choice = runToChoice(state);
    assert.equal(choice.model.title, 'Choose Final verification candidate');
    state.resolveChoice('discard');
    assert.equal(get(state, 'stage'), 'discarded');
    assert.equal(get(state, 'statusText'), 'Final verification discarded. No Auto-UV profile was saved.');
    assert.equal(state.profiles.length, 0);
    assert.deepEqual(state.navigated, []);
});

test('a failed final verification offers a safer candidate; Abort ends as Aborted', () => {
    const state = createPreview();
    state.appState.simulate.finalVerificationFailure = true;
    startScan(state);
    let choice = runToChoice(state);
    const failedPick = choice.pick;
    state.resolveChoice('select');
    choice = runToChoice(state);
    assert.equal(choice.model.title, 'Choose Safer Auto-UV Candidate');
    assert.ok(!choice.model.records.some(r => r.id === failedPick));
    const failedRun = get(state, 'runs').at(-1);
    assert.equal(failedRun.state, 'fail');
    assert.equal(failedRun.failure.label, 'Vulkan device lost');
    state.resolveChoice('abort');
    assert.equal(get(state, 'stage'), 'aborted');
    assert.equal(get(state, 'headerStage'), 'Aborted');
    assert.equal(get(state, 'tierState.Efficiency'), 'stopped');
    assert.equal(get(state, 'tierState.Performance'), 'not-run');
    assert.equal(state.profiles.length, 0);

    // Verify Selected runs the safer pick and keeps going.
    const again = createPreview();
    again.appState.simulate.finalVerificationFailure = true;
    startScan(again);
    again.resolveChoice('select', runToChoice(again).pick);
    const safer = runToChoice(again);
    again.resolveChoice('select', safer.pick);
    runFinal(again);
    assert.equal(again.profiles.length, 1);
    assert.equal(get(again, 'tierState.Balanced'), 'active');
});

test('crash recovery resumes a saved candidate without a duration picker', () => {
    const state = createPreview();
    state.appState.simulate.crashRecovery = true;
    startScan(state);
    const choice = get(state, 'choiceState');
    assert.equal(choice.model.title, 'Resume Auto-UV Candidate');
    assert.equal(choice.model.recovery, true);
    assert.match(choice.model.intro, /Previous decision: 840mV@2585MHz/);
    assert.equal(choice.model.records.at(-1).id, choice.pick, 'the last safe point is preselected');
    assert.equal(state.appState.simulate.crashRecovery, false, 'one-shot');
    state.resolveChoice('select');
    assert.equal(get(state, 'headerStage'), 'Resume verification');
    assert.equal(get(state, 'runs').at(-1).kind, 'resume');
    runFinal(state);
    assert.equal(get(state, 'stage'), 'complete');
    assert.equal(state.profiles[0][0], 'Balanced');
});

test('stopping keeps measurements; a matching setup resumes them as saved', () => {
    const state = createPreview();
    startScan(state, 'Selected profile', 'Efficiency');
    for (let i = 0; i < 9; i++) state.tick();
    state.stopScan();
    assert.equal(get(state, 'runs').at(-1).state, 'stopping');
    assert.equal(get(state, 'headerStage'), 'Stopping');
    state.tick();
    const choice = get(state, 'choiceState');
    assert.equal(choice.request.reason, 'user-stop');
    state.resolveChoice('discard');
    assert.equal(get(state, 'stage'), 'discarded');
    const done = get(state, 'completed');
    startScan(state, 'Selected profile', 'Efficiency');
    assert.equal(get(state, 'headerStage'), 'Resuming');
    assert.equal(get(state, 'statusText'), 'Restored previous scan measurements and curves. Continuing from saved progress.');
    assert.equal(get(state, 'runs').length, done);
    assert.ok(get(state, 'runs').every(r => r.restored));
    assert.equal(state.failureTooltip(get(state, 'runs')[1]), 'Saved measurement: passed');
    assert.match(get(state, 'candidateText'), /\(saved\)$/);
    state.tick();
    assert.equal(get(state, 'phase'), 'probe');
});

test('a failed scan process shows Error, failed tiers and the error dialog', () => {
    const state = createPreview();
    get(state, 'appState.simulate.processFailure = true');
    startScan(state);
    runToChoice(state);
    assert.equal(get(state, 'stage'), 'failed');
    assert.equal(get(state, 'headerStage'), 'Error');
    assert.equal(get(state, 'tierState.Efficiency'), 'failed');
    assert.equal(get(state, 'tierState.Balanced'), 'not-run');
    assert.equal(state.errors.length, 1);
    assert.equal(state.errors[0][0], 'Auto-UV failed');
    assert.match(state.errors[0][2], /^Action: Auto-UV scan\nExit code: 1\nExit status: NormalExit\n\nRecent logs:\n/);
    assert.equal(get(state, 'runs').at(-1).state, 'failed');
});

test('dependency download reports header progress before the baseline', () => {
    const state = createPreview();
    state.appState.simulate.dependencyDownload = true;
    startScan(state);
    assert.equal(get(state, 'headerStage'), 'Downloading dependencies');
    for (let i = 0; i < 5; i++) state.tick();
    assert.deepEqual(state.progress.map(p => p[0]).slice(0, 3),
        ['Downloading dependencies 0%', 'Downloading dependencies 20%', 'Downloading dependencies 40%']);
    assert.equal(state.progress.at(-1)[0], null);
    assert.equal(get(state, 'headerStage'), 'Baseline');
});

test('profile verification runs on the Auto UV tab with header progress', () => {
    const state = createPreview();
    state.profiles.push(['Balanced', 850, 2639, 62.77, 265.24, 0.2367]);
    state.profileMeta.push({tier: 'Balanced', vf: sample.rows[9].points, meas: 2615.55, valid: false});
    state.tab = 'Profiles';
    state.startProfileVerification(0, 5);
    assert.deepEqual(state.navigated, ['Auto UV']);
    assert.equal(get(state, 'headerStage'), 'Profile verification');
    assert.deepEqual(state.progress[0], ['Verifying profile 0s / 5min', 0]);
    for (let i = 0; i < 6; i++) state.tick();
    assert.equal(state.progress.at(-1)[0], 'Verifying profile 2min 30s / 5min');
    for (let i = 0; i < 6; i++) state.tick();
    assert.equal(get(state, 'statusText'), 'Profile verification complete.');
    assert.equal(state.progress.at(-1)[0], 'Verifying profile 5min / 5min');
    assert.equal(state.profileMeta[0].valid, true);
});

test('saved candidates copy their curve', () => {
    const state = createPreview();
    const row = structuredClone(sample.rows[1]);
    const id = state.saveVerifiedCandidate(row.tier, row, 50, 9);
    assert.equal(state.order[0], id);
    assert.equal(state.profileMeta[id].fromVerification, true);
    assert.equal(state.profileMeta[id].mem, 50);
    assert.equal(state.profiles[id][5], row.fps / row.power);
    const original = row.points[0][1];
    row.points[0][1] += 30;
    assert.equal(state.profileMeta[id].vf[0][1], original);
});

test('resume reopens a tier whose descent finished but was never verified', () => {
    const state = createPreview();
    startScan(state);
    state.resolveChoice('select', runToChoice(state).pick, 1);
    runFinal(state);
    assert.equal(runToChoice(state).model.title, 'Tune Balanced profile candidate');
    state.resolveChoice('abort');
    assert.equal(get(state, 'stage'), 'aborted');
    const before = get(state, 'runs').filter(r => r.state === 'pass' || r.state === 'fail').length;
    assert.ok(get(state, 'runs').some(r => r.kind === 'final'), 'Efficiency final row exists');

    startScan(state);
    assert.equal(get(state, 'headerStage'), 'Resuming');
    assert.equal(get(state, 'tierState.Efficiency'), 'complete');
    assert.equal(get(state, 'tierState.Balanced'), 'active', 'Balanced is not skipped');
    assert.equal(get(state, 'tierState.Performance'), 'pending');
    const runs = get(state, 'runs');
    assert.equal(runs.length, before, 'final rows are kept, so run numbers stay put');
    assert.ok(runs.some(r => r.kind === 'final' && r.restored));
    state.tick();
    const choice = get(state, 'choiceState');
    assert.equal(choice.model.title, 'Tune Balanced profile candidate');
    state.resolveChoice('select', choice.pick, 1);
    runFinal(state);
    assert.equal(state.profiles.length, 2);
    assert.equal(state.profiles[1][0], 'Balanced');
    assert.equal(get(state, 'tierState.Performance'), 'active');
});

test('a single-tier scan aborted at its choice resumes to that choice', () => {
    const state = createPreview();
    startScan(state, 'Selected profile', 'Performance');
    runToChoice(state);
    state.resolveChoice('abort');
    startScan(state, 'Selected profile', 'Performance');
    state.tick();
    assert.equal(get(state, 'phase'), 'choice');
    assert.equal(get(state, 'choiceState.request.tier'), 'Performance');
});

test('a scan on the second GPU saves and applies its profile on that GPU', () => {
    const state = createPreview();
    state.appState.gpus.push({index: 1});
    get(state, 'var profileTarget = 0, silentFanCurve = true, runtime = {gpu: 0, id: "stock", fan: true}');
    get(state, 'draft = structuredClone(settings); draft.gpu = 1; draft.scope = "Selected profile"; draft.tier = "Efficiency"');
    state.beginScan();
    assert.equal(get(state, 'preScanSilentFan'), true);
    get(state, 'silentFanCurve = false');
    state.resolveChoice('select', runToChoice(state).pick, 1);
    runFinal(state);
    assert.equal(get(state, 'stage'), 'complete');
    assert.equal(state.profileMeta[0].gpu, 1);
    assert.equal(get(state, 'profileTarget'), 1, 'Profiles targets the scanned GPU before applying');
    assert.equal(get(state, 'silentFanCurve'), true, 'the pre-scan silent-fan intent is restored');
    assert.deepEqual(state.applies, [0]);
    // The native renderer passes its own GPU index.
    const id = state.saveVerifiedCandidate('Balanced', structuredClone(sample.rows[9]), 0, 3, 1);
    assert.equal(state.profileMeta[id].gpu, 1);
});

test('profile verification reports to the Profiles hook', () => {
    const state = createPreview();
    const calls = [];
    state.profileVerificationFinished = (i, passed) => calls.push([i, passed]);
    state.profiles.push(['2700 MHz 880 mV', 880, 2700, 60, 250, 0.24]);
    state.profileMeta.push({gpu: null, valid: true});
    state.startProfileVerification(0, 1);
    for (let i = 0; i < 12; i++) state.tick();
    assert.deepEqual(calls, [[0, true]]);
});

test('fragment logs replay the live scan-stream lines for completed runs only', () => {
    const state = createPreview();
    get(state, 'plan = [stock, ...sample.rows]; completed = plan.length; stage = "choice"');
    state.ensureFragmentState();
    const runs = get(state, 'runs'), lines = get(state, 'logLines');
    const results = lines.filter(l => l.includes('probe_result'));
    assert.equal(results.length, runs.length);
    assert.ok(lines.every(l => /^\[\d\d:\d\d:\d\d\] /.test(l)), 'one timestamped format');
    assert.ok(!lines.some(l => / balanced | performance /i.test(l.replace(/requested settings.*/i, ''))),
        'only the Efficiency descent is logged');
    assert.ok(lines.some(l => l.includes('fail · Clock too low')), 'failures use failureFor labels');
});
