"""Token-splice injection of the PenguinBurner wrapper into launch options.

Steam substitutes the literal ``%command%`` with the full launch chain and
runs the result through ``/bin/sh -c``. Replacing the token (never
prepending) keeps every user prefix working — env assignments, gamemoderun,
mangohud, and gamescope's ``--`` all stay outside us, so our layer env
reaches only the game.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from overlay.wrapper_tokens import (
    ingame_latency_present,
    overlay_present,
    strip_penguin_burner_tokens as strip_wrapper_tokens,
    wrapper_present,
    wrapper_tokens,
)

COMMAND_TOKEN = "%command%"
# Steam expands this placeholder even inside a quoted command-building script.
# Recognize only the wrapper fragment immediately attached to that placeholder;
# arbitrary quoted text remains opaque to the shared shell-word stripper.
_COMMAND_WRAPPER_RE = re.compile(
    r"(?<![\w/])PENGUIN_BURNER(?:\s+--pb-[a-z0-9-]+=[^\s\"']*)*\s+(?=%command%)"
)


@dataclass(frozen=True)
class InjectionState:
    wrapped: bool
    overlay: bool
    #: The Reflex marker opt-in, in either shape the wrapper accepts.
    ingame_latency: bool = False


def injection_state(launch_options: str | None) -> InjectionState:
    value = launch_options or ""
    fragment = _COMMAND_WRAPPER_RE.search(value)
    if fragment is not None:
        value += " " + fragment.group()
    return InjectionState(
        wrapped=wrapper_present(value),
        overlay=overlay_present(value),
        ingame_latency=ingame_latency_present(value),
    )


def strip_penguin_burner_tokens(value: str) -> str:
    return strip_wrapper_tokens(_COMMAND_WRAPPER_RE.sub("", value or ""))


def inject_launch_options(
    launch_options: str | None,
    *,
    overlay: bool = False,
    ingame_latency: bool = False,
) -> str:
    """Splice the wrapper innermost; idempotent and normalizes legacy placement.

    Existing PB tokens are stripped first, then the first ``%command%`` is
    replaced with ``<tokens> %command%``. A token-less string was game
    arguments, so it is preserved verbatim after the token.

    The latency opt-in rides as a wrapper flag rather than an environment
    assignment: our tokens land where ``%command%`` was, and an assignment
    there is a program name to anything that execs its child directly --
    ``gamescope -- %command%`` being the case that matters.
    """
    base = strip_penguin_burner_tokens(launch_options or "")
    prefix = wrapper_tokens(
        overlay=overlay,
        # With the overlay on the wrapper already runs the markers, so the
        # flag would only restate the default.
        ingame_latency=bool(ingame_latency) and not overlay,
        latency_as_flag=True,
    )
    if COMMAND_TOKEN in base:
        return base.replace(COMMAND_TOKEN, f"{prefix} {COMMAND_TOKEN}", 1)
    injected = f"{prefix} {COMMAND_TOKEN}"
    return f"{injected} {base}" if base else injected


def remove_injection(
    launch_options: str | None,
    *,
    stored_original: str | None = None,
    stored_injected: str | None = None,
) -> str:
    """Undo an injection, restoring the stored original when it still matches.

    Otherwise conservatively strip only our tokens; a result of exactly
    ``%command%`` means the field was ours alone, so it collapses to empty.
    """
    value = launch_options or ""
    if stored_injected is not None and value == stored_injected:
        return stored_original or ""
    stripped = strip_penguin_burner_tokens(value)
    if stripped == COMMAND_TOKEN:
        return ""
    return stripped


def launch_options_problems(launch_options: str) -> tuple[str, ...]:
    """Validation errors that would break the sh -c launch line."""
    problems: list[str] = []
    if launch_options.count(COMMAND_TOKEN) > 1:
        problems.append("more than one %command% token")
    try:
        shlex.split(launch_options, posix=True)
    except ValueError:
        problems.append("unbalanced quotes")
    return tuple(problems)
