const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const preview = path.resolve(__dirname, '..');
const script = fs.readFileSync(path.join(preview, 'history.js'), 'utf8');
const design = JSON.parse(fs.readFileSync(path.join(preview, '..', '..', 'ui', 'design', 'pburn-design.json'), 'utf8'));
// build_preview.py hands the frozen prototype one flat colour set, the
// shipped dark look; resolve the same roles here.
design.colors = Object.assign({}, design.themes.dark, design.accents.penguinburner.dark);

// The Perf Profile model runs without a page: rings, columns, captions and
// the recording heartbeat are plain functions over the sample games.
function createHistory() {
    const session = () => ({state: 'stopped'});
    const games = [
        {id: '1091500', name: 'Cyberpunk 2077', wrapped: true, cap: true, mode: 'Stock', target: 60, session: session()},
        {id: '2531310', name: 'The Last of Us Part II Remastered', wrapped: true, cap: true, mode: 'Stock', target: 60, session: session()},
        {id: '1888930', name: 'The Last of Us Part I', wrapped: true, cap: false, mode: 'Stock', target: 60, session: session()},
        {id: '782330', name: 'DOOM Eternal', wrapped: true, cap: true, mode: 'Stock', target: 60, session: session()},
        {id: 'demo-lutris', name: 'Dishonored', wrapped: true, cap: true, mode: 'Stock', target: 60, session: session()},
    ];
    const state = {
        design, games, appState: {selectedGpu: 0}, idx: 0, tab: 'Game Library', report: false,
        document: {hidden: true, addEventListener() {}},
        withAlpha: (hex, a) => hex.slice(0, 7) + Math.round(a * 255).toString(16).padStart(2, '0'),
    };
    state.g = () => state.games[state.idx];
    vm.createContext(state);
    vm.runInContext(script, state);
    // Top-level const bindings are not context properties; expose the ones under test.
    vm.runInContext('Object.assign(globalThis, {PERF, PERF_OVERVIEW_CAPTION, PERF_LIVE_CAPTION, perfRecord})', state);
    return state;
}
// Values built inside the vm carry its realm's prototypes.
const plain = v => JSON.parse(JSON.stringify(v));
const byId = (state, id) => state.games.find(x => x.id === id);
const recordOf = (state, id) => state.perfRecord(byId(state, id));

test('overview columns hold at least 200 frames and end at now', () => {
    const state = createHistory();
    assert.equal(state.densifyFrametimes([]).length, 0);
    const short = state.densifyFrametimes(Array(50).fill(10));
    assert.equal(short.length, 1);
    assert.equal(short[0].x, 0);
    const fiveMin = state.densifyFrametimes(Array(18000).fill(1000 / 60));
    assert.equal(fiveMin.length, 90);
    assert.ok(Math.abs(fiveMin.at(-1).x) < 1e-9);
    assert.ok(Math.abs(fiveMin[0].x + 4.9444) < 0.001);
    const long = state.densifyFrametimes(Array(120000).fill(11.5));
    assert.equal(long.length, 300);
});

test('a hitch is over 33 ms and twice its column median', () => {
    const state = createHistory();
    const cols = [[16.7, 18], [10, 45], [33.4, 40], [33.4, 90]].map(([p50, max], i) => ({x: i, p50, p99: p50, max}));
    assert.deepEqual(plain(state.hitchPoints(cols).map(c => c.max)), [45, 90]);
});

test('the live tail stops at the window and marks frame starts', () => {
    const state = createHistory();
    const {xs, values} = state.liveTail(Array(2000).fill(10), 10000);
    assert.ok(values.length === 1000 || values.length === 1001);
    assert.equal(xs.at(-1), -0.01);
    assert.equal(state.liveTail([50000], 10000).values.length, 1);
});

test('ceiling, ticks and captions follow PR #137', () => {
    const state = createHistory();
    assert.equal(state.frametimeCeiling(11.5, 16.4), 35);
    assert.equal(state.frametimeCeiling(4, 8), 20);
    assert.equal(state.frametimeCeiling(30, 40), 75);
    assert.deepEqual([20, 25, 30, 50, 75].map(state.frametimeStep), [5, 5, 10, 10, 20]);
    assert.equal(state.PERF_OVERVIEW_CAPTION,
        'line: median frametime · band: up to the 1% worst frames · dots: hitches over 33 ms');
    assert.equal(state.PERF_LIVE_CAPTION,
        'flat & low = smooth · stutter needles yellow -> red by severity (red ~30 ms) · lines: median & 1%-low (p99)');
    assert.equal(state.clippedCaption('c', 182.4, 35), 'c · worst frame 182 ms (clipped)');
    assert.equal(state.clippedCaption('c', 34, 35), 'c');
    assert.equal(state.warmingText(4.6), 'Warming up · 4 of 5 min captured');
    assert.equal(state.latencyText({ms: 22.4, estimated: true}), '~22 ms');
    assert.equal(state.latencyText({ms: 28, estimated: false}), '28 ms');
});

test('needles grade from warning to severe on the true value', () => {
    const state = createHistory();
    assert.equal(state.stutterColor(16.7), design.colors.warning.toUpperCase());
    assert.equal(state.stutterColor(33.3), design.colors.severe.toUpperCase());
    assert.equal(state.stutterColor(120), design.colors.severe.toUpperCase());
    assert.equal(state.hitchColor(33.3), design.colors.warning.toUpperCase());
});

test('the legend reads exactly as the caption, with a swatch per part', () => {
    const state = createHistory();
    const caption = state.clippedCaption(state.PERF_OVERVIEW_CAPTION, 182, 35);
    const html = state.perfLegendHtml(caption);
    assert.equal(html.replace(/<[^>]+>/g, ''), caption);
    // Line, band, and a warning-then-severe dot pair for the graded hitches.
    assert.equal((html.match(/class="swatch/g) || []).length, 4);
    assert.match(html, new RegExp('--swatch:' + design.colors.warning + '[^>]*></i><i class="swatch dotonly" style="--swatch:' + design.colors.severe, 'i'));
    assert.match(html, /clipnote/);
    const live = state.perfLegendHtml(state.PERF_LIVE_CAPTION);
    assert.equal(live.replace(/<[^>]+>/g, ''), state.PERF_LIVE_CAPTION);
});

test('sample rings are deterministic and cover hitches, a cap and warming', () => {
    const a = createHistory(), b = createHistory();
    const cyber = recordOf(a, '1091500');
    assert.equal(cyber.frames.length, recordOf(b, '1091500').frames.length);
    assert.equal(cyber.totalMs, recordOf(b, '1091500').totalMs);

    const s = a.perfSummary(cyber);
    assert.ok(s.qualified);
    assert.ok(s.framegen);
    assert.ok(Math.abs(s.fps - 87) <= 2, 'median near 87 fps: ' + s.fps);
    const hitches = a.hitchPoints(a.perfColumns(cyber));
    assert.ok(hitches.length >= 8, 'real hitches: ' + hitches.length);
    assert.ok(Math.max(...hitches.map(c => c.max)) > a.frametimeCeiling(s.p50, s.p99), 'one hitch is clipped');

    // A 60 fps menu in a ~155 fps game is not a hitch: the column median rises with it.
    const doom = recordOf(a, '782330');
    const capped = a.perfColumns(doom).filter(c => Math.abs(c.p50 - 1000 / 60) < 0.5);
    assert.ok(capped.length > 0);
    assert.equal(a.hitchPoints(capped).length, 0);

    const tlou = a.perfSummary(recordOf(a, '2531310'));
    assert.ok(!tlou.qualified);
    assert.match(a.perfMetaText(recordOf(a, '2531310'), tlou), /settling · fingerprint at 5 min$/);

    // The library reads these fields for its rows and peek.
    const x = byId(a, '1091500');
    assert.equal(x.fps, Math.round(s.fps));
    assert.equal(x.recTier, 'Balanced');
    assert.equal(x.latency_ms, 28);
    assert.equal(byId(a, '1888930').mins, undefined);
});

test('spokes use real units and colour by band', () => {
    const state = createHistory();
    const s = state.perfSummary(recordOf(state, '1091500'));
    const spokes = state.perfSpokes(s, 60, 360);
    assert.deepEqual(plain(spokes.map(a => a.code)), ['PWR', 'GPU', 'FPS', 'LOW', 'CPU']);
    const band = Object.fromEntries(spokes.map(a => [a.code, state.axisInBand(a)]));
    assert.deepEqual(band, {PWR: false, GPU: null, FPS: true, LOW: false, CPU: true});
    const low = spokes[3];
    assert.equal(low.spoke, Math.round(s.low / s.fps * 100) + '%');
    assert.equal(low.text, low.spoke + ' of median');
    assert.equal(spokes[0].text, '224 W');
});

test('the library entry is disabled without a ring and peeks once qualified', () => {
    const state = createHistory();
    const none = state.perfEntryInfo(byId(state, '1888930'));
    assert.equal(none.enabled, false);
    assert.match(none.tip, /Wrap the game, pick a mode other than Stock, keep Capture telemetry on/);
    const warming = state.perfEntryInfo(byId(state, '2531310'));
    assert.deepEqual([warming.enabled, warming.peek], [true, false]);
    assert.equal(warming.tip, 'Game Perf Profile — Warming up · 3 of 5 min captured');
    const ready = state.perfEntryInfo(byId(state, '1091500'));
    assert.deepEqual([ready.enabled, ready.peek], [true, true]);
    const rows = state.perfPeekRows(byId(state, '782330'));
    assert.deepEqual(plain(rows.map(r => r[0])), ['Median', '1% low', 'Power draw', 'Latency']);
    assert.equal(rows[3][1], '~22 ms');
    assert.deepEqual(plain(state.perfPeekRows(byId(state, '2531310')).map(r => r[0])), ['Median', '1% low', 'Power draw']);
});

test('a running wrapped session records one second per heartbeat', () => {
    const state = createHistory();
    const doom = byId(state, '782330'), rec = state.perfRecord(doom);
    const before = rec.frames.length, version = state.perfRecordingVersion(doom);
    doom.session.state = 'running';
    state.perfHeartbeat();
    state.perfHeartbeat();
    assert.ok(rec.live);
    assert.ok(rec.frames.length > before + 200, 'two seconds at ~155 fps');
    assert.ok(state.perfRecordingVersion(doom) > version);
    assert.match(state.perfMetaText(rec, state.perfSummary(rec)), /^● REC · 7\.\d min captured/);
    doom.session.state = 'stopped';
    state.perfHeartbeat();
    assert.ok(!rec.live);
    const frames = rec.frames.length;
    state.perfHeartbeat();
    assert.equal(rec.frames.length, frames, 'an archived ring does not grow');
});

test('a fresh session records only when wrapped, captured and not Stock', () => {
    const state = createHistory();
    const x = byId(state, 'demo-lutris');
    x.session.state = 'running';
    state.perfHeartbeat();
    assert.equal(state.perfRecord(x), null, 'Stock mode does not record');
    x.mode = 'Balanced';
    state.perfHeartbeat();
    const rec = state.perfRecord(x);
    assert.ok(rec && rec.live);
    assert.equal(rec.tier, 'Balanced');
    const s = state.perfSummary(rec);
    assert.ok(!s.qualified);
    // A live ring draws its fingerprint early and says it is settling.
    assert.match(state.perfMetaText(rec, s), /^● REC · 0\.0 min captured · [\d,]+ frames · settling · fingerprint at 5 min$/);
    assert.equal(state.perfEntryInfo(x).tip, 'Game Perf Profile — Warming up · 0 of 5 min captured');
});

test('the ring keeps the newest 30 minutes', () => {
    const state = createHistory();
    const rec = state.perfRecord(byId(state, '1091500'));
    state.perfAppend(rec, 10 * 60000);
    assert.ok(rec.totalMs <= 30 * 60000);
    assert.ok(rec.totalMs > 29.9 * 60000);
    const sum = rec.frames.reduce((a, b) => a + b, 0);
    assert.ok(Math.abs(sum - rec.totalMs) < 1e-3);
});

test('an existing ring stops recording while its game runs in Stock', () => {
    const state = createHistory();
    const cyber = byId(state, '1091500'), rec = state.perfRecord(cyber);
    cyber.mode = 'Stock';
    cyber.session.state = 'running';
    const frames = rec.frames.length;
    state.perfHeartbeat();
    state.perfHeartbeat();
    assert.ok(!rec.live, 'Stock is not recorded');
    assert.equal(rec.frames.length, frames);
    assert.doesNotMatch(state.perfMetaText(rec, state.perfSummary(rec)), /REC/);
    cyber.mode = 'Balanced';
    state.perfHeartbeat();
    assert.ok(rec.live);
    assert.ok(rec.frames.length > frames);
});

test('the target follows system-wide unless the game overrides it', () => {
    const state = createHistory();
    const x = byId(state, '1091500');
    x.target = 120;
    x.override = false;
    assert.equal(state.perfTarget(x), 60);
    x.override = true;
    assert.equal(state.perfTarget(x), 120);
});

test('the frametime plot fills the window between 240 and 480 px', () => {
    const state = createHistory();
    // Qt at 1350x900: a 736 px web view, the plot 222 px down, 40 px legend.
    assert.equal(state.perfPlotHeight(736, 222, 40), 418);
    assert.equal(state.perfPlotHeight(500, 222, 40), 240);
    assert.equal(state.perfPlotHeight(1600, 366, 40), 480);
});
