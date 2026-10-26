"""Launch a small Vulkan program with the overlay on, as a live preview.

The settings tab paints its own picture of the readout, which is enough to
answer "which fields, which corner, roughly how it looks" without anything
installed. It is still a second implementation of the layout, and a drawing
cannot show the distance-field type or the blended pipeline the way the layer
draws them.

This runs the real thing instead. The layer re-reads ``overlay.toml`` four
times a second, so one launch is enough: every change made in the tab appears
in the running window within a quarter second, with no restart.

The module builds the command and the environment and nothing else -- starting
and stopping the process belongs to the caller, which is the only part that
needs a process to manage.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from .config import OVERLAY_CONFIG_ENV
from .launcher import MASTER_ENABLE_ENV, VK_LAYER_PATH_ENV
from .native_layer import native_layer_dirs
from .state import OVERLAY_ENABLE_ENV_ALIAS, OVERLAY_OVERRIDE_ENV

# What we know how to drive. vkcube ships with vulkan-tools and draws a spinning
# cube at the display's refresh rate, which is all a preview needs: a Vulkan
# swapchain for the layer to attach to.
PREVIEW_EXECUTABLE = "vkcube"
PREVIEW_PACKAGE = "vulkan-tools"

# Big enough that the overlay is legible without covering the settings behind
# it. The layer sizes itself from the swapchain, so this also decides which of
# its resolution tiers the preview exercises.
PREVIEW_WIDTH = 1280
PREVIEW_HEIGHT = 720

# The live visibility override the in-game hotkey writes. The preview must not
# follow it: a player who hid the overlay in a game would otherwise get an
# empty preview window. Nothing can ever exist under /dev/null, so the layer
# always finds "no override" and falls back to PB_OVERLAY=1.
PREVIEW_OVERRIDE_PATH = "/dev/null/penguin-burner-preview-override"

MISSING_EXECUTABLE_MESSAGE = (
    f"The live preview needs {PREVIEW_EXECUTABLE}, from the {PREVIEW_PACKAGE} "
    f"package. Install it and the button will work; everything else on this "
    f"tab is unaffected."
)


def preview_executable(
    env: Mapping[str, str] | None = None,
    *,
    which=shutil.which,
) -> str | None:
    """Absolute path to the preview program, or None when it is not installed.

    `which` is injected so a test can decide the answer without depending on
    what happens to be on the machine running it.
    """
    env = os.environ if env is None else env
    path = which(PREVIEW_EXECUTABLE, path=env.get("PATH"))
    return str(path) if path else None


def live_preview_command(
    *,
    env: Mapping[str, str] | None = None,
    which=shutil.which,
) -> list[str] | None:
    """The argv for the preview window, or None when it cannot be run."""
    executable = preview_executable(env, which=which)
    if executable is None:
        return None
    return [
        executable,
        "--width",
        str(PREVIEW_WIDTH),
        "--height",
        str(PREVIEW_HEIGHT),
    ]


def live_preview_env(
    config_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    layer_dirs=None,
) -> dict[str, str]:
    """The environment that turns the overlay on for the preview window.

    Inherits the caller's environment so a developer's own overrides survive,
    then pins what the preview decides: the overlay is on, it ignores the
    in-game visibility override, and it reads the same config file the tab is
    editing.

    The layer directory is only added when one can be found in the checkout or
    alongside the package. An installed PenguinBurner registers the layer with
    the Vulkan loader, so there is nothing to point at and nothing to add.
    """
    resolved = dict(os.environ if env is None else env)
    resolved[MASTER_ENABLE_ENV] = "1"
    resolved[OVERLAY_ENABLE_ENV_ALIAS] = "1"
    resolved[OVERLAY_OVERRIDE_ENV] = PREVIEW_OVERRIDE_PATH
    if config_path is not None:
        resolved[OVERLAY_CONFIG_ENV] = str(Path(config_path).expanduser())
    dirs = native_layer_dirs(resolved) if layer_dirs is None else list(layer_dirs)
    if dirs:
        existing = str(resolved.get(VK_LAYER_PATH_ENV) or "").strip()
        joined = os.pathsep.join(
            [str(path) for path in dirs] + ([existing] if existing else [])
        )
        resolved[VK_LAYER_PATH_ENV] = joined
    return resolved
