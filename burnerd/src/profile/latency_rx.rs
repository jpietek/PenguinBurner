//! In-game latency/FPS telemetry receiver — port of `overlay/telemetry/`
//! (`receiver.py::LatencyTelemetryMeter` + `LatencyTelemetryLogger`, `samples.py`,
//! `framegen.py`, `sockets.py`).
//!
//! The C++ Vulkan layer (and the nvapi shim) in the game emits JSON timing
//! datagrams to one or more `AF_UNIX` `SOCK_DGRAM` sockets. A background thread
//! drains them into a bounded ring, and [`LatencyReceiver::snapshot`] aggregates
//! a fresh window into the [`LatencySnapshot`] the engine loop injects into the
//! adaptive controller (base-present frametime p95) and the overlay publisher
//! (fps / framegen / latency fields). The wire format, path resolution, socket
//! permissions/ownership, staleness window, and every aggregation match the
//! Python originals; only the diagnostic journal summary lines (`event=latency-
//! meter` / status / raw-timing) are intentionally not reproduced — nothing
//! parses them and they are not part of the snapshot contract (see STATUS Wave A5).

use std::collections::{HashMap, VecDeque};
use std::os::unix::io::AsRawFd;
use std::os::unix::net::UnixDatagram;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use serde_json::{Map, Value};

use super::{monotonic_now, round_half_even, LatencySnapshot};
use crate::paths::{
    claim_desktop_user_ownership, cpath, desktop_cache_dir, nonempty_env, run_user_penguin_dir,
};

// --- constants (receiver.py / framegen.py) ----------------------------------

pub(super) const METER_SAMPLE_MAX_AGE_S: f64 = 3.0;
const METER_MAX_SAMPLES: usize = 4096;
const METER_MIN_PRESENT_SAMPLES: usize = 4;
const METER_MARKER_FPS_MAX_PRESENT_RATIO: f64 = 1.25;
const DRIVER_REPORT_RING_SPAN: i64 = 256;
const FRAMEGEN_CADENCE_RATIO: f64 = 1.5;
const DXVK_NVAPI_VKREFLEX_SOURCE: &str = "dxvk-nvapi-vkreflex";
const NVAPI_MARKER_SOURCE: &str = "nvapi-marker-log";

const BASE_FRAME_MARKER_PRIORITY: [&str; 3] =
    ["oob-present-start", "present-start", "rendersubmit-start"];
const FRAMEGEN_ACTIVE_KEYS: [&str; 3] = ["framegen_active", "frame_generation_active", "fg_active"];
const FRAMEGEN_MARKER_KEYS: [&str; 3] = ["framegen_frame", "generated_frame", "fg_frame"];
const FRAMEGEN_COUNT_KEYS: [&str; 3] = [
    "framegen_frame_count",
    "generated_frame_count",
    "fg_frame_count",
];
const FRAMEGEN_MEASUREMENTS: [&str; 3] = ["framegen-marker", "framegen-frame", "generated-frame"];
// `framegen.py::FRAMEGEN_MARKER_NAMES` — the out-of-band present marker names.
const FRAMEGEN_MARKER_NAME_SET: [&str; 6] = [
    "oob-present-start",
    "oob-present-end",
    "out-of-band-present-start",
    "out-of-band-present-end",
    "out_of_band_present_start",
    "out_of_band_present_end",
];

// --- value accessors (samples.py `_int_value` / `_positive_us`) --------------

/// `_int_value(value)` = `int(value or 0)` with the same tolerances: JSON ints
/// pass through, floats truncate toward zero, integer-literal strings parse,
/// `true`→1, everything else (null / non-integer strings / arrays) → 0.
fn int_value(value: Option<&Value>) -> i64 {
    match value {
        None | Some(Value::Null) => 0,
        Some(Value::Bool(b)) => i64::from(*b),
        Some(Value::Number(n)) => {
            if let Some(i) = n.as_i64() {
                i
            } else if let Some(u) = n.as_u64() {
                u as i64
            } else {
                n.as_f64().map(|f| f.trunc() as i64).unwrap_or(0)
            }
        }
        Some(Value::String(s)) => parse_python_int(s),
        Some(Value::Array(_)) | Some(Value::Object(_)) => 0,
    }
}

/// `int(str_or_0)` — strip, `""`→0, base-10 integer with optional sign else 0.
fn parse_python_int(s: &str) -> i64 {
    let t = s.trim();
    if t.is_empty() {
        return 0;
    }
    t.parse::<i64>().unwrap_or(0)
}

fn positive_us(sample: &Map<String, Value>, key: &str) -> bool {
    int_value(sample.get(key)) > 0
}

/// `str(sample.get("measurement") or "")` (always an ASCII token in practice).
fn measurement(sample: &Map<String, Value>) -> &str {
    sample
        .get("measurement")
        .and_then(Value::as_str)
        .unwrap_or("")
}

fn number_is_zero(n: &serde_json::Number) -> bool {
    n.as_f64().map(|f| f == 0.0).unwrap_or(false)
}

/// `_truthy_value(value)` = `str(value or "").strip().lower() in {..}`.
fn truthy_value(value: Option<&Value>) -> bool {
    let text = match value {
        None | Some(Value::Null) | Some(Value::Bool(false)) => String::new(),
        Some(Value::Bool(true)) => "true".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) if number_is_zero(n) => String::new(),
        Some(Value::Number(n)) => n.to_string(),
        Some(other) => other.to_string(),
    };
    super::truthy_str(&text)
}

fn any_truthy(sample: &Map<String, Value>, keys: &[&str]) -> bool {
    keys.iter().any(|k| truthy_value(sample.get(*k)))
}

fn any_positive_count(sample: &Map<String, Value>, keys: &[&str]) -> bool {
    keys.iter().any(|k| int_value(sample.get(*k)) > 0)
}

/// `_pid_from_latency_snapshot` inner rule: the raw pid stringified, `None`/`""`
/// skipped. The snapshot dict has no top-level pid, so this scans samples.
fn pid_value(value: Option<&Value>) -> Option<String> {
    match value {
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        Some(Value::Number(n)) => Some(n.to_string()),
        _ => None,
    }
}

// --- aggregation helpers (receiver.py) --------------------------------------

/// `_p95_us(values)` — drop `<= 0`, sort, index `round((n-1)*0.95)` (banker's).
fn quantile_index(n: usize, q: f64) -> usize {
    let raw = round_half_even((n as f64 - 1.0) * q) as i64;
    raw.clamp(0, n as i64 - 1) as usize
}

fn p95_us(values: &[i64]) -> Option<i64> {
    let mut v: Vec<i64> = values.iter().copied().filter(|&x| x > 0).collect();
    if v.is_empty() {
        return None;
    }
    v.sort_unstable();
    let n = v.len();
    Some(v[quantile_index(n, 0.95)])
}

/// Every statistic the window offers, from one filtered copy and one sort.
///
/// The percentiles are order statistics of the same sample, so they share the
/// work instead of taking a copy and a sort each; the miss count needs no
/// ordering at all and rides along in the filtering pass.
struct FrametimeStats {
    p95_us: i64,
    /// Median of the same accepted set as `p95_us`.
    p50_us: i64,
    /// Share of frames over the deadline, when one was supplied.
    ///
    /// The real-time literature calls this the deadline miss ratio: not "how
    /// late was the worst frame" but "how much of the window blew its budget".
    /// A tail can be one stutter; a ratio cannot.
    miss_ratio: Option<f64>,
}

fn frametime_stats(values: &[i64], miss_deadline_us: Option<i64>) -> Option<FrametimeStats> {
    let mut v: Vec<i64> = Vec::with_capacity(values.len());
    let mut missed = 0usize;
    for &value in values {
        if value <= 0 {
            continue;
        }
        if miss_deadline_us.is_some_and(|deadline| value > deadline) {
            missed += 1;
        }
        v.push(value);
    }
    if v.is_empty() {
        return None;
    }
    v.sort_unstable();
    let n = v.len();
    Some(FrametimeStats {
        p95_us: v[quantile_index(n, 0.95)],
        p50_us: v[quantile_index(n, 0.5)],
        miss_ratio: miss_deadline_us.map(|_| missed as f64 / n as f64),
    })
}

/// `_mean_fps` — `n * 1e6 / sum` over positive frametimes (float division).
fn mean_fps(values: &[i64]) -> Option<f64> {
    let ft: Vec<i64> = values.iter().copied().filter(|&v| v > 0).collect();
    if ft.is_empty() {
        return None;
    }
    let sum: i64 = ft.iter().sum();
    Some(ft.len() as f64 * 1_000_000.0 / sum as f64)
}

/// `_fps_from_frametime` — `None`/`0`→None else `1e6/us`.
fn fps_from_frametime(frametime_us: Option<i64>) -> Option<f64> {
    match frametime_us {
        Some(us) if us != 0 => Some(1_000_000.0 / us as f64),
        _ => None,
    }
}

/// `_format_fps_value` — `<=0`/None → "n/a" else `floor(fps + 0.5)`.
fn format_fps_value(fps: Option<f64>) -> String {
    match fps {
        Some(f) if f > 0.0 => ((f + 0.5).floor() as i64).to_string(),
        _ => "n/a".to_string(),
    }
}

// --- normalization (samples.py) ---------------------------------------------

fn elapsed_us(sample: &Map<String, Value>, start_key: &str, end_key: &str) -> i64 {
    let start = int_value(sample.get(start_key));
    let end = int_value(sample.get(end_key));
    if start == 0 || end == 0 || end <= start {
        return 0;
    }
    end - start
}

fn looks_like_driver_timing_report(sample: &Map<String, Value>) -> bool {
    positive_us(sample, "timing_count")
        || [
            "driver_start_us",
            "driver_end_us",
            "gpu_render_start_us",
            "gpu_render_end_us",
        ]
        .iter()
        .any(|k| positive_us(sample, k))
}

/// `normalize_timing_sample` — infer a driver-report measurement, and synthesize
/// `gpu_render_us` / `render_present_us` from start/end pairs when absent.
fn normalize_timing_sample(mut sample: Map<String, Value>) -> Map<String, Value> {
    if sample.get("type").and_then(Value::as_str) != Some("timing") {
        return sample;
    }
    let measurement_empty = measurement(&sample).is_empty();
    let source_is_vkreflex =
        sample.get("source").and_then(Value::as_str) == Some(DXVK_NVAPI_VKREFLEX_SOURCE);
    if measurement_empty && source_is_vkreflex && looks_like_driver_timing_report(&sample) {
        sample.insert("measurement".to_string(), Value::from("driver-report"));
    }
    if !positive_us(&sample, "gpu_render_us") {
        let v = elapsed_us(&sample, "gpu_render_start_us", "gpu_render_end_us");
        if v != 0 {
            sample.insert("gpu_render_us".to_string(), Value::from(v));
        }
    }
    if !positive_us(&sample, "render_present_us") {
        let v = elapsed_us(&sample, "present_start_us", "gpu_render_end_us");
        if v != 0 {
            sample.insert("render_present_us".to_string(), Value::from(v));
        }
    }
    sample
}

// --- framegen / marker helpers (framegen.py) --------------------------------

/// `_explicit_framegen_active` — any hard evidence of generated frames.
fn explicit_framegen_active(samples: &[&Sample]) -> bool {
    samples.iter().any(|s| {
        let d = &s.data;
        any_truthy(d, &FRAMEGEN_ACTIVE_KEYS)
            || any_truthy(d, &FRAMEGEN_MARKER_KEYS)
            || any_positive_count(d, &FRAMEGEN_COUNT_KEYS)
            || FRAMEGEN_MARKER_NAME_SET.contains(
                &d.get("marker_name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_lowercase()
                    .as_str(),
            )
            || positive_us(d, "sim_to_oob_present_us")
            || positive_us(d, "input_to_oob_present_us")
            || FRAMEGEN_MEASUREMENTS.contains(&measurement(d).trim().to_lowercase().as_str())
    })
}

/// `_framegen_active_for_overlay` — the cadence-ratio gate: report frame
/// generation only when a deinterlaced (independent) present cadence clearly
/// exceeds the base render cadence, or on explicit generated-frame evidence.
fn framegen_active_for_overlay(
    samples: &[&Sample],
    base_fps: Option<f64>,
    raw_output_fps: Option<f64>,
    cadence_is_independent: bool,
) -> bool {
    if cadence_is_independent {
        if let (Some(base), Some(output)) = (base_fps, raw_output_fps) {
            if base > 0.0 && output >= base * FRAMEGEN_CADENCE_RATIO {
                return true;
            }
        }
    }
    samples.iter().any(|s| {
        let d = &s.data;
        any_truthy(d, &FRAMEGEN_ACTIVE_KEYS)
            || any_truthy(d, &FRAMEGEN_MARKER_KEYS)
            || any_positive_count(d, &FRAMEGEN_COUNT_KEYS)
            || FRAMEGEN_MEASUREMENTS.contains(&measurement(d).trim().to_lowercase().as_str())
    })
}

/// `_has_marker_stream` — whether the window carries a Reflex/nvapi marker
/// stream (gates the deinterlace division so a real menu→game rate jump is not
/// mistaken for frame generation).
fn has_marker_stream(samples: &[&Sample]) -> bool {
    samples.iter().any(|s| {
        let d = &s.data;
        int_value(d.get("marker_bits")) != 0
            || measurement(d) == "marker-proxy"
            || int_value(d.get("base_frame_frametime_us")) > 0
            || positive_us(d, "sim_to_present_us")
            || positive_us(d, "sim_to_oob_present_us")
    })
}

// --- base-frame-marker + latency-proxy selection ----------------------------

struct BaseMarkerSelection {
    p95_us: i64,
    /// Median of the *same* accepted sample set as `p95_us`, so the two are
    /// comparable. A cap holds the median still while the tail keeps moving,
    /// which is what makes it the honest thing to latch a cap against.
    p50_us: i64,
    /// Deadline miss ratio over that same set, when a deadline was supplied.
    miss_ratio: Option<f64>,
    fps_source: &'static str,
}

/// Prefer the shim's original NVAPI SIMULATION_START cadence for pre-FG FPS.
/// The native Vulkan marker cadence is the fallback for sessions without a
/// working shim stream. Every candidate is still rejected if its implied FPS
/// overshoots the independently measured raw-present cadence.
fn select_base_marker_frametime_p95(
    samples: &[&Sample],
    raw_avg_fps: Option<f64>,
    miss_deadline_us: Option<i64>,
) -> Option<BaseMarkerSelection> {
    let valid_stats = |frametimes: &[i64]| -> Option<FrametimeStats> {
        if frametimes.len() < METER_MIN_PRESENT_SAMPLES {
            return None;
        }
        let stats = frametime_stats(frametimes, miss_deadline_us)?;
        if let (Some(marker_fps), Some(raw)) = (fps_from_frametime(Some(stats.p95_us)), raw_avg_fps)
        {
            if marker_fps > raw * METER_MARKER_FPS_MAX_PRESENT_RATIO {
                return None;
            }
        }
        Some(stats)
    };

    let shim_frametimes: Vec<i64> = samples
        .iter()
        .filter(|s| {
            s.data.get("source").and_then(Value::as_str) == Some(NVAPI_MARKER_SOURCE)
                && s.data.get("marker_name").and_then(Value::as_str) == Some("simulation-start")
        })
        .map(|s| int_value(s.data.get("base_frame_frametime_us")))
        .filter(|&value| value > 0)
        .collect();
    if let Some(stats) = valid_stats(&shim_frametimes) {
        return Some(BaseMarkerSelection {
            p95_us: stats.p95_us,
            p50_us: stats.p50_us,
            miss_ratio: stats.miss_ratio,
            fps_source: "nvapi-base-frame-marker",
        });
    }

    let mut marker_frametimes: HashMap<String, Vec<i64>> = HashMap::new();
    for s in samples {
        if s.data.get("source").and_then(Value::as_str) == Some(NVAPI_MARKER_SOURCE) {
            continue;
        }
        let ft = int_value(s.data.get("base_frame_frametime_us"));
        if ft <= 0 {
            continue;
        }
        let name = s
            .data
            .get("marker_name")
            .and_then(Value::as_str)
            .filter(|n| !n.is_empty())
            .unwrap_or("unknown")
            .to_string();
        marker_frametimes.entry(name).or_default().push(ft);
    }

    for name in BASE_FRAME_MARKER_PRIORITY {
        if let Some(list) = marker_frametimes.get(name) {
            if let Some(stats) = valid_stats(list) {
                return Some(BaseMarkerSelection {
                    p95_us: stats.p95_us,
                    p50_us: stats.p50_us,
                    miss_ratio: stats.miss_ratio,
                    fps_source: "base-frame-marker",
                });
            }
        }
    }

    let mut eligible: Vec<(String, usize, FrametimeStats)> = marker_frametimes
        .iter()
        .filter_map(|(name, fts)| valid_stats(fts).map(|stats| (name.clone(), fts.len(), stats)))
        .collect();
    if eligible.is_empty() {
        return None;
    }
    // sort by (-len, name): most samples first, name ascending as tie-break.
    eligible.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    let best = &eligible[0].2;
    Some(BaseMarkerSelection {
        p95_us: best.p95_us,
        p50_us: best.p50_us,
        miss_ratio: best.miss_ratio,
        fps_source: "base-frame-marker",
    })
}

/// `_latency_proxy_p95` — the widest available click-to-photon proxy span, in
/// tier order. Under explicit frame generation, prefer the displayed-present
/// (out-of-band) spans that include the pacing hold. Only the p95 is consumed
/// (the tier label feeds the summary log, which we do not reproduce).
fn latency_proxy_from_samples(samples: &[&Sample], framegen_active: bool) -> Option<i64> {
    let tier_p95 = |key: &str, marker_proxy: Option<bool>| -> Option<i64> {
        let vals: Vec<i64> = samples
            .iter()
            .filter(|s| match marker_proxy {
                None => true,
                Some(mp) => (measurement(&s.data) == "marker-proxy") == mp,
            })
            .map(|s| int_value(s.data.get(key)))
            .collect();
        p95_us(&vals)
    };

    if framegen_active {
        for key in ["input_to_oob_present_us", "sim_to_oob_present_us"] {
            if let Some(p95) = tier_p95(key, None) {
                return Some(p95);
            }
        }
    }

    let tiers: [(&str, Option<bool>); 6] = [
        ("input_to_present_us", Some(false)),
        ("input_to_present_us", Some(true)),
        ("sim_to_present_us", None),
        ("sim_to_oob_present_us", None),
        ("submit_to_present_us", None),
        ("render_submit_us", None),
    ];
    for (key, marker_proxy) in tiers {
        if let Some(p95) = tier_p95(key, marker_proxy) {
            return Some(p95);
        }
    }
    None
}

fn latency_proxy_p95(samples: &[&Sample]) -> Option<i64> {
    // Frame-generation evidence lives on the layer's marker samples, not the
    // bridge's (which never assert it) — judge it across ALL samples so the
    // shim-preferred pass can still pick the displayed-present spans.
    let framegen_active = explicit_framegen_active(samples);
    let shim_samples: Vec<&Sample> = samples
        .iter()
        .copied()
        .filter(|s| {
            measurement(&s.data) == "marker-proxy"
                && s.data.get("source").and_then(Value::as_str) == Some(NVAPI_MARKER_SOURCE)
        })
        .collect();
    if let Some(value) = latency_proxy_from_samples(&shim_samples, framegen_active) {
        return Some(value);
    }

    let fallback_samples: Vec<&Sample> = samples
        .iter()
        .copied()
        .filter(|s| s.data.get("source").and_then(Value::as_str) != Some(NVAPI_MARKER_SOURCE))
        .collect();
    latency_proxy_from_samples(&fallback_samples, framegen_active)
}

// --- meter ------------------------------------------------------------------

struct Sample {
    data: Map<String, Value>,
    received_monotonic: f64,
}

/// Port of `LatencyTelemetryMeter`: a bounded ring of normalized timing samples
/// plus the cross-window base-present-FPS memory and driver-report dedup state.
struct Meter {
    samples: VecDeque<Sample>,
    last_base_present_fps: Option<f64>,
    // A wrapper session can have multiple telemetry PIDs: the NVAPI shim uses
    // Wine's PID namespace while the Vulkan layer uses the host PID. Session
    // identity keeps those complementary producers together and still rejects
    // a concurrently launched second game. PID ownership remains as a legacy
    // fallback for clients that do not yet publish a session ID.
    owner_session_id: Option<String>,
    legacy_owner_pid: Option<String>,
    last_owner_sample_monotonic: Option<f64>,
    // (pid, source, device, swapchain) → (max_present_id, duplicate_count).
    driver_report_present_ids: HashMap<String, (i64, i64)>,
}

impl Meter {
    fn new() -> Self {
        Meter {
            samples: VecDeque::new(),
            last_base_present_fps: None,
            owner_session_id: None,
            legacy_owner_pid: None,
            last_owner_sample_monotonic: None,
            driver_report_present_ids: HashMap::new(),
        }
    }

    /// `add_sample` — normalize, drop repeated driver reports (a stale Reflex ring
    /// re-presented in any order), and ring-append fresh ones.
    fn add_sample(&mut self, sample: Map<String, Value>) {
        if sample.get("type").and_then(Value::as_str) != Some("timing") {
            return;
        }
        let received = monotonic_now();
        let mut data = normalize_timing_sample(sample);
        if !self.accept_sample_owner(&data, received) {
            return;
        }
        let is_driver_report = measurement(&data) == "driver-report";
        if is_driver_report {
            self.synthesize_driver_report_duplicate_count(&mut data);
        }
        if is_driver_report && int_value(data.get("driver_report_duplicate_count")) > 0 {
            // Ignored duplicate: remembered by the Python summary path only, so we
            // simply drop it here (never aggregated).
            return;
        }
        if self.samples.len() == METER_MAX_SAMPLES {
            self.samples.pop_front();
        }
        self.samples.push_back(Sample {
            data,
            received_monotonic: received,
        });
    }

    fn accept_sample_owner(&mut self, data: &Map<String, Value>, received: f64) -> bool {
        if let Some(session_id) = pid_value(data.get("session_id")) {
            if self
                .owner_session_id
                .as_ref()
                .is_some_and(|owner| owner != &session_id)
                || self.legacy_owner_pid.is_some()
            {
                if !self.owner_is_stale(received) {
                    return false;
                }
                self.reset_owner_state();
            }
            if self.owner_session_id.is_none() {
                self.owner_session_id = Some(session_id);
            }
            self.last_owner_sample_monotonic = Some(received);
            return true;
        }

        if self.owner_session_id.is_some() {
            // The legacy fallback must be able to reclaim a stale session
            // owner, exactly like the two mismatch paths above; otherwise one
            // exited session-tagged game mutes every later legacy client for
            // the daemon's lifetime.
            if !self.owner_is_stale(received) {
                return false;
            }
            self.reset_owner_state();
        }
        if let Some(sample_pid) = pid_value(data.get("pid")) {
            if self
                .legacy_owner_pid
                .as_ref()
                .is_some_and(|owner| owner != &sample_pid)
            {
                if !self.owner_is_stale(received) {
                    return false;
                }
                self.reset_owner_state();
            }
            if self.legacy_owner_pid.is_none() {
                self.legacy_owner_pid = Some(sample_pid);
            }
            self.last_owner_sample_monotonic = Some(received);
        }
        true
    }

    fn owner_is_stale(&self, now: f64) -> bool {
        self.last_owner_sample_monotonic
            .is_none_or(|last| now - last > METER_SAMPLE_MAX_AGE_S)
    }

    fn reset_owner_state(&mut self) {
        self.samples.clear();
        self.last_base_present_fps = None;
        self.owner_session_id = None;
        self.legacy_owner_pid = None;
        self.last_owner_sample_monotonic = None;
        self.driver_report_present_ids.clear();
    }

    fn synthesize_driver_report_duplicate_count(&mut self, sample: &mut Map<String, Value>) {
        let present_id = int_value(sample.get("present_id"));
        if present_id == 0 {
            sample
                .entry("driver_report_duplicate_count".to_string())
                .or_insert_with(|| Value::from(0));
            return;
        }
        let key = driver_report_key(sample);
        let (mut max_present_id, mut duplicate_count) = self
            .driver_report_present_ids
            .get(&key)
            .copied()
            .unwrap_or((0, 0));
        if max_present_id != 0
            && present_id <= max_present_id
            && max_present_id - present_id <= DRIVER_REPORT_RING_SPAN
        {
            duplicate_count += 1;
        } else {
            duplicate_count = 0;
            max_present_id = present_id;
        }
        self.driver_report_present_ids
            .insert(key, (max_present_id, duplicate_count));
        let provided = int_value(sample.get("driver_report_duplicate_count"));
        sample.insert(
            "driver_report_duplicate_count".to_string(),
            Value::from(provided.max(duplicate_count)),
        );
    }

    /// `snapshot(now)` — the fresh-window aggregation the engine consumes. `None`
    /// when there are no samples within `METER_SAMPLE_MAX_AGE_S` of `now`.
    fn snapshot(&mut self, now: f64, miss_deadline_us: Option<i64>) -> Option<LatencySnapshot> {
        if self.samples.is_empty() {
            return None;
        }
        let samples: Vec<&Sample> = self
            .samples
            .iter()
            .filter(|s| now - s.received_monotonic <= METER_SAMPLE_MAX_AGE_S)
            .collect();
        if samples.is_empty() {
            return None;
        }

        let present_frametime_values: Vec<i64> = samples
            .iter()
            .map(|s| int_value(s.data.get("present_frametime_us")))
            .collect();
        let present_frametimes: Vec<i64> = present_frametime_values
            .iter()
            .copied()
            .filter(|&v| v > 0)
            .collect();
        let present_frametime_p95 = if present_frametimes.len() >= METER_MIN_PRESENT_SAMPLES {
            p95_us(&present_frametimes)
        } else {
            None
        };
        let raw_present_avg_fps = mean_fps(&present_frametimes);
        // `_present_fps_stats["avg"]`: "n/a" below the minimum sample count.
        let raw_present_fps_stats_avg = if present_frametimes.len() >= METER_MIN_PRESENT_SAMPLES {
            format_fps_value(raw_present_avg_fps)
        } else {
            "n/a".to_string()
        };

        let marker_selection =
            select_base_marker_frametime_p95(&samples, raw_present_avg_fps, miss_deadline_us);
        // The presented-frame median, kept whatever the marker path decided:
        // it is the only median a game without markers can offer.
        let present_pacing_p50_us = frametime_stats(&present_frametimes, None).map(|s| s.p50_us);
        let base_present_frametime_p95_us;
        // Only reported when they come off the same accepted marker set as the
        // p95; the present-pacing fallback has no comparable set.
        let mut base_present_frametime_p50_us = None;
        let mut base_present_frametime_miss_ratio = None;
        let present_fps_value;
        let cadence_is_independent;
        let fps_source;
        if let Some(marker) = marker_selection {
            base_present_frametime_p95_us = Some(marker.p95_us);
            base_present_frametime_p50_us = Some(marker.p50_us);
            base_present_frametime_miss_ratio = marker.miss_ratio;
            present_fps_value = fps_from_frametime(Some(marker.p95_us));
            self.last_base_present_fps = present_fps_value;
            cadence_is_independent = true;
            fps_source = marker.fps_source;
        } else {
            let (fps_val, deinterlaced) = estimate_base_present_fps(
                &mut self.last_base_present_fps,
                fps_from_frametime(present_frametime_p95),
                raw_present_avg_fps,
                has_marker_stream(&samples),
                explicit_framegen_active(&samples),
            );
            present_fps_value = fps_val;
            cadence_is_independent = deinterlaced;
            fps_source = if fps_val.is_none() {
                "none"
            } else if deinterlaced {
                "present-pacing-deinterlaced"
            } else {
                "present-pacing"
            };
            base_present_frametime_p95_us = match fps_val {
                Some(v) if v != 0.0 => Some(round_half_even(1_000_000.0 / v) as i64),
                _ => None,
            };
        }

        let framegen_active = framegen_active_for_overlay(
            &samples,
            present_fps_value,
            raw_present_avg_fps,
            cadence_is_independent,
        );
        let latency_proxy = latency_proxy_p95(&samples);
        let display_latency = p95_us(
            &samples
                .iter()
                .map(|s| int_value(s.data.get("display_latency_us")))
                .collect::<Vec<_>>(),
        );
        let pid = samples
            .iter()
            .rev()
            .filter(|s| positive_us(&s.data, "present_frametime_us"))
            .find_map(|s| pid_value(s.data.get("pid")));
        let pid = pid.or_else(|| {
            samples
                .iter()
                .rev()
                .find_map(|s| pid_value(s.data.get("pid")))
        });
        let session_id = samples
            .iter()
            .rev()
            .find_map(|s| pid_value(s.data.get("session_id")));

        Some(LatencySnapshot {
            base_present_frametime_p95_ms: base_present_frametime_p95_us.map(|v| v as f64 / 1000.0),
            base_present_frametime_p50_ms: base_present_frametime_p50_us.map(|v| v as f64 / 1000.0),
            present_pacing_p50_ms: present_pacing_p50_us.map(|v| v as f64 / 1000.0),
            base_present_frametime_miss_ratio,
            present_fps: Some(format_fps_value(present_fps_value)),
            fps_source: Some(fps_source.to_string()),
            raw_present_fps_stats_avg: Some(raw_present_fps_stats_avg),
            raw_present_fps_avg: raw_present_avg_fps,
            framegen_active: Some(if framegen_active { "true" } else { "false" }.to_string()),
            latency_p95_ms: latency_proxy.map(|v| v as f64 / 1000.0),
            display_latency_p95_ms: display_latency.map(|v| v as f64 / 1000.0),
            pid,
            session_id,
        })
    }
}

/// `_estimate_base_present_fps` — recover the base render FPS, dividing out a
/// detected frame-generation multiplier only with marker or generated-frame
/// evidence. Operates on
/// the meter's `last_base` cell directly (disjoint from the borrowed sample view).
fn estimate_base_present_fps(
    last_base: &mut Option<f64>,
    p95_fps: Option<f64>,
    raw_avg_fps: Option<f64>,
    marker_stream: bool,
    framegen_active: bool,
) -> (Option<f64>, bool) {
    let Some(p95_fps) = p95_fps else {
        return (None, false);
    };
    let seed = *last_base;
    // Present pacing is usable from the first window, at any framerate. A
    // high rate alone is not evidence of generated frames. Real base-frame
    // markers take priority in snapshot() whenever the game supplies them.
    let inferred = infer_framegen_multiplier(raw_avg_fps, seed);
    let mut base_fps = p95_fps;
    let mut deinterlaced = false;
    if marker_stream || framegen_active {
        if let (Some(last), Some(raw)) = (seed, raw_avg_fps) {
            if p95_fps > last * 1.6 && raw != 0.0 {
                if let Some(mult) = inferred {
                    base_fps = raw / mult as f64;
                    deinterlaced = true;
                }
            }
        }
    }
    // Known generated output is not a base-frame measurement. If markers are
    // missing and the prior base cadence cannot recover it, keep it unknown
    // instead of seeding future decisions with the displayed framerate.
    if framegen_active && !deinterlaced {
        return (None, false);
    }
    *last_base = Some(base_fps);
    (Some(base_fps), deinterlaced)
}

/// `_infer_framegen_multiplier` — the raw/base ratio rounds to a 2/3/4× integer
/// within a tolerance, else `None`.
fn infer_framegen_multiplier(raw_avg_fps: Option<f64>, last_base_fps: Option<f64>) -> Option<i64> {
    let (Some(raw), Some(last)) = (raw_avg_fps, last_base_fps) else {
        return None;
    };
    if raw == 0.0 || last == 0.0 {
        return None;
    }
    let ratio = raw / last;
    let nearest = round_half_even(ratio) as i64;
    if !matches!(nearest, 2..=4) {
        return None;
    }
    if (ratio - nearest as f64).abs() > 0.45 {
        return None;
    }
    Some(nearest)
}

/// Hashable form of the Python `(pid, source, device, swapchain)` tuple key.
fn driver_report_key(sample: &Map<String, Value>) -> String {
    let field = |k: &str| {
        sample
            .get(k)
            .map(|v| v.to_string())
            .unwrap_or_else(|| "null".to_string())
    };
    format!(
        "{}\u{1}{}\u{1}{}\u{1}{}",
        field("pid"),
        field("source"),
        field("device"),
        field("swapchain")
    )
}

// --- socket path resolution (sockets.py) ------------------------------------

fn getuid() -> u32 {
    // SAFETY: getuid is always safe.
    unsafe { libc::getuid() }
}

/// `latency_socket_path(env)` — the primary (XDG / root-runtime / tmp) socket.
fn latency_socket_path() -> PathBuf {
    if let Some(explicit) = nonempty_env("PENGUIN_BURNER_LATENCY_SOCKET") {
        return PathBuf::from(shellexpand_home(&explicit));
    }
    if let Some(runtime) = nonempty_env("XDG_RUNTIME_DIR") {
        return PathBuf::from(runtime)
            .join("penguin-burner")
            .join("latency.sock");
    }
    // Root-only /run/user probe uses the REAL uid (see run_user_penguin_dir).
    if let Some(dir) = run_user_penguin_dir(getuid() == 0) {
        return dir.join("latency.sock");
    }
    PathBuf::from(format!("/tmp/penguin-burner-latency-{}.sock", getuid()))
}

/// `_home_latency_socket_path(env)` — the `~/.cache/penguin-burner` socket the
/// game launcher pins into the game process (the load-bearing path).
fn home_latency_socket_path() -> Option<PathBuf> {
    if let Some(cache) = desktop_cache_dir() {
        return Some(cache.join("latency.sock"));
    }
    // Last resort: a cwd ancestor directly under /home/<user>. (The Python code
    // also scans the module's source path; a compiled binary has no analogue, and
    // this branch is unreachable when SUDO_* is set — as it always is here.)
    if let Ok(cwd) = std::env::current_dir() {
        let mut current: Option<&Path> = Some(cwd.as_path());
        while let Some(dir) = current {
            if dir.parent() == Some(Path::new("/home")) {
                return Some(cache_socket(dir));
            }
            current = dir.parent();
        }
    }
    None
}

fn cache_socket(home: &Path) -> PathBuf {
    home.join(".cache")
        .join("penguin-burner")
        .join("latency.sock")
}

fn shellexpand_home(path: &str) -> String {
    if let Some(rest) = path.strip_prefix("~/") {
        if let Ok(home) = std::env::var("HOME") {
            return format!("{home}/{rest}");
        }
    }
    path.to_string()
}

/// `latency_socket_paths(env)` — the primary path plus (unless a socket is pinned
/// explicitly) the home-cache path, deduplicated in order.
fn latency_socket_paths() -> Vec<PathBuf> {
    let mut paths = vec![latency_socket_path()];
    if nonempty_env("PENGUIN_BURNER_LATENCY_SOCKET").is_none() {
        if let Some(home_path) = home_latency_socket_path() {
            paths.push(home_path);
        }
    }
    let mut unique: Vec<PathBuf> = Vec::new();
    for path in paths {
        if !unique.iter().any(|p| p == &path) {
            unique.push(path);
        }
    }
    unique
}

// --- socket receiver (LatencyTelemetryLogger) -------------------------------

/// The running latency telemetry receiver: bound sockets drained by a background
/// thread into a shared [`Meter`]. `snapshot` is polled by the engine loop; the
/// sockets/thread are torn down (and the socket files unlinked) on drop.
pub(super) struct LatencyReceiver {
    meter: Arc<Mutex<Meter>>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
    paths: Vec<PathBuf>,
    closed: bool,
}

impl LatencyReceiver {
    /// `start_latency_telemetry_logger` — bind every resolved socket path and
    /// spawn the drain thread. Any bind failure logs and returns `None` (the
    /// engine then holds tier / omits fps like the Python `None` meter).
    pub(super) fn start(log: &mut dyn FnMut(&str)) -> Option<LatencyReceiver> {
        Self::start_at(latency_socket_paths(), log)
    }

    /// Bind an explicit set of paths (production passes the resolved set; tests
    /// pass a temp path to avoid touching real socket locations).
    pub(super) fn start_at(
        paths: Vec<PathBuf>,
        log: &mut dyn FnMut(&str),
    ) -> Option<LatencyReceiver> {
        let mut sockets: Vec<UnixDatagram> = Vec::new();
        for path in &paths {
            match bind_socket(path) {
                Ok(sock) => sockets.push(sock),
                Err(err) => {
                    // Unlink only the paths WE bound (one per socket, in order).
                    // A not-yet-attempted path may be a live socket owned by a
                    // previous binder and must be left alone.
                    let bound = sockets.len();
                    drop(sockets);
                    for path in paths.iter().take(bound) {
                        let _ = std::fs::remove_file(path);
                    }
                    log(&format!("Latency telemetry unavailable: {err}"));
                    return None;
                }
            }
        }
        for path in &paths {
            log(&format!("Latency telemetry socket: {}", path.display()));
        }
        let meter = Arc::new(Mutex::new(Meter::new()));
        let stop = Arc::new(AtomicBool::new(false));
        let thread_meter = meter.clone();
        let thread_stop = stop.clone();
        let thread = std::thread::Builder::new()
            .name("penguin-burnerd-latency".to_string())
            .spawn(move || run_receiver(sockets, &thread_meter, &thread_stop))
            .ok()?;
        Some(LatencyReceiver {
            meter,
            stop,
            thread: Some(thread),
            paths,
            closed: false,
        })
    }

    /// `latency_meter.snapshot(now=loop_started)` — aggregate the current window.
    pub(super) fn snapshot(
        &self,
        now: f64,
        miss_deadline_us: Option<i64>,
    ) -> Option<LatencySnapshot> {
        let mut meter = self
            .meter
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        meter.snapshot(now, miss_deadline_us)
    }

    fn close(&mut self) {
        if self.closed {
            return;
        }
        self.closed = true;
        self.stop.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            // The drain thread wakes at least every poll interval; bound the join
            // defensively (mirrors the Python 2s join timeout) so shutdown never
            // wedges on a stuck thread.
            let deadline = Instant::now() + Duration::from_secs(2);
            while !thread.is_finished() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(20));
            }
            if thread.is_finished() {
                let _ = thread.join();
            }
        }
        for path in &self.paths {
            let _ = std::fs::remove_file(path);
        }
    }
}

impl Drop for LatencyReceiver {
    fn drop(&mut self) {
        self.close();
    }
}

const RECEIVE_BUF_LEN: usize = 8192;
const POLL_TIMEOUT_MS: libc::c_int = 500;

fn bind_socket(path: &Path) -> std::io::Result<UnixDatagram> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
        claim_desktop_user_ownership(parent, true);
    }
    if path.exists() {
        let _ = std::fs::remove_file(path);
    }
    let sock = UnixDatagram::bind(path)?;
    // A post-bind failure would otherwise leave this freshly-created socket file
    // on disk (start_at only unlinks paths it recorded as bound), so clean up
    // our own file before propagating the error.
    if let Err(err) = sock.set_nonblocking(true) {
        let _ = std::fs::remove_file(path);
        return Err(err);
    }
    chmod_666(path);
    claim_desktop_user_ownership(path, false);
    Ok(sock)
}

fn chmod_666(path: &Path) {
    if let Some(c) = cpath(path) {
        // SAFETY: chmod on a valid path; failures are ignored (best-effort, as in
        // the Python `try/except OSError`).
        unsafe {
            libc::chmod(c.as_ptr(), 0o666);
        }
    }
}

/// The drain thread: `select`-style poll across the bound sockets, draining each
/// readable one into the shared meter until it would block.
fn run_receiver(sockets: Vec<UnixDatagram>, meter: &Arc<Mutex<Meter>>, stop: &AtomicBool) {
    let mut buf = [0u8; RECEIVE_BUF_LEN];
    let mut pollfds: Vec<libc::pollfd> = sockets
        .iter()
        .map(|s| libc::pollfd {
            fd: s.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        })
        .collect();
    while !stop.load(Ordering::SeqCst) {
        for pfd in &mut pollfds {
            pfd.revents = 0;
        }
        // SAFETY: pollfds is a valid, correctly-sized array of owned fds.
        let rc = unsafe {
            libc::poll(
                pollfds.as_mut_ptr(),
                pollfds.len() as libc::nfds_t,
                POLL_TIMEOUT_MS,
            )
        };
        if rc < 0 {
            // A persistent poll error (EBADF/EINVAL/ENOMEM) returns instantly
            // and would busy-spin this loop at 100% CPU; back off before the
            // re-poll. EINTR just re-checks the stop flag.
            let err = std::io::Error::last_os_error();
            if err.raw_os_error() != Some(libc::EINTR) {
                std::thread::sleep(Duration::from_millis(100));
            }
            continue;
        }
        if rc == 0 {
            continue; // timeout — re-check the stop flag.
        }
        let mut drained = false;
        for (i, sock) in sockets.iter().enumerate() {
            if pollfds[i].revents & libc::POLLIN != 0 {
                receive_available(sock, &mut buf, meter);
                drained = true;
            }
        }
        if !drained {
            // rc > 0 with no POLLIN means error-only revents (POLLERR/POLLHUP/
            // POLLNVAL), which poll() reports immediately — back off so the
            // loop cannot busy-spin on a persistently errored fd.
            std::thread::sleep(Duration::from_millis(100));
        }
    }
}

fn receive_available(sock: &UnixDatagram, buf: &mut [u8], meter: &Arc<Mutex<Meter>>) {
    loop {
        let n = match sock.recv(buf) {
            Ok(n) => n,
            Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => return,
            Err(_) => return,
        };
        // `errors="replace"` decode, then `json.loads`; malformed lines are
        // skipped without killing the receiver.
        let text = String::from_utf8_lossy(&buf[..n]);
        let value: Value = match serde_json::from_str(&text) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let Value::Object(map) = value else {
            continue; // non-dict payloads ignored, like the Python `isinstance`.
        };
        // Status frames feed only the (unreproduced) diagnostic log; non-timing
        // frames are dropped. Only timing frames reach the meter.
        if map.get("type").and_then(Value::as_str) != Some("timing") {
            continue;
        }
        meter
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
            .add_sample(map);
    }
}

// ============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::atomic::{AtomicU32, Ordering as AtomicOrdering};
    use std::time::Instant;

    fn obj(value: Value) -> Map<String, Value> {
        value.as_object().expect("object").clone()
    }

    fn approx(a: f64, b: f64) -> bool {
        (a - b).abs() < 1e-9
    }

    /// Feed a fresh window of samples into a new meter and snapshot it "now".
    fn snapshot_of(samples: Vec<Value>) -> Option<LatencySnapshot> {
        let mut meter = Meter::new();
        for s in samples {
            meter.add_sample(obj(s));
        }
        meter.snapshot(monotonic_now(), None)
    }

    fn timing(extra: Value) -> Value {
        let mut map = obj(extra);
        map.insert("type".to_string(), Value::from("timing"));
        Value::Object(map)
    }

    // --- `_int_value` parity table (samples.py) ------------------------------

    #[test]
    fn int_value_matches_python() {
        assert_eq!(int_value(None), 0);
        assert_eq!(int_value(Some(&Value::Null)), 0);
        assert_eq!(int_value(Some(&json!(true))), 1);
        assert_eq!(int_value(Some(&json!(false))), 0);
        assert_eq!(int_value(Some(&json!(16600))), 16600);
        assert_eq!(int_value(Some(&json!(-12.9))), -12); // trunc toward zero
        assert_eq!(int_value(Some(&json!(12.9))), 12);
        assert_eq!(int_value(Some(&json!("123"))), 123);
        assert_eq!(int_value(Some(&json!("12.5"))), 0); // int("12.5") → ValueError
        assert_eq!(int_value(Some(&json!(""))), 0);
        assert_eq!(int_value(Some(&json!([1, 2]))), 0);
    }

    // --- `_p95_us` index selection (banker's round) --------------------------

    #[test]
    fn p95_index_cases() {
        assert_eq!(p95_us(&[]), None);
        assert_eq!(p95_us(&[0, -5]), None); // all non-positive
                                            // n=6, idx = round(5*0.95)=round(4.75)=5 → largest.
        assert_eq!(p95_us(&[10, 60, 20, 50, 30, 40]), Some(60));
        // n=1 → idx 0.
        assert_eq!(p95_us(&[42]), Some(42));
        // Zeros are filtered before ranking.
        assert_eq!(p95_us(&[0, 0, 10, 20, 30, 40]), Some(40));
    }

    #[test]
    fn format_fps_value_floors_half_up() {
        assert_eq!(format_fps_value(Some(59.808)), "60");
        assert_eq!(format_fps_value(Some(49.5)), "50"); // floor(50.0)
        assert_eq!(format_fps_value(Some(49.49)), "49");
        assert_eq!(format_fps_value(None), "n/a");
        assert_eq!(format_fps_value(Some(0.0)), "n/a");
        assert_eq!(format_fps_value(Some(-5.0)), "n/a");
    }

    #[test]
    fn mean_fps_matches() {
        assert_eq!(mean_fps(&[]), None);
        assert!(approx(
            mean_fps(&[16600, 16700]).unwrap(),
            2.0 * 1e6 / 33300.0
        ));
    }

    // --- `normalize_timing_sample` (samples.py) ------------------------------

    #[test]
    fn normalize_infers_driver_report_and_spans() {
        let s = obj(json!({
            "type": "timing",
            "source": "dxvk-nvapi-vkreflex",
            "timing_count": 1,
            "gpu_render_start_us": 100,
            "gpu_render_end_us": 250,
            "present_start_us": 300,
        }));
        let n = normalize_timing_sample(s);
        assert_eq!(measurement(&n), "driver-report");
        assert_eq!(int_value(n.get("gpu_render_us")), 150); // 250-100
                                                            // render_present_us needs present_start<gpu_render_end; 300>250 → not set.
        assert_eq!(int_value(n.get("render_present_us")), 0);
    }

    #[test]
    fn normalize_leaves_non_timing_untouched() {
        let s = obj(json!({"type": "status", "source": "dxvk-nvapi-vkreflex", "timing_count": 1}));
        let n = normalize_timing_sample(s);
        assert_eq!(measurement(&n), "");
    }

    // --- staleness window ----------------------------------------------------

    #[test]
    fn stale_samples_yield_none() {
        let mut meter = Meter::new();
        for us in [16600, 16700, 16650, 16680] {
            meter.add_sample(obj(timing(json!({"pid": 1, "present_frametime_us": us}))));
        }
        let received = meter.samples.back().unwrap().received_monotonic;
        // 4s past the newest sample → outside the 3s window.
        assert!(meter.snapshot(received + 4.0, None).is_none());
        // Still fresh at receipt time.
        assert!(meter.snapshot(received, None).is_some());
    }

    #[test]
    fn empty_meter_snapshot_none() {
        assert!(Meter::new().snapshot(monotonic_now(), None).is_none());
    }

    #[test]
    fn meter_keeps_first_game_pid_as_telemetry_owner() {
        let mut meter = Meter::new();
        for us in [16600, 16700, 16650, 16680] {
            meter.add_sample(obj(timing(json!({
                "pid": 111,
                "present_frametime_us": us,
            }))));
        }
        for us in [8000, 8100, 7900, 8050] {
            meter.add_sample(obj(timing(json!({
                "pid": 222,
                "present_frametime_us": us,
            }))));
        }

        assert_eq!(meter.samples.len(), 4);
        let snapshot = meter.snapshot(monotonic_now(), None).unwrap();
        assert_eq!(snapshot.pid.as_deref(), Some("111"));
        assert_eq!(snapshot.present_fps.as_deref(), Some("60"));
    }

    #[test]
    fn meter_merges_complementary_pids_in_one_wrapper_session() {
        let mut meter = Meter::new();
        for _ in 0..6 {
            meter.add_sample(obj(timing(json!({
                "session_id": "700",
                "pid": 312,
                "measurement": "base-frame-marker-pacing",
                "source": NVAPI_MARKER_SOURCE,
                "marker_name": "simulation-start",
                "base_frame_frametime_us": 20000,
            }))));
            meter.add_sample(obj(timing(json!({
                "session_id": "700",
                "pid": 219896,
                "measurement": "present-pacing",
                "present_frametime_us": 8333,
            }))));
        }

        let snapshot = meter.snapshot(monotonic_now(), None).unwrap();
        assert_eq!(meter.samples.len(), 12);
        assert_eq!(snapshot.session_id.as_deref(), Some("700"));
        assert_eq!(snapshot.pid.as_deref(), Some("219896"));
        assert_eq!(snapshot.present_fps.as_deref(), Some("50"));
        assert_eq!(
            snapshot.fps_source.as_deref(),
            Some("nvapi-base-frame-marker")
        );
        assert_eq!(snapshot.raw_present_fps_stats_avg.as_deref(), Some("120"));
        assert_eq!(snapshot.framegen_active.as_deref(), Some("true"));
    }

    #[test]
    fn meter_rejects_a_different_live_wrapper_session() {
        let mut meter = Meter::new();
        meter.add_sample(obj(timing(json!({
            "session_id": "700",
            "pid": 312,
            "present_frametime_us": 16600,
        }))));
        meter.add_sample(obj(timing(json!({
            "session_id": "701",
            "pid": 313,
            "present_frametime_us": 8000,
        }))));

        assert_eq!(meter.samples.len(), 1);
        assert_eq!(
            meter.samples[0]
                .data
                .get("session_id")
                .and_then(Value::as_str),
            Some("700")
        );
    }

    #[test]
    fn legacy_pid_reclaims_a_stale_session_owner() {
        let mut meter = Meter::new();
        let session = obj(timing(json!({"session_id": "700", "pid": 312})));
        let legacy = obj(timing(json!({"pid": 999})));

        assert!(meter.accept_sample_owner(&session, 100.0));
        // A live session owner still rejects untagged samples...
        assert!(!meter.accept_sample_owner(&legacy, 101.0));
        // ...but a stale one is evicted, like on every other ownership path.
        assert!(meter.accept_sample_owner(&legacy, 101.0 + METER_SAMPLE_MAX_AGE_S + 1.0));
        assert!(meter.owner_session_id.is_none());
        assert_eq!(meter.legacy_owner_pid.as_deref(), Some("999"));
    }

    #[test]
    fn framegen_evidence_from_layer_samples_selects_oob_span_for_shim_latency() {
        let mut samples = Vec::new();
        for i in 0..6 {
            samples.push(timing(json!({
                "session_id": "700",
                "pid": 312,
                "measurement": "marker-proxy",
                "source": NVAPI_MARKER_SOURCE,
                "sim_to_present_us": 18000 + i * 100,
                "sim_to_oob_present_us": 42000 + i * 100,
            })));
        }
        // Frame-generation evidence arrives on the layer's own marker samples
        // (the bridge never asserts it); it must still steer the shim-preferred
        // pass to the displayed-present span.
        samples.push(timing(json!({
            "session_id": "700",
            "pid": 219896,
            "marker_name": "oob-present-start",
        })));

        let snapshot = snapshot_of(samples).unwrap();
        assert!(approx(snapshot.latency_p95_ms.unwrap(), 42.5));
    }

    #[test]
    fn shim_latency_samples_win_over_native_semantic_duplicates() {
        let mut samples = Vec::new();
        for i in 0..6 {
            samples.push(timing(json!({
                "session_id": "700",
                "pid": 312,
                "measurement": "marker-proxy",
                "source": NVAPI_MARKER_SOURCE,
                "present_frametime_us": 16600,
                "sim_to_present_us": 20000 + i * 100,
            })));
            samples.push(timing(json!({
                "session_id": "700",
                "pid": 219896,
                "measurement": "marker-proxy",
                "source": "native-layer",
                "sim_to_present_us": 80000 + i * 100,
            })));
        }

        let snapshot = snapshot_of(samples).unwrap();
        assert!(approx(snapshot.latency_p95_ms.unwrap(), 20.5));
    }

    // --- parity fixtures (generated by running the Python meter directly) -----
    // Generator: `PYTHONPATH=/home/jp/PenguinBurner python3 gen_fixtures.py`
    // constructing `LatencyTelemetryMeter(time_monotonic=lambda: 100.0)`, adding
    // the samples below, and projecting `snapshot(now=...)`.

    #[test]
    fn fixture_present_pacing_60() {
        let s = snapshot_of(
            [16600, 16700, 16650, 16680, 16720, 16600]
                .iter()
                .map(|us| timing(json!({"pid": 4242, "present_frametime_us": us})))
                .collect(),
        )
        .unwrap();
        assert!(approx(s.base_present_frametime_p95_ms.unwrap(), 16.72));
        assert_eq!(s.present_fps.as_deref(), Some("60"));
        assert_eq!(s.fps_source.as_deref(), Some("present-pacing"));
        assert_eq!(s.raw_present_fps_stats_avg.as_deref(), Some("60"));
        assert!(approx(
            s.raw_present_fps_avg.unwrap(),
            60.030_015_007_503_75
        ));
        assert_eq!(s.framegen_active.as_deref(), Some("false"));
        assert!(s.latency_p95_ms.is_none());
        assert!(s.display_latency_p95_ms.is_none());
        assert_eq!(s.pid.as_deref(), Some("4242"));
    }

    #[test]
    fn fixture_present_pacing_under_min() {
        let s = snapshot_of(
            [16600, 16700, 16650]
                .iter()
                .map(|us| timing(json!({"pid": 7, "present_frametime_us": us})))
                .collect(),
        )
        .unwrap();
        assert!(s.base_present_frametime_p95_ms.is_none());
        assert_eq!(s.present_fps.as_deref(), Some("n/a"));
        assert_eq!(s.fps_source.as_deref(), Some("none"));
        assert_eq!(s.raw_present_fps_stats_avg.as_deref(), Some("n/a"));
        assert!(approx(
            s.raw_present_fps_avg.unwrap(),
            60.060_060_060_060_06
        ));
        assert_eq!(s.framegen_active.as_deref(), Some("false"));
        assert_eq!(s.pid.as_deref(), Some("7"));
    }

    #[test]
    fn fixture_base_frame_marker() {
        let s = snapshot_of(
            [20000, 20100, 19900, 20050, 20000, 20200]
                .iter()
                .map(|us| {
                    timing(json!({
                        "pid": 999,
                        "present_frametime_us": 8300,
                        "base_frame_frametime_us": us,
                        "marker_name": "present-start",
                        "marker_bits": 64,
                    }))
                })
                .collect(),
        )
        .unwrap();
        assert!(approx(s.base_present_frametime_p95_ms.unwrap(), 20.2));
        assert_eq!(s.present_fps.as_deref(), Some("50"));
        assert_eq!(s.fps_source.as_deref(), Some("base-frame-marker"));
        assert_eq!(s.raw_present_fps_stats_avg.as_deref(), Some("120"));
        assert!(approx(
            s.raw_present_fps_avg.unwrap(),
            120.481_927_710_843_38
        ));
        assert_eq!(s.framegen_active.as_deref(), Some("true"));
        assert_eq!(s.pid.as_deref(), Some("999"));
    }

    #[test]
    fn fixture_latency_proxy_and_display() {
        let s = snapshot_of(
            (0..6)
                .map(|i| {
                    timing(json!({
                        "pid": 555,
                        "present_frametime_us": 16600,
                        "input_to_present_us": 22000 + i * 100,
                        "display_latency_us": 4000 + i * 10,
                    }))
                })
                .collect(),
        )
        .unwrap();
        assert!(approx(s.base_present_frametime_p95_ms.unwrap(), 16.6));
        assert!(approx(s.latency_p95_ms.unwrap(), 22.5));
        assert!(approx(s.display_latency_p95_ms.unwrap(), 4.05));
        assert_eq!(s.pid.as_deref(), Some("555"));
    }

    #[test]
    fn fixture_driver_report_duplicates_suppressed() {
        let mut samples: Vec<Value> = (0..6)
            .map(|_| timing(json!({"pid": 3, "present_frametime_us": 16600})))
            .collect();
        // Three same-present_id driver reports: only the first is fresh + kept.
        for _ in 0..3 {
            samples.push(timing(json!({
                "pid": 3,
                "source": "dxvk-nvapi-vkreflex",
                "present_id": 100,
                "timing_count": 1,
                "input_to_present_us": 99000,
            })));
        }
        let mut meter = Meter::new();
        for s in samples {
            meter.add_sample(obj(s));
        }
        // 6 present samples + exactly 1 retained driver report.
        assert_eq!(meter.samples.len(), 7);
        let s = meter.snapshot(monotonic_now(), None).unwrap();
        assert!(approx(s.base_present_frametime_p95_ms.unwrap(), 16.6));
        assert!(approx(s.latency_p95_ms.unwrap(), 99.0));
        assert!(s.display_latency_p95_ms.is_none());
        assert_eq!(s.pid.as_deref(), Some("3"));
    }

    #[test]
    fn present_pacing_starts_at_any_framerate_without_a_seed() {
        // A game's first samples are usable without first visiting a slow menu.
        // Marker availability alone must not impose a framerate ceiling either.
        for frametime_us in [33333, 16667, 10000, 8333, 6944, 4167, 2000] {
            for marker_bits in [0, 64] {
                let snapshot = snapshot_of(
                    (0..6)
                        .map(|_| {
                            timing(json!({
                                "pid": 8,
                                "measurement": "present-pacing",
                                "present_frametime_us": frametime_us,
                                "marker_bits": marker_bits,
                            }))
                        })
                        .collect(),
                )
                .unwrap();
                assert_eq!(
                    snapshot.base_present_frametime_p95_ms,
                    Some(frametime_us as f64 / 1000.0),
                    "frametime={frametime_us}, marker_bits={marker_bits}",
                );
                assert_eq!(snapshot.fps_source.as_deref(), Some("present-pacing"));
                assert_eq!(snapshot.framegen_active.as_deref(), Some("false"));
            }
        }
    }

    #[test]
    fn real_fps_above_target_eases_down_from_performance() {
        use crate::profile::adaptive::{
            AdaptiveProfileController, FrametimeReadings, MedianSource, PolicyConfig,
        };

        let tiers = vec!["efficiency".into(), "balanced".into(), "performance".into()];
        for (target_fps, frametime_us) in [(60.0, 8333), (90.0, 5556), (144.0, 3472)] {
            let mut meter = Meter::new();
            let mut controller = AdaptiveProfileController::new(
                "performance",
                PolicyConfig::for_target_fps(target_fps),
            );
            let mut decision = None;
            for tick in 0..=120 {
                meter.samples.clear();
                for _ in 0..6 {
                    meter.add_sample(obj(timing(json!({
                        "pid": 8,
                        "measurement": "present-pacing",
                        "present_frametime_us": frametime_us,
                    }))));
                }
                let snapshot = meter.snapshot(monotonic_now(), None).unwrap();
                let readings = FrametimeReadings {
                    p95_ms: snapshot.base_present_frametime_p95_ms,
                    p50_ms: snapshot.present_pacing_p50_ms,
                    p50_source: MedianSource::Pacing,
                    miss_ratio: None,
                };
                decision = Some(controller.update(
                    readings,
                    &tiers,
                    100.0 + tick as f64,
                    Some(38.0),
                    Some(20.0),
                    Some(35.0),
                ));
            }
            assert_eq!(decision.unwrap().tier, "efficiency", "target={target_fps}");
        }
    }

    #[test]
    fn reflex_base_fps_wins_over_generated_output_at_startup() {
        for base_us in [25000, 8333, 4167] {
            let snapshot = snapshot_of(
                (0..6)
                    .map(|_| {
                        timing(json!({
                            "pid": 8,
                            "source": NVAPI_MARKER_SOURCE,
                            "marker_name": "simulation-start",
                            "base_frame_frametime_us": base_us,
                            "present_frametime_us": base_us / 2,
                        }))
                    })
                    .collect(),
            )
            .unwrap();
            assert_eq!(
                snapshot.base_present_frametime_p95_ms,
                Some(base_us as f64 / 1000.0)
            );
            assert_eq!(
                snapshot.fps_source.as_deref(),
                Some("nvapi-base-frame-marker")
            );
            assert_eq!(snapshot.framegen_active.as_deref(), Some("true"));
        }
    }

    #[test]
    fn generated_output_without_base_timing_is_not_used_as_real_fps() {
        for frametime_us in [33333, 16667, 8333, 4167] {
            let mut meter = Meter::new();
            for _ in 0..6 {
                meter.add_sample(obj(timing(json!({
                    "pid": 8,
                    "measurement": "present-pacing",
                    "present_frametime_us": frametime_us,
                    "framegen_active": true,
                }))));
            }
            let snapshot = meter.snapshot(monotonic_now(), None).unwrap();
            assert_eq!(snapshot.base_present_frametime_p95_ms, None);
            assert_eq!(snapshot.fps_source.as_deref(), Some("none"));
            assert_eq!(snapshot.framegen_active.as_deref(), Some("true"));
            assert!(snapshot.raw_present_fps_avg.is_some());
            assert_eq!(meter.last_base_present_fps, None);
        }
    }

    #[test]
    fn late_reflex_markers_replace_present_pacing_with_real_fps() {
        let mut meter = Meter::new();
        for _ in 0..6 {
            meter.add_sample(obj(timing(json!({
                "session_id": "700", "pid": 42,
                "measurement": "present-pacing", "present_frametime_us": 5000,
            }))));
        }
        let first = meter.snapshot(monotonic_now(), None).unwrap();
        assert_eq!(first.base_present_frametime_p95_ms, Some(5.0));
        for _ in 0..6 {
            meter.add_sample(obj(timing(json!({
                "session_id": "700", "pid": 99,
                "source": NVAPI_MARKER_SOURCE,
                "marker_name": "simulation-start",
                "base_frame_frametime_us": 10000,
            }))));
        }
        let second = meter.snapshot(monotonic_now(), None).unwrap();
        assert_eq!(second.base_present_frametime_p95_ms, Some(10.0));
        assert_eq!(
            second.fps_source.as_deref(),
            Some("nvapi-base-frame-marker")
        );
        assert_eq!(second.framegen_active.as_deref(), Some("true"));
        assert_eq!(meter.last_base_present_fps, Some(100.0));
    }

    #[test]
    fn fixture_framegen_deinterlace_two_windows() {
        let mut meter = Meter::new();
        // Window 1: base ~50fps with a marker stream (marker_bits).
        for _ in 0..6 {
            meter.add_sample(obj(timing(
                json!({"pid": 8, "present_frametime_us": 20000, "marker_bits": 64}),
            )));
        }
        let w1 = meter.snapshot(monotonic_now(), None).unwrap();
        assert!(approx(w1.base_present_frametime_p95_ms.unwrap(), 20.0));
        assert_eq!(w1.fps_source.as_deref(), Some("present-pacing"));
        assert_eq!(w1.framegen_active.as_deref(), Some("false"));
        assert!(approx(w1.raw_present_fps_avg.unwrap(), 50.0));

        // Window 2: present rate doubles → deinterlace back to the 50fps base.
        meter.samples.clear();
        for _ in 0..6 {
            meter.add_sample(obj(timing(
                json!({"pid": 8, "present_frametime_us": 10000, "marker_bits": 64}),
            )));
        }
        let w2 = meter.snapshot(monotonic_now(), None).unwrap();
        assert!(approx(w2.base_present_frametime_p95_ms.unwrap(), 20.0));
        assert_eq!(w2.present_fps.as_deref(), Some("50"));
        assert_eq!(
            w2.fps_source.as_deref(),
            Some("present-pacing-deinterlaced")
        );
        assert_eq!(w2.raw_present_fps_stats_avg.as_deref(), Some("100"));
        assert!(approx(w2.raw_present_fps_avg.unwrap(), 100.0));
        assert_eq!(w2.framegen_active.as_deref(), Some("true"));
    }

    #[test]
    fn generated_present_frames_do_not_veto_base_fps_promotion() {
        use crate::profile::adaptive::{
            AdaptiveProfileController, FrametimeReadings, MedianSource, PolicyConfig,
        };

        let mut meter = Meter::new();
        // Seed the real 40 FPS cadence, then enable 2x frame generation.
        for _ in 0..6 {
            meter.add_sample(obj(timing(
                json!({"pid": 8, "present_frametime_us": 25000, "marker_bits": 64}),
            )));
        }
        meter.snapshot(monotonic_now(), None).unwrap();
        meter.samples.clear();
        for _ in 0..6 {
            meter.add_sample(obj(timing(
                json!({"pid": 8, "present_frametime_us": 12500, "marker_bits": 64}),
            )));
        }
        let snapshot = meter.snapshot(monotonic_now(), Some(22000)).unwrap();
        assert_eq!(
            snapshot.fps_source.as_deref(),
            Some("present-pacing-deinterlaced")
        );
        assert_eq!(snapshot.base_present_frametime_p95_ms, Some(25.0));
        assert_eq!(snapshot.base_present_frametime_p50_ms, None);
        assert_eq!(snapshot.present_pacing_p50_ms, Some(12.5));

        let readings = FrametimeReadings {
            p95_ms: snapshot.base_present_frametime_p95_ms,
            p50_ms: snapshot.present_pacing_p50_ms,
            p50_source: MedianSource::Pacing,
            miss_ratio: snapshot.base_present_frametime_miss_ratio,
        };
        let tiers = vec!["efficiency".into(), "balanced".into(), "performance".into()];
        let mut controller = AdaptiveProfileController::new("efficiency", PolicyConfig::default());
        let decision =
            controller.update(readings, &tiers, 100.0, Some(99.0), Some(20.0), Some(35.0));
        // 80 displayed FPS must not hide a GPU-bound 40 base FPS session
        // missing the default 60 FPS target.
        assert_eq!(decision.tier, "performance", "{decision:?}");
        assert_eq!(decision.reason, "badly-slow");
    }

    #[test]
    fn deinterlace_gated_on_marker_stream() {
        // Same rate jump WITHOUT a marker stream is a real framerate increase, not
        // frame generation: no deinterlace, base tracks the new rate.
        let mut meter = Meter::new();
        for _ in 0..6 {
            meter.add_sample(obj(timing(
                json!({"pid": 9, "present_frametime_us": 20000}),
            )));
        }
        meter.snapshot(monotonic_now(), None);
        meter.samples.clear();
        for _ in 0..6 {
            meter.add_sample(obj(timing(
                json!({"pid": 9, "present_frametime_us": 10000}),
            )));
        }
        let w2 = meter.snapshot(monotonic_now(), None).unwrap();
        assert_eq!(w2.fps_source.as_deref(), Some("present-pacing"));
        assert_eq!(w2.framegen_active.as_deref(), Some("false"));
        assert!(approx(w2.base_present_frametime_p95_ms.unwrap(), 10.0));
    }

    // --- malformed input tolerance ------------------------------------------

    #[test]
    fn malformed_lines_do_not_kill_receiver() {
        let mut meter = Meter::new();
        // A non-object and a non-timing frame are dropped by the caller; a timing
        // frame with junk fields still aggregates the good ones.
        meter.add_sample(obj(timing(json!({
            "pid": "12345",
            "present_frametime_us": "not-a-number",
            "marker_bits": [1, 2, 3],
        }))));
        for _ in 0..4 {
            meter.add_sample(obj(timing(
                json!({"pid": "12345", "present_frametime_us": 16667}),
            )));
        }
        let s = meter.snapshot(monotonic_now(), None).unwrap();
        assert_eq!(s.pid.as_deref(), Some("12345"));
        assert!(approx(s.base_present_frametime_p95_ms.unwrap(), 16.667));
    }

    // --- socket path resolution ---------------------------------------------

    #[test]
    fn explicit_socket_env_is_the_only_path() {
        std::env::set_var("PENGUIN_BURNER_LATENCY_SOCKET", "/tmp/pb-explicit.sock");
        let paths = latency_socket_paths();
        std::env::remove_var("PENGUIN_BURNER_LATENCY_SOCKET");
        assert_eq!(paths, vec![PathBuf::from("/tmp/pb-explicit.sock")]);
    }

    // --- end-to-end: real socket, synthetic client, engine-visible snapshot ---

    static SOCK_COUNTER: AtomicU32 = AtomicU32::new(0);

    #[test]
    fn end_to_end_client_datagrams_reach_snapshot() {
        let n = SOCK_COUNTER.fetch_add(1, AtomicOrdering::SeqCst);
        let sock_path =
            std::env::temp_dir().join(format!("pb-latency-test-{}-{}.sock", std::process::id(), n));
        let _ = std::fs::remove_file(&sock_path);

        let mut logs: Vec<String> = Vec::new();
        let receiver = LatencyReceiver::start_at(vec![sock_path.clone()], &mut |m| {
            logs.push(m.to_string());
        })
        .expect("receiver binds");
        assert!(logs.iter().any(|l| l.contains("Latency telemetry socket:")));

        // Synthetic in-game client: six present-timing datagrams at ~60 fps.
        let client = UnixDatagram::unbound().unwrap();
        for us in [16600, 16700, 16650, 16680, 16720, 16600] {
            let payload =
                format!("{{\"type\":\"timing\",\"pid\":31337,\"present_frametime_us\":{us}}}");
            client.send_to(payload.as_bytes(), &sock_path).unwrap();
        }

        // Poll the snapshot the engine loop would see until the drain thread has
        // ingested the window (or time out).
        let deadline = Instant::now() + Duration::from_secs(3);
        let mut got = None;
        while Instant::now() < deadline {
            if let Some(snap) = receiver.snapshot(monotonic_now(), None) {
                if snap.present_fps.as_deref() == Some("60") {
                    got = Some(snap);
                    break;
                }
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        let snap = got.expect("engine-visible snapshot with present_fps");
        assert_eq!(snap.pid.as_deref(), Some("31337"));
        assert_eq!(snap.fps_source.as_deref(), Some("present-pacing"));
        assert!(approx(snap.base_present_frametime_p95_ms.unwrap(), 16.72));

        drop(receiver);
        // The socket file is unlinked on drop.
        assert!(!sock_path.exists());
    }

    /// A bind failure must unlink only the paths this receiver actually bound —
    /// never a not-yet-attempted path that may belong to a live previous binder.
    #[test]
    fn bind_failure_unlinks_only_bound_paths() {
        let n = SOCK_COUNTER.fetch_add(1, AtomicOrdering::SeqCst);
        let dir = std::env::temp_dir().join(format!("pb-latbind-{}-{}", std::process::id(), n));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();

        let bound_ok = dir.join("a.sock");
        // Parent is a regular file → bind_socket's create_dir_all fails.
        let blocker = dir.join("blocker");
        std::fs::write(&blocker, "x").unwrap();
        let bind_fails = blocker.join("b.sock");
        // Stand-in for a foreign live binder's socket: never attempted here.
        let foreign = dir.join("c.sock");
        std::fs::write(&foreign, "").unwrap();

        let mut logs: Vec<String> = Vec::new();
        let receiver = LatencyReceiver::start_at(
            vec![bound_ok.clone(), bind_fails.clone(), foreign.clone()],
            &mut |m| logs.push(m.to_string()),
        );
        assert!(receiver.is_none());
        assert!(logs
            .iter()
            .any(|l| l.contains("Latency telemetry unavailable:")));
        assert!(!bound_ok.exists(), "the bound path must be cleaned up");
        assert!(
            foreign.exists(),
            "a never-attempted path must be left alone"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
