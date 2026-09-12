"""The one write path a config-file launcher needs, shared by all of them.

Turning PenguinBurner on for a game is the same job whatever launcher owns it:
read what the game launches with today, splice our wrapper in front, remember
what was there before, and be able to hand it back exactly. The subtleties --
a value inherited from a level above the game, a user who edited the command by
hand, an off/on toggle that must not reset a configured game -- are the same
too, and getting one of them wrong is silent: the game simply launches
untouched, or never launches again.

So the state machine lives here once, and a launcher supplies only the four
facts it alone knows: which games there are, what a game launches with, what it
would launch with if its own level said nothing, and how to write that field.

Steam is not one of these: it holds launch options in a running client's
memory, not in a file this can rewrite, and keeps its own manager.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from common.flatpak_wrappers import ensure_host_integration
from overlay.wrapper_tokens import (
    ingame_latency_present,
    overlay_present,
    strip_penguin_burner_tokens,
    wrapper_present,
)
from profiles.game_profile import (
    GAME_MODE_ADAPTIVE,
    GAME_MODE_DEFAULT,
    GAME_MODE_NONE,
    game_mode_uses_latency_markers,
    normalize_game_mode,
    normalize_game_target_fps,
)

from .game_settings import GameSettingsError, GameSettingsStore, LauncherGameSetting
from .wrapper_command import inject_wrapper, remove_wrapper

#: The game's own level, as opposed to anything it inherits from.
SOURCE_GAME = "game"


@dataclass(frozen=True)
class EffectiveCommand:
    """What a game really launches with, and which level said so."""

    value: str
    source: str  # SOURCE_GAME, a launcher-specific level, or "" when unset

    @property
    def inherited(self) -> bool:
        return bool(self.value) and self.source != SOURCE_GAME


@dataclass(frozen=True)
class CommandWrite:
    """What a write attempt actually achieved, after reading the field back."""

    ok: bool
    command: str
    message: str = ""


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    message: str
    command: str = ""


@dataclass(frozen=True)
class SettingsSave:
    """Whether the preset was recorded, and anything the user must be told."""

    ok: bool
    note: str = ""


@dataclass(frozen=True)
class LauncherGameRow:
    """One game as the tab sees it: the library entry plus what we did to it."""

    game: Any
    setting: LauncherGameSetting
    effective: EffectiveCommand
    #: The inherited level in words ("the runner"), empty when not inherited.
    source_label: str = ""

    @property
    def command(self) -> str:
        """What this game actually launches with, whatever level set it."""
        return self.effective.value

    @property
    def inherited(self) -> bool:
        return self.effective.inherited

    @property
    def wrapped(self) -> bool:
        """Whether the game currently launches through our wrapper."""
        return wrapper_present(self.effective.value)


class WrapperManager:
    """Read a launcher's games, and own the field that runs our wrapper."""

    launcher_id: str = ""
    display_name: str = ""
    #: Level name -> how to say it in the detail pane.
    source_labels: Mapping[str, str] = {}

    def __init__(
        self,
        store: GameSettingsStore,
        *,
        settings_path: str | Path | None = None,
    ) -> None:
        self._store = store
        self._settings_path = settings_path
        self._rows: dict[str, LauncherGameRow] = {}

    # -- what a launcher must answer -----------------------------------------

    @property
    def available(self) -> bool:
        """Whether this launcher has anything to read on this machine."""
        raise NotImplementedError

    def read_games(self) -> Iterable[Any]:
        """The library. Each game exposes ``game_id`` and ``display_name``."""
        raise NotImplementedError

    def read_effective(self, game: Any) -> EffectiveCommand:
        """What the game launches with today, across every config level."""
        raise NotImplementedError

    def read_inherited(self, game: Any) -> str:
        """What it would launch with if the game's own level said nothing."""
        return ""

    def write_command(self, game: Any, command: str) -> CommandWrite:
        """Write the game's level, then report what really landed there."""
        raise NotImplementedError

    def write_block(self, game: Any) -> str:
        """Why this game cannot be written right now; empty when it can."""
        return ""

    def apply_hint(self) -> str:
        """The sentence telling the user when a change takes effect."""
        return f"Applies the next time {self.display_name} starts the game."

    # -- reading -------------------------------------------------------------

    def rows(self) -> tuple[LauncherGameRow, ...]:
        return tuple(self._rows.values())

    def row(self, game_id: str) -> LauncherGameRow | None:
        return self._rows.get(str(game_id))

    def refresh(self) -> tuple[LauncherGameRow, ...]:
        """Re-read the library and each game's live launch command.

        The launcher's own files are the truth: a user who edited the command
        in the launcher itself should see that here rather than our stale copy.
        """
        settings = self._store.load(self._settings_path)
        self._rows = {
            game.game_id: self._row(
                game, settings.get(game.game_id, LauncherGameSetting())
            )
            for game in self.read_games()
        }
        return self.rows()

    def _row(self, game: Any, setting: LauncherGameSetting) -> LauncherGameRow:
        effective = self.read_effective(game)
        return LauncherGameRow(
            game=game,
            setting=setting,
            effective=effective,
            source_label=(
                self.source_labels.get(effective.source, "")
                if effective.inherited
                else ""
            ),
        )

    # -- writing -------------------------------------------------------------

    def set_game_enabled(self, game_id: str, enabled: bool) -> ApplyResult:
        row = self.row(game_id)
        if row is None:
            return ApplyResult(False, "Unknown game.")
        blocked = self.write_block(row.game)
        if enabled and blocked:
            return ApplyResult(False, blocked)
        setting = replace(row.setting, enabled=bool(enabled))
        if enabled and setting.mode in (GAME_MODE_NONE, GAME_MODE_DEFAULT):
            setting = replace(setting, mode=GAME_MODE_ADAPTIVE)
        return self._sync_game(row, setting)

    def set_game_mode(self, game_id: str, mode: str) -> ApplyResult:
        return self._update(game_id, mode=normalize_game_mode(mode))

    def set_game_overlay(self, game_id: str, overlay: bool) -> ApplyResult:
        return self._update(game_id, overlay=bool(overlay))

    def set_game_target_fps(self, game_id: str, target_fps: float | None) -> ApplyResult:
        return self._update(
            game_id,
            target_fps=(
                None if target_fps is None else normalize_game_target_fps(target_fps)
            ),
        )

    def set_game_gpu(self, game_id: str, gpu_uuid: str) -> ApplyResult:
        return self._update(game_id, gpu_uuid=str(gpu_uuid or "").strip())

    def _update(self, game_id: str, **changes) -> ApplyResult:
        row = self.row(game_id)
        if row is None:
            return ApplyResult(False, "Unknown game.")
        return self._sync_game(row, replace(row.setting, **changes))

    def set_all_games_enabled(self, game_ids, enabled: bool) -> ApplyResult:
        failures: list[str] = []
        changed = 0
        for game_id in list(game_ids):
            result = self.set_game_enabled(game_id, enabled)
            if result.ok:
                changed += 1
            else:
                failures.append(result.message)
        if failures:
            return ApplyResult(
                False,
                f"{changed} game(s) updated; {len(failures)} failed: {failures[0]}",
            )
        word = "enabled" if enabled else "disabled"
        return ApplyResult(True, f"PenguinBurner {word} for {changed} game(s).")

    def set_game_command(self, game_id: str, text: str) -> ApplyResult:
        """Write the game's launch command exactly as the user typed it.

        The managed controls write a command PenguinBurner composes; this
        writes one the user composed, preserving expert overrides that the form
        does not model (for example forcing markers under a fixed tier).

        What lands is then read back *out of the command* rather than kept from
        the old setting: after a hand edit the file is the truth, and a stored
        setting still claiming the wrapper is on would have the tab disagree
        with the config it just wrote.
        """
        row = self.row(game_id)
        if row is None:
            return ApplyResult(False, "Unknown game.")
        blocked = self.write_block(row.game)
        if blocked:
            return ApplyResult(False, blocked)
        wanted = str(text or "").strip()
        if wrapper_present(wanted):
            problem = self._ensure_wrapper_installed()
            if problem:
                return ApplyResult(False, problem)
        write = self.write_command(row.game, wanted)
        if not write.ok:
            return ApplyResult(False, write.message, write.command)

        landed = write.command
        wrapped = wrapper_present(landed)
        overlay = overlay_present(landed)
        stored = replace(
            row.setting,
            enabled=wrapped,
            overlay=overlay,
            # Injection deliberately leaves the latency flag out while the
            # overlay is on (the wrapper runs the markers anyway), so on such a
            # command its absence says nothing about the stored opt-in; only an
            # overlay-off command speaks for it.
            ingame_latency=(
                row.setting.ingame_latency if overlay else ingame_latency_present(landed)
            ),
            original_command=strip_penguin_burner_tokens(landed),
            original_inherited=False,
            injected_command=landed if wrapped else "",
        )
        # Stored either way: a hand edit that removed the wrapper still leaves
        # the user's tier, GPU choice and FPS target worth keeping for the next
        # enable, exactly as a toggle-driven disable does.
        save = self._save(row.game, stored)
        if not save.ok:
            return ApplyResult(False, save.note, landed)
        return ApplyResult(True, save.note, landed)

    # -- the one write path --------------------------------------------------

    def _sync_game(
        self, row: LauncherGameRow, setting: LauncherGameSetting
    ) -> ApplyResult:
        """Persist a setting and bring the launch command in line with it.

        The setting file is only written after the launcher's own config write
        succeeds, so a config the launcher refused to keep never leaves a
        stored setting claiming otherwise.
        """
        # Marker capture follows Adaptive automatically. It remains independent
        # of overlay visibility: the overlay decides whether the HUD is drawn.
        setting = replace(
            setting,
            ingame_latency=game_mode_uses_latency_markers(setting.mode),
        )
        blocked = self.write_block(row.game)
        if blocked:
            return ApplyResult(False, blocked)
        effective = self.read_effective(row.game)
        current = effective.value
        inherited = setting.original_inherited
        if setting.enabled:
            # The command about to be written execs the PENGUIN_BURNER host
            # wrapper, so inside a Flatpak that wrapper must exist on the host
            # before the config claims it does -- otherwise the game simply
            # stops launching.
            problem = self._ensure_wrapper_installed()
            if problem:
                return ApplyResult(False, problem)
            # Injecting on top of the EFFECTIVE value, not the game-level one.
            # Writing the game level replaces whatever a level above it set, so
            # a game that inherits "game-performance" would otherwise lose it
            # the moment PenguinBurner is switched on.
            wanted = inject_wrapper(
                current,
                overlay=setting.overlay,
                launcher_id=self.launcher_id,
                game_id=row.game.game_id,
                ingame_latency=setting.ingame_latency,
            )
            if wrapper_present(current):
                # Already wrapped. Either we wrote it -- trust the recorded
                # original -- or the user added the wrapper by hand, in which
                # case whatever survives stripping is what they had before and
                # what a later disable owes them back.
                if setting.injected_command and current != setting.injected_command:
                    # A user edit now owns this game-level command, even if our
                    # next setting change rewrites the wrapper flags.
                    original = strip_penguin_burner_tokens(current)
                    inherited = False
                else:
                    original = setting.original_command or strip_penguin_burner_tokens(
                        current
                    )
            else:
                original = current
                inherited = effective.source != SOURCE_GAME
        else:
            wanted = remove_wrapper(
                current,
                stored_original=setting.original_command,
                stored_injected=setting.injected_command,
            )
            original = setting.original_command
            if (inherited is True and current == setting.injected_command) or (
                inherited is None and wanted and wanted == self.read_inherited(row.game)
            ):
                # Resume inheritance even if the level above changed meanwhile.
                # Legacy records lack provenance, so only their equality
                # fallback is safe. Never discard an externally edited command.
                wanted = ""

        write = self.write_command(row.game, wanted)
        if not write.ok:
            return ApplyResult(False, write.message, write.command)

        if setting.enabled:
            stored = replace(
                setting,
                original_command=original,
                original_inherited=inherited,
                injected_command=wanted,
            )
        else:
            # Disabled is a durable per-game choice, and so are the tier, GPU
            # choice and FPS target: keep the record, as the Steam side does,
            # so an off/on toggle does not silently reset a configured game to
            # defaults. What lands as the "original" is the restored command.
            stored = replace(setting, original_command=wanted, injected_command="")
        save = self._save(row.game, stored)
        if not save.ok:
            return ApplyResult(False, save.note, write.command)
        described = self._describe(row.game, stored)
        message = f"{described} {save.note}".strip() if save.note else described
        return ApplyResult(True, message, write.command)

    def _save(self, game: Any, setting: LauncherGameSetting) -> SettingsSave:
        """Record the preset, and report anything that did not go quietly.

        The launcher's config has already been written by the time this runs,
        so a settings file that cannot be saved is not a silent condition: the
        game launches wrapped while PenguinBurner has no record of why.
        """
        try:
            write = self._store.store(
                game.game_id, setting, path=self._settings_path
            )
        except GameSettingsError as error:
            return SettingsSave(
                False,
                f"{game.display_name}: launch command written, but the "
                f"PenguinBurner settings file could not be: {error}",
            )
        self._rows[game.game_id] = self._row(game, setting)
        if write.preserved is None:
            return SettingsSave(True)
        return SettingsSave(
            True,
            "The previous settings file could not be read and was kept as "
            f"{write.preserved}; other games may need setting up again.",
        )

    @staticmethod
    def _ensure_wrapper_installed() -> str:
        """Make the PENGUIN_BURNER host wrapper real before naming it, or say why not.

        Outside a Flatpak this is a no-op: the console-script entry point ships
        with every pip/native install.
        """
        try:
            ensure_host_integration()
        except (OSError, RuntimeError) as error:
            return f"PenguinBurner launcher integration repair failed: {error}"
        return ""

    def _describe(self, game: Any, setting: LauncherGameSetting) -> str:
        if not setting.enabled:
            return f"{game.display_name}: PenguinBurner off, command restored."
        overlay = "overlay on" if setting.overlay else "overlay off"
        target = (
            f", target {float(setting.target_fps):g} FPS"
            if setting.target_fps is not None and setting.mode == GAME_MODE_ADAPTIVE
            else ""
        )
        return (
            f"{game.display_name}: {setting.mode}, {overlay}{target}. "
            f"{self.apply_hint()}"
        )
