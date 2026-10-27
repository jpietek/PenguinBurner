"""Commands and desktop guidance for overlay global shortcuts."""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass

from common.flatpak_wrappers import running_in_flatpak


@dataclass(frozen=True, slots=True)
class OverlayShortcut:
    desktop_id: str
    label: str
    flag: str
    key: str
    description: str

    @property
    def help_text(self) -> str:
        return f"{self.description} Default shortcut: Alt+{self.key}."

    @property
    def attribute(self) -> str:
        return self.flag.removeprefix("--").replace("-", "_")

    @property
    def gnome_binding(self) -> str:
        key = {"[": "bracketleft", "]": "bracketright", "<": "less", ">": "greater"}.get(self.key, self.key.lower())
        return "<Alt>" + key


SHORTCUTS = (
    OverlayShortcut('ToggleOverlay', 'Toggle overlay', '--overlay-toggle',
                    'X', 'Show or hide the in-game overlay in a running game and exit. Bind this to a desktop shortcut to reach it without leaving the game; the running layer checks visibility every 50 ms.'),
    OverlayShortcut('NextOverlayDetail', 'Cycle overlay preset', '--overlay-detail-next',
                    'C', 'Cycle Small → Medium → Large → Biggest → Small and exit. Custom starts at Small.'),
    OverlayShortcut('NextOverlayGraph', 'Cycle frame timing graphs', '--overlay-graph-next',
                    'G', 'Cycle frame timing graphs: Present calls, Displayed frames, Both.'),
    OverlayShortcut('NextOverlayOpacity', 'Cycle background opacity', '--overlay-opacity-next',
                    'O', 'Cycle background opacity: 50, 60, 70, 75, 80, 85, 90, 95, 100 percent.'),
    OverlayShortcut('SmallerOverlayText', 'Decrease overlay Scale', '--overlay-scale-down',
                    '[', 'Decrease overlay scale one step (minimum 0.5x).'),
    OverlayShortcut('LargerOverlayText', 'Increase overlay Scale', '--overlay-scale-up',
                    ']', 'Increase overlay scale one step (maximum 2x).'),
    OverlayShortcut('PreviousOverlayPosition', 'Previous overlay position', '--overlay-position-prev',
                    '<', 'Cycle to the previous overlay position, wrapping.'),
    OverlayShortcut('NextOverlayPosition', 'Next overlay position', '--overlay-position-next',
                    '>', 'Cycle to the next overlay position, wrapping.'),
 )
SHORTCUT_ACTIONS = tuple((item.label, item.flag) for item in SHORTCUTS)
SHORTCUT_DEFAULTS = {item.flag: "Alt+" + item.key for item in SHORTCUTS}


def shortcut_command(action: str) -> str:
    if action not in {flag for _, flag in SHORTCUT_ACTIONS}:
        raise ValueError(f"Unknown overlay shortcut action: {action}")
    if running_in_flatpak():
        command = [
            "flatpak", "run", "--command=penguin-burner-cli",
            "io.github.jpietek.PenguinBurner",
        ]
    else:
        command = [shutil.which("penguin-burner-cli") or "penguin-burner-cli"]
    return shlex.join([*command, action])


def shortcut_desktop() -> str:
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")
    if "KDE" in desktops:
        return "kde"
    if "GNOME" in desktops:
        return "gnome"
    return "other"


def shortcut_settings_command() -> list[str] | None:
    desktop = shortcut_desktop()
    if desktop == "kde":
        return [
            "/usr/bin/kcmshell6", "kcm_keys", "--args",
            "io.github.jpietek.PenguinBurner.desktop",
        ]
    if desktop == "gnome":
        return ["/usr/bin/gnome-control-center", "keyboard"]
    return None


def shortcut_instructions() -> str:
    desktop = shortcut_desktop()
    if desktop == "kde":
        return (
            "PenguinBurner's overlay actions are provided in KDE shortcut settings. "
            "Choose a key beside an action to change it. "
            "Default keys apply unless you have assigned your own."
        )
    if desktop == "gnome":
        return (
            "Set up all overlay shortcuts automatically. Existing shortcuts and "
            "your customized bindings are preserved. If a default key is already "
            "used, its new action is left unassigned for you to choose a free key."
        )
    return (
        "Add a global shortcut in your desktop settings and paste a command "
        "below. On Sway use bindsym with exec; on Hyprland use bind with exec."
    )
