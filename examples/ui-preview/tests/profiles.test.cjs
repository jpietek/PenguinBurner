const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const preview = path.resolve(__dirname, '..');
const read = (name) => fs.readFileSync(path.join(preview, name), 'utf8');
const sample = JSON.parse(read('sample.json'));
const design = JSON.parse(read('pburn-design.json'));
// build_preview.py hands the frozen prototype one flat colour set, the
// shipped dark look; resolve the same roles here.
design.colors = Object.assign({}, design.themes.dark, design.accents.penguinburner.dark);

// Load the profiles area as the page does (classic scripts sharing one
// global scope). Rendering, dialogs and the shell are stubbed boundaries.
function createPreview({ gpus = 1 } = {}) {
    const calls = { info: [], panels: [], verify: [], status: [] };
    const node = { addEventListener() {}, focus() {}, setAttribute() {}, querySelector: () => null };
    const context = {
        console, structuredClone, Date, Math, Number, String, JSON, Set, Object, Array,
        sample, design, calls,
        document: {
            addEventListener() {}, getElementById: () => null, querySelector: () => null,
            querySelectorAll: () => [], createElement: () => node,
        },
        window: { addEventListener() {} },
        M: { querySelector: () => null, focus() {} },
        setTimeout: (fn) => fn(),
    };
    vm.createContext(context);
    vm.runInContext(`
        const tiers = ['Efficiency', 'Balanced', 'Performance'];
        const GPU_CHOICES = [
            {index: 0, name: 'NVIDIA GeForce RTX 5080', short: 'RTX 5080'},
            {index: 1, name: 'NVIDIA GeForce RTX 4070', short: 'RTX 4070'},
        ];
        const appState = {gpus: GPU_CHOICES.slice(0, ${gpus}), selectedGpu: 0, daemon: 'ready',
            runningGame: null, runningProfile: null, bootProfile: null};
        const profiles = [
            ['Efficiency', 850, 2460, 56.76, 232.9, 0.2437],
            ['Balanced', 850, 2639, 62.7, 267.15, 0.2347],
            ['Performance', 915, 2980, 66.8, 310.18, 0.2154],
        ];
        const games = [{name: 'Game', mode: 'Saved', target: 60}];
        let pi = 1, order = [0, 1, 2], useExamples = true, stage = 'idle', tab = 'Profiles', idx = 0;
        const g = () => games[idx];
        function render() {}
        function renderShellStatus() {}
        function setGlobalStatus(s) { calls.status.push(s); }
        function showPanel(html) { calls.panels.push(html); }
        function closePanel() {}
        function info(title, text) { calls.info.push([title, text]); }
        function ensureDaemonReady(next) { next(); }
        function startProfileVerification(i, minutes) { calls.verify.push([i, minutes]); }
        function defaultVerificationMinutes() { return 5; }
        function tierColor(t) { return design.colors[t.toLowerCase()]; }
        const pill = (s) => s, head = (t) => t, row = (t) => t, metric = (t) => t, title = (t) => t;
    `, context);
    for (const name of ['profiles.js', 'profile-menu.js', 'afterburner.js']) vm.runInContext(read(name), context);
    // Values cross the realm as JSON so deepEqual compares plain data.
    const raw = (code) => vm.runInContext(code, context);
    const run = (code) => {
        const value = raw(code);
        return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
    };
    raw('normalizeProfiles()');
    return { run, raw, calls };
}

test('the table defaults to newest first and hides the other GPU', () => {
    const { run } = createPreview();
    assert.deepEqual(run('sortedProfiles(available())'), [2, 1, 0, 4]);
    run('sortProfiles("saved")');
    assert.deepEqual(run('sortedProfiles(available())'), [4, 0, 1, 2]);
    run('sortProfiles("power")');
    assert.equal(run('profileSort.desc'), false, 'a new column starts ascending');
    assert.deepEqual(run('sortedProfiles(available())'), [0, 1, 4, 2]);
});

test('missing measurements sort last in both directions', () => {
    const { run } = createPreview();
    const id = run(`saveImportedProfile({device: 'VEN_10DE&DEV_2C02.cfg', section: 'Profile1', mv: 940,
        clock: 2550, points: [[900, 2500], [940, 2550]], fan: null})`);
    for (const desc of [false, true]) {
        const ids = run(`sortedProfiles(available(), {key: 'fps', desc: ${desc}})`);
        assert.equal(ids.at(-1), id);
    }
    assert.equal(run(`profileMeta[${id}].preferred && profileSel.has(${id}) && pi === ${id}`), true);
    assert.equal(run(`canEditMemory(${id})`), false);
    assert.equal(run(`hasFanCurve(${id})`), false);
});

test('two GPUs filter by target and refuse mismatched or legacy applies', () => {
    const { run } = createPreview({ gpus: 2 });
    assert.deepEqual(run('sortedProfiles(available())'), [2, 1, 0, 4]);
    run('setProfileTarget("1")');
    assert.deepEqual(run('sortedProfiles(available())'), [3, 4]);
    assert.match(run('applyRefusal(4)'), /legacy profile must be verified on the selected GPU/);
    run('setProfileTarget("")');
    assert.match(run('applyRefusal(1)'), /Choose a target GPU/);
    run('setProfileTarget("0")');
    assert.equal(run('applyRefusal(1)'), '');
    assert.match(run('applyRefusal(3)'), /belongs to a different GPU/);
});

test('Apply honours Apply on startup and Restore defaults persists stock', () => {
    const { run, calls } = createPreview();
    run('applyProfile(1)');
    assert.deepEqual(run('[runtime.id, runtime.gpu, bootByGpu[0] ?? null]'), [1, 0, null]);
    assert.match(calls.status[0].running, /Starting profile: Balanced; Autostart: No/);
    run('toggleStartup(); silentFanCurve = true; applyProfile(2)');
    assert.deepEqual(run('[bootByGpu[0].id, bootByGpu[0].fan]'), [2, true]);
    assert.equal(run('appState.runningProfile'), 'Performance · silent fan curve');
    run('toggleStartup()');
    assert.equal(run('bootByGpu[0]'), undefined, 'unticking clears the boot entry now');
    run('restoreDefaults()');
    assert.deepEqual(run('[runtime.id, bootByGpu[0].id, startupChecked(0)]'), ['stock', 'stock', true]);
    assert.equal(run('appState.runningProfile'), null);
});

test('a running per-game Adaptive game shows both runtime layers', () => {
    const { run } = createPreview();
    run('applyProfile(0); games[0].mode = "Adaptive"; games[0].session = {state: "running"}; appState.runningGame = 0');
    assert.equal(run('appState.runningProfile'), 'Balanced (Adaptive, per-game) · Standing: Efficiency');
    run('games[0].session.state = "stopping"');
    assert.equal(run('appState.runningProfile'), 'Efficiency', 'a stopping game no longer overrides');
    run('appState.runningGame = null');
    assert.equal(run('appState.runningProfile'), 'Efficiency', 'stopping the game elsewhere drops the per-game layer');
});

test('delete warns when it restores stock', () => {
    const { run } = createPreview();
    assert.equal(run('deleteConfirmationText(["Balanced"], false, false)'), 'Delete Auto-UV profile Balanced?');
    run('toggleStartup(); applyProfile(1)');
    assert.deepEqual(run('deleteSafety([1, 2])'), { restoreStock: true, lastAdaptive: false, gpu: 0 });
    assert.match(run('deleteConfirmationText(["Balanced", "Performance"], true, false)'),
        /^Delete 2 selected profiles\? This profile is currently persisted on startup/);
    run('runtime.adaptive = true');
    assert.deepEqual(run('deleteSafety([0, 1, 2, 4])'), { restoreStock: true, lastAdaptive: true, gpu: 0 });
});

test('deleting the running profile deletes only after stock is restored on its GPU', () => {
    const { run, raw, calls } = createPreview({ gpus: 2 });
    run('applyProfile(1)');
    raw('const dialogNode = {focus() {}}; document.getElementById = () => dialogNode');
    raw('ensureDaemonReady = () => {}; deleteSelected([1]); document.getElementById("deleteConfirm").onclick()');
    assert.deepEqual(run('[profileMeta[1].deleted, runtime.id]'), [false, 1], 'declining the service keeps everything');
    raw('ensureDaemonReady = (next) => next(); setProfileTarget("")');
    assert.equal(run('targetGpu()'), null);
    raw('deleteSelected([1]); document.getElementById("deleteConfirm").onclick()');
    assert.deepEqual(run('[profileMeta[1].deleted, runtime.id, runtime.gpu, bootByGpu[0].id]'), [true, 'stock', 0, 'stock']);
    assert.match(run('profileNotice.text'), /Deleted 1 saved profile\. GPU 0 .* runs stock settings/);
    assert.ok(calls.status.some((s) => s && /Restoring GPU to stock/.test(s.running)));
});

test('extended selection selects a range in the visible order', () => {
    const { run } = createPreview();
    run('rowClick({target: {closest: () => null}}, 2)');
    run('rowClick({shiftKey: true, target: {closest: () => null}}, 0)');
    assert.deepEqual(run('selectedIds()'), [2, 1, 0]);
    run('rowClick({ctrlKey: true, target: {closest: () => null}}, 1)');
    assert.deepEqual(run('selectedIds()'), [2, 0]);
});

test('Adaptive shows one tier per GPU and an assignment moves it', () => {
    const { run } = createPreview();
    assert.deepEqual(run('[tierLabel(1), tierLabel(4)]'), ['Balanced', '']);
    run('assignTier(4, "Balanced")');
    assert.deepEqual(run('[tierLabel(1), tierLabel(4)]'), ['', 'Balanced']);
    run('assignTier(4, "None")');
    assert.equal(run('tierLabel(1)'), 'Balanced');
});

test('fan edits keep verification while V/F and memory edits need Verify', () => {
    const { run } = createPreview();
    const fan = run('saveEditedCurve("fan", 1, [[30, 25], [60, 45], [90, 100]])');
    assert.equal(run(`profileMeta[${fan}].valid`), true);
    const vf = run('saveEditedCurve("vf", 1, [[840, 2400], [850, 2600], [860, 2600]], [850])');
    assert.equal(run(`profileMeta[${vf}].valid`), false);
    assert.equal(run(`profiles[${vf}][0]`), 'User edited 2600 MHz 850 mV mem +1500 MHz');
    assert.deepEqual(run(`profileMeta[${vf}].controls`), [850]);
    assert.equal(run(`profileMeta[1].valid`), true, 'the original is untouched');
    assert.equal(run('transferRate(1500)'), '= +3000 MT/s transfer rate');
});

test('verify passes the chosen duration to Auto UV', () => {
    const { run, raw, calls } = createPreview();
    const minutes = { value: '12', reportValidity: () => true, focus() {}, select() {} };
    const start = {};
    raw('document').getElementById = (id) => (id === 'verifyMinutes' ? minutes : id === 'startVerify' ? start : null);
    run('verifyProfile(1)');
    assert.match(calls.panels.at(-1), /min="1" max="60"/);
    start.onclick();
    assert.deepEqual(run('calls.verify'), [[1, 12]]);
    run('profileMeta[1].valid = false; profileVerificationFinished(1, true)');
    assert.equal(run('profileMeta[1].valid && profileMeta[1].preferred'), true);
});

test('the V/F editor keeps the curve monotonic', () => {
    const { run } = createPreview();
    run('editor = newEditor("vf", 1)');
    const n = run('editor.points.length');
    run('editor.index = 10; editorNudgeY(editor, 300)');
    const pts = run('editor.points');
    for (let k = 1; k < n; k++) assert.ok(pts[k][1] >= pts[k - 1][1] || k <= 10, 'higher bins never fall below');
    for (let k = 11; k < n; k++) assert.ok(pts[k][1] >= pts[10][1]);
    run('editorFlatten(editor)');
    assert.ok(run('editor.points.slice(10).every(p => p[1] === editor.points[10][1])'));
    run('editorUndo(); editorUndo()');
    assert.deepEqual(run('editor.points'), run('editor.before'));
    run('editor.index = 5; editorExtendRange(editor); editorExtendRange(editor); editorNudgeY(editor, 15)');
    assert.deepEqual(run('editorRangeIds(editor)'), [5, 6, 7]);
});

test('the fan editor moves points in both axes inside the fixed plot', () => {
    const { run } = createPreview();
    run('editor = newEditor("fan", 1)');
    assert.deepEqual(run('editor.xRange'), [30, 95]);
    assert.ok(run('editor.measured.length') > 10);
    run('editor.index = 3; editorNudgeX(editor, 4); editorNudgeY(editor, 2)');
    assert.deepEqual(run('editor.points[3]'), [69, 53]);
    run('editorAddPoint(editor, 50, 40)');
    assert.deepEqual(run('editor.points[editor.index]'), [50, 40]);
    run('editorAddPoint(editor, 94, 100)');
    assert.equal(run('editor.index'), run('editor.points.length - 1'));
    run('editorRevert()');
    assert.deepEqual(run('editor.points'), run('editor.before'));
    assert.ok(run('editor.points[editor.index] !== undefined'), 'Revert keeps the selection on the curve');
    run('editorUndo()');
    assert.equal(run('editor.points.length'), run('editor.before.length + 2'), 'Revert is undoable');
    run('editor.index = 0; editorNudgeX(editor, -20)');
    assert.equal(run('editor.points[0][0]'), 30);
});

test('context menu enables only actions that can work', () => {
    const { run } = createPreview();
    const state = (id) => Object.fromEntries(run(`profileMenuActions(${id}).map(a => [a[0], a[2] || ''])`));
    assert.match(state(4)['Edit fan curve'], /No editable fan curve/);
    run('profileMeta[1].valid = false');
    assert.match(state(1)['Apply'], /Verify this profile first/);
    assert.match(state(1)['Export LACT…'], /Verify/);
    run('stage = "running"');
    assert.match(state(0)['Delete'], /Finish the current/);
    assert.equal(state(0)['Edit V/F curve'], '');
});

test('Afterburner import refuses while another action runs', () => {
    const { run, calls } = createPreview();
    run('stage = "verifying"; openImport()');
    assert.deepEqual(run('calls.info.at(-1)'),
        ['Import Afterburner', 'Finish the current PenguinBurner action before importing Afterburner.']);
});
