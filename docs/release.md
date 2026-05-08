# Release

PenguinBurner GitHub/COPR releases are driven by a local shell script. The
script builds Python distributions, builds a Fedora source RPM, creates the
GitHub release, submits the source RPM to COPR, pushes the AUR package, and
uploads Ubuntu source packages to the PPA.

PyPI publishing stays in the existing `Publish Python package` workflow, which
is triggered by the published GitHub release.

## Required Local Tools

- `gh`, authenticated with permission to create releases.
- `copr-cli`, authenticated with `~/.config/copr`.
- `makepkg`, for AUR metadata generation.
- `rpmbuild`.
- `dput`, `debuild`, and GPG, for Launchpad PPA uploads.
- Keep the existing PyPI workflow credentials/configuration unchanged.

## One-Command Release

From a clean checkout on the release commit:

```bash
scripts/release.sh 0.1.6
```

The version must match `pyproject.toml`, and
`docs/release-notes-<version>.md` must exist.

## Fedora Package

The RPM is x86_64-only and has a hard runtime dependency on RPM Fusion NVIDIA
driver packages:

```text
xorg-x11-drv-nvidia-cuda >= 3:580
or
xorg-x11-drv-nvidia-580xx-cuda >= 3:580
```

Users must enable RPM Fusion nonfree and the COPR repo before installing.

Package description:

```text
NVIDIA GPU automatic undervolting tool. It helps you visualize and manage your
GPU fine tuning setup with single voltage/frequency bin precision to maximize
FPS per Watt, potentially leading into +33% and higher improvements for recent
cards. MSI Afterburner imports and LACT exports are also supported.
```

## Arch And CachyOS Package

The AUR package is published at:

```text
https://aur.archlinux.org/packages/penguin-burner
```

It is x86_64-only and has a hard `nvidia-utils>=580` dependency. Arch and
CachyOS users can install it with an AUR helper:

```bash
paru -S penguin-burner
```

or:

```bash
yay -S penguin-burner
```

## Ubuntu PPA

The Ubuntu PPA target is:

```text
https://launchpad.net/~jpietek/+archive/ubuntu/penguin-burner
```

Create/activate that PPA once in Launchpad before the first upload:

```text
https://launchpad.net/~/+activate-ppa
```

Supported source upload series:

- Ubuntu 25.10 `questing`
- Ubuntu 26.04 `resolute`

Users can install with:

```bash
sudo add-apt-repository ppa:jpietek/penguin-burner
sudo apt update
sudo apt install penguin-burner
```
