"""Resolve the colour roles for a theme, accent and font choice.

Pure and Qt-free: the Qt layer (style.py) and its tests read colours through ``resolve_colors``. pburn-design.json holds a
neutral token set per theme and the accent presets; an accent is either a
preset name or a ``#rrggbb`` seed, and its button, link, focus and selection
roles are derived from that seed unless the preset spells them out (the
PenguinBurner preset pins the shipped app's exact greens).
"""

from __future__ import annotations

import colorsys
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

SPEC = json.loads(Path(__file__).with_name("pburn-design.json").read_text())
THEMES = ("auto", "dark", "light")
FONT_MODES = ("custom", "system")
ACCENT_ROLES = (
    "primary",
    "primaryHover",
    "primaryPressed",
    "primaryBorder",
    "onPrimary",
    "selection",
    "accent",
    "focus",
)
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")


@dataclass(frozen=True)
class AppearanceSettings:
    """What the Appearance dialog edits; the real app keeps it in ``[ui]``.

    ``theme`` is auto, dark or light; ``accent`` a preset name or ``#rrggbb``;
    ``font`` custom (``font_family``, the bundled Inter by default) or system.
    """

    theme: str = SPEC["appearance"]["theme"]
    accent: str = SPEC["appearance"]["accent"]
    font: str = SPEC["appearance"]["font"]
    font_family: str = SPEC["typography"]["family"]

    def normalized(self) -> AppearanceSettings:
        """Unknown values fall back to the defaults, as a hand-edited
        config would."""
        default = AppearanceSettings()
        return replace(
            self,
            theme=self.theme if self.theme in THEMES else default.theme,
            accent=self.accent if is_accent(self.accent) else default.accent,
            font=self.font if self.font in FONT_MODES else default.font,
            font_family=self.font_family.strip() or default.font_family,
        )


SYSTEM_ACCENT = "system"
# Used for the System accent when the desktop publishes none.
FALLBACK_ACCENT = "penguinburner"
_PORTAL_RGB = re.compile(r"\(\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*\)")


def is_accent(value: str) -> bool:
    return (
        value == SYSTEM_ACCENT
        or value in SPEC["accents"]
        or bool(_HEX.fullmatch(value or ""))
    )


def parse_portal_accent(reply: str) -> str | None:
    """``#RRGGBB`` from a ``gdbus`` reply to the portal's ``accent-color``
    read, e.g. ``(<(0.42, 0.33, 0.65)>,)``. The portal reports channels
    outside 0..1 when the desktop sets no accent; that, or any unparsable
    reply, is None."""
    match = _PORTAL_RGB.search(reply or "")
    if not match:
        return None
    try:
        channels = [float(value) for value in match.groups()]
    except ValueError:
        return None
    if not all(0.0 <= value <= 1.0 for value in channels):
        return None
    return "#" + "".join(f"{round(value * 255):02X}" for value in channels)


def resolve_mode(theme: str, system_is_light: bool | None) -> str:
    """auto follows the desktop; an unknown desktop scheme stays dark, the
    app's look before themes existed."""
    if theme in ("dark", "light"):
        return theme
    return "light" if system_is_light else "dark"


def _hls(hex_color: str) -> tuple[float, float, float]:
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)


def _hex(h: float, lightness: float, saturation: float) -> str:
    lightness = min(1.0, max(0.0, lightness))
    saturation = min(1.0, max(0.0, saturation))
    r, g, b = colorsys.hls_to_rgb(h, lightness, saturation)
    return "#" + "".join(f"{round(v * 255):02X}" for v in (r, g, b))


# Lightness and saturation cap per role, fitted to the shipped dark greens
# (#2f6f55 button, #3d8d6d border, #72d596 accent) and mirrored for light so
# white button text and accent-on-white links keep their contrast.
_DERIVE = {
    "dark": {
        "primary": (0.31, 0.45),
        "primaryHover": (0.35, 0.45),
        "primaryPressed": (0.22, 0.45),
        "primaryBorder": (0.40, 0.45),
        "selection": (0.19, 0.25),
        "accent": (0.64, 0.60),
        "focus": (0.72, 0.62),
    },
    "light": {
        "primary": (0.36, 0.60),
        "primaryHover": (0.32, 0.60),
        "primaryPressed": (0.27, 0.60),
        "primaryBorder": (0.30, 0.60),
        "selection": (0.90, 0.55),
        "accent": (0.38, 0.65),
        "focus": (0.41, 0.70),
    },
}
_ON_PRIMARY = {"dark": "#F2F5F2", "light": "#FFFFFF"}


def accent_roles(accent: str, mode: str) -> dict[str, str]:
    """The eight accent roles for ``mode``: a preset's own values where it
    pins them, the rest derived from its seed (or from a ``#rrggbb``)."""
    preset = SPEC["accents"].get(accent)
    seed = preset["seed"] if preset else accent
    h, _, s = _hls(seed)
    roles = {
        role: _hex(h, lightness, min(s, cap))
        for role, (lightness, cap) in _DERIVE[mode].items()
    }
    on_primary = roles["onPrimary"] = _ON_PRIMARY[mode]
    # A seed that already carries button text keeps its exact colour on
    # filled buttons, so a desktop accent looks like the desktop's; only a
    # seed too light for white text is moved to the role's lightness.
    seed = seed.upper()
    if contrast(on_primary, seed) >= 4.5:
        _, lightness, _ = _hls(seed)
        lighter = _hex(h, lightness + 0.04, s)
        roles["primary"] = seed
        roles["primaryHover"] = (
            lighter
            if contrast(on_primary, lighter) >= 4.5
            else _hex(h, lightness - 0.04, s)
        )
        roles["primaryPressed"] = _hex(h, lightness - 0.09, s)
        roles["primaryBorder"] = _hex(
            h, lightness + 0.09 if mode == "dark" else lightness - 0.06, s
        )
    if preset:
        roles.update(preset.get(mode, {}))
    return roles


def resolve_colors(mode: str, accent: str) -> dict[str, str]:
    """Every colour role for a concrete ``mode`` (dark or light)."""
    colors = dict(SPEC["themes"][mode])
    colors.update(accent_roles(accent, mode))
    return colors


def accent_presets() -> list[tuple[str, str, str]]:
    """``(name, label, swatch)`` in picker order; the swatch is the dark
    accent role, the colour people recognise the preset by."""
    return [
        (name, preset["label"], accent_roles(name, "dark")["accent"])
        for name, preset in SPEC["accents"].items()
    ]


def relative_luminance(hex_color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG 2 contrast ratio of two ``#rrggbb`` colours."""
    first, second = sorted((relative_luminance(a), relative_luminance(b)))
    return (second + 0.05) / (first + 0.05)
