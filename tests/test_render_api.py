"""Classifying how a game presents, to know if the Vulkan overlay can attach."""

from __future__ import annotations

from overlay.render_api import (
    RENDER_OPENGL,
    RENDER_UNKNOWN,
    RENDER_VULKAN,
    detect_render_api,
    overlay_support,
)


def _binary(tmp_path, name: str, body: bytes):
    path = tmp_path / name
    path.write_bytes(body)
    return path


def test_a_vulkan_binary_is_detected(tmp_path) -> None:
    exe = _binary(tmp_path, "game", b"\x7fELF...libvulkan.so.1...")
    assert detect_render_api(exe) == RENDER_VULKAN


def test_an_opengl_binary_is_detected(tmp_path) -> None:
    exe = _binary(tmp_path, "game", b"\x7fELF...libGL.so.1...libGLX.so.0...")
    assert detect_render_api(exe) == RENDER_OPENGL


def test_vulkan_wins_when_a_binary_names_both(tmp_path) -> None:
    # A game that can do either presents through Vulkan when asked, so the
    # overlay can attach -- do not gate it out.
    exe = _binary(tmp_path, "game", b"libGL.so.1 libvulkan.so.1")
    assert detect_render_api(exe) == RENDER_VULKAN


def test_neither_reference_is_unknown(tmp_path) -> None:
    exe = _binary(tmp_path, "game", b"just some bytes")
    assert detect_render_api(exe) == RENDER_UNKNOWN


def test_a_missing_or_empty_path_is_unknown(tmp_path) -> None:
    assert detect_render_api(None) == RENDER_UNKNOWN
    assert detect_render_api("") == RENDER_UNKNOWN
    assert detect_render_api(tmp_path / "does-not-exist") == RENDER_UNKNOWN


def test_a_translated_game_is_supported_without_touching_disk() -> None:
    # Wine/Proton translate DirectX to Vulkan, so the overlay always attaches --
    # the executable (a Windows .exe under a prefix) is never inspected.
    supported, reason = overlay_support(
        translated_to_vulkan=True, executable="/nonexistent/game.exe"
    )
    assert supported is True
    assert reason == ""


def test_a_native_opengl_game_is_not_supported_with_a_reason(tmp_path) -> None:
    exe = _binary(tmp_path, "game", b"libGL.so.1")
    supported, reason = overlay_support(translated_to_vulkan=False, executable=exe)
    assert supported is False
    assert "OpenGL" in reason


def test_a_native_vulkan_game_is_supported(tmp_path) -> None:
    exe = _binary(tmp_path, "game", b"libvulkan.so.1")
    supported, _ = overlay_support(translated_to_vulkan=False, executable=exe)
    assert supported is True


def test_an_undetermined_native_game_fails_open(tmp_path) -> None:
    """A native game the scan cannot classify keeps its overlay -- a detection
    miss must never hide a control that would have worked."""
    exe = _binary(tmp_path, "game", b"opaque")
    supported, reason = overlay_support(translated_to_vulkan=False, executable=exe)
    assert supported is True
    assert reason == ""
