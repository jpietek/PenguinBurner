"""Whether the PenguinBurner overlay can attach to a given game.

The overlay and its in-game latency/FPS telemetry are a Vulkan layer, so they
only work on a game that presents through Vulkan. A wine/Proton game always
does (DXVK/vkd3d translate its DirectX to Vulkan), but a *native* Linux game
renders through whatever it was built for -- and an OpenGL-only title (Oxenfree
on Unity 5.3, say) has no Vulkan swapchain for the layer to hook.

The GPU profile the wrapper applies is unaffected: it is graphics-API agnostic
and works on every game. This is only about the overlay switch and the latency
row, which the library tab greys out for a game that cannot show them.
"""

from __future__ import annotations

import os
from pathlib import Path

RENDER_VULKAN = "vulkan"
RENDER_OPENGL = "opengl"
RENDER_UNKNOWN = "unknown"

_ELF_MAGIC = b"\x7fELF"
#: Enough of a binary to carry its dynamic-link strings and the string table a
#: dlopen'd loader name lives in, without reading a multi-hundred-MB
#: asset-packed binary end to end.
_SCAN_LIMIT = 8 * 1024 * 1024
#: A game's launcher (start.sh) often sits beside a bin/ or game/ subdir, so a
#: shallow walk finds the real executable without descending into asset trees.
_WALK_DEPTH = 3
_MAX_BINARIES = 64


def _classify_blob(blob: bytes) -> str:
    if b"libvulkan" in blob:
        return RENDER_VULKAN
    if b"libGL" in blob:
        return RENDER_OPENGL
    return RENDER_UNKNOWN


def _classify_file(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            head = handle.read(4)
            if head != _ELF_MAGIC:
                # A wrapper script (GOG start.sh) or a data file says nothing
                # about the renderer; the real binary beside it does.
                return RENDER_UNKNOWN
            return _classify_blob(head + handle.read(_SCAN_LIMIT - 4))
    except OSError:
        return RENDER_UNKNOWN


def detect_render_api(executable: str | Path | None) -> str:
    """Classify how one file presents: vulkan, opengl, or unknown.

    Reads the binary's own references rather than running it: a Vulkan program
    names ``libvulkan`` (linked or dlopen'd), an OpenGL one ``libGL``. A
    non-ELF file (a launcher script) is unknown -- see ``detect_game_render_api``
    for the directory that resolves it.
    """
    if not executable:
        return RENDER_UNKNOWN
    return _classify_file(Path(executable))


def detect_game_render_api(
    directory: str | Path | None,
    *,
    exe_hint: str | Path | None = None,
) -> str:
    """How a native game presents, found across its install directory.

    The named executable is often a ``start.sh`` wrapper, so the real ELF is
    located by a shallow walk of the game directory. Vulkan wins the moment any
    binary references it; OpenGL is reported only when something did and nothing
    Vulkan did; unknown when no ELF could be classified (the caller fails open).
    """
    hint = _classify_file(Path(exe_hint)) if exe_hint else RENDER_UNKNOWN
    if hint != RENDER_UNKNOWN:
        return hint
    if not directory:
        return RENDER_UNKNOWN
    root = Path(directory)
    saw_opengl = False
    scanned = 0
    for current, _dirs, files in os.walk(root):
        if scanned >= _MAX_BINARIES:
            break
        if len(Path(current).relative_to(root).parts) > _WALK_DEPTH:
            _dirs[:] = []
            continue
        for name in files:
            if scanned >= _MAX_BINARIES:
                break
            api = _classify_file(Path(current) / name)
            if api == RENDER_UNKNOWN:
                continue
            scanned += 1
            if api == RENDER_VULKAN:
                return RENDER_VULKAN
            saw_opengl = True
    return RENDER_OPENGL if saw_opengl else RENDER_UNKNOWN


def overlay_support(
    *,
    translated_to_vulkan: bool,
    executable: str | Path | None = None,
    directory: str | Path | None = None,
):
    """Whether the overlay can attach, and if not, one line saying why.

    ``translated_to_vulkan`` is the launcher's own fact: a wine/Proton game is
    always Vulkan at the driver, so it short-circuits to supported without
    touching the disk. Only a native game is inspected, across its directory.
    """
    if translated_to_vulkan:
        return True, ""
    api = detect_game_render_api(directory, exe_hint=executable)
    if api == RENDER_OPENGL:
        return False, (
            "This game renders with OpenGL. The overlay and its latency/FPS "
            "readout are a Vulkan layer, so they cannot draw here -- the "
            "per-game GPU profile still applies."
        )
    # Vulkan or undetermined: leave the controls on. Undetermined fails open so
    # a native Vulkan game the scan could not classify keeps its overlay.
    return True, ""
