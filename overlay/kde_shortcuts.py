"""Point a `pip --user` desktop entry at its own scripts for KDE shortcuts.

The packaged entry runs `penguin-burner-cli` by bare name. A `--user` install
puts that script in the user base's bin directory, which the Plasma session
PATH usually lacks, so every overlay shortcut fails with "Could not find the
program". Distribution packages install to /usr and are left alone.
"""

from __future__ import annotations

import re
import site
from pathlib import Path

from common.atomic_write import atomic_write_text
from common.flatpak_wrappers import running_in_flatpak

DESKTOP_ID = "io.github.jpietek.PenguinBurner.desktop"
ENTRY_COMMANDS = ("penguin-burner", "penguin-burner-cli")
_PLAIN_ARGUMENT = re.compile(r"[A-Za-z0-9/._+-]+")


def _exec_argument(path: Path) -> str:
    """Quote one Exec argument as the Desktop Entry specification requires."""
    text = str(path).replace("%", "%%")
    if _PLAIN_ARGUMENT.fullmatch(text):
        return text
    escaped = re.sub(r'(["`$\\])', r"\\\1", text)
    return f'"{escaped}"'


def pin_exec_commands(text: str, commands: dict[str, Path]) -> str:
    """Replace bare `Exec=<command>` programs with their absolute paths."""
    lines = []
    for line in text.splitlines(keepends=True):
        if line.startswith("Exec="):
            program, separator, rest = line[len("Exec="):].partition(" ")
            program_name = program.rstrip("\r\n")
            if program_name in commands:
                ending = program[len(program_name):]
                line = f"Exec={_exec_argument(commands[program_name])}{ending}{separator}{rest}"
        lines.append(line)
    return "".join(lines)


def repair_user_desktop_entry() -> Path | None:
    """Rewrite the user-base desktop entry in place. Return it when changed."""
    if running_in_flatpak():
        return None
    base = Path(site.getuserbase())
    entry = base / "share" / "applications" / DESKTOP_ID
    if not entry.is_file():
        return None
    commands = {
        name: base / "bin" / name
        for name in ENTRY_COMMANDS
        if (base / "bin" / name).is_file()
    }
    text = entry.read_text(encoding="utf-8")
    pinned = pin_exec_commands(text, commands)
    if pinned == text:
        return None
    atomic_write_text(entry, pinned)
    return entry
