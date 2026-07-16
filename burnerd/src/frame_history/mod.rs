//! Frame-history recorder: the per-game telemetry ring behind Frame DNA.
//!
//! One ring file per watched game session on tmpfs
//! (`/run/penguin-burner/frame-history/<pid>.ring`): a 64-byte header, a ring
//! of 36-byte per-second metric records, and a ring of one log2-companded
//! byte per rendered frame. On session end the ring is trimmed and archived
//! durably (and world-readably) to
//! `/var/lib/penguin-burner/frame-history/<app_id>.ring` for the GUI.
//!
//! The module is fully self-contained — the engine, supervisor, and latency
//! receiver each touch it through a single call (`record_metrics`,
//! `session_start`/`session_end`, `record_frame`). The byte layout is format
//! v1 from `notes/frame-dna-plan.md` and must stay in lockstep with the
//! Python reader in `runtime/frame_history.py`.

use std::fs;
use std::fs::File;
use std::os::unix::fs::{FileExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use serde_json::Value;

use crate::profile::telemetry::OverlayState;

pub const FRAME_HISTORY_DIR: &str = "/run/penguin-burner/frame-history";
pub const FRAME_HISTORY_ARCHIVE_DIR: &str = "/var/lib/penguin-burner/frame-history";
/// Test override: pins the live dir; the archive dir becomes `<dir>/archive`
/// so a single variable pins both (the savings.rs convention).
pub const FRAME_HISTORY_DIR_ENV: &str = "PENGUIN_BURNERD_FRAME_HISTORY_DIR";

const MAGIC: &[u8; 4] = b"PBFH";
const FORMAT_VERSION: u16 = 1;
const HEADER_SIZE: usize = 64;
const RECORD_SIZE: usize = 36;
const METRICS_CAP: u32 = 1800;
const FRAME_CAP: u32 = 1 << 20;
const WINDOW_S: u16 = 1800;

const FLAG_FRAMEGEN_ACTIVE: u8 = 0x01;
const FLAG_ADAPTIVE: u8 = 0x02;
const FPS_SOURCE_SHIFT: u8 = 2;
const TIER_SHIFT: u8 = 4;

static ACTIVE: AtomicBool = AtomicBool::new(false);
static RECORDER: Mutex<Option<Recorder>> = Mutex::new(None);
static LAST_METRICS: Mutex<Option<Instant>> = Mutex::new(None);

fn live_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os(FRAME_HISTORY_DIR_ENV) {
        if !dir.is_empty() {
            return PathBuf::from(dir);
        }
    }
    PathBuf::from(FRAME_HISTORY_DIR)
}

fn archive_dir() -> PathBuf {
    if let Some(dir) = std::env::var_os(FRAME_HISTORY_DIR_ENV) {
        if !dir.is_empty() {
            return PathBuf::from(dir).join("archive");
        }
    }
    PathBuf::from(FRAME_HISTORY_ARCHIVE_DIR)
}

/// True while a game session is being recorded (cheap frame-path gate; also
/// used by the engine loop to keep its tick at least 1 Hz while recording).
pub fn recording_active() -> bool {
    ACTIVE.load(Ordering::Relaxed)
}

/// The engine loop's 1 Hz throttle for `record_metrics`.
pub fn metrics_due() -> bool {
    if !recording_active() {
        return false;
    }
    let mut last = LAST_METRICS.lock().unwrap_or_else(|p| p.into_inner());
    let now = Instant::now();
    match *last {
        Some(prev) if now.duration_since(prev).as_secs_f64() < 1.0 => false,
        _ => {
            *last = Some(now);
            true
        }
    }
}

/// Companding: one byte per frame, `ft_ms = 2^(v/32)` (1..~250.6 ms).
pub fn encode_frametime_ms(frametime_ms: f64) -> u8 {
    let max_ms = 2f64.powf(255.0 / 32.0);
    let clamped = frametime_ms.clamp(1.0, max_ms);
    (32.0 * clamped.log2()).round().clamp(0.0, 255.0) as u8
}

fn fps_source_code(fps_source: &str) -> u8 {
    let source = fps_source.to_ascii_lowercase();
    if source.is_empty() {
        0
    } else if source.contains("nvapi") {
        2
    } else if source.contains("marker") {
        1
    } else {
        3
    }
}

fn tier_nibble(tier_key: &str) -> u8 {
    match tier_key.trim() {
        "efficiency" => 1,
        "balanced" => 2,
        "performance" => 3,
        _ => 0,
    }
}

fn c16(value: f64) -> u16 {
    value.round().clamp(0.0, 65535.0) as u16
}

fn parse_metric(text: &str) -> f64 {
    text.trim().parse::<f64>().unwrap_or(0.0)
}

/// Stutter-biased percentile matching the Python reader: floor indexing so
/// the p99 of 100 frames is the single worst frame.
fn percentile(sorted: &[f64], fraction: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let index = ((fraction * sorted.len() as f64).floor() as usize).min(sorted.len() - 1);
    sorted[index]
}

struct HeaderState {
    app_id: u32,
    pid: u32,
    gpu_index: u16,
    power_limit_w: u16,
    max_boost_mhz: u16,
    metrics_head: u32,
    metrics_count: u32,
    frame_head: u32,
    frame_count: u32,
    started_unix: u64,
    metrics_cap: u32,
    frame_cap: u32,
}

fn pack_header(state: &HeaderState) -> [u8; HEADER_SIZE] {
    let mut buf = [0u8; HEADER_SIZE];
    buf[0..4].copy_from_slice(MAGIC);
    buf[4..6].copy_from_slice(&FORMAT_VERSION.to_le_bytes());
    // flags u16 at 6..8 stays zero.
    buf[8..12].copy_from_slice(&state.app_id.to_le_bytes());
    buf[12..16].copy_from_slice(&state.pid.to_le_bytes());
    buf[16..18].copy_from_slice(&state.gpu_index.to_le_bytes());
    buf[18..20].copy_from_slice(&state.power_limit_w.to_le_bytes());
    buf[20..22].copy_from_slice(&state.max_boost_mhz.to_le_bytes());
    buf[22] = 1; // sample_hz
    buf[24..26].copy_from_slice(&WINDOW_S.to_le_bytes());
    buf[26..28].copy_from_slice(&(state.metrics_cap as u16).to_le_bytes());
    buf[28..30].copy_from_slice(&((state.metrics_head % state.metrics_cap) as u16).to_le_bytes());
    buf[30..32]
        .copy_from_slice(&(state.metrics_count.min(state.metrics_cap) as u16).to_le_bytes());
    buf[32..36].copy_from_slice(&state.frame_cap.to_le_bytes());
    buf[36..40].copy_from_slice(&(state.frame_head % state.frame_cap).to_le_bytes());
    buf[40..44].copy_from_slice(&state.frame_count.min(state.frame_cap).to_le_bytes());
    buf[44..52].copy_from_slice(&state.started_unix.to_le_bytes());
    buf
}

struct Recorder {
    file: File,
    path: PathBuf,
    header: HeaderState,
    started_mono: Instant,
    // Frames buffered per producer stream for the current second. A session
    // can carry more than one present stream (the game plus e.g. the Steam
    // overlay swapchain); only the dominant stream is the game's cadence.
    sec_frametimes: std::collections::BTreeMap<String, Vec<f64>>,
}

impl Recorder {
    fn open(pid: u32, app_id: u32) -> std::io::Result<Recorder> {
        let dir = live_dir();
        fs::create_dir_all(&dir)?;
        let _ = fs::set_permissions(&dir, fs::Permissions::from_mode(0o755));
        let path = dir.join(format!("{pid}.ring"));
        let file = File::create(&path)?;
        let total = HEADER_SIZE as u64
            + u64::from(METRICS_CAP) * RECORD_SIZE as u64
            + u64::from(FRAME_CAP);
        file.set_len(total)?;
        // The GUI runs unprivileged and reads the live ring directly.
        let _ = fs::set_permissions(&path, fs::Permissions::from_mode(0o644));
        let header = HeaderState {
            app_id,
            pid,
            gpu_index: 0,
            power_limit_w: 0,
            max_boost_mhz: 0,
            metrics_head: 0,
            metrics_count: 0,
            frame_head: 0,
            frame_count: 0,
            started_unix: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0),
            metrics_cap: METRICS_CAP,
            frame_cap: FRAME_CAP,
        };
        file.write_all_at(&pack_header(&header), 0)?;
        Ok(Recorder {
            file,
            path,
            header,
            started_mono: Instant::now(),
            sec_frametimes: std::collections::BTreeMap::new(),
        })
    }

    fn push_frame(&mut self, stream: String, frametime_ms: f64) {
        self.sec_frametimes.entry(stream).or_default().push(frametime_ms);
    }

    fn append_frames(&mut self, companded: &[u8]) -> std::io::Result<()> {
        let frames_offset = HEADER_SIZE as u64 + u64::from(METRICS_CAP) * RECORD_SIZE as u64;
        let cap = u64::from(self.header.frame_cap);
        let mut slot = u64::from(self.header.frame_head % self.header.frame_cap);
        let mut remaining = companded;
        while !remaining.is_empty() {
            let run = remaining.len().min((cap - slot) as usize);
            self.file
                .write_all_at(&remaining[..run], frames_offset + slot)?;
            slot = (slot + run as u64) % cap;
            remaining = &remaining[run..];
        }
        let added = companded.len() as u32;
        self.header.frame_head = (self.header.frame_head + added) % self.header.frame_cap;
        self.header.frame_count = self.header.frame_count.saturating_add(added);
        Ok(())
    }

    fn flush_second(
        &mut self,
        state: &OverlayState,
        mem_clock_mhz: Option<u32>,
        power_limit_w: Option<i64>,
    ) -> std::io::Result<()> {
        // Keep only the dominant stream's frames for this second; ties break
        // deterministically by key so equal-rate streams don't interleave.
        let buckets = std::mem::take(&mut self.sec_frametimes);
        let mut frametimes = buckets
            .into_iter()
            .max_by(|a, b| a.1.len().cmp(&b.1.len()).then(b.0.cmp(&a.0)))
            .map(|(_, frames)| frames)
            .unwrap_or_default();
        let companded: Vec<u8> = frametimes
            .iter()
            .map(|&ft| encode_frametime_ms(ft))
            .collect();
        frametimes.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let (p50, p99, p999) = (
            percentile(&frametimes, 0.50),
            percentile(&frametimes, 0.99),
            percentile(&frametimes, 0.999),
        );
        self.append_frames(&companded)?;

        let t_rel = self.started_mono.elapsed().as_secs().min(65535) as u16;
        let flags = (if state.framegen_active {
            FLAG_FRAMEGEN_ACTIVE
        } else {
            0
        }) | (if state.adaptive { FLAG_ADAPTIVE } else { 0 })
            | (fps_source_code(&state.fps_source) << FPS_SOURCE_SHIFT)
            | (tier_nibble(&state.profile_tier_key) << TIER_SHIFT);

        let mut record = [0u8; RECORD_SIZE];
        let put16 = |buf: &mut [u8; RECORD_SIZE], offset: usize, value: u16| {
            buf[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
        };
        put16(&mut record, 0, t_rel);
        put16(&mut record, 2, c16(companded.len() as f64));
        put16(&mut record, 4, c16(state.clock_mhz.unwrap_or(0) as f64));
        put16(&mut record, 6, c16(mem_clock_mhz.unwrap_or(0) as f64));
        put16(&mut record, 8, c16(state.voltage_mv.unwrap_or(0) as f64));
        put16(&mut record, 10, c16(state.power_w.unwrap_or(0) as f64));
        record[12] = state.gpu_util_pct.unwrap_or(0).clamp(0, 255) as u8;
        record[13] = state.cpu_util_pct.unwrap_or(0).clamp(0, 255) as u8;
        record[14] = state.cpu_peak_thread_pct.unwrap_or(0).clamp(0, 255) as u8;
        record[15] = state.fan_pct.unwrap_or(0).clamp(0, 255) as u8;
        record[16] = state.temperature_c.unwrap_or(0).clamp(0, 255) as u8;
        record[17] = flags;
        let uv = state.uv_offset_mv.unwrap_or(0).clamp(-32768, 32767) as i16;
        record[18..20].copy_from_slice(&uv.to_le_bytes());
        put16(&mut record, 20, c16(parse_metric(&state.present_fps) * 10.0));
        put16(&mut record, 22, c16(parse_metric(&state.framegen_fps) * 10.0));
        put16(&mut record, 24, c16(parse_metric(&state.latency_ms) * 20.0));
        put16(
            &mut record,
            26,
            c16(parse_metric(&state.display_latency_ms) * 20.0),
        );
        put16(&mut record, 28, c16(p50 * 20.0));
        put16(&mut record, 30, c16(p99 * 20.0));
        put16(&mut record, 32, c16(p999 * 20.0));

        let slot = self.header.metrics_head % self.header.metrics_cap;
        let offset = HEADER_SIZE as u64 + u64::from(slot) * RECORD_SIZE as u64;
        self.file.write_all_at(&record, offset)?;
        self.header.metrics_head = (self.header.metrics_head + 1) % self.header.metrics_cap;
        self.header.metrics_count = self.header.metrics_count.saturating_add(1);

        if self.header.gpu_index == 0 {
            self.header.gpu_index = state.gpu_index.clamp(0, 65535) as u16;
        }
        if self.header.power_limit_w == 0 {
            if let Some(limit) = power_limit_w {
                self.header.power_limit_w = limit.clamp(0, 65535) as u16;
            }
        }
        self.file.write_all_at(&pack_header(&self.header), 0)?;
        Ok(())
    }
}

/// Called by the supervisor once a game watch is registered. Also archives
/// any orphaned rings a previous daemon left behind.
pub fn session_start(pid: u32, app_id: &str) {
    recover_orphans();
    let app_id_num = app_id.trim().parse::<u32>().unwrap_or(0);
    match Recorder::open(pid, app_id_num) {
        Ok(recorder) => {
            let mut slot = RECORDER.lock().unwrap_or_else(|p| p.into_inner());
            *slot = Some(recorder);
            ACTIVE.store(true, Ordering::Relaxed);
        }
        Err(_) => {
            // Recording is best-effort; the daemon must never fail a game
            // launch because telemetry storage is unavailable.
            ACTIVE.store(false, Ordering::Relaxed);
        }
    }
}

/// Called by the latency receiver for every timing datagram. Prefers the
/// base-frame (pre-frame-gen) frametime; falls back to the raw present.
pub fn record_frame(sample: &serde_json::Map<String, Value>) {
    if !recording_active() {
        return;
    }
    let frametime_us = sample
        .get("base_frame_frametime_us")
        .and_then(Value::as_f64)
        .or_else(|| sample.get("present_frametime_us").and_then(Value::as_f64));
    let Some(frametime_us) = frametime_us else {
        return;
    };
    if !(frametime_us.is_finite()) || frametime_us <= 0.0 {
        return;
    }
    let stream = sample
        .get("session_id")
        .or_else(|| sample.get("pid"))
        .map(|value| match value {
            Value::String(text) => text.clone(),
            other => other.to_string(),
        })
        .unwrap_or_default();
    let mut slot = RECORDER.lock().unwrap_or_else(|p| p.into_inner());
    if let Some(recorder) = slot.as_mut() {
        recorder.push_frame(stream, frametime_us / 1000.0);
    }
}

/// Called by the engine loop at 1 Hz (gated by `metrics_due`).
pub fn record_metrics(
    state: &OverlayState,
    mem_clock_mhz: Option<u32>,
    power_limit_w: Option<i64>,
) {
    let mut slot = RECORDER.lock().unwrap_or_else(|p| p.into_inner());
    if let Some(recorder) = slot.as_mut() {
        let _ = recorder.flush_second(state, mem_clock_mhz, power_limit_w);
    }
}

/// Called by the supervisor when the watched game's process exits. Trims the
/// live ring into the durable per-app archive and removes it.
pub fn session_end(pid: u32, app_id: &str) {
    let recorder = {
        let mut slot = RECORDER.lock().unwrap_or_else(|p| p.into_inner());
        match slot.as_ref() {
            Some(recorder) if recorder.header.pid == pid => slot.take(),
            _ => None,
        }
    };
    let Some(recorder) = recorder else {
        return;
    };
    ACTIVE.store(false, Ordering::Relaxed);
    let app_id_num = app_id.trim().parse::<u32>().unwrap_or(recorder.header.app_id);
    let path = recorder.path.clone();
    drop(recorder);
    let _ = archive_ring_file(&path, app_id_num);
    let _ = fs::remove_file(&path);
}

/// Archive rings left behind by a crashed/restarted daemon, then drop them.
fn recover_orphans() {
    let Ok(entries) = fs::read_dir(live_dir()) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("ring") {
            continue;
        }
        let app_id = ring_app_id(&path).unwrap_or(0);
        if app_id > 0 {
            let _ = archive_ring_file(&path, app_id);
        }
        let _ = fs::remove_file(&path);
    }
}

fn ring_app_id(path: &Path) -> Option<u32> {
    let mut header = [0u8; HEADER_SIZE];
    File::open(path).ok()?.read_exact_at(&mut header, 0).ok()?;
    if &header[0..4] != MAGIC {
        return None;
    }
    Some(u32::from_le_bytes(header[8..12].try_into().ok()?))
}

/// Rebuild a live ring as a compact archive (caps trimmed to the data) at
/// `<archive_dir>/<app_id>.ring`, world-readable, atomically.
fn archive_ring_file(path: &Path, app_id: u32) -> std::io::Result<()> {
    let data = fs::read(path)?;
    let Some(compact) = compact_ring(&data, app_id) else {
        return Ok(());
    };
    let dir = archive_dir();
    fs::create_dir_all(&dir)?;
    let _ = fs::set_permissions(&dir, fs::Permissions::from_mode(0o755));
    let target = dir.join(format!("{app_id}.ring"));
    let temp = tempfile::NamedTempFile::new_in(&dir)?;
    temp.as_file().write_all_at(&compact, 0)?;
    let persisted = temp.persist(&target).map_err(|e| e.error)?;
    persisted.set_permissions(fs::Permissions::from_mode(0o644))?;
    Ok(())
}

/// Unwrap both rings and re-lay them out with caps equal to their counts.
fn compact_ring(data: &[u8], app_id: u32) -> Option<Vec<u8>> {
    if data.len() < HEADER_SIZE || &data[0..4] != MAGIC {
        return None;
    }
    let read_u16 =
        |offset: usize| u16::from_le_bytes(data[offset..offset + 2].try_into().unwrap());
    let read_u32 =
        |offset: usize| u32::from_le_bytes(data[offset..offset + 4].try_into().unwrap());
    let metrics_cap = u32::from(read_u16(26));
    let metrics_head = u32::from(read_u16(28));
    let metrics_count = u32::from(read_u16(30)).min(metrics_cap);
    let frame_cap = read_u32(32);
    let frame_head = read_u32(36);
    let frame_count = read_u32(40).min(frame_cap);
    if metrics_cap == 0 || frame_cap == 0 || metrics_count == 0 {
        return None;
    }
    let metrics_offset = HEADER_SIZE;
    let frames_offset = HEADER_SIZE + metrics_cap as usize * RECORD_SIZE;
    if data.len() < frames_offset + frame_cap as usize {
        return None;
    }

    let out_metrics_cap = metrics_count;
    let out_frame_cap = frame_count.max(1);
    let mut out = vec![
        0u8;
        HEADER_SIZE + out_metrics_cap as usize * RECORD_SIZE + out_frame_cap as usize
    ];
    out[..HEADER_SIZE].copy_from_slice(&data[..HEADER_SIZE]);
    out[8..12].copy_from_slice(&app_id.to_le_bytes());
    out[26..28].copy_from_slice(&(out_metrics_cap as u16).to_le_bytes());
    out[28..30].copy_from_slice(&0u16.to_le_bytes()); // head: unwrapped layout
    out[30..32].copy_from_slice(&(metrics_count as u16).to_le_bytes());
    out[32..36].copy_from_slice(&out_frame_cap.to_le_bytes());
    out[36..40].copy_from_slice(&(frame_count % out_frame_cap).to_le_bytes());
    out[40..44].copy_from_slice(&frame_count.to_le_bytes());

    let metrics_start = if metrics_count < metrics_cap {
        0
    } else {
        metrics_head % metrics_cap
    };
    for index in 0..metrics_count {
        let src_slot = ((metrics_start + index) % metrics_cap) as usize;
        let src = metrics_offset + src_slot * RECORD_SIZE;
        let dst = HEADER_SIZE + index as usize * RECORD_SIZE;
        out[dst..dst + RECORD_SIZE].copy_from_slice(&data[src..src + RECORD_SIZE]);
    }
    let frame_start = if frame_count < frame_cap {
        0
    } else {
        frame_head % frame_cap
    };
    let out_frames_offset = HEADER_SIZE + out_metrics_cap as usize * RECORD_SIZE;
    for index in 0..frame_count {
        let src_slot = ((frame_start + index) % frame_cap) as usize;
        out[out_frames_offset + index as usize] = data[frames_offset + src_slot];
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Serializes env mutation across tests (the savings.rs guard pattern).
    static ENV_LOCK: Mutex<()> = Mutex::new(());

    struct DirGuard {
        _lock: std::sync::MutexGuard<'static, ()>,
        _dir: tempfile::TempDir,
    }

    fn pin_dir() -> DirGuard {
        let lock = ENV_LOCK.lock().unwrap_or_else(|p| p.into_inner());
        let dir = tempfile::tempdir().expect("tempdir");
        std::env::set_var(FRAME_HISTORY_DIR_ENV, dir.path());
        *RECORDER.lock().unwrap_or_else(|p| p.into_inner()) = None;
        ACTIVE.store(false, Ordering::Relaxed);
        DirGuard {
            _lock: lock,
            _dir: dir,
        }
    }

    fn overlay_state(tier: &str) -> OverlayState {
        OverlayState {
            gpu_index: 0,
            clock_mhz: Some(2610),
            voltage_mv: Some(1010),
            power_w: Some(298),
            gpu_util_pct: Some(99),
            cpu_util_pct: Some(40),
            cpu_peak_thread_pct: Some(66),
            fan_pct: Some(63),
            temperature_c: Some(71),
            uv_offset_mv: Some(-120),
            present_fps: "78.2".to_string(),
            fps_source: "nvapi-base-frame-marker".to_string(),
            framegen_fps: "156.4".to_string(),
            framegen_active: true,
            latency_ms: "34.1".to_string(),
            display_latency_ms: "19.0".to_string(),
            profile_tier: "Performance".to_string(),
            profile_tier_key: tier.to_string(),
            profile_id: "auto-uv".to_string(),
            adaptive: false,
            updated_unix_ns: 0,
        }
    }

    fn timing_sample(frametime_us: f64) -> serde_json::Map<String, Value> {
        let mut map = serde_json::Map::new();
        map.insert("type".into(), Value::from("timing"));
        map.insert("base_frame_frametime_us".into(), Value::from(frametime_us));
        map
    }

    #[test]
    fn companding_matches_the_python_reader() {
        for frametime in [1.0, 2.5, 4.17, 6.94, 12.8, 16.7, 33.3, 63.9, 120.0, 250.0] {
            let decoded = 2f64.powf(f64::from(encode_frametime_ms(frametime)) / 32.0);
            assert!(
                ((decoded - frametime) / frametime).abs() <= 0.023,
                "{frametime} decoded as {decoded}"
            );
        }
        assert_eq!(encode_frametime_ms(0.2), 0);
        assert_eq!(encode_frametime_ms(999.0), 255);
    }

    #[test]
    fn tier_and_fps_source_codes_match_the_contract() {
        assert_eq!(tier_nibble("efficiency"), 1);
        assert_eq!(tier_nibble("balanced"), 2);
        assert_eq!(tier_nibble("performance"), 3);
        assert_eq!(tier_nibble(""), 0);
        assert_eq!(fps_source_code("nvapi-base-frame-marker"), 2);
        assert_eq!(fps_source_code("base-frame-marker"), 1);
        assert_eq!(fps_source_code(""), 0);
        assert_eq!(fps_source_code("raw-present"), 3);
    }

    #[test]
    fn session_records_and_archives_a_readable_ring() {
        let guard = pin_dir();
        session_start(4242, "3764200");
        assert!(recording_active());
        for _ in 0..78 {
            record_frame(&timing_sample(12_800.0));
        }
        record_frame(&timing_sample(42_000.0));
        record_metrics(&overlay_state("performance"), Some(10_251), Some(360));
        record_metrics(&overlay_state("performance"), Some(10_251), Some(360));

        let live = live_dir().join("4242.ring");
        assert!(live.exists());
        let data = fs::read(&live).expect("live ring bytes");
        assert_eq!(&data[0..4], MAGIC);
        assert_eq!(u16::from_le_bytes([data[30], data[31]]), 2); // metrics_count
        assert_eq!(u32::from_le_bytes(data[40..44].try_into().unwrap()), 79);
        // First record: t_rel 0, 79 frames, clock 2610, power 298.
        let record = &data[HEADER_SIZE..HEADER_SIZE + RECORD_SIZE];
        assert_eq!(u16::from_le_bytes([record[2], record[3]]), 79);
        assert_eq!(u16::from_le_bytes([record[4], record[5]]), 2610);
        assert_eq!(u16::from_le_bytes([record[10], record[11]]), 298);
        assert_eq!(record[17] & 0x0F, FLAG_FRAMEGEN_ACTIVE | (2 << FPS_SOURCE_SHIFT));
        assert_eq!(record[17] >> TIER_SHIFT, 3);
        // p99 of [12.8ms x78, 42ms] is the worst frame.
        let p99 = f64::from(u16::from_le_bytes([record[30], record[31]])) / 20.0;
        assert!((p99 - 42.0).abs() < 0.06, "p99 was {p99}");
        // Header carries the board power limit for the PWR spoke.
        assert_eq!(u16::from_le_bytes([data[18], data[19]]), 360);

        session_end(4242, "3764200");
        assert!(!recording_active());
        assert!(!live.exists());
        let archived = archive_dir().join("3764200.ring");
        let compact = fs::read(&archived).expect("archive bytes");
        assert_eq!(&compact[0..4], MAGIC);
        // Trimmed caps: metrics_cap == count == 2, frame_cap == 79.
        assert_eq!(u16::from_le_bytes([compact[26], compact[27]]), 2);
        assert_eq!(u32::from_le_bytes(compact[32..36].try_into().unwrap()), 79);
        assert_eq!(
            u32::from_le_bytes(compact[8..12].try_into().unwrap()),
            3_764_200
        );
        assert_eq!(
            compact.len(),
            HEADER_SIZE + 2 * RECORD_SIZE + 79,
            "archive is compact"
        );
        drop(guard);
    }

    #[test]
    fn only_the_dominant_present_stream_is_recorded() {
        let guard = pin_dir();
        session_start(31337, "367520");
        // The game presents 120 frames; a second swapchain (Steam overlay)
        // presents 30. Only the game's cadence must land in the ring.
        for index in 0..150 {
            let mut map = timing_sample(if index % 5 == 4 { 33_333.0 } else { 8_333.0 });
            let stream = if index % 5 == 4 { "overlay-swapchain" } else { "game-session" };
            map.insert("session_id".into(), Value::from(stream));
            record_frame(&map);
        }
        record_metrics(&overlay_state("performance"), None, None);
        let data = fs::read(live_dir().join("31337.ring")).expect("ring");
        let record = &data[HEADER_SIZE..HEADER_SIZE + RECORD_SIZE];
        let frame_count = u16::from_le_bytes([record[2], record[3]]);
        assert_eq!(frame_count, 120);
        let p99 = f64::from(u16::from_le_bytes([record[30], record[31]])) / 20.0;
        assert!(p99 < 10.0, "overlay frames leaked into percentiles: {p99}");
        session_end(31337, "367520");
        drop(guard);
    }

    #[test]
    fn orphaned_rings_are_archived_on_the_next_session_start() {
        let guard = pin_dir();
        session_start(1111, "1089130");
        record_frame(&timing_sample(7_000.0));
        record_metrics(&overlay_state("efficiency"), None, Some(360));
        // Daemon "crashes": no session_end. The recorder slot is dropped, the
        // live ring file stays behind.
        *RECORDER.lock().unwrap_or_else(|p| p.into_inner()) = None;
        ACTIVE.store(false, Ordering::Relaxed);
        assert!(live_dir().join("1111.ring").exists());

        session_start(2222, "3764200");
        assert!(!live_dir().join("1111.ring").exists());
        assert!(archive_dir().join("1089130.ring").exists());
        session_end(2222, "3764200");
        drop(guard);
    }

    #[test]
    fn frame_ring_wraps_and_metrics_survive() {
        let guard = pin_dir();
        session_start(7, "42");
        // Overflow one frame ring: FRAME_CAP + 100 frames in two batches.
        {
            let mut slot = RECORDER.lock().unwrap_or_else(|p| p.into_inner());
            let recorder = slot.as_mut().expect("recorder");
            recorder.header.frame_head = FRAME_CAP - 50;
            recorder.header.frame_count = FRAME_CAP;
        }
        for _ in 0..100 {
            record_frame(&timing_sample(10_000.0));
        }
        record_metrics(&overlay_state("balanced"), None, None);
        let data = fs::read(live_dir().join("7.ring")).expect("ring");
        let frame_head = u32::from_le_bytes(data[36..40].try_into().unwrap());
        let frame_count = u32::from_le_bytes(data[40..44].try_into().unwrap());
        assert_eq!(frame_head, 50);
        assert_eq!(frame_count, FRAME_CAP); // saturated at the cap
        session_end(7, "42");
        // A wrapped ring still archives compactly at the cap.
        let archived = fs::read(archive_dir().join("42.ring")).expect("archive");
        let cap = u32::from_le_bytes(archived[32..36].try_into().unwrap());
        assert_eq!(cap, FRAME_CAP);
        drop(guard);
    }
}
