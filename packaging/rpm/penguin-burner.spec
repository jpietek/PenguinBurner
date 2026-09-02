Name:           penguin-burner
Version:        0.7.9
Release:        1%{?dist}
Summary:        NVIDIA GPU automatic undervolting and fine tuning tool

%global debug_package %{nil}

# PyPI calls the minimal Qt wheel "PySide6-Essentials", but Fedora folds that
# distribution into python3-pyside6 and provides python3dist(pyside6), not
# python3dist(pyside6-essentials).  Keep the explicit Fedora dependency below
# and filter only the unresolvable wheel-metadata dependency generated from the
# installed .dist-info/METADATA file.
%global __requires_exclude ^python%{python3_version}dist\\(pyside6-essentials\\).*$

License:        GPL-3.0-or-later
URL:            https://github.com/jpietek/PenguinBurner
Source0:        %{name}-%{version}.tar.gz

ExclusiveArch:  x86_64
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  cmake
BuildRequires:  gcc-c++
# Cross-compiles the NVAPI latency shim (a PE nvapi64.dll) into the wheel;
# the static winpthreads is needed for its -static link.
BuildRequires:  mingw64-gcc-c++
BuildRequires:  mingw64-winpthreads-static
BuildRequires:  vulkan-headers
BuildRequires:  desktop-file-utils
# Root daemon (penguin-burnerd) is compiled from the bundled Rust crate.
BuildRequires:  cargo
BuildRequires:  rust

Requires:       python3-pyside6 >= 6.7
Requires:       python3-colorama >= 0.4
Requires:       python3-pyqtgraph >= 0.13
Requires:       python3-pyyaml >= 6.0
Requires:       hicolor-icon-theme
Requires:       bash
Requires:       systemd

%description
PenguinBurner is an NVIDIA GPU automatic undervolting tool. It helps you
visualize and manage your GPU fine tuning setup with single voltage/frequency
bin precision to maximize FPS per Watt, potentially leading into +33% and higher
improvements for recent cards. MSI Afterburner imports and LACT exports are
also supported.

It provides a Qt desktop UI, command-line tools, automatic voltage/frequency
scanning with Q2RTX/CUDA-backed verification, and optional runtime profile
service installation.

This package is intended for Fedora systems using the proprietary NVIDIA driver,
version 580 or newer, from either Fedora's NVIDIA driver repository or RPM
Fusion.

%prep
%autosetup

%build
export PENGUIN_BURNER_REQUIRE_NATIVE_LAYER=1
export PENGUIN_BURNER_REQUIRE_NVAPI_SHIM=1
%pyproject_wheel

# Root daemon: compiled from the bundled Rust crate in burnerd/. --locked pins
# the committed Cargo.lock; crates.io is fetched at build time, so the COPR
# project must have internet access enabled (see packaging/rpm/README.md).
cargo build --release --locked --manifest-path burnerd/Cargo.toml

%install
%pyproject_install
%pyproject_save_files \
    auto_uv \
    cli \
    common \
    curve_editors \
    integrations \
    overlay \
    profiles \
    runtime \
    stability \
    ui \
    drivers \
    penguin_burner

# Root daemon binary at the fixed discovery path (0755, root-owned).
install -D -m 0755 burnerd/target/release/penguin-burnerd \
    %{buildroot}%{_libexecdir}/penguin-burnerd

desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.jpietek.PenguinBurner.desktop

%files -f %{pyproject_files}
%license LICENSE
%doc README.md readme-cli.md
%{_bindir}/penguin-burner
%{_bindir}/pburn
%{_bindir}/penguin-burner-cli
%{_bindir}/pburn-cli
%{_bindir}/PENGUIN_BURNER
%{_libexecdir}/penguin-burnerd
%{_bindir}/penguin-burner-install-wrappers
%{_datadir}/applications/io.github.jpietek.PenguinBurner.desktop
%{_datadir}/icons/hicolor/256x256/apps/penguin-burner.png
%{_datadir}/icons/hicolor/512x512/apps/penguin-burner.png
%{_datadir}/penguin-burner/penguin_burner.sh

%changelog
* Mon Aug 17 2026 PenguinBurner contributors <noreply@github.com> - 0.7.9-1
- Add RTD3 sleep handling, resume recovery, telemetry fallback, and partial
  multi-GPU profile support.

* Sat Aug 08 2026 PenguinBurner contributors <noreply@github.com> - 0.7.8-1
- Hotfix mobile GPU power-limit detection.

* Thu Aug 06 2026 PenguinBurner contributors <noreply@github.com> - 0.7.7-1
- Fix RTX 5090-class and other power-limited Auto-UV scan scenarios.
- Skip unsupported fixed power writes on mobile GPUs.
- Improve saved-profile verification and memory-offset editing.

* Wed Jul 15 2026 PenguinBurner contributors <noreply@github.com> - 0.7.6-1
- Install or update the hardware service before the Auto-UV setup dialog
  opens, instead of showing a generic GPU with no limits.
- Name the unreachable hardware service in the setup dialog and how to fix
  it.
- Remove the legacy PenguinBurner.service unit file during migration.

* Wed Jul 15 2026 PenguinBurner contributors <noreply@github.com> - 0.7.5-1
- Add the opt-in "Apply on startup" toggle; applies are session-only by
  default and a new --restore-stock recovery command resets to stock.
- Tolerate mobile GPUs that reject the power-limit setter or expose no
  controllable fans.
- Show Proton vs Native Linux per Steam game and gray the compatibility
  selector out for native titles.

* Tue Jul 14 2026 PenguinBurner contributors <noreply@github.com> - 0.7.4-1
- Deduplicate in-game telemetry with per-session sample ownership.
- Keep the Vulkan layer marker fallback active when the NVAPI shim is
  deployed but never streams.
- Remove superseded latency fallbacks, dead flags, and legacy scripts.

* Tue Jul 14 2026 PenguinBurner contributors <noreply@github.com> - 0.7.3-1
- Install the Flatpak Steam host integration only when host Steam is detected.
- Never block GUI startup or privileged actions on Steam integration repair.

* Tue Jul 14 2026 PenguinBurner contributors <noreply@github.com> - 0.7.2-2
- Fix Fedora dependency resolution for the PySide6 runtime package.

* Sat Jun 20 2026 PenguinBurner contributors <noreply@github.com> - 0.5-1
- Remove nvidia-smi shell calls in favor of NVML API helpers.
- Use true headless Q2RTX tuned for PenguinBurner benchmarking.
- Include multiple minor Auto-UV, UI, fan, profile, and cleanup fixes.

* Mon Jun 15 2026 PenguinBurner contributors <noreply@github.com> - 0.4.7-1
- Use PenguinBurner's headless Q2RTX benchmark binary and shareware data.
- Remove Q2RTX OpenSSL compatibility staging, RUNPATH patching, RPM payload
  extraction, gamescope, Xvfb, and off-screen window workarounds.
- Read hot benchmark metrics from the Q2RTX event pipe instead of loop log
  parsing.

* Sun Jun 14 2026 PenguinBurner contributors <noreply@github.com> - 0.4.6-1
- Add the PENGUIN_BURNER_DUMP_LATENCY_DATA advanced diagnostic environment.
- Document advanced latency diagnostics.

* Sun Jun 14 2026 PenguinBurner contributors <noreply@github.com> - 0.4.5-1
- Fix phantom frame generation reported on titles without a marker stream.
- Mark Beta development status; packaging and license metadata fixes.

* Sun Jun 14 2026 PenguinBurner contributors <noreply@github.com> - 0.4.4-1
- Package PenguinBurner 0.4.4.
- Build the native Vulkan latency layer.
- Add cmake, gcc-c++, and vulkan-headers build dependencies.
- Package overlay and latency telemetry modules and entry points.

* Mon Jun 01 2026 PenguinBurner contributors <noreply@github.com> - 0.2.6-1
- Package PenguinBurner 0.2.6.
- Constrain Fedora Python GUI dependency versions.
- Do not hard-require distro-packaged NVIDIA drivers.

* Wed May 27 2026 PenguinBurner contributors <noreply@github.com> - 0.2-2
- Include nvml_perf_cap_reason in the RPM Python file manifest.

* Tue May 26 2026 PenguinBurner contributors <noreply@github.com> - 0.2-1
- Revise Auto-UV profiles for the 0.2 major release.
- Tune the Efficiency profile for the lowest stable voltage while retaining as much clock as possible.
- Keep Performance profiles focused on revised voltage and clock targets.
- Reapply memory OC offset consistently with runtime V/F curve reapplies, thanks to cbro33's pull request.

* Mon May 25 2026 PenguinBurner contributors <noreply@github.com> - 0.1.8-1
- Replace old Auto-UV internals with the cleaned Auto-UV package namespace.
- Add a separate Performance Auto-OC ladder after the balanced undervolt pass.
- Improve Auto-UV table progress, status, and Auto-OC reporting.
- Keep Performance final selection sorted by measured FPS.

* Wed May 13 2026 PenguinBurner contributors <noreply@github.com> - 0.1.7-1
- Honor time-based Q2RTX verification durations instead of precomputed loop counts.
- Improve PRIME laptop selected-GPU execution and diagnostics.
- Add safer interrupted-probe crash caching.
- Use a conservative Auto-UV default for RTX 3080.

* Fri May 08 2026 PenguinBurner contributors <noreply@github.com> - 0.1.6-1
- Add python3-colorama runtime dependency for pyqtgraph.
- Add borrowed GPU voltage/frequency guardrails for Auto-UV.
- Cap performance-mode Auto-OC by the GPU Performance table voltage.
- Improve final candidate sorting and stopped-scan final candidate selection.

* Mon May 04 2026 PenguinBurner contributors <noreply@github.com> - 0.1.5-1
- Initial COPR package for Fedora 42, 43, and 44.
