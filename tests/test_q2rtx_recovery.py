from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import stability.q2rtx.cli as q2rtx_cli
import stability.q2rtx.config as q2rtx_config
import stability.q2rtx.install as q2rtx_install
from stability.q2rtx.constants import Q2RTX_REQUIRED_DATA_FILES


def _managed_install(root: Path, *, complete: bool) -> Path:
    install_dir = root / "pb-benchmark-v0.1.1"
    (install_dir / "baseq2").mkdir(parents=True)
    (install_dir / "q2rtx").touch()
    if complete:
        for relative in Q2RTX_REQUIRED_DATA_FILES:
            required_file = install_dir / relative
            required_file.parent.mkdir(parents=True, exist_ok=True)
            required_file.write_bytes(b"data")
    return install_dir


def test_find_managed_install_rejects_partial_shareware_data(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    managed_root = tmp_path / "q2rtx"
    _managed_install(managed_root, complete=False)
    monkeypatch.setattr(
        q2rtx_config,
        "default_q2rtx_install_data_dir",
        lambda: managed_root,
    )

    discovery = q2rtx_config._discover_managed_q2rtx_install(
        "Issue #68 repro",
        emit_dependency_progress=lambda *_args, **_kwargs: None,
    )

    assert discovery.source == ""
    assert discovery.incomplete is True
    assert "incomplete managed Q2RTX install" in capsys.readouterr().out


def test_find_managed_install_accepts_complete_shareware_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    managed_root = tmp_path / "q2rtx"
    complete_install = _managed_install(managed_root, complete=True)
    monkeypatch.setattr(
        q2rtx_config,
        "default_q2rtx_install_data_dir",
        lambda: managed_root,
    )

    discovery = q2rtx_config._discover_managed_q2rtx_install(
        "Complete install",
        emit_dependency_progress=lambda *_args, **_kwargs: None,
    )

    assert discovery.source == str(complete_install)
    assert discovery.incomplete is False


def test_build_stability_config_repairs_partial_managed_install(
    tmp_path: Path,
    monkeypatch,
) -> None:
    managed_root = tmp_path / "q2rtx"
    repaired_install = _managed_install(managed_root, complete=False)
    actions = []
    monkeypatch.setattr(
        q2rtx_config,
        "default_q2rtx_install_data_dir",
        lambda: managed_root,
    )
    monkeypatch.setattr(
        q2rtx_config,
        "clean_managed_q2rtx",
        lambda: actions.append("cleanup") or (managed_root,),
    )
    monkeypatch.setattr(
        q2rtx_config,
        "install_latest_q2rtx",
        lambda **kwargs: actions.append(("install", kwargs))
        or SimpleNamespace(
            version="pb-benchmark-v0.1.1",
            install_dir=repaired_install,
        ),
    )
    monkeypatch.setattr(
        q2rtx_config,
        "resolve_q2rtx_render_resolution",
        lambda **_kwargs: SimpleNamespace(
            width=2560,
            height=1440,
            reason="test",
            auto_selected=False,
            gpu_name="",
        ),
    )

    config = q2rtx_config.build_stability_config(
        SimpleNamespace(stability_seconds=30),
        gpu_index=0,
        config_path=tmp_path / "config.json",
        dependency_text_progress=False,
    )

    assert config.width == 2560
    assert actions == [
        "cleanup",
        (
            "install",
            {
                "show_progress": False,
                "progress_callback": None,
            },
        ),
    ]


def test_clean_managed_q2rtx_removes_install_and_cache_only(tmp_path: Path) -> None:
    data_dir = tmp_path / "data" / "q2rtx"
    cache_dir = tmp_path / "cache" / "q2rtx"
    config_file = tmp_path / "config" / "PenguinBurner" / "profiles.json"
    (data_dir / "version").mkdir(parents=True)
    (cache_dir / "archive.part").parent.mkdir(parents=True)
    (cache_dir / "archive.part").write_bytes(b"partial")
    config_file.parent.mkdir(parents=True)
    config_file.write_text("keep", encoding="utf-8")

    removed = q2rtx_install.clean_managed_q2rtx(
        data_dir=data_dir,
        cache_dir=cache_dir,
    )

    assert removed == (data_dir.resolve(), cache_dir.resolve())
    assert not data_dir.exists()
    assert not cache_dir.exists()
    assert config_file.read_text(encoding="utf-8") == "keep"


def test_q2rtx_cli_clean_flag_reports_removed_directories(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    data_dir = tmp_path / "data" / "q2rtx"
    cache_dir = tmp_path / "cache" / "q2rtx"
    calls = []
    monkeypatch.setattr(
        q2rtx_cli,
        "clean_managed_q2rtx",
        lambda: calls.append(True) or (data_dir, cache_dir),
    )

    exit_code = q2rtx_cli.main(["--clean-q2rtx"])

    assert exit_code == 0
    assert calls == [True]
    output = capsys.readouterr().out
    assert str(data_dir) in output
    assert str(cache_dir) in output
