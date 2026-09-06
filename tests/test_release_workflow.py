"""Run release orchestration against local Git and fake publication services."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def _command(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\nset -eu\n" + body + "\n")
    path.chmod(0o755)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def release_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copy2("scripts/release.sh", scripts / "release.sh")
    _command(scripts / "check-release-version.sh", "exit 0")
    _command(scripts / "release-gpg.sh", '[[ "${LOCKED_KEY:-0}" == 0 ]]')
    _command(
        scripts / "build-python-dist.sh",
        "mkdir -p dist/python; echo wheel > dist/python/package.whl; echo sdist > dist/python/package.tar.gz",
    )
    _command(
        scripts / "build-srpm.sh",
        "mkdir -p dist/rpm; echo rpm > dist/rpm/package.src.rpm",
    )
    for channel in ("copr", "aur", "ppa", "flatpak-pages"):
        _command(
            scripts / f"publish-{channel}.sh",
            f'echo {channel} >> "$RELEASE_TRACE"\n'
            + (
                '[[ "${FAIL_COPR:-0}" == 0 ]]'
                if channel == "copr"
                else "mkdir -p dist/flatpak-pages; echo archive > dist/flatpak-pages/PenguinBurner-pages-v0.8.1.tar.gz; "
                "echo checksum > dist/flatpak-pages/PenguinBurner-pages-v0.8.1.tar.gz.sha256"
                if channel == "flatpak-pages"
                else "exit 0"
            ),
        )
    (repo / ".gitignore").write_text("dist/\n")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "release@example.invalid")
    _git(repo, "config", "user.name", "Release test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "Prepared release")
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    aur = tmp_path / "aur"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(aur)],
        check=True,
        capture_output=True,
    )
    (repo / "private-untracked.txt").write_text("must stay local")
    fake = tmp_path / "bin"
    _command(
        fake / "gh",
        """
[[ "$GH_PROMPT_DISABLED" == 1 ]]
case "$1 $2" in
  "auth status") exit 0 ;;
  "release view")
    [[ -f "$RELEASE_STATE/github" ]] || exit 1
    if [[ "$*" == *--json* && -d "$RELEASE_STATE/assets" ]]; then
      find "$RELEASE_STATE/assets" -type f -printf '%f\n'
    fi ;;
  "release create") touch "$RELEASE_STATE/github" ;;
  "release upload")
    [[ "${FAIL_CHECKSUM:-0}" == 0 || "$4" != *.sha256 ]] || exit 1
    mkdir -p "$RELEASE_STATE/assets"
    cp "$4" "$RELEASE_STATE/assets/" ;;
  "release download") cp "$RELEASE_STATE/assets/$5" "$7/" ;;
  "workflow run") echo https://github.com/example/project/actions/runs/123 ;;
  "run watch") [[ "${FAIL_PAGES:-0}" == 0 ]] ;;
  *) echo "unexpected gh command: $*" >&2; exit 2 ;;
esac
""",
    )
    _command(
        fake / "python3",
        """
if [[ "$*" == "-m venv "* ]]; then
    mkdir -p "$3/bin"
    exit 0
fi
if [[ "$1" == scripts/release_pypi.py ]]; then
    [[ "${2:-}" == --check-credentials ]] && exit 0
    [[ -f "$RELEASE_STATE/pypi" ]] && exit 0
    exit 3
fi
[[ "$*" == "-m twine upload --non-interactive "* ]]
echo pypi >> "$RELEASE_TRACE"
touch "$RELEASE_STATE/pypi"
""",
    )
    for name in (
        "rpmbuild",
        "copr-cli",
        "makepkg",
        "cargo",
        "dpkg-buildpackage",
        "dput",
        "gpg",
        "gpg-connect-agent",
        "flatpak",
        "flatpak-builder",
    ):
        _command(fake / name, "exit 0")
    env = {
        **os.environ,
        "PATH": f"{fake}:{os.environ['PATH']}",
        "PENGUIN_BURNER_AUR_REPO": str(aur),
        "RELEASE_STATE": str(tmp_path),
        "RELEASE_TRACE": str(tmp_path / "trace"),
    }
    return repo, env


def _release(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["scripts/release.sh", "0.8.1"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )


def test_release_publishes_all_channels_from_clean_clone(release_repo) -> None:
    repo, env = release_repo
    result = _release(repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert set(Path(env["RELEASE_TRACE"]).read_text().splitlines()) == {
        "pypi",
        "copr",
        "aur",
        "ppa",
        "flatpak-pages",
    }
    commit = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-parse", "v0.8.1^{commit}") == commit
    work = repo / "dist/release/0.8.1" / commit / "source"
    assert not (work / "private-untracked.txt").exists()
    assert (repo / "private-untracked.txt").read_text() == "must stay local"


def test_failed_channel_does_not_stop_others_and_resume_skips_successes(
    release_repo,
) -> None:
    repo, env = release_repo
    failed = _release(repo, {**env, "FAIL_COPR": "1"})
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert set(Path(env["RELEASE_TRACE"]).read_text().splitlines()) == {
        "pypi",
        "copr",
        "aur",
        "ppa",
        "flatpak-pages",
    }
    result = _release(repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = Path(env["RELEASE_TRACE"]).read_text().splitlines()
    assert calls.count("copr") == 2
    assert all(
        calls.count(channel) == 1 for channel in ("pypi", "aur", "ppa", "flatpak-pages")
    )


def test_locked_signing_key_stops_before_build_or_publication(release_repo) -> None:
    repo, env = release_repo
    result = _release(repo, {**env, "LOCKED_KEY": "1"})
    assert result.returncode != 0
    assert not Path(env["RELEASE_TRACE"]).exists()
    assert not (repo / "dist").exists()


def test_gpg_signing_never_requests_pinentry(tmp_path: Path) -> None:
    args = tmp_path / "args"
    _command(tmp_path / "gpg", 'printf "%s\\n" "$@" > "$SIGN_ARGS"\nexit 2')
    result = subprocess.run(
        ["scripts/release-gpg.sh", "--check", "test-key"],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "SIGN_ARGS": str(args),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    recorded = args.read_text().splitlines()
    assert "--batch" in recorded
    assert recorded[recorded.index("--pinentry-mode") + 1] == "error"
    assert "unavailable for unattended use" in result.stderr


def test_partial_flatpak_upload_resumes_without_rebuilding(release_repo) -> None:
    repo, env = release_repo
    first = _release(repo, {**env, "FAIL_CHECKSUM": "1"})
    assert first.returncode == 1, first.stdout + first.stderr
    assets = Path(env["RELEASE_STATE"]) / "assets"
    assert (assets / "PenguinBurner-pages-v0.8.1.tar.gz").exists()
    assert not (assets / "PenguinBurner-pages-v0.8.1.tar.gz.sha256").exists()
    result = _release(repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        Path(env["RELEASE_TRACE"]).read_text().splitlines().count("flatpak-pages") == 1
    )
    assert (assets / "PenguinBurner-pages-v0.8.1.tar.gz.sha256").exists()


def test_failed_pages_deployment_is_not_recorded_as_complete(release_repo) -> None:
    repo, env = release_repo
    result = _release(repo, {**env, "FAIL_PAGES": "1"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert not list((repo / "dist/release").rglob("flatpak.done"))
    result = _release(repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        Path(env["RELEASE_TRACE"]).read_text().splitlines().count("flatpak-pages") == 1
    )


def test_existing_tag_cannot_be_moved_to_another_commit(release_repo) -> None:
    repo, env = release_repo
    _git(repo, "tag", "v0.8.1")
    before = _git(repo, "rev-parse", "v0.8.1")
    _git(repo, "commit", "--allow-empty", "-m", "Later commit")
    _git(repo, "push")
    result = _release(repo, env)
    assert result.returncode != 0
    assert "different commit" in result.stderr
    assert _git(repo, "rev-parse", "v0.8.1") == before
    assert not (Path(env["RELEASE_STATE"]) / "github").exists()


def test_pypi_partial_upload_requires_matching_hashes() -> None:
    spec = importlib.util.spec_from_file_location(
        "release_pypi", "scripts/release_pypi.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expected = {"wheel": "abc", "sdist": "def"}
    assert not module.publication_complete(expected, {})
    assert not module.publication_complete(expected, {"wheel": "abc"})
    assert module.publication_complete(expected, expected)
    with pytest.raises(ValueError, match="differ"):
        module.publication_complete({"wheel": "abc"}, {"wheel": "wrong"})
    with pytest.raises(ValueError, match="differ"):
        module.publication_complete({"wheel": "abc"}, {"unexpected": "abc"})


@pytest.mark.parametrize(("protection", "success"), [("P", False), ("C", True)])
def test_unattended_signing_refuses_protected_keys_even_when_cached(
    tmp_path, protection, success
):
    _command(
        tmp_path / "gpg",
        """
if [[ "$*" == *--with-colons* ]]; then
    printf 'sec:::::::::::s:\ngrp:::::::::ABC:\n'
else
    touch "$SIGN_DONE"
fi
""",
    )
    _command(
        tmp_path / "gpg-connect-agent", 'echo "S KEYINFO ABC D - - 1 $PROTECTION - - -"'
    )
    done = tmp_path / "signed"
    result = subprocess.run(
        ["scripts/release-gpg.sh", "--check-unattended", "test-key"],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PROTECTION": protection,
            "SIGN_DONE": str(done),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is success, result.stderr
    assert done.exists() is success


@pytest.mark.parametrize(
    "changed",
    [
        None,
        "pyproject.toml",
        "burnerd/Cargo.toml",
        "burnerd/Cargo.lock",
        "packaging/arch/PKGBUILD",
        "packaging/rpm/penguin-burner.spec",
        "packaging/flatpak/io.github.jpietek.PenguinBurner.metainfo.xml",
    ],
)
def test_release_version_checks_every_distribution(tmp_path, changed):
    import tomllib

    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    paths = [
        "pyproject.toml",
        "burnerd/Cargo.toml",
        "burnerd/Cargo.lock",
        "packaging/arch/PKGBUILD",
        "packaging/rpm/penguin-burner.spec",
        "packaging/flatpak/io.github.jpietek.PenguinBurner.metainfo.xml",
        f"docs/release-notes-{version}.md",
    ]
    for name in paths:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        data = Path(name).read_text()
        target.write_text(data.replace(version, "99.0.0") if changed == name else data)
    result = subprocess.run(
        [str(Path("scripts/check-release-version.sh").resolve()), version],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is (changed is None), result.stderr
    if changed:
        assert changed in result.stderr


@pytest.mark.parametrize("credentials", ["missing", "environment", "config"])
def test_pypi_credentials_are_required_without_prompting(tmp_path, credentials):
    config = tmp_path / "pypirc"
    config.write_text(
        "[pypi]\npassword = test-only\n" if credentials == "config" else ""
    )
    env = {**os.environ, "TWINE_CONFIG_FILE": str(config)}
    env.pop("TWINE_PASSWORD", None)
    if credentials == "environment":
        env["TWINE_PASSWORD"] = "test-only"
    result = subprocess.run(
        ["python3", "scripts/release_pypi.py", "--check-credentials"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert (result.returncode == 0) is (credentials != "missing"), result.stderr
    assert "test-only" not in result.stdout + result.stderr


def test_aur_retry_pushes_previously_committed_package(tmp_path):
    remote = tmp_path / "aur.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    aur = tmp_path / "aur"
    subprocess.run(
        ["git", "clone", str(remote), str(aur)], check=True, capture_output=True
    )
    _git(aur, "config", "user.email", "release@example.invalid")
    _git(aur, "config", "user.name", "Release test")
    _git(aur, "commit", "--allow-empty", "-m", "Initial")
    _git(aur, "push", "-u", "origin", "HEAD")
    initial = _git(aur, "rev-parse", "HEAD")
    hook = remote / "hooks/pre-receive"
    _command(hook, "exit 1")
    _command(tmp_path / "bin/makepkg", "printf '\tpkgver = 0.8.0\n\tpkgrel = 1\n'")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "PENGUIN_BURNER_SKIP_PACKAGE_SMOKE": "1",
    }
    command = ["scripts/publish-aur.sh", str(aur)]
    failed = subprocess.run(command, env=env, capture_output=True, check=False)
    assert failed.returncode != 0
    committed = _git(aur, "rev-parse", "HEAD")
    assert committed != initial
    hook.unlink()
    result = subprocess.run(command, env=env, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert _git(aur, "rev-parse", "HEAD") == committed
    assert committed in _git(aur, "ls-remote", "origin").split()
