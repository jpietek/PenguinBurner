"""Putting the PenguinBurner wrapper into a launch command, and taking it out.

Lutris's ``prefix_command``, Heroic's wrapper rows and Faugus's
``launch_arguments`` all have the same shape: a command prefix the launcher
puts in front of the game. Our tokens go at the END of it, innermost, the spot
Steam's ``%command%`` marks: env assignments, gamemoderun and gamescope's
``--`` all stay outside us, so our layer env reaches only the game and never a
nested compositor. Steam keeps its own module for the placeholder splice.
"""

from __future__ import annotations

from overlay.wrapper_tokens import (
    game_key,
    strip_penguin_burner_tokens,
    wrapper_tokens,
)


def inject_wrapper(
    command: str | None,
    *,
    overlay: bool,
    launcher_id: str,
    game_id: str,
    ingame_latency: bool = False,
    executable: str = "PENGUIN_BURNER",
) -> str:
    """Append our tokens innermost; idempotent, and moves a legacy placement."""
    base = strip_penguin_burner_tokens(command or "")
    tokens = wrapper_tokens(
        overlay=overlay,
        executable=executable,
        game_key=game_key(launcher_id, game_id),
        # With the overlay on the launcher turns the markers on by itself, so
        # writing the opt-in as well would only be noise in the command.
        ingame_latency=ingame_latency and not overlay,
    )
    return " ".join(part for part in (base, tokens) if part)


def remove_wrapper(
    command: str | None,
    *,
    stored_original: str | None = None,
    stored_injected: str | None = None,
) -> str:
    """Undo an injection, restoring the stored original when it still matches."""
    value = command or ""
    if stored_injected is not None and value == stored_injected:
        return stored_original or ""
    return strip_penguin_burner_tokens(value)
