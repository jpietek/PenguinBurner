const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const preview = path.resolve(__dirname, '..');
const script = fs.readFileSync(path.join(preview, 'overlay.js'), 'utf8');

// Load overlay.js as the page does, with the shell and Profiles state it
// reads stubbed. Only the status slot exists in this fake document.
function createOverlay({runtime = null, runningProfile = null, runningGame = null} = {}) {
    const slot = {dataset: {}, innerHTML: ''};
    const context = {
        appState: {
            runningGame, runningProfile,
            simulate: {overlaySaveFailure: false},
        },
        games: [{id: '1091500', name: 'Cyberpunk 2077'}, {id: '782330', name: 'DOOM Eternal', mode: 'Adaptive', liveTier: 'Efficiency'}],
        profiles: [['Efficiency', 850], ['Balanced', 850], ['Performance', 915], ['Imported curve', 900]],
        profileMeta: [{tier: 'Efficiency'}, {tier: 'Balanced'}, {tier: 'Performance'}, {}],
        runtime,
        adaptiveTiers: () => ['Efficiency', 'Balanced'],
        sample: {defaults: ['base_fps', 'profile'], presets: {}, metrics: {}},
        prototypeChoices: [], prototypeToggles: [],
        tab: 'In-Game Overlay',
        render() {},
        esc: (s) => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
        document: {getElementById: (id) => (id === 'ovStatusSlot' ? slot : null), activeElement: null},
        structuredClone,
    };
    vm.createContext(context);
    vm.runInContext(script + '\n;this.api = {ovRunningTier, ovCurrentTelemetry, ovMetric, ovSet, ovPaint, ovPreviewValues, get status() { return ovStatus; }};', context);
    return {api: context.api, context, slot};
}

test('profile_tier is the bare tier, never the status band label', () => {
    // Profiles labels this run 'Balanced · silent fan curve'.
    const {api} = createOverlay({
        runtime: {gpu: 0, id: 1, fan: true, adaptive: false},
        runningProfile: 'Balanced · silent fan curve',
    });
    assert.equal(api.ovRunningTier(), 'Balanced');
    const values = api.ovPreviewValues(api.ovCurrentTelemetry());
    assert.equal(api.ovMetric('profile', values, 'hud').value, 'Balanced');
    assert.equal(api.ovMetric('profile', values, 'table').value, 'BAL');
});

test('stock settings read Default, also while a game runs', () => {
    assert.equal(createOverlay().api.ovRunningTier(), 'Default');
    const {api} = createOverlay({runtime: {gpu: 0, id: 'stock'}, runningGame: 0});
    assert.equal(api.ovRunningTier(), 'Default');
    assert.equal(api.ovMetric('profile', api.ovPreviewValues(api.ovCurrentTelemetry()), 'table').value, 'DEF');
});

test('a per-game Adaptive game reports its live tier; an untiered profile its name', () => {
    assert.equal(createOverlay({runtime: {gpu: 0, id: 2}, runningGame: 1}).api.ovRunningTier(), 'Efficiency');
    assert.equal(createOverlay({runtime: {gpu: 0, id: 3}}).api.ovRunningTier(), 'Imported curve');
    // Auto UV applies by name without a Profiles runtime record.
    assert.equal(createOverlay({runningProfile: 'Performance'}).api.ovRunningTier(), 'Performance');
});

test('a slider save clears an earlier error from the screen', () => {
    const {api, context, slot} = createOverlay();
    context.appState.simulate.overlaySaveFailure = true;
    assert.equal(api.ovSet('font', 'mono', {full: false}), false);
    api.ovPaint();
    assert.match(slot.innerHTML, /role="alert">Overlay config save failed/);
    context.appState.simulate.overlaySaveFailure = false;
    assert.equal(api.ovSet('opacity', 50, {full: false}), true);
    assert.equal(api.status, null);
    assert.equal(slot.innerHTML, '');
});
