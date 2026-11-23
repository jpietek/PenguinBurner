from __future__ import annotations

AUTO_UV_TIER_ORDER = ("efficiency", "balanced", "performance")

_TIER_LABELS = {
    "efficiency": "Efficiency",
    "balanced": "Balanced",
    "performance": "Performance",
}

_STATE_LABELS = {
    "pending": "Pending",
    "active": "Scanning",
    "complete": "✓ Complete",
    "skipped": "Skipped",
    "stopped": "Stopped",
    "failed": "Failed",
    "not-run": "Not run",
}


class AutoUvTierProgress:
    """Compact live progress for the three-profile Full scan.

    The setup dialog closes before scanning starts, so this strip mirrors the
    same Efficiency -> Balanced -> Performance order in the existing runs area.
    It stays hidden for a one-profile scan. The scan session decides each
    tier's state; this only shows it.
    """

    def __init__(self, *, QtWidgets):
        self.QtWidgets = QtWidgets
        self.widget = QtWidgets.QFrame()
        self.widget.setObjectName("autoUvTierProgress")
        layout = QtWidgets.QHBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(7)

        title = QtWidgets.QLabel("Full scan")
        title.setObjectName("autoUvTierProgressTitle")
        layout.addWidget(title)

        self.steps = {}
        self._states = {}
        for position, tier in enumerate(AUTO_UV_TIER_ORDER, start=1):
            step = QtWidgets.QLabel()
            step.setObjectName("autoUvTierProgressStep")
            step.setProperty("profileTier", tier)
            step.setMinimumHeight(38)
            step.setMinimumWidth(150)
            self.steps[tier] = step
            layout.addWidget(step, 1)
            self._set_state(tier, "pending", position=position)
        self.widget.hide()

    def show_states(self, states: dict[str, str]) -> None:
        """Show the scan session's tier states; an empty map hides the strip."""
        for position, tier in enumerate(AUTO_UV_TIER_ORDER, start=1):
            self._set_state(tier, states.get(tier, "pending"), position=position)
        self.widget.setVisible(bool(states))

    def state(self, tier: str) -> str | None:
        normalized = _normalized_tier(tier)
        return None if normalized is None else self._states.get(normalized)

    def _set_state(self, tier: str, state: str, *, position: int | None = None) -> None:
        step = self.steps[tier]
        self._states[tier] = state
        step.setProperty("progressState", state)
        shown_position = position or AUTO_UV_TIER_ORDER.index(tier) + 1
        step.setText(
            f"{shown_position}. {_TIER_LABELS[tier]}  ·  "
            f"{_STATE_LABELS.get(state, state.title())}"
        )
        step.style().unpolish(step)
        step.style().polish(step)


def _normalized_tier(value: object) -> str | None:
    tier = str(value or "").strip().lower()
    return tier if tier in AUTO_UV_TIER_ORDER else None
