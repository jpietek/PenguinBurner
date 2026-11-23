from __future__ import annotations

from pathlib import Path

from cli.runtime_config_file import runtime_gpu_index
from common.penguin_burner_paths import (
    default_runtime_config_path,
    discover_afterburner_device_profiles,
    managed_afterburner_root,
    resolve_afterburner_root,
    sync_afterburner_export_tree,
)
from drivers.nvidia.daemon_gpu import DaemonGpuClient
from drivers.nvidia.hidden_nvapi_vf import create_hidden_vf_curve_reader
from integrations.afterburner.fan_curve import load_afterburner_fan_settings
from integrations.afterburner.import_vf_curve import (
    build_plan,
    load_afterburner_runtime_options,
    persist_afterburner_import,
)
from integrations.afterburner.vfcurve import (
    discover_afterburner_vf_sections,
    resolve_afterburner_vf_source,
)
from integrations.afterburner.vfcurve_describe import (
    describe_afterburner_flatten_validation,
)
from profiles.gpu_identity import normalized_gpu_identity
from profiles.uv.profile_store import archive_auto_uv_profile


def configured_afterburner_root() -> str:
    try:
        options = load_afterburner_runtime_options(default_runtime_config_path())
    except Exception:  # noqa: BLE001
        return ""
    return str(options.get("afterburner_root", "")).strip()


def afterburner_profile_entries(afterburner_root: str | Path) -> list[dict]:
    root = resolve_afterburner_root(afterburner_root).expanduser()
    missing = []
    if not (root / "MSIAfterburner.cfg").is_file():
        missing.append("MSIAfterburner.cfg")
    if not (root / "Profiles").is_dir():
        missing.append("Profiles/")
    if missing:
        raise FileNotFoundError(
            "Invalid Afterburner directory: missing "
            + ", ".join(missing)
            + f" under {root}"
        )

    entries = []
    for device_profile in discover_afterburner_device_profiles(root):
        for section in discover_afterburner_vf_sections(device_profile):
            if bool(section.get("is_builtin")):
                continue
            status, importable = afterburner_profile_status(section)
            entries.append(
                {
                    "afterburner_root": str(root),
                    "profile_path": str(Path(device_profile).resolve()),
                    "device_profile_relative_path": relative_profile_path(
                        root,
                        device_profile,
                    ),
                    "device_profile_name": Path(device_profile).name,
                    "section": str(section.get("section", "")),
                    "target": afterburner_profile_target_text(section),
                    "target_voltage_mv": afterburner_profile_target_value(
                        section,
                        "lock_voltage_mv",
                    ),
                    "target_clock_mhz": afterburner_profile_target_value(
                        section,
                        "lock_clock_mhz",
                    ),
                    "curve_points": afterburner_section_curve_points(section),
                    "status": status,
                    "importable": bool(importable),
                }
            )
    return sorted(
        entries,
        key=lambda entry: (
            0 if bool(entry.get("importable")) else 1,
            str(entry.get("device_profile_name", "")).lower(),
            str(entry.get("section", "")).lower(),
        ),
    )


def persist_afterburner_import_selection(entry: dict) -> dict:
    if not bool(entry.get("importable")):
        raise ValueError("selected Afterburner profile is not importable")
    config_path = default_runtime_config_path()
    source_root = resolve_afterburner_root(entry.get("afterburner_root", "")).resolve()
    section = str(entry.get("section", "")).strip()
    if not section:
        raise ValueError("selected Afterburner profile has no section name")
    source_profile_path = Path(str(entry.get("profile_path", ""))).expanduser()
    if not source_profile_path.is_file():
        raise FileNotFoundError(f"Afterburner profile file not found: {source_profile_path}")
    device_profile_relative_path = relative_profile_path(source_root, source_profile_path)
    managed_root = sync_afterburner_export_tree(source_root, managed_afterburner_root())
    source = resolve_afterburner_vf_source(
        afterburner_root=managed_root,
        section=section,
        device_profile_hint=device_profile_relative_path,
    )
    section_info = source.get("section_info", {})
    gpu_index = runtime_gpu_index(config_path)
    identity = normalized_gpu_identity(
        DaemonGpuClient(gpu_index).capabilities().identity,
        index_at_verification=gpu_index,
    )
    if not identity.get("uuid"):
        raise RuntimeError("could not identify the GPU used for this import")
    reader = create_hidden_vf_curve_reader(gpu_index=gpu_index)
    if reader is None:
        raise RuntimeError("could not open the live Nvidia V/F curve reader")
    try:
        plan, missing_voltage_bins = build_plan(
            reader,
            section_info["materialization"]["points"],
        )
    finally:
        reader.close()
    if missing_voltage_bins:
        raise ValueError(
            "Afterburner profile did not cover Linux voltage bins: "
            + ", ".join(str(item) for item in missing_voltage_bins[:16])
            + (" ..." if len(missing_voltage_bins) > 16 else "")
        )
    clock, voltage = afterburner_profile_target_pair(section_info)
    if clock is None or voltage is None:
        raise ValueError("selected Afterburner profile has no lock clock/voltage")
    display_name = f"MSI Afterburner {section} {clock} MHz {voltage} mV"
    profile_payload = {
        "profile_source": "MSI Afterburner",
        "display_name": display_name,
        "candidate_id": (
            f"afterburner-{Path(device_profile_relative_path).stem}-"
            f"{section}-{voltage}mv-{clock}mhz"
        ),
        "candidate_voltage_mv": int(voltage),
        "lock_clock_mhz": int(clock),
        "final_verified": True,
        "verification_status": "imported",
        "gpu_identity": identity,
        "plan": plan,
        "points": plan,
        "flatten_target": dict(section_info.get("flatten_target") or {}),
        "curve_points": afterburner_section_curve_points(section_info),
        "afterburner_import": {
            "root": str(managed_root),
            "device_profile": str(device_profile_relative_path),
            "section": str(section),
            "source_profile_path": str(source_profile_path),
        },
    }
    fan_payload = afterburner_fan_curve_payload(managed_root)
    if fan_payload is not None:
        profile_payload["fan_curve_payload"] = fan_payload
    profile_path = archive_auto_uv_profile(profile_payload)
    runtime_options = load_afterburner_runtime_options(config_path)
    runtime_options["afterburner_root"] = str(managed_root)
    persist_afterburner_import(
        config_path,
        runtime_gpu_index(config_path),
        managed_root,
        None,
        None,
        runtime_options=runtime_options,
    )
    return {
        "afterburner_root": str(managed_root),
        "device_profile_relative_path": str(device_profile_relative_path),
        "section": str(section),
        "config_path": str(config_path),
        "profile_path": str(profile_path),
        "profile_id": profile_path.stem.removeprefix("auto-uv-profile-"),
        "display_name": display_name,
    }


def afterburner_fan_curve_payload(afterburner_root: str | Path) -> dict | None:
    try:
        settings = load_afterburner_fan_settings(Path(afterburner_root))
    except Exception:  # noqa: BLE001
        return None
    curve_points = fan_curve_points(settings.get("curve", {}).get("points"))
    if not curve_points:
        return None
    return {
        "source": "MSI Afterburner",
        "fan": {"curve": curve_points},
        "reference_curve": fan_curve_points(settings.get("curve2", {}).get("points")),
        "profile_path": str(settings.get("profile_path", "")),
    }


def afterburner_profile_status(section: dict) -> tuple[str, bool]:
    if bool(section.get("is_valid_manual_candidate")):
        return "Ready", True
    if not bool(section.get("is_manual_candidate")):
        return "Not Importable: same as Defaults or Startup", False
    validation = section.get("flatten_validation")
    if isinstance(validation, dict):
        reason = str(validation.get("reason", "")).strip()
        if reason:
            return f"Not Importable: {reason}", False
        description = describe_afterburner_flatten_validation(validation)
        if description:
            return f"Not Importable: {description}", False
    return "Not Importable: invalid Afterburner V/F preset", False


def afterburner_profile_target_text(section: dict) -> str:
    clock, voltage = afterburner_profile_target_pair(section)
    return "" if clock is None or voltage is None else f"{clock} MHz {voltage} mV"


def afterburner_profile_target_pair(section: dict) -> tuple[int | None, int | None]:
    target = section.get("flatten_target")
    if not isinstance(target, dict):
        return None, None
    try:
        clock = round(float(target["lock_clock_mhz"]))
        voltage = round(float(target["lock_voltage_mv"]))
    except (KeyError, TypeError, ValueError):
        return None, None
    return clock, voltage


def afterburner_profile_target_value(section: dict, key: str) -> int | None:
    target = section.get("flatten_target")
    if not isinstance(target, dict):
        return None
    try:
        return round(float(target[key]))
    except (KeyError, TypeError, ValueError):
        return None


def afterburner_section_curve_points(section: dict) -> list[tuple[float, float]]:
    materialization = section.get("materialization")
    raw_points = (
        materialization.get("points")
        if isinstance(materialization, dict)
        else section.get("points")
    )
    if not isinstance(raw_points, list):
        return []
    points = []
    for point in raw_points:
        if not isinstance(point, dict):
            continue
        voltage = point.get("voltage_mv")
        clock = point.get("frequency_mhz", point.get("target_mhz"))
        try:
            points.append((float(str(voltage)), float(str(clock))))
        except (TypeError, ValueError):
            continue
    return points


def entry_curve_points(entry: dict) -> list[tuple[float, float]]:
    raw_points = entry.get("curve_points")
    if not isinstance(raw_points, list):
        return []
    points = []
    for point in raw_points:
        try:
            points.append((float(point[0]), float(point[1])))
        except (IndexError, TypeError, ValueError):
            continue
    return points


def fan_curve_points(raw_points) -> list[list[float]]:
    if not isinstance(raw_points, list):
        return []
    points = []
    for point in raw_points:
        if not isinstance(point, dict):
            continue
        try:
            points.append([float(point["temperature_c"]), float(point["speed_pct"])])
        except (KeyError, TypeError, ValueError):
            continue
    return points


def relative_profile_path(root: str | Path, profile_path: str | Path) -> str:
    root_path = Path(root).expanduser().resolve()
    path = Path(profile_path).expanduser().resolve()
    try:
        return str(path.relative_to(root_path))
    except ValueError:
        return path.name
