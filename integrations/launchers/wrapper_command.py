"""Putting the PenguinBurner wrapper into a launch command, and taking it out.

Lutris prepends to ``prefix_command`` and Heroic adds a wrapper entry, but both
end up with the same shape: our tokens run first and whatever the user already
had there stays between us and the game. Only Steam differs, because it splices
into a ``%command%`` placeholder instead of prepending, and keeps its own
module for it.
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
) -> str:
    """Put our tokens in front of whatever the user already had there."""
    base = strip_penguin_burner_tokens(command or "")
    tokens = wrapper_tokens(
        overlay=overlay,
        game_key=game_key(launcher_id, game_id),
        # With the overlay on the launcher turns the markers on by itself, so
        # writing the opt-in as well would only be noise in the command.
        ingame_latency=ingame_latency and not overlay,
    )
    return f"{tokens} {base}".strip() if base else tokens


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
