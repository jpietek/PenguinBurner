"""One installation owns a launcher's discovery, writes, and execution."""
from __future__ import annotations

import subprocess

import pytest

from integrations.faugus.paths import faugus_installation
from integrations.heroic.paths import heroic_installation
from integrations.launchers import installation
from integrations.lutris.paths import lutris_config_root, lutris_installation
from integrations.steam.cdp import cdp_marker_path
from integrations.steam.users import steam_installation

LAUNCHERS = [
    (heroic_installation, '.config/heroic', '.var/app/com.heroicgameslauncher.hgl/config/heroic', 'config.json'),
    (lutris_installation, '.local/share/lutris', '.var/app/net.lutris.Lutris/data/lutris', 'pga.db'),
    (steam_installation, '.local/share/Steam', '.var/app/com.valvesoftware.Steam/.local/share/Steam', 'steamapps'),
    (faugus_installation, '.local/share/faugus-launcher',
     '.var/app/io.github.Faugus.faugus-launcher/data/faugus-launcher', 'games.json'),
]


def configured(path, marker):
    path.mkdir(parents=True, exist_ok=True)
    if marker == 'steamapps':
        (path / marker).mkdir()
    else:
        (path / marker).write_text('{}')


@pytest.mark.parametrize('resolver,native,flatpak,marker', LAUNCHERS)
@pytest.mark.parametrize('native_installed', [False, True])
def test_selection_and_dispatch_stay_together(tmp_path, monkeypatch, resolver, native, flatpak, marker, native_installed):
    monkeypatch.setattr(installation, 'host_command_path', lambda name: f'/usr/bin/{name}' if native_installed else None)
    monkeypatch.setattr(installation, 'run_on_host', lambda argv: subprocess.CompletedProcess(argv, 0))
    configured(tmp_path / flatpak, marker)
    selected = resolver(tmp_path)
    assert selected.flatpak  # an unconfigured native copy must not capture this library
    assert selected.command('launch') == ['flatpak', 'run', selected.app_id, 'launch']
    assert selected.python_command('-c', 'probe') == ['flatpak', 'run', '--command=python3', selected.app_id, '-c', 'probe']
    assert selected.wrapper.startswith(str(selected.app_home))
    configured(tmp_path / native, marker)
    selected = resolver(tmp_path)
    assert selected.flatpak is not native_installed
    assert selected.root == tmp_path / (native if native_installed else flatpak)
    if native_installed:
        assert selected.command('launch') == [f'/usr/bin/{selected.name}', 'launch']
        assert selected.wrapper == 'PENGUIN_BURNER'


@pytest.mark.parametrize('resolver,native,flatpak,marker', LAUNCHERS)
def test_missing_native_executable_never_launches_different_library(tmp_path, monkeypatch, resolver, native, flatpak, marker):
    configured(tmp_path / native, marker)
    monkeypatch.setattr(installation, 'host_command_path', lambda name: None)
    monkeypatch.setattr(installation, 'run_on_host', lambda argv: pytest.fail('must not fall back to Flatpak'))
    selected = resolver(tmp_path)
    assert selected.root == tmp_path / native
    assert selected.command('launch') is None


def test_flatpak_lutris_config_inheritance_does_not_use_native_settings(tmp_path, monkeypatch):
    configured(tmp_path / LAUNCHERS[1][2], 'pga.db')
    (tmp_path / '.config/lutris').mkdir(parents=True)
    selected = lutris_installation(tmp_path)
    assert lutris_config_root(tmp_path) == selected.root
    sandbox_config = selected.app_home / 'config/lutris'
    sandbox_config.mkdir(parents=True)
    assert lutris_config_root(tmp_path) == sandbox_config


def test_flatpak_steam_marker_is_in_selected_installation(tmp_path):
    root = tmp_path / LAUNCHERS[2][2]
    configured(root, 'steamapps')
    assert cdp_marker_path(tmp_path) == root / '.cef-enable-remote-debugging'


@pytest.mark.parametrize('running,flatpak,allowed', [
    ('native', False, True), ('flatpak', True, True), ('', True, True),
    ('flatpak', False, False), ('native', True, False),
    ('native flatpak', True, False), ('unknown', False, False),
])
def test_steam_global_control_rejects_other_installation(tmp_path, monkeypatch, running, flatpak, allowed):
    from integrations.steam import process

    selected = installation.LauncherInstallation('steam', 'com.valvesoftware.Steam', tmp_path, flatpak)
    monkeypatch.setattr(process, 'steam_installation', lambda home: selected)
    def probe(argv, *, capture):
        assert capture
        return subprocess.CompletedProcess(argv, 0, running)
    monkeypatch.setattr(process, 'run_on_host', probe)
    assert process.steam_matches_installation(tmp_path) is allowed


def test_steam_cdp_rejects_wrong_client_before_opening_connection(tmp_path, monkeypatch):
    from integrations.steam import manager

    monkeypatch.setattr(manager, 'steam_matches_installation', lambda home: False)
    monkeypatch.setattr(manager, 'SteamCdpClient', lambda **kwargs: pytest.fail('wrong client opened'))
    integration = manager.SteamIntegrationManager(home=tmp_path)
    result = integration.set_game_compat_tool('123', '')
    assert not result.ok
    assert 'other Steam installation' in result.message
    assert not integration.cdp_ready()


@pytest.mark.parametrize('name,app_id', [('lutris', 'net.lutris.Lutris'), ('steam', 'com.valvesoftware.Steam')])
def test_flatpak_launch_uses_own_uri_or_arguments(tmp_path, monkeypatch, name, app_id):
    from integrations.lutris import process as lutris
    from integrations.steam import process as steam

    selected = installation.LauncherInstallation(name, app_id, tmp_path, True, tmp_path)
    process = lutris if name == 'lutris' else steam
    monkeypatch.setattr(process, f'{name}_installation', lambda home: selected)
    monkeypatch.setattr(installation, 'run_on_host', lambda argv: subprocess.CompletedProcess(argv, 0))
    monkeypatch.setattr(steam, 'steam_matches_installation', lambda home: True)
    calls = []
    monkeypatch.setattr(process, 'start_on_host', lambda argv: calls.append(argv) or True)
    assert getattr(process, f'launch_{name}_game')('27', home=tmp_path)
    suffix = ['lutris:rungameid/27'] if name == 'lutris' else ['-applaunch', '27']
    assert calls == [['flatpak', 'run', app_id, *suffix]]


def test_real_steam_identity_probe_parses_flatpak_environment(tmp_path):
    from integrations.steam.process import _STEAM_INSTALLATIONS

    process = tmp_path / '12'
    process.mkdir()
    (process / 'comm').write_text('steam\n')
    (process / 'environ').write_bytes(b'FLATPAK_ID=com.valvesoftware.Steam\0SECRET=never printed\0')
    command = _STEAM_INSTALLATIONS.replace("Path('/proc')", f'Path({str(tmp_path)!r})')
    result = subprocess.run(['/usr/bin/python3', '-c', command], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'flatpak'
    (process / 'environ').write_bytes(b'SECRET=never printed\0')
    result = subprocess.run(['/usr/bin/python3', '-c', command], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'native'
