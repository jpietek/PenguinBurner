"""Build generic Q2RTX workload configurations and dependency setup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .constants import DEFAULT_DEMO_NAME, Q2RTX_REQUIRED_DATA_FILES
from .install import (
    clean_managed_q2rtx,
    default_q2rtx_install_data_dir,
    fetch_latest_q2rtx_release_metadata,
    install_latest_q2rtx,
)
from .long_stability_config import long_stability_workload_durations
from .models import Q2RTXStabilityConfig, StabilityTestError
from .resolution import (
    Q2RTX_SCAN_RESOLUTIONS,
    format_q2rtx_resolution_choice,
    resolve_q2rtx_render_resolution,
)


@dataclass(frozen=True, slots=True)
class _ManagedQ2RTXDiscovery:
    source: str
    incomplete: bool


def build_stability_config(
    args,
    *,
    gpu_index,
    config_path,
    duration_override=None,
    auto_install_q2rtx=True,
    progress_context="Q2RTX stability",
    dependency_progress_callback=None,
    dependency_text_progress=True,
):
    def emit_dependency_progress(percent, detail, **payload) -> None:
        if dependency_progress_callback is None:
            return
        data = {
            "label": "Downloading dependencies",
            "percent": round(max(0.0, min(100.0, float(percent))), 1),
            "detail": str(detail),
        }
        data.update(payload)
        dependency_progress_callback(data)

    config_dir = Path(config_path).expanduser().parent
    default_log_dir = config_dir / "stability-logs"
    emit_dependency_progress(0.0, "Checking Q2RTX dependency setup")

    discovery = _discover_managed_q2rtx_install(
        progress_context,
        emit_dependency_progress=emit_dependency_progress,
    )
    q2rtx_dir = discovery.source

    if auto_install_q2rtx and discovery.incomplete:
        print(
            f"{progress_context}: removing incomplete managed Q2RTX dependencies",
            flush=True,
        )
        emit_dependency_progress(
            3.0,
            "Removing incomplete Q2RTX dependencies",
        )
        removed = clean_managed_q2rtx()
        emit_dependency_progress(
            4.0,
            "Incomplete Q2RTX dependencies removed",
            removed_paths=[str(path) for path in removed],
        )
        q2rtx_dir = ""

    if auto_install_q2rtx:
        q2rtx_dir = _refresh_stale_managed_q2rtx_source(
            progress_context,
            q2rtx_dir=q2rtx_dir,
            dependency_text_progress=dependency_text_progress,
            dependency_progress_callback=dependency_progress_callback,
            emit_dependency_progress=emit_dependency_progress,
        )

    if not q2rtx_dir:
        if not auto_install_q2rtx:
            print(
                f"{progress_context}: no managed Q2RTX install found and auto-install is disabled",
                flush=True,
            )
        else:
            print(
                f"{progress_context}: no managed Q2RTX install found; installing now",
                flush=True,
            )
            emit_dependency_progress(
                4.0,
                "No managed Q2RTX install found; downloading dependencies",
            )
            install_result = install_latest_q2rtx(
                show_progress=bool(dependency_text_progress),
                progress_callback=dependency_progress_callback,
            )
            q2rtx_dir = str(install_result.install_dir)
            print(
                f"{progress_context}: using installed Q2RTX {install_result.version} at {q2rtx_dir}",
                flush=True,
            )

    if q2rtx_dir:
        print(f"{progress_context}: Q2RTX source {q2rtx_dir}", flush=True)
        emit_dependency_progress(
            100.0,
            "Dependencies are ready",
            source=str(q2rtx_dir),
        )
    resolution_preset = str(getattr(args, "auto_uv_q2rtx_resolution", None) or "auto")
    try:
        width, height = Q2RTX_SCAN_RESOLUTIONS[resolution_preset]
    except KeyError as exc:
        raise ValueError(f"Unknown Q2RTX scan resolution: {resolution_preset}") from exc
    resolution = resolve_q2rtx_render_resolution(
        gpu_index=int(gpu_index),
        requested_width=width,
        requested_height=height,
    )
    print(
        f"{progress_context}: Q2RTX render resolution "
        f"{format_q2rtx_resolution_choice(resolution)}",
        flush=True,
    )
    return Q2RTXStabilityConfig(
        duration_s=(
            int(duration_override)
            if duration_override is not None
            else int(args.stability_seconds)
        ),
        width=int(resolution.width),
        height=int(resolution.height),
        demo_name=str(DEFAULT_DEMO_NAME).strip(),
        gpu_index=int(gpu_index),
        log_dir=default_log_dir,
    )


def _discover_managed_q2rtx_install(
    progress_context,
    *,
    emit_dependency_progress,
) -> _ManagedQ2RTXDiscovery:
    managed_root = default_q2rtx_install_data_dir()
    print(
        f"{progress_context}: checking managed Q2RTX install under {managed_root}",
        flush=True,
    )
    emit_dependency_progress(
        2.0,
        "Checking managed Q2RTX install",
        path=str(managed_root),
    )
    if not managed_root.exists():
        return _ManagedQ2RTXDiscovery(source="", incomplete=False)

    version_dirs = sorted(
        (
            path
            for path in managed_root.iterdir()
            if path.is_dir() and path.name != "compat"
        ),
        reverse=True,
    )
    source = ""
    incomplete = False
    for candidate in version_dirs:
        candidate_discovery = _inspect_managed_q2rtx_candidate(
            progress_context,
            candidate,
            emit_dependency_progress=emit_dependency_progress,
        )
        incomplete = incomplete or candidate_discovery.incomplete
        if candidate_discovery.source:
            source = candidate_discovery.source
            break

    if not source:
        root_discovery = _inspect_managed_q2rtx_candidate(
            progress_context,
            managed_root,
            emit_dependency_progress=emit_dependency_progress,
        )
        source = root_discovery.source
        incomplete = incomplete or root_discovery.incomplete

    return _ManagedQ2RTXDiscovery(source=source, incomplete=incomplete)


def _inspect_managed_q2rtx_candidate(
    progress_context,
    candidate: Path,
    *,
    emit_dependency_progress,
) -> _ManagedQ2RTXDiscovery:
    missing_files = _missing_managed_q2rtx_files(candidate)
    if not missing_files:
        print(
            f"{progress_context}: found managed Q2RTX install {candidate}",
            flush=True,
        )
        return _ManagedQ2RTXDiscovery(source=str(candidate), incomplete=False)
    if (candidate / "q2rtx").is_file():
        _report_incomplete_managed_q2rtx_install(
            progress_context,
            candidate,
            missing_files,
            emit_dependency_progress=emit_dependency_progress,
        )
        return _ManagedQ2RTXDiscovery(source="", incomplete=True)
    return _ManagedQ2RTXDiscovery(source="", incomplete=False)


def _missing_managed_q2rtx_files(candidate: Path) -> tuple[str, ...]:
    required_files = ("q2rtx", *Q2RTX_REQUIRED_DATA_FILES)
    return tuple(
        relative for relative in required_files if not (candidate / relative).is_file()
    )


def _report_incomplete_managed_q2rtx_install(
    progress_context,
    candidate: Path,
    missing_files: tuple[str, ...],
    *,
    emit_dependency_progress,
) -> None:
    missing_text = ", ".join(missing_files)
    print(
        f"{progress_context}: incomplete managed Q2RTX install {candidate}; "
        f"missing {missing_text}",
        flush=True,
    )
    emit_dependency_progress(
        3.0,
        "Incomplete Q2RTX install found; repairing dependencies",
        path=str(candidate),
        missing_files=list(missing_files),
    )


def _managed_q2rtx_source_version(source: str) -> str | None:
    source_path = Path(source).expanduser()
    managed_root = default_q2rtx_install_data_dir().expanduser()
    try:
        relative = source_path.resolve().relative_to(managed_root.resolve())
    except (OSError, ValueError):
        return None
    if not relative.parts:
        return ""
    version = str(relative.parts[0]).strip()
    if not version or version == "compat":
        return None
    return version


def _refresh_stale_managed_q2rtx_source(
    progress_context,
    *,
    q2rtx_dir,
    dependency_text_progress,
    dependency_progress_callback,
    emit_dependency_progress,
):
    configured_source = str(q2rtx_dir or "").strip()
    if not configured_source:
        return ""

    current_version = _managed_q2rtx_source_version(configured_source)
    if current_version is None:
        return q2rtx_dir

    try:
        latest_version, _asset_name, _asset_url = fetch_latest_q2rtx_release_metadata()
    except StabilityTestError as exc:
        print(
            f"{progress_context}: warning: could not check latest managed Q2RTX release: {exc}",
            flush=True,
        )
        return q2rtx_dir

    if current_version == latest_version:
        return q2rtx_dir

    print(
        f"{progress_context}: managed Q2RTX {current_version} is older than "
        f"{latest_version}; installing latest release",
        flush=True,
    )
    emit_dependency_progress(
        4.0,
        f"Updating managed Q2RTX to {latest_version}",
        current_version=current_version,
        latest_version=latest_version,
    )
    install_result = install_latest_q2rtx(
        show_progress=bool(dependency_text_progress),
        progress_callback=dependency_progress_callback,
    )
    print(
        f"{progress_context}: using installed Q2RTX {install_result.version} "
        f"at {install_result.install_dir}",
        flush=True,
    )
    return str(install_result.install_dir)


def stability_workload_label() -> str:
    return "Q2RTX benchmark + CUDA compute"


def stability_workload_split_label(total_duration_s: int) -> str:
    q2rtx_duration_s, cuda_duration_s = long_stability_workload_durations(
        int(total_duration_s),
    )
    return f"q2rtx={int(q2rtx_duration_s)}s cuda={int(cuda_duration_s)}s"
