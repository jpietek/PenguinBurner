#!/usr/bin/env python3

import atexit
import functools
import shutil
import sys
from pathlib import Path

from auto_uv.run.cli_runtime import (
    AutoUvForegroundDependencies,
    run_auto_uv_foreground_command,
)
from cli.arguments import parse_arguments
from cli.entry import dispatch_cli
from cli.interactive_terminal_prompt import prompt_yes_no as cli_prompt_yes_no
from cli.json_event_output import emit_cli_json_event
from cli.main_command_routing import (
    MainCommandRoutingDependencies,
    route_main_command,
)
from cli.runtime_config_file import (
    load_runtime_config,
)
from common.penguin_burner_errors import NvmlError
from common.penguin_burner_paths import (
    claim_desktop_user_ownership,
    default_saved_uv_dir,
    default_user_config_dir,
)
from profiles.uv.profile_fan_curve import AUTO_UV_FAN_CURVE_FILENAME
from profiles.uv.profile_store import auto_uv_profiles_dir
from profiles.verification.runner import run_profile_verification
from runtime.support.runtime_debug import (
    close_debug_log,
    close_stdio_capture,
    debug_log,
    enable_debug_logging,
)
from runtime.support.runtime_service import DEFAULT_JOURNAL_HOURS

atexit.register(close_debug_log)
atexit.register(close_stdio_capture)


def clear_auto_uv_state(*, log=print) -> None:
    config_dir = default_user_config_dir()
    paths = [
        config_dir / "uv-result",
        auto_uv_profiles_dir(),
        config_dir / "auto-uv-final-curve.json",
        config_dir / AUTO_UV_FAN_CURVE_FILENAME,
        config_dir / "debug-logs",
        config_dir / "stability-logs",
        default_saved_uv_dir(),
    ]
    for path in paths:
        path = Path(path)
        if not path.exists():
            log(f"Auto-UV clear: already absent {path}")
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except PermissionError as exc:
            raise NvmlError(
                f"failed to remove {path}: {exc}. Re-run with sudo."
            ) from exc
        log(f"Auto-UV clear: removed {path}")
    log(
        "Auto-UV clear: complete. Q2RTX downloads were left untouched."
    )


def main(argv=None, *, journal_hours=DEFAULT_JOURNAL_HOURS):
    if argv is None:
        argv = sys.argv[1:]
    explicit_cli_args = bool(argv)

    args = parse_arguments(argv)
    if args.debug_log:
        enable_debug_logging(Path(args.config).expanduser(), argv=argv)
    claim_desktop_user_ownership(
        default_user_config_dir(),
        recursive=True,
        include_parents=True,
    )
    claim_desktop_user_ownership(
        default_saved_uv_dir(),
        recursive=True,
        include_parents=True,
    )
    command_route = route_main_command(
        args=args,
        argv=argv,
        explicit_cli_args=explicit_cli_args,
        interactive=sys.stdin.isatty(),
        dependencies=MainCommandRoutingDependencies(
            clear_auto_uv_state=clear_auto_uv_state,
            load_config=load_runtime_config,
            run_profile_verification=run_profile_verification,
            run_auto_uv_foreground_command=functools.partial(
                run_auto_uv_foreground_command,
                dependencies=AutoUvForegroundDependencies(
                    emit_json_event=emit_cli_json_event,
                ),
                program_file=__file__,
                journal_hours=journal_hours,
                prompt_yes_no=functools.partial(
                    cli_prompt_yes_no,
                    debug_log=debug_log,
                ),
            ),
        ),
    )
    if command_route.handled:
        return

    # Foreground Python apply was removed with the Python daemon engine. Privileged
    # profile apply now runs in-process in the Rust penguin-burnerd; reach it over
    # the socket via --daemonize.
    raise NvmlError(
        "apply runtime profiles through the daemon -- re-run with "
        "--daemonize --auto-uv-profile <selector> (foreground apply was removed "
        "with the Rust daemon)"
    )


def cli_main() -> int:
    return dispatch_cli(program_file=__file__, main_callback=main)


if __name__ == "__main__":
    raise SystemExit(cli_main())
