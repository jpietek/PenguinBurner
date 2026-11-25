"""Profiles page model: the data-source contract and page-level helpers.

Nothing here touches Qt widgets, the daemon or the disk. The page reads
saved profiles as the summary dicts ``profiles.uv.profile_store`` returns
(``read_auto_uv_profile_summaries``); every rule is the backend's own
(``profiles.uv.profile_rules`` and ``profile_presentation``), called with the
tier assignments the source reports.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from profiles.gpu_identity import profile_gpu_label, profile_gpu_uuid
from profiles.uv.memory_offset_edit import editable_memory_offset_from_profile
from profiles.uv.profile_presentation import (
    PROFILE_COLUMNS,
    PROFILE_SORTABLE_COLUMNS,
    format_number,
    format_signed_memory_clock,
    metric_delta_percent,
    profile_base_metric,
    profile_metric_tooltip,
    profile_name,
    profile_sort_values,
    profile_source_label,
    profile_tier_column_label,
    promote_preferred_profile,
    sort_value_less,
    to_float,
)
from profiles.uv.profile_presentation import (
    date_text as _full_date_text,
)
from profiles.uv.profile_rules import (
    DeleteSafety,
    TierStore,
    adaptive_profile_tier_labels,
    profile_can_apply,
    profile_can_verify,
    profile_delete_safety,
    profile_for_selector,
    profile_matches_gpu_target,
    profile_row_applicable,
    profile_row_deletable,
    profile_status_label,
    runner_status_parts,
)
from profiles.uv.profile_rules import apply_refusal as _apply_refusal
from profiles.uv.profile_rules import verify_refusal as _verify_refusal
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR

STOCK = STOCK_PROFILE_SELECTOR
COLUMNS = PROFILE_COLUMNS
SORTABLE_COLUMNS = PROFILE_SORTABLE_COLUMNS
DATE, PROFILE, GPU, MV, TARGET, EFFECTIVE, FPSW, FPS, POWER, MEM, TIER, SOURCE = range(
    len(COLUMNS)
)
RIGHT_ALIGNED = frozenset({MV, TARGET, EFFECTIVE, FPSW, FPS, POWER, MEM})
# Columns whose tooltip compares against the scan's own baseline, with the
# classic table's precision (profile_list.set_profiles metric_columns).
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
    # The copyable error report of a failed command (process_failure_details).
    details: str = ""
    # The user declined a step the command needed (the root-service prompt):
    # nothing ran and there is nothing to report.
    cancelled: bool = False


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
    # One GPU's boot entry; "" is the daemon's active boot entry, the one the
    # window status reports as Autostart.
    def boot_entry(self, gpu_uuid: str) -> BootEntry | None: ...
    def silent_fan_preference(self) -> bool: ...
    def curve_plan(self, profile_id: str) -> list[dict]: ...
    def base_curve_points(self, profile_id: str) -> list[tuple[float, float]]: ...
    def fan_curve(self, profile_id: str) -> list[tuple[float, float]]: ...
    def fan_measurements(self, profile_id: str) -> list[tuple[float, float]]: ...
    def fan_target_point(self, profile_id: str) -> tuple[float, float] | None: ...
    def fan_runtime_ready(self, profile_id: str) -> bool: ...
    def memory_offset_range(self, gpu_uuid: str) -> tuple[int, int]: ...
    def lact_directories(self) -> list[str]: ...

    # Writes (each one follows an explicit user action)
    def set_silent_fan_preference(self, enabled: bool) -> None: ...
    def remember_apply_on_startup(self, enabled: bool) -> None: ...
    def remember_target_gpu(self, gpu_index: int) -> None: ...
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


def tier_store(source: ProfilesSource) -> TierStore:
    """The backend tier rules over the source's assignment store."""
    return TierStore(assignments=source.tier_assignments, disabled=source.tier_disabled)


# ---------------------------------------------------------------------------
# GPU targeting (ProfileList.configure_gpu_targets)
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
    """One GPU targets itself; with two or more, a preferred index (the
    window's --gpu-index or the last choice) wins, then the previous choice.

    Targeting needs stable identities: GPUs without a UUID are dropped, and
    when discovery produced none at all, the first GPU still targets itself
    so a one-GPU machine can apply (ProfileList.configure_gpu_targets)."""
    listed = list(gpus)
    choices = tuple(g for g in listed if g.uuid.strip()) or tuple(listed[:1])
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
    return profile_matches_gpu_target(
        profile, target.uuid, target_chosen=target.chosen, single_gpu=target.single_gpu
    )


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
    """The classic header rule: same column flips; Date starts newest first."""
    if clicked not in SORTABLE_COLUMNS:
        return column, descending
    if clicked == column:
        return column, not descending
    return clicked, clicked == DATE


def sort_profiles(profiles: list[dict], column: int, descending: bool) -> list[dict]:
    """Order rows like QTableWidget.sortItems with the classic item comparator.

    The input order breaks ties, so the store's newest-first order stays
    the secondary key."""
    if column not in SORTABLE_COLUMNS:
        return list(profiles)
    keyed = [(profile_sort_values(p)[column], p) for p in profiles]

    def compare(a, b) -> int:
        left, right = (b[0], a[0]) if descending else (a[0], b[0])
        if sort_value_less(left, right, descending=descending):
            return -1
        if sort_value_less(right, left, descending=descending):
            return 1
        return 0

    return [p for _value, p in sorted(keyed, key=functools.cmp_to_key(compare))]


def promote_preferred(profiles: list[dict], preferred_profile_id: str) -> list[dict]:
    return promote_preferred_profile(profiles, preferred_profile_id=preferred_profile_id)


# ---------------------------------------------------------------------------
# Tiers: the label sits only on the profile Adaptive would pick
# ---------------------------------------------------------------------------


def tier_label(profile: dict, winners: dict[str, str]) -> str:
    return profile_tier_column_label(profile, winners)


def running_tier_label(
    runtime: RuntimeState, profiles: list[dict], winners: dict[str, str]
) -> str:
    """The overlay HUD's tier row: Adaptive's live tier, the running
    profile's tier, "Custom" for an untiered profile, "Default" at stock."""
    if runtime.running_adaptive and runtime.live_tier:
        return runtime.live_tier
    selector = runtime.running_selector
    if not selector or selector == STOCK:
        return "Default"
    profile = profile_by_id(profiles, selector)
    return (tier_label(profile, winners) if profile else "") or "Custom"


def adaptive_tier_labels(
    profiles: list[dict], store: TierStore, target: Target
) -> list[str]:
    """Tiers Adaptive can switch between on the target GPU."""
    if not target.chosen:
        return []
    return adaptive_profile_tier_labels(
        profiles,
        gpu_uuid=target.uuid,
        include_legacy_profiles=target.single_gpu,
        store=store,
    )


# ---------------------------------------------------------------------------
# Table cells (formatting and metric context from profile_presentation)
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
            watts = to_float(gpu.factory_power_w)
            return watts if watts is not None and watts > 0 else None
    return None


def date_text(profile: dict) -> str:
    # The classic table shows seconds; the minute is enough to tell rows apart
    # and keeps the column narrow. Sorting still uses the full timestamp.
    text = _full_date_text(profile)
    return text[:16] if len(text) >= 16 else text


def profile_cells(
    profile: dict, *, factory_power_w: float | None, tier: str
) -> list[Cell]:
    cells = [
        Cell(date_text(profile)),
        Cell(profile_name(profile)),
        gpu_cell(profile),
        Cell(format_number(profile.get("candidate_voltage_mv"), precision=0)),
        Cell(format_number(profile.get("lock_clock_mhz"), precision=0)),
        Cell(format_number(profile.get("avg_core_clock_mhz"), precision=2)),
        Cell(format_number(profile.get("efficiency_fps_per_w"), precision=4)),
        Cell(format_number(profile.get("avg_fps"), precision=2)),
        power_cell(profile.get("avg_power_w"), factory_power_w),
        memory_cell(profile),
        Cell(tier),
        Cell(profile_source_label(profile)),
    ]
    for column, (metric, precision) in METRIC_COLUMNS.items():
        tooltip = profile_metric_tooltip(
            profile.get(metric),
            profile_base_metric(profile, metric),
            label=COLUMNS[column],
            precision=precision,
        )
        cells[column] = Cell(cells[column].text, tooltip)
    return cells


def gpu_cell(profile: dict) -> Cell:
    """The GPU label, shortened to the model; the tooltip keeps it all."""
    label = profile_gpu_label(profile)
    short = label.replace("NVIDIA ", "").replace("GeForce ", "")
    return Cell(short, label if short != label else "")


def power_cell(watts, limit_w: float | None) -> Cell:
    text = format_number(watts, precision=2)
    if not text:
        return Cell("")
    delta = metric_delta_percent(watts, limit_w)
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
    text = format_signed_memory_clock(profile.get("memory_offset_mhz"))
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
    return _apply_refusal(
        profile, target.uuid, target_chosen=target.chosen, single_gpu=target.single_gpu
    )


def verify_refusal(profile: dict, target: Target) -> str:
    return _verify_refusal(profile, target.uuid, target_chosen=target.chosen)


def is_applicable(profile: dict) -> bool:
    """The table row's Apply state (verified, or an imported Afterburner row)."""
    return profile_row_applicable(profile)


def is_deletable(profile: dict) -> bool:
    return profile_row_deletable(profile)


def action_reasons(
    profile: dict,
    target: Target,
    *,
    busy: bool,
    has_vf_plan: bool,
    has_fan_curve: bool,
) -> dict[str, str]:
    """Each action mapped to why it is disabled ('' = enabled)."""
    unverified = "" if profile_can_apply(profile) else "Verify this profile first."
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


def boot_info(boot: BootEntry | None, gpu_uuid: str) -> dict[str, object]:
    """A boot entry as the backend rules read it."""
    if boot is None:
        return {}
    return {
        "selector": boot.selector,
        "silent_fan_curve": boot.silent_fan,
        "adaptive_auto_uv": boot.adaptive,
        "gpu_uuid": gpu_uuid,
    }


def running_info(runtime: RuntimeState) -> dict[str, object]:
    """What runs now as the backend rules read it; empty when nothing runs."""
    if not runtime.running_selector:
        return {}
    return {
        "selector": runtime.running_selector,
        "silent_fan_curve": runtime.running_silent_fan,
        "adaptive_auto_uv": runtime.running_adaptive,
        "gpu_uuid": runtime.running_gpu_uuid,
    }


def delete_safety(
    profiles: list[dict],
    selected_ids: list[str],
    *,
    boot: BootEntry | None,
    runtime: RuntimeState,
    target: Target,
    store: TierStore,
) -> DeleteSafety:
    """Deleting the target GPU's startup profile, the running profile, or the
    last usable Adaptive tier restores stock now and at boot."""
    return profile_delete_safety(
        profiles,
        list(selected_ids),
        autostart_info=boot_info(boot, target.uuid),
        running_info=running_info(runtime),
        include_legacy_profiles=target.single_gpu,
        store=store,
    )


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
    profiles: list[dict], runtime: RuntimeState, autostart: BootEntry | None
) -> StatusLines:
    """The two-group runner status: headline, then everything behind it.
    ``autostart`` is the daemon's active boot entry."""
    head, parts = runner_status_parts(
        profiles,
        running_selector=runtime.running_selector,
        running_adaptive=runtime.running_adaptive,
        autostart_selector="" if autostart is None else autostart.selector,
        running_silent_fan=runtime.running_silent_fan,
        autostart_silent_fan=bool(autostart and autostart.silent_fan),
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
