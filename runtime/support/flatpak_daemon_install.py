from __future__ import annotations

import shlex
from typing import Literal

from runtime.support.runtime_service import (
    DAEMON_BINARY,
    DAEMON_INSTALL_BASE,
    DAEMON_PRODUCT_DIRECTORY,
    DAEMON_SELINUX_EXEC_TYPE,
    DAEMON_SELINUX_MODULE_CIL,
    DAEMON_SELINUX_MODULE_NAME,
    LAST_RUNTIME_STATE_PATH,
)

FlatpakRuntimeAction = Literal["apply-intent", "migrate-legacy", "none"]


def _daemon_restart_and_wait_script() -> str:
    return r"""
daemon_socket=/run/penguin-burnerd.sock

wait_for_penguin_burnerd() {
    attempt=0
    while [ "$attempt" -lt 30 ]; do
        if /usr/bin/python3 - "$daemon_socket" >/dev/null 2>&1 <<'PY'
import json
import socket
import sys

path = sys.argv[1]
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
    client.settimeout(1.0)
    client.connect(path)
    client.sendall(b'{"method":"status"}\n')
    data = b""
    while b"\n" not in data:
        chunk = client.recv(4096)
        if not chunk:
            break
        data += chunk
response = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
if not response.get("ok"):
    raise SystemExit(1)
PY
        then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 0.1
    done
    return 1
}

print_penguin_burnerd_diagnostics() {
    systemctl status --no-pager penguin-burnerd.service >&2 || true
    journalctl -u penguin-burnerd.service -n 80 --no-pager >&2 || true
}

restart_penguin_burnerd() {
    rm -f "$daemon_socket"
    if ! systemctl restart penguin-burnerd.service; then
        echo "Failed to restart penguin-burnerd.service." >&2
        print_penguin_burnerd_diagnostics
        return 1
    fi
    if wait_for_penguin_burnerd; then
        return 0
    fi
    echo "penguin-burnerd.service restarted, but $daemon_socket did not answer status." >&2
    print_penguin_burnerd_diagnostics
    return 1
}
""".strip()


def _runtime_block(action: FlatpakRuntimeAction) -> str:
    if action == "apply-intent":
        return (
            "\nintent_json=\"$(printf '%s' "
            '"$PENGUIN_BURNER_RUNTIME_INTENT_B64" | base64 -d)\"\n'
            "env PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 "
            'PYTHONPATH="$PENGUIN_BURNER_RUNTIME_PYTHONPATH" '
            'PENGUIN_BURNER_HOME="$PENGUIN_BURNER_RUNTIME_HOME" '
            "/usr/bin/python3 -m runtime.daemon_client apply-runtime-intent "
            '--boot "$intent_json"\n'
        )
    if action == "migrate-legacy":
        return (
            "\nenv PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 "
            'PYTHONPATH="$PENGUIN_BURNER_RUNTIME_PYTHONPATH" '
            'PENGUIN_BURNER_HOME="$PENGUIN_BURNER_RUNTIME_HOME" '
            "/usr/bin/python3 -m runtime.daemon_client "
            "migrate-legacy-boot-intent\n"
        )
    if action == "none":
        return ""
    raise ValueError(f"unknown Flatpak daemon runtime action: {action}")


def build_flatpak_daemon_install_script(
    *,
    runtime_action: FlatpakRuntimeAction,
) -> str:
    """Build the elevated host-side daemon installation transaction."""
    clear_legacy_state = runtime_action != "migrate-legacy"
    script = (
        r"""
legacy_unit=/etc/systemd/system/PenguinBurner.service
unit=/etc/systemd/system/penguin-burnerd.service
"""
        + f"daemon_install_base={DAEMON_INSTALL_BASE}\n"
        + f"daemon_product_dir={DAEMON_PRODUCT_DIRECTORY}\n"
        + f"daemon_dir={DAEMON_BINARY.parent}\n"
        + f'daemon_target="{DAEMON_BINARY}"\n'
        + f"daemon_selinux_exec_type={DAEMON_SELINUX_EXEC_TYPE}\n"
        + f"daemon_selinux_module={DAEMON_SELINUX_MODULE_NAME}\n"
        + f"daemon_selinux_cil={shlex.quote(DAEMON_SELINUX_MODULE_CIL.strip())}\n"
        + r"""
daemon_tmp=
unit_tmp=
daemon_backup=
unit_backup=
legacy_unit_backup=
daemon_had_previous=0
unit_had_previous=0
legacy_unit_had_previous=0
daemon_was_enabled=0
daemon_was_active=0
legacy_was_enabled=0
legacy_was_active=0
service_state_captured=0
rollback_started=0
install_committed=0

cleanup_install_temps() {
    if [ -n "$daemon_tmp" ]; then rm -f -- "$daemon_tmp"; fi
    if [ -n "$unit_tmp" ]; then rm -f -- "$unit_tmp"; fi
}

discard_rollback_links() {
    if [ -n "$daemon_backup" ]; then rm -f -- "$daemon_backup"; daemon_backup=; fi
    if [ -n "$unit_backup" ]; then rm -f -- "$unit_backup"; unit_backup=; fi
    if [ -n "$legacy_unit_backup" ]; then
        rm -f -- "$legacy_unit_backup"
        legacy_unit_backup=
    fi
}

restore_rollback_link() {
    backup=$1
    target=$2
    had_previous=$3
    if [ "$had_previous" -eq 1 ]; then
        mv -f -- "$backup" "$target"
    else
        rm -f -- "$target"
    fi
}

disable_present_service() {
    # `systemctl disable` fails on a unit that does not exist, and the legacy
    # PenguinBurner.service is absent on almost every system -- rollback must
    # not report failure for a unit there was nothing to disable.
    if ! systemctl cat -- "$1" >/dev/null 2>&1; then
        return 0
    fi
    systemctl disable --now "$1" >/dev/null 2>&1
}

rollback_daemon_install() {
    set +e
    rollback_failed=0
    if [ "$service_state_captured" -eq 1 ]; then
        disable_present_service penguin-burnerd.service || rollback_failed=1
        disable_present_service PenguinBurner.service || rollback_failed=1
    fi
    restore_rollback_link "$daemon_backup" "$daemon_target" "$daemon_had_previous" || rollback_failed=1
    restore_rollback_link "$unit_backup" "$unit" "$unit_had_previous" || rollback_failed=1
    restore_rollback_link "$legacy_unit_backup" "$legacy_unit" "$legacy_unit_had_previous" || rollback_failed=1
    if [ "$service_state_captured" -eq 1 ]; then
        systemctl daemon-reload || rollback_failed=1
        if [ "$daemon_was_enabled" -eq 1 ]; then
            systemctl enable penguin-burnerd.service || rollback_failed=1
        fi
        if [ "$legacy_was_enabled" -eq 1 ]; then
            systemctl enable PenguinBurner.service || rollback_failed=1
        fi
        if [ "$daemon_was_active" -eq 1 ]; then
            systemctl start penguin-burnerd.service || rollback_failed=1
            wait_for_penguin_burnerd || rollback_failed=1
        fi
        if [ "$legacy_was_active" -eq 1 ]; then
            systemctl start PenguinBurner.service || rollback_failed=1
        fi
    fi
    if [ "$rollback_failed" -ne 0 ]; then
        echo "Rollback of the previous PenguinBurner hardware service also failed." >&2
        return 1
    fi
    echo "Restored the previous PenguinBurner hardware service after setup failed." >&2
    return 0
}

finish_daemon_install() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$status" -ne 0 ] && [ "$rollback_started" -eq 1 ] && \
       [ "$install_committed" -eq 0 ]; then
        rollback_daemon_install || true
    fi
    cleanup_install_temps
    if [ "$install_committed" -eq 1 ]; then discard_rollback_links; fi
    exit "$status"
}

commit_daemon_install() {
    discard_rollback_links
    install_committed=1
}

ensure_safe_root_directory() {
    directory=$1
    if [ ! -e "$directory" ]; then
        mkdir -m0755 -- "$directory"
    fi
    if [ ! -d "$directory" ] || [ -L "$directory" ] || \
       [ "$(stat -c %u "$directory")" != 0 ] || \
       [ $((0$(stat -c %a "$directory") & 0022)) -ne 0 ]; then
        echo "$directory is not a safe root-owned real directory." >&2
        exit 1
    fi
}

preserve_regular_file() {
    path=$1
    backup_template=$2
    preserved_link=
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then
        return 1
    fi
    if [ ! -f "$path" ] || [ -L "$path" ] || \
       [ "$(stat -c %u "$path")" != 0 ] || \
       [ $((0$(stat -c %a "$path") & 0022)) -ne 0 ]; then
        echo "$path is not a safe root-owned managed file." >&2
        return 2
    fi
    rollback_link="$(mktemp "$backup_template")"
    rm -f -- "$rollback_link"
    ln -- "$path" "$rollback_link"
    preserved_link=$rollback_link
    return 0
}

selinux_is_active() {
    if [ -e /sys/fs/selinux/enforce ]; then return 0; fi
    if command -v selinuxenabled >/dev/null 2>&1; then
        selinuxenabled
        return $?
    fi
    return 1
}

selinux_file_type() {
    stat -c %C -- "$1" 2>/dev/null | cut -d: -f3
}

daemon_label_is_executable() {
    case "$1" in
        ""|"?"|"$daemon_selinux_exec_type") return 0 ;;
    esac
    return 1
}

restore_daemon_context() {
    if ! selinux_is_active; then return 0; fi
    if ! command -v restorecon >/dev/null 2>&1; then
        echo "SELinux is active, but restorecon is unavailable." >&2
        return 1
    fi
    restorecon -RF "$daemon_product_dir"
    # Policies before Fedora 42 / EL10 label /var/opt var_t, which systemd
    # cannot execute; add PenguinBurner's file-context module only then.
    daemon_label="$(selinux_file_type "$daemon_target")"
    if daemon_label_is_executable "$daemon_label"; then return 0; fi
    if ! command -v semodule >/dev/null 2>&1; then
        echo "SELinux labels $daemon_target as $daemon_label, which systemd cannot execute, and semodule is unavailable." >&2
        return 1
    fi
    module_dir="$(mktemp -d)"
    printf '%s\n' "$daemon_selinux_cil" > "$module_dir/$daemon_selinux_module.cil"
    if ! semodule -i "$module_dir/$daemon_selinux_module.cil"; then
        rm -rf -- "$module_dir"
        echo "Could not install the $daemon_selinux_module SELinux file-context module." >&2
        return 1
    fi
    rm -rf -- "$module_dir"
    restorecon -RF "$daemon_product_dir"
    daemon_label="$(selinux_file_type "$daemon_target")"
    if daemon_label_is_executable "$daemon_label"; then return 0; fi
    echo "SELinux still labels $daemon_target as $daemon_label; systemd cannot start penguin-burnerd." >&2
    return 1
}

trap finish_daemon_install EXIT
trap 'exit 1' HUP INT TERM

ensure_safe_root_directory "$daemon_install_base"
ensure_safe_root_directory "$daemon_product_dir"
ensure_safe_root_directory "$daemon_dir"
if command -v findmnt >/dev/null 2>&1; then
    mount_options="$(findmnt -no OPTIONS --target "$daemon_dir" 2>/dev/null || true)"
    case ",$mount_options," in
        *,noexec,*)
            echo "PenguinBurner cannot install its hardware service because $daemon_dir is mounted noexec." >&2
            exit 1
            ;;
    esac
fi

if [ ! -f "$PENGUIN_BURNER_DAEMON_BINARY_SRC" ] || \
   [ -L "$PENGUIN_BURNER_DAEMON_BINARY_SRC" ]; then
    echo "PenguinBurner daemon install source is not a regular file." >&2
    exit 1
fi
daemon_tmp="$(mktemp "$daemon_dir/.penguin-burnerd.XXXXXX")"
dd if="$PENGUIN_BURNER_DAEMON_BINARY_SRC" of="$daemon_tmp" \
   iflag=nofollow,fullblock,nonblock oflag=nofollow conv=fsync status=none
if [ "$(od -An -tx1 -N4 "$daemon_tmp" | tr -d '[:space:]')" != 7f454c46 ]; then
    echo "PenguinBurner daemon install source is not an ELF binary." >&2
    exit 1
fi
chown root:root "$daemon_tmp"
chmod 0755 "$daemon_tmp"
# Pre-flight the staged binary before touching any installed state. The Flatpak
# daemon is built against the freedesktop runtime but executes on the host, so
# a host with older system libraries (glibc below the runtime's floor) cannot
# load it; surface the loader's error with clear guidance instead of a service
# that can never start.
if ! daemon_preflight="$("$daemon_tmp" --version 2>&1)"; then
    echo "The bundled PenguinBurner hardware daemon cannot run on this host:" >&2
    echo "$daemon_preflight" >&2
    echo "This host's system libraries are older than the Flatpak build supports; install PenguinBurner from a native package (COPR, PPA, AUR, or pip) instead." >&2
    exit 1
fi
unit_tmp="$(mktemp /etc/systemd/system/.penguin-burnerd.service.XXXXXX)"
printf '%s' "$PENGUIN_BURNER_SYSTEMD_UNIT_B64" | base64 -d > "$unit_tmp"
chown root:root "$unit_tmp"
chmod 0644 "$unit_tmp"

if preserve_regular_file "$daemon_target" "$daemon_dir/.penguin-burnerd.rollback.XXXXXX"; then
    daemon_backup=$preserved_link
    daemon_had_previous=1
elif [ "$?" -ne 1 ]; then
    exit 1
fi
if preserve_regular_file "$unit" /etc/systemd/system/.penguin-burnerd.rollback.XXXXXX; then
    unit_backup=$preserved_link
    unit_had_previous=1
elif [ "$?" -ne 1 ]; then
    exit 1
fi
if preserve_regular_file "$legacy_unit" /etc/systemd/system/.PenguinBurner.rollback.XXXXXX; then
    legacy_unit_backup=$preserved_link
    legacy_unit_had_previous=1
elif [ "$?" -ne 1 ]; then
    exit 1
fi
if systemctl is-enabled --quiet penguin-burnerd.service; then daemon_was_enabled=1; fi
if systemctl is-active --quiet penguin-burnerd.service; then daemon_was_active=1; fi
if systemctl is-enabled --quiet PenguinBurner.service; then legacy_was_enabled=1; fi
if systemctl is-active --quiet PenguinBurner.service; then legacy_was_active=1; fi
service_state_captured=1
rollback_started=1

mv -f -- "$daemon_tmp" "$daemon_target"
daemon_tmp=
restore_daemon_context
mv -f -- "$unit_tmp" "$unit"
unit_tmp=
systemctl disable --now PenguinBurner.service >/dev/null 2>&1 || true
if [ -f "$legacy_unit" ]; then
    rm -f "$legacy_unit"
    echo "Removed existing static PenguinBurner.service."
fi
"""
        + "\nrm -f /run/penguin-burner/active-runtime.json\n"
        + r"""
systemctl daemon-reload
systemctl reset-failed PenguinBurner.service >/dev/null 2>&1 || true
systemctl reset-failed penguin-burnerd.service >/dev/null 2>&1 || true
systemctl enable penguin-burnerd.service
restart_penguin_burnerd
"""
        + _runtime_block(runtime_action)
        + (
            f'\nrm -f "{LAST_RUNTIME_STATE_PATH}"\n'
            if clear_legacy_state
            else ""
        )
        + r"""
commit_daemon_install
echo "Copied penguin-burnerd to /var/opt/penguin-burner/libexec/penguin-burnerd."
echo "Installed and enabled penguin-burnerd.service at $unit."
echo "Follow the journal with: journalctl -u penguin-burnerd.service --since \"-4 hours\" -f"
""".strip()
    )
    return f"{_daemon_restart_and_wait_script()}\n{script}"
