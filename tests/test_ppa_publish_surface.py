from __future__ import annotations

import subprocess
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


def test_debian_recipe_builds_native_payload_fully_offline() -> None:
    rules = Path("packaging/debian/rules").read_text(encoding="utf-8")

    assert "export CARGO_NET_OFFLINE = true" in rules
    assert "PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1" in rules
    assert "PENGUIN_BURNER_REQUIRE_NVAPI_SHIM=1" in rules
    assert "PENGUIN_BURNER_BUILD_DAEMON=0" in rules
    assert "cargo build --release --locked --offline" in rules


def test_one_click_publisher_guards_git_and_waits_for_both_series() -> None:
    script = Path("scripts/publish-ppa.sh").read_text(encoding="utf-8")

    assert "series_list=(questing resolute)" in script
    assert "PPA publication requires a clean checkout" in script
    assert "git check-ignore -q dist/deb" in script
    assert "dput_profile_backup" in script
    assert '"$status_helper" check' in script
    assert '"$status_helper" wait' in script
    assert 'changes_files+=("$changes")' in script
    assert 'dput "$ppa" "$changes"' in script
    for forbidden in ("git add", "git commit", "git push"):
        assert forbidden not in script


def test_ppa_docs_promise_generated_artifacts_stay_out_of_git() -> None:
    readme = Path("packaging/debian/README.md").read_text(encoding="utf-8")

    assert "scripts/publish-ppa.sh 0.7.2" in readme
    assert "never committed to Git" in readme
    assert "REMAINING STEP" not in readme
