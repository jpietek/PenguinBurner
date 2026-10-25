"""Overlay actions a key binding can trigger while a game is running.

The layer captures no input. A desktop shortcut runs one of these instead, and
the running game picks the change up on its own: the visibility override is
re-read every 50 ms and overlay.toml four times a second, so nothing
here has to reach into the game process.

Everything in here is an unprivileged write to the user's own files. Changing
the active tier is not, and goes through the root daemon like every other
privileged action.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from overlay.config import (
    OVERLAY_DETAIL_LABELS,
    OVERLAY_POSITION_IDS,
    OVERLAY_SCALE_OPTIONS,
    OverlayConfig,
    load_overlay_config,
    next_overlay_detail,
    normalize_overlay_detail,
    save_overlay_config,
    set_overlay_detail,
    set_overlay_position,
    set_overlay_scale,
)
from overlay.state import (
    clear_overlay_override,
    read_overlay_override,
    write_overlay_override,
)

VISIBILITY_MODES = ("on", "off", "auto")


@dataclass(frozen=True, slots=True)
class OverlayControlResult:
    """What an action did, for the CLI to print or serialise."""

    action: str
    message: str
    visible: bool | None = None
    detail: str | None = None
    scale: float | None = None
    position: str | None = None

    def as_json(self) -> dict[str, object]:
        payload: dict[str, object] = {"action": self.action, "message": self.message}
        if self.visible is not None:
            payload["visible"] = self.visible
        if self.detail is not None:
            payload["detail"] = self.detail
        if self.scale is not None:
            payload["scale"] = self.scale
        if self.position is not None:
            payload["position"] = self.position
        return payload


def toggle_overlay_visibility(
    path: str | Path | None = None,
    config_path: str | Path | None = None,
) -> OverlayControlResult:
    """Flip the live visibility override.

    With no override in place the game follows its launch setting: the wrapper
    launches with ``PENGUIN_BURNER_OVERLAY=auto``, so the layer shows the overlay
    exactly when ``enabled`` is set in overlay.toml. The first press inverts
    that, so it always changes what is on screen. A per-game ``PB_OVERLAY=1``
    launch option is not visible from here; that case needs a second press.
    """
    current = read_overlay_override(path)
    if current is None:
        current = load_overlay_config(config_path).enabled
    return set_overlay_visibility("off" if current else "on", path)


def set_overlay_visibility(
    mode: str,
    path: str | Path | None = None,
) -> OverlayControlResult:
    normalized = str(mode or "").strip().lower()
    if normalized not in VISIBILITY_MODES:
        raise ValueError(
            f"overlay visibility must be one of {', '.join(VISIBILITY_MODES)}"
        )
    if normalized == "auto":
        clear_overlay_override(path)
        return OverlayControlResult(
            action="overlay_visibility",
            message="Overlay follows the game's launch setting again.",
        )
    visible = normalized == "on"
    if not write_overlay_override(visible, path):
        raise OSError("could not write the overlay visibility override")
    return OverlayControlResult(
        action="overlay_visibility",
        message="Overlay shown." if visible else "Overlay hidden.",
        visible=visible,
    )


def cycle_overlay_detail(
    config_path: str | Path | None = None,
) -> OverlayControlResult:
    config = load_overlay_config(config_path)
    return _apply_detail(config, next_overlay_detail(config.detail), config_path)


def set_overlay_detail_level(
    detail: str,
    config_path: str | Path | None = None,
) -> OverlayControlResult:
    normalized = normalize_overlay_detail(detail)
    if normalized != str(detail or "").strip().lower():
        raise ValueError(
            f"overlay detail must be one of {', '.join(OVERLAY_DETAIL_LABELS)}"
        )
    return _apply_detail(load_overlay_config(config_path), normalized, config_path)


def _apply_detail(
    config: OverlayConfig,
    detail: str,
    config_path: str | Path | None,
) -> OverlayControlResult:
    updated = set_overlay_detail(config, detail)
    save_overlay_config(updated, config_path)
    return OverlayControlResult(
        action="overlay_detail",
        message=f"Overlay preset: {OVERLAY_DETAIL_LABELS[updated.detail]}.",
        detail=updated.detail,
    )


def step_overlay_scale(
    direction: int,
    config_path: str | Path | None = None,
) -> OverlayControlResult:
    """Change scale by one step, stopping at either end."""
    if direction not in (-1, 1):
        raise ValueError("overlay scale direction must be -1 or 1")
    config = load_overlay_config(config_path)
    index = OVERLAY_SCALE_OPTIONS.index(config.scale)
    index = max(0, min(len(OVERLAY_SCALE_OPTIONS) - 1, index + direction))
    updated = set_overlay_scale(config, OVERLAY_SCALE_OPTIONS[index])
    save_overlay_config(updated, config_path)
    return OverlayControlResult(
        action="overlay_scale",
        message=f"Overlay scale: {updated.scale:g}x.",
        scale=updated.scale,
    )


def cycle_overlay_position(
    direction: int,
    config_path: str | Path | None = None,
) -> OverlayControlResult:
    """Move through the five positions, wrapping in either direction."""
    if direction not in (-1, 1):
        raise ValueError("overlay position direction must be -1 or 1")
    config = load_overlay_config(config_path)
    index = (OVERLAY_POSITION_IDS.index(config.position) + direction) % len(OVERLAY_POSITION_IDS)
    updated = set_overlay_position(config, OVERLAY_POSITION_IDS[index])
    save_overlay_config(updated, config_path)
    return OverlayControlResult(
        action="overlay_position",
        message=f"Overlay position: {updated.position.replace('_', ' ')}.",
        position=updated.position,
    )
