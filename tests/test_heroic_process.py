"""Starting, watching and stopping a Heroic game."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from integrations.heroic import paths, process
from integrations.launchers import installation, wrapped_sessions
from integrations.launchers.wrapped_sessions import LauncherSessions


@pytest.fixture(autouse=True)
def isolated_installation(tmp_path, monkeypatch):
    resolver = paths.heroic_installation
    monkeypatch.setattr(process, "heroic_installation", lambda home=None: resolver(home or tmp_path))
    installation.native_command.cache_clear()
    yield
    installation.native_command.cache_clear()


def _installed(monkeypatch, *, native: bool = True):
    monkeypatch.setattr(
        installation, "host_command_path", lambda name: name if native else None
    )
    monkeypatch.setattr(
        installation, "run_on_host", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)
    )


def test_a_game_is_started_through_heroics_own_launch_url(monkeypatch) -> None:
    """Heroic builds the command from the game's config, wrappers included."""
    _installed(monkeypatch)
    started: list[list[str]] = []
    monkeypatch.setattr(process, "launch_with_current_settings", lambda cmd, **kwargs: started.append(cmd) or True)

    assert process.launch_heroic_game("legendary", "Turkey") is True
    assert started == [
        ["heroic", "--no-gui", "heroic://launch/legendary/Turkey?gui=false"]
    ]


def test_the_launch_url_asks_heroic_to_stay_out_of_the_way(monkeypatch) -> None:
    """A running Heroic parsed its own argv at startup, so the flag alone
    would raise its window over the game."""
    _installed(monkeypatch)

    command = process.launch_command("legendary", "Turkey")

    assert command is not None
    assert command[-1].endswith("?gui=false")


def test_a_flatpak_heroic_is_asked_the_same_thing(monkeypatch) -> None:
    _installed(monkeypatch, native=False)

    assert process.launch_command("gog", "1454") == [
        "flatpak",
        "run",
        "com.heroicgameslauncher.hgl",
        "--no-gui",
        "heroic://launch/gog/1454?gui=false",
    ]


def test_an_unusable_name_never_reaches_the_command_line(monkeypatch) -> None:
    _installed(monkeypatch)

    for runner, app_name in (("", "Turkey"), ("legendary", ""), ("legendary", "a/b")):
        assert process.launch_command(runner, app_name) is None


def test_running_sessions_are_read_from_the_host_probe(
    monkeypatch,
) -> None:
    """Session identities remain usable after the wrapper's exec."""
    monkeypatch.setattr(
        wrapped_sessions,
        "run_on_host",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps({"sessions": [
                (4210, "heroic:Turkey"), (4211, "heroic:Turkey"), (4300, "lutris:27")
            ], "unreadable": []})
        ),
    )

    sessions = process.probe_heroic_sessions()
    assert sessions is not None
    assert sessions.wrapped == {"Turkey": (4210, 4211)}


def test_a_failed_probe_says_so_instead_of_reporting_nothing_running(
    monkeypatch,
) -> None:
    monkeypatch.setattr(wrapped_sessions, "run_on_host", lambda *args, **kwargs: None)

    assert process.probe_heroic_sessions() is None


def test_a_game_id_with_a_space_survives_the_host_probe(monkeypatch) -> None:
    """The environment carries the decoded game key, not its flag encoding."""
    monkeypatch.setattr(
        wrapped_sessions,
        "run_on_host",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps({"sessions": [(9, "heroic:Sid Meier")], "unreadable": []})
        ),
    )

    sessions = process.probe_heroic_sessions()
    assert sessions is not None
    assert sessions.wrapped == {"Sid Meier": (9,)}


@pytest.mark.parametrize("launcher", ["heroic", "faugus"])
def test_stop_revalidates_the_game_before_signalling(launcher) -> None:
    env = dict(os.environ, PENGUIN_BURNER_GAME_KEY=f'{launcher}:StopIdentityProbe',
               PENGUIN_BURNER_SESSION_ID='stop-identity')
    with subprocess.Popen(['/bin/sleep', '30'], env=env) as child:
        try:
            assert not wrapped_sessions.stop_wrapped_session(child.pid, f'{launcher}:WrongGame')
            assert child.poll() is None
            assert wrapped_sessions.stop_wrapped_session(child.pid, f'{launcher}:StopIdentityProbe')
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


def test_unwrapped_launch_probe_excludes_helpers_and_orphans(tmp_path, monkeypatch):
    """Heroic's runner owns the lifetime; inherited app ids are not sessions."""
    def proc(pid, parent, command, env=b''):
        directory = tmp_path / str(pid)
        directory.mkdir(exist_ok=True)
        (directory / 'environ').write_bytes(env)
        (directory / 'cmdline').write_bytes(command + b'\0')
        (directory / 'stat').write_text(f'{pid} (process name) S {parent} 0 0')

    identity = b'HEROIC_APP_NAME=Turkey\0HEROIC_APP_RUNNER=legendary\0'
    proc(10, 1, b'/app/bin/heroic/heroic')
    proc(20, 10, b'/app/bin/legendary', identity)
    proc(21, 20, b'/bin/game', identity)
    proc(22, 1, b'/bin/wineserver', identity)
    proc(23, 10, b'/bin/unrelated', b'HEROIC_APP_NAME=Turkey\0')

    def probe(command, **kwargs):
        command[-3] = str(tmp_path)
        return subprocess.run(command, capture_output=True, text=True, check=False)

    monkeypatch.setattr(wrapped_sessions, 'run_on_host', probe)
    assert process.probe_heroic_sessions() == LauncherSessions(external={'Turkey': (20,)})

    # After the actual runner exits, a surviving child belongs to the reaper.
    proc(21, 1, b'/bin/game', identity)
    (tmp_path / '20/environ').unlink()
    assert process.probe_heroic_sessions() == LauncherSessions()


@pytest.mark.parametrize("launcher", ["heroic", "faugus"])
def test_external_session_cannot_be_stopped_and_wrapped_session_takes_precedence(monkeypatch, launcher):
    from integrations.launchers.library_source import WrapperLibrarySource
    from integrations.launchers.registry import build_sources

    source = next(source for source in build_sources() if source.launcher_id == launcher)
    assert isinstance(source, WrapperLibrarySource)
    sessions = LauncherSessions(external={'Turkey': (20,)})
    monkeypatch.setattr(source, 'probe_sessions', lambda **kwargs: sessions)
    signalled = []
    monkeypatch.setattr('integrations.launchers.library_source.stop_wrapped_session', lambda pid, game: signalled.append((pid, game)) or True)
    assert source.running_game_ids() == frozenset({'Turkey'})
    assert source.external_game_ids() == frozenset({'Turkey'})
    assert not source.stop('Turkey')[0]
    assert not signalled

    sessions = LauncherSessions(wrapped={'Turkey': (21, 22)}, external={'Turkey': (20,)})
    assert source.running_game_ids() == frozenset({'Turkey'})
    assert source.external_game_ids() == frozenset()
    assert source.stop('Turkey')[0]
    assert signalled == [(21, f'{launcher}:Turkey'), (22, f'{launcher}:Turkey')]
    monkeypatch.setattr('integrations.launchers.library_source.stop_wrapped_session', lambda pid, key: pid == 21)
    assert not source.stop('Turkey')[0]


def test_real_unwrapped_heroic_child_is_observed_until_exit():
    """Exercise real /proc without launching a game or changing user settings."""
    import sys

    code = '''
import os, subprocess, sys
child = subprocess.Popen(['/bin/sleep', '30'], env={
    **os.environ, 'HEROIC_APP_NAME': 'UnwrappedProbe', 'HEROIC_APP_RUNNER': 'legendary',
})
print(child.pid, flush=True)
try:
    sys.stdin.readline()
finally:
    child.terminate()
    child.wait()
'''
    with subprocess.Popen(
        ['heroic', '-c', code], executable=sys.executable,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    ) as heroic:
        try:
            assert heroic.stdout is not None
            pid = int(heroic.stdout.readline())
            sessions = process.probe_heroic_sessions()
            assert sessions is not None
            assert sessions.external['UnwrappedProbe'] == (pid,)
            assert 'UnwrappedProbe' not in sessions.wrapped
        finally:
            heroic.communicate('\n', timeout=5)
    sessions = process.probe_heroic_sessions()
    assert sessions is not None
    assert 'UnwrappedProbe' not in sessions.external


@pytest.mark.parametrize("native", [False, True])
def test_leftover_configs_and_executable_select_the_same_installation(tmp_path, monkeypatch, native):
    _installed(monkeypatch, native=native)
    for root in (tmp_path / '.config/heroic', tmp_path / paths.HEROIC_FLATPAK_DIRNAME):
        root.mkdir(parents=True)
        (root / 'config.json').write_text('{}')
    root = paths.heroic_config_root(tmp_path)
    expected = tmp_path / ('.config/heroic' if native else paths.HEROIC_FLATPAK_DIRNAME)
    assert root == expected
    launches = []
    monkeypatch.setattr(process, 'launch_with_current_settings',
                        lambda command, **kwargs: launches.append((command, kwargs)) or True)
    assert process.launch_heroic_game('legendary', 'Turkey', home=tmp_path)
    assert launches[0][0][0] == ('heroic' if native else 'flatpak')
    assert launches[0][1]['home'] == tmp_path


def test_flatpak_config_does_not_launch_an_unconfigured_native_install(tmp_path, monkeypatch):
    _installed(monkeypatch)
    root = tmp_path / paths.HEROIC_FLATPAK_DIRNAME
    root.mkdir(parents=True)
    (root / 'config.json').write_text('{}')
    command = process.launch_command('legendary', 'Turkey', home=tmp_path)
    assert command is not None and command[:3] == ['flatpak', 'run', "com.heroicgameslauncher.hgl"]


def test_missing_selected_executable_never_falls_back_to_other_settings(tmp_path, monkeypatch):
    _installed(monkeypatch, native=False)
    root = tmp_path / '.config/heroic'
    root.mkdir(parents=True)
    (root / 'config.json').write_text('{}')
    assert not process.heroic_available(tmp_path)
    assert process.launch_command('legendary', 'Turkey', home=tmp_path) is None


def test_rescan_reselects_installation_after_native_removal(tmp_path, monkeypatch):
    from integrations.heroic.manager import HeroicIntegrationManager

    _installed(monkeypatch)
    for root in (tmp_path / '.config/heroic', tmp_path / paths.HEROIC_FLATPAK_DIRNAME):
        root.mkdir(parents=True)
        (root / 'config.json').write_text('{}')
    assert paths.heroic_config_root(tmp_path) == tmp_path / '.config/heroic'
    _installed(monkeypatch, native=False)
    HeroicIntegrationManager(home=tmp_path).refresh()
    assert paths.heroic_config_root(tmp_path) == tmp_path / paths.HEROIC_FLATPAK_DIRNAME


def test_stop_reaches_handoff_children_but_not_helpers_or_external_games():
    from integrations.heroic.library_source import HeroicLibrarySource

    # Simulate a wrapper that forks two successors and a detached PB helper.
    # All retain its telemetry PID; only the successors retain SESSION_ID.
    code = '''
import os, subprocess, sys
env = dict(os.environ, PENGUIN_BURNER_GAME_KEY='heroic:StopHandoffProbe',
           PENGUIN_BURNER_TELEMETRY_SESSION=str(os.getpid()),
           PENGUIN_BURNER_SESSION_ID='stop-handoff-probe')
for _ in range(2):
    p = subprocess.Popen(['/bin/sleep', '30'], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(p.pid, flush=True)
'''
    children = []
    helper_env = dict(os.environ, PENGUIN_BURNER_GAME_KEY='heroic:StopHandoffProbe',
                      PENGUIN_BURNER_TELEMETRY_SESSION='1')
    helper_env.pop('PENGUIN_BURNER_SESSION_ID', None)
    with subprocess.Popen(['/bin/sleep', '30'], env=helper_env) as helper:
        try:
            parent = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True)
            children = [int(pid) for pid in parent.stdout.splitlines()]
            sessions = process.probe_heroic_sessions()
            assert sessions is not None
            assert set(sessions.wrapped['StopHandoffProbe']) == set(children)
            assert helper.pid not in sessions.wrapped['StopHandoffProbe']
            source = HeroicLibrarySource()
            import select

            with_fds = [os.pidfd_open(pid) for pid in children]
            try:
                assert source.stop('StopHandoffProbe')[0]
                poller = select.poll()
                for fd in with_fds:
                    poller.register(fd, select.POLLIN)
                ready = set()
                while len(ready) < len(with_fds):
                    events = poller.poll(3000)
                    assert events, 'Stop did not terminate the surviving game children'
                    for fd, _event in events:
                        ready.add(fd)
                        poller.unregister(fd)
            finally:
                for fd in with_fds:
                    os.close(fd)
            assert helper.poll() is None
        finally:
            helper.terminate()
            helper.wait(timeout=5)
            for pid in children:
                try:
                    os.kill(pid, 15)
                except ProcessLookupError:
                    pass
