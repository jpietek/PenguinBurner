"""Starting, watching and stopping a Heroic game."""

from __future__ import annotations

from integrations.heroic import process
from integrations.launchers import host_process as host
from integrations.launchers import wrapper_command


def _installed(monkeypatch, *, native: bool = True):
    monkeypatch.setattr(
        process, "host_has_command", lambda name: native or name == "flatpak"
    )


def test_a_game_is_started_through_heroics_own_launch_url(monkeypatch) -> None:
    """Heroic builds the command from the game's config, wrappers included."""
    _installed(monkeypatch)
    started: list[list[str]] = []
    monkeypatch.setattr(process, "start_on_host", lambda cmd: started.append(cmd) or True)

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


def test_running_sessions_are_read_off_our_own_wrappers_command_line(
    monkeypatch,
) -> None:
    """Exact where a name match is not: the flag is the game's own key."""
    monkeypatch.setattr(
        wrapper_command,
        "host_pgrep",
        lambda _pattern: [
            (4210, "PENGUIN_BURNER --pb-overlay=1 --pb-game-id=heroic:Turkey wine"),
            (4211, "PENGUIN_BURNER --pb-overlay=1 --pb-game-id=heroic:Turkey wine"),
            (4300, "PENGUIN_BURNER --pb-overlay=0 --pb-game-id=lutris:27 wine"),
        ],
    )

    assert process.running_heroic_games() == {"Turkey": (4210, 4211)}


def test_a_failed_probe_says_so_instead_of_reporting_nothing_running(
    monkeypatch,
) -> None:
    monkeypatch.setattr(wrapper_command, "host_pgrep", lambda _pattern: None)

    assert process.running_heroic_games() is None


def test_a_game_id_with_a_space_survives_the_command_line(monkeypatch) -> None:
    """Heroic app names are the store's string, so the flag is encoded."""
    monkeypatch.setattr(
        wrapper_command,
        "host_pgrep",
        lambda _pattern: [(9, "PENGUIN_BURNER --pb-game-id=heroic:Sid%20Meier wine")],
    )

    assert process.running_heroic_games() == {"Sid Meier": (9,)}


def test_stopping_signals_the_wrapper_that_became_the_session(monkeypatch) -> None:
    """The wrapper execs the game, so its pid is the session's own."""
    sent: list[int] = []
    monkeypatch.setattr(host, "running_in_flatpak", lambda: False)
    monkeypatch.setattr(host.os, "kill", lambda pid, _sig: sent.append(pid))

    assert process.stop_heroic_game(4210) is True
    assert sent == [4210]
