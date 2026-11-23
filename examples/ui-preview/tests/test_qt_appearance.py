"""Appearance: theme, accent and font tokens, the dialog and the restyle."""

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import preview
from PySide6 import QtGui as G
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import ui.design.style as design
from ui import theme as shipped
from ui.design import tokens
from ui.design.tokens import (
    AppearanceSettings,
    accent_presets,
    contrast,
    resolve_colors,
)

MODES = ("dark", "light")
ACCENTS = [name for name, _label, _swatch in accent_presets()] + ["#E0A030"]


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    preview.configure_app(instance)
    return instance


@pytest.fixture
def default_appearance(app):
    """Every test leaves the app on the default Appearance."""
    yield app
    design.install_theme(app, AppearanceSettings())


def test_themes_define_the_same_roles():
    dark, light = (set(tokens.SPEC["themes"][mode]) for mode in MODES)
    assert dark == light
    for mode in MODES:
        for accent in ACCENTS:
            colors = resolve_colors(mode, accent)
            assert set(tokens.ACCENT_ROLES) <= set(colors)
            assert all(
                value.startswith("#") and len(value) == 7 for value in colors.values()
            )


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("accent", ACCENTS)
def test_every_theme_and_accent_keeps_its_contrast(mode, accent):
    c = resolve_colors(mode, accent)
    # WCAG 2: body text 7:1 on surfaces, secondary text and text on a filled
    # button or selection 4.5:1, accent and focus rings 3:1 as UI parts.
    minimums = (
        ("ink", "panel", 7),
        ("ink", "window", 7),
        ("secondary", "panel", 4.5),
        ("secondary", "control", 4.5),
        ("onPrimary", "primary", 4.5),
        ("onPrimary", "primaryHover", 4.5),
        ("ink", "selection", 4.5),
        ("accent", "panel", 3),
        ("accent", "control", 3),
        ("focus", "panel", 3),
        ("focus", "control", 3),
        ("loss", "panel", 4.5),
        ("gain", "panel", 4.5),
        ("warning", "panel", 4.5),
    )
    for fg, bg, minimum in minimums:
        assert contrast(c[fg], c[bg]) >= minimum, (mode, accent, fg, bg)
    for tier in ("efficiency", "balanced", "performance"):
        assert contrast(c[tier], c["plotSurface"]) >= 3, (mode, tier)


def test_default_follows_the_desktop_accent_and_falls_back_to_the_shipped_look(
    app, monkeypatch
):
    """The default accent is the desktop's. With none, Dark with the
    PenguinBurner accent is today's app: its neutral surfaces and its green
    filled buttons, borders and selection."""
    default = AppearanceSettings()
    assert (default.theme, default.accent, default.font) == (
        "dark",
        "system",
        "custom",
    )
    # Headless (tests, captures) never asks the desktop.
    assert design.system_accent() is None
    assert design.effective_accent("system") == "penguinburner"
    monkeypatch.setattr(G.QGuiApplication, "platformName", lambda: "wayland")
    monkeypatch.setattr(design, "read_portal_accent", lambda: "#6C53A6")
    assert design.effective_accent("system") == "#6C53A6"
    assert design.effective_accent("blue") == "blue"
    monkeypatch.setattr(design, "read_portal_accent", lambda: None)
    monkeypatch.setitem(design._PLATFORM_ACCENT, "accent", None)
    assert design.effective_accent("system") == "penguinburner"
    c = resolve_colors("dark", "penguinburner")
    for role, value in (
        ("window", shipped.WINDOW_BG),
        ("panel", shipped.SURFACE_BG),
        ("control", shipped.CONTROL_BG),
        ("primary", shipped.PRIMARY_BUTTON_BG),
        ("primaryBorder", shipped.PRIMARY_BUTTON_BORDER),
        ("primaryPressed", shipped.PRIMARY_BUTTON_PRESSED_BG),
        ("accent", shipped.PRIMARY_BUTTON_HOVER_BORDER),
        ("selection", shipped.PROFILE_SELECTED_BG),
    ):
        assert c[role].lower() == value.lower(), role


def test_unknown_settings_fall_back_and_auto_follows_the_desktop():
    odd = AppearanceSettings(
        theme="sepia", accent="chartreuse", font="comic", font_family=" "
    )
    assert odd.normalized() == AppearanceSettings()
    assert AppearanceSettings(accent="#12ab34").normalized().accent == "#12ab34"
    assert tokens.resolve_mode("auto", True) == "light"
    assert tokens.resolve_mode("auto", False) == "dark"
    assert tokens.resolve_mode("auto", None) == "dark"
    assert tokens.resolve_mode("light", False) == "light"


def test_bundled_inter_is_registered(app):
    faces = sorted(design.FONT_DIR.glob("*.ttf"))
    assert [path.name for path in faces] == [
        "Inter-Bold.ttf",
        "Inter-Regular.ttf",
        "Inter-SemiBold.ttf",
    ]
    assert (design.FONT_DIR / "OFL.txt").is_file()
    for path in faces:
        font_id = G.QFontDatabase.addApplicationFont(str(path))
        # SemiBold also registers its legacy "Inter SemiBold" name.
        assert "Inter" in G.QFontDatabase.applicationFontFamilies(font_id)


def test_install_theme_switches_roles_font_and_style_sheet(default_appearance):
    app = default_appearance
    design.install_theme(app, AppearanceSettings(theme="light", accent="blue"))
    light_blue = resolve_colors("light", "blue")
    assert design.MODE["current"] == "light"
    assert light_blue == design.COL
    assert design.TIER_COLORS["Balanced"] == light_blue["balanced"]
    assert light_blue["window"] == shipped.WINDOW_BG
    assert light_blue["accent"] in app.styleSheet()
    assert (
        app.palette().color(G.QPalette.ColorRole.Window).name().upper()
        == (light_blue["window"])
    )
    design.install_theme(app, AppearanceSettings(font="system"))
    assert design.TYPE["family"] == design.system_font_family()
    assert app.font().family() == design.system_font_family()
    design.install_theme(app, AppearanceSettings())
    assert design.TYPE["family"] == "Inter"
    assert resolve_colors("dark", "penguinburner") == design.COL
    # Production rules formatted from the shipped hexes still map after a
    # second install, not only the first.
    assert resolve_colors("dark", "penguinburner")["primary"] in app.styleSheet()


def test_dialog_applies_theme_and_rebuilds_on_the_same_tab(default_appearance):
    window = preview.Preview()
    preview.CURRENT["window"] = window
    window.tabs.setCurrentIndex(2)
    window.show_appearance()
    dialog = window.appearance_dialog
    assert dialog.theme.value() == "dark"
    assert dialog.accent.value() == "system"
    assert dialog.accent.system_button.isChecked()
    assert dialog.family.isEnabled()
    dialog.theme.buttons["light"].click()
    dialog.accent.swatches["violet"].click()
    dialog.font_mode.buttons["system"].click()
    assert not dialog.family.isEnabled()
    assert dialog.sample.colors == resolve_colors("light", "violet")
    assert dialog.sample.family == design.system_font_family()
    # Nothing changes until Apply.
    assert design.MODE["current"] == "dark"
    dialog.apply_button.click()
    QTest.qWait(20)
    rebuilt = preview.CURRENT["window"]
    assert rebuilt is not window
    assert rebuilt.tabs.currentIndex() == 2
    assert (
        AppearanceSettings(theme="light", accent="violet", font="system")
        == design.APPEARANCE
    )
    assert design.COL["accent"] == resolve_colors("light", "violet")["accent"]
    rebuilt.close()


def test_restore_defaults_and_cancel_leave_the_app_alone(default_appearance):
    design.install_theme(
        default_appearance, AppearanceSettings(theme="light", accent="#E0A030")
    )
    window = preview.Preview()
    window.show_appearance()
    dialog = window.appearance_dialog
    assert dialog.accent.custom_swatch.isChecked()
    dialog.restore_defaults()
    assert dialog.settings == AppearanceSettings()
    assert dialog.theme.value() == "dark"
    assert dialog.accent.system_button.isChecked()
    dialog.reject()
    assert design.APPEARANCE.accent == "#E0A030"
    window.close()


def test_desktop_scheme_change_rebuilds_only_on_auto(default_appearance, monkeypatch):
    window = preview.Preview()
    preview.CURRENT["window"] = window
    monkeypatch.setattr(design, "system_prefers_light", lambda: True)
    preview.follow_desktop_scheme()
    assert preview.CURRENT["window"] is window
    design.install_theme(default_appearance, AppearanceSettings(theme="auto"))
    assert design.MODE["current"] == "light"
    monkeypatch.setattr(design, "system_prefers_light", lambda: False)
    preview.follow_desktop_scheme()
    assert preview.CURRENT["window"] is not window
    assert design.MODE["current"] == "dark"
    preview.CURRENT["window"].close()


def test_portal_accent_reply_parsing():
    parse = tokens.parse_portal_accent
    assert parse(
        "(<(0.42352941632270813, 0.3254902064800262, 0.6509804129600525)>,)"
    ) == ("#6C53A6")
    # Out of range means the desktop sets no accent.
    assert parse("(<(-1.0, -1.0, -1.0)>,)") is None
    assert parse("Error: GDBus.Error:org.freedesktop.portal.Error.NotFound") is None
    assert parse("") is None
