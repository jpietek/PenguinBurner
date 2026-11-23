const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const preview = path.resolve(__dirname, '..');
const script = fs.readFileSync(path.join(preview, 'library.js'), 'utf8');

// Load library.js as the page does, with the shell's shared state stubbed and
// a manual clock standing in for the browser's timers.
function createLibrary() {
    const timers = [];
    const state = {
        clock: 0,
        panels: [],
        statuses: [],
        games: [
            {id: '1091500', name: 'Cyberpunk 2077', mins: 24, fps: 87, low: 61, cap: true},
            {id: '2531310', name: 'The Last of Us Part II Remastered', mins: 3, fps: 72, low: 53, cap: true},
            {id: '1888930', name: 'The Last of Us Part I', mins: 0, cap: false},
            {id: '782330', name: 'DOOM Eternal', mins: 0, cap: true},
        ],
        GPU_CHOICES: [
            {index: 0, name: 'NVIDIA GeForce RTX 5080', short: 'RTX 5080'},
            {index: 1, name: 'NVIDIA GeForce RTX 4070', short: 'RTX 4070'},
        ],
        tiers: ['Efficiency', 'Balanced', 'Performance'],
        tab: 'Game Library', report: false, idx: 0, q: '',
        assets: {}, design: {colors: {}},
        prototypeChoices: [['launcherWriteState', 'Launcher setup', ['ready', 'not-set-up', 'needs-restart'], ['Ready', 'Steam not set up', 'Steam needs restart']]],
        prototypeToggles: [],
        document: {
            addEventListener() {}, querySelector: () => null,
            getElementById: () => ({focus() {}, setAttribute() {}, hidden: true}),
            querySelectorAll: () => [], activeElement: null,
        },
        window: {addEventListener() {}},
        addEventListener() {},
        localStorage: {getItem: () => null, setItem() {}},
        setTimeout(fn, ms) {
            const timer = {fn, at: state.clock + ms, id: timers.length + 1};
            timers.push(timer);
            return timer.id;
        },
        clearTimeout(id) {
            const timer = timers.find((t) => t.id === id);
            if (timer) timer.cancelled = true;
        },
        setInterval() {},
        render() {},
        esc: (s) => String(s),
        row: (title, desc, value) => `<div class="row"><h3>${title}</h3><small>${desc}</small>${value}</div>`,
        showPanel(html) { state.panels.push(html); },
        closePanel() {},
        setGlobalStatus(status) { state.statuses.push(status); },
        gpuLabel: (i = 0) => 'GPU ' + i,
        adaptiveTiers: () => [],
        openReport() { state.opened = (state.opened || 0) + 1; },
    };
    state.appState = {
        gpus: state.GPU_CHOICES.slice(0, 1), selectedGpu: 0, daemon: 'ready', runningGame: null,
        launcherWriteState: 'ready', emptyLibrary: false, simulate: {},
    };
    state.advance = (ms) => {
        const end = state.clock + ms;
        for (;;) {
            const next = timers
                .filter((t) => !t.done && !t.cancelled && t.at <= end)
                .sort((a, b) => a.at - b.at)[0];
            if (!next) break;
            state.clock = next.at;
            next.done = true;
            next.fn();
        }
        state.clock = end;
    };
    vm.createContext(state);
    vm.runInContext('var g = () => games[idx];', state);
    vm.runInContext(script, state);
    state.run = (code) => vm.runInContext(code, state);
    state.advance(2000); // the first library scan
    return state;
}

test('Play walks Starting, Stop, Stopping and back, one game at a time', () => {
    const s = createLibrary();
    const cyberpunk = s.games[0], doom = s.games[3];
    assert.equal(s.playView(cyberpunk, 0).text, 'Play');
    s.playClicked();
    let view = s.playView(cyberpunk, 0);
    assert.deepEqual([view.text, view.enabled], ['Starting…', false]);
    assert.match(s.run('lib.message'), /preparing launch/);
    s.advance(500);
    assert.match(s.run('lib.message'), /launching via Steam/);
    s.advance(1000);
    // Running is observed, but the button holds Starting… until it settles.
    assert.equal(cyberpunk.session.state, 'running');
    assert.equal(s.playView(cyberpunk, 0).text, 'Starting…');
    s.advance(2000);
    view = s.playView(cyberpunk, 0);
    assert.deepEqual([view.text, view.enabled], ['Stop', true]);
    assert.equal(s.appState.runningGame, 0);
    assert.equal(s.statuses.at(-1).running, 'Playing Cyberpunk 2077');

    const blocked = s.playView(doom, 3);
    assert.deepEqual([blocked.text, blocked.enabled], ['Play', false]);
    assert.equal(blocked.tip, 'Cyberpunk 2077 is still running. Stop it before launching another game.');

    s.playClicked();
    view = s.playView(cyberpunk, 0);
    assert.deepEqual([view.text, view.enabled], ['Stopping…', true]);
    s.playClicked(); // a second press asks again instead of being ignored
    assert.match(s.run('lib.message'), /requesting stop again/);
    s.advance(2000);
    assert.equal(s.playView(cyberpunk, 0).text, 'Play');
    assert.equal(s.appState.runningGame, null);
    assert.equal(s.statuses.at(-1), null);
    assert.equal(s.playView(doom, 3).enabled, true);
});

test('launch failures, unconfirmed launches and outside games', () => {
    const s = createLibrary();
    s.appState.launchOutcome = 'fails';
    s.playClicked();
    s.advance(1500);
    assert.equal(s.games[0].session.warning, "PenguinBurner could not execute the game's launch command.");
    assert.equal(s.playView(s.games[0], 0).text, 'Play');

    s.appState.launchOutcome = 'unconfirmed';
    s.playClicked();
    s.advance(5000);
    const view = s.playView(s.games[0], 0);
    assert.deepEqual([view.text, view.enabled], ['Play', true]);
    assert.match(view.tip, /not confirmed/);
    assert.equal(s.playView(s.games[3], 3).enabled, false, 'an unconfirmed launch still blocks others');
    s.playClicked();
    assert.match(s.panels.at(-1), /may start a second instance/);

    const t = createLibrary();
    t.appState.externalGame = true;
    t.syncSessions();
    const outside = t.playView(t.games[2], 2);
    assert.deepEqual([outside.text, outside.enabled], ['Running', false]);
    assert.match(t.playView(t.games[0], 0).tip, /The Last of Us Part I is still running/);
    assert.equal(t.canLaunch(t.games[6]), false, 'the Faugus client is not installed');
});

test('Steam write state gates writes and names its fix-it action', () => {
    const s = createLibrary();
    assert.equal(
        s.libraryStatusText(s.libraryGames(), '', s.writeState('Steam')),
        '9 games · 6 configured · Steam user: jp · live apply',
    );
    s.appState.launcherWriteState = 'needs-restart';
    const restart = s.writeState('Steam');
    assert.equal(restart.ready, false);
    assert.equal(restart.action, 'Restart Steam to finish');
    assert.equal(restart.summary, 'read-only until initialized');
    const compat = s.compatField(s.games[0]);
    assert.equal(compat.enabled, false);
    assert.match(compat.subtitle, /^Needs live apply/);
    assert.equal(s.writeState('Lutris').ready, true);
    s.appState.launcherWriteState = 'not-set-up';
    assert.equal(s.writeState('Steam').action, 'Scan my Steam library');
    s.appState.launcherWriteState = 'stopped';
    assert.equal(s.writeState('Steam').summary, 'Steam stopped');
});

test('compatibility picker names the effective tool and keeps missing ones', () => {
    const s = createLibrary();
    const cyberpunk = s.compatField(s.games[0]);
    assert.equal(cyberpunk.choices[0][1], 'Steam default (Proton 9.0-4)');
    assert.equal(cyberpunk.enabled, true);
    const missing = s.compatField(s.games[2]);
    assert.equal(missing.choices[0][1], 'Steam default');
    assert.deepEqual(Array.from(missing.choices.at(-1)), ['GE-Proton9-11', 'GE-Proton9-11 (not listed)']);
    const native = s.compatField(s.games[7]);
    assert.equal(native.choices[0][1], 'Native Linux — no compatibility tool');
    assert.equal(native.enabled, false);
    assert.equal(native.subtitle, 'This game does not use Wine or Proton.');
    assert.equal(s.compatField(s.games[4]).choices[0][1], 'Heroic default');
    // A Flatpak Heroic only sees the tools inside its sandbox.
    const flatpak = s.compatField(s.games[4]).choices.length;
    s.appState.heroicInstall = 'native';
    assert.ok(s.compatField(s.games[4]).choices.length > flatpak);
});

test('command edits: launcher subtitles, refused saves keep the draft and selection', () => {
    const s = createLibrary();
    assert.match(s.commandSubtitle(s.games[0]), /%command% is where the game goes/);
    assert.equal(s.commandSubtitle(s.games[4]), 'wrapper_command — inherited from Heroic global settings');
    assert.equal(s.commandSubtitle(s.games[5]), 'prefix_command in the Lutris config');
    s.games[4].wrapped = true;
    s.games[4].flatpakReady = false;
    assert.match(s.commandSubtitle(s.games[4]), /^Flatpak setup required/);
    s.games[4].wrapped = false;

    assert.deepEqual(Array.from(s.commandProblems('Steam', 'a %command% %command%')), ['more than one %command% token']);
    assert.deepEqual(Array.from(s.commandProblems('Steam', 'FOO="bar %command%')), ['unbalanced quotes']);
    assert.deepEqual(Array.from(s.commandProblems('Steam', "FOO='a b' %command%")), []);
    assert.deepEqual(Array.from(s.commandProblems('Lutris', '"')), []);

    s.run('lib.draft = {index: 0, value: \'FOO="x %command%\'}');
    s.selectGame(3);
    assert.equal(s.run('idx'), 0, 'a refused save keeps the selection');
    assert.equal(s.run('lib.draft.value'), 'FOO="x %command%');
    assert.match(s.run('lib.message'), /save failed \(unbalanced quotes\)/);

    s.run('lib.draft = {index: 0, value: "gamemoderun %command%"}');
    s.selectGame(3);
    assert.equal(s.run('idx'), 3);
    assert.equal(s.games[0].command, 'gamemoderun %command%');
    assert.equal(s.games[0].wrapped, false, 'a hand edit without the wrapper unwraps the game');
    s.advance(1000);
    assert.equal(s.run('lib.message'), 'Applied live.');
});

test('All games menu greys no-op entries and skips games without overlay support', () => {
    const s = createLibrary();
    const [enable, disable, show, hide] = s.run('BULK_ACTIONS');
    const list = s.libraryGames();
    assert.equal(s.bulkWouldChange(enable, list), true);
    assert.equal(s.bulkWouldChange(show, list), false, 'every enabled Vulkan game already shows it');
    assert.ok(!s.bulkScope(show, list).some((x) => x.name === 'Oxenfree'));
    assert.ok(s.bulkScope(hide, list).some((x) => x.name === 'Oxenfree'));
    assert.equal(s.bulkScope(hide, list).length, 6);
    assert.match(s.bulkConfirmText(disable, 1), /from 1 game and/);

    s.confirmBulk(0);
    assert.match(s.panels.at(-1), /launch command of 9 games/);
    s.applyBulk(0);
    assert.match(s.run('lib.message'), /Saving changes for 9 games/);
    s.advance(2000);
    assert.ok(s.libraryGames().every((x) => x.wrapped));
    assert.equal(s.bulkWouldChange(enable, s.libraryGames()), false);

    s.appState.gpus = s.GPU_CHOICES.slice(0, 2);
    s.games[0].wrapped = false;
    s.confirmBulk(0);
    assert.equal(s.run('lib.message'), 'Choose a Game GPU for each game before enabling all games.');
});

test('All games leaves launchers that cannot take a write alone', () => {
    const s = createLibrary();
    const [enable] = s.run('BULK_ACTIONS');
    s.appState.launcherWriteState = 'not-set-up';
    assert.ok(!s.bulkScope(enable).some((x) => x.launcher === 'Steam'));
    assert.deepEqual(Array.from(s.bulkSkipped(enable), ([launcher]) => launcher), ['Steam']);
    s.confirmBulk(0);
    assert.match(s.panels.at(-1), /launch command of 3 games/);
    assert.match(s.panels.at(-1), /Skipped: Steam \(not set up\)\. Use Scan my Steam library first\./);
    s.applyBulk(0);
    s.advance(2000);
    assert.equal(s.games[8].wrapped, false, 'Hades II (Steam) is unchanged');
    assert.equal(s.games[4].wrapped, true, 'Control (Heroic) is wrapped');
    assert.match(s.run('lib.message'), /steam: skipped \(not set up\)$/);
    // With only Steam games left to change, the entry is a readout.
    assert.equal(s.bulkWouldChange(enable, s.libraryGames()), false);
});

test('runningGame follows a tuned running game; launchLock holds every active one', () => {
    const s = createLibrary();
    s.playClicked();
    assert.equal(s.appState.launchLock, 0);
    assert.equal(s.appState.runningGame, null, 'launching is not running yet');
    s.advance(1500);
    assert.equal(s.appState.runningGame, 0, 'running counts while Play still settles');
    s.advance(2000);
    s.playClicked();
    assert.equal(s.appState.runningGame, null, 'stopping is not running');
    assert.equal(s.appState.launchLock, 0);
    s.advance(2000);
    assert.equal(s.appState.launchLock, null);

    s.run('idx = 4'); // Control, not wrapped
    s.playClicked();
    s.advance(4000);
    assert.equal(s.games[4].session.state, 'running');
    assert.equal(s.appState.runningGame, null, 'an unwrapped game is not tuned');
    assert.equal(s.appState.launchLock, 4);

    const t = createLibrary();
    t.appState.externalGame = true;
    t.syncSessions();
    assert.equal(t.appState.runningGame, null, 'a game started outside PenguinBurner is not tuned');
    assert.equal(t.appState.launchLock, 2);
});

test('a busy Play button ignores presses', () => {
    const s = createLibrary();
    s.playClicked();
    const timers = s.run('games[0].session.timers.length');
    s.playClicked(); // Starting… is aria-disabled: no second launch
    assert.equal(s.run('games[0].session.timers.length'), timers);
    assert.equal(s.games[0].session.state, 'launching');
});

test('settings writes report the launcher result and respect GPU choice', () => {
    const s = createLibrary();
    s.setMode('Balanced');
    assert.match(s.run('lib.message'), /saving…$/);
    s.advance(1000);
    assert.equal(s.run('lib.message'), 'Mode saved for next launch; no active profile for this game.');
    assert.equal(s.clampTarget(3), 15);
    assert.equal(s.clampTarget(5000), 1000);
    assert.equal(s.clampTarget(92), 90);

    s.appState.gpus = s.GPU_CHOICES.slice(0, 2);
    s.run('idx = 4');
    s.setWrap(true);
    assert.equal(s.games[4].wrapped, false);
    assert.equal(s.run('lib.message'), 'Choose a Game GPU before enabling PenguinBurner.');
    s.setGpu('1');
    s.advance(1000);
    s.setWrap(true);
    assert.equal(s.games[4].wrapped, true);
});

test('empty states, row tooltips and the Perf Profile fallback', () => {
    const s = createLibrary();
    assert.equal(s.libraryPlaceholder(), '');
    s.appState.emptyLibrary = true;
    assert.match(s.libraryPlaceholder(), /^A launcher is installed but has no games yet/);
    s.appState.emptyLibrary = false;
    s.appState.libraryScan = 'no-launcher';
    assert.equal(
        s.libraryPlaceholder(),
        'No game launcher found. PenguinBurner looks for Steam, Lutris, Heroic, Faugus Launcher. Install one, then press Rescan.',
    );
    s.appState.libraryScan = 'heroic-error';
    assert.ok(!s.libraryGames().some((x) => x.launcher === 'Heroic'));
    assert.deepEqual(Array.from(s.availableLaunchers()), ['Steam', 'Lutris', 'Faugus']);

    assert.equal(s.gameTooltip(s.games[8]), 'Hades II\nSteam · Proton\nNot installed');
    assert.equal(s.perfState(s.games[2]).enabled, false);
    s.perfClicked();
    assert.equal(s.opened, 1, 'a game with a recording opens the view');
});

test('a tuned Adaptive game shows its per-game profile in the status detail', () => {
    const s = createLibrary();
    s.games[0].mode = 'Adaptive';
    s.appState.runningProfile = 'Balanced (Adaptive, per-game) · Standing: Default';
    s.playClicked();
    s.advance(4000);
    assert.equal(s.appState.runningGame, 0);
    const status = s.statuses.at(-1);
    assert.equal(status.running, 'Playing Cyberpunk 2077');
    assert.match(status.detail, /^Balanced \(Adaptive, per-game\) · Standing: Default · GPU 0 · via Steam$/);
});
