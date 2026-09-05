import pytest

from cli.arguments import parse_arguments


def _help_output(capsys, parser, argv=None):
    with pytest.raises(SystemExit) as exc:
        parser(["--help"] if argv is None else argv)
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_main_cli_help_hides_default_and_unclear_compat_flags(capsys):
    help_text = _help_output(capsys, parse_arguments)

    assert "--foreground" not in help_text
    assert "--power-limit-override-w" not in help_text
    assert "--auto-uv-power-limit-w" in help_text


def test_main_cli_help_includes_gui_auto_uv_scan_options(capsys):
    help_text = _help_output(capsys, parse_arguments)

    visible_gui_scan_flags = [
        "--auto-uv-voltage-scan",
        "--auto-uv-mode",
        "--gpu-index",
        "--auto-uv-min-voltage-mv",
        "--auto-uv-memory-offset-mhz",
        "--auto-uv-power-limit-w",
        "--auto-uv-tail-rise-bins",
        "--auto-oc-target-voltage-mv",
        "--auto-oc-target-clock-mhz",
    ]
    for flag in visible_gui_scan_flags:
        assert flag in help_text


def test_main_cli_help_includes_profile_tier_assignment(capsys):
    help_text = _help_output(capsys, parse_arguments)

    assert "--assign-auto-uv-tier" in help_text
    assert "efficiency" in help_text
    assert "balanced" in help_text
    assert "performance" in help_text
    assert "none" in help_text


@pytest.mark.parametrize("tier", [None, "efficiency", "balanced", "performance"])
def test_clock_loss_overrides_are_removed(tier, capsys):
    prefix = "--auto-uv-" if tier is None else f"--auto-uv-{tier}-"
    flag = prefix + "max-clock-drop-pct"
    assert flag not in _help_output(capsys, parse_arguments)
    with pytest.raises(SystemExit) as exc:
        parse_arguments([flag, "20"])
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_main_cli_help_includes_main_gpu_controls(capsys):
    help_text = _help_output(capsys, parse_arguments)

    assert "--set-main-gpu" in help_text
    assert "--clear-main-gpu" in help_text


def test_main_cli_help_removes_old_compat_flags(capsys):
    help_text = _help_output(capsys, parse_arguments)

    removed_flags = [
        "--foreground",
        "--power-limit-override-w",
        "--show-q2rtx-window",
        "--stability-q2rtx-dir",
        "--stability-q2rtx-binary",
        "--prefer-afterburner-curve",
        "--restore-defaults-from-config",
        "--preserve-vf-below-mv",
        "--dangerously-skip-validation",
        "--afterburner-dir",
        "--profile-section",
        "--section",
        "--afterburner-device-profile",
        "--dry-run",
        "--export-lact-config",
        "--lact-source",
        "--auto-uv-max-drop-pct",
        "--auto-uv-final-seconds",
        "--auto-uv-short-seconds",
        "--install-q2rtx",
        "--overlay-toggle",
        "--overlay-enable",
        "--overlay-disable",
        "--check-latency-layer",
        "--dump-latency-data",
    ]
    for flag in removed_flags:
        assert flag not in help_text


def test_main_cli_help_hides_internal_profile_verification_flags(capsys):
    help_text = _help_output(capsys, parse_arguments)

    assert "--stability-test" not in help_text
    assert "--stability-seconds" not in help_text
    assert "--stability-workload" not in help_text
    assert "--stability-width" not in help_text
    assert "--stability-height" not in help_text
    assert "--stability-log-dir" not in help_text
    assert "--show-q2rtx-window" not in help_text
    assert "--stability-q2rtx-dir" not in help_text
    assert "--stability-q2rtx-binary" not in help_text


@pytest.mark.parametrize(
    "argv",
    [
        ["--foreground"],
        ["--power-limit-override-w", "320"],
        ["--show-q2rtx-window"],
        ["--stability-q2rtx-dir", "/tmp/q2rtx"],
        ["--stability-q2rtx-binary", "/tmp/q2rtx/q2rtx"],
        ["--prefer-afterburner-curve"],
        ["--restore-defaults-from-config"],
        ["--preserve-vf-below-mv", "800"],
        ["--dangerously-skip-validation"],
        ["--afterburner-dir", "/tmp/afterburner"],
        ["--profile-section", "profile1"],
        ["--section", "profile1"],
        ["--afterburner-device-profile", "GPU0.cfg"],
        ["--dry-run"],
        ["--export-lact-config", "/tmp/lact.yaml"],
        ["--lact-source", "auto-uv"],
        ["--auto-uv-max-drop-pct", "15"],
        ["--auto-uv-final-seconds", "600"],
        ["--auto-uv-short-seconds", "10"],
        ["--install-q2rtx"],
        ["--overlay-toggle"],
        ["--overlay-enable"],
        ["--overlay-disable"],
        ["--check-latency-layer"],
        ["--dump-latency-data"],
    ],
)
def test_removed_main_cli_flags_are_rejected(argv):
    with pytest.raises(SystemExit) as exc:
        parse_arguments(argv)
    assert exc.value.code == 2


def test_auto_uv_power_limit_flag_is_accepted():
    args = parse_arguments(["--auto-uv-power-limit-w", "390"])

    assert args.auto_uv_power_limit_w == 390


def test_profile_tier_assignment_flag_is_accepted():
    args = parse_arguments(["--assign-auto-uv-tier", "profile-a", "balanced"])

    assert args.assign_auto_uv_tier == ["profile-a", "balanced"]


def test_gui_auto_uv_scan_flags_are_accepted():
    args = parse_arguments(
        [
            "--auto-uv-voltage-scan",
            "--auto-uv-mode",
            "performance",
            "--gpu-index",
            "1",
            "--auto-uv-min-voltage-mv",
            "850",
            "--auto-uv-memory-offset-mhz",
            "500",
            "--auto-uv-power-limit-w",
            "390",
            "--auto-uv-tail-rise-bins",
            "6",
            "--auto-oc-target-voltage-mv",
            "925",
            "--auto-oc-target-clock-mhz",
            "2670",
        ]
    )

    assert args.auto_uv_voltage_scan is True
    assert args.auto_uv_mode == "performance"
    assert args.gpu_index == 1
    assert args.auto_uv_min_voltage_mv == 850
    assert args.auto_uv_memory_offset_mhz == 500
    assert args.auto_uv_power_limit_w == 390
    assert args.auto_uv_tail_rise_bins == 6
    assert args.auto_oc_target_voltage_mv == 925
    assert args.auto_oc_target_clock_mhz == 2670


def test_internal_profile_verification_flags_are_accepted_for_ui_command_path():
    args = parse_arguments(
        [
            "--stability-test",
            "--auto-uv-profile",
            "latest",
            "--stability-seconds",
            "600",
            "--stability-stop-request-file",
            "/tmp/verify.stop",
        ]
    )

    assert args.stability_test is True
    assert args.auto_uv_profile == "latest"
    assert args.stability_seconds == 600
    assert args.stability_stop_request_file == "/tmp/verify.stop"
