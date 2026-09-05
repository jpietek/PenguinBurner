"""Classifying how a game presents, to know if the Vulkan overlay can attach."""

from __future__ import annotations

from overlay.render_api import (
    RENDER_OPENGL,
    RENDER_UNKNOWN,
    RENDER_VULKAN,
    detect_game_render_api,
    detect_render_api,
    overlay_support,
)

_ELF = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8


def _binary(path, body: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_ELF + body)
    return path


# -- one file ----------------------------------------------------------------


def test_a_vulkan_binary_is_detected(tmp_path) -> None:
    assert detect_render_api(_binary(tmp_path / "g", b"libvulkan.so.1")) == RENDER_VULKAN


def test_an_opengl_binary_is_detected(tmp_path) -> None:
    exe = _binary(tmp_path / "g", b"libGL.so.1 libGLX.so.0")
    assert detect_render_api(exe) == RENDER_OPENGL


def test_vulkan_wins_when_a_binary_names_both(tmp_path) -> None:
    # A game that can do either presents through Vulkan when asked.
    exe = _binary(tmp_path / "g", b"libGL.so.1 libvulkan.so.1")
    assert detect_render_api(exe) == RENDER_VULKAN


def test_a_non_elf_file_says_nothing(tmp_path) -> None:
    # A GOG start.sh wrapper is not the renderer; the real binary is.
    script = tmp_path / "start.sh"
    script.write_bytes(b"#!/bin/sh\nexec ./game/Game.x86_64\n")
    assert detect_render_api(script) == RENDER_UNKNOWN


def test_a_missing_or_empty_path_is_unknown(tmp_path) -> None:
    assert detect_render_api(None) == RENDER_UNKNOWN
    assert detect_render_api("") == RENDER_UNKNOWN
    assert detect_render_api(tmp_path / "nope") == RENDER_UNKNOWN


# -- across a game directory -------------------------------------------------


def test_the_directory_walk_finds_the_binary_behind_a_wrapper(tmp_path) -> None:
    """The named exe is a script; the real ELF sits one level down."""
    (tmp_path / "start.sh").write_bytes(b"#!/bin/sh\nexec game/Game.x86_64\n")
    _binary(tmp_path / "game" / "Game.x86_64", b"libGL.so.1")

    assert (
        detect_game_render_api(tmp_path, exe_hint=tmp_path / "start.sh")
        == RENDER_OPENGL
    )


def test_the_hint_short_circuits_the_walk(tmp_path) -> None:
    exe = _binary(tmp_path / "Game.x86_64", b"libvulkan.so.1")
    _binary(tmp_path / "game" / "other", b"libGL.so.1")
    assert detect_game_render_api(tmp_path, exe_hint=exe) == RENDER_VULKAN


def test_vulkan_anywhere_in_the_tree_wins(tmp_path) -> None:
    _binary(tmp_path / "game" / "Game.x86_64", b"libGL.so.1")
    _binary(tmp_path / "game" / "renderer_vk.so", b"libvulkan.so.1")
    assert detect_game_render_api(tmp_path) == RENDER_VULKAN


def test_a_tree_with_no_classifiable_binary_is_unknown(tmp_path) -> None:
    (tmp_path / "readme.txt").write_text("hi", encoding="utf-8")
    _binary(tmp_path / "engine.so", b"just sdl and audio")
    assert detect_game_render_api(tmp_path) == RENDER_UNKNOWN


# -- the overlay decision ----------------------------------------------------


def test_a_translated_game_is_supported_without_touching_disk() -> None:
    supported, reason = overlay_support(
        translated_to_vulkan=True, directory="/nonexistent"
    )
    assert supported is True
    assert reason == ""


def test_a_native_opengl_game_is_not_supported_with_a_reason(tmp_path) -> None:
    _binary(tmp_path / "game" / "Game.x86_64", b"libGL.so.1")
    supported, reason = overlay_support(translated_to_vulkan=False, directory=tmp_path)
    assert supported is False
    assert "OpenGL" in reason


def test_a_native_vulkan_game_is_supported(tmp_path) -> None:
    _binary(tmp_path / "game" / "Game.x86_64", b"libvulkan.so.1")
    supported, _ = overlay_support(translated_to_vulkan=False, directory=tmp_path)
    assert supported is True


def test_an_undetermined_native_game_fails_open(tmp_path) -> None:
    """A native game the scan cannot classify keeps its overlay -- a detection
    miss must never hide a control that would have worked."""
    _binary(tmp_path / "game" / "Game.x86_64", b"opaque engine")
    supported, reason = overlay_support(translated_to_vulkan=False, directory=tmp_path)
    assert supported is True
    assert reason == ""


def test_opengl_hint_does_not_hide_a_separate_vulkan_renderer(tmp_path) -> None:
    exe = _binary(tmp_path / "game", b"libGL.so.1")
    _binary(tmp_path / "renderer.so", b"libvulkan.so.1")
    assert detect_game_render_api(tmp_path, exe_hint=exe) == RENDER_VULKAN


def test_incomplete_directory_scan_keeps_controls_available(tmp_path, monkeypatch) -> None:
    import overlay.render_api as render_api

    exe = _binary(tmp_path / "game", b"libGL.so.1")
    monkeypatch.setattr(render_api, "_MAX_FILES", 0)
    assert detect_game_render_api(tmp_path, exe_hint=exe) == RENDER_UNKNOWN


def test_non_regular_file_is_not_opened(tmp_path) -> None:
    import os

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    assert detect_render_api(fifo) == RENDER_UNKNOWN


def test_large_asset_tree_does_not_hide_the_renderer(tmp_path) -> None:
    """A GOG launcher can sit over hundreds of textures and one real ELF."""
    wrapper = tmp_path / "start.sh"
    wrapper.write_text("#!/bin/sh\nexec game/Soma.bin.x86_64\n")
    _binary(tmp_path / "game" / "Soma.bin.x86_64", b"libGL.so.1")
    assets = tmp_path / "game" / "textures"
    assets.mkdir()
    for index in range(600):
        (assets / f"{index}.dds").write_bytes(b"DDS texture data")

    assert detect_game_render_api(tmp_path, exe_hint=wrapper) == RENDER_OPENGL
    _binary(tmp_path / "game" / "renderer.so", b"libvulkan.so.1")
    assert detect_game_render_api(tmp_path, exe_hint=wrapper) == RENDER_VULKAN


def test_unclassified_elf_files_count_toward_the_binary_limit(tmp_path, monkeypatch) -> None:
    import overlay.render_api as render_api

    monkeypatch.setattr(render_api, "_MAX_BINARIES", 1)
    exe = _binary(tmp_path / "game", b"libGL.so.1")
    for index in range(2):
        _binary(tmp_path / f"engine{index}.so", b"opaque engine")
    assert detect_game_render_api(tmp_path, exe_hint=exe) == RENDER_UNKNOWN
