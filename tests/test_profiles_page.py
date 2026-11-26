"""Native Profiles page: sorting, selection, action rules, delete safety,
Apply / Apply on startup, multi-GPU targeting and the dialog hooks."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore as C
from PySide6 import QtWidgets as W
from PySide6.QtTest import QTest
from ui_samples.profiles_source import SAMPLE_GPUS, SampleProfilesSource

from curve_editors.fan.fan_curve_manual_editor import ManualFanCurveEdit
from curve_editors.uv.vf_curve_manual_editor import ManualCurveEdit
from ui.design.style import install_theme
from ui.features.profiles import profile_model as model
from ui.features.profiles.profiles_page import (
    CurveEditors,
    ProfileDialogs,
    ProfilesPage,
)

EFF = "20260713-222800-efficiency"
BAL = "20260713-223600-balanced"
PERF = "20260714-000000-performance"
GPU1 = "20260702-191200-balanced-4070"
LEGACY = "20260602-210400-legacy"


@pytest.fixture(scope="module")
def app():
    instance = W.QApplication.instance() or W.QApplication([])
    install_theme(instance)
    return instance


class ScriptedDialogs(ProfileDialogs):
    """Answers every dialog from a script and records what was asked."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.confirm_answer = True
        self.minutes: int | None = 5
        self.directory: str | None = "/etc/lact"
        self.memory_value: int | None = None

    def confirm(self, parent, title, text, *, accept="Yes"):
        self.calls.append(("confirm", title, text))
        return self.confirm_answer

    def info(self, parent, title, text):
        self.calls.append(("info", title, text))

    def error(self, parent, title, text):
        self.calls.append(("error", title, text))

    def failure(self, parent, title, message, details):
        self.calls.append(("failure", title, message, details))

    def verify_minutes(self, parent, label):
        self.calls.append(("verify", label))
        return self.minutes

    def memory_offset(self, parent, *, current, min_mhz, max_mhz, save):
        self.calls.append(("memory", current, min_mhz, max_mhz))
        if self.memory_value is not None:
            save(self.memory_value)
        return self.memory_value is not None

    def lact_directory(self, parent, suggestions):
        self.calls.append(("lact_dir", tuple(suggestions)))
        return self.directory

    def lact_result(self, parent, result, message):
        self.calls.append(("lact_result", result.path, message))

    def titles(self, kind):
        return [call[1] for call in self.calls if call[0] == kind]

    def last(self, kind):
        return next(call for call in reversed(self.calls) if call[0] == kind)


class StubEditors(CurveEditors):
    def __init__(self):
        self.opened: list[tuple[str, dict]] = []
        self.edit = None

    def open_vf(self, parent, **kwargs):
        self.opened.append(("vf", kwargs))
        if self.edit is not None:
            kwargs["save"](self.edit)

    def open_fan(self, parent, **kwargs):
        self.opened.append(("fan", kwargs))
        if self.edit is not None:
            kwargs["save"](self.edit)


@pytest.fixture
def page(app):
    return make_page(app)


class RigPage(ProfilesPage):
    """The page with its scripted collaborators typed for the tests."""

    source: SampleProfilesSource
    dialogs: ScriptedDialogs
    editors: StubEditors

    def __init__(self, source, *, daemon: bool):
        super().__init__(
            source,
            dialogs=ScriptedDialogs(),
            editors=StubEditors(),
            ensure_daemon_ready=self._ready,
        )
        self.daemon = daemon
        self.asked_daemon: list[str] = []
        self.statuses: list[dict] = []
        self.status_changed.connect(self.statuses.append)

    def _ready(self, label: str) -> bool:
        self.asked_daemon.append(label)
        return self.daemon


def make_page(app, gpus=1, daemon=True) -> RigPage:
    page = RigPage(
        SampleProfilesSource(gpu_count=gpus, command_delay_ms=0), daemon=daemon
    )
    page.resize(1300, 900)
    return page


def fps_order(page):
    return [page.profile(i).get("avg_fps") for i in page.table.row_ids()]


# ---------------------------------------------------------------- table


def test_default_order_is_newest_first_with_real_columns(page):
    assert page.table.row_ids() == [PERF, BAL, EFF, LEGACY]
    header = page.table.horizontalHeader()
    assert [page.table.horizontalHeaderItem(i).text() for i in range(12)] == list(
        model.COLUMNS
    )
    assert (header.sort_column, header.descending) == (model.DATE, True)
    assert page.selected_ids() == [PERF]
    assert not page.target_bar.isVisibleTo(page)


def test_header_sorting_cycles_and_missing_values_sort_last(page):
    edited = page.source.save_memory_offset(EFF, 1000)
    page.reload()
    assert page.profile(edited).get("avg_fps") is None
    page.sort_by(model.FPS)
    order = fps_order(page)
    assert order[:-1] == sorted(order[:-1]) and order[-1] is None
    page.sort_by(model.FPS)
    order = fps_order(page)
    assert order[:-1] == sorted(order[:-1], reverse=True) and order[-1] is None
    # Date starts newest first; a non-sortable column keeps the order.
    page.sort_by(model.DATE)
    assert page.table.row_ids()[0] == edited
    page.sort_by(model.MEM)
    assert (page.sort_column, page.descending) == (model.DATE, True)
    assert {0, 2, 3, 4, 5, 6, 7, 8} == model.SORTABLE_COLUMNS


def test_header_click_sorts(page, app):
    page.show()
    header = page.table.horizontalHeader()
    x = header.sectionViewportPosition(model.MV) + header.sectionSize(model.MV) // 2
    QTest.mouseClick(
        header.viewport(), C.Qt.MouseButton.LeftButton, pos=C.QPoint(x, 10)
    )
    assert (page.sort_column, page.descending) == (model.MV, False)
    mvs = [page.profile(i)["candidate_voltage_mv"] for i in page.table.row_ids()]
    assert mvs == sorted(mvs)


def test_sort_and_selection_survive_reload(page):
    page.sort_by(model.POWER)
    page.select_profiles([BAL, EFF])
    page.source.changed()
    assert page.sort_column == model.POWER
    assert set(page.selected_ids()) == {BAL, EFF}
    assert set(page.table.selected_ids()) == {BAL, EFF}


def click_row(page, profile_id, modifier=C.Qt.KeyboardModifier.NoModifier, button=None):
    table = page.table
    rect = table.visualRect(table.model().index(table.row_of(profile_id), model.DATE))
    QTest.mouseClick(
        table.viewport(), button or C.Qt.MouseButton.LeftButton, modifier, rect.center()
    )


def test_ctrl_and_shift_click_select_like_a_desktop_table(page, app):
    page.show()
    click_row(page, PERF)
    click_row(page, EFF, C.Qt.KeyboardModifier.ControlModifier)
    assert set(page.selected_ids()) == {PERF, EFF}
    assert page.detail.state.mode == "multi"
    assert page.count_label.text() == "4 profiles · 2 selected"
    click_row(page, BAL)
    click_row(page, LEGACY, C.Qt.KeyboardModifier.ShiftModifier)
    assert set(page.selected_ids()) == {BAL, EFF, LEGACY}
    # The multi-row menu offers only deleting the selection.
    assert page.menu_state(page.selected_ids()) == {"delete": True}
    assert page.delete_button.isEnabled()


def test_right_click_inside_selection_keeps_it(page, app):
    page.show()
    page.select_profiles([PERF, BAL])
    table = page.table
    rect = table.visualRect(table.model().index(table.row_of(BAL), model.DATE))
    QTest.mousePress(table.viewport(), C.Qt.MouseButton.RightButton, pos=rect.center())
    assert set(table.selected_ids()) == {PERF, BAL}


def test_delete_key_asks_before_deleting(page, app):
    page.show()
    page.table.setFocus()
    page.dialogs.confirm_answer = False
    QTest.keyClick(page.table, C.Qt.Key.Key_Delete)
    assert page.dialogs.titles("confirm") == ["Delete profiles"]
    assert PERF in page.source.payloads


def test_cells_carry_metric_context(page):
    table = page.table
    assert table.cell_text(BAL, model.MEM) == "+1500 MHz"
    assert table.cell_tooltip(BAL, model.MEM) == "= +3000 MT/s transfer rate"
    assert table.cell_text(BAL, model.POWER) == "265.24 W"
    tip = table.cell_tooltip(BAL, model.POWER)
    assert "factory/default power limit (360.00 W)" in tip
    assert table.item(table.row_of(BAL), model.POWER).data(
        C.Qt.ItemDataRole.UserRole + 2
    )[1]
    assert "unavailable" in table.cell_tooltip(LEGACY, model.POWER)
    assert "vs this scan's baseline" in table.cell_tooltip(BAL, model.MV)
    assert table.cell_text(LEGACY, model.GPU) == "Unassigned (legacy)"
    assert table.cell_text(BAL, model.GPU) == "RTX 5080 (01:00.0)"
    assert table.cell_tooltip(BAL, model.GPU) == "NVIDIA GeForce RTX 5080 (01:00.0)"


def test_tier_label_sits_only_on_the_adaptive_winner(page):
    table = page.table
    assert table.cell_text(BAL, model.TIER) == "Balanced"
    assert table.cell_text(LEGACY, model.TIER) == ""
    page.assign_tier(LEGACY, "balanced")
    assert page.table.cell_text(LEGACY, model.TIER) == "Balanced"
    assert page.table.cell_text(BAL, model.TIER) == ""
    page.assign_tier(LEGACY, "none")
    assert page.table.cell_text(BAL, model.TIER) == "Balanced"
    assert page.adaptive.tiers == ["Efficiency", "Balanced", "Performance"]


# ---------------------------------------------------------------- action rules


def test_menu_actions_follow_the_real_enablement_rules(page):
    every = {k: True for k in model.ACTIONS}
    assert page.menu_state([BAL]) == every
    legacy = page.menu_state([LEGACY])
    assert legacy["edit_fan"] is False and legacy["apply"] is True
    page.set_busy(True)
    busy = page.menu_state([BAL])
    for key in ("apply", "verify", "export_lact", "assign_tier", "delete"):
        assert busy[key] is False, key
    for key in ("edit_vf", "edit_fan", "edit_memory"):
        assert busy[key] is True, key
    assert not page.restore_button.isEnabled() and not page.delete_button.isEnabled()
    assert not page.detail.primary_button.isEnabled()
    page.set_busy(False)
    edited = page.source.save_memory_offset(BAL, 2000)
    page.reload()
    unverified = page.menu_state([edited])
    assert unverified["apply"] is False
    assert unverified["export_lact"] is False and unverified["assign_tier"] is False
    assert unverified["verify"] is True


def test_detail_card_for_a_profile_without_fan_curve(page):
    page.select_profiles([LEGACY])
    state = page.detail.state
    assert state.mode == "single" and state.show_fan_button is False
    assert not page.detail.edit_fan_button.isVisibleTo(page.detail)
    assert state.silent_fan_subtitle == "This profile has no saved fan curve."
    page.edit_fan(LEGACY)
    assert (
        page.dialogs.last("info")[2]
        == "No editable fan curve is available for this profile."
    )
    page.set_silent_fan(True)
    assert "No runtime-ready silent fan curve" in page.detail.state.session_subtitle
    assert not page.detail.state.primary_enabled
    assert page.source.silent_fan is True


def test_unverified_profile_offers_verify_as_the_primary(page):
    edited = page.source.save_memory_offset(BAL, 2000)
    page.show_new_profile(edited)
    state = page.detail.state
    assert state.primary_action == "verify" and state.primary_text == "Verify…"
    assert state.status == ("Needs verification", "warning")
    assert page.table.pills(edited) == ["Needs verification", "New"]
    page.apply_profile(edited)
    assert (
        page.notice[0]
        == "This edited profile must be verified before it can be applied."
    )


# ---------------------------------------------------------------- apply / boot / restore


def test_apply_writes_boot_only_when_apply_on_startup_is_ticked(page):
    page.select_profiles([BAL])
    page.apply_profile(BAL)
    gpu0 = SAMPLE_GPUS[0].uuid
    assert page.source.runtime().running_selector == BAL
    assert page.source.boot_entry(gpu0) is None
    assert page.asked_daemon == ["Applying runtime profile"]
    assert page.statuses[-2]["running"] == "Starting profile: Balanced; Autostart: No."
    assert page.statuses[-1]["running"] == "Currently running profile: Balanced"
    assert page.statuses[-1]["detail"] == "Autostart: No · Silent fan curve: Off"
    page.set_apply_on_startup(True)
    assert page.source.boot_entry(gpu0) is None and page.startup_checked()
    page.apply_profile(PERF)
    assert page.source.boot_entry(gpu0).selector == PERF
    assert page.table.pills(PERF) == ["Running", "At startup"]
    assert page.detail.state.startup_profile == "Performance"


def test_unticking_apply_on_startup_clears_boot_or_rolls_back(page):
    gpu0 = SAMPLE_GPUS[0].uuid
    page.set_apply_on_startup(True)
    page.apply_profile(BAL)
    page.set_apply_on_startup(False)
    assert page.source.boot_entry(gpu0) is None and not page.startup_checked()
    page.set_apply_on_startup(True)
    page.apply_profile(BAL)
    page.source.set_daemon_available(False)
    page.set_apply_on_startup(False)
    assert page.source.boot_entry(gpu0).selector == BAL
    assert page.startup_checked() and page.detail.startup_switch.isChecked()
    title, text = page.dialogs.last("error")[1:]
    assert title == "Apply on startup" and text.startswith(
        "Could not clear the saved boot profile"
    )


def test_restore_defaults_runs_without_confirmation(page):
    page.apply_profile(BAL)
    page.restore_button.click()
    gpu0 = SAMPLE_GPUS[0].uuid
    assert not page.dialogs.titles("confirm")
    assert page.source.runtime().running_selector == model.STOCK
    assert page.source.boot_entry(gpu0).selector == model.STOCK
    assert page.startup_checked()
    assert page.status()["running"] == "Currently running profile: Default"
    assert "clear core and memory offsets" in page.restore_button.toolTip()


def test_apply_waits_for_the_root_service(app):
    page = make_page(app, daemon=False)
    page.apply_profile(BAL)
    assert page.source.runtime().running_selector == ""


def test_runner_status_shows_per_game_standing(page):
    assert page.status()["running"] == "No running/autostart profile available yet."
    page.apply_profile(BAL)
    page.source.set_game_session(PERF, adaptive=True, live_tier="Performance")
    status = page.status()
    assert status["running"].startswith(
        "Currently running profile: Performance (Adaptive, per-game)"
    )
    assert status["detail"].startswith("Standing: Balanced · Autostart: No")


# ---------------------------------------------------------------- delete safety


def test_deleting_a_plain_profile_keeps_the_gpu_state(page):
    page.apply_profile(BAL)
    page.delete_selected([EFF])
    text = page.dialogs.last("confirm")[2]
    assert text == "Delete Auto-UV profile Efficiency?"
    assert EFF not in page.source.payloads
    assert page.source.runtime().running_selector == BAL


def test_deleting_the_startup_profile_restores_stock(page, app):
    page.set_apply_on_startup(True)
    page.apply_profile(BAL)
    page.delete_selected([BAL])
    assert "persisted on startup" in page.dialogs.last("confirm")[2]
    QTest.qWait(10)
    assert BAL not in page.source.payloads
    assert page.source.runtime().running_selector == model.STOCK
    assert page.source.boot_entry(SAMPLE_GPUS[0].uuid).selector == model.STOCK


def test_deleting_the_running_session_profile_restores_stock(page, app):
    page.apply_profile(PERF)
    page.delete_selected([PERF])
    assert "restore stock now and at boot" in page.dialogs.last("confirm")[2]
    QTest.qWait(10)
    assert page.source.runtime().running_selector == model.STOCK


def test_deleting_the_last_adaptive_tiers_warns(page, app):
    page.source.apply_adaptive(SAMPLE_GPUS[0].uuid, live_tier="Balanced")
    page.select_profiles([EFF, BAL, PERF])
    page.delete_selected()
    text = page.dialogs.last("confirm")[2]
    assert "Delete 3 selected profiles?" in text and "last usable Adaptive" not in text
    page.delete_selected([LEGACY])
    assert "last usable Adaptive Auto-UV profile" in page.dialogs.last("confirm")[2]


def test_declining_delete_changes_nothing(page):
    page.dialogs.confirm_answer = False
    page.delete_selected([BAL])
    assert BAL in page.source.payloads


# ---------------------------------------------------------------- multi-GPU


def test_multi_gpu_targeting(app):
    page = make_page(app, gpus=2)
    assert page.target_bar.isVisibleTo(page)
    combo = page.target_combo
    assert [combo.itemText(i) for i in range(combo.count())] == [
        "Choose target GPU…",
        "GPU 0 · RTX 5080",
        "GPU 1 · RTX 4070",
    ]
    assert "does not modify hardware settings" in combo.toolTip()
    # No target yet: Apply and Restore wait for a choice.
    assert not page.restore_button.isEnabled()
    assert "Choose a target GPU" in page.detail.state.session_subtitle
    page.set_target_gpu(1)
    assert set(page.table.row_ids()) == {GPU1, LEGACY}
    page.select_profiles([GPU1])
    assert page.detail.state.primary_text == "Apply to GPU 1"
    page.select_profiles([LEGACY])
    assert "legacy profile must be verified" in page.detail.state.session_subtitle
    page.select_profiles([GPU1])
    assert not page.main_gpu.isEnabled()
    assert "Apply on startup first" in page.main_gpu.toolTip()
    page.set_apply_on_startup(True)
    page.apply_profile(GPU1)
    assert page.main_gpu.isEnabled()
    page.main_gpu.setChecked(True)
    assert page.source.main_gpu == SAMPLE_GPUS[1].uuid
    assert "owns daemon monitoring" in page.main_gpu.toolTip()
    # Applying to the other GPU asks before moving runtime there.
    page.set_target_gpu(0)
    page.select_profiles([BAL])
    page.dialogs.confirm_answer = False
    page.apply_profile(BAL)
    assert page.dialogs.last("confirm")[1] == "Switch managed GPU"
    assert page.source.runtime().running_selector == GPU1
    page.dialogs.confirm_answer = True
    page.apply_profile(BAL)
    assert page.source.runtime().running_gpu_uuid == SAMPLE_GPUS[0].uuid


def test_verify_refuses_another_gpus_profile(app):
    page = make_page(app, gpus=2)
    page.set_target_gpu(0)
    page.verify(GPU1)
    assert page.dialogs.last("error")[2] == (
        "This profile belongs to a different GPU. Choose its GPU target."
    )


# ---------------------------------------------------------------- verify


def test_verify_asks_for_a_duration_and_hands_off(page):
    requests = []
    page.verify_requested.connect(lambda *args: requests.append(args))
    page.dialogs.minutes = 7
    page.verify(BAL)
    ((pid, label, row, minutes),) = requests
    assert (pid, label, minutes) == (BAL, "Balanced", 7)
    assert row["mv"] == 850 and row["target"] == 2639 and len(row["points"]) == 41
    assert row["tier"] == "Balanced"
    assert page.asked_daemon[-1] == "Verifying an Auto-UV profile"
    page.dialogs.minutes = None
    page.verify(BAL)
    assert len(requests) == 1


def test_real_verify_dialog_offers_one_to_sixty_minutes(app):
    seen = {}

    def inspect():
        dialog = app.activeModalWidget()
        spin = dialog.findChild(W.QSpinBox)
        seen.update(
            minimum=spin.minimum(), maximum=spin.maximum(), suffix=spin.suffix()
        )
        dialog.reject()

    C.QTimer.singleShot(0, inspect)
    assert ProfileDialogs().verify_minutes(None, "Balanced") is None
    assert seen == {"minimum": 1, "maximum": 60, "suffix": " min"}


def test_verification_pass_promotes_and_selects(page):
    edited = page.source.save_memory_offset(BAL, 2000)
    page.reload()
    page.sort_by(model.FPS)
    page.verification_finished(edited, True)
    assert page.table.row_ids()[0] == edited
    assert page.selected_ids() == [edited]
    assert page.table.pills(edited) == ["New"]
    assert page.profile(edited)["final_verified"] is True


# ---------------------------------------------------------------- edits


def test_vf_edit_saves_a_copy_that_needs_verification(page):
    plan = page.source.curve_plan(BAL)
    page.editors.edit = ManualCurveEdit(
        plan=plan, anchor_voltage_mv=850, anchor_clock_mhz=2600, edit_kind="drag-anchor"
    )
    page.edit_vf(BAL)
    kind, kwargs = page.editors.opened[-1]
    assert kind == "vf" and kwargs["anchor"] == (850, 2639) and kwargs["base_points"]
    new = page.selected_ids()[0]
    assert new != BAL and page.profile(new)["final_verified"] is False
    assert page.profile(new)["display_name"].startswith("User edited 2600 MHz 850 mV")


def test_fan_edit_keeps_the_parents_verification(page):
    curve = page.source.fan_curve(BAL)
    page.editors.edit = ManualFanCurveEdit(
        points=[(t, s + 2) for t, s in curve], edit_kind="drag"
    )
    page.edit_fan(BAL)
    kind, kwargs = page.editors.opened[-1]
    assert (
        kind == "fan" and len(kwargs["measured_points"]) > 10 and kwargs["target_point"]
    )
    new = page.selected_ids()[0]
    assert page.profile(new)["final_verified"] is True
    assert page.menu_state([new])["apply"] is True


def test_memory_offset_editor_gets_the_gpu_range(page):
    page.dialogs.memory_value = 4000
    page.edit_memory(BAL)
    assert page.dialogs.last("memory") == ("memory", 3000, 0, 6000)
    new = page.selected_ids()[0]
    assert page.profile(new)["memory_offset_mhz"] == 4000
    assert page.table.cell_text(new, model.MEM) == "+2000 MHz"


def test_export_lact_reports_path_or_gpu_id_error(page):
    page.export_lact(BAL)
    assert page.dialogs.last("lact_result")[1] == "/etc/lact/config.yaml"
    assert page.notice[0] == "LACT profile written: /etc/lact/config.yaml"
    page.dialogs.directory = "~/.config/PenguinBurner"
    page.export_lact(BAL)
    assert page.dialogs.last("error")[2].startswith("Could not detect the LACT GPU id")


# ---------------------------------------------------------------- Afterburner


def afterburner_entry():
    return {
        "section": "Profile2",
        "target_voltage_mv": 875,
        "target_clock_mhz": 2600,
        "afterburner_root": "/tmp/afterburner",
        "profile_path": "/tmp/afterburner/Profiles/VEN_10DE&DEV_2C02.cfg",
        "curve_points": [[800, 2000], [875, 2600], [1000, 2600]],
        "importable": True,
    }


def test_afterburner_import_selects_the_new_row(page):
    pid = page.import_afterburner(afterburner_entry())
    assert page.selected_ids() == [pid] and page.table.row_ids()[0] == pid
    assert page.dialogs.last("info")[2] == (
        "Imported Afterburner profile Profile2 from VEN_10DE&DEV_2C02.cfg."
    )
    assert page.detail.state.status == ("Imported", "accent")
    page.set_busy(True)
    assert page.import_afterburner(afterburner_entry()) is None
    assert page.notice[0].startswith("Finish the current PenguinBurner action")


# ---------------------------------------------------------------- empty + warnings


def test_empty_state_offers_setup_and_import(app):
    page = make_page(app)
    page.source.payloads.clear()
    page.reload()
    assert page.empty_card.isVisibleTo(page) and not page.table_card.isVisibleTo(page)
    asked = []
    page.setup_requested.connect(lambda: asked.append("setup"))
    page.import_requested.connect(lambda: asked.append("import"))
    for button in page.empty_card.findChildren(W.QPushButton):
        button.click()
    assert asked == ["setup", "import"]


def test_showing_the_page_prints_no_qt_warnings(app):
    messages = []

    def handler(kind, _context, message):
        # The offscreen platform plugin reports what it cannot do.
        if not message.startswith("This plugin does not support"):
            messages.append(message)

    previous = C.qInstallMessageHandler(handler)
    try:
        page = make_page(app, gpus=2)
        page.show()
        page.set_target_gpu(0)
        page.select_profiles([BAL, PERF])
        page.select_profiles([BAL])
        menu = page.fill_menu(W.QMenu(page), [BAL])
        menu.popup(C.QPoint(10, 10))
        app.processEvents()
        menu.close()
        page.grab()
    finally:
        C.qInstallMessageHandler(previous)
    assert messages == []
