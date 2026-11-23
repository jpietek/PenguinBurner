from __future__ import annotations

import json
from pathlib import Path
import struct

from integrations.afterburner.fan_curve import (
    decode_sw_auto_fan_flags,
    load_afterburner_fan_settings,
    parse_sw_auto_fan_curve,
    speed_for_temperature,
    temperature_for_speed,
    validate_afterburner_fan_settings,
)
from integrations.afterburner.vfcurve import (
    load_afterburner_profile_settings,
    parse_vfcurve_blob,
    point_map_by_voltage,
)
import integrations.afterburner.profile_import as ui_app
from integrations.afterburner.profile_import import (
    afterburner_profile_entries as _afterburner_profile_entries,
    persist_afterburner_import_selection as _persist_afterburner_import_selection,
)


def _fan_curve_hex(points: list[tuple[float, float]], *, flags: int = 0) -> str:
    payload = struct.pack("<III", 1, len(points), flags)
    for temp_c, speed_pct in points:
        payload += struct.pack("<ff", float(temp_c), float(speed_pct))
    return payload.hex()


def _vf_curve_hex(points: list[tuple[float, float, float]]) -> str:
    values = [1.0, float(len(points)), 0.0]
    for voltage_mv, frequency_mhz, third_value in points:
        values.extend([float(voltage_mv), float(frequency_mhz), float(third_value)])
    values.extend([0.0, 0.0, 0.0])
    return b"".join(struct.pack("<f", value) for value in values).hex()


def _write_afterburner_export(root: Path) -> Path:
    root.mkdir(exist_ok=True)
    (root / "MSIAfterburner.cfg").write_text("[Settings]\n", encoding="utf-8")
    profiles_dir = root / "Profiles"
    profiles_dir.mkdir()
    device_profile = (
        profiles_dir / "VEN_10DE&DEV_2684&SUBSYS_00000000&REV_A1&BUS_1&DEV_0&FN_0.cfg"
    )
    defaults = _vf_curve_hex(
        [
            (800.0, 1800.0, 0.0),
            (850.0, 1900.0, 0.0),
            (900.0, 2000.0, 0.0),
            (950.0, 2100.0, 0.0),
            (1000.0, 2200.0, 0.0),
            (1050.0, 2300.0, 0.0),
        ]
    )
    undervolt = _vf_curve_hex(
        [
            (800.0, 1800.0, 0.0),
            (850.0, 1900.0, 0.0),
            (900.0, 2100.0, 0.0),
            (950.0, 2100.0, 0.0),
            (1000.0, 2100.0, 0.0),
            (1050.0, 2100.0, 0.0),
        ]
    )
    device_profile.write_text(
        "\n".join(
            [
                "[Defaults]",
                f"VFCurve={defaults}",
                "[Startup]",
                f"VFCurve={defaults}",
                "[Profile1]",
                f"VFCurve={undervolt}",
                "[Profile2]",
                f"VFCurve={defaults}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return device_profile


class _FakeVfCurveReader:
    def editable_core_points(self) -> list[dict]:
        return [
            {
                "index": index,
                "voltage_uv": voltage_mv * 1000,
                "base_freq_khz": base_mhz * 1000,
                "current_offset_khz": 0,
            }
            for index, (voltage_mv, base_mhz) in enumerate(
                [
                    (800, 1800),
                    (850, 1900),
                    (900, 2000),
                    (950, 2100),
                    (1000, 2200),
                    (1050, 2300),
                ]
            )
        ]

    def close(self) -> None:
        pass


def _patch_afterburner_import_io(monkeypatch, tmp_path: Path) -> tuple[dict, Path]:
    captured_payload: dict = {}
    profile_path = tmp_path / "profiles" / "auto-uv-profile-imported.json"

    def archive(payload: dict) -> Path:
        captured_payload.update(payload)
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(json.dumps(payload), encoding="utf-8")
        return profile_path

    monkeypatch.setattr(
        ui_app,
        "create_hidden_vf_curve_reader",
        lambda **_kwargs: _FakeVfCurveReader(),
    )
    monkeypatch.setattr(ui_app, "archive_auto_uv_profile", archive)
    monkeypatch.setattr(
        ui_app.DaemonGpuClient,
        "capabilities",
        lambda self: type(
            "Caps",
            (),
            {
                "identity": type(
                    "Identity",
                    (),
                    {
                        "index": 0,
                        "uuid": "GPU-import-test",
                        "name": "RTX Import Test",
                        "pci_bus_id": "0000:01:00.0",
                        "pci_device_id": "0x1234",
                    },
                )()
            },
        )(),
    )
    return captured_payload, profile_path


def test_parse_afterburner_fan_curve_blob_and_interpolate() -> None:
    parsed = parse_sw_auto_fan_curve(
        _fan_curve_hex([(40.0, 0.0), (60.0, 30.0), (80.0, 75.0)], flags=0x5)
    )

    assert parsed["point_count"] == 3
    assert parsed["flags_u32"] == 0x5
    assert parsed["points"][1]["temperature_c"] == 60.0
    assert temperature_for_speed(parsed["points"], 15.0) == 50.0
    assert speed_for_temperature(parsed["points"], 70.0) == 52.5
    assert decode_sw_auto_fan_flags(0x5) == {
        "force_update_each_period": True,
        "override_zero_with_hardware_curve": True,
        "unknown_bits_u32": 0,
    }


def test_validate_afterburner_fan_curve_reports_human_readable_problems() -> None:
    settings = {
        "sw_auto_enabled": 0,
        "curve": {
            "points": [
                {"temperature_c": 40.0, "speed_pct": 50.0},
                {"temperature_c": 60.0, "speed_pct": 40.0},
            ]
        },
        "curve2": {
            "points": [
                {"temperature_c": 40.0, "speed_pct": 30.0},
                {"temperature_c": 60.0, "speed_pct": 60.0},
            ]
        },
    }

    result = validate_afterburner_fan_settings(settings)

    assert result["valid"] is False
    assert "Afterburner software auto fan control is disabled" in result["problems"]
    assert "primary fan curve speeds must be nondecreasing" in result["problems"]


def test_load_afterburner_fan_settings_from_synthetic_profile(tmp_path: Path) -> None:
    profile = tmp_path / "MSIAfterburner.cfg"
    primary = _fan_curve_hex([(45.0, 0.0), (60.0, 30.0), (80.0, 80.0)])
    reference = _fan_curve_hex([(45.0, 0.0), (60.0, 40.0), (80.0, 90.0)])
    profile.write_text(
        "\n".join(
            [
                "SwAutoFanControl=1",
                "SwAutoFanControlFlags=00000005h",
                "SwAutoFanControlPeriod=5000",
                f"SwAutoFanControlCurve={primary}",
                f"SwAutoFanControlCurve2={reference}",
            ]
        )
        + "\n"
    )

    settings = load_afterburner_fan_settings(profile)

    assert settings["sw_auto_enabled"] == 1
    assert settings["period_ms"] == 5000
    assert settings["flags"]["force_update_each_period"] is True
    assert settings["curve"]["points"][2]["speed_pct"] == 80.0


def test_parse_afterburner_vf_curve_blob_without_real_profile() -> None:
    header, points, tail = parse_vfcurve_blob(
        _vf_curve_hex([(800.0, 1800.0, 0.0), (900.0, 2000.0, 0.0)])
    )

    assert "magic_u32" in header
    assert tail == []
    assert point_map_by_voltage(points)[800]["frequency_mhz"] == 1800.0
    assert point_map_by_voltage(points)[900]["frequency_mhz"] == 2000.0


def test_load_afterburner_profile_settings_preserves_mixed_case_keys(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile.cfg"
    vf_hex = _vf_curve_hex([(800.0, 1800.0, 0.0)])
    profile.write_text(
        "\n".join(
            [
                "[Startup]",
                "Format=2",
                "PowerLimit=100",
                "CoreClkBoost=15000",
                "MemClkBoost=-5000",
                f"VFCurve={vf_hex}",
            ]
        )
        + "\n"
    )

    settings = load_afterburner_profile_settings(profile, section="startup")

    assert settings["section"] == "Startup"
    assert settings["format"] == 2
    assert settings["core_clk_boost_khz"] == 15000
    assert settings["mem_clk_boost_khz"] == -5000
    assert settings["vf_curve_hex"] == vf_hex


def test_gui_afterburner_import_entries_list_profiles_with_status(
    tmp_path: Path,
) -> None:
    _write_afterburner_export(tmp_path)

    entries = _afterburner_profile_entries(tmp_path)

    assert [(entry["section"], entry["importable"]) for entry in entries] == [
        ("Profile1", True),
        ("Profile2", False),
    ]
    assert entries[0]["target"] == "2100 MHz 900 mV"
    assert entries[0]["target_voltage_mv"] == 900
    assert entries[0]["target_clock_mhz"] == 2100
    assert (900.0, 2100.0) in entries[0]["curve_points"]
    assert entries[0]["status"] == "Ready"
    assert "same as Defaults or Startup" in entries[1]["status"]


def test_gui_afterburner_import_persists_selected_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "afterburner"
    _write_afterburner_export(source_root)
    config_path = tmp_path / "config" / "penguin_burner.toml"
    managed_root = tmp_path / "managed-afterburner"
    monkeypatch.setattr(ui_app, "default_runtime_config_path", lambda: config_path)
    monkeypatch.setattr(ui_app, "managed_afterburner_root", lambda: managed_root)
    captured_payload, profile_path = _patch_afterburner_import_io(monkeypatch, tmp_path)
    entry = next(
        entry
        for entry in _afterburner_profile_entries(source_root)
        if entry["importable"]
    )

    result = _persist_afterburner_import_selection(entry)

    assert result["afterburner_root"] == str(managed_root)
    assert captured_payload["gpu_identity"] == {
        "name": "RTX Import Test",
        "uuid": "GPU-import-test",
        "pci_bus_id": "0000:01:00.0",
        "pci_device_id": "0x1234",
        "index_at_verification": 0,
    }
    assert result["section"] == "Profile1"
    assert result["profile_path"] == str(profile_path)
    assert (managed_root / result["device_profile_relative_path"]).is_file()
    assert captured_payload["profile_source"] == "MSI Afterburner"
    assert captured_payload["final_verified"] is True
    assert captured_payload["display_name"] == "MSI Afterburner Profile1 2100 MHz 900 mV"
    assert captured_payload["candidate_voltage_mv"] == 900
    assert captured_payload["lock_clock_mhz"] == 2100
    assert captured_payload["plan"][2]["new_offset_mhz"] == 100
    assert (900.0, 2100.0) in captured_payload["curve_points"]
    rendered = config_path.read_text(encoding="utf-8")
    assert "afterburner_profile" not in rendered
    assert "afterburner_device_profile" not in rendered
    assert f'afterburner_root = "{managed_root}"' in rendered


def test_gui_afterburner_import_profile_file_is_runtime_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "afterburner"
    _write_afterburner_export(source_root)
    config_path = tmp_path / "config" / "penguin_burner.toml"
    managed_root = tmp_path / "managed-afterburner"
    monkeypatch.setattr(ui_app, "default_runtime_config_path", lambda: config_path)
    monkeypatch.setattr(ui_app, "managed_afterburner_root", lambda: managed_root)
    _captured_payload, profile_path = _patch_afterburner_import_io(monkeypatch, tmp_path)
    entry = next(
        entry
        for entry in _afterburner_profile_entries(source_root)
        if entry["importable"]
    )
    result = _persist_afterburner_import_selection(entry)

    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    assert result["profile_path"] == str(profile_path)
    assert payload["profile_source"] == "MSI Afterburner"
    assert "runtime_source" not in payload
    assert payload["points"] == payload["plan"]
    assert payload["afterburner_import"]["section"] == "Profile1"


def test_gui_afterburner_import_clears_stale_runtime_selection_keys(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "afterburner"
    _write_afterburner_export(source_root)
    config_path = tmp_path / "config" / "penguin_burner.toml"
    managed_root = tmp_path / "managed-afterburner"
    monkeypatch.setattr(ui_app, "default_runtime_config_path", lambda: config_path)
    monkeypatch.setattr(ui_app, "managed_afterburner_root", lambda: managed_root)
    _patch_afterburner_import_io(monkeypatch, tmp_path)
    entry = next(
        entry
        for entry in _afterburner_profile_entries(source_root)
        if entry["importable"]
    )
    _persist_afterburner_import_selection(entry)

    rendered = config_path.read_text(encoding="utf-8")
    assert "afterburner_profile" not in rendered
    assert "afterburner_device_profile" not in rendered
    assert f'afterburner_root = "{managed_root}"' in rendered


def test_persist_drops_q2rtx_override_keys(tmp_path: Path) -> None:
    # PenguinBurner must always use its own managed headless Q2RTX fork: a
    # user-supplied [stability] q2rtx_dir/q2rtx_binary override must never be
    # carried forward into the generated runtime config.
    import tomllib

    from integrations.afterburner.import_vf_curve import _persist_afterburner_runtime_state

    config_path = tmp_path / "penguin_burner.toml"
    config_path.write_text(
        "[gpu]\nindex = 0\n\n"
        "[stability]\n"
        'q2rtx_dir = "/opt/evil/q2rtx"\n'
        'q2rtx_binary = "/opt/evil/q2rtx/q2rtx"\n',
        encoding="utf-8",
    )

    _persist_afterburner_runtime_state(config_path, 0)

    written = tomllib.loads(config_path.read_text(encoding="utf-8"))
    stability = written.get("stability", {})
    assert "q2rtx_dir" not in stability
    assert "q2rtx_binary" not in stability


def test_write_config_replaces_atomically_and_leaves_no_temp_file(
    tmp_path: Path,
) -> None:
    """The config is shared by several live processes (GUI, CLI, overlay,
    Flatpak); an in-place truncating write lets a concurrent reader observe a
    torn file, and read-modify-write callers would then rewrite it without the
    sections they never saw. write_config must land via atomic rename."""
    import tomllib

    from integrations.afterburner.import_fan_curve import write_config

    config_path = tmp_path / "penguin_burner.toml"
    write_config(
        config_path,
        {"gpu": {"index": 1}, "ui": {"persist_on_startup": True}},
    )

    written = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert written["ui"]["persist_on_startup"] is True
    assert written["gpu"]["index"] == 1
    assert list(tmp_path.iterdir()) == [config_path]  # no .tmp left behind
