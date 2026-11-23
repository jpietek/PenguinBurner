"""Profiles page: saved profiles, Apply / Apply on startup, edits, Verify.

Native widgets over a ProfilesSource (profile_model.ProfilesSource): the
classic Profiles tab's workflows (ui/features/profiles/profile_actions.py),
with every rule from the backend (profiles.uv.profile_rules) through
profile_model. Dialogs (confirmations, Verify, Edit memory offset, LACT,
Afterburner) and the V/F and fan curve editors open through small hook
objects so a host can restyle or script them.

Host wiring (the window, or the preview):
  page = ProfilesPage(source, dialogs=..., editors=..., ensure_daemon_ready=...)
  page.status_changed(dict)        -> window status band
  page.verify_requested(id, label, row, minutes) -> profile verification;
      call page.verification_finished(id, passed) when it ends
  page.adaptive_requested()        -> open Game Library's Adaptive picker
  page.setup_requested()           -> Auto UV setup (empty state)
  page.import_requested()          -> run_afterburner_import(), or a host's picker
  page.set_busy(bool)              <- scan/verification running elsewhere
  page.import_afterburner(entry, fan_payload) after the Afterburner picker
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore as C
from PySide6 import QtGui as G
from PySide6 import QtWidgets as W

from curve_editors.uv.vf_curve_manual_editor import (
    editable_anchor_from_profile,
    manual_edit_control_voltage_mvs,
)
from profiles.gpu_identity import profile_gpu_label, profile_gpu_uuid
from profiles.uv.memory_offset_edit import editable_memory_offset_from_profile
from profiles.uv.profile_presentation import (
    format_number,
    profile_name,
    profile_source_label,
    to_float,
)
from profiles.uv.profile_rules import (
    NO_SILENT_FAN_CURVE,
    boot_apply_checked,
    delete_confirmation_text,
    main_gpu_change_allowed,
    profile_can_verify,
    profile_status_label,
    tier_winner_ids,
)
from profiles.uv.profile_tiers import (
    PROFILE_TIER_LABELS,
    PROFILE_TIERS,
    normalize_profile_tier,
)
from ui.components import confirm_dialog
from ui.components.page_header import PAGE_MARGINS, PageHeader
from ui.components.profile_style import (
    apply_style,
    divider,
    repolish,
    text_label,
    trash_icon,
)
from ui.design.style import GAP
from ui.dialogs.error_details import process_failure_details, show_error_dialog
from ui.features.profiles import profile_model as model
from ui.features.profiles.profile_detail import (
    AdaptiveCard,
    DetailState,
    Metric,
    ProfileDetailCard,
)
from ui.features.profiles.profile_model import (
    CommandResult,
    LactExport,
    ProfilesSource,
    StatusLines,
    Target,
)
from ui.features.profiles.profile_table import ProfileTable, TableRow

HINT = (
    "Ctrl-click selects several profiles and Shift-click selects a range. Press "
    "Delete, use the trash button or right-click to delete the selection. "
    "Per-game Adaptive targets are set in Game Library."
)
RESTORE_TIP = (
    "Reset the GPU to stock now and at boot: clear core and memory offsets, "
    "release locked clocks, restore the factory V/F curve, and restore the "
    "default power limit."
)
TARGET_TIP = (
    "Choose which GPU this Profiles action targets. Changing this selection "
    "does not modify hardware settings."
)
SWITCH_GPU_TEXT = (
    "Another GPU is currently managed. Switching stops monitoring that GPU. "
    "Its applied curve, memory offset, or power limit may remain until you "
    "restore it or the driver resets it.\n\nSwitch to the selected GPU?"
)
MENU_LABELS = {
    "edit_vf": "Edit V/F curve…",
    "edit_fan": "Edit fan curve…",
    "edit_memory": "Edit memory offset…",
    "apply": "Apply",
    "verify": "Verify…",
    "export_lact": "Export LACT…",
    "assign_tier": "Assign tier",
    "delete": "Delete",
}


# ---------------------------------------------------------------------------
# Hooks: dialogs and editors a host may replace
# ---------------------------------------------------------------------------


# Confirmations whose action destroys data: a loss-outlined button, and
# Enter picks Cancel.
DESTRUCTIVE_ACTIONS = frozenset({"Delete"})


class ProfileDialogs:
    """Message boxes and the real Profiles dialogs."""

    def confirm(self, parent, title: str, text: str, *, accept: str = "Yes") -> bool:
        return confirm_dialog.confirm(
            parent, title, text, accept=accept, destructive=accept in DESTRUCTIVE_ACTIONS
        )

    def info(self, parent, title: str, text: str) -> None:
        W.QMessageBox.information(parent, title, text)

    def error(self, parent, title: str, text: str) -> None:
        W.QMessageBox.warning(parent, title, text)

    def failure(self, parent, title: str, message: str, details: str) -> None:
        """A failed daemon command: the app's copyable error report
        (ui/dialogs/error_details, as the window's ErrorReporter opens it)."""
        show_error_dialog(
            QtCore=C,
            QtGui=G,
            QtWidgets=W,
            parent=parent,
            title=title,
            message=message,
            details=details,
        )

    def verify_minutes(self, parent, label: str) -> int | None:
        from ui.dialogs.verify import select_verify_options

        options = select_verify_options(QtWidgets=W, parent=parent, profile_label=label)
        return None if options is None else max(1, int(options["duration_s"]) // 60)

    def memory_offset(
        self, parent, *, current: int, min_mhz: int, max_mhz: int, save
    ) -> bool:
        from ui.components.memory_offset_editor import open_memory_offset_editor_dialog

        # The real dialog runs its own exec(): polish it once it is up (one
        # filled primary, sentence-case label).
        C.QTimer.singleShot(0, _polish_memory_offset_dialog)
        return open_memory_offset_editor_dialog(
            QtWidgets=W,
            parent=parent,
            current_memory_offset_mhz=current,
            min_mhz=min_mhz,
            max_mhz=max_mhz,
            save_callback=save,
        )

    def lact_directory(self, parent, suggestions: list[str]) -> str | None:
        """Pick the export directory: suggestions, or Browse… for any folder."""
        dialog = W.QDialog(parent)
        dialog.setWindowTitle("Export LACT")
        layout = W.QVBoxLayout(dialog)
        layout.addWidget(
            text_label(
                "Choose the directory that holds LACT's config.yaml. The GPU id "
                "is read from that file.",
                "profilesCaption",
                wrap=True,
            )
        )
        row = W.QHBoxLayout()
        combo = W.QComboBox()
        combo.setEditable(True)
        combo.addItems(suggestions)
        combo.setMinimumWidth(320)
        browse = W.QPushButton("Browse…")

        def pick():
            folder = W.QFileDialog.getExistingDirectory(
                dialog, "Choose LACT Export Directory", str(Path.home())
            )
            if folder:
                combo.setEditText(folder)

        browse.clicked.connect(pick)
        row.addWidget(combo, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        buttons = W.QDialogButtonBox()
        export = buttons.addButton("Export", W.QDialogButtonBox.ButtonRole.AcceptRole)
        export.setObjectName("nextAction")
        buttons.addButton(W.QDialogButtonBox.StandardButton.Cancel)
        export.setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != W.QDialog.DialogCode.Accepted:
            return None
        return combo.currentText().strip() or None

    def afterburner_entry(self, parent) -> dict | None:
        """The classic Afterburner chooser: pick an exported slot to import."""
        import pyqtgraph as pg

        from ui.dialogs.afterburner_import import select_afterburner_import

        return select_afterburner_import(
            QtCore=C, QtGui=G, QtWidgets=W, pg=pg, parent=parent
        )

    def lact_result(self, parent, result: LactExport, message: str) -> None:
        dialog = W.QDialog(parent)
        dialog.setWindowTitle("Export LACT")
        dialog.setMinimumWidth(560)
        layout = W.QVBoxLayout(dialog)
        layout.addWidget(text_label(message, "profilesCaption", wrap=True))
        preview = W.QPlainTextEdit(result.text)
        preview.setReadOnly(True)
        preview.setMinimumHeight(260)
        layout.addWidget(preview)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()


def _polish_memory_offset_dialog() -> None:
    dialog = W.QApplication.activeModalWidget()
    if not isinstance(dialog, W.QDialog) or dialog.windowTitle() != "Edit Memory Offset":
        return
    dialog.setWindowTitle("Edit memory offset")
    for label in dialog.findChildren(W.QLabel):
        if label.text() == "Memory Offset":
            label.setText("Memory offset")
    for button in dialog.findChildren(W.QPushButton):
        if button.text() == "Save":
            button.setObjectName("nextAction")
            button.setDefault(True)
            repolish(button)


class CurveEditors:
    """Opens the real V/F and fan curve editors (ui/components)."""

    def open_vf(
        self, parent, *, plan, anchor, base_points, control_voltage_mvs, save
    ) -> None:
        import pyqtgraph as pg

        from ui.components.vf_curve_editor import open_vf_curve_editor_dialog

        open_vf_curve_editor_dialog(
            QtCore=C,
            QtGui=G,
            QtWidgets=W,
            pg=pg,
            parent=parent,
            plan=plan,
            anchor=anchor,
            base_points=base_points,
            control_voltage_mvs=list(control_voltage_mvs),
            save_callback=save,
        )

    def open_fan(
        self, parent, *, curve_points, measured_points, target_point, save
    ) -> None:
        import pyqtgraph as pg

        from ui.components.fan_curve_editor import open_fan_curve_editor_dialog

        open_fan_curve_editor_dialog(
            QtCore=C,
            QtGui=G,
            QtWidgets=W,
            pg=pg,
            parent=parent,
            curve_points=curve_points,
            measured_points=measured_points,
            target_point=target_point,
            save_callback=save,
        )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


class ProfilesPage(W.QWidget):
    status_changed = C.Signal(dict)
    verify_requested = C.Signal(str, str, dict, int)
    adaptive_requested = C.Signal()
    setup_requested = C.Signal()
    import_requested = C.Signal()
    adaptive_tiers_changed = C.Signal(list)
    target_gpu_changed = C.Signal(int)

    def __init__(
        self,
        source: ProfilesSource,
        *,
        dialogs: ProfileDialogs | None = None,
        editors: CurveEditors | None = None,
        ensure_daemon_ready: Callable[[str], bool] | None = None,
        gpu_index: int | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("profilesPage")
        self.source = source
        # The window's --gpu-index, then the last target chosen here: it wins
        # over the previous target when the GPU list is re-read.
        self.preferred_gpu_index = gpu_index
        self.dialogs = dialogs or ProfileDialogs()
        self.editors = editors or CurveEditors()
        self.ensure_daemon_ready = ensure_daemon_ready or (lambda _label: True)
        self.profiles: list[dict] = []
        self.target = Target(())
        self._verify_gpus: dict[str, str] = {}
        self.sort_column, self.descending = model.DATE, True
        self.selected: list[str] = []
        self.selection_cleared = False
        self.preferred_id = ""
        self.boot_intent: dict[str, bool] = {}
        self.external_busy = False
        self.command: StatusLines | None = None
        self.notice: tuple[str, str] = ("", "")
        self._status: dict | None = None
        self._tiers: list[str] | None = None
        self._loading = False
        self.silent_fan = bool(source.silent_fan_preference())
        apply_style(self)
        self._build()
        source.subscribe(self._source_changed)
        self.reload()

    # ---------------- layout ----------------

    def _build(self) -> None:
        outer = W.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = W.QScrollArea()
        scroll.setObjectName("profilesScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(C.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        body = W.QWidget()
        body.setObjectName("profilesBody")
        scroll.setWidget(body)
        column = W.QVBoxLayout(body)
        column.setContentsMargins(*PAGE_MARGINS)
        column.setSpacing(GAP["md"])

        column.addWidget(
            PageHeader("Profiles", "Apply, edit and verify saved undervolt profiles.")
        )

        # Saved profiles: header + toolbar, target bar, table, hint.
        self.table_card = W.QFrame()
        self.table_card.setObjectName("profilesCard")
        card = W.QVBoxLayout(self.table_card)
        card.setContentsMargins(0, 0, 0, 0)
        card.setSpacing(0)
        head = W.QHBoxLayout()
        head.setContentsMargins(GAP["lg"] + 4, GAP["md"] + 2, GAP["lg"], GAP["md"])
        titles = W.QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(text_label("Saved profiles", "profilesTitle"))
        self.count_label = text_label("", "profilesCaption")
        titles.addWidget(self.count_label)
        head.addLayout(titles, 1)
        self.delete_button = W.QToolButton()
        self.delete_button.setObjectName("profilesIconButton")
        self.delete_button.setIcon(trash_icon())
        self.delete_button.setIconSize(C.QSize(18, 18))
        self.delete_button.setToolTip("Delete selected profiles")
        self.delete_button.setAccessibleName("Delete selected profiles")
        self.delete_button.clicked.connect(lambda: self.delete_selected())
        self.restore_button = W.QPushButton("Restore defaults")
        self.restore_button.setObjectName("restoreDefaults")
        self.restore_button.setToolTip(RESTORE_TIP)
        self.restore_button.clicked.connect(lambda: self.restore_defaults())
        head.addWidget(self.delete_button, 0, C.Qt.AlignmentFlag.AlignVCenter)
        head.addSpacing(GAP["sm"])
        head.addWidget(self.restore_button, 0, C.Qt.AlignmentFlag.AlignVCenter)
        card.addLayout(head)

        self.target_bar = W.QWidget()
        bar = W.QHBoxLayout(self.target_bar)
        bar.setContentsMargins(GAP["lg"] + 4, 0, GAP["lg"], GAP["md"])
        bar.setSpacing(GAP["sm"])
        target_label = text_label("Target GPU", "profilesRowTitle")
        self.target_combo = W.QComboBox()
        self.target_combo.setObjectName("profileTargetGpu")
        self.target_combo.setToolTip(TARGET_TIP)
        self.target_combo.setMinimumWidth(220)
        target_label.setBuddy(self.target_combo)
        self.target_combo.currentIndexChanged.connect(self._target_combo_changed)
        self.main_gpu = W.QCheckBox("Main GPU")
        self.main_gpu.setObjectName("profileMainGpu")
        self.main_gpu.toggled.connect(self._main_gpu_toggled)
        bar.addWidget(target_label)
        bar.addWidget(self.target_combo)
        bar.addSpacing(GAP["md"])
        bar.addWidget(self.main_gpu)
        bar.addSpacing(GAP["md"])
        bar.addWidget(
            text_label(
                "Changing the target does not change hardware settings.", "profilesHint"
            )
        )
        bar.addStretch()
        card.addWidget(self.target_bar)

        self.table = ProfileTable()
        self.table.sort_requested.connect(self.sort_by)
        self.table.selection_changed.connect(self._table_selection)
        self.table.delete_requested.connect(lambda: self.delete_selected())
        self.table.menu_requested.connect(self._open_menu)
        card.addWidget(self.table)
        self.empty_row = text_label(
            "No saved profiles for this GPU.", "profilesCaption"
        )
        self.empty_row.setContentsMargins(GAP["lg"] + 4, GAP["md"], 0, GAP["md"])
        card.addWidget(self.empty_row)
        card.addWidget(divider())
        hint = text_label(HINT, "profilesHint", wrap=True)
        hint.setContentsMargins(GAP["lg"] + 4, GAP["sm"] + 2, GAP["lg"], GAP["sm"] + 2)
        card.addWidget(hint)
        column.addWidget(self.table_card)

        self.detail = ProfileDetailCard()
        self.detail.edit_vf.connect(lambda: self.edit_vf(self.lead_id()))
        self.detail.edit_fan.connect(lambda: self.edit_fan(self.lead_id()))
        self.detail.verify.connect(lambda: self.verify(self.lead_id()))
        self.detail.primary.connect(self._primary)
        self.detail.silent_fan_toggled.connect(self.set_silent_fan)
        self.detail.startup_toggled.connect(self.set_apply_on_startup)
        self.detail.delete_selection.connect(lambda: self.delete_selected())
        self.more_menu = W.QMenu(self.detail)
        self.more_menu.setObjectName("profilesMenu")
        self.more_menu.aboutToShow.connect(
            lambda: self.fill_menu(
                self.more_menu, [self.lead_id()] if self.lead_id() else []
            )
        )
        self.detail.set_more_menu(self.more_menu)
        column.addWidget(self.detail)

        self.adaptive = AdaptiveCard()
        self.adaptive.use_adaptive.connect(self.adaptive_requested)
        column.addWidget(self.adaptive)

        # Empty state: nothing saved yet.
        self.empty_card = W.QFrame()
        self.empty_card.setObjectName("profilesCard")
        empty = W.QVBoxLayout(self.empty_card)
        empty.setContentsMargins(GAP["lg"] + 4, GAP["lg"], GAP["lg"] + 4, GAP["lg"])
        empty.setSpacing(GAP["sm"])
        empty.addWidget(text_label("No saved profiles yet", "profilesTitle"))
        empty.addWidget(
            text_label(
                "Complete Auto UV and its final verification to create your first "
                "profile, or import an MSI Afterburner profile.",
                "profilesCaption",
                wrap=True,
            )
        )
        buttons = W.QHBoxLayout()
        buttons.setSpacing(GAP["sm"])
        setup = W.QPushButton("Setup Auto Undervolt")
        setup.setObjectName("profilesPrimary")
        setup.clicked.connect(self.setup_requested)
        importer = W.QPushButton("Import Afterburner…")
        importer.clicked.connect(self.import_requested)
        buttons.addWidget(setup)
        buttons.addWidget(importer)
        buttons.addStretch()
        empty.addSpacing(GAP["xs"])
        empty.addLayout(buttons)
        column.addWidget(self.empty_card)
        column.addStretch()

    # ---------------- data ----------------

    def _source_changed(self) -> None:
        if not self._loading:
            self.reload()

    def refresh(self) -> None:
        """Re-read everything, the silent-fan load rule included (after the
        root service changed)."""
        self.silent_fan = bool(self.source.silent_fan_preference())
        self.reload()

    def reload(self) -> None:
        """Re-read the source; the sort order and selection survive."""
        self._loading = True
        try:
            self.profiles = list(self.source.profiles())
            self._configure_target(self.target.uuid)
            if self.boot() is not None:
                # Confirmed on the daemon: there is nothing left pending.
                self.boot_intent.pop(self.target.uuid.casefold(), None)
            ids = {p["profile_id"] for p in self.visible()}
            if self.preferred_id not in {p["profile_id"] for p in self.profiles}:
                self.preferred_id = ""
            self.selected = [i for i in self.selected if i in ids]
            if not self.selected and not self.selection_cleared and ids:
                self.selected = [self.ordered()[0]["profile_id"]]
            self._render()
        finally:
            self._loading = False

    def _configure_target(self, previous_uuid: str) -> None:
        gpus = self.source.gpus()
        target = model.resolve_target(
            gpus, previous_uuid=previous_uuid, preferred_index=self.preferred_gpu_index
        )
        changed = target != self.target
        self.target = target
        blocked = self.target_combo.blockSignals(True)
        try:
            self.target_combo.clear()
            if target.selection_required:
                self.target_combo.addItem("Choose target GPU…", "")
            for gpu in target.gpus:
                self.target_combo.addItem(gpu.label, gpu.uuid)
            index = max(0, self.target_combo.findData(target.uuid))
            self.target_combo.setCurrentIndex(index)
        finally:
            self.target_combo.blockSignals(blocked)
        self.target_bar.setVisible(target.selection_required)
        if changed:
            self.target_gpu_changed.emit(-1 if target.index is None else target.index)

    def set_target_gpu(self, index: int | None) -> None:
        """Choose the target GPU by index (None = Choose target GPU…)."""
        uuid = ""
        for gpu in self.target.gpus:
            if gpu.index == index:
                uuid = gpu.uuid
        combo_index = self.target_combo.findData(uuid)
        if combo_index >= 0:
            self.target_combo.setCurrentIndex(combo_index)

    def _target_combo_changed(self, _index: int) -> None:
        uuid = str(self.target_combo.currentData() or "")
        self.target = Target(self.target.gpus, uuid)
        self.preferred_gpu_index = self.target.index
        self.notice = ("", "")
        self.target_gpu_changed.emit(
            -1 if self.target.index is None else self.target.index
        )
        if self.target.index is not None:
            # The chosen GPU becomes the saved runtime GPU, as the classic
            # target selector does.
            try:
                self.source.remember_target_gpu(self.target.index)
            except Exception as exc:  # noqa: BLE001 - a corrupt config is refused
                self.dialogs.error(
                    self, "GPU selection", f"Could not save selected GPU index: {exc}"
                )
        self.reload()

    def visible(self) -> list[dict]:
        return model.visible_profiles(self.profiles, self.target)

    def ordered(self) -> list[dict]:
        rows = model.sort_profiles(self.visible(), self.sort_column, self.descending)
        return model.promote_preferred(rows, self.preferred_id)

    def profile(self, profile_id: str) -> dict | None:
        return model.profile_by_id(self.profiles, profile_id)

    def lead_id(self) -> str:
        return self.selected[-1] if self.selected else ""

    def busy(self) -> bool:
        return self.external_busy or self.command is not None

    def set_busy(self, busy: bool) -> None:
        """A scan or verification elsewhere locks Apply, Delete and Restore."""
        busy = bool(busy)
        if busy != self.external_busy:
            self.external_busy = busy
            self._render()

    def _winners(self) -> dict[str, str]:
        return tier_winner_ids(
            self.profiles,
            include_legacy_profiles=self.target.single_gpu,
            store=model.tier_store(self.source),
        )

    def adaptive_tiers(self) -> list[str]:
        return model.adaptive_tier_labels(
            self.profiles, model.tier_store(self.source), self.target
        )

    def boot(self):
        return self.source.boot_entry(self.target.uuid) if self.target.chosen else None

    def startup_checked(self) -> bool:
        """Apply on startup: the daemon's boot entry for the target GPU, or a
        freshly ticked box whose entry the next Apply writes."""
        if not self.target.chosen:
            return False
        boot = self.boot()
        return boot_apply_checked(
            "" if boot is None else boot.selector,
            self.boot_intent.get(self.target.uuid.casefold(), False),
        )

    def silent_fan_enabled(self) -> bool:
        return self.silent_fan

    # ---------------- rendering ----------------

    def _render(self) -> None:
        has_any = bool(self.profiles)
        for widget in (self.table_card, self.detail, self.adaptive):
            widget.setVisible(has_any)
        self.empty_card.setVisible(not has_any)
        rows = self.ordered()
        runtime = self.source.runtime()
        boot_ids = {
            e.selector
            for g in self.target.gpus
            if (e := self.source.boot_entry(g.uuid)) is not None
        }
        winners = self._winners()
        gpus = self.source.gpus()
        table_rows = []
        for profile in rows:
            pid = profile["profile_id"]
            pills: list[tuple[str, str]] = []
            if runtime.running_selector == pid:
                pills.append(("Running", "accent"))
            if not model.is_applicable(profile):
                pills.append(("Needs verification", "warning"))
            if pid in boot_ids:
                pills.append(("At startup", "quiet"))
            if pid == self.preferred_id:
                pills.append(("New", "accent"))
            table_rows.append(
                TableRow(
                    pid,
                    model.profile_cells(
                        profile,
                        factory_power_w=model.factory_power_limit(profile, gpus),
                        tier=model.tier_label(profile, winners),
                    ),
                    tuple(pills),
                )
            )
        self.table.header.set_sort(self.sort_column, self.descending)
        self.table.set_rows(table_rows, self.selected)
        self.table.fit_height()
        self.table.setVisible(bool(rows))
        self.empty_row.setVisible(not rows)
        count = len(rows)
        text = f"{count} profile" + ("" if count == 1 else "s")
        if len(self.selected) > 1:
            text += f" · {len(self.selected)} selected"
        self.count_label.setText(text)
        busy = self.busy()
        deletable = bool(self.selected) and all(
            model.is_deletable(p) for p in (self.profile(i) for i in self.selected) if p
        )
        self.delete_button.setEnabled(not busy and deletable)
        self.restore_button.setEnabled(not busy and self.target.chosen)
        # A run in progress keeps its GPU: no retargeting until it ends.
        self.target_combo.setEnabled(not busy)
        self._sync_main_gpu()
        self.detail.set_state(self.detail_state())
        tiers = self.adaptive_tiers()
        self.adaptive.set_tiers(tiers, target_chosen=self.target.chosen)
        if tiers != self._tiers:
            self._tiers = tiers
            self.adaptive_tiers_changed.emit(list(tiers))
        self._emit_status()

    def _sync_main_gpu(self) -> None:
        boot = self.boot()
        has_boot = boot is not None
        checked = bool(boot and boot.main_gpu)
        blocked = self.main_gpu.blockSignals(True)
        self.main_gpu.setChecked(checked)
        self.main_gpu.blockSignals(blocked)
        self.main_gpu.setEnabled(
            not self.busy()
            and self.target.selection_required
            and self.target.chosen
            and has_boot
        )
        if not self.target.chosen:
            tip = "Choose a target NVIDIA GPU first."
        elif not has_boot:
            tip = (
                "Apply a profile with Apply on startup first, then mark this NVIDIA "
                "GPU as the daemon's main monitored GPU."
            )
        elif checked:
            tip = (
                "This NVIDIA GPU owns daemon monitoring after boot. Untick to restore "
                "the default last-saved-GPU behavior."
            )
        else:
            tip = (
                "Make this NVIDIA GPU own daemon monitoring after boot. Manual Apply "
                "can still move monitoring for the current session."
            )
        self.main_gpu.setToolTip(tip)

    def _where(self) -> str:
        return (
            "the target GPU"
            if self.target.index is None
            else f"GPU {self.target.index}"
        )

    def detail_state(self) -> DetailState:
        busy = self.busy()
        if len(self.selected) > 1:
            names = tuple(
                profile_status_label(self.profiles, pid) for pid in self.selected
            )
            return DetailState(
                mode="multi",
                title=f"{len(names)} profiles selected",
                subtitle=", ".join(names),
                selected_names=names,
                delete_enabled=self.delete_button.isEnabled(),
            )
        profile = self.profile(self.lead_id())
        if profile is None:
            return DetailState()
        pid = profile["profile_id"]
        name = profile_name(profile)
        winners = self._winners()
        tier = model.tier_label(profile, winners)
        gpu_text = profile_gpu_label(profile)
        uuid = profile_gpu_uuid(profile)
        gpu = next((g for g in self.target.gpus if g.uuid == uuid), None)
        subtitle = " · ".join(
            filter(
                None,
                (
                    profile_source_label(profile),
                    f"{tier} tier" if tier else "",
                    gpu.label if gpu else gpu_text,
                ),
            )
        )
        has_plan = bool(self.source.curve_plan(pid))
        has_fan = bool(self.source.fan_curve(pid))
        reasons = model.action_reasons(
            profile, self.target, busy=busy, has_vf_plan=has_plan, has_fan_curve=has_fan
        )
        meas = to_float(profile.get("avg_core_clock_mhz"))
        memory = model.memory_clock_mhz(profile)
        stored = editable_memory_offset_from_profile(profile)
        metrics = (
            Metric(
                "Core voltage",
                format_number(profile.get("candidate_voltage_mv"), precision=0) or "–",
                "mV",
            ),
            Metric(
                "Target clock",
                format_number(profile.get("lock_clock_mhz"), precision=0) or "–",
                "MHz",
            ),
            Metric(
                "Effective clock",
                "–" if meas is None else str(round(meas)),
                "" if meas is None else "MHz",
                model.profile_cells(profile, factory_power_w=None, tier="")[
                    model.EFFECTIVE
                ].tooltip,
            ),
            Metric(
                "Memory offset",
                "–" if memory is None else f"{memory:+d}" if memory else "0",
                "" if memory is None else "MHz",
                "" if stored is None else model.transfer_rate_text(stored),
            ),
        )
        applicable = model.is_applicable(profile)
        refusal = model.apply_refusal(profile, self.target)
        if (
            applicable
            and not refusal
            and self.silent_fan
            and not self.source.fan_runtime_ready(pid)
        ):
            refusal = NO_SILENT_FAN_CURVE
        runtime = self.source.runtime()
        running_here = runtime.running_selector == pid and (
            not runtime.running_gpu_uuid or runtime.running_gpu_uuid == self.target.uuid
        )
        startup = self.startup_checked()
        if not applicable:
            session = "This edited profile must be verified before it can be applied."
            tone = "warning"
        elif refusal:
            session, tone = refusal, "warning"
        elif running_here:
            # The Running pill already says it; the line names only what the
            # last action did, never both.
            session, tone = (
                ("", "")
                if self.notice[0]
                else (f"{name} is running on {self._where()}.", "")
            )
        else:
            session = f"Applies {name} to {self._where()}" + (
                " now and at startup." if startup else " for this session."
            )
            tone = ""
        if not applicable:
            primary_text, action = "Verify…", "verify"
            primary_enabled = not reasons["verify"]
            primary_tip = (
                reasons["verify"] or "Verify this profile before it can be applied."
            )
        else:
            action = "apply"
            if self.target.selection_required and self.target.chosen:
                primary_text = f"Apply to GPU {self.target.index}"
            elif len(name) <= 18:
                primary_text = f"Apply {name}"
            else:
                primary_text = "Apply"
            primary_enabled = not busy and not refusal
            primary_tip = (
                refusal
                or "Apply the single selected profile now. Apply on startup controls "
                "whether it also becomes the boot profile."
            )
            if running_here and not refusal:
                primary_text = "Re-apply"
                primary_tip = f"{name} is running now. Apply it again."
        boot = self.boot()
        status = model.verification_label(profile)
        return DetailState(
            mode="single",
            title=name,
            subtitle=subtitle,
            status=status,
            metrics=metrics,
            edit_vf_reason=reasons["edit_vf"],
            show_fan_button=has_fan,
            verify_reason=reasons["verify"],
            more_enabled=True,
            silent_fan_checked=self.silent_fan,
            silent_fan_enabled=not busy,
            silent_fan_subtitle=(
                "Apply also sets the saved fan curve."
                if has_fan
                else "This profile has no saved fan curve."
            ),
            startup_checked=startup,
            startup_enabled=not busy and self.target.chosen,
            startup_subtitle=(
                "Choose a target GPU first."
                if not self.target.chosen
                else f"Apply also saves the profile for {self._where()} at boot."
                if startup
                else "Apply changes only the current session."
            ),
            startup_profile=model.boot_label(self.profiles, boot)
            if self.target.chosen
            else "–",
            startup_profile_subtitle=f"What {self._where()} runs after boot.",
            session_subtitle=session,
            session_tone=tone,
            primary_text=primary_text,
            primary_action=action,
            primary_enabled=primary_enabled,
            primary_tooltip=primary_tip,
            primary_secondary=bool(applicable and running_here and not refusal),
            notice=self.notice,
        )

    def _emit_status(self) -> None:
        if self.command is not None:
            lines = self.command
        else:
            lines = model.runner_status(
                self.profiles, self.source.runtime(), self.source.boot_entry("")
            )
        status = lines.as_dict()
        if status != self._status:
            self._status = status
            self.status_changed.emit(dict(status))

    def status(self) -> dict:
        return dict(self._status or {})

    # ---------------- selection and sorting ----------------

    def sort_by(self, column: int) -> None:
        self.sort_column, self.descending = model.next_sort(
            self.sort_column, self.descending, column
        )
        self._render()

    def select_profiles(self, ids: list[str]) -> None:
        visible = {p["profile_id"] for p in self.visible()}
        self.selected = [i for i in ids if i in visible]
        self.selection_cleared = not self.selected
        self.notice = ("", "")
        self._render()

    def selected_ids(self) -> list[str]:
        return list(self.selected)

    def _table_selection(self, ids: list[str]) -> None:
        current = self.table.row_id(self.table.currentRow())
        # Keep the clicked row as the lead so the detail card follows it.
        if current in ids:
            ids = [i for i in ids if i != current] + [current]
        self.selected = list(ids)
        self.selection_cleared = not ids
        self.notice = ("", "")
        self._render()

    def show_new_profile(self, profile_id: str) -> None:
        """Promote a just-saved profile to the top, highlighted and selected."""
        self.reload()
        if self.profile(profile_id) is None:
            return
        self.preferred_id = profile_id
        self.select_profiles([profile_id])
        self.table.setFocus(C.Qt.FocusReason.OtherFocusReason)

    # ---------------- menus ----------------

    def _open_menu(self, ids: list[str], pos: C.QPoint) -> None:
        self.selected = list(ids)
        self._render()
        menu = W.QMenu(self)
        menu.setObjectName("profilesMenu")
        self.fill_menu(menu, ids)
        menu.exec(pos)

    def fill_menu(self, menu: W.QMenu, ids: list[str]) -> W.QMenu:
        """Per-profile actions, each enabled only when it can work."""
        menu.clear()
        menu.setToolTipsVisible(True)
        busy = self.busy()
        if len(ids) > 1:
            action = menu.addAction(f"Delete {len(ids)} selected profiles")
            action.setObjectName("delete")
            action.setEnabled(not busy and self.delete_button.isEnabled())
            if busy:
                action.setToolTip(model.BUSY_REASON)
            action.triggered.connect(lambda: self.delete_selected(list(ids)))
            return menu
        profile = self.profile(ids[0]) if ids else None
        if profile is None:
            return menu
        pid = profile["profile_id"]
        reasons = model.action_reasons(
            profile,
            self.target,
            busy=busy,
            has_vf_plan=bool(self.source.curve_plan(pid)),
            has_fan_curve=bool(self.source.fan_curve(pid)),
        )
        handlers = {
            "edit_vf": lambda: self.edit_vf(pid),
            "edit_fan": lambda: self.edit_fan(pid),
            "edit_memory": lambda: self.edit_memory(pid),
            "apply": lambda: self.apply_profile(pid),
            "verify": lambda: self.verify(pid),
            "export_lact": lambda: self.export_lact(pid),
            "delete": lambda: self.delete_selected([pid]),
        }
        for key in model.ACTIONS:
            if key == "delete":
                menu.addSeparator()
            if key == "assign_tier":
                sub = menu.addMenu(MENU_LABELS[key])
                sub.setObjectName("profilesMenu")
                sub.menuAction().setObjectName(key)
                sub.setEnabled(not reasons[key])
                sub.menuAction().setToolTip(reasons[key])
                self._fill_tier_menu(sub, profile)
                continue
            action = menu.addAction(MENU_LABELS[key])
            action.setObjectName(key)
            action.setEnabled(not reasons[key])
            action.setToolTip(reasons[key])
            action.triggered.connect(handlers[key])
        return menu

    def _fill_tier_menu(self, menu: W.QMenu, profile: dict) -> None:
        group = G.QActionGroup(menu)
        group.setExclusive(True)
        current = normalize_profile_tier(profile.get("assigned_profile_tier_key"))
        disabled = bool(profile.get("profile_tier_disabled"))
        pid = profile["profile_id"]
        for tier in (*PROFILE_TIERS, "none"):
            if tier == "none":
                menu.addSeparator()
            action = menu.addAction(PROFILE_TIER_LABELS.get(tier, "None"))
            action.setObjectName(f"tier:{tier}")
            action.setCheckable(True)
            action.setChecked(tier == current or (tier == "none" and disabled))
            group.addAction(action)
            action.triggered.connect(lambda _=False, t=tier: self.assign_tier(pid, t))

    def menu_state(self, ids: list[str]) -> dict[str, bool]:
        """Action name -> enabled, for the menu these ids would open."""
        menu = self.fill_menu(W.QMenu(self), ids)
        state = {
            a.objectName(): a.isEnabled() for a in menu.actions() if a.objectName()
        }
        menu.deleteLater()
        return state

    # ---------------- commands ----------------

    def _start(self, running: str, detail: str) -> None:
        self.command = StatusLines(running, detail, busy=True)
        self._render()

    def _finish(self, label: str, after: Callable[[CommandResult], None] | None = None):
        def done(result: CommandResult) -> None:
            self.command = None
            if result.cancelled:
                self.reload()
                return
            if after is not None and result.ok:
                after(result)
            if result.ok:
                self.notice = (
                    self.notice[0] or result.message or f"{label} complete.",
                    self.notice[1],
                )
            else:
                # As the window's ErrorReporter: the first line in the status
                # notice, the full report in the copyable error dialog.
                message = result.message or f"{label} failed."
                self.notice = (f"{label} failed: {message.splitlines()[0]}", "loss")
                self.reload()
                self.dialogs.failure(
                    self,
                    f"{label} failed",
                    f"{label} failed. The full error details can be copied for "
                    "troubleshooting.",
                    result.details
                    or process_failure_details(
                        action_label=label,
                        exit_code=1,
                        exit_status="NormalExit",
                        log_tail=message,
                    ),
                )
                return
            self.reload()

        return done

    def _primary(self, action: str) -> None:
        if action == "verify":
            self.verify(self.lead_id())
        else:
            self.apply_profile(self.lead_id())

    def apply_selected(self) -> None:
        if len(self.selected) == 1:
            self.apply_profile(self.selected[0])

    def apply_profile(self, profile_id: str) -> None:
        if self.busy():
            return
        profile = self.profile(profile_id)
        if profile is None:
            return
        refusal = model.apply_refusal(profile, self.target)
        if (
            not refusal
            and self.silent_fan
            and not self.source.fan_runtime_ready(profile_id)
        ):
            refusal = NO_SILENT_FAN_CURVE
        if refusal:
            self.notice = (refusal, "warning")
            self._render()
            return
        if not self.ensure_daemon_ready("Applying runtime profile"):
            return
        if not self._confirm_gpu_switch():
            return
        persist = self.startup_checked()
        label = profile_status_label(self.profiles, profile_id)
        uuid = self.target.uuid
        gpu = self.target.gpu
        self.notice = ("", "")
        self._start(
            f"Starting profile: {label}; Autostart: {'Yes' if persist else 'No'}.",
            f"Applying through the root service · {gpu.label if gpu else ''}",
        )

        def applied(_result: CommandResult) -> None:
            if persist:
                # The daemon now holds the entry: the intent is confirmed.
                self.boot_intent.pop(uuid.casefold(), None)
            self.notice = (
                f"{label} applied to {self._where()}"
                + (" now and at startup." if persist else " for this session."),
                "accent",
            )

        self.source.apply_profile(
            profile_id,
            uuid,
            silent_fan=self.silent_fan,
            persist_on_startup=persist,
            done=self._finish("Apply selected profile", applied),
        )

    def _confirm_gpu_switch(self) -> bool:
        runtime = self.source.runtime()
        running = runtime.running_gpu_uuid.strip()
        if (
            not runtime.running_selector
            or not running
            or not self.target.uuid
            or running.casefold() == self.target.uuid.casefold()
        ):
            return True
        return self.dialogs.confirm(
            self, "Switch managed GPU", SWITCH_GPU_TEXT, accept="Switch GPU"
        )

    def restore_defaults(
        self, *, after: Callable[[], None] | None = None, gpu_uuid: str = ""
    ) -> None:
        """Stock now and at boot, no confirmation: stock is the safe state."""
        uuid = gpu_uuid or self.target.uuid
        if self.busy() or not uuid:
            return
        if not self.ensure_daemon_ready("Restoring GPU to stock"):
            return
        gpu = next((g for g in self.target.gpus if g.uuid == uuid), None)
        self._start(
            "Restoring GPU to stock now and at boot (daemon keeps running).",
            f"The root service keeps running for fan control · {gpu.label if gpu else ''}",
        )

        def restored(_result: CommandResult) -> None:
            self.boot_intent[uuid.casefold()] = True
            self.notice = (
                "Restore defaults complete. The GPU runs stock settings now and at boot.",
                "accent",
            )
            if after is not None:
                after()

        self.source.restore_defaults(
            uuid,
            silent_fan=self.silent_fan,
            done=self._finish("Restore defaults", restored),
        )

    def set_silent_fan(self, checked: bool) -> None:
        """A lasting preference: Apply and Export LACT read it."""
        self.silent_fan = bool(checked)
        self.source.set_silent_fan_preference(self.silent_fan)
        self.notice = ("", "")
        self._render()

    def set_apply_on_startup(self, checked: bool) -> None:
        """Ticking records intent for the next Apply; unticking clears this
        GPU's boot entry now, and rolls back if the daemon refuses."""
        if not self.target.chosen:
            self._render()
            return
        key = self.target.uuid.casefold()
        if checked:
            # Intent only: the boot entry itself is written by the next Apply.
            self.boot_intent[key] = True
            self.source.remember_apply_on_startup(True)
            self._render()
            return
        try:
            self.source.clear_boot(self.target.uuid)
        except Exception as exc:  # noqa: BLE001
            self.boot_intent[key] = True
            self._render()
            self.dialogs.error(
                self,
                "Apply on startup",
                f"Could not clear the saved boot profile: {exc}",
            )
            return
        self.boot_intent.pop(key, None)
        self.reload()

    def _main_gpu_toggled(self, checked: bool) -> None:
        uuid = self.target.uuid
        if not main_gpu_change_allowed(
            checked,
            target_gpu_uuid=uuid,
            selection_required=self.target.selection_required,
            has_boot_profile=self.boot() is not None,
        ):
            self._sync_main_gpu()
            return
        try:
            self.source.set_main_gpu(uuid if checked else "")
        except Exception as exc:  # noqa: BLE001
            self._sync_main_gpu()
            self.dialogs.error(
                self, "Main GPU", f"Could not update the daemon's main GPU: {exc}"
            )
            return
        self.reload()

    # ---------------- delete ----------------

    def delete_selected(self, ids: list[str] | None = None) -> None:
        ids = list(self.selected if ids is None else ids)
        profiles = [p for p in (self.profile(i) for i in ids) if p]
        if (
            self.busy()
            or not profiles
            or not all(model.is_deletable(p) for p in profiles)
        ):
            return
        safety = model.delete_safety(
            self.profiles,
            ids,
            boot=self.boot(),
            runtime=self.source.runtime(),
            target=self.target,
            store=model.tier_store(self.source),
        )
        names = [profile_status_label(self.profiles, i) for i in ids]
        text = delete_confirmation_text(
            names,
            restores_stock=safety.restore_stock,
            removes_last_usable_adaptive_profile=safety.last_adaptive,
        )
        if not self.dialogs.confirm(self, "Delete profiles", text, accept="Delete"):
            return
        self._start(
            "Deleting selected Auto-UV profiles.",
            "Restores stock now and at boot afterwards."
            if safety.restore_stock
            else "",
        )

        def deleted(result: CommandResult) -> None:
            self.selected = [i for i in self.selected if i not in ids]
            self.notice = (result.message, "")
            if safety.restore_stock:
                # The profile is gone: restore stock rather than leave an
                # orphaned curve applied, once the delete command has ended.
                C.QTimer.singleShot(0, self.restore_defaults)

        self.source.delete_profiles(
            ids, self._finish("Delete selected profiles", deleted)
        )

    # ---------------- verify ----------------

    def verify(self, profile_id: str) -> None:
        profile = self.profile(profile_id)
        if self.busy() or profile is None or not profile_can_verify(profile):
            return
        refusal = model.verify_refusal(profile, self.target)
        if refusal:
            self.dialogs.error(self, "Profile verification", refusal)
            return
        label = profile_status_label(self.profiles, profile_id)
        minutes = self.dialogs.verify_minutes(self, label)
        if minutes is None:
            return
        if not self.ensure_daemon_ready("Verifying an Auto-UV profile"):
            return
        plan = self.source.curve_plan(profile_id)
        tier = model.tier_label(profile, self._winners()) or str(
            profile.get("profile_tier") or label
        )
        row = {
            "tier": tier,
            "mv": profile.get("candidate_voltage_mv"),
            "target": profile.get("lock_clock_mhz"),
            "meas": profile.get("avg_core_clock_mhz"),
            "points": [(p["voltage_mv"], p["target_mhz"]) for p in plan],
            "base_points": list(self.source.base_curve_points(profile_id)),
            "gpu_index": self.target.index,
            # The GPU this run verifies on; a legacy profile binds to it even
            # if the target changes before the run ends.
            "gpu_uuid": self.target.uuid,
        }
        self._verify_gpus[profile_id] = self.target.uuid
        self.verify_requested.emit(profile_id, label, row, int(minutes))

    def verification_finished(
        self, profile_id: str, passed: bool, stopped: bool = False
    ) -> None:
        """The verification host reports the end of a Profiles verification."""
        uuid = self._verify_gpus.pop(profile_id, self.target.uuid)
        self.source.finish_verification(profile_id, uuid, bool(passed))
        if passed:
            self.show_new_profile(profile_id)
            self.notice = (
                "Profile verification passed. It can be applied now.",
                "accent",
            )
        elif stopped:
            self.notice = (
                "Profile verification stopped. The profile is unchanged.",
                "",
            )
        else:
            self.notice = (
                "Profile verification failed. The profile is unchanged.",
                "loss",
            )
        self._render()

    # ---------------- edits ----------------

    def _saved_copy(self, new_id: str, message: str, tone: str = "") -> str:
        self.show_new_profile(new_id)
        self.notice = (message, tone)
        self._render()
        return message

    def edit_vf(self, profile_id: str) -> None:
        profile = self.profile(profile_id)
        if profile is None:
            return
        plan = self.source.curve_plan(profile_id)
        anchor = editable_anchor_from_profile(profile)
        if not plan or anchor is None:
            self.dialogs.info(
                self,
                "Edit VF Curve",
                "No editable V/F curve is available for this profile.",
            )
            return

        def save(edit) -> str:
            new_id = self.source.save_curve_edit(profile_id, edit)
            return self._saved_copy(
                new_id, "Saved an edited curve copy. Verify it before Apply."
            )

        self.editors.open_vf(
            self,
            plan=plan,
            anchor=anchor,
            base_points=self.source.base_curve_points(profile_id),
            control_voltage_mvs=manual_edit_control_voltage_mvs(
                profile.get("manual_edit")
            ),
            save=save,
        )

    def edit_fan(self, profile_id: str) -> None:
        curve = self.source.fan_curve(profile_id)
        if self.profile(profile_id) is None or not curve:
            self.dialogs.info(
                self,
                "Edit Fan Curve",
                "No editable fan curve is available for this profile.",
            )
            return

        def save(edit) -> str:
            new_id = self.source.save_fan_edit(profile_id, edit, curve)
            return self._saved_copy(
                new_id,
                "Saved an edited fan curve copy. It keeps the profile's verification.",
            )

        self.editors.open_fan(
            self,
            curve_points=curve,
            measured_points=self.source.fan_measurements(profile_id),
            target_point=self.source.fan_target_point(profile_id),
            save=save,
        )

    def edit_memory(self, profile_id: str) -> None:
        profile = self.profile(profile_id)
        current = (
            None if profile is None else editable_memory_offset_from_profile(profile)
        )
        if current is None:
            self.dialogs.info(
                self,
                "Edit Memory Offset",
                "No editable memory offset is available for this profile.",
            )
            return
        gpu = self.target.gpu
        if gpu is None:
            self.dialogs.error(
                self,
                "Edit Memory Offset",
                "Choose this profile's GPU before editing its memory offset.",
            )
            return
        low, high = self.source.memory_offset_range(gpu.uuid)

        def save(value: int) -> str:
            if int(value) == int(current):
                return ""
            new_id = self.source.save_memory_offset(profile_id, int(value))
            return self._saved_copy(
                new_id, "Saved an edited memory offset copy. Verify it before Apply."
            )

        self.dialogs.memory_offset(
            self, current=current, min_mhz=low, max_mhz=high, save=save
        )

    def export_lact(self, profile_id: str) -> None:
        profile = self.profile(profile_id)
        if self.busy() or profile is None or not model.is_applicable(profile):
            return
        directory = self.dialogs.lact_directory(self, self.source.lact_directories())
        if not directory:
            return
        result = self.source.export_lact(
            profile_id, directory, include_fan_curve=self.silent_fan
        )
        if result.error:
            self.dialogs.error(self, "Export LACT", result.error)
            return
        message = f"LACT profile successfully written:\n{result.path}"
        if result.warnings:
            message += "\n\nWarnings:\n" + "\n".join(result.warnings)
        self.notice = (f"LACT profile written: {result.path}", "")
        self._render()
        self.dialogs.lact_result(self, result, message)

    def assign_tier(self, profile_id: str, tier: str) -> None:
        if self.busy():
            return
        self.source.assign_tier(profile_id, tier)
        self.reload()

    # ---------------- Afterburner ----------------

    def run_afterburner_import(self) -> str | None:
        """Import Afterburner: the classic chooser, then save the chosen slot
        as a profile row (ui/features/integrations/afterburner_workflow)."""
        if self.busy():
            return self.import_afterburner({})
        try:
            entry = self.dialogs.afterburner_entry(self)
        except Exception as exc:  # noqa: BLE001 - the chooser parses user files
            self.dialogs.error(
                self, "Import Afterburner", f"Afterburner import failed:\n{exc}"
            )
            return None
        if not entry:
            return None
        return self.import_afterburner(entry)

    def import_afterburner(
        self, entry: dict, fan_payload: dict | None = None
    ) -> str | None:
        """Save an entry picked in the real Afterburner import dialog."""
        if self.busy():
            self.notice = (
                "Finish the current PenguinBurner action before importing Afterburner.",
                "warning",
            )
            self._render()
            return None
        try:
            result = self.source.save_afterburner_import(entry, fan_payload)
        except Exception as exc:  # noqa: BLE001
            self.dialogs.error(
                self, "Import Afterburner", f"Afterburner import failed:\n{exc}"
            )
            return None
        message = (
            f"Imported Afterburner profile {result['section']} from "
            f"{Path(result['device_profile_relative_path']).name}."
        )
        profile_id = str(result.get("profile_id", ""))
        self._saved_copy(profile_id, message, "accent")
        self.dialogs.info(self, "Import Afterburner", message)
        return profile_id
