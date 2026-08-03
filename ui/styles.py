from . import theme


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
QLabel#profilesAdaptiveNote {{
    color: {theme.TEXT_MUTED};
    font-size: 12px;
}}
QLabel#aboutSavings {{
    color: {theme.GOOD};
    font-size: 15px;
    font-weight: 700;
    line-height: 1.4;
}}
QLabel#gpuNvmlInfo {{
    background: {theme.SURFACE_BG};
    border: 1px solid {theme.BORDER};
    border-radius: 5px;
    color: {theme.TEXT_MUTED};
    font-size: 11px;
    padding: 7px 9px;
}}
QGroupBox#autoUvPresetGroup {{
    margin-top: 6px;
}}
QGroupBox#autoUvScanScopeGroup {{
    margin-top: 6px;
}}
QLabel#autoUvScanEstimate {{
    color: {theme.TEXT_MUTED};
    font-size: 11px;
}}
QLabel#autoUvPresetSequence {{
    color: {theme.TEXT_MUTED};
    font-size: 11px;
}}
QFrame#autoUvTierProgress {{
    background: transparent;
    border: 0;
}}
QLabel#autoUvTierProgressTitle {{
    color: {theme.TEXT_MUTED};
    font-size: 11px;
    font-weight: 700;
    padding: 0 4px;
}}
QLabel#autoUvTierProgressStep {{
    background: {theme.CONTROL_BG};
    border: 1px solid {theme.BORDER_STRONG};
    border-radius: 5px;
    color: {theme.TEXT_MUTED};
    font-size: 11px;
    font-weight: 600;
    padding: 5px 8px;
    qproperty-alignment: AlignCenter;
}}
QLabel#autoUvTierProgressStep[profileTier="efficiency"] {{
    border-color: {theme.TIER_CURVE_EFFICIENCY};
}}
QLabel#autoUvTierProgressStep[profileTier="balanced"] {{
    border-color: {theme.TIER_CURVE_BALANCED};
}}
QLabel#autoUvTierProgressStep[profileTier="performance"] {{
    border-color: {theme.TIER_CURVE_PERFORMANCE};
}}
QLabel#autoUvTierProgressStep[profileTier="efficiency"][progressState="active"] {{
    background: {theme.TIER_CURVE_EFFICIENCY};
    color: {theme.TEXT_ON_LIGHT};
    border-width: 2px;
}}
QLabel#autoUvTierProgressStep[profileTier="balanced"][progressState="active"] {{
    background: {theme.TIER_CURVE_BALANCED};
    color: {theme.TEXT_ON_LIGHT};
    border-width: 2px;
}}
QLabel#autoUvTierProgressStep[profileTier="performance"][progressState="active"] {{
    background: {theme.TIER_CURVE_PERFORMANCE};
    color: {theme.TEXT_ON_ERROR};
    border-width: 2px;
}}
QLabel#autoUvTierProgressStep[profileTier="efficiency"][progressState="complete"] {{
    background: {theme.ROW_GOOD_BG};
    color: {theme.TIER_CURVE_EFFICIENCY};
}}
QLabel#autoUvTierProgressStep[profileTier="balanced"][progressState="complete"] {{
    background: {theme.ROW_RUNNING_BG};
    color: {theme.TIER_CURVE_BALANCED};
}}
QLabel#autoUvTierProgressStep[profileTier="performance"][progressState="complete"] {{
    background: {theme.ROW_ERROR_BG};
    color: {theme.ERROR_TEXT};
}}
QLabel#autoUvTierProgressStep[progressState="skipped"],
QLabel#autoUvTierProgressStep[progressState="stopped"],
QLabel#autoUvTierProgressStep[progressState="failed"],
QLabel#autoUvTierProgressStep[progressState="not-run"] {{
    background: {theme.SURFACE_BG};
    color: {theme.TEXT_DISABLED};
    border-color: {theme.BORDER};
}}
QGroupBox#advancedTuningGroup {{
    margin-top: 12px;
}}
QLineEdit, QPlainTextEdit, QTableWidget, QListWidget, QComboBox {{
    background: {theme.SURFACE_BG};
    border: 1px solid {theme.BORDER};
    color: {theme.TEXT};
}}
QFrame#steamLibrarySetupCard {{
    background: {theme.SURFACE_BG};
    border: 1px solid {theme.BORDER_STRONG};
    border-radius: 12px;
}}
QLabel#steamLibrarySetupTitle {{
    background: transparent;
    border: 0;
    color: {theme.TEXT_PROGRESS};
    font-size: 24px;
    font-weight: 700;
}}
QLabel#steamLibrarySetupText {{
    background: transparent;
    border: 0;
    color: {theme.TEXT_MUTED};
    font-size: 13px;
}}
QPushButton#steamLibrarySetupButton {{
    background: {theme.PRIMARY_BUTTON_BG};
    border-color: {theme.PRIMARY_BUTTON_BORDER};
    color: {theme.PRIMARY_BUTTON_TEXT};
    font-size: 15px;
    font-weight: 700;
    padding: 12px 24px;
}}
QPushButton#steamLibrarySetupButton:hover {{
    border-color: {theme.PRIMARY_BUTTON_HOVER_BORDER};
}}
QPushButton#steamLibrarySetupButton:pressed {{
    background: {theme.PRIMARY_BUTTON_PRESSED_BG};
    border-color: {theme.PRIMARY_BUTTON_PRESSED_BORDER};
}}
QFrame#steamLibraryPane, QFrame#steamGameDetailsPane {{
    background: {theme.SURFACE_BG};
    border: 1px solid {theme.BORDER};
    border-radius: 7px;
}}
QLabel#steamPaneTitle {{
    color: {theme.TEXT_STRONG};
    font-size: 14px;
    font-weight: 700;
}}
QLabel#steamGameTitle {{
    color: {theme.TEXT_PROGRESS};
    font-size: 21px;
    font-weight: 700;
}}
QLabel#steamGameMetadata {{
    color: {theme.TEXT_MUTED};
}}
QLabel#steamGameStatus {{
    color: {theme.TEXT_MUTED};
    font-weight: 600;
}}
QLabel#steamGameStatus[gameState="launching"],
QLabel#steamGameStatus[gameState="running"] {{
    color: {theme.GOOD};
}}
QLabel#steamGameStatus[gameState="stopping"] {{
    color: {theme.WARNING};
}}
QLabel#steamFieldLabel {{
    color: {theme.TEXT_STRONG};
    font-weight: 600;
}}
QLabel#steamFieldLabel:disabled {{
    color: {theme.TEXT_DISABLED};
}}
QListWidget#steamGameList {{
    outline: 0;
}}
QListWidget#steamGameList::item {{
    border-radius: 5px;
    padding: 5px 7px;
}}
QListWidget#steamGameList::item:hover {{
    background: {theme.CONTROL_HOVER_BG};
}}
QListWidget#steamGameList::item:selected {{
    background: {theme.PROFILE_SELECTED_BG};
    color: {theme.TEXT_STRONG};
}}
QComboBox#steamGameSort, QComboBox#steamAutoUvMode {{
    padding: 5px 8px;
}}
QComboBox#steamAutoUvMode:disabled,
QComboBox#steamCompatTool:disabled {{
    background: {theme.CONTROL_BG};
    border-color: {theme.BORDER};
    color: {theme.TEXT_DISABLED};
}}
QLineEdit#steamLaunchOptions {{
    font-family: monospace;
    padding: 8px;
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
QPushButton#autoUvScopeButton {{
    min-width: 180px;
    padding: 8px 14px;
}}
QPushButton#autoUvScopeButton:checked {{
    background: {theme.PRIMARY_BUTTON_BG};
    border-color: {theme.PRIMARY_BUTTON_BORDER};
    color: {theme.PRIMARY_BUTTON_TEXT};
}}
QPushButton#steamPlayButton {{
    background: {theme.PRIMARY_BUTTON_BG};
    border-color: {theme.PRIMARY_BUTTON_BORDER};
    color: {theme.PRIMARY_BUTTON_TEXT};
    font-size: 14px;
    font-weight: 700;
}}
QPushButton#steamPlayButton:hover {{
    border-color: {theme.PRIMARY_BUTTON_HOVER_BORDER};
}}
QPushButton#steamPlayButton:pressed {{
    background: {theme.PRIMARY_BUTTON_PRESSED_BG};
    border-color: {theme.PRIMARY_BUTTON_PRESSED_BORDER};
}}
QToolButton#steamGamePerfProfileBadge {{
    background: {theme.PERF_PROFILE_SURFACE};
    border: 1px solid {theme.PERF_PROFILE_BORDER};
    border-radius: 12px;
    padding: 4px;
}}
QToolButton#steamGamePerfProfileBadge:hover {{
    border-color: {theme.PERF_PROFILE_TIER_BALANCED};
}}
QLabel#steamGamePerfProfileCaption {{
    color: {theme.PERF_PROFILE_TEXT_MUTED};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QPushButton#steamPlayButton[playState="running"] {{
    background: {theme.DANGER_BUTTON_BG};
    border-color: {theme.DANGER_BUTTON_BORDER};
    color: {theme.DANGER_BUTTON_TEXT};
}}
QPushButton#steamPlayButton[playState="running"]:hover {{
    border-color: {theme.DANGER_BUTTON_HOVER_BORDER};
}}
QPushButton#steamPlayButton[playState="running"]:pressed {{
    background: {theme.DANGER_BUTTON_PRESSED_BG};
    border-color: {theme.DANGER_BUTTON_PRESSED_BORDER};
}}
QPushButton#steamPlayButton[playState="starting"],
QPushButton#steamPlayButton[playState="stopping"] {{
    background: {theme.CONTROL_BG};
    border-color: {theme.BORDER_STRONG};
    color: {theme.TEXT_MUTED};
}}
QPushButton#autoUvPresetButton {{
    min-width: 108px;
    padding: 7px 12px;
}}
QPushButton#autoUvPresetButton[scanIncluded="true"] {{
    color: {theme.TEXT_ON_LIGHT};
}}
QPushButton#autoUvPresetButton[presetId="efficiency"][scanIncluded="true"] {{
    background: {theme.TIER_CURVE_EFFICIENCY};
    border-color: {theme.GOOD_BRIGHT};
}}
QPushButton#autoUvPresetButton[presetId="efficiency"][scanIncluded="true"]:hover {{
    border-color: {theme.GOOD_TEXT};
}}
QPushButton#autoUvPresetButton[presetId="balanced"][scanIncluded="true"] {{
    background: {theme.TIER_CURVE_BALANCED};
    border-color: #c7eaff;
}}
QPushButton#autoUvPresetButton[presetId="balanced"][scanIncluded="true"]:hover {{
    border-color: #e7f7ff;
}}
QPushButton#autoUvPresetButton[presetId="performance"][scanIncluded="true"] {{
    background: {theme.TIER_CURVE_PERFORMANCE};
    border-color: {theme.ERROR_TEXT};
    color: {theme.TEXT_ON_ERROR};
}}
QPushButton#autoUvPresetButton[presetId="performance"][scanIncluded="true"]:hover {{
    border-color: #fff0f0;
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
/* A minor utility, not a primary action: muted like About, not the green
   Setup-Auto-Undervolt call to action. */
QPushButton#importAfterburnerButton {{
    background: {theme.ABOUT_BUTTON_BG};
    border-color: {theme.ABOUT_BUTTON_BORDER};
    color: {theme.TEXT_MUTED};
}}
QPushButton#importAfterburnerButton:hover {{
    border-color: #7f93ad;
}}
QPushButton#importAfterburnerButton:pressed {{
    background: {theme.ABOUT_BUTTON_PRESSED_BG};
    border-color: #9fb1c7;
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
