"""Starting, observing and stopping a Faugus game."""

from __future__ import annotations

import json
import subprocess

from integrations.faugus import process
from integrations.launchers import wrapped_sessions
from integrations.launchers.wrapped_sessions import LauncherSessions


def _available(monkeypatch) -> None:
    monkeypatch.setattr(process, "host_has_command", lambda name: name == process.COMMAND)


def test_a_game_is_started_through_faugus_own_command(monkeypatch) -> None:
    _available(monkeypatch)
    started: list[list[str]] = []
    monkeypatch.setattr(process, "start_on_host", lambda cmd: started.append(cmd) or True)

    assert process.launch_faugus_game("expedition-33")
    assert started == [["faugus-launcher", "--game", "expedition-33"]]


def test_a_flatpak_faugus_is_asked_the_same_thing(monkeypatch) -> None:
    monkeypatch.setattr(process, "host_has_command", lambda name: name == "flatpak")
    monkeypatch.setattr(
        process,
        "run_on_host",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0),
    )

    assert process.launch_command("expedition-33") == [
        "flatpak",
        "run",
        process.FLATPAK_APP_ID,
        "--game",
        "expedition-33",
    ]


def test_an_unusable_id_never_reaches_the_command_line(monkeypatch) -> None:
    _available(monkeypatch)

    assert process.launch_command("../elsewhere") is None
    assert process.launch_command("--version") is None
    assert process.launch_command("  ") is None


def test_wrapped_sessions_are_read_from_the_host_probe(monkeypatch) -> None:
    """Session identity survives the exec that replaces the wrapper."""
    monkeypatch.setattr(
        wrapped_sessions,
        "run_on_host",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "sessions": [
                        (4210, "faugus:expedition-33"),
                        (4211, "faugus:expedition-33"),
                        (4300, "heroic:Turkey"),
                    ],
                    "external": [],
                    "unreadable": [],
                }
            ),
        ),
    )

    sessions = process.probe_faugus_sessions()

    assert sessions is not None
    assert sessions.wrapped == {"expedition-33": (4210, 4211)}
    # Another launcher's session is not ours to report or to stop.
    assert sessions.external == {}


def test_a_failed_probe_says_so_instead_of_reporting_nothing_running(
    monkeypatch,
) -> None:
    monkeypatch.setattr(wrapped_sessions, "run_on_host", lambda *args, **kwargs: None)

    assert process.probe_faugus_sessions() is None


def _proc(root, pid, env=b"", *, state="S"):
    directory = root / str(pid)
    directory.mkdir(exist_ok=True)
    (directory / "environ").write_bytes(env)
    (directory / "stat").write_text(f"{pid} (process name) {state} 1 0 0")


def _probe(monkeypatch, root):
    def run(command, **kwargs):
        # argv is [python, -c, probe, /proc, game key env, external env]
        command[-3] = str(root)
        return subprocess.run(command, capture_output=True, text=True, check=False)

    monkeypatch.setattr(wrapped_sessions, "run_on_host", run)


def test_a_game_faugus_started_without_us_is_still_observed(tmp_path, monkeypatch):
    """Otherwise Play sits on "Starting…" until the startup timeout."""
    _proc(tmp_path, 10, b"FAUGUSID=expedition-33\0")
    _probe(monkeypatch, tmp_path)

    assert process.probe_faugus_sessions() == LauncherSessions(
        external={"expedition-33": (10,)}
    )


def test_a_wrapped_game_is_not_also_counted_as_an_unwrapped_one(tmp_path, monkeypatch):
    """Faugus stamps FAUGUSID on the whole tree, wrapper session included."""
    wrapped = (
        b"FAUGUSID=expedition-33\0"
        b"PENGUIN_BURNER_TELEMETRY_SESSION=10\0"
        b"PENGUIN_BURNER_GAME_KEY=faugus:expedition-33\0"
    )
    _proc(tmp_path, 10, wrapped)
    # The game itself, which inherited both but leads no session of its own.
    _proc(tmp_path, 11, b"FAUGUSID=expedition-33\0PENGUIN_BURNER_TELEMETRY_SESSION=10\0")
    _probe(monkeypatch, tmp_path)

    assert process.probe_faugus_sessions() == LauncherSessions(
        wrapped={"expedition-33": (10,)}
    )


def test_a_zombie_holding_the_marker_is_not_a_running_game(tmp_path, monkeypatch):
    _proc(tmp_path, 10, b"FAUGUSID=expedition-33\0", state="Z")
    _probe(monkeypatch, tmp_path)

    assert process.probe_faugus_sessions() == LauncherSessions()


def test_an_external_session_cannot_be_stopped_but_a_wrapped_one_can(monkeypatch):
    from integrations.faugus.library_source import FaugusLibrarySource

    source = FaugusLibrarySource.__new__(FaugusLibrarySource)
    source._running_pids = ()
    source._external_games = frozenset()
    monkeypatch.setattr(
        "integrations.faugus.library_source.probe_faugus_sessions",
        lambda **kwargs: LauncherSessions(external={"expedition-33": (10,)}),
    )

    stopped, message = source.stop("expedition-33")

    assert not stopped
    assert "no running session" in message
    # Still reported as running, so the button stops saying "Starting…".
    assert source.running_game_ids() == frozenset({"expedition-33"})
    assert source.external_game_ids() == frozenset({"expedition-33"})
