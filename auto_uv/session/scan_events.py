"""Parse the Auto-UV scan stream's JSON events into values the UI shows."""

from __future__ import annotations

import math
import re


def event_points(payload: dict) -> list[tuple[float, float]]:
    return _points_from_values(
        payload.get("points"),
        x_keys=("voltage_mv",),
        y_keys=("clock_mhz",),
    )


def event_base_points(payload: dict) -> list[tuple[float, float]]:
    return _points_from_values(
        payload.get("points"),
        x_keys=("voltage_mv",),
        y_keys=("base_mhz", "base_clock_mhz"),
    ) or event_points(payload)


def fan_points(payload: dict) -> list[tuple[float, float]]:
    return _points_from_values(
        payload.get("points") or payload.get("curve_points"),
        x_keys=("temperature_c", "temp_c"),
        y_keys=("fan_pct", "speed_pct", "fan_speed_pct"),
    )


def fan_measurement_point(payload: dict) -> tuple[float, float] | None:
    temp = _number(payload.get("temp_c") or payload.get("temperature_c"))
    fan = _number(payload.get("fan_pct") or payload.get("fan_speed_pct"))
    if temp is None or fan is None:
        return None
    return temp, fan


def candidate_id_from_payload(payload: dict) -> str:
    candidate_id = str(payload.get("candidate_id", "")).strip()
    if candidate_id:
        return candidate_id
    voltage = _number(payload.get("candidate_voltage_mv") or payload.get("voltage_mv"))
    clock = _number(payload.get("lock_clock_mhz") or payload.get("clock_mhz"))
    if voltage is None or clock is None:
        return ""
    return f"{round(voltage)}mv-{round(clock)}mhz"


def stage_title(value) -> str:
    raw = str(value or "").strip().lower()
    if raw == "resume-verify":
        return "Resume verification"
    if raw in {"base-baseline", "stock-baseline"}:
        return "Baseline"
    text = raw.replace("_", "-").replace("-", " ").strip()
    if "candidate" in text:
        return "Undervolting Candidates Sweep"
    if not text:
        return "Probe"
    return text.title()


def status_value(value, *, precision: int = 2) -> str:
    number = _number(value)
    if number is None:
        return ""
    if math.isclose(number, round(number), abs_tol=0.005):
        return str(round(number))
    return f"{number:.{max(0, int(precision))}f}"


def top_status_text(text: str) -> str:
    """A scan line for the window: a structured ``Auto-UV phase=...`` probe
    line becomes a readable summary; anything else is kept, rounded."""
    text = str(text or "").strip()
    return live_status_text(text) or _round_gui_decimals(text)


_LIVE_PHASES = {
    "candidate-live": "Testing",
    "final-verify-live": "Final verification",
    "resume-verify-live": "Resume verification",
    "baseline-live": "Baseline",
}
_WORKLOADS = {"q2rtx": "Q2RTX", "cuda": "CUDA"}


def live_status_text(line: str) -> str:
    """``Auto-UV phase=final-verify-live candidate=860mV target=2800MHz
    elapsed=71.40s running=q2rtx power=263.30W core_clock=2745MHz temp=62C
    fan=40%`` -> ``Final verification · 860 mV @ 2800 MHz · 1:11 · Q2RTX ·
    263 W · 2745 MHz · 62 °C · fan 40%``. The tier is left out (the tier
    strip shows it). "" for a line without live probe fields."""
    if not line.startswith("Auto-UV phase=") or "candidate=" not in line:
        return ""
    fields: dict[str, str] = {}
    starting = False
    for token in line[len("Auto-UV ") :].split():
        key, sep, value = token.partition("=")
        if sep and key not in fields:
            fields[key] = value
        elif key == "phase" and value == "startup":
            starting = True  # the workload is still starting up
    phase = fields.get("phase", "")
    kind = next((k for k in _LIVE_PHASES if phase.endswith(k)), "")
    parts = [_LIVE_PHASES.get(kind) or phase.replace("-", " ").capitalize()]
    if starting:
        parts.append("starting")
    volts, clock = _number(fields.get("candidate", "").rstrip("mV")), _number(
        fields.get("target", "").rstrip("MHz")
    )
    if volts is not None and clock is not None:
        parts.append(f"{round(volts)} mV @ {round(clock)} MHz")
    elapsed = _number(fields.get("elapsed", "").rstrip("s"))
    if elapsed is not None:
        minutes, seconds = divmod(int(elapsed), 60)
        parts.append(f"{minutes}:{seconds:02d}")
    workload = fields.get("running", "")
    if workload:
        parts.append(_WORKLOADS.get(workload, workload))
    for key, unit, label in (
        ("power", "W", "{} W"),
        ("core_clock", "MHz", "{} MHz"),
        ("temp", "C", "{} °C"),
        ("fan", "%", "fan {}%"),
    ):
        value = _number(fields.get(key, "").rstrip(unit))
        if value is not None:
            parts.append(label.format(round(value)))
    return " · ".join(part for part in parts if part)


def probe_decision_label(payload: dict) -> str:
    decision = str(payload.get("decision", payload.get("status", ""))).strip().lower()
    if decision in {"pass", "accept", "accepted"}:
        return "Pass"
    if decision == "running":
        return "Running"
    if decision == "stopping":
        return "Stopping"
    if decision in {"fail", "failed", "error", "stopped"}:
        return "Failed"
    if not decision:
        return ""
    return "Failed"


def probe_failure_label(payload: dict) -> str:
    reason = str(payload.get("reason", "")).strip().lower()
    kind = str(payload.get("failure_kind", "")).strip().lower()
    fatal_matches = " ".join(
        str(value).lower() for value in payload.get("fatal_output_matches", []) or []
    )
    if "device lost" in fatal_matches or "vk_error_device_lost" in fatal_matches:
        return "Vulkan device lost"
    if kind == "low-clock" or "core clock" in reason or (
        "clock" in reason and "floor" in reason
    ):
        return "Clock too low"
    if kind == "fps-regression":
        if "single-run" in reason:
            return "Single run FPS low"
        return "Average FPS low"
    if kind == "frame-count-regression":
        return "Frame count changed"
    if kind in {"load-lost"} or "load" in reason:
        return "GPU load too low"
    if kind in {"metrics-missing", "metrics-invalid"}:
        return "Metrics invalid"
    if kind == "cuda-failed" or reason.startswith("cuda"):
        return "CUDA failed"
    if kind == "nvidia-xid":
        return "Nvidia Xid fail"
    if kind == "fatal-output":
        return "Fatal output"
    if kind == "timed-out" or "timeout" in reason:
        return "Timed out"
    if kind == "user-stop":
        return "Stopped"
    if kind == "q2rtx-failed":
        return "Q2RTX failed"
    return "Failed"


def probe_reason_tooltip(payload: dict) -> str:
    reason = str(payload.get("reason", "")).strip()
    kind = str(payload.get("failure_kind", "")).strip()
    severity = str(payload.get("failure_severity", "")).strip()
    log_path = str(payload.get("log_path", "")).strip()
    parts = []
    if reason:
        parts.append(reason)
    if kind:
        parts.append(f"kind: {kind}")
    if severity:
        parts.append(f"severity: {severity}")
    fatal_matches = list(payload.get("fatal_output_matches", []) or [])
    if fatal_matches:
        parts.append("fatal output: " + ", ".join(str(value) for value in fatal_matches))
    if log_path:
        parts.append(f"log: {log_path}")
    return "\n".join(parts)


def _points_from_values(
    values,
    *,
    x_keys: tuple[str, ...],
    y_keys: tuple[str, ...],
) -> list[tuple[float, float]]:
    if not isinstance(values, list):
        return []
    points: list[tuple[float, float]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        x = _first_number(value, x_keys)
        y = _first_number(value, y_keys)
        if x is None or y is None:
            continue
        points.append((x, y))
    return points


def _first_number(values: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        number = _number(values.get(key))
        if number is not None:
            return number
    return None


def _number(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_DECIMAL_TEXT_RE = re.compile(
    r"(?<![A-Za-z0-9_.+-])([+-]?\d+\.\d+(?:[eE][+-]?\d+)?)(?![\d.])"
)


def _round_gui_decimals(text: str, *, precision: int = 2) -> str:
    precision = max(0, min(int(precision), 2))

    def replace(match: re.Match[str]) -> str:
        raw = match.group(1)
        try:
            number = float(raw)
        except ValueError:
            return raw
        if not math.isfinite(number):
            return raw
        return f"{number:.{precision}f}"

    return _DECIMAL_TEXT_RE.sub(replace, str(text or ""))
