//! argv whitelists + value formatting — load-bearing, byte-exact with the Python
//! daemon (`runtime/daemon_api.py`). See spec 01 §4.2/§4.4.

use serde_json::Value;

/// Auto-UV option key → CLI flag, in the exact order the daemon emits them.
/// Order is load-bearing: it fixes the argv order of the scan command.
pub const AUTO_UV_OPTION_FLAGS: &[(&str, &str)] = &[
    ("gpu_index", "--gpu-index"),
    ("auto_uv_mode", "--auto-uv-mode"),
    ("auto_uv_min_voltage_mv", "--auto-uv-min-voltage-mv"),
    ("auto_uv_memory_offset_mhz", "--auto-uv-memory-offset-mhz"),
    ("auto_uv_power_limit_w", "--auto-uv-power-limit-w"),
    ("auto_uv_tail_rise_bins", "--auto-uv-tail-rise-bins"),
    ("auto_oc_target_voltage_mv", "--auto-oc-target-voltage-mv"),
    ("auto_oc_target_clock_mhz", "--auto-oc-target-clock-mhz"),
    // Per-tier budgets plus targets (targets also apply to single-tier scans).
    (
        "auto_uv_efficiency_power_limit_w",
        "--auto-uv-efficiency-power-limit-w",
    ),
    (
        "auto_uv_efficiency_memory_offset_mhz",
        "--auto-uv-efficiency-memory-offset-mhz",
    ),
    (
        "auto_uv_efficiency_target_voltage_mv",
        "--auto-uv-efficiency-target-voltage-mv",
    ),
    (
        "auto_uv_efficiency_target_clock_mhz",
        "--auto-uv-efficiency-target-clock-mhz",
    ),
    (
        "auto_uv_balanced_power_limit_w",
        "--auto-uv-balanced-power-limit-w",
    ),
    (
        "auto_uv_balanced_memory_offset_mhz",
        "--auto-uv-balanced-memory-offset-mhz",
    ),
    (
        "auto_uv_balanced_target_voltage_mv",
        "--auto-uv-balanced-target-voltage-mv",
    ),
    (
        "auto_uv_balanced_target_clock_mhz",
        "--auto-uv-balanced-target-clock-mhz",
    ),
    (
        "auto_uv_performance_power_limit_w",
        "--auto-uv-performance-power-limit-w",
    ),
    (
        "auto_uv_performance_memory_offset_mhz",
        "--auto-uv-performance-memory-offset-mhz",
    ),
    (
        "auto_uv_performance_target_voltage_mv",
        "--auto-uv-performance-target-voltage-mv",
    ),
    (
        "auto_uv_performance_target_clock_mhz",
        "--auto-uv-performance-target-clock-mhz",
    ),
];

/// Profile-verification option key → CLI flag, in the exact argv order of
/// `ui/commands.py::profile_verify_command` (after the fixed `--stability-test`
/// prefix; the daemon-owned `--stability-stop-request-file` is appended last).
pub const PROFILE_VERIFY_OPTION_FLAGS: &[(&str, &str)] = &[
    ("stability_seconds", "--stability-seconds"),
    ("gpu_index", "--gpu-index"),
    ("auto_uv_profile", "--auto-uv-profile"),
];

/// Build the Auto-UV option flag pairs that follow the fixed scan-command prefix
/// (`_auto_uv_scan_command`). Errors are byte-exact with Python.
pub fn auto_uv_option_args(options: &Value) -> Result<Vec<String>, String> {
    option_args(
        options,
        AUTO_UV_OPTION_FLAGS,
        "start_auto_uv_scan options must be a JSON object",
        "Auto-UV option",
    )
}

/// Build the profile-verification option flag pairs (same option semantics as
/// the scan: whitelist keys, skip null/empty, scalars only).
pub fn profile_verify_option_args(options: &Value) -> Result<Vec<String>, String> {
    option_args(
        options,
        PROFILE_VERIFY_OPTION_FLAGS,
        "start_profile_verification options must be a JSON object",
        "profile verification option",
    )
}

fn option_args(
    options: &Value,
    flags: &[(&str, &str)],
    object_error: &str,
    label: &str,
) -> Result<Vec<String>, String> {
    let object = match options.as_object() {
        Some(object) => object,
        None => return Err(object_error.to_string()),
    };

    let mut unknown: Vec<&str> = object
        .keys()
        .filter(|key| !flags.iter().any(|(known, _)| *known == key.as_str()))
        .map(|key| key.as_str())
        .collect();
    unknown.sort_unstable();
    if !unknown.is_empty() {
        return Err(format!("unknown {label}: {}", unknown.join(", ")));
    }

    let mut args = Vec::new();
    for (key, flag) in flags {
        let value = match object.get(*key) {
            Some(value) => value,
            None => continue,
        };
        // A value that is `None` or `""` is skipped (parity with `value in (None, "")`).
        if value.is_null() {
            continue;
        }
        if let Some(text) = value.as_str() {
            if text.is_empty() {
                continue;
            }
        }
        let text = match command_value_text(value) {
            Some(text) => text,
            None => return Err(format!("{label} {key} must be scalar")),
        };
        args.push((*flag).to_string());
        args.push(text);
    }
    Ok(args)
}

/// Port of `_command_value_text`. `None` means the value is not a scalar
/// (str/int/float/bool). Empty-string/null are handled by the caller before this.
fn command_value_text(value: &Value) -> Option<String> {
    match value {
        Value::Bool(flag) => Some(if *flag { "1" } else { "0" }.to_string()),
        Value::Number(number) => {
            if let Some(int) = number.as_i64() {
                Some(int.to_string())
            } else if let Some(uint) = number.as_u64() {
                Some(uint.to_string())
            } else {
                number.as_f64().map(format_g6)
            }
        }
        Value::String(text) => Some(text.clone()),
        _ => None,
    }
}

/// Format a float exactly like Python's `f"{value:.6g}"` (6 significant digits,
/// general format, trailing zeros stripped, C-style `e±NN` exponent).
pub fn format_g6(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_string();
    }
    if value.is_infinite() {
        return if value < 0.0 { "-inf" } else { "inf" }.to_string();
    }
    const P: i32 = 6;
    let negative = value.is_sign_negative();
    let magnitude = value.abs();
    // `%e` at precision P-1 yields the decision exponent (matches C's %g).
    let exponential = format!("{:.*e}", (P - 1) as usize, magnitude);
    let (mantissa, exp_text) = exponential.split_once('e').expect("LowerExp has 'e'");
    let exponent: i32 = exp_text.parse().expect("LowerExp exponent is an integer");

    let body = if (-4..P).contains(&exponent) {
        let precision = (P - 1 - exponent).max(0) as usize;
        strip_trailing_zeros(&format!("{magnitude:.precision$}"))
    } else {
        let mantissa = strip_trailing_zeros(mantissa);
        let sign = if exponent < 0 { '-' } else { '+' };
        format!("{}e{}{:02}", mantissa, sign, exponent.abs())
    };

    if negative {
        format!("-{body}")
    } else {
        body
    }
}

fn strip_trailing_zeros(text: &str) -> String {
    if text.contains('.') {
        text.trim_end_matches('0').trim_end_matches('.').to_string()
    } else {
        text.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    #[allow(clippy::approx_constant)] // 3.14159265 is a deliberate %.6g test value, not PI.
    fn format_g6_matches_python() {
        // Values + expectations produced by CPython `format(v, ".6g")`.
        let cases: &[(f64, &str)] = &[
            (0.0, "0"),
            (-0.0, "-0"),
            (1.0, "1"),
            (6.0, "6"),
            (6.5, "6.5"),
            (12.0, "12"),
            (12.5, "12.5"),
            (8.25, "8.25"),
            (0.1, "0.1"),
            (2.5, "2.5"),
            (123.456, "123.456"),
            (1234.5678, "1234.57"),
            (0.0001, "0.0001"),
            (0.00012345, "0.00012345"),
            (1e-5, "1e-05"),
            (1e-6, "1e-06"),
            (1e20, "1e+20"),
            (1234567.0, "1.23457e+06"),
            (999999.0, "999999"),
            (999999.9, "1e+06"),
            (100000.0, "100000"),
            (1000000.0, "1e+06"),
            (9.999995, "10"),
            (0.000999999, "0.000999999"),
            (3.14159265, "3.14159"),
            (-3.14159265, "-3.14159"),
            (-0.0001, "-0.0001"),
            (1e100, "1e+100"),
            (1.5e-8, "1.5e-08"),
            (2900.0, "2900"),
            (895.0, "895"),
            (5.999999, "6"),
            (0.30000000000000004, "0.3"),
            (33.333333, "33.3333"),
            (66.66666666, "66.6667"),
        ];
        for (value, expected) in cases {
            assert_eq!(&format_g6(*value), expected, "format_g6({value})");
        }
    }

    #[test]
    fn command_value_text_bool_before_numeric() {
        assert_eq!(command_value_text(&json!(true)).unwrap(), "1");
        assert_eq!(command_value_text(&json!(false)).unwrap(), "0");
        assert_eq!(command_value_text(&json!(0)).unwrap(), "0");
        assert_eq!(command_value_text(&json!(5)).unwrap(), "5");
        assert_eq!(command_value_text(&json!(-7)).unwrap(), "-7");
        assert_eq!(command_value_text(&json!(6.0)).unwrap(), "6");
        assert_eq!(command_value_text(&json!(6.5)).unwrap(), "6.5");
        assert_eq!(command_value_text(&json!("perf")).unwrap(), "perf");
        assert!(command_value_text(&json!([1, 2])).is_none());
        assert!(command_value_text(&json!({"a": 1})).is_none());
    }

    #[test]
    fn scan_command_argv_order_and_prefix() {
        let options = json!({"gpu_index": 0, "auto_uv_mode": "performance"});
        let args = auto_uv_option_args(&options).unwrap();
        assert_eq!(
            args,
            vec!["--gpu-index", "0", "--auto-uv-mode", "performance"]
        );
    }

    #[test]
    fn scan_command_orders_by_flag_map_not_options() {
        // Map order (gpu_index before auto_uv_mode) wins regardless of input order.
        let options = json!({"auto_uv_mode": "eco", "gpu_index": 2});
        let args = auto_uv_option_args(&options).unwrap();
        assert_eq!(args, vec!["--gpu-index", "2", "--auto-uv-mode", "eco"]);
    }

    #[test]
    fn scan_command_skips_null_and_empty() {
        let options = json!({
            "gpu_index": 1,
            "auto_uv_mode": null,
            "auto_uv_min_voltage_mv": "",
            "auto_oc_target_voltage_mv": 925.0,
        });
        let args = auto_uv_option_args(&options).unwrap();
        assert_eq!(
            args,
            vec!["--gpu-index", "1", "--auto-oc-target-voltage-mv", "925"]
        );
    }

    #[test]
    fn scan_command_rejects_unknown_option() {
        let err = auto_uv_option_args(&json!({"unknown": "value"})).unwrap_err();
        assert_eq!(err, "unknown Auto-UV option: unknown");
    }

    #[test]
    fn scan_command_rejects_multiple_unknown_sorted() {
        let err = auto_uv_option_args(&json!({"zeta": 1, "alpha": 2})).unwrap_err();
        assert_eq!(err, "unknown Auto-UV option: alpha, zeta");
    }

    #[test]
    fn scan_command_rejects_non_object() {
        let err = auto_uv_option_args(&json!([1, 2])).unwrap_err();
        assert_eq!(err, "start_auto_uv_scan options must be a JSON object");
    }

    #[test]
    fn scan_command_rejects_non_scalar_value() {
        let err = auto_uv_option_args(&json!({"gpu_index": [1]})).unwrap_err();
        assert_eq!(err, "Auto-UV option gpu_index must be scalar");
    }

    #[test]
    fn verify_command_argv_order_matches_profile_verify_command() {
        // Map order fixes the argv order regardless of the JSON key order.
        let options =
            json!({"auto_uv_profile": "profile-a", "gpu_index": 0, "stability_seconds": 120});
        let args = profile_verify_option_args(&options).unwrap();
        assert_eq!(
            args,
            vec![
                "--stability-seconds",
                "120",
                "--gpu-index",
                "0",
                "--auto-uv-profile",
                "profile-a"
            ]
        );
    }

    #[test]
    fn verify_command_skips_null_and_empty_and_rejects_unknown() {
        let args = profile_verify_option_args(
            &json!({"gpu_index": 1, "auto_uv_profile": "", "stability_seconds": null}),
        )
        .unwrap();
        assert_eq!(args, vec!["--gpu-index", "1"]);

        assert_eq!(
            profile_verify_option_args(&json!({"zeta": 1, "alpha": 2})).unwrap_err(),
            "unknown profile verification option: alpha, zeta"
        );
        assert_eq!(
            profile_verify_option_args(&json!("x")).unwrap_err(),
            "start_profile_verification options must be a JSON object"
        );
        assert_eq!(
            profile_verify_option_args(&json!({"gpu_index": [1]})).unwrap_err(),
            "profile verification option gpu_index must be scalar"
        );
    }
}
