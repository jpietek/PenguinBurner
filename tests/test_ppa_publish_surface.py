from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path


def test_ppa_shell_scripts_parse() -> None:
    subprocess.run(
        ["bash", "-n", "scripts/build-deb-source.sh", "scripts/publish-ppa.sh"],
        check=True,
    )


def test_source_builder_exports_tag_and_vendors_cargo_offline() -> None:
    script = Path("scripts/build-deb-source.sh").read_text(encoding="utf-8")

    assert 'source_ref="${PPA_SOURCE_REF:-v${version}}"' in script
    assert 'git archive --format=tar "$source_ref"' in script
    assert "cargo vendor" in script
    assert "--locked" in script
    assert "--versioned-dirs" in script
    assert "cargo metadata" in script
    assert "--offline" in script
    assert 'cd "$source_dir"' in script
    assert 'orig_tarball_path="${PPA_ORIG_TARBALL:-}"' in script


def _write_success_command(path: Path) -> None:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _source_builder_env(tmp_path: Path) -> dict[str, str]:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    for command in ("cargo", "dpkg-buildpackage", "gpg"):
        _write_success_command(shim_dir / command)
    return {
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "PATH": f"{shim_dir}:{os.environ['PATH']}",
    }


def _write_orig_fixture(path: Path, tmp_path: Path) -> bytes:
    source_root = tmp_path / "fixture" / "penguin-burner-0.7.7"
    required_files = (
        "burnerd/Cargo.lock",
        "burnerd/src/main.rs",
        "integrations/steam/manager.py",
        "overlay/native/latency_layer/CMakeLists.txt",
        "overlay/native/nvapi_shim/nvapi64.def",
        "overlay/native/nvapi_shim/src/nvapi_shim.cpp",
    )
    for relative in required_files:
        target = source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture\n", encoding="utf-8")
    (source_root / "vendor").mkdir()
    cargo_config = source_root / ".cargo" / "config.toml"
    cargo_config.parent.mkdir()
    cargo_config.write_text(
        '[source.crates-io]\nreplace-with = "vendored-sources"\n'
        '[source.vendored-sources]\ndirectory = "vendor"\n',
        encoding="utf-8",
    )
    path.parent.mkdir(parents=True)
    with tarfile.open(path, "w:gz") as archive:
        archive.add(source_root, arcname=source_root.name)
    return path.read_bytes()


def test_source_builder_requires_accepted_orig_for_retry(tmp_path: Path) -> None:
    result = subprocess.run(
        ["scripts/build-deb-source.sh", "resolute", "0.7.7", "3"],
        env=_source_builder_env(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "PPA_ORIG_TARBALL is required for Debian revision 3" in result.stderr


def test_source_builder_reuses_orig_before_cleaning_output(tmp_path: Path) -> None:
    outdir = tmp_path / "out"
    orig = outdir / "penguin-burner_0.7.7.orig.tar.gz"
    expected = _write_orig_fixture(orig, tmp_path)
    env = _source_builder_env(tmp_path)
    env.update({"OUTDIR": str(outdir), "PPA_ORIG_TARBALL": str(orig)})

    subprocess.run(
        ["scripts/build-deb-source.sh", "resolute", "0.7.7", "3"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert orig.read_bytes() == expected


def test_debian_recipe_builds_native_payload_fully_offline() -> None:
    rules = Path("packaging/debian/rules").read_text(encoding="utf-8")

    assert "export CARGO_NET_OFFLINE = true" in rules
    assert "override_dh_clean:" in rules
    assert "dh_clean -Xvendor/" in rules
    assert "PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1" in rules
    assert "PENGUIN_BURNER_REQUIRE_NVAPI_SHIM=1" in rules
    assert "PENGUIN_BURNER_BUILD_DAEMON=0" in rules
    assert "cargo build --release --locked --offline" in rules


def test_debian_clean_preserves_vendored_orig(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    debian_dir = source_dir / "debian"
    debian_dir.mkdir(parents=True)
    shutil.copy2("packaging/debian/rules", debian_dir / "rules")
    vendored_orig = source_dir / "vendor" / "crate" / "Cargo.toml.orig"
    vendored_orig.parent.mkdir(parents=True)
    vendored_orig.write_text("checksum-required\n", encoding="utf-8")

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    dh = shim_dir / "dh"
    dh.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "make -f debian/rules override_dh_auto_clean\n"
        "make -f debian/rules override_dh_clean\n",
        encoding="utf-8",
    )
    dh.chmod(0o755)
    dh_clean = shim_dir / "dh_clean"
    dh_clean.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "case \" $* \" in\n"
        "  *\" -Xvendor/ \"*) find . -name '*.orig' ! -path './vendor/*' -delete ;;\n"
        "  *) find . -name '*.orig' -delete ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    dh_clean.chmod(0o755)

    subprocess.run(
        [str(debian_dir / "rules"), "clean"],
        cwd=source_dir,
        env={
            "HOME": os.environ.get("HOME", str(tmp_path)),
            "PATH": f"{shim_dir}:{os.environ['PATH']}",
        },
        check=True,
    )

    assert vendored_orig.read_text(encoding="utf-8") == "checksum-required\n"


def test_one_click_publisher_guards_git_and_waits() -> None:
    script = Path("scripts/publish-ppa.sh").read_text(encoding="utf-8")

    assert "PPA publication requires a clean checkout" in script
    assert "git check-ignore -q dist/deb" in script
    assert "dput_profile_backup" in script
    assert '"login": "anonymous"' in script
    assert '"method": "ftp"' in script
    assert '"passive_ftp": true' in script
    assert '"$status_helper" exists' in script
    assert '"$status_helper" wait' in script
    assert 'changes_files+=("$changes")' in script
    assert 'dput "$ppa" "$changes"' in script
    for forbidden in ("git add", "git commit", "git push"):
        assert forbidden not in script


def test_ppa_docs_promise_generated_artifacts_stay_out_of_git() -> None:
    readme = Path("packaging/debian/README.md").read_text(encoding="utf-8")

    # The documented command must always name the current release, or the
    # copy-pasted example fails check-release-version.sh at publish time.
    version = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    assert f"scripts/publish-ppa.sh {version}" in readme
    assert "never committed to Git" in readme
    assert "REMAINING STEP" not in readme


def test_ppa_surfaces_target_supported_ubuntu_series() -> None:
    readme = Path("packaging/debian/README.md").read_text(encoding="utf-8")
    source_builder = Path("scripts/build-deb-source.sh").read_text(encoding="utf-8")
    publisher = Path("scripts/publish-ppa.sh").read_text(encoding="utf-8")

    assert "Ubuntu 26.04 `resolute`" in readme
    assert "Ubuntu 25.10 `questing`" not in readme
    assert "questing" not in source_builder
    assert "series_list=(resolute)" in publisher
    assert "questing" not in publisher
