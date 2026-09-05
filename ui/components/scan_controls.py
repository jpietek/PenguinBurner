from __future__ import annotations

import importlib


def _elided_label_class(QtWidgets, QtCore):
    """A one-line label that shrinks instead of pinning the window open.

    A plain QLabel reports its whole string as its minimum width, so a status
    line of ~170 characters puts a floor of about a thousand pixels under the
    window -- more at a larger font -- and a smaller window snaps back out to
    fit it. This one reports nothing and elides whatever it is given to
    whatever width it ends up with; the full text lives in the tooltip.
    """

    class ElidedLabel(QtWidgets.QLabel):
        def __init__(self, text: str = ""):
            super().__init__()
            self._full_text = ""
            self.setSizePolicy(
                QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred
            )
            self.setFullText(text)

        def setFullText(self, text: str) -> None:
            self._full_text = str(text)
            self.setToolTip(self._full_text)
            self._render()

        def fullText(self) -> str:
            return self._full_text

        def minimumSizeHint(self):
            # QLabel derives this from its text even under an Ignored policy,
            # which is exactly the floor this class exists to remove.
            hint = super().minimumSizeHint()
            hint.setWidth(0)
            return hint

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self._render()

        def showEvent(self, event):
            # Laid out but never resized afterwards, the label would keep the
            # text it was given at width zero -- the whole string.
            super().showEvent(event)
            self._render()

        def _render(self) -> None:
            metrics = self.fontMetrics()
            width = max(0, self.width())
            if width <= 0:
                super().setText(self._full_text)
                return
            super().setText(
                metrics.elidedText(self._full_text, QtCore.Qt.ElideRight, width)
            )

    return ElidedLabel


class ScanControls:
    def __init__(self, *, QtWidgets, QtCore=None):
        # Derived rather than required, so the one caller that only ever had
        # QtWidgets keeps working.
        if QtCore is None:
            QtCore = importlib.import_module(
                QtWidgets.__name__.replace("QtWidgets", "QtCore")
            )
        self.widget = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        self.start_button = QtWidgets.QPushButton("Setup Auto Undervolt")
        self.start_button.setObjectName("startAutoUvButton")
        self.import_afterburner_button = QtWidgets.QPushButton("Import Afterburner")
        self.import_afterburner_button.setObjectName("importAfterburnerButton")
        self.import_afterburner_button.setIcon(
            self.widget.style().standardIcon(
                getattr(QtWidgets.QStyle, "StandardPixmap", QtWidgets.QStyle).SP_DialogOpenButton
            )
        )
        self.about_button = QtWidgets.QPushButton("About")
        self.about_button.setObjectName("aboutButton")
        self.about_button.setIcon(
            self.widget.style().standardIcon(
                getattr(QtWidgets.QStyle, "StandardPixmap", QtWidgets.QStyle).SP_MessageBoxInformation
            )
        )
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setEnabled(False)
        # Two lines, each on one subject: the first says what is running, the
        # second what stands behind it and what boot will do. As one
        # semicolon-joined run the headline sat in the middle and was the first
        # thing elision took away.
        elided_label = _elided_label_class(QtWidgets, QtCore)
        self.status_label = elided_label(
            "Auto-UV profiles are stored automatically in the main profile store."
        )
        self.status_label.setObjectName("statusLabel")
        self.status_detail_label = elided_label("")
        self.status_detail_label.setObjectName("statusDetailLabel")
        # Hidden while empty so a one-line message keeps the bar's height.
        self.status_detail_label.hide()
        status_box = QtWidgets.QWidget()
        status_layout = QtWidgets.QVBoxLayout(status_box)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(2)
        status_layout.addWidget(self.status_label)
        status_layout.addWidget(self.status_detail_label)
        self.dependency_progress = QtWidgets.QProgressBar()
        self.dependency_progress.setObjectName("dependencyProgress")
        self.dependency_progress.setRange(0, 100)
        self.dependency_progress.setValue(0)
        self.dependency_progress.setTextVisible(True)
        self.dependency_progress.setFormat("Downloading dependencies 0%")
        self.dependency_progress.setFixedHeight(20)
        self.dependency_progress.setMinimumWidth(260)
        self.dependency_progress.hide()
        layout.addWidget(status_box, 1)
        layout.addWidget(self.dependency_progress)
        layout.addWidget(self.start_button)
        layout.addWidget(self.stop_button)
        layout.addWidget(self.import_afterburner_button)
        layout.addWidget(self.about_button)

    def set_status_text(self, text: str, detail: str = "") -> None:
        """Set the status line, optionally with a muted second line.

        Every one-off message (an error, a step of a scan) passes text alone
        and so clears any detail left by the running-profile line, which is the
        only caller that has a second line to show.
        """
        self.status_label.setFullText(str(text))
        detail_text = str(detail or "")
        self.status_detail_label.setFullText(detail_text)
        self.status_detail_label.setVisible(bool(detail_text))
        # Both carry the whole status, so hovering anywhere over the line shows
        # what elision took away.
        full = f"{text} {detail_text}".strip() if detail_text else str(text)
        self.status_label.setToolTip(full)
        self.status_detail_label.setToolTip(full)

    def set_dependency_progress(self, percent, *, detail: str = "") -> None:
        self.set_progress(
            "Downloading dependencies",
            percent,
            detail=detail or "Downloading dependencies",
        )

    def set_verify_progress(
        self,
        percent,
        *,
        elapsed_s=None,
        target_s=None,
        detail: str = "",
    ) -> None:
        text = None
        if elapsed_s is not None and target_s is not None:
            shown_elapsed_s = _clamped_elapsed_s(elapsed_s, target_s)
            text = (
                "Verifying profile "
                f"{_format_duration_compact(shown_elapsed_s)} / "
                f"{_format_duration_compact(target_s)}"
            )
        self.set_progress(
            "Verifying profile",
            percent,
            detail=detail or "Verifying profile",
            text=text,
        )

    def set_progress(
        self,
        label: str,
        percent,
        *,
        detail: str = "",
        text: str | None = None,
    ) -> None:
        try:
            value = int(round(float(percent)))
        except (TypeError, ValueError):
            value = 0
        value = max(0, min(100, value))
        self.dependency_progress.setValue(value)
        self.dependency_progress.setFormat(text or f"{label} {value}%")
        self.dependency_progress.setToolTip(str(detail or label))
        self.dependency_progress.show()

    def hide_dependency_progress(self) -> None:
        self.dependency_progress.hide()
        self.dependency_progress.setValue(0)
        self.dependency_progress.setFormat("Downloading dependencies 0%")

    def set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.import_afterburner_button.setEnabled(not running)
        self.stop_button.setEnabled(bool(running))


def _format_duration_compact(seconds) -> str:
    try:
        total_s = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total_s = 0
    if total_s < 60:
        return f"{total_s}s"
    minutes = total_s // 60
    remaining_s = total_s % 60
    if minutes < 60:
        if remaining_s:
            return f"{minutes}min {remaining_s}s"
        return f"{minutes}min"
    hours = minutes // 60
    remaining_min = minutes % 60
    if remaining_min:
        return f"{hours}h {remaining_min}min"
    return f"{hours}h"


def _clamped_elapsed_s(elapsed_s, target_s) -> float:
    try:
        elapsed = max(0.0, float(elapsed_s))
        target = max(0.0, float(target_s))
    except (TypeError, ValueError):
        return 0.0
    if target > 0.0:
        return min(elapsed, target)
    return elapsed
