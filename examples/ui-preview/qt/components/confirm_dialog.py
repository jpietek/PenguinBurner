"""A token-styled confirmation: title, body, Cancel and the action.

Replaces ``QMessageBox.question``: no off-palette standard icon, Cancel is
the default (secondary) button, and the action is either the view's one
filled primary or, for a destructive action, a loss-outlined button.
"""

from __future__ import annotations

from PySide6 import QtCore as C
from PySide6 import QtWidgets as W

from ui.design.style import GAP


class ConfirmDialog(W.QDialog):
    """``ConfirmDialog(parent, title, text, accept="Delete", destructive=True)``;
    ``exec()`` returns Accepted only for the action button."""

    def __init__(
        self,
        parent,
        title: str,
        text: str,
        *,
        accept: str = "Yes",
        destructive: bool = False,
    ):
        super().__init__(parent)
        self.setObjectName("confirmDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(GAP["xl"], GAP["lg"], GAP["xl"], GAP["lg"])
        layout.setSpacing(GAP["sm"])
        heading = W.QLabel(title)
        heading.setObjectName("sectionTitle")
        heading.setTextFormat(C.Qt.TextFormat.PlainText)
        layout.addWidget(heading)
        body = W.QLabel(text)
        body.setObjectName("confirmText")
        body.setTextFormat(C.Qt.TextFormat.PlainText)
        body.setWordWrap(True)
        layout.addWidget(body)
        layout.addSpacing(GAP["sm"])
        foot = W.QHBoxLayout()
        foot.setSpacing(GAP["sm"])
        foot.addStretch(1)
        self.cancel_button = W.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.accept_button = W.QPushButton(accept)
        self.accept_button.setObjectName("dangerAction" if destructive else "nextAction")
        self.accept_button.setAutoDefault(False)
        self.accept_button.clicked.connect(self.accept)
        foot.addWidget(self.cancel_button)
        foot.addWidget(self.accept_button)
        layout.addLayout(foot)
        # Enter cancels a destructive action; the action needs a deliberate click.
        self.cancel_button.setDefault(destructive)
        self.accept_button.setDefault(not destructive)
        self.setMinimumWidth(420)
        (self.cancel_button if destructive else self.accept_button).setFocus(
            C.Qt.FocusReason.OtherFocusReason
        )


def confirm(
    parent, title: str, text: str, *, accept: str = "Yes", destructive: bool = False
) -> bool:
    dialog = ConfirmDialog(parent, title, text, accept=accept, destructive=destructive)
    return dialog.exec() == W.QDialog.DialogCode.Accepted
