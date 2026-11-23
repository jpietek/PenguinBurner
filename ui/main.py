from __future__ import annotations

import sys
from dataclasses import dataclass

from common.flatpak_wrappers import ensure_host_integration

from .assets import application_icon
from .constants import APP_DESKTOP_ID, APP_DISPLAY_NAME
from .qt import (
    apply_dark_palette,
    apply_desktop_font_settings,
    import_qt,
    prepare_desktop_scale_env,
)


@dataclass(frozen=True, slots=True)
class GuiLaunchOptions:
    qt_argv: list[str]
    gpu_index: int | None = None
    # The classic window instead of the new one, until every page is ported.
    classic: bool = False


def parse_gui_launch_options(argv: list[str] | None = None) -> GuiLaunchOptions:
    raw = list(sys.argv if argv is None else argv)
    if not raw:
        raw = ["penguin-burner-ui"]
    qt_argv = [raw[0]]
    gpu_index = None
    classic = False
    index = 1
    while index < len(raw):
        arg = raw[index]
        if arg == "--new-ui":
            index += 1
            continue
        if arg == "--classic":
            classic = True
            index += 1
            continue
        if arg in {"--gpu-index", "--index"}:
            if index + 1 >= len(raw):
                raise ValueError(f"{arg} requires an integer value")
            try:
                gpu_index = max(0, int(raw[index + 1]))
            except ValueError as exc:
                raise ValueError(f"{arg} requires an integer value") from exc
            index += 2
            continue
        for prefix in ("--gpu-index=", "--index="):
            if arg.startswith(prefix):
                try:
                    gpu_index = max(0, int(arg[len(prefix) :]))
                except ValueError as exc:
                    raise ValueError(f"{prefix[:-1]} requires an integer value") from exc
                break
        else:
            qt_argv.append(arg)
        index += 1
    return GuiLaunchOptions(
        qt_argv=qt_argv,
        gpu_index=gpu_index,
        classic=classic,
    )


def parse_gui_args(argv: list[str] | None = None) -> list[str]:
    return parse_gui_launch_options(argv).qt_argv


def run(argv: list[str] | None = None) -> int:
    try:
        launch_options = parse_gui_launch_options(sys.argv if argv is None else argv)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    qt_argv = launch_options.qt_argv
    try:
        qt_modules = import_qt()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    prepare_desktop_scale_env()
    _QtCore, QtGui, QtWidgets, _pg = qt_modules
    app = QtWidgets.QApplication(qt_argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    if hasattr(app, "setApplicationDisplayName"):
        app.setApplicationDisplayName(APP_DISPLAY_NAME)
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName(APP_DESKTOP_ID)
    try:
        ensure_host_integration()
    except Exception as exc:  # noqa: BLE001
        # Launcher integration repair failing must not keep the GPU tuning UI
        # from starting, whatever the failure mode (a corrupt packaged
        # manifest raises ValueError/KeyError, not just OSError/RuntimeError);
        # the launcher write actions themselves re-check and refuse.
        print(
            "warning: PenguinBurner could not repair its launcher integration; "
            f"wrapped game launches will not work until this is fixed: {exc}",
            file=sys.stderr,
        )
    icon = application_icon(QtGui)
    if not icon.isNull():
        app.setWindowIcon(icon)
    apply_desktop_font_settings(app, QtGui)
    apply_dark_palette(app, QtGui)
    if launch_options.classic:
        window = _classic_window(qt_modules, launch_options.gpu_index)
        if not icon.isNull():
            window.window.setWindowIcon(icon)
    else:
        window = _new_window(app, launch_options.gpu_index)
        if not icon.isNull():
            window.setWindowIcon(icon)
    window.show()
    return int(app.exec())


def _classic_window(qt_modules, gpu_index: int | None):
    from .window import MainWindow

    return MainWindow(qt_modules, gpu_index=gpu_index)


def _new_window(app, gpu_index: int | None):
    from .shell_window import open_shell_window

    return open_shell_window(app, gpu_index=gpu_index)


def main(argv: list[str] | None = None) -> int:
    return run(sys.argv if argv is None else argv)
