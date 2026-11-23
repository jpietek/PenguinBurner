from __future__ import annotations

import html

from ui.features.tuning.tuning import GPU_UNDERVOLTING_PURPOSE_TEXT

from ..assets import application_version, asset_image_path
from ..constants import APP_DISPLAY_NAME
from .error_details import qt_flags, selectable_text_flags

SPONSOR_URL = "https://github.com/sponsors/jpietek"
ISSUES_URL = "https://github.com/jpietek/PenguinBurner/issues"
ABOUT_AUTHORS = (
    ("Jan Pietek", "https://github.com/jpietek"),
    ("Michał Ernst (Ernold)", "https://github.com/Ernold11"),
)
ABOUT_LINKS_HTML = (
    "If you like the tool please consider supporting me on Github!<br>"
    f'<a href="{SPONSOR_URL}">{SPONSOR_URL}</a><br><br>'
    "Having issues with PenguinBurner? Please report the bugs here:<br>"
    f'<a href="{ISSUES_URL}">{ISSUES_URL}</a><br><br>Authors<br>'
    + " · ".join(
        f'<a href="{url}">{html.escape(name)}</a>' for name, url in ABOUT_AUTHORS
    )
)


def format_total_runtime(seconds: float) -> str:
    """Linux-uptime style: only the units that carry information."""
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def format_energy_saved(watt_seconds: float) -> str:
    """Wh, then kWh, then (one can dream) MWh.

    Two decimals below 100 Wh so the live counter visibly climbs while a
    game is running instead of sitting on one number for minutes.
    """
    watt_hours = max(0.0, float(watt_seconds)) / 3600.0
    if watt_hours >= 1_000_000:
        return f"{watt_hours / 1_000_000:.2f} MWh"
    if watt_hours >= 1_000:
        return f"{watt_hours / 1_000:.2f} kWh"
    if watt_hours >= 100:
        return f"{watt_hours:.1f} Wh"
    return f"{watt_hours:.2f} Wh"


def energy_savings_lines(status: dict | None = None) -> str:
    """Two display lines from the daemon's persistent energy-saved counter.

    Empty when the daemon is unreachable or nothing has been counted yet —
    the caller then keeps the descriptive About text instead.
    """
    if status is None:
        from runtime.daemon_client import daemon_status

        try:
            status = daemon_status(timeout_s=1.0)
        except Exception:  # noqa: BLE001
            return ""
    savings = status.get("energy_savings") if isinstance(status, dict) else None
    if not isinstance(savings, dict):
        return ""
    try:
        active_seconds = float(savings.get("active_seconds") or 0.0)
        saved_watt_seconds = float(savings.get("saved_watt_seconds") or 0.0)
    except (TypeError, ValueError):
        return ""
    if active_seconds <= 0.0:
        return ""
    return (
        f"Total runtime: {format_total_runtime(active_seconds)}\n"
        f"Energy saved: {format_energy_saved(saved_watt_seconds)}"
    )


def show_about_dialog(
    *,
    QtCore,
    QtGui,
    QtWidgets,
    parent,
    purpose_text: str = GPU_UNDERVOLTING_PURPOSE_TEXT,
) -> None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("About")
    dialog.setMinimumWidth(520)
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.setContentsMargins(24, 24, 24, 18)
    layout.setSpacing(12)

    logo_label = QtWidgets.QLabel()
    logo_label.setAlignment(QtCore.Qt.AlignCenter)
    icon_path = asset_image_path("penguin-burner.png")
    pixmap = QtGui.QPixmap(str(icon_path)) if icon_path is not None else None
    if pixmap is not None and not pixmap.isNull():
        logo_label.setPixmap(
            pixmap.scaled(
                180,
                180,
                _aspect_mode(QtCore),
                _transform_mode(QtCore),
            )
        )
    layout.addWidget(logo_label)

    title = QtWidgets.QLabel(APP_DISPLAY_NAME)
    title.setAlignment(QtCore.Qt.AlignCenter)
    title.setObjectName("aboutTitle")
    title.setTextInteractionFlags(selectable_text_flags(QtCore))
    layout.addWidget(title)

    version_label = QtWidgets.QLabel(f"Version {application_version()}")
    version_label.setAlignment(QtCore.Qt.AlignCenter)
    version_label.setObjectName("aboutVersion")
    version_label.setTextInteractionFlags(selectable_text_flags(QtCore))
    layout.addWidget(version_label)

    # The daemon's lifetime energy-saved counter replaces the descriptive
    # blurb once there is something to show (issue #23).
    savings_text = energy_savings_lines()
    if savings_text:
        savings = QtWidgets.QLabel(savings_text)
        savings.setObjectName("aboutSavings")
        savings.setAlignment(QtCore.Qt.AlignCenter)
        savings.setTextInteractionFlags(selectable_text_flags(QtCore))
        layout.addWidget(savings)

        # Live tick: the daemon mirrors in-memory totals to tmpfs every
        # engine tick, so polling once a second makes the counter climb
        # while a game is running. The timer dies with the dialog.
        def _refresh_savings() -> None:
            text = energy_savings_lines()
            if text and text != savings.text():
                savings.setText(text)

        refresh_timer = QtCore.QTimer(dialog)
        refresh_timer.setInterval(1000)
        refresh_timer.timeout.connect(_refresh_savings)
        refresh_timer.start()
    else:
        purpose = QtWidgets.QLabel(purpose_text)
        purpose.setObjectName("purposeText")
        purpose.setWordWrap(True)
        purpose.setAlignment(QtCore.Qt.AlignCenter)
        purpose.setTextInteractionFlags(selectable_text_flags(QtCore))
        layout.addWidget(purpose)

    body = QtWidgets.QLabel(ABOUT_LINKS_HTML)
    body.setAlignment(QtCore.Qt.AlignCenter)
    body.setWordWrap(True)
    body.setOpenExternalLinks(True)
    # Links, not a text field: no caret is left after a clicked link.
    body.setTextInteractionFlags(
        qt_flags(
            QtCore.Qt,
            "TextInteractionFlag",
            "LinksAccessibleByMouse",
            "LinksAccessibleByKeyboard",
        )
    )
    layout.addWidget(body)

    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)
    ok = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
    if ok is not None:
        ok.setDefault(True)
        ok.setFocus()
    dialog.exec()


def _aspect_mode(QtCore):
    return getattr(QtCore.Qt, "AspectRatioMode", QtCore.Qt).KeepAspectRatio


def _transform_mode(QtCore):
    return getattr(QtCore.Qt, "TransformationMode", QtCore.Qt).SmoothTransformation
