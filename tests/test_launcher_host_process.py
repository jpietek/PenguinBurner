"""Reaching the host from inside the Flatpak sandbox, for every launcher."""

from __future__ import annotations

from integrations.launchers import host_process as host


def _sandboxed(monkeypatch, *, returncode: int = 0, stdout: str = ""):
    """A Flatpak with a working portal, recording what it was asked to run."""
    commands: list[list[str]] = []
    monkeypatch.setattr(host, "running_in_flatpak", lambda: True)
    monkeypatch.setattr(
        host.shutil,
        "which",
        lambda name: "/usr/bin/flatpak-spawn" if name == "flatpak-spawn" else None,
    )

    def fake_run(command, **_kwargs):
        commands.append(command)
        return host.subprocess.CompletedProcess(command, returncode, stdout=stdout)

    monkeypatch.setattr(host.subprocess, "run", fake_run)
    return commands


def test_a_command_is_asked_of_the_host_inside_a_flatpak(monkeypatch) -> None:
    """The sandbox PATH says nothing about the host.

    An in-sandbox which() kept can_launch False forever in the Flatpak build --
    listing and configuring games whose whole launch surface was unreachable.
    """
    commands = _sandboxed(monkeypatch)

    assert host.host_has_command("lutris") is True
    (command,) = commands
    assert command[:2] == ["/usr/bin/flatpak-spawn", "--host"]
    # The name is an argument to the script, never part of it.
    assert command[-5:] == [
        "/usr/bin/sh",
        "-c",
        'command -v "$1"',
        "sh",
        "lutris",
    ]


def test_a_name_that_is_not_a_program_name_is_never_asked_about(monkeypatch) -> None:
    """The one lookup helper that reaches a shell must not carry syntax into it."""
    commands = _sandboxed(monkeypatch)

    assert host.host_has_command("lutris; rm -rf ~") is False
    assert host.host_has_command("$(id)") is False
    assert host.host_has_command("") is False
    assert commands == []


def test_the_host_command_runs_from_a_directory_the_host_has(monkeypatch) -> None:
    """flatpak-spawn mirrors the sandbox cwd, and /app is not on the host."""
    _sandboxed(monkeypatch)

    command = host.host_command(["true"])

    assert command is not None
    assert "--directory=/tmp" in command


def test_no_portal_means_unknown_rather_than_false(monkeypatch) -> None:
    monkeypatch.setattr(host, "running_in_flatpak", lambda: True)
    monkeypatch.setattr(host.shutil, "which", lambda _name: None)

    assert host.host_command(["true"]) is None
    assert host.run_on_host(["true"]) is None
    assert host.host_pgrep("anything") is None
    assert host.start_on_host(["true"]) is False


def test_a_signal_travels_to_the_host_pid_namespace(monkeypatch) -> None:
    """A pid from the host pgrep is nothing in the sandbox's own namespace."""
    commands = _sandboxed(monkeypatch)

    def _never(_pid, _sig):
        raise AssertionError("os.kill must not run in the sandbox namespace")

    monkeypatch.setattr(host.os, "kill", _never)

    assert host.host_terminate(4210) is True
    (command,) = commands
    assert command[-3:] == ["/usr/bin/kill", "-TERM", "4210"]


def test_a_pgrep_answer_is_pids_with_their_command_lines(monkeypatch) -> None:
    _sandboxed(monkeypatch, stdout="4210 lutris-wrapper: Portal 2\nnot-a-pid line\n")

    assert host.host_pgrep("[l]utris-wrapper") == [(4210, "lutris-wrapper: Portal 2")]


def test_an_untrustworthy_exit_code_is_not_an_empty_answer(monkeypatch) -> None:
    """1 means "ran fine, matched nothing"; anything else means "cannot tell"."""
    _sandboxed(monkeypatch, returncode=1)
    assert host.host_pgrep("nothing") == []

    _sandboxed(monkeypatch, returncode=2)
    assert host.host_pgrep("nothing") is None
