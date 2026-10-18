//! Overlay/GUI telemetry — byte-compatible producer of `overlay-state.txt`
//! (spec 05) plus the per-poll telemetry text (`live_gpu_telemetry_text.py`).
//!
//! The state file's format, escaping, atomicity, ownership, cadence, and
//! stickiness are reproduced exactly; the C++ Vulkan layer and the Python GUI
//! parse it in-process.

use std::io::Write;
use std::path::{Path, PathBuf};

use tempfile::NamedTempFile;

use crate::gpu::{ClockType, GpuBackend, VfPoint};
use crate::paths;

use super::cpu::ProcessCpuUsageSampler;
use super::floor_div;
use super::runtime_spec::profile_tier_label;
use super::LatencySnapshot;

// --- overlay-state.txt path resolution --------------------------------------
// Passwd lookups, desktop-user id resolution, and chown-back live in `paths`
// (shared with the latency receiver); this module resolves the overlay path and
// writes the state file.

use paths::{claim_desktop_user_ownership, geteuid_is_root, nonempty_env};

/// `overlay_state_path` — env override first (always pinned by the launcher),
/// then the home cache dir, XDG runtime, root fallback, and `/tmp` last resort.
pub fn overlay_state_path() -> PathBuf {
    if let Some(explicit) = nonempty_env("PENGUIN_BURNER_OVERLAY_STATE") {
        return PathBuf::from(explicit);
    }
    if let Some(home_dir) = paths::desktop_cache_dir() {
        return home_dir.join("overlay-state.txt");
    }
    if let Some(runtime) = nonempty_env("XDG_RUNTIME_DIR") {
        return PathBuf::from(runtime)
            .join("penguin-burner")
            .join("overlay-state.txt");
    }
    // Root-only /run/user probe uses the EFFECTIVE uid (see run_user_penguin_dir).
    if let Some(dir) = paths::run_user_penguin_dir(geteuid_is_root()) {
        return dir.join("overlay-state.txt");
    }
    // SAFETY: getuid is always safe.
    let uid = unsafe { libc::getuid() };
    PathBuf::from(format!("/tmp/penguin-burner-overlay-{uid}.txt"))
}

// --- overlay-state.txt writer -----------------------------------------------

#[derive(Debug, Clone, Default)]
pub struct OverlayState {
    pub gpu_index: i64,
    pub clock_mhz: Option<i64>,
    pub voltage_mv: Option<i64>,
    pub power_w: Option<i64>,
    pub gpu_util_pct: Option<i64>,
    pub cpu_util_pct: Option<i64>,
    pub cpu_peak_thread_pct: Option<i64>,
    pub fan_pct: Option<i64>,
    pub temperature_c: Option<i64>,
    pub uv_offset_mv: Option<i64>,
    pub present_fps: String,
    pub fps_source: String,
    pub framegen_fps: String,
    pub framegen_active: bool,
    pub latency_ms: String,
    pub display_latency_ms: String,
    pub profile_tier: String,
    pub profile_tier_key: String,
    pub profile_id: String,
    pub adaptive: bool,
    pub updated_unix_ns: i64,
}

fn escape_value(value: &str) -> String {
    value.replace('\\', "\\\\").replace('\n', "\\n")
}

fn value_text(value: Option<i64>) -> String {
    value.map(|v| v.to_string()).unwrap_or_default()
}

/// Render the file body (all 22 keys, exact order, `key=value\n`).
pub fn render_overlay_state(state: &OverlayState) -> String {
    let updated = if state.updated_unix_ns > 0 {
        state.updated_unix_ns
    } else {
        super::now_unix_ns()
    };
    let bool_text = |b: bool| if b { "1" } else { "0" };
    let pairs: [(&str, String); 22] = [
        ("version", "1".to_string()),
        ("updated_unix_ns", updated.to_string()),
        ("gpu_index", state.gpu_index.to_string()),
        ("clock_mhz", value_text(state.clock_mhz)),
        ("voltage_mv", value_text(state.voltage_mv)),
        ("power_w", value_text(state.power_w)),
        ("gpu_util_pct", value_text(state.gpu_util_pct)),
        ("cpu_util_pct", value_text(state.cpu_util_pct)),
        ("cpu_peak_thread_pct", value_text(state.cpu_peak_thread_pct)),
        ("fan_pct", value_text(state.fan_pct)),
        ("temperature_c", value_text(state.temperature_c)),
        ("uv_offset_mv", value_text(state.uv_offset_mv)),
        ("present_fps", state.present_fps.clone()),
        ("fps_source", state.fps_source.clone()),
        ("framegen_fps", state.framegen_fps.clone()),
        (
            "framegen_active",
            bool_text(state.framegen_active).to_string(),
        ),
        ("latency_ms", state.latency_ms.clone()),
        ("display_latency_ms", state.display_latency_ms.clone()),
        ("profile_tier", state.profile_tier.clone()),
        ("profile_tier_key", state.profile_tier_key.clone()),
        ("profile_id", state.profile_id.clone()),
        ("adaptive", bool_text(state.adaptive).to_string()),
    ];
    let mut text = String::new();
    for (key, value) in &pairs {
        text.push_str(key);
        text.push('=');
        text.push_str(&escape_value(value));
        text.push('\n');
    }
    text
}

/// `write_overlay_state` — atomic tmp+rename with chown-back to the desktop user.
pub fn write_overlay_state(state: &OverlayState, path: &Path) -> std::io::Result<()> {
    let text = render_overlay_state(state);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
        claim_desktop_user_ownership(parent, true);
    }
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let mut temp = NamedTempFile::new_in(parent)?;
    temp.write_all(text.as_bytes())?;
    temp.persist(path).map_err(|error| error.error)?;
    claim_desktop_user_ownership(path, false);
    Ok(())
}

// --- per-poll telemetry text ------------------------------------------------

/// The nearest active VF point to `(core_clock_mhz, voltage_uv)` plus the nearest
/// editable *base* point by base-frequency distance. `None` when either is
/// missing (no nearest point / no editable points). Shared by
/// `format_vf_curve_comparison` and `uv_offset_mv`; neither refreshes the cache
/// here — the caller does so beforehand when it needs a fresh read.
fn nearest_and_base(
    backend: &dyn GpuBackend,
    core_clock_mhz: i64,
    voltage_uv: i64,
) -> Option<(VfPoint, VfPoint)> {
    let point = backend.find_nearest_vf_point(core_clock_mhz, voltage_uv)?;
    let editable = backend.editable_core_vf_points();
    let base_point = *editable
        .iter()
        .min_by_key(|c| (c.base_freq_khz - core_clock_mhz * 1000).abs())?;
    Some((point, base_point))
}

/// `format_vf_curve_comparison` — the `vf_point=… uv=…` fragment.
fn format_vf_curve_comparison(
    backend: &dyn GpuBackend,
    core_clock_mhz: i64,
    voltage_uv: i64,
) -> String {
    let Some((point, base_point)) = nearest_and_base(backend, core_clock_mhz, voltage_uv) else {
        return String::new();
    };
    let point_freq_mhz = floor_div(point.freq_khz, 1000);
    let point_voltage_mv = floor_div(point.voltage_uv, 1000);
    let point_offset_mhz = floor_div(point.current_offset_khz, 1000);
    let base_clock_mhz = floor_div(base_point.base_freq_khz, 1000);
    let base_voltage_mv = floor_div(base_point.voltage_uv, 1000);
    let uv_delta_mv = point_voltage_mv - base_voltage_mv;
    format!(
        "vf_point={point_freq_mhz}MHz@{point_voltage_mv}mV vf_offset={point_offset_mhz:+}MHz vf_base={base_clock_mhz}MHz@{base_voltage_mv}mV uv={uv_delta_mv:+}mV "
    )
}

/// `format_telemetry` — the per-poll telemetry string (before `single_line_text`).
pub fn format_telemetry(
    backend: &dyn GpuBackend,
    fan_count: u32,
    current_temp_c: f64,
    power_draw_w: Option<f64>,
    clock_ceiling_text: &str,
) -> String {
    let fan_text = match backend.reported_fan_speeds(fan_count) {
        None => "n/a".to_string(),
        Some(speeds) => speeds
            .iter()
            .map(|s| format!("{s}%"))
            .collect::<Vec<_>>()
            .join("/"),
    };
    let power = power_draw_w.or_else(|| backend.power_draw_w());
    let power_text = power.map_or_else(|| "n/a".to_string(), |p| format!("{p:.2}W"));
    let core_clock = backend.clock_info_mhz(ClockType::Graphics);
    let clock_text = core_clock.map_or_else(|| "n/a".to_string(), |c| format!("{c}MHz"));
    let mem_clock = backend.clock_info_mhz(ClockType::Memory);
    let mem_clock_text = mem_clock.map_or_else(|| "n/a".to_string(), |c| format!("{c}MHz"));
    let voltage_uv = backend.read_voltage_uv();
    let voltage_text = voltage_uv.map_or_else(
        || "n/a".to_string(),
        |v| format!("{:.0}mV", v as f64 / 1000.0),
    );
    let clock_offset_text = match backend.clock_offsets().mem_clk_vf_offset_mhz {
        Some(offset) => format!("mem_vf_offset={offset:+}MHz "),
        None => String::new(),
    };
    let vf_point_text = match (core_clock, voltage_uv) {
        (Some(clock), Some(volt)) => {
            let _ = backend.refresh_vf_points();
            format_vf_curve_comparison(backend, i64::from(clock), volt)
        }
        _ => String::new(),
    };
    format!(
        "temp={current_temp_c:.1}C fan={fan_text} power={power_text} gpu_clock={clock_text} mem_clock={mem_clock_text} voltage={voltage_text} {clock_ceiling_text}{clock_offset_text}{vf_point_text}"
    )
    .trim_end()
    .to_string()
}

/// `_telemetry_number` — parse `key=<num><unit>` out of the telemetry text.
pub fn telemetry_number(text: &str, key: &str, unit: &str) -> Option<f64> {
    let prefix = format!("{key}=");
    for token in text.split_whitespace() {
        if let Some(middle) = token
            .strip_prefix(&prefix)
            .and_then(|t| t.strip_suffix(unit))
        {
            if let Ok(value) = middle.parse::<f64>() {
                return Some(value);
            }
        }
    }
    None
}

// --- uv_offset_mv (overlay) -------------------------------------------------

fn uv_offset_mv(
    backend: &dyn GpuBackend,
    core_clock_mhz: Option<i64>,
    voltage_mv: Option<i64>,
) -> Option<i64> {
    let (Some(clock), Some(volt)) = (core_clock_mhz, voltage_mv) else {
        return None;
    };
    let _ = backend.refresh_vf_points();
    let (point, base_point) = nearest_and_base(backend, clock, volt * 1000)?;
    let point_voltage_mv = floor_div(point.voltage_uv, 1000);
    let base_voltage_mv = floor_div(base_point.voltage_uv, 1000);
    Some(point_voltage_mv - base_voltage_mv)
}

// --- overlay state publisher ------------------------------------------------

fn hold_ns_from_interval(interval_s: f64, min_hold_s: f64) -> i64 {
    let interval = interval_s.clamp(1.0, 10.0);
    let hold_s = if min_hold_s > 0.0 {
        (interval * 3.0).clamp(min_hold_s, 10.0)
    } else {
        interval
    };
    super::round_half_even(hold_s * 1_000_000_000.0) as i64
}

pub struct OverlayStatePublisher {
    pub gpu_index: i64,
    pub enabled: bool,
    pub update_interval_s: f64,
    pub profile_tier: String,
    pub profile_tier_key: String,
    pub profile_id: String,
    pub adaptive: bool,
    pub path: Option<PathBuf>,
    process_cpu_sampler: Option<ProcessCpuUsageSampler>,

    last_cpu_util_pct: Option<i64>,
    last_cpu_peak_thread_pct: Option<i64>,
    last_cpu_metric_ns: Option<i64>,
    last_published_gpu_util_pct: Option<i64>,
    last_published_cpu_util_pct: Option<i64>,
    last_published_cpu_peak_thread_pct: Option<i64>,

    last_present_fps: Option<String>,
    last_fps_source: Option<String>,
    last_framegen_fps: Option<String>,
    last_framegen_active: bool,
    last_fps_pid: Option<String>,
    last_fps_ns: Option<i64>,

    last_latency_ms: Option<String>,
    last_display_latency_ms: Option<String>,
    last_latency_pid: Option<String>,
    last_latency_ns: Option<i64>,
}

impl OverlayStatePublisher {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        gpu_index: i64,
        enabled: bool,
        update_interval_s: f64,
        profile_tier: String,
        profile_tier_key: String,
        profile_id: String,
        adaptive: bool,
        process_cpu_sampler: Option<ProcessCpuUsageSampler>,
    ) -> Self {
        OverlayStatePublisher {
            gpu_index,
            enabled,
            update_interval_s,
            profile_tier,
            profile_tier_key,
            profile_id,
            adaptive,
            path: None,
            process_cpu_sampler,
            last_cpu_util_pct: None,
            last_cpu_peak_thread_pct: None,
            last_cpu_metric_ns: None,
            last_published_gpu_util_pct: None,
            last_published_cpu_util_pct: None,
            last_published_cpu_peak_thread_pct: None,
            last_present_fps: None,
            last_fps_source: None,
            last_framegen_fps: None,
            last_framegen_active: false,
            last_fps_pid: None,
            last_fps_ns: None,
            last_latency_ms: None,
            last_display_latency_ms: None,
            last_latency_pid: None,
            last_latency_ns: None,
        }
    }

    // These expose the last *published* metrics (the Python `last_*` properties
    // the adaptive controller reads); the similarly-named `last_*` fields are the
    // sticky-cache internals, so the getter/field names deliberately differ.
    #[allow(clippy::misnamed_getters)]
    pub fn last_gpu_util_pct(&self) -> Option<i64> {
        self.last_published_gpu_util_pct
    }
    #[allow(clippy::misnamed_getters)]
    pub fn last_cpu_util_pct(&self) -> Option<i64> {
        self.last_published_cpu_util_pct
    }
    #[allow(clippy::misnamed_getters)]
    pub fn last_cpu_peak_thread_pct(&self) -> Option<i64> {
        self.last_published_cpu_peak_thread_pct
    }

    /// Build the `OverlayState` for one tick and write it (the loop gates
    /// cadence). `now_ns` is the wall-clock stamp (injectable for tests).
    pub fn publish(
        &mut self,
        backend: &dyn GpuBackend,
        latency_snapshot: Option<&LatencySnapshot>,
        now_ns: i64,
    ) -> std::io::Result<()> {
        let state = self.build_state(backend, latency_snapshot, now_ns);
        let path = self.path.clone().unwrap_or_else(overlay_state_path);
        write_overlay_state(&state, &path)
    }

    pub fn build_state(
        &mut self,
        backend: &dyn GpuBackend,
        latency_snapshot: Option<&LatencySnapshot>,
        now_ns: i64,
    ) -> OverlayState {
        let clock_mhz = backend.clock_info_mhz(ClockType::Graphics).map(i64::from);
        let power_w = backend
            .power_draw_w()
            .map(|p| super::round_half_even(p) as i64);
        let gpu_util_pct = backend.gpu_utilization_pct().map(i64::from);
        self.last_published_gpu_util_pct = gpu_util_pct;

        // CPU metrics (need a pid from the latency snapshot).
        let mut cpu_util_pct = None;
        let mut cpu_peak_thread_pct = None;
        if let Some(sampler) = self.process_cpu_sampler.as_mut() {
            if let Some(pid) = latency_snapshot.and_then(pid_from_snapshot) {
                let usage = sampler.sample_usage(&pid);
                cpu_util_pct = usage.process_util_pct;
                cpu_peak_thread_pct = usage.peak_thread_util_pct;
            }
        }
        self.remember_cpu_metrics(now_ns, cpu_util_pct, cpu_peak_thread_pct);
        let (cpu_util_pct, cpu_peak_thread_pct) = self.sticky_cpu_metrics(now_ns);
        self.last_published_cpu_util_pct = cpu_util_pct;
        self.last_published_cpu_peak_thread_pct = cpu_peak_thread_pct;

        let fan_count = backend.fan_count().unwrap_or(0);
        let fan_pct = backend.reported_fan_speeds(fan_count).and_then(|speeds| {
            if speeds.is_empty() {
                None
            } else {
                let sum: f64 = speeds.iter().map(|&s| f64::from(s)).sum();
                Some(super::round_half_even(sum / speeds.len() as f64) as i64)
            }
        });
        let temperature_c = backend
            .temperature_c()
            .ok()
            .map(|t| super::round_half_even(t) as i64);
        let voltage_mv = backend
            .read_voltage_uv()
            .map(|uv| super::round_half_even(uv as f64 / 1000.0) as i64);
        let uv = uv_offset_mv(backend, clock_mhz, voltage_mv);

        let label = {
            let tier = self.profile_tier.trim();
            if !tier.is_empty() {
                tier.to_string()
            } else {
                let from_key = profile_tier_label(&self.profile_tier_key);
                if from_key.is_empty() {
                    "Balanced".to_string()
                } else {
                    from_key
                }
            }
        };

        let (present_fps, fps_source, framegen_fps, framegen_active) =
            self.overlay_fps_values(latency_snapshot, now_ns);
        let (latency_ms, display_latency_ms) = match latency_snapshot {
            Some(snapshot) => self.overlay_latency_values(snapshot, now_ns),
            None => (String::new(), String::new()),
        };

        OverlayState {
            gpu_index: self.gpu_index,
            clock_mhz,
            voltage_mv,
            power_w,
            gpu_util_pct,
            cpu_util_pct,
            cpu_peak_thread_pct,
            fan_pct,
            temperature_c,
            uv_offset_mv: uv,
            present_fps,
            fps_source,
            framegen_fps,
            framegen_active,
            latency_ms,
            display_latency_ms,
            profile_tier: label,
            profile_tier_key: self.profile_tier_key.clone(),
            profile_id: self.profile_id.clone(),
            adaptive: self.adaptive,
            updated_unix_ns: now_ns,
        }
    }

    fn remember_cpu_metrics(&mut self, now_ns: i64, cpu: Option<i64>, peak: Option<i64>) {
        if cpu.is_none() && peak.is_none() {
            return;
        }
        if let Some(c) = cpu {
            self.last_cpu_util_pct = Some(c);
        }
        if let Some(p) = peak {
            self.last_cpu_peak_thread_pct = Some(p);
        }
        self.last_cpu_metric_ns = Some(now_ns);
    }

    fn sticky_cpu_metrics(&self, now_ns: i64) -> (Option<i64>, Option<i64>) {
        let Some(last) = self.last_cpu_metric_ns else {
            return (None, None);
        };
        if now_ns - last > hold_ns_from_interval(self.update_interval_s, 0.0) {
            return (None, None);
        }
        (self.last_cpu_util_pct, self.last_cpu_peak_thread_pct)
    }

    fn overlay_fps_values(
        &mut self,
        snapshot: Option<&LatencySnapshot>,
        now_ns: i64,
    ) -> (String, String, String, bool) {
        let Some(snapshot) = snapshot else {
            return self.sticky_fps_values(now_ns, None);
        };
        let fps_pid = identity_from_snapshot(snapshot);
        let present_fps = fps_text(snapshot.present_fps.as_deref());
        let fps_source = snapshot
            .fps_source
            .clone()
            .unwrap_or_default()
            .trim()
            .to_string();
        let framegen_fps = framegen_fps_from_snapshot(snapshot);
        let framegen_active = flag_enabled(snapshot.framegen_active.as_deref());
        if !present_fps.is_empty() {
            self.last_present_fps = Some(present_fps.clone());
            self.last_fps_source = Some(fps_source.clone());
            self.last_framegen_fps = Some(framegen_fps.clone());
            self.last_framegen_active = framegen_active;
            self.last_fps_pid = fps_pid;
            self.last_fps_ns = Some(now_ns);
            return (present_fps, fps_source, framegen_fps, framegen_active);
        }
        self.sticky_fps_values(now_ns, fps_pid)
    }

    fn sticky_fps_values(
        &mut self,
        now_ns: i64,
        fps_pid: Option<String>,
    ) -> (String, String, String, bool) {
        if self.last_present_fps.as_deref().unwrap_or("").is_empty() {
            return (String::new(), String::new(), String::new(), false);
        }
        let pid_match = match (&self.last_fps_pid, &fps_pid) {
            (None, _) | (_, None) => true,
            (Some(a), Some(b)) => a == b,
        };
        let fresh = self.last_fps_ns.is_some_and(|last| {
            now_ns - last <= hold_ns_from_interval(self.update_interval_s, 3.0)
        });
        if !pid_match || !fresh {
            self.clear_fps();
            return (String::new(), String::new(), String::new(), false);
        }
        (
            self.last_present_fps.clone().unwrap_or_default(),
            self.last_fps_source.clone().unwrap_or_default(),
            self.last_framegen_fps.clone().unwrap_or_default(),
            self.last_framegen_active,
        )
    }

    fn clear_fps(&mut self) {
        self.last_present_fps = None;
        self.last_fps_source = None;
        self.last_framegen_fps = None;
        self.last_framegen_active = false;
        self.last_fps_pid = None;
        self.last_fps_ns = None;
    }

    fn overlay_latency_values(
        &mut self,
        snapshot: &LatencySnapshot,
        now_ns: i64,
    ) -> (String, String) {
        let latency_pid = identity_from_snapshot(snapshot);
        let latency_ms = rounded_text(snapshot.latency_p95_ms);
        let display_latency_ms = rounded_text(snapshot.display_latency_p95_ms);
        if !latency_ms.is_empty() {
            self.last_latency_ms = Some(latency_ms.clone());
            self.last_display_latency_ms = Some(display_latency_ms.clone());
            self.last_latency_pid = latency_pid;
            self.last_latency_ns = Some(now_ns);
            return (latency_ms, display_latency_ms);
        }
        let matches = self.last_latency_ms.is_some()
            && self.last_latency_pid.is_some()
            && latency_pid.is_some()
            && self.last_latency_pid == latency_pid;
        let fresh = self.last_latency_ns.is_some_and(|last| {
            now_ns - last <= hold_ns_from_interval(self.update_interval_s, 3.0)
        });
        if matches && fresh {
            return (
                self.last_latency_ms.clone().unwrap_or_default(),
                self.last_display_latency_ms.clone().unwrap_or_default(),
            );
        }
        self.last_latency_ms = None;
        self.last_display_latency_ms = None;
        self.last_latency_pid = None;
        self.last_latency_ns = None;
        (String::new(), String::new())
    }
}

fn pid_from_snapshot(snapshot: &LatencySnapshot) -> Option<String> {
    snapshot.pid.as_ref().filter(|p| !p.is_empty()).cloned()
}

fn identity_from_snapshot(snapshot: &LatencySnapshot) -> Option<String> {
    snapshot
        .session_id
        .as_ref()
        .filter(|value| !value.is_empty())
        .cloned()
        .or_else(|| pid_from_snapshot(snapshot))
}

fn fps_text(value: Option<&str>) -> String {
    let text = value.unwrap_or("").trim();
    if text.is_empty() || text.eq_ignore_ascii_case("n/a") {
        String::new()
    } else {
        text.to_string()
    }
}

fn framegen_fps_from_snapshot(snapshot: &LatencySnapshot) -> String {
    if let Some(avg) = snapshot.raw_present_fps_stats_avg.as_deref() {
        let text = avg.trim();
        if !text.is_empty() && !text.eq_ignore_ascii_case("n/a") {
            return text.to_string();
        }
    }
    match snapshot.raw_present_fps_avg {
        Some(v) => (super::round_half_even(v) as i64).to_string(),
        None => String::new(),
    }
}

fn flag_enabled(value: Option<&str>) -> bool {
    super::truthy_str(value.unwrap_or(""))
}

fn rounded_text(value: Option<f64>) -> String {
    match value {
        Some(v) => (super::round_half_even(v) as i64).to_string(),
        None => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn state_file_bytes_populated() {
        let state = OverlayState {
            gpu_index: 0,
            clock_mhz: Some(2820),
            voltage_mv: Some(925),
            power_w: Some(318),
            gpu_util_pct: Some(97),
            cpu_util_pct: Some(32),
            cpu_peak_thread_pct: Some(98),
            fan_pct: Some(62),
            temperature_c: Some(67),
            uv_offset_mv: Some(-75),
            present_fps: "119".into(),
            fps_source: "native".into(),
            framegen_fps: "176".into(),
            framegen_active: true,
            latency_ms: "23".into(),
            display_latency_ms: "4".into(),
            profile_tier: "Balanced".into(),
            profile_tier_key: "balanced".into(),
            profile_id: "uv_925mv_2820mhz".into(),
            adaptive: false,
            updated_unix_ns: 1751889600123456789,
        };
        let expected = "version=1\nupdated_unix_ns=1751889600123456789\ngpu_index=0\nclock_mhz=2820\nvoltage_mv=925\npower_w=318\ngpu_util_pct=97\ncpu_util_pct=32\ncpu_peak_thread_pct=98\nfan_pct=62\ntemperature_c=67\nuv_offset_mv=-75\npresent_fps=119\nfps_source=native\nframegen_fps=176\nframegen_active=1\nlatency_ms=23\ndisplay_latency_ms=4\nprofile_tier=Balanced\nprofile_tier_key=balanced\nprofile_id=uv_925mv_2820mhz\nadaptive=0\n";
        assert_eq!(render_overlay_state(&state), expected);
    }

    #[test]
    fn state_file_bytes_idle() {
        let state = OverlayState {
            gpu_index: 0,
            clock_mhz: Some(210),
            power_w: Some(15),
            gpu_util_pct: Some(0),
            fan_pct: Some(30),
            temperature_c: Some(41),
            profile_tier: "Balanced".into(),
            profile_tier_key: "balanced".into(),
            updated_unix_ns: 1751889600123456789,
            ..Default::default()
        };
        let expected = "version=1\nupdated_unix_ns=1751889600123456789\ngpu_index=0\nclock_mhz=210\nvoltage_mv=\npower_w=15\ngpu_util_pct=0\ncpu_util_pct=\ncpu_peak_thread_pct=\nfan_pct=30\ntemperature_c=41\nuv_offset_mv=\npresent_fps=\nfps_source=\nframegen_fps=\nframegen_active=0\nlatency_ms=\ndisplay_latency_ms=\nprofile_tier=Balanced\nprofile_tier_key=balanced\nprofile_id=\nadaptive=0\n";
        assert_eq!(render_overlay_state(&state), expected);
    }

    #[test]
    fn escaping_backslash_and_newline() {
        assert_eq!(escape_value("a\\b\nc"), "a\\\\b\\nc");
    }

    #[test]
    fn telemetry_number_parses() {
        let text = "temp=67.0C fan=62% gpu_clock=2820MHz voltage=925mV";
        assert_eq!(telemetry_number(text, "gpu_clock", "MHz"), Some(2820.0));
        assert_eq!(telemetry_number(text, "voltage", "mV"), Some(925.0));
        assert_eq!(telemetry_number(text, "missing", "MHz"), None);
    }
}
