"""The Game Perf Profile button a game's details header carries beside Play.

Same shape as Play in a secondary colour, with the tab's own icon. Hover or
keyboard focus peeks the game's fingerprint; a click opens the Game Perf
Profile tab through ``on_open``. Every launcher's games carry one (rings are
stored under the ``<launcher>:<id>`` game key); until a game has recorded
telemetry it is disabled and its tooltip says how to record some.
"""

from __future__ import annotations

from collections.abc import Callable

from runtime.frame_history import (
    frame_history_file_stem,
    read_frame_history_for_game,
    summarize,
)

from ..assets import asset_image_path
from ..dialogs.form_rows import wrapped_tooltip
from .game_perf_profile import GamePerfProfilePeek, warming_text

ICON_SIZE = 18


class GamePerfProfileBadge:
    def __init__(self, *, QtCore, QtGui, QtWidgets, parent) -> None:
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self._parent = parent
        #: on_open(game_key, game_name, target_fps) -- the window opens the tab.
        self.on_open: Callable[[str, str, float | None], None] | None = None
        self._game_key = ""
        self._game_name = ""
        self._target_fps: float | None = None
        self._summary = None
        self._power_limit_w = 0
        self._peek: GamePerfProfilePeek | None = None

        self.button = QtWidgets.QPushButton("Perf Profile")
        self.button.setObjectName("gamePerfProfileButton")
        icon_path = asset_image_path("tab-game-perf-profile.png")
        if icon_path is not None:
            self.button.setIcon(QtGui.QIcon(str(icon_path)))
        self.button.setIconSize(QtCore.QSize(ICON_SIZE, ICON_SIZE))
        self.button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.button.setVisible(False)
        self.button.setAccessibleName(
            "Game Perf Profile — open the Game Perf Profile tab"
        )
        self.button.clicked.connect(self._open)
        badge = self

        class _Hover(QtCore.QObject):
            """Hover or focus peeks the fingerprint; any click falls through
            to open the tab."""

            def eventFilter(self, watched, event):
                event_type = event.type()
                if event_type in (QtCore.QEvent.Type.Enter, QtCore.QEvent.Type.FocusIn):
                    badge._show_peek()
                elif event_type in (
                    QtCore.QEvent.Type.Leave,
                    QtCore.QEvent.Type.FocusOut,
                    QtCore.QEvent.Type.MouseButtonPress,
                    QtCore.QEvent.Type.Hide,
                ):
                    badge.hide_peek()
                return False

        self._hover_filter = _Hover(parent)
        self.button.installEventFilter(self._hover_filter)

    def _device_pixel_ratio(self) -> float:
        window = self._parent.window()
        handle = window.windowHandle() if window is not None else None
        return float(handle.devicePixelRatio()) if handle is not None else 1.0

    def sync(
        self, game_key: str = "", *, game_name: str = "", target_fps: float | None = None
    ) -> None:
        """Refresh for the selected game; disabled until it has recorded anything."""
        self._game_key = game_key if frame_history_file_stem(game_key) else ""
        self._game_name = game_name
        self._target_fps = target_fps
        if not self._game_key:
            self._summary = None
            self.hide_peek()
            self.button.setVisible(False)
            return
        history = read_frame_history_for_game(self._game_key)
        self.button.setVisible(True)
        self.button.setEnabled(history is not None)
        if history is None:
            self._summary = None
            self.hide_peek()
            self.button.setToolTip(
                wrapped_tooltip(
                    "Game Perf Profile unavailable — no telemetry recorded for "
                    "this game yet. Wrap the game, pick a mode other than "
                    "Default, keep Capture telemetry on, and play it."
                )
            )
            return
        summary = summarize(history)
        self._power_limit_w = history.header.power_limit_w
        if summary is not None and summary.qualified:
            self._summary = summary
            self.button.setToolTip(
                wrapped_tooltip("Game Perf Profile — click to open the full view.")
            )
        else:
            self._summary = None
            self.hide_peek()
            minutes = summary.minutes if summary is not None else 0.0
            self.button.setToolTip(
                wrapped_tooltip(f"Game Perf Profile — {warming_text(minutes)}")
            )

    def _show_peek(self) -> None:
        if self._summary is None:
            return
        if self._peek is None:
            self._peek = GamePerfProfilePeek(
                QtCore=self.QtCore,
                QtGui=self.QtGui,
                QtWidgets=self.QtWidgets,
                parent=self._parent,
            )
        below = self.button.mapToGlobal(self.button.rect().bottomRight())
        below.setY(below.y() + 6)
        self._peek.show_for(
            summary=self._summary,
            target_fps=self._target_fps,
            power_limit_w=self._power_limit_w,
            global_pos=below,
            device_pixel_ratio=self._device_pixel_ratio(),
            align_right=True,
        )

    def hide_peek(self) -> None:
        if self._peek is not None:
            self._peek.hide()

    def _open(self) -> None:
        self.hide_peek()
        if self._game_key and self.on_open is not None:
            self.on_open(self._game_key, self._game_name, self._target_fps)
