"""The Perf Profile's data: the daemon's per-game frame-history rings.

``RuntimePerfSource`` is the ``PerfProfileSource`` the Game Library page and
its Perf Profile read. It reads the same rings the classic Game Perf Profile
tab reads (``runtime.frame_history``: the live ring under /run while the game
plays, else the archive) and passes the daemon's summary through unchanged.
The library supplies what the ring does not hold: the game's name, its
Capture telemetry setting and its Adaptive target.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from profiles.uv.profile_tiers import PROFILE_TIER_LABELS
from runtime.frame_history import (
    FrameHistory,
    FrameHistorySummary,
    frame_history_files_for_game,
    read_frame_history_for_game,
    summarize,
)
from runtime.support.adaptive_target_fps import adaptive_target_fps_from_env
from ui.components.perf_profile_plot import PerfRecording, PerfSummary


def perf_summary(summary: FrameHistorySummary) -> PerfSummary:
    """The daemon's summary as the page draws it; the tier by its label. Pure."""
    values = asdict(summary)
    values["tier"] = PROFILE_TIER_LABELS.get(summary.tier, "")
    return PerfSummary(**values)


def _fingerprint(paths: tuple[Path, ...]) -> tuple:
    """What has to change before a ring is worth decoding again."""
    stamps = []
    for path in paths:
        try:
            stat = os.stat(path)
        except OSError:
            stamps.append((str(path), None))
            continue
        stamps.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(stamps)


@dataclass
class _Decoded:
    fingerprint: tuple
    history: FrameHistory | None
    summary: FrameHistorySummary | None
    inputs: tuple = ()
    version: int = 0
    recording: PerfRecording | None = None


class RuntimePerfSource:
    """``PerfProfileSource`` over ``runtime.frame_history``.

    ``game(key)`` returns the library's ``LibraryGame`` for a key (or None).
    A ring is decoded again only when its files change, so the library can
    ask on every selection and game-state poll.
    """

    def __init__(
        self,
        game: Callable[[str], Any],
        *,
        env: Mapping[str, str] | None = None,
        system_target_fps: Callable[[], float] = adaptive_target_fps_from_env,
    ) -> None:
        self._game = game
        self._env = env
        self._system_target_fps = system_target_fps
        self._decoded: dict[str, _Decoded] = {}

    def _setting(self, game_key: str):
        return getattr(getattr(self._game(game_key), "detail", None), "setting", None)

    def game_name(self, game_key: str) -> str:
        game = self._game(game_key)
        return str(getattr(game, "name", "") or game_key)

    def capture_enabled(self, game_key: str) -> bool:
        return bool(getattr(self._setting(game_key), "telemetry", True))

    def has_recording(self, game_key: str) -> bool:
        return bool(frame_history_files_for_game(game_key, env=self._env))

    def target_fps(self, game_key: str) -> tuple[float, bool]:
        """(target, is the game's own): the per-game Adaptive target, else
        the system-wide one it follows."""
        target = getattr(self._setting(game_key), "target_fps", None)
        if target is not None:
            return float(target), True
        return float(self._system_target_fps()), False

    def record(self, game_key: str) -> PerfRecording | None:
        paths = frame_history_files_for_game(game_key, env=self._env)
        if not paths:
            self._decoded.pop(game_key, None)
            return None
        fingerprint = _fingerprint(paths)
        decoded = self._decoded.get(game_key)
        if decoded is None or decoded.fingerprint != fingerprint:
            history = read_frame_history_for_game(game_key, env=self._env)
            decoded = _Decoded(
                fingerprint=fingerprint,
                history=history,
                summary=summarize(history) if history is not None else None,
                version=(decoded.version + 1) if decoded is not None else 1,
            )
            self._decoded[game_key] = decoded
        history = decoded.history
        if history is None:
            return None
        target, is_game = self.target_fps(game_key)
        inputs = (target, is_game)
        if decoded.recording is None or decoded.inputs != inputs:
            if decoded.recording is not None:
                decoded.version += 1
            decoded.inputs = inputs
            summary = perf_summary(decoded.summary) if decoded.summary is not None else None
            decoded.recording = PerfRecording(
                game_key=game_key,
                frametimes_ms=history.frametimes_ms,
                live=history.live,
                tier=summary.tier if summary is not None else "",
                target_fps=target,
                target_is_game=is_game,
                power_limit_w=history.header.power_limit_w,
                summary=summary,
                version=decoded.version,
            )
        return decoded.recording
