from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common.cli_output import enable_cli_output_wrapping

from .assets import _validate_demo_name
from .constants import (
    DEFAULT_DEMO_NAME,
    DEFAULT_DURATION_S,
    DEFAULT_HEIGHT,
    DEFAULT_LOG_DIR,
    DEFAULT_WIDTH,
)
from .install import clean_managed_q2rtx, install_latest_q2rtx
from .models import Q2RTXStabilityConfig, StabilityTestError
from .output import attach_stdout_progress
from .reporting import print_q2rtx_stability_result
from .resolution import resolve_q2rtx_render_resolution
from .runtime import run_q2rtx_stability_test


def _default_prog_name() -> str:
    invoked_name = Path(sys.argv[0]).name
    if invoked_name == "__main__.py":
        return "python -m stability.q2rtx"
    return invoked_name or "q2rtx_stability.py"


def parse_q2rtx_stability_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=_default_prog_name(),
        description="Run a non-interactive Q2RTX benchmark stability workload.",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--install-q2rtx",
        action="store_true",
        help=(
            "Download the PenguinBurner headless Q2RTX binary and shareware "
            "demo data under ~/.local/share/PenguinBurner/q2rtx/"
        ),
    )
    actions.add_argument(
        "--clean-q2rtx",
        action="store_true",
        help=(
            "Remove the managed Q2RTX install and download cache, preserving "
            "PenguinBurner profiles, settings, scan history, and logs"
        ),
    )
    parser.add_argument(
        "--stability-seconds",
        type=int,
        default=DEFAULT_DURATION_S,
        help=(
            f"Measured benchmark duration in seconds; default {DEFAULT_DURATION_S}"
        ),
    )
    parser.add_argument(
        "--stability-demo",
        default=DEFAULT_DEMO_NAME,
        help=(
            "Demo name under baseq2/demos or inside pak0.pak, or 'auto' to "
            f"prefer a built-in benchmark demo like q2demo1; default {DEFAULT_DEMO_NAME}"
        ),
    )
    parser.add_argument(
        "--stability-width",
        type=int,
        default=None,
        help=(
            "Q2RTX render width; default auto by GPU model "
            f"(supported high-end desktop GPUs: {DEFAULT_WIDTH}, all others: 1920)"
        ),
    )
    parser.add_argument(
        "--stability-height",
        type=int,
        default=None,
        help=(
            "Q2RTX render height; default auto by GPU model "
            f"(supported high-end desktop GPUs: {DEFAULT_HEIGHT}, all others: 1080)"
        ),
    )
    parser.add_argument(
        "--gpu-index",
        type=int,
        default=0,
        help="GPU index for telemetry polling; default 0",
    )
    parser.add_argument(
        "--stability-log-dir",
        default=str(DEFAULT_LOG_DIR),
        help=f"Directory for captured logs; default {DEFAULT_LOG_DIR}",
    )
    return parser.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> Q2RTXStabilityConfig:
    try:
        resolution = resolve_q2rtx_render_resolution(
            gpu_index=int(args.gpu_index),
            requested_width=getattr(args, "stability_width", None),
            requested_height=getattr(args, "stability_height", None),
        )
    except ValueError as exc:
        raise StabilityTestError(str(exc)) from exc
    return Q2RTXStabilityConfig(
        duration_s=int(args.stability_seconds),
        width=int(resolution.width),
        height=int(resolution.height),
        demo_name=_validate_demo_name(args.stability_demo),
        gpu_index=int(args.gpu_index),
        log_dir=Path(args.stability_log_dir).expanduser(),
    )


def main(argv: list[str] | None = None) -> int:
    enable_cli_output_wrapping()
    args = parse_q2rtx_stability_args(argv)
    try:
        if args.clean_q2rtx:
            removed = clean_managed_q2rtx()
            if removed:
                print("Removed managed Q2RTX files:", flush=True)
                for directory in removed:
                    print(f"  {directory}", flush=True)
            else:
                print("Managed Q2RTX install and cache are already absent.", flush=True)
            print(
                "PenguinBurner profiles, settings, scan history, and logs were "
                "preserved.",
                flush=True,
            )
            return 0
        if args.install_q2rtx:
            result = install_latest_q2rtx()
            print(
                f"Installed Q2RTX {result.version} to {result.install_dir}",
                flush=True,
            )
            print(f"Executable: {result.executable_path}", flush=True)
            print(f"Archive cache: {result.archive_path}", flush=True)
            print(f"Source: {result.asset_url}", flush=True)
            return 0
        config = config_from_args(args)
        attach_stdout_progress(config)
        result = run_q2rtx_stability_test(config)
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr, flush=True)
        return 130
    except StabilityTestError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2
    except OSError as exc:
        print(f"error: failed to start Q2RTX: {exc}", file=sys.stderr, flush=True)
        return 2

    print_q2rtx_stability_result(result)
    return 0 if result.success else 1
