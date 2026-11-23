from __future__ import annotations

import json

import pytest

import integrations.lact.export as lact_export
import profiles.uv.profile_store as profile_store
from integrations.lact.export import (
    build_lact_nvidia_config,
    build_lact_nvidia_config_from_plan,
    write_lact_nvidia_config,
)
from profiles.uv.profile_store import archive_auto_uv_profile


def _use_auto_uv_config_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(lact_export, "default_user_config_dir", lambda: tmp_path)
    monkeypatch.setattr(profile_store, "default_user_config_dir", lambda: tmp_path)


def _write_final_profile(payload: dict):
    stored = dict(payload)
    stored.setdefault("candidate_voltage_mv", 900)
    stored.setdefault("lock_clock_mhz", 2100)
    stored["final_verified"] = True
    return archive_auto_uv_profile(stored)


def test_lact_nvidia_export_writes_full_config(tmp_path, monkeypatch) -> None:
    _use_auto_uv_config_dir(tmp_path, monkeypatch)
    _write_final_profile(
        {
            "lock_clock_mhz": 2445,
            "candidate_voltage_mv": 900,
            "points": [
                {
                    "index": 42,
                    "voltage_mv": 900,
                    "base_mhz": 2350,
                    "target_mhz": 2445,
                    "new_offset_mhz": 95,
                },
                {
                    "index": 43,
                    "voltage_mv": 925,
                    "base_mhz": 2380,
                    "target_mhz": 2445,
                    "new_offset_mhz": 65,
                },
            ],
        }
    )
    (tmp_path / "auto-uv-fan-curve.json").write_text(
        json.dumps(
            {
                "zero_rpm_until_temperature_c": 45.0,
                "fan": {
                    "poll_interval_s": 1.0,
                    "hysteresis_c": 2.0,
                    "curve": [[45.0, 0.0], [55.0, 30.0], [75.0, 70.0]],
                },
            }
        ),
        encoding="utf-8",
    )

    rendered, warnings = build_lact_nvidia_config(
        gpu_id="10DE:2704-1462:5110-0000:09:00.0",
        include_fan_curve=True,
    )

    assert warnings == []
    assert "daemon:\n  log_level: info\n  disable_nvapi: false" in rendered
    assert "10DE:2704-1462:5110-0000:09:00.0:" in rendered
    assert "fan_control_enabled: true" in rendered
    assert "45: 0" in rendered
    assert "55: 0.3" in rendered
    assert "auto_threshold: 45" in rendered
    assert "change_threshold: 2" in rendered
    assert "gpu_vf_curve:" in rendered
    assert "42:\n        clockspeed: 2445\n        voltage: 900" in rendered
    assert "43:\n        clockspeed: 2445\n        voltage: 925" in rendered
    assert "profiles: {}" in rendered


def test_lact_nvidia_export_defaults_to_vf_only(tmp_path, monkeypatch) -> None:
    _use_auto_uv_config_dir(tmp_path, monkeypatch)
    _write_final_profile(
        {
            "points": [
                {
                    "index": 1,
                    "voltage_mv": 900,
                    "base_mhz": 2000,
                    "target_mhz": 2100,
                    "new_offset_mhz": 100,
                }
            ]
        }
    )
    (tmp_path / "auto-uv-fan-curve.json").write_text(
        json.dumps(
            {
                "zero_rpm_until_temperature_c": 42.0,
                "fan": {
                    "poll_interval_s": 2.0,
                    "hysteresis_c": 1.0,
                    "curve": [[42.0, 0.0], [65.0, 55.0]],
                },
            }
        ),
        encoding="utf-8",
    )

    rendered, warnings = build_lact_nvidia_config(gpu_id="gpu0")

    assert warnings == []
    assert "fan_control_enabled: false" in rendered
    assert "gpu_vf_curve:" in rendered
    assert "65: 0.55" not in rendered


def test_lact_nvidia_writer_can_export_selected_auto_uv_profile(
    tmp_path, monkeypatch
) -> None:
    _use_auto_uv_config_dir(tmp_path, monkeypatch)
    selected_path = _write_final_profile(
        {
            "profile_id": "selected",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2100,
            "points": [
                {
                    "index": 1,
                    "voltage_mv": 900,
                    "base_mhz": 2000,
                    "target_mhz": 2100,
                    "new_offset_mhz": 100,
                }
            ],
        }
    )
    _write_final_profile(
        {
            "profile_id": "newer",
            "candidate_voltage_mv": 925,
            "lock_clock_mhz": 2200,
            "points": [
                {
                    "index": 1,
                    "voltage_mv": 925,
                    "base_mhz": 2050,
                    "target_mhz": 2200,
                    "new_offset_mhz": 150,
                }
            ],
        }
    )

    output_path = tmp_path / "lact" / "config.yaml"
    written_path, warnings = write_lact_nvidia_config(
        output_path=output_path,
        gpu_id="gpu0",
        final_curve_path=selected_path,
    )

    assert written_path == output_path
    assert warnings == []
    rendered = output_path.read_text(encoding="utf-8")
    assert "clockspeed: 2100" in rendered
    assert "clockspeed: 2200" not in rendered


def test_lact_nvidia_export_resolves_selected_auto_uv_profile(
    tmp_path, monkeypatch
) -> None:
    _use_auto_uv_config_dir(tmp_path, monkeypatch)
    _write_final_profile(
        {
            "profile_id": "selected",
            "candidate_voltage_mv": 900,
            "lock_clock_mhz": 2100,
            "points": [
                {
                    "index": 1,
                    "voltage_mv": 900,
                    "base_mhz": 2000,
                    "target_mhz": 2100,
                    "new_offset_mhz": 100,
                }
            ],
        }
    )
    _write_final_profile(
        {
            "profile_id": "newer",
            "candidate_voltage_mv": 925,
            "lock_clock_mhz": 2200,
            "points": [
                {
                    "index": 1,
                    "voltage_mv": 925,
                    "base_mhz": 2050,
                    "target_mhz": 2200,
                    "new_offset_mhz": 150,
                }
            ],
        }
    )

    rendered, warnings = build_lact_nvidia_config(
        gpu_id="gpu0",
        profile_selector="selected",
    )

    assert warnings == []
    assert "clockspeed: 2100" in rendered
    assert "clockspeed: 2200" not in rendered


def test_lact_nvidia_export_clamps_offsets_to_lact_limit() -> None:
    rendered, warnings = build_lact_nvidia_config_from_plan(
        gpu_id="gpu0",
        vf_plan=[
            {
                "index": 4,
                "voltage_mv": 900,
                "base_mhz": 2000,
                "target_mhz": 3300,
                "new_offset_mhz": 1300,
            }
        ],
        source_label="auto-uv",
        source_vf="/tmp/profile.json",
    )

    assert "4:\n        clockspeed: 3000\n        voltage: 900" in rendered
    assert "clockspeed: 3300" not in rendered
    assert warnings == [
        "LACT V/F offsets were clamped to +1000MHz over each point's base clock: "
        "index=4 voltage=900mV offset=+1300->+1000MHz"
    ]


def test_lact_nvidia_export_disables_fan_when_no_fan_artifact(
    tmp_path, monkeypatch
) -> None:
    _use_auto_uv_config_dir(tmp_path, monkeypatch)
    _write_final_profile(
        {
            "points": [
                {
                    "index": 1,
                    "voltage_mv": 900,
                    "base_mhz": 2000,
                    "target_mhz": 2100,
                    "new_offset_mhz": 100,
                }
            ]
        }
    )

    rendered, warnings = build_lact_nvidia_config(gpu_id="gpu0")

    assert warnings == []
    assert "fan_control_enabled: false" in rendered
    assert "gpu_vf_curve:" in rendered


def test_lact_nvidia_export_can_emit_fan_only_auto_uv_config(
    tmp_path, monkeypatch
) -> None:
    _use_auto_uv_config_dir(tmp_path, monkeypatch)
    (tmp_path / "auto-uv-fan-curve.json").write_text(
        json.dumps(
            {
                "zero_rpm_until_temperature_c": 42.0,
                "fan": {
                    "poll_interval_s": 2.0,
                    "hysteresis_c": 1.0,
                    "curve": [[42.0, 0.0], [65.0, 55.0], [82.0, 90.0]],
                },
            }
        ),
        encoding="utf-8",
    )

    rendered, warnings = build_lact_nvidia_config(
        gpu_id="gpu0",
        include_vf_curve=False,
        include_fan_curve=True,
    )

    assert warnings == []
    assert "# Source V/F curve: omitted" in rendered
    assert "fan_control_enabled: true" in rendered
    assert "interval_ms: 2000" in rendered
    assert "65: 0.55" in rendered
    assert "gpu_vf_curve:" not in rendered


def test_lact_nvidia_export_supports_afterburner_plan_source() -> None:
    rendered, warnings = build_lact_nvidia_config_from_plan(
        gpu_id="gpu0",
        vf_plan=[
            {
                "index": 7,
                "voltage_mv": 950,
                "base_mhz": 2400,
                "target_mhz": 2550,
                "new_offset_mhz": 150,
            }
        ],
        fan_config={
            "poll_interval_s": 5.0,
            "hysteresis_c": 0.0,
            "auto_restore_temp_c": 45.0,
            "curve": [[45.0, 0.0], [60.0, 50.0], [80.0, 100.0]],
        },
        source_label="afterburner",
        source_vf="/mnt/windows/MSI Afterburner/Profiles/GPU.cfg [Profile1]",
        source_fan="/mnt/windows/MSI Afterburner/MSIAfterburner.cfg",
        include_fan_curve=True,
    )

    assert warnings == []
    assert "# Source: afterburner" in rendered
    assert "interval_ms: 5000" in rendered
    assert "60: 0.5" in rendered
    assert "7:\n        clockspeed: 2550\n        voltage: 950" in rendered


def test_lact_nvidia_export_can_emit_fan_only_plan_config() -> None:
    rendered, warnings = build_lact_nvidia_config_from_plan(
        gpu_id="gpu0",
        vf_plan=None,
        fan_config={
            "poll_interval_s": 5.0,
            "hysteresis_c": 0.0,
            "auto_restore_temp_c": 45.0,
            "curve": [[45.0, 0.0], [60.0, 50.0], [80.0, 100.0]],
        },
        source_label="afterburner",
        source_vf="omitted",
        source_fan="/mnt/windows/MSI Afterburner/MSIAfterburner.cfg",
        include_vf_curve=False,
        include_fan_curve=True,
    )

    assert warnings == []
    assert "# Source: afterburner" in rendered
    assert "# Source V/F curve: omitted" in rendered
    assert "fan_control_enabled: true" in rendered
    assert "gpu_vf_curve:" not in rendered


# --- pure helper unit tests ------------------------------------------------


def test_yaml_scalar_quotes_only_unsafe_values():
    assert lact_export._yaml_scalar("gpu0") == "gpu0"
    assert lact_export._yaml_scalar("10DE:2704-1462:5110-0000:09:00.0") == (
        "10DE:2704-1462:5110-0000:09:00.0"
    )
    assert lact_export._yaml_scalar("") == '""'
    assert lact_export._yaml_scalar("a b") == '"a b"'


def test_clamp_bounds_value():
    assert lact_export._clamp(5, 0, 10) == 5.0
    assert lact_export._clamp(-3, 0, 10) == 0.0
    assert lact_export._clamp(42, 0, 10) == 10.0


def test_optional_int_coerces_or_returns_none():
    assert lact_export._optional_int(None) is None
    assert lact_export._optional_int("") is None
    assert lact_export._optional_int("12.6") == 13
    assert lact_export._optional_int(0) == 0
    assert lact_export._optional_int("not-a-number") is None


def test_auto_uv_fan_config_handles_none_and_blocked():
    assert lact_export._auto_uv_fan_config(None) is None
    assert lact_export._auto_uv_fan_config({"fan_curve_blocked": True}) is None


def test_auto_uv_fan_config_requires_fan_section():
    with pytest.raises(lact_export.LactExportError, match="missing the fan section"):
        lact_export._auto_uv_fan_config({"fan": "not-a-dict"})


def test_auto_uv_fan_config_applies_zero_rpm_override():
    fan = lact_export._auto_uv_fan_config(
        {"zero_rpm_until_temperature_c": 44.0, "fan": {"curve": [[44.0, 0.0]]}}
    )
    assert fan["auto_restore_temp_c"] == 44.0


def test_fan_config_yaml_none_disables_control():
    lines, warnings = lact_export._fan_config_yaml(None)
    assert lines == ["    fan_control_enabled: false"]
    assert warnings == []


def test_fan_config_yaml_requires_curve_points():
    with pytest.raises(lact_export.LactExportError, match="no curve points"):
        lact_export._fan_config_yaml({"curve": []})


def test_fan_config_yaml_rejects_malformed_point():
    with pytest.raises(lact_export.LactExportError, match="invalid fan curve point"):
        lact_export._fan_config_yaml({"curve": [[45.0, 0.0], [60.0]]})


def test_fan_config_yaml_warns_when_auto_threshold_disabled():
    lines, warnings = lact_export._fan_config_yaml(
        {"curve": [[45.0, 0.0], [60.0, 50.0]], "poll_interval_s": 1.0}
    )
    assert "      auto_threshold: 0" in lines
    assert warnings == [
        "fan auto_threshold is disabled because no zero-RPM temp was saved"
    ]


def test_vf_curve_yaml_requires_points():
    with pytest.raises(lact_export.LactExportError, match="no points"):
        lact_export._vf_curve_yaml_from_points([])


def test_vf_curve_yaml_rejects_non_dict_point():
    with pytest.raises(lact_export.LactExportError, match="invalid V/F point"):
        lact_export._vf_curve_yaml_from_points(["nope"])


def test_vf_curve_yaml_rejects_missing_keys():
    with pytest.raises(lact_export.LactExportError, match="invalid V/F point"):
        lact_export._vf_curve_yaml_from_points([{"index": 1, "voltage_mv": 900}])


def test_vf_curve_yaml_rejects_index_out_of_range():
    with pytest.raises(lact_export.LactExportError, match="outside LACT range"):
        lact_export._vf_curve_yaml_from_points(
            [{"index": 256, "voltage_mv": 900, "target_mhz": 2000}]
        )


def test_vf_curve_yaml_clamps_negative_clockspeed():
    lines, warnings = lact_export._vf_curve_yaml_from_points(
        [{"index": 2, "voltage_mv": 800, "target_mhz": -50}]
    )
    assert "        clockspeed: 0" in lines
    assert warnings == [
        "LACT V/F clocks were clamped to non-negative values: "
        "index=2 voltage=800mV clockspeed=-50->0MHz"
    ]


def test_vf_curve_yaml_unlimited_offset_when_max_is_none():
    lines, warnings = lact_export._vf_curve_yaml_from_points(
        [{"index": 3, "voltage_mv": 900, "base_mhz": 2000, "target_mhz": 5000}],
        max_vf_offset_mhz=None,
    )
    assert "        clockspeed: 5000" in lines
    assert warnings == []


def test_build_from_plan_requires_gpu_id():
    with pytest.raises(lact_export.LactExportError, match="GPU id is required"):
        lact_export.build_lact_nvidia_config_from_plan(
            gpu_id="   ",
            vf_plan=[{"index": 1, "voltage_mv": 900, "target_mhz": 2000}],
            source_label="auto-uv",
            source_vf="/tmp/p.json",
        )


# --- GPU id detection and output path ----------------------------------------


def test_lact_gpu_id_from_config_reads_first_gpu_key(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "# a comment",
                "daemon:",
                "  log_level: info",
                "gpus:",
                "  1002:73df-1043:8888:",
                "    fan_control_enabled: true",
            ]
        ),
        encoding="utf-8",
    )
    assert lact_export.lact_gpu_id_from_config(config) == "1002:73df-1043:8888"


def test_lact_gpu_id_from_config_returns_empty_when_no_gpus_block(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("daemon:\n  log_level: info\n", encoding="utf-8")
    assert lact_export.lact_gpu_id_from_config(config) == ""


def test_lact_gpu_id_from_config_stops_when_block_ends(tmp_path) -> None:
    # A non-indented line after gpus: ends the block before any id is found.
    config = tmp_path / "config.yaml"
    config.write_text("gpus:\nother: 1\n", encoding="utf-8")
    assert lact_export.lact_gpu_id_from_config(config) == ""


def test_lact_gpu_id_from_config_missing_file_returns_empty(tmp_path) -> None:
    assert lact_export.lact_gpu_id_from_config(tmp_path / "nope.yaml") == ""


def test_detect_lact_gpu_id_prefers_output_dir(tmp_path) -> None:
    (tmp_path / "config.yaml").write_text(
        "gpus:\n  abcd:0001:\n    x: 1\n", encoding="utf-8"
    )
    assert lact_export.detect_lact_gpu_id(tmp_path) == "abcd:0001"


def test_detect_lact_gpu_id_empty_when_nothing_found(tmp_path, monkeypatch) -> None:
    # Force every candidate path to yield no id, independent of the host's /etc/lact.
    monkeypatch.setattr(lact_export, "lact_gpu_id_from_config", lambda _path: "")
    assert lact_export.detect_lact_gpu_id(tmp_path) == ""


def test_lact_export_output_path_appends_filename(tmp_path) -> None:
    assert lact_export.lact_export_output_path(tmp_path) == tmp_path / "config.yaml"
