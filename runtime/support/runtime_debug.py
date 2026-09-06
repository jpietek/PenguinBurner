#!/usr/bin/env python3

import os
import platform
import pwd
import shlex
import sys
import time
import traceback
from pathlib import Path

from drivers.nvidia.daemon_gpu import DaemonGpuClient

DEBUG_LOG_ENABLED = False
DEBUG_LOG_PATH = None
DEBUG_LOG_FILE = None
DEBUG_LOG_MAX_BYTES = 700 * 1024
DEBUG_LOG_BYTES_WRITTEN = 0
DEBUG_LOG_TRUNCATED = False
STDIO_CAPTURE_PATH = None
STDIO_CAPTURE_FILE = None
STDIO_CAPTURE_ORIGINAL_STDOUT = None
STDIO_CAPTURE_ORIGINAL_STDERR = None


def log(message):
    text = str(message)
    print(text, flush=True)
    _write_debug_log_line(text)


def debug_log(message):
    if not DEBUG_LOG_ENABLED:
        return
    text = f"[debug] {message}"
    _write_debug_log_line(text)


def close_debug_log():
    global DEBUG_LOG_FILE
    if DEBUG_LOG_FILE is None:
        return
    DEBUG_LOG_FILE.close()
    DEBUG_LOG_FILE = None


class _TeeStream:
    def __init__(self, original, capture_file):
        self._original = original
        self._capture_file = capture_file

    def write(self, text):
        written = self._original.write(text)
        try:
            self._capture_file.write(str(text))
        except Exception:
            pass
        return written

    def flush(self):
        self._original.flush()
        try:
            self._capture_file.flush()
        except Exception:
            pass

    def isatty(self):
        return self._original.isatty()

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._original, "errors", "replace")

    def __getattr__(self, name):
        return getattr(self._original, name)


def close_stdio_capture():
    global \
        STDIO_CAPTURE_FILE, \
        STDIO_CAPTURE_ORIGINAL_STDOUT, \
        STDIO_CAPTURE_ORIGINAL_STDERR
    if STDIO_CAPTURE_FILE is None:
        return
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    if STDIO_CAPTURE_ORIGINAL_STDOUT is not None:
        sys.stdout = STDIO_CAPTURE_ORIGINAL_STDOUT
    if STDIO_CAPTURE_ORIGINAL_STDERR is not None:
        sys.stderr = STDIO_CAPTURE_ORIGINAL_STDERR
    try:
        STDIO_CAPTURE_FILE.close()
    finally:
        STDIO_CAPTURE_FILE = None


def enable_stdio_capture(config_path, *, argv=None, label="stdout"):
    global \
        STDIO_CAPTURE_PATH, \
        STDIO_CAPTURE_FILE, \
        STDIO_CAPTURE_ORIGINAL_STDOUT, \
        STDIO_CAPTURE_ORIGINAL_STDERR
    if STDIO_CAPTURE_FILE is not None:
        return STDIO_CAPTURE_PATH

    config_path = Path(config_path).expanduser().resolve()
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    safe_label = (
        "".join(
            char if char.isalnum() or char in ("-", "_") else "-"
            for char in str(label).strip().lower()
        ).strip("-")
        or "stdout"
    )
    debug_dir = config_path.parent / "debug-logs"
    STDIO_CAPTURE_PATH = debug_dir / f"penguin_burner-{safe_label}-{timestamp}.log"
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        STDIO_CAPTURE_FILE = STDIO_CAPTURE_PATH.open(
            "a",
            encoding="utf-8",
            errors="replace",
            buffering=1,
        )
    except Exception as exc:
        STDIO_CAPTURE_FILE = None
        print(
            f"warning: failed to open stdout/stderr capture under {debug_dir}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None
    STDIO_CAPTURE_ORIGINAL_STDOUT = sys.stdout
    STDIO_CAPTURE_ORIGINAL_STDERR = sys.stderr
    sys.stdout = _TeeStream(STDIO_CAPTURE_ORIGINAL_STDOUT, STDIO_CAPTURE_FILE)
    sys.stderr = _TeeStream(STDIO_CAPTURE_ORIGINAL_STDERR, STDIO_CAPTURE_FILE)
    STDIO_CAPTURE_FILE.write("# PenguinBurner captured stdout/stderr log\n")
    STDIO_CAPTURE_FILE.write(f"# started_at={time.strftime('%Y-%m-%d %H:%M:%S %z')}\n")
    STDIO_CAPTURE_FILE.write(
        f"# argv={shlex.join([Path(sys.argv[0]).name, *(argv or [])])}\n"
    )
    STDIO_CAPTURE_FILE.write(f"# cwd={Path.cwd()}\n")
    STDIO_CAPTURE_FILE.write(f"# config-path={config_path}\n")
    STDIO_CAPTURE_FILE.flush()
    return STDIO_CAPTURE_PATH


def _write_debug_log_line(text):
    global DEBUG_LOG_BYTES_WRITTEN, DEBUG_LOG_TRUNCATED
    if DEBUG_LOG_FILE is None or DEBUG_LOG_TRUNCATED:
        return

    line = str(text) + "\n"
    encoded = line.encode("utf-8", errors="replace")
    if DEBUG_LOG_BYTES_WRITTEN + len(encoded) <= DEBUG_LOG_MAX_BYTES:
        DEBUG_LOG_FILE.write(line)
        DEBUG_LOG_FILE.flush()
        DEBUG_LOG_BYTES_WRITTEN += len(encoded)
        return

    remaining = max(0, DEBUG_LOG_MAX_BYTES - DEBUG_LOG_BYTES_WRITTEN)
    truncation_notice = (
        "[debug] debug log truncated after reaching the 700KB safety limit; "
        "foreground and daemon logging continue normally\n"
    )
    encoded_notice = truncation_notice.encode("utf-8", errors="replace")
    if remaining >= len(encoded_notice):
        DEBUG_LOG_FILE.write(truncation_notice)
        DEBUG_LOG_FILE.flush()
        DEBUG_LOG_BYTES_WRITTEN += len(encoded_notice)
    DEBUG_LOG_TRUNCATED = True


def _write_stdio_capture_line(text):
    if STDIO_CAPTURE_FILE is None:
        return
    try:
        STDIO_CAPTURE_FILE.write(str(text) + "\n")
        STDIO_CAPTURE_FILE.flush()
    except Exception:
        pass


def enable_debug_logging(config_path, *, argv=None):
    global \
        DEBUG_LOG_ENABLED, \
        DEBUG_LOG_PATH, \
        DEBUG_LOG_FILE, \
        DEBUG_LOG_BYTES_WRITTEN, \
        DEBUG_LOG_TRUNCATED
    if DEBUG_LOG_ENABLED:
        return DEBUG_LOG_PATH

    DEBUG_LOG_ENABLED = True
    config_path = Path(config_path).expanduser().resolve()
    debug_dir = config_path.parent / "debug-logs"
    debug_dir.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    DEBUG_LOG_PATH = debug_dir / f"penguin_burner-debug-{timestamp}.log"
    try:
        DEBUG_LOG_FILE = DEBUG_LOG_PATH.open("a", encoding="utf-8", buffering=1)
    except Exception as exc:
        DEBUG_LOG_FILE = None
        print(
            f"warning: failed to open debug log file under {debug_dir}: {exc}",
            file=sys.stderr,
            flush=True,
        )
    DEBUG_LOG_BYTES_WRITTEN = 0
    DEBUG_LOG_TRUNCATED = False
    debug_log("debug log enabled")
    if DEBUG_LOG_PATH is not None:
        debug_log(f"debug-log-path={DEBUG_LOG_PATH}")
    debug_log(f"argv={shlex.join([Path(sys.argv[0]).name, *(argv or [])])}")
    debug_log(f"cwd={Path.cwd()}")
    debug_log(f"config-path={config_path}")
    _debug_log_runtime_environment()
    return DEBUG_LOG_PATH


def debug_exception(context, exc):
    if not DEBUG_LOG_ENABLED and STDIO_CAPTURE_FILE is None:
        return

    def _emit(message):
        if DEBUG_LOG_ENABLED:
            debug_log(message)
        if STDIO_CAPTURE_FILE is not None:
            _write_stdio_capture_line(f"[debug] {message}")

    _emit(f"{context}: {exc.__class__.__name__}: {exc}")
    for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
        for fragment in line.rstrip().splitlines():
            _emit(f"traceback: {fragment}")


def _debug_log_runtime_environment():
    if not DEBUG_LOG_ENABLED:
        return
    debug_log(f"python={sys.version.split()[0]} executable={sys.executable}")
    debug_log(f"platform={platform.platform()}")
    debug_log(
        f"user={pwd.getpwuid(os.getuid()).pw_name} uid={os.getuid()} "
        f"euid={os.geteuid()} sudo-user={os.environ.get('SUDO_USER', '').strip() or '(none)'}"
    )

    try:
        identities = DaemonGpuClient.discover_identities()
        debug_log(f"nvml-gpu-query count={len(identities)}")
        for identity in identities:
            debug_log(
                "nvml-gpu="
                f"index={identity.index} "
                f"name={identity.name} "
                f"driver={identity.driver_version} "
                f"pci_bus_id={identity.pci_bus_id}"
            )
    except Exception as exc:
        debug_exception("failed to query NVML GPU metadata", exc)


def debug_effective_runtime_options(*, config_path, gpu_index, auto_uv_runtime_options):
    if not DEBUG_LOG_ENABLED:
        return
    # Full scans carry per-tier overrides instead of the scan-wide keys; dump
    # whichever are set so debug logs show the values that drive each tier.
    per_tier_options = " ".join(
        f"{key.replace('_', '-')}={value}"
        for key, value in sorted(auto_uv_runtime_options.items())
        if key.startswith(
            ("auto_uv_efficiency_", "auto_uv_balanced_", "auto_uv_performance_")
        )
        and value not in (None, "")
    )
    debug_log(
        "effective-runtime="
        f"config-path={Path(config_path).expanduser().resolve()} "
        f"gpu-index={gpu_index} "
        f"auto-uv-mode={auto_uv_runtime_options.get('auto_uv_mode') or '(default)'} "
        f"auto-uv-min-voltage-mv={auto_uv_runtime_options.get('auto_uv_min_voltage_mv') or '(default)'}"
        + (f" {per_tier_options}" if per_tier_options else "")
    )
