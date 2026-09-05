"""Text-mode Auto-UV final-candidate chooser for interactive CLI scans."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from auto_uv.persistence.auto_uv_persisted_json_files import safe_json_write
from auto_uv.scan_mode.auto_uv_mode import (
    AUTO_UV_MODE_PERFORMANCE,
    normalize_auto_uv_mode,
)
from ui.features.auto_uv.final_choice_ranking import (
    best_final_choice_candidate_id,
    candidate_oc_mhz,
    candidate_short_duration_s,
    final_choice_shows_oc_column,
    sort_candidates_for_final_choice,
)


InputFn = Callable[[str], str]
LogFn = Callable[[str], None]


def handle_cli_final_choice_request(
    payload: dict,
    *,
    input_fn: InputFn = input,
    log: LogFn = print,
) -> bool:
    """Render a terminal choice table and write the backend response JSON."""

    response_path_text = str(payload.get("response_path") or "").strip()
    if not response_path_text:
        return False
    response_path = Path(response_path_text).expanduser()

    candidates = _display_candidates(payload)
    if not candidates:
        safe_json_write(response_path, {"action": "discard"})
        log(
            "Auto-UV final choice: no candidates were available; "
            "starting from scratch."
        )
        return True

    default_id = _display_default_candidate_id(payload, candidates)
    log("")
    for line in format_cli_final_choice_request(
        payload,
        candidates=candidates,
        default_candidate_id=default_id,
    ):
        log(line)

    response = _prompt_for_final_choice_response(
        payload,
        candidates=candidates,
        default_candidate_id=default_id,
        input_fn=input_fn,
        log=log,
    )
    safe_json_write(response_path, response)
    return True


def format_cli_final_choice_request(
    payload: dict,
    *,
    candidates: list[dict] | None = None,
    default_candidate_id: str | None = None,
) -> list[str]:
    auto_uv_mode = str(payload.get("auto_uv_mode") or "").strip()
    request_reason = str(payload.get("request_reason") or "").strip().lower()
    display_candidates = (
        _display_candidates(payload) if candidates is None else candidates
    )
    default_id = (
        _display_default_candidate_id(payload, display_candidates)
        if default_candidate_id is None
        else str(default_candidate_id)
    )
    lines = [
        _request_title(request_reason),
        _intro_text(
            auto_uv_mode,
            request_reason=request_reason,
            recovery_decision=payload.get("recovery_decision"),
        ),
        f"Final verification duration: {_duration_label(_duration_s(payload))}",
    ]
    failed_detail = _recovery_decision_text(payload.get("recovery_decision"))
    if failed_detail and request_reason == "final-verification-failed":
        lines.append(f"Blacklisted failed point: {failed_detail}")
    lines.extend(
        [
            "",
            *_candidate_table_lines(
                display_candidates,
                default_id,
                auto_uv_mode,
                request_reason,
            ),
        ]
    )
    return lines


def _display_candidates(payload: dict) -> list[dict]:
    candidates = [
        dict(candidate)
        for candidate in payload.get("candidates", [])
        if isinstance(candidate, dict)
    ]
    request_reason = str(payload.get("request_reason") or "").strip().lower()
    if request_reason == "previous-crash":
        return candidates
    return sort_candidates_for_final_choice(
        candidates,
        str(payload.get("auto_uv_mode") or "").strip(),
    )


def _display_default_candidate_id(payload: dict, candidates: list[dict]) -> str:
    requested_default = str(payload.get("default_candidate_id") or "").strip()
    candidate_ids = {
        str(candidate.get("candidate_id") or "").strip() for candidate in candidates
    }
    request_reason = str(payload.get("request_reason") or "").strip().lower()
    if (
        request_reason == "previous-crash" or request_reason.startswith("adaptive-")
    ) and requested_default in candidate_ids:
        return requested_default
    best_id = best_final_choice_candidate_id(
        candidates,
        str(payload.get("auto_uv_mode") or "").strip(),
    )
    if best_id:
        return best_id
    if requested_default in candidate_ids:
        return requested_default
    return str(candidates[0].get("candidate_id") or "").strip()


def _candidate_table_lines(
    candidates: list[dict],
    default_candidate_id: str,
    auto_uv_mode: str,
    request_reason: str,
) -> list[str]:
    show_oc = final_choice_shows_oc_column(auto_uv_mode)
    headers = ["#", "mV", "Target MHz"]
    if show_oc:
        headers.append("OC MHz")
    headers.extend(
        ["Effective MHz", "FPS/W", "FPS", "Power W", "Short Check", "Status"]
    )

    rows = []
    for index, candidate in enumerate(candidates, start=1):
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        row = [
            str(index),
            _number(candidate.get("candidate_voltage_mv"), precision=0),
            _number(candidate.get("lock_clock_mhz"), precision=0),
        ]
        if show_oc:
            row.append(_oc_text(candidate))
        row.extend(
            [
                _number(candidate.get("avg_core_clock_mhz"), precision=2),
                _metric_text(candidate, "efficiency_fps_per_w", precision=4),
                _metric_text(candidate, "avg_fps", precision=2),
                _metric_text(
                    candidate,
                    "avg_power_w",
                    precision=2,
                    lower_is_better=True,
                ),
                _duration_label(candidate_short_duration_s(candidate)),
                _status_text(
                    candidate,
                    is_default=candidate_id == default_candidate_id,
                    auto_uv_mode=auto_uv_mode,
                    request_reason=request_reason,
                ),
            ]
        )
        rows.append(row)

    widths = [
        max(len(str(row[column])) for row in [headers, *rows])
        for column in range(len(headers))
    ]
    lines = [
        _format_row(headers, widths),
        _format_row(["-" * width for width in widths], widths),
    ]
    lines.extend(_format_row(row, widths) for row in rows)
    return lines


def _prompt_for_final_choice_response(
    payload: dict,
    *,
    candidates: list[dict],
    default_candidate_id: str,
    input_fn: InputFn,
    log: LogFn,
) -> dict:
    by_id = {
        str(candidate.get("candidate_id") or "").strip().casefold(): candidate
        for candidate in candidates
    }
    request_reason = str(payload.get("request_reason") or "").strip().lower()
    recovery_prompt = request_reason in {
        "previous-crash",
        "final-verification-failed",
    }
    duration_s = _duration_s(payload)
    options = "row/id, Enter=default, s=start from scratch, a=abort"
    if not recovery_prompt:
        options = "row/id, Enter=default, d=discard"
    prompt = f"Select Auto-UV candidate [{default_candidate_id}] ({options}): "

    while True:
        try:
            raw_answer = input_fn(prompt)
        except EOFError:
            raw_answer = ""
        answer = str(raw_answer or "").strip()
        if not answer:
            return {
                "candidate_id": default_candidate_id,
                "final_verification_duration_s": int(duration_s),
            }
        lowered = answer.casefold()
        if lowered in {"a", "abort", "q", "quit"}:
            return {"action": "abort" if recovery_prompt else "discard"}
        if lowered in {"d", "discard", "s", "scratch", "start", "start-over"}:
            return {"action": "discard"}
        selected = _candidate_from_answer(answer, candidates, by_id)
        if selected is not None:
            return {
                "candidate_id": str(selected.get("candidate_id") or "").strip(),
                "final_verification_duration_s": int(duration_s),
            }
        log("Invalid selection. Enter a row number, candidate id, or command.")


def _candidate_from_answer(
    answer: str,
    candidates: list[dict],
    by_id: dict[str, dict],
) -> dict | None:
    try:
        row_number = int(answer)
    except ValueError:
        row_number = 0
    if 1 <= row_number <= len(candidates):
        return candidates[row_number - 1]
    return by_id.get(str(answer).strip().casefold())


def _request_title(request_reason: str) -> str:
    if request_reason == "previous-crash":
        return "Resume Auto-UV Candidate"
    if request_reason == "final-verification-failed":
        return "Choose Safer Auto-UV Candidate"
    return "Choose Final verification candidate"


def _intro_text(
    auto_uv_mode: str,
    *,
    request_reason: str,
    recovery_decision: object,
) -> str:
    metric_text = (
        "highest FPS"
        if normalize_auto_uv_mode(auto_uv_mode) == AUTO_UV_MODE_PERFORMANCE
        else "best FPS/W"
    )
    detail = _recovery_decision_text(recovery_decision)
    if request_reason == "previous-crash":
        suffix = f" Previous decision: {detail}" if detail else ""
        return (
            "A previous Auto-UV run ended during a GPU probe. Choose a saved "
            "candidate to resume from a safer voltage bin, or start a new scan "
            f"from scratch.{suffix}"
        )
    if request_reason == "final-verification-failed":
        suffix = f" Failed candidate: {detail}." if detail else ""
        return (
            "Final verification failed. Choose another passed short-probe "
            "candidate at a safer voltage, or start a new scan from scratch. "
            f"The {metric_text} remaining candidate is selected by default."
            f"{suffix}"
        )
    if request_reason == "user-stop":
        return (
            "Auto-UV was stopped. Choose one of the previously stable candidates. "
            f"The {metric_text} candidate is selected for the long Final verification."
        )
    return (
        "The short candidate sweep is complete. "
        f"The {metric_text} passed candidate is selected for the long Final verification."
    )


def _status_text(
    candidate: dict,
    *,
    is_default: bool,
    auto_uv_mode: str,
    request_reason: str,
) -> str:
    parts = []
    if is_default:
        if request_reason == "previous-crash":
            parts.append("Resume from here")
        elif request_reason == "final-verification-failed":
            parts.append("Next safer pick")
        elif request_reason.startswith("adaptive-"):
            parts.append("Tier suggestion")
        else:
            parts.append(
                "Highest FPS"
                if normalize_auto_uv_mode(auto_uv_mode) == AUTO_UV_MODE_PERFORMANCE
                else ("Recommended" if normalize_auto_uv_mode(auto_uv_mode) == "efficiency" else "Best FPS/W")
            )
    parts.append(
        "Final stability verified"
        if bool(candidate.get("final_verified"))
        else "Passed short probe"
    )
    return " | ".join(parts)


def _metric_text(
    candidate: dict,
    metric: str,
    *,
    precision: int,
    lower_is_better: bool = False,
) -> str:
    value_text = _number(candidate.get(metric), precision=precision)
    if not value_text:
        return ""
    delta = _metric_delta_percent(
        candidate.get(metric),
        _profile_base_metric(candidate, metric),
    )
    if delta is None:
        return value_text
    _ = lower_is_better
    return f"{value_text} ({delta:+.2f}%)"


def _profile_base_metric(candidate: dict, metric: str):
    base_key = f"base_{metric}"
    return candidate.get(base_key)


def _metric_delta_percent(current, baseline) -> float | None:
    current_number = _float_or_none(current)
    baseline_number = _float_or_none(baseline)
    if current_number is None or baseline_number in (None, 0.0):
        return None
    return ((current_number - float(baseline_number)) / float(baseline_number)) * 100.0


def _oc_text(candidate: dict) -> str:
    rounded = candidate_oc_mhz(candidate)
    if rounded is None:
        return ""
    return f"{rounded:+d}"


def _recovery_decision_text(recovery_decision: object) -> str:
    if not isinstance(recovery_decision, dict):
        return ""
    voltage = _number(recovery_decision.get("candidate_voltage_mv"), precision=0)
    clock = _number(recovery_decision.get("lock_clock_mhz"), precision=0)
    decision = str(recovery_decision.get("decision") or "").strip()
    prefix = f"{voltage}mV@{clock}MHz" if voltage and clock else ""
    if prefix and decision:
        return f"{prefix} - {decision}"
    return decision or prefix


def _duration_s(payload: dict) -> int:
    try:
        return max(1, int(round(float(payload.get("final_verification_duration_s")))))
    except (TypeError, ValueError):
        return 1


def _duration_label(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    remaining_s = seconds % 60
    if remaining_s:
        return f"{minutes}min {remaining_s}s"
    return f"{minutes}min"


def _number(value, *, precision: int) -> str:
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    precision = max(0, min(int(precision), 4))
    return str(int(round(number))) if precision <= 0 else f"{number:.{precision}f}"


def _float_or_none(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _format_row(values: list[str], widths: list[int]) -> str:
    cells = []
    for index, value in enumerate(values):
        text = str(value)
        if index == 0 or index == len(values) - 1:
            cells.append(text.ljust(widths[index]))
        else:
            cells.append(text.rjust(widths[index]))
    return "  ".join(cells)
