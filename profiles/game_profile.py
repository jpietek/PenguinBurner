"""What a per-game preset means, independent of which launcher configured it.

Steam and Lutris disagree about everything around a game — how a library is
enumerated, where a launch string lives, whether an app id is stable — but not
about what "this game runs Adaptive at 120 FPS on that GPU" resolves to. That
part is profile payload interpretation, so it lives here rather than in either
integration, and both call it.

The functions take any setting object carrying the four decision fields; each
integration keeps its own dataclass with its own launcher-specific bookkeeping
alongside them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

from profiles.gpu_identity import gpu_index_for_uuid
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR, read_auto_uv_profiles
from profiles.uv.profile_tiers import (
    PROFILE_TIERS,
    normalize_profile_tier,
    resolve_profile_tier_profiles,
)
from runtime.support.adaptive_target_fps import (
    MAX_ADAPTIVE_TARGET_FPS,
    MIN_ADAPTIVE_TARGET_FPS,
)

# Integration is opt-in per game. New games keep the wrapper disabled,
# preselect Adaptive, and keep overlay visibility off. Hidden legacy modes stay
# readable and migrate to Adaptive.
GAME_MODE_NONE = "none"
GAME_MODE_DEFAULT = "default"
GAME_MODE_STOCK = "stock"
GAME_MODE_ADAPTIVE = "adaptive"
# Modes a stored per-game setting may hold. Stock is a real choice: pin the
# factory GPU state for this game while the system-wide profile stays tuned.
GAME_MODES = (GAME_MODE_ADAPTIVE, *PROFILE_TIERS, GAME_MODE_STOCK)


class GameProfileSetting(Protocol):
    """The four fields a per-game preset needs to resolve to daemon argv.

    Read-only, so the frozen setting dataclasses both integrations keep
    actually satisfy it.
    """

    @property
    def enabled(self) -> bool: ...

    @property
    def mode(self) -> str: ...

    @property
    def target_fps(self) -> float | None: ...

    @property
    def gpu_uuid(self) -> str: ...


def normalize_game_mode(
    value: object | None, *, default: str = GAME_MODE_DEFAULT
) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in (GAME_MODE_NONE, GAME_MODE_DEFAULT, GAME_MODE_STOCK, GAME_MODE_ADAPTIVE):
        return text
    return normalize_profile_tier(text, default=default)


def normalize_game_target_fps(value: object | None) -> float | None:
    """Per-game adaptive target FPS; None means "use the global default"."""
    try:
        fps = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fps):
        return None
    if not MIN_ADAPTIVE_TARGET_FPS <= fps <= MAX_ADAPTIVE_TARGET_FPS:
        return None
    return fps


def game_mode_uses_latency_markers(mode: object | None) -> bool:
    """Whether a managed game mode needs hidden marker capture.

    Adaptive consumes base-frame pacing, especially to avoid treating frame-
    generated presents as work the GPU rendered. Fixed tiers and Stock do not
    adapt from that signal, so without the overlay they do not need markers.
    """
    return normalize_game_mode(mode) == GAME_MODE_ADAPTIVE


def profile_argv_for_setting(
    setting: GameProfileSetting,
    *,
    gpu_index: int | None = None,
    gpu_uuid: str = "",
    include_legacy_profiles: bool = False,
) -> list[str] | None:
    """Daemon runtime argv for a preset; None when there is nothing to apply."""
    if not setting.enabled:
        return None
    if setting.mode in (GAME_MODE_NONE, GAME_MODE_DEFAULT):
        return None
    if setting.mode == GAME_MODE_STOCK:
        # Explicit per-game stock: pin factory GPU state while this game
        # runs, even when a standing adaptive/tier profile is active.
        argv = ["--auto-uv-profile", STOCK_PROFILE_SELECTOR]
        return _argv_with_gpu_index(argv, gpu_index)
    selected_uuid = str(gpu_uuid or setting.gpu_uuid or "").strip()
    profiles = read_auto_uv_profiles()
    resolved = (
        resolve_profile_tier_profiles(
            profiles,
            gpu_uuid=selected_uuid,
            include_legacy_profiles=include_legacy_profiles,
        )
        if selected_uuid
        else resolve_profile_tier_profiles(profiles)
    )
    if setting.mode == GAME_MODE_ADAPTIVE:
        # Start from the highest explicitly assigned/available tier, not the
        # newest file. "latest" lets a newer scratch or verification profile
        # silently replace the user's Performance assignment in the runtime
        # spec before adaptive switching even begins.
        profile_id = ""
        for tier in reversed(PROFILE_TIERS):
            profile = resolved.get(tier)
            if isinstance(profile, dict):
                profile_id = str(profile.get("profile_id") or "").strip()
            if profile_id:
                break
        if not profile_id:
            return None
        argv = ["--auto-uv-profile", profile_id, "--adaptive-auto-uv"]
        if setting.target_fps is not None:
            argv += ["--adaptive-target-fps", f"{float(setting.target_fps):g}"]
        return _argv_with_gpu_index(argv, gpu_index)
    profile = resolved.get(setting.mode)
    profile_id = (
        str(profile.get("profile_id") or "").strip()
        if isinstance(profile, dict)
        else ""
    )
    if not profile_id:
        return None
    return _argv_with_gpu_index(["--auto-uv-profile", profile_id], gpu_index)


def _argv_with_gpu_index(argv: list[str], gpu_index: int | None) -> list[str]:
    if gpu_index is None:
        return argv
    return [*argv, "--gpu-index", str(max(0, int(gpu_index)))]


def game_gpu_target(
    setting: GameProfileSetting,
    identities: Sequence[object],
) -> tuple[str, int] | None:
    """Resolve a saved UUID to today's index, or infer the only physical GPU."""
    selected_uuid = str(setting.gpu_uuid or "").strip()
    if selected_uuid:
        index = gpu_index_for_uuid(identities, selected_uuid)
        return (selected_uuid, index) if index is not None else None
    if len(identities) != 1:
        return None
    identity = identities[0]
    uuid = str(getattr(identity, "uuid", "") or "").strip()
    index = gpu_index_for_uuid(identities, uuid)
    return (uuid, index) if uuid and index is not None else None
