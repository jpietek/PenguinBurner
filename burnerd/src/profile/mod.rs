//! Profile engine: the in-process runtime that applies a saved Auto-UV profile
//! (VF curve, power/memory/persistence, locked clock ceiling), runs the fan +
//! adaptive + guard + telemetry loop, and restores fans/clock-lock on exit.
//!
//! The supervisor passes one validated immutable [`RuntimeSpec`] to [`start`].

mod adaptive;
mod apply;
mod ceiling;
mod cpu;
mod fan;
mod guard;
mod latency_rx;
mod logfmt;
mod resume;
mod runtime_spec;
pub(crate) mod savings;
mod telemetry;

#[cfg(test)]
mod tests;

use std::collections::HashMap;
use std::fmt;
use std::io::Write;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::gpu::GpuBackend;
use crate::logging;

// Re-export the banker's-rounding helper so submodules can use `super::…`.
pub(crate) use crate::gpu::round_half_even;

use adaptive::AdaptiveAutoUvRuntimeController;
use apply::VfPolicyResult;
use ceiling::FlattenedClockCeilingController;
use cpu::ProcessCpuUsageSampler;
use fan::{FanConfig, RuntimeFanSettings};
use guard::{detect_vf_curve_reset, format_vf_curve_mismatch_preview};
use logfmt::{
    emergency_line, fan_event_line, format_log_line, single_line_text, status_line,
    status_signature, warn_line, StatusSignature,
};
use runtime_spec::{profile_tier_label, PlanItem, RuntimeMode};
use telemetry::{format_telemetry, telemetry_number, OverlayStatePublisher};

pub use runtime_spec::RuntimeSpec;

/// One semantic stock reset for scan/verification clients.  The mutation and
/// strict readback sequence stays shared with stock RuntimeSpec application.
pub(crate) fn reset_gpu_to_stock(backend: &dyn GpuBackend) -> Result<(), String> {
    let mut log = |message: &str| logging::info(message);
    apply::reset_gpu_to_stock(backend, &mut log)
}

/// The overlay "truthy" set — `{1, true, yes, on, active}` after strip+lowercase.
/// Shared by the overlay flag parser (`telemetry::flag_enabled`) and the latency
/// marker truthy check (`latency_rx::truthy_value`).
pub(crate) fn truthy_str(text: &str) -> bool {
    matches!(
        text.trim().to_lowercase().as_str(),
        "1" | "true" | "yes" | "on" | "active"
    )
}

/// Python floor division (`a // b`), used for the kHz→MHz conversions that must
/// round toward negative infinity for negative offsets.
pub(crate) fn floor_div(a: i64, b: i64) -> i64 {
    let q = a / b;
    let r = a % b;
    if r != 0 && (r < 0) != (b < 0) {
        q - 1
    } else {
        q
    }
}

/// The subset of the overlay latency snapshot the engine consumes. Wave A5 wires
/// the latency *receiver* (`latency_rx`, port of `overlay/telemetry/`), so this is
/// fed for real from in-game timing datagrams; the injected-snapshot seam is kept
/// so the engine loop tests still drive it as `None`.
#[derive(Debug, Clone, Default)]
pub struct LatencySnapshot {
    pub base_present_frametime_p95_ms: Option<f64>,
    /// Median of the same accepted marker set as the p95 above, so the two are
    /// comparable. Only present when a marker stream supplied the p95 -- the
    /// present-pacing fallback derives its p95 from a smoothed FPS estimate,
    /// not from a set this could be a median of.
    pub base_present_frametime_p50_ms: Option<f64>,
    /// Median of the presented frames themselves. Not comparable with the p95
    /// above, so it is kept apart and used only where a median is compared
    /// with another median -- which is most games, since most have no markers.
    pub present_pacing_p50_ms: Option<f64>,
    /// Share of the marker window that missed the deadline. Reported only off
    /// the same accepted set as the p95, for the same reason as the median.
    pub base_present_frametime_miss_ratio: Option<f64>,
    pub present_fps: Option<String>,
    pub fps_source: Option<String>,
    pub raw_present_fps_stats_avg: Option<String>,
    pub raw_present_fps_avg: Option<f64>,
    pub framegen_active: Option<String>,
    pub latency_p95_ms: Option<f64>,
    pub display_latency_p95_ms: Option<f64>,
    pub pid: Option<String>,
    pub session_id: Option<String>,
}

/// Outcome of `EngineHandle::stop`.
#[derive(Debug, PartialEq, Eq)]
pub enum StopOutcome {
    Stopped,
    /// The engine thread did not exit within the timeout (wedged).
    TimedOut,
}

/// A running engine. `returncode` mirrors the Python child's `poll()`: `None`
/// while running, `Some(0)` after a clean stop, `Some(1)` after an engine error.
pub struct EngineHandle {
    stop_flag: Arc<AtomicBool>,
    returncode: Arc<Mutex<Option<i32>>>,
    thread: Option<JoinHandle<()>>,
    /// Set once a `stop()` has timed out on this handle: subsequent `stop()`
    /// calls then poll the wedged thread once instead of re-paying the full
    /// timeout. The supervisor holds its mutex across `stop()`, so re-blocking
    /// for the full timeout on every retry would starve the systemd watchdog.
    timed_out: bool,
}

pub struct EngineStartError {
    message: String,
    engine: Option<EngineHandle>,
}

impl EngineStartError {
    fn stopped(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            engine: None,
        }
    }

    fn wedged(message: impl Into<String>, engine: EngineHandle) -> Self {
        Self {
            message: message.into(),
            engine: Some(engine),
        }
    }

    pub fn into_parts(self) -> (String, Option<EngineHandle>) {
        (self.message, self.engine)
    }
}

impl fmt::Display for EngineStartError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl fmt::Debug for EngineStartError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("EngineStartError")
            .field("message", &self.message)
            .field("engine_still_running", &self.engine.is_some())
            .finish()
    }
}

impl std::error::Error for EngineStartError {}

impl EngineHandle {
    pub fn returncode(&self) -> Option<i32> {
        *self
            .returncode
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
    }

    pub fn is_running(&self) -> bool {
        self.returncode().is_none()
    }

    /// Signal the engine to stop and join it, waiting at most `timeout`.
    pub fn stop(&mut self, timeout: Duration) -> StopOutcome {
        self.stop_flag.store(true, Ordering::SeqCst);
        let Some(thread) = self.thread.take() else {
            return StopOutcome::Stopped;
        };
        // A retry against an already-timed-out (still-wedged) engine polls once
        // rather than re-waiting the whole timeout under the supervisor mutex.
        let deadline = if self.timed_out {
            Instant::now()
        } else {
            Instant::now() + timeout
        };
        while !thread.is_finished() {
            if Instant::now() >= deadline {
                // Keep the handle so a later stop() re-checks the wedged thread
                // instead of spuriously reporting success. The supervisor refuses
                // to start new GPU work (scan/verification/profile) while the
                // engine has not provably stopped; the thread itself re-checks
                // the stop flag before its GPU-write section, so a late-returning
                // wedged call exits without further writes.
                self.thread = Some(thread);
                self.timed_out = true;
                return StopOutcome::TimedOut;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let _ = thread.join();
        self.timed_out = false;
        let mut returncode = self
            .returncode
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        if returncode.is_none() {
            *returncode = Some(0);
        }
        StopOutcome::Stopped
    }
}

/// Start the engine and wait until initial GPU apply + validation has completed.
/// A timed-out initializer is returned inside [`EngineStartError`] so the
/// supervisor can retain it and refuse competing GPU work until it really exits.
pub fn start(spec: RuntimeSpec) -> Result<EngineHandle, EngineStartError> {
    let stop_flag = Arc::new(AtomicBool::new(false));
    let returncode = Arc::new(Mutex::new(None));
    let (ready_tx, ready_rx) = mpsc::sync_channel(1);
    let thread_flag = stop_flag.clone();
    let thread_returncode = returncode.clone();
    let thread = std::thread::Builder::new()
        .name("penguin-burnerd-engine".to_string())
        .spawn(move || {
            // Catch a panic at the thread boundary so a bug in the loop can never
            // take the daemon down; cleanup still runs via the RAII guard during
            // unwinding (DESIGN §Threading model / panic policy).
            let flag = thread_flag.clone();
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                run_engine(spec, flag, ready_tx)
            }));
            let code = match result {
                Ok(code) => code,
                Err(_) => {
                    logging::error("runtime profile engine panicked");
                    1
                }
            };
            let mut stored = thread_returncode
                .lock()
                .unwrap_or_else(|poison| poison.into_inner());
            if stored.is_none() {
                *stored = Some(code);
            }
        })
        .map_err(|error| EngineStartError::stopped(error.to_string()))?;
    let mut handle = EngineHandle {
        stop_flag,
        returncode,
        thread: Some(thread),
        timed_out: false,
    };

    let ready = ready_rx.recv_timeout(Duration::from_secs(30));
    let failure = match ready {
        Ok(Ok(())) => return Ok(handle),
        Ok(Err(error)) => error,
        Err(mpsc::RecvTimeoutError::Timeout) => {
            "runtime profile initial apply timed out after 30 seconds".to_string()
        }
        Err(mpsc::RecvTimeoutError::Disconnected) => {
            "runtime profile engine exited before initial apply completed".to_string()
        }
    };
    match handle.stop(Duration::from_secs(10)) {
        StopOutcome::Stopped => Err(EngineStartError::stopped(failure)),
        StopOutcome::TimedOut => Err(EngineStartError::wedged(
            format!("{failure}; engine did not stop after initialization failure"),
            handle,
        )),
    }
}

/// Test-only engine whose thread ignores the stop flag for `wedge_for` (a
/// blocking NVML call that outlives the stop timeout), then exits cleanly.
/// Lets supervisor tests exercise the `StopOutcome::TimedOut` refusal paths.
#[cfg(test)]
pub(crate) fn wedged_engine_for_test(wedge_for: Duration) -> EngineHandle {
    let stop_flag = Arc::new(AtomicBool::new(false));
    let returncode: Arc<Mutex<Option<i32>>> = Arc::new(Mutex::new(None));
    let thread_returncode = returncode.clone();
    let thread = std::thread::Builder::new()
        .name("penguin-burnerd-engine-wedged-test".to_string())
        .spawn(move || {
            std::thread::sleep(wedge_for);
            let mut stored = thread_returncode
                .lock()
                .unwrap_or_else(|poison| poison.into_inner());
            if stored.is_none() {
                *stored = Some(0);
            }
        })
        .expect("spawn wedged test engine");
    EngineHandle {
        stop_flag,
        returncode,
        thread: Some(thread),
        timed_out: false,
    }
}

// --- engine assembly --------------------------------------------------------

/// Raw runtime-log line to stdout (systemd `StandardOutput=journal`), matching
/// the Python `runtime_debug.log` (`print(text, flush=True)`).
fn engine_log(msg: &str) {
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "{msg}");
    let _ = out.flush();
}

fn local_timestamp() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as libc::time_t;
    // SAFETY: a zeroed tm is valid; localtime_r fills it.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    // SAFETY: valid pointers for the call.
    unsafe {
        libc::localtime_r(&secs, &mut tm);
    }
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec
    )
}

fn now_unix_ns() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as i64)
        .unwrap_or(0)
}

/// Seconds from `CLOCK_MONOTONIC`, matching Python's `time.monotonic()` (an
/// absolute, always-increasing clock with an arbitrary large epoch). Used for
/// every loop `loop_started - last_*` delta so the `0.0`-init markers work.
fn monotonic_now() -> f64 {
    clock_seconds(libc::CLOCK_MONOTONIC)
}

/// Shared sampler for the monotonic/boottime pair the sleep detector
/// compares — both clocks must be read identically for the gap math to hold.
fn clock_seconds(clock: libc::clockid_t) -> f64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: clock_gettime writes into the owned timespec.
    unsafe {
        libc::clock_gettime(clock, &mut ts);
    }
    ts.tv_sec as f64 + ts.tv_nsec as f64 / 1_000_000_000.0
}

/// Test-only inert engine: idle until stop, no NVML/hardware writes. Gated solely
/// on its own env knob (`PENGUIN_BURNERD_TEST_INERT_ENGINE`) so it is independent
/// of the state-file path knob; the integration harnesses set it explicitly.
fn test_inert_engine() -> bool {
    std::env::var_os("PENGUIN_BURNERD_TEST_INERT_ENGINE").is_some()
}

fn run_inert(stop_flag: Arc<AtomicBool>) -> i32 {
    while !stop_flag.load(Ordering::SeqCst) {
        std::thread::sleep(Duration::from_millis(50));
    }
    0
}

fn run_engine(
    spec: RuntimeSpec,
    stop_flag: Arc<AtomicBool>,
    ready_tx: SyncSender<Result<(), String>>,
) -> i32 {
    #[cfg(test)]
    if std::env::var("PENGUIN_BURNERD_TEST_FAIL_PROFILE_ID")
        .is_ok_and(|profile_id| profile_id == spec.active_profile_id())
    {
        let message = "injected runtime profile initial apply failure".to_string();
        let _ = ready_tx.send(Err(message));
        return 1;
    }

    if test_inert_engine() {
        let _ = ready_tx.send(Ok(()));
        return run_inert(stop_flag);
    }

    let gpu_index = spec.gpu.index_at_resolution;

    let backend = match crate::gpu::NvmlBackend::open(gpu_index) {
        Ok(backend) => backend,
        Err(err) => {
            let message = format!("runtime profile engine init failed: {err}");
            let _ = ready_tx.send(Err(message.clone()));
            logging::error(&message);
            return 1;
        }
    };

    let live_uuid = backend.identity().uuid;
    if live_uuid != spec.gpu.uuid {
        let message = format!(
            "runtime profile GPU mismatch: expected={} index={} observed={}",
            spec.gpu.uuid, gpu_index, live_uuid
        );
        let _ = ready_tx.send(Err(message.clone()));
        logging::error(&message);
        return 1;
    }

    let mut ready = Some(ready_tx);
    match run_with_backend(&backend, &spec, stop_flag, None, &mut ready) {
        Ok(()) => 0,
        Err(err) => {
            if let Some(sender) = ready.take() {
                let _ = sender.send(Err(err.clone()));
            }
            logging::error(&format!("runtime profile engine error: {err}"));
            1
        }
    }
}

/// The assembly + loop, generic over the backend (driven by `MockGpu` in tests).
fn run_with_backend(
    backend: &dyn GpuBackend,
    spec: &RuntimeSpec,
    stop_flag: Arc<AtomicBool>,
    max_iterations: Option<u64>,
    ready: &mut Option<SyncSender<Result<(), String>>>,
) -> Result<(), String> {
    let gpu_index = spec.gpu.index_at_resolution;
    let mut log = |m: &str| engine_log(m);

    let fan_config = spec.fan.config.clone();
    let fan_control_enabled = spec.fan.enabled;
    if !spec.fan.notice.is_empty() {
        log(&spec.fan.notice);
    }

    let initial_curve = match spec.mode {
        RuntimeMode::Stock => None,
        RuntimeMode::Static => spec.static_profile.as_ref(),
        RuntimeMode::Adaptive => spec
            .adaptive
            .as_ref()
            .and_then(|adaptive| adaptive.profiles.get(&adaptive.initial_tier)),
    };

    // VF-curve policy: persistence → memory → VF → power → clock ceiling
    // (mem before VF: a mem-offset write wipes the per-point VF table).
    // Persistence mode is a documented RTD3 blocker, so it is suppressed
    // in Mobile or Unknown deep-sleep mode; the profile is
    // reapplied on wake instead of relying on the driver staying initialized.
    let enable_persistence_mode =
        spec.policy.enable_persistence_mode && !crate::rtd3::suppress_persistence();
    if spec.policy.enable_persistence_mode && !enable_persistence_mode {
        log("Deep sleep: skipping GPU persistence mode (it blocks runtime D3)");
    }
    let vf_policy = apply::configure_runtime_spec_policy(
        backend,
        enable_persistence_mode,
        spec.mode,
        initial_curve,
        &mut log,
    )?;

    let mut publisher = OverlayStatePublisher::new(
        i64::from(gpu_index),
        spec.overlay.enabled,
        spec.overlay.update_interval_s as f64,
        vf_policy.active_profile_tier.clone(),
        vf_policy.active_profile_tier_key.clone(),
        vf_policy.active_profile_id.clone(),
        spec.mode == RuntimeMode::Adaptive,
        Some(ProcessCpuUsageSampler::new()),
    );

    // Adaptive controller (only for an Auto-UV profile source).
    let mut adaptive_ctrl: Option<AdaptiveAutoUvRuntimeController> = None;
    if let Some(adaptive) = spec.adaptive.as_ref() {
        if vf_policy.active_vf_curve_source.as_deref() == Some("auto-uv-final") {
            let tier_curves: HashMap<_, _> = adaptive
                .profiles
                .iter()
                .map(|(tier, curve)| (tier.clone(), curve.clone()))
                .collect();
            adaptive_ctrl = Some(AdaptiveAutoUvRuntimeController::new(
                &adaptive.initial_tier,
                backend.vf_curve_available(),
                tier_curves,
                &adaptive.policy,
                &mut log,
            ));
        } else {
            log("Adaptive Auto-UV disabled: active runtime curve is not an Auto-UV profile.");
        }
    }

    // Latency telemetry receiver (in-game FPS/latency datagrams). Created
    // unconditionally like the Python runtime; a bind failure logs and yields no
    // meter (adaptive then holds tier / overlay omits fps). Dropped on every exit
    // path — including error return and panic unwind — closing the sockets.
    let latency_receiver = latency_rx::LatencyReceiver::start(&mut log);

    // Energy-saved accounting (issue #23): None when the applied profiles
    // carry no scan power metrics. Drop flushes the totals on every exit path.
    let mut savings_tracker =
        savings::SavingsTracker::from_spec(spec, savings::savings_state_path());

    run_fan_control_loop(
        backend,
        gpu_index,
        enable_persistence_mode,
        fan_config,
        fan_control_enabled,
        vf_policy,
        &mut publisher,
        adaptive_ctrl.as_mut(),
        latency_receiver.as_ref(),
        savings_tracker.as_mut(),
        stop_flag,
        max_iterations,
        None,
        ready,
    )
}

/// Restores fans (to hardware auto) + releases the clock lock on EVERY exit path
/// (clean stop, error return, panic unwind). Deliberately leaves the UV curve,
/// power limit, memory offset, and persistence mode applied (parity).
struct EngineCleanup<'a> {
    backend: &'a dyn GpuBackend,
    fan_control_enabled: bool,
    fan_count: u32,
    ceiling: Option<FlattenedClockCeilingController<'a>>,
    done: bool,
}

impl EngineCleanup<'_> {
    fn restore(&mut self) {
        if self.done {
            return;
        }
        self.done = true;
        if self.fan_control_enabled {
            if let Err(exc) = self.backend.set_all_fans_default(self.fan_count) {
                engine_log(&format!(
                    "Warning: failed to restore default fan speed: {exc}"
                ));
            }
        }
        if let Some(ceiling) = self.ceiling.as_mut() {
            let _ = ceiling.close();
        }
    }
}

impl Drop for EngineCleanup<'_> {
    fn drop(&mut self) {
        self.restore();
    }
}

#[allow(clippy::too_many_arguments)]
fn run_fan_control_loop(
    backend: &dyn GpuBackend,
    gpu_index: u32,
    enable_persistence_mode: bool,
    fan_config: FanConfig,
    fan_control_enabled: bool,
    vf_policy: VfPolicyResult<'_>,
    publisher: &mut OverlayStatePublisher,
    mut adaptive_ctrl: Option<&mut AdaptiveAutoUvRuntimeController>,
    latency_receiver: Option<&latency_rx::LatencyReceiver>,
    mut savings_tracker: Option<&mut savings::SavingsTracker>,
    stop_flag: Arc<AtomicBool>,
    max_iterations: Option<u64>,
    // Test seam (like `max_iterations`): overrides the (monotonic, boottime)
    // pair the resume machinery samples, so loop tests can fabricate sleeps.
    mut resume_clocks: Option<&mut dyn FnMut() -> (f64, f64)>,
    ready: &mut Option<SyncSender<Result<(), String>>>,
) -> Result<(), String> {
    let mut settings = RuntimeFanSettings::build(&fan_config, fan_control_enabled)?;
    let poll_interval_s = settings.poll_interval_s;
    let vf_reapply_cooldown_s = poll_interval_s.max(10.0);

    // Laptop dGPUs commonly return NOT_SUPPORTED for the fan count (the EC
    // owns the fans). That must not block profiles that never touch fans.
    let fan_count = match backend.fan_count() {
        Ok(count) => count,
        Err(exc) if !fan_control_enabled => {
            engine_log(&format!(
                "fan control unavailable (continuing without it): {exc}"
            ));
            0
        }
        Err(exc) => return Err(exc.to_string()),
    };
    if fan_control_enabled && fan_count == 0 {
        return Err("GPU reports zero controllable fans".to_string());
    }
    if fan_control_enabled {
        let (device_min, device_max) = backend.fan_speed_limits();
        settings.apply_device_fan_limits(device_min, device_max)?;
        settings.effective_manual_curve = fan::build_effective_manual_curve(
            &settings.curve,
            settings.manual_enable_temp_c,
            settings.effective_min_fan_speed_pct as f64,
            settings.effective_max_fan_speed_pct as f64,
            &settings.mode,
        );
    }

    // Pull the guard/ceiling state out of the policy before moving the ceiling
    // into the cleanup guard.
    let mut vf_apply_plan: Option<Vec<PlanItem>> = vf_policy.vf_apply_plan.clone();
    let mut vf_expected_samples: Vec<PlanItem> = vf_policy.vf_expected_samples.clone();
    let mut reapply_memory_offset_mhz: Option<i64> =
        vf_policy.auto_uv_profile_gpu_policy.mem_clk_vf_offset_mhz;
    // Tracks the power limit applied for the CURRENT tier: startup value
    // here, updated on every adaptive tier switch below, consumed by the
    // post-resume re-verification.
    let mut expected_power_limit_w = vf_policy.auto_uv_profile_gpu_policy.power_limit_w;
    let profile_clock_mhz = vf_policy.profile_clock_mhz;
    let profile_voltage_mv = vf_policy.profile_voltage_mv;
    let mut current_tier_label = vf_policy_tier_label(&vf_policy);
    let mut current_tier_key = vf_policy.active_profile_tier_key.clone();
    let gpu_policy_text = vf_policy.auto_uv_profile_gpu_policy.describe();

    let mut cleanup = EngineCleanup {
        backend,
        fan_control_enabled,
        fan_count,
        ceiling: vf_policy.clock_ceiling_controller,
        done: false,
    };

    log_startup(
        gpu_index,
        enable_persistence_mode,
        &gpu_policy_text,
        fan_control_enabled,
        fan_count,
        &settings,
        &current_tier_label,
    );
    if let Some(sender) = ready.take() {
        let _ = sender.send(Ok(()));
    }

    // Loop state (runtime_loop.py §5.4). `loop_started` uses an absolute
    // CLOCK_MONOTONIC clock like Python's `time.monotonic()`, so the `0.0`-init
    // "distant past" markers (VF-reapply, adaptive last-switch dwell) behave
    // identically — the first detected drift reapplies immediately, and the
    // startup tier is demote-eligible without a fabricated dwell wait.
    let mut last_speed: Option<f64> = None;
    let mut last_set_temp_c: Option<f64> = None;
    let mut last_update_time = monotonic_now();
    let mut manual_mode_active = false;
    let mut hot_auto_mode_active = false;
    let mut iteration_count: u64 = 0;
    let mut last_status_signature: Option<StatusSignature> = None;
    let mut last_overlay_publish: Option<f64> = None;
    let mut overlay_publish_failed = false;
    let mut last_vf_reapply = 0.0_f64;
    let (setup_monotonic, setup_boottime) = match resume_clocks.as_mut() {
        Some(clocks) => clocks(),
        None => (monotonic_now(), resume::boottime_now()),
    };
    let mut sleep_detector = resume::SleepGapDetector::new(
        resume::SLEEP_GAP_THRESHOLD_S,
        setup_monotonic,
        setup_boottime,
    );
    let mut resume_deadline: Option<f64> = None;
    let mut resume_attempts = 0u32;

    loop {
        if let Some(max) = max_iterations {
            if iteration_count >= max {
                return Ok(());
            }
        }
        if stop_flag.load(Ordering::SeqCst) {
            return Ok(());
        }
        iteration_count += 1;
        let loop_started = monotonic_now();

        // System resume detection: CLOCK_BOOTTIME keeps counting through a
        // sleep while the loop's monotonic clock does not, so a divergence
        // between the two IS a completed suspend cycle. Applied GPU state may
        // have silently reset; re-verify after a short driver-settle grace.
        let (tick_monotonic, tick_boottime) = match resume_clocks.as_mut() {
            Some(clocks) => clocks(),
            None => (loop_started, resume::boottime_now()),
        };
        if let Some(slept_s) = sleep_detector.observe(tick_monotonic, tick_boottime) {
            engine_log(&format!(
                "{} event=system-resume-detected slept_s={slept_s:.0} action=reverify-in-{}s",
                local_timestamp(),
                resume::RESUME_REAPPLY_GRACE_S,
            ));
            resume_deadline = Some(tick_monotonic + resume::RESUME_REAPPLY_GRACE_S);
            resume_attempts = 0;
            // Pre-suspend utilization samples still look "recent" on the
            // monotonic clock; drop them so the CPU-bound guard reasons only
            // about post-resume load.
            if let Some(controller) = adaptive_ctrl.as_deref_mut() {
                controller.note_system_resume();
            }
        }

        // Driver-settle window: while a re-verification is pending, this tick
        // makes NO backend calls at all. Right after wake, telemetry reads,
        // adaptive writes, and the VF guard would all hit the half-initialized
        // driver — a transient error on any of them is fatal to the loop,
        // which is exactly what the grace exists to prevent.
        if let Some(deadline) = resume_deadline {
            if tick_monotonic < deadline {
                // Cap the wait at the remaining grace so a long poll interval
                // does not stretch the no-backend-calls blackout past it.
                sleep_loop(
                    &stop_flag,
                    poll_interval_s.min((deadline - tick_monotonic).max(0.05)),
                    overlay_update_interval_s(publisher),
                    publisher.enabled,
                );
                continue;
            }
            match run_resume_recovery(
                backend,
                enable_persistence_mode,
                expected_power_limit_w,
                cleanup.ceiling.as_mut(),
            ) {
                Ok(power_reapplied) => {
                    engine_log(&format!(
                        "{} event=resume-reverify-complete power_limit_reapplied={power_reapplied}",
                        local_timestamp()
                    ));
                    last_vf_reapply = 0.0;
                    last_speed = None;
                    last_set_temp_c = None;
                    resume_deadline = None;
                }
                Err(exc) => {
                    resume_attempts += 1;
                    if resume_attempts >= resume::RESUME_REAPPLY_MAX_ATTEMPTS {
                        // Giving up is deliberately non-fatal: erroring out
                        // would strip fan control and the clock ceiling while
                        // leaving the undervolt applied. The per-tick guards
                        // keep re-verifying the curve from here on.
                        engine_log(&format!(
                            "{} event=resume-reverify-gave-up attempts={resume_attempts} error={exc}",
                            local_timestamp()
                        ));
                        resume_deadline = None;
                        // Same re-assertion the success path forces: the fan
                        // dedup and VF cooldown must not trust pre-suspend
                        // state just because the recovery writes failed.
                        last_vf_reapply = 0.0;
                        last_speed = None;
                        last_set_temp_c = None;
                    } else {
                        engine_log(&format!(
                            "{} event=resume-reverify-retry attempt={resume_attempts} error={exc}",
                            local_timestamp()
                        ));
                        // Re-anchor AFTER the failed attempt (which may have
                        // blocked past the old anchor) so every retry gets a
                        // full settle grace; the grace branch above owns the
                        // sleep on the next tick.
                        let after_attempt = match resume_clocks.as_mut() {
                            Some(clocks) => clocks().0,
                            None => monotonic_now(),
                        };
                        resume_deadline = Some(after_attempt + resume::RESUME_REAPPLY_GRACE_S);
                        continue;
                    }
                }
            }
        }

        let overlay_interval_s = overlay_update_interval_s(publisher);

        // Aggregate the current in-game telemetry window (None when no receiver /
        // no fresh samples). Owned here; adaptive + overlay borrow it this tick.
        // The deadline the frametime window counts misses against comes from
        // the adaptive target, so the ratio means "share of the window that
        // blew its budget" rather than an arbitrary bar.
        let miss_deadline_us = adaptive_ctrl.as_deref().map(|c| c.miss_deadline_us());
        let latency_owned =
            latency_receiver.and_then(|rx| rx.snapshot(loop_started, miss_deadline_us));
        let latency_snapshot: Option<&LatencySnapshot> = latency_owned.as_ref();

        // Adaptive update — BEFORE the fan decision, same iteration. A tier
        // switch issues GPU writes (VF/mem/power/ceiling), so honor a stop that
        // landed since the loop-top check before entering it.
        if stop_flag.load(Ordering::SeqCst) {
            return Ok(());
        }
        if let Some(controller) = adaptive_ctrl.as_deref_mut() {
            if let Some(update) = controller.update(
                latency_snapshot,
                loop_started,
                backend,
                &mut cleanup.ceiling,
                publisher,
                &mut |m| engine_log(m),
            ) {
                current_tier_label = profile_tier_label(&update.tier);
                current_tier_key = update.tier.clone();
                if update.changed {
                    if let Some(plan) = update.vf_apply_plan {
                        vf_apply_plan = Some(plan);
                    }
                    vf_expected_samples = update.vf_expected_samples;
                    reapply_memory_offset_mhz = update.memory_offset_mhz;
                    // A tier that carries no limit leaves the previously
                    // applied limit in force on the hardware, so keep
                    // tracking that value for the post-resume re-verify.
                    if update.applied_power_limit_w.is_some() {
                        expected_power_limit_w = update.applied_power_limit_w;
                    }
                }
            }
        }

        let current_temp_c = match backend.temperature_c() {
            Ok(temp) => temp,
            Err(exc) => {
                // A suspend can land mid-tick inside a blocking call and
                // surface as a transient post-wake error before the loop-top
                // detector saw the gap. A fresh gap means "enter the settle
                // window", not "die".
                if let Some((slept_s, now)) =
                    probe_sleep_gap(&mut sleep_detector, &mut resume_clocks)
                {
                    engine_log(&format!(
                        "{} event=system-resume-detected slept_s={slept_s:.0} trigger=telemetry-error error={exc}",
                        local_timestamp()
                    ));
                    resume_deadline = Some(now + resume::RESUME_REAPPLY_GRACE_S);
                    resume_attempts = 0;
                    if let Some(controller) = adaptive_ctrl.as_deref_mut() {
                        controller.note_system_resume();
                    }
                    continue;
                }
                return Err(exc.to_string());
            }
        };
        let power_draw_w = backend.power_draw_w();

        let ceiling_text = cleanup
            .ceiling
            .as_ref()
            .map(|c| c.telemetry_text())
            .unwrap_or_default();
        let telemetry_text = format_telemetry(
            backend,
            fan_count,
            current_temp_c,
            power_draw_w,
            &ceiling_text,
        );
        let telemetry_clock = telemetry_number(&telemetry_text, "gpu_clock", "MHz");
        let telemetry_voltage = telemetry_number(&telemetry_text, "voltage", "mV");

        // Energy-saved accounting: credit this tick when the GPU is under a
        // real workload while the profile (current adaptive tier) is applied.
        if let Some(tracker) = savings_tracker.as_deref_mut() {
            tracker.record(
                loop_started,
                backend.gpu_utilization_pct(),
                telemetry_clock,
                &current_tier_key,
            );
        }
        let status_core_clock = profile_clock_mhz.filter(|&c| c != 0.0).or(telemetry_clock);
        let status_voltage = profile_voltage_mv
            .filter(|&v| v != 0.0)
            .or(telemetry_voltage);

        engine_log(&format_log_line(
            "telemetry",
            &[
                Some(local_timestamp()),
                Some(single_line_text(&telemetry_text)),
            ],
        ));

        // A stop request may have arrived while a backend call above was blocked
        // (a wedged NVML call can outlive `EngineHandle::stop`'s timeout).
        // Re-check before the GPU-write section so a late-returning iteration
        // cannot write fans/mem/VF state over a successor that now owns the GPU.
        if stop_flag.load(Ordering::SeqCst) {
            return Ok(());
        }

        // Runtime-state publish (throttled). On failure, log once (the Python
        // `overlay_publish_failed` latch) and do NOT advance the publish marker
        // (Python retried every tick, silently).
        //
        // Published regardless of `publisher.enabled`: this file is the
        // daemon's live-state channel, not the overlay's on switch. The
        // in-game HUD is gated separately inside the Vulkan layer, which
        // checks its own env/config, so writing here never shows an overlay
        // the user turned off. Consumers that need live state without the HUD
        // -- the GUI's running-profile line, which otherwise shows the
        // adaptive run's *initial* tier forever -- depend on it being written.
        // `publisher.enabled` still drives the loop cadence, so with the HUD
        // off this lands at the plain poll interval instead of the faster
        // overlay one.
        let publish_due =
            last_overlay_publish.is_none_or(|last| loop_started - last >= overlay_interval_s);
        if publish_due {
            match publisher.publish(backend, latency_snapshot, now_unix_ns()) {
                Ok(()) => last_overlay_publish = Some(loop_started),
                Err(exc) => {
                    if !overlay_publish_failed {
                        engine_log(&warn_line("overlay publish unavailable", &exc.to_string()));
                    }
                    overlay_publish_failed = true;
                }
            }
        }

        // VF-curve reapply guard.
        if let (Some(plan), false) = (vf_apply_plan.as_ref(), vf_expected_samples.is_empty()) {
            last_vf_reapply = maybe_reapply_vf_curve(
                backend,
                &vf_expected_samples,
                plan,
                &telemetry_text,
                loop_started,
                last_vf_reapply,
                vf_reapply_cooldown_s,
                reapply_memory_offset_mhz,
                &stop_flag,
            );
        }

        // Fan decision (runtime_loop.py §5.7).
        let emit = |fan_pct: Option<f64>, fan_mode: &str, sig: &mut Option<StatusSignature>| {
            let tier = if current_tier_label.is_empty() {
                None
            } else {
                Some(current_tier_label.as_str())
            };
            let new_sig = status_signature(
                Some(current_temp_c),
                power_draw_w,
                status_core_clock,
                status_voltage,
                fan_pct,
                fan_mode,
                tier,
            );
            if sig.as_ref() != Some(&new_sig) {
                *sig = Some(new_sig);
                engine_log(&status_line(
                    Some(current_temp_c),
                    power_draw_w,
                    status_core_clock,
                    status_voltage,
                    fan_pct,
                    fan_mode,
                    tier,
                ));
            }
        };

        if !fan_control_enabled {
            emit(None, "disabled", &mut last_status_signature);
            sleep_loop(
                &stop_flag,
                poll_interval_s,
                overlay_interval_s,
                publisher.enabled,
            );
            continue;
        }

        if hot_auto_mode_active && current_temp_c > settings.emergency_auto_resume_temp_c {
            emit(None, "auto", &mut last_status_signature);
            sleep_loop(
                &stop_flag,
                poll_interval_s,
                overlay_interval_s,
                publisher.enabled,
            );
            continue;
        }
        if hot_auto_mode_active && current_temp_c <= settings.emergency_auto_resume_temp_c {
            hot_auto_mode_active = false;
            engine_log(&emergency_line("cleared", Some(current_temp_c), "auto"));
        }
        if current_temp_c > settings.emergency_auto_override_temp_c {
            if manual_mode_active {
                let _ = backend.set_all_fans_default(fan_count);
                manual_mode_active = false;
                last_speed = None;
                last_set_temp_c = None;
            }
            hot_auto_mode_active = true;
            engine_log(&emergency_line("override", Some(current_temp_c), "auto"));
            sleep_loop(
                &stop_flag,
                poll_interval_s,
                overlay_interval_s,
                publisher.enabled,
            );
            continue;
        }
        if !manual_mode_active && current_temp_c < settings.manual_enable_temp_c {
            emit(None, "auto", &mut last_status_signature);
            sleep_loop(
                &stop_flag,
                poll_interval_s,
                overlay_interval_s,
                publisher.enabled,
            );
            continue;
        }
        if !manual_mode_active {
            manual_mode_active = true;
            last_speed = None;
            last_set_temp_c = None;
            last_update_time = loop_started;
            engine_log(&fan_event_line("→ manual", Some(current_temp_c)));
        }
        if current_temp_c <= settings.auto_restore_temp_c {
            let _ = backend.set_all_fans_default(fan_count);
            manual_mode_active = false;
            last_speed = None;
            last_set_temp_c = None;
            engine_log(&fan_event_line("→ auto", Some(current_temp_c)));
            sleep_loop(
                &stop_flag,
                poll_interval_s,
                overlay_interval_s,
                publisher.enabled,
            );
            continue;
        }

        let raw_target = fan::clamp(
            fan::speed_for_temp(current_temp_c, &settings.curve, &settings.mode),
            settings.effective_min_fan_speed_pct as f64,
            settings.effective_max_fan_speed_pct as f64,
        );
        let hysteresis_target = fan::apply_hysteresis(
            current_temp_c,
            raw_target,
            last_set_temp_c,
            last_speed,
            settings.hysteresis_c,
        );
        let limited = fan::limit_speed_change(
            hysteresis_target,
            last_speed,
            loop_started - last_update_time,
            settings.max_step_up_pct_per_s,
            settings.max_step_down_pct_per_s,
        );
        let target_speed = round_half_even(fan::clamp(
            limited,
            settings.effective_min_fan_speed_pct as f64,
            settings.effective_max_fan_speed_pct as f64,
        ));

        if settings.force_update_every_poll || Some(target_speed) != last_speed {
            if let Err(exc) = backend.set_all_fans_speed(fan_count, target_speed as u32) {
                // Same mid-tick-suspend tolerance as the telemetry read.
                if let Some((slept_s, now)) =
                    probe_sleep_gap(&mut sleep_detector, &mut resume_clocks)
                {
                    engine_log(&format!(
                        "{} event=system-resume-detected slept_s={slept_s:.0} trigger=fan-write-error error={exc}",
                        local_timestamp()
                    ));
                    resume_deadline = Some(now + resume::RESUME_REAPPLY_GRACE_S);
                    resume_attempts = 0;
                    if let Some(controller) = adaptive_ctrl.as_deref_mut() {
                        controller.note_system_resume();
                    }
                    continue;
                }
                return Err(exc.to_string());
            }
            last_set_temp_c = Some(current_temp_c);
            last_speed = Some(target_speed);
            last_update_time = loop_started;
        }
        emit(Some(target_speed), "manual", &mut last_status_signature);
        sleep_loop(
            &stop_flag,
            poll_interval_s,
            overlay_interval_s,
            publisher.enabled,
        );
    }
}

fn vf_policy_tier_label(vf_policy: &VfPolicyResult<'_>) -> String {
    let label = vf_policy.active_profile_tier.trim();
    if !label.is_empty() {
        return label.to_string();
    }
    let key = vf_policy.active_profile_tier_key.trim();
    if key.is_empty() {
        String::new()
    } else {
        profile_tier_label(key)
    }
}

fn overlay_update_interval_s(publisher: &OverlayStatePublisher) -> f64 {
    publisher.update_interval_s.clamp(1.0, 10.0)
}

fn sleep_loop(
    stop_flag: &AtomicBool,
    poll_interval_s: f64,
    overlay_interval_s: f64,
    overlay_enabled: bool,
) {
    let interval = if overlay_enabled {
        poll_interval_s.min(overlay_interval_s)
    } else {
        poll_interval_s
    };
    let total = Duration::from_secs_f64(interval.max(0.0));
    let deadline = Instant::now() + total;
    // Sleep in short chunks so a stop request is observed promptly.
    while Instant::now() < deadline {
        if stop_flag.load(Ordering::SeqCst) {
            return;
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        std::thread::sleep(remaining.min(Duration::from_millis(100)));
    }
}

#[allow(clippy::too_many_arguments)]
fn maybe_reapply_vf_curve(
    backend: &dyn GpuBackend,
    vf_expected_samples: &[PlanItem],
    vf_apply_plan: &[PlanItem],
    telemetry_text: &str,
    loop_started: f64,
    last_vf_reapply: f64,
    cooldown_s: f64,
    memory_offset_mhz: Option<i64>,
    stop_flag: &AtomicBool,
) -> f64 {
    let mismatches =
        detect_vf_curve_reset(&backend.editable_core_vf_points(), vf_expected_samples, 1);
    if mismatches.is_empty() {
        return last_vf_reapply;
    }
    if (loop_started - last_vf_reapply) < cooldown_s {
        return last_vf_reapply;
    }
    // The `editable_core_vf_points()` read above is a blocking NVML call; a stop
    // (e.g. a scan handoff) can land while it is wedged. Bail before the mem/VF
    // writes so this iteration cannot clobber the state the successor now owns —
    // these are precisely the writes F2 warns about (mem write wipes the VF
    // table; the VF re-apply rewrites the whole plan).
    if stop_flag.load(Ordering::SeqCst) {
        return last_vf_reapply;
    }
    let ts = local_timestamp();

    // Memory-offset guard, BEFORE the VF re-apply. A coarse mem-offset write
    // WIPES the entire core per-point VF table — on both NVML API families
    // (`nvmlDeviceSetClockOffsets` and the deprecated fallback; each proven
    // 2026-07-07 on driver 610.43.02) — so an UNCONDITIONAL rewrite here would
    // erase the very curve we are about to re-apply — the VF guard and mem
    // guard then fight every cadence (~10s). Read the live offset back first
    // and only rewrite when it has drifted from the target; when we do rewrite,
    // the VF re-apply below MUST follow it (ordered dependency) so the curve
    // survives the wipe.
    if let Some(offset) = memory_offset_mhz.filter(|&m| m > 0) {
        if backend.clock_offsets().mem_clk_vf_offset_mhz != Some(offset as i32) {
            match backend.apply_clock_offsets(None, Some(offset as i32)) {
                Ok(applied) => engine_log(&format!(
                    "{ts} event=mem-offset-reapplied requested={offset} readback={}",
                    applied
                        .mem_clk_vf_offset_readback_mhz
                        .map(|v| v.to_string())
                        .unwrap_or_else(|| "None".to_string())
                )),
                Err(exc) => engine_log(&format!("{ts} event=mem-offset-reapply-error error={exc}")),
            }
        }
    }

    // Mem handled by the conditional guard above → pass None; the helper does the
    // VF plan + readback (mem-before-VF invariant lives on it).
    if let Err(exc) =
        apply::apply_memory_offset_then_vf_plan(backend, "vf-curve reapply", None, vf_apply_plan)
    {
        engine_log(&format!("{ts} event=vf-curve-reapply-error error={exc}"));
        return last_vf_reapply;
    }
    let preview = format_vf_curve_mismatch_preview(&mismatches, 4);
    engine_log(&format_log_line(
        "vf",
        &[
            Some(ts),
            Some(single_line_text(telemetry_text)),
            Some("event=vf-curve-reapplied".to_string()),
            Some(format!("mismatches={}", mismatches.len())),
            Some(format!("samples={}", single_line_text(&preview))),
        ],
    ));
    loop_started
}

/// One post-resume recovery pass: persistence mode (a resume can drop it;
/// only applied at startup otherwise), then the power limit (read-first) and
/// the locked-clock ceiling re-lock — attempted independently so one failing
/// step cannot starve the other across the bounded retry budget. The VF/mem
/// reapply stays with the loop's existing guard (whose cooldown the caller
/// clears) so the mem-before-VF ordering invariant keeps exactly one owner.
fn run_resume_recovery(
    backend: &dyn GpuBackend,
    enable_persistence_mode: bool,
    expected_power_limit_w: Option<i64>,
    ceiling: Option<&mut FlattenedClockCeilingController<'_>>,
) -> Result<bool, String> {
    let mut log = |message: &str| engine_log(message);
    apply::apply_gpu_base_policy(backend, enable_persistence_mode, &mut log);
    let mut failures: Vec<String> = Vec::new();
    let power_reapplied =
        match resume::verify_power_limit(backend, expected_power_limit_w, &mut log) {
            Ok(reapplied) => reapplied,
            Err(exc) => {
                failures.push(exc);
                false
            }
        };
    if let Some(controller) = ceiling {
        if let Err(exc) = controller.apply() {
            failures.push(format!("clock ceiling re-lock failed: {exc}"));
        }
    }
    if failures.is_empty() {
        Ok(power_reapplied)
    } else {
        Err(failures.join("; "))
    }
}

/// Sample the clock pair (seam-aware) and ask the detector whether a suspend
/// just ended; returns the slept seconds and the sampled monotonic time.
fn probe_sleep_gap(
    detector: &mut resume::SleepGapDetector,
    clocks: &mut Option<&mut dyn FnMut() -> (f64, f64)>,
) -> Option<(f64, f64)> {
    let (monotonic, boottime) = match clocks.as_mut() {
        Some(clocks) => clocks(),
        None => (monotonic_now(), resume::boottime_now()),
    };
    detector
        .observe(monotonic, boottime)
        .map(|gap| (gap, monotonic))
}

#[allow(clippy::too_many_arguments)]
fn log_startup(
    gpu_index: u32,
    enable_persistence_mode: bool,
    gpu_policy_text: &str,
    fan_control_enabled: bool,
    fan_count: u32,
    settings: &RuntimeFanSettings,
    tier_label: &str,
) {
    if fan_control_enabled {
        engine_log(&format!(
            "Controlling GPU {gpu_index} with {fan_count} fan(s), mode={}, hysteresis={} C, manual-limits={}-{}%, manual-enable={} C, auto-restore={} C, emergency-auto={} C/{} C. Press Ctrl-C to restore auto mode.",
            settings.mode,
            settings.hysteresis_c,
            settings.effective_min_fan_speed_pct,
            settings.effective_max_fan_speed_pct,
            settings.manual_enable_temp_c,
            settings.auto_restore_temp_c,
            settings.emergency_auto_override_temp_c,
            settings.emergency_auto_resume_temp_c,
        ));
    } else {
        engine_log(&format!(
            "Running GPU {gpu_index} telemetry and V/F policy loop with fan control disabled. Use --silent-fan-curve to let PenguinBurner control fans. Press Ctrl-C to exit."
        ));
    }
    engine_log(&format!(
        "GPU policy: persistence={}, {gpu_policy_text}.",
        if enable_persistence_mode { "on" } else { "off" }
    ));
    if !tier_label.is_empty() {
        engine_log(&format!("Active profile tier: {tier_label}."));
    }
    if fan_control_enabled {
        engine_log(&format!(
            "Fan curve points: {}",
            fan::format_curve_points(&settings.curve)
        ));
    }
}
