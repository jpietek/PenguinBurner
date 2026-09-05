#!/usr/bin/env python3
"""Render a simulated Full Auto-UV scan through PenguinBurner's real Qt UI.

The script never starts the scanner or talks to the hardware daemon. It feeds
synthetic scan events into ``MainWindow`` and captures the actual PySide6 and
pyqtgraph widgets, then tours the real Profiles, Steam, and In-Game Overlay
tabs, producing a short README-friendly animated GIF.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1")
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "0")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ui.components.overlay_config as overlay_config_mod  # noqa: E402
import ui.window as window_mod  # noqa: E402
from integrations.steam.library import InstalledSteamGame  # noqa: E402
from integrations.steam.manager import SteamGameRow  # noqa: E402
from integrations.steam.settings import SteamGameSetting  # noqa: E402
from overlay.config import OverlayConfig  # noqa: E402
from ui.main import APP_DISPLAY_NAME  # noqa: E402
from ui.qt import apply_dark_palette, import_qt  # noqa: E402
from ui.window import MainWindow  # noqa: E402


FPS = 7
DURATION_S = 24.0
# Mobile-miniature intro: how long the completed three-curve payoff holds as
# the GIF's opening (and therefore its static preview frame).
PAYOFF_HOLD_S = 1.3
CAPTURE_SIZE = (1600, 900)
OUTPUT_SIZE = (1280, 720)
DEFAULT_OUTPUT = REPO_ROOT / "docs/assets/auto-uv-full-scan-demo.gif"
PROFILES_REVEAL_S = 15.6
STEAM_REVEAL_S = 18.4
OVERLAY_REVEAL_S = 21.2

SOURCE_POINTS: list[tuple[float, float]] = [
    (800, 1940),
    (820, 2010),
    (840, 2080),
    (860, 2150),
    (880, 2230),
    (900, 2310),
    (920, 2390),
    (940, 2470),
    (960, 2550),
    (980, 2630),
    (1000, 2710),
    (1020, 2790),
    (1040, 2860),
    (1060, 2920),
    (1080, 2970),
    (1100, 3010),
    (1120, 3040),
    (1140, 3060),
    (1160, 3075),
    (1180, 3090),
    (1200, 3100),
]

TIER_SPECS = {
    "efficiency": {
        "position": 1,
        "next_tier": "balanced",
        "start": 1.7,
        "end": 5.3,
        "final": (850, 2460),
        "metrics": (56.764, 232.896, 61.186, 38.093),
        "candidates": [
            (920, 2580, True),
            (895, 2530, True),
            (870, 2490, True),
            (840, 2430, False),
            (850, 2460, True),
        ],
    },
    "balanced": {
        "position": 2,
        "next_tier": "performance",
        "start": 5.3,
        "end": 8.9,
        "final": (850, 2639),
        "metrics": (62.703, 267.154, 64.0, 39.628),
        "candidates": [
            (920, 2780, True),
            (890, 2720, True),
            (840, 2585, False),
            (860, 2655, True),
            (850, 2639, True),
        ],
    },
    "performance": {
        "position": 3,
        "next_tier": "",
        "start": 8.9,
        "end": 12.5,
        "final": (915, 2980),
        "metrics": (66.804, 310.180, 66.767, 41.093),
        "candidates": [
            (900, 2860, True),
            (910, 2920, True),
            (920, 3010, False),
            (915, 2960, True),
            (915, 2980, True),
        ],
    },
}

# Verified RTX 5080 results used for the final Profiles-tab reveal. Keeping the
# summaries here makes the public demo deterministic and safe to rerender on a
# machine without access to the source profile store.
DEMO_PROFILES = [
    {
        "profile_id": "demo-performance-915mv-2980mhz",
        "candidate_id": "915mv-2980mhz",
        "profile_created_at": "2026-07-14T00:00:44+02:00",
        "profile_source": "auto-uv-final",
        "candidate_voltage_mv": 915,
        "lock_clock_mhz": 2980,
        "avg_core_clock_mhz": 2911.512,
        "avg_fps": 66.804,
        "avg_power_w": 310.180,
        "efficiency_fps_per_w": 0.215372,
        "base_candidate_voltage_mv": 1240,
        "base_avg_core_clock_mhz": 2738.647,
        "base_avg_fps": 64.309,
        "base_avg_power_w": 351.733,
        "base_efficiency_fps_per_w": 0.182835,
        "memory_offset_mhz": 6000,
        "power_limit_w": 360,
        "final_verified": True,
        "generated_profile_tier": "Performance",
        "generated_profile_tier_key": "performance",
        "assigned_profile_tier": "Performance",
        "assigned_profile_tier_key": "performance",
        "profile_tier": "Performance",
        "profile_tier_key": "performance",
        "profile_tier_disabled": False,
    },
    {
        "profile_id": "demo-balanced-850mv-2639mhz",
        "candidate_id": "850mv-2639mhz",
        "profile_created_at": "2026-07-13T22:36:15+02:00",
        "profile_source": "auto-uv-final",
        "candidate_voltage_mv": 850,
        "lock_clock_mhz": 2639,
        "avg_core_clock_mhz": 2595.419,
        "avg_fps": 62.703,
        "avg_power_w": 267.154,
        "efficiency_fps_per_w": 0.234707,
        "base_candidate_voltage_mv": 1240,
        "base_avg_core_clock_mhz": 2757.118,
        "base_avg_fps": 61.268,
        "base_avg_power_w": 341.574,
        "base_efficiency_fps_per_w": 0.179370,
        "memory_offset_mhz": 6000,
        "power_limit_w": 360,
        "final_verified": True,
        "generated_profile_tier": "Balanced",
        "generated_profile_tier_key": "balanced",
        "assigned_profile_tier": "Balanced",
        "assigned_profile_tier_key": "balanced",
        "profile_tier": "Balanced",
        "profile_tier_key": "balanced",
        "profile_tier_disabled": False,
    },
    {
        "profile_id": "demo-efficiency-850mv-2460mhz",
        "candidate_id": "850mv-2460mhz",
        "profile_created_at": "2026-07-13T22:28:33+02:00",
        "profile_source": "auto-uv-final",
        "candidate_voltage_mv": 850,
        "lock_clock_mhz": 2460,
        "avg_core_clock_mhz": 2439.070,
        "avg_fps": 56.764,
        "avg_power_w": 232.896,
        "efficiency_fps_per_w": 0.243731,
        "base_candidate_voltage_mv": 1240,
        "base_avg_core_clock_mhz": 2757.118,
        "base_avg_fps": 61.268,
        "base_avg_power_w": 341.574,
        "base_efficiency_fps_per_w": 0.179370,
        "memory_offset_mhz": 0,
        "power_limit_w": 300,
        "final_verified": True,
        "generated_profile_tier": "Efficiency",
        "generated_profile_tier_key": "efficiency",
        "assigned_profile_tier": "Efficiency",
        "assigned_profile_tier_key": "efficiency",
        "profile_tier": "Efficiency",
        "profile_tier_key": "efficiency",
        "profile_tier_disabled": False,
    },
]


@dataclass(frozen=True)
class DemoSteamGame:
    app_id: str
    name: str
    last_played: int
    mode: str = "adaptive"
    target_fps: float | None = None
    compat_tool: str = "proton-cachyos-11.0-20260601-slr-x86_64"
    effective_compat_tool: str | None = None


# A deterministic snapshot of the current installed-game list and its visible
# per-game choices. No Steam files are read when this renderer is run.
DEMO_STEAM_GAMES = (
    DemoSteamGame("1771300", "Kingdom Come: Deliverance II", 1784063542, target_fps=125.0, compat_tool="proton_experimental"),
    DemoSteamGame("2531310", "The Last of Us™ Part II Remastered", 1784062655, target_fps=125.0, compat_tool="", effective_compat_tool="proton-cachyos-11.0-20260602-slr-x86_64"),
    DemoSteamGame("835960", "The Talos Principle 2", 1784060686, target_fps=80.0),
    DemoSteamGame("3768760", "007 First Light", 1784060043, target_fps=80.0),
    DemoSteamGame("1089130", "Quake II RTX", 1783980654, mode="performance", target_fps=125.0),
    DemoSteamGame("1378990", "Crash Bandicoot™ 4: It’s About Time", 1783896865, mode="efficiency"),
    DemoSteamGame("3606110", "Coffee Talk Tokyo Demo", 1783884063, mode="efficiency"),
    DemoSteamGame("3764200", "Resident Evil Requiem", 1783872527),
    DemoSteamGame("1206070", "Phonopolis", 1783627933, mode="efficiency", compat_tool="", effective_compat_tool="proton-cachyos-11.0-20260602-slr-x86_64"),
    DemoSteamGame("2215200", "LEGO® Batman™: Legacy of the Dark Knight", 1783374955, mode="efficiency"),
    DemoSteamGame("3472040", "NBA 2K26", 1782844583, compat_tool="proton_experimental"),
    DemoSteamGame("2909400", "FINAL FANTASY VII REBIRTH", 1782656113),
    DemoSteamGame("2057760", "Esoteric Ebb", 1781433439),
    DemoSteamGame("2318070", "Little Big Adventure – Twinsen’s Quest", 1781432345),
    DemoSteamGame("3622640", "Vending Machine Co.", 1781432284),
    DemoSteamGame("1226670", "Rosewater", 1781432252),
)


DEMO_OVERLAY_CONFIG = OverlayConfig(
    enabled=True,
    enabled_item_ids=(
        "base_fps",
        "fg_fps",
        "latency_ms",
        "clock_mhz",
        "voltage_mv",
        "power_w",
        "profile",
        "gpu_util_pct",
        "cpu_peak_thread_pct",
    ),
    update_interval_s=1,
    scale=0.5,
)

DEMO_OVERLAY_STATE = {
    "present_fps": "125.0",
    "framegen_fps": "238.4",
    "latency_ms": "31.7",
    "clock_mhz": "2912",
    "voltage_mv": "915",
    "power_w": "310",
    "profile_tier": "Performance",
    "gpu_util_pct": "98",
    "cpu_util_pct": "24",
    "cpu_peak_thread_pct": "72",
    "fan_pct": "41",
    "temperature_c": "67",
    "uv_offset_mv": "-325",
}


def _stub_external_state() -> None:
    """Keep MainWindow construction completely local and daemon-free."""

    # The library tab normally starts a background, read-only scan the first
    # time it is shown. The demo injects its fixed snapshot instead.
    setattr(window_mod.GameLibraryPanel, "rescan", lambda *_args, **_kwargs: None)
    setattr(
        overlay_config_mod,
        "load_overlay_config",
        lambda *_args, **_kwargs: DEMO_OVERLAY_CONFIG,
    )
    setattr(
        overlay_config_mod,
        "read_overlay_state",
        lambda: dict(DEMO_OVERLAY_STATE),
    )
    window_mod.load_profile_summaries = lambda: []
    window_mod.systemd_autostart_profile_info = lambda: {
        "selector": "",
        "silent_fan_curve": False,
    }
    window_mod.running_auto_uv_profile_info = lambda: {
        "selector": "",
        "silent_fan_curve": False,
        "adaptive_auto_uv": False,
    }
    window_mod.penguin_burner_runtime_is_active = lambda: False
    window_mod.silent_fan_curve_from_runtime_config = lambda: False
    window_mod.silent_fan_curve_to_runtime_config = lambda value: value


def _demo_steam_source():
    """The real Steam adapter, fed the demo snapshot instead of a live client.

    Going through the adapter rather than poking the panel keeps this render
    honest: what the tour shows is what the launcher contract produces.
    """
    from integrations.steam.library_source import SteamLibrarySource

    class _Manager:
        def available_compat_tools(self, _app_id):
            return (("proton_experimental", "Proton Experimental"),)

        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    source = SteamLibrarySource(manager=_Manager())
    source._rows = _demo_steam_rows()
    source._playtime = {}
    source._probe = {
        "marker": True,
        "running": True,
        "cdp_ready": True,
        "user": "connected",
    }
    return source


def _demo_steam_rows() -> tuple[SteamGameRow, ...]:
    launch_options = "PENGUIN_BURNER --pb-overlay=1 %command%"
    steamapps_dir = Path("/demo/SteamLibrary/steamapps")
    return tuple(
        SteamGameRow(
            game=InstalledSteamGame(
                app_id=game.app_id,
                name=game.name,
                install_dir=game.name,
                steamapps_dir=steamapps_dir,
                state_flags=4,
                last_played=game.last_played,
                icon_path=None,
                compat_tool=game.compat_tool,
                effective_compat_tool=(
                    game.effective_compat_tool
                    if game.effective_compat_tool is not None
                    else game.compat_tool
                ),
                effective_compat_tool_display=(
                    game.effective_compat_tool
                    if game.effective_compat_tool is not None
                    else game.compat_tool
                ),
            ),
            setting=SteamGameSetting(
                enabled=True,
                mode=game.mode,
                overlay=True,
                target_fps=game.target_fps,
                injected_launch_options=launch_options,
            ),
            launch_options=launch_options,
        )
        for game in DEMO_STEAM_GAMES
    )


def _curve(
    anchor_mv: float,
    target_mhz: float,
    phase: float = 0.0,
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for voltage_mv in range(800, 1201, 10):
        if voltage_mv < anchor_mv:
            span = max(1.0, anchor_mv - 800.0)
            ratio = max(0.0, min(1.0, (voltage_mv - 800.0) / span))
            clock_mhz = 1940.0 + (target_mhz - 1940.0) * ratio**0.82
        else:
            clock_mhz = target_mhz + min(38.0, (voltage_mv - anchor_mv) * 0.085)
        # A restrained live-settling motion. The underlying Qt plot is real;
        # only the emitted scanner measurements are simulated.
        clock_mhz += math.sin(phase * 8.0 + voltage_mv / 26.0) * 4.0
        points.append((float(voltage_mv), clock_mhz))
    return points


def _event_points(
    points: Sequence[tuple[float, float]],
    *,
    base: bool = False,
) -> list[dict]:
    key = "base_mhz" if base else "clock_mhz"
    return [{"voltage_mv": voltage, key: round(clock, 2)} for voltage, clock in points]


def _tier_for_time(elapsed_s: float) -> str | None:
    for tier, spec in TIER_SPECS.items():
        if float(spec["start"]) <= elapsed_s < float(spec["end"]):
            return tier
    return None


def _candidate_index(tier: str, elapsed_s: float) -> int:
    spec = TIER_SPECS[tier]
    progress = (elapsed_s - float(spec["start"])) / (
        float(spec["end"]) - float(spec["start"])
    )
    candidates = spec["candidates"]
    return min(len(candidates) - 1, max(0, int(progress * len(candidates))))


def _candidate_progress(tier: str, elapsed_s: float) -> float:
    spec = TIER_SPECS[tier]
    duration = float(spec["end"]) - float(spec["start"])
    scaled = max(0.0, min(0.999999, (elapsed_s - float(spec["start"])) / duration))
    return (scaled * len(spec["candidates"])) % 1.0


def _candidate_payload(
    tier: str,
    candidate_index: int,
    *,
    elapsed_s: float,
    decision: str | None = None,
) -> dict:
    spec = TIER_SPECS[tier]
    voltage_mv, clock_mhz, passed = spec["candidates"][candidate_index]
    fps, power_w, temp_c, fan_pct = spec["metrics"]
    final_voltage, final_clock = spec["final"]
    clock_delta = float(clock_mhz) - float(final_clock)
    measured_clock = float(clock_mhz) - 23.0 + math.sin(elapsed_s * 7.0) * 5.0
    measured_power = float(power_w) + clock_delta * 0.14 + math.sin(elapsed_s * 5.0) * 2.0
    measured_fps = float(fps) + clock_delta * 0.007 + math.sin(elapsed_s * 4.0) * 0.18
    payload = {
        "stage": "candidate" if tier != "performance" else "auto-oc-candidate",
        "candidate_id": f"demo-{tier}-{candidate_index}",
        "voltage_mv": voltage_mv,
        "candidate_voltage_mv": voltage_mv,
        "clock_mhz": clock_mhz,
        "lock_clock_mhz": clock_mhz,
        "measured_voltage_mv": voltage_mv,
        "measured_clock_mhz": round(measured_clock, 2),
        "fps": round(measured_fps, 2),
        "power_w": round(measured_power, 2),
        "temp_c": round(float(temp_c) + math.sin(elapsed_s * 2.0) * 0.7, 2),
        "fan_pct": round(float(fan_pct) + math.sin(elapsed_s * 2.4) * 0.6, 2),
        "efficiency_fps_per_w": round(measured_fps / measured_power, 3),
        "baseline_clock_mhz": 2762,
        "elapsed_s": round(_candidate_progress(tier, elapsed_s) * 20.0, 2),
        "target_duration_s": 20,
        "perf_cap_reason": "none",
    }
    if decision is not None:
        payload["decision"] = decision
        if decision == "fail":
            payload["failure_kind"] = "fps-regression"
            payload["reason"] = "candidate missed the performance guardrail"
        elif passed:
            payload["reason"] = "stable run"
    return payload


def _append_log(window: MainWindow, text: str) -> None:
    window.log_view.append(text.rstrip() + "\n")


def _capture(window: MainWindow, QtCore, app, path: Path) -> None:
    app.processEvents()
    pixmap = window.window.grab()
    if pixmap.isNull():
        raise RuntimeError("Qt returned an empty window capture")
    pixmap = pixmap.scaled(
        CAPTURE_SIZE[0],
        CAPTURE_SIZE[1],
        QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation,
    )
    if not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"Could not save Qt frame {path}")


def _encode_gif(frame_dir: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filter_graph = (
        f"[0:v]fps={FPS},scale={OUTPUT_SIZE[0]}:{OUTPUT_SIZE[1]}:flags=lanczos,split[a][b];"
        "[a]palettegen=max_colors=128:stats_mode=diff[p];"
        "[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            str(FPS),
            "-i",
            str(frame_dir / "frame-%03d.png"),
            "-filter_complex",
            filter_graph,
            "-loop",
            "0",
            str(output_path),
        ],
        check=True,
    )


def render(output_path: Path) -> None:
    _stub_external_state()
    QtCore, QtGui, QtWidgets, pg = import_qt()
    if pg is None:
        raise RuntimeError("pyqtgraph is required to render the Auto-UV demo")

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["penguin-burner-auto-uv-demo"]
    )
    app.setApplicationName(APP_DISPLAY_NAME)
    apply_dark_palette(app, QtGui)

    # Never allow a modal dialog to block an unattended render.
    QtWidgets.QDialog.exec = lambda self: 0
    window = MainWindow((QtCore, QtGui, QtWidgets, pg))
    window._status_timer.stop()
    window.game_library_panel._library_timer.stop()
    window.game_library_panel._state_timer.stop()
    window.overlay_config.timer.stop()
    window.overlay_config.refresh_preview()
    window.window.setWindowTitle(
        f"{APP_DISPLAY_NAME} — simulated Full Auto-UV scan (compressed)"
    )
    window.window.resize(*CAPTURE_SIZE)
    window.window.show()
    app.processEvents()

    auto_uv_view = window.tabs.widget(window.auto_uv_tab_index)
    if isinstance(auto_uv_view, QtWidgets.QSplitter):
        auto_uv_view.setSizes([1070, 500])
    window.auto_uv_split.setSizes([585, 285])
    window.controls.set_running(True)
    window.controls.set_status_text(
        f"Simulation: Full 3-in-1 scan compressed to {DURATION_S:.0f} seconds — "
        "no GPU writes."
    )
    window.runs_table.clear()
    window.vf_plot.clear()
    window.auto_uv_tier_progress.start()
    window.header.set_stage("Starting Full scan")
    window.header.set_candidate("Simulation only — the hardware daemon is not used")
    window.log_view.text_edit.clear()
    window.log_view._at_line_start = True
    _append_log(window, "$ penguin-burner demo-auto-uv --mode adaptive --no-gpu-writes")

    window._handle_scan_event(
        {
            "event": "source_curve",
            "points": _event_points(SOURCE_POINTS, base=True),
        }
    )
    window.vf_plot.plot.setXRange(800, 1200, padding=0.0)
    window.vf_plot.plot.setYRange(1800, 3200, padding=0.0)
    window.vf_plot.plot.disableAutoRange()

    work_dir = Path(tempfile.mkdtemp(prefix="penguinburner-auto-uv-qt-demo-"))
    frame_dir = work_dir / "frames"
    frame_dir.mkdir(parents=True)
    active_tier: str | None = None
    active_candidate_index: int | None = None
    active_payload: dict | None = None
    baseline_started = False
    baseline_finished = False
    verify_started = False
    complete_started = False
    profiles_revealed = False
    steam_revealed = False
    overlay_revealed = False

    try:
        frame_count = int(round(DURATION_S * FPS))
        for frame_index in range(frame_count):
            elapsed_s = frame_index / FPS

            if not baseline_started:
                baseline_started = True
                baseline = {
                    "event": "probe_start",
                    "stage": "base-baseline",
                    "candidate_id": "demo-baseline",
                    "voltage_mv": 1000,
                    "clock_mhz": 2762,
                    "measured_clock_mhz": 2734,
                    "fps": 63.4,
                    "power_w": 341.0,
                    "temp_c": 67.0,
                    "fan_pct": 41.0,
                    "efficiency_fps_per_w": 0.186,
                    "elapsed_s": 0,
                    "target_duration_s": 20,
                }
                window._handle_scan_event(baseline)
                _append_log(window, "Auto-UV baseline: measuring the stock curve under Q2RTX load")

            if elapsed_s < 1.7:
                baseline_progress = max(0.0, min(1.0, elapsed_s / 1.7))
                window._handle_scan_event(
                    {
                        "event": "load_telemetry",
                        "stage": "base-baseline",
                        "voltage_mv": 1000,
                        "clock_mhz": 2762,
                        "measured_voltage_mv": 1000,
                        "measured_clock_mhz": 2708 + baseline_progress * 26,
                        "elapsed_s": baseline_progress * 20,
                        "target_duration_s": 20,
                    }
                )
            elif not baseline_finished:
                baseline_finished = True
                window._handle_scan_event(
                    {
                        "event": "probe_result",
                        "stage": "base-baseline",
                        "candidate_id": "demo-baseline",
                        "voltage_mv": 1000,
                        "clock_mhz": 2762,
                        "measured_clock_mhz": 2734,
                        "fps": 63.4,
                        "power_w": 341.0,
                        "temp_c": 67.0,
                        "fan_pct": 41.0,
                        "efficiency_fps_per_w": 0.186,
                        "elapsed_s": 20,
                        "target_duration_s": 20,
                        "decision": "pass",
                        "reason": "stock baseline accepted",
                    }
                )
                _append_log(window, "Auto-UV baseline accepted: 2734MHz, 341W, 63.4 FPS")

            tier = _tier_for_time(elapsed_s)
            if tier is not None:
                spec = TIER_SPECS[tier]
                candidate_index = _candidate_index(tier, elapsed_s)
                if tier != active_tier:
                    if (
                        active_payload is not None
                        and active_tier is not None
                        and active_candidate_index is not None
                    ):
                        previous_passed = bool(
                            TIER_SPECS[active_tier]["candidates"][active_candidate_index][2]
                        )
                        window._handle_scan_event(
                            {
                                "event": "probe_result",
                                **active_payload,
                                "decision": "pass" if previous_passed else "fail",
                                "reason": "stable run" if previous_passed else "candidate missed the performance guardrail",
                                "failure_kind": "" if previous_passed else "fps-regression",
                                "elapsed_s": 20,
                                "target_duration_s": 20,
                            }
                        )
                    if active_tier is not None:
                        previous_spec = TIER_SPECS[active_tier]
                        final_voltage, final_clock = previous_spec["final"]
                        final_points = _curve(final_voltage, final_clock)
                        window._handle_scan_event(
                            {
                                "event": "tier_confirmed",
                                "tier": active_tier,
                                "voltage_mv": final_voltage,
                                "target_mhz": final_clock,
                                "points": _event_points(final_points),
                            }
                        )
                        window._handle_scan_event(
                            {
                                "event": "tier_completed",
                                "tier": active_tier,
                                "position": previous_spec["position"],
                                "total": 3,
                                "next_tier": previous_spec["next_tier"],
                            }
                        )
                        _append_log(
                            window,
                            f"{active_tier.title()} tier verified: {final_voltage}mV @ {final_clock}MHz",
                        )
                    active_tier = tier
                    active_candidate_index = None
                    window._handle_scan_event(
                        {
                            "event": "tier_started",
                            "tier": tier,
                            "position": spec["position"],
                            "total": 3,
                            "next_tier": spec["next_tier"],
                        }
                    )
                    _append_log(window, f"Starting {tier.title()} scan ({spec['position']}/3)")

                if candidate_index != active_candidate_index:
                    if active_payload is not None and active_candidate_index is not None:
                        previous_passed = bool(spec["candidates"][active_candidate_index][2])
                        window._handle_scan_event(
                            {
                                "event": "probe_result",
                                **active_payload,
                                "decision": "pass" if previous_passed else "fail",
                                "reason": "stable run" if previous_passed else "candidate missed the performance guardrail",
                                "failure_kind": "" if previous_passed else "fps-regression",
                                "elapsed_s": 20,
                                "target_duration_s": 20,
                            }
                        )
                    active_candidate_index = candidate_index
                    active_payload = _candidate_payload(
                        tier,
                        candidate_index,
                        elapsed_s=elapsed_s,
                    )
                    window._handle_scan_event({"event": "probe_start", **active_payload})
                    voltage_mv, clock_mhz, _passed = spec["candidates"][candidate_index]
                    _append_log(
                        window,
                        f"Auto-UV phase=candidate-live tier={tier} candidate={voltage_mv}mV target={clock_mhz}MHz",
                    )

                active_payload = _candidate_payload(
                    tier,
                    candidate_index,
                    elapsed_s=elapsed_s,
                )
                voltage_mv, clock_mhz, passed = spec["candidates"][candidate_index]
                wobble = 11.0 if not passed else 5.0
                animated_points = _curve(
                    voltage_mv,
                    clock_mhz + math.sin(elapsed_s * 8.5) * wobble,
                    phase=elapsed_s,
                )
                window._handle_scan_event(
                    {
                        "event": "candidate_curve",
                        **active_payload,
                        "points": _event_points(animated_points),
                    }
                )
                window._handle_scan_event({"event": "load_telemetry", **active_payload})

            elif elapsed_s >= 12.5 and elapsed_s < 15.1:
                if not verify_started:
                    verify_started = True
                    if active_payload is not None and active_tier is not None:
                        previous_passed = bool(
                            TIER_SPECS[active_tier]["candidates"][active_candidate_index][2]
                        )
                        window._handle_scan_event(
                            {
                                "event": "probe_result",
                                **active_payload,
                                "decision": "pass" if previous_passed else "fail",
                                "reason": "stable run" if previous_passed else "candidate missed the performance guardrail",
                                "failure_kind": "" if previous_passed else "fps-regression",
                                "elapsed_s": 20,
                                "target_duration_s": 20,
                            }
                        )
                        final_voltage, final_clock = TIER_SPECS[active_tier]["final"]
                        final_points = _curve(final_voltage, final_clock)
                        window._handle_scan_event(
                            {
                                "event": "tier_confirmed",
                                "tier": active_tier,
                                "voltage_mv": final_voltage,
                                "target_mhz": final_clock,
                                "points": _event_points(final_points),
                            }
                        )
                        window._handle_scan_event(
                            {
                                "event": "tier_completed",
                                "tier": active_tier,
                                "position": 3,
                                "total": 3,
                                "next_tier": "",
                            }
                        )
                    window._handle_human_line("Starting final verification now")
                    _append_log(window, "Starting final verification for all three saved tiers")

                verify_progress = max(0.0, min(1.0, (elapsed_s - 12.5) / 2.6))
                verify_tier_index = min(2, int(verify_progress * 3))
                verify_tier = ("efficiency", "balanced", "performance")[verify_tier_index]
                final_voltage, final_clock = TIER_SPECS[verify_tier]["final"]
                window.header.set_candidate(
                    f"{verify_tier.title()}: {final_voltage} mV @ {final_clock} MHz"
                )
                window.controls.set_verify_progress(
                    verify_progress * 100.0,
                    elapsed_s=verify_progress * 600.0,
                    target_s=600,
                    detail="Simulated final verification",
                )
                window.vf_plot.set_candidate_points(
                    _curve(
                        final_voltage,
                        final_clock + math.sin(elapsed_s * 6.0) * 3.0,
                        phase=elapsed_s,
                    ),
                    remember_previous=False,
                    curve_id=f"demo-{verify_tier}-final",
                )

            elif elapsed_s >= 15.1 and not complete_started:
                complete_started = True
                window.vf_plot.set_candidate_points([], remember_previous=False)
                window.vf_plot.clear_load_markers()
                window.header.set_stage("Complete")
                window.header.set_candidate(
                    "3 profiles verified — Efficiency · Balanced · Performance"
                )
                window.controls.hide_dependency_progress()
                window.controls.set_running(False)
                window.controls.set_status_text(
                    "Simulation complete — no GPU state was changed."
                )
                _append_log(window, "Full scan complete: all three profiles verified and saved")

            if elapsed_s >= PROFILES_REVEAL_S and not profiles_revealed:
                profiles_revealed = True
                window.profile_list.set_profiles(
                    [dict(profile) for profile in DEMO_PROFILES],
                    silent_fan_checked=True,
                )
                window.tabs.setCurrentIndex(window.profiles_tab_index)
                window.controls.set_status_text(
                    "Verified RTX 5080 results — every GPU tunes differently."
                )

            if elapsed_s >= STEAM_REVEAL_S and not steam_revealed:
                steam_revealed = True
                panel = window.game_library_panel
                panel._sources = (_demo_steam_source(),)
                panel._by_launcher = {
                    source.launcher_id: source for source in panel._sources
                }
                panel._scanned = True  # the snapshot stands in for the scan
                panel._reload_games()
                panel._select_key("steam:1771300")
                panel.splitter.setSizes([480, 1040])
                current_game = panel.game_list.currentItem()
                if current_game is not None:
                    panel.game_list.scrollToItem(current_game)
                panel._sync_status("per-game tuning ready")
                window.header.set_candidate(
                    "Per-game Adaptive target · overlay · one-click Play"
                )
                window.controls.set_status_text(
                    "Steam integration — real game list, simulated UI tour."
                )
                window.tabs.setCurrentIndex(window.game_library_tab_index)

            if elapsed_s >= OVERLAY_REVEAL_S and not overlay_revealed:
                overlay_revealed = True
                window.overlay_config.refresh_preview()
                window.header.set_candidate(
                    "Overlay: base FPS · frame generation · PC latency"
                )
                window.controls.set_status_text(
                    "In-Game Overlay — representative live telemetry."
                )
                window.tabs.setCurrentIndex(window.overlay_tab_index)

            app.processEvents()
            _capture(
                window,
                QtCore,
                app,
                frame_dir / f"frame-{frame_index:03d}.png",
            )

        # GitHub mobile does not autoplay GIFs: the static miniature is the
        # FIRST frame. Open on the scan-complete payoff (all three verified
        # curves) held briefly, so the preview sells the result instead of a
        # single gray baseline curve; the scan story then plays from the top.
        payoff_index = int(PROFILES_REVEAL_S * FPS) - 1
        hold_frames = int(round(PAYOFF_HOLD_S * FPS))
        for index in range(frame_count - 1, -1, -1):
            (frame_dir / f"frame-{index:03d}.png").rename(
                frame_dir / f"frame-{index + hold_frames:03d}.png"
            )
        for index in range(hold_frames):
            shutil.copy2(
                frame_dir / f"frame-{payoff_index + hold_frames:03d}.png",
                frame_dir / f"frame-{index:03d}.png",
            )

        # Static fallback/preview: the same completion payoff.
        poster_path = output_path.with_name(output_path.stem + "-poster.png")
        shutil.copy2(frame_dir / f"frame-{payoff_index + hold_frames:03d}.png", poster_path)
        _encode_gif(frame_dir, output_path)
    finally:
        window.window.close()
        app.processEvents()
        shutil.rmtree(work_dir, ignore_errors=True)

    size_mib = output_path.stat().st_size / 1024 / 1024
    print(f"GIF: {output_path}")
    print(f"Poster: {output_path.with_name(output_path.stem + '-poster.png')}")
    print(f"Render: real PySide6 MainWindow + pyqtgraph, {FPS} fps, {DURATION_S:.0f}s")
    print(f"GIF size: {size_mib:.2f} MiB")


def main() -> int:
    output = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUTPUT
    render(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
