import shutil
import subprocess
from pathlib import Path

import pytest

from runtime.support.flatpak_daemon_install import build_flatpak_daemon_install_script
from runtime.support.runtime_service import DAEMON_SELINUX_MODULE_CIL

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None, reason="needs a POSIX shell"
)


def _write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _run_restore_daemon_context(
    tmp_path: Path, labels: list[str], *, semodule: bool = True
) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the host script's real restore_daemon_context with faked tools.

    ``stat -c %C`` answers successive labels from ``labels``; restorecon and
    semodule only record their calls.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Keep the host's SELinux administration tools out of the test. In
    # particular, the missing-semodule case must not invoke a real semodule.
    for command in ("sh", "head", "tail", "mv", "cut", "mktemp", "basename", "cp", "rm"):
        executable = shutil.which(command)
        assert executable is not None, f"test requires {command}"
        (bin_dir / command).symlink_to(executable)
    log = tmp_path / "calls.log"
    label_file = tmp_path / "labels"
    label_file.write_text("\n".join(labels) + "\n", encoding="utf-8")
    _write_stub(bin_dir, "restorecon", f'echo "restorecon $*" >> "{log}"\n')
    _write_stub(
        bin_dir,
        "stat",
        f'label="$(head -n 1 "{label_file}")"\n'
        f'tail -n +2 "{label_file}" > "{label_file}.next"\n'
        f'mv "{label_file}.next" "{label_file}"\n'
        'echo "system_u:object_r:${label}:s0"\n',
    )
    if semodule:
        _write_stub(
            bin_dir,
            "semodule",
            f'echo "semodule $1 $(basename "$2")" >> "{log}"\n'
            f'cp "$2" "{tmp_path}/module.cil"\n',
        )
    script = build_flatpak_daemon_install_script(runtime_action="none")
    definitions = script.split("trap finish_daemon_install EXIT", 1)[0]
    product = tmp_path / "penguin-burner"
    program = (
        definitions
        + f'\ndaemon_product_dir="{product}"\n'
        + f'daemon_target="{product}/libexec/penguin-burnerd"\n'
        + "selinux_is_active() { return 0; }\n"
        + "restore_daemon_context\n"
    )
    result = subprocess.run(
        ["sh", "-eu", "-c", program],
        capture_output=True,
        text=True,
        env={"PATH": str(bin_dir)},
        check=False,
    )
    return result, log


def test_host_script_keeps_policy_label_that_systemd_can_execute(tmp_path: Path) -> None:
    result, log = _run_restore_daemon_context(tmp_path, ["bin_t"])

    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"restorecon -RF {tmp_path / 'penguin-burner'}"
    ]


def test_host_script_adds_file_context_module_when_policy_labels_var_t(
    tmp_path: Path,
) -> None:
    result, log = _run_restore_daemon_context(tmp_path, ["var_t", "bin_t"])

    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"restorecon -RF {tmp_path / 'penguin-burner'}",
        "semodule -i penguin_burner.cil",
        f"restorecon -RF {tmp_path / 'penguin-burner'}",
    ]
    # The Flatpak host path installs the very module the Python path does.
    assert (tmp_path / "module.cil").read_text(encoding="utf-8") == (
        DAEMON_SELINUX_MODULE_CIL
    )


def test_host_script_fails_when_relabel_does_not_take(tmp_path: Path) -> None:
    result, _log = _run_restore_daemon_context(tmp_path, ["var_t", "var_t"])

    assert result.returncode != 0
    assert "still labels" in result.stderr


def test_host_script_names_missing_semodule(tmp_path: Path) -> None:
    result, _log = _run_restore_daemon_context(tmp_path, ["var_t"], semodule=False)

    assert result.returncode != 0
    assert "semodule is unavailable" in result.stderr
