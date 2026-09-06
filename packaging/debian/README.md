# Ubuntu PPA Packaging

This directory contains the Debian packaging template used by
`scripts/build-deb-source.sh`.

Supported PPA targets:

- Ubuntu 26.04 `resolute`

The package is amd64-only and requires NVIDIA driver/userspace packages 580 or
newer.

## Rust root daemon (`penguin-burnerd`)

Since 0.6.x the privileged root daemon is a compiled Rust binary built from the
bundled `burnerd/` crate. `debian/rules` (`override_dh_auto_build`) runs:

```
cargo build --release --locked --manifest-path burnerd/Cargo.toml
```

and installs the result to `/usr/libexec/penguin-burnerd` (0755, root-owned).
This package-owned file is an install source: explicit hardware-service setup
copies it to `/var/opt/penguin-burner/libexec/penguin-burnerd`, which is the
only path generated systemd units execute. `cargo` is a `Build-Depends`.
`CARGO_HOME` is redirected into `debian/cargo` so the build stays inside the
tree (matching `Rules-Requires-Root: no`).

### Offline Launchpad builds

Launchpad PPA builders have no network access. `scripts/build-deb-source.sh`
therefore exports the matching release tag into a temporary tree, runs
`cargo vendor --locked`, writes Cargo's project-local source replacement, and
includes the generated crates in the `.orig.tar.gz`. It proves the lockfile can
resolve with `cargo metadata --locked --offline` before signing the source.

The Launchpad build runs:

```text
cargo build --release --locked --offline --manifest-path burnerd/Cargo.toml
```

The vendor directory, Cargo configuration, source archives, and binary build
outputs are generated only below ignored `dist/` or temporary directories. They
are never committed to Git.

## Publish

From a clean checkout containing the release tag and Debian signing key:

```bash
scripts/publish-ppa.sh 0.8.0
```

That one command builds and validates the Resolute source package, uploads it
through Launchpad's anonymous passive-FTP endpoint, waits for Launchpad's amd64
build, prints its URL, and fails if the build fails. Pass a series name after
the version to publish only that target.

When retrying the same upstream version with a new Debian revision, reuse the
orig tarball already accepted by Launchpad so its immutable checksum does not
change:

```bash
PPA_ORIG_TARBALL=/path/to/penguin-burner_0.7.7.orig.tar.gz \
  DEBIAN_REVISION=2 scripts/publish-ppa.sh 0.7.7 resolute
```
