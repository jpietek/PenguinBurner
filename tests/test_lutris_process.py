"""Starting, watching and stopping a Lutris game through its own CLI."""

from __future__ import annotations

import signal

from integrations.lutris import process


def test_a_game_is_started_by_its_database_id_through_the_lutris_cli(
    monkeypatch,
) -> None:
    """The id the library is keyed by is the id the CLI takes -- no mapping.

    Going through the CLI is the point: Lutris builds the launch line from the
    game's stored config, so the prefix_command PenguinBurner wrote is already
    in it and nothing here has to re-implement a launch.
    """
    seen: list[list[str]] = []

    class _Popen:
        def __init__(self, command, **_kwargs):
            seen.append(list(command))

    monkeypatch.setattr(process.subprocess, "Popen", _Popen)
    monkeypatch.setattr(process, "running_in_flatpak", lambda: False)

    assert process.launch_lutris_game("27") is True
    assert seen == [["lutris", "lutris:rungameid/27"]]


def test_a_non_numeric_id_never_reaches_the_command_line(monkeypatch) -> None:
    """rungameid takes the numeric id; anything else is not ours to pass on."""
    called: list[object] = []
    monkeypatch.setattr(
        process.subprocess, "Popen", lambda *a, **k: called.append(a)
    )

    for game_id in ("", "  ", "assassins-creed-shadows", "27; rm -rf ~"):
        assert process.launch_lutris_game(game_id) is False
    assert called == []


def test_the_wrapper_is_recognised_after_it_renames_itself() -> None:
    """This is the form a probe actually sees, and the one that was missed.

    lutris-wrapper is exec'd with the title followed by two counts and then
    calls setproctitle("lutris-wrapper: " + title), so by the time anything
    looks at it the counts are gone and the title runs to the end. Matching
    only the exec form found nothing, which showed a running game as stopped
    and left Stop with no session to signal.
    """
    known = ["Portal", "Portal 2", "Assassin's Creed Shadows"]

    assert (
        process.title_from_wrapper_line(
            "lutris-wrapper: Assassin's Creed Shadows", known
        )
        == "Assassin's Creed Shadows"
    )
    assert process.title_from_wrapper_line("lutris-wrapper: Portal 2", known) == "Portal 2"
    # Still not a game we have, colon or no colon.
    assert (
        process.title_from_wrapper_line("lutris-wrapper: Portal Knights", known) is None
    )


def test_a_longer_title_cannot_lose_its_session_to_a_shorter_one() -> None:
    """"Portal" must not claim "Portal 2"'s wrapper.

    argv reaches us joined by spaces and a title may contain them, so the
    boundary is confirmed by the two counts Lutris puts after the title, and
    the longest matching name wins.
    """
    known = ["Portal", "Portal 2", "Assassin's Creed Shadows"]

    assert (
        process.title_from_wrapper_line("lutris-wrapper Portal 2 0 0 game.exe", known)
        == "Portal 2"
    )
    assert (
        process.title_from_wrapper_line("lutris-wrapper Portal 0 0 game.exe", known)
        == "Portal"
    )
    assert (
        process.title_from_wrapper_line(
            "/usr/bin/python /usr/share/lutris/bin/lutris-wrapper "
            "Assassin's Creed Shadows 0 0 mangohud umu-run x.exe",
            known,
        )
        == "Assassin's Creed Shadows"
    )
    assert process.title_from_wrapper_line("lutris-wrapper Other 0 0 x", known) is None
    assert process.title_from_wrapper_line("something else entirely", known) is None

    # A game we do not have, whose name merely starts with one we do. Length
    # ordering cannot save this one -- only the counts say where a title ends.
    assert (
        process.title_from_wrapper_line(
            "lutris-wrapper Portal Knights 0 0 pk.exe", known
        )
        is None
    )


def _pgrep(monkeypatch, *, stdout: str, returncode: int = 0):
    class _Result:
        pass

    result = _Result()
    result.stdout = stdout
    result.returncode = returncode
    monkeypatch.setattr(process, "running_in_flatpak", lambda: False)
    monkeypatch.setattr(process.subprocess, "run", lambda *a, **k: result)


def test_one_probe_answers_for_the_whole_library(monkeypatch) -> None:
    _pgrep(
        monkeypatch,
        stdout=(
            "4210 /usr/share/lutris/bin/lutris-wrapper Portal 2 0 0 umu-run p2.exe\n"
            "4300 lutris-wrapper: Battle.net\n"  # the renamed form, seen live

            "9999 grep something-else\n"
        ),
    )

    running = process.running_lutris_games(["Portal", "Portal 2", "Battle.net"])

    assert running == {"Portal 2": (4210,), "Battle.net": (4300,)}


def test_a_failed_probe_says_so_instead_of_reporting_an_empty_library(
    monkeypatch,
) -> None:
    """"Nothing running" and "could not tell" must not be the same answer.

    The caller holds every state it knows on None; reading a stalled probe as
    an empty set would show every running game as stopped.
    """
    _pgrep(monkeypatch, stdout="", returncode=2)

    assert process.running_lutris_games(["Portal"]) is None

    _pgrep(monkeypatch, stdout="", returncode=1)
    assert process.running_lutris_games(["Portal"]) == {}


def test_stopping_signals_the_wrapper_rather_than_the_game(monkeypatch) -> None:
    """lutris-wrapper is a child subreaper with its own SIGTERM handler.

    It takes the game's whole tree down and escalates to SIGKILL itself on a
    second signal. Killing the game process instead would leave that wrapper
    holding the orphans.
    """
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(process.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    assert process.stop_lutris_game(4210) is True
    assert sent == [(4210, signal.SIGTERM)]


def test_stopping_a_pid_that_is_gone_is_a_failure_not_a_crash(monkeypatch) -> None:
    def _gone(_pid, _sig):
        raise ProcessLookupError

    monkeypatch.setattr(process.os, "kill", _gone)

    assert process.stop_lutris_game(4210) is False


def test_availability_is_asked_of_the_host_inside_a_flatpak(monkeypatch) -> None:
    """The sandbox PATH says nothing about the host, so an in-sandbox which()
    kept can_launch False forever in the Flatpak build -- listing and
    configuring games whose whole launch surface was unreachable."""
    monkeypatch.setattr(process, "running_in_flatpak", lambda: True)
    monkeypatch.setattr(
        process.shutil,
        "which",
        lambda name: "/usr/bin/flatpak-spawn" if name == "flatpak-spawn" else None,
    )
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return process.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(process.subprocess, "run", fake_run)

    assert process.lutris_available() is True
    (command,) = commands
    assert command[:2] == ["/usr/bin/flatpak-spawn", "--host"]
    assert command[-3:] == ["/usr/bin/sh", "-c", "command -v lutris"]


def test_stopping_from_a_flatpak_signals_through_the_host(monkeypatch) -> None:
    """The pid came from the host pgrep: in the sandbox's own PID namespace
    that number is nothing, or some unrelated sandbox process."""
    monkeypatch.setattr(process, "running_in_flatpak", lambda: True)
    monkeypatch.setattr(
        process.shutil,
        "which",
        lambda name: "/usr/bin/flatpak-spawn" if name == "flatpak-spawn" else None,
    )
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return process.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(process.subprocess, "run", fake_run)

    def _never(_pid, _sig):
        raise AssertionError("os.kill must not run in the sandbox namespace")

    monkeypatch.setattr(process.os, "kill", _never)

    assert process.stop_lutris_game(4210) is True
    (command,) = commands
    assert command[:2] == ["/usr/bin/flatpak-spawn", "--host"]
    assert command[-3:] == ["/usr/bin/kill", "-TERM", "4210"]
