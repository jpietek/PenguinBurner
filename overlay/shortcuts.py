"""Commands and desktop guidance for overlay global shortcuts."""

from __future__ import annotations

import os
import shlex
import shutil

from common.flatpak_wrappers import running_in_flatpak

SHORTCUT_ACTIONS = (
    ("Toggle overlay", "--overlay-toggle"),
    ("Cycle overlay preset", "--overlay-detail-next"),
    ("Decrease overlay Scale", "--overlay-scale-down"),
    ("Increase overlay Scale", "--overlay-scale-up"),
    ("Previous overlay position", "--overlay-position-prev"),
    ("Next overlay position", "--overlay-position-next"),
)
SHORTCUT_DEFAULTS = {
    "--overlay-toggle": "Alt+X",
    "--overlay-detail-next": "Alt+C",
    "--overlay-scale-down": "Alt+[",
    "--overlay-scale-up": "Alt+]",
    "--overlay-position-prev": "Alt+<",
    "--overlay-position-next": "Alt+>",
}


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
            "Set up all six overlay shortcuts automatically. Existing shortcuts and "
            "your customized bindings are preserved. If a default key is already "
            "used, its new action is left unassigned for you to choose a free key."
        )
    return (
        "Add a global shortcut in your desktop settings and paste a command "
        "below. On Sway use bindsym with exec; on Hyprland use bind with exec."
    )
