"""Launch with the settings saved on disk, even when Heroic cached old rows."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from integrations.heroic import launch_sync as sync


@pytest.fixture
def config(tmp_path):
    root = tmp_path / '.config/heroic'
    (root / 'GamesConfig').mkdir(parents=True)
    (root / 'config.json').write_text(json.dumps({'defaultSettings': {'wrapperOptions': []}}))
    (root / 'GamesConfig/Turkey.json').write_text(json.dumps({
        'Turkey': {'wrapperOptions': [{'exe': 'PENGUIN_BURNER', 'args': '--pb-overlay=1'}]},
    }))
    return root


def test_stale_launcher_restarts_before_launch_and_fresh_receipt_survives_restart(
    tmp_path, config, monkeypatch,
):
    state = {'launchers': {'41': 'boot:old'}, 'busy': False}
    events = []

    def probe(root, stop=None):
        assert root == config
        if stop:
            assert stop == {'41': 'boot:old'}
            events.append('stop')
            state['launchers'] = {}
        return dict(state)

    def start(command):
        events.append(command)
        state['launchers'] = {'42': 'boot:new'}
        return True

    monkeypatch.setattr(sync, '_probe', probe)
    monkeypatch.setattr(sync, 'start_on_host', start)
    command = ['heroic', 'heroic://launch/legendary/Turkey?gui=false']
    assert sync.launch_with_current_settings(command, home=tmp_path)
    assert events == ['stop', command]
    # A second call uses only the persisted receipt, not an in-memory flag.
    assert sync.launch_with_current_settings(command, home=tmp_path)
    assert events == ['stop', command, command]

    # Disabling the wrapper must also invalidate Heroic's cached configuration.
    (config / 'GamesConfig/Turkey.json').write_text(json.dumps({'Turkey': {'wrapperOptions': []}}))
    state['launchers'] = {'41': 'boot:old'}
    assert sync.launch_with_current_settings(command, home=tmp_path)
    assert events[-2:] == ['stop', command]


def test_changed_settings_invalidate_same_running_launcher(tmp_path, config, monkeypatch):
    state = {'launchers': {'42': 'boot:new'}, 'busy': False}
    receipt = tmp_path / '.config/PenguinBurner/heroic-launch-receipt.json'
    receipt.parent.mkdir()
    receipt.write_text(json.dumps({'root': str(config), 'launchers': state['launchers'],
                                   'settings': sync._settings_fingerprint(config, tmp_path)}))
    (config / 'GamesConfig/Turkey.json').write_text(json.dumps({'Turkey': {'wrapperOptions': []}}))
    stops = []
    def probe(root, stop=None):
        if stop:
            stops.append(stop)
            state['launchers'] = {}
        return dict(state)
    def start(command):
        state['launchers'] = {'43': 'boot:next'}
        return True
    monkeypatch.setattr(sync, '_probe', probe)
    monkeypatch.setattr(sync, 'start_on_host', start)
    assert sync.launch_with_current_settings(['heroic'], home=tmp_path)
    assert stops == [{'42': 'boot:new'}]


def test_busy_launcher_is_neither_stopped_nor_launched_stale(tmp_path, config, monkeypatch):
    calls = []
    def probe(root, stop=None):
        assert stop is None
        return {'launchers': {'41': 'boot:old'}, 'busy': True}
    monkeypatch.setattr(sync, '_probe', probe)
    monkeypatch.setattr(sync, 'start_on_host', lambda command: calls.append(command))
    with pytest.raises(RuntimeError, match='is busy'):
        sync.launch_with_current_settings(['heroic'], home=tmp_path)
    assert calls == []


def test_restart_timeout_does_not_launch_with_old_settings(tmp_path, config, monkeypatch):
    stops = []
    calls = []
    def probe(root, stop=None):
        if stop:
            stops.append(stop)
        return {'launchers': {'41': 'boot:old'}, 'busy': False}
    monkeypatch.setattr(sync, '_probe', probe)
    monkeypatch.setattr(sync, '_RESTART_TIMEOUT_S', 0)
    monkeypatch.setattr(sync, 'start_on_host', lambda command: calls.append(command))
    with pytest.raises(RuntimeError, match='did not exit'):
        sync.launch_with_current_settings(['heroic'], home=tmp_path)
    assert len(stops) == 1
    assert not calls


def test_formatting_and_explicit_inheritance_do_not_require_restart(tmp_path, config):
    (config / 'config.json').write_text(json.dumps({'defaultSettings': {
        'wrapperOptions': [{'exe': 'gamemoderun', 'args': ''}]}}))
    path = config / 'GamesConfig/Turkey.json'
    path.write_text(json.dumps({'Turkey': {}}))
    before = sync._settings_fingerprint(config, tmp_path)
    path.write_text(json.dumps({'version': 'v0', 'Turkey': {
        'wrapperOptions': [{'exe': 'gamemoderun', 'args': ''}], 'unrelated': True}}, indent=2))
    assert sync._settings_fingerprint(config, tmp_path) == before


def test_receipt_failure_after_launch_does_not_report_launch_failed(tmp_path, config, monkeypatch):
    states = iter([{'launchers': {}, 'busy': False}, {'launchers': {'42': 'boot:new'}, 'busy': False}])
    monkeypatch.setattr(sync, '_probe', lambda *a, **k: next(states))
    monkeypatch.setattr(sync, 'start_on_host', lambda command: True)
    def fail(*args, **kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(sync, 'atomic_write_text', fail)
    assert sync.launch_with_current_settings(['heroic'], home=tmp_path)


def test_real_probe_stops_only_matching_idle_main_and_rechecks_identity(tmp_path, config):
    env = dict(os.environ, XDG_CONFIG_HOME=str(config.parent))
    with subprocess.Popen(['heroic', '-c', 'import time; time.sleep(30)'],
                          executable=sys.executable, env=env) as child:
        try:
            state = sync._probe(config)
            assert str(child.pid) in state['launchers']
            assert not state['busy']
            with pytest.raises(RuntimeError):
                sync._probe(config, stop={str(child.pid): 'wrong incarnation'})
            assert child.poll() is None
            sync._probe(config, stop=state['launchers'])
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


def test_real_probe_refuses_to_stop_parent_of_active_child(tmp_path, config):
    code = '''
import subprocess, sys
child = subprocess.Popen(['/bin/sleep', '30'])
print('ready', flush=True)
try: sys.stdin.readline()
finally:
    child.terminate()
    child.wait()
'''
    env = dict(os.environ, XDG_CONFIG_HOME=str(config.parent))
    with subprocess.Popen(['heroic', '-c', code], executable=sys.executable, env=env,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == 'ready'
            state = sync._probe(config)
            assert state['busy']
            with pytest.raises(RuntimeError):
                sync._probe(config, stop=state['launchers'])
            assert child.poll() is None
        finally:
            child.communicate('\n', timeout=5)


def test_flatpak_main_with_flattened_title_uses_parent_environment(tmp_path, config):
    proc = tmp_path / 'proc'
    boot = proc / 'sys/kernel/random/boot_id'
    boot.parent.mkdir(parents=True)
    boot.write_text('test-boot')
    main = proc / '41'
    main.mkdir()
    (main / 'cmdline').write_bytes(b'/app/bin/heroic/heroic heroic://launch/legendary/Turkey\0')
    (main / 'comm').write_text('heroic\n')
    (main / 'exe').symlink_to('/app/bin/heroic/heroic')
    (main / 'environ').write_bytes(b'LD_PRELOAD=zypak\0')
    (main / 'stat').write_text('41 (heroic) S 40 ' + '0 ' * 17 + '123')
    parent = proc / '40'
    parent.mkdir()
    (parent / 'cmdline').write_bytes(b'/bin/sh\0/app/bin/heroic-run\0')
    (parent / 'comm').write_text('heroic-run\n')
    (parent / 'environ').write_bytes(f'XDG_CONFIG_HOME={config.parent}\0'.encode())
    (parent / 'stat').write_text('40 (heroic-run) S 1 ' + '0 ' * 17 + '122')
    script = sync._LAUNCHER_PROBE.replace("'/proc", repr(str(proc))[:-1])
    result = subprocess.run([sys.executable, '-c', script, str(config), '{}'],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {'launchers': {'41': 'test-boot:123'}, 'busy': False}
