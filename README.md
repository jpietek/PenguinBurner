<p align="center">
  <img src="docs/assets/penguin-burner-logo.png" alt="PenguinBurner logo" width="180">
</p>

# NVIDIA GPU Automatic Undervolting Tool

PenguinBurner is an NVIDIA GPU automatic undervolting tool. It helps you visualize and manage your GPU fine tuning setup with single voltage/frequency bin precision to maximize FPS per Watt, potentially leading into +33% and higher improvements for recent cards. MSI Afterburner imports and LACT exports are also supported.

Its main feature is **automatic** GPU undervolting: PenguinBurner tests your GPU under gaming and compute load, finds the most efficient stable undervolt, and can save it as a systemd daemon when you decide to apply it.

PenguinBurner is proven to work on modern Linux systems with the **NVIDIA proprietary** graphics driver. For best results, use an up-to-date driver. Supported GPUs are NVIDIA GeForce RTX 50 series Blackwell, RTX 40 series Ada Lovelace, and RTX 30 series Ampere cards. Older GPUs may miss required driver-level voltage/frequency control functionality.

GPU undervolting is meant to make your graphics card consume significantly less power while giving up as little performance as possible. The practical result can be **dead-silent fan operation**, **lower temperatures**, and lower electricity bills. PenguinBurner automatically searches for the operating sweet spot of your NVIDIA GPU, so you do not have to resort to trial and error or risk introducing avoidable system instability.

## Install

Install the published package:

```bash
python -m pip install --user --upgrade penguin-burner
```

Fedora 42/43/44 users can install the COPR package with a proprietary NVIDIA
driver stack from either Fedora's NVIDIA driver repository or RPM Fusion. If
you already have Fedora's `nvidia-driver-cuda` package installed, you can skip
the RPM Fusion enablement step and only enable the COPR.

For RPM Fusion systems:

```bash
sudo dnf install -y \
  https://download1.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm \
  https://download1.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm
sudo dnf install -y dnf-plugins-core
sudo dnf copr enable -y jpietek/penguin-burner
sudo dnf install -y penguin-burner
```

For systems already using Fedora's NVIDIA driver repository:

```bash
sudo dnf copr enable -y jpietek/penguin-burner
sudo dnf install -y penguin-burner
```

Arch Linux and CachyOS users can install the AUR package:

```bash
paru -S penguin-burner
```

or:

```bash
yay -S penguin-burner
```

Ubuntu 25.10 and 26.04 users can install the PPA package:

```bash
sudo add-apt-repository ppa:jpietek/penguin-burner
sudo apt update
sudo apt install penguin-burner
```

Bundled pip entrypoints:

- GUI: `penguin-burner` - alias: `pburn`
- CLI/non-GUI: `penguin-burner-cli` - alias: `pburn-cli`

The pip package also provides a desktop file, so PenguinBurner should appear with its icon in your desktop environment's app launcher.

If your shell cannot find the commands after installation, make sure `~/.local/bin` is in your `PATH`.

## Automatic Undervolting

Core PenguinBurner component in action: algorithmic Auto Undervolting with built-in performance and stability checks based on a path-tracing gaming scenario, **Q2RTX**, and a custom **CUDA** compute test.

![PenguinBurner Auto Undervolting V/F curve](docs/assets/1-uv-curve.png)

Afterburner alike curve editor in Linux, fully manual with all the shortcuts bells and whistles!

![PenguinBurner V/F curve editor](docs/assets/1a-vf-curve-editor.png)

![PenguinBurner Auto Undervolting performance bias slider](docs/assets/auto-uv-performance-bias.png)

Before a scan starts, the Performance bias slider lets you decide what kind of
undervolt PenguinBurner should search for. Move it toward **Efficiency** for the
lowest practical power draw, or toward **Performance** when you want the scan to
recover more clock and prioritize keeping or improving FPS.

## MSI Afterburner Import

Feel at home and import your MSI Afterburner profile from Windows.

![PenguinBurner MSI Afterburner import](docs/assets/2-afterburner-import.png)

Performance profile for the undervolt, especially for those with older GPU where every FPS matters. Hidden --yolo mode.

## Silent Fan Curve

Apply a silent fan curve after PenguinBurner finds a stable undervolt.

![PenguinBurner silent fan curve](docs/assets/3-fan-curve.png)

Customize your fan curve with manual editor

![PenguinBurner fan curve editor](docs/assets/3a-fan-curve-editor.png)

## LACT Export

Export to the LACT Linux GPU control tool is available from the profiles view.

## Proprietary Inputs Are Not Bundled

This repository does not ship MSI Afterburner binaries or copied profile exports.

If you want to import Afterburner data, point PenguinBurner at the real MSI Afterburner directory from Windows. By default that directory is:

```text
C:\Program Files (x86)\MSI Afterburner
```

## Acknowledgements

Special thanks to the [LACT project](https://github.com/ilya-zlobintsev/LACT) and to Ilya Zlobintsev for pushing Linux NVIDIA tuning forward.

While PenguinBurner was still reverse engineering proprietary NVIDIA binaries and had only working voltage getters, LACT landed a working custom voltage/frequency point setter first. In particular, [LACT pull request #957, feat: add Nvidia VF curve editor](https://github.com/ilya-zlobintsev/LACT/pull/957), was merged on April 18, 2026.

## Run At Your Own Risk

Real hardware changes are made during the Auto UV procedure.

PenguinBurner can perform operations such as:

- enabling persistence mode
- setting board power limits
- writing core V/F offsets
- writing memory V/F offsets
- taking over fan control

## Support

If you like the tool, please consider supporting the project on GitHub:

https://github.com/sponsors/jpietek

Having issues with PenguinBurner? Please report bugs here:

https://github.com/jpietek/PenguinBurner/issues

## CLI Documentation

The previous CLI-focused README has been archived here:

[readme-cli.md](readme-cli.md)

## Start Clean From Scratch

To reset PenguinBurner user state for a fresh profile-style run, remove the
config, local data, and cache directories:

```bash
rm -rf /home/jp/.config/PenguinBurner \
       /home/jp/.local/share/PenguinBurner \
       /home/jp/.cache/PenguinBurner
```

For a local PyPI-style wheel upgrade from this checkout, keep app flags separate
from pip flags:

```bash
python -m pip install --user --no-index --no-deps --find-links dist --upgrade penguin-burner
penguin-burner --yolo
```
