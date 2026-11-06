use std::collections::{BTreeMap, HashSet};
use std::path::PathBuf;

use serde::{Deserialize, Serialize};

use super::fan::{self, FanConfig};

pub const RUNTIME_SPEC_FORMAT_VERSION: u32 = 1;
pub const PROFILE_TIER_EFFICIENCY: &str = "efficiency";
pub const PROFILE_TIER_BALANCED: &str = "balanced";
pub const PROFILE_TIER_PERFORMANCE: &str = "performance";
pub const PROFILE_TIERS: [&str; 3] = [
    PROFILE_TIER_EFFICIENCY,
    PROFILE_TIER_BALANCED,
    PROFILE_TIER_PERFORMANCE,
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RuntimeMode {
    Stock,
    Static,
    Adaptive,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GpuSpec {
    pub uuid: String,
    pub index_at_resolution: u32,
    #[serde(default)]
    pub pci_bus_id: String,
    #[serde(default)]
    pub name: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlanItem {
    pub index: u32,
    pub voltage_mv: i64,
    pub base_mhz: i64,
    pub target_mhz: i64,
    pub new_offset_mhz: i64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FlattenTarget {
    pub source: String,
    pub lock_clock_mhz: i64,
    pub lock_voltage_mv: Option<i64>,
    pub end_voltage_mv: Option<i64>,
    pub tail_point_count: Option<i64>,
    pub ceiling_clock_mhz: Option<i64>,
    pub tail_rise_bins: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeProfile {
    #[serde(default)]
    pub path: PathBuf,
    pub profile_id: String,
    #[serde(default)]
    pub profile_tier: String,
    #[serde(default)]
    pub profile_tier_key: String,
    pub plan: Vec<PlanItem>,
    pub lock_clock_mhz: i64,
    pub candidate_voltage_mv: i64,
    pub memory_offset_mhz: Option<i64>,
    pub power_limit_w: Option<i64>,
    // Scan-measured average power under load for this profile and the same
    // scan's stock baseline; their difference drives the energy-savings
    // accounting. Optional both ways: older specs and profiles predate the
    // fields, and absent fields round-trip byte-identically (persisted
    // boot/active runtime state keeps its old shape).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub avg_power_w: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub base_avg_power_w: Option<f64>,
    pub flatten_target: FlattenTarget,
}

pub type LoadedCurve = RuntimeProfile;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdaptivePolicySpec {
    pub target_fps: f64,
    pub target_slow_windows: i64,
    pub near_slow_windows: i64,
    pub comfort_windows: i64,
    pub performance_comfort_windows: i64,
    pub demote_dwell_s: f64,
    pub performance_demote_dwell_s: f64,
    pub cpu_bound_gpu_util_max_pct: f64,
    pub cpu_bound_peak_thread_min_pct: f64,
    pub cpu_bound_process_util_min_pct: f64,
    /// The frame-cap and desktop-idle knobs are defaulted so a client from
    /// before they existed still resolves: deny_unknown_fields makes the spec
    /// strict in the other direction, and a missing guard threshold must not
    /// fail an otherwise valid runtime. The defaults here must stay identical
    /// to `AdaptiveProfilePolicyConfig` in
    /// `runtime/gpu_control/adaptive_profile_policy.py` (pinned by a literal
    /// test on each side): the client omits any of these it holds at the
    /// default, so a 0.7.x daemon that has not been restarted keeps accepting
    /// a default-config runtime. Serialization skips defaults for the same
    /// reason in the other direction -- persisted boot/active state keeps a
    /// shape a 0.7.x daemon still loads after a rollback. The aliases accept
    /// state persisted by the pre-rename build of this feature branch.
    #[serde(
        default = "default_frame_cap_enter_gpu_pct",
        alias = "capped_gpu_util_max_pct",
        skip_serializing_if = "is_default_frame_cap_enter_gpu_pct"
    )]
    pub frame_cap_enter_gpu_pct: f64,
    #[serde(
        default = "default_frame_cap_exit_gpu_pct",
        alias = "capped_exit_gpu_util_pct",
        skip_serializing_if = "is_default_frame_cap_exit_gpu_pct"
    )]
    pub frame_cap_exit_gpu_pct: f64,
    #[serde(
        default = "default_frame_cap_confirm_windows",
        skip_serializing_if = "is_default_frame_cap_confirm_windows"
    )]
    pub frame_cap_confirm_windows: i64,
    #[serde(
        default = "default_frame_cap_exit_pacing_pct",
        skip_serializing_if = "is_default_frame_cap_exit_pacing_pct"
    )]
    pub frame_cap_exit_pacing_pct: f64,
    #[serde(
        default = "default_desktop_idle_gpu_pct",
        alias = "desktop_idle_gpu_util_max_pct",
        skip_serializing_if = "is_default_desktop_idle_gpu_pct"
    )]
    pub desktop_idle_gpu_pct: f64,
    #[serde(
        default = "default_desktop_idle_after_s",
        skip_serializing_if = "is_default_desktop_idle_after_s"
    )]
    pub desktop_idle_after_s: f64,
}

fn default_frame_cap_enter_gpu_pct() -> f64 {
    60.0
}

fn default_frame_cap_exit_gpu_pct() -> f64 {
    90.0
}

fn default_frame_cap_confirm_windows() -> i64 {
    3
}

fn default_frame_cap_exit_pacing_pct() -> f64 {
    15.0
}

fn default_desktop_idle_gpu_pct() -> f64 {
    20.0
}

fn default_desktop_idle_after_s() -> f64 {
    60.0
}

fn is_default_frame_cap_enter_gpu_pct(value: &f64) -> bool {
    *value == default_frame_cap_enter_gpu_pct()
}

fn is_default_frame_cap_exit_gpu_pct(value: &f64) -> bool {
    *value == default_frame_cap_exit_gpu_pct()
}

fn is_default_frame_cap_confirm_windows(value: &i64) -> bool {
    *value == default_frame_cap_confirm_windows()
}

fn is_default_frame_cap_exit_pacing_pct(value: &f64) -> bool {
    *value == default_frame_cap_exit_pacing_pct()
}

fn is_default_desktop_idle_gpu_pct(value: &f64) -> bool {
    *value == default_desktop_idle_gpu_pct()
}

fn is_default_desktop_idle_after_s(value: &f64) -> bool {
    *value == default_desktop_idle_after_s()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AdaptiveSpec {
    pub initial_tier: String,
    pub profiles: BTreeMap<String, RuntimeProfile>,
    pub policy: AdaptivePolicySpec,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FanSpec {
    pub enabled: bool,
    pub config: FanConfig,
    #[serde(default)]
    pub notice: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimePolicySpec {
    pub enable_persistence_mode: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OverlaySpec {
    pub enabled: bool,
    pub update_interval_s: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeSpec {
    pub format_version: u32,
    pub gpu: GpuSpec,
    pub mode: RuntimeMode,
    pub static_profile: Option<RuntimeProfile>,
    pub adaptive: Option<AdaptiveSpec>,
    pub fan: FanSpec,
    pub policy: RuntimePolicySpec,
    pub overlay: OverlaySpec,
    /// Frame-history capture for the watched game (Game Perf Profile). Absent means
    /// enabled — capture is the default — and the default is not serialized
    /// so persisted runtime state keeps its old shape.
    #[serde(
        default = "default_game_telemetry",
        skip_serializing_if = "game_telemetry_is_default"
    )]
    pub game_telemetry: bool,
}

fn default_game_telemetry() -> bool {
    true
}

fn game_telemetry_is_default(value: &bool) -> bool {
    *value
}

impl RuntimeSpec {
    pub fn validate(&self) -> Result<(), String> {
        if self.format_version != RUNTIME_SPEC_FORMAT_VERSION {
            return Err(format!(
                "unsupported runtime spec format_version: {}",
                self.format_version
            ));
        }
        if self.gpu.uuid.trim().is_empty() {
            return Err("runtime spec GPU uuid is required".to_string());
        }
        match self.mode {
            RuntimeMode::Stock if self.static_profile.is_none() && self.adaptive.is_none() => {}
            RuntimeMode::Static if self.static_profile.is_some() && self.adaptive.is_none() => {}
            RuntimeMode::Adaptive if self.static_profile.is_none() && self.adaptive.is_some() => {}
            _ => {
                return Err(
                    "runtime spec mode does not match static_profile/adaptive fields".to_string(),
                )
            }
        }
        if let Some(profile) = &self.static_profile {
            validate_profile(profile)?;
        }
        if let Some(adaptive) = &self.adaptive {
            validate_adaptive(adaptive)?;
        }
        validate_fan(&self.fan)?;
        if !(1..=10).contains(&self.overlay.update_interval_s) {
            return Err("overlay update_interval_s must be in 1..10".to_string());
        }
        Ok(())
    }

    pub fn mode_name(&self) -> &'static str {
        match self.mode {
            RuntimeMode::Stock => "stock",
            RuntimeMode::Static => "static",
            RuntimeMode::Adaptive => "adaptive",
        }
    }

    pub fn active_profile_id(&self) -> String {
        match self.mode {
            RuntimeMode::Stock => String::new(),
            RuntimeMode::Static => self
                .static_profile
                .as_ref()
                .map(|profile| profile.profile_id.clone())
                .unwrap_or_default(),
            RuntimeMode::Adaptive => self
                .adaptive
                .as_ref()
                .and_then(|adaptive| adaptive.profiles.get(&adaptive.initial_tier))
                .map(|profile| profile.profile_id.clone())
                .unwrap_or_default(),
        }
    }

    pub fn stock_fallback(&self) -> Self {
        let mut stock = self.clone();
        stock.mode = RuntimeMode::Stock;
        stock.static_profile = None;
        stock.adaptive = None;
        stock.fan.enabled = false;
        stock
    }

    #[cfg(test)]
    pub fn test_stock(gpu_uuid: &str) -> Self {
        Self {
            format_version: RUNTIME_SPEC_FORMAT_VERSION,
            gpu: GpuSpec {
                uuid: gpu_uuid.to_string(),
                index_at_resolution: 0,
                pci_bus_id: "0000:01:00.0".to_string(),
                name: "test-gpu".to_string(),
            },
            mode: RuntimeMode::Stock,
            static_profile: None,
            adaptive: None,
            fan: FanSpec {
                enabled: false,
                config: FanConfig::defaults(),
                notice: String::new(),
            },
            policy: RuntimePolicySpec {
                enable_persistence_mode: false,
            },
            overlay: OverlaySpec {
                enabled: false,
                update_interval_s: 1,
            },
            game_telemetry: true,
        }
    }

    #[cfg(test)]
    pub fn test_static(gpu_uuid: &str, profile_id: &str) -> Self {
        let mut spec = Self::test_stock(gpu_uuid);
        spec.mode = RuntimeMode::Static;
        spec.static_profile = Some(RuntimeProfile {
            path: PathBuf::from("/tmp/test-profile.json"),
            profile_id: profile_id.to_string(),
            profile_tier: "Balanced".to_string(),
            profile_tier_key: PROFILE_TIER_BALANCED.to_string(),
            plan: vec![PlanItem {
                index: 12,
                voltage_mv: 900,
                base_mhz: 2500,
                target_mhz: 2600,
                new_offset_mhz: 100,
            }],
            lock_clock_mhz: 2600,
            candidate_voltage_mv: 900,
            memory_offset_mhz: None,
            power_limit_w: None,
            avg_power_w: None,
            base_avg_power_w: None,
            flatten_target: FlattenTarget {
                source: "auto-uv-final".to_string(),
                lock_clock_mhz: 2600,
                lock_voltage_mv: Some(900),
                end_voltage_mv: Some(1100),
                tail_point_count: Some(6),
                ceiling_clock_mhz: None,
                tail_rise_bins: Some(0),
            },
        });
        spec
    }
}

fn validate_profile(profile: &RuntimeProfile) -> Result<(), String> {
    if profile.profile_id.trim().is_empty() {
        return Err("runtime profile id is required".to_string());
    }
    if profile.plan.is_empty() {
        return Err(format!(
            "runtime profile {} has no V/F points",
            profile.profile_id
        ));
    }
    if profile.lock_clock_mhz <= 0 || profile.candidate_voltage_mv <= 0 {
        return Err(format!(
            "runtime profile {} has an invalid clock or voltage",
            profile.profile_id
        ));
    }
    let mut indexes = HashSet::new();
    let mut previous_voltage = None;
    for point in &profile.plan {
        if !indexes.insert(point.index) {
            return Err(format!(
                "runtime profile {} repeats V/F point {}",
                profile.profile_id, point.index
            ));
        }
        if point.voltage_mv <= 0 || point.base_mhz <= 0 || point.target_mhz <= 0 {
            return Err(format!(
                "runtime profile {} contains an invalid V/F point",
                profile.profile_id
            ));
        }
        if i32::try_from(point.new_offset_mhz.saturating_mul(1000)).is_err() {
            return Err(format!(
                "runtime profile {} contains an out-of-range V/F offset",
                profile.profile_id
            ));
        }
        if previous_voltage.is_some_and(|voltage| point.voltage_mv < voltage) {
            return Err(format!(
                "runtime profile {} V/F voltages are not ordered",
                profile.profile_id
            ));
        }
        previous_voltage = Some(point.voltage_mv);
    }
    if profile.memory_offset_mhz.is_some_and(|value| value < 0) {
        return Err(format!(
            "runtime profile {} has a negative memory offset",
            profile.profile_id
        ));
    }
    if profile.power_limit_w.is_some_and(|value| value <= 0) {
        return Err(format!(
            "runtime profile {} has an invalid power limit",
            profile.profile_id
        ));
    }
    let flatten = &profile.flatten_target;
    if flatten.lock_clock_mhz <= 0
        || flatten.lock_voltage_mv.is_some_and(|value| value <= 0)
        || flatten.end_voltage_mv.is_some_and(|value| value <= 0)
        || flatten.tail_point_count.is_some_and(|value| value < 0)
        || flatten.ceiling_clock_mhz.is_some_and(|value| value <= 0)
        || flatten.tail_rise_bins.is_some_and(|value| value < 0)
    {
        return Err(format!(
            "runtime profile {} has an invalid flatten target",
            profile.profile_id
        ));
    }
    Ok(())
}

fn validate_adaptive(adaptive: &AdaptiveSpec) -> Result<(), String> {
    if adaptive.profiles.is_empty() {
        return Err("adaptive runtime spec has no profiles".to_string());
    }
    if !adaptive.profiles.contains_key(&adaptive.initial_tier) {
        return Err("adaptive initial tier is not present in profiles".to_string());
    }
    let mut profile_ids = HashSet::new();
    for (tier, profile) in &adaptive.profiles {
        if !matches!(tier.as_str(), "efficiency" | "balanced" | "performance") {
            return Err(format!("unsupported adaptive tier: {tier}"));
        }
        validate_profile(profile)?;
        if !profile_ids.insert(profile.profile_id.as_str()) {
            return Err(format!(
                "adaptive runtime spec repeats profile id: {}",
                profile.profile_id
            ));
        }
    }
    validate_adaptive_policy(&adaptive.policy)
}

fn validate_adaptive_policy(policy: &AdaptivePolicySpec) -> Result<(), String> {
    if !policy.target_fps.is_finite() || !(1.0..=1000.0).contains(&policy.target_fps) {
        return Err("adaptive target_fps must be finite and in 1..1000".to_string());
    }
    for (name, value) in [
        ("target_slow_windows", policy.target_slow_windows),
        ("near_slow_windows", policy.near_slow_windows),
        ("comfort_windows", policy.comfort_windows),
        (
            "performance_comfort_windows",
            policy.performance_comfort_windows,
        ),
        (
            "frame_cap_confirm_windows",
            policy.frame_cap_confirm_windows,
        ),
    ] {
        if !(1..=120).contains(&value) {
            return Err(format!("adaptive {name} must be in 1..120"));
        }
    }
    for (name, value) in [
        ("demote_dwell_s", policy.demote_dwell_s),
        (
            "performance_demote_dwell_s",
            policy.performance_demote_dwell_s,
        ),
        ("desktop_idle_after_s", policy.desktop_idle_after_s),
    ] {
        if !value.is_finite() || !(0.0..=3600.0).contains(&value) {
            return Err(format!("adaptive {name} must be finite and in 0..3600"));
        }
    }
    for (name, value) in [
        (
            "cpu_bound_gpu_util_max_pct",
            policy.cpu_bound_gpu_util_max_pct,
        ),
        (
            "cpu_bound_peak_thread_min_pct",
            policy.cpu_bound_peak_thread_min_pct,
        ),
        (
            "cpu_bound_process_util_min_pct",
            policy.cpu_bound_process_util_min_pct,
        ),
        ("frame_cap_enter_gpu_pct", policy.frame_cap_enter_gpu_pct),
        ("frame_cap_exit_gpu_pct", policy.frame_cap_exit_gpu_pct),
        (
            "frame_cap_exit_pacing_pct",
            policy.frame_cap_exit_pacing_pct,
        ),
        ("desktop_idle_gpu_pct", policy.desktop_idle_gpu_pct),
    ] {
        if !value.is_finite() || !(0.0..=100.0).contains(&value) {
            return Err(format!("adaptive {name} must be finite and in 0..100"));
        }
    }
    // The two cap thresholds latch a recognised frame cap on and back off.
    // Equal or inverted, the latch cancels itself on the tick after it is set,
    // which is exactly the demote/promote oscillation the latch exists to stop.
    if policy.frame_cap_exit_gpu_pct <= policy.frame_cap_enter_gpu_pct {
        return Err(format!(
            "adaptive frame_cap_exit_gpu_pct ({}) must be above \
             frame_cap_enter_gpu_pct ({})",
            policy.frame_cap_exit_gpu_pct, policy.frame_cap_enter_gpu_pct
        ));
    }
    // An idle bar at or above the cap-entry bar would claim a card working
    // under a frame cap is doing nothing, and step the tier down for a session
    // that is being played.
    if policy.desktop_idle_gpu_pct >= policy.frame_cap_enter_gpu_pct {
        return Err(format!(
            "adaptive desktop_idle_gpu_pct ({}) must be below \
             frame_cap_enter_gpu_pct ({})",
            policy.desktop_idle_gpu_pct, policy.frame_cap_enter_gpu_pct
        ));
    }
    Ok(())
}

fn validate_fan(fan_spec: &FanSpec) -> Result<(), String> {
    let config = &fan_spec.config;
    for (name, value) in [
        ("poll_interval_s", config.poll_interval_s),
        ("hysteresis_c", config.hysteresis_c),
        ("max_step_up_pct_per_s", config.max_step_up_pct_per_s),
        ("max_step_down_pct_per_s", config.max_step_down_pct_per_s),
        ("manual_enable_temp_c", config.manual_enable_temp_c),
        ("auto_restore_temp_c", config.auto_restore_temp_c),
        (
            "emergency_auto_override_temp_c",
            config.emergency_auto_override_temp_c,
        ),
        (
            "emergency_auto_resume_temp_c",
            config.emergency_auto_resume_temp_c,
        ),
    ] {
        if !value.is_finite() {
            return Err(format!("fan {name} must be finite"));
        }
    }
    if !(0.05..=60.0).contains(&config.poll_interval_s) {
        return Err("fan poll_interval_s must be in 0.05..60".to_string());
    }
    if !(0.0..=50.0).contains(&config.hysteresis_c) {
        return Err("fan hysteresis_c must be in 0..50".to_string());
    }
    if !matches!(config.mode.as_str(), "linear" | "step") {
        return Err("fan mode must be linear or step".to_string());
    }
    if !(0..=100).contains(&config.min_fan_speed_pct)
        || !(0..=100).contains(&config.max_fan_speed_pct)
        || config.min_fan_speed_pct > config.max_fan_speed_pct
    {
        return Err("fan speed limits must be ordered in 0..100".to_string());
    }
    if !(0.0..=100.0).contains(&config.max_step_up_pct_per_s)
        || !(0.0..=100.0).contains(&config.max_step_down_pct_per_s)
    {
        return Err("fan step rates must be in 0..100".to_string());
    }
    for (name, value) in [
        ("manual_enable_temp_c", config.manual_enable_temp_c),
        ("auto_restore_temp_c", config.auto_restore_temp_c),
        (
            "emergency_auto_override_temp_c",
            config.emergency_auto_override_temp_c,
        ),
        (
            "emergency_auto_resume_temp_c",
            config.emergency_auto_resume_temp_c,
        ),
    ] {
        if !(-50.0..=150.0).contains(&value) {
            return Err(format!("fan {name} must be in -50..150"));
        }
    }
    fan::validate_curve(&config.curve)
}

pub fn normalize_profile_tier(value: &str, default: &str) -> String {
    match value.trim().to_lowercase().as_str() {
        "eff" | "efficiency" => "efficiency".to_string(),
        "balanced" | "balance" | "bal" | "normal" => "balanced".to_string(),
        "perf" | "performance" | "aggressive" => "performance".to_string(),
        "" => default.to_string(),
        _ => default.to_string(),
    }
}

pub fn profile_tier_label(value: &str) -> String {
    match normalize_profile_tier(value, "").as_str() {
        "efficiency" => "Efficiency",
        "balanced" => "Balanced",
        "performance" => "Performance",
        _ => "",
    }
    .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn policy() -> AdaptivePolicySpec {
        AdaptivePolicySpec {
            target_fps: 60.0,
            target_slow_windows: 3,
            near_slow_windows: 2,
            comfort_windows: 6,
            performance_comfort_windows: 10,
            demote_dwell_s: 60.0,
            performance_demote_dwell_s: 45.0,
            cpu_bound_gpu_util_max_pct: 60.0,
            cpu_bound_peak_thread_min_pct: 97.0,
            cpu_bound_process_util_min_pct: 60.0,
            frame_cap_enter_gpu_pct: default_frame_cap_enter_gpu_pct(),
            frame_cap_exit_gpu_pct: default_frame_cap_exit_gpu_pct(),
            frame_cap_confirm_windows: default_frame_cap_confirm_windows(),
            frame_cap_exit_pacing_pct: default_frame_cap_exit_pacing_pct(),
            desktop_idle_gpu_pct: default_desktop_idle_gpu_pct(),
            desktop_idle_after_s: default_desktop_idle_after_s(),
        }
    }

    #[test]
    fn the_default_bars_are_ordered_idle_below_entry_below_exit() {
        assert!(validate_adaptive_policy(&policy()).is_ok());
    }

    #[test]
    fn frame_cap_exit_bar_must_sit_above_the_enter_bar() {
        // Equal bars make the latch cancel itself on the next tick, which is
        // the demote/promote oscillation it exists to prevent.
        let mut equal = policy();
        equal.frame_cap_exit_gpu_pct = equal.frame_cap_enter_gpu_pct;
        let error = validate_adaptive_policy(&equal).unwrap_err();
        assert!(error.contains("frame_cap_exit_gpu_pct"), "{error}");

        let mut inverted = policy();
        inverted.frame_cap_enter_gpu_pct = 80.0;
        inverted.frame_cap_exit_gpu_pct = 55.0;
        assert!(validate_adaptive_policy(&inverted).is_err());
    }

    #[test]
    fn the_idle_bar_must_sit_below_the_frame_cap_enter_bar() {
        // At or above it, the idle rule would call a card working under a
        // frame cap "doing nothing" and step down a session being played.
        let mut equal = policy();
        equal.desktop_idle_gpu_pct = equal.frame_cap_enter_gpu_pct;
        let error = validate_adaptive_policy(&equal).unwrap_err();
        assert!(error.contains("desktop_idle_gpu_pct"), "{error}");

        let mut above = policy();
        above.desktop_idle_gpu_pct = 70.0;
        assert!(validate_adaptive_policy(&above).is_err());
    }

    #[test]
    fn the_bars_stay_percentages() {
        for mutate in [
            (|p: &mut AdaptivePolicySpec| p.frame_cap_exit_gpu_pct = 140.0)
                as fn(&mut AdaptivePolicySpec),
            |p: &mut AdaptivePolicySpec| p.desktop_idle_gpu_pct = -1.0,
            |p: &mut AdaptivePolicySpec| p.frame_cap_exit_pacing_pct = 250.0,
        ] {
            let mut spec = policy();
            mutate(&mut spec);
            let error = validate_adaptive_policy(&spec).unwrap_err();
            assert!(error.contains("must be finite and in 0..100"), "{error}");
        }
    }

    #[test]
    fn the_confirm_streak_and_idle_delay_are_bounded() {
        let mut zero_windows = policy();
        zero_windows.frame_cap_confirm_windows = 0;
        let error = validate_adaptive_policy(&zero_windows).unwrap_err();
        assert!(error.contains("frame_cap_confirm_windows"), "{error}");

        let mut endless_idle = policy();
        endless_idle.desktop_idle_after_s = 7200.0;
        let error = validate_adaptive_policy(&endless_idle).unwrap_err();
        assert!(error.contains("desktop_idle_after_s"), "{error}");
    }

    #[test]
    fn a_spec_without_the_new_knobs_still_deserializes() {
        // Older clients never send these fields, and the current client omits
        // any it holds at the default; a missing knob must resolve to the
        // built-in default rather than fail an otherwise valid runtime.
        let json = serde_json::json!({
            "target_fps": 60.0,
            "target_slow_windows": 3,
            "near_slow_windows": 2,
            "comfort_windows": 6,
            "performance_comfort_windows": 10,
            "demote_dwell_s": 60.0,
            "performance_demote_dwell_s": 45.0,
            "cpu_bound_gpu_util_max_pct": 60.0,
            "cpu_bound_peak_thread_min_pct": 97.0,
            "cpu_bound_process_util_min_pct": 60.0,
            "frame_cap_enter_gpu_pct": 50.0,
        });
        let parsed: AdaptivePolicySpec = serde_json::from_value(json).unwrap();
        assert_eq!(parsed.frame_cap_exit_gpu_pct, 90.0);
        assert_eq!(parsed.frame_cap_confirm_windows, 3);
        assert_eq!(parsed.frame_cap_exit_pacing_pct, 15.0);
        assert_eq!(parsed.desktop_idle_gpu_pct, 20.0);
        assert_eq!(parsed.desktop_idle_after_s, 60.0);
        assert!(validate_adaptive_policy(&parsed).is_ok());
    }

    fn old_fields_json() -> serde_json::Value {
        serde_json::json!({
            "target_fps": 60.0,
            "target_slow_windows": 3,
            "near_slow_windows": 2,
            "comfort_windows": 6,
            "performance_comfort_windows": 10,
            "demote_dwell_s": 60.0,
            "performance_demote_dwell_s": 45.0,
            "cpu_bound_gpu_util_max_pct": 60.0,
            "cpu_bound_peak_thread_min_pct": 97.0,
            "cpu_bound_process_util_min_pct": 60.0,
        })
    }

    #[test]
    fn an_omitted_policy_tail_resolves_the_documented_defaults() {
        // The client omits any knob at its default, so the values resolved
        // here ARE the applied config for a default-config user. This table
        // is mirrored literal-for-literal by
        // test_the_new_knob_defaults_are_the_documented_wire_contract in
        // tests/test_adaptive_profile_policy.py; a retune on either side must
        // change both, or default clients silently run the other language's
        // number.
        let parsed: AdaptivePolicySpec = serde_json::from_value(old_fields_json()).unwrap();
        assert_eq!(parsed.frame_cap_enter_gpu_pct, 60.0);
        assert_eq!(parsed.frame_cap_exit_gpu_pct, 90.0);
        assert_eq!(parsed.frame_cap_confirm_windows, 3);
        assert_eq!(parsed.frame_cap_exit_pacing_pct, 15.0);
        assert_eq!(parsed.desktop_idle_gpu_pct, 20.0);
        assert_eq!(parsed.desktop_idle_after_s, 60.0);
        assert!(validate_adaptive_policy(&parsed).is_ok());
    }

    #[test]
    fn state_persisted_under_the_old_knob_names_still_loads() {
        // The pre-rename build of this branch persisted boot/active state with
        // the capped_*/desktop_idle_gpu_util_max names; the aliases keep that
        // state loading (and boot replaying) after an upgrade.
        let mut json = old_fields_json();
        json["capped_gpu_util_max_pct"] = 55.0.into();
        json["capped_exit_gpu_util_pct"] = 92.0.into();
        json["desktop_idle_gpu_util_max_pct"] = 11.0.into();
        let parsed: AdaptivePolicySpec = serde_json::from_value(json).unwrap();
        assert_eq!(parsed.frame_cap_enter_gpu_pct, 55.0);
        assert_eq!(parsed.frame_cap_exit_gpu_pct, 92.0);
        assert_eq!(parsed.desktop_idle_gpu_pct, 11.0);
        assert!(validate_adaptive_policy(&parsed).is_ok());
    }

    #[test]
    fn default_knobs_stay_out_of_the_persisted_state_shape() {
        // The daemon re-serializes parsed specs into its boot/active state
        // files. Knobs at their defaults stay out of that shape so a 0.7.x
        // daemon still loads the files after a rollback; a tuned knob is
        // persisted.
        let value = serde_json::to_value(policy()).unwrap();
        for key in [
            "frame_cap_enter_gpu_pct",
            "frame_cap_exit_gpu_pct",
            "frame_cap_confirm_windows",
            "frame_cap_exit_pacing_pct",
            "desktop_idle_gpu_pct",
            "desktop_idle_after_s",
        ] {
            assert!(
                value.get(key).is_none(),
                "default {key} must not be persisted"
            );
        }

        let mut tuned = policy();
        tuned.frame_cap_enter_gpu_pct = 55.0;
        let value = serde_json::to_value(tuned).unwrap();
        assert_eq!(value["frame_cap_enter_gpu_pct"], 55.0);
        assert!(value.get("frame_cap_exit_gpu_pct").is_none());
    }
}

#[cfg(test)]
mod telemetry_flag_tests {
    use super::*;

    fn minimal_spec_json() -> serde_json::Value {
        serde_json::json!({
            "format_version": 1,
            "gpu": {
                "uuid": "GPU-test",
                "index_at_resolution": 0,
                "pci_bus_id": "0000:01:00.0",
                "name": "Test GPU"
            },
            "mode": "stock",
            "static_profile": null,
            "adaptive": null,
            "fan": {
                "enabled": false,
                "config": {
                    "poll_interval_s": 2.0,
                    "curve": [[55.0, 30.0], [80.0, 45.0]],
                    "hysteresis_c": 2.0,
                    "mode": "linear",
                    "min_fan_speed_pct": 20,
                    "max_fan_speed_pct": 100,
                    "max_step_up_pct_per_s": 25.0,
                    "max_step_down_pct_per_s": 15.0,
                    "manual_enable_temp_c": 55.0,
                    "auto_restore_temp_c": 50.0,
                    "emergency_auto_override_temp_c": 80.0,
                    "emergency_auto_resume_temp_c": 75.0,
                    "force_update_every_poll": false,
                    "curve_source": null,
                    "curve_source_path": null
                },
                "notice": ""
            },
            "policy": {"enable_persistence_mode": false},
            "overlay": {"enabled": false, "update_interval_s": 1}
        })
    }

    #[test]
    fn game_telemetry_defaults_to_enabled_for_older_specs() {
        let spec: RuntimeSpec = serde_json::from_value(minimal_spec_json()).expect("spec parses");
        assert!(spec.game_telemetry);
    }

    #[test]
    fn game_telemetry_opt_out_round_trips() {
        let mut json = minimal_spec_json();
        json["game_telemetry"] = serde_json::Value::from(false);
        let spec: RuntimeSpec = serde_json::from_value(json).expect("spec parses");
        assert!(!spec.game_telemetry);
    }
}
