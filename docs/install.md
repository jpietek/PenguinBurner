# Install PenguinBurner

Install the NVIDIA proprietary driver and CUDA first. PenguinBurner supports
RTX 30 (Ampere), RTX 40 (Ada), and RTX 50 (Blackwell) cards.

The driver must provide `libnvidia-ml.so.1` for NVML telemetry/GPU discovery,
`libnvidia-api.so.1` for NVIDIA V/F curve control, the Vulkan runtime for Q2RTX,
and `libcuda.so.1` for the CUDA companion test. PenguinBurner reads GPU identity
and VRAM through NVML directly; the `nvidia-smi` command is useful for manual
debugging but is not the GPU picker backend.

## pip (any distro)

```bash
python -m pip install --user --upgrade penguin-burner
```

Fedora 38+, Ubuntu 23.04+, and Debian 12+ mark the system Python as
externally managed (PEP 668), so the command above fails with
`error: externally-managed-environment`. On those distros install through
pipx instead:

```bash
pipx install penguin-burner   # sudo dnf/apt install pipx first if needed
```

or use a dedicated virtual environment
(`python -m venv ~/.venvs/penguin-burner && ~/.venvs/penguin-burner/bin/pip
install penguin-burner`), or simply prefer the native COPR/AUR/PPA package
for your distro below.

## Flatpak

```bash
flatpak remote-add --user --if-not-exists penguinburner https://jpietek.github.io/PenguinBurner/penguin-burner.flatpakrepo
flatpak install --user -y penguinburner io.github.jpietek.PenguinBurner
flatpak run io.github.jpietek.PenguinBurner
```

When a host Steam installation is detected, the
first GUI launch silently installs and verifies the host wrappers, Vulkan
layer registration, and NVAPI shim payload; hosts without Steam are left
untouched. See the [Flatpak guide](flatpak.md) for repair details, the update
command, and clean uninstall.

The Flatpak includes the privileged root daemon (`penguin-burnerd`, a compiled
Rust binary) built into the sandbox. The first privileged action installs it
onto the host at
`/var/opt/penguin-burner/libexec/penguin-burnerd` together with its systemd
service, with a single admin prompt; after that all privileged GPU operations
go through the running service with no further prompts. This writable
root-owned location works on rpm-ostree systems where `/usr` is read-only.

After that very first daemon setup, quit and relaunch the app once: the
sandbox can only see the daemon socket (`/run/penguin-burnerd.sock`) when it
already exists at app launch. If GPU actions report the daemon socket as
missing right after the first setup, the relaunch is the fix.

## Fedora ([COPR](https://copr.fedorainfracloud.org/coprs/jpietek/penguin-burner/))

Fedora 42 / 43 / 44, with the NVIDIA driver from Fedora's repo or RPM Fusion:

```bash
sudo dnf install -y dnf-plugins-core
sudo dnf copr enable -y jpietek/penguin-burner
sudo dnf install -y penguin-burner
```

## Arch / CachyOS ([AUR](https://aur.archlinux.org/packages/penguin-burner))

```bash
paru -S penguin-burner   # or: yay -S penguin-burner
```

## Ubuntu ([PPA](https://launchpad.net/~jpietek/+archive/ubuntu/penguin-burner))

Ubuntu 25.10 / 26.04:

```bash
sudo add-apt-repository ppa:jpietek/penguin-burner
sudo apt update && sudo apt install penguin-burner
```

## Entry points

- GUI: `penguin-burner` (alias `pburn`)
- CLI: `penguin-burner-cli` (alias `pburn-cli`)

The pip and Flatpak-wrapper installs also add desktop-friendly command entries.
If the commands are not found, make sure `~/.local/bin` is on your `PATH`.

## Local wheel (from a checkout)

```bash
python -m pip install --user --no-index --no-deps --find-links dist --upgrade penguin-burner
```

## Root daemon (from a checkout)

The privileged root daemon (`penguin-burnerd`) is a compiled Rust binary. The
native COPR/AUR/PPA packages build it for you and ship a package-owned source
at `/usr/libexec/penguin-burnerd`. The PyPI/local wheel also bundles a compiled
source copy (built with `cargo` at wheel-build time) inside the package at
`runtime/daemon_bin/penguin-burnerd`; building the wheel from source therefore
needs a Rust toolchain (`cargo`) just as it needs `cmake` for the Vulkan layer
and MinGW for the NVAPI shim. From a checkout you can instead build the daemon
once with:

```bash
scripts/build-daemon.sh   # cargo build --release --locked in burnerd/
```

The systemd unit always executes the root-owned
`/var/opt/penguin-burner/libexec/penguin-burnerd`; it never points into a
user-writable wheel, checkout, or Flatpak deployment. During the one elevated
service setup, the installer atomically copies the current wheel-bundled
`runtime/daemon_bin/penguin-burnerd`, the dev build at
`burnerd/target/release/penguin-burnerd`, or a native package's safe
`/usr/libexec/penguin-burnerd` source into that fixed path. Failed startup or
socket readiness restores the previous binary, unit, and service state.

## Recovery: getting back to stock

Applied tuning persists across reboots only while **Apply on startup** is
ticked for the selected GPU (off by default). Unticking it clears that GPU's
saved boot entry immediately without removing other GPUs' entries. The daemon
replays saved GPUs serially by stable UUID and leaves one GPU actively
monitored; see [Profile Management](features/profile-management.md).

If the GPU is in a bad state (or a boot profile misbehaves), reset it to
stock — now and at boot — without the GUI:

```bash
penguin-burner-cli --restore-stock
```

This asks the running root daemon to clear core/memory offsets, release locked
clocks, restore the factory V/F curve and default power limit, and makes stock
the boot state. Saved profiles are kept and can be re-applied later.
You can then start a GUI or CLI Auto-UV scan normally: scan startup releases
the active stock runtime profile without disabling the service or changing
the stock boot setting.

If a boot profile ever makes the desktop unusable before you can run that
command, boot once into systemd rescue mode (hold the boot menu, select the
rescue/recovery entry, or add `systemd.unit=rescue.target` to the kernel
command line) and run:

```bash
systemctl disable --now penguin-burnerd.service
```

That stops the profile from applying at the next normal boot. (The
`--restore-stock` command needs the daemon service running, so in rescue mode
either disable the unit as above, or `systemctl start penguin-burnerd.service`
first and then run `penguin-burner-cli --restore-stock`.)
