# Release Process

Run the complete release from a prepared, committed version:

```bash
scripts/release.sh VERSION
```

It builds Python and RPM artifacts, creates the annotated tag and GitHub
release, then publishes **PyPI, AUR, COPR, Ubuntu PPA, and Flatpak/Pages**.
Distribution channels run concurrently; a failure in one does not skip others.
The command waits for remote COPR/PPA builds and the Pages deployment.

## Before running

1. Update Python, Cargo, Arch, RPM, and Flatpak version metadata and write
   `docs/release-notes-VERSION.md`.
2. Run the tests, static checks, and relevant package builds. Merge the prepared
   release into `main`; start from that exact commit with no tracked changes.
3. Authenticate `gh`, PyPI/Twine, COPR, and AUR SSH. Install the tools required
   by the individual package builders, including Cargo, RPM/Debian build tools,
   `makepkg`, Flatpak, and a working container engine.
4. Confirm the Ubuntu and Flatpak signing keys are available locally.

The runner uses a clean clone so untracked local files do not enter artifacts.
Python build tools live in a release-specific virtual environment. Podman is
preferred when available; `CIBW_CONTAINER_ENGINE` overrides that choice.

## Package checks

Arch, Fedora, and Ubuntu package checks build committed `HEAD` in disposable
containers and inspect native payloads. Flatpak compatibility and lifecycle
checks exercise the working tree. Run all checks from the clean release commit.

| Surface | Check |
| --- | --- |
| Arch and CachyOS | `scripts/check-arch-package-build.sh vanilla cachyos-shelly` |
| Supported Fedora | `scripts/check-fedora-package-build.sh fedora-43 fedora-44` |
| Ubuntu PPA | `scripts/check-ubuntu-package-build.sh resolute` |
| Flatpak package/install | `scripts/check-flatpak-install-smoke.sh --container fedora` (also `ubuntu-lts`, `arch`) |
| Flatpak host Python | `scripts/check-flatpak-host-python.sh` (Debian, Ubuntu, Fedora, Arch scenarios) |
| Flatpak daemon lifecycle | `scripts/check-flatpak-daemon-lifecycle.sh` |
| PyPI wheel | `scripts/build-python-dist.sh dist/python` builds in manylinux and checks native payloads and entry points. |

The AUR, COPR, and PPA publishers run their supported package checks by default.
Only set `PENGUIN_BURNER_SKIP_PACKAGE_SMOKE=1` when matching checks have already
passed for the exact source and packaging being released; keep the CI URL or
local log as evidence. Fedora `rawhide` and Ubuntu `devel` check future toolchain
drift separately from stable-release gates.

Container checks accept `PENGUIN_BURNER_CONTAINER_ENGINE`. Package builds
default to Docker; the systemd lifecycle check defaults to Podman. The manylinux
builder separately uses `CIBW_CONTAINER_ENGINE`. Flatpak publication also tests
fresh installation and upgrading the previous signed snapshot before uploading
the new snapshot.

## Unattended signing

The runner checks configured PyPI credentials, GitHub/COPR access, AUR SSH,
and signing before building. PyPI credentials come from `TWINE_PASSWORD` or
the `pypi` entry in the Twine configuration; interactive keyrings are disabled.
The runner disables Git, pip, and Twine prompts. Ubuntu signing disables GPG
pinentry and fails early if its key is locked. Flatpak requires an unprotected
signing key because OSTree controls its signing process. Configure signing
keys locally; the repository contains no private key material.

Check a signing key before starting:

```bash
scripts/release-gpg.sh --check KEY_ID
scripts/release-gpg.sh --check-unattended KEY_ID
```

Override the selected keys with `DEBSIGN_KEYID` and
`PENGUIN_BURNER_FLATPAK_GPG_KEY`. Credentials and private keys stay outside Git.
Missing credentials cause a failure; the runner does not ask for secrets.

## Retry a failed release

Run the same command from the same commit. Artifacts, logs, and `.done` receipts
remain under `dist/release/VERSION/COMMIT/`. Successful stages are skipped.

- Existing GitHub assets must match local files exactly.
- Partial PyPI uploads resume only when existing files have matching hashes.
- AUR retries push a commit that a previous attempt created but could not push.
- PPA retries monitor an already accepted source instead of uploading it again.
- Flatpak retries reuse the prepared snapshot and complete missing uploads.

An interrupted COPR submission may create a new build on retry. Inspect the
channel log when a remote operation's outcome is uncertain. Keep the retained
artifacts: rebuilding an already published version may produce different hashes.

For a failed PPA build that needs source changes, increment the Debian revision
and reuse the accepted orig archive; see [PPA packaging](../packaging/debian/README.md).

## Verify completion

Check the GitHub release/tag, PyPI file hashes, AUR version, COPR/PPA build
results and package repositories, and the public Flatpak bundle/repository.
Existing report pages must remain available after Pages deployment.
Update the local GUI/daemon only when that host upgrade is requested.
