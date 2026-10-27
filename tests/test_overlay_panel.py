"""The Overlay preference cards preserve settings and edit metrics inline."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from overlay.config import (
    OVERLAY_DETAIL_IDS,
    OVERLAY_DETAIL_PRESETS,
    OverlayConfig,
    load_overlay_config,
    save_overlay_config,
    set_overlay_detail,
    set_overlay_item_enabled,
)
from overlay.metrics import (
    OVERLAY_ITEM_IDS,
    OVERLAY_ITEM_SECTIONS,
)
from overlay.style import (
    OVERLAY_SCALE_OPTIONS,
)


@pytest.fixture
def panel_factory(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtGui, QtWidgets

    from ui.components import overlay_config
    from ui.styles import STYLESHEET

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    monkeypatch.setattr(overlay_config, "read_overlay_state", lambda: {})
    panels = []

    def create(config=None, *, config_path=None):
        path = config_path or tmp_path / f"overlay-{len(panels)}.toml"
        if config is not None:
            save_overlay_config(config, path)
        panel = overlay_config.OverlayConfigPanel(
            QtCore=QtCore, QtWidgets=QtWidgets, config_path=path
        )
        panel.widget.setFont(QtGui.QFont("Noto Sans", 11))
        panel.widget.setStyleSheet(STYLESHEET)
        panel.timer.stop()
        panel.widget.resize(1430, 790)
        panel.widget.show()
        app.processEvents()
        panels.append(panel)
        return panel

    yield create

    for panel in panels:
        panel.timer.stop()
        panel.widget.close()
        panel.widget.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()


def _settle_layout():
    from PySide6 import QtWidgets

    for _ in range(4):
        QtWidgets.QApplication.processEvents()


def test_opening_and_editing_sections_do_not_create_or_write_config(
    panel_factory, monkeypatch
) -> None:
    from PySide6 import QtWidgets

    from ui.components import overlay_config

    def unexpected_save(*_args, **_kwargs):
        pytest.fail("Opening or editing metric choices must not save settings")

    monkeypatch.setattr(overlay_config, "save_overlay_config", unexpected_save)
    panel = panel_factory()
    original = panel.config

    for title, _items in OVERLAY_ITEM_SECTIONS:
        button = panel.section_edit_buttons[title]
        button.click()
        assert button.text() == "Done"
        assert QtWidgets.QApplication.activeModalWidget() is None
        assert not panel.widget.findChildren(QtWidgets.QDialog)
        button.click()
        assert button.text() == "Edit"

    assert panel.config == original
    assert not panel.config_path.exists()


def test_metric_summaries_show_only_selected_rows_and_edit_each_section_inline(
    panel_factory,
) -> None:
    config = set_overlay_detail(OverlayConfig(), "minimal")
    panel = panel_factory(config)
    selected = set(config.enabled_item_ids)
    assert set(panel.item_checkboxes) == set(OVERLAY_ITEM_IDS)

    for item_id, checkbox in panel.item_checkboxes.items():
        assert panel.item_rows[item_id].isHidden() is (item_id not in selected)
        assert checkbox.isHidden()

    for title, items in OVERLAY_ITEM_SECTIONS:
        count = len(selected.intersection(items))
        assert panel.section_count_labels[title].text() == f"{count} selected"
        assert panel.section_empty_labels[title].isHidden() is bool(count)

    panel.section_edit_buttons["GPU"].click()
    gpu_ids = set(dict(OVERLAY_ITEM_SECTIONS)["GPU"])
    for item_id, checkbox in panel.item_checkboxes.items():
        assert checkbox.isHidden() is (item_id not in gpu_ids)
        assert panel.item_rows[item_id].isHidden() is (
            item_id not in selected | gpu_ids
        )
    assert panel.section_empty_labels["GPU"].isHidden()

    panel.section_edit_buttons["Frame rate"].click()
    panel.section_edit_buttons["GPU"].click()
    assert panel.editing_sections == {"Frame rate"}
    for item_id in gpu_ids:
        assert panel.item_checkboxes[item_id].isHidden()
        assert panel.item_rows[item_id].isHidden() is (item_id not in selected)


def test_clicking_full_metric_label_persists_and_done_restores_summary(
    panel_factory,
) -> None:
    from PySide6 import QtCore, QtTest

    original = OverlayConfig(
        enabled=True,
        enabled_item_ids=("base_fps", "clock_mhz"),
        scale=1.5,
        opacity=0.6,
        position="bottom_left",
        layout="table",
        font="mono",
        update_interval_s=7,
    )
    panel = panel_factory(original)
    label = panel.item_title_labels["fan_pct"]
    panel.section_edit_buttons["GPU"].click()
    _settle_layout()
    QtTest.QTest.mouseClick(label, QtCore.Qt.MouseButton.LeftButton)

    expected = set_overlay_item_enabled(original, "fan_pct", True)
    assert load_overlay_config(panel.config_path) == expected
    assert panel.item_checkboxes["fan_pct"].isChecked()
    panel.section_edit_buttons["GPU"].click()
    assert not panel.item_rows["fan_pct"].isHidden()
    assert panel.item_checkboxes["fan_pct"].isHidden()

    # Summary text must not silently toggle a saved selection.
    QtTest.QTest.mouseClick(label, QtCore.Qt.MouseButton.LeftButton)
    assert load_overlay_config(panel.config_path) == expected

    reloaded = panel_factory(config_path=panel.config_path)
    assert reloaded.config == expected
    assert not reloaded.item_rows["fan_pct"].isHidden()
    assert reloaded.item_checkboxes["fan_pct"].isHidden()
    reloaded.section_edit_buttons["GPU"].click()
    reloaded.item_checkboxes["fan_pct"].click()
    assert load_overlay_config(reloaded.config_path) == set_overlay_item_enabled(
        expected, "fan_pct", False
    )
    assert not reloaded.item_rows["fan_pct"].isHidden()
    reloaded.section_edit_buttons["GPU"].click()
    assert reloaded.item_rows["fan_pct"].isHidden()


def test_the_last_metric_cannot_be_deselected_in_the_inline_editor(
    panel_factory,
) -> None:
    panel = panel_factory(OverlayConfig(enabled_item_ids=("base_fps",)))
    panel.section_edit_buttons["Frame rate"].click()
    panel.item_checkboxes["base_fps"].click()

    assert panel.item_checkboxes["base_fps"].isChecked()
    assert load_overlay_config(panel.config_path).enabled_item_ids == ("base_fps",)
    assert panel.section_count_labels["Frame rate"].text() == "1 selected"
    panel.section_edit_buttons["Frame rate"].click()
    assert not panel.item_rows["base_fps"].isHidden()


@pytest.mark.parametrize("detail", tuple(OVERLAY_DETAIL_PRESETS))
def test_presets_keep_the_same_metrics_and_preserve_appearance(
    panel_factory, detail
) -> None:
    original = OverlayConfig(
        enabled=True,
        enabled_item_ids=("base_fps",),
        scale=0.75,
        position="bottom_right",
        opacity=0.65,
        font="mono",
        layout="table",
        update_interval_s=8,
    )
    panel = panel_factory(original)
    panel.detail_combo.setCurrentIndex(OVERLAY_DETAIL_IDS.index(detail))

    expected = set_overlay_detail(original, detail)
    assert load_overlay_config(panel.config_path) == expected
    for item_id, checkbox in panel.item_checkboxes.items():
        assert checkbox.isChecked() is (item_id in expected.enabled_item_ids)
        assert checkbox.isHidden()
        assert panel.item_rows[item_id].isHidden() is (
            item_id not in expected.enabled_item_ids
        )


def test_scale_slider_uses_existing_steps_with_keyboard_and_percent_label(
    panel_factory,
) -> None:
    from PySide6 import QtCore, QtTest

    panel = panel_factory(OverlayConfig(scale=OVERLAY_SCALE_OPTIONS[0]))
    slider = panel.scale_slider
    slider.setFocus()
    assert slider.minimum() == 0
    assert slider.maximum() == len(OVERLAY_SCALE_OPTIONS) - 1

    for index, scale in enumerate(OVERLAY_SCALE_OPTIONS):
        if index:
            QtTest.QTest.keyClick(slider, QtCore.Qt.Key.Key_Right)
        assert slider.value() == index
        assert panel.scale_value_label.text() == f"{round(scale * 100)}%"
        assert load_overlay_config(panel.config_path).scale == scale

    QtTest.QTest.keyClick(slider, QtCore.Qt.Key.Key_Right)
    assert slider.value() == slider.maximum()
    QtTest.QTest.keyClick(slider, QtCore.Qt.Key.Key_Home)
    assert load_overlay_config(panel.config_path).scale == OVERLAY_SCALE_OPTIONS[0]
    QtTest.QTest.keyClick(slider, QtCore.Qt.Key.Key_End)
    assert load_overlay_config(panel.config_path).scale == OVERLAY_SCALE_OPTIONS[-1]


def test_opacity_slider_steps_five_percent_with_keyboard_and_percent_label(
    panel_factory,
) -> None:
    from PySide6 import QtCore, QtTest

    panel = panel_factory(OverlayConfig(opacity=0.85))
    slider = panel.opacity_slider
    slider.setFocus()
    assert (slider.minimum(), slider.maximum()) == (20, 100)
    assert slider.value() == 85
    assert panel.opacity_value_label.text() == "85%"

    QtTest.QTest.keyClick(slider, QtCore.Qt.Key.Key_Left)
    assert slider.value() == 80
    assert panel.opacity_value_label.text() == "80%"
    assert load_overlay_config(panel.config_path).opacity == 0.8

    QtTest.QTest.keyClick(slider, QtCore.Qt.Key.Key_End)
    assert load_overlay_config(panel.config_path).opacity == 1.0
    QtTest.QTest.keyClick(slider, QtCore.Qt.Key.Key_Home)
    assert load_overlay_config(panel.config_path).opacity == 0.2
    assert panel.opacity_value_label.text() == "20%"


def test_external_config_refresh_updates_controls_without_writing_or_closing_edit(
    panel_factory, monkeypatch
) -> None:
    from ui.components import overlay_config

    panel = panel_factory(OverlayConfig())
    panel.section_edit_buttons["GPU"].click()
    external = OverlayConfig(
        enabled=True,
        enabled_item_ids=("base_fps", "fan_pct", "present_mode"),
        scale=2.0,
        position="bottom_left",
        opacity=0.5,
        renderer="clear_rect",
        font="mono",
        layout="table",
        update_interval_s=9,
    )
    save_overlay_config(external, panel.config_path)

    def unexpected_save(*_args, **_kwargs):
        pytest.fail("Reading an external configuration must not rewrite it")

    monkeypatch.setattr(overlay_config, "save_overlay_config", unexpected_save)
    panel.refresh_preview()

    assert panel.config == load_overlay_config(panel.config_path)
    assert panel.enabled_switch.isChecked()
    assert panel.scale_slider.value() == OVERLAY_SCALE_OPTIONS.index(2.0)
    assert panel.scale_value_label.text() == "200%"
    assert panel.update_interval_spin.value() == 9
    assert panel.editing_sections == {"GPU"}
    assert panel.item_checkboxes["fan_pct"].isChecked()
    assert not panel.item_checkboxes["fan_pct"].isHidden()
    assert not panel.item_rows["present_mode"].isHidden()
    assert panel.item_checkboxes["present_mode"].isHidden()


@pytest.mark.parametrize("renderer", ("blended", "clear_rect"))
def test_renderer_has_no_selector_and_changes_to_blended_only_on_explicit_save(
    panel_factory, renderer
) -> None:
    from PySide6 import QtWidgets

    original = OverlayConfig(renderer=renderer)
    panel = panel_factory(original)
    before = panel.config_path.stat()

    assert panel.widget.findChild(
        QtWidgets.QComboBox, "overlayRendererCombo"
    ) is None
    panel.section_edit_buttons["GPU"].click()
    panel.refresh_preview()
    panel.section_edit_buttons["GPU"].click()
    assert load_overlay_config(panel.config_path).renderer == renderer
    assert panel.config_path.stat().st_mtime_ns == before.st_mtime_ns

    panel.opacity_slider.setValue(60)
    assert load_overlay_config(panel.config_path) == replace(
        original, opacity=0.6, renderer="blended",
        custom_item_ids=original.enabled_item_ids
    )


def test_failed_save_restores_saved_controls_and_keeps_error_visible(
    panel_factory, monkeypatch
) -> None:
    from ui.components import overlay_config

    panel = panel_factory(OverlayConfig(scale=1.0))
    original = load_overlay_config(panel.config_path)

    def fail_save(*_args, **_kwargs):
        raise PermissionError("read-only test configuration")

    monkeypatch.setattr(overlay_config, "save_overlay_config", fail_save)
    panel.scale_slider.setValue(OVERLAY_SCALE_OPTIONS.index(2.0))

    assert load_overlay_config(panel.config_path) == original
    assert panel.config == original
    assert panel.scale_slider.value() == OVERLAY_SCALE_OPTIONS.index(original.scale)
    assert panel.scale_value_label.text() == "100%"
    assert "read-only test configuration" in panel.status_label.text()
    assert panel.status_label.isVisibleTo(panel.widget)


@pytest.mark.parametrize(("width", "columns"), ((1430, 3), (1000, 2), (760, 1)))
@pytest.mark.parametrize("editing", (False, True))
def test_cards_reflow_without_clipping_metric_labels_or_controls(
    panel_factory, width, columns, editing
) -> None:
    from PySide6 import QtCore, QtWidgets

    panel = panel_factory(set_overlay_detail(OverlayConfig(), "full"))
    if editing:
        for button in panel.section_edit_buttons.values():
            button.click()
    panel.widget.resize(width, 790)
    _settle_layout()

    assert panel.widget.width() == width
    assert panel._layout_columns == columns
    scroll = panel._options_scroll
    assert scroll.horizontalScrollBar().maximum() == 0
    viewport_width = scroll.viewport().width()
    for item_id, row in panel.item_rows.items():
        assert row.isVisibleTo(panel.widget)
        left = row.mapTo(scroll.viewport(), QtCore.QPoint(0, 0)).x()
        assert left >= 0, item_id
        assert left + row.width() <= viewport_width, item_id
        title = panel.item_title_labels[item_id]
        assert title.wordWrap(), item_id
        assert title.heightForWidth(title.width()) <= title.height(), item_id
        for child in row.findChildren(
            QtWidgets.QWidget, options=QtCore.Qt.FindChildOption.FindDirectChildrenOnly
        ):
            if not child.isHidden():
                assert child.geometry().left() >= 0, (item_id, child.objectName())
                assert child.geometry().right() < row.width(), (
                    item_id,
                    child.objectName(),
                )


def test_full_labels_and_scale_use_the_shared_game_library_preference_cards(
    panel_factory,
) -> None:
    from PySide6 import QtWidgets

    panel = panel_factory()
    headings = {
        label.property("headingText")
        for label in panel.widget.findChildren(QtWidgets.QLabel, "prefGroupHeading")
    }
    assert headings == {"Appearance", "Behaviour"} | {
        title for title, _items in OVERLAY_ITEM_SECTIONS
    }
    assert panel.item_title_labels["fg_fps"].text() == "FPS after frame generation"
    assert panel.item_title_labels["cpu_peak_thread_pct"].text() == "Busiest CPU thread"
    assert panel.item_title_labels["temperature_c"].text() == "GPU temperature"
    assert panel.item_title_labels["uv_offset_mv"].text() == "Undervolt offset"
    for item_id, row in panel.item_rows.items():
        assert row.objectName() == "prefRow"
        assert row.parentWidget().objectName() == "prefGroupCard"
        assert panel.item_checkboxes[item_id].accessibleName() == (
            panel.item_title_labels[item_id].text()
        )
    scale_row = panel.scale_slider.parentWidget()
    while scale_row is not None and scale_row.objectName() != "prefRow":
        scale_row = scale_row.parentWidget()
    assert scale_row is not None
    assert scale_row.property("titleLabel").text() == "Scale"
