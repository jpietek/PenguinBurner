"""Coverage for ui/gpu_selection.py and ui/tuning.py.

gpu_selection parsing/fallback is pure; daemon-backed tuning helpers are
exercised with a fake client so no real GPU is required.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import ui.features.tuning.gpu_selection as gpu_selection
import ui.features.tuning.tuning as tuning
from ui.features.tuning.gpu_selection import (
    GpuChoice,
    gpu_choices_with_fallback,
    gpu_choices_from_nvml_identities,
    persist_runtime_gpu_index,
    runtime_gpu_index,
)
from ui.features.tuning.tuning import (
    AutoUvNvmlInfo,
    auto_uv_nvml_info_text,
    auto_uv_performance_preset_label,
    auto_uv_performance_preset_tooltip,
    auto_uv_performance_target_default,
    auto_uv_power_limit_default,
    auto_uv_scan_estimate_minutes,
    auto_uv_scan_estimate_text,
    auto_uv_voltage_floor_range_mv,
    memory_offset_mhz_range,
)


class _FakeVfReader:
    def __init__(self, points):  # points: list of (voltage_mv, clock_mhz)
        self._points = points

    def editable_core_points(self):
        return [
            {"voltage_uv": v * 1000, "base_freq_khz": c * 1000} for v, c in self._points
        ]

    def close(self):
        pass


# --- ui/gpu_selection.py ------------------------------------------------------


def test_gpu_choice_label_with_and_without_bus() -> None:
    assert GpuChoice(0, "RTX 4090", "00000000:01:00.0").label == (
        "GPU 0 - RTX 4090 (01:00.0)"
    )
    assert GpuChoice(1, "").label == "GPU 1 - NVIDIA GPU"


def test_auto_uv_scan_estimates_cover_single_profiles_and_full_scan() -> None:
    assert auto_uv_scan_estimate_minutes("efficiency") == (10, 20)
    assert auto_uv_scan_estimate_minutes("balanced") == (10, 20)
    assert auto_uv_scan_estimate_minutes("performance") == (15, 25)
    assert auto_uv_scan_estimate_minutes("adaptive") == (25, 35)
    assert auto_uv_scan_estimate_text("efficiency") == "about 10-20 minutes"
    assert auto_uv_scan_estimate_text("adaptive") == "about 25-35 minutes"


def test_gpu_choices_from_nvml_identities_skips_bad_rows_and_dupes() -> None:
    choices = gpu_choices_from_nvml_identities(
        [
            SimpleNamespace(
                index=0,
                name="RTX 4090",
                pci_bus_id="00000000:01:00.0",
                uuid="GPU-abc",
            ),
            SimpleNamespace(index="x", name="bad-index"),
            SimpleNamespace(index=0, name="duplicate"),
            SimpleNamespace(index=1, name="RTX 4080"),
        ]
    )
    assert [(c.index, c.name) for c in choices] == [(0, "RTX 4090"), (1, "RTX 4080")]
    assert choices[1].pci_bus_id == ""


def test_detected_gpu_choices_empty_without_nvml_identities(monkeypatch) -> None:
    monkeypatch.setattr(
        gpu_selection.DaemonGpuClient, "discover_identities", lambda: []
    )
    assert gpu_selection.detected_gpu_choices() == []


def test_detected_gpu_choices_reads_nvml_identities(monkeypatch) -> None:
    monkeypatch.setattr(
        gpu_selection.DaemonGpuClient,
        "discover_identities",
        lambda: [
            SimpleNamespace(
                index=2,
                name="RTX 5090",
                pci_bus_id="00000000:03:00.0",
                uuid="GPU-test",
            )
        ],
    )
    assert gpu_selection.detected_gpu_choices() == [
        GpuChoice(2, "RTX 5090", "00000000:03:00.0", "GPU-test")
    ]


def test_gpu_choices_with_fallback_does_not_invent_an_undetected_gpu() -> None:
    choices, selected = gpu_choices_with_fallback(selected_index=2, choices=[])
    assert selected == 2
    assert choices == []


def test_gpu_choices_with_fallback_selects_a_real_gpu_when_saved_index_is_missing() -> None:
    detected = [GpuChoice(2, "A"), GpuChoice(3, "B")]
    choices, selected = gpu_choices_with_fallback(selected_index=5, choices=detected)
    assert selected == 2
    assert choices == detected


def test_gpu_choices_preserve_a_detected_selection() -> None:
    detected = [GpuChoice(0, "A"), GpuChoice(1, "B")]
    choices, selected = gpu_choices_with_fallback(selected_index=1, choices=detected)
    assert selected == 1
    assert choices == detected


def test_stale_saved_gpu_does_not_create_a_phantom_or_rewrite_config(tmp_path) -> None:
    config = tmp_path / "config.toml"
    original = '[gpu]\nindex = 1\n[ui]\npersist_on_startup = true\n'
    config.write_text(original)
    gpu = GpuChoice(0, "RTX 5080", uuid="GPU-real")

    choices, selected = gpu_choices_with_fallback(config_path=config, choices=[gpu])

    assert choices == [gpu]
    assert selected == 0
    assert config.read_text() == original


def test_scan_dialog_cannot_start_without_a_detected_gpu(qapp, monkeypatch) -> None:
    from PySide6 import QtCore, QtGui, QtWidgets
    import ui.dialogs.scan_tuning as scan_tuning

    monkeypatch.setattr(gpu_selection, "detected_gpu_choices", lambda: [])
    monkeypatch.setattr(
        scan_tuning, "DaemonGpuClient", lambda *_: pytest.fail("Undetected GPU queried")
    )

    def inspect_dialog(dialog):
        combo = dialog.findChild(QtWidgets.QComboBox, "gpuSelector")
        assert combo.count() == 0
        assert not combo.isEnabled()
        info = dialog.findChild(QtWidgets.QLabel, "gpuNvmlInfo")
        assert "No NVIDIA GPU detected" in info.text()
        buttons = dialog.findChild(QtWidgets.QDialogButtonBox)
        start = next(button for button in buttons.buttons() if button.text().startswith("Start"))
        assert not start.isEnabled()
        # A programmatic acceptance must not produce an invalid scan either.
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(QtWidgets.QDialog, "exec", inspect_dialog)
    assert scan_tuning.select_scan_tuning(
        QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, parent=None, gpu_index=1
    ) is None


def test_runtime_gpu_index_reads_config_and_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        gpu_selection, "load_raw_runtime_config", lambda _p: {"gpu": {"index": 3}}
    )
    assert runtime_gpu_index(tmp_path / "c.toml") == 3

    def _boom(_p):
        raise RuntimeError("bad config")

    monkeypatch.setattr(gpu_selection, "load_raw_runtime_config", _boom)
    assert runtime_gpu_index(tmp_path / "c.toml") == 0


def test_persist_runtime_gpu_index_writes_selected(tmp_path, monkeypatch) -> None:
    written = {}
    monkeypatch.setattr(
        gpu_selection, "load_raw_runtime_config", lambda _p: {"other": 1}
    )
    monkeypatch.setattr(
        gpu_selection, "write_config", lambda path, cfg: written.update(cfg)
    )
    result = persist_runtime_gpu_index(4, config_path=tmp_path / "c.toml")
    assert result == 4
    assert written["gpu"]["index"] == 4
    assert written["other"] == 1


def test_persist_runtime_gpu_index_never_rewrites_an_unreadable_config(
    tmp_path, monkeypatch
) -> None:
    """A torn/corrupt read must not become a destructive full rewrite.

    Rewriting from {} would drop every section the reader never saw — the
    user-visible symptom was the [ui] persist-on-startup toggle deselecting
    itself after a scan start (which persists the GPU index on this path).
    """
    written = []

    def _boom(_p):
        raise RuntimeError("bad config")

    monkeypatch.setattr(gpu_selection, "load_raw_runtime_config", _boom)
    monkeypatch.setattr(
        gpu_selection, "write_config", lambda path, cfg: written.append(cfg)
    )
    assert persist_runtime_gpu_index(-1, config_path=tmp_path / "c.toml") == 0
    assert written == []


# --- ui/tuning.py -------------------------------------------------------------


def test_performance_preset_label_and_tooltip() -> None:
    assert auto_uv_performance_preset_label() == "Performance"
    assert "2-bin tail curve" in auto_uv_performance_preset_tooltip()
    assert "Performance Auto-OC ladder" in auto_uv_performance_preset_tooltip()


def test_power_limit_default_caps_efficiency_from_stock_tgp() -> None:
    # 5080 FE: default 360 W, driver range 300-390 W. The cap is a fraction of
    # the STOCK budget, not the raised 390 W OC maximum.
    default = auto_uv_power_limit_default(
        max_w=390.0,
        min_w=300.0,
        default_w=360.0,
        gpu_name="NVIDIA GeForce RTX 5080",
        preset_id="efficiency",
    )
    assert default.preset_matched is True
    # 88% stored less the fixed 12% efficiency reduction = 77.44%.
    assert default.pct == pytest.approx(77.44)
    # 360 W * 77.44% = 278.8 -> 279 W would drop under the 300 W driver floor,
    # so the efficiency default clamps up to the 300 W hardware minimum.
    assert default.watts == 300


def test_power_limit_default_balanced_keeps_stock_board_power() -> None:
    default = auto_uv_power_limit_default(
        max_w=390.0,
        min_w=300.0,
        default_w=360.0,
        gpu_name="NVIDIA GeForce RTX 5080",
        preset_id="balanced",
    )
    # Balanced keeps the stock budget so the full scan's balanced descent
    # stays donatable to the performance tier.
    assert default.pct == pytest.approx(100.0)
    assert default.watts == 360


def test_power_limit_default_performance_keeps_stock_board_power() -> None:
    default = auto_uv_power_limit_default(
        max_w=390.0,
        default_w=360.0,
        gpu_name="NVIDIA GeForce RTX 5080",
        preset_id="performance",
    )
    # Performance means the stock budget, not the raised OC maximum.
    assert default.pct == 100.0
    assert default.watts == 360


def test_power_limit_default_falls_back_to_max_without_default() -> None:
    default = auto_uv_power_limit_default(
        max_w=360.0,
        gpu_name="NVIDIA GeForce RTX 5080",
        preset_id="efficiency",
    )
    # No default_w and no min_w: base is max_w (360), reduced by efficiency to
    # 360 * 77.44% = 278.8 -> 279 W, with no driver floor to clamp against.
    assert default.watts == 279


def test_power_limit_default_clamps_to_min_floor() -> None:
    default = auto_uv_power_limit_default(
        max_w=220.0,
        min_w=180.0,
        default_w=200.0,
        gpu_name="NVIDIA GeForce RTX 5070",
        preset_id="efficiency",
    )
    # 200 W * 80% = 160 W would drop under the 180 W driver floor.
    assert default.watts == 180


def test_power_limit_default_without_limits_is_unresolved() -> None:
    default = auto_uv_power_limit_default(
        max_w=None,
        gpu_name="NVIDIA GeForce RTX 5080",
        preset_id="efficiency",
    )
    assert default.watts is None
    assert default.preset_matched is False


def test_power_limit_default_unlisted_gpu_defaults_stock_power() -> None:
    default = auto_uv_power_limit_default(
        max_w=275.0,
        default_w=250.0,
        gpu_name="totally-unknown-gpu-9999",
        preset_id="efficiency",
    )
    assert default.preset_matched is False
    assert default.watts == 250
    assert default.pct == 100.0


def test_performance_target_default_for_unknown_gpu() -> None:
    target = auto_uv_performance_target_default(gpu_name="totally-unknown-gpu-9999")
    assert target.preset_matched is False
    assert target.voltage_mv is None
    assert target.gpu_name == "totally-unknown-gpu-9999"


def test_auto_uv_nvml_info_text_is_compact_and_tuning_relevant() -> None:
    text = auto_uv_nvml_info_text(
        AutoUvNvmlInfo(
            power_draw_w=42.25,
            power_management_enabled=True,
            power_limit_set_supported=True,
            power_limit_w=320.0,
            power_limit_default_w=350.0,
            power_limit_min_w=200.0,
            power_limit_max_w=450.0,
            graphics_clock_mhz=2100,
            memory_clock_mhz=10501,
            supported_memory_clocks_mhz=(810, 5001, 10501),
            supported_graphics_clock_steps_mhz=(210, 3000, 3015),
        )
    )

    assert "Power limit: current 320 W | default 350 W | range 200-450 W" in text
    assert "Current draw" not in text
    assert "42.2 W" not in text
    assert "Clocks now: core 2100 MHz | memory 10501 MHz" in text
    assert "Supported memory clocks: 810, 5001, 10501 MHz" in text
    assert "Supported core range: 210-3015 MHz (3 steps)" in text
    assert "Power management: enabled" in text
    assert "Fixed power-limit writes: supported" in text
    assert "RTX" not in text
    assert "PCI" not in text
    assert "thermal" not in text.lower()
    assert "perf cap" not in text.lower()


def test_auto_uv_nvml_info_text_summarizes_long_clock_lists() -> None:
    text = auto_uv_nvml_info_text(
        AutoUvNvmlInfo(
            supported_memory_clocks_mhz=(1000, 2000, 3000, 4000, 5000, 6000),
        )
    )

    assert text == "Supported memory clocks: 1000-6000 MHz (6 steps)"


def test_read_auto_uv_nvml_info_skips_setter_probe_without_power_limits(
    monkeypatch,
) -> None:
    # No daemon snapshot -> no power capabilities -> the power-limit setter
    # probe must not run and support stays unknown (None).
    monkeypatch.setattr(
        tuning,
        "DaemonGpuClient",
        lambda _gpu_index: (_ for _ in ()).throw(RuntimeError("daemon down")),
    )
    info = tuning.read_auto_uv_nvml_info(0)

    assert info.power_limit_set_supported is None
    assert info.power_limit_min_w is None
    assert info.power_limit_max_w is None


def test_read_auto_uv_nvml_info_grays_power_controls_from_setter_probe(
    monkeypatch,
) -> None:
    # RTX 2050 mobile GPUs can expose readable limits under a desktop-like
    # name. The setter probe, not identity strings, must disable the controls.
    power = SimpleNamespace(
        management_enabled=True,
        current_w=43.0,
        default_w=60.0,
        minimum_w=35.0,
        maximum_w=80.0,
    )
    telemetry = SimpleNamespace(power_draw_w=42.0, clocks=None)
    snapshot = SimpleNamespace(
        capabilities=SimpleNamespace(
            power=power,
            supported_memory_clocks_mhz=(),
            supported_core_clocks_mhz=(),
        ),
        telemetry=telemetry,
    )

    probe_calls: list[bool] = []

    class FakeDaemonClient:
        def __init__(self, _gpu_index):
            pass

        def snapshot(self, *, refresh=False):
            return snapshot

        def power_limit_set_supported(self) -> bool:
            probe_calls.append(True)
            return False

    monkeypatch.setattr(tuning, "DaemonGpuClient", FakeDaemonClient)

    info = tuning.read_auto_uv_nvml_info(0)

    assert info.power_draw_w == 42.0
    assert info.power_limit_set_supported is False
    assert probe_calls == [True]


class _FakeController:
    def __init__(self, *, gpu_index, raise_on_query=False, driver_range=(0, 1500)):
        self._raise = raise_on_query
        self._range = driver_range
        self.gpu_index = int(gpu_index)

    def capabilities(self):
        if self._raise:
            raise RuntimeError("nvml down")
        return SimpleNamespace(memory_clock_offset_range_mhz=self._range)


def test_memory_offset_range_clamps_driver_max(monkeypatch) -> None:
    monkeypatch.setattr(
        tuning,
        "DaemonGpuClient",
        lambda *, gpu_index: _FakeController(
            gpu_index=gpu_index, driver_range=(0, 1500)
        ),
    )
    assert memory_offset_mhz_range() == (0, 1500)


def test_memory_offset_range_falls_back_on_error(monkeypatch) -> None:
    monkeypatch.setattr(
        tuning,
        "DaemonGpuClient",
        lambda *, gpu_index: _FakeController(gpu_index=gpu_index, raise_on_query=True),
    )
    assert memory_offset_mhz_range() == (0, 2000)


def test_memory_offset_range_falls_back_on_empty_range(monkeypatch) -> None:
    monkeypatch.setattr(
        tuning,
        "DaemonGpuClient",
        lambda *, gpu_index: _FakeController(gpu_index=gpu_index, driver_range=()),
    )
    assert memory_offset_mhz_range() == (0, 2000)


def test_voltage_floor_range_knee_from_curve(monkeypatch) -> None:
    # Idle shelf at 180 MHz; knee = lowest voltage reaching >= half the max clock
    # (3000/2 = 1500): 750 mV -> 757 (below), 800 mV -> 1875 (at/above) -> knee 800.
    points = [(700, 180), (750, 757), (800, 1875), (850, 2167), (1200, 3000)]
    monkeypatch.setattr(
        tuning,
        "DaemonGpuClient",
        lambda *, gpu_index: _FakeVfReader(points),
    )
    assert auto_uv_voltage_floor_range_mv(gpu_index=0) == (800, 1200)


def test_voltage_floor_range_falls_back_without_curve(monkeypatch) -> None:
    def fail_client(*, gpu_index):
        raise RuntimeError(f"GPU {gpu_index} unavailable")

    monkeypatch.setattr(tuning, "DaemonGpuClient", fail_client)
    assert auto_uv_voltage_floor_range_mv(gpu_index=0) == (800, 1250)


def test_tuning_uses_shared_runtime_gpu_index() -> None:
    # tuning now delegates to the single shared gpu_selection.runtime_gpu_index
    # (behaviour itself is covered by test_runtime_gpu_index_reads_config_and_defaults).
    assert tuning.runtime_gpu_index is gpu_selection.runtime_gpu_index
