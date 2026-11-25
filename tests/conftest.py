from __future__ import annotations

import site
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def write_desktop_rtd3_tree() -> Callable[[Path], tuple[Path, Path]]:
    """Build a fake NVIDIA PCI tree that explicitly selects Desktop mode."""

    def _write(base: Path) -> tuple[Path, Path]:
        address = "0000:01:00.0"
        sys_root = base / "rtd3-sys"
        proc_root = base / "rtd3-proc"
        device = sys_root / address
        (device / "power").mkdir(parents=True)
        (device / "vendor").write_text("0x10de\n", encoding="utf-8")
        (device / "class").write_text("0x030000\n", encoding="utf-8")
        (device / "power" / "control").write_text("auto\n", encoding="utf-8")
        (device / "power" / "runtime_status").write_text(
            "active\n", encoding="utf-8"
        )
        proc_gpu = proc_root / address
        proc_gpu.mkdir(parents=True)
        (proc_gpu / "power").write_text(
            "Runtime D3 status:          Disabled by default\n",
            encoding="utf-8",
        )
        return sys_root, proc_root

    return _write


@pytest.fixture(autouse=True)
def _isolate_user_home(monkeypatch, tmp_path_factory):
    """Keep the suite out of the developer's own PenguinBurner state.

    Every user-visible path (runtime config, saved profiles, caches) hangs off
    _effective_home(), and PENGUIN_BURNER_HOME overrides it. Without this,
    anything that reaches a real persistence helper writes the machine it runs
    on: selecting a GPU in a widget, for instance, calls through to
    persist_runtime_gpu_index and rewrites [gpu] index in the developer's
    ~/.config/PenguinBurner/penguin_burner.toml, silently repointing the tool
    at a different card.

    A test that needs to control the home itself sets the variable after this
    fixture has run, which wins.

    The directory comes from the factory rather than the test's own tmp_path:
    several tests assert on the exact contents of tmp_path, and a home planted
    inside it would show up as a stray entry.
    """
    home = tmp_path_factory.mktemp("penguin-burner-home")
    monkeypatch.setenv("PENGUIN_BURNER_HOME", str(home))
    # The KDE shortcut repair rewrites the pip --user desktop entry.
    monkeypatch.setattr(site, "getuserbase", lambda: str(home / "userbase"))


@pytest.fixture(autouse=True)
def _no_detached_launch_processes(monkeypatch):
    """Launcher main()-flow tests must never spawn real detached processes
    (re-front watcher, per-game FIFO drainer): those outlive the test run and
    litter ~/.cache with FIFOs. Tests that assert on the spawns monkeypatch
    these attributes themselves, which overrides this default stub."""
    from overlay import launcher

    monkeypatch.setattr(launcher, "spawn_refront_watcher", lambda env: None)
    monkeypatch.setattr(
        launcher, "spawn_detached_drainer", lambda env, path, session_pid=None: None
    )


@pytest.fixture(scope="session")
def build_overlay_probe(tmp_path_factory):
    from native_build import OverlayProbeBuilder

    return OverlayProbeBuilder(REPO_ROOT, tmp_path_factory.mktemp("overlay-objects"))


@pytest.fixture(scope="session")
def native_overlay_probe(tmp_path_factory, build_overlay_probe) -> Path:
    source = REPO_ROOT / "overlay/native/latency_layer/tools/overlay_text_probe.cpp"
    output = tmp_path_factory.mktemp("native-overlay-probe") / "overlay_text_probe"
    return build_overlay_probe(source, output)


@pytest.fixture(autouse=True)
def _launcher_command_cache():
    from integrations.launchers.installation import native_command

    native_command.cache_clear()
    yield
    native_command.cache_clear()


@pytest.fixture
def native_launcher_installed(monkeypatch):
    """Say the host has the native launcher, instead of asking the real one.

    select_installation() only keeps the native roots while the launcher has
    an executable on PATH, and falls back to ~/.var/app/<app id>/... when it
    does not. A test that asserts on native Lutris or Heroic paths without
    saying which host it is running on therefore passes on a developer machine
    that has the launcher and fails on CI, which does not -- so it has to say.

    A test about a missing native copy stubs host_command_path itself; that
    runs after this fixture and wins.
    """
    from integrations.launchers import installation

    monkeypatch.setattr(
        installation, "host_command_path", lambda name: f"/usr/bin/{name}"
    )
    installation.native_command.cache_clear()


def _dispose_window(window) -> None:
    """Close and delete a whole window now. Its connected lambdas keep the
    wrapper alive, so a closed window otherwise leaks, and every later
    app-wide restyle (isolated_theme) walks all of its widgets again."""
    from PySide6 import QtCore as C

    window.close()
    window.deleteLater()
    C.QCoreApplication.sendPostedEvents(None, C.QEvent.Type.DeferredDelete)


@pytest.fixture
def dispose_window():
    return _dispose_window


@pytest.fixture
def isolated_theme(qapp):
    """install_theme restyles the whole process (app style sheet, palette,
    font, the classic ui.theme colours and the switch). The two windows run
    in separate processes, but tests share one, so put everything back."""
    from PySide6 import QtCore as C
    from PySide6 import QtGui as G
    from PySide6 import QtWidgets as W

    import ui.design.style as design
    from ui import theme
    from ui.components.toggle_switch import toggle_switch_class

    switch = toggle_switch_class(C, G, W)
    saved = (
        {name: getattr(theme, name) for name in dir(theme) if name.isupper()},
        qapp.styleSheet(),
        G.QPalette(qapp.palette()),
        G.QFont(qapp.font()),
        switch.__dict__.get("_colors"),
        dict(design.COL),
        dict(design.TYPE),
        dict(design.TIER_COLORS),
        dict(design.MODE),
        design.APPEARANCE,
    )
    yield qapp
    constants, qss, palette, font, colors, col, type_, tiers, mode, appearance = saved
    for name, value in constants.items():
        setattr(theme, name, value)
    qapp.setStyleSheet(qss)
    qapp.setPalette(palette)
    qapp.setFont(font)
    if colors is not None:
        switch._colors = colors
    for live, value in (
        (design.COL, col),
        (design.TYPE, type_),
        (design.TIER_COLORS, tiers),
        (design.MODE, mode),
    ):
        live.clear()
        live.update(value)
    design.APPEARANCE = appearance


@pytest.fixture
def offline_profiles(monkeypatch):
    """The new window's Profiles source without a daemon or GPUs: no saved
    profiles, nothing running or saved for boot, and no status poll."""
    import ui.features.profiles.runtime_profiles_source as source_mod

    monkeypatch.setattr(source_mod, "detected_gpu_choices", lambda: [])
    monkeypatch.setattr(source_mod, "read_auto_uv_profile_summaries", lambda: [])
    monkeypatch.setattr(
        source_mod, "live_profile_status", lambda _uuids: ({}, {"": {}})
    )
    monkeypatch.setattr(source_mod, "STATUS_POLL_MS", 0)
    return source_mod


@pytest.fixture
def offline_auto_uv(monkeypatch):
    """The Auto UV page without the root service: a fresh scan engine and no
    daemon read for the GPU caption. Pages built in the test use it."""
    from ui.features.tuning import auto_uv_page
    from ui.features.tuning.gpu_selection import GpuChoice

    monkeypatch.setattr(
        auto_uv_page,
        "gpu_choices_with_fallback",
        lambda selected_index=None, **_: (
            [GpuChoice(index=0, name="NVIDIA GeForce RTX 5080")],
            0 if selected_index is None else int(selected_index),
        ),
    )
    monkeypatch.setitem(auto_uv_page.ENGINE, "engine", None)
    yield auto_uv_page
    engine = auto_uv_page.ENGINE["engine"]
    if engine is not None:
        engine.page = None
        engine.deleteLater()
