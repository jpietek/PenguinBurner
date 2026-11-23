from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from common.penguin_burner_paths import (
    claim_desktop_user_ownership,
    default_user_config_dir,
)
from profiles.uv.profile_fan_curve import (
    AUTO_UV_FAN_CURVE_FILENAME,
    sync_profile_fan_payload,
)
from profiles.uv.profile_store import resolve_auto_uv_profile


class LactExportError(RuntimeError):
    pass


class LactExportRefused(LactExportError):
    """The export cannot start; the message is shown to the user as is."""


LACT_GPU_ID_MISSING = (
    "Could not detect the LACT GPU id. Start LACT once, or choose a "
    "directory that already contains config.yaml."
)
LACT_NO_SILENT_FAN_CURVE = (
    "No runtime-ready silent fan curve is available for this profile."
)


LACT_NVIDIA_DEFAULT_MAX_VF_OFFSET_MHZ = 1000
LACT_CONFIG_FILENAME = "config.yaml"


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError as exc:
        raise LactExportError(f"missing Auto-UV artifact: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LactExportError(f"invalid JSON in Auto-UV artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise LactExportError(f"Auto-UV artifact is not a JSON object: {path}")
    return payload


def _yaml_scalar(value: object) -> str:
    text = str(value)
    if not text:
        return '""'
    safe_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-:/")
    if all(char in safe_chars for char in text):
        return text
    return json.dumps(text)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(float(lower), min(float(value), float(upper)))


def _fan_config_yaml(fan: dict | None) -> tuple[list[str], list[str]]:
    if fan is None:
        return ["    fan_control_enabled: false"], []

    raw_curve = fan.get("curve")
    if not isinstance(raw_curve, list) or not raw_curve:
        raise LactExportError("fan curve has no curve points")

    curve: dict[int, float] = {}
    for point in raw_curve:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise LactExportError(f"invalid fan curve point: {point!r}")
        try:
            temp_c = round(float(point[0]))
            speed_fraction = _clamp(float(point[1]) / 100.0, 0.0, 1.0)
        except (TypeError, ValueError) as exc:
            raise LactExportError(f"invalid fan curve point: {point!r}") from exc
        curve[temp_c] = round(speed_fraction, 4)

    auto_threshold = fan.get("auto_restore_temp_c")
    try:
        auto_threshold_c = round(float(auto_threshold or 0))
    except (TypeError, ValueError):
        auto_threshold_c = 0

    lines = [
        "    fan_control_enabled: true",
        "    fan_control_settings:",
        "      mode: curve",
        "      static_speed: 1.0",
        "      temperature_key: edge",
        f"      interval_ms: {int(float(fan.get('poll_interval_s', 1.0)) * 1000)}",
        "      curve:",
    ]
    for temp_c in sorted(curve):
        lines.append(f"        {temp_c}: {curve[temp_c]:.4g}")
    lines.extend(
        [
            "      spindown_delay_ms: 0",
            f"      change_threshold: {round(float(fan.get('hysteresis_c', 0.0)))}",
            f"      auto_threshold: {auto_threshold_c}",
        ]
    )
    warnings: list[str] = []
    if auto_threshold_c <= 0:
        warnings.append("fan auto_threshold is disabled because no zero-RPM temp was saved")
    return lines, warnings


def _auto_uv_fan_config(fan_payload: dict | None) -> dict | None:
    if fan_payload is None or fan_payload.get("fan_curve_blocked"):
        return None
    fan = fan_payload.get("fan")
    if not isinstance(fan, dict):
        raise LactExportError("Auto-UV fan curve is missing the fan section")
    if fan_payload.get("zero_rpm_until_temperature_c") is not None:
        fan = dict(fan)
        fan["auto_restore_temp_c"] = fan_payload.get("zero_rpm_until_temperature_c")
    return fan


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return round(float(cast(Any, value)))
    except (TypeError, ValueError):
        return None


def _vf_curve_yaml_from_points(
    raw_points: list[dict],
    *,
    max_vf_offset_mhz: int | None = LACT_NVIDIA_DEFAULT_MAX_VF_OFFSET_MHZ,
) -> tuple[list[str], list[str]]:
    if not isinstance(raw_points, list) or not raw_points:
        raise LactExportError("V/F curve has no points")

    points: list[tuple[int, int, int]] = []
    warnings: list[str] = []
    clamped_offsets: list[str] = []
    clamped_negative: list[str] = []
    max_offset = None if max_vf_offset_mhz is None else max(0, int(max_vf_offset_mhz))
    for raw in raw_points:
        if not isinstance(raw, dict):
            raise LactExportError(f"invalid V/F point: {raw!r}")
        try:
            index = int(raw["index"])
            voltage_mv = int(raw["voltage_mv"])
            target_mhz = int(raw["target_mhz"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LactExportError(f"invalid V/F point: {raw!r}") from exc
        if not 0 <= index <= 255:
            raise LactExportError(f"V/F point index is outside LACT range: {index}")
        base_mhz = _optional_int(raw.get("base_mhz"))
        if base_mhz is not None:
            offset_mhz = int(target_mhz) - int(base_mhz)
            if max_offset is not None and offset_mhz > max_offset:
                target_mhz = int(base_mhz) + int(max_offset)
                clamped_offsets.append(
                    f"index={index} voltage={voltage_mv}mV "
                    f"offset={offset_mhz:+d}->{int(max_offset):+d}MHz"
                )
        if target_mhz < 0:
            clamped_negative.append(
                f"index={index} voltage={voltage_mv}mV clockspeed={target_mhz}->0MHz"
            )
            target_mhz = 0
        points.append((index, voltage_mv, target_mhz))

    lines = ["    gpu_vf_curve:"]
    for index, voltage_mv, target_mhz in sorted(points):
        lines.extend(
            [
                f"      {index}:",
                f"        clockspeed: {target_mhz}",
                f"        voltage: {voltage_mv}",
            ]
        )
    if clamped_offsets:
        assert max_offset is not None
        warnings.append(
            "LACT V/F offsets were clamped to "
            f"+{int(max_offset)}MHz over each point's base clock: "
            + "; ".join(clamped_offsets[:8])
            + (" ..." if len(clamped_offsets) > 8 else "")
        )
    if clamped_negative:
        warnings.append(
            "LACT V/F clocks were clamped to non-negative values: "
            + "; ".join(clamped_negative[:8])
            + (" ..." if len(clamped_negative) > 8 else "")
        )
    return lines, warnings


def build_lact_nvidia_config_from_plan(
    *,
    gpu_id: str,
    vf_plan: list[dict] | None,
    fan_config: dict | None = None,
    source_label: str,
    source_vf: str,
    source_fan: str | None = None,
    include_vf_curve: bool = True,
    include_fan_curve: bool = False,
    max_vf_offset_mhz: int | None = LACT_NVIDIA_DEFAULT_MAX_VF_OFFSET_MHZ,
) -> tuple[str, list[str]]:
    gpu_id = str(gpu_id).strip()
    if not gpu_id:
        raise LactExportError("LACT GPU id is required; use `lact cli list-gpus`")

    fan_lines, warnings = _fan_config_yaml(fan_config if include_fan_curve else None)
    if include_vf_curve:
        vf_lines, vf_warnings = _vf_curve_yaml_from_points(
            vf_plan or [],
            max_vf_offset_mhz=max_vf_offset_mhz,
        )
        warnings.extend(vf_warnings)
    else:
        vf_lines = []
    generated_at = datetime.now().astimezone().isoformat()

    lines = [
        "# Generated by PenguinBurner for LACT Nvidia control.",
        f"# Generated at: {generated_at}",
        f"# Source: {source_label}",
        f"# Source V/F curve: {source_vf}",
        f"# Source fan curve: {source_fan or 'none'}",
        "# Apply deliberately, for example: sudo install -m 0644 lact-config.yaml /etc/lact/config.yaml",
        "daemon:",
        "  log_level: info",
        "  disable_nvapi: false",
        "apply_settings_timer: 5",
        "gpus:",
        f"  {_yaml_scalar(gpu_id)}:",
    ]
    lines.extend(fan_lines)
    lines.extend(vf_lines)
    lines.extend(["profiles: {}", "current_profile: null", "auto_switch_profiles: false"])
    return "\n".join(lines) + "\n", warnings


def build_lact_nvidia_config(
    *,
    gpu_id: str,
    final_curve_path: Path | None = None,
    fan_curve_path: Path | None = None,
    profile_selector: str = "",
    include_vf_curve: bool = True,
    include_fan_curve: bool = False,
    max_vf_offset_mhz: int | None = LACT_NVIDIA_DEFAULT_MAX_VF_OFFSET_MHZ,
) -> tuple[str, list[str]]:
    config_dir = default_user_config_dir()
    if final_curve_path is None and include_vf_curve:
        selector = str(profile_selector or "latest").strip() or "latest"
        resolved_profile = resolve_auto_uv_profile(selector)
        final_curve_path = (
            resolved_profile[0]
            if resolved_profile is not None
            else config_dir / "auto-uv-profiles" / "latest-profile.json"
        )
        if resolved_profile is None and selector not in {"active", "latest"}:
            raise LactExportError(f"Auto-UV profile not found: {selector}")
    fan_curve_path = fan_curve_path or config_dir / AUTO_UV_FAN_CURVE_FILENAME
    if include_vf_curve:
        assert final_curve_path is not None
        final_curve_payload = _read_json(final_curve_path)
    else:
        final_curve_payload = {}
    fan_curve_payload = None
    if include_fan_curve and Path(fan_curve_path).is_file():
        fan_curve_payload = _read_json(Path(fan_curve_path))
    raw_points = final_curve_payload.get("points")
    return build_lact_nvidia_config_from_plan(
        gpu_id=gpu_id,
        vf_plan=raw_points,
        fan_config=_auto_uv_fan_config(fan_curve_payload),
        source_label="auto-uv",
        source_vf=str(final_curve_path) if include_vf_curve else "omitted",
        source_fan=str(fan_curve_path),
        include_vf_curve=include_vf_curve,
        include_fan_curve=include_fan_curve,
        max_vf_offset_mhz=max_vf_offset_mhz,
    )


def write_lact_nvidia_config(
    *,
    output_path: Path,
    gpu_id: str,
    final_curve_path: Path | None = None,
    fan_curve_path: Path | None = None,
    profile_selector: str = "",
    include_vf_curve: bool = True,
    include_fan_curve: bool = False,
    max_vf_offset_mhz: int | None = LACT_NVIDIA_DEFAULT_MAX_VF_OFFSET_MHZ,
) -> tuple[Path, list[str]]:
    rendered, warnings = build_lact_nvidia_config(
        gpu_id=gpu_id,
        final_curve_path=final_curve_path,
        fan_curve_path=fan_curve_path,
        profile_selector=profile_selector,
        include_vf_curve=include_vf_curve,
        include_fan_curve=include_fan_curve,
        max_vf_offset_mhz=max_vf_offset_mhz,
    )
    output_path = Path(output_path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    claim_desktop_user_ownership(output_path.parent, include_parents=True)
    output_path.write_text(rendered, encoding="utf-8")
    claim_desktop_user_ownership(output_path)
    return output_path, warnings


def lact_export_output_path(directory: str | Path) -> Path:
    return Path(directory).expanduser() / LACT_CONFIG_FILENAME


def lact_gpu_id_from_config(path: str | Path) -> str:
    """The first GPU key under ``gpus:`` in a LACT config, or empty."""
    path = Path(path).expanduser()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    in_gpus = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not in_gpus:
            if stripped == "gpus:":
                in_gpus = True
            continue
        if not line.startswith((" ", "\t")):
            return ""
        if not line.startswith("  ") or line.startswith("    "):
            continue
        key = stripped.rsplit(":", 1)[0].strip().strip("'\"")
        if key:
            return key
    return ""


def detect_lact_gpu_id(output_dir: str | Path) -> str:
    """The GPU id from an existing config in ``output_dir``, else LACT's own."""
    directory = Path(output_dir).expanduser()
    for config_path in (
        directory / LACT_CONFIG_FILENAME,
        Path("/etc/lact") / LACT_CONFIG_FILENAME,
    ):
        gpu_id = lact_gpu_id_from_config(config_path)
        if gpu_id:
            return gpu_id
    return ""


def export_profile_to_lact(
    profile: dict,
    directory: str | Path,
    *,
    include_fan_curve: bool,
) -> tuple[Path, list[str]]:
    """Profiles → Export LACT: write ``config.yaml`` into ``directory`` for
    the GPU id LACT already uses there (or in /etc/lact). With the silent fan
    curve, the profile's runtime fan payload is synced first.

    Raises ``LactExportRefused`` when the export cannot start and
    ``LactExportError`` (or an OS error) when writing fails."""
    output_path = lact_export_output_path(directory)
    gpu_id = detect_lact_gpu_id(output_path.parent)
    if not gpu_id:
        raise LactExportRefused(LACT_GPU_ID_MISSING)
    if include_fan_curve and not sync_profile_fan_payload(profile):
        raise LactExportRefused(LACT_NO_SILENT_FAN_CURVE)
    return write_lact_profile_config(
        profile,
        output_path=output_path,
        gpu_id=gpu_id,
        include_fan_curve=include_fan_curve,
    )


def write_lact_profile_config(
    profile: dict,
    *,
    output_path: Path,
    gpu_id: str,
    include_fan_curve: bool = False,
) -> tuple[Path, list[str]]:
    """Write a LACT config for a saved Auto-UV profile row."""
    profile_path = Path(str(profile.get("path", "")).strip()).expanduser()
    if not profile_path.is_file():
        raise LactExportError("Selected Auto-UV profile file was not found")
    return write_lact_nvidia_config(
        output_path=output_path,
        gpu_id=gpu_id,
        final_curve_path=profile_path,
        include_vf_curve=True,
        include_fan_curve=include_fan_curve,
    )
