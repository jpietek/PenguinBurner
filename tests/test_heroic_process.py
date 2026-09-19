"""Starting, watching and stopping a Heroic game."""

from __future__ import annotations

import json
import subprocess

from integrations.heroic import process
from integrations.launchers import host_process as host


def _installed(monkeypatch, *, native: bool = True):
    monkeypatch.setattr(
        process, "host_has_command", lambda name: native or name == "flatpak"
    )
    monkeypatch.setattr(
        process, "run_on_host", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0)
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
        process,
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
    monkeypatch.setattr(process, "run_on_host", lambda *args, **kwargs: None)

    assert process.probe_heroic_sessions() is None


def test_a_game_id_with_a_space_survives_the_host_probe(monkeypatch) -> None:
    """The environment carries the decoded game key, not its flag encoding."""
    monkeypatch.setattr(
        process,
        "run_on_host",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, json.dumps({"sessions": [(9, "heroic:Sid Meier")], "unreadable": []})
        ),
    )

    sessions = process.probe_heroic_sessions()
    assert sessions is not None
    assert sessions.wrapped == {"Sid Meier": (9,)}


def test_stopping_signals_the_wrapper_that_became_the_session(monkeypatch) -> None:
    """The wrapper execs the game, so its pid is the session's own."""
    sent: list[int] = []
    monkeypatch.setattr(host, "running_in_flatpak", lambda: False)
    monkeypatch.setattr(host.os, "kill", lambda pid, _sig: sent.append(pid))

    assert process.stop_heroic_game(4210) is True
    assert sent == [4210]


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
        command[-2] = str(tmp_path)
        return subprocess.run(command, capture_output=True, text=True, check=False)

    monkeypatch.setattr(process, 'run_on_host', probe)
    assert process.probe_heroic_sessions() == process.HeroicSessions(external={'Turkey': (20,)})

    # After the actual runner exits, a surviving child belongs to the reaper.
    proc(21, 1, b'/bin/game', identity)
    (tmp_path / '20/environ').unlink()
    assert process.probe_heroic_sessions() == process.HeroicSessions()


def test_external_session_cannot_be_stopped_and_wrapped_session_takes_precedence(monkeypatch):
    from integrations.heroic import library_source
    from integrations.heroic.library_source import HeroicLibrarySource

    source = HeroicLibrarySource()
    sessions = process.HeroicSessions(external={'Turkey': (20,)})
    monkeypatch.setattr(library_source, 'probe_heroic_sessions', lambda **kwargs: sessions)
    signalled = []
    monkeypatch.setattr(library_source, 'stop_heroic_game', lambda pid: signalled.append(pid) or True)
    assert source.running_game_ids() == frozenset({'Turkey'})
    assert source.external_game_ids() == frozenset({'Turkey'})
    assert not source.stop('Turkey')[0]
    assert not signalled

    sessions = process.HeroicSessions(wrapped={'Turkey': (21,)}, external={'Turkey': (20,)})
    assert source.running_game_ids() == frozenset({'Turkey'})
    assert source.external_game_ids() == frozenset()
    assert source.stop('Turkey')[0]
    assert signalled == [21]


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
