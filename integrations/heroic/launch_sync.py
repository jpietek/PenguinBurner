"""Refresh Heroic's cached launch settings before handing it a Play request.

Heroic has no external config-reload API. Keep a receipt for the configuration
loaded by a fresh launcher, and restart an idle launcher when that receipt no
longer matches. This runs on the launch worker, never on the Qt thread.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from common.atomic_write import atomic_write_text
from common.penguin_burner_paths import default_user_config_dir
from integrations.launchers.host_process import run_on_host, running_in_flatpak, start_on_host

from .config_store import entries_command, read_global_entries
from .paths import heroic_config_root

_RESTART_TIMEOUT_S = 10.0

# Inspect and signal on the host so native PenguinBurner and its Flatpak use
# the same PID namespace. Only same-user Heroic main processes for this config
# root can be stopped; Electron renderers and other profiles are not targets.
_LAUNCHER_PROBE = r'''
import json, os, signal, sys
from pathlib import Path

root = Path(sys.argv[1])
expected = json.loads(sys.argv[2])
boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
processes = {}
launchers = {}
for proc in Path('/proc').iterdir():
    if not proc.name.isdecimal():
        continue
    try:
        if proc.stat().st_uid != os.getuid():
            continue
        command = [os.fsdecode(arg) for arg in (proc / 'cmdline').read_bytes().split(b'\0') if arg]
        stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        if not command or stat[0] == 'Z':
            continue
        pid = int(proc.name)
        name = Path(command[0]).name.lower()
        # Electron can flatten argv into its process title after startup.
        if (proc / 'comm').read_text().strip().lower() == 'heroic':
            name = Path(os.readlink(proc / 'exe')).name.lower()
        processes[pid] = (int(stat[1]), name, command)
        if name != 'heroic':
            continue
        if any(arg.startswith('--type=') or ' --type=' in arg for arg in command):
            continue
        env = dict(field.split(b'=', 1) for field in (proc / 'environ').read_bytes().split(b'\0') if b'=' in field)
        # Zypak starts Electron with only LD_PRELOAD in its kernel-visible
        # environment. Its heroic-run parent still carries the config root.
        parent = int(stat[1])
        for _ in range(8):
            if b'XDG_CONFIG_HOME' in env or b'HOME' in env or parent <= 1:
                break
            ancestor = Path('/proc') / str(parent)
            if ancestor.stat().st_uid != os.getuid():
                break
            env = dict(field.split(b'=', 1) for field in (ancestor / 'environ').read_bytes().split(b'\0') if b'=' in field)
            parent = int((ancestor / 'stat').read_text().rsplit(')', 1)[1].split()[1])
        if b'XDG_CONFIG_HOME' not in env and b'HOME' not in env:
            raise RuntimeError('Cannot identify the running Heroic configuration.')
        config = Path(os.fsdecode(env.get(b'XDG_CONFIG_HOME', env.get(b'HOME', b'') + b'/.config'))) / 'heroic'
        if config != root:
            continue
        launchers[str(pid)] = f'{boot}:{stat[19]}'
    except (FileNotFoundError, ProcessLookupError):
        continue

busy = False
for pid, (parent, executable, command) in processes.items():
    if str(parent) in launchers and executable not in (
        'heroic', 'chrome_crashpad_handler', 'zypak-helper', 'zypak-sandbox',
    ):
        busy = True
queue_path = root / 'store/download-manager.json'
if queue_path.exists():
    busy = busy or bool(json.loads(queue_path.read_text()).get('queue'))

if expected:
    if launchers != expected:
        raise RuntimeError('Heroic changed while preparing to restart; try Play again.')
    if busy:
        raise RuntimeError('Heroic is busy. Finish its game, download or other operation, then press Play again.')
    for pid in launchers:
        os.kill(int(pid), signal.SIGTERM)
print(json.dumps({'launchers': launchers, 'busy': busy}))
'''


def _probe(root: Path, stop: dict | None = None) -> dict:
    python = (
        os.environ.get("PENGUIN_BURNER_HOST_PYTHON") or "/usr/bin/python3"
        if running_in_flatpak() else sys.executable
    )
    result = run_on_host(
        [python, "-c", _LAUNCHER_PROBE, str(root), json.dumps(stop or {})],
        capture=True,
    )
    if result is None or result.returncode:
        raise RuntimeError("Could not safely refresh Heroic's running launcher. Try Play again.")
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload["launchers"], dict) or not isinstance(payload["busy"], bool):
            raise ValueError("invalid launcher probe")
        return payload
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Could not read Heroic's running launcher state.") from error


def _settings_fingerprint(root: Path, home: Path | None) -> str:
    """Hash effective wrapper commands, ignoring Heroic's formatting rewrites."""
    global_command = entries_command(read_global_entries(home))
    commands = {}
    for path in sorted((root / "GamesConfig").glob("*.json")):
        document = json.loads(path.read_text())
        settings = document.get(path.stem, {})
        commands[path.stem] = (
            entries_command(settings["wrapperOptions"])
            if "wrapperOptions" in settings else global_command
        )
    # Installing a new Flatpak payload may also introduce filesystem grants
    # that an already-running sandbox has not received.
    base = home if home is not None else Path.home()
    wrapper = base / ".var/app/com.heroicgameslauncher.hgl/data/penguin-burner/PENGUIN_BURNER"
    payload = {
        "default": global_command, "games": commands,
        "flatpak_runtime": wrapper.read_text() if wrapper.is_file() else "",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def launch_with_current_settings(command: list[str], *, home: Path | None = None) -> bool:
    """Refresh an idle launcher if needed, then deliver the original launch URL.

    A missing receipt means we cannot trust a running launcher's cache. After
    one fresh start, further launches reuse it until wrapper settings change.
    Never terminate a game or a busy launcher, and never escalate to SIGKILL.
    """
    root = heroic_config_root(home)
    config_dir = home / ".config/PenguinBurner" if home is not None else default_user_config_dir()
    receipt_path = config_dir / "heroic-launch-receipt.json"
    fingerprint = _settings_fingerprint(root, home)
    state = _probe(root)
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError):
        receipt = {}
    if state["launchers"] and receipt == {
        "root": str(root), "launchers": state["launchers"], "settings": fingerprint,
    }:
        return start_on_host(command)
    if state["launchers"]:
        if state["busy"]:
            raise RuntimeError(
                "Heroic needs to reload changed settings but is busy. "
                "Finish its game, download or other operation, then press Play again."
            )
        _probe(root, stop=state["launchers"])
        deadline = time.monotonic() + _RESTART_TIMEOUT_S
        while _probe(root)["launchers"]:
            if time.monotonic() >= deadline:
                raise RuntimeError("Heroic did not exit; the game was not launched with stale settings.")
            time.sleep(0.1)
    if not start_on_host(command):
        return False
    deadline = time.monotonic() + _RESTART_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            state = _probe(root)
        except RuntimeError:
            # The request already succeeded. Missing a cache receipt must not
            # turn a launched game into a failed launch in the UI.
            return True
        if state["launchers"]:
            # Record the pre-launch fingerprint, never bless a settings write
            # that raced the start of this launcher.
            try:
                config_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_text(receipt_path, json.dumps({
                    "root": str(root), "launchers": state["launchers"], "settings": fingerprint,
                }), durable=True)
            except OSError:
                pass  # A receipt is an optimization, not a launch requirement.
            return True
        time.sleep(0.1)
    # The launch request was delivered, but no persistent launcher was found.
    # Do not create a receipt that would claim any future cache is fresh.
    return True
