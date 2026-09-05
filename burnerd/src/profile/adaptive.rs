//! Adaptive Auto-UV tier policy + runtime controller — port of
//! `runtime/gpu_control/adaptive_profile_policy.py` and
//! `adaptive_profile_runtime.py`. The pure policy is a frametime-p95 state
//! machine with a CPU-bound promotion guard; the runtime controller loads
//! per-tier curves and applies switches to the GPU.

use std::collections::HashMap;

use crate::gpu::GpuBackend;

use super::apply::apply_adaptive_curve;
use super::ceiling::FlattenedClockCeilingController;
use super::guard::select_expected_vf_samples;
use super::latency_rx::METER_SAMPLE_MAX_AGE_S;
use super::logfmt::{tier_switch_line, DecisionInputs};
use super::runtime_spec::{
    normalize_profile_tier, profile_tier_label, AdaptivePolicySpec, LoadedCurve, PlanItem,
    PROFILE_TIERS, PROFILE_TIER_BALANCED, PROFILE_TIER_PERFORMANCE,
};
use super::telemetry::OverlayStatePublisher;
use super::LatencySnapshot;

const CPU_BOUND_GUARD_LOG_THROTTLE_S: f64 = 30.0;

/// How long a band has to hold before `windows` counts as satisfied.
///
/// The band counters advanced once per decision tick. That made every
/// confirmation depend on `overlay.update_interval_s` -- a *display* setting,
/// adjustable 1..10 s -- while the telemetry behind each reading is a fixed
/// 3 s window. Two consequences, both wrong: raising the overlay refresh rate
/// silently shortened every adaptive confirmation, and consecutive ticks
/// re-read overlapping samples, so "3 windows" was never three independent
/// observations.
///
/// Confirmation is elapsed time instead. The first reading in a band already
/// carries one full window of samples, so N windows need (N-1) more windows to
/// pass -- which is what the counters meant to express all along.
fn confirmation_secs(windows: i64) -> f64 {
    (windows - 1).max(0) as f64 * METER_SAMPLE_MAX_AGE_S
}

/// Which stream a median came from.
///
/// Two medians reach the policy and they are not interchangeable: markers
/// describe the frames the game rendered, present pacing describes the frames
/// that reached the screen. Comparing one against the other -- which is what a
/// latch does every tick -- silently compares two different things the moment
/// a stream starts or stops, so the latch has to remember which it took.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum MedianSource {
    #[default]
    None,
    Marker,
    Pacing,
}

impl MedianSource {
    /// Log label. `None` prints as the statistic actually used in its place,
    /// because the only place it is rendered is a latch that falls back to the
    /// tail when no median is on offer.
    pub fn label(self) -> &'static str {
        match self {
            MedianSource::None => "p95",
            MedianSource::Marker => "marker",
            MedianSource::Pacing => "pacing",
        }
    }
}

/// What one tick knows about frame pacing.
#[derive(Debug, Clone, Copy, Default)]
pub struct FrametimeReadings {
    pub p95_ms: Option<f64>,
    pub p50_ms: Option<f64>,
    pub p50_source: MedianSource,
    /// Share of the window past the badly-slow bar. A tail can be one stutter;
    /// a ratio cannot, which is why the branch that jumps straight to the top
    /// tier asks for it.
    pub miss_ratio: Option<f64>,
}

impl From<Option<f64>> for FrametimeReadings {
    /// The p95 alone, for callers (and tests) that have nothing else.
    fn from(p95_ms: Option<f64>) -> Self {
        FrametimeReadings {
            p95_ms,
            p50_ms: None,
            p50_source: MedianSource::None,
            miss_ratio: None,
        }
    }
}

/// A recognised frame cap, and the median it was recognised against.
#[derive(Debug, Clone, Copy)]
struct CappedReference {
    ms: f64,
    source: MedianSource,
}

/// A promotion that has been made and not yet judged.
#[derive(Debug, Clone)]
struct PromotionProbe {
    /// The median at the moment of the promotion, to compare against.
    median_ms: f64,
    /// And where it came from, so the comparison stays like for like.
    median_source: MedianSource,
    /// Where to go back to if the promotion turns out to have done nothing.
    from_tier: String,
    /// When the sample window will hold only frames paced by the new tier.
    due_at: f64,
}

// --- policy config ----------------------------------------------------------

#[derive(Debug, Clone, Copy)]
pub struct PolicyConfig {
    pub comfort_ms: f64,
    pub target_ms: f64,
    pub near_slow_ms: f64,
    pub badly_slow_ms: f64,
    pub target_slow_windows: i64,
    pub near_slow_windows: i64,
    pub comfort_windows: i64,
    pub performance_comfort_windows: i64,
    pub demote_dwell_s: f64,
    pub performance_demote_dwell_s: f64,
    pub cpu_bound_gpu_util_max_pct: f64,
    pub cpu_bound_peak_thread_min_pct: f64,
    pub cpu_bound_process_util_min_pct: f64,
    /// At or below this windowed GPU utilisation the GPU is not what holds
    /// the frame rate back, so a missed target cannot be answered with clock.
    pub frame_cap_enter_gpu_pct: f64,
    /// Utilisation at which a recognised cap is abandoned regardless of pacing.
    ///
    /// Deliberately far above the entry threshold. Entry and exit sharing one
    /// number is precisely what oscillated: a demotion nudges utilisation up a
    /// little, and that little was enough to cancel the recognition. A gap this
    /// wide cannot be crossed by the policy's own step, only by the workload
    /// genuinely saturating the card.
    pub frame_cap_exit_gpu_pct: f64,
    /// Consecutive capped-looking ticks required before a cap is recognised.
    ///
    /// One tick is not evidence: right after frames resume the utilisation
    /// window still describes the previous session, and telemetry publishes a
    /// tick behind the policy, so the first reading of a new game can look
    /// loafing while the card is already flat out. While the streak builds the
    /// tier is held -- neither promoted (the miss may not be a clock problem)
    /// nor eased down (the cap is not established).
    pub frame_cap_confirm_windows: i64,
    /// Share of the window that must have missed the deadline before a single
    /// window may jump straight to the top tier.
    pub badly_slow_miss_ratio_min: f64,
    /// How much worse (percent) than the reference pacing frametime has to get
    /// before the cap is considered gone. A hard cap holds the frame rate
    /// steady no matter which tier runs, so anything past this margin means
    /// the tier -- not the cap -- is now the limit, and the normal ladder
    /// should take over again.
    pub frame_cap_exit_pacing_pct: f64,
    /// At or below this, with nothing presenting, the session counts as a
    /// desktop rather than a game we cannot measure.
    pub desktop_idle_gpu_pct: f64,
    /// How long that has to hold before the tier moves.
    pub desktop_idle_after_s: f64,
    /// How long after a promotion to check whether it changed the pacing.
    ///
    /// A promotion is a prediction -- more clock will move the frame rate --
    /// and this is the only rule that goes back and reads the outcome. Long
    /// enough that the sample window no longer holds frames paced by the tier
    /// that was replaced.
    pub promotion_probe_s: f64,
    /// How far the median may move and still count as not having moved.
    ///
    /// An external limiter holds the median at the same value whichever tier
    /// runs, so "unchanged" -- not "did not improve enough" -- is the
    /// signature being looked for. Pacing that got worse means the scene
    /// changed, which is no evidence about the tier either way.
    pub promotion_probe_tolerance: f64,
}

impl Default for PolicyConfig {
    fn default() -> Self {
        PolicyConfig {
            comfort_ms: 14.5,
            target_ms: 16.6,
            near_slow_ms: 18.5,
            badly_slow_ms: 22.0,
            target_slow_windows: 3,
            near_slow_windows: 2,
            badly_slow_miss_ratio_min: 0.5,
            comfort_windows: 6,
            performance_comfort_windows: 10,
            demote_dwell_s: 60.0,
            performance_demote_dwell_s: 45.0,
            cpu_bound_gpu_util_max_pct: 60.0,
            cpu_bound_peak_thread_min_pct: 97.0,
            cpu_bound_process_util_min_pct: 60.0,
            // 60 matches the cpu-bound guard's "the GPU is not the limiter"
            // bar and the live sessions this feature was validated under
            // (capped games measured 51-58% utilisation at higher tiers; a 40
            // entry bar could never have recognised them). The evidence that
            // easing DOWN demands comes from the confirm streak, not from a
            // stricter utilisation number.
            frame_cap_enter_gpu_pct: 60.0,
            frame_cap_exit_gpu_pct: 90.0,
            frame_cap_confirm_windows: 3,
            frame_cap_exit_pacing_pct: 15.0,
            desktop_idle_gpu_pct: 20.0,
            desktop_idle_after_s: 60.0,
            promotion_probe_s: 2.0 * METER_SAMPLE_MAX_AGE_S,
            promotion_probe_tolerance: 0.02,
        }
    }
}

impl PolicyConfig {
    pub fn for_target_fps(fps: f64) -> Self {
        let default = PolicyConfig::default();
        let target_ms = 1000.0 / fps;
        let scale = target_ms / default.target_ms;
        PolicyConfig {
            comfort_ms: default.comfort_ms * scale,
            target_ms,
            near_slow_ms: default.near_slow_ms * scale,
            badly_slow_ms: default.badly_slow_ms * scale,
            ..default
        }
    }

    fn from_spec(spec: &AdaptivePolicySpec) -> Self {
        let scaled = Self::for_target_fps(spec.target_fps);
        PolicyConfig {
            target_slow_windows: spec.target_slow_windows,
            near_slow_windows: spec.near_slow_windows,
            comfort_windows: spec.comfort_windows,
            performance_comfort_windows: spec.performance_comfort_windows,
            demote_dwell_s: spec.demote_dwell_s,
            performance_demote_dwell_s: spec.performance_demote_dwell_s,
            cpu_bound_gpu_util_max_pct: spec.cpu_bound_gpu_util_max_pct,
            cpu_bound_peak_thread_min_pct: spec.cpu_bound_peak_thread_min_pct,
            cpu_bound_process_util_min_pct: spec.cpu_bound_process_util_min_pct,
            frame_cap_enter_gpu_pct: spec.frame_cap_enter_gpu_pct,
            frame_cap_exit_gpu_pct: spec.frame_cap_exit_gpu_pct,
            frame_cap_confirm_windows: spec.frame_cap_confirm_windows,
            frame_cap_exit_pacing_pct: spec.frame_cap_exit_pacing_pct,
            desktop_idle_gpu_pct: spec.desktop_idle_gpu_pct,
            desktop_idle_after_s: spec.desktop_idle_after_s,
            ..scaled
        }
    }
}

// --- pure policy controller -------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Decision {
    pub tier: String,
    pub changed: bool,
    pub reason: String,
}

#[derive(Debug, Clone)]
struct PolicyState {
    current_tier: String,
    last_switch_monotonic: f64,
    target_slow_since: Option<f64>,
    near_slow_since: Option<f64>,
    comfort_since: Option<f64>,
    /// Pacing observed when an external cap was first recognised.
    ///
    /// GPU utilisation cannot maintain that recognition on its own: dropping a
    /// tier makes the card work harder for the *same* capped frame rate, so
    /// utilisation climbs back over the entry threshold and un-recognises the
    /// cap the demotion just acted on -- tier down, tier up, forever. Frametime
    /// does not move with the tier while a cap holds the rate, so it is the one
    /// signal the policy's own decisions cannot invalidate.
    capped_reference: Option<CappedReference>,
    /// The median this tick reported, and where it came from.
    median_ms: Option<f64>,
    median_source: MedianSource,
    promotion_probe: Option<PromotionProbe>,
    /// Share of this tick's window past the badly-slow bar, when known.
    miss_ratio: Option<f64>,
    /// Consecutive frame ticks that have looked externally capped without a
    /// latch being held yet. Recognition waits for
    /// `frame_cap_confirm_windows` of these; the tier is held while it does.
    frame_cap_streak: i64,
    /// When the current run of quiet, idle-looking ticks started.
    desktop_idle_since: Option<f64>,
    /// Whether the previous tick had frame telemetry. A frames tick after a
    /// gap starts a new session: the utilisation window and the ladder's
    /// counters describe whatever ended, not what just began.
    frames_last_tick: bool,
}

/// How far back the cpu-bound guard looks when judging utilization.
const CPU_BOUND_WINDOW_S: f64 = 8.0;

#[derive(Debug, Clone, Copy)]
struct UtilSample {
    at: f64,
    gpu: Option<f64>,
    cpu: Option<f64>,
    peak_thread: Option<f64>,
}

pub struct AdaptiveProfileController {
    config: PolicyConfig,
    state: PolicyState,
    util_samples: std::collections::VecDeque<UtilSample>,
}

fn ordered_available_tiers(raw: &[String]) -> Vec<String> {
    let normalized: Vec<String> = raw.iter().map(|t| normalize_profile_tier(t, "")).collect();
    PROFILE_TIERS
        .iter()
        .filter(|tier| normalized.iter().any(|n| n == *tier))
        .map(|t| t.to_string())
        .collect()
}

fn higher_tier(current: &str, ordered: &[String]) -> String {
    let idx = ordered.iter().position(|t| t == current).unwrap_or(0);
    ordered[(idx + 1).min(ordered.len() - 1)].clone()
}

fn lower_tier(current: &str, ordered: &[String]) -> String {
    let idx = ordered.iter().position(|t| t == current).unwrap_or(0);
    ordered[idx.saturating_sub(1)].clone()
}

fn highest_non_performance_tier(ordered: &[String]) -> String {
    for tier in ordered.iter().rev() {
        if tier != PROFILE_TIER_PERFORMANCE {
            return tier.clone();
        }
    }
    ordered
        .first()
        .cloned()
        .unwrap_or_else(|| PROFILE_TIER_BALANCED.to_string())
}

fn tier_index(tier: &str, ordered: &[String]) -> i64 {
    ordered
        .iter()
        .position(|t| t == tier)
        .map_or(-1, |i| i as i64)
}

impl AdaptiveProfileController {
    pub fn new(initial_tier: &str, config: PolicyConfig) -> Self {
        AdaptiveProfileController {
            config,
            state: PolicyState {
                current_tier: normalize_profile_tier(initial_tier, PROFILE_TIER_BALANCED),
                last_switch_monotonic: 0.0,
                target_slow_since: None,
                near_slow_since: None,
                comfort_since: None,
                capped_reference: None,
                median_ms: None,
                median_source: MedianSource::None,
                promotion_probe: None,
                miss_ratio: None,
                frame_cap_streak: 0,
                desktop_idle_since: None,
                frames_last_tick: false,
            },
            util_samples: std::collections::VecDeque::new(),
        }
    }

    /// The deadline a frame has to clear to count as on time.
    ///
    /// The badly-slow bar rather than the target: a frame over target is late,
    /// but a frame over *this* is the kind of late the ladder is allowed to
    /// answer by jumping a tier -- so it is the bar worth counting.
    /// Whether one badly-slow window is evidence enough to jump a tier.
    ///
    /// The branch below answers a single window by going straight to the top,
    /// and a promotion takes one window where every step back down pays its
    /// dwell -- so one stutter can outrun several minutes of easing. Two things
    /// argue it is not a stutter: most of the window missed the deadline, and
    /// the median is over target too, not just the tail.
    ///
    /// Missing base-frame readings mean the old behaviour. The present-pacing
    /// median can include generated frames, so it cannot be compared with the
    /// base-frame target. It remains useful for like-for-like cap/probe checks.
    fn badly_slow_is_evidenced(&self) -> bool {
        if self
            .state
            .miss_ratio
            .is_some_and(|ratio| ratio < self.config.badly_slow_miss_ratio_min)
        {
            return false;
        }
        !(self.state.median_source == MedianSource::Marker
            && self
                .state
                .median_ms
                .is_some_and(|median| median <= self.config.target_ms))
    }

    /// The reference a fresh latch is made against: the median where one
    /// exists, else the tail, which is all a stream-less game offers.
    fn pacing_reference(&self, frametime_ms: f64) -> CappedReference {
        match self.state.median_ms {
            Some(ms) if self.state.median_source != MedianSource::None => CappedReference {
                ms,
                source: self.state.median_source,
            },
            _ => CappedReference {
                ms: frametime_ms,
                source: MedianSource::None,
            },
        }
    }

    /// The held cap reference, for the log line that explains a decision.
    fn capped_reference_parts(&self) -> (Option<f64>, &'static str) {
        match self.state.capped_reference {
            Some(reference) => (Some(reference.ms), reference.source.label()),
            None => (None, "off"),
        }
    }

    /// Whether pacing still sits where the cap was recognised.
    ///
    /// `None` when it cannot be told -- the stream that fed the reference is no
    /// longer reporting -- which releases the latch rather than comparing a
    /// marker median against a pacing one and calling the difference movement.
    fn reference_still_holds(&self, reference: CappedReference, frametime_ms: f64) -> Option<bool> {
        let current = if reference.source == MedianSource::None {
            Some(frametime_ms)
        } else if self.state.median_source == reference.source {
            self.state.median_ms
        } else {
            None
        }?;
        let slack = 1.0 + self.config.frame_cap_exit_pacing_pct / 100.0;
        // Symmetric: a cap that lifts shows up as pacing getting *better*, and
        // that has to release the latch too.
        Some(current <= reference.ms * slack && current >= reference.ms / slack)
    }

    fn miss_deadline_ms(&self) -> f64 {
        self.config.badly_slow_ms
    }

    /// Seconds since the band was entered, starting the clock on first entry.
    fn band_held_s(since: &mut Option<f64>, now_monotonic: f64) -> f64 {
        now_monotonic - *since.get_or_insert(now_monotonic)
    }

    fn snapshot_state(&self) -> PolicyState {
        self.state.clone()
    }

    fn restore_state(&mut self, state: &PolicyState) {
        self.state = state.clone();
    }

    fn reset_counts(&mut self) {
        self.state.target_slow_since = None;
        self.state.near_slow_since = None;
        self.state.comfort_since = None;
    }

    #[allow(clippy::too_many_arguments)]
    pub fn update(
        &mut self,
        frametime: impl Into<FrametimeReadings>,
        available_tiers: &[String],
        now_monotonic: f64,
        gpu_util_pct: Option<f64>,
        cpu_util_pct: Option<f64>,
        cpu_peak_thread_pct: Option<f64>,
    ) -> Decision {
        // Frames after a gap are a new session. The utilisation window still
        // holds the previous regime (an idle desktop's 3-4%, or a game that
        // stopped reporting), and the ladder's counters hold its progress:
        // judged against either, the first seconds of a launched game read as
        // "externally capped" and get eased DOWN when they need promotion.
        //
        // The boundary tick's own reading is previous-session data too --
        // telemetry publishes a tick behind the policy -- so it is not
        // recorded either. Seeding the fresh window with it kept the average
        // under the enter bar for the whole confirm streak (one 4% desktop
        // sample drags three readings of anything up to ~88% below 60), and a
        // latch formed off it never re-tests entry: a launch could sit on the
        // low tier for its entire session. The guards fall back to the
        // instantaneous values for the one tick the window is empty, which is
        // the most recent data that exists.
        let frametime = frametime.into();
        let present_frametime_p95_ms = frametime.p95_ms;
        self.state.median_ms = frametime.p50_ms;
        self.state.miss_ratio = frametime.miss_ratio;
        self.state.median_source = if frametime.p50_ms.is_some() {
            frametime.p50_source
        } else {
            MedianSource::None
        };
        let session_boundary = present_frametime_p95_ms.is_some() && !self.state.frames_last_tick;
        if session_boundary {
            self.note_session_boundary();
            self.reset_counts();
        }
        self.state.frames_last_tick = present_frametime_p95_ms.is_some();
        if !session_boundary {
            self.record_util_sample(
                now_monotonic,
                gpu_util_pct,
                cpu_util_pct,
                cpu_peak_thread_pct,
            );
        }
        let ordered = ordered_available_tiers(available_tiers);
        if ordered.len() < 2 {
            let tier = ordered
                .first()
                .cloned()
                .unwrap_or_else(|| self.state.current_tier.clone());
            self.state.current_tier = tier.clone();
            self.reset_counts();
            return Decision {
                tier,
                changed: false,
                reason: "not-enough-tiers".into(),
            };
        }
        if !ordered.contains(&self.state.current_tier) {
            self.state.current_tier = ordered[0].clone();
            self.state.last_switch_monotonic = now_monotonic;
            self.reset_counts();
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: true,
                reason: "snap-to-available".into(),
            };
        }
        let Some(frametime_ms) = present_frametime_p95_ms else {
            // Nothing is presenting: whatever cap was recognised belonged to a
            // session that has gone quiet, and the next one must be judged on
            // its own pacing rather than inheriting this reference.
            self.state.capped_reference = None;
            self.state.promotion_probe = None;
            return self.idle_decision(&ordered, now_monotonic, gpu_util_pct);
        };
        // Frames again: whatever the idle rule had counted towards belongs to a
        // session that is over. Cleared before any ladder branch can run, so a
        // game that starts pays no dwell for the desktop it interrupted.
        self.state.desktop_idle_since = None;

        // A promotion is a prediction -- more clock will move the frame rate --
        // and this is the only rule that goes back and reads the outcome.
        // Utilisation alone cannot settle it: measured live, a capped game sat
        // at 61-66%, above the bar that recognises a cap and below the
        // saturation that would justify the clock, and the ladder climbed back
        // to the top tier 52 times, spending 93% of its time there. The median
        // settles it without a threshold -- it stayed at 16.6 ms whichever
        // tier ran, because a limiter, not the GPU, was setting it.
        if let Some(probe) = self.state.promotion_probe.clone() {
            if now_monotonic >= probe.due_at {
                self.state.promotion_probe = None;
                // A flat-out card is the one case where an unmoved median
                // proves nothing. The check reasons "the clock changed
                // nothing, so the clock was not the limit" -- and at
                // saturation the clock demonstrably IS the limit, so a step
                // too small to show inside the probe window is a small win,
                // not an external cap.
                let saturated = self
                    .windowed_avg(|s| s.gpu)
                    .or(gpu_util_pct)
                    .is_some_and(|gpu| gpu >= self.config.frame_cap_exit_gpu_pct);
                // Dropped, not acted on: the tick carries on to the ladder,
                // which is free to do whatever a saturated card warrants.
                if !saturated {
                    // Same stream on both sides. A session that gains or loses
                    // a marker stream would otherwise read the change of
                    // instrument as the promotion having worked.
                    if let Some(median_ms) = self
                        .state
                        .median_ms
                        .filter(|_| self.state.median_source == probe.median_source)
                    {
                        let moved = (median_ms - probe.median_ms).abs();
                        if moved <= probe.median_ms * self.config.promotion_probe_tolerance {
                            self.state.capped_reference = Some(self.pacing_reference(frametime_ms));
                            return self.switch(
                                probe.from_tier,
                                now_monotonic,
                                "promotion-had-no-effect",
                            );
                        }
                    }
                }
            }
        }

        // Checked before the slow ladder: every branch below reads a missed
        // target as "needs more clock", which is wrong the moment the GPU is
        // not the thing holding the frame rate back.
        //
        // Recognition is latched against the pacing seen when it was made.
        // Utilisation is only the entry hint; re-testing it every tick would
        // let each demotion (which raises utilisation for the same capped rate)
        // cancel the recognition that caused it.
        if let Some(reference) = self.state.capped_reference {
            let saturated = self
                .windowed_avg(|s| s.gpu)
                .or(gpu_util_pct)
                .is_some_and(|gpu| gpu >= self.config.frame_cap_exit_gpu_pct);
            // Judged on the median rather than the tail. A cap holds the median
            // still while the tail keeps moving, so a tail comparison releases
            // the latch on the first stutter under a cap that never lifted --
            // and the tick that releases it falls straight through to the
            // branch that jumps a tier, which is the oscillation.
            //
            // `None` means the stream that fed the reference stopped reporting.
            // Nothing can be concluded from a comparison against a median that
            // no longer exists, so the latch is released rather than guessed at.
            let holds = self.reference_still_holds(reference, frametime_ms);
            if !saturated && holds == Some(true) {
                // Pacing has not moved: the tier still is not the limit.
                return self.ease_down(&ordered, now_monotonic, "externally-capped-held");
            }
            // Either pacing degraded past what the cap was hiding, or the card
            // is now flat out. Release the latch -- but do not hand the tick to
            // the ladder yet, because "this reference stopped explaining the
            // pacing" is not the same claim as "the tier is the limit now".
            self.state.capped_reference = None;
        }

        // Recognition, and re-recognition on the very tick a latch was
        // released. Observed live: a latch held at 17.0ms dropped when pacing
        // jumped to 24.7ms, and the same tick promoted to the top tier on
        // badly-slow -- with the card at 58% and the CPU at 10%, so nothing
        // there was short of clock. Falling straight through skipped the one
        // test that would have said so, and a promotion takes a single window
        // while every step back down pays its dwell.
        //
        // A single capped-looking tick is a hint, not a diagnosis: the streak
        // must reach frame_cap_confirm_windows before the reference latches
        // and easing begins. Until then the tier is held outright -- promoting
        // would answer with clock a miss that does not look like a clock
        // problem, and easing would act on a cap that is not established. The
        // hold deliberately leaves the ladder's own counters untouched.
        if frametime_ms > self.config.target_ms
            && self.externally_capped(gpu_util_pct, cpu_util_pct, cpu_peak_thread_pct)
        {
            self.state.frame_cap_streak += 1;
            if self.state.frame_cap_streak >= self.config.frame_cap_confirm_windows {
                self.state.frame_cap_streak = 0;
                self.state.capped_reference = Some(self.pacing_reference(frametime_ms));
                return self.ease_down(&ordered, now_monotonic, "externally-capped");
            }
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: false,
                reason: "externally-capped-confirm".into(),
            };
        }
        self.state.frame_cap_streak = 0;

        if frametime_ms > self.config.badly_slow_ms && self.badly_slow_is_evidenced() {
            return self.switch_with_cpu_bound_guard(
                ordered[ordered.len() - 1].clone(),
                &ordered,
                now_monotonic,
                "badly-slow",
                gpu_util_pct,
                cpu_util_pct,
                cpu_peak_thread_pct,
            );
        }
        if frametime_ms > self.config.near_slow_ms {
            self.state.target_slow_since = None;
            self.state.comfort_since = None;
            let held_s = Self::band_held_s(&mut self.state.near_slow_since, now_monotonic);
            if held_s >= confirmation_secs(self.config.near_slow_windows) {
                let target = higher_tier(&self.state.current_tier, &ordered);
                return self.switch_with_cpu_bound_guard(
                    target,
                    &ordered,
                    now_monotonic,
                    "clearly-slow",
                    gpu_util_pct,
                    cpu_util_pct,
                    cpu_peak_thread_pct,
                );
            }
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: false,
                reason: "clearly-slow-wait".into(),
            };
        }
        if frametime_ms > self.config.target_ms {
            self.state.near_slow_since = None;
            self.state.comfort_since = None;
            let held_s = Self::band_held_s(&mut self.state.target_slow_since, now_monotonic);
            if held_s >= confirmation_secs(self.config.target_slow_windows) {
                let target = higher_tier(&self.state.current_tier, &ordered);
                return self.switch_with_cpu_bound_guard(
                    target,
                    &ordered,
                    now_monotonic,
                    "near-slow",
                    gpu_util_pct,
                    cpu_util_pct,
                    cpu_peak_thread_pct,
                );
            }
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: false,
                reason: "near-slow-wait".into(),
            };
        }
        if frametime_ms <= self.config.comfort_ms {
            return self.ease_down(&ordered, now_monotonic, "comfort");
        }
        self.reset_counts();
        Decision {
            tier: self.state.current_tier.clone(),
            changed: false,
            reason: "target-ok".into(),
        }
    }

    /// Count one window towards stepping down a tier, and take the step once
    /// the existing hysteresis allows it.
    ///
    /// Shared by the two states that both mean "this tier is more than the
    /// workload needs": pacing comfortably ahead of target, and a GPU that is
    /// plainly not the limiter. Reusing one path keeps a single set of dwell
    /// and window rules rather than inventing a second, differently tuned one.
    fn ease_down(&mut self, ordered: &[String], now_monotonic: f64, reason: &str) -> Decision {
        self.state.target_slow_since = None;
        self.state.near_slow_since = None;
        let held_s = Self::band_held_s(&mut self.state.comfort_since, now_monotonic);
        let at_performance = self.state.current_tier == PROFILE_TIER_PERFORMANCE;
        let required_windows = if at_performance {
            self.config.performance_comfort_windows
        } else {
            self.config.comfort_windows
        };
        let required_dwell = if at_performance {
            self.config.performance_demote_dwell_s
        } else {
            self.config.demote_dwell_s
        };
        let dwell_s = now_monotonic - self.state.last_switch_monotonic;
        if held_s >= confirmation_secs(required_windows) && dwell_s >= required_dwell {
            let target = lower_tier(&self.state.current_tier, ordered);
            return self.switch(target, now_monotonic, reason);
        }
        Decision {
            tier: self.state.current_tier.clone(),
            changed: false,
            reason: format!("{reason}-wait"),
        }
    }

    /// What to do when nothing is presenting frames.
    ///
    /// Holding the tier was the old answer, and it left a tuned card sitting on
    /// a high tier for as long as the desktop was idle. The hard part is that
    /// "nobody is playing" and "a game we cannot measure is running" look
    /// identical from here -- neither produces frame telemetry. Utilisation is
    /// what separates them, and by a wide margin: an idle desktop measured 3-4%
    /// on the development machine, while a game runs 50-90% even with an
    /// external cap holding its frame rate down.
    ///
    /// Entry is slow and exit is immediate, because the costs are not
    /// symmetric. Easing down a minute late on a desktop costs nothing; leaving
    /// a game on a low tier for a minute costs frames in the first seconds of
    /// play, which is when the tool is judged.
    fn idle_decision(
        &mut self,
        ordered: &[String],
        now_monotonic: f64,
        gpu_util_pct: Option<f64>,
    ) -> Decision {
        let busy_now = gpu_util_pct.is_some_and(|gpu| gpu > self.config.desktop_idle_gpu_pct);
        let busy_window = self
            .windowed_avg(|s| s.gpu)
            .or(gpu_util_pct)
            .is_none_or(|gpu| gpu > self.config.desktop_idle_gpu_pct);
        if busy_now || busy_window {
            // Something is working the card without telling us about frames.
            // Not a desktop, so the tier stays where the last measured session
            // left it. The counters go with it: this is the "hold" answer, and
            // easing down counts comfort windows that a quiet tick must not
            // contribute to.
            self.reset_counts();
            self.state.desktop_idle_since = None;
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: false,
                reason: "no-sample".into(),
            };
        }
        let since = *self.state.desktop_idle_since.get_or_insert(now_monotonic);
        if now_monotonic - since < self.config.desktop_idle_after_s {
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: false,
                reason: "desktop-idle-wait".into(),
            };
        }
        self.ease_down(ordered, now_monotonic, "desktop-idle")
    }

    /// True when the frame rate is held by something clocks cannot move.
    ///
    /// A GPU loafing over the whole window while pacing misses target means
    /// the limiter is elsewhere -- a frame cap, vsync, the CPU, IO. Promoting
    /// then burns power for frames that will not arrive, which is exactly
    /// what a menu locked below the target used to do.
    fn externally_capped(
        &self,
        gpu_util_pct: Option<f64>,
        cpu_util_pct: Option<f64>,
        cpu_peak_thread_pct: Option<f64>,
    ) -> bool {
        let gpu_idle = self
            .windowed_avg(|s| s.gpu)
            .or(gpu_util_pct)
            .is_some_and(|gpu| gpu <= self.config.frame_cap_enter_gpu_pct);
        if !gpu_idle {
            return false;
        }
        // A saturated CPU is a different diagnosis with its own, deliberately
        // gentler handling (cap the promotion, do not step down). Leaving that
        // case to the cpu-bound guard keeps the two rules disjoint instead of
        // silently overriding one with the other.
        let cpu_avg = self.windowed_avg(|s| s.cpu).or(cpu_util_pct);
        let peak_avg = self.windowed_avg(|s| s.peak_thread).or(cpu_peak_thread_pct);
        !self.cpu_saturated(cpu_avg, peak_avg)
    }

    #[allow(clippy::too_many_arguments)]
    fn switch_with_cpu_bound_guard(
        &mut self,
        target_tier: String,
        ordered: &[String],
        now_monotonic: f64,
        reason: &str,
        gpu_util_pct: Option<f64>,
        cpu_util_pct: Option<f64>,
        cpu_peak_thread_pct: Option<f64>,
    ) -> Decision {
        let target_tier = normalize_profile_tier(&target_tier, &self.state.current_tier);
        if !self.performance_promotion_cpu_bound(
            &target_tier,
            gpu_util_pct,
            cpu_util_pct,
            cpu_peak_thread_pct,
        ) {
            let previous = self.state.current_tier.clone();
            let decision = self.switch(target_tier, now_monotonic, reason);
            if decision.changed
                && tier_index(&decision.tier, ordered) > tier_index(&previous, ordered)
            {
                self.arm_promotion_probe(previous, now_monotonic);
            }
            return decision;
        }
        let capped = highest_non_performance_tier(ordered);
        if tier_index(&capped, ordered) > tier_index(&self.state.current_tier, ordered) {
            return self.switch(capped, now_monotonic, "cpu-bound-performance-cap");
        }
        self.reset_counts();
        Decision {
            tier: self.state.current_tier.clone(),
            changed: false,
            reason: "cpu-bound-performance-block".into(),
        }
    }

    fn record_util_sample(
        &mut self,
        now_monotonic: f64,
        gpu_util_pct: Option<f64>,
        cpu_util_pct: Option<f64>,
        cpu_peak_thread_pct: Option<f64>,
    ) {
        self.util_samples.push_back(UtilSample {
            at: now_monotonic,
            gpu: gpu_util_pct,
            cpu: cpu_util_pct,
            peak_thread: cpu_peak_thread_pct,
        });
        while let Some(front) = self.util_samples.front() {
            if now_monotonic - front.at > CPU_BOUND_WINDOW_S {
                self.util_samples.pop_front();
            } else {
                break;
            }
        }
    }

    /// Drop the utilization window (post-resume: pre-suspend samples are
    /// hours old by wall time but still "recent" on the monotonic clock the
    /// window is pruned by, and would poison the CPU-bound guard).
    fn clear_util_window(&mut self) {
        self.util_samples.clear();
    }

    /// Forget everything windowed or half-counted: the next frames tick is
    /// judged as the start of a new session. Used on system resume, where a
    /// game presenting on both sides of a suspend never passes through the
    /// no-sample gap that normally marks a boundary -- without this, a
    /// confirm streak from before the lid closed could complete on a single
    /// stale post-resume reading and latch a cap against a wake-up stutter.
    fn note_session_boundary(&mut self) {
        self.clear_util_window();
        self.state.frame_cap_streak = 0;
        // A new session cannot tell us whether the previous session's
        // promotion helped, even when both happen to report the same median.
        self.state.promotion_probe = None;
        self.state.frames_last_tick = false;
    }

    fn windowed_avg(&self, pick: impl Fn(&UtilSample) -> Option<f64>) -> Option<f64> {
        let values: Vec<f64> = self.util_samples.iter().filter_map(&pick).collect();
        if values.is_empty() {
            return None;
        }
        Some(values.iter().sum::<f64>() / values.len() as f64)
    }

    /// The windowed GPU utilisation the guards actually judged, which is not
    /// the instantaneous reading the caller passed in.
    fn windowed_gpu_util_pct(&self) -> Option<f64> {
        self.windowed_avg(|s| s.gpu)
    }

    /// The windowed CPU figures the same branches judge.
    ///
    /// Reported for the same reason as the GPU one: the cap test is half a CPU
    /// test, and an unreported CPU reading silently passes that half. A record
    /// showing one side windowed and the other instantaneous made absent CPU
    /// telemetry look like a policy that ignores the CPU.
    fn windowed_cpu_util_pct(&self) -> Option<f64> {
        self.windowed_avg(|s| s.cpu)
    }

    fn windowed_cpu_peak_thread_pct(&self) -> Option<f64> {
        self.windowed_avg(|s| s.peak_thread)
    }

    fn performance_promotion_cpu_bound(
        &self,
        target_tier: &str,
        gpu_util_pct: Option<f64>,
        cpu_util_pct: Option<f64>,
        cpu_peak_thread_pct: Option<f64>,
    ) -> bool {
        if normalize_profile_tier(target_tier, "") != PROFILE_TIER_PERFORMANCE {
            return false;
        }
        // Judge the last few seconds, not the instant the promotion fires: a
        // menu shader-compile spike (or one calm tick right after it) must
        // not decide a gameplay tier. Falls back to the instantaneous values
        // while the window is still filling.
        let gpu_avg = self.windowed_avg(|s| s.gpu).or(gpu_util_pct);
        let cpu_avg = self.windowed_avg(|s| s.cpu).or(cpu_util_pct);
        let peak_avg = self.windowed_avg(|s| s.peak_thread).or(cpu_peak_thread_pct);
        let Some(gpu) = gpu_avg else {
            return false;
        };
        if gpu > self.config.cpu_bound_gpu_util_max_pct {
            return false;
        }
        self.cpu_saturated(cpu_avg, peak_avg)
    }

    /// Whether the CPU is the plausible explanation for missed frames.
    fn cpu_saturated(&self, cpu_avg: Option<f64>, peak_avg: Option<f64>) -> bool {
        let peak_busy = peak_avg.is_some_and(|v| v >= self.config.cpu_bound_peak_thread_min_pct);
        let process_busy = cpu_avg.is_some_and(|v| v >= self.config.cpu_bound_process_util_min_pct);
        peak_busy || process_busy
    }

    /// Remember what to compare against once the new tier has had time to
    /// show its effect. Without a median there is nothing to judge, so the
    /// promotion simply stands as before.
    fn arm_promotion_probe(&mut self, from_tier: String, now_monotonic: f64) {
        let Some(median_ms) = self.state.median_ms else {
            return;
        };
        if median_ms <= 0.0 || self.state.median_source == MedianSource::None {
            return;
        }
        self.state.promotion_probe = Some(PromotionProbe {
            median_ms,
            median_source: self.state.median_source,
            from_tier,
            due_at: now_monotonic + self.config.promotion_probe_s,
        });
    }

    fn switch(&mut self, target_tier: String, now_monotonic: f64, reason: &str) -> Decision {
        let target_tier = normalize_profile_tier(&target_tier, &self.state.current_tier);
        if target_tier == self.state.current_tier {
            self.reset_counts();
            return Decision {
                tier: self.state.current_tier.clone(),
                changed: false,
                reason: format!("{reason}-already"),
            };
        }
        self.state.current_tier = target_tier.clone();
        self.state.last_switch_monotonic = now_monotonic;
        self.reset_counts();
        // Any tier change makes a pending probe meaningless: it was armed to
        // judge a tier that is no longer the one running. The promotion path
        // re-arms straight after calling this.
        self.state.promotion_probe = None;
        Decision {
            tier: target_tier,
            changed: true,
            reason: reason.to_string(),
        }
    }
}

// --- runtime controller -----------------------------------------------------

pub struct AdaptiveSwitchResult {
    pub changed: bool,
    pub tier: String,
    pub vf_apply_plan: Option<Vec<PlanItem>>,
    pub vf_expected_samples: Vec<PlanItem>,
    pub memory_offset_mhz: Option<i64>,
    /// Power limit the tier switch actually applied (`None` when the tier
    /// carries no limit or the driver skipped it): the loop's post-resume
    /// re-verification must track the CURRENT tier, not the startup value.
    pub applied_power_limit_w: Option<i64>,
}

pub struct AdaptiveAutoUvRuntimeController {
    policy: AdaptiveProfileController,
    tier_curves: HashMap<String, LoadedCurve>,
    available_tiers: Vec<String>,
    last_tier_label: Option<String>,
    last_cpu_bound_guard_log: f64,
    vf_curve_available: bool,
}

impl AdaptiveAutoUvRuntimeController {
    /// A system resume was detected: invalidate windowed utilization state and
    /// any half-built cap recognition so the guards reason only about
    /// post-resume samples.
    /// Deadline the frametime window should count misses against, in
    /// microseconds, so the meter can record the ratio.
    pub fn miss_deadline_us(&self) -> i64 {
        (self.policy.miss_deadline_ms() * 1000.0).round() as i64
    }

    pub fn note_system_resume(&mut self) {
        self.policy.note_session_boundary();
    }

    /// Construct + enumerate tier curves. `log` receives the enable/target lines.
    pub fn new(
        current_tier: &str,
        vf_curve_available: bool,
        tier_curves: HashMap<String, LoadedCurve>,
        policy_spec: &AdaptivePolicySpec,
        log: &mut dyn FnMut(&str),
    ) -> Self {
        // available tiers: PROFILE_TIERS order, deduped by profile_id.
        let mut available_tiers = Vec::new();
        let mut seen: Vec<String> = Vec::new();
        for tier in PROFILE_TIERS {
            let Some(curve) = tier_curves.get(tier) else {
                continue;
            };
            let pid = curve.profile_id.trim().to_string();
            if pid.is_empty() || seen.contains(&pid) {
                continue;
            }
            available_tiers.push(tier.to_string());
            seen.push(pid);
        }

        let initial_tier = if !current_tier.is_empty() {
            current_tier.to_string()
        } else {
            available_tiers.first().cloned().unwrap_or_default()
        };
        let adaptive_target_fps = policy_spec.target_fps;
        let policy_config = PolicyConfig::from_spec(policy_spec);
        let policy = AdaptiveProfileController::new(&initial_tier, policy_config);
        let last_tier_label = if initial_tier.is_empty() {
            None
        } else {
            Some(profile_tier_label(&initial_tier))
        };

        if available_tiers.len() >= 2 {
            let labels = available_tiers
                .iter()
                .map(|t| profile_tier_label(t))
                .collect::<Vec<_>>()
                .join(", ");
            log(&format!("Adaptive Auto-UV enabled for tiers: {labels}."));
            log(&format!(
                "Adaptive Auto-UV target: {} FPS ({:.2} ms p95).",
                format_target_fps(adaptive_target_fps),
                policy_config.target_ms
            ));
        } else if available_tiers.len() == 1 {
            log(&format!(
                "Adaptive Auto-UV: single profile tier available ({}); applying it without tier switching.",
                profile_tier_label(&available_tiers[0])
            ));
        } else {
            log("Adaptive Auto-UV disabled: no profile tiers are available.");
        }

        AdaptiveAutoUvRuntimeController {
            policy,
            tier_curves,
            available_tiers,
            last_tier_label,
            last_cpu_bound_guard_log: -1_000_000_000.0,
            vf_curve_available,
        }
    }

    pub fn enabled(&self) -> bool {
        self.available_tiers.len() >= 2 && self.vf_curve_available
    }

    #[allow(clippy::too_many_arguments)]
    pub fn update(
        &mut self,
        latency_snapshot: Option<&LatencySnapshot>,
        now_monotonic: f64,
        backend: &dyn GpuBackend,
        ceiling: &mut Option<FlattenedClockCeilingController<'_>>,
        publisher: &mut OverlayStatePublisher,
        log: &mut dyn FnMut(&str),
    ) -> Option<AdaptiveSwitchResult> {
        if !self.enabled() {
            return None;
        }
        // The marker median is preferred: it is a median of the same accepted
        // set as the p95, so the two are comparable. Present pacing is the
        // fallback, and the source travels with the value because a latch made
        // against one must never be re-tested against the other.
        let (p50_ms, p50_source) = match latency_snapshot {
            Some(snapshot) => match snapshot.base_present_frametime_p50_ms {
                Some(ms) => (Some(ms), MedianSource::Marker),
                None => match snapshot.present_pacing_p50_ms {
                    Some(ms) => (Some(ms), MedianSource::Pacing),
                    None => (None, MedianSource::None),
                },
            },
            None => (None, MedianSource::None),
        };
        let readings = FrametimeReadings {
            p95_ms: latency_snapshot.and_then(|s| s.base_present_frametime_p95_ms),
            p50_ms,
            p50_source,
            miss_ratio: latency_snapshot.and_then(|s| s.base_present_frametime_miss_ratio),
        };
        let policy_state = self.policy.snapshot_state();
        let gpu = publisher.last_gpu_util_pct().map(|v| v as f64);
        let cpu = publisher.last_cpu_util_pct().map(|v| v as f64);
        let cpu_peak = publisher.last_cpu_peak_thread_pct().map(|v| v as f64);
        let decision = self.policy.update(
            readings,
            &self.available_tiers,
            now_monotonic,
            gpu,
            cpu,
            cpu_peak,
        );
        // After update(), so the latch state printed is the one the decision was
        // taken against rather than the previous tick's.
        let (capped_reference_ms, capped_reference_source) = self.policy.capped_reference_parts();
        let inputs = DecisionInputs {
            present_frametime_p95_ms: readings.p95_ms,
            present_frametime_p50_ms: readings.p50_ms,
            median_source: readings.p50_source.label(),
            present_frametime_miss_ratio: readings.miss_ratio,
            // The windowed figures the branches read, falling back to the
            // instantaneous ones only for the tick the window is empty --
            // which is what the guards themselves do.
            windowed_gpu_util_pct: self.policy.windowed_gpu_util_pct().or(gpu),
            cpu_util_pct: self.policy.windowed_cpu_util_pct().or(cpu),
            cpu_peak_thread_pct: self.policy.windowed_cpu_peak_thread_pct().or(cpu_peak),
            capped_reference_ms,
            capped_reference_source,
        };
        if !decision.changed {
            self.maybe_log_cpu_bound_guard(&decision, now_monotonic, gpu, cpu, cpu_peak, log);
            return Some(AdaptiveSwitchResult {
                changed: false,
                tier: decision.tier,
                vf_apply_plan: None,
                vf_expected_samples: Vec::new(),
                memory_offset_mhz: None,
                applied_power_limit_w: None,
            });
        }
        let Some(curve) = self.tier_curves.get(&decision.tier).cloned() else {
            self.policy.restore_state(&policy_state);
            return Some(AdaptiveSwitchResult {
                changed: false,
                tier: policy_state.current_tier,
                vf_apply_plan: None,
                vf_expected_samples: Vec::new(),
                memory_offset_mhz: None,
                applied_power_limit_w: None,
            });
        };
        match self.apply_curve(
            &decision.tier,
            &curve,
            &decision.reason,
            &inputs,
            backend,
            ceiling,
            publisher,
            log,
        ) {
            Ok(result) => Some(result),
            Err(exc) => {
                self.policy.restore_state(&policy_state);
                log(&format!(
                    "Adaptive Auto-UV switch failed: tier={} reason={} error={exc}",
                    profile_tier_label(&decision.tier),
                    decision.reason
                ));
                Some(AdaptiveSwitchResult {
                    changed: false,
                    tier: policy_state.current_tier,
                    vf_apply_plan: None,
                    vf_expected_samples: Vec::new(),
                    memory_offset_mhz: None,
                    applied_power_limit_w: None,
                })
            }
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn apply_curve(
        &mut self,
        tier: &str,
        curve: &LoadedCurve,
        reason: &str,
        inputs: &DecisionInputs,
        backend: &dyn GpuBackend,
        ceiling: &mut Option<FlattenedClockCeilingController<'_>>,
        publisher: &mut OverlayStatePublisher,
        log: &mut dyn FnMut(&str),
    ) -> Result<AdaptiveSwitchResult, String> {
        let label = profile_tier_label(tier);
        let (power, memory) = apply_adaptive_curve(backend, curve, ceiling, &label, log)?;
        publisher.profile_tier = curve.profile_tier.clone();
        publisher.profile_tier_key = if curve.profile_tier_key.is_empty() {
            tier.to_string()
        } else {
            curve.profile_tier_key.clone()
        };
        publisher.profile_id = curve.profile_id.clone();
        log(&tier_switch_line(
            self.last_tier_label.as_deref().unwrap_or("?"),
            &label,
            reason,
            inputs,
        ));
        self.last_tier_label = Some(label);
        Ok(AdaptiveSwitchResult {
            changed: true,
            tier: tier.to_string(),
            vf_apply_plan: Some(curve.plan.clone()),
            vf_expected_samples: select_expected_vf_samples(&curve.plan, 8),
            memory_offset_mhz: memory,
            applied_power_limit_w: power,
        })
    }

    fn maybe_log_cpu_bound_guard(
        &mut self,
        decision: &Decision,
        now_monotonic: f64,
        gpu: Option<f64>,
        cpu: Option<f64>,
        cpu_peak: Option<f64>,
        log: &mut dyn FnMut(&str),
    ) {
        if decision.reason != "cpu-bound-performance-block" {
            return;
        }
        if now_monotonic - self.last_cpu_bound_guard_log < CPU_BOUND_GUARD_LOG_THROTTLE_S {
            return;
        }
        self.last_cpu_bound_guard_log = now_monotonic;
        log(&format!(
            "Adaptive Auto-UV held below Performance: reason=cpu-bound gpu={}% cpu={}% cpu-t={}%.",
            metric_text(gpu),
            metric_text(cpu),
            metric_text(cpu_peak)
        ));
    }
}

fn metric_text(value: Option<f64>) -> String {
    match value {
        Some(v) => (super::round_half_even(v) as i64).to_string(),
        None => "n/a".to_string(),
    }
}

fn format_target_fps(fps: f64) -> String {
    if fps.fract().abs() < f64::EPSILON {
        format!("{fps:.0}")
    } else {
        format!("{fps:.3}")
            .trim_end_matches('0')
            .trim_end_matches('.')
            .to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tiers() -> Vec<String> {
        vec!["efficiency".into(), "balanced".into(), "performance".into()]
    }

    fn controller() -> AdaptiveProfileController {
        AdaptiveProfileController::new("balanced", PolicyConfig::default())
    }

    #[test]
    fn badly_slow_jumps_to_top_tier() {
        let mut c = controller();
        let d = c.update(Some(30.0), &tiers(), 100.0, None, None, None);
        assert!(d.changed);
        assert_eq!(d.tier, "performance");
        assert_eq!(d.reason, "badly-slow");
    }

    #[test]
    fn near_slow_promotes_once_the_band_has_held_long_enough() {
        let mut c = controller();
        // target_ms 16.6, near_slow_ms 18.5. frametime 17 → target-slow band.
        // target_slow_windows=3, so the band has to hold two further windows
        // (6 s) past the reading that entered it.
        let d1 = c.update(Some(17.0), &tiers(), 1.0, None, None, None);
        assert_eq!(d1.reason, "near-slow-wait");
        let d2 = c.update(Some(17.0), &tiers(), 4.0, None, None, None);
        assert_eq!(d2.reason, "near-slow-wait");
        let d3 = c.update(Some(17.0), &tiers(), 7.0, None, None, None);
        assert!(d3.changed);
        assert_eq!(d3.tier, "performance");
        assert_eq!(d3.reason, "near-slow");
    }

    #[test]
    fn confirmation_does_not_depend_on_how_often_the_loop_ticks() {
        // The counters advanced once per decision tick, so a faster overlay
        // refresh silently shortened every confirmation. Ticking twice as
        // often must not promote twice as fast.
        let mut c = controller();
        let mut last = None;
        for i in 0..7 {
            last = Some(c.update(Some(17.0), &tiers(), 1.0 + i as f64 * 0.5, None, None, None));
        }
        assert_eq!(
            last.expect("ticks ran").reason,
            "near-slow-wait",
            "3 s of frames must not satisfy a 6 s band, however many ticks it took"
        );
    }

    #[test]
    fn comfort_demotes_after_windows_and_dwell() {
        let mut c = controller();
        c.state.current_tier = "performance".into();
        c.state.last_switch_monotonic = 0.0;
        // comfort_ms 14.5; performance requires 10 windows + 45s dwell.
        for i in 1..10 {
            let d = c.update(Some(10.0), &tiers(), i as f64, None, None, None);
            assert_eq!(d.reason, "comfort-wait");
        }
        let d = c.update(Some(10.0), &tiers(), 50.0, None, None, None);
        assert!(d.changed);
        assert_eq!(d.tier, "balanced");
        assert_eq!(d.reason, "comfort");
    }

    #[test]
    fn cpu_bound_window_forgets_shader_compile_spike() {
        // Menu shader compilation (GPU idle, one thread pegged) must not
        // poison the gameplay decision once the utilization window has
        // moved on: after ~8s of real GPU-bound gameplay samples the
        // promotion goes through.
        let mut c = controller();
        for i in 0..4 {
            let t = 100.0 + i as f64 * 2.0;
            let d = c.update(Some(30.0), &tiers(), t, Some(5.0), Some(80.0), Some(99.0));
            assert_ne!(d.tier, "performance", "shader-compile spike must hold");
        }
        // Gameplay: GPU pegged, CPU relaxed. Old samples age out of the
        // 8s window; the badly-slow promotion is then allowed.
        let mut promoted = false;
        for i in 0..8 {
            let t = 108.0 + i as f64 * 2.0;
            let d = c.update(Some(30.0), &tiers(), t, Some(92.0), Some(30.0), Some(60.0));
            if d.tier == "performance" {
                promoted = true;
                break;
            }
        }
        assert!(promoted, "gameplay window must lift the cpu-bound hold");
    }

    #[test]
    fn a_frame_cap_below_target_does_not_promote() {
        // The reported case: target 100 FPS, a menu locked at 60. 16.67 ms is
        // well past badly_slow, so the ladder used to jump straight to the top
        // tier and sit there for the whole menu -- burning power for frames the
        // cap will never let through.
        let mut c =
            AdaptiveProfileController::new("efficiency", PolicyConfig::for_target_fps(100.0));
        for i in 0..6 {
            let t = 100.0 + i as f64 * 2.0;
            // 60 FPS held by the cap, GPU loafing, CPU relaxed: nothing here
            // is short of clock.
            let d = c.update(Some(16.67), &tiers(), t, Some(25.0), Some(20.0), Some(35.0));
            assert_eq!(d.tier, "efficiency", "a capped menu must not promote");
        }
    }

    #[test]
    fn target_ok_deadband_is_not_reclassified_as_an_external_cap() {
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(60.0));
        for i in 0..20 {
            let d = c.update(
                Some(15.5),
                &tiers(),
                1.0 + i as f64 * 5.0,
                Some(25.0),
                Some(20.0),
                Some(35.0),
            );
            assert_eq!(d.tier, "performance", "target-ok must remain a hold");
            assert_eq!(d.reason, "target-ok");
        }
        assert!(c.state.capped_reference.is_none());
    }

    #[test]
    fn a_frame_cap_eases_the_tier_back_down() {
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        let mut eased = None;
        for i in 0..40 {
            let t = 100.0 + i as f64 * 2.0;
            let d = c.update(Some(16.67), &tiers(), t, Some(25.0), Some(20.0), Some(35.0));
            if d.changed {
                eased = Some(d);
                break;
            }
        }
        let decision = eased.expect("a sustained cap must step the tier down");
        assert_eq!(decision.tier, "balanced");
        // "held" rather than plain: the step happens on a tick where the latch
        // was already made and still explains the pacing.
        assert_eq!(decision.reason, "externally-capped-held");
    }

    #[test]
    fn releasing_the_latch_re_tests_the_cap_before_the_ladder_runs() {
        // Observed live 2026-08-24 11:17:25: a latch held at 17.0ms dropped
        // when pacing jumped to 24.7ms, and that same tick promoted to the top
        // tier on badly-slow -- with the card at 58% and the CPU at 10%, where
        // nothing was short of clock. The default enter bar (60) now covers
        // the 58% this was logged at; it used to need an override.
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        // Settle a recognised cap at ~17ms with the card loafing.
        for i in 0..12 {
            c.update(
                Some(17.0),
                &tiers(),
                100.0 + i as f64 * 2.0,
                Some(58.0),
                Some(10.0),
                Some(22.0),
            );
        }
        assert!(
            c.state.capped_reference.is_some(),
            "the cap must be recognised before the release can be tested"
        );

        // Pacing regresses past the reference: the latch has to go.
        let decision = c.update(
            Some(24.7),
            &tiers(),
            130.0,
            Some(58.0),
            Some(10.0),
            Some(22.0),
        );

        assert_ne!(
            decision.reason, "badly-slow",
            "a loafing card must not be answered with clock the tick a latch drops"
        );
        assert!(
            decision.tier != "performance" || !decision.changed,
            "no promotion to the top tier: {decision:?}"
        );
    }

    #[test]
    fn a_saturated_card_still_ends_the_cap_rather_than_re_recognising_it() {
        // The other release path: the card is genuinely flat out, so the
        // re-test must fail and the ladder must get the tick.
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        for i in 0..12 {
            c.update(
                Some(17.0),
                &tiers(),
                100.0 + i as f64 * 2.0,
                Some(30.0),
                Some(10.0),
                Some(22.0),
            );
        }
        assert!(c.state.capped_reference.is_some());

        for i in 0..8 {
            c.update(
                Some(24.7),
                &tiers(),
                130.0 + i as f64 * 2.0,
                Some(97.0),
                Some(10.0),
                Some(22.0),
            );
        }

        assert!(
            c.state.capped_reference.is_none(),
            "a flat-out card must not hold or re-take a cap recognition"
        );
    }

    #[test]
    fn a_recognised_cap_does_not_oscillate_as_utilisation_climbs() {
        // Observed live: easing down made the card work harder for the same
        // capped 60 FPS, utilisation crossed back over the entry threshold, the
        // cap stopped being recognised, and badly-slow jumped straight back to
        // the top tier -- over and over.
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        // 60 FPS held by a cap. Utilisation rises with every step down, exactly
        // as it did on hardware (51% at performance, past 55% at efficiency).
        let utilisation = [30.0, 35.0, 45.0, 58.0, 70.0, 85.0];
        let mut tiers_seen = Vec::new();
        for (index, gpu) in (0..48).map(|i| (i, utilisation[(i / 8).min(5)])) {
            let t = 100.0 + index as f64 * 2.0;
            let d = c.update(Some(16.67), &tiers(), t, Some(gpu), Some(20.0), Some(35.0));
            if d.changed {
                tiers_seen.push((d.tier.clone(), d.reason.clone()));
            }
        }
        assert!(
            // Recognition and every held tick after it both count; what this
            // forbids is a promote reason appearing among them.
            tiers_seen
                .iter()
                .all(|(_, reason)| reason.starts_with("externally-capped")),
            "no promotion may fire while the cap holds: {tiers_seen:?}"
        );
        // Monotonically downwards, never back up.
        let order = ["performance", "balanced", "efficiency"];
        let mut previous = 0;
        for (tier, _) in &tiers_seen {
            let index = order.iter().position(|t| t == tier).unwrap();
            assert!(index >= previous, "tier went back up: {tiers_seen:?}");
            previous = index;
        }
    }

    #[test]
    fn easing_past_what_the_cap_hid_hands_control_back_to_the_ladder() {
        // The escape hatch: if pacing actually degrades after a step down, the
        // tier -- not the cap -- is now the limit.
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        // Spaced so the capped stretch actually serves the ease-down
        // confirmation, which is elapsed time rather than a tick count.
        for i in 0..12 {
            let t = 100.0 + i as f64 * 8.0;
            c.update(Some(16.67), &tiers(), t, Some(25.0), Some(20.0), Some(35.0));
        }
        assert_ne!(
            c.state.current_tier, "performance",
            "the cap must have eased the tier down before the regression"
        );
        // Frames fall well past the capped reference: 16.67 -> 25 ms.
        let mut promoted = false;
        for i in 0..6 {
            let t = 200.0 + i as f64 * 2.0;
            let d = c.update(Some(25.0), &tiers(), t, Some(90.0), Some(20.0), Some(35.0));
            if d.changed && d.reason != "externally-capped" {
                promoted = true;
                assert_eq!(d.tier, "performance");
                break;
            }
        }
        assert!(promoted, "a real regression must release the capped latch");
    }

    #[test]
    fn a_quiet_session_drops_the_capped_reference() {
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        for i in 0..3 {
            c.update(
                Some(16.67),
                &tiers(),
                100.0 + i as f64 * 2.0,
                Some(25.0),
                Some(20.0),
                Some(35.0),
            );
        }
        assert!(c.state.capped_reference.is_some());

        c.update(None, &tiers(), 106.0, Some(25.0), Some(20.0), Some(35.0));

        assert!(
            c.state.capped_reference.is_none(),
            "the next session must be judged on its own pacing"
        );
    }

    #[test]
    fn a_busy_gpu_missing_target_still_promotes() {
        // The guard must not swallow the case it exists to leave alone: a GPU
        // pegged and behind target genuinely wants more clock.
        let mut c =
            AdaptiveProfileController::new("efficiency", PolicyConfig::for_target_fps(100.0));
        let d = c.update(
            Some(16.67),
            &tiers(),
            100.0,
            Some(95.0),
            Some(30.0),
            Some(60.0),
        );
        assert_eq!(d.tier, "performance");
        assert_eq!(d.reason, "badly-slow");
    }

    #[test]
    fn leaving_a_capped_menu_promotes_again_immediately() {
        let mut c =
            AdaptiveProfileController::new("efficiency", PolicyConfig::for_target_fps(100.0));
        for i in 0..4 {
            let t = 100.0 + i as f64 * 2.0;
            c.update(Some(16.67), &tiers(), t, Some(25.0), Some(20.0), Some(35.0));
        }
        // Gameplay starts: the GPU wakes up and the same frametime now means
        // something clock can fix. The capped samples still sit in the 8 s
        // utilisation window, so promotion waits for them to age out -- the
        // same memory the shader-compile guard relies on.
        let mut promoted = false;
        for i in 0..8 {
            let t = 108.0 + i as f64 * 2.0;
            if c.update(Some(16.67), &tiers(), t, Some(96.0), Some(30.0), Some(55.0))
                .tier
                == "performance"
            {
                promoted = true;
                break;
            }
        }
        assert!(
            promoted,
            "gameplay must lift the capped hold once the window clears"
        );
    }

    #[test]
    fn a_single_capped_looking_tick_does_not_latch() {
        // One loafing reading is a hint, not a diagnosis. The tier is held
        // while the streak builds, and a busy tick hands control straight
        // back to the ladder with nothing latched.
        let mut c =
            AdaptiveProfileController::new("efficiency", PolicyConfig::for_target_fps(100.0));
        let first = c.update(
            Some(16.67),
            &tiers(),
            100.0,
            Some(25.0),
            Some(30.0),
            Some(40.0),
        );
        assert_eq!(first.reason, "externally-capped-confirm");
        assert!(!first.changed, "a confirm tick must hold, not move");
        assert!(c.state.capped_reference.is_none());

        let second = c.update(
            Some(16.67),
            &tiers(),
            102.0,
            Some(97.0),
            Some(30.0),
            Some(40.0),
        );
        assert_eq!(second.reason, "badly-slow");
        assert_eq!(second.tier, "performance");
        assert!(c.state.capped_reference.is_none());
    }

    #[test]
    fn a_game_launched_from_an_idle_desktop_is_not_read_as_capped() {
        // The idle rule walks the tier down; then a game launches. The 8s
        // utilisation window is still full of desktop readings, and telemetry
        // publishes a tick behind the policy, so the first frame tick carries
        // a stale idle-era sample. Judged against those, a badly slow launch
        // used to latch "externally-capped" and hold the LOW tier through the
        // first seconds of play -- the exact window the tool is judged on.
        //
        // The sweep covers the whole busy range, not just a pegged card: one
        // stale 4% sample averaged into the fresh window kept anything up to
        // ~88% under the enter bar, and a latch formed that way never re-tests
        // entry -- a 70-88% launch stayed on the low tier for its entire
        // session.
        for game_util in [70.0, 85.0, 95.0] {
            let mut c =
                AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
            // Long enough in wall-clock terms to serve the idle dwell and the
            // ease-down confirmation that follows it, both of which are now
            // elapsed time rather than a number of ticks.
            for i in 0..40 {
                c.update(
                    None,
                    &tiers(),
                    1.0 + i as f64 * 5.0,
                    Some(4.0),
                    Some(5.0),
                    Some(9.0),
                );
            }

            // Launch: badly slow frames; the first tick's utilisation is
            // still the published idle-era value, the rest are the game's.
            let mut promoted = None;
            for i in 0..6 {
                let gpu = if i == 0 { 4.0 } else { game_util };
                let d = c.update(
                    Some(30.0),
                    &tiers(),
                    200.0 + i as f64 * 2.0,
                    Some(gpu),
                    Some(30.0),
                    Some(40.0),
                );
                assert!(
                    !(d.changed && d.reason == "externally-capped"),
                    "a launching game must not be eased down as capped at {game_util}%: {d:?}"
                );
                if d.changed && d.tier == "performance" {
                    promoted = Some(d);
                    break;
                }
            }
            assert!(
                c.state.capped_reference.is_none(),
                "no cap may latch off the desktop's stale window at {game_util}%"
            );
            let decision = promoted.unwrap_or_else(|| {
                panic!("a badly slow {game_util}% launch must climb back within a few ticks")
            });
            assert_eq!(decision.reason, "badly-slow");
        }
    }

    #[test]
    fn a_system_resume_does_not_complete_an_old_streak_on_stale_data() {
        // A game presenting on both sides of a suspend never passes the
        // no-sample boundary, so resume declares the session boundary itself.
        // Without it, a streak one short of latching before the lid closed
        // completed on a single stale sample and latched against a wake-up
        // stutter frametime.
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        for i in 0..2 {
            c.update(
                Some(16.67),
                &tiers(),
                100.0 + i as f64 * 2.0,
                Some(25.0),
                Some(20.0),
                Some(35.0),
            );
        }
        assert!(c.state.capped_reference.is_none());

        c.note_session_boundary();

        let d = c.update(
            Some(40.0),
            &tiers(),
            5000.0,
            Some(25.0),
            Some(20.0),
            Some(35.0),
        );
        assert!(
            c.state.capped_reference.is_none(),
            "the pre-suspend streak must not carry over: {d:?}"
        );
    }

    #[test]
    fn frames_resuming_after_a_gap_clear_stale_comfort_progress() {
        // Post-dwell idle ticks count comfort windows. A game that starts must
        // not inherit that progress, or its first comfortable stretch demotes
        // windows early.
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        for i in 0..36 {
            c.update(
                None,
                &tiers(),
                100.0 + i as f64 * 2.0,
                Some(4.0),
                Some(5.0),
                Some(9.0),
            );
        }
        assert!(
            c.state.comfort_since.is_some(),
            "the idle stretch must have started the ease-down clock"
        );

        c.update(
            Some(8.0),
            &tiers(),
            200.0,
            Some(95.0),
            Some(30.0),
            Some(40.0),
        );

        assert_eq!(
            c.state.comfort_since,
            Some(200.0),
            "the game's comfort clock must start at its own first tick, not the desktop's"
        );
    }

    #[test]
    fn the_pacing_slack_is_configurable() {
        // 24.7ms against a 17.0ms reference is 45% worse: past the default 15%
        // slack, but within a configured 50%.
        let config = PolicyConfig {
            frame_cap_exit_pacing_pct: 50.0,
            ..PolicyConfig::for_target_fps(100.0)
        };
        let mut c = AdaptiveProfileController::new("performance", config);
        for i in 0..3 {
            c.update(
                Some(17.0),
                &tiers(),
                100.0 + i as f64 * 2.0,
                Some(30.0),
                Some(10.0),
                Some(22.0),
            );
        }
        assert!(c.state.capped_reference.is_some());

        let d = c.update(
            Some(24.7),
            &tiers(),
            108.0,
            Some(30.0),
            Some(10.0),
            Some(22.0),
        );

        assert!(
            c.state.capped_reference.is_some(),
            "wider slack must keep the latch through the same regression"
        );
        assert!(d.reason.starts_with("externally-capped"), "{d:?}");
    }

    #[test]
    fn cpu_bound_blocks_performance_promotion() {
        let mut c = controller();
        c.state.current_tier = "balanced".into();
        // badly-slow would jump to performance, but an idle GPU with a truly
        // pegged thread caps it. (Thresholds are deliberately conservative:
        // gpu <= 60, thread >= 97 — a render thread at 70-90% is normal for
        // GPU-bound gameplay and must NOT hold the promotion.)
        let d = c.update(
            Some(30.0),
            &tiers(),
            1.0,
            Some(20.0),
            Some(30.0),
            Some(99.0),
        );
        assert!(!d.changed);
        assert_eq!(d.reason, "cpu-bound-performance-block");
    }

    /// Scripted frametime sequence walked end to end, as a regression net over
    /// the whole ladder rather than one branch.
    ///
    /// This used to pin the sequence against a Python `AdaptiveProfileController`
    /// run by hand. That class no longer exists -- `adaptive_profile_policy.py`
    /// keeps only the config dataclass the spec is built from -- so the daemon
    /// is the sole engine and the expectations are its own. The timestamps are
    /// spaced by the telemetry window, because confirmation is elapsed time.
    #[test]
    fn a_scripted_frametime_sequence_walks_the_ladder() {
        let mut c = controller();
        let seq = [(17.0, 1.0), (17.0, 4.0), (17.0, 7.0), (10.0, 10.0)];
        let expected = [
            ("balanced", false, "near-slow-wait"),
            ("balanced", false, "near-slow-wait"),
            ("performance", true, "near-slow"),
            ("performance", false, "comfort-wait"),
        ];
        for (i, (ft, now)) in seq.iter().enumerate() {
            let d = c.update(Some(*ft), &tiers(), *now, None, None, None);
            assert_eq!(
                (d.tier.as_str(), d.changed, d.reason.as_str()),
                expected[i],
                "step {i}"
            );
        }
    }

    #[test]
    fn no_sample_holds_tier_when_the_card_is_working() {
        // No frame telemetry and a busy card is a game we cannot measure, not
        // a desktop. The tier stays where the last measured session left it.
        let mut c = controller();
        let d = c.update(None, &tiers(), 1.0, Some(70.0), None, None);
        assert!(!d.changed);
        assert_eq!(d.reason, "no-sample");
    }

    #[test]
    fn a_busy_unmeasured_game_immediately_cancels_idle_progress() {
        let mut c = AdaptiveProfileController::new("balanced", PolicyConfig::for_target_fps(100.0));
        for i in 0..35 {
            c.update(
                None,
                &tiers(),
                100.0 + i as f64 * 2.0,
                Some(4.0),
                Some(5.0),
                Some(9.0),
            );
        }
        assert_eq!(c.state.current_tier, "balanced");
        assert!(c.state.comfort_since.is_some());

        let d = c.update(None, &tiers(), 170.0, Some(70.0), None, None);

        assert_eq!(d.tier, "balanced");
        assert_eq!(d.reason, "no-sample");
        assert!(c.state.comfort_since.is_none());
        assert!(c.state.desktop_idle_since.is_none());
    }

    #[test]
    fn no_sample_without_a_utilisation_reading_holds_the_tier() {
        // Nothing measured at all is not evidence of an idle desktop, and the
        // rule steps the tier DOWN, so absence must not be read as zero.
        let mut c = controller();
        let d = c.update(None, &tiers(), 1.0, None, None, None);
        assert!(!d.changed);
        assert_eq!(d.reason, "no-sample");
    }

    #[test]
    fn an_idle_desktop_eases_the_tier_down_after_its_dwell() {
        // The measured case: nothing presenting, card at desktop utilisation.
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        let mut eased = None;
        for i in 0..80 {
            let t = 100.0 + i as f64 * 2.0;
            let d = c.update(None, &tiers(), t, Some(4.0), Some(5.0), Some(9.0));
            if d.changed {
                eased = Some(d);
                break;
            }
            assert_eq!(d.reason, "desktop-idle-wait");
        }
        let decision = eased.expect("an idle desktop must not hold a high tier forever");
        assert_eq!(decision.reason, "desktop-idle");
        assert_eq!(decision.tier, "balanced");
    }

    #[test]
    fn the_idle_dwell_is_not_paid_by_a_game_that_starts() {
        // A quiet stretch before frames arrive must not leave a countdown that
        // fires later during play.
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        for i in 0..20 {
            c.update(
                None,
                &tiers(),
                100.0 + i as f64 * 2.0,
                Some(4.0),
                Some(5.0),
                Some(9.0),
            );
        }
        assert!(
            c.state.desktop_idle_since.is_some(),
            "the idle count must have started"
        );

        // Frames arrive: the countdown belongs to a session that is over.
        c.update(
            Some(8.0),
            &tiers(),
            200.0,
            Some(70.0),
            Some(30.0),
            Some(40.0),
        );

        assert!(
            c.state.desktop_idle_since.is_none(),
            "a game must not inherit the desktop's countdown"
        );
    }

    #[test]
    fn runtime_spec_policy_values_are_used() {
        let spec = AdaptivePolicySpec {
            target_fps: 60.0,
            target_slow_windows: 2,
            near_slow_windows: 4,
            comfort_windows: 7,
            performance_comfort_windows: 11,
            demote_dwell_s: 50.0,
            performance_demote_dwell_s: 40.0,
            cpu_bound_gpu_util_max_pct: 80.0,
            cpu_bound_peak_thread_min_pct: 75.0,
            cpu_bound_process_util_min_pct: 15.0,
            frame_cap_enter_gpu_pct: 30.0,
            frame_cap_exit_gpu_pct: 85.0,
            frame_cap_confirm_windows: 2,
            frame_cap_exit_pacing_pct: 25.0,
            desktop_idle_gpu_pct: 18.0,
            desktop_idle_after_s: 45.0,
        };
        let config = PolicyConfig::from_spec(&spec);
        assert_eq!(config.target_slow_windows, 2);
        assert_eq!(config.near_slow_windows, 4);
        assert_eq!(config.comfort_windows, 7);
        assert_eq!(config.frame_cap_enter_gpu_pct, 30.0);
        assert_eq!(config.frame_cap_exit_gpu_pct, 85.0);
        assert_eq!(config.frame_cap_confirm_windows, 2);
        assert_eq!(config.frame_cap_exit_pacing_pct, 25.0);
        assert_eq!(config.desktop_idle_gpu_pct, 18.0);
        assert_eq!(config.desktop_idle_after_s, 45.0);
        assert_eq!(config.performance_comfort_windows, 11);
        assert_eq!(config.demote_dwell_s, 50.0);
        assert_eq!(config.cpu_bound_gpu_util_max_pct, 80.0);
    }

    fn readings(p95: f64, p50: f64, source: MedianSource, miss: f64) -> FrametimeReadings {
        FrametimeReadings {
            p95_ms: Some(p95),
            p50_ms: Some(p50),
            p50_source: source,
            miss_ratio: Some(miss),
        }
    }

    #[test]
    fn one_slow_window_does_not_jump_a_tier_without_evidence() {
        // A promotion takes a single window; every step back down pays its
        // dwell. So one stutter can undo minutes of easing, and the tick that
        // releases a cap latch falls straight into this branch -- which is the
        // demote/promote oscillation seen live.
        let mut c = AdaptiveProfileController::new("balanced", PolicyConfig::for_target_fps(100.0));

        // The card is busy, so the cap branch does not claim this tick and it
        // reaches the promote. Tail well past badly-slow, but the median is on
        // target and almost nothing missed: a stutter, not a slow session.
        let d = c.update(
            readings(30.0, 9.8, MedianSource::Marker, 0.05),
            &tiers(),
            100.0,
            Some(95.0),
            Some(20.0),
            Some(35.0),
        );
        assert_ne!(d.reason, "badly-slow", "one stutter is not evidence");
        assert_eq!(d.tier, "balanced");
    }

    #[test]
    fn a_window_that_really_is_slow_still_jumps() {
        // The veto must not cost a genuine promotion: most of the window over
        // the deadline and a median past target is exactly the case the branch
        // exists for.
        let mut c = AdaptiveProfileController::new("balanced", PolicyConfig::for_target_fps(100.0));

        let d = c.update(
            readings(30.0, 25.0, MedianSource::Marker, 0.9),
            &tiers(),
            100.0,
            Some(95.0),
            Some(20.0),
            Some(35.0),
        );

        assert_eq!(d.reason, "badly-slow");
        assert_eq!(d.tier, "performance");
    }

    /// Promote on a genuinely slow window, at a utilisation that is neither
    /// capped-looking nor saturated, so the probe arms and is judged on merit.
    fn promoted_with_probe_armed(median_ms: f64) -> AdaptiveProfileController {
        let mut c = AdaptiveProfileController::new("balanced", PolicyConfig::for_target_fps(100.0));
        let d = c.update(
            readings(30.0, median_ms, MedianSource::Marker, 0.9),
            &tiers(),
            100.0,
            Some(66.0),
            Some(20.0),
            Some(35.0),
        );
        assert_eq!(d.tier, "performance", "setup must promote");
        assert!(
            c.state.promotion_probe.is_some(),
            "setup must arm the probe"
        );
        c
    }

    #[test]
    fn a_promotion_that_did_not_move_the_median_is_taken_back() {
        // A limiter holds the median at the same value whichever tier runs, so
        // an unmoved median after the probe window says the clock was never
        // the limit -- and the tier that was paid for it is given back.
        let mut c = promoted_with_probe_armed(25.0);

        let judged = c.update(
            readings(30.0, 25.0, MedianSource::Marker, 0.9),
            &tiers(),
            107.0,
            Some(66.0),
            Some(20.0),
            Some(35.0),
        );

        assert_eq!(judged.reason, "promotion-had-no-effect");
        assert_eq!(judged.tier, "balanced");
        assert!(
            c.state.capped_reference.is_some(),
            "the revert latches, so the ladder does not immediately re-promote"
        );
    }

    #[test]
    fn a_promotion_probe_does_not_cross_a_no_sample_gap() {
        let mut c = promoted_with_probe_armed(25.0);
        let quiet = c.update(None, &tiers(), 104.0, Some(70.0), Some(20.0), Some(35.0));
        assert_eq!(quiet.reason, "no-sample");

        // The next session happens to report the old median. It says nothing
        // about whether the previous session benefited from its promotion.
        let resumed = c.update(
            readings(30.0, 25.0, MedianSource::Marker, 0.9),
            &tiers(),
            110.0,
            Some(70.0),
            Some(20.0),
            Some(35.0),
        );
        assert_eq!(resumed.tier, "performance", "{resumed:?}");
        assert!(c.state.capped_reference.is_none());
    }

    #[test]
    fn a_promotion_probe_does_not_cross_system_resume() {
        let mut c = promoted_with_probe_armed(25.0);
        // Resume can happen without an intervening no-sample tick.
        c.note_session_boundary();

        let resumed = c.update(
            readings(30.0, 25.0, MedianSource::Marker, 0.9),
            &tiers(),
            110.0,
            Some(70.0),
            Some(20.0),
            Some(35.0),
        );
        assert_eq!(resumed.tier, "performance", "{resumed:?}");
        assert!(c.state.capped_reference.is_none());
    }

    #[test]
    fn the_probe_is_not_judged_before_its_window_has_passed() {
        // Judged early it would read frames still paced by the tier that was
        // replaced, which is the one thing the delay exists to avoid.
        let mut c = promoted_with_probe_armed(25.0);

        let early = c.update(
            readings(30.0, 25.0, MedianSource::Marker, 0.9),
            &tiers(),
            103.0,
            Some(66.0),
            Some(20.0),
            Some(35.0),
        );

        assert_ne!(early.reason, "promotion-had-no-effect");
        assert!(c.state.promotion_probe.is_some(), "the probe still stands");
    }

    #[test]
    fn a_promotion_that_moved_the_frame_rate_is_kept() {
        // The direction that matters most: the clock did what was predicted,
        // so the tier stays. Reverting here would undo every promotion that
        // worked and leave the ladder unable to hold a win at all.
        let mut c = promoted_with_probe_armed(25.0);

        let judged = c.update(
            readings(22.0, 19.0, MedianSource::Marker, 0.4),
            &tiers(),
            107.0,
            Some(66.0),
            Some(20.0),
            Some(35.0),
        );

        assert_ne!(judged.reason, "promotion-had-no-effect");
        assert_eq!(c.state.current_tier, "performance");
    }

    #[test]
    fn pacing_that_got_worse_is_not_evidence_against_the_promotion() {
        // The signature being looked for is "did not move", not "did not
        // improve". A median that got worse means the scene changed, which
        // says nothing about the tier either way.
        let mut c = promoted_with_probe_armed(25.0);

        let judged = c.update(
            readings(36.0, 31.0, MedianSource::Marker, 0.9),
            &tiers(),
            107.0,
            Some(66.0),
            Some(20.0),
            Some(35.0),
        );

        assert_ne!(judged.reason, "promotion-had-no-effect");
        assert_eq!(c.state.current_tier, "performance");
    }

    #[test]
    fn a_flat_out_card_keeps_the_promotion_it_earned() {
        // The check reasons "the clock changed nothing, so the clock was not
        // the limit". At saturation the clock demonstrably is the limit, so an
        // unmoved median there is a win too small to show, not a cap.
        let mut c = promoted_with_probe_armed(25.0);

        let judged = c.update(
            readings(30.0, 25.0, MedianSource::Marker, 0.9),
            &tiers(),
            107.0,
            Some(95.0),
            Some(20.0),
            Some(35.0),
        );

        assert_ne!(judged.reason, "promotion-had-no-effect");
        assert_eq!(c.state.current_tier, "performance");
    }

    #[test]
    fn medians_from_different_streams_are_never_compared() {
        // A game that starts reporting markers mid-session changes what the
        // median is measured from. Comparing across that switch would read the
        // change of instrument as a verdict on the tier.
        let mut c = promoted_with_probe_armed(25.0);

        let judged = c.update(
            readings(30.0, 25.0, MedianSource::Pacing, 0.9),
            &tiers(),
            107.0,
            Some(66.0),
            Some(20.0),
            Some(35.0),
        );

        assert_ne!(judged.reason, "promotion-had-no-effect");
        assert_eq!(c.state.current_tier, "performance");
    }

    #[test]
    fn without_a_median_the_promotion_stands_as_before() {
        // Nothing to compare against is not evidence against the tier, so the
        // probe never arms and the ladder behaves exactly as it used to.
        let mut c = AdaptiveProfileController::new("balanced", PolicyConfig::for_target_fps(100.0));

        let d = c.update(
            FrametimeReadings {
                p95_ms: Some(30.0),
                p50_ms: None,
                p50_source: MedianSource::None,
                miss_ratio: Some(0.9),
            },
            &tiers(),
            100.0,
            Some(66.0),
            Some(20.0),
            Some(35.0),
        );

        assert_eq!(d.tier, "performance");
        assert!(c.state.promotion_probe.is_none());
    }

    #[test]
    fn a_game_with_no_readings_keeps_the_old_behaviour() {
        // No marker stream means neither figure exists. Refusing to promote
        // there would be worse than the oscillation this damps.
        let mut c = AdaptiveProfileController::new("balanced", PolicyConfig::for_target_fps(100.0));

        let d = c.update(
            Some(30.0),
            &tiers(),
            100.0,
            Some(95.0),
            Some(20.0),
            Some(35.0),
        );

        assert_eq!(d.reason, "badly-slow");
    }

    #[test]
    fn the_reported_utilisation_is_the_window_the_guards_read() {
        // The record has to show the figure the policy judged. Reporting the
        // instantaneous reading beside a windowed decision makes a correct
        // decision look arbitrary -- and the field is named "windowed".
        let mut c = controller();
        for (i, gpu) in [10.0, 20.0, 60.0].iter().enumerate() {
            c.update(
                Some(16.0),
                &tiers(),
                100.0 + i as f64 * 0.5,
                Some(*gpu),
                Some(*gpu / 2.0),
                Some(*gpu),
            );
        }

        // The first tick with frames is a session boundary, which clears the
        // window, so what remains is the two ticks after it -- averaged, not
        // the last one.
        assert_eq!(
            c.windowed_gpu_util_pct(),
            Some(40.0),
            "the mean of the window, not the last sample"
        );
        assert_eq!(c.windowed_cpu_util_pct(), Some(20.0));
        assert_eq!(c.windowed_cpu_peak_thread_pct(), Some(40.0));
    }

    #[test]
    fn the_logged_latch_reports_the_measurement_it_was_taken_against() {
        // The log line is the only record of a latch: it lives in memory and is
        // gone by the time anyone reads the journal. Reporting the wrong median
        // source there makes a held cap unreadable after the fact.
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        assert_eq!(c.capped_reference_parts(), (None, "off"));

        for i in 0..4 {
            c.update(
                readings(16.67, 16.6, MedianSource::Pacing, 0.9),
                &tiers(),
                100.0 + i as f64 * 2.0,
                Some(25.0),
                Some(20.0),
                Some(35.0),
            );
        }
        let (ms, source) = c.capped_reference_parts();
        assert_eq!(source, "pacing", "the stream that fed the latch");
        assert!(
            (ms.expect("a sustained cap latches") - 16.6).abs() < 0.01,
            "reports the latched median, got {ms:?}"
        );
    }

    #[test]
    fn a_cap_is_latched_against_the_median_not_the_tail() {
        // Under a cap the median sits still while the tail wanders. Latching on
        // the tail releases on the first stutter, and the releasing tick is the
        // one that promotes.
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        for i in 0..4 {
            c.update(
                readings(16.67, 16.6, MedianSource::Marker, 0.9),
                &tiers(),
                100.0 + i as f64 * 2.0,
                Some(25.0),
                Some(20.0),
                Some(35.0),
            );
        }
        let reference = c.state.capped_reference.expect("a sustained cap latches");
        assert_eq!(reference.source, MedianSource::Marker);
        assert!(
            (reference.ms - 16.6).abs() < 0.01,
            "latched on the median, got {}",
            reference.ms
        );

        // Tail jumps, median does not: the cap has not lifted.
        let d = c.update(
            readings(24.7, 16.6, MedianSource::Marker, 0.9),
            &tiers(),
            120.0,
            Some(58.0),
            Some(10.0),
            Some(20.0),
        );
        assert!(
            d.reason.starts_with("externally-capped"),
            "a moving tail must not release a held cap, got {}",
            d.reason
        );
    }

    #[test]
    fn a_latch_is_released_when_its_stream_stops_reporting() {
        // Comparing a marker median against a pacing one calls the difference
        // between two different measurements "movement". Nothing can be
        // concluded, so the latch goes rather than being guessed at.
        let mut c =
            AdaptiveProfileController::new("performance", PolicyConfig::for_target_fps(100.0));
        for i in 0..4 {
            c.update(
                readings(16.67, 16.6, MedianSource::Marker, 0.9),
                &tiers(),
                100.0 + i as f64 * 2.0,
                Some(25.0),
                Some(20.0),
                Some(35.0),
            );
        }
        assert!(c.state.capped_reference.is_some());

        c.update(
            readings(16.67, 16.6, MedianSource::Pacing, 0.9),
            &tiers(),
            120.0,
            Some(25.0),
            Some(20.0),
            Some(35.0),
        );

        assert!(
            c.state.capped_reference.is_none(),
            "a reference whose stream stopped must not be re-tested against another"
        );
    }
}
