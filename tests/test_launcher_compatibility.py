"""Compatibility selection uses one UI/write component and launcher-owned storage."""
from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from test_faugus_manager import _faugus, _game
from test_faugus_manager import _manager as faugus_manager
from test_heroic_manager import _manager as heroic_manager
from test_lutris_manager import _manager as lutris_manager

from integrations.faugus.library_source import FaugusLibrarySource
from integrations.heroic import compat_probe
from integrations.heroic import compatibility as heroic_compat
from integrations.heroic.library_source import HeroicLibrarySource
from integrations.launchers.compatibility import CompatibilityTool
from integrations.lutris import compatibility as lutris_compat
from integrations.lutris.library_source import LutrisLibrarySource


@pytest.fixture(autouse=True)
def _launchers_installed_natively(native_launcher_installed) -> None:
    """Probe the native installation unless a test asks for the Flatpak one."""


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return path


@pytest.fixture(params=['heroic', 'lutris', 'faugus'])
def setup(request, tmp_path, monkeypatch):
    if request.param == 'heroic':
        manager = heroic_manager(tmp_path)
        source = HeroicLibrarySource(manager, home=tmp_path)
        game_id = 'Turkey'
        root = tmp_path / '.config/heroic'
        binary = executable(root / 'tools/proton/GE-Test/proton')
        value = str(binary)
        path = root / 'GamesConfig/Turkey.json'
        document = {'version': 'v0', 'Turkey': {'winePrefix': '/keep/prefix', 'wrapperOptions': []}}
        path.write_text(json.dumps(document))
    elif request.param == 'faugus':
        document = [_game(runner=''), _game(gameid='other', runner='Keep-Me')]
        path = _faugus(tmp_path, document)
        manager = faugus_manager(tmp_path)
        source = FaugusLibrarySource(manager, home=tmp_path)
        game_id = 'e33'
        value = 'GE-Test'
        executable(tmp_path / '.local/share/Steam/compatibilitytools.d/GE-Test/proton')
    else:
        manager = lutris_manager(tmp_path)
        source = LutrisLibrarySource(manager, home=tmp_path)
        game_id = '27'
        value = 'GE-Test'
        path = manager.row(game_id).game.config_path
        document = yaml.safe_load(path.read_text())
        document['wine'] = {'custom_wine_path': '/keep/custom', 'dxvk': True}
        path.write_text(yaml.safe_dump(document))
        monkeypatch.setattr(manager.compatibility.backend, 'discover',
                            lambda: (CompatibilityTool(value, value),))
    monkeypatch.setattr(source, 'probe_can_launch', lambda: False)
    source.refresh()
    return manager, source, game_id, value, path, document


def read_config(path):
    return json.loads(path.read_text()) if path.suffix == '.json' else yaml.safe_load(path.read_text())


def test_selection_and_default_preserve_other_settings(setup):
    manager, source, game_id, value, path, original = setup
    field = manager.compatibility.field(manager.row(game_id).game)
    assert field.enabled and field.value == ''
    assert value in dict(field.choices)
    assert manager.set_game_compat_tool(game_id, value).ok
    assert manager.compatibility.field(manager.row(game_id).game).value == value
    changed = read_config(path)
    if source.launcher_id == 'heroic':
        record = changed[game_id].pop('wineVersion')
        assert record == {'bin': value, 'name': 'GE-Test', 'type': 'proton'}
    elif source.launcher_id == 'faugus':
        assert changed[0]['runner'] == value
        changed[0]['runner'] = ''
    else:
        assert changed['wine'].pop('version') == value
    assert changed == original
    assert manager.set_game_compat_tool(game_id, '').ok
    assert read_config(path) == original


def test_removed_tool_rejected_without_mutation(setup, monkeypatch):
    manager, _, game_id, value, path, _ = setup
    before = path.read_bytes()
    monkeypatch.setattr(manager.compatibility.backend, 'discover', lambda: ())
    result = manager.set_game_compat_tool(game_id, value)
    assert not result.ok and 'unavailable' in result.message
    assert path.read_bytes() == before


def test_discovery_failure_disables_picker_without_losing_selection(setup, monkeypatch):
    manager, _, game_id, value, path, _ = setup
    assert manager.set_game_compat_tool(game_id, value).ok
    before = path.read_bytes()
    def fail():
        raise ValueError('Catalogue unavailable')
    monkeypatch.setattr(manager.compatibility.backend, 'discover', fail)
    manager.compatibility.refresh()
    field = manager.compatibility.field(manager.row(game_id).game)
    assert not field.enabled and field.value == value
    assert value in dict(field.choices)
    assert 'Catalogue unavailable' in field.subtitle
    assert not manager.set_game_compat_tool(game_id, value).ok
    assert path.read_bytes() == before
    # Restoring inheritance does not require discovery to work.
    assert manager.set_game_compat_tool(game_id, '').ok


@pytest.mark.parametrize('broken', ['[', '[1, 2]', 'null'])
def test_invalid_document_is_never_overwritten(setup, broken):
    manager, _, game_id, value, path, _ = setup
    path.write_text(broken)
    result = manager.set_game_compat_tool(game_id, value)
    assert not result.ok
    assert path.read_text() == broken
    assert not manager.compatibility.field(manager.row(game_id).game).enabled


def test_native_games_disable_version_selection(setup):
    manager, source, game_id, value, path, _ = setup
    row = manager.row(game_id)
    native_runner = 'Linux-Native' if source.launcher_id == 'faugus' else 'linux'
    game = replace(row.game, **({'platform': 'linux'} if source.launcher_id == 'heroic' else {'runner': native_runner}))
    if source.launcher_id == 'faugus':
        document = read_config(path)
        document[0]['runner'] = native_runner
        path.write_text(json.dumps(document))
    manager._rows[game_id] = replace(row, game=game)
    before = path.read_bytes()
    assert not manager.compatibility.field(manager.row(game_id).game).enabled
    assert not manager.set_game_compat_tool(game_id, value).ok
    assert path.read_bytes() == before


def test_launchers_use_existing_qt_picker(setup, qapp, qtbot):
    from ui.components.game_library_panel import GameLibraryPanel
    from ui.qt import import_qt

    manager, source, game_id, value, path, original = setup
    QtCore, QtGui, QtWidgets, _ = import_qt()
    panel = GameLibraryPanel(QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, sources=(source,))
    qtbot.addWidget(panel.widget)
    panel.ensure_scanned()
    qtbot.waitUntil(lambda: bool(panel._games), timeout=5000)
    panel._select_key(f'{source.launcher_id}:{game_id}')
    combo = panel._fields['compat_tool']['control']
    assert isinstance(combo, QtWidgets.QComboBox)
    assert combo.isEnabled()
    combo.setCurrentIndex(combo.findData(value))
    qtbot.waitUntil(lambda: panel._setting_thread is None, timeout=5000)
    assert manager.compatibility.field(manager.row(game_id).game).value == value
    assert 'next launch' in panel.status_label.text()
    combo.setCurrentIndex(combo.findData(''))
    qtbot.waitUntil(lambda: panel._setting_thread is None, timeout=5000)
    assert read_config(path) == original


def test_heroic_catalogue_records_wine_helpers_and_filters_missing_tools(tmp_path):
    root = tmp_path / '.config/heroic'
    root.mkdir(parents=True)
    (root / 'config.json').write_text(json.dumps({'defaultSettings': {}}))
    wine = executable(root / 'tools/wine/Wine-Test/bin/wine')
    server = executable(wine.parent / 'wineserver')
    (wine.parent.parent / 'lib64').mkdir()
    (root / 'tools/proton/unfinished').mkdir(parents=True)
    records = compat_probe.discover({'root': str(root), 'home': str(tmp_path), 'system': False})
    assert len(records) == 1
    assert records[0]['bin'] == str(wine)
    assert records[0]['wineserver'] == str(server)
    assert records[0]['lib'] and records[0]['lib32'] == ''


def test_heroic_catalogue_respects_valve_proton_setting_and_custom_paths(tmp_path):
    root = tmp_path / '.config/heroic'
    root.mkdir(parents=True)
    config = root / 'config.json'
    steam = tmp_path / 'Steam'
    valve = executable(steam / 'steamapps/common/Proton/proton')
    ge = executable(steam / 'compatibilitytools.d/GE/proton')
    custom = executable(tmp_path / 'custom/bin/wine')
    defaults = {'customWinePaths': [str(custom), '/nonexistent/wine']}
    config.write_text(json.dumps({'defaultSettings': defaults}))
    request = {'root': str(root), 'home': str(tmp_path), 'steam_roots': [str(steam)], 'system': False}
    assert {x['bin'] for x in compat_probe.discover(request)} == {str(ge), str(custom)}
    defaults['showValveProton'] = True
    config.write_text(json.dumps({'defaultSettings': defaults}))
    assert {x['bin'] for x in compat_probe.discover(request)} == {str(ge), str(custom), str(valve)}


def test_flatpak_discovery_runs_in_heroic_namespace(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps([
            {'bin': '/app/bin/wine', 'name': 'Sandbox Wine', 'type': 'wine'},
        ]))
    from integrations.launchers.installation import LauncherInstallation
    monkeypatch.setattr(heroic_compat, 'heroic_installation', lambda home: LauncherInstallation(
        'heroic', 'com.heroicgameslauncher.hgl', Path('/tmp'), flatpak=True))
    monkeypatch.setattr(heroic_compat, 'run_on_host', run)
    tools = heroic_compat.HeroicCompatibility().discover()
    assert calls[0][:4] == ['flatpak', 'run', '--command=python3', 'com.heroicgameslauncher.hgl']
    assert tools[0].value == '/app/bin/wine'


def test_lutris_catalogue_uses_launcher_api(monkeypatch):
    def run(command, **kwargs):
        assert 'get_installed_wine_versions' in command[2]
        return subprocess.CompletedProcess(command, 0, json.dumps({
            'versions': ['GE-Installed', 'system', 'ge-proton'], 'default': 'ge-proton',
        }))
    monkeypatch.setattr(lutris_compat, 'run_on_host', run)
    backend = lutris_compat.LutrisCompatibility()
    assert {tool.value for tool in backend.discover()} == {'GE-Installed', 'system', 'ge-proton'}
    assert backend.default == 'GE-Proton Latest'


def test_native_discovery_uses_host_python(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '[]')
    from integrations.launchers.installation import LauncherInstallation
    monkeypatch.setattr(heroic_compat, 'heroic_installation', lambda home: LauncherInstallation(
        'heroic', 'com.heroicgameslauncher.hgl', Path('/tmp')))
    monkeypatch.setattr(heroic_compat, 'run_on_host', run)
    assert heroic_compat.HeroicCompatibility().discover() == ()
    assert calls[0][:2] == ['/usr/bin/python3', '-c']


def test_stale_native_config_does_not_capture_flatpak_version_write(tmp_path, monkeypatch):
    import shutil

    from integrations.heroic import paths
    from integrations.heroic.manager import HeroicIntegrationManager

    heroic_manager(tmp_path)
    native = tmp_path / '.config/heroic'
    flatpak = tmp_path / paths.HEROIC_FLATPAK_DIRNAME
    shutil.copytree(native, flatpak)
    binary = executable(flatpak / 'tools/proton/Flatpak-GE/proton')
    native_files = {str(p.relative_to(native)): p.read_bytes() for p in native.rglob('*') if p.is_file()}
    from integrations.launchers import installation
    monkeypatch.setattr(installation, 'host_command_path', lambda _: None)
    manager = HeroicIntegrationManager(home=tmp_path, settings_path=tmp_path / 'pb-settings.json')
    try:
        manager.refresh()
        manager.compatibility.refresh()
        assert manager.set_game_compat_tool('Turkey', str(binary)).ok
        saved = json.loads((flatpak / 'GamesConfig/Turkey.json').read_text())
        assert saved['Turkey']['wineVersion']['bin'] == str(binary)
        assert {str(p.relative_to(native)): p.read_bytes() for p in native.rglob('*') if p.is_file()} == native_files
    finally:
        installation.native_command.cache_clear()


def test_lutris_default_resumes_runner_inheritance(tmp_path, monkeypatch):
    manager = lutris_manager(tmp_path)
    runner = tmp_path / '.local/share/lutris/runners/wine.yml'
    runner.parent.mkdir(parents=True)
    runner.write_text(yaml.safe_dump({'wine': {'version': 'Runner-GE'}}))
    monkeypatch.setattr(manager.compatibility.backend, 'discover', lambda: (CompatibilityTool('Game-GE', 'Game-GE'),))
    manager.compatibility.refresh()
    assert 'Runner-GE' in manager.compatibility.field(manager.row('27').game).choices[0][1]
    assert manager.set_game_compat_tool('27', 'Game-GE').ok
    assert manager.set_game_compat_tool('27', '').ok
    assert 'Runner-GE' in manager.compatibility.field(manager.row('27').game).choices[0][1]
    assert 'version' not in read_config(manager.row('27').game.config_path).get('wine', {})


def test_heroic_restoring_default_invalidates_cached_version(tmp_path):
    from integrations.heroic.launch_sync import _settings_fingerprint

    manager = heroic_manager(tmp_path)
    root = tmp_path / '.config/heroic'
    path = root / 'GamesConfig/Turkey.json'
    path.write_text(json.dumps({'Turkey': {'wineVersion': {'bin': '/previous/proton'}}}))
    before = _settings_fingerprint(root, tmp_path)
    assert manager.set_game_compat_tool('Turkey', '').ok
    assert _settings_fingerprint(root, tmp_path) != before


def test_shallow_refresh_does_not_probe_tools(setup, monkeypatch):
    manager, source, *_ = setup
    calls = []
    monkeypatch.setattr(manager.compatibility.backend, 'discover', lambda: calls.append(True) or ())
    source.refresh(deep=False)
    assert calls == []
    source.refresh(deep=True)
    assert calls == [True]


def test_flatpak_lutris_catalogue_queries_sandbox_python(monkeypatch, tmp_path):
    from integrations.launchers.installation import LauncherInstallation

    selected = LauncherInstallation('lutris', 'net.lutris.Lutris', tmp_path, flatpak=True)
    monkeypatch.setattr(lutris_compat, 'lutris_installation', lambda home: selected)
    def run(command, **kwargs):
        assert command[:5] == ['flatpak', 'run', '--command=python3', selected.app_id, '-c']
        assert 'get_installed_wine_versions' in command[5]
        return subprocess.CompletedProcess(command, 0, json.dumps({'versions': ['Sandbox-GE'], 'default': 'Sandbox-GE'}))
    monkeypatch.setattr(lutris_compat, 'run_on_host', run)
    assert [tool.value for tool in lutris_compat.LutrisCompatibility().discover()] == ['Sandbox-GE']
