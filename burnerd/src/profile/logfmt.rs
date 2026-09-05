//! Stable, compact journal-line formatting for the runtime engine.
//!
//! The grammar uses a six-character tag column, `" | "` joins, dropped empty
//! sections, and a bucketed status signature to avoid steady-state log spam.

use crate::gpu::round_half_even;

const TAG_WIDTH: usize = 6;

/// Flatten embedded newlines into `" | "`-joined non-empty stripped parts.
pub fn single_line_text(value: &str) -> String {
    value
        .split('\n')
        .map(|part| part.trim_end_matches('\r').trim())
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" | ")
}

/// Drop empty sections, join with `" | "`, and left-pad the tag to width 6.
pub fn format_log_line(tag: &str, sections: &[Option<String>]) -> String {
    let body = sections
        .iter()
        .filter_map(|section| section.as_deref())
        .map(single_line_text)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" | ");
    let tag = tag.trim();
    if tag.is_empty() {
        return body;
    }
    if body.is_empty() {
        return tag.to_string();
    }
    format!("{tag:<TAG_WIDTH$} | {body}")
}

fn int_round(value: f64) -> i64 {
    round_half_even(value) as i64
}

/// Render `"2642MHz @ 860mV"` (or one side, or empty).
pub fn format_clock_voltage(core_clock_mhz: Option<f64>, voltage_mv: Option<f64>) -> String {
    let clock = core_clock_mhz.map(|c| format!("{}MHz", int_round(c)));
    let volt = voltage_mv.map(|v| format!("{}mV", int_round(v)));
    match (clock, volt) {
        (Some(c), Some(v)) => format!("{c} @ {v}"),
        (Some(c), None) => c,
        (None, Some(v)) => v,
        (None, None) => String::new(),
    }
}

/// Render `"fan 35% manual"` / `"fan auto"` / `"fan off"`.
pub fn format_fan_section(fan_pct: Option<f64>, fan_mode: &str) -> String {
    let mode = fan_mode.trim();
    if mode == "disabled" {
        return "fan off".to_string();
    }
    match fan_pct {
        // `format!("fan {mode}")` always starts with "fan", so the trim is never
        // empty (empty mode → "fan"); no empty-string fallback branch needed.
        None => format!("fan {mode}").trim().to_string(),
        Some(pct) => format!("fan {}% {}", int_round(pct), mode)
            .trim()
            .to_string(),
    }
}

#[allow(clippy::too_many_arguments)]
pub fn status_line(
    temp_c: Option<f64>,
    power_w: Option<f64>,
    core_clock_mhz: Option<f64>,
    voltage_mv: Option<f64>,
    fan_pct: Option<f64>,
    fan_mode: &str,
    tier: Option<&str>,
) -> String {
    format_log_line(
        "status",
        &[
            temp_c.map(|t| format!("{t:.0}C")),
            power_w.map(|p| format!("{p:.0}W")),
            Some(format_clock_voltage(core_clock_mhz, voltage_mv)),
            Some(format_fan_section(fan_pct, fan_mode)),
            tier.map(str::to_string),
        ],
    )
}

/// Coarse status signature: continuous fields bucketed, fan/mode/tier exact.
#[derive(Debug, Clone, PartialEq)]
pub struct StatusSignature {
    temp: Option<i64>,
    power: Option<i64>,
    clock: Option<i64>,
    voltage: Option<i64>,
    fan_pct: Option<i64>,
    fan_mode: String,
    tier: Option<String>,
}

fn bucket(value: Option<f64>, size: f64) -> Option<i64> {
    value.map(|v| round_half_even(v / size) as i64)
}

#[allow(clippy::too_many_arguments)]
pub fn status_signature(
    temp_c: Option<f64>,
    power_w: Option<f64>,
    core_clock_mhz: Option<f64>,
    voltage_mv: Option<f64>,
    fan_pct: Option<f64>,
    fan_mode: &str,
    tier: Option<&str>,
) -> StatusSignature {
    StatusSignature {
        temp: bucket(temp_c, 2.0),
        power: bucket(power_w, 10.0),
        clock: bucket(core_clock_mhz, 30.0),
        voltage: bucket(voltage_mv, 5.0),
        fan_pct: fan_pct.map(int_round),
        fan_mode: fan_mode.to_string(),
        tier: tier.map(str::to_string),
    }
}

/// The measurements a tier decision was taken on.
///
/// A reason names the branch that fired; it does not say what the branch saw.
/// Several of these are windowed or compared against a latch held only in
/// memory, so once the tick is over nothing on disk can reconstruct them, and a
/// switching pattern cannot be told apart from a policy bug after the fact.
#[derive(Debug, Clone, Copy, Default)]
pub struct DecisionInputs {
    /// p95 of the base present frametime -- what the policy actually judges.
    pub present_frametime_p95_ms: Option<f64>,
    /// Median of the same set. Recorded but not judged: it is what separates a
    /// genuinely slower GPU (median rises with the tail) from a hitch inside
    /// the window (median holds, tail blows out), which a p95 alone cannot say.
    pub present_frametime_p50_ms: Option<f64>,
    /// Which stream supplied that median, or `None` when none did. The latch
    /// compares medians across ticks, so a switch of source is itself a reason
    /// a comparison can change without the frame rate changing.
    pub median_source: &'static str,
    /// Share of the window past the badly-slow bar -- the quantity the
    /// weakly-hard (m,k) models bound. Recorded, never judged.
    pub present_frametime_miss_ratio: Option<f64>,
    /// Windowed GPU utilisation, which is what the cap branches read. Not the
    /// instantaneous figure the status line prints.
    pub windowed_gpu_util_pct: Option<f64>,
    pub cpu_util_pct: Option<f64>,
    pub cpu_peak_thread_pct: Option<f64>,
    /// Pacing an external cap was recognised at, while that latch is held.
    pub capped_reference_ms: Option<f64>,
    /// Which median the latch was taken against, so a held cap can be read for
    /// what it was judged against.
    pub capped_reference_source: &'static str,
}

/// Render the inputs as `key=value` pairs for the tier line's last field.
pub fn decision_inputs(inputs: &DecisionInputs) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(ms) = inputs.present_frametime_p95_ms {
        parts.push(format!("p95={ms:.1}ms"));
    }
    // Beside the p95 rather than instead of it: the pair is the signal. A
    // spread of roughly 1.0 means the whole distribution moved, which clock can
    // answer; a wide one means a few long frames did, which it cannot.
    if let Some(ms) = inputs.present_frametime_p50_ms {
        parts.push(format!("p50={ms:.1}ms({})", inputs.median_source));
    }
    // How much of the window blew the budget, beside how bad the worst of it
    // was. A single hitch and a scene that is slow throughout can print the
    // same p95; they cannot print the same miss ratio.
    if let Some(ratio) = inputs.present_frametime_miss_ratio {
        parts.push(format!("miss={:.0}%", ratio * 100.0));
    }
    if let Some(gpu) = inputs.windowed_gpu_util_pct {
        parts.push(format!("gpu={gpu:.0}%"));
    }
    if let Some(cpu) = inputs.cpu_util_pct {
        parts.push(format!("cpu={cpu:.0}%"));
    }
    if let Some(peak) = inputs.cpu_peak_thread_pct {
        parts.push(format!("peak={peak:.0}%"));
    }
    // Always stated, including when off: "no latch" is the fact that explains
    // why a capped rate took the slow ladder instead of the cap branch.
    parts.push(match inputs.capped_reference_ms {
        Some(ms) => format!("cap-latch={ms:.1}ms({})", inputs.capped_reference_source),
        None => "cap-latch=off".to_string(),
    });
    parts.join(" ")
}

pub fn tier_switch_line(
    from_tier: &str,
    to_tier: &str,
    reason: &str,
    inputs: &DecisionInputs,
) -> String {
    format_log_line(
        "tier",
        &[
            Some(format!("{from_tier} → {to_tier}")),
            Some(reason.to_string()),
            Some(decision_inputs(inputs)),
        ],
    )
}

pub fn fan_event_line(event: &str, temp_c: Option<f64>) -> String {
    format_log_line(
        "fan",
        &[
            Some(event.to_string()),
            temp_c.map(|t| format!("{t:.0}C")),
            None,
        ],
    )
}

pub fn emergency_line(event: &str, temp_c: Option<f64>, fan_mode: &str) -> String {
    format_log_line(
        "emerg",
        &[
            Some(event.to_string()),
            temp_c.map(|t| format!("{t:.0}C")),
            Some(format_fan_section(None, fan_mode)),
        ],
    )
}

pub fn warn_line(what: &str, detail: &str) -> String {
    format_log_line("warn", &[Some(what.to_string()), Some(detail.to_string())])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn log_line_grammar() {
        assert_eq!(
            format_log_line(
                "status",
                &[
                    Some("58C".into()),
                    Some("257W".into()),
                    Some("fan 35% manual".into()),
                    Some("Balanced".into())
                ]
            ),
            "status | 58C | 257W | fan 35% manual | Balanced"
        );
        assert_eq!(
            format_log_line(
                "warn",
                &[
                    Some("overlay publish unavailable".into()),
                    None,
                    Some("".into())
                ]
            ),
            "warn   | overlay publish unavailable"
        );
    }

    #[test]
    fn status_line_shapes() {
        assert_eq!(
            status_line(
                Some(58.0),
                Some(257.0),
                Some(2642.0),
                Some(860.0),
                Some(35.0),
                "manual",
                Some("Balanced")
            ),
            "status | 58C | 257W | 2642MHz @ 860mV | fan 35% manual | Balanced"
        );
        // Fan control disabled → "fan off", empty tier dropped.
        assert_eq!(
            status_line(
                Some(40.0),
                None,
                Some(210.0),
                None,
                None,
                "disabled",
                Some("")
            ),
            "status | 40C | 210MHz | fan off"
        );
    }

    #[test]
    fn signature_buckets() {
        let a = status_signature(
            Some(58.0),
            Some(257.0),
            Some(2642.0),
            Some(860.0),
            Some(35.0),
            "manual",
            Some("Balanced"),
        );
        // Within the same buckets (temp/2, power/10, clock/30, volt/5) → equal.
        let b = status_signature(
            Some(58.9),
            Some(258.0),
            Some(2650.0),
            Some(861.0),
            Some(35.0),
            "manual",
            Some("Balanced"),
        );
        assert_eq!(a, b);
        // Fan pct differs exactly → not equal.
        let c = status_signature(
            Some(58.0),
            Some(257.0),
            Some(2642.0),
            Some(860.0),
            Some(36.0),
            "manual",
            Some("Balanced"),
        );
        assert_ne!(a, c);
    }

    #[test]
    fn single_line_flattens() {
        assert_eq!(single_line_text("a\n\n  b  \nc"), "a | b | c");
    }

    fn sample_inputs() -> DecisionInputs {
        DecisionInputs {
            present_frametime_p95_ms: Some(16.72),
            present_frametime_p50_ms: Some(16.68),
            median_source: "marker",
            present_frametime_miss_ratio: Some(0.42),
            windowed_gpu_util_pct: Some(76.4),
            cpu_util_pct: Some(8.2),
            cpu_peak_thread_pct: Some(18.9),
            capped_reference_ms: Some(16.7),
            capped_reference_source: "marker",
        }
    }

    #[test]
    fn decision_inputs_render_every_measurement_the_policy_read() {
        assert_eq!(
            decision_inputs(&sample_inputs()),
            "p95=16.7ms p50=16.7ms(marker) miss=42% gpu=76% cpu=8% peak=19% \
             cap-latch=16.7ms(marker)"
        );
    }

    #[test]
    fn a_released_latch_is_stated_rather_than_omitted() {
        // "off" is the fact that explains a capped rate taking the slow ladder,
        // so it has to survive the empty-section drop that eats the others.
        let inputs = DecisionInputs {
            capped_reference_ms: None,
            capped_reference_source: "off",
            ..DecisionInputs::default()
        };
        assert_eq!(decision_inputs(&inputs), "cap-latch=off");
    }

    #[test]
    fn a_median_carries_the_stream_it_came_from() {
        // Marker and pacing medians are not the same measurement; a latch that
        // compares across a source change compares two different things.
        let pacing = DecisionInputs {
            median_source: "pacing",
            ..sample_inputs()
        };
        assert!(decision_inputs(&pacing).contains("p50=16.7ms(pacing)"));
    }

    #[test]
    fn tier_switch_line_carries_the_inputs_in_its_last_field() {
        assert_eq!(
            tier_switch_line(
                "Performance",
                "Balanced",
                "externally-capped",
                &sample_inputs()
            ),
            "tier   | Performance → Balanced | externally-capped | \
             p95=16.7ms p50=16.7ms(marker) miss=42% gpu=76% cpu=8% peak=19% \
             cap-latch=16.7ms(marker)"
        );
    }
}
