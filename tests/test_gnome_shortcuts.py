from __future__ import annotations

import json
import subprocess

import pytest

from overlay import gnome_shortcuts as shortcuts


class SettingsHost:
    def __init__(self):
        self.schemas = {shortcuts.MEDIA_SCHEMA, "org.gnome.desktop.wm.keybindings"}
        self.paths = []
        self.values = {}
        self.builtins = {}
        self.schema_builtins = {}
        self.calls = []
        self.locked = set()
        self.fail_key = None
        self.fail_list = False
        self.malformed_paths = None

    def entry(self, path, command, binding, name="Existing shortcut"):
        self.paths.append(path)
        schema = f"{shortcuts.CUSTOM_SCHEMA}:{path}"
        self.values.update({(schema, "name"): name, (schema, "command"): command, (schema, "binding"): binding})

    def run(self, command, *, capture=False):
        assert capture
        assert command[0] == "/usr/bin/gsettings"
        self.calls.append(command[1:])
        operation, *args = command[1:]
        output = ""
        if operation == "list-schemas":
            output = "\n".join(self.schemas)
        elif operation == "list-relocatable-schemas":
            output = shortcuts.CUSTOM_SCHEMA
        elif operation == "list-recursively":
            bindings = self.schema_builtins.get(args[0], self.builtins if args[0] == "org.gnome.desktop.wm.keybindings" else {})
            output = "\n".join(f"{args[0]} {key} {json.dumps(value)}" for key, value in bindings.items())
        elif operation == "writable":
            output = "false" if tuple(args) in self.locked else "true"
        elif operation == "get":
            if args == [shortcuts.MEDIA_SCHEMA, "custom-keybindings"]:
                output = self.malformed_paths or json.dumps(self.paths)
            else:
                output = json.dumps(self.values.get(tuple(args), ""))
        elif operation == "set":
            schema, key, value = args
            if key == self.fail_key or (key == "custom-keybindings" and self.fail_list):
                self.fail_key = None
                self.fail_list = False
                return subprocess.CompletedProcess(command, 1, "", "Settings write failed")
            if key == "custom-keybindings":
                self.paths = json.loads(value)
            else:
                self.values[(schema, key)] = json.loads(value)
        elif operation == "reset":
            self.values.pop(tuple(args), None)
        else:
            raise AssertionError(operation)
        return subprocess.CompletedProcess(command, 0, output, "")


@pytest.fixture
def host(monkeypatch):
    fake = SettingsHost()
    monkeypatch.setattr(shortcuts, "run_on_host", fake.run)
    monkeypatch.setattr(shortcuts, "shortcut_desktop", lambda: "gnome")
    monkeypatch.setattr(shortcuts, "shortcut_command", lambda action: f"/opt/Penguin Burner/bin/pburn-cli {action}")
    return fake


def test_registers_all_and_is_idempotent_preserving_unrelated(host, monkeypatch):
    monkeypatch.setattr(shortcuts, "shortcut_command", lambda action: f"'/opt/Penguin Burner/bin/pburn-cli' {action}")
    unrelated = shortcuts.CUSTOM_PATH + "custom0/"
    host.entry(unrelated, "unrelated --arg", "<Super>u")
    unrelated_values = dict(host.values)
    first = shortcuts.setup_gnome_shortcuts()
    assert first.success
    assert len(first.added) == 8
    assert len(host.paths) == 9
    assert host.paths[0] == unrelated
    for key, value in unrelated_values.items():
        assert host.values[key] == value
    before = dict(host.values), list(host.paths)
    host.calls.clear()
    second = shortcuts.setup_gnome_shortcuts()
    assert second.success
    assert len(second.preserved) == 8
    assert not second.added
    assert (host.values, host.paths) == before
    assert not any(call[0] in {"set", "reset"} for call in host.calls)


@pytest.mark.parametrize("binding", ["<Super>F10", ""])
def test_preserves_user_binding_and_disabled_entry(host, binding):
    path = shortcuts.CUSTOM_PATH + "user-toggle/"
    host.entry(path, "/home/user/.local/bin/pburn-cli --overlay-toggle", binding)
    result = shortcuts.setup_gnome_shortcuts()
    assert result.success
    assert "Toggle overlay" in result.preserved
    assert host.values[(f"{shortcuts.CUSTOM_SCHEMA}:{path}", "binding")] == binding
    assert len(result.added) == 7


def test_preserves_removed_owned_shortcut(host):
    path = shortcuts.CUSTOM_PATH + "penguinburner-toggle/"
    host.entry(path, "pburn-cli --overlay-toggle", "<Alt>x")
    host.paths.remove(path)
    result = shortcuts.setup_gnome_shortcuts()
    assert result.success
    assert "Toggle overlay" in result.preserved
    assert path not in host.paths


def test_occupied_owned_path_gets_unused_suffix(host):
    path = shortcuts.CUSTOM_PATH + "penguinburner-toggle/"
    host.entry(path, "unrelated-tool", "<Super>u")
    before = dict(host.values)
    result = shortcuts.setup_gnome_shortcuts()
    assert result.success
    assert shortcuts.CUSTOM_PATH + "penguinburner-toggle-2/" in host.paths
    assert all(host.values[key] == value for key, value in before.items())


def test_conflicts_register_unassigned_without_stealing(host):
    host.builtins = {"builtin-action": ["<Mod1>x", "<Shift><Alt>comma"]}
    host.entry(shortcuts.CUSTOM_PATH + "other/", "another-tool", "<Alt>bracketright")
    before = dict(host.values)
    result = shortcuts.setup_gnome_shortcuts()
    assert result.success
    assert set(result.conflicts) == {"Toggle overlay", "Previous overlay position", "Increase overlay Scale"}
    for suffix in ["toggle", "position-prev", "scale-up"]:
        path = shortcuts.CUSTOM_PATH + f"penguinburner-{suffix}/"
        assert host.values[(f"{shortcuts.CUSTOM_SCHEMA}:{path}", "binding")] == ""
    assert all(host.values[key] == value for key, value in before.items())
    assert "already in use" in result.message


def test_locked_setting_preflight_makes_no_writes(host):
    path = shortcuts.CUSTOM_PATH + "penguinburner-position-next/"
    host.locked.add((f"{shortcuts.CUSTOM_SCHEMA}:{path}", "binding"))
    result = shortcuts.setup_gnome_shortcuts()
    assert not result.success
    assert "locked" in result.message
    assert not any(call[0] in {"set", "reset"} for call in host.calls)


def test_shell_builtin_conflict_is_detected(host):
    host.schemas.add("org.gnome.shell")
    host.schema_builtins["org.gnome.shell"] = {
        "toggle-overview": ["<Alt>c"],
        "app-picker-layout": [[{"application.desktop": {"position": 0}}]],
    }
    result = shortcuts.setup_gnome_shortcuts()
    assert result.success
    assert result.conflicts == ("Cycle overlay preset",)


def test_rollback_preserves_entire_concurrently_edited_entry(host):
    path = shortcuts.CUSTOM_PATH + "penguinburner-toggle/"
    schema = f"{shortcuts.CUSTOM_SCHEMA}:{path}"
    host.entry(path, "pburn-cli --overlay-toggle", "<Super>x", "PenguinBurner: Toggle overlay")
    before = dict(host.values), list(host.paths)
    writes = [(schema, "name", "PenguinBurner: Toggle overlay"), (schema, "command", "pburn-cli --overlay-toggle"), (schema, "binding", "<Alt>x")]
    assert not shortcuts._rollback(writes, [path])
    assert (host.values, host.paths) == before


@pytest.mark.parametrize("fail", ["command", "list"])
def test_failed_setup_rolls_back_only_new_entries(host, fail):
    host.entry(shortcuts.CUSTOM_PATH + "other/", "another-tool", "<Super>a")
    before = dict(host.values), list(host.paths)
    if fail == "list":
        host.fail_list = True
    else:
        host.fail_key = fail
    result = shortcuts.setup_gnome_shortcuts()
    assert not result.success
    assert "Settings write failed" in result.message
    assert (host.values, host.paths) == before


def test_missing_schema_fails_before_writes(host):
    host.schemas.remove(shortcuts.MEDIA_SCHEMA)
    result = shortcuts.setup_gnome_shortcuts()
    assert not result.success
    assert "not installed" in result.message
    assert not any(call[0] in {"set", "reset"} for call in host.calls)


def test_non_gnome_does_not_call_host(host, monkeypatch):
    monkeypatch.setattr(shortcuts, "shortcut_desktop", lambda: "kde")
    assert not shortcuts.setup_gnome_shortcuts().success
    assert not host.calls


def test_unavailable_host_bridge_reports_error(host, monkeypatch):
    monkeypatch.setattr(shortcuts, "run_on_host", lambda *_args, **_kwargs: None)
    result = shortcuts.setup_gnome_shortcuts()
    assert not result.success
    assert "host" in result.message


@pytest.mark.parametrize("value,expected", [
    ("@as []", []),
    ("['one', \"two\"]", ["one", "two"]),
    (r"'quoted \' command \\ path\n'", "quoted ' command \\ path\n"),
    (r"'\u0142\U0001f600'", "ł😀"),
])
def test_gvariant_string_parser(value, expected):
    assert shortcuts._parse_value(value) == expected


@pytest.mark.parametrize("value", ["__import__('os').system('echo wrong')", "['one', 2]", "['one'] trailing", "{'wrong': 'type'}"])
def test_gvariant_parser_rejects_non_string_shapes(value):
    with pytest.raises(shortcuts._SetupError):
        shortcuts._parse_value(value)


def test_invalid_paths_fail_before_writes(host):
    host.malformed_paths = "['bad:path']"
    result = shortcuts.setup_gnome_shortcuts()
    assert not result.success
    assert not any(call[0] in {"set", "reset"} for call in host.calls)


def test_flatpak_command_is_recognized_and_preserved(host):
    host.entry(shortcuts.CUSTOM_PATH + "flatpak/", "flatpak run --command=penguin-burner-cli io.github.jpietek.PenguinBurner --overlay-toggle", "<Alt>x")
    result = shortcuts.setup_gnome_shortcuts()
    assert result.success
    assert "Toggle overlay" in result.preserved
