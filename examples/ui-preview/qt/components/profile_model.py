"""Profiles page model: the data-source contract and pure helpers.

Nothing here touches Qt widgets, the daemon or the disk. The page reads
saved profiles as the same summary dicts ``profiles.uv.profile_store``
returns (``read_auto_uv_profile_summaries``), so every rule below can call
the real helpers in ``ui/`` and ``profiles/`` directly. The only local
copies are the two places where the real helper would read the user's
tier-assignment file: here the assignments come from the source instead.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from profiles.gpu_identity import (
    GPU_COMPATIBILITY_LEGACY,
    GPU_COMPATIBILITY_MATCH,
    profile_gpu_compatibility,
    profile_gpu_label,
    profile_gpu_uuid,
)
from profiles.uv.memory_offset_edit import editable_memory_offset_from_profile
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR
from profiles.uv.profile_tiers import (
    PROFILE_TIER_LABELS,
    available_adaptive_tiers,
    resolve_profile_tier_profiles,
)
from ui.components.profile_list import (
    PROFILE_SORTABLE_COLUMNS,
    ProfileList,
    _date_text,
    _format_number,
    _format_signed_memory_clock,
    _metric_delta_percent,
    _profile_base_metric,
    _profile_metric_tooltip,
    _profile_name,
    _profile_sort_values,
    _profile_source_label,
    _profile_tier_label,
    _promote_preferred_profile,
    _sort_value_less,
    _to_float,
)
from ui.features.profiles.profiles import (
    profile_can_apply,
    profile_can_verify,
    profile_delete_autostart_action,
    profile_for_selector,
    profile_is_deletable,
    profile_status_label,
    runner_status_parts,
)

STOCK = STOCK_PROFILE_SELECTOR
# The real table's columns and sortable set (ui/components/profile_list.py).
COLUMNS = ProfileList.COLUMNS
SORTABLE_COLUMNS = PROFILE_SORTABLE_COLUMNS
DATE, PROFILE, GPU, MV, TARGET, EFFECTIVE, FPSW, FPS, POWER, MEM, TIER, SOURCE = range(
    len(COLUMNS)
)
RIGHT_ALIGNED = frozenset({MV, TARGET, EFFECTIVE, FPSW, FPS, POWER, MEM})
# Columns whose tooltip compares against the scan's own baseline, with the
# real table's precision (profile_list.set_profiles metric_columns).
METRIC_COLUMNS = {
    MV: ("candidate_voltage_mv", 2),
    EFFECTIVE: ("avg_core_clock_mhz", 2),
    FPSW: ("efficiency_fps_per_w", 4),
    FPS: ("avg_fps", 2),
}
BUSY_REASON = "Finish the current PenguinBurner action first."


# ---------------------------------------------------------------------------
# Source contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuTarget:
    """One NVIDIA GPU the Profiles page can target."""

    index: int
    uuid: str
    name: str
    pci_bus_id: str = ""
    factory_power_w: float | None = None
    # Stored memory-offset range (transfer-rate units, as memory_offset_mhz_range).
    memory_offset_range: tuple[int, int] = (0, 2000)

    @property
    def short_name(self) -> str:
        return self.name.replace("NVIDIA ", "").replace("GeForce ", "")

    @property
    def label(self) -> str:
        return f"GPU {self.index} · {self.short_name}"


@dataclass(frozen=True)
class BootEntry:
    """What the daemon applies to one GPU at boot."""

    selector: str
    silent_fan: bool = False
    adaptive: bool = False
    main_gpu: bool = False


@dataclass(frozen=True)
class RuntimeState:
    """What the daemon runs now, as runner_status_parts reads it."""

    running_selector: str = ""
    running_gpu_uuid: str = ""
    running_adaptive: bool = False
    running_silent_fan: bool = False
    # Adaptive's live tier label, when Adaptive runs.
    live_tier: str = ""
    # A per-game profile the Game Library swapped in while the game runs.
    game_override: bool = False
    standing_selector: str = ""
    standing_adaptive: bool = False
    defaults_restored: bool = False


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    message: str = ""
    profile_id: str = ""


@dataclass(frozen=True)
class LactExport:
    path: str = ""
    warnings: tuple[str, ...] = ()
    text: str = ""
    error: str = ""


Done = Callable[[CommandResult], None]


class ProfilesSource(Protocol):
    """Everything the Profiles page reads and writes.

    The preview passes a sample adapter; the real app passes one backed by
    the profile store and the root daemon. Writes run only after an
    explicit user action, and the long ones report through ``done``, which
    is called exactly once per command, with a failed ``CommandResult`` when
    the command raised.
    """

    # Reads
    def profiles(self) -> list[dict]: ...
    def gpus(self) -> list[GpuTarget]: ...
    def tier_assignments(self, gpu_uuid: str) -> dict[str, str]: ...
    def tier_disabled(self, gpu_uuid: str) -> set[str]: ...
    def runtime(self) -> RuntimeState: ...
    def boot_entry(self, gpu_uuid: str) -> BootEntry | None: ...
    def silent_fan_preference(self) -> bool: ...
    def curve_plan(self, profile_id: str) -> list[dict]: ...
    def base_curve_points(self, profile_id: str) -> list[tuple[float, float]]: ...
    def fan_curve(self, profile_id: str) -> list[tuple[float, float]]: ...
    def fan_measurements(self, profile_id: str) -> list[tuple[float, float]]: ...
    def fan_target_point(self, profile_id: str) -> tuple[float, float] | None: ...
    def fan_runtime_ready(self, profile_id: str) -> bool: ...
    def lact_directories(self) -> list[str]: ...

    # Writes (each one follows an explicit user action)
    def set_silent_fan_preference(self, enabled: bool) -> None: ...
    def apply_profile(
        self,
        profile_id: str,
        gpu_uuid: str,
        *,
        silent_fan: bool,
        persist_on_startup: bool,
        done: Done,
    ) -> None: ...
    def restore_defaults(
        self, gpu_uuid: str, *, silent_fan: bool, done: Done
    ) -> None: ...
    def delete_profiles(self, profile_ids: list[str], done: Done) -> None: ...
    def clear_boot(self, gpu_uuid: str) -> None: ...
    def set_main_gpu(self, gpu_uuid: str) -> None: ...
    def assign_tier(self, profile_id: str, tier: str) -> None: ...
    def save_curve_edit(self, profile_id: str, edit) -> str: ...
    def save_fan_edit(
        self, profile_id: str, edit, original_points: list[tuple[float, float]]
    ) -> str: ...
    def save_memory_offset(self, profile_id: str, memory_offset: int) -> str: ...
    def export_lact(
        self, profile_id: str, directory: str, *, include_fan_curve: bool
    ) -> LactExport: ...
    def finish_verification(
        self, profile_id: str, gpu_uuid: str, passed: bool
    ) -> None: ...
    def save_afterburner_import(
        self, entry: dict, fan_payload: dict | None
    ) -> dict: ...

    # Change notification: called after any write or live-state change.
    def subscribe(self, callback: Callable[[], None]) -> None: ...


# ---------------------------------------------------------------------------
# GPU targeting (ProfileList.configure_gpu_targets / profile_matches_target)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    gpus: tuple[GpuTarget, ...]
    uuid: str = ""

    @property
    def selection_required(self) -> bool:
        return len(self.gpus) >= 2

    @property
    def single_gpu(self) -> bool:
        return len(self.gpus) == 1

    @property
    def gpu(self) -> GpuTarget | None:
        key = self.uuid.casefold()
        return next((g for g in self.gpus if g.uuid.casefold() == key), None)

    @property
    def index(self) -> int | None:
        gpu = self.gpu
        return None if gpu is None else gpu.index

    @property
    def chosen(self) -> bool:
        return self.gpu is not None


def resolve_target(
    gpus: Iterable[GpuTarget],
    *,
    previous_uuid: str = "",
    preferred_index: int | None = None,
) -> Target:
    """One GPU targets itself; with two or more, keep the user's choice."""
    choices = tuple(g for g in gpus if g.uuid.strip())
    if len(choices) == 1:
        return Target(choices, choices[0].uuid)
    if preferred_index is not None:
        match = next((g for g in choices if g.index == preferred_index), None)
        if match is not None:
            return Target(choices, match.uuid)
    keep = next(
        (g for g in choices if g.uuid.casefold() == previous_uuid.casefold()), None
    )
    return Target(choices, keep.uuid if keep else "")


def profile_matches_target(profile: dict, target: Target) -> bool:
    compatibility = profile_gpu_compatibility(profile, target.uuid)
    if compatibility == GPU_COMPATIBILITY_MATCH:
        return target.chosen
    if compatibility == GPU_COMPATIBILITY_LEGACY:
        return target.single_gpu and target.chosen
    return False


def row_visible(profile: dict, target: Target) -> bool:
    """Rows bound to another GPU hide behind the target (legacy rows stay)."""
    profile_uuid = profile_gpu_uuid(profile).casefold()
    return not (target.uuid and profile_uuid and profile_uuid != target.uuid.casefold())


def visible_profiles(profiles: Iterable[dict], target: Target) -> list[dict]:
    return [p for p in profiles if row_visible(p, target)]


# ---------------------------------------------------------------------------
# Sorting (header click; missing values sort last in either direction)
# ---------------------------------------------------------------------------


def next_sort(column: int, descending: bool, clicked: int) -> tuple[int, bool]:
    """The real header rule: same column flips; Date starts newest first."""
    if clicked not in SORTABLE_COLUMNS:
        return column, descending
    if clicked == column:
        return column, not descending
    return clicked, clicked == DATE


def sort_profiles(profiles: list[dict], column: int, descending: bool) -> list[dict]:
    """Order rows like QTableWidget.sortItems with the real item comparator.

    The input order breaks ties, so the store's newest-first order stays
    the secondary key."""
    if column not in SORTABLE_COLUMNS:
        return list(profiles)
    keyed = [(_profile_sort_values(p)[column], p) for p in profiles]

    def compare(a, b) -> int:
        left, right = (b[0], a[0]) if descending else (a[0], b[0])
        if _sort_value_less(left, right, descending=descending):
            return -1
        if _sort_value_less(right, left, descending=descending):
            return 1
        return 0

    return [p for _value, p in sorted(keyed, key=functools.cmp_to_key(compare))]


def promote_preferred(profiles: list[dict], preferred_profile_id: str) -> list[dict]:
    return _promote_preferred_profile(
        profiles, preferred_profile_id=preferred_profile_id
    )


# ---------------------------------------------------------------------------
# Tiers: the label sits only on the profile Adaptive would pick
# ---------------------------------------------------------------------------


AssignmentsFor = Callable[[str], dict[str, str]]
DisabledFor = Callable[[str], set[str]]


def merged_tier_store(
    assignments: AssignmentsFor,
    disabled: DisabledFor,
    gpu_uuid: str,
    include_legacy_profiles: bool,
) -> tuple[dict[str, str], set[str]]:
    """Per-GPU assignments; on a one-GPU machine the old global ones too
    (resolve_profile_tier_profiles' own merge rule)."""
    tiers = dict(assignments(gpu_uuid))
    off = set(disabled(gpu_uuid))
    if gpu_uuid and include_legacy_profiles:
        tiers = {**assignments(""), **tiers}
        off |= set(disabled(""))
    return tiers, off


def tier_winner_ids(
    profiles: list[dict],
    assignments: AssignmentsFor,
    disabled: DisabledFor,
    *,
    include_legacy_profiles: bool = False,
) -> dict[str, str]:
    """profile_list._resolved_tier_winner_ids, with the assignment store
    passed in instead of read from the user's config file."""
    winners: dict[str, str] = {}
    gpu_uuids = {profile_gpu_uuid(p) for p in profiles}
    if include_legacy_profiles and gpu_uuids - {""}:
        gpu_uuids -= {""}
    for gpu_uuid in gpu_uuids:
        tiers, off = merged_tier_store(
            assignments, disabled, gpu_uuid, include_legacy_profiles
        )
        resolved = resolve_profile_tier_profiles(
            profiles,
            tiers,
            off,
            gpu_uuid=gpu_uuid,
            include_legacy_profiles=include_legacy_profiles,
        )
        for tier, profile in resolved.items():
            profile_id = str((profile or {}).get("profile_id") or "").strip()
            if not profile_id:
                continue
            winners[f"{gpu_uuid}\0{tier}" if gpu_uuid else tier] = profile_id
            if include_legacy_profiles and gpu_uuid:
                winners[tier] = profile_id
    return winners


def tier_label(profile: dict, winners: dict[str, str]) -> str:
    return _profile_tier_label(profile, winners)


def adaptive_tier_labels(
    profiles: list[dict],
    assignments: AssignmentsFor,
    disabled: DisabledFor,
    target: Target,
) -> list[str]:
    """Tiers Adaptive can switch between on the target GPU."""
    if not target.chosen:
        return []
    tiers, off = merged_tier_store(
        assignments, disabled, target.uuid, target.single_gpu
    )
    resolved = resolve_profile_tier_profiles(
        profiles,
        tiers,
        off,
        gpu_uuid=target.uuid,
        include_legacy_profiles=target.single_gpu,
    )
    return [PROFILE_TIER_LABELS[t] for t in available_adaptive_tiers(resolved)]


# ---------------------------------------------------------------------------
# Table cells (formatting and metric context from profile_list.set_profiles)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    text: str
    tooltip: str = ""
    # Power W: signed % against the GPU's factory power limit.
    delta: str = ""
    delta_good: bool = False


def factory_power_limit(profile: dict, gpus: Iterable[GpuTarget]) -> float | None:
    key = profile_gpu_uuid(profile).casefold()
    if not key:
        return None
    for gpu in gpus:
        if gpu.uuid.casefold() == key:
            watts = _to_float(gpu.factory_power_w)
            return watts if watts is not None and watts > 0 else None
    return None


def date_text(profile: dict) -> str:
    # The real table shows seconds; the minute is enough to tell rows apart
    # and keeps the column narrow. Sorting still uses the full timestamp.
    text = _date_text(profile)
    return text[:16] if len(text) >= 16 else text


def profile_cells(
    profile: dict, *, factory_power_w: float | None, tier: str
) -> list[Cell]:
    cells = [
        Cell(date_text(profile)),
        Cell(_profile_name(profile)),
        gpu_cell(profile),
        Cell(_format_number(profile.get("candidate_voltage_mv"), precision=0)),
        Cell(_format_number(profile.get("lock_clock_mhz"), precision=0)),
        Cell(_format_number(profile.get("avg_core_clock_mhz"), precision=2)),
        Cell(_format_number(profile.get("efficiency_fps_per_w"), precision=4)),
        Cell(_format_number(profile.get("avg_fps"), precision=2)),
        power_cell(profile.get("avg_power_w"), factory_power_w),
        memory_cell(profile),
        Cell(tier),
        Cell(_profile_source_label(profile)),
    ]
    for column, (metric, precision) in METRIC_COLUMNS.items():
        tooltip = _profile_metric_tooltip(
            profile.get(metric),
            _profile_base_metric(profile, metric),
            label=COLUMNS[column],
            precision=precision,
        )
        cells[column] = Cell(cells[column].text, tooltip)
    return cells


def gpu_cell(profile: dict) -> Cell:
    """The real GPU label, shortened to the model; the tooltip keeps it all."""
    label = profile_gpu_label(profile)
    short = label.replace("NVIDIA ", "").replace("GeForce ", "")
    return Cell(short, label if short != label else "")


def power_cell(watts, limit_w: float | None) -> Cell:
    text = _format_number(watts, precision=2)
    if not text:
        return Cell("")
    delta = _metric_delta_percent(watts, limit_w)
    if delta is None or limit_w is None:
        return Cell(
            f"{text} W",
            "Factory/default power limit unavailable for this profile's GPU.",
        )
    return Cell(
        f"{text} W",
        f"{delta:+.2f}% relative to this GPU's factory/default power limit "
        f"({limit_w:.2f} W).\nThis compares measured power to a power cap, not "
        "measured stock power savings.",
        delta=f"{delta:+.2f}%".replace("-", "−"),
        delta_good=delta < 0,
    )


def memory_cell(profile: dict) -> Cell:
    text = _format_signed_memory_clock(profile.get("memory_offset_mhz"))
    stored = editable_memory_offset_from_profile(profile)
    return Cell(text, transfer_rate_text(stored) if text and stored is not None else "")


def transfer_rate_text(stored_offset: int) -> str:
    """The memory editor's '= ±N MT/s transfer rate' line for a stored value."""
    return f"= {int(stored_offset):+d} MT/s transfer rate"


def memory_clock_mhz(profile: dict) -> int | None:
    stored = editable_memory_offset_from_profile(profile)
    return None if stored is None else stored // 2


# ---------------------------------------------------------------------------
# Per-profile action rules (profile_actions._show_profile_context_menu)
# ---------------------------------------------------------------------------


ACTIONS = (
    "edit_vf",
    "edit_fan",
    "edit_memory",
    "apply",
    "verify",
    "export_lact",
    "assign_tier",
    "delete",
)


def apply_refusal(profile: dict, target: Target) -> str:
    """Why Apply cannot run for this profile, or '' when it can."""
    if not target.chosen:
        return "Choose a target GPU before applying this profile."
    if not is_applicable(profile):
        return "This edited profile must be verified before it can be applied."
    if not profile_matches_target(profile, target):
        if profile_gpu_compatibility(profile, target.uuid) == GPU_COMPATIBILITY_LEGACY:
            return (
                "This legacy profile must be verified on the selected GPU "
                "before it can be applied."
            )
        return "This profile belongs to a different GPU. Choose its GPU target."
    return ""


def verify_refusal(profile: dict, target: Target) -> str:
    if not target.chosen:
        return "Choose a target GPU before verifying this profile."
    if profile_gpu_compatibility(profile, target.uuid) not in {
        GPU_COMPATIBILITY_MATCH,
        GPU_COMPATIBILITY_LEGACY,
    }:
        return "This profile belongs to a different GPU. Choose its GPU target."
    return ""


def is_applicable(profile: dict) -> bool:
    """Verified, or an imported Afterburner profile (profile_list rows)."""
    return (
        profile_can_apply(profile)
        or str(profile.get("runtime_source", "")) == "afterburner"
    )


def is_deletable(profile: dict) -> bool:
    return (
        profile_is_deletable(profile)
        or str(profile.get("runtime_source", "")) == "afterburner"
    )


def action_reasons(
    profile: dict,
    target: Target,
    *,
    busy: bool,
    has_vf_plan: bool,
    has_fan_curve: bool,
) -> dict[str, str]:
    """Each action mapped to why it is disabled ('' = enabled)."""
    unverified = "" if is_applicable(profile) else "Verify this profile first."
    busy_reason = BUSY_REASON if busy else ""
    memory = ""
    if editable_memory_offset_from_profile(profile) is None:
        memory = "No editable memory offset is available for this profile."
    return {
        "edit_vf": ""
        if has_vf_plan
        else "No editable V/F curve is available for this profile.",
        "edit_fan": ""
        if has_fan_curve
        else "No editable fan curve is available for this profile.",
        "edit_memory": memory,
        "apply": busy_reason or apply_refusal(profile, target),
        "verify": busy_reason
        or (
            ""
            if profile_can_verify(profile)
            else "This profile has no saved file to verify."
        ),
        "export_lact": busy_reason or unverified,
        "assign_tier": busy_reason
        or unverified
        or (
            ""
            if str(profile.get("profile_id") or "").strip()
            else "This profile has no id."
        ),
        "delete": busy_reason
        or ("" if is_deletable(profile) else "This profile cannot be deleted."),
    }


def verification_label(profile: dict) -> tuple[str, str]:
    """(text, tone) for the detail card's status pill."""
    if str(profile.get("verification_status") or "") == "imported":
        return "Imported", "accent"
    if is_applicable(profile):
        return "Verified", "accent"
    return "Needs verification", "warning"


# ---------------------------------------------------------------------------
# Delete safety (profile_actions._delete_selected_profiles)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeleteSafety:
    restore_stock: bool = False
    last_adaptive: bool = False
    gpu_uuid: str = ""


def _selector_info(
    selector: str, *, adaptive: bool, gpu_uuid: str
) -> dict[str, object]:
    return {"selector": selector, "adaptive_auto_uv": adaptive, "gpu_uuid": gpu_uuid}


def _autostart_action(
    profiles: list[dict],
    selected: list[str],
    info: dict[str, object],
    assignments: AssignmentsFor,
    disabled: DisabledFor,
    include_legacy: bool,
) -> dict[str, str]:
    """profiles.profile_delete_autostart_action; the Adaptive branch resolves
    tiers from the source's assignments instead of the config file."""
    if (
        not bool(info.get("adaptive_auto_uv"))
        or not str(info.get("selector") or "").strip()
    ):
        return profile_delete_autostart_action(profiles, selected, info)
    gpu_uuid = str(info.get("gpu_uuid") or "")
    chosen = {str(v).strip() for v in selected}
    remaining = [
        p for p in profiles if str(p.get("profile_id", "")).strip() not in chosen
    ]
    tiers, off = merged_tier_store(assignments, disabled, gpu_uuid, include_legacy)
    resolved = resolve_profile_tier_profiles(
        remaining,
        tiers,
        off,
        gpu_uuid=gpu_uuid,
        include_legacy_profiles=include_legacy,
    )
    if available_adaptive_tiers(resolved):
        return {"action": "keep"}
    return {"action": "restore-stock", "reason": "last-usable-adaptive-profile"}


def delete_safety(
    profiles: list[dict],
    selected_ids: list[str],
    *,
    boot: BootEntry | None,
    runtime: RuntimeState,
    target: Target,
    assignments: AssignmentsFor,
    disabled: DisabledFor,
) -> DeleteSafety:
    """Deleting the startup or running profile, or the last usable Adaptive
    tier, restores stock now and at boot on that GPU."""
    include_legacy = target.single_gpu
    boot_action = {"action": "keep"}
    if boot is not None and boot.selector != STOCK:
        boot_action = _autostart_action(
            profiles,
            selected_ids,
            _selector_info(boot.selector, adaptive=boot.adaptive, gpu_uuid=target.uuid),
            assignments,
            disabled,
            include_legacy,
        )
    running_action = {"action": "keep"}
    if runtime.running_selector and runtime.running_selector != STOCK:
        running_action = _autostart_action(
            profiles,
            selected_ids,
            _selector_info(
                runtime.running_selector,
                adaptive=runtime.running_adaptive,
                gpu_uuid=runtime.running_gpu_uuid or target.uuid,
            ),
            assignments,
            disabled,
            include_legacy,
        )
    actions = (boot_action.get("action"), running_action.get("action"))
    restore = "restore-stock" in actions
    last_adaptive = "last-usable-adaptive-profile" in (
        boot_action.get("reason"),
        running_action.get("reason"),
    )
    gpu_uuid = target.uuid
    if running_action.get("action") == "restore-stock" and runtime.running_gpu_uuid:
        gpu_uuid = runtime.running_gpu_uuid
    return DeleteSafety(restore, last_adaptive, gpu_uuid if restore else "")


# ---------------------------------------------------------------------------
# Runner status (window status band)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusLines:
    running: str
    detail: str
    tone: str = ""
    busy: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "running": self.running,
            "detail": self.detail,
            "tone": self.tone,
            "busy": self.busy,
        }


def runner_status(
    profiles: list[dict], runtime: RuntimeState, boot: BootEntry | None
) -> StatusLines:
    """The real two-group runner status: headline, then everything behind it."""
    head, parts = runner_status_parts(
        profiles,
        running_selector=runtime.running_selector,
        running_adaptive=runtime.running_adaptive,
        autostart_selector=""
        if boot is None or boot.selector == STOCK
        else boot.selector,
        running_silent_fan=runtime.running_silent_fan,
        autostart_silent_fan=bool(boot and boot.silent_fan),
        defaults_restored=runtime.defaults_restored,
        game_override=runtime.game_override,
        standing_selector=runtime.standing_selector,
        standing_adaptive=runtime.standing_adaptive,
    )
    running = "; ".join(head)
    if runtime.running_adaptive and runtime.live_tier:
        running += f" · live tier {runtime.live_tier}"
    return StatusLines(running, " · ".join(parts))


def boot_label(profiles: list[dict], boot: BootEntry | None) -> str:
    if boot is None:
        return "Nothing applies at boot"
    if boot.selector == STOCK:
        return "Stock settings"
    label = profile_status_label(profiles, boot.selector)
    if boot.adaptive:
        label += " (Adaptive)"
    if boot.silent_fan:
        label += " · silent fan curve"
    return label


def profile_by_id(profiles: list[dict], profile_id: str) -> dict | None:
    return profile_for_selector(profiles, profile_id) if profile_id else None


@dataclass
class Selection:
    """Selected profile ids in table order; the last one leads."""

    ids: list[str] = field(default_factory=list)

    @property
    def lead(self) -> str:
        return self.ids[-1] if self.ids else ""
