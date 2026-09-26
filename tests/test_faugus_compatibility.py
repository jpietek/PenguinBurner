"""Faugus runner IDs, installation visibility and fresh-file write safety."""
from __future__ import annotations

import json
import subprocess

import pytest
from test_faugus_manager import _faugus, _game, _manager

from integrations.faugus import compat_probe, compatibility
from integrations.faugus.paths import FAUGUS_FLATPAK_APP_ID
from integrations.launchers.compatibility import CompatibilityTools
from integrations.launchers.installation import LauncherInstallation


def test_catalogue_matches_faugus_aliases_and_directory_precedence(tmp_path):
    native = tmp_path / '.local/share/Steam/compatibilitytools.d'
    sandbox = tmp_path / '.var/app/com.valvesoftware.Steam/.local/share/Steam/compatibilitytools.d'
    for root in (native, sandbox):
        for name in ('GE-Test', 'Proton-GE Latest', 'UMU-Latest'):
            (root / name).mkdir(parents=True)
    (sandbox / 'Flatpak-Only').mkdir()
    (native / 'not-a-runner').write_text('')
    tools = dict(compat_probe.discover({'home': str(tmp_path)}))
    assert tools['Proton-GE Latest'] == 'GE-Proton Latest'
    assert tools['GE-Test'] == 'GE-Test'
    assert tools['Flatpak-Only'] == 'Flatpak-Only (Flatpak)'
    assert tools[str(sandbox / 'Proton-GE Latest')] == 'Proton-GE Latest (Flatpak)'
    assert 'UMU-Latest' not in tools
    assert 'not-a-runner' not in tools


@pytest.mark.parametrize('flatpak', [False, True])
def test_probe_runs_in_selected_installation(monkeypatch, tmp_path, flatpak):
    selected = LauncherInstallation('faugus-launcher', FAUGUS_FLATPAK_APP_ID, tmp_path, flatpak)
    monkeypatch.setattr(compatibility, 'faugus_installation', lambda home: selected)
    monkeypatch.setattr(LauncherInstallation, 'command', lambda self: ['available'])
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps([['GE-Test', 'GE-Test']]))
    monkeypatch.setattr(compatibility, 'run_on_host', run)
    tools = compatibility.FaugusCompatibility().discover()
    assert tools[0].value == 'GE-Test'
    assert calls[0][:-3] == selected.python_command()
    assert calls[0][-3] == '-c'


@pytest.mark.parametrize('missing', [False, True])
def test_failed_installation_probe_disables_shared_picker(monkeypatch, tmp_path, missing):
    selected = LauncherInstallation('faugus-launcher', FAUGUS_FLATPAK_APP_ID, tmp_path, True)
    monkeypatch.setattr(compatibility, 'faugus_installation', lambda home: selected)
    monkeypatch.setattr(LauncherInstallation, 'command', lambda self: None if missing else ['available'])
    monkeypatch.setattr(compatibility, 'run_on_host', lambda *a, **kw: subprocess.CompletedProcess([], 1, ''))
    picker = CompatibilityTools(compatibility.FaugusCompatibility(), guidance='Next launch')
    picker.refresh()
    assert picker._error
    assert not picker._tools


@pytest.mark.parametrize('runner', ['Steam', 'Linux-Native'])
def test_fresh_runner_blocks_changes_even_when_cached_game_is_windows(tmp_path, runner):
    path = _faugus(tmp_path, [_game()])
    manager = _manager(tmp_path)
    manager.refresh()
    path.write_text(json.dumps([_game(runner=runner)]))
    before = path.read_bytes()
    assert not manager.set_game_compat_tool('e33', '').ok
    assert path.read_bytes() == before


def test_unknown_runner_kept_visible_and_empty_means_umu(tmp_path):
    _faugus(tmp_path, [_game(runner='Removed-Version')])
    manager = _manager(tmp_path)
    manager.refresh()
    manager.compatibility.refresh()
    field = manager.compatibility.field(manager.row('e33').game)
    assert field.value == 'Removed-Version'
    assert dict(field.choices)['Removed-Version'] == 'Removed-Version (not listed)'
    assert dict(field.choices)[''] == 'UMU-Proton Latest'
    assert manager.set_game_compat_tool('e33', '').ok


def test_invalid_other_entry_is_not_lost(tmp_path):
    path = _faugus(tmp_path, [_game(), None])
    manager = _manager(tmp_path)
    manager.refresh()
    before = path.read_bytes()
    assert not manager.set_game_compat_tool('e33', '').ok
    assert path.read_bytes() == before


@pytest.mark.parametrize('native_installed', [False, True])
def test_coexisting_installations_write_only_selected_library(tmp_path, monkeypatch, native_installed):
    from integrations.launchers import installation

    monkeypatch.setattr(installation, 'host_command_path',
                        lambda name: '/usr/bin/faugus-launcher' if native_installed else None)
    native = _faugus(tmp_path, [_game(runner='Native-Keep')])
    sandbox = tmp_path / '.var/app' / FAUGUS_FLATPAK_APP_ID / 'data/faugus-launcher/games.json'
    sandbox.parent.mkdir(parents=True)
    sandbox.write_text(json.dumps([_game(runner='Flatpak-Keep')]))
    selected, untouched = (native, sandbox) if native_installed else (sandbox, native)
    before = untouched.read_bytes()
    manager = _manager(tmp_path)
    manager.refresh()
    assert manager.set_game_compat_tool('e33', 'Proton-GE Latest').ok
    assert json.loads(selected.read_text())[0]['runner'] == 'Proton-GE Latest'
    assert untouched.read_bytes() == before
