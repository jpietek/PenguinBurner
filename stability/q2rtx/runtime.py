from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from common.penguin_burner_paths import claim_desktop_user_ownership
from drivers.nvidia.daemon_gpu import DaemonGpuClient

from .assets import _validate_demo_name, resolve_q2rtx_executable, resolve_workload
from .constants import (
    FATAL_OUTPUT_REGEXES,
    FATAL_OUTPUT_PATTERNS,
    LAUNCHER_ERROR_PATTERNS,
    Q2RTX_LAUNCHER_ERROR_REASON,
)
from .gpu_binding import (
    _apply_nvidia_render_offload_env,
    _query_selected_nvidia_gpu,
    _selected_gpu_log_lines,
)
from .identity import _prepare_q2rtx_subprocess_env, _resolve_q2rtx_run_identity
from .install import _prepare_q2rtx_runtime_env
from .models import (
    Q2RTXBenchmarkSummary,
    Q2RTXStabilityConfig,
    Q2RTXStabilityResult,
    StabilityTestError,
    TelemetrySample,
)
from .output import (
    _benchmark_summary_from_events,
    _format_sample_metrics,
    _read_recent_output,
    _scan_output_for_fatal_patterns,
)
from .process_harness import (
    _child_process_group_preexec,
    _terminate_process_group,
    _wrap_command_for_live_output,
)
from .telemetry import (
    _query_xid_messages_since,
    query_gpu_metrics,
)
from .resolution import resolve_q2rtx_render_resolution


@dataclass(slots=True)
class _Q2RTXProcessRun:
    process_exit_code: int | None
    observed_duration_s: float
    telemetry_samples: list[TelemetrySample]
    companion_telemetry_samples: list[TelemetrySample]
    exit_reason: str
    benchmark_events: list[dict]
    benchmark_event_errors: list[str]
    benchmark_summary: Q2RTXBenchmarkSummary | None
    benchmark_telemetry_samples: list[TelemetrySample]
    fatal_output_matches: list[str]
    output_tail: list[str]


def _result_has_launcher_error(result: Q2RTXStabilityResult) -> bool:
    """True when Q2RTX failed in the dynamic loader."""
    tail = "\n".join(str(line) for line in result.output_tail)
    return any(pattern in tail for pattern in LAUNCHER_ERROR_PATTERNS)


def _reclassify_launcher_failure(
    result: Q2RTXStabilityResult,
) -> Q2RTXStabilityResult:
    """Tag an unrecovered loader failure so Auto-UV never blacklists the voltage."""
    if bool(result.success) or not _result_has_launcher_error(result):
        return result
    return replace(result, reason=Q2RTX_LAUNCHER_ERROR_REASON)


def _common_q2rtx_args(
    *,
    width: int,
    height: int,
    demo_name: str,
    duration_s: float,
) -> list[str]:
    return [
        "+set",
        "sys_console",
        "1",
        "+set",
        "bench",
        "1",
        "+set",
        "bench_seconds",
        f"{float(duration_s):.3f}",
        "+set",
        "bench_demo",
        demo_name,
        "+set",
        "bench_headless",
        "1",
        "+set",
        "bench_constant_load",
        "1",
        "+set",
        "bench_min_loops",
        "1",
        "+set",
        "bench_width",
        str(int(width)),
        "+set",
        "bench_height",
        str(int(height)),
        "+set",
        "bench_event_stdout",
        "1",
        "+set",
        "vid_rtx",
        "1",
        "+set",
        "cl_async",
        "1",
        "+set",
        "r_maxfps",
        "0",
        "+set",
        "cl_maxfps",
        "1000",
        "+set",
        "vid_vsync",
        "0",
        "+set",
        "drs_enable",
        "0",
        "+set",
        "drs_minscale",
        "100",
        "+set",
        "drs_maxscale",
        "100",
        "+set",
        "viewsize",
        "100",
        "+set",
        "scr_demobar",
        "0",
        "+set",
        "scr_fps",
        "0",
        "+set",
        "s_enable",
        "0",
        "+set",
        "bloom_enable",
        "1",
        "+set",
        "flt_enable",
        "1",
        "+set",
        "flt_taa",
        "1",
        "+set",
        "flt_fsr_enable",
        "0",
        "+set",
        "gr_enable",
        "1",
        "+set",
        "physical_sky_draw_clouds",
        "1",
        "+set",
        "pt_caustics",
        "1",
        "+set",
        "pt_cameras",
        "1",
        "+set",
        "pt_direct_polygon_lights",
        "1",
        "+set",
        "pt_direct_dyn_lights",
        "1",
        "+set",
        "pt_direct_sun_light",
        "1",
        "+set",
        "pt_indirect_polygon_lights",
        "1",
        "+set",
        "pt_indirect_dyn_lights",
        "1",
        "+set",
        "pt_enable_particles",
        "1",
        "+set",
        "pt_enable_beams",
        "1",
        "+set",
        "pt_enable_sprites",
        "1",
        "+set",
        "pt_enable_surface_lights",
        "1",
        "+set",
        "pt_num_bounce_rays",
        "2",
        "+set",
        "pt_reflect_refract",
        "8",
        "+set",
        "pt_thick_glass",
        "2",
    ]


def build_benchmark_command(
    executable_path: Path,
    *,
    demo_name: str,
    width: int,
    height: int,
    duration_s: float = 60.0,
) -> list[str]:
    demo_name = _validate_demo_name(demo_name)
    return [
        str(executable_path),
        *_common_q2rtx_args(
            width=width,
            height=height,
            demo_name=demo_name,
            duration_s=float(duration_s),
        ),
    ]


def _q2rtx_command_with_event_fd(command: list[str], event_fd: int) -> list[str]:
    command_with_fd = list(command)
    for index, value in enumerate(command_with_fd[:-1]):
        if value == "bench_event_stdout":
            command_with_fd[index + 1] = "0"
            break
    else:
        command_with_fd.extend(["+set", "bench_event_stdout", "0"])
    command_with_fd.extend(["+set", "bench_event_fd", str(int(event_fd))])
    return command_with_fd


def _prepare_log_path(log_dir: Path) -> Path:
    log_dir = log_dir.expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(log_dir, include_parents=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return log_dir / f"q2rtx-stability-{timestamp}.log"


def _benchmark_window_telemetry_samples(
    telemetry_samples: list[TelemetrySample],
    benchmark_summary: Q2RTXBenchmarkSummary | None,
) -> list[TelemetrySample]:
    if benchmark_summary is None:
        return []
    if benchmark_summary.measure_start_elapsed_s is None:
        return []
    if benchmark_summary.measured_s <= 0.0:
        return []
    start_s = max(0.0, float(benchmark_summary.measure_start_elapsed_s))
    end_s = start_s + float(benchmark_summary.measured_s)
    return [
        sample
        for sample in telemetry_samples
        if float(sample.elapsed_s) >= start_s and float(sample.elapsed_s) <= end_s
    ]


def _scan_fatal_patterns_in_lines(lines: list[str] | deque[str]) -> list[str]:
    text = "\n".join(str(line) for line in lines)
    normalized_text = text.casefold()
    matches = [
        pattern
        for pattern in FATAL_OUTPUT_PATTERNS
        if str(pattern).casefold() in normalized_text
    ]
    for regex in FATAL_OUTPUT_REGEXES:
        for match in regex.finditer(text):
            matches.append(match.group(0))
    return matches


def _q2rtx_exit_failure_reason(process_exit_code: int | None) -> str:
    if process_exit_code is None:
        return "benchmark-nonzero-exit"
    if int(process_exit_code) < 0:
        signal_number = abs(int(process_exit_code))
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = f"SIG{signal_number}"
        return f"benchmark-crashed-signal-{signal_name}"
    return f"benchmark-nonzero-exit-{int(process_exit_code)}"


def _drain_text_fd(
    fd: int,
    *,
    buffer: str,
    on_line,
) -> tuple[str, bool]:
    closed = False
    while True:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            break
        except OSError:
            closed = True
            break
        if not chunk:
            closed = True
            break
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            on_line(line.rstrip("\r"))
    return buffer, closed


FRAME_HANG_WATCHDOG_REASON = "gpu-hang-watchdog"


class _FrameProgressWatchdog:
    """Trips only on a definitive, sustained rendering freeze.

    A trip immediately blacklists the probed voltage, so this is deliberately
    biased hard against false positives — better to miss a hang (the pass
    timeout still catches it) than to discard a stable voltage.

    Three independent guards keep transient behaviour from ever tripping it:

    1. The fork emits a *time-throttled* ``frame`` heartbeat (~every 250 ms of
       wall time, not per rendered frame), so even a collapse to a few FPS keeps
       the pulse flowing; frame-time jitter and slow frames never gap it.
    2. It only becomes trippable after confirmed steady rendering
       (``_MIN_ADVANCES_BEFORE_TRIP`` distinct frame advances). A single warm-up
       frame followed by a one-off compile/scene pause cannot arm-then-trip it.
    3. It disarms on any phase/loop/done boundary (scene reloads and finish
       drains legitimately stop producing frames) and re-confirms from scratch.

    Only the process being alive with the frame index frozen for the *entire*
    ``threshold_s`` window — after steady rendering was established — trips it.
    A binary that never emits ``frame`` events leaves the watchdog dormant.
    """

    __slots__ = ("threshold_s", "_advances", "_last_index", "_last_progress_s")

    # Distinct frame advances required before a freeze can trip. Proves the
    # render loop was genuinely live, so a lone startup frame can't false-trip.
    _MIN_ADVANCES_BEFORE_TRIP = 2

    # Events that mark a legitimate rendering pause; reset (disarm) on these so a
    # scene reload or hold phase never looks like a hang.
    _RESET_EVENTS = frozenset({"phase", "loop_start", "loop_done", "done"})

    def __init__(self, threshold_s: float) -> None:
        self.threshold_s = float(threshold_s)
        self._advances = 0
        self._last_index: int | None = None
        self._last_progress_s = 0.0

    @property
    def armed(self) -> bool:
        return self._advances >= self._MIN_ADVANCES_BEFORE_TRIP

    def note_event(self, event: dict, now_monotonic: float) -> None:
        name = event.get("event")
        if name == "frame":
            index = event.get("n")
            if isinstance(index, bool) or not isinstance(index, int):
                return
            if self._last_index is None or index != self._last_index:
                self._advances += 1
                self._last_index = index
                self._last_progress_s = float(now_monotonic)
        elif name in self._RESET_EVENTS:
            self._advances = 0
            self._last_index = None
            self._last_progress_s = float(now_monotonic)

    def tripped(self, now_monotonic: float) -> bool:
        if not self.armed or self.threshold_s <= 0.0:
            return False
        return (float(now_monotonic) - self._last_progress_s) >= self.threshold_s


def _benchmark_abort_is_immediate(reason: str) -> bool:
    reason = str(reason or "").strip()
    if not reason:
        return False
    if reason in {
        "benchmark-duration-short",
        "benchmark-crashed-signal",
        "benchmark-event-pipe-closed",
        "benchmark-event-protocol-error",
        "benchmark-fatal-event",
        "benchmark-measure-start-missing",
        "benchmark-metrics-invalid",
        "benchmark-nonzero-exit",
        "benchmark-start-missing",
        "benchmark-summary-missing",
        "user-stop-requested",
        "fatal-q2rtx-output",
        "fatal-cuda-output",
        "nvidia-xid-detected",
    }:
        return True
    if reason.startswith(
        (
            "profile-verification-voltage-mismatch",
            "q2rtx-selected-nvidia-gpu-idle",
            "telemetry-live-load-lost",
        )
    ):
        return True
    return False


def _fatal_output_abort_reason(
    fatal_output_matches: list[str],
    *,
    running: str,
) -> str | None:
    if not fatal_output_matches:
        return None
    if str(running).strip().lower() == "cuda":
        return "fatal-cuda-output"
    return "fatal-q2rtx-output"


def _reap_concurrent_companion(
    companion: subprocess.Popen,
    *,
    companion_log_path: Path,
    q2rtx_succeeded: bool,
    grace_s: float = 30.0,
) -> tuple[int | None, str | None]:
    """Collect a concurrently-running CUDA companion once the benchmark ends.

    On a failed benchmark the companion is simply killed — the probe verdict
    is already decided. On success we give an aligned-duration companion a
    short grace to finish; a straggler killed at the grace deadline is not a
    failure (the probe's stress window is over), but a nonzero self-exit or a
    fatal pattern in its log is.
    """
    if not q2rtx_succeeded:
        _terminate_process_group(companion)
        return companion.poll(), None
    deadline = time.monotonic() + float(grace_s)
    while companion.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    straggler = companion.poll() is None
    if straggler:
        _terminate_process_group(companion)
    # Always scan the companion log: a marginal undervolt often prints an
    # Xid/fatal line and then hangs, so the straggler path must not skip it.
    fatal_reason = _fatal_output_abort_reason(
        list(_scan_output_for_fatal_patterns(companion_log_path)),
        running="cuda",
    )
    if straggler:
        return None, fatal_reason
    return companion.poll(), fatal_reason


def _run_companion_process(
    *,
    config: Q2RTXStabilityConfig,
    command: tuple[str, ...],
    log_file,
    run_start_monotonic: float,
    cuda_telemetry_samples: list[TelemetrySample],
    gpu_client: DaemonGpuClient,
    section_name: str,
    workload_name: str,
    log_path: Path,
    progress_elapsed_offset_s: float = 0.0,
) -> tuple[int | None, str | None]:
    companion_env = dict(os.environ)
    companion_env["PYTHONUNBUFFERED"] = "1"
    companion_process = subprocess.Popen(
        _wrap_command_for_live_output(list(command)),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=companion_env,
        start_new_session=True,
    )
    next_heartbeat_monotonic = time.monotonic()
    next_sample_monotonic = next_heartbeat_monotonic
    companion_start_monotonic = next_heartbeat_monotonic
    companion_exit_code = None
    try:
        while True:
            now_monotonic = time.monotonic()
            wall_elapsed_s = now_monotonic - run_start_monotonic
            pass_elapsed_s = (
                float(progress_elapsed_offset_s)
                + now_monotonic
                - companion_start_monotonic
            )
            companion_exit_code = companion_process.poll()
            latest_sample = None
            if now_monotonic >= next_sample_monotonic:
                latest_sample = query_gpu_metrics(
                    config.gpu_index,
                    gpu_client=gpu_client,
                )
                if latest_sample is not None:
                    latest_sample.elapsed_s = pass_elapsed_s
                    cuda_telemetry_samples.append(latest_sample)
                next_sample_monotonic = now_monotonic + float(config.poll_interval_s)
            if now_monotonic >= next_heartbeat_monotonic:
                latest_metrics = _format_sample_metrics(
                    latest_sample
                    if latest_sample is not None
                    else (
                        cuda_telemetry_samples[-1] if cuda_telemetry_samples else None
                    )
                )
                log_file.write(
                    f"# heartbeat elapsed={wall_elapsed_s:.1f}s "
                    f"net_elapsed={pass_elapsed_s:.1f}s "
                    f"running=cuda"
                    + (f" {latest_metrics}" if latest_metrics else "")
                    + "\n"
                )
                log_file.flush()
                progress_state = {
                    "section_name": section_name,
                    "workload_name": workload_name,
                    "elapsed_s": float(wall_elapsed_s),
                    "net_elapsed_s": float(pass_elapsed_s),
                    "progress_elapsed_s": float(wall_elapsed_s),
                    "command": list(command),
                    "log_path": log_path,
                    "latest_sample": (
                        latest_sample
                        if latest_sample is not None
                        else (
                            cuda_telemetry_samples[-1]
                            if cuda_telemetry_samples
                            else None
                        )
                    ),
                    "telemetry_samples": [],
                    "running": "cuda",
                    "fatal_output_matches": list(
                        _scan_output_for_fatal_patterns(log_path)
                    ),
                }
                if config.progress_callback is not None:
                    try:
                        config.progress_callback(progress_state)
                    except Exception:
                        pass
                fatal_abort_reason = _fatal_output_abort_reason(
                    list(progress_state["fatal_output_matches"]),
                    running="cuda",
                )
                if fatal_abort_reason is not None:
                    _terminate_process_group(companion_process)
                    return companion_process.poll() or -15, str(fatal_abort_reason)
                if config.abort_callback is not None:
                    try:
                        abort_reason = config.abort_callback(progress_state)
                    except Exception:
                        abort_reason = None
                    if abort_reason:
                        _terminate_process_group(companion_process)
                        return companion_process.poll() or -15, str(abort_reason)
                next_heartbeat_monotonic = now_monotonic + 1.0
            if companion_exit_code is not None:
                return companion_exit_code, None
            time.sleep(0.1)
    finally:
        _terminate_process_group(companion_process)


def run_cuda_stability_test(config: Q2RTXStabilityConfig) -> Q2RTXStabilityResult:
    command = tuple(config.companion_command or ())
    if not command:
        raise StabilityTestError("CUDA stability command is not configured")
    if int(config.duration_s) <= 0:
        raise StabilityTestError("CUDA stability duration must be greater than zero")
    if float(config.poll_interval_s) <= 0:
        raise StabilityTestError("stability poll interval must be greater than zero")

    started_at = datetime.now().astimezone()
    log_path = _prepare_log_path(config.log_dir)
    command_text = " ".join(str(part) for part in command)
    log_path.write_text(
        (
            "# PenguinBurner CUDA stability log\n"
            f"# started_at={started_at.isoformat()}\n"
            "# workload=CUDA compute\n"
            f"# command={command_text}\n"
            f"# workdir={Path.cwd()}\n"
        ),
        encoding="utf-8",
    )
    claim_desktop_user_ownership(log_path)

    cuda_telemetry_samples: list[TelemetrySample] = []
    gpu_client = DaemonGpuClient(config.gpu_index)
    run_start_monotonic = time.monotonic()
    exit_code = None
    abort_reason = None
    with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
        exit_code, abort_reason = _run_companion_process(
            config=config,
            command=command,
            log_file=log_file,
            run_start_monotonic=run_start_monotonic,
            cuda_telemetry_samples=cuda_telemetry_samples,
            gpu_client=gpu_client,
            section_name="cuda-compute",
            workload_name="CUDA compute",
            log_path=log_path,
        )
    observed_duration_s = time.monotonic() - run_start_monotonic
    fatal_output_matches = _scan_output_for_fatal_patterns(log_path)
    xid_messages = _query_xid_messages_since(started_at)
    output_tail = _read_recent_output(log_path)
    if abort_reason:
        reason = str(abort_reason)
        success = False
    elif exit_code not in (None, 0):
        reason = f"cuda-bruteforce-failed exit={int(exit_code)}"
        success = False
    elif fatal_output_matches:
        reason = "fatal-cuda-output"
        success = False
    elif xid_messages:
        reason = "nvidia-xid-detected"
        success = False
    else:
        reason = "ok"
        success = True
    return Q2RTXStabilityResult(
        success=success,
        reason=reason,
        workload_kind="cuda",
        workload_name="CUDA compute",
        command=list(command),
        executable_path=Path(str(command[0])),
        workdir=Path.cwd(),
        duration_requested_s=int(config.duration_s),
        duration_observed_s=float(observed_duration_s),
        demo_path=None,
        log_path=log_path,
        process_exit_code=exit_code,
        shutdown_mode="cuda-complete" if success else reason,
        fatal_output_matches=fatal_output_matches,
        xid_messages=xid_messages,
        telemetry_samples=list(cuda_telemetry_samples),
        companion_telemetry_samples=list(cuda_telemetry_samples),
        output_tail=output_tail,
    )


def _run_benchmark_process(
    *,
    config: Q2RTXStabilityConfig,
    command: list[str],
    workdir: Path,
    log_path: Path,
    section_name: str,
    workload_name: str,
    runtime_env: dict[str, str],
) -> _Q2RTXProcessRun:
    telemetry_samples: list[TelemetrySample] = []
    gpu_client = DaemonGpuClient(config.gpu_index)
    selected_gpu = _query_selected_nvidia_gpu(config.gpu_index)
    q2rtx_env = _apply_nvidia_render_offload_env(
        runtime_env,
        selected_gpu=selected_gpu,
    )
    child_env, child_preexec_fn, child_user_name = _prepare_q2rtx_subprocess_env(
        q2rtx_env,
    )
    section_started_dt = datetime.now().astimezone()
    section_started_at = section_started_dt.isoformat()
    run_start_monotonic = time.monotonic()
    next_sample_monotonic = run_start_monotonic
    next_heartbeat_monotonic = run_start_monotonic
    exit_reason = "completed"
    observed_duration_s = 0.0
    companion_exit_code = None
    companion_abort_reason = None
    cuda_telemetry_samples: list[TelemetrySample] = []
    process = None
    process_exit_code: int | None = None
    benchmark_events: list[dict] = []
    benchmark_event_errors: list[str] = []
    benchmark_summary: Q2RTXBenchmarkSummary | None = None
    benchmark_telemetry_samples: list[TelemetrySample] = []
    frame_watchdog = _FrameProgressWatchdog(config.hang_watchdog_s)
    output_tail: deque[str] = deque(maxlen=80)
    fatal_output_matches_seen: set[str] = set()
    event_read_fd = -1
    event_write_fd = -1
    event_buffer = ""
    output_buffer = ""
    event_closed = False
    output_closed = False
    concurrent_companion = None
    companion_log_file = None
    companion_log_path = log_path.with_name(f"{log_path.name}.cuda")

    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            event_read_fd, event_write_fd = os.pipe()
            os.set_blocking(event_read_fd, False)
            launch_command = _q2rtx_command_with_event_fd(command, event_write_fd)

            def _handle_event_line(line: str) -> None:
                stripped = str(line).strip()
                if not stripped:
                    return
                log_file.write(f"{stripped}\n")
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    benchmark_event_errors.append(
                        f"invalid-json line={stripped[:200]!r} error={exc.msg}"
                    )
                    return
                if not isinstance(event, dict) or "event" not in event:
                    benchmark_event_errors.append(
                        f"invalid-event line={stripped[:200]!r}"
                    )
                    return
                benchmark_events.append(event)
                frame_watchdog.note_event(event, time.monotonic())

            def _handle_output_line(line: str) -> None:
                text = str(line).rstrip("\r")
                output_tail.append(text)
                log_file.write(f"{text}\n")
                for match in _scan_fatal_patterns_in_lines([text]):
                    fatal_output_matches_seen.add(str(match))

            def _refresh_benchmark_state() -> None:
                nonlocal benchmark_summary
                nonlocal benchmark_telemetry_samples
                benchmark_summary = _benchmark_summary_from_events(benchmark_events)
                benchmark_telemetry_samples = _benchmark_window_telemetry_samples(
                    telemetry_samples,
                    benchmark_summary,
                )

            log_file.write(f"\n=== {section_name} start {section_started_at} ===\n")
            if child_user_name is not None:
                log_file.write(f"# q2rtx_run_user={child_user_name}\n")
            log_file.write(
                "# q2rtx_nvidia_render_offload="
                "__NV_PRIME_RENDER_OFFLOAD=1 "
                "__VK_LAYER_NV_optimus=NVIDIA_only "
                "__GLX_VENDOR_LIBRARY_NAME=nvidia\n"
            )
            for line in _selected_gpu_log_lines(
                selected_gpu=selected_gpu,
                child_env=child_env,
            ):
                log_file.write(f"{line}\n")
            log_file.write("# q2rtx_window=pb-headless\n")
            log_file.write(f"# launch_command={' '.join(command)}\n")
            if config.companion_command:
                log_file.write(
                    f"# companion_command={' '.join(list(config.companion_command))}\n"
                )
            log_file.write("# q2rtx_benchmark_events=pipe-fd\n")
            log_file.flush()
            process = subprocess.Popen(
                _wrap_command_for_live_output(launch_command),
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=child_env,
                preexec_fn=_child_process_group_preexec(child_preexec_fn),
                pass_fds=(event_write_fd,),
            )
            os.close(event_write_fd)
            event_write_fd = -1
            if process.stdout is not None:
                os.set_blocking(process.stdout.fileno(), False)

            if config.companion_command is not None and config.companion_concurrent:
                # The companion writes to its own log so its output never
                # interleaves with the benchmark's; fatal-pattern scanning
                # reads that file separately.
                companion_log_file = companion_log_path.open(
                    "a", encoding="utf-8", errors="replace"
                )
                companion_env = dict(os.environ)
                companion_env["PYTHONUNBUFFERED"] = "1"
                concurrent_companion = subprocess.Popen(
                    _wrap_command_for_live_output(list(config.companion_command)),
                    stdout=companion_log_file,
                    stderr=subprocess.STDOUT,
                    env=companion_env,
                    start_new_session=True,
                )
                log_file.write(f"# cuda_companion_log={companion_log_path}\n")
                log_file.write("# cuda_companion_mode=concurrent\n")
                log_file.flush()

            while True:
                now_monotonic = time.monotonic()
                pass_elapsed_s = now_monotonic - run_start_monotonic
                if not event_closed:
                    event_buffer, event_closed = _drain_text_fd(
                        event_read_fd,
                        buffer=event_buffer,
                        on_line=_handle_event_line,
                    )
                if process.stdout is not None and not output_closed:
                    output_buffer, output_closed = _drain_text_fd(
                        process.stdout.fileno(),
                        buffer=output_buffer,
                        on_line=_handle_output_line,
                    )
                _refresh_benchmark_state()
                process_exit_code = process.poll()
                if concurrent_companion is not None:
                    companion_exit_code = concurrent_companion.poll()
                    if companion_exit_code not in (None, 0):
                        # The compute half of the combined load died: that is
                        # an instability verdict, not a companion detail —
                        # end the probe immediately like a fatal q2rtx event.
                        _terminate_process_group(process)
                        process_exit_code = process.returncode
                        observed_duration_s = time.monotonic() - run_start_monotonic
                        exit_reason = (
                            f"cuda-bruteforce-failed exit={int(companion_exit_code)}"
                        )
                        break
                if process_exit_code is None and frame_watchdog.tripped(now_monotonic):
                    _terminate_process_group(process)
                    process_exit_code = process.returncode
                    observed_duration_s = time.monotonic() - run_start_monotonic
                    exit_reason = FRAME_HANG_WATCHDOG_REASON
                    break
                if now_monotonic >= next_heartbeat_monotonic:
                    latest_metrics = _format_sample_metrics(
                        telemetry_samples[-1] if telemetry_samples else None
                    )
                    log_file.write(
                        f"# heartbeat elapsed={pass_elapsed_s:.1f}s "
                        f"running=q2rtx"
                        + (f" {latest_metrics}" if latest_metrics else "")
                        + "\n"
                    )
                    log_file.flush()
                    next_heartbeat_monotonic = now_monotonic + 1.0
                if benchmark_event_errors:
                    _terminate_process_group(process)
                    process_exit_code = process.returncode
                    observed_duration_s = time.monotonic() - run_start_monotonic
                    exit_reason = "benchmark-event-protocol-error"
                    break
                if any(event.get("event") == "fatal" for event in benchmark_events):
                    _terminate_process_group(process)
                    process_exit_code = process.returncode
                    observed_duration_s = time.monotonic() - run_start_monotonic
                    exit_reason = "benchmark-fatal-event"
                    break
                if (
                    event_closed
                    and process_exit_code is None
                    and not any(
                        event.get("event") == "done" for event in benchmark_events
                    )
                ):
                    _terminate_process_group(process)
                    process_exit_code = process.returncode
                    observed_duration_s = time.monotonic() - run_start_monotonic
                    exit_reason = "benchmark-event-pipe-closed"
                    break
                if process_exit_code is not None:
                    observed_duration_s = pass_elapsed_s
                    break

                if pass_elapsed_s >= float(config.single_pass_timeout_s):
                    _terminate_process_group(process)
                    process_exit_code = process.returncode
                    observed_duration_s = time.monotonic() - run_start_monotonic
                    exit_reason = "benchmark-timeout"
                    break

                if now_monotonic >= next_sample_monotonic:
                    sample = query_gpu_metrics(
                        config.gpu_index,
                        gpu_client=gpu_client,
                    )
                    if sample is not None:
                        sample.elapsed_s = pass_elapsed_s
                        telemetry_samples.append(sample)
                        if (
                            concurrent_companion is not None
                            and concurrent_companion.poll() is None
                        ):
                            cuda_telemetry_samples.append(sample)
                    benchmark_summary = _benchmark_summary_from_events(benchmark_events)
                    benchmark_telemetry_samples = _benchmark_window_telemetry_samples(
                        telemetry_samples,
                        benchmark_summary,
                    )
                    fatal_output_matches = sorted(fatal_output_matches_seen)
                    net_elapsed_s = (
                        float(benchmark_summary.measured_s)
                        if benchmark_summary is not None
                        else 0.0
                    )
                    progress_state = {
                        "section_name": section_name,
                        "workload_name": workload_name,
                        "started_at": section_started_dt,
                        "elapsed_s": float(pass_elapsed_s),
                        "net_elapsed_s": float(net_elapsed_s),
                        "progress_elapsed_s": float(pass_elapsed_s),
                        "command": list(command),
                        "workdir": workdir,
                        "log_path": log_path,
                        "process_pid": int(process.pid),
                        "latest_sample": telemetry_samples[-1]
                        if telemetry_samples
                        else None,
                        "telemetry_samples": list(telemetry_samples),
                        "running": "q2rtx",
                        "fatal_output_matches": list(fatal_output_matches),
                        "benchmark_events": list(benchmark_events),
                        "benchmark_event_errors": list(benchmark_event_errors),
                        "benchmark_summary": benchmark_summary,
                    }
                    if config.progress_callback is not None:
                        try:
                            config.progress_callback(progress_state)
                        except Exception:
                            pass
                    fatal_abort_reason = _fatal_output_abort_reason(
                        fatal_output_matches,
                        running="q2rtx",
                    )
                    if fatal_abort_reason is None and concurrent_companion is not None:
                        fatal_abort_reason = _fatal_output_abort_reason(
                            list(_scan_output_for_fatal_patterns(companion_log_path)),
                            running="cuda",
                        )
                    if fatal_abort_reason is not None:
                        _terminate_process_group(process)
                        process_exit_code = process.returncode
                        observed_duration_s = time.monotonic() - run_start_monotonic
                        exit_reason = str(fatal_abort_reason)
                        break
                    if config.abort_callback is not None:
                        try:
                            abort_reason = config.abort_callback(progress_state)
                        except Exception:
                            abort_reason = None
                        if abort_reason and _benchmark_abort_is_immediate(
                            str(abort_reason)
                        ):
                            _terminate_process_group(process)
                            process_exit_code = process.returncode
                            observed_duration_s = time.monotonic() - run_start_monotonic
                            exit_reason = str(abort_reason)
                            break
                    next_sample_monotonic = now_monotonic + float(
                        config.poll_interval_s
                    )

                time.sleep(0.1)

            if not event_closed:
                event_buffer, event_closed = _drain_text_fd(
                    event_read_fd,
                    buffer=event_buffer,
                    on_line=_handle_event_line,
                )
            if event_buffer.strip():
                _handle_event_line(event_buffer.strip())
                event_buffer = ""
            if process.stdout is not None and not output_closed:
                output_buffer, output_closed = _drain_text_fd(
                    process.stdout.fileno(),
                    buffer=output_buffer,
                    on_line=_handle_output_line,
                )
            if output_buffer:
                _handle_output_line(output_buffer)
                output_buffer = ""
            _refresh_benchmark_state()
            log_file.flush()

            if concurrent_companion is not None:
                companion_exit_code, companion_abort_reason = (
                    _reap_concurrent_companion(
                        concurrent_companion,
                        companion_log_path=companion_log_path,
                        q2rtx_succeeded=(
                            process_exit_code == 0 and exit_reason == "completed"
                        ),
                    )
                )
                # observed_duration_s keeps the benchmark-loop value: the
                # companion reap grace is not benchmark time.
                if companion_abort_reason:
                    if exit_reason == "completed":
                        exit_reason = str(companion_abort_reason)
                elif companion_exit_code not in (None, 0):
                    if exit_reason == "completed":
                        exit_reason = (
                            f"cuda-bruteforce-failed exit={int(companion_exit_code)}"
                        )
            elif (
                process_exit_code == 0
                and exit_reason == "completed"
                and config.companion_command is not None
            ):
                q2rtx_net_duration_s = (
                    float(benchmark_summary.measured_s)
                    if benchmark_summary is not None
                    else 0.0
                )
                companion_exit_code, companion_abort_reason = _run_companion_process(
                    config=config,
                    command=config.companion_command,
                    log_file=log_file,
                    run_start_monotonic=run_start_monotonic,
                    cuda_telemetry_samples=cuda_telemetry_samples,
                    gpu_client=gpu_client,
                    section_name=section_name,
                    workload_name=workload_name,
                    log_path=log_path,
                    progress_elapsed_offset_s=float(q2rtx_net_duration_s),
                )
                observed_duration_s = time.monotonic() - run_start_monotonic
                if companion_abort_reason:
                    exit_reason = str(companion_abort_reason)
                elif companion_exit_code not in (None, 0):
                    exit_reason = (
                        f"cuda-bruteforce-failed exit={int(companion_exit_code)}"
                    )
    finally:
        if event_write_fd != -1:
            try:
                os.close(event_write_fd)
            except OSError:
                pass
        if event_read_fd != -1:
            try:
                os.close(event_read_fd)
            except OSError:
                pass
        if concurrent_companion is not None:
            _terminate_process_group(concurrent_companion)
        if companion_log_file is not None:
            try:
                companion_log_file.close()
            except OSError:
                pass
        _terminate_process_group(process)

    if companion_abort_reason and exit_reason == "completed":
        exit_reason = str(companion_abort_reason)
    elif companion_exit_code not in (None, 0) and exit_reason == "completed":
        exit_reason = f"cuda-bruteforce-failed exit={int(companion_exit_code)}"

    if exit_reason.startswith(("cuda", "fatal-cuda")):
        # Keep the failure evidence available to Auto-UV's classifier. The
        # concurrent companion writes separately; serial CUDA uses the main log.
        output_tail.extend(_read_recent_output(
            companion_log_path if concurrent_companion is not None else log_path
        ))

    return _Q2RTXProcessRun(
        process_exit_code=process_exit_code,
        observed_duration_s=float(observed_duration_s),
        telemetry_samples=list(telemetry_samples),
        companion_telemetry_samples=list(cuda_telemetry_samples),
        exit_reason=str(exit_reason),
        benchmark_events=list(benchmark_events),
        benchmark_event_errors=list(benchmark_event_errors),
        benchmark_summary=benchmark_summary,
        benchmark_telemetry_samples=list(benchmark_telemetry_samples),
        fatal_output_matches=sorted(fatal_output_matches_seen),
        output_tail=list(output_tail),
    )


def _run_benchmark_session(
    *,
    config: Q2RTXStabilityConfig,
    executable_path: Path,
    workdir: Path,
    workload_name: str,
    demo_path: Path | None,
    log_path: Path,
    runtime_env: dict[str, str],
) -> Q2RTXStabilityResult:
    started_at = datetime.now().astimezone()
    telemetry_samples: list[TelemetrySample] = []
    shutdown_mode = "not-started"
    process_exit_code: int | None = None
    early_failure_reason = ""
    observed_duration_s = 0.0
    command_used: list[str] = []
    companion_telemetry_samples: list[TelemetrySample] = []
    fatal_output_matches: list[str] = []
    xid_messages: list[str] = []
    benchmark_summary: Q2RTXBenchmarkSummary | None = None
    benchmark_telemetry_samples: list[TelemetrySample] = []
    command_used = build_benchmark_command(
        executable_path,
        demo_name=workload_name,
        width=config.width,
        height=config.height,
        duration_s=float(config.duration_s),
    )

    log_path.write_text(
        (
            "# PenguinBurner Q2RTX stability log\n"
            f"# started_at={started_at.isoformat()}\n"
            f"# workload={workload_name}\n"
            f"# command={' '.join(command_used)}\n"
            f"# workdir={workdir}\n"
            f"# demo_name={workload_name}\n"
            f"# demo_path={demo_path if demo_path is not None else '<unresolved>'}\n"
        ),
        encoding="utf-8",
    )
    claim_desktop_user_ownership(log_path)
    run_identity = _resolve_q2rtx_run_identity()
    if run_identity is not None:
        with log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            log_file.write(f"# q2rtx_target_user={run_identity['user_name']}\n")

    section_name = f"benchmark {float(config.duration_s):.1f}s"
    process_run = _run_benchmark_process(
        config=config,
        command=command_used,
        workdir=workdir,
        log_path=log_path,
        section_name=section_name,
        workload_name=workload_name,
        runtime_env=runtime_env,
    )
    process_exit_code = process_run.process_exit_code
    observed_duration_s = process_run.observed_duration_s
    telemetry_samples = process_run.telemetry_samples
    companion_telemetry_samples = process_run.companion_telemetry_samples
    shutdown_mode = process_run.exit_reason
    benchmark_summary = process_run.benchmark_summary
    benchmark_telemetry_samples = process_run.benchmark_telemetry_samples
    benchmark_events = process_run.benchmark_events
    benchmark_event_errors = process_run.benchmark_event_errors
    fatal_output_matches = process_run.fatal_output_matches
    output_tail = process_run.output_tail
    has_start_event = any(event.get("event") == "start" for event in benchmark_events)
    has_measure_start_event = any(
        event.get("event") == "phase" and event.get("name") == "measure_start"
        for event in benchmark_events
    )
    has_fatal_event = any(event.get("event") == "fatal" for event in benchmark_events)

    if shutdown_mode != "completed":
        early_failure_reason = shutdown_mode
    elif benchmark_event_errors:
        early_failure_reason = "benchmark-event-protocol-error"
    elif not has_start_event:
        early_failure_reason = "benchmark-start-missing"
    elif has_fatal_event:
        early_failure_reason = "benchmark-fatal-event"
    elif process_exit_code not in (0, None):
        early_failure_reason = _q2rtx_exit_failure_reason(process_exit_code)
    elif benchmark_summary is None:
        early_failure_reason = "benchmark-summary-missing"
    elif (
        benchmark_summary.measure_start_elapsed_s is None or not has_measure_start_event
    ):
        early_failure_reason = "benchmark-measure-start-missing"
    elif (
        int(benchmark_summary.render_frames) <= 0
        or float(benchmark_summary.measured_s) <= 0.0
        or float(benchmark_summary.fps_avg) <= 0.0
    ):
        early_failure_reason = "benchmark-metrics-invalid"
    elif benchmark_summary.target_s is not None and float(
        benchmark_summary.measured_s
    ) + 0.001 < float(benchmark_summary.target_s):
        early_failure_reason = "benchmark-duration-short"

    xid_messages = _query_xid_messages_since(started_at)
    if not early_failure_reason and fatal_output_matches:
        early_failure_reason = "fatal-q2rtx-output"
    if not early_failure_reason and xid_messages:
        early_failure_reason = "nvidia-xid-detected"
    if not early_failure_reason:
        shutdown_mode = "benchmark-duration-complete"

    if early_failure_reason:
        reason = early_failure_reason
        success = False
    else:
        reason = "ok"
        success = True

    return Q2RTXStabilityResult(
        success=success,
        reason=reason,
        workload_kind="benchmark",
        workload_name=workload_name,
        command=command_used,
        executable_path=executable_path,
        workdir=workdir,
        duration_requested_s=int(config.duration_s),
        duration_observed_s=observed_duration_s,
        demo_path=demo_path,
        log_path=log_path,
        process_exit_code=process_exit_code,
        shutdown_mode=shutdown_mode,
        fatal_output_matches=fatal_output_matches,
        xid_messages=xid_messages,
        telemetry_samples=telemetry_samples,
        companion_telemetry_samples=companion_telemetry_samples,
        output_tail=output_tail,
        benchmark_summary=benchmark_summary,
        benchmark_measure_start_s=(
            benchmark_summary.measure_start_elapsed_s
            if benchmark_summary is not None
            else None
        ),
        benchmark_telemetry_samples=benchmark_telemetry_samples,
    )


def run_q2rtx_stability_test(config: Q2RTXStabilityConfig) -> Q2RTXStabilityResult:
    if int(config.width) <= 0 or int(config.height) <= 0:
        try:
            resolution = resolve_q2rtx_render_resolution(
                gpu_index=int(config.gpu_index),
                requested_width=int(config.width),
                requested_height=int(config.height),
            )
        except ValueError as exc:
            raise StabilityTestError(str(exc)) from exc
        config = replace(
            config,
            width=int(resolution.width),
            height=int(resolution.height),
        )
    if int(config.duration_s) <= 0:
        raise StabilityTestError("stability duration must be greater than zero")
    if int(config.width) <= 0 or int(config.height) <= 0:
        raise StabilityTestError("stability window size must be positive")
    if float(config.poll_interval_s) <= 0:
        raise StabilityTestError("stability poll interval must be greater than zero")
    if float(config.single_pass_timeout_s) <= 0:
        raise StabilityTestError(
            "single benchmark pass timeout must be greater than zero"
        )

    executable_path, workdir = resolve_q2rtx_executable()
    runtime_env = _prepare_q2rtx_runtime_env(
        executable_path,
        show_progress=False,
    )
    workload_name, demo_path = resolve_workload(
        config.demo_name,
        workdir=workdir,
    )
    log_path = _prepare_log_path(config.log_dir)

    result = _run_benchmark_session(
        config=config,
        executable_path=executable_path,
        workdir=workdir,
        workload_name=workload_name,
        demo_path=demo_path,
        log_path=log_path,
        runtime_env=runtime_env,
    )

    # A launch failure tells us nothing about voltage stability; tag it so the
    # Auto-UV layer never records the voltage as unsafe.
    return _reclassify_launcher_failure(result)
