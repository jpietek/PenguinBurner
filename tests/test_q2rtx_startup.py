from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import stability.q2rtx.runtime as q2rtx_runtime
from auto_uv.domain.types import FailureSeverity
from auto_uv.probes.live_abort import telemetry_live_abort_reason
from auto_uv.probes.runtime_guardrails import probe_failure_should_mark_voltage_unsafe
from auto_uv.probes.stability_decision import classify_failed_result
from profiles.verification.rules import profile_verification_voltage_abort_callback
from stability.q2rtx.models import Q2RTXStabilityConfig, TelemetrySample
from stability.q2rtx.output import _format_live_progress_state


@pytest.fixture
def run_events(tmp_path, monkeypatch):
    """Drive the actual process supervisor with a deterministic event timeline."""

    def run(
        *,
        startup_s=37.0,
        render_busy=True,
        timeout_s=120.0,
        abort=None,
        concurrent=False,
        companion=False,
        fatal=False,
        measure_event=True,
        missing_render_telemetry=False,
    ):
        clock = {"now": 0.0}
        states = []
        launches = []
        duration_s = 20.0
        # The child's benchmark clock starts after engine initialization;
        # elapsed_ms deliberately does not share the host's process origin.
        scheduled = [
            (0.0, {"event": "start"}),
            (startup_s, {"event": "phase", "name": "measure_start", "elapsed_ms": 0}),
            (
                startup_s + duration_s,
                {
                    "event": "done",
                    "measured_ms": duration_s * 1000,
                    "target_ms": duration_s * 1000,
                    "render_frames": 1000,
                    "fps_avg": 50.0,
                },
            ),
        ]
        if fatal:
            scheduled.insert(1, (1.0, {"event": "fatal", "message": "device lost"}))
        if not measure_event:
            scheduled = [
                (at, event)
                for at, event in scheduled
                if event.get("name") != "measure_start"
            ]

        class Process:
            stdout = None
            pid = 12345

            def __init__(self, *args, **kwargs):
                self.main = "pass_fds" in kwargs
                self.returncode = None
                launches.append((self.main, clock["now"]))

            def poll(self):
                if self.returncode is None and (
                    not self.main or clock["now"] >= startup_s + duration_s
                ):
                    self.returncode = 0
                return self.returncode

        def terminate(process):
            if process.poll() is None:
                process.returncode = -15

        def drain(_fd, *, buffer, on_line):
            while scheduled and scheduled[0][0] <= clock["now"]:
                _at, event = scheduled.pop(0)
                on_line(json.dumps(event))
            return buffer, False

        def sleep(seconds):
            clock["now"] += max(0.5, seconds)

        def sample(*args, **kwargs):
            if missing_render_telemetry and clock["now"] >= startup_s:
                return None
            busy = render_busy and clock["now"] >= startup_s
            return TelemetrySample(
                elapsed_s=0.0,
                gpu_util_pct=99.0 if busy else 0.0,
                power_w=180.0 if busy else 4.0,
                core_clock_mhz=None,
                temperature_c=None,
                voltage_mv=None,
                fan_speed_pct=None,
            )

        streaks = {}

        def check(state):
            if abort is not None:
                return abort(state)
            return telemetry_live_abort_reason(
                state,
                busy_power_floor_w=100.0,
                proper_run_power_floor_w=90.0,
                progress_state=streaks,
            )

        monkeypatch.setattr(
            q2rtx_runtime,
            "time",
            SimpleNamespace(
                monotonic=lambda: clock["now"],
                sleep=sleep,
            ),
        )
        monkeypatch.setattr(q2rtx_runtime.subprocess, "Popen", Process)
        monkeypatch.setattr(q2rtx_runtime, "_terminate_process_group", terminate)
        monkeypatch.setattr(q2rtx_runtime, "_drain_text_fd", drain)
        monkeypatch.setattr(q2rtx_runtime, "DaemonGpuClient", lambda _index: None)
        monkeypatch.setattr(
            q2rtx_runtime, "_query_selected_nvidia_gpu", lambda _index: None
        )
        monkeypatch.setattr(q2rtx_runtime, "query_gpu_metrics", sample)
        monkeypatch.setattr(
            q2rtx_runtime, "_prepare_q2rtx_subprocess_env", lambda env: (env, {}, None)
        )
        result = q2rtx_runtime._run_benchmark_process(
            config=Q2RTXStabilityConfig(
                duration_s=int(duration_s),
                log_dir=tmp_path,
                poll_interval_s=1.0,
                single_pass_timeout_s=timeout_s,
                progress_callback=states.append,
                abort_callback=check,
                companion_concurrent=concurrent,
                companion_command=("cuda-test",) if concurrent or companion else None,
            ),
            command=["q2rtx"],
            workdir=tmp_path,
            log_path=tmp_path / "events.log",
            section_name="test",
            workload_name="q2demo1",
            runtime_env={},
        )
        return result, states, launches

    return run


@pytest.mark.parametrize("startup_s", [0.0, 23.0, 97.0])
def test_compilation_is_excluded_without_a_time_based_grace(run_events, startup_s):
    result, states, _launches = run_events(startup_s=startup_s)

    assert result.exit_reason == "completed"
    assert result.benchmark_summary.measured_s == 20.0
    startup = [s for s in states if s["benchmark_phase"] == "startup"]
    assert all(s["progress_elapsed_s"] == 0.0 for s in startup)
    assert all(s["telemetry_samples"] == [] for s in startup)
    measured = [s for s in states if s["benchmark_phase"] == "measure"]
    assert measured[0]["progress_elapsed_s"] == 0.0
    assert measured[-1]["progress_elapsed_s"] == 19.0
    assert len(result.benchmark_telemetry_samples) == 20
    assert all(s.power_w == 180.0 for s in result.benchmark_telemetry_samples)
    assert len(result.telemetry_samples) == int(startup_s) + 20


def test_real_idle_rendering_still_aborts_after_measure_start(run_events):
    result, states, _launches = run_events(render_busy=False)

    assert result.exit_reason.startswith("q2rtx-selected-nvidia-gpu-idle")
    assert states[-1]["benchmark_phase"] == "measure"
    assert len(states[-1]["telemetry_samples"]) == 8


def test_missing_render_telemetry_does_not_reuse_startup_readings(run_events):
    result, states, _launches = run_events(missing_render_telemetry=True)
    assert result.benchmark_telemetry_samples == []
    startup = [s for s in states if s["benchmark_phase"] == "startup"]
    assert all(s["latest_sample"] is not None for s in startup)
    measured = [s for s in states if s["benchmark_phase"] == "measure"]
    assert measured
    assert all(s["latest_sample"] is None for s in measured)


def test_render_timeout_budget_is_not_consumed_by_startup(run_events):
    result, _states, _launches = run_events(startup_s=23.0, timeout_s=30.0)
    assert result.exit_reason == "completed"
    assert result.observed_duration_s == 43.0


def test_render_timeout_still_limits_a_started_benchmark(run_events):
    result, _states, _launches = run_events(startup_s=3.0, timeout_s=10.0)
    assert result.exit_reason == "benchmark-timeout"
    assert result.observed_duration_s == 13.0


def test_missing_measure_start_times_out_without_blacklisting(run_events):
    result, states, _launches = run_events(startup_s=97.0, timeout_s=30.0)
    assert result.exit_reason == "benchmark-startup-timeout"
    assert all(s["progress_elapsed_s"] == 0.0 for s in states)
    assert result.benchmark_telemetry_samples == []
    assert not probe_failure_should_mark_voltage_unsafe(result.exit_reason)
    assert (
        classify_failed_result(result.exit_reason, log_path=None).severity
        == FailureSeverity.CRITICAL
    )


def test_stop_still_works_during_compilation(run_events):
    result, _states, _launches = run_events(abort=lambda _state: "user-stop-requested")
    assert result.exit_reason == "user-stop-requested"
    assert result.observed_duration_s == 0.0


def test_fatal_event_still_aborts_during_compilation(run_events):
    result, _states, _launches = run_events(fatal=True)
    assert result.exit_reason == "benchmark-fatal-event"
    assert result.benchmark_telemetry_samples == []


def test_concurrent_cuda_waits_for_actual_rendering(run_events):
    result, _states, launches = run_events(concurrent=True)
    assert result.exit_reason == "completed"
    assert launches == [(True, 0.0), (False, 37.0)]


def test_missing_concurrent_load_does_not_silently_run_serially(run_events):
    result, _states, launches = run_events(concurrent=True, measure_event=False)
    assert result.exit_reason == "benchmark-companion-start-missing"
    assert launches == [(True, 0.0)]
    assert not probe_failure_should_mark_voltage_unsafe(result.exit_reason)


def test_serial_cuda_progress_keeps_startup_excluded(run_events):
    result, states, launches = run_events(companion=True)
    assert result.exit_reason == "completed"
    assert launches == [(True, 0.0), (False, 57.0)]
    assert states[-1]["running"] == "cuda"
    assert states[-1]["progress_elapsed_s"] == 20.0
    assert states[-1]["elapsed_s"] == 57.0


def test_verification_progress_displays_startup_without_counting_it():
    line = _format_live_progress_state(
        {
            "running": "q2rtx",
            "elapsed_s": 97.0,
            "progress_elapsed_s": 0.0,
            "benchmark_phase": "startup",
        },
        prefix="Stability live:",
    )
    assert "elapsed=0.0s" in line
    assert "phase=startup" in line


def test_voltage_verification_waits_for_event_even_when_other_gpu_work_is_busy():
    check = profile_verification_voltage_abort_callback({"lock_voltage_mv": 900})
    sample = TelemetrySample(
        elapsed_s=97.0,
        gpu_util_pct=99.0,
        voltage_mv=1100.0,
        power_w=None,
        core_clock_mhz=None,
        temperature_c=None,
        fan_speed_pct=None,
    )
    state = {
        "running": "q2rtx",
        "benchmark_phase": "startup",
        "elapsed_s": 97.0,
        "latest_sample": sample,
    }
    for _ in range(20):
        assert check(state) is None
    state["benchmark_phase"] = "measure"
    state["progress_elapsed_s"] = 0.0
    for _ in range(4):
        assert check(state) is None
    assert check(state).startswith("profile-verification-voltage-mismatch")
