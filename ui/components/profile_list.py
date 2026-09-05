from __future__ import annotations

from datetime import datetime

from profiles.uv.profile_store import profile_display_name
from profiles.gpu_identity import (
    profile_gpu_compatibility,
    profile_gpu_label,
    profile_gpu_uuid,
)
from profiles.gpu_identity import GPU_COMPATIBILITY_LEGACY, GPU_COMPATIBILITY_MATCH
from profiles.uv.profile_tiers import (
    normalize_profile_tier,
    resolve_profile_tier_profiles,
)
from .. import theme


GOOD_DELTA_COLOR = theme.GOOD
BAD_DELTA_COLOR = theme.ERROR
PROFILE_SORT_ROLE = 261
PROFILE_SORTABLE_COLUMNS = frozenset({0, 2, 3, 4, 5, 6, 7, 8})


class ProfileList:
    COLUMNS = [
        "Date",
        "Profile",
        "GPU",
        "mV",
        "Target MHz",
        "Effective MHz",
        "FPS/W",
        "FPS",
        "Power W",
        "Mem",
        "Tier",
        "Source",
    ]
    DATE_COLUMN = 0
    PROFILE_COLUMN = 1
    GPU_COLUMN = 2
    VOLTAGE_COLUMN = 3
    TARGET_MHZ_COLUMN = 4
    EFFECTIVE_MHZ_COLUMN = 5
    FPSW_COLUMN = 6
    FPS_COLUMN = 7
    POWER_COLUMN = 8
    MEMORY_OFFSET_COLUMN = 9
    SOURCE_COLUMN = 11
    PROFILE_ID_ROLE = 257
    CANDIDATE_ID_ROLE = 258
    PROFILE_PATH_ROLE = 259
    PROFILE_DELETABLE_ROLE = 260
    PROFILE_APPLY_ROLE = 262
    PROFILE_GPU_UUID_ROLE = 263
    PROFILE_VERIFIED_ROLE = 264
    SORT_VALUE_ROLE = PROFILE_SORT_ROLE
    SORTABLE_COLUMNS = PROFILE_SORTABLE_COLUMNS

    def __init__(self, *, QtCore, QtGui, QtWidgets):
        self.QtCore = QtCore
        self.QtGui = QtGui
        self.QtWidgets = QtWidgets
        self._item_class = _sortable_item_class(QtWidgets, self.SORT_VALUE_ROLE)
        self._runtime_actions_available = True
        self._gpu_choices: list[object] = []
        self._target_gpu_uuid = ""
        self._target_gpu_index: int | None = None
        self._target_selection_required = False
        self._main_gpu_target_has_boot_profile = False
        self.on_target_gpu_changed = None
        self._sort_column = self.DATE_COLUMN
        self._sort_order = QtCore.Qt.DescendingOrder
        self.widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        top = QtWidgets.QHBoxLayout()
        self.target_gpu_label = QtWidgets.QLabel("Target GPU")
        self.target_gpu_label.setObjectName("profileTargetGpuLabel")
        self.target_gpu_combo = QtWidgets.QComboBox()
        self.target_gpu_combo.setObjectName("profileTargetGpu")
        self.target_gpu_combo.setToolTip(
            "Choose which GPU this Profiles action targets. Changing this selection "
            "does not modify hardware settings."
        )
        self.target_gpu_label.setVisible(False)
        self.target_gpu_combo.setVisible(False)
        self.silent_fan_checkbox = QtWidgets.QCheckBox("Silent fan curve")
        self.main_gpu_checkbox = QtWidgets.QCheckBox("Main GPU")
        self.main_gpu_checkbox.setObjectName("profileMainGpu")
        self.main_gpu_checkbox.setVisible(False)
        self.boot_apply_checkbox = QtWidgets.QCheckBox("Apply on startup")
        self.boot_apply_checkbox.setToolTip(
            "When ticked, Apply also saves the profile for this GPU at boot. "
            "Unticking clears this GPU's saved boot profile immediately, and "
            "Apply then changes only the current session."
        )
        self.daemonize_button = QtWidgets.QPushButton("Apply")
        self.daemonize_button.setToolTip(
            "Apply the single selected profile now. \"Apply on startup\" "
            "controls whether it also becomes the boot profile."
        )
        self.delete_button = QtWidgets.QToolButton()
        self.delete_button.setObjectName("deleteProfilesButton")
        self.delete_button.setIcon(_standard_trash_icon(QtWidgets, self.widget))
        self.delete_button.setIconSize(QtCore.QSize(18, 18))
        self.delete_button.setToolTip("Delete Selected Profiles")
        self.delete_button.setAccessibleName("Delete Selected Profiles")
        self.restore_defaults_button = QtWidgets.QPushButton("Restore defaults")
        self.restore_defaults_button.setToolTip(
            "Reset the GPU to stock now and at boot: clear core and memory "
            "offsets, release locked clocks, restore the factory V/F curve, "
            "and restore the default power limit."
        )
        top.addWidget(QtWidgets.QLabel("Stored undervolt profiles"))
        top.addWidget(self.target_gpu_label)
        top.addWidget(self.target_gpu_combo)
        top.addWidget(self.main_gpu_checkbox)
        top.addStretch(1)
        top.addWidget(self.silent_fan_checkbox)
        top.addWidget(self.boot_apply_checkbox)
        top.addWidget(self.daemonize_button)
        top.addWidget(self.delete_button)
        top.addWidget(self.restore_defaults_button)

        self.table = QtWidgets.QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        # Size columns to their contents so the table fits the viewport width;
        # the free-text columns (Profile, Source) take up any remaining slack.
        # A horizontal scrollbar only appears when the window is genuinely too
        # narrow for the content, rather than always.
        header.setMinimumSectionSize(48)
        for column in range(len(self.COLUMNS)):
            header.setSectionResizeMode(
                column, QtWidgets.QHeaderView.ResizeToContents
            )
        header.setSectionResizeMode(
            self.PROFILE_COLUMN, QtWidgets.QHeaderView.Stretch
        )
        header.setSectionResizeMode(
            self.SOURCE_COLUMN, QtWidgets.QHeaderView.Stretch
        )
        header.setHighlightSections(False)
        header_font = header.font()
        header_font.setBold(False)
        header.setFont(header_font)
        header.setSectionsClickable(True)
        self._apply_sort_indicator(self._sort_column, self._sort_order)
        header.sectionClicked.connect(self._sort_by_header_column)

        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        adaptive_note = QtWidgets.QLabel(
            "Apply changes the current session; tick \"Apply on startup\" to "
            "also save the profile for boot. Restore defaults makes stock "
            "the current and boot state. Per-game adaptive profiles with a "
            "target pre-frame-gen FPS are managed in the Game Library tab."
        )
        adaptive_note.setObjectName("profilesAdaptiveNote")
        adaptive_note.setWordWrap(True)
        layout.addWidget(adaptive_note)
        self.table.itemSelectionChanged.connect(self._sync_action_state)
        self.target_gpu_combo.currentIndexChanged.connect(self._target_gpu_changed)
        self._sync_profile_filter()
        self._sync_action_state()

    def set_profiles(
        self,
        profiles: list[dict],
        *,
        preferred_candidate_id: str = "",
        preferred_profile_id: str = "",
        select_preferred: bool = False,
        silent_fan_checked: bool | None = None,
        preserve_silent_fan_toggle: bool = True,
    ) -> None:
        selected_profile_ids = self.selected_profile_ids()
        silent_fan_checked_before = self.silent_fan_checkbox.isChecked()
        sort_column = self._active_sort_column()
        sort_order = self._sort_order
        profiles = _promote_preferred_profile(
            profiles,
            preferred_candidate_id=preferred_candidate_id,
            preferred_profile_id=preferred_profile_id,
        )
        table_signals_blocked = self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            tier_winner_ids = _resolved_tier_winner_ids(
                profiles,
                include_legacy_profiles=self.single_physical_gpu(),
            )
            for profile in profiles:
                row = self.table.rowCount()
                self.table.insertRow(row)
                profile_id = str(profile.get("profile_id", ""))
                candidate_id = str(profile.get("candidate_id", ""))
                profile_path = str(profile.get("path", ""))
                is_deletable = bool(profile_path) or str(
                    profile.get("runtime_source", "")
                ) == "afterburner"
                is_applicable = bool(profile.get("final_verified", False)) or str(
                    profile.get("runtime_source", "")
                ) == "afterburner"
                is_preferred = row == 0 and _profile_matches_preference(
                    profile,
                    preferred_candidate_id=preferred_candidate_id,
                    preferred_profile_id=preferred_profile_id,
                )
                values = [
                    _date_text(profile),
                    _profile_name(profile),
                    profile_gpu_label(profile),
                    _format_profile_metric_with_delta(
                        profile.get("candidate_voltage_mv"),
                        _profile_base_metric(profile, "candidate_voltage_mv"),
                        precision=0,
                        lower_is_better=True,
                    ),
                    _format_number(profile.get("lock_clock_mhz"), precision=0),
                    _format_profile_metric_with_delta(
                        profile.get("avg_core_clock_mhz"),
                        _profile_base_metric(profile, "avg_core_clock_mhz"),
                        precision=2,
                    ),
                    _format_profile_metric_with_delta(
                        profile.get("efficiency_fps_per_w"),
                        _profile_base_metric(profile, "efficiency_fps_per_w"),
                        precision=2,
                    ),
                    _format_profile_metric_with_delta(
                        profile.get("avg_fps"),
                        _profile_base_metric(profile, "avg_fps"),
                        precision=2,
                    ),
                    _format_profile_metric_with_delta(
                        profile.get("avg_power_w"),
                        _profile_base_metric(profile, "avg_power_w"),
                        precision=2,
                        lower_is_better=True,
                    ),
                    _format_signed_memory_clock(
                        profile.get("memory_offset_mhz"),
                    ),
                    _profile_tier_label(profile, tier_winner_ids),
                    _profile_source_label(profile),
                ]
                sort_values = _profile_sort_values(profile)
                for column, value in enumerate(values):
                    item = self._item_class(str(value))
                    item.setData(self.PROFILE_ID_ROLE, profile_id)
                    item.setData(self.CANDIDATE_ID_ROLE, candidate_id)
                    item.setData(self.PROFILE_PATH_ROLE, profile_path)
                    item.setData(self.PROFILE_DELETABLE_ROLE, bool(is_deletable))
                    item.setData(self.PROFILE_APPLY_ROLE, bool(is_applicable))
                    item.setData(
                        self.PROFILE_GPU_UUID_ROLE,
                        str(
                            (profile.get("gpu_identity") or {}).get("uuid") or ""
                            if isinstance(profile.get("gpu_identity"), dict)
                            else ""
                        ).strip(),
                    )
                    item.setData(self.PROFILE_VERIFIED_ROLE, bool(is_applicable))
                    item.setData(self.SORT_VALUE_ROLE, sort_values[column])
                    if (
                        column in self.SORTABLE_COLUMNS - {self.DATE_COLUMN}
                        or column == self.MEMORY_OFFSET_COLUMN
                    ):
                        item.setTextAlignment(
                            self.QtCore.Qt.AlignRight | self.QtCore.Qt.AlignVCenter
                        )
                    if column == self.VOLTAGE_COLUMN:
                        _paint_profile_delta_item(
                            item,
                            self.QtGui,
                            profile.get("candidate_voltage_mv"),
                            _profile_base_metric(profile, "candidate_voltage_mv"),
                            label="mV",
                            lower_is_better=True,
                        )
                    if column == self.EFFECTIVE_MHZ_COLUMN:
                        _paint_profile_delta_item(
                            item,
                            self.QtGui,
                            profile.get("avg_core_clock_mhz"),
                            _profile_base_metric(profile, "avg_core_clock_mhz"),
                            label="Effective MHz",
                        )
                    if column == self.FPSW_COLUMN:
                        _paint_profile_delta_item(
                            item,
                            self.QtGui,
                            profile.get("efficiency_fps_per_w"),
                            _profile_base_metric(profile, "efficiency_fps_per_w"),
                            label="FPS/W",
                        )
                    if column == self.FPS_COLUMN:
                        _paint_profile_delta_item(
                            item,
                            self.QtGui,
                            profile.get("avg_fps"),
                            _profile_base_metric(profile, "avg_fps"),
                            label="FPS",
                        )
                    if column == self.POWER_COLUMN:
                        _paint_profile_delta_item(
                            item,
                            self.QtGui,
                            profile.get("avg_power_w"),
                            _profile_base_metric(profile, "avg_power_w"),
                            label="Power W",
                            lower_is_better=True,
                        )
                    if is_preferred:
                        item.setBackground(self.QtGui.QColor(theme.PROFILE_SELECTED_BG))
                    self.table.setItem(row, column, item)
            self._sort_table(sort_column, sort_order)
            should_preserve_selection = _should_preserve_selection(
                selected_profile_ids,
                preferred_candidate_id=preferred_candidate_id,
                preferred_profile_id=preferred_profile_id,
            )
            if (
                should_preserve_selection
                and selected_profile_ids
                and self.select_profiles(selected_profile_ids)
            ):
                pass
            elif select_preferred and preferred_profile_id:
                self.select_profile(preferred_profile_id)
            elif (
                select_preferred
                and preferred_candidate_id
                and self.select_candidate(preferred_candidate_id)
            ):
                pass
        finally:
            self.table.blockSignals(table_signals_blocked)
        should_preserve_silent_fan_toggle = (
            preserve_silent_fan_toggle
            and _should_preserve_single_selection_toggle(
                selected_profile_ids,
                self.selected_profile_ids(),
            )
        )
        if should_preserve_silent_fan_toggle:
            self._set_silent_fan_checked(silent_fan_checked_before)
        elif silent_fan_checked is not None:
            self._set_silent_fan_checked(bool(silent_fan_checked))
        self._sync_action_state()

    def configure_gpu_targets(
        self,
        profiles: list[dict],
        choices: list[object],
        *,
        preferred_index: int | None = None,
    ) -> None:
        previous_uuid = self._target_gpu_uuid
        choices_with_stable_identity = [
            choice
            for choice in choices
            if str(getattr(choice, "uuid", "") or "").strip()
        ]
        # gpu_choices_with_fallback() may append a UUID-less placeholder for
        # a configured index that is no longer present. It is useful to other
        # index-based workflows, but must not turn this UUID-bound selector
        # into a fake multi-GPU choice. Keep one placeholder only when GPU
        # discovery produced no stable identity at all.
        self._gpu_choices = (
            choices_with_stable_identity
            if choices_with_stable_identity
            else list(choices[:1])
        )
        choice_by_uuid = {
            str(getattr(choice, "uuid", "") or "").strip().casefold(): choice
            for choice in self._gpu_choices
            if str(getattr(choice, "uuid", "") or "").strip()
        }
        self._target_selection_required = len(self._gpu_choices) >= 2

        target_uuid = ""
        if self._target_selection_required:
            preferred_choice = next(
                (
                    choice
                    for choice in self._gpu_choices
                    if preferred_index is not None
                    and int(getattr(choice, "index", -1)) == int(preferred_index)
                ),
                None,
            )
            preferred_uuid = str(getattr(preferred_choice, "uuid", "") or "").strip()
            if preferred_uuid:
                target_uuid = preferred_uuid
            elif previous_uuid.casefold() in choice_by_uuid:
                target_uuid = previous_uuid
        elif len(self._gpu_choices) == 1:
            target_uuid = str(getattr(self._gpu_choices[0], "uuid", "") or "").strip()

        target_choice = choice_by_uuid.get(target_uuid.casefold())
        if target_choice is None and not target_uuid and len(self._gpu_choices) == 1:
            target_choice = self._gpu_choices[0]
        self._target_gpu_uuid = target_uuid
        self._target_gpu_index = (
            int(getattr(target_choice, "index")) if target_choice is not None else None
        )
        self._populate_target_gpu_combo(target_uuid)
        self.set_main_gpu_state(checked=False, has_boot_profile=False)
        self._sync_target_gpu_presentation()
        self._sync_profile_filter()
        self._sync_action_state()

    def target_gpu_index(self) -> int | None:
        return self._target_gpu_index

    def target_gpu_uuid(self) -> str:
        return self._target_gpu_uuid

    def target_selection_required(self) -> bool:
        return self._target_selection_required

    def single_physical_gpu(self) -> bool:
        return len(self._gpu_choices) == 1

    def profile_matches_target(self, profile: dict) -> bool:
        compatibility = profile_gpu_compatibility(profile, self._target_gpu_uuid)
        if compatibility == GPU_COMPATIBILITY_MATCH:
            return self._target_gpu_index is not None
        if compatibility == GPU_COMPATIBILITY_LEGACY:
            return len(self._gpu_choices) == 1 and self._target_gpu_index is not None
        return False

    def _populate_target_gpu_combo(self, target_uuid: str) -> None:
        blocked = self.target_gpu_combo.blockSignals(True)
        try:
            self.target_gpu_combo.clear()
            if len(self._gpu_choices) >= 2:
                self.target_gpu_combo.addItem("Choose target GPU...", "")
            selected_combo_index = 0
            for choice in self._gpu_choices:
                uuid = str(getattr(choice, "uuid", "") or "").strip()
                if not uuid:
                    continue
                label = str(getattr(choice, "label", "") or "").strip()
                self.target_gpu_combo.addItem(label or uuid, uuid)
                if uuid.casefold() == target_uuid.casefold():
                    selected_combo_index = self.target_gpu_combo.count() - 1
            self.target_gpu_combo.setCurrentIndex(selected_combo_index)
        finally:
            self.target_gpu_combo.blockSignals(blocked)

    def _target_gpu_changed(self, _combo_index: int) -> None:
        uuid = str(self.target_gpu_combo.currentData() or "").strip()
        choice = next(
            (
                item
                for item in self._gpu_choices
                if str(getattr(item, "uuid", "") or "").strip().casefold()
                == uuid.casefold()
            ),
            None,
        )
        self._target_gpu_uuid = uuid
        self._target_gpu_index = (
            int(getattr(choice, "index")) if choice is not None else None
        )
        self.set_main_gpu_state(checked=False, has_boot_profile=False)
        self._sync_target_gpu_presentation()
        self._sync_profile_filter()
        self._sync_action_state()
        if callable(self.on_target_gpu_changed):
            self.on_target_gpu_changed(self._target_gpu_index, self._target_gpu_uuid)

    def _sync_target_gpu_presentation(self) -> None:
        visible = bool(self._gpu_choices)
        selectable = len(self._gpu_choices) >= 2
        self.target_gpu_label.setVisible(visible)
        self.target_gpu_label.setEnabled(selectable)
        self.target_gpu_combo.setVisible(visible)
        self.target_gpu_combo.setEnabled(selectable)
        self.main_gpu_checkbox.setVisible(selectable)
        self.target_gpu_combo.setToolTip(
            "Only one GPU detected."
            if len(self._gpu_choices) == 1
            else "Choose which GPU this Profiles action targets. Changing this "
            "selection does not modify hardware settings."
        )
        if self._target_selection_required and self._target_gpu_index is not None:
            self.daemonize_button.setText(f"Apply to GPU {self._target_gpu_index}")
        else:
            self.daemonize_button.setText("Apply")

    def _sync_profile_filter(self) -> None:
        target_uuid = self._target_gpu_uuid.casefold()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            profile_uuid = (
                ""
                if item is None
                else str(item.data(self.PROFILE_GPU_UUID_ROLE) or "").strip().casefold()
            )
            hidden = bool(
                target_uuid and profile_uuid and profile_uuid != target_uuid
            )
            self.table.setRowHidden(row, hidden)
            if hidden:
                for column in range(self.table.columnCount()):
                    cell = self.table.item(row, column)
                    if cell is not None:
                        cell.setSelected(False)

    def _active_sort_column(self) -> int:
        column = int(self._sort_column)
        if column not in self.SORTABLE_COLUMNS:
            return 0
        return column

    def _sort_by_header_column(self, column: int) -> None:
        column = int(column)
        if column not in self.SORTABLE_COLUMNS:
            self._apply_sort_indicator(self._sort_column, self._sort_order)
            return
        if int(self._sort_column) == column:
            order = (
                self.QtCore.Qt.AscendingOrder
                if self._sort_order == self.QtCore.Qt.DescendingOrder
                else self.QtCore.Qt.DescendingOrder
            )
        else:
            order = (
                self.QtCore.Qt.DescendingOrder
                if column == 0
                else self.QtCore.Qt.AscendingOrder
            )
        self._sort_column = column
        self._sort_order = order
        self._apply_sort_indicator(column, order)
        self._sort_table(column, order)
        self._sync_action_state()

    def _apply_sort_indicator(self, column: int, order) -> None:
        header = self.table.horizontalHeader()
        signals_blocked = header.blockSignals(True)
        try:
            header.setSortIndicatorShown(True)
            header.setSortIndicator(int(column), order)
        finally:
            header.blockSignals(signals_blocked)

    def _sort_table(self, column: int, order) -> None:
        column = int(column)
        if column not in self.SORTABLE_COLUMNS:
            return
        self._sort_column = column
        self._sort_order = order
        self._apply_sort_indicator(column, order)
        self._item_class.sort_descending = order == self.QtCore.Qt.DescendingOrder
        self.table.sortItems(column, order)

    def selected_profile_id(self) -> str:
        profile_ids = self.selected_profile_ids()
        if not profile_ids:
            return ""
        return profile_ids[-1]

    def selected_profile_ids(self) -> list[str]:
        return self._selected_row_values(self.PROFILE_ID_ROLE)

    def selected_profile_paths(self) -> list[str]:
        return self._selected_row_values(self.PROFILE_PATH_ROLE)

    def selected_profile_names(self) -> list[str]:
        names = []
        for row in self._selected_rows():
            item = self.table.item(row, 1)
            if item is not None and str(item.text()).strip():
                names.append(str(item.text()).strip())
        return names

    def silent_fan_enabled(self) -> bool:
        return bool(self.silent_fan_checkbox.isChecked())

    def selected_profile_name(self) -> str:
        rows = self._selected_rows()
        if not rows:
            return ""
        row = rows[-1]
        item = self.table.item(row, 1)
        return "" if item is None else str(item.text())

    def set_runtime_actions_enabled(self, enabled: bool) -> None:
        self._runtime_actions_available = bool(enabled)
        self._sync_action_state()

    def select_profile(self, profile_id: str) -> None:
        self.select_profiles([profile_id])

    def select_profiles(self, profile_ids: list[str]) -> bool:
        wanted = {str(profile_id) for profile_id in profile_ids if str(profile_id)}
        if not wanted:
            return False
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return False
        selection_model.clearSelection()
        flags = _selection_flags(
            self.QtCore,
            "Select",
            "Rows",
        )
        matched = False
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            profile_id = "" if item is None else str(item.data(self.PROFILE_ID_ROLE) or "")
            if profile_id not in wanted:
                continue
            selection_model.select(self.table.model().index(row, 0), flags)
            matched = True
        return matched

    def select_candidate(self, candidate_id: str) -> bool:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and str(
                item.data(self.CANDIDATE_ID_ROLE) or ""
            ) == str(candidate_id):
                self.table.selectRow(row)
                return True
        return False

    def _sync_action_state(self) -> None:
        selected_ids = self.selected_profile_ids()
        has_single_selection = self._runtime_actions_available and len(
            selected_ids
        ) == 1
        selected_rows = self._selected_rows()
        has_apply_selection = (
            has_single_selection
            and len(selected_rows) == 1
            and self._row_is_applicable(selected_rows[0])
        )
        has_delete_selection = (
            self._runtime_actions_available
            and bool(selected_rows)
            and all(self._row_is_deletable(row) for row in selected_rows)
        )
        self.daemonize_button.setEnabled(has_apply_selection)
        self.boot_apply_checkbox.setEnabled(
            self._runtime_actions_available and self._target_gpu_index is not None
        )
        self.main_gpu_checkbox.setEnabled(
            self._runtime_actions_available
            and self._target_selection_required
            and self._target_gpu_index is not None
            and self._main_gpu_target_has_boot_profile
        )
        self.delete_button.setEnabled(has_delete_selection)
        self.restore_defaults_button.setEnabled(
            self._runtime_actions_available and self._target_gpu_index is not None
        )

    def _set_silent_fan_checked(self, checked: bool) -> None:
        signals_blocked = self.silent_fan_checkbox.blockSignals(True)
        try:
            self.silent_fan_checkbox.setChecked(bool(checked))
        finally:
            self.silent_fan_checkbox.blockSignals(signals_blocked)

    def set_silent_fan_checked(self, checked: bool) -> None:
        """Re-assert the silent-fan tick without firing the persist signal.

        Used by the scan-completion path to restore the pre-scan intent
        before the auto-apply reads the checkbox.
        """
        self._set_silent_fan_checked(checked)

    def persist_on_startup_enabled(self) -> bool:
        return bool(self.boot_apply_checkbox.isChecked())

    def set_boot_apply_checked(self, checked: bool) -> None:
        """Update the selected GPU's boot state without firing persistence."""
        signals_blocked = self.boot_apply_checkbox.blockSignals(True)
        try:
            self.boot_apply_checkbox.setChecked(bool(checked))
        finally:
            self.boot_apply_checkbox.blockSignals(signals_blocked)

    def set_main_gpu_state(
        self,
        *,
        checked: bool,
        has_boot_profile: bool,
    ) -> None:
        """Render the selected NVIDIA GPU's explicit boot-owner state."""
        self._main_gpu_target_has_boot_profile = bool(has_boot_profile)
        signals_blocked = self.main_gpu_checkbox.blockSignals(True)
        try:
            self.main_gpu_checkbox.setChecked(bool(checked))
        finally:
            self.main_gpu_checkbox.blockSignals(signals_blocked)
        if self._target_gpu_index is None:
            tooltip = "Choose a target NVIDIA GPU first."
        elif not has_boot_profile:
            tooltip = (
                "Apply a profile with Apply on startup first, then mark this "
                "NVIDIA GPU as the daemon's main monitored GPU."
            )
        elif checked:
            tooltip = (
                "This NVIDIA GPU owns daemon monitoring after boot. Untick to "
                "restore the default last-saved-GPU behavior."
            )
        else:
            tooltip = (
                "Make this NVIDIA GPU own daemon monitoring after boot. Manual "
                "Apply can still move monitoring for the current session."
            )
        self.main_gpu_checkbox.setToolTip(tooltip)
        self._sync_action_state()

    def main_gpu_target_has_boot_profile(self) -> bool:
        return bool(self._main_gpu_target_has_boot_profile)

    def _selected_rows(self) -> list[int]:
        selection_model = self.table.selectionModel()
        if selection_model is None:
            return []
        return sorted({int(index.row()) for index in selection_model.selectedRows()})

    def _selected_row_values(self, role: int) -> list[str]:
        values = []
        seen = set()
        for row in self._selected_rows():
            item = self.table.item(row, 0)
            value = "" if item is None else str(item.data(role) or "").strip()
            if not value or value in seen:
                continue
            values.append(value)
            seen.add(value)
        return values

    def _row_is_deletable(self, row: int) -> bool:
        item = self.table.item(int(row), 0)
        return bool(item is not None and item.data(self.PROFILE_DELETABLE_ROLE))

    def _row_is_applicable(self, row: int) -> bool:
        item = self.table.item(int(row), 0)
        if item is None or not item.data(self.PROFILE_VERIFIED_ROLE):
            return False
        profile_uuid = str(item.data(self.PROFILE_GPU_UUID_ROLE) or "").strip()
        if profile_uuid:
            return bool(
                self._target_gpu_index is not None
                and profile_uuid.casefold() == self._target_gpu_uuid.casefold()
            )
        if not self._gpu_choices and not self._target_selection_required:
            return True
        return bool(len(self._gpu_choices) == 1 and self._target_gpu_index is not None)


def _standard_trash_icon(QtWidgets, widget):
    style = widget.style()
    standard_pixmap = getattr(QtWidgets.QStyle, "StandardPixmap", QtWidgets.QStyle)
    return style.standardIcon(getattr(standard_pixmap, "SP_TrashIcon"))


def _sortable_item_class(QtWidgets, sort_role: int):
    class SortableTableWidgetItem(QtWidgets.QTableWidgetItem):
        sort_descending = False

        def __lt__(self, other):
            other_value = (
                other.data(sort_role) if hasattr(other, "data") else str(other.text())
            )
            return _sort_value_less(
                self.data(sort_role),
                other_value,
                descending=bool(self.sort_descending),
            )

    return SortableTableWidgetItem


def _sort_value_less(left, right, *, descending: bool = False) -> bool:
    left_missing = left in (None, "")
    right_missing = right in (None, "")
    if left_missing != right_missing:
        return left_missing if descending else right_missing
    if left_missing and right_missing:
        return False
    left_key = _sort_key(left)
    right_key = _sort_key(right)
    return left_key < right_key


def _sort_key(value) -> tuple[int, float, str]:
    number = _to_float(value)
    if number is not None:
        return (0, float(number), "")
    return (1, 0.0, str(value).casefold())


def _selection_flags(QtCore, *names: str):
    enum = getattr(QtCore.QItemSelectionModel, "SelectionFlag", QtCore.QItemSelectionModel)
    value = None
    for name in names:
        flag = getattr(enum, name)
        value = flag if value is None else value | flag
    return value


def _should_preserve_selection(
    selected_profile_ids: list[str],
    *,
    preferred_candidate_id: str = "",
    preferred_profile_id: str = "",
) -> bool:
    return bool(selected_profile_ids)


def _should_preserve_single_selection_toggle(
    previous_profile_ids: list[str],
    current_profile_ids: list[str],
) -> bool:
    return (
        len(previous_profile_ids) == 1
        and len(current_profile_ids) == 1
        and list(previous_profile_ids) == list(current_profile_ids)
    )


def _date_text(profile: dict) -> str:
    value = str(
        profile.get("profile_created_at") or profile.get("verified_at") or ""
    ).strip()
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value[:19].replace("T", " ")


def _format_number(value, *, precision: int) -> str:
    if value in (None, ""):
        return ""
    number = _to_float(value)
    if number is None:
        return ""
    precision = max(0, min(int(precision), 2))
    if precision <= 0:
        return str(int(round(number)))
    return f"{number:.{precision}f}"


def _format_signed_number(value, *, precision: int) -> str:
    text = _format_number(value, precision=precision)
    if not text:
        return ""
    number = _to_float(value)
    if number is None:
        return text
    if abs(number) < 0.5:
        return "0"
    return f"+{text}" if number > 0 else text


def _format_signed_memory_clock(value) -> str:
    # The stored memory offset is an NVML transfer-rate value (MT/s); the
    # realized memory clock moves by half of it (verified on Blackwell,
    # issue #20). Show the memory-clock MHz, matching the Auto-UV dialog.
    number = _to_float(value)
    if number is None:
        return ""
    text = _format_signed_number(number / 2, precision=0)
    if not text:
        return ""
    return f"{text} MHz"


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metric_delta_percent(
    current,
    baseline,
) -> float | None:
    current_number = _to_float(current)
    baseline_number = _to_float(baseline)
    if current_number is None or baseline_number in (None, 0.0):
        return None
    return (
        (current_number - float(baseline_number)) / float(baseline_number)
    ) * 100.0


def _format_profile_metric_with_delta(
    current,
    baseline,
    *,
    precision: int,
    lower_is_better: bool = False,
) -> str:
    value_text = _format_number(current, precision=precision)
    if not value_text:
        return ""
    delta = _metric_delta_percent(
        current,
        baseline,
    )
    if delta is None:
        return value_text
    return f"{value_text} ({delta:+.2f}%)"


def _profile_metric_delta_color(
    current,
    baseline,
    *,
    lower_is_better: bool = False,
) -> str:
    delta = _metric_delta_percent(
        current,
        baseline,
    )
    if delta is None:
        return ""
    improved = delta < -0.005 if lower_is_better else delta > 0.005
    regressed = delta > 0.005 if lower_is_better else delta < -0.005
    if improved:
        return GOOD_DELTA_COLOR
    if regressed:
        return BAD_DELTA_COLOR
    return ""


def _paint_profile_delta_item(
    item,
    QtGui,
    current,
    baseline,
    *,
    label: str,
    lower_is_better: bool = False,
) -> None:
    color = _profile_metric_delta_color(
        current,
        baseline,
        lower_is_better=lower_is_better,
    )
    delta = _metric_delta_percent(
        current,
        baseline,
    )
    if delta is None:
        return
    if color:
        item.setForeground(QtGui.QColor(color))
    base_text = _format_number(baseline, precision=2)
    if lower_is_better:
        comparison_text = "lower" if delta < 0.0 else "higher"
        if label == "Power W" and delta < 0.0:
            comparison_text = "saved"
        item.setToolTip(
            f"{label} {delta:+.2f}% {comparison_text} vs base {base_text}"
        )
    else:
        item.setToolTip(f"{label} {delta:+.2f}% vs base {base_text}")


def _profile_base_metric(profile: dict, metric: str):
    lookup = {
        "candidate_voltage_mv": (
            "base_candidate_voltage_mv",
            "baseline_candidate_voltage_mv",
            "baseline_voltage_mv",
            "base_voltage_mv",
        ),
        "avg_core_clock_mhz": (
            "base_avg_core_clock_mhz",
            "baseline_avg_core_clock_mhz",
            "baseline_core_clock_mhz",
        ),
        "efficiency_fps_per_w": (
            "base_efficiency_fps_per_w",
            "baseline_efficiency_fps_per_w",
            "baseline_fps_per_w",
        ),
        "avg_power_w": (
            "base_avg_power_w",
            "baseline_avg_power_w",
            "baseline_power_w",
        ),
        "avg_fps": (
            "base_avg_fps",
            "baseline_avg_fps",
            "baseline_fps",
        ),
    }
    for key in lookup.get(str(metric), ()):
        value = profile.get(key)
        if _to_float(value) is not None:
            return value
    return None


def _date_sort_value(profile: dict) -> float:
    value = str(
        profile.get("profile_created_at") or profile.get("verified_at") or ""
    ).strip()
    if not value:
        return 0.0
    try:
        return float(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return 0.0


def _profile_value_sort_value(profile: dict, metric: str) -> float | str:
    value = _to_float(profile.get(metric))
    return "" if value is None else float(value)


def _profile_sort_values(profile: dict) -> list[float | str]:
    return [
        _date_sort_value(profile),
        "",
        profile_gpu_label(profile).casefold(),
        _profile_value_sort_value(profile, "candidate_voltage_mv"),
        _profile_value_sort_value(profile, "lock_clock_mhz"),
        _profile_value_sort_value(profile, "avg_core_clock_mhz"),
        _profile_value_sort_value(profile, "efficiency_fps_per_w"),
        _profile_value_sort_value(profile, "avg_fps"),
        _profile_value_sort_value(profile, "avg_power_w"),
        "",
        "",
        "",
    ]


def _profile_source_label(profile: dict) -> str:
    source = str(profile.get("profile_source", "")).strip()
    labels = {
        "auto-uv-final": "Auto UV",
        "user-edited": "User edited",
    }
    return labels.get(source, source)


def _resolved_tier_winner_ids(
    profiles: list[dict],
    *,
    include_legacy_profiles: bool = False,
) -> dict[str, str]:
    # Adaptive mode collapses every tier down to a single profile (see
    # resolve_profile_tier_profiles); the table mirrors that so each tier label
    # appears on exactly one row -- the profile adaptive mode would actually
    # pick. Superseded duplicates fall back to a blank tier in the UI.
    winners: dict[str, str] = {}
    gpu_uuids = {profile_gpu_uuid(profile) for profile in profiles}
    if include_legacy_profiles and gpu_uuids - {""}:
        # A single-GPU host resolves legacy and bound profiles as one merged
        # population (the game-launch rule), so the tier label must land on
        # the row adaptive would actually pick regardless of its binding.
        gpu_uuids -= {""}
    for gpu_uuid in gpu_uuids:
        resolved = resolve_profile_tier_profiles(
            profiles,
            gpu_uuid=gpu_uuid,
            include_legacy_profiles=include_legacy_profiles,
        )
        for tier, profile in resolved.items():
            if not isinstance(profile, dict):
                continue
            profile_id = str(profile.get("profile_id") or "").strip()
            if not profile_id:
                continue
            winners[f"{gpu_uuid}\0{tier}" if gpu_uuid else tier] = profile_id
            if include_legacy_profiles and gpu_uuid:
                # Legacy rows look up by bare tier; mirror the merged winner
                # so the superseded population blanks consistently.
                winners[tier] = profile_id
    return winners


def _profile_tier_label(
    profile: dict,
    tier_winner_ids: dict[str, str] | None = None,
) -> str:
    if _truthy(profile.get("profile_tier_disabled")):
        return ""
    assigned = str(profile.get("assigned_profile_tier") or "").strip()
    label = (
        assigned
        or str(profile.get("profile_tier") or "").strip()
        or str(profile.get("generated_profile_tier") or "").strip()
    )
    if not label:
        return ""
    if tier_winner_ids:
        tier_key = normalize_profile_tier(label)
        gpu_uuid = profile_gpu_uuid(profile)
        winner_key = f"{gpu_uuid}\0{tier_key}" if gpu_uuid else tier_key
        winner_id = str(tier_winner_ids.get(winner_key) or "").strip()
        profile_id = str(profile.get("profile_id") or "").strip()
        # Only the resolved winner keeps the tier; duplicates show no tier so
        # the column stays unique and matches adaptive mode's choice.
        if winner_id and profile_id and winner_id != profile_id:
            return ""
    return label


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _profile_name(profile: dict) -> str:
    display_name = str(profile.get("display_name", "")).strip()
    if display_name:
        return display_name
    return profile_display_name(profile)


def _profile_matches_preference(
    profile: dict,
    *,
    preferred_candidate_id: str = "",
    preferred_profile_id: str = "",
) -> bool:
    profile_id = str(profile.get("profile_id", ""))
    candidate_id = str(profile.get("candidate_id", ""))
    if preferred_profile_id and profile_id == str(preferred_profile_id):
        return True
    if preferred_candidate_id and candidate_id == str(preferred_candidate_id):
        return True
    return False


def _promote_preferred_profile(
    profiles: list[dict],
    *,
    preferred_candidate_id: str = "",
    preferred_profile_id: str = "",
) -> list[dict]:
    if not preferred_candidate_id and not preferred_profile_id:
        return list(profiles)
    promoted_index = None
    for index, profile in enumerate(profiles):
        if _profile_matches_preference(
            profile,
            preferred_candidate_id=preferred_candidate_id,
            preferred_profile_id=preferred_profile_id,
        ):
            promoted_index = index
            break
    if promoted_index is None:
        return list(profiles)
    promoted = profiles[promoted_index]
    return [promoted, *profiles[:promoted_index], *profiles[promoted_index + 1 :]]
