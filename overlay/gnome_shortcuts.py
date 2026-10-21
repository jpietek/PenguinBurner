"""Explicit, non-destructive setup of GNOME overlay keyboard shortcuts."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from integrations.launchers.host_process import run_on_host
from overlay.shortcuts import SHORTCUT_ACTIONS, shortcut_command, shortcut_desktop

MEDIA_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = MEDIA_SCHEMA + ".custom-keybinding"
CUSTOM_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
GNOME_DEFAULTS = {
    "--overlay-toggle": "<Alt>x",
    "--overlay-detail-next": "<Alt>c",
    "--overlay-scale-down": "<Alt>bracketleft",
    "--overlay-scale-up": "<Alt>bracketright",
    "--overlay-position-prev": "<Alt>less",
    "--overlay-position-next": "<Alt>greater",
}
_STRING = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", re.DOTALL)
_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\n": ""}


@dataclass(frozen=True)
class GnomeShortcutSetupResult:
    success: bool
    message: str
    added: tuple[str, ...] = ()
    preserved: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


class _SetupError(Exception):
    pass


def _run(*arguments: str) -> str:
    result = run_on_host(["/usr/bin/gsettings", *arguments], capture=True)
    if result is None:
        raise _SetupError("Could not reach GNOME settings on the host.")
    if result.returncode:
        detail = (result.stderr or result.stdout or "Unknown settings error").strip()
        raise _SetupError(f"GNOME settings: {detail}")
    return (result.stdout or "").strip()


def _decode_string(body: str) -> str:
    """Decode only GVariant string escapes, without evaluating Python text."""
    def replace(match: re.Match[str]) -> str:
        value = match.group(1)
        if value.startswith(("u", "U")):
            return chr(int(value[1:], 16))
        return _ESCAPES.get(value, value)

    return re.sub(r"\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|.)", replace, body, flags=re.DOTALL)


def _parse_value(text: str) -> str | list[str]:
    value = text.strip()
    for annotation in ("@as ", "@s ", "string "):
        if value.startswith(annotation):
            value = value[len(annotation):].lstrip()
            break
    match = _STRING.fullmatch(value)
    if match:
        return _decode_string(match.group(1) if match.group(1) is not None else match.group(2))
    if value.startswith("[") and value.endswith("]"):
        remaining = value[1:-1].strip()
        items = []
        while remaining:
            match = _STRING.match(remaining)
            if not match:
                break
            items.append(_decode_string(match.group(1) if match.group(1) is not None else match.group(2)))
            remaining = remaining[match.end():].strip()
            if not remaining:
                return items
            if not remaining.startswith(","):
                break
            remaining = remaining[1:].strip()
        if not remaining:
            return items
    raise _SetupError("GNOME returned an unsupported shortcut setting value.")


def _read_string(schema: str, key: str) -> str:
    value = _parse_value(_run("get", schema, key))
    if not isinstance(value, str):
        raise _SetupError(f"GNOME setting {key} is not text.")
    return value


def _read_paths() -> list[str]:
    value = _parse_value(_run("get", MEDIA_SCHEMA, "custom-keybindings"))
    if not isinstance(value, list) or any(not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)+", path) for path in value):
        raise _SetupError("GNOME returned an invalid custom shortcut path list.")
    return value


def _read_entry(path: str) -> dict[str, str]:
    return {key: _read_string(f"{CUSTOM_SCHEMA}:{path}", key) for key in ("name", "command", "binding")}


def _action_for_command(command: str) -> str | None:
    try:
        arguments = shlex.split(command)
    except ValueError:
        return None
    if len(arguments) == 2 and PurePosixPath(arguments[0]).name in {"pburn-cli", "penguin-burner-cli"}:
        action = arguments[1]
    elif len(arguments) == 5 and PurePosixPath(arguments[0]).name == "flatpak" and arguments[1:4] == [
        "run", "--command=penguin-burner-cli", "io.github.jpietek.PenguinBurner",
    ]:
        action = arguments[4]
    else:
        return None
    return action if action in GNOME_DEFAULTS else None


def _normalized_binding(binding: str) -> tuple[tuple[str, ...], str]:
    modifiers = {part.lower() for part in re.findall(r"<([^>]+)>", binding)}
    modifiers = {"alt" if value == "mod1" else "ctrl" if value in {"control", "primary"} else value for value in modifiers}
    key = re.sub(r"<[^>]+>", "", binding).lower()
    key = {"[": "bracketleft", "]": "bracketright", ",": "comma", ".": "period"}.get(key, key)
    if "shift" in modifiers and key in {"comma", "period", "less", "greater"}:
        key = {"comma": "less", "period": "greater"}.get(key, key)
        modifiers.remove("shift")
    return tuple(sorted(modifiers)), key


def _occupied_bindings(schemas: set[str], entries: dict[str, dict[str, str]]) -> set[tuple[tuple[str, ...], str]]:
    occupied = {_normalized_binding(entry["binding"]) for entry in entries.values() if entry["binding"]}
    for schema in sorted(schemas):
        if not schema.startswith("org.gnome.") or ("keybindings" not in schema and schema not in {MEDIA_SCHEMA, "org.gnome.shell"}):
            continue
        for line in _run("list-recursively", schema).splitlines():
            parts = line.split(None, 2)
            if len(parts) != 3:
                raise _SetupError("GNOME returned incomplete keyboard settings.")
            text = parts[2]
            if not text.startswith(("'", '"', "[", "@as ", "@s ")):
                continue
            try:
                value = _parse_value(text)
            except _SetupError:
                # Shell also stores structured arrays such as app-picker-layout.
                # Only strings and string arrays can contain accelerators.
                continue
            for binding in [value] if isinstance(value, str) else value:
                if binding and not binding.startswith("/"):
                    occupied.add(_normalized_binding(binding))
    return occupied


def _writable(schema: str, key: str) -> None:
    if _run("writable", schema, key) != "true":
        raise _SetupError(f"GNOME setting {key} is locked by system policy.")


def _rollback(writes: list[tuple[str, str, str]], added_paths: list[str]) -> bool:
    """Undo our new entries, preserving unrelated and concurrently edited values."""
    clean = True
    expected: dict[str, dict[str, str]] = {}
    for schema, key, value in writes:
        expected.setdefault(schema, {"name": "", "command": "", "binding": ""})[key] = value
    untouched = set()
    for schema, values in expected.items():
        try:
            if _read_entry(schema.split(":", 1)[1]) == values:
                untouched.add(schema)
            else:
                clean = False
        except _SetupError:
            clean = False
    try:
        current = _read_paths()
        removable = {path for path in added_paths if f"{CUSTOM_SCHEMA}:{path}" in untouched}
        remaining = [path for path in current if path not in removable]
        if remaining != current:
            _run("set", MEDIA_SCHEMA, "custom-keybindings", json.dumps(remaining))
    except _SetupError:
        clean = False
    for schema, key, value in reversed(writes):
        if schema not in untouched:
            continue
        try:
            if _read_string(schema, key) == value:
                _run("reset", schema, key)
            else:
                clean = False
        except _SetupError:
            clean = False
    return clean


def setup_gnome_shortcuts() -> GnomeShortcutSetupResult:
    """Install missing actions only after the user explicitly requests setup."""
    if shortcut_desktop() != "gnome":
        return GnomeShortcutSetupResult(False, "Automatic GNOME shortcut setup is available in a GNOME session.")
    writes: list[tuple[str, str, str]] = []
    added_paths: list[str] = []
    added: list[str] = []
    preserved: list[str] = []
    conflicts: list[str] = []
    try:
        schemas = set(_run("list-schemas").splitlines())
        relocatable = set(_run("list-relocatable-schemas").splitlines())
        if MEDIA_SCHEMA not in schemas or CUSTOM_SCHEMA not in relocatable:
            raise _SetupError("GNOME's custom keyboard shortcut settings are not installed on the host.")
        paths = _read_paths()
        entries = {path: _read_entry(path) for path in paths}
        existing = {_action_for_command(entry["command"]) for entry in entries.values()}
        occupied = _occupied_bindings(schemas, entries)
        planned: list[tuple[str, str, dict[str, str]]] = []
        for label, action in SHORTCUT_ACTIONS:
            if action in existing:
                preserved.append(label)
                continue
            stem = "penguinburner-" + action.removeprefix("--overlay-")
            for index in range(1, 101):
                suffix = "" if index == 1 else f"-{index}"
                path = f"{CUSTOM_PATH}{stem}{suffix}/"
                entry = entries.get(path) or _read_entry(path)
                if _action_for_command(entry["command"]) == action:
                    # A removed owned entry stays disabled, as does an empty binding.
                    preserved.append(label)
                    break
                if path in paths or any(entry.values()):
                    continue
                binding = GNOME_DEFAULTS[action]
                if _normalized_binding(binding) in occupied:
                    binding = ""
                    conflicts.append(label)
                values = {"name": f"PenguinBurner: {label}", "command": shortcut_command(action), "binding": binding}
                planned.append((path, label, values))
                occupied.add(_normalized_binding(binding))
                break
            else:
                raise _SetupError("No unused GNOME shortcut entry is available for PenguinBurner.")
        if planned:
            _writable(MEDIA_SCHEMA, "custom-keybindings")
        for path, _label, values in planned:
            for key in values:
                _writable(f"{CUSTOM_SCHEMA}:{path}", key)
        for path, label, values in planned:
            if any(_read_entry(path).values()):
                raise _SetupError("GNOME shortcuts changed during setup; please try again.")
            for key, value in values.items():
                schema = f"{CUSTOM_SCHEMA}:{path}"
                _run("set", schema, key, json.dumps(value, ensure_ascii=False))
                writes.append((schema, key, value))
                if _read_string(schema, key) != value:
                    raise _SetupError("GNOME did not save a shortcut setting.")
            added_paths.append(path)
            added.append(label)
        if added_paths:
            current = _read_paths()
            merged = list(dict.fromkeys([*current, *added_paths]))
            _run("set", MEDIA_SCHEMA, "custom-keybindings", json.dumps(merged))
            if not set(added_paths).issubset(_read_paths()):
                raise _SetupError("GNOME did not activate the new shortcut entries.")
    except (ValueError, _SetupError) as error:
        clean = _rollback(writes, added_paths) if writes else True
        message = str(error)
        if not clean:
            message += " Some new entries could not be rolled back; review GNOME shortcut settings."
        return GnomeShortcutSetupResult(False, message)
    message = "Overlay shortcuts are ready in GNOME Settings. Existing key assignments were preserved."
    if conflicts:
        message += " Choose an available key for: " + ", ".join(conflicts) + ". Their default keys are already in use."
    return GnomeShortcutSetupResult(True, message, tuple(added), tuple(preserved), tuple(conflicts))
