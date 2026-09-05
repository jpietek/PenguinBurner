from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import ui.features.integrations.afterburner_import as afterburner_import
import ui.features.curves.fan_profiles as fan_profiles
from ui.main import parse_gui_args
from ui.main import parse_gui_launch_options
from ui.features.curves.fan_profiles import fan_payload_has_silent_runtime_fields
from ui.features.tuning.gpu_selection import GpuChoice
from ui.features.tuning.gpu_selection import gpu_choices_from_nvml_identities
from ui.features.tuning.gpu_selection import gpu_choices_with_fallback
from ui.features.tuning.gpu_selection import persist_runtime_gpu_index
from ui.features.curves.fan_profiles import profile_fan_curve_points
from ui.features.curves.fan_profiles import profile_fan_measurement_points
from ui.features.curves.fan_profiles import profile_fan_curve_target_point
from ui.features.integrations.lact_export import lact_export_output_path
from ui.features.integrations.lact_export import lact_gpu_id_from_config
from ui.controllers.scan import ScanController
from ui.features.curves.curve_profiles import curve_plan_from_values
from ui.features.curves.curve_profiles import curve_points_from_values
from ui.models import candidate_id_from_payload
from ui.models import event_points
from ui.models import fan_points
from ui.models import stage_title
from ui.features.profiles.profiles import profile_can_apply
from ui.features.profiles.profiles import profile_can_verify
from ui.features.profiles.profiles import profile_for_selector
from ui.features.profiles.profiles import profile_verify_selector
from ui.features.profiles.profiles import runner_status_text
from ui.features.tuning.verify import elapsed_from_line
from ui.features.tuning.verify import progress_percent
from ui.features.tuning.verify import workload_label
from ui.features.tuning.tuning import auto_uv_performance_preset_label
from ui.features.tuning.tuning import auto_uv_preset


def test_ui_launcher_ignores_old_new_ui_flag() -> None:
    argv = parse_gui_args(["pburn-ui", "--new-ui"])

    assert argv == ["pburn-ui"]


def test_ui_launcher_accepts_gpu_index_without_passing_it_to_qt() -> None:
    options = parse_gui_launch_options(
        ["pburn-ui", "--gpu-index", "2", "--style", "Fusion"]
    )

    assert options.qt_argv == ["pburn-ui", "--style", "Fusion"]
    assert options.gpu_index == 2


def test_ui_launcher_accepts_index_alias() -> None:
    options = parse_gui_launch_options(["pburn-ui", "--index=1"])

    assert options.qt_argv == ["pburn-ui"]
    assert options.gpu_index == 1


def test_ui_launcher_passes_through_non_gui_options() -> None:
    options = parse_gui_launch_options(
        ["pburn-ui", "--auto-uv-tail-rise-bins=5", "--style", "Fusion"]
    )

    assert options.qt_argv == [
        "pburn-ui",
        "--auto-uv-tail-rise-bins=5",
        "--style",
        "Fusion",
    ]


def test_new_ui_package_is_installed() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    packages = set(metadata["tool"]["setuptools"]["packages"])
    package_data = metadata["tool"]["setuptools"]["package-data"]

    assert {"ui", "ui.components", "ui.controllers", "ui.dialogs"} <= packages
    assert "*.png" in package_data["ui.assets"]


def test_gpu_selection_builds_nvml_identity_choices() -> None:
    choices = gpu_choices_from_nvml_identities(
        [
            SimpleNamespace(
                index=0,
                name="NVIDIA GeForce RTX 4090",
                pci_bus_id="00000000:01:00.0",
                uuid="GPU-4090",
            ),
            SimpleNamespace(
                index=1,
                name="NVIDIA GeForce RTX 5090",
                pci_bus_id="00000000:03:00.0",
                uuid="GPU-5090",
            ),
        ]
    )

    assert [choice.index for choice in choices] == [0, 1]
    assert choices[0].label == "GPU 0 - NVIDIA GeForce RTX 4090 (01:00.0)"
    assert choices[1].label == "GPU 1 - NVIDIA GeForce RTX 5090 (03:00.0)"


def test_gpu_selection_falls_back_to_the_detected_card() -> None:
    choices, selected = gpu_choices_with_fallback(
        selected_index=2,
        choices=[GpuChoice(index=0, name="NVIDIA GeForce RTX 4090")],
    )

    assert selected == 0
    assert [choice.index for choice in choices] == [0]
    assert choices[0].label == "GPU 0 - NVIDIA GeForce RTX 4090"


def test_gpu_selection_persists_runtime_index(tmp_path) -> None:
    config_path = tmp_path / "runtime.toml"
    config_path.write_text(
        "[gpu]\nenable_persistence_mode = true\n\n[fan]\nmode = \"linear\"\n",
        encoding="utf-8",
    )

    selected = persist_runtime_gpu_index(3, config_path=config_path)

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert selected == 3
    assert config["gpu"]["index"] == 3
    assert config["gpu"]["enable_persistence_mode"] is True
    assert config["fan"]["mode"] == "linear"


def test_new_ui_vf_plot_does_not_hardcode_gpu_clock_or_voltage_range() -> None:
    source = Path("ui/window.py").read_text(encoding="utf-8")
    vf_plot_block = source.split("self.vf_plot = CurvePlot(", 1)[1].split(
        "self.fan_plot = CurvePlot(",
        1,
    )[0]

    assert "x_range=" not in vf_plot_block
    assert "y_range=" not in vf_plot_block
    assert "x_range=" not in Path("ui/components/vf_curve_editor.py").read_text(
        encoding="utf-8"
    )
    assert "y_range=" not in Path("ui/components/vf_curve_editor.py").read_text(
        encoding="utf-8"
    )


def test_new_ui_models_keep_gpu_ranges_data_driven() -> None:
    assert event_points(
        {
            "points": [
                {"voltage_mv": "1200", "clock_mhz": "3300"},
            ]
        }
    ) == [(1200.0, 3300.0)]
    assert curve_points_from_values([{"voltage_mv": "1200", "target_mhz": "3300"}]) == [
        (1200.0, 3300.0)
    ]
    assert curve_plan_from_values(
        [{"index": 1, "voltage_mv": "1200", "base_mhz": "3200", "target_mhz": "3300"}]
    )[0]["target_mhz"] == 3300
    assert fan_points({"curve_points": [{"temp_c": "80", "fan_pct": "75"}]}) == [
        (80.0, 75.0)
    ]
    assert candidate_id_from_payload({"voltage_mv": 1200, "clock_mhz": 3300}) == (
        "1200mv-3300mhz"
    )
    assert stage_title("base-baseline") == "Baseline"


def test_new_ui_profile_and_tuning_helpers_cover_moved_workflows() -> None:
    profiles = [{"profile_id": "profile-a", "final_verified": True}]

    assert profile_for_selector(profiles, "profile-a") == profiles[0]
    assert profile_can_apply(profiles[0])
    assert not profile_can_verify(profiles[0])
    assert "Currently running profile" in runner_status_text(
        profiles,
        running_selector="profile-a",
    )
    assert auto_uv_preset("balanced").tail_rise_bins == 2
    assert auto_uv_preset("performance").tail_rise_bins == 2
    assert auto_uv_performance_preset_label() == "Performance"
    assert profile_verify_selector({"path": "/tmp/profile.json"}) == "/tmp/profile.json"
    assert workload_label() == "Q2RTX benchmark and CUDA compute test"
    assert elapsed_from_line("Stability progress elapsed=150.0s") == 150.0
    assert progress_percent(150, 600) == 25


def test_new_ui_afterburner_import_points_helper_covers_moved_workflow() -> None:
    profile = {
        "profile_id": "afterburner-imported",
        "candidate_id": "afterburner-imported",
        "profile_source": "MSI Afterburner",
        "final_verified": True,
        "path": "/tmp/afterburner-imported.json",
        "curve_points": [(1200.0, 3300.0)],
    }
    profiles = [profile]

    assert profile_for_selector(profiles, "afterburner-imported") == profile
    assert profile_can_apply(profile)
    assert profile_can_verify(profile)
    assert profile_verify_selector(profile) == "/tmp/afterburner-imported.json"
    assert afterburner_import.entry_curve_points(profile) == [(1200.0, 3300.0)]


def test_new_ui_fan_and_lact_helpers_cover_moved_workflows(tmp_path, monkeypatch) -> None:
    profile = {
        "fan_curve_payload": {
            "loaded_temperature_c": 75,
            "observed_fan_speed_pct": 42,
            "fan": {"curve": [[45, 0], [75, 42]]},
            "telemetry": {"measured_fan_points": [{"temperature_c": 66, "fan_speed_pct": 35}]},
        }
    }
    lact_config = tmp_path / "config.yaml"
    lact_config.write_text("gpus:\n  gpu0:\n    fan_control_enabled: false\n")

    assert profile_fan_curve_points(profile) == [(45.0, 0.0), (75.0, 42.0)]
    assert profile_fan_measurement_points(profile) == [(66.0, 35.0)]
    assert profile_fan_curve_target_point(profile) == (75.0, 42.0)
    assert fan_payload_has_silent_runtime_fields(profile["fan_curve_payload"])
    assert lact_export_output_path(tmp_path) == lact_config
    assert lact_gpu_id_from_config(lact_config) == "gpu0"
    monkeypatch.setattr(fan_profiles, "default_user_config_dir", lambda: tmp_path)
    assert fan_profiles.write_auto_uv_fan_curve_payload(profile["fan_curve_payload"]).exists()


def test_scan_controller_routes_json_events_and_human_lines() -> None:
    controller = ScanController(QtCore=object())
    events: list[dict] = []
    human_lines: list[str] = []
    controller.on_event = events.append
    controller.on_human_line = human_lines.append

    controller._handle_line('{"event": "probe_start", "voltage_mv": 1200}')
    controller._handle_line("candidate=1200mV target=3300MHz")

    assert events == [{"event": "probe_start", "voltage_mv": 1200}]
    assert human_lines == ["candidate=1200mV target=3300MHz"]
