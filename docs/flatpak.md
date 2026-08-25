# PenguinBurner Flatpak

PenguinBurner publishes a self-hosted Flatpak repository at
`https://jpietek.github.io/PenguinBurner/penguin-burner.flatpakrepo`.

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
