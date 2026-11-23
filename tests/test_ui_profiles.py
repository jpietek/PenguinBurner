"""Coverage for profiles/uv/profile_rules.py (profile selection, delete/autostart
logic, status text) and runtime/profile_status.py (the daemon and systemd
query wrappers, subprocess monkeypatched).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import profiles.uv.profile_rules as rules
import runtime.profile_status as status


@pytest.fixture(autouse=True)
def _no_host_runtime_state(monkeypatch):
    """Keep the host's published runtime state out of these tests.

    An adaptive run's reported profile now prefers the live state file over
    the job spec, so without this every test that stubs a daemon payload would
    quietly read whatever the developer's own daemon happens to be running --
    green on CI, red on a machine with a profile applied. Tests that care
    about the live layer stub this themselves and override the default.
    """
    monkeypatch.setattr(status, "read_overlay_state", dict)


_P1 = {"profile_id": "p1", "candidate_id": "c1", "path": "/tmp/p1.json", "display_name": "P1"}
_P2 = {"profile_id": "p2", "candidate_id": "c2", "path": "/tmp/p2.json"}


# --- selection ----------------------------------------------------------------


def test_profile_for_selector_variants() -> None:
    catalog = [_P1, _P2]
    assert rules.profile_for_selector(catalog, "latest") is _P1
    assert rules.profile_for_selector(catalog, "p2") is _P2
    assert rules.profile_for_selector(catalog, "c1") is _P1
    assert rules.profile_for_selector(catalog, "/tmp/p1.json") is _P1
    assert rules.profile_for_selector(catalog, "p1") is _P1  # stem/name
    assert rules.profile_for_selector(catalog, "") is None
    assert rules.profile_for_selector(catalog, "nope") is None
    assert rules.profile_for_selector([], "latest") is None


def test_selected_ids_include_selector() -> None:
    assert rules.selected_profile_ids_include_selector([_P1], ["p1"], "p1") is True
    assert rules.selected_profile_ids_include_selector([_P1], ["p2"], "p1") is False
    assert rules.selected_profile_ids_include_selector([_P1], [], "p1") is False


# --- delete / autostart action ------------------------------------------------


def test_delete_autostart_action_non_adaptive() -> None:
    info = {"selector": "p1", "adaptive_auto_uv": False}
    assert rules.profile_delete_autostart_action([_P1], ["p1"], info) == {
        "action": "restore-stock"
    }
    assert rules.profile_delete_autostart_action([_P1, _P2], ["p2"], info) == {
        "action": "keep"
    }
    assert rules.profile_delete_autostart_action([_P1], [], info) == {"action": "keep"}
    assert rules.profile_delete_autostart_action([_P1], ["p1"], {"selector": ""}) == {
        "action": "keep"
    }


def test_delete_autostart_action_adaptive_branches(monkeypatch) -> None:
    info = {"selector": "p1", "adaptive_auto_uv": True}
    monkeypatch.setattr(
        rules, "resolve_profile_tier_profiles",
        lambda profs, *_store, gpu_uuid="", include_legacy_profiles=False: {"balanced": _P2},
    )

    # Two remaining tiers -> keep.
    monkeypatch.setattr(rules, "available_adaptive_tiers", lambda resolved: ["a", "b"])
    assert rules.profile_delete_autostart_action([_P1, _P2], ["p1"], info) == {
        "action": "keep"
    }

    # Exactly one remaining tier -> keep; adaptive applies it without switching.
    monkeypatch.setattr(rules, "available_adaptive_tiers", lambda resolved: ["balanced"])
    assert rules.profile_delete_autostart_action([_P1, _P2], ["p1"], info) == {
        "action": "keep"
    }

    # No remaining tiers -> restore stock as the standing boot state.
    monkeypatch.setattr(rules, "available_adaptive_tiers", lambda resolved: [])
    assert rules.profile_delete_autostart_action([_P1], ["p1"], info)["action"] == (
        "restore-stock"
    )


def test_delete_adaptive_profile_only_counts_its_gpu_profiles() -> None:
    profile_a = {
        "profile_id": "profile-a",
        "final_verified": True,
        "profile_tier": "Balanced",
        "gpu_identity": {"uuid": "GPU-A"},
    }
    profile_b = {
        "profile_id": "profile-b",
        "final_verified": True,
        "profile_tier": "Balanced",
        "gpu_identity": {"uuid": "GPU-B"},
    }

    action = rules.profile_delete_autostart_action(
        [profile_a, profile_b],
        ["profile-a"],
        {
            "selector": "profile-a",
            "adaptive_auto_uv": True,
            "gpu_uuid": "GPU-A",
        },
    )

    assert action == {
        "action": "restore-stock",
        "reason": "last-usable-adaptive-profile",
    }


# --- capability / label helpers ----------------------------------------------


def test_capability_helpers() -> None:
    assert rules.profile_can_apply({"final_verified": True}) is True
    assert rules.profile_can_apply({}) is False
    assert rules.profile_can_verify(_P1) is True
    assert rules.profile_can_verify({}) is False
    assert rules.profile_is_deletable(_P1) is True
    assert rules.profile_is_deletable({}) is False
    assert rules.profile_verify_selector(_P1) == "/tmp/p1.json"
    assert rules.profile_verify_selector({"profile_id": "p9"}) == "p9"


def test_adaptive_tier_keys_and_labels(monkeypatch) -> None:
    monkeypatch.setattr(rules, "resolve_profile_tier_profiles", lambda profs, *_store, **k: {})
    monkeypatch.setattr(rules, "available_adaptive_tiers", lambda resolved: ["efficiency", "performance"])
    monkeypatch.setattr(rules, "profile_tier_label", lambda tier: tier.title())
    assert rules.adaptive_profile_tier_keys([_P1]) == ["efficiency", "performance"]
    assert rules.adaptive_profile_tier_labels([_P1]) == ["Efficiency", "Performance"]


def test_status_label_and_frequency_voltage() -> None:
    assert rules.profile_status_label([_P1], "p1") == "P1"  # display_name wins
    # _P2 has no display_name/clock/voltage -> falls back to a non-empty label.
    assert rules.profile_status_label([_P2], "p2")
    assert rules.profile_status_label([], "__systemd_default__") == "latest Auto-UV profile"
    assert rules.profile_status_label([], "ghost") == "ghost"
    assert rules.profile_frequency_voltage(
        {"lock_clock_mhz": 2500, "candidate_voltage_mv": 900}
    ) == "2500 MHz 900 mV"
    assert rules.profile_frequency_voltage({"lock_clock_mhz": 2500}) == "2500 MHz"
    assert rules.profile_frequency_voltage({"candidate_voltage_mv": 900}) == "900 mV"
    assert rules.profile_frequency_voltage({}) == ""


def test_runner_status_text_branches() -> None:
    catalog = [_P1, _P2]
    running_match = rules.runner_status_text(
        catalog, running_selector="p1", autostart_selector="p1", running_silent_fan=True
    )
    assert "Currently running profile" in running_match
    assert "Autostart: Yes" in running_match

    running_diff = rules.runner_status_text(
        catalog, running_selector="p1", autostart_selector="p2"
    )
    assert "Autostart profile" in running_diff and "Autostart: No" in running_diff

    autostart_only = rules.runner_status_text(catalog, autostart_selector="p2")
    assert "Not running now." in autostart_only

    assert rules.runner_status_text(catalog) == "No running/autostart profile available yet."


# --- command-text parsing -----------------------------------------------------


def test_profile_info_from_command_text() -> None:
    info = status.profile_info_from_command_text(
        "pburn --auto-uv-profile p1 --silent-fan-curve --adaptive-auto-uv"
    )
    assert info == {"selector": "p1", "silent_fan_curve": True, "adaptive_auto_uv": True}
    assert status.profile_info_from_command_text("pburn --auto-uv-profile=p2")["selector"] == "p2"
    assert status.profile_info_from_command_text("pburn run", default_if_present=True)[
        "selector"
    ] == "__systemd_default__"
    assert status.profile_info_from_command_text("")["selector"] == ""


# --- delete confirmation text -------------------------------------------------


def test_delete_confirmation_text_variants() -> None:
    assert "the selected profiles" in rules.delete_confirmation_text([])
    assert "Auto-UV profile P1" in rules.delete_confirmation_text(["P1"])
    assert "2 selected profiles" in rules.delete_confirmation_text(["A", "B"])
    assert "restore stock now and at boot" in rules.delete_confirmation_text(
        ["P1"], restores_stock=True
    )
    assert "last usable Adaptive" in rules.delete_confirmation_text(
        ["P1"], restores_stock=True, removes_last_usable_adaptive_profile=True
    )
    assert "last usable Adaptive Auto-UV profiles" in rules.delete_confirmation_text(
        ["A", "B"], restores_stock=True, removes_last_usable_adaptive_profile=True
    )


# --- systemd wrappers (subprocess + path monkeypatched) -----------------------


def test_systemctl_backed_queries(monkeypatch) -> None:
    monkeypatch.setattr(status, "_daemon_status_payload", lambda: {})
    monkeypatch.setattr(
        status.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="")
    )
    assert status.systemd_service_is_enabled() is True
    assert status.penguin_burner_runtime_is_active() is True

    def _boom(*a, **k):
        raise OSError("no systemctl")

    monkeypatch.setattr(status.subprocess, "run", _boom)
    assert status.systemd_service_is_enabled() is False


def test_legacy_running_exec_start(monkeypatch) -> None:
    monkeypatch.setattr(
        status.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="pburn --auto-uv-profile p3\n"),
    )
    assert "--auto-uv-profile p3" in status._legacy_systemd_running_exec_start()
    monkeypatch.setattr(
        status.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="x")
    )
    assert status._legacy_systemd_running_exec_start() == ""


def test_daemon_unit_autostart_and_entry_exists(monkeypatch, tmp_path) -> None:
    # Boot intent is reported by the daemon; the persistent entry remains the
    # installed unit file.
    unit = tmp_path / "pb.service"
    unit.write_text("[Service]\n", encoding="utf-8")
    legacy = tmp_path / "legacy.service"
    monkeypatch.setattr(status, "systemd_service_is_enabled", lambda: True)
    monkeypatch.setattr(
        status, "boot_runtime_spec",
        lambda **_kwargs: {
            "configured": True,
            "profile_id": "p4",
            "gpu_uuid": "GPU-A",
            "runtime_mode": "static",
            "silent_fan_curve": True,
        },
    )
    monkeypatch.setattr(status, "systemd_service_unit_path", lambda: unit)
    monkeypatch.setattr(status, "legacy_systemd_service_unit_path", lambda: legacy)
    assert status.systemd_autostart_profile_info() == {
        "selector": "p4",
        "silent_fan_curve": True,
        "adaptive_auto_uv": False,
        "gpu_uuid": "GPU-A",
    }
    assert status.systemd_unit_entry_exists() is True

    # If the daemon API is unavailable, fall back to visible host unit files.
    monkeypatch.setattr(
        status, "boot_runtime_spec",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    monkeypatch.setattr(
        status, "systemd_service_unit_path", lambda: tmp_path / "missing.service"
    )
    assert status.systemd_unit_entry_exists() is False


def test_autostart_info_selects_saved_spec_by_gpu_uuid(monkeypatch) -> None:
    monkeypatch.setattr(
        status, "boot_runtime_spec",
        lambda **_kwargs: {
            "configured": True,
            "gpu_uuid": "GPU-B",
            "main_gpu_uuid": "GPU-A",
            "profile_id": "profile-b",
            "runtime_mode": "static",
            "gpus": [
                {
                    "configured": True,
                    "gpu_uuid": "GPU-A",
                    "profile_id": "profile-a",
                    "runtime_mode": "adaptive",
                    "silent_fan_curve": True,
                },
                {
                    "configured": True,
                    "gpu_uuid": "GPU-B",
                    "profile_id": "profile-b",
                    "runtime_mode": "static",
                    "silent_fan_curve": False,
                },
            ],
        },
    )

    assert status.systemd_autostart_profile_info(gpu_uuid="gpu-a") == {
        "selector": "profile-a",
        "silent_fan_curve": True,
        "adaptive_auto_uv": True,
        "gpu_uuid": "GPU-A",
        "main_gpu": True,
    }


def test_autostart_info_uses_daemon_inside_flatpak_without_systemctl(monkeypatch) -> None:
    monkeypatch.setattr(
        status, "systemd_service_is_enabled",
        lambda: (_ for _ in ()).throw(AssertionError("must not query systemctl")),
    )
    monkeypatch.setattr(
        status, "boot_runtime_spec",
        lambda **_kwargs: {
            "configured": True,
            "profile_id": "adaptive-profile",
            "runtime_mode": "adaptive",
            "silent_fan_curve": True,
        },
    )

    assert status.systemd_autostart_profile_info() == {
        "selector": "adaptive-profile",
        "silent_fan_curve": True,
        "adaptive_auto_uv": True,
    }


def test_autostart_and_running_info(monkeypatch) -> None:
    monkeypatch.setattr(status, "systemd_service_is_enabled", lambda: False)
    monkeypatch.setattr(
        status, "boot_runtime_spec",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    monkeypatch.setattr(
        status, "_legacy_systemd_autostart_profile_info",
        lambda: {"selector": "", "silent_fan_curve": False, "adaptive_auto_uv": False},
    )
    assert status.systemd_autostart_profile_info() == {
        "selector": "",
        "silent_fan_curve": False,
        "adaptive_auto_uv": False,
    }
    monkeypatch.setattr(status, "systemd_service_is_enabled", lambda: True)
    monkeypatch.setattr(
        status, "boot_runtime_spec",
        lambda **_kwargs: {
            "configured": True,
            "profile_id": "p5",
            "runtime_mode": "static",
            "silent_fan_curve": True,
        },
    )
    assert status.systemd_autostart_profile_info()["selector"] == "p5"

    monkeypatch.setattr(status, "_daemon_status_payload", lambda: {})
    monkeypatch.setattr(status, "_legacy_systemd_running_exec_start", lambda: "")
    # Nothing is actually running: the running-profile lookup reports empty and
    # does NOT fall back to the autostart entry (a boot-configured profile is not
    # a running one). Autostart is surfaced separately via
    # systemd_autostart_profile_info().
    assert status.running_auto_uv_profile_info()["selector"] == ""


def test_running_info_uses_daemon_status(monkeypatch) -> None:
    monkeypatch.setattr(
        status, "_daemon_status_payload",
        lambda: {
            "state": "runtime_profile_running",
            "active_job": {
                "type": "runtime_profile",
                "profile_id": "p6",
                "runtime_mode": "adaptive",
                "silent_fan_curve": False,
            },
        },
    )

    assert status.penguin_burner_runtime_is_active() is True
    info = status.running_auto_uv_profile_info()
    assert info["selector"] == "p6"
    assert info["adaptive_auto_uv"] is True


def test_runner_status_text_shows_per_game_override_and_standing() -> None:
    """Two writers, one truthful line: the Steam tab's per-game override is
    labeled as such, the standing profile is shown alongside, and autostart
    is judged against the STANDING profile (the override is transient)."""
    summaries = [
        {"profile_id": "game-prof", "candidate_voltage_mv": 920, "lock_clock_mhz": 2970},
        {"profile_id": "standing-prof", "candidate_voltage_mv": 850, "lock_clock_mhz": 2664},
    ]

    status = rules.runner_status_text(
        summaries,
        running_selector="game-prof",
        running_adaptive=True,
        autostart_selector="standing-prof",
        game_override=True,
        standing_selector="standing-prof",
    )

    assert "Currently running profile: 2970 MHz 920 mV (Adaptive, per-game)" in status
    assert "Standing: 2664 MHz 850 mV" in status
    assert "Autostart: Yes" in status

    # Standing stock: reads as Default; autostart mismatch reads No.
    status = rules.runner_status_text(
        summaries,
        running_selector="game-prof",
        autostart_selector="standing-prof",
        game_override=True,
        standing_selector="__stock__",
    )
    assert "(per-game)" in status
    assert "Standing: Default" in status
    assert "Autostart: No" in status


def test_delete_autostart_keeps_adaptive_when_legacy_tiers_remain_on_single_gpu() -> None:
    bound = {
        "profile_id": "bound-a",
        "final_verified": True,
        "profile_tier": "Performance",
        "gpu_identity": {"uuid": "GPU-A"},
    }
    legacy = {
        "profile_id": "legacy-b",
        "final_verified": True,
        "profile_tier": "Balanced",
    }
    info = {"selector": "bound-a", "adaptive_auto_uv": True, "gpu_uuid": "GPU-A"}

    without_legacy = rules.profile_delete_autostart_action(
        [bound, legacy], ["bound-a"], info
    )
    with_legacy = rules.profile_delete_autostart_action(
        [bound, legacy], ["bound-a"], info, include_legacy_profiles=True
    )

    assert without_legacy == {
        "action": "restore-stock",
        "reason": "last-usable-adaptive-profile",
    }
    assert with_legacy == {"action": "keep"}


def test_adaptive_tier_helpers_include_legacy_profiles_on_single_gpu() -> None:
    legacy = {
        "profile_id": "legacy-b",
        "final_verified": True,
        "profile_tier": "Balanced",
    }

    assert rules.adaptive_profile_tier_keys([legacy], gpu_uuid="GPU-A") == []
    assert rules.adaptive_profile_tier_keys(
        [legacy], gpu_uuid="GPU-A", include_legacy_profiles=True
    ) == ["balanced"]
    assert rules.adaptive_profile_tier_labels(
        [legacy], gpu_uuid="GPU-A", include_legacy_profiles=True
    ) == ["Balanced"]


def _state(profile_id: str, *, age_s: float = 0.0) -> dict[str, str]:
    import time

    return {
        "profile_id": profile_id,
        "updated_unix_ns": str(int((time.time() - age_s) * 1_000_000_000)),
    }


def test_live_runtime_profile_id_reads_the_published_state(monkeypatch) -> None:
    monkeypatch.setattr(status, "read_overlay_state", lambda: _state("perf-1"))
    assert status.live_runtime_profile_id() == "perf-1"


def test_live_runtime_profile_id_allows_the_slowest_supported_publish_interval(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        status, "read_overlay_state", lambda: _state("perf-1", age_s=60)
    )
    assert status.live_runtime_profile_id() == "perf-1"


def test_live_runtime_profile_id_rejects_a_stale_file(monkeypatch) -> None:
    """A file left by a finished session must not be reported as live."""
    monkeypatch.setattr(
        status, "read_overlay_state", lambda: _state("perf-1", age_s=76)
    )
    assert status.live_runtime_profile_id() == ""


def test_live_runtime_profile_id_survives_missing_or_broken_state(monkeypatch) -> None:
    monkeypatch.setattr(status, "read_overlay_state", dict)
    assert status.live_runtime_profile_id() == ""

    monkeypatch.setattr(
        status, "read_overlay_state",
        lambda: {"profile_id": "perf-1", "updated_unix_ns": "not-a-number"},
    )
    assert status.live_runtime_profile_id() == ""

    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr(status, "read_overlay_state", boom)
    assert status.live_runtime_profile_id() == ""


def _adaptive_payload() -> dict:
    return {
        "state": "runtime_profile_running",
        "active_job": {
            "runtime_mode": "adaptive",
            "profile_id": "eff-start",
            "gpu_uuid": "GPU-A",
        },
    }


def test_adaptive_status_reports_the_live_tier_not_the_starting_one(
    monkeypatch,
) -> None:
    """The job spec names the starting tier forever; adaptive has moved on."""
    monkeypatch.setattr(status, "_daemon_status_payload", _adaptive_payload)
    monkeypatch.setattr(status, "read_overlay_state", lambda: _state("perf-live"))

    info = status.running_auto_uv_profile_info()

    assert info["selector"] == "perf-live"
    assert info["adaptive_auto_uv"] is True


def test_adaptive_status_falls_back_to_the_spec_without_live_state(
    monkeypatch,
) -> None:
    monkeypatch.setattr(status, "_daemon_status_payload", _adaptive_payload)
    monkeypatch.setattr(status, "read_overlay_state", dict)

    assert status.running_auto_uv_profile_info()["selector"] == "eff-start"


def test_static_status_ignores_the_published_state(monkeypatch) -> None:
    """Only adaptive drifts from its spec; a static run must not be second-guessed."""
    monkeypatch.setattr(
        status, "_daemon_status_payload",
        lambda: {
            "state": "runtime_profile_running",
            "active_job": {"runtime_mode": "static", "profile_id": "pinned"},
        },
    )
    monkeypatch.setattr(status, "read_overlay_state", lambda: _state("something-else"))

    assert status.running_auto_uv_profile_info()["selector"] == "pinned"


def test_the_status_splits_into_what_runs_and_what_stands_behind_it() -> None:
    """One line ran to ~170 characters with the headline buried in the middle.

    The profile actually applied is what the bar exists to say, so it gets a
    line of its own and everything else follows underneath.
    """
    catalog = [_P1, _P2]

    head, detail = rules.runner_status_parts(
        catalog,
        running_selector="p1",
        running_adaptive=True,
        autostart_selector="p2",
        game_override=True,
        standing_selector="p2",
    )

    assert len(head) == 1
    assert head[0].startswith("Currently running profile:")
    assert any(part.startswith("Standing:") for part in detail)
    assert any(part.startswith("Autostart:") for part in detail)
    # Nothing about the standing profile may leak into the headline, which is
    # how a per-game fact once ended up reading as a claim about another one.
    assert "Standing" not in head[0]


def test_the_one_sentence_form_still_reads_the_same() -> None:
    """The tooltip and the CLI keep the joined sentence they always had."""
    catalog = [_P1, _P2]
    kwargs = dict(running_selector="p1", autostart_selector="p2")

    head, detail = rules.runner_status_parts(catalog, **kwargs)
    text = rules.runner_status_text(catalog, **kwargs)

    assert text == "; ".join(head + detail) + "."
    assert text.endswith(".")
    # The empty state is already a sentence and must not gain a second period.
    assert rules.runner_status_text(catalog).endswith("yet.")


def test_the_status_line_no_longer_pins_the_window_open(qapp) -> None:
    """A plain QLabel reports its whole string as its minimum width.

    That is why the window would not narrow and why a smaller one snapped back
    out to fit the text. The bar has to shrink and elide instead.
    """
    from ui.components.scan_controls import ScanControls
    from ui.qt import import_qt

    QtCore, QtGui, QtWidgets, _pg = import_qt()
    if QtWidgets is None:
        import pytest

        pytest.skip("PySide6 not available")

    controls = ScanControls(QtWidgets=QtWidgets, QtCore=QtCore)
    long_line = "Currently running profile: " + "2920 MHz 925 mV, " * 12

    controls.set_status_text(long_line, "Standing: 2730 MHz 850 mV · Autostart: Yes")

    assert controls.status_label.minimumSizeHint().width() == 0
    # The precise property: how wide the bar may shrink to must not depend on
    # how long the status is. It used to, which is why a smaller window snapped
    # back out to fit the text.
    wide = controls.widget.minimumSizeHint().width()
    controls.set_status_text("Idle", "")
    assert controls.widget.minimumSizeHint().width() == wide
    controls.set_status_text(long_line, "Standing: 2730 MHz 850 mV · Autostart: Yes")
    # The second line carries its own subject and appears only when it has one.
    assert controls.status_detail_label.isVisibleTo(controls.widget)
    # Elided on screen, whole in the tooltip.
    assert controls.status_label.toolTip().startswith("Currently running profile:")
    # A one-off message clears the detail, so the bar keeps its single-line height.
    controls.set_status_text("Idle")
    assert not controls.status_detail_label.isVisibleTo(controls.widget)
