from __future__ import annotations

from auto_uv.domain.types import AutoUvProbeSummary


def probe_indicates_power_saturation(
    probe: AutoUvProbeSummary,
    *,
    power_limit_w: int | None,
    power_saturation_headroom_pct: float = 2.0,
    require_power_evidence: bool = False,
) -> bool:
    """Whether this probe ran against the board-power wall.

    The driver's perf-cap reason alone is a weak signal: Blackwell summarizes
    a power cap on probes drawing well under the configured limit (287W of a
    319W cap in the 2026-08-04 log). That can justify trying a clock-reclaim
    pass after voltage descent. Ending a climb requires stronger evidence:
    callers pass ``require_power_evidence`` to require measured power at the
    limit or the board's explicit hardware-brake bit.
    """
    perf_cap_reason = str(getattr(probe, "perf_cap_reason", "") or "").lower()
    perf_cap_tokens = {
        token.strip()
        for token in perf_cap_reason.replace(",", "+").split("+")
        if token.strip()
    }
    # Unlike the driver's loose sw-power summary, this bit is direct evidence
    # that the board's power-delivery protection is already limiting clocks.
    # Do not keep climbing merely because board power is below the software
    # cap: EDPp/OCP can assert before that aggregate limit is reached.
    #
    # Read the counted samples first: the brake is transient by nature, and
    # the summarizer drops a minority power token from ``perf_cap_reason``
    # entirely, so a real brake reaches this decision only through the count
    # that survives summarization.
    if int(getattr(probe, "hw_power_brake_samples", 0) or 0) > 0:
        return True
    if "hw-power-brake" in perf_cap_tokens:
        return True
    if not require_power_evidence and any(
        "power" in token for token in perf_cap_tokens
    ):
        return True
    avg_power_w = getattr(probe, "avg_power_w", None)
    if avg_power_w is None or power_limit_w is None or int(power_limit_w) <= 0:
        return False
    saturation_floor_w = float(power_limit_w) * (
        1.0 - max(0.0, float(power_saturation_headroom_pct)) / 100.0
    )
    return float(avg_power_w) >= float(saturation_floor_w)
