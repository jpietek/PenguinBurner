"""Command-line arguments for the PenguinBurner CLI.

This module only defines flags and defaults; command execution stays in the runtime entrypoint.
"""

from __future__ import annotations

import argparse

from auto_uv.domain.user_options import AUTO_UV_DEFAULTS
from auto_uv.scan_mode.auto_uv_mode import ADAPTIVE_TIER_MODES, AUTO_UV_MODES
from integrations.afterburner.policy import MAX_AFTERBURNER_MEM_OFFSET_MHZ
from common.penguin_burner_paths import default_runtime_config_path
from runtime.support.runtime_service import DEFAULT_JOURNAL_HOURS

DEFAULT_AUTO_UV_FINAL_DURATION_S = AUTO_UV_DEFAULTS.final_duration_s


def default_cli_config_path() -> str:
    return str(default_runtime_config_path())


def parse_arguments(argv):
    parser = argparse.ArgumentParser(
        prog="penguin_burner.py",
        usage="penguin_burner.py [options]",
        description=("PenguinBurner Auto-UV scan and runtime profile utility."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    auto_uv_group = parser.add_argument_group("Auto-UV")
    steam_group = parser.add_argument_group("Steam overlay setup")
    daemon_group = parser.add_argument_group("Runtime and daemon essentials")
    runtime_group = parser.add_argument_group("Runtime tuning")
    advanced_group = parser.add_argument_group("Advanced/debug")

    auto_uv_group.add_argument(
        "--auto-uv-voltage-scan",
        action="store_true",
        help=(
            "Discover a stable fixed-clock undervolt from the live/default "
            "NVIDIA V/F curve, step the lock voltage down through real editable "
            "VF bins, and verify candidates with Q2RTX plus CUDA load"
        ),
    )
    auto_uv_group.add_argument(
        "--json-events",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--auto-uv-require-final-choice",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--list-auto-uv-profiles",
        action="store_true",
        help=(
            "List saved Auto-UV profiles and exit; use a shown profile id or "
            "candidate id with --auto-uv-profile."
        ),
    )
    auto_uv_group.add_argument(
        "--delete-auto-uv-profiles",
        nargs="+",
        default=[],
        metavar="PROFILE",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--assign-auto-uv-tier",
        nargs=2,
        metavar=("PROFILE", "TIER"),
        help=(
            "Assign a saved verified Auto-UV profile to an adaptive tier and "
            "exit. PROFILE accepts the same selectors as --auto-uv-profile; "
            "TIER is efficiency, balanced, performance, or none."
        ),
    )
    auto_uv_group.add_argument(
        "--fresh-auto-uv-scan",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--clear-auto-uv-state",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    auto_uv_group.add_argument(
        "--auto-uv-max-clock-drop-pct",
        type=float,
        default=None,
        metavar="N",
        help=(
            "Maximum loaded GPU core clock drop allowed during Auto-UV; "
            "default is preset-aware from the GPU table when detected, otherwise "
            "12.5. Example: 12 allows up to a 12%% clock drop."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-mode",
        choices=AUTO_UV_MODES,
        default=None,
        metavar="MODE",
        help=(
            "Auto-UV preset path. efficiency uses a flat base sweep plus a "
            "low-voltage tail-tune pass; balanced uses the 4-bin tail sweep; "
            "performance uses the 4-bin tail sweep plus Auto-OC."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-min-voltage-mv",
        type=int,
        default=None,
        metavar="mV",
        help=(
            "Lowest voltage for the selected undervolt point. Overrides the detected GPU "
            "table floor and the percentage-drop fallback. Lower-voltage curve "
            "transitions are checked separately at their lower clocks."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-memory-offset-mhz",
        type=int,
        default=None,
        metavar="MHz",
        help=(
            "Memory clock V/F offset to apply during Auto-UV and save with "
            "the final profile. NVML offsets are in transfer-rate units "
            "(MT/s): the realized memory clock rises by half the value. "
            "Clamped to the driver-reported limit for the GPU (fallback "
            f"0..{MAX_AFTERBURNER_MEM_OFFSET_MHZ} when NVML exposes no range)."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-power-limit-w",
        type=int,
        default=None,
        metavar="W",
        help=(
            "Power limit in watts to apply during Auto-UV and save with the "
            "final profile. The UI clamps this to the selected GPU's NVML "
            "minimum and maximum power-limit range."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-tail-rise-bins",
        type=int,
        default=None,
        metavar="N",
        help=(
            "How many voltage bins can the voltage curve rise above the locked "
            "undervolt point."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-uv-final-verification-s",
        type=int,
        default=None,
        metavar="SECONDS",
        help=(
            "Final verification soak per tier for this scan (every stage of "
            "an adaptive 3-in-1 run uses the same duration). Default 300 "
            "seconds; equivalent to PENGUIN_BURNER_AUTO_UV_FINAL_SECONDS."
        ),
    )
    steam_group.add_argument(
        "--set-steam-overlay-launch",
        metavar="APPID",
        help=(
            "Write the PenguinBurner native-overlay launch option for a Steam "
            "app id and exit. The write is verified so running Steam rewrites "
            "are reported instead of treated as success."
        ),
    )
    for tier in ADAPTIVE_TIER_MODES:
        tier_label = tier.capitalize()
        auto_uv_group.add_argument(
            f"--auto-uv-{tier}-max-clock-drop-pct",
            type=float,
            default=None,
            metavar="N",
            help=(
                f"Full-scan override: the {tier_label} tier's maximum loaded "
                "clock drop. Only meaningful with --auto-uv-mode adaptive; "
                "absent tiers fall back to --auto-uv-max-clock-drop-pct, then "
                "the GPU table."
            ),
        )
        auto_uv_group.add_argument(
            f"--auto-uv-{tier}-power-limit-w",
            type=int,
            default=None,
            metavar="W",
            help=(
                f"Full-scan override: the {tier_label} tier's power limit in "
                "watts, applied for that tier's baseline, descent, final "
                "verification, and saved profile. Only meaningful with "
                "--auto-uv-mode adaptive."
            ),
        )
        auto_uv_group.add_argument(
            f"--auto-uv-{tier}-memory-offset-mhz",
            type=int,
            default=None,
            metavar="MHz",
            help=(
                f"Full-scan override: the {tier_label} tier's memory V/F "
                "offset (NVML transfer-rate units, like "
                "--auto-uv-memory-offset-mhz), applied for that tier's descent "
                "and saved with its profile. Only meaningful with "
                "--auto-uv-mode adaptive."
            ),
        )
    auto_uv_group.add_argument(
        "--auto-oc-target-voltage-mv",
        type=int,
        default=None,
        metavar="mV",
        help=(
            "Performance-mode Auto-OC voltage cap in mV. Overrides the detected "
            "GPU table target."
        ),
    )
    auto_uv_group.add_argument(
        "--auto-oc-target-clock-mhz",
        type=int,
        default=None,
        metavar="MHz",
        help=(
            "Performance-mode Auto-OC core clock cap in MHz. Overrides the detected "
            "GPU table target."
        ),
    )
    daemon_group.add_argument(
        "--silent-fan-curve",
        action="store_true",
        help=(
            "Runtime/daemon only: opt in to PenguinBurner manual fan-curve "
            "control; by default fan control is left to the GPU driver. "
            "Auto-UV scans write a suggested fan curve automatically when safe."
        ),
    )
    daemon_group.add_argument(
        "--daemonize",
        action="store_true",
        help=(
            "Apply normal runtime through the running penguin-burnerd service "
            "after an Auto-UV final curve exists. Auto-UV scans remain "
            "foreground-only."
        ),
    )
    daemon_group.add_argument(
        "--install-systemd-service",
        action="store_true",
        help=(
            "Install or update the PenguinBurner daemon service: refresh the "
            "daemon binary at /var/opt/penguin-burner/libexec, rewrite the "
            "unit, and restart it. With profile options, also set the "
            "persistent boot profile; without them, an existing boot profile "
            "is kept. Asks for authorization (pkexec/sudo) when not run as "
            "root."
        ),
    )
    daemon_group.add_argument(
        "--uninstall-systemd-service",
        "--deinstall-systemd-service",
        dest="uninstall_systemd_service",
        action="store_true",
        help=(
            "Stop and remove the persistent PenguinBurner systemd service. "
            "Asks for authorization (pkexec/sudo) when not run as root."
        ),
    )
    daemon_group.add_argument(
        "--migrate-to-daemon-service",
        action="store_true",
        help=(
            "Install penguin-burnerd.service and migrate an existing "
            "legacy PenguinBurner.service when possible. Asks for "
            "authorization (pkexec/sudo) when not run as root."
        ),
    )
    daemon_group.add_argument(
        "--daemon-status",
        action="store_true",
        help="Print PenguinBurner hardware daemon status and exit.",
    )
    daemon_group.add_argument(
        "--set-main-gpu",
        metavar="GPU-UUID",
        default="",
        help=(
            "On a multi-NVIDIA-GPU system, make a saved startup GPU the "
            "daemon's main monitored GPU after boot."
        ),
    )
    daemon_group.add_argument(
        "--clear-main-gpu",
        action="store_true",
        help="Clear the explicit Main GPU and restore last-saved-GPU behavior.",
    )
    daemon_group.add_argument(
        "--restore-stock",
        action="store_true",
        help=(
            "Recovery: reset the GPU to stock now and make stock the boot "
            "state through the running penguin-burnerd service. Saved "
            "profiles are kept. Works without the GUI."
        ),
    )
    daemon_group.add_argument(
        "--auto-uv-profile",
        default="",
        help=(
            "Use an Auto-UV profile by profile id, candidate id, JSON path, "
            "'active', or 'latest' for daemon runtime, systemd service runtime, "
            "and internal final verification."
        ),
    )
    daemon_group.add_argument(
        "--adaptive-auto-uv",
        action="store_true",
        help=(
            "Runtime/daemon only: allow PenguinBurner to adapt between saved "
            "Auto-UV profile tiers from base present-frame p95 pacing. Requires "
            "at least one saved verified profile tier. Target defaults to 60 FPS; "
            "override the service env PENGUIN_BURNER_ADAPTIVE_TARGET_FPS for "
            "30, 50, 60, 120, etc."
        ),
    )
    daemon_group.add_argument(
        "--journal-hours",
        type=float,
        default=DEFAULT_JOURNAL_HOURS,
        metavar="N",
        help=(
            "Hours of systemd journal history to suggest after daemonizing; "
            f"default {DEFAULT_JOURNAL_HOURS}."
        ),
    )
    runtime_group.add_argument(
        "--config",
        default=default_cli_config_path(),
        help="Runtime config path to read defaults from",
    )
    runtime_group.add_argument(
        "--gpu-index",
        type=int,
        default=None,
        help="Override the configured GPU index",
    )
    advanced_group.add_argument(
        "--stability-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    advanced_group.add_argument(
        "--stability-seconds",
        type=int,
        default=DEFAULT_AUTO_UV_FINAL_DURATION_S,
        help=argparse.SUPPRESS,
    )
    advanced_group.add_argument(
        "--stability-stop-request-file",
        default="",
        help=argparse.SUPPRESS,
    )
    advanced_group.add_argument(
        "--debug-log",
        action="store_true",
        help=(
            "Write a verbose diagnostic log next to the selected config file "
            "under debug-logs/; with the default config this is "
            "~/.config/PenguinBurner/debug-logs"
        ),
    )
    return parser.parse_args(argv)
