"""Saved-profile rules shared by every Profiles view.

Pure decisions over profile summaries (``read_auto_uv_profile_summaries``):
which profile a selector names, whether a profile can be applied, verified
or deleted on a target GPU, which profile Adaptive picks for each tier, what
deleting a profile does to the boot and running state, and the runner-status
wording. No Qt and no daemon calls. Tier assignments come from a
``TierStore``; the default one reads the user's assignment file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from profiles.gpu_identity import (
    GPU_COMPATIBILITY_LEGACY,
    GPU_COMPATIBILITY_MATCH,
    profile_gpu_compatibility,
    profile_gpu_uuid,
)
from profiles.uv.profile_store import (
    STOCK_PROFILE_SELECTOR,
    profile_clock_voltage_memory_summary,
    profile_display_name,
    profile_presentation_name,
)
from profiles.uv.profile_tiers import (
    available_adaptive_tiers,
    load_profile_tier_assignments,
    load_profile_tier_disabled_profile_ids,
    profile_tier_label,
    resolve_profile_tier_profiles,
)

NO_SILENT_FAN_CURVE = "No runtime-ready silent fan curve is available."
CHOOSE_TARGET_TO_APPLY = "Choose a target GPU before applying this profile."
CHOOSE_TARGET_TO_VERIFY = "Choose a target GPU before verifying this profile."
VERIFY_BEFORE_APPLY = "This edited profile must be verified before it can be applied."
LEGACY_VERIFY_BEFORE_APPLY = (
    "This legacy profile must be verified on the selected GPU before it can be "
    "applied."
)
OTHER_GPU_PROFILE = "This profile belongs to a different GPU. Choose its GPU target."


# ---------------------------------------------------------------------------
# Tier assignments
# ---------------------------------------------------------------------------


def _saved_assignments(gpu_uuid: str) -> dict[str, str]:
    return load_profile_tier_assignments(gpu_uuid=gpu_uuid)


def _saved_disabled(gpu_uuid: str) -> set[str]:
    return load_profile_tier_disabled_profile_ids(gpu_uuid=gpu_uuid)


@dataclass(frozen=True)
class TierStore:
    """Where per-GPU tier assignments and "None" tiers come from ("" is the
    old global store). The default reads the user's assignment file."""

    assignments: Callable[[str], dict[str, str]] = _saved_assignments
    disabled: Callable[[str], set[str]] = _saved_disabled

    def merged(
        self, gpu_uuid: str, include_legacy_profiles: bool
    ) -> tuple[dict[str, str], set[str]]:
        """Per-GPU assignments; on a one-GPU machine the old global ones
        too, with the per-GPU ones winning."""
        tiers = dict(self.assignments(gpu_uuid))
        disabled = set(self.disabled(gpu_uuid))
        if gpu_uuid and include_legacy_profiles:
            tiers = {**self.assignments(""), **tiers}
            disabled |= set(self.disabled(""))
        return tiers, disabled


SAVED_TIERS = TierStore()


def resolved_profile_tiers(
    profiles: list[dict],
    *,
    gpu_uuid: str = "",
    include_legacy_profiles: bool = False,
    store: TierStore = SAVED_TIERS,
) -> dict[str, dict | None]:
    """The profile Adaptive picks for each tier on one GPU."""
    selected = str(gpu_uuid or "").strip()
    tiers, disabled = store.merged(selected, include_legacy_profiles)
    return resolve_profile_tier_profiles(
        list(profiles),
        tiers,
        disabled,
        gpu_uuid=selected,
        include_legacy_profiles=include_legacy_profiles,
    )


def adaptive_profile_tier_keys(
    profiles: list[dict],
    *,
    gpu_uuid: str = "",
    include_legacy_profiles: bool = False,
    store: TierStore = SAVED_TIERS,
) -> list[str]:
    return available_adaptive_tiers(
        resolved_profile_tiers(
            profiles,
            gpu_uuid=gpu_uuid,
            include_legacy_profiles=include_legacy_profiles,
            store=store,
        )
    )


def adaptive_profile_tier_labels(
    profiles: list[dict],
    *,
    gpu_uuid: str = "",
    include_legacy_profiles: bool = False,
    store: TierStore = SAVED_TIERS,
) -> list[str]:
    """Tiers Adaptive can switch between on one GPU, as labels."""
    return [
        label
        for label in (
            profile_tier_label(tier)
            for tier in adaptive_profile_tier_keys(
                profiles,
                gpu_uuid=gpu_uuid,
                include_legacy_profiles=include_legacy_profiles,
                store=store,
            )
        )
        if label
    ]


def tier_winner_ids(
    profiles: list[dict],
    *,
    include_legacy_profiles: bool = False,
    store: TierStore = SAVED_TIERS,
) -> dict[str, str]:
    """Tier key -> the profile Adaptive would pick, for every GPU present.

    Adaptive collapses every tier to a single profile, so a table shows each
    tier label on exactly one row. Keys are ``"<gpu uuid>\\0<tier>"``, or the
    bare tier for unbound profiles.
    """
    winners: dict[str, str] = {}
    gpu_uuids = {profile_gpu_uuid(profile) for profile in profiles}
    if include_legacy_profiles and gpu_uuids - {""}:
        # A single-GPU host resolves legacy and bound profiles as one merged
        # population (the game-launch rule), so the tier label must land on
        # the row adaptive would actually pick regardless of its binding.
        gpu_uuids -= {""}
    for gpu_uuid in gpu_uuids:
        resolved = resolved_profile_tiers(
            profiles,
            gpu_uuid=gpu_uuid,
            include_legacy_profiles=include_legacy_profiles,
            store=store,
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


# ---------------------------------------------------------------------------
# Selectors and per-profile capabilities
# ---------------------------------------------------------------------------


def profile_for_selector(profiles: list[dict], selector: str) -> dict | None:
    text = str(selector or "").strip()
    if not text:
        return None
    if text in {"latest", "__systemd_default__"}:
        return profiles[0] if profiles else None
    for profile in profiles:
        path = str(profile.get("path", "")).strip()
        path_names = {Path(path).name, Path(path).stem} if path else set()
        if text in {
            str(profile.get("profile_id", "")),
            str(profile.get("candidate_id", "")),
            path,
            *path_names,
        }:
            return profile
    return None


def selected_profile_ids_include_selector(
    profiles: list[dict],
    selected_ids: list[str],
    selector: str,
) -> bool:
    selected = {str(value) for value in selected_ids}
    if not selected or not str(selector or "").strip():
        return False
    profile = profile_for_selector(profiles, selector)
    return bool(profile and str(profile.get("profile_id", "")) in selected)


def profile_can_apply(profile: dict) -> bool:
    return bool(profile.get("final_verified", False))


def profile_can_verify(profile: dict) -> bool:
    return bool(str(profile.get("path", "")).strip())


def profile_verify_selector(profile: dict) -> str:
    path = str(profile.get("path", "")).strip()
    if path:
        return path
    return str(profile.get("profile_id", "")).strip()


def profile_is_deletable(profile: dict) -> bool:
    return bool(str(profile.get("path", "")).strip())


def profile_row_applicable(profile: dict) -> bool:
    """A table row offers Apply: verified, or an imported Afterburner row."""
    return profile_can_apply(profile) or _afterburner_runtime_row(profile)


def profile_row_deletable(profile: dict) -> bool:
    """A table row offers Delete: a saved file, or an imported Afterburner row."""
    return profile_is_deletable(profile) or _afterburner_runtime_row(profile)


def _afterburner_runtime_row(profile: dict) -> bool:
    return str(profile.get("runtime_source", "")) == "afterburner"


def profile_matches_gpu_target(
    profile: dict,
    target_gpu_uuid: str,
    *,
    target_chosen: bool,
    single_gpu: bool,
) -> bool:
    """A bound profile applies only on its own GPU; an unbound (legacy) one
    only where the GPU is unambiguous: a machine with one GPU."""
    compatibility = profile_gpu_compatibility(profile, target_gpu_uuid)
    if compatibility == GPU_COMPATIBILITY_MATCH:
        return bool(target_chosen)
    if compatibility == GPU_COMPATIBILITY_LEGACY:
        return bool(single_gpu and target_chosen)
    return False


def apply_refusal(
    profile: dict,
    target_gpu_uuid: str,
    *,
    target_chosen: bool,
    single_gpu: bool,
) -> str:
    """Why Apply cannot run this profile on the target GPU, or ''."""
    if not target_chosen:
        return CHOOSE_TARGET_TO_APPLY
    if not profile_can_apply(profile):
        return VERIFY_BEFORE_APPLY
    if not profile_matches_gpu_target(
        profile, target_gpu_uuid, target_chosen=target_chosen, single_gpu=single_gpu
    ):
        if profile_gpu_compatibility(profile, target_gpu_uuid) == GPU_COMPATIBILITY_LEGACY:
            return LEGACY_VERIFY_BEFORE_APPLY
        return OTHER_GPU_PROFILE
    return ""


def verify_refusal(profile: dict, target_gpu_uuid: str, *, target_chosen: bool) -> str:
    """Why Verify cannot run this profile on the target GPU, or ''."""
    if not target_chosen:
        return CHOOSE_TARGET_TO_VERIFY
    if profile_gpu_compatibility(profile, target_gpu_uuid) not in {
        GPU_COMPATIBILITY_MATCH,
        GPU_COMPATIBILITY_LEGACY,
    }:
        return OTHER_GPU_PROFILE
    return ""


def legacy_profile_needs_binding(profile: dict, target_gpu_uuid: str) -> bool:
    """An unbound profile with a saved file binds to the GPU it is applied on."""
    return (
        profile_gpu_compatibility(profile, target_gpu_uuid) == GPU_COMPATIBILITY_LEGACY
        and Path(str(profile.get("path") or "")).is_file()
    )


# ---------------------------------------------------------------------------
# Boot, main GPU and silent fan state
# ---------------------------------------------------------------------------


def boot_apply_checked(saved_selector: str, pending_intent: bool) -> bool:
    """Apply on startup shows what boot will do: the daemon's saved entry for
    the GPU, or a freshly ticked box whose entry the next Apply writes. It is
    never shown off while the daemon holds an entry, because every Apply
    sends --boot or --clear-boot from this state."""
    return bool(str(saved_selector or "").strip()) or bool(pending_intent)


def main_gpu_change_allowed(
    checked: bool,
    *,
    target_gpu_uuid: str,
    selection_required: bool,
    has_boot_profile: bool,
) -> bool:
    """Main GPU exists only on multi-GPU machines and can only be set for a
    GPU that has a boot entry; clearing it is always allowed there."""
    if not str(target_gpu_uuid or "").strip() or not selection_required:
        return False
    return not (checked and not has_boot_profile)


def silent_fan_checked_on_load(
    preference: bool,
    running_info: dict[str, object],
    autostart_info: dict[str, object],
) -> bool:
    """The silent-fan tick is sticky: the saved choice survives discarded or
    aborted Auto-UV runs and reloads; a running or boot profile with the fan
    curve also shows it ticked on a fresh install."""
    return (
        bool(preference)
        or bool(running_info.get("silent_fan_curve"))
        or bool(autostart_info.get("silent_fan_curve"))
    )


def defaults_restored_after_status(defaults_restored: bool, running_selector: str) -> bool:
    """A live real profile means a restore-to-stock flag is stale; the stock
    sentinel is not a real profile, so it keeps "Default"."""
    selector = str(running_selector or "").strip()
    if selector and selector != STOCK_PROFILE_SELECTOR:
        return False
    return bool(defaults_restored)


# ---------------------------------------------------------------------------
# Delete safety
# ---------------------------------------------------------------------------


def profile_delete_autostart_action(
    profiles: list[dict],
    selected_ids: list[str],
    autostart_info: dict[str, object],
    *,
    include_legacy_profiles: bool = False,
    store: TierStore = SAVED_TIERS,
) -> dict[str, str]:
    selected = {str(value).strip() for value in selected_ids if str(value).strip()}
    if not selected:
        return {"action": "keep"}
    selector = str(autostart_info.get("selector", "")).strip()
    if not selector:
        return {"action": "keep"}
    if bool(autostart_info.get("adaptive_auto_uv", False)):
        gpu_uuid = str(autostart_info.get("gpu_uuid") or "").strip()
        remaining_profiles = [
            profile
            for profile in profiles
            if str(profile.get("profile_id", "")).strip() not in selected
        ]
        remaining_tiers = adaptive_profile_tier_keys(
            remaining_profiles,
            gpu_uuid=gpu_uuid,
            include_legacy_profiles=include_legacy_profiles,
            store=store,
        )
        if remaining_tiers:
            # Adaptive runtime is valid with a single remaining tier: it
            # applies that tier's curve and simply has nothing to switch to.
            return {"action": "keep"}
        return {
            "action": "restore-stock",
            "reason": "last-usable-adaptive-profile",
        }
    if selected_profile_ids_include_selector(profiles, list(selected), selector):
        return {"action": "restore-stock"}
    return {"action": "keep"}


@dataclass(frozen=True)
class DeleteSafety:
    """Whether deleting a selection restores stock now and at boot, and
    whether that is because it removes the last usable Adaptive tier."""

    restore_stock: bool = False
    last_adaptive: bool = False


def profile_delete_safety(
    profiles: list[dict],
    selected_ids: list[str],
    *,
    autostart_info: dict[str, object],
    running_info: dict[str, object],
    include_legacy_profiles: bool = False,
    store: TierStore = SAVED_TIERS,
) -> DeleteSafety:
    """Deleting the target GPU's boot profile, the running profile (a
    session-only Apply leaves no boot entry), or the last usable Adaptive
    tier restores stock instead of leaving an orphaned curve applied.

    ``running_info`` is empty when no runtime is active."""
    actions = [
        profile_delete_autostart_action(
            profiles,
            list(selected_ids),
            info,
            include_legacy_profiles=include_legacy_profiles,
            store=store,
        )
        for info in (autostart_info, running_info)
    ]
    return DeleteSafety(
        restore_stock=any(a.get("action") == "restore-stock" for a in actions),
        last_adaptive=any(
            a.get("reason") == "last-usable-adaptive-profile" for a in actions
        ),
    )


def delete_confirmation_text(
    names: list[str],
    *,
    restores_stock: bool = False,
    removes_last_usable_adaptive_profile: bool = False,
) -> str:
    clean_names = [str(name).strip() for name in names if str(name).strip()]
    if not clean_names:
        subject = "the selected profiles"
    elif len(clean_names) == 1:
        subject = f"Auto-UV profile {clean_names[0]}"
    else:
        subject = f"{len(clean_names)} selected profiles"
    message = f"Delete {subject}?"
    if restores_stock and removes_last_usable_adaptive_profile:
        if len(clean_names) == 1:
            message += (
                "\n\nThis is the last usable Adaptive Auto-UV profile. "
                "Deleting it will restore stock now and at boot."
            )
        else:
            message += (
                "\n\nThese are the last usable Adaptive Auto-UV profiles. "
                "Deleting them will restore stock now and at boot."
            )
    elif restores_stock:
        message += (
            "\n\nThis profile is currently persisted on startup. Deleting it will "
            "restore stock now and at boot."
        )
    return message


# ---------------------------------------------------------------------------
# Names and runner status
# ---------------------------------------------------------------------------


def profile_status_label(profiles: list[dict], selector: str) -> str:
    profile = profile_for_selector(profiles, selector)
    if profile is None:
        text = str(selector or "").strip()
        if text == "__systemd_default__":
            return "latest Auto-UV profile"
        return text or "unknown profile"
    display_name = str(profile.get("display_name", "")).strip()
    if display_name:
        # Saved name, with a legacy user-edited one refreshed on read.
        return profile_presentation_name(profile)
    text = profile_frequency_voltage(profile)
    return text or profile_display_name(profile) or str(profile.get("profile_id", ""))


def profile_frequency_voltage(profile: dict) -> str:
    # Same phrasing as the profile table and the names of user-edited
    # profiles: one helper owns it.
    return profile_clock_voltage_memory_summary(profile)


def runner_status_parts(
    profiles: list[dict],
    *,
    running_selector: str = "",
    running_adaptive: bool = False,
    autostart_selector: str = "",
    running_silent_fan: bool = False,
    autostart_silent_fan: bool = False,
    defaults_restored: bool = False,
    game_override: bool = False,
    standing_selector: str = "",
    standing_adaptive: bool = False,
) -> tuple[list[str], list[str]]:
    """The status split into what is running and everything behind it.

    Two groups, not one sentence. The headline -- the profile actually applied,
    which is what the bar exists to say -- kept losing the race for width: the
    whole thing runs to ~170 characters and the interesting half sits in the
    middle, so it was the first part to disappear.
    """
    running_selector = str(running_selector or "").strip()
    autostart_selector = str(autostart_selector or "").strip()
    standing_selector = str(standing_selector or "").strip()
    # Daemon running the reserved stock runtime, or a just-completed restore:
    # the GPU is at stock, so report it plainly as Default (no misleading
    # clock/voltage numbers -- those are the V/F ceiling, not the live point).
    if defaults_restored or running_selector == STOCK_PROFILE_SELECTOR:
        autostarts = "Yes" if autostart_selector else "No"
        return (
            ["Currently running profile: Default"],
            [f"Autostart: {autostarts}"],
        )
    if running_selector:
        autostarts = _profile_selectors_match(
            profiles,
            running_selector,
            autostart_selector,
        )
        running_label = profile_status_label(profiles, running_selector)
        markers = ["Adaptive"] if running_adaptive else []
        if game_override:
            # Two writers exist: the Profiles tab sets the standing/boot
            # profile, the Steam tab swaps a per-game one in while the game
            # runs. Show both layers, and judge autostart against the
            # STANDING profile -- the per-game override is transient.
            markers.append("per-game")
        if markers:
            running_label = f"{running_label} ({', '.join(markers)})"
        head = [f"Currently running profile: {running_label}"]
        parts: list[str] = []
        if game_override:
            if standing_selector and standing_selector != STOCK_PROFILE_SELECTOR:
                standing_label = profile_status_label(profiles, standing_selector)
                if standing_adaptive:
                    standing_label = f"{standing_label} (Adaptive)"
            else:
                standing_label = "Default"
            parts.append(f"Standing: {standing_label}")
            autostarts = _profile_selectors_match(
                profiles,
                standing_selector,
                autostart_selector,
            )
        parts.append(f"Autostart: {'Yes' if autostarts else 'No'}")
        parts.append(f"Silent fan curve: {_on_off(running_silent_fan)}")
        if autostart_selector and not autostarts and not game_override:
            parts.append(
                f"Autostart profile: {profile_status_label(profiles, autostart_selector)}"
            )
        return head, parts
    if autostart_selector:
        return (
            [f"Autostart profile: {profile_status_label(profiles, autostart_selector)}"],
            [
                "Autostart: Yes",
                f"Silent fan curve: {_on_off(autostart_silent_fan)}",
                "Not running now",
            ],
        )
    return ["No running/autostart profile available yet."], []


def runner_status_text(
    profiles: list[dict],
    *,
    running_selector: str = "",
    running_adaptive: bool = False,
    autostart_selector: str = "",
    running_silent_fan: bool = False,
    autostart_silent_fan: bool = False,
    defaults_restored: bool = False,
    game_override: bool = False,
    standing_selector: str = "",
    standing_adaptive: bool = False,
) -> str:
    """The same status as one sentence -- tooltips, the CLI, and old callers."""
    head, details = runner_status_parts(
        profiles,
        running_selector=running_selector,
        running_adaptive=running_adaptive,
        autostart_selector=autostart_selector,
        running_silent_fan=running_silent_fan,
        autostart_silent_fan=autostart_silent_fan,
        defaults_restored=defaults_restored,
        game_override=game_override,
        standing_selector=standing_selector,
        standing_adaptive=standing_adaptive,
    )
    text = "; ".join(head + details)
    return text if text.endswith(".") else f"{text}."


def _profile_selectors_match(
    profiles: list[dict],
    left_selector: str,
    right_selector: str,
) -> bool:
    left = str(left_selector or "").strip()
    right = str(right_selector or "").strip()
    if not left or not right:
        return False
    if left == right:
        return True
    left_profile = profile_for_selector(profiles, left)
    right_profile = profile_for_selector(profiles, right)
    if left_profile is None or right_profile is None:
        return False
    return str(left_profile.get("profile_id", "")) == str(
        right_profile.get("profile_id", "")
    )


def _on_off(value: bool) -> str:
    return "On" if bool(value) else "Off"
