"""One place the Lutris tab talks to: library, settings, and game configs.

Deliberately narrower than the Steam manager. Lutris exposes no CDP or DBus
API, so there is no live apply, no launching or stopping games from here, and
no account layer — this owns exactly the per-game configuration the tab edits,
and every change lands in the game's YAML for its next launch.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

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

from .config_store import (
    EffectivePrefixCommand,
    LutrisConfigError,
    effective_prefix_command,
    inject_prefix_command,
    prefix_command_source_label,
    prefix_command_wrapped,
    remove_injection,
    write_prefix_command,
)
from .library import InstalledLutrisGame, read_lutris_games
from .paths import lutris_installed, runner_config_path, system_config_path
from .settings import (
    LutrisGameSetting,
    load_lutris_game_settings,
    store_lutris_game_setting,
)


@dataclass(frozen=True)
class LutrisGameRow:
    game: InstalledLutrisGame
    setting: LutrisGameSetting
    effective: EffectivePrefixCommand

    @property
    def prefix_command(self) -> str:
        """What this game actually launches with, whatever level set it."""
        return self.effective.value

    @property
    def inherited_prefix(self) -> bool:
        return self.effective.inherited

    @property
    def prefix_source_label(self) -> str:
        return prefix_command_source_label(self.effective.source)

    @property
    def wrapped(self) -> bool:
        """Whether the game currently launches through our wrapper."""
        return prefix_command_wrapped(self.effective.value)


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    message: str
    prefix_command: str = ""


class LutrisIntegrationManager:
    def __init__(
        self,
        *,
        home: Path | None = None,
        settings_path: str | Path | None = None,
    ):
        self._home = home
        self._settings_path = settings_path
        self._rows: dict[str, LutrisGameRow] = {}

    # -- reading -------------------------------------------------------------

    @property
    def available(self) -> bool:
        return lutris_installed(self._home)

    def rows(self) -> tuple[LutrisGameRow, ...]:
        return tuple(self._rows.values())

    def row(self, game_id: str) -> LutrisGameRow | None:
        return self._rows.get(str(game_id))

    def refresh(self) -> tuple[LutrisGameRow, ...]:
        """Re-read the library and each game's live prefix_command.

        The config files are the truth: a user who edited prefix_command in
        Lutris itself should see that here rather than our stale copy.
        """
        settings = load_lutris_game_settings(self._settings_path)
        rows: dict[str, LutrisGameRow] = {}
        for game in read_lutris_games(self._home):
            setting = settings.get(game.game_id, LutrisGameSetting())
            rows[game.game_id] = LutrisGameRow(
                game=game,
                setting=setting,
                effective=self._read_prefix(game),
            )
        self._rows = rows
        return self.rows()

    def _inherited_prefix(self, game: InstalledLutrisGame) -> str:
        """What this game would launch with if its own level said nothing."""
        try:
            return effective_prefix_command(
                game_config=None,
                runner_config=runner_config_path(game.runner, self._home),
                system_config=system_config_path(self._home),
            ).value
        except LutrisConfigError:
            return ""

    def _read_prefix(self, game: InstalledLutrisGame) -> EffectivePrefixCommand:
        """What the game really launches with, across all three config levels."""
        try:
            return effective_prefix_command(
                game_config=game.config_path,
                runner_config=runner_config_path(game.runner, self._home),
                system_config=system_config_path(self._home),
            )
        except LutrisConfigError:
            # A malformed config must not take the whole list down; the row
            # still renders and the write path reports the real error.
            return EffectivePrefixCommand("", "")

    # -- writing -------------------------------------------------------------

    def set_game_enabled(self, game_id: str, enabled: bool) -> ApplyResult:
        row = self.row(game_id)
        if row is None:
            return ApplyResult(False, "Unknown game.")
        if enabled and row.game.config_path is None:
            return ApplyResult(
                False,
                f"{row.game.display_name} has no Lutris configuration file to write.",
            )
        setting = replace(row.setting, enabled=bool(enabled))
        if enabled and setting.mode in (GAME_MODE_NONE, GAME_MODE_DEFAULT):
            setting = replace(setting, mode=GAME_MODE_ADAPTIVE)
        return self._sync_game(row, setting)

    def set_game_mode(self, game_id: str, mode: str) -> ApplyResult:
        row = self.row(game_id)
        if row is None:
            return ApplyResult(False, "Unknown game.")
        return self._sync_game(row, replace(row.setting, mode=normalize_game_mode(mode)))

    def set_game_overlay(self, game_id: str, overlay: bool) -> ApplyResult:
        row = self.row(game_id)
        if row is None:
            return ApplyResult(False, "Unknown game.")
        return self._sync_game(row, replace(row.setting, overlay=bool(overlay)))

    def set_game_prefix_command(self, game_id: str, text: str) -> ApplyResult:
        """Write the game's prefix_command exactly as the user typed it.

        The managed controls write a line PenguinBurner composes; this writes
        one the user composed, preserving expert overrides that the form does
        not model (for example forcing markers under a fixed tier).

        What lands is then read back *out of the line* rather than kept from the
        old setting: after a hand edit the file is the truth, and a stored
        setting still claiming the wrapper is on would have the tab disagree
        with the config it just wrote.
        """
        row = self.row(game_id)
        if row is None:
            return ApplyResult(False, "Unknown game.")
        config_path = row.game.config_path
        if config_path is None:
            return ApplyResult(
                False,
                f"{row.game.display_name} has no Lutris configuration file to write.",
            )
        wanted = str(text or "").strip()
        if wrapper_present(wanted):
            problem = self._ensure_wrapper_installed()
            if problem:
                return ApplyResult(False, problem)
        write = write_prefix_command(config_path, wanted)
        if not write.ok:
            return ApplyResult(False, write.message, write.prefix_command)

        landed = write.prefix_command
        wrapped = wrapper_present(landed)
        overlay = overlay_present(landed)
        stored = replace(
            row.setting,
            enabled=wrapped,
            overlay=overlay,
            # Injection deliberately leaves the latency flag out while the
            # overlay is on (the wrapper runs the markers anyway), so on such a
            # line its absence says nothing about the stored opt-in; only an
            # overlay-off line speaks for it.
            ingame_latency=(
                row.setting.ingame_latency
                if overlay
                else ingame_latency_present(landed)
            ),
            original_prefix_command=strip_penguin_burner_tokens(landed),
            original_prefix_inherited=False,
            injected_prefix_command=landed if wrapped else "",
        )
        # Stored either way: a hand edit that removed the wrapper still leaves
        # the user's tier, GPU choice and FPS target worth keeping for the
        # next enable, exactly as a toggle-driven disable does.
        store_lutris_game_setting(row.game.game_id, stored, path=self._settings_path)

        self._rows[row.game.game_id] = LutrisGameRow(
            game=row.game,
            setting=stored,
            effective=self._read_prefix(row.game),
        )
        return ApplyResult(True, "", landed)

    def set_game_target_fps(self, game_id: str, target_fps: float | None) -> ApplyResult:
        row = self.row(game_id)
        if row is None:
            return ApplyResult(False, "Unknown game.")
        normalized = (
            None if target_fps is None else normalize_game_target_fps(target_fps)
        )
        return self._sync_game(row, replace(row.setting, target_fps=normalized))

    def set_game_gpu(self, game_id: str, gpu_uuid: str) -> ApplyResult:
        row = self.row(game_id)
        if row is None:
            return ApplyResult(False, "Unknown game.")
        return self._sync_game(
            row, replace(row.setting, gpu_uuid=str(gpu_uuid or "").strip())
        )

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

    # -- the one write path --------------------------------------------------

    def _sync_game(
        self, row: LutrisGameRow, setting: LutrisGameSetting
    ) -> ApplyResult:
        """Persist a setting and bring the game's prefix_command in line with it.

        The setting file is only written after the game config write succeeds,
        so a config Lutris refused to keep never leaves a stored setting
        claiming otherwise.
        """
        # Marker capture follows Adaptive automatically. It remains independent
        # of overlay visibility: the overlay decides whether the HUD is drawn.
        setting = replace(
            setting,
            ingame_latency=game_mode_uses_latency_markers(setting.mode),
        )
        config_path = row.game.config_path
        if config_path is None:
            return ApplyResult(
                False,
                f"{row.game.display_name} has no Lutris configuration file to write.",
            )
        effective = self._read_prefix(row.game)
        current = effective.value
        inherited = setting.original_prefix_inherited
        if setting.enabled:
            # Same rule as Steam's _apply: the line about to be written execs
            # the PENGUIN_BURNER host wrapper, so inside a Flatpak that wrapper
            # must exist on the host before the config claims it does -- a
            # Lutris-only host installs no wrapper at startup otherwise, and
            # the game simply stops launching.
            problem = self._ensure_wrapper_installed()
            if problem:
                return ApplyResult(False, problem)
            # Injecting on top of the EFFECTIVE value, not the game-level one.
            # Writing the game level replaces whatever the runner or global
            # config set, so a game that inherits "game-performance" would
            # otherwise lose it the moment PenguinBurner is switched on.
            wanted = inject_prefix_command(
                current,
                overlay=setting.overlay,
                game_id=row.game.game_id,
                ingame_latency=setting.ingame_latency,
            )
            if prefix_command_wrapped(current):
                # Already wrapped. Either we wrote it -- trust the recorded
                # original -- or the user added the wrapper by hand, in which
                # case whatever survives stripping is what they had before and
                # what a later disable owes them back.
                if setting.injected_prefix_command and current != setting.injected_prefix_command:
                    # A user edit now owns this game-level prefix, even if
                    # our next setting change rewrites the wrapper flags.
                    original = strip_penguin_burner_tokens(current)
                    inherited = False
                else:
                    original = setting.original_prefix_command or (
                        strip_penguin_burner_tokens(current)
                    )
            else:
                original = current
                inherited = effective.source != "game"
        else:
            wanted = remove_injection(
                current,
                stored_original=setting.original_prefix_command,
                stored_injected=setting.injected_prefix_command,
            )
            original = setting.original_prefix_command
            if (
                inherited is True and current == setting.injected_prefix_command
            ) or (
                inherited is None and wanted and wanted == self._inherited_prefix(row.game)
            ):
                # Resume inheritance even if the runner changed meanwhile.
                # Legacy records lack provenance, so only their equality
                # fallback is safe. Never discard an externally edited line.
                wanted = ""

        write = write_prefix_command(config_path, wanted)
        if not write.ok:
            return ApplyResult(False, write.message, write.prefix_command)

        if setting.enabled:
            stored = replace(
                setting,
                original_prefix_command=original,
                original_prefix_inherited=inherited,
                injected_prefix_command=wanted,
            )
        else:
            # Disabled is a durable per-game choice, and so are the tier, GPU
            # choice and FPS target: keep the record, as the Steam side does,
            # so an off/on toggle does not silently reset a configured game to
            # defaults. What lands as the "original" is the restored line.
            stored = replace(
                setting,
                original_prefix_command=wanted,
                injected_prefix_command="",
            )
        store_lutris_game_setting(row.game.game_id, stored, path=self._settings_path)

        self._rows[row.game.game_id] = LutrisGameRow(
            game=row.game,
            setting=stored,
            effective=self._read_prefix(row.game),
        )
        return ApplyResult(
            True,
            self._describe(row.game, stored),
            write.prefix_command,
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

    @staticmethod
    def _describe(game: InstalledLutrisGame, setting: LutrisGameSetting) -> str:
        if not setting.enabled:
            return f"{game.display_name}: PenguinBurner off, prefix restored."
        overlay = "overlay on" if setting.overlay else "overlay off"
        target = (
            f", target {float(setting.target_fps):g} FPS"
            if setting.target_fps is not None and setting.mode == GAME_MODE_ADAPTIVE
            else ""
        )
        return (
            f"{game.display_name}: {setting.mode}, {overlay}{target}. "
            "Applies the next time Lutris starts the game."
        )
