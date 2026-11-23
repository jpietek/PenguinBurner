"""Use Adaptive for a game: pick a game and its target FPS.

Profiles opens this from its Adaptive card; the host then applies the
choice in Game Library (wrap the game, mode Adaptive, per-game target).
"""

from __future__ import annotations

from dataclasses import dataclass

from design import GAP
from PySide6 import QtWidgets as W

TITLE = "Use Adaptive for a game"
NOTE = (
    "Choose a game and the target FPS before frame generation. "
    "Adaptive uses the verified tiers on its GPU."
)
MIN_FPS, MAX_FPS = 1, 500


@dataclass(frozen=True)
class AdaptiveGame:
    key: str
    name: str
    target_fps: int


class AdaptiveGamePicker(W.QDialog):
    """``choice()`` is ``(game_key, fps)`` after Apply to game."""

    def __init__(self, games: list[AdaptiveGame], selected_key: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("adaptiveGamePicker")
        self.setWindowTitle(TITLE)
        self.games = list(games)
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(GAP["xl"], GAP["lg"], GAP["xl"], GAP["lg"])
        layout.setSpacing(GAP["sm"])
        title = W.QLabel(TITLE)
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        note = W.QLabel(NOTE)
        note.setObjectName("caption")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addSpacing(GAP["sm"])
        form = W.QFormLayout()
        form.setHorizontalSpacing(GAP["md"])
        form.setVerticalSpacing(GAP["sm"])
        self.game_combo = W.QComboBox()
        self.game_combo.setObjectName("adaptiveGame")
        self.game_combo.setAccessibleName("Game")
        for game in self.games:
            self.game_combo.addItem(game.name, game.key)
        self.fps_spin = W.QSpinBox()
        self.fps_spin.setObjectName("adaptiveFps")
        self.fps_spin.setAccessibleName("Target FPS")
        self.fps_spin.setRange(MIN_FPS, MAX_FPS)
        self.fps_spin.setSuffix(" FPS")
        form.addRow("Game", self.game_combo)
        form.addRow("Target FPS", self.fps_spin)
        layout.addLayout(form)
        self.game_combo.currentIndexChanged.connect(self._game_changed)
        index = max(0, self.game_combo.findData(selected_key))
        self.game_combo.setCurrentIndex(index)
        self._game_changed(index)
        layout.addSpacing(GAP["sm"])
        foot = W.QHBoxLayout()
        foot.addStretch()
        cancel = W.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.apply_button = W.QPushButton("Apply to game")
        self.apply_button.setObjectName("nextAction")
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self.accept)
        foot.addWidget(cancel)
        foot.addWidget(self.apply_button)
        layout.addLayout(foot)
        self.setMinimumWidth(440)

    def _game_changed(self, index: int) -> None:
        if 0 <= index < len(self.games):
            self.fps_spin.setValue(self.games[index].target_fps)

    def choice(self) -> tuple[str, int]:
        return str(self.game_combo.currentData()), int(self.fps_spin.value())
