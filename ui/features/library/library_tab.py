"""The Game Library tab: the library, and a game's Perf Profile drill-down.

The Perf Profile opens in place of the list, from the entry beside Play;
Back to Game Library returns to the same selection, filters and keyboard focus.
Both views read the daemon's frame-history rings through one
``RuntimePerfSource``.
"""

from __future__ import annotations

from PySide6 import QtCore as C
from PySide6 import QtWidgets as W

from ui.features.library.library_page import LibraryPage
from ui.features.library.perf_profile_page import PerfProfilePage
from ui.features.library.perf_source import RuntimePerfSource


class GameLibraryTab(W.QStackedWidget):
    """``library`` is the ``LibraryPage`` (its signals are the tab's
    cross-page hooks); ``perf_page`` is the drill-down."""

    def __init__(self, parent=None, *, sources=None, gpu_choices=None, perf_env=None):
        super().__init__(parent)
        self.setObjectName("gameLibraryTab")
        self.perf_source = RuntimePerfSource(self._game, env=perf_env)
        self.library = LibraryPage(self.perf_source, sources=sources, gpu_choices=gpu_choices)
        self.perf_page = PerfProfilePage(
            self.perf_source,
            on_back=self.back_to_library,
            on_enable_capture=self.library.enable_capture,
        )
        self.addWidget(self.library)
        self.addWidget(self.perf_page)
        self.library.open_perf_profile.connect(self.open_perf_profile)

    def _game(self, game_key: str):
        # Asked only once games are listed, but the library is built after
        # the source it reads through.
        library = getattr(self, "library", None)
        return library.game(game_key) if library is not None else None

    def open_perf_profile(self, game_key: str) -> None:
        # The game first, so showing the page renders it once.
        self.perf_page.show_game(game_key)
        self.setCurrentWidget(self.perf_page)
        self.perf_page.back_button.setFocus(C.Qt.FocusReason.OtherFocusReason)

    def back_to_library(self) -> None:
        self.setCurrentWidget(self.library)
        self.library.return_from_perf_profile()

    def refresh(self) -> None:
        """The root service changed: re-read the library's live state."""
        self.library.refresh()
