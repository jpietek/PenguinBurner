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

from pathlib import Path

RENDER_VULKAN = "vulkan"
RENDER_OPENGL = "opengl"
RENDER_UNKNOWN = "unknown"

#: Enough of the executable to carry its dynamic-link strings and the string
#: table a dlopen'd loader name lives in, without reading a multi-hundred-MB
#: asset-packed binary end to end.
_SCAN_LIMIT = 8 * 1024 * 1024


def detect_render_api(executable: str | Path | None) -> str:
    """Classify how a native executable presents: vulkan, opengl, or unknown.

    Reads the binary's own references rather than running it: a Vulkan program
    names ``libvulkan`` (whether it links it or dlopen's it), and an OpenGL one
    names ``libGL``/``libGLX``. ``unknown`` when the file cannot be read or
    names neither -- the caller treats unknown as "leave the option enabled",
    so a detection miss never hides a control that would have worked.
    """
    if not executable:
        return RENDER_UNKNOWN
    path = Path(executable)
    try:
        with path.open("rb") as handle:
            blob = handle.read(_SCAN_LIMIT)
    except OSError:
        return RENDER_UNKNOWN
    if b"libvulkan" in blob:
        return RENDER_VULKAN
    if b"libGL" in blob:
        return RENDER_OPENGL
    return RENDER_UNKNOWN


def overlay_support(*, translated_to_vulkan: bool, executable: str | Path | None):
    """Whether the overlay can attach, and if not, one line saying why.

    ``translated_to_vulkan`` is the launcher's own fact: a wine/Proton game is
    always Vulkan at the driver, so it short-circuits to supported without
    touching the disk. Only a native game is inspected.
    """
    if translated_to_vulkan:
        return True, ""
    api = detect_render_api(executable)
    if api == RENDER_OPENGL:
        return False, (
            "This game renders with OpenGL. The overlay and its latency/FPS "
            "readout are a Vulkan layer, so they cannot draw here -- the "
            "per-game GPU profile still applies."
        )
    # Vulkan or undetermined: leave the controls on. Undetermined fails open so
    # a native Vulkan game the scan could not classify keeps its overlay.
    return True, ""
