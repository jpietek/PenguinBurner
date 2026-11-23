"""The new window's Game Library page and its Perf Profile drill-down
(ui/features/library): real launcher writes, sessions, and the ring source."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore as C

from integrations.launchers.library import LauncherWriteState, LibraryGame
from runtime.frame_history import (
    MetricsSample,
    frame_history_file_stem,
    write_frame_history,
)
from ui.features.library import library_page as page_module
from ui.features.library.library_page import (
    gpu_text,
    launcher_names_text,
)
from ui.features.library.library_tab import GameLibraryTab
from ui.features.library.perf_source import RuntimePerfSource


class _Manager:
    """Records the setter calls a real launcher manager would perform."""

    def __init__(self, games):
        self.calls: list[tuple] = []
        self._games = games

    def __getattr__(self, name):
        if not name.startswith("set_"):
            raise AttributeError(name)

        def setter(game_id, *args):
            self.calls.append((name, game_id, *args))
            field = {
                "set_game_enabled": "enabled",
                "set_game_mode": "mode",
                "set_game_target_fps": "target_fps",
                "set_game_telemetry": "telemetry",
            }.get(name)
            if field and game_id in self._games:
                setting = self._games[game_id].detail.setting
                setattr(setting, field, args[0])
            return SimpleNamespace(ok=True, message="")

        return setter


class _Source:
    """The launcher contract the real adapters fill, and nothing else."""

    def __init__(self, launcher_id, display_name, games, *, state=None):
        self.launcher_id = launcher_id
        self.display_name = display_name
        self.icon_asset = f"tab-{launcher_id}.png"
        self.can_launch = True
        self._rows = {game.game_id: game for game in games}
        self.manager = _Manager(self._rows)
        self._state = state or LauncherWriteState()

    def available(self):
        return True

    def refresh(self, *, deep=True):
        pass

    def games(self):
        # The setting is shared, as a manager's cache is: rows follow it.
        from dataclasses import replace

        return tuple(
            replace(game, enabled=bool(game.detail.setting.enabled))
            for game in self._rows.values()
        )

    def fields(self, game):
        return ()

    def write_state(self):
        return self._state

    def bulk_actions(self):
        return ()

    def launch(self, game_id):
        raise AssertionError("tests never launch a game")

    def stop(self, game_id):
        raise AssertionError("tests never stop a game")


def _game(launcher, game_id, name, *, overlay_supported=True, **setting):
    values = {
        "enabled": False,
        "mode": "balanced",
        "overlay": True,
        "target_fps": None,
        "gpu_uuid": "",
        "telemetry": True,
    }
    values.update(setting)
    return LibraryGame(
        launcher=launcher,
        game_id=game_id,
        name=name,
        last_played=int(game_id) if game_id.isdigit() else 0,
        enabled=bool(values["enabled"]),
        overlay=bool(values["overlay"]),
        overlay_supported=overlay_supported,
        overlay_unsupported_reason="" if overlay_supported else "OpenGL detected.",
        detail=SimpleNamespace(setting=SimpleNamespace(**values)),
    )


def _sources():
    steam = _Source(
        "steam",
        "Steam",
        [
            _game("steam", "620", "Portal 2", enabled=True, mode="performance"),
            _game("steam", "400", "Portal", enabled=False, telemetry=False),
            _game("steam", "300", "Oxenfree", overlay_supported=False),
        ],
    )
    lutris = _Source("lutris", "Lutris", [_game("lutris", "27", "Celeste", enabled=True)])
    return steam, lutris


GPU = SimpleNamespace(index=0, name="GeForce RTX 5080", uuid="GPU-a")


@pytest.fixture(autouse=True)
def _no_daemon(monkeypatch, tmp_path):
    # Nothing here may reach the host's daemon or its rings.
    monkeypatch.setenv("PENGUIN_BURNER_DAEMON_SOCKET", str(tmp_path / "no-daemon.sock"))
    monkeypatch.setenv("PENGUIN_BURNER_FRAME_HISTORY_DIR", str(tmp_path / "live"))
    monkeypatch.setenv("PENGUIN_BURNER_FRAME_HISTORY_SYSTEM_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(page_module, "adaptive_tier_available", lambda *a, **k: True)


@pytest.fixture
def tab(qapp):
    steam, lutris = _sources()
    widget = GameLibraryTab(sources=(steam, lutris), gpu_choices=lambda: (GPU,))
    widget.steam, widget.lutris = steam, lutris
    widget.resize(1300, 860)
    widget.show()
    _scan(qapp, widget.library)
    yield widget
    widget.close()


def _scan(qapp, library):
    deadline = time.monotonic() + 3.0
    while (library.panel._scan_active or not library.panel._games) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert library.panel._games


def _finish_writes(qapp, panel):
    deadline = time.monotonic() + 3.0
    while (panel._setting_thread is not None or panel._setting_queue) and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert panel._setting_thread is None and not panel._setting_queue


def _sample(t, *, tier="balanced", fps=96.0):
    return MetricsSample(
        t_rel_s=t, frame_count=round(fps), clock_mhz=2550, mem_clock_mhz=10251,
        voltage_mv=965, power_w=214, gpu_util_pct=92, cpu_util_pct=34,
        cpu_peak_thread_pct=60, fan_pct=52, temperature_c=66, uv_offset_mv=-135,
        present_fps=fps, framegen_fps=0.0, latency_ms=22.4, display_latency_ms=12.1,
        framegen_active=False, adaptive=False, fps_source=2, tier=tier,
        ft_p50_ms=10.4, ft_p99_ms=14.1, ft_p999_ms=20.0,
    )


def _write_ring(directory, key, *, minutes=6, name=None, live_pid=None):
    stem = frame_history_file_stem(key)
    file_name = name or (f"{live_pid}.{stem}.ring" if live_pid else f"{stem}.ring")
    samples = [_sample(t) for t in range(minutes * 60)]
    frames = [10.4] * (minutes * 60 * 96)
    assert write_frame_history(
        directory / file_name, samples=samples, frametimes_ms=frames,
        power_limit_w=360, started_unix=1000,
    )
    return directory / file_name


# -- pure helpers --------------------------------------------------------------


def test_heading_and_status_helpers():
    assert launcher_names_text([]) == "No game launcher found"
    assert launcher_names_text(["Steam"]) == "Steam"
    assert launcher_names_text(["Steam", "Heroic", "Lutris"]) == "Steam, Heroic and Lutris"
    assert gpu_text((GPU,), "") == "GPU 0 · GeForce RTX 5080"
    assert gpu_text((GPU,), "GPU-a") == "GPU 0 · GeForce RTX 5080"
    assert gpu_text((GPU, SimpleNamespace(index=1, name="B", uuid="GPU-b")), "") == ""


# -- the page over real launcher contracts -------------------------------------


def test_page_lists_real_sources_and_filters(qapp, tab):
    library = tab.library
    panel = library.panel
    assert panel.library_label.text() == "Steam and Lutris"
    assert [panel.launcher_filter.itemData(i) for i in range(panel.launcher_filter.count())] == [
        "", "steam", "lutris",
    ]
    panel.search.setText("portal")
    visible = [
        panel.game_list.item(i).data(C.Qt.ItemDataRole.UserRole)
        for i in range(panel.game_list.count())
        if not panel.game_list.item(i).isHidden()
    ]
    assert sorted(visible) == ["steam:400", "steam:620"]
    panel.reset_filters()
    panel.wrap_filter.setCurrentIndex(panel.wrap_filter.findData("on"))
    assert panel.filters_button.text() == "Filters (1)"
    assert all(
        library.game(panel.game_list.item(i).data(C.Qt.ItemDataRole.UserRole)).enabled
        for i in range(panel.game_list.count())
        if not panel.game_list.item(i).isHidden()
    )


def test_selection_signal_carries_the_library_game(qapp, tab):
    seen = []
    tab.library.selected_game_changed.connect(seen.append)
    tab.library.select_game("lutris:27")
    assert seen and isinstance(seen[-1], LibraryGame)
    assert seen[-1].game_id == "27"
    assert tab.library.selected_key() == "lutris:27"
    # The classic badge is replaced by the entry beside Play.
    panel = tab.library.panel
    assert not panel.perf_profile_badge.button.isVisible()
    assert panel.perf_button.isVisible()


def test_capture_switch_writes_through_the_manager_and_reports(qapp, tab):
    changes = []
    tab.library.capture_changed.connect(lambda key, on: changes.append((key, on)))
    tab.library.select_game("steam:620")
    panel = tab.library.panel
    assert panel.telemetry_switch.isChecked()
    panel.telemetry_switch.setChecked(False)
    _finish_writes(qapp, panel)
    assert ("set_game_telemetry", "620", False) in tab.steam.manager.calls
    assert changes == [("steam:620", False)]


def test_enable_capture_from_the_perf_profile_is_the_same_write(qapp, tab):
    tab.library.enable_capture("steam:400")
    _finish_writes(qapp, tab.library.panel)
    assert tab.steam.manager.calls == [("set_game_telemetry", "400", True)]


def test_session_snapshot_reports_session_recording_and_status(qapp, tab, monkeypatch):
    library = tab.library
    monkeypatch.setattr(library, "system_target_fps", lambda: 60.0)
    sessions, recordings, statuses = [], [], []
    library.game_session_changed.connect(lambda *args: sessions.append(args))
    library.recording_changed.connect(lambda *args: recordings.append(args))
    library.status_changed.connect(statuses.append)
    panel = library.panel
    panel._apply_session_snapshot({
        "epoch": "e", "sequence": 1, "ended": [],
        "sessions": [{"app_id": "steam:620", "wrapped": True, "phase": "running",
                      "profile": "applied", "session_id": "s1"}],
    })
    assert sessions == [("steam:620", True, "performance", 60)]
    assert recordings == [("steam:620", True, "performance")]
    assert statuses[-1] == {
        "running": "Playing Portal 2",
        "detail": "Performance · GPU 0 · GeForce RTX 5080 · via Steam",
    }
    # Repeated snapshots report nothing new.
    panel._apply_session_snapshot({
        "epoch": "e", "sequence": 2, "ended": [],
        "sessions": [{"app_id": "steam:620", "wrapped": True, "phase": "running",
                      "profile": "applied", "session_id": "s1"}],
    })
    assert len(sessions) == 1 and len(statuses) == 1
    panel._apply_session_snapshot({"epoch": "e", "sequence": 3, "ended": [], "sessions": []})
    assert sessions[-1] == ("steam:620", False, "performance", 60)
    assert recordings[-1] == ("steam:620", False, "performance")
    assert statuses[-1] is None


def test_session_target_follows_the_game_or_system_target(qapp, tab, monkeypatch):
    library = tab.library
    monkeypatch.setattr(library, "system_target_fps", lambda: 90.0)
    assert library.session_target_fps(library.game("steam:620")) == 90
    library.game("steam:620").detail.setting.target_fps = 144.0
    assert library.session_target_fps(library.game("steam:620")) == 144


def test_unwrapped_or_capture_off_sessions_do_not_record(qapp, tab):
    library = tab.library
    assert library.recording_mode(library.game("steam:620")) == "performance"
    assert library.recording_mode(library.game("steam:400")) == ""  # unwrapped, capture off
    library.game("steam:620").detail.setting.mode = "stock"
    assert library.recording_mode(library.game("steam:620")) == ""


def test_external_process_is_not_a_library_session(qapp, tab):
    statuses = []
    tab.library.status_changed.connect(statuses.append)
    tab.library.panel._apply_game_states({"steam": {"620"}}, {"steam": {"620"}})
    assert statuses == []


def test_apply_adaptive_queues_the_three_panel_writes(qapp, tab):
    library = tab.library
    assert library.apply_adaptive("steam:400", 90)
    _finish_writes(qapp, library.panel)
    assert tab.steam.manager.calls == [
        ("set_game_enabled", "400", True),
        ("set_game_mode", "400", "adaptive"),
        ("set_game_target_fps", "400", 90.0),
    ]
    assert library.selected_key() == "steam:400"
    # OpenGL: Adaptive needs Vulkan frame data.
    assert not library.apply_adaptive("steam:300", 90)
    selected, games = library.adaptive_picker_state()
    assert selected == "steam:400"
    assert "steam:300" not in [key for key, *_ in games]


def test_apply_adaptive_needs_a_gpu_on_a_multi_gpu_host(qapp):
    steam, lutris = _sources()
    second = SimpleNamespace(index=1, name="RTX 4070", uuid="GPU-b")
    widget = GameLibraryTab(sources=(steam, lutris), gpu_choices=lambda: (GPU, second))
    widget.show()
    try:
        _scan(qapp, widget.library)
        assert not widget.library.apply_adaptive("steam:400", 90)
        assert "Choose a Game GPU" in widget.library.panel.status_label.text()
        assert steam.manager.calls == []
    finally:
        widget.close()


def test_adaptive_gate_reads_saved_profiles_per_gpu(qapp, monkeypatch):
    calls = []
    monkeypatch.setattr(
        page_module,
        "adaptive_tier_available",
        lambda profiles, **kw: calls.append((list(profiles), kw)) or False,
    )
    monkeypatch.setattr(page_module, "read_auto_uv_profile_summaries", lambda: [{"profile_id": "p"}])
    steam, lutris = _sources()
    widget = GameLibraryTab(sources=(steam, lutris), gpu_choices=lambda: (GPU,))
    widget.show()
    try:
        _scan(qapp, widget.library)
        widget.library.select_game("steam:620")
        assert calls[-1] == (
            [{"profile_id": "p"}],
            {"gpu_uuid": "GPU-a", "include_legacy_profiles": True},
        )
        combo = widget.library.panel.mode_combo
        assert not combo.model().item(combo.findData("adaptive")).isEnabled()
        widget.library.set_profile_summaries([{"profile_id": "q"}])
        assert calls[-1][0] == [{"profile_id": "q"}]
    finally:
        widget.close()


def test_overlay_publishing_needs_wrapper_overlay_and_vulkan(qapp, tab):
    assert tab.library.overlay_publishing("steam:620")
    assert not tab.library.overlay_publishing("steam:400")  # unwrapped
    assert not tab.library.overlay_publishing("steam:300")  # OpenGL


def test_page_scans_only_once_shown(qapp):
    steam, lutris = _sources()
    widget = GameLibraryTab(sources=(steam, lutris), gpu_choices=lambda: (GPU,))
    try:
        assert not widget.library.panel._scanned
        widget.show()
        assert widget.library.panel._scanned
    finally:
        widget.close()


# -- the ring source and the drill-down -----------------------------------------


def test_runtime_perf_source_reads_rings_and_caches_them(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    games = {"steam:620": _game("steam", "620", "Portal 2", target_fps=None)}
    env = {
        "PENGUIN_BURNER_FRAME_HISTORY_DIR": str(tmp_path / "live"),
        "PENGUIN_BURNER_FRAME_HISTORY_SYSTEM_DIR": str(archive),
    }
    source = RuntimePerfSource(games.get, env=env, system_target_fps=lambda: 75.0)
    assert source.record("steam:620") is None
    assert not source.has_recording("steam:620")
    assert source.game_name("steam:620") == "Portal 2"
    assert source.game_name("steam:1") == "steam:1"
    assert source.capture_enabled("steam:620")

    _write_ring(archive, "steam:620")
    assert source.has_recording("steam:620")
    recording = source.record("steam:620")
    assert recording is not None and not recording.live
    assert recording.tier == "Balanced" and recording.summary.tier == "Balanced"
    assert recording.summary.qualified
    assert (recording.target_fps, recording.target_is_game) == (75.0, False)
    assert recording.power_limit_w == 360
    assert source.record("steam:620") is recording  # unchanged ring: no decode

    games["steam:620"].detail.setting.target_fps = 120.0
    retargeted = source.record("steam:620")
    assert (retargeted.target_fps, retargeted.target_is_game) == (120.0, True)
    assert retargeted.version != recording.version

    live = tmp_path / "live"
    live.mkdir()
    _write_ring(live, "steam:620", minutes=1, live_pid=4242)
    grown = source.record("steam:620")
    assert grown.live and grown.version > retargeted.version
    assert not grown.summary.qualified


def test_perf_profile_opens_and_back_restores_the_selection(qapp, tab, tmp_path):
    _write_ring(tmp_path / "archive", "steam:620")
    library = tab.library
    library.panel.search.setText("portal 2")
    library.select_game("steam:620")
    button = library.panel.perf_button
    button.sync("steam:620")
    assert button.has_recording
    button.click()
    assert tab.currentWidget() is tab.perf_page
    assert tab.perf_page.game_key == "steam:620"
    assert tab.perf_page.state == "detail"
    assert tab.perf_page.title.text() == "Portal 2"
    tab.perf_page.back_button.click()
    assert tab.currentWidget() is library
    assert library.selected_key() == "steam:620"
    assert library.panel.search.text() == "portal 2"


def test_perf_profile_explains_capture_off_and_turns_it_on(qapp, tab):
    tab.open_perf_profile("steam:400")
    assert tab.perf_page.state == "captureoff"
    assert tab.perf_page.capture_button.isVisible()
    tab.perf_page.capture_button.click()
    _finish_writes(qapp, tab.library.panel)
    assert tab.steam.manager.calls == [("set_game_telemetry", "400", True)]
    tab.perf_page.refresh(force=True)
    assert tab.perf_page.state == "norecord"


def test_refresh_rereads_profiles_and_rescans_quietly(qapp, tab, monkeypatch):
    scans = []
    monkeypatch.setattr(tab.library.panel, "rescan", lambda **kw: scans.append(kw))
    tab.library._profile_summaries = [{"profile_id": "old"}]
    tab.refresh()
    assert scans == [{"deep": False, "quiet": True}]
    assert tab.library._profile_summaries != [{"profile_id": "old"}]
