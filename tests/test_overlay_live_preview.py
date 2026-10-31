"""The live preview: the real layer in a small Vulkan window.

The painted preview is this app's own reading of the layout. This one is the
layer itself, so it cannot drift -- but it depends on a program that may not be
installed, and that case has to be a sentence rather than a traceback.
"""

from __future__ import annotations

from pathlib import Path

from overlay.live_preview import (
    MISSING_EXECUTABLE_MESSAGE,
    PREVIEW_EXECUTABLE,
    PREVIEW_PACKAGE,
    live_preview_command,
    live_preview_env,
    preview_executable,
)


def test_the_command_names_the_found_executable() -> None:
    command = live_preview_command(which=lambda name, path=None: f"/usr/bin/{name}")

    assert command is not None
    assert command[0] == f"/usr/bin/{PREVIEW_EXECUTABLE}"
    # A window, not fullscreen: the settings have to stay reachable behind it.
    assert "--width" in command and "--height" in command


def test_a_missing_executable_is_a_sentence_not_a_crash() -> None:
    assert live_preview_command(which=lambda *_args, **_kwargs: None) is None
    assert preview_executable({}, which=lambda *_args, **_kwargs: None) is None
    # The message has to say what to install, or it is no help at all.
    assert PREVIEW_PACKAGE in MISSING_EXECUTABLE_MESSAGE
    assert PREVIEW_EXECUTABLE in MISSING_EXECUTABLE_MESSAGE


def test_the_environment_turns_the_overlay_on_for_this_config(tmp_path) -> None:
    config = tmp_path / "overlay.toml"
    env = live_preview_env(config, env={"PATH": "/usr/bin"}, layer_dirs=[])

    assert env["PENGUIN_BURNER"] == "1"
    assert env["PB_OVERLAY"] == "1"
    # A hotkey-hidden game must not blank the preview window.
    assert env["PENGUIN_BURNER_OVERLAY_OVERRIDE"].startswith("/dev/null/")
    # The same file the tab writes, so the window follows what is edited.
    assert env["PENGUIN_BURNER_OVERLAY_CONFIG"] == str(config)
    # The caller's environment survives.
    assert env["PATH"] == "/usr/bin"


def test_a_found_layer_directory_is_put_on_the_loader_path(tmp_path) -> None:
    env = live_preview_env(
        tmp_path / "overlay.toml",
        env={"PATH": "/usr/bin"},
        layer_dirs=[Path("/opt/pb/layer")],
    )

    assert env["VK_ADD_IMPLICIT_LAYER_PATH"] == "/opt/pb/layer"


def test_an_existing_loader_path_is_kept_behind_ours(tmp_path) -> None:
    env = live_preview_env(
        tmp_path / "overlay.toml",
        env={"VK_ADD_IMPLICIT_LAYER_PATH": "/opt/other"},
        layer_dirs=[Path("/opt/pb/layer")],
    )

    assert env["VK_ADD_IMPLICIT_LAYER_PATH"] == "/opt/pb/layer:/opt/other"


def test_an_installed_layer_needs_no_loader_path(tmp_path) -> None:
    # A packaged PenguinBurner registers the layer with the loader, so there is
    # nothing to point at and the variable must not be invented.
    env = live_preview_env(tmp_path / "overlay.toml", env={}, layer_dirs=[])

    assert "VK_ADD_IMPLICIT_LAYER_PATH" not in env


def test_the_button_reports_a_missing_program_without_starting_anything(
    tmp_path, monkeypatch
) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import pytest

    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtWidgets

    from ui.components import overlay_config
    from ui.components.overlay_config import LIVE_PREVIEW_START, OverlayConfigPanel

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    _ = app
    panel = OverlayConfigPanel(
        QtCore=QtCore, QtWidgets=QtWidgets, config_path=tmp_path / "overlay.toml"
    )
    monkeypatch.setattr(overlay_config, "live_preview_command", lambda: None)

    panel.live_button.click()

    assert panel._live_process is None
    assert panel.live_button.text() == LIVE_PREVIEW_START
    assert MISSING_EXECUTABLE_MESSAGE in panel.status_label.text()
