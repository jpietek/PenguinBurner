"""Whether the PenguinBurner overlay can attach to a given game.

The overlay and its in-game latency/FPS telemetry are a Vulkan layer, so they
only work on a game that presents through Vulkan. DXVK/vkd3d translate
DirectX to Vulkan, but a native Linux game
renders through whatever it was built for -- and an OpenGL-only title (Oxenfree
on Unity 5.3, say) has no Vulkan swapchain for the layer to hook.

The GPU profile the wrapper applies is unaffected: it is graphics-API agnostic
and works on every game. Overlay and Adaptive both need the Vulkan layer.
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
# Asset files need only a magic-header read; bound the expensive ELF reads
# separately so a large texture/audio tree does not exhaust the binary budget.
_MAX_FILES = 16384
_MAX_BINARIES = 64


def _classify_blob(blob: bytes) -> str:
    if b"libvulkan" in blob:
        return RENDER_VULKAN
    if b"libGL" in blob:
        return RENDER_OPENGL
    return RENDER_UNKNOWN


def _classify_file(path: Path) -> str | None:
    """Return None for non-ELF files, unknown for unclassified ELF binaries."""
    try:
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            head = handle.read(4)
            if head != _ELF_MAGIC:
                # A wrapper script (GOG start.sh) or a data file says nothing
                # about the renderer; the real binary beside it does.
                return None
            return _classify_blob(head + handle.read(_SCAN_LIMIT - 4))
    except OSError:
        return None


def detect_render_api(executable: str | Path | None) -> str:
    """Classify how one file presents: vulkan, opengl, or unknown.

    Reads the binary's own references rather than running it: a Vulkan program
    names ``libvulkan`` (linked or dlopen'd), an OpenGL one ``libGL``. A
    non-ELF file (a launcher script) is unknown -- see ``detect_game_render_api``
    for the directory that resolves it.
    """
    if not executable:
        return RENDER_UNKNOWN
    return _classify_file(Path(executable)) or RENDER_UNKNOWN


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
    hint = detect_render_api(exe_hint)
    if hint == RENDER_VULKAN:
        return hint
    if not directory:
        return hint
    root = Path(directory)
    saw_opengl = hint == RENDER_OPENGL
    scanned = 0
    binaries = 0
    for current, _dirs, files in os.walk(root):
        if len(Path(current).relative_to(root).parts) > _WALK_DEPTH:
            _dirs[:] = []
            continue
        for name in files:
            if scanned >= _MAX_FILES or binaries >= _MAX_BINARIES:
                # An incomplete scan cannot rule out a Vulkan renderer.
                return RENDER_UNKNOWN
            scanned += 1
            api = _classify_file(Path(current) / name)
            if api is None:
                continue
            binaries += 1
            if api == RENDER_UNKNOWN:
                continue
            if api == RENDER_VULKAN:
                return RENDER_VULKAN
            saw_opengl = True
    return RENDER_OPENGL if saw_opengl else RENDER_UNKNOWN


def overlay_support(
    *,
    translated_to_vulkan: bool,
    executable: str | Path | None = None,
    directory: str | Path | None = None,
) -> tuple[bool, str]:
    """Whether the overlay can attach, and if not, one line saying why.

    A translated game stays enabled without native binary inspection. This
    probe cannot classify a Windows renderer or the runner's translation path.
    """
    if translated_to_vulkan:
        return True, ""
    api = detect_game_render_api(directory, exe_hint=executable)
    if api == RENDER_OPENGL:
        return False, (
            "Game not compatible with Overlay or Adaptive: OpenGL detected. "
            "These features need Vulkan frame data. Fixed GPU profiles "
            "remain available."
        )
    # Vulkan or undetermined: leave the controls on. Undetermined fails open so
    # a native Vulkan game the scan could not classify keeps its overlay.
    return True, ""
