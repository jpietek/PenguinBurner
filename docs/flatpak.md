# PenguinBurner Flatpak

PenguinBurner publishes a self-hosted Flatpak repository at
`https://jpietek.github.io/PenguinBurner/penguin-burner.flatpakrepo`.

## Heroic installed as Flatpak

The Heroic integration supports `com.heroicgameslauncher.hgl`. Enabling **Wrap
this game** prepares a PenguinBurner launch runtime in Heroic's own data directory
and checks it inside a fresh sandbox. The wrapper uses Heroic's Python runtime;
the game continues to launch through Heroic and its selected Wine/Proton runner.

This explicit action grants Heroic access to `~/.config/PenguinBurner`,
`~/.cache/penguin-burner`, and `/run/penguin-burnerd.sock`. The daemon remains
responsible for privileged operations and authenticates the connecting user.
No general host-execution permission is added. Existing Heroic overrides and
other game wrappers are preserved.

**Fully exit and reopen Heroic after enabling integration for the first time.**
An already-running Heroic process retains its previous sandbox permissions.
Then relaunch the game: the switches show saved launch settings, not proof that
an already-running game loaded the overlay. Reapply the setting after a PB
upgrade to refresh Heroic's runtime payload. An integration-check error must be
resolved before PB saves an enabled wrapper; an old daemon needs updating too.

Disabling a game's wrapper restores its previous wrapper configuration. Shared
runtime files and grants remain available for other enabled Heroic games.

## Install

```bash
flatpak remote-add --user --if-not-exists penguinburner https://jpietek.github.io/PenguinBurner/penguin-burner.flatpakrepo
flatpak install --user -y penguinburner io.github.jpietek.PenguinBurner
flatpak run io.github.jpietek.PenguinBurner
```

Or as a single pasteable command:

```bash
flatpak remote-add --user --if-not-exists penguinburner https://jpietek.github.io/PenguinBurner/penguin-burner.flatpakrepo && flatpak install --user -y penguinburner io.github.jpietek.PenguinBurner && flatpak run io.github.jpietek.PenguinBurner
```

That single command installs and opens the Flatpak. When a host Steam
installation is detected, PenguinBurner silently repairs and verifies the
complete host integration before its window appears. After that,
`penguin-burner`, `pburn`, `penguin-burner-cli`, `pburn-cli`, and
`PENGUIN_BURNER` work from your `PATH`
just like the native packages.

Hosts without Steam are left completely untouched — no wrappers, Vulkan
manifest, or shim files are written, and all GPU tuning (Auto-UV, profiles,
fan control) works normally without them. To get the `pburn` PATH commands on
a Steam-less host anyway, run the manual repair command below once; after
that, startup keeps the generated files repaired across updates like on any
other host.

## Host requirements

The root hardware daemon and the Vulkan latency layer are built inside the
Flatpak runtime but execute on the host, so the host needs glibc 2.39 or
newer — Ubuntu 24.04+, Debian 13+, Fedora 40+, or any current rolling
release. On older hosts (Debian 12, Ubuntu 22.04, Mint 21) the daemon setup
stops with a clear error before touching the system; install PenguinBurner
from a native package (COPR, PPA, AUR, or pip) there instead.

## Host Wrappers

The first GUI launch on a host with Steam adds these commands under
`~/.local/bin`, forwarding each one into the Flatpak sandbox:

- `penguin-burner`
- `pburn`
- `penguin-burner-cli`
- `pburn-cli`
- `PENGUIN_BURNER`

Startup also registers the native Vulkan overlay layer and verifies the shipped
NVAPI shim; existing native/PyPI commands are left untouched. For manual repair,
run `flatpak run --user --command=penguin-burner-install-wrappers
io.github.jpietek.PenguinBurner`; add `--force` only to replace non-managed
commands deliberately.

If the commands are not found after installation, make sure `~/.local/bin` is on
your `PATH`.

## Update

Existing Flatpak users should update and launch once; startup refreshes the
complete integration automatically:

```bash
flatpak update --user -y io.github.jpietek.PenguinBurner && flatpak run io.github.jpietek.PenguinBurner
```

## Uninstall

To uninstall the Flatpak cleanly, remove the host wrappers before removing the
app. Cleanup removes only files created by PenguinBurner: the `~/.local/bin`
commands, user Vulkan manifest, and NVAPI shim fronts in tracked Wine or Proton
prefixes (the real DLLs are restored). It does not remove config or Auto-UV
profiles under `~/.config/PenguinBurner`, so those stay available to PyPI/native
installs.

```bash
flatpak run --user --command=penguin-burner-install-wrappers io.github.jpietek.PenguinBurner --uninstall
flatpak uninstall --user --delete-data io.github.jpietek.PenguinBurner
flatpak remote-delete --user penguinburner
```

## Launch

Launch the installed app at any time with:

```bash
flatpak run io.github.jpietek.PenguinBurner
```

## Pre-release verification

Automated, per change and weekly in CI (`.github/workflows/flatpak-*.yml`):

- `scripts/check-flatpak-install-smoke.sh --container <fedora|ubuntu-lts|arch>`
  builds and installs the current tree, asserts exports, sandbox entry
  points, the wrapper installer, and that the host-side binaries stay within
  the accepted host glibc floor.
- `scripts/check-flatpak-host-python.sh` proves the pkexec-elevated import
  closure runs under each supported host python (Debian 12's 3.11 floor
  through current rolling releases) and that the rendered install
  transaction parses.
- `scripts/check-flatpak-channel-install.sh` installs the *published*
  Flatpak from the live Pages repository per distro and round-trips the
  wrapper installer, asserting uninstall leaves nothing behind.

Manual, on the release host before dispatching a Pages deployment: build the
snapshot with `--prepare-only`, install that exact build locally, and walk
the GPU checklist — first launch installs a reachable daemon in the same
session, an Auto-UV scan verifies, apply changes real clocks, boot persist
survives a reboot, the overlay renders in one Steam game, and the documented
uninstall leaves the host clean. Restore the host's native install medium
afterwards.

## Publishing

Flatpak repository output is deployed through a GitHub Pages Actions artifact,
not committed to a `gh-pages` branch. Signing stays local: the publisher builds
with the existing GPG key, verifies a clean installation and an update from the
previous snapshot, uploads the signed snapshot to the matching GitHub Release,
and dispatches the Pages workflow.

From a clean checkout at the release tag, run:

```bash
scripts/publish-flatpak-pages.sh vX.Y.Z
```

Publication first runs the containerized host-python compatibility scenarios
(`scripts/check-flatpak-host-python.sh`, requires docker or podman), because
the elevated daemon-install step executes product code on the host's
`python3`. `PENGUIN_BURNER_SKIP_PACKAGE_SMOKE=1` skips that gate, mirroring
the COPR and PPA publishers.

The publisher discovers the newest compatible snapshot Release asset and uses
it to preserve the OSTree repository across releases. It refuses to replace an
existing snapshot unless `--replace` is passed explicitly.

Use `--prepare-only` instead of `--upload-only` to complete all local build,
signature, installation, update, archive, and checksum checks without uploading
an asset or dispatching the Pages workflow.

The smoke test targets the published `x86_64` ref. On a cross-architecture
maintenance host, set `PENGUIN_BURNER_FLATPAK_SMOKE_NO_DEPS=1` to validate the
app ref without downloading a foreign-architecture runtime; release hosts
should leave dependency verification enabled.

After the workflow deploys, verify a fresh install and an update through the
public URL. The private GPG key is never uploaded;
only the public key, signed repository, bundle, archive, and checksum leave the
release machine.
