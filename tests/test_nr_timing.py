"""One session transport carries GPU durations alongside existing Reflex data."""
from __future__ import annotations

import pytest

from overlay.telemetry.nr_timing import parse_nr_timing

RECORD = "pb-nr-gpu: pid=42 device=0000abc0 sample=7 gpuUs=2200.125 api=d3d12 scope=model"


def test_nr_protocol_keeps_fractional_gpu_microseconds_and_session():
    result = parse_nr_timing(RECORD, "700")
    assert result == {
        "v": 1, "type": "timing", "measurement": "nr-gpu",
        "source": "ngx-runtime", "scope": "model", "api": "d3d12",
        "session_id": "700", "pid": 42, "device": "0000abc0",
        "sample_id": 7, "nr_gpu_us": 2200.125,
    }
    assert parse_nr_timing(RECORD, "") is None


@pytest.mark.parametrize("field,replacement", [
    ("2200.125", "0"), ("2200.125", "-1"), ("2200.125", "nan"),
    ("2200.125", "1000000"), ("2200.125", "1..2"),
    ("scope=model", "scope=cpu"), ("sample=7", "sample=0"),
    ("pid=42", "pid=0"), ("api=d3d12", "api=unknown"),
])
def test_nr_protocol_rejects_invalid_values(field, replacement):
    assert parse_nr_timing(RECORD.replace(field, replacement), "700") is None


def test_bridge_keeps_nr_and_reflex_in_same_session(monkeypatch, tmp_path):
    import overlay.telemetry.nvapi_marker_bridge as bridge
    lines = iter([
        "1.000:2a:0:latency-marker:pb:qpcUs=1000000 frameID=7 markerType=SIMULATION_START",
        RECORD,
        "1.010:2a:0:latency-marker:pb:qpcUs=1010000 frameID=7 markerType=PRESENT_END",
    ])
    samples = []
    monkeypatch.setattr(bridge, "_follow", lambda *_a, **_kw: lines)
    monkeypatch.setattr(bridge, "_send_sample", lambda _s, _t, sample: samples.append(sample))
    bridge.run(tmp_path / "markers.fifo", env={bridge.TELEMETRY_SESSION_ENV: "700"}, pid=999)
    assert len(samples) == 2
    assert samples[0]["nr_gpu_us"] == 2200.125
    assert samples[1]["sim_to_present_us"] == 10000
    assert "nr_gpu_us" not in samples[1]
    assert all(s["session_id"] == "700" and s["pid"] == 42 for s in samples)


def test_nr_gui_toggle_persists_and_drives_independent_capture(tmp_path, monkeypatch):
    from PySide6 import QtCore, QtWidgets

    from overlay import launcher
    from overlay.config import OVERLAY_CONFIG_ENV, load_overlay_config
    from overlay.metrics import METRICS
    from overlay.overlay_text import format_overlay_item
    from ui.components.overlay_config import OverlayConfigPanel

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    path = tmp_path / "overlay.toml"
    panel = OverlayConfigPanel(QtCore=QtCore, QtWidgets=QtWidgets, config_path=path)
    checkbox = panel.item_checkboxes["nr_gpu_ms"]
    assert METRICS["nr_gpu_ms"].label == "DLSS 5 neural rendering GPU time"
    checkbox.setChecked(True)
    app.processEvents()
    assert "nr_gpu_ms" in load_overlay_config(path).enabled_item_ids
    assert format_overlay_item("nr_gpu_ms", {"nr_gpu_ms": "2.20"}) == "dlss5 nr 2.20 ms"
    assert format_overlay_item("nr_gpu_ms", {}) == "dlss5 nr -"
    configured = []
    monkeypatch.setattr(launcher, "_configure_dxvk_nvapi_marker_output", lambda *_a, **_kw: configured.append(True) or True)
    env = {OVERLAY_CONFIG_ENV: str(path), "PB_OVERLAY": "1", "PB_INGAME_LATENCY": "0"}
    assert launcher.configure_penguin_burner_environment(env)
    assert env["PENGUIN_BURNER_NR_TIMING"] == "1"
    assert len(configured) == 1
    assert launcher.DISPLAY_LATENCY_ENV not in env
    checkbox.setChecked(False)
    app.processEvents()
    launcher.configure_penguin_burner_environment(env)
    assert env["PENGUIN_BURNER_NR_TIMING"] == "0"
    assert len(configured) == 1
    panel.widget.close()
