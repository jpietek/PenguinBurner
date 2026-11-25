"""The new window's Profiles tab over the real backend: the runtime source
(ui/features/profiles/runtime_profiles_source.py), its command runner and
the window wiring. Owners are monkeypatched where
the classic window's tests patch them; the asserted calls are the daemon
commands, daemon requests and files the classic Profiles tab produces."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tomllib
from pathlib import Path

import pytest
from PySide6 import QtCore as C

import runtime.profile_status as status_mod
import ui.features.profiles.runtime_profiles_source as source_mod
from cli import runtime_config_file
from profiles.uv.profile_rules import (
    CHOOSE_TARGET_TO_APPLY,
    LEGACY_VERIFY_BEFORE_APPLY,
    OTHER_GPU_PROFILE,
    VERIFY_BEFORE_APPLY,
    apply_refusal,
    boot_apply_checked,
    defaults_restored_after_status,
    main_gpu_change_allowed,
    profile_delete_safety,
    silent_fan_checked_on_load,
    verify_refusal,
)
from profiles.uv.profile_store import STOCK_PROFILE_SELECTOR
from ui.design.tokens import AppearanceSettings
from ui.features.profiles.command_runner import ProfileCommandRunner, failure_message
from ui.features.profiles.profile_model import CommandResult
from ui.features.profiles.profiles_page import ProfileDialogs, ProfilesPage
from ui.features.profiles.profiles_shell import ProfilesTab, profiles_status_sources
from ui.features.tuning.gpu_selection import GpuChoice

GPU_A = "GPU-aaaa"
GPU_B = "GPU-bbbb"


def summary(profile_id, *, gpu=GPU_A, verified=True, path=None, **extra):
    profile = {
        "profile_id": profile_id,
        "candidate_id": f"cand-{profile_id}",
        "display_name": profile_id.title(),
        "profile_created_at": "2026-11-20T10:00:00",
        "path": path or f"/profiles/auto-uv-profile-{profile_id}.json",
        "final_verified": verified,
        "candidate_voltage_mv": 900,
        "lock_clock_mhz": 2600,
        "profile_tier": "Balanced",
    }
    if gpu:
        profile["gpu_identity"] = {"uuid": gpu, "name": "NVIDIA GeForce RTX 5080"}
    profile.update(extra)
    return profile


class FakeRunner:
    """The command runner, finishing each command at once."""

    def __init__(self):
        self.started: list[tuple[str, str, list[str]]] = []
        self.ok = True

    def is_running(self) -> bool:
        return False

    def start(self, label, kind, command, finished, *, fail_text="", success_message=""):
        self.started.append((label, kind, list(command)))
        if self.ok:
            finished(CommandResult(True, success_message or f"{label} complete."))
        else:
            finished(CommandResult(False, "NVML: Insufficient Permissions", details="log"))


class Backend:
    """Records the backend calls the source makes."""

    def __init__(self, monkeypatch):
        self.profiles = [summary("bal"), summary("perf", profile_tier="Performance")]
        self.gpus = [GpuChoice(index=0, name="RTX 5080", uuid=GPU_A, power_limit_default_w=360.0)]
        self.running: dict = {}
        self.boot: dict = {}
        self.calls: list[tuple] = []
        self.fan_ready = True
        self.silent_config = False
        m = monkeypatch
        m.setattr(source_mod, "read_auto_uv_profile_summaries", lambda: [dict(p) for p in self.profiles])
        m.setattr(source_mod, "detected_gpu_choices", lambda: list(self.gpus))
        m.setattr(source_mod, "live_profile_status", self.live_status)
        m.setattr(
            source_mod,
            "runtime_profile_command",
            lambda action, **kw: ["apply-runtime-intent", action, json.dumps(kw, sort_keys=True)],
        )
        m.setattr(source_mod, "delete_profiles_command", lambda paths: ["delete", *paths])
        m.setattr(source_mod, "sync_profile_fan_payload", self.sync_fan)
        m.setattr(source_mod, "silent_fan_curve_to_runtime_config", lambda v: self.calls.append(("silent_fan_config", v)))
        m.setattr(source_mod, "silent_fan_curve_from_runtime_config", lambda: self.silent_config)
        m.setattr(source_mod, "persist_on_startup_to_runtime_config", lambda v: self.calls.append(("persist_on_startup", v)))
        m.setattr(source_mod, "clear_boot_runtime_spec", self.clear_boot)
        m.setattr(source_mod, "set_boot_main_gpu", lambda uuid: self.calls.append(("main_gpu", uuid)))
        m.setattr(source_mod, "persist_runtime_gpu_index", lambda i: self.calls.append(("gpu_index", i)))
        m.setattr(source_mod, "load_profile_tier_assignments", lambda gpu_uuid="": {})
        m.setattr(source_mod, "load_profile_tier_disabled_profile_ids", lambda gpu_uuid="": set())
        m.setattr(source_mod, "profile_fan_payload", lambda profile: None)
        m.setattr(source_mod, "profile_curve_plan", lambda profile: [])

    def live_status(self, uuids):
        boot = {"": self.boot.get("", {})}
        boot.update({u: self.boot.get(u, {}) for u in uuids})
        return dict(self.running), boot

    def sync_fan(self, profile):
        self.calls.append(("sync_fan", profile["profile_id"]))
        return self.fan_ready

    def clear_boot(self, *, gpu_uuid=""):
        self.calls.append(("clear_boot", gpu_uuid))
        self.boot.pop(gpu_uuid, None)


@pytest.fixture
def backend(monkeypatch):
    return Backend(monkeypatch)


@pytest.fixture
def source(qapp, backend):
    return source_mod.RuntimeProfilesSource(runner=FakeRunner(), poll_ms=0)


def applied_intent(command):
    return json.loads(command[2])


# ---------------------------------------------------------------- reads


def test_snapshot_reads_gpus_runtime_and_boot(source, backend):
    backend.running = {
        "selector": "bal",
        "gpu_uuid": GPU_A,
        "silent_fan_curve": True,
        "adaptive_auto_uv": False,
        "game_override": True,
        "standing_selector": "perf",
    }
    backend.boot = {GPU_A: {"selector": "perf", "main_gpu": True}, "": {"selector": "perf"}}
    source.refresh()
    [gpu] = source.gpus()
    assert (gpu.index, gpu.uuid, gpu.factory_power_w) == (0, GPU_A, 360.0)
    runtime = source.runtime()
    assert runtime.running_selector == "bal" and runtime.running_silent_fan
    assert runtime.game_override and runtime.standing_selector == "perf"
    assert source.boot_entry(GPU_A).selector == "perf"
    assert source.boot_entry(GPU_A).main_gpu
    assert source.boot_entry(GPU_B) is None
    assert source.boot_entry("").selector == "perf"


def test_silent_fan_load_rule_is_sticky(source, backend):
    assert not source.silent_fan_preference()
    backend.boot = {"": {"selector": "bal", "silent_fan_curve": True}}
    source.refresh()
    assert source.silent_fan_preference(), "a boot profile with the fan curve ticks it"
    backend.boot = {}
    backend.silent_config = True
    source.refresh()
    assert source.silent_fan_preference(), "the saved choice survives"


def test_status_poll_updates_only_on_change(qapp, backend, monkeypatch):
    source = source_mod.RuntimeProfilesSource(runner=FakeRunner(), poll_ms=0)
    seen = []
    source.subscribe(lambda: seen.append(source.runtime().running_selector))
    monkeypatch.setattr(source_mod, "STATUS_COLLECT_MS", 0)
    source.poll_status()
    source._poll_thread.join()
    source._collect_status()
    assert seen == []
    backend.running = {"selector": "perf", "gpu_uuid": GPU_A}
    source.poll_status()
    source._poll_thread.join()
    source._collect_status()
    assert seen == ["perf"]


# ---------------------------------------------------------------- apply / restore


def test_apply_sends_the_daemon_intent_with_boot_and_fan(source, backend):
    results = []
    source.apply_profile("bal", GPU_A, silent_fan=True, persist_on_startup=True, done=results.append)
    [(label, kind, command)] = source.runner.started
    assert (label, kind) == ("Apply selected profile", "daemonize")
    assert applied_intent(command) == {
        "gpu_index": 0,
        "persist_on_startup": True,
        "profile_selector": "bal",
        "silent_fan_curve": True,
    }
    # The fan payload sync (a durable write) and the [ui] preference.
    assert ("sync_fan", "bal") in backend.calls
    assert ("silent_fan_config", True) in backend.calls
    assert results == [CommandResult(True, "Apply selected profile complete.", "bal")]


def test_apply_session_only_sends_clear_boot(source, backend):
    source.apply_profile("perf", GPU_A, silent_fan=False, persist_on_startup=False, done=lambda r: None)
    intent = applied_intent(source.runner.started[0][2])
    assert intent["persist_on_startup"] is False and intent["silent_fan_curve"] is False
    assert ("sync_fan", "perf") not in backend.calls


def test_apply_refuses_without_a_runtime_fan_curve(source, backend):
    backend.fan_ready = False
    results = []
    source.apply_profile("bal", GPU_A, silent_fan=True, persist_on_startup=False, done=results.append)
    assert source.runner.started == []
    assert results == [CommandResult(False, "No runtime-ready silent fan curve is available.")]


def test_apply_binds_a_legacy_profile_first(source, backend, monkeypatch, tmp_path):
    saved = tmp_path / "auto-uv-profile-old.json"
    saved.write_text("{}")
    backend.profiles.append(summary("old", gpu="", path=str(saved)))
    source.refresh()
    bound = []
    monkeypatch.setattr(
        source_mod,
        "bind_legacy_profile_to_gpu",
        lambda profile, index: bound.append((profile["profile_id"], index)),
    )
    source.apply_profile("old", GPU_A, silent_fan=False, persist_on_startup=False, done=lambda r: None)
    assert bound == [("old", 0)]
    assert applied_intent(source.runner.started[0][2])["profile_selector"] == "old"


def test_restore_defaults_saves_stock_for_boot_always(source, backend):
    results = []
    source.restore_defaults(GPU_A, silent_fan=True, done=results.append)
    [(label, kind, command)] = source.runner.started
    assert kind == "restore-keep-stock"
    intent = applied_intent(command)
    assert intent["profile_selector"] == STOCK_PROFILE_SELECTOR
    assert intent["persist_on_startup"] is True and intent["silent_fan_curve"] is True
    assert results[0].ok and source.runtime().defaults_restored
    # A real profile going live clears the flag.
    backend.running = {"selector": "bal"}
    source.refresh()
    assert not source.runtime().defaults_restored


def test_a_failed_command_reports_its_log(source, backend):
    source.runner.ok = False
    results = []
    source.apply_profile("bal", GPU_A, silent_fan=False, persist_on_startup=False, done=results.append)
    assert len(results) == 1 and not results[0].ok and results[0].details == "log"


# ---------------------------------------------------------------- delete


def test_delete_removes_user_files_directly(source, backend, monkeypatch):
    deleted = []
    monkeypatch.setattr(source_mod, "delete_auto_uv_profile_paths", lambda paths: deleted.extend(paths) or list(paths))
    results = []
    source.delete_profiles(["bal"], results.append)
    assert deleted == ["/profiles/auto-uv-profile-bal.json"]
    assert results == [CommandResult(True, "Deleted 1 saved profile.")]
    assert source.runner.started == []


def test_delete_falls_back_to_the_root_service(qapp, backend, monkeypatch):
    def refuse(paths):
        raise PermissionError("root-owned")

    monkeypatch.setattr(source_mod, "delete_auto_uv_profile_paths", refuse)
    asked = []
    source = source_mod.RuntimeProfilesSource(
        runner=FakeRunner(), poll_ms=0, ensure_daemon_ready=lambda label: asked.append(label) or True
    )
    results = []
    source.delete_profiles(["bal", "perf"], results.append)
    assert asked == ["Deleting saved Auto-UV profiles"]
    assert source.runner.started[0][1:] == (
        "delete-profiles",
        ["delete", "/profiles/auto-uv-profile-bal.json", "/profiles/auto-uv-profile-perf.json"],
    )
    assert results[0].message == "Selected Auto-UV profiles deleted."
    # Declining the root-service prompt runs nothing.
    source.ensure_daemon_ready = lambda label: False
    source.delete_profiles(["bal"], results.append)
    assert results[-1].cancelled and len(source.runner.started) == 1


# ---------------------------------------------------------------- boot, GPU, tiers


def test_clear_boot_writes_the_preference_only_after_the_daemon(source, backend, monkeypatch):
    source.clear_boot(GPU_A)
    assert backend.calls == [("clear_boot", GPU_A), ("persist_on_startup", False)]

    def refuse(*, gpu_uuid=""):
        raise OSError("socket")

    monkeypatch.setattr(source_mod, "clear_boot_runtime_spec", refuse)
    backend.calls.clear()
    with pytest.raises(OSError):
        source.clear_boot(GPU_A)
    assert backend.calls == []


def test_preferences_and_main_gpu(source, backend):
    source.remember_apply_on_startup(True)
    source.remember_target_gpu(1)
    source.set_main_gpu(GPU_A)
    source.set_main_gpu("")
    assert backend.calls == [
        ("persist_on_startup", True),
        ("gpu_index", 1),
        ("main_gpu", GPU_A),
        ("main_gpu", ""),
    ]


def test_assign_tier_writes_the_profiles_gpu_store(source, monkeypatch):
    saved = []
    monkeypatch.setattr(source_mod, "save_profile_tier_assignment", lambda pid, tier, gpu_uuid: saved.append((pid, tier, gpu_uuid)))
    monkeypatch.setattr(source_mod, "save_profile_tier_none_assignment", lambda pid, gpu_uuid: saved.append((pid, None, gpu_uuid)))
    source.assign_tier("bal", "performance")
    source.assign_tier("bal", "none")
    assert saved == [("bal", "performance", GPU_A), ("bal", None, GPU_A)]


# ---------------------------------------------------------------- edits and LACT


def test_edits_save_copies_and_return_their_ids(source, monkeypatch):
    monkeypatch.setattr(source_mod, "editable_anchor_from_profile", lambda p: (900, 2600))
    calls = []

    def save_curve(profile, edit, *, original_anchor_voltage_mv, original_anchor_clock_mhz):
        calls.append((profile["profile_id"], original_anchor_voltage_mv, original_anchor_clock_mhz))
        return Path("/p/auto-uv-profile-new-edited.json"), {}

    monkeypatch.setattr(source_mod, "save_edited_curve_profile", save_curve)
    monkeypatch.setattr(
        source_mod,
        "save_edited_memory_offset_profile",
        lambda profile, value, *, original_memory_offset_mhz: calls.append((value, original_memory_offset_mhz))
        or (Path("/p/auto-uv-profile-mem-edited.json"), {}),
    )
    monkeypatch.setattr(source_mod, "editable_memory_offset_from_profile", lambda p: 1000)
    assert source.save_curve_edit("bal", object()) == "new-edited"
    assert source.save_memory_offset("bal", 2000) == "mem-edited"
    assert calls == [("bal", 900, 2600), (2000, 1000)]


def test_export_lact_reports_refusal_or_the_written_file(source, monkeypatch, tmp_path):
    from integrations.lact.export import LACT_GPU_ID_MISSING, LactExportRefused

    def refuse(profile, directory, *, include_fan_curve):
        raise LactExportRefused(LACT_GPU_ID_MISSING)

    monkeypatch.setattr(source_mod, "export_profile_to_lact", refuse)
    assert source.export_lact("bal", "/tmp/x", include_fan_curve=False).error == LACT_GPU_ID_MISSING
    written = tmp_path / "config.yaml"
    written.write_text("gpus:\n")
    monkeypatch.setattr(
        source_mod, "export_profile_to_lact", lambda p, d, *, include_fan_curve: (written, ["w1"])
    )
    result = source.export_lact("bal", str(tmp_path), include_fan_curve=True)
    assert (result.path, result.warnings, result.text) == (str(written), ("w1",), "gpus:\n")


def test_afterburner_import_persists_the_chosen_slot(source, monkeypatch):
    seen = []
    monkeypatch.setattr(
        source_mod,
        "persist_afterburner_import_selection",
        lambda entry: seen.append(entry) or {"profile_id": "ab", "section": "Profile1"},
    )
    assert source.save_afterburner_import({"section": "Profile1"}, None)["profile_id"] == "ab"
    assert seen == [{"section": "Profile1"}]


# ---------------------------------------------------------------- the page over the source


class Dialogs(ProfileDialogs):
    def __init__(self):
        self.calls = []

    def confirm(self, parent, title, text, *, accept="Yes"):
        self.calls.append(("confirm", title, text))
        return True

    def info(self, parent, title, text):
        self.calls.append(("info", title, text))

    def error(self, parent, title, text):
        self.calls.append(("error", title, text))

    def failure(self, parent, title, message, details):
        self.calls.append(("failure", title, details))


def make_page(source, gpu_index=None):
    return ProfilesPage(source, dialogs=Dialogs(), ensure_daemon_ready=lambda label: True, gpu_index=gpu_index)


def test_page_apply_on_startup_and_restore_defaults(source, backend):
    page = make_page(source)
    page.select_profiles(["bal"])
    page.set_apply_on_startup(True)
    assert ("persist_on_startup", True) in backend.calls
    page.apply_profile("bal")
    assert applied_intent(source.runner.started[-1][2])["persist_on_startup"] is True
    # Unticking clears the GPU's boot entry through the daemon.
    page.set_apply_on_startup(False)
    assert ("clear_boot", GPU_A) in backend.calls
    page.apply_profile("bal")
    assert applied_intent(source.runner.started[-1][2])["persist_on_startup"] is False
    # Restore defaults saves stock for boot even with the box unticked.
    page.restore_defaults()
    intent = applied_intent(source.runner.started[-1][2])
    assert intent["profile_selector"] == STOCK_PROFILE_SELECTOR and intent["persist_on_startup"]


def test_page_shows_the_daemons_boot_entry(source, backend):
    backend.boot = {GPU_A: {"selector": "perf"}, "": {"selector": "perf"}}
    source.refresh()
    page = make_page(source)
    assert page.startup_checked()
    assert page.table.pills("perf") == ["At startup"]
    assert page.status()["running"] == "Autostart profile: Perf"


def test_page_delete_of_the_running_profile_restores_stock(source, backend, monkeypatch, qapp):
    monkeypatch.setattr(source_mod, "delete_auto_uv_profile_paths", lambda paths: list(paths))
    backend.running = {"selector": "bal", "gpu_uuid": GPU_A}
    source.refresh()
    page = make_page(source)
    page.delete_selected(["bal"])
    assert "restore stock now and at boot" in page.dialogs.calls[-1][2]
    for _ in range(5):
        qapp.processEvents()
    assert applied_intent(source.runner.started[-1][2])["profile_selector"] == STOCK_PROFILE_SELECTOR


def test_page_multi_gpu_target_is_saved_and_preferred(source, backend):
    backend.gpus.append(GpuChoice(index=1, name="RTX 4070", uuid=GPU_B))
    source.refresh()
    page = make_page(source)
    assert not page.target.chosen, "two GPUs: choose one first"
    page.set_target_gpu(1)
    assert page.target.uuid == GPU_B and ("gpu_index", 1) in backend.calls
    preferred = make_page(source, gpu_index=0)
    assert preferred.target.uuid == GPU_A


def test_page_failure_opens_the_error_report(source):
    source.runner.ok = False
    page = make_page(source)
    page.apply_profile("bal")
    assert page.dialogs.calls[-1] == ("failure", "Apply selected profile failed", "log")
    assert page.notice[1] == "loss" and not page.busy()


# ---------------------------------------------------------------- command runner


class FakeController:
    def __init__(self, exit_code=0, start_ok=True):
        self.exit_code, self.start_ok = exit_code, start_ok
        self.on_output = self.on_finished = None
        self.commands = []

    def is_running(self):
        return False

    def start(self, kind, command, *, fail_text=""):
        self.commands.append((kind, command, fail_text))
        if not self.start_ok:
            self.on_output(fail_text + "\n")
            self.on_finished(kind, -1, "CrashExit")
            return False
        self.on_output("$ cmd\nworking\nNVML: Insufficient Permissions\n")
        self.on_finished(kind, self.exit_code, "NormalExit")
        return True


def test_runner_reports_each_command_once():
    results = []
    runner = ProfileCommandRunner(controller=FakeController())
    runner.start("Restore defaults", "restore-keep-stock", ["pb"], results.append)
    assert results == [CommandResult(True, "Restore defaults complete.")]
    runner = ProfileCommandRunner(controller=FakeController(exit_code=1))
    runner.start("Apply selected profile", "daemonize", ["pb"], results.append)
    failed = results[-1]
    assert failed.message == "NVML: Insufficient Permissions"
    assert "Exit code: 1" in failed.details and "working" in failed.details
    runner = ProfileCommandRunner(controller=FakeController(start_ok=False))
    runner.start("Apply selected profile", "daemonize", ["pb"], results.append, fail_text="Failed to start.")
    assert len(results) == 3 and results[-1].message == "Failed to start."
    assert failure_message("$ only\n", 3) == "Exit code 3."


# ---------------------------------------------------------------- window wiring


def test_profiles_status_routes_busy_and_idle_lines():
    assert profiles_status_sources({"running": "x", "busy": True}) == {
        "profilesBusy": {"running": "x", "busy": True}
    }
    assert profiles_status_sources({"running": "y"}) == {
        "profilesBusy": None,
        "profiles": {"running": "y"},
    }


def test_shell_window_hosts_the_profiles_tab(
    isolated_theme, offline_profiles, tmp_path, monkeypatch, dispose_window
):
    import ui.design.style as design
    import ui.shell_window as shell

    config = tmp_path / "config.toml"
    config.write_text("")
    monkeypatch.setattr(runtime_config_file, "default_config_path", lambda: config)
    design.install_theme(isolated_theme, AppearanceSettings())
    window = shell.ShellWindow()
    assert isinstance(window.pages[1], ProfilesPage)
    assert window.profiles_tab.page is window.pages[1]
    # Refresh after a root-service update re-reads the source.
    reads = []
    monkeypatch.setattr(offline_profiles, "read_auto_uv_profile_summaries", lambda: reads.append(1) or [])
    window.refresh_pages()
    assert reads == [1]
    assert tomllib.loads(config.read_text() or "") == {}
    dispose_window(window)


def test_import_runs_the_chooser_on_the_profiles_tab(isolated_theme, offline_profiles):
    class Window(C.QObject):
        gpu_index = None

        def __init__(self):
            super().__init__()
            from PySide6 import QtWidgets as W

            self.tabs = W.QTabBar()
            for name in ("a", "b", "c"):
                self.tabs.addTab(name)
            self.statuses = []

        def set_global_status(self, status, source):
            self.statuses.append((source, status))

        def set_header_progress(self, *args):
            pass

    window = Window()
    tab = ProfilesTab(window, source=source_mod.RuntimeProfilesSource(poll_ms=0))
    chosen = []
    tab.page.dialogs.afterburner_entry = lambda parent: chosen.append(parent) or None
    tab.import_afterburner()
    assert chosen == [tab.page] and window.tabs.currentIndex() == 1
    assert ("profiles", tab.page.status()) in window.statuses


# ---------------------------------------------------------------- shared rules


def test_backend_rules_match_the_classic_messages():
    bound = summary("bal")
    legacy = summary("old", gpu="")
    assert apply_refusal(bound, GPU_A, target_chosen=False, single_gpu=True) == CHOOSE_TARGET_TO_APPLY
    assert apply_refusal(summary("x", verified=False), GPU_A, target_chosen=True, single_gpu=True) == VERIFY_BEFORE_APPLY
    assert apply_refusal(bound, GPU_B, target_chosen=True, single_gpu=False) == OTHER_GPU_PROFILE
    assert apply_refusal(legacy, GPU_A, target_chosen=True, single_gpu=False) == LEGACY_VERIFY_BEFORE_APPLY
    assert apply_refusal(legacy, GPU_A, target_chosen=True, single_gpu=True) == ""
    assert verify_refusal(legacy, GPU_A, target_chosen=True) == ""
    assert verify_refusal(bound, GPU_B, target_chosen=True) == OTHER_GPU_PROFILE
    assert boot_apply_checked("", True) and boot_apply_checked("p", False)
    assert not boot_apply_checked("", False)
    assert not main_gpu_change_allowed(True, target_gpu_uuid=GPU_A, selection_required=True, has_boot_profile=False)
    assert main_gpu_change_allowed(False, target_gpu_uuid=GPU_A, selection_required=True, has_boot_profile=False)
    assert not main_gpu_change_allowed(False, target_gpu_uuid=GPU_A, selection_required=False, has_boot_profile=True)
    assert silent_fan_checked_on_load(False, {"silent_fan_curve": True}, {})
    assert not silent_fan_checked_on_load(False, {}, {})
    assert defaults_restored_after_status(True, STOCK_PROFILE_SELECTOR)
    assert not defaults_restored_after_status(True, "bal")


def test_delete_safety_counts_boot_and_running_profiles():
    profiles = [summary("bal"), summary("perf")]
    keep = profile_delete_safety(profiles, ["perf"], autostart_info={"selector": "bal"}, running_info={})
    assert not keep.restore_stock
    boot = profile_delete_safety(profiles, ["bal"], autostart_info={"selector": "bal"}, running_info={})
    assert boot.restore_stock and not boot.last_adaptive
    running = profile_delete_safety(profiles, ["perf"], autostart_info={}, running_info={"selector": "perf"})
    assert running.restore_stock


def test_boot_entries_come_from_one_daemon_read(monkeypatch):
    reads = []
    summary_payload = {
        "configured": True,
        "profile_id": "bal",
        "runtime_mode": "static",
        "gpu_uuid": GPU_A,
        "main_gpu_uuid": GPU_A,
        "gpus": [
            {"profile_id": "bal", "runtime_mode": "static", "gpu_uuid": GPU_A},
            {"profile_id": "", "runtime_mode": "stock", "gpu_uuid": GPU_B, "silent_fan_curve": True},
        ],
    }
    monkeypatch.setattr(status_mod, "boot_runtime_spec", lambda timeout_s: reads.append(1) or summary_payload)
    infos = status_mod.autostart_profile_infos(["", GPU_A, GPU_B, "GPU-none"])
    assert reads == [1]
    assert infos[""]["selector"] == "bal"
    assert infos[GPU_A]["main_gpu"] is True
    assert infos[GPU_B]["selector"] == STOCK_PROFILE_SELECTOR and infos[GPU_B]["silent_fan_curve"]
    assert infos["GPU-none"]["selector"] == ""
