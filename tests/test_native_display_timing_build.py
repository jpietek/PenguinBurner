"""Exercise the build boundary which allowed 0.8.4 to omit display capture."""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "overlay/native/latency_layer"
CHECK = ROOT / "scripts/check-native-display-timing.py"


def test_old_headers_fail_configuration(tmp_path):
    if not shutil.which("cmake") or not shutil.which("c++"):
        pytest.skip("native build toolchain unavailable")
    headers = tmp_path / "headers/vulkan"
    headers.mkdir(parents=True)
    (headers / "vulkan.h").write_text("// Simulate headers without present timing.\n")
    result = subprocess.run(
        ["cmake", "-S", str(SOURCE), "-B", str(tmp_path / "build"),
         f"-DVULKAN_INCLUDE_DIR={headers.parent}"], capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "Vulkan headers must support VK_EXT_present_timing" in result.stderr


def test_bundled_headers_build_capture_and_validator_rejects_missing_code(tmp_path):
    if not shutil.which("cmake") or not shutil.which("c++"):
        pytest.skip("native build toolchain unavailable")
    build = tmp_path / "build"
    subprocess.run(["cmake", "-S", str(SOURCE), "-B", str(build),
                    "-DCMAKE_BUILD_TYPE=Release"], check=True)
    subprocess.run(["cmake", "--build", str(build), "-j2"], check=True)
    library = build / "libVkLayer_penguinburner_latency.so"
    subprocess.run([sys.executable, str(CHECK), str(library)], check=True)
    # Model the old binary: valid ELF, but no EXT timing implementation.
    incomplete = tmp_path / "incomplete.so"
    incomplete.write_bytes(library.read_bytes().replace(
        b"vkGetPastPresentationTimingEXT", b"xxGetPastPresentationTimingEXT"))
    result = subprocess.run([sys.executable, str(CHECK), str(incomplete)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "missing display-timing entry points" in result.stderr


def test_wheel_requires_capture_in_both_architectures(tmp_path):
    wheel = tmp_path / "candidate.whl"
    payload = b"\x7fELF" + b"\0".join((
        b"vkGetPastPresentationTimingEXT",
        b"vkSetSwapchainPresentTimingQueueSizeEXT",
        b"vkGetSwapchainTimeDomainPropertiesEXT", b"",
    ))
    for valid32 in (False, True):
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("overlay/native_layer/libVkLayer_penguinburner_latency.so", payload)
            archive.writestr("overlay/native_layer/libVkLayer_penguinburner_latency_i386.so",
                             payload if valid32 else b"\x7fELF")
        result = subprocess.run([sys.executable, str(CHECK), str(wheel)],
                                capture_output=True, text=True)
        assert (result.returncode == 0) == valid32
        if not valid32:
            assert "_i386.so: missing display-timing" in result.stderr
