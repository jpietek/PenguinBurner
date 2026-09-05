from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from integrations.steam.vdf import app_value_from_localconfig, quoted_tokens

BARE_PENGUIN_BURNER_WRAPPER = "PENGUIN_BURNER"
PENGUIN_BURNER_WRAPPER = BARE_PENGUIN_BURNER_WRAPPER

# The only token any game needs in its launch line is the PenguinBurner wrapper;
# overlay and in-game latency are opt-in via extra tokens (see steam_game_setup).
# Flatpak installs also register a user Vulkan implicit-layer manifest enabled
# by PENGUIN_BURNER=1, so this short token works even if Steam treats it as an
# environment toggle instead of executing the PATH wrapper.
DEFAULT_REQUIRED_TOKENS = (
    PENGUIN_BURNER_WRAPPER,
)


@dataclass(frozen=True)
class LaunchOptionsCheck:
    app_id: str
    config_path: Path | None
    launch_options: str | None
    required_tokens: tuple[str, ...]
    missing_tokens: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.launch_options is not None and not self.missing_tokens

    def format_text(self) -> str:
        lines = [
            f"app_id={self.app_id}",
            f"config={self.config_path or 'not-found'}",
            f"ok={self.ok}",
        ]
        if self.launch_options is None:
            lines.append("launch_options=not-found")
        else:
            lines.append(f"launch_options={self.launch_options}")
        if self.missing_tokens:
            lines.append("missing:")
            lines.extend(f"- {token}" for token in self.missing_tokens)
        return "\n".join(lines)


@dataclass(frozen=True)
class LaunchOptionsRewrite:
    app_id: str
    config_path: Path | None
    requested_launch_options: str
    previous_launch_options: str | None
    current_launch_options: str | None
    persisted: bool

    @property
    def ok(self) -> bool:
        return self.persisted

    def format_text(self) -> str:
        lines = [
            f"app_id={self.app_id}",
            f"config={self.config_path or 'not-found'}",
            f"persisted={self.persisted}",
            f"requested_launch_options={self.requested_launch_options}",
        ]
        if self.previous_launch_options is None:
            lines.append("previous_launch_options=not-found")
        else:
            lines.append(f"previous_launch_options={self.previous_launch_options}")
        if self.current_launch_options is None:
            lines.append("current_launch_options=not-found")
        else:
            lines.append(f"current_launch_options={self.current_launch_options}")
        return "\n".join(lines)


def default_localconfig_paths(home: Path | None = None) -> list[Path]:
    home = Path.home() if home is None else home
    candidates: list[Path] = []
    for root in (
        home / ".local" / "share" / "Steam" / "userdata",
        home / ".steam" / "steam" / "userdata",
    ):
        candidates.extend(root.glob("*/config/localconfig.vdf"))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            unique.append(path)
            seen.add(resolved)
    return unique


def launch_options_from_localconfig(text: str, app_id: str) -> str | None:
    # The same block walk the rewrite below performs, shared so the reader and
    # the writer cannot drift apart. The library tab reads Playtime through it
    # as well -- one key out of one app's block is the whole shape.
    return app_value_from_localconfig(text, app_id, "LaunchOptions")


def rewrite_launch_options_in_localconfig(
    text: str,
    app_id: str,
    launch_options: str,
) -> tuple[str, str | None] | None:
    lines = text.splitlines(keepends=True)
    if not lines and text:
        lines = [text]
    escaped_launch_options = _vdf_escape(launch_options)

    for index, line in enumerate(lines):
        if quoted_tokens(line) != [app_id]:
            continue

        depth = 0
        entered_block = False
        insert_index: int | None = None
        launch_indent: str | None = None
        close_indent = ""
        for block_index in range(index + 1, len(lines)):
            block_line = lines[block_index]
            stripped = block_line.strip()
            if stripped == "{":
                depth += 1
                entered_block = True
                continue
            if stripped == "}":
                if not entered_block:
                    break
                if depth == 1:
                    insert_index = block_index
                    close_indent = _line_indent(block_line)
                depth -= 1
                if depth <= 0:
                    break
                continue
            if not entered_block or depth != 1:
                continue

            tokens = quoted_tokens(block_line)
            if launch_indent is None and tokens:
                launch_indent = _line_indent(block_line)
            if len(tokens) >= 2 and tokens[0] == "LaunchOptions":
                previous = tokens[1]
                lines[block_index] = (
                    f"{_line_indent(block_line)}"
                    f"\"LaunchOptions\"\t\t\"{escaped_launch_options}\""
                    f"{_line_ending(block_line)}"
                )
                return "".join(lines), previous

        if insert_index is None:
            return None
        indent = launch_indent if launch_indent is not None else f"{close_indent}\t"
        line_ending = _line_ending(lines[insert_index]) or "\n"
        lines.insert(
            insert_index,
            f"{indent}\"LaunchOptions\"\t\t\"{escaped_launch_options}\"{line_ending}",
        )
        return "".join(lines), None

    return None


def rewrite_launch_options(
    *,
    app_id: str,
    launch_options: str,
    config_paths: list[Path] | None = None,
    verify_delay_s: float = 1.5,
    poll_interval_s: float = 0.1,
) -> LaunchOptionsRewrite:
    paths = config_paths if config_paths is not None else default_localconfig_paths()
    requested = str(launch_options)
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rewrite = rewrite_launch_options_in_localconfig(text, app_id, requested)
        if rewrite is None:
            continue
        updated_text, previous = rewrite
        path.write_text(updated_text, encoding="utf-8")
        current = _verified_launch_options(
            path,
            app_id=app_id,
            delay_s=verify_delay_s,
            poll_interval_s=poll_interval_s,
        )
        return LaunchOptionsRewrite(
            app_id=app_id,
            config_path=path,
            requested_launch_options=requested,
            previous_launch_options=previous,
            current_launch_options=current,
            persisted=current == requested,
        )

    return LaunchOptionsRewrite(
        app_id=app_id,
        config_path=None,
        requested_launch_options=requested,
        previous_launch_options=None,
        current_launch_options=None,
        persisted=False,
    )


def check_launch_options(
    *,
    app_id: str,
    required_tokens: tuple[str, ...],
    config_paths: list[Path] | None = None,
) -> LaunchOptionsCheck:
    paths = config_paths if config_paths is not None else default_localconfig_paths()
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        launch_options = launch_options_from_localconfig(text, app_id)
        if launch_options is None:
            continue
        missing = tuple(
            token for token in required_tokens if token not in launch_options.split()
        )
        return LaunchOptionsCheck(
            app_id=app_id,
            config_path=path,
            launch_options=launch_options,
            required_tokens=required_tokens,
            missing_tokens=missing,
        )

    return LaunchOptionsCheck(
        app_id=app_id,
        config_path=None,
        launch_options=None,
        required_tokens=required_tokens,
        missing_tokens=required_tokens,
    )


def _vdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _line_indent(line: str) -> str:
    match = re.match(r"\s*", line)
    return match.group(0) if match else ""


def _line_ending(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    if line.endswith("\r"):
        return "\r"
    return ""


def _verified_launch_options(
    path: Path,
    *,
    app_id: str,
    delay_s: float,
    poll_interval_s: float,
) -> str | None:
    deadline = time.monotonic() + max(0.0, float(delay_s))
    current = _read_launch_options(path, app_id)
    while time.monotonic() < deadline:
        sleep_s = min(max(0.01, float(poll_interval_s)), deadline - time.monotonic())
        if sleep_s > 0:
            time.sleep(sleep_s)
        current = _read_launch_options(path, app_id)
    return current


def _read_launch_options(path: Path, app_id: str) -> str | None:
    try:
        return launch_options_from_localconfig(
            path.read_text(encoding="utf-8", errors="replace"),
            app_id,
        )
    except OSError:
        return None
