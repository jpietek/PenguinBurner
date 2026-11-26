"""ui/main.py launch-option parsing (pure)."""

from __future__ import annotations

import pytest

from ui.main import parse_gui_args, parse_gui_launch_options


def test_parse_gui_launch_options_gpu_index() -> None:
    opts = parse_gui_launch_options(["pburn", "--gpu-index", "2", "extra"])
    assert opts.gpu_index == 2
    assert opts.qt_argv == ["pburn", "extra"]


def test_parse_gui_launch_options_equals_forms() -> None:
    opts = parse_gui_launch_options(["pburn", "--index=3"])
    assert opts.gpu_index == 3


def test_parse_gui_launch_options_empty_argv_defaults() -> None:
    opts = parse_gui_launch_options([])
    assert opts.qt_argv == ["penguin-burner-ui"]
    assert opts.gpu_index is None


def test_parse_gui_launch_options_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        parse_gui_launch_options(["pburn", "--gpu-index", "x"])
    with pytest.raises(ValueError):
        parse_gui_launch_options(["pburn", "--gpu-index"])


def test_parse_gui_args_returns_qt_argv() -> None:
    assert parse_gui_args(["pburn", "--gpu-index", "1", "passthrough"]) == [
        "pburn",
        "passthrough",
    ]
