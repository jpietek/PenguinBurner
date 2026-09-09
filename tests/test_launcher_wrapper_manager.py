"""The write path every config-file launcher shares.

Lutris exercises this through real YAML elsewhere; here the launcher is a
stand-in with two config levels in memory, so the state machine itself is the
subject: what happens to a command that was inherited, to one the user edited
behind our back, and to a bulk write that fails halfway through.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from integrations.launchers.game_settings import GameSettingsStore, LauncherGameSetting
from integrations.launchers.wrapper_manager import (
    SOURCE_GAME,
    CommandWrite,
    EffectiveCommand,
    WrapperManager,
)
from overlay.telemetry.steam_launch_check import PENGUIN_BURNER_WRAPPER
from overlay.wrapper_tokens import strip_penguin_burner_tokens, wrapper_present

SOURCE_RUNNER = "runner"
SETTINGS_FILENAME = "fake-game-settings.json"
STORE = GameSettingsStore(SETTINGS_FILENAME)


@dataclass(frozen=True)
class FakeGame:
    game_id: str
    display_name: str


class FakeLauncher(WrapperManager):
    """A launcher with a game level and one level above it, both in memory."""

    launcher_id = "fake"
    display_name = "Fake"
    source_labels = {SOURCE_RUNNER: "the runner"}

    def __init__(self, settings_path, *, game_ids=("1",), inherited: str = ""):
        super().__init__(GameSettingsStore(SETTINGS_FILENAME), settings_path=settings_path)
        self.games = tuple(FakeGame(game_id, f"Game {game_id}") for game_id in game_ids)
        self.commands = {game.game_id: "" for game in self.games}
        self.inherited = inherited
        #: Games whose config refuses to be written, as a locked file would.
        self.locked: set[str] = set()
        self.writes: list[tuple[str, str]] = []

    def read_games(self):
        return self.games

    def read_effective(self, game) -> EffectiveCommand:
        own = self.commands.get(game.game_id, "")
        if own:
            return EffectiveCommand(own, SOURCE_GAME)
        return EffectiveCommand(
            self.inherited, SOURCE_RUNNER if self.inherited else ""
        )

    def read_inherited(self, game) -> str:
        return self.inherited

    def write_command(self, game, command: str) -> CommandWrite:
        if game.game_id in self.locked:
            return CommandWrite(
                False, self.commands.get(game.game_id, ""), "the config is locked"
            )
        self.writes.append((game.game_id, command))
        self.commands[game.game_id] = command
        return CommandWrite(True, command)


def _launcher(tmp_path, **kwargs) -> FakeLauncher:
    launcher = FakeLauncher(tmp_path / SETTINGS_FILENAME, **kwargs)
    launcher.refresh()
    return launcher


def _settings(tmp_path) -> dict[str, LauncherGameSetting]:
    return STORE.load(tmp_path / SETTINGS_FILENAME)


def _wrapper_count(command: str) -> int:
    return command.split().count(PENGUIN_BURNER_WRAPPER)


# -- a value the game only inherits ---------------------------------------------


def test_enabling_keeps_what_the_game_only_inherited(tmp_path) -> None:
    """Writing the game level replaces the level above it, so an injection
    that ignores what is inherited silently drops it for this game."""
    launcher = _launcher(tmp_path, inherited="game-performance")

    assert launcher.set_game_enabled("1", True).ok

    written = launcher.commands["1"]
    assert wrapper_present(written)
    assert strip_penguin_burner_tokens(written) == "game-performance"


def test_inheritance_that_changed_while_enabled_is_resumed_not_frozen(
    tmp_path,
) -> None:
    """The level above is external: it can change while we are switched on.

    Disabling has to hand the game back to *inheritance*, not to the snapshot
    of what it inherited when the wrapper went in -- otherwise the moment
    PenguinBurner is switched off the game pins a command the user changed
    weeks ago.
    """
    launcher = _launcher(tmp_path, inherited="game-performance")
    assert launcher.set_game_enabled("1", True).ok

    launcher.inherited = "mangohud"

    assert launcher.set_game_enabled("1", False).ok
    assert launcher.commands["1"] == ""
    row = launcher.row("1")
    assert row is not None
    assert row.command == "mangohud"
    assert row.effective.source == SOURCE_RUNNER
    assert not wrapper_present(row.command)


def test_an_option_change_while_inheriting_still_resumes_inheritance(
    tmp_path,
) -> None:
    """The overlay toggle rewrites the wrapper; it must not turn the inherited
    command into an explicit one on the way through."""
    launcher = _launcher(tmp_path, inherited="game-performance")
    assert launcher.set_game_enabled("1", True).ok
    launcher.inherited = "mangohud"

    assert launcher.set_game_overlay("1", True).ok
    assert launcher.set_game_enabled("1", False).ok

    assert launcher.commands["1"] == ""
    row = launcher.row("1")
    assert row is not None
    assert row.command == "mangohud"


# -- a command the user edited behind our back ----------------------------------


@pytest.mark.parametrize(
    "edit",
    [
        "mangohud",
        "env NAME='two  spaces'  mangohud",
        'env NAME="a b" gamescope -w 2560 --',
        "sh -c 'a && b'",
    ],
)
def test_a_manual_edit_while_enabled_survives_every_later_write(
    tmp_path, edit
) -> None:
    """PenguinBurner wrote this command last, but the user owns it now.

    Their edit has to survive the next managed option change (which rewrites
    our flags around it) and the disable after it -- and what comes back must
    be their command, not the one we recorded before they touched it.
    """
    launcher = _launcher(tmp_path)
    assert launcher.set_game_command("1", "gamemoderun").ok
    assert launcher.set_game_enabled("1", True).ok

    # The user edits the launcher's own config, keeping our wrapper in front.
    launcher.commands["1"] = f"{PENGUIN_BURNER_WRAPPER} --pb-overlay=0 {edit}"

    assert launcher.set_game_overlay("1", True).ok
    reinjected = launcher.commands["1"]
    assert _wrapper_count(reinjected) == 1
    assert strip_penguin_burner_tokens(reinjected) == edit

    assert launcher.set_game_enabled("1", False).ok
    assert launcher.commands["1"] == edit
    setting = _settings(tmp_path)["1"]
    assert setting.original_command == edit
    assert setting.injected_command == ""


def test_a_manual_edit_that_removed_the_wrapper_is_not_re_wrapped_silently(
    tmp_path,
) -> None:
    """Taking the wrapper out by hand is a disable the tab has to agree with."""
    launcher = _launcher(tmp_path)
    assert launcher.set_game_enabled("1", True).ok

    assert launcher.set_game_command("1", "mangohud").ok

    assert launcher.commands["1"] == "mangohud"
    setting = _settings(tmp_path)["1"]
    assert setting.enabled is False
    assert setting.injected_command == ""


def test_typing_a_command_makes_the_game_stop_inheriting(tmp_path) -> None:
    """A command typed into the field is a game-level override by definition:
    it is written at the game level, which is what stops inheritance. Disable
    therefore restores that command rather than clearing the level."""
    launcher = _launcher(tmp_path, inherited="game-performance")

    assert launcher.set_game_command("1", "mangohud").ok

    setting = _settings(tmp_path)["1"]
    assert setting.original_inherited is False

    assert launcher.set_game_enabled("1", True).ok
    assert launcher.set_game_enabled("1", False).ok
    assert launcher.commands["1"] == "mangohud"
    row = launcher.row("1")
    assert row is not None
    assert row.effective.source == SOURCE_GAME


# -- bulk writes ----------------------------------------------------------------


def test_a_bulk_write_that_fails_in_the_middle_leaves_the_rest_consistent(
    tmp_path,
) -> None:
    """Partial success is the intended outcome -- rolling back a launcher's own
    config is riskier than stopping where we got to -- so what matters is that
    the games that failed are reported and left exactly as they were."""
    launcher = _launcher(tmp_path, game_ids=("1", "2", "3"))
    launcher.locked = {"2"}

    result = launcher.set_all_games_enabled(["1", "2", "3"], True)

    assert result.ok is False
    assert "1 failed" in result.message
    assert "the config is locked" in result.message
    assert wrapper_present(launcher.commands["1"])
    assert wrapper_present(launcher.commands["3"])
    assert launcher.commands["2"] == ""
    settings = _settings(tmp_path)
    assert set(settings) == {"1", "3"}
    assert all(setting.enabled for setting in settings.values())


def test_retrying_the_game_that_failed_wraps_it_exactly_once(tmp_path) -> None:
    launcher = _launcher(tmp_path, game_ids=("1", "2"))
    launcher.locked = {"2"}
    launcher.set_all_games_enabled(["1", "2"], True)

    launcher.locked = set()
    assert launcher.set_game_enabled("2", True).ok

    assert _wrapper_count(launcher.commands["2"]) == 1
    settings = _settings(tmp_path)
    assert set(settings) == {"1", "2"}


def test_a_config_that_refuses_the_write_stores_no_setting(tmp_path) -> None:
    launcher = _launcher(tmp_path)
    launcher.locked = {"1"}

    result = launcher.set_game_enabled("1", True)

    assert result.ok is False
    assert _settings(tmp_path) == {}


# -- the settings file itself ---------------------------------------------------


def test_a_settings_file_that_could_not_be_read_is_reported_once_kept(
    tmp_path,
) -> None:
    settings_path = tmp_path / SETTINGS_FILENAME
    settings_path.write_text('{"games": {"1": {"enab', encoding="utf-8")
    launcher = _launcher(tmp_path)

    result = launcher.set_game_enabled("1", True)

    assert result.ok is True
    backups = sorted(tmp_path.glob(f"{SETTINGS_FILENAME}.corrupt-*"))
    assert len(backups) == 1
    assert str(backups[0]) in result.message
    assert wrapper_present(launcher.commands["1"])


def test_a_settings_file_that_cannot_be_kept_is_reported_as_a_failure(
    tmp_path, monkeypatch
) -> None:
    """The launcher's config was already written by then, so this cannot pass
    silently: the game launches wrapped with nothing recording why."""
    settings_path = tmp_path / SETTINGS_FILENAME
    settings_path.write_text('{"games": {"1": {"enab', encoding="utf-8")
    launcher = _launcher(tmp_path)

    def refuse(_path):
        raise OSError("read-only file system")

    monkeypatch.setattr(
        "integrations.launchers.game_settings.preserve_unreadable_file", refuse
    )

    result = launcher.set_game_enabled("1", True)

    assert result.ok is False
    assert "settings file could not be" in result.message
    assert settings_path.read_text(encoding="utf-8") == '{"games": {"1": {"enab'


def test_an_unknown_game_is_refused(tmp_path) -> None:
    launcher = _launcher(tmp_path)

    assert launcher.set_game_enabled("nope", True).ok is False
    assert launcher.set_game_command("nope", "mangohud").ok is False
