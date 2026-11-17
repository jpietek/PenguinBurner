const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const preview = path.resolve(__dirname, '..');
const script = fs.readFileSync(path.join(preview, 'auto-uv-dialogs.js'), 'utf8');
const sample = JSON.parse(fs.readFileSync(path.join(preview, 'sample.json'), 'utf8'));
const design = JSON.parse(fs.readFileSync(path.join(preview, 'pburn-design.json'), 'utf8'));
const tiers = ['Efficiency', 'Balanced', 'Performance'];

// Exercise the same classic-script entry points used by the page and Qt
// projection. Rendering and timers are the boundaries of these model tests.
function createPreview() {
    const state = {
        design, tiers, plan: structuredClone(sample.rows), completed: sample.rows.length,
        profiles: [], profileMeta: [], order: [], verified: [], pi: -1,
        verifyMinutes: 5, stage: 'choice', ticks: 0, structuredClone,
        est: () => '10–20 min', activeTiers: () => tiers,
        closePanel() {}, render() {}, log() {}, armTimer() {},
        document: {getElementById: () => ({reportValidity: () => true})},
    };
    vm.createContext(state);
    vm.runInContext(script, state);
    state.uvDialog = () => {};
    state.settings = state.buildInitialSetup();
    return state;
}

function selectCandidate(state, tier, index, minutes) {
    // This is the radio group's change handler; the native form separately
    // enforces bounds on its duration field.
    state.chooseCandidateTier(tier);
    vm.runInContext('proposedVerification[candidateTier].index=' + index, state);
    state.setCandidateDuration({validity: {valid: true}, value: String(minutes)});
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
    state.document = {getElementById: id => elements[id]};
    state.draft = state.buildInitialSetup();
    state.setAdvanced('memory', '250');
    assert.equal(elements.uvMemoryRate.textContent, '500 MT/s');
    assert.equal(state.draft.settings.Performance.memory, 250);
    assert.equal(state.draft.settings.Efficiency.memory, 0);
    state.changePowerRange('365');
    assert.equal(state.draft.settings.Balanced.watts, 365);
    assert.equal(state.draft.settings.Performance.watts, 360);

    state.setScope('Selected profile');
    state.setSetupTier('Performance');
    state.setAdvanced('memory', '300');
    assert.equal(state.draft.settings.Balanced.memory, 250);
    state.setScope('Full scan');
    assert.equal(state.draft.settings.Performance.memory, 250);
    assert.equal(state.settings.settings.Balanced.memory, 0);
});

test('full and single-tier suggestions choose passing, completed probes', () => {
    const state = createPreview();
    const candidates = state.tierCandidates('Performance');
    assert.ok(candidates.every(({row}) => row.tier === 'Performance' && row.passed));
    assert.equal(candidates.length, 4);
    assert.equal(state.suggestedCandidate(candidates), 14);
    state.settings.scope = 'Selected profile';
    assert.equal(state.suggestedCandidate(candidates), 13);
    state.completed = 12;
    assert.equal(state.tierCandidates('Performance').length, 2);
    assert.equal(state.suggestedCandidate(state.tierCandidates('Efficiency')), 4);
});

test('final verification saves the chosen probes once and retains earlier profiles', () => {
    const state = createPreview();
    state.settings.settings.Balanced.memory = 255;
    state.verifySetup();
    selectCandidate(state, 'Balanced', 5, 13);
    state.startVerify();
    assert.equal(state.verifyMinutes, 5 + 13 + 5);
    assert.equal(state.selectedVerificationCandidate('Balanced'), state.plan[5]);
    assert.equal(state.selectedVerificationMinutes('Balanced'), 13);
    state.commitVerifiedChoices();
    state.commitVerifiedChoices();
    assert.equal(state.profiles.length, 3);
    const first = state.profileMeta[1];
    assert.equal(first.meas, state.plan[5].meas);
    assert.deepEqual(first.vf, state.plan[5].points);
    assert.equal(first.durationMinutes, 13);
    assert.equal(first.mem, 255);
    assert.equal(state.profiles[1][3], state.plan[5].fps);
    assert.deepEqual(state.verified, tiers);

    state.plan = structuredClone(sample.rows);
    state.settings.scope = 'Selected profile';
    state.activeTiers = () => ['Balanced'];
    state.verifySetup();
    selectCandidate(state, 'Balanced', 8, 7);
    state.startVerify();
    state.commitVerifiedChoices();
    assert.equal(state.profiles.length, 4);
    assert.equal(state.profiles[3][3], state.plan[8].fps);
    assert.equal(state.profileMeta[3].durationMinutes, 7);
    assert.deepEqual(state.profileMeta[1].vf, state.plan[5].points);
    assert.equal(state.profileMeta[1].durationMinutes, 13);
    assert.equal(state.pi, 3);
});

test('native projection accepts a chosen candidate and copies its curve', () => {
    const state = createPreview();
    const row = state.plan[1];
    const id = state.saveVerifiedCandidate(row.tier, row, 50, 9);
    assert.equal(state.order[0], id);
    assert.equal(state.profileMeta[id].fromVerification, true);
    assert.equal(state.profileMeta[id].mem, 50);
    assert.equal(state.profiles[id][5], row.fps / row.power);
    const original = row.points[0][1];
    row.points[0][1] += 30;
    assert.equal(state.profileMeta[id].vf[0][1], original);
});
