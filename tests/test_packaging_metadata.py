from __future__ import annotations

import struct
import tomllib
import zlib
from pathlib import Path


def test_base_package_installs_gui_dependencies() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(metadata["project"]["dependencies"])

    assert "PySide6>=6.7" in dependencies
    assert "colorama>=0.4" in dependencies
    assert "pyqtgraph>=0.13" in dependencies


def test_ui_extra_remains_as_compatibility_alias() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    ui_dependencies = set(metadata["project"]["optional-dependencies"]["ui"])

    assert "PySide6>=6.7" in ui_dependencies
    assert "colorama>=0.4" in ui_dependencies
    assert "pyqtgraph>=0.13" in ui_dependencies


def test_native_packages_install_pyqtgraph_colorama_runtime_dependency() -> None:
    arch_pkgbuild = Path("packaging/arch/PKGBUILD").read_text(encoding="utf-8")
    debian_control = Path("packaging/debian/control").read_text(encoding="utf-8")
    rpm_spec = Path("packaging/rpm/penguin-burner.spec").read_text(encoding="utf-8")

    assert "'python-colorama'" in arch_pkgbuild
    assert "'python-pyqtgraph>=0.13'" in arch_pkgbuild
    assert " python3-colorama," in debian_control
    assert " python3-pyqtgraph (>= 0.13)," in debian_control
    assert "Requires:       python3-colorama" in rpm_spec
    assert "Requires:       python3-pyqtgraph" in rpm_spec


def test_console_scripts_use_gui_default_and_explicit_cli_names() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    scripts = metadata["project"]["scripts"]

    assert scripts["penguin-burner"] == "ui:main"
    assert scripts["pburn"] == "ui:main"
    assert scripts["penguin-burner-ui"] == "ui:main"
    assert scripts["pburn-ui"] == "ui:main"
    assert "penguin-burner-yolo" not in scripts
    assert "pburn-yolo" not in scripts
    assert scripts["penguin-burner-cli"] == "penguin_burner:cli_main"
    assert scripts["pburn-cli"] == "penguin_burner:cli_main"
    assert "penguin_burner" not in scripts


def test_package_installs_desktop_launcher_and_icons() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    data_files = metadata["tool"]["setuptools"]["data-files"]
    package_data = metadata["tool"]["setuptools"]["package-data"]
    packages = set(metadata["tool"]["setuptools"]["packages"])

    assert "ui.assets" in packages
    assert data_files["share/applications"] == [
        "packaging/linux/io.github.jpietek.PenguinBurner.desktop"
    ]
    assert data_files["share/icons/hicolor/256x256/apps"] == [
        "packaging/icons/hicolor/256x256/apps/penguin-burner.png"
    ]
    assert data_files["share/icons/hicolor/512x512/apps"] == [
        "packaging/icons/hicolor/512x512/apps/penguin-burner.png"
    ]
    assert "*.png" in package_data["ui.assets"]


def test_desktop_icons_are_transparent_and_large_enough() -> None:
    for path in (
        Path("packaging/icons/hicolor/256x256/apps/penguin-burner.png"),
        Path("packaging/icons/hicolor/512x512/apps/penguin-burner.png"),
        Path("ui/assets/penguin-burner.png"),
        Path("docs/assets/penguin-burner-logo.png"),
    ):
        width, height, alphas = _png_alpha_channel(path)
        corners = (
            alphas[0],
            alphas[width - 1],
            alphas[(height - 1) * width],
            alphas[-1],
        )
        opaque_indices = [index for index, alpha in enumerate(alphas) if alpha > 0]
        xs = [index % width for index in opaque_indices]
        ys = [index // width for index in opaque_indices]

        assert corners == (0, 0, 0, 0)
        assert max(xs) - min(xs) >= int(width * 0.68)
        assert max(ys) - min(ys) >= int(height * 0.76)


def test_package_installs_shared_subprocess_locale_helper() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    py_modules = set(metadata["tool"]["setuptools"]["py-modules"])

    assert "subprocess_locale" in py_modules
    assert "cuda_bruteforce_stability" in py_modules
    assert "hidden_nvapi_voltage" in py_modules
    assert "import_afterburner_fan_curve" in py_modules
    assert "import_afterburner_vf_curve" in py_modules
    assert "penguin_burner_errors" in py_modules
    assert "q2rtx_stability" in py_modules


def test_fedora_rpm_accepts_fedora_and_rpmfusion_nvidia_drivers() -> None:
    spec_text = Path("packaging/rpm/penguin-burner.spec").read_text(
        encoding="utf-8"
    )

    assert "nvidia-driver-cuda >= 3:580" in spec_text
    assert "xorg-x11-drv-nvidia-cuda >= 3:580" in spec_text
    assert "xorg-x11-drv-nvidia-580xx-cuda >= 3:580" in spec_text


def test_package_installs_auto_uv3_subpackages_and_initial_check() -> None:
    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    packages = set(metadata["tool"]["setuptools"]["packages"])

    assert "auto_uv" not in packages
    assert "cli" in packages
    assert "initial_check" in packages
    assert "auto_uv3.recovery" in packages
    assert "auto_uv3.scan_mode" in packages
    assert "auto_uv3.final_verification" in packages
    assert "manual_fan_curve_editor" in packages
    assert "manual_uv_curve_editor" in packages
    assert "runtime_fan_control" in packages
    assert "runtime_gpu_control" in packages
    assert "runtime_stability_test" in packages
    assert "saved_profile_verification" in packages
    assert "saved_uv_profiles" in packages


def test_desktop_launcher_is_english_only_nvidia_gpu_tool() -> None:
    desktop_text = Path(
        "packaging/linux/io.github.jpietek.PenguinBurner.desktop"
    ).read_text(encoding="utf-8")

    assert "Name=NVIDIA GPU Automatic Undervolting Tool" in desktop_text
    assert "GenericName=NVIDIA GPU Automatic Undervolting Tool" in desktop_text
    assert "single voltage/frequency bin precision" in desktop_text
    assert "MSI Afterburner imports" in desktop_text
    assert "LACT exports" in desktop_text
    assert "Exec=penguin-burner" in desktop_text
    assert "Icon=penguin-burner" in desktop_text
    assert "Penguin Burner" in desktop_text
    assert "PenguinBurner" in desktop_text
    assert "StartupWMClass=io.github.jpietek.PenguinBurner" in desktop_text
    assert "Name[" not in desktop_text
    assert "Comment[" not in desktop_text


def test_readme_uses_logo_image_instead_of_emoji_title() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    first_lines = "\n".join(readme.splitlines()[:10])

    assert "docs/assets/penguin-burner-logo.png" in first_lines
    assert "# NVIDIA GPU Automatic Undervolting Tool" in first_lines
    assert "PenguinBurner is an NVIDIA GPU automatic undervolting tool." in first_lines
    assert "single voltage/frequency bin precision" in readme
    assert "MSI Afterburner imports and LACT exports are also supported." in readme
    assert "dead-silent fan operation" in readme
    assert "https://github.com/jpietek/PenguinBurner/issues" in readme
    assert "# 🐧 PenguinBurner 🔥" not in readme


def _png_alpha_channel(path: Path) -> tuple[int, int, list[int]]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    offset = 8
    width = height = bit_depth = color_type = None
    compressed = bytearray()
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_data = data[offset + 8 : offset + 8 + length]
        offset += length + 12
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(
                ">IIBB",
                chunk_data[:10],
            )
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    assert isinstance(width, int)
    assert isinstance(height, int)
    assert bit_depth == 8
    assert color_type == 6

    raw = zlib.decompress(bytes(compressed))
    row_bytes = width * 4
    previous = bytearray(row_bytes)
    pixels = bytearray()
    source_offset = 0
    for _row in range(height):
        filter_type = raw[source_offset]
        source_offset += 1
        row = bytearray(raw[source_offset : source_offset + row_bytes])
        source_offset += row_bytes
        _unfilter_png_row(row, previous, filter_type, bytes_per_pixel=4)
        pixels.extend(row)
        previous = row
    return width, height, list(pixels[3::4])


def _unfilter_png_row(
    row: bytearray,
    previous: bytearray,
    filter_type: int,
    *,
    bytes_per_pixel: int,
) -> None:
    if filter_type == 0:
        return
    for index, value in enumerate(row):
        left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
        up = previous[index]
        upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
        if filter_type == 1:
            predictor = left
        elif filter_type == 2:
            predictor = up
        elif filter_type == 3:
            predictor = (left + up) // 2
        elif filter_type == 4:
            predictor = _png_paeth(left, up, upper_left)
        else:
            raise AssertionError(f"unsupported PNG filter: {filter_type}")
        row[index] = (value + predictor) & 0xFF


def _png_paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    if up_distance <= upper_left_distance:
        return up
    return upper_left
