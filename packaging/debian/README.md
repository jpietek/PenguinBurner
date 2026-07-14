# Ubuntu PPA Packaging

This directory contains the Debian packaging template used by
`scripts/build-deb-source.sh`.

Supported PPA targets:

- Ubuntu 25.10 `questing`
- Ubuntu 26.04 `resolute`

The package is amd64-only and requires NVIDIA driver/userspace packages 580 or
newer.

## Rust root daemon (`penguin-burnerd`)

Since 0.6.x the privileged root daemon is a compiled Rust binary built from the
bundled `burnerd/` crate. `debian/rules` (`override_dh_auto_build`) runs:

```
cargo build --release --locked --manifest-path burnerd/Cargo.toml
```

and installs the result to `/usr/libexec/penguin-burnerd` (0755, root-owned) —
the path `runtime/support/runtime_service.py` discovers first. `cargo` is a
`Build-Depends`. `CARGO_HOME` is redirected into `debian/cargo` so the build
stays inside the tree (matching `Rules-Requires-Root: no`).

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
scripts/publish-ppa.sh 0.7.2
```

That one command builds and validates both Questing and Resolute source
packages, uploads them, waits for Launchpad's amd64 builds, prints their URLs,
and fails if either build fails. Pass series names after the version to publish
only selected targets.
