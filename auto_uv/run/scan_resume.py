"""Select a measured recovery point with frequency and voltage headroom."""

from __future__ import annotations

from auto_uv.curve.base_vf_curve_voltage_bins import next_higher_editable_voltage_bin
from auto_uv.curve.flattened_voltage_probe_curve import (
    build_flattened_voltage_probe_curve,
)
from auto_uv.domain.types import AutoUvError, VfCurveCandidate
from auto_uv.persistence.unsafe_voltage_cache import unsafe_voltage_block_reason


def recovery_candidate(
    base_curve: list[dict],
    passed: list[dict],
    *,
    tier: str,
    unsafe: list[dict],
    failed_clock_mhz: int | None,
    failed_voltage_mv: int | None = None,
) -> VfCurveCandidate:
    """Pick the last passed point below the failure, then step one voltage bin up.

    A descent holds one requested clock while voltage falls, so the crashed
    probe shares its clock with every earlier pass of that tier. A pass at the
    failed clock is still a safe source when its voltage was higher than the
    one that failed; only a higher clock, or an equal-or-lower voltage at the
    same clock, is outside the margin.
    """
    for item in reversed(passed):
        if item["tier"] != tier:
            continue
        candidate = item["candidate"]
        if failed_clock_mhz is not None:
            if candidate.target_mhz > failed_clock_mhz:
                continue
            if candidate.target_mhz == failed_clock_mhz and (
                failed_voltage_mv is None or candidate.voltage_mv <= failed_voltage_mv
            ):
                continue
        if unsafe_voltage_block_reason(
            unsafe,
            candidate_voltage_mv=candidate.voltage_mv,
            lock_clock_mhz=candidate.target_mhz,
            profile_tier=tier,
        ):
            continue
        voltage = next_higher_editable_voltage_bin(base_curve, candidate.voltage_mv)
        if voltage is None:
            continue
        if unsafe_voltage_block_reason(
            unsafe,
            candidate_voltage_mv=voltage,
            lock_clock_mhz=candidate.target_mhz,
            profile_tier=tier,
        ):
            continue
        return build_flattened_voltage_probe_curve(
            base_curve,
            candidate_voltage_mv=voltage,
            target_clock_mhz=candidate.target_mhz,
            tail_rise_bins=int(candidate.metadata.get("tail_rise_bins", 0)),
            label="Resume verification",
            metadata={
                **candidate.metadata,
                "resume_recovery": True,
                "resume_source_voltage_mv": candidate.voltage_mv,
                "resume_source_clock_mhz": candidate.target_mhz,
            },
        )
    raise AutoUvError(
        "Cannot resume: no passed candidate satisfies the clock and voltage safety margins"
    )


def completed_ui_events(
    events: list[dict], unsafe: list[dict], mode: str
) -> list[dict]:
    """Keep completed, allowed probes; omit failed/incomplete rows and their curves."""
    restored = []
    completed = {
        item["payload"].get("tier")
        for item in events
        if item["event"] == "tier_completed"
    }
    pending = []
    tier = "efficiency" if mode == "adaptive" else mode
    for item in events:
        event, payload = item["event"], item["payload"]
        if event == "tier_started":
            tier = str(payload.get("tier") or tier)
        if event in {"candidate_curve", "probe_start"}:
            # Final display curves aren't followed by another result.
            if event == "candidate_curve" and payload.get("stage") == "final":
                restored.append(item)
            elif event == "candidate_curve":
                pending = [item]
            else:
                if pending and any(
                    pending[-1]["payload"].get(key) != payload.get(key)
                    for key in ("stage", "voltage_mv", "clock_mhz")
                ):
                    pending = []
                pending.append(item)
        elif event == "tier_confirmed" and payload.get("tier") not in completed:
            continue
        elif event == "probe_result":
            allowed = payload.get(
                "decision"
            ) == "pass" and not unsafe_voltage_block_reason(
                unsafe,
                candidate_voltage_mv=int(payload.get("voltage_mv") or 0),
                lock_clock_mhz=int(payload.get("clock_mhz") or 0),
                profile_tier=tier,
            )
            if allowed:
                restored.extend(pending)
                restored.append(item)
            pending = []
        else:
            restored.append(item)
    return restored
