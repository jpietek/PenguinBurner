from __future__ import annotations

import os
import pwd
from collections.abc import Mapping
from pathlib import Path

OVERLAY_STATE_ENV = "PENGUIN_BURNER_OVERLAY_STATE"
OVERLAY_ENABLE_ENV = "PENGUIN_BURNER_OVERLAY"
OVERLAY_ENABLE_ENV_ALIAS = "PB_OVERLAY"
OVERLAY_OVERRIDE_ENV = "PENGUIN_BURNER_OVERLAY_OVERRIDE"


def _home_overlay_dir(env: Mapping[str, str]) -> Path | None:
    home = str(env.get("HOME") or "").strip()
    if home and home != "/root":
        return Path(home).expanduser() / ".cache" / "penguin-burner"

    sudo_uid = str(env.get("SUDO_UID") or "").strip()
    if sudo_uid.isdigit():
        try:
            user_home = pwd.getpwuid(int(sudo_uid)).pw_dir
        except KeyError:
            user_home = ""
        if user_home:
            return Path(user_home) / ".cache" / "penguin-burner"

    sudo_user = str(env.get("SUDO_USER") or "").strip()
    if sudo_user:
        try:
            user_home = pwd.getpwnam(sudo_user).pw_dir
        except KeyError:
            user_home = ""
        if user_home:
            return Path(user_home) / ".cache" / "penguin-burner"

    candidates: list[Path] = []
    try:
        candidates.append(Path.cwd())
    except OSError:
        pass
    try:
        candidates.append(Path(__file__).resolve())
    except OSError:
        pass
    for base in candidates:
        for candidate in (base, *base.parents):
            if candidate.parent == Path("/home"):
                return candidate / ".cache" / "penguin-burner"
    return None


def overlay_state_path(env: Mapping[str, str] | None = None) -> Path:
    resolved_env = os.environ if env is None else env
    explicit = str(resolved_env.get(OVERLAY_STATE_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()

    # The game runs inside Steam's pressure-vessel container, where
    # XDG_RUNTIME_DIR holds only the sockets the container forwards;
    # the home directory is shared, so prefer it for the state files.
    home_dir = _home_overlay_dir(resolved_env)
    if home_dir is not None:
        return home_dir / "overlay-state.txt"

    runtime_dir = str(resolved_env.get("XDG_RUNTIME_DIR") or "").strip()
    if runtime_dir:
        return Path(runtime_dir) / "penguin-burner" / "overlay-state.txt"

    if os.getuid() == 0:
        sudo_uid = str(resolved_env.get("SUDO_UID") or "").strip()
        if sudo_uid.isdigit():
            candidate = Path("/run/user") / sudo_uid
            if candidate.exists():
                return candidate / "penguin-burner" / "overlay-state.txt"
        sudo_user = str(resolved_env.get("SUDO_USER") or "").strip()
        if sudo_user:
            try:
                candidate = Path("/run/user") / str(pwd.getpwnam(sudo_user).pw_uid)
            except KeyError:
                candidate = Path()
            if candidate.exists():
                return candidate / "penguin-burner" / "overlay-state.txt"

    return Path(f"/tmp/penguin-burner-overlay-{os.getuid()}.txt")


def overlay_override_path(env: Mapping[str, str] | None = None) -> Path:
    """The live overlay visibility override the native layer polls (every 50 ms).

    "1"/"0" wins over the game's launch-time env; absent means "follow the
    launch setting". The wrapper clears it at every launch so a stale
    override never leaks into the next game session.
    """
    resolved_env = os.environ if env is None else env
    explicit = str(resolved_env.get(OVERLAY_OVERRIDE_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return overlay_state_path(resolved_env).with_name("overlay-override")


def read_overlay_override(path: str | Path | None = None) -> bool | None:
    """The live override the layer is honouring, or None when it is absent.

    None means "follow the launch setting", which is what the wrapper leaves
    behind at every launch.
    """
    target = overlay_override_path() if path is None else Path(path)
    try:
        value = target.read_text(encoding="utf-8", errors="replace").strip().lower()
    except OSError:
        return None
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def write_overlay_override(enabled: bool, path: str | Path | None = None) -> bool:
    target = overlay_override_path() if path is None else Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        temp.write_text("1" if enabled else "0", encoding="ascii")
        temp.replace(target)
    except OSError:
        return False
    return True


def clear_overlay_override(path: str | Path | None = None) -> None:
    target = overlay_override_path() if path is None else Path(path)
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def read_overlay_state(path: str | Path | None = None) -> dict[str, str]:
    state_path = Path(path).expanduser() if path is not None else overlay_state_path()
    try:
        lines = state_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        values[key] = _unescape_value(value.strip())
    return values


def _unescape_value(value: str) -> str:
    return str(value).replace("\\n", "\n").replace("\\\\", "\\")
