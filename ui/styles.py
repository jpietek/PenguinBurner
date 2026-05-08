from . import theme
from .tuning import PERFORMANCE_BIAS_DANGER_PCT
from .tuning import PERFORMANCE_BIAS_WARNING_PCT
from .tuning import performance_bias_slider_position


STYLESHEET = f"""
QMainWindow {{
    background: {theme.WINDOW_BG};
    color: {theme.TEXT};
    font-size: 12px;
}}
QLabel#stageLabel {{
    color: {theme.STAGE};
    font-size: 22px;
    font-weight: 700;
}}
QLabel#candidateLabel {{
    color: {theme.CANDIDATE};
    font-size: 13px;
    font-weight: 400;
}}
QLabel#aboutTitle {{
    color: {theme.TEXT_PROGRESS};
    font-size: 20px;
    font-weight: 700;
}}
QLabel#aboutVersion {{
    color: #aab3c1;
    font-size: 12px;
}}
QLabel#purposeText {{
    color: {theme.TEXT};
    font-size: 12px;
    font-weight: 400;
    line-height: 1.25;
}}
QLabel#autoVoltageDropNote {{
    color: {theme.TEXT_MUTED};
    font-size: 11px;
}}
QGroupBox#performanceBiasGroup {{
    margin-top: 12px;
}}
QGroupBox#advancedTuningGroup {{
    margin-top: 12px;
}}
QSlider#performanceBiasSlider {{
    margin: 8px 0 10px 0;
}}
QLineEdit, QPlainTextEdit, QTableWidget {{
    background: {theme.SURFACE_BG};
    border: 1px solid {theme.BORDER};
    color: {theme.TEXT};
}}
QGroupBox {{
    border: 1px solid {theme.BORDER};
    border-radius: 6px;
    margin-top: 10px;
}}
QGroupBox::title {{
    color: {theme.TEXT_MUTED};
    left: 10px;
    padding: 0 4px;
}}
QPushButton, QToolButton {{
    background: {theme.CONTROL_BG};
    border: 1px solid {theme.BORDER_STRONG};
    border-radius: 5px;
    color: {theme.TEXT_STRONG};
    padding: 5px 10px;
}}
QPushButton:hover, QToolButton:hover {{
    background: {theme.CONTROL_HOVER_BG};
}}
QPushButton:disabled, QToolButton:disabled {{
    color: {theme.TEXT_DISABLED};
}}
QPushButton#startAutoUvButton {{
    background: {theme.AUTO_UV_BUTTON_BG};
    border-color: {theme.AUTO_UV_BUTTON_BORDER};
    color: {theme.AUTO_UV_BUTTON_TEXT};
}}
QPushButton#startAutoUvButton:hover {{
    border-color: {theme.AUTO_UV_BUTTON_HOVER_BORDER};
}}
QPushButton#startAutoUvButton:pressed {{
    background: {theme.AUTO_UV_BUTTON_PRESSED_BG};
    border-color: {theme.AUTO_UV_BUTTON_PRESSED_BORDER};
}}
QPushButton#startAutoUvButton:disabled {{
    background: {theme.CONTROL_BG};
    border-color: {theme.BORDER_STRONG};
    color: {theme.TEXT_DISABLED};
}}
QPushButton#stopButton {{
    background: {theme.DANGER_BUTTON_BG};
    border-color: {theme.DANGER_BUTTON_BORDER};
    color: {theme.DANGER_BUTTON_TEXT};
}}
QPushButton#stopButton:hover {{
    border-color: {theme.DANGER_BUTTON_HOVER_BORDER};
}}
QPushButton#stopButton:pressed {{
    background: {theme.DANGER_BUTTON_PRESSED_BG};
    border-color: {theme.DANGER_BUTTON_PRESSED_BORDER};
}}
QPushButton#importAfterburnerButton {{
    background: {theme.PRIMARY_BUTTON_BG};
    border-color: {theme.PRIMARY_BUTTON_BORDER};
    color: {theme.PRIMARY_BUTTON_TEXT};
}}
QPushButton#importAfterburnerButton:hover {{
    border-color: {theme.PRIMARY_BUTTON_HOVER_BORDER};
}}
QPushButton#importAfterburnerButton:pressed {{
    background: {theme.PRIMARY_BUTTON_PRESSED_BG};
    border-color: {theme.PRIMARY_BUTTON_PRESSED_BORDER};
}}
QPushButton#aboutButton {{
    background: {theme.ABOUT_BUTTON_BG};
    border-color: {theme.ABOUT_BUTTON_BORDER};
    color: {theme.TEXT_PROGRESS};
}}
QPushButton#aboutButton:hover {{
    border-color: #7f93ad;
}}
QPushButton#aboutButton:pressed {{
    background: {theme.ABOUT_BUTTON_PRESSED_BG};
    border-color: #9fb1c7;
}}
QPushButton#startAutoUvButton:disabled,
QPushButton#stopButton:disabled,
QPushButton#importAfterburnerButton:disabled {{
    background: {theme.CONTROL_BG};
    border-color: {theme.BORDER_STRONG};
    color: {theme.TEXT_DISABLED};
}}
QToolButton#deleteProfilesButton {{
    background: {theme.DANGER_BUTTON_PRESSED_BG};
    border-color: {theme.DANGER_BUTTON_BORDER};
    color: {theme.DANGER_BUTTON_TEXT};
}}
QToolButton#deleteProfilesButton:hover {{
    background: #963030;
    border-color: {theme.DANGER_BUTTON_HOVER_BORDER};
}}
QToolButton#deleteProfilesButton:pressed {{
    background: #631b1b;
    border-color: {theme.DANGER_BUTTON_PRESSED_BORDER};
}}
QToolButton#infoButton {{
    background: #252a31;
    border: 1px solid #5b6675;
    border-radius: 9px;
    color: {theme.CANDIDATE};
    font-size: 11px;
    font-weight: 800;
    padding: 0;
}}
QToolButton#infoButton:hover {{
    background: #2f3844;
    border-color: {theme.CANDIDATE};
    color: #fff2c7;
}}
QProgressBar#dependencyProgress {{
    border: 1px solid {theme.BORDER};
    border-radius: 4px;
    color: {theme.TEXT_PROGRESS};
    text-align: center;
}}
QProgressBar#dependencyProgress::chunk {{
    background: {theme.PRIMARY_BUTTON_BORDER};
}}
QTabWidget::pane {{
    border: 1px solid {theme.BORDER};
}}
QTabBar::tab {{
    background: {theme.SURFACE_ALT_BG};
    color: {theme.TEXT_MUTED};
    padding: 7px 12px;
}}
QTabBar::tab:selected {{
    background: {theme.CONTROL_BG};
    color: {theme.TEXT_STRONG};
}}
"""


def performance_bias_slider_stylesheet(max_pct: float) -> str:
    warning_stop = performance_bias_slider_position(
        PERFORMANCE_BIAS_WARNING_PCT,
        max_pct=float(max_pct),
    ) / 100.0
    danger_stop = performance_bias_slider_position(
        PERFORMANCE_BIAS_DANGER_PCT,
        max_pct=float(max_pct),
    ) / 100.0
    return f"""
    QSlider#performanceBiasSlider {{
        margin: 8px 0 10px 0;
    }}
    QSlider#performanceBiasSlider::groove:horizontal {{
        height: 7px;
        border-radius: 3px;
        background: qlineargradient(
            x1: 0, y1: 0, x2: 1, y2: 0,
            stop: 0 {theme.GOOD},
            stop: {warning_stop:.2f} {theme.GOOD},
            stop: {warning_stop:.2f} {theme.WARNING},
            stop: {danger_stop:.2f} {theme.WARNING},
            stop: {danger_stop:.2f} {theme.ERROR},
            stop: 1.00 {theme.ERROR}
        );
    }}
    QSlider#performanceBiasSlider::handle:horizontal {{
        background: {theme.TEXT_PROGRESS};
        border: 1px solid {theme.TEXT_ON_LIGHT};
        width: 18px;
        margin: -7px 0;
        border-radius: 9px;
    }}
    """


def curve_editor_legend_stylesheet(object_prefix: str) -> str:
    return f"""
    QFrame#{object_prefix}Legend {{
        background: rgba(12, 17, 23, 184);
        border: 1px solid rgba(140, 156, 172, 92);
        border-radius: 7px;
    }}
    QLabel#{object_prefix}Title {{
        color: {theme.TEXT_STRONG};
        font-size: 10px;
        font-weight: 700;
    }}
    QLabel#{object_prefix}Key {{
        color: {theme.GOOD_TEXT};
        background: rgba(94, 243, 140, 36);
        border: 1px solid rgba(94, 243, 140, 72);
        border-radius: 4px;
        padding: 1px 4px;
        font-size: 9px;
        font-weight: 700;
    }}
    QLabel#{object_prefix}Text {{
        color: {theme.TEXT_MUTED};
        font-size: 9px;
    }}
    QToolButton#{object_prefix}Toggle {{
        color: {theme.TEXT_STRONG};
        background: rgba(255, 255, 255, 18);
        border: 1px solid rgba(255, 255, 255, 34);
        border-radius: 4px;
        padding: 0px 4px;
        font-size: 10px;
        font-weight: 700;
    }}
    QToolButton#{object_prefix}Toggle:hover {{
        background: rgba(94, 243, 140, 38);
    }}
    """
