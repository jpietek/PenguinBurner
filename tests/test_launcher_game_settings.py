"""The per-launcher settings file: what it remembers, and what it refuses to lose.

Every launcher's presets live in one JSON map, rewritten whole on every save.
That makes the *read* the dangerous half: a file that does not parse reads as
empty, and writing that back would delete every other game's preset to record
one. These tests pin the three cases apart -- absent, empty, unreadable -- and
the migration of the names the fields were written under before the launchers
shared a record.
"""

from __future__ import annotations

import json

import pytest

from integrations.lutris.settings import LEGACY_KEYS as LUTRIS_LEGACY_KEYS

from integrations.launchers.game_settings import (
    GameSettingsError,
    GameSettingsStore,
    LauncherGameSetting,
)

STORE = GameSettingsStore("test-game-settings.json")
#: The old field names belong to the launcher that wrote them, so a store only
#: reads them when that launcher declares them -- as Lutris does.
LEGACY_STORE = GameSettingsStore(
    "test-game-settings.json", legacy_keys=LUTRIS_LEGACY_KEYS
)

CORRUPT = '{"games": {"27": {"enabled": true}, "28": {"ena'


def _path(tmp_path):
    return tmp_path / "test-game-settings.json"


def _write(tmp_path, payload: dict):
    path = _path(tmp_path)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _backups(tmp_path):
    return sorted(tmp_path.glob("test-game-settings.json.corrupt-*"))


# -- reading -------------------------------------------------------------------


def test_a_settings_file_that_was_never_written_is_an_empty_store(tmp_path) -> None:
    assert STORE.load(_path(tmp_path)) == {}
    assert STORE.get("27", path=_path(tmp_path)) is None


def test_a_stored_setting_reads_back(tmp_path) -> None:
    path = _path(tmp_path)
    setting = LauncherGameSetting(
        enabled=True,
        mode="balanced",
        overlay=True,
        original_command="gamemoderun",
        injected_command="PENGUIN_BURNER --pb-overlay=1 gamemoderun",
        target_fps=90.0,
        gpu_uuid="GPU-1",
        original_inherited=False,
    )

    STORE.store("27", setting, path=path)

    assert STORE.get("27", path=path) == setting


def test_an_unreadable_file_reads_as_no_settings_rather_than_failing(tmp_path) -> None:
    """One bad file must not take the library tab down with it."""
    _path(tmp_path).write_text(CORRUPT, encoding="utf-8")

    assert STORE.load(_path(tmp_path)) == {}


def test_an_empty_file_is_not_treated_as_damaged(tmp_path) -> None:
    """A write that never got any content into it held no settings to lose."""
    path = _path(tmp_path)
    path.write_text("", encoding="utf-8")

    STORE.store("27", LauncherGameSetting(enabled=True), path=path)

    assert _backups(tmp_path) == []
    assert STORE.get("27", path=path) is not None


# -- writing over a file we could not read --------------------------------------


def test_an_unreadable_file_is_kept_before_it_is_written_over(tmp_path) -> None:
    """The other games in it are gone from the live file either way; this is
    what makes them recoverable rather than destroyed."""
    path = _path(tmp_path)
    path.write_text(CORRUPT, encoding="utf-8")

    write = STORE.store("27", LauncherGameSetting(enabled=True), path=path)

    assert write.preserved is not None
    assert write.preserved.read_text(encoding="utf-8") == CORRUPT
    assert _backups(tmp_path) == [write.preserved]
    assert STORE.get("27", path=path) == LauncherGameSetting(enabled=True)


def test_a_second_corruption_does_not_overwrite_the_first_rescue(tmp_path) -> None:
    path = _path(tmp_path)
    path.write_text(CORRUPT, encoding="utf-8")
    first = STORE.store("27", LauncherGameSetting(enabled=True), path=path)
    path.write_text("also not json", encoding="utf-8")

    second = STORE.store("28", LauncherGameSetting(enabled=True), path=path)

    assert second.preserved != first.preserved
    assert first.preserved is not None
    assert first.preserved.read_text(encoding="utf-8") == CORRUPT
    assert len(_backups(tmp_path)) == 2


def test_a_file_that_cannot_be_kept_is_not_written_over(tmp_path, monkeypatch) -> None:
    """Refusing the write is the only honest answer: the content is still
    there, and nothing has read it."""
    path = _path(tmp_path)
    path.write_text(CORRUPT, encoding="utf-8")

    def refuse(_path):
        raise OSError("read-only file system")

    monkeypatch.setattr(
        "integrations.launchers.game_settings.preserve_unreadable_file", refuse
    )

    with pytest.raises(GameSettingsError):
        STORE.store("27", LauncherGameSetting(enabled=True), path=path)

    assert path.read_text(encoding="utf-8") == CORRUPT


def test_a_readable_file_keeps_every_other_game(tmp_path) -> None:
    path = _path(tmp_path)
    STORE.store("27", LauncherGameSetting(enabled=True, mode="balanced"), path=path)

    STORE.store("28", LauncherGameSetting(enabled=False), path=path)

    settings = STORE.load(path)
    assert set(settings) == {"27", "28"}
    assert settings["27"].mode == "balanced"
    assert _backups(tmp_path) == []


# -- the names these fields used to be written under ----------------------------


def test_settings_written_before_the_launchers_shared_a_record_still_load(
    tmp_path,
) -> None:
    path = _write(
        tmp_path,
        {
            "games": {
                "27": {
                    "enabled": True,
                    "mode": "adaptive",
                    "overlay": True,
                    "original_prefix_command": "gamemoderun",
                    "injected_prefix_command": (
                        "PENGUIN_BURNER --pb-overlay=1 --pb-lutris-id=27 gamemoderun"
                    ),
                    "original_prefix_inherited": True,
                }
            }
        },
    )

    setting = LEGACY_STORE.get("27", path=path)

    assert setting is not None
    assert setting.original_command == "gamemoderun"
    assert setting.injected_command.endswith("gamemoderun")
    assert setting.original_inherited is True
    # Derived from the injected command, which is where the answer was before
    # the flag had a field of its own.
    assert setting.ingame_latency is False


def test_the_next_save_migrates_the_file_to_the_current_names(tmp_path) -> None:
    path = _write(
        tmp_path,
        {
            "games": {
                "27": {
                    "enabled": True,
                    "original_prefix_command": "gamemoderun",
                    "injected_prefix_command": "PENGUIN_BURNER --pb-overlay=0 gamemoderun",
                    "original_prefix_inherited": False,
                }
            }
        },
    )

    LEGACY_STORE.store("28", LauncherGameSetting(enabled=False), path=path)

    written = json.loads(path.read_text(encoding="utf-8"))["games"]
    assert set(written) == {"27", "28"}
    assert written["27"]["original_command"] == "gamemoderun"
    assert written["27"]["original_inherited"] is False
    assert "original_prefix_command" not in written["27"]
    assert "injected_prefix_command" not in written["27"]
    # And the migrated file still reads as the same settings it held.
    assert LEGACY_STORE.get("27", path=path) == LauncherGameSetting(
        enabled=True,
        original_command="gamemoderun",
        injected_command="PENGUIN_BURNER --pb-overlay=0 gamemoderun",
        original_inherited=False,
    )


def test_a_record_without_provenance_stays_unanswered(tmp_path) -> None:
    """Settings written before we recorded where a command came from must not
    claim it was the game's own; the disable path treats None differently."""
    path = _write(tmp_path, {"games": {"27": {"enabled": True}}})

    setting = STORE.get("27", path=path)

    assert setting is not None
    assert setting.original_inherited is None
