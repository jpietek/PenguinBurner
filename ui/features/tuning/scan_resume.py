"""Restore the Auto-UV presentation only after the user starts a matching scan."""

from __future__ import annotations


def restore_scan_presentation(window, payload: dict) -> None:
    events = payload.get("events", [])
    window.runs_table.clear()
    window.vf_plot.clear()
    if any(item["event"] == "tier_started" for item in events):
        window.auto_uv_tier_progress.start()
    else:
        window.auto_uv_tier_progress.clear()
    safe_probe = None
    safe_curve = None
    candidate_curve = None
    for item in events:
        event, data = item["event"], item["payload"]
        restored = {"event": event, **data, "restored": True}
        if event == "probe_result":
            restored["reason"] = "Saved measurement: " + str(
                data.get("reason") or "passed"
            )
        window._handle_scan_event(restored)
        if event == "candidate_curve":
            candidate_curve = data
        elif event == "probe_result" and data.get("decision") == "pass":
            safe_probe = data
            if candidate_curve and all(
                candidate_curve.get(key) == data.get(key)
                for key in ("voltage_mv", "clock_mhz")
            ):
                safe_curve = candidate_curve
    if safe_curve:
        window._handle_scan_event(
            {"event": "candidate_curve", **safe_curve, "restored": True}
        )
    if safe_probe:
        window.vf_plot.set_probe_marker(safe_probe)
        window.vf_plot.set_load_markers(safe_probe)
        window.header.set_candidate(
            f"{safe_probe.get('voltage_mv')}mV @ {safe_probe.get('clock_mhz')}MHz (saved)"
        )
    window.header.set_stage("Resuming")
    window.controls.hide_dependency_progress()
    window.controls.set_status_text(
        "Restored previous scan measurements and curves. Continuing from saved progress."
    )
