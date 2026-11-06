from __future__ import annotations

import cli.entry as entry


def test_dispatch_cli_allows_adaptive_systemd_install_with_two_tiers(monkeypatch) -> None:
    installed = []

    monkeypatch.setattr(entry.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        entry,
        "daemon_status",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("not installed")),
    )
    monkeypatch.setattr(entry, "read_auto_uv_profiles", lambda: [{"profile_id": "p1"}])
    monkeypatch.setattr(
        entry,
        "resolve_profile_tier_profiles",
        lambda profiles: {
            "efficiency": {"profile_id": "p1"},
            "performance": {"profile_id": "p2"},
        },
    )
    monkeypatch.setattr(
        entry,
        "available_adaptive_tiers",
        lambda resolved: ["efficiency", "performance"],
    )
    monkeypatch.setattr(
        entry,
        "install_systemd_service",
        lambda program_file, argv, **kwargs: installed.append(
            (program_file, list(argv), kwargs)
        ),
    )

    exit_code = entry.dispatch_cli(
        program_file="/tmp/penguin_burner.py",
        main_callback=lambda *_args, **_kwargs: None,
        argv=["--install-systemd-service", "--adaptive-auto-uv"],
    )

    assert exit_code == 0
    assert installed[0][1] == ["--adaptive-auto-uv"]


def test_dispatch_cli_allows_adaptive_systemd_install_with_one_tier(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(entry.os, "geteuid", lambda: 0)
    monkeypatch.setattr(entry, "read_auto_uv_profiles", lambda: [{"profile_id": "p1"}])
    monkeypatch.setattr(
        entry,
        "resolve_profile_tier_profiles",
        lambda profiles: {"balanced": {"profile_id": "p1"}},
    )
    monkeypatch.setattr(
        entry,
        "available_adaptive_tiers",
        lambda resolved: ["balanced"],
    )
    installed = []
    monkeypatch.setattr(
        entry,
        "daemon_status",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("not installed")),
    )
    monkeypatch.setattr(
        entry,
        "install_systemd_service",
        lambda program_file, argv, **kwargs: installed.append(
            (program_file, list(argv), kwargs)
        ),
    )

    exit_code = entry.dispatch_cli(
        program_file="/tmp/penguin_burner.py",
        main_callback=lambda *_args, **_kwargs: None,
        argv=["--install-systemd-service", "--adaptive-auto-uv"],
    )

    assert exit_code == 0
    assert installed[0][1] == ["--adaptive-auto-uv"]
    assert capsys.readouterr().err == ""


def test_dispatch_cli_rejects_adaptive_systemd_install_with_no_tiers(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(entry.os, "geteuid", lambda: 0)
    monkeypatch.setattr(entry, "read_auto_uv_profiles", lambda: [])
    monkeypatch.setattr(entry, "resolve_profile_tier_profiles", lambda profiles: {})
    monkeypatch.setattr(entry, "available_adaptive_tiers", lambda resolved: [])
    monkeypatch.setattr(
        entry,
        "install_systemd_service",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("service should not be installed")
        ),
    )

    exit_code = entry.dispatch_cli(
        program_file="/tmp/penguin_burner.py",
        main_callback=lambda *_args, **_kwargs: None,
        argv=["--install-systemd-service", "--adaptive-auto-uv"],
    )

    assert exit_code == 1
    assert (
        "requires at least one saved verified" in capsys.readouterr().err
    )


def test_dispatch_cli_restore_stock_persists_stock_for_boot(monkeypatch) -> None:
    applied = []
    monkeypatch.setattr(
        entry,
        "apply_runtime_intent",
        lambda intent, **kwargs: applied.append((intent, kwargs))
        or {"started": True, "runtime_mode": "stock"},
    )

    exit_code = entry.dispatch_cli(
        program_file="/tmp/penguin_burner.py",
        main_callback=lambda *_args, **_kwargs: None,
        argv=["--restore-stock"],
    )

    assert exit_code == 0
    assert applied == [
        (
            {"profile_selector": "__stock__"},
            {
                "persist_on_startup": True,
                "socket_path": entry.DEFAULT_DAEMON_SOCKET,
            },
        )
    ]


def test_dispatch_cli_daemonize_applies_through_running_daemon(monkeypatch) -> None:
    applied = []
    monkeypatch.setattr(
        entry,
        "resolve_auto_uv_profile",
        lambda *_args, **_kwargs: {"profile_id": "latest"},
    )
    monkeypatch.setattr(
        entry,
        "apply_runtime_intent",
        lambda intent, **kwargs: applied.append((intent, kwargs))
        or {"started": True, "runtime_mode": "static"},
    )

    exit_code = entry.dispatch_cli(
        program_file="/tmp/penguin_burner.py",
        main_callback=lambda *_args, **_kwargs: None,
        argv=["--daemonize", "--auto-uv-profile", "latest", "--gpu-index", "1"],
    )

    assert exit_code == 0
    assert applied == [
        (
            {
                "profile_selector": "latest",
                "silent_fan_curve": False,
                "adaptive_auto_uv": False,
                "adaptive_target_fps": None,
                "game_telemetry": True,
                "gpu_index": 1,
            },
            {
                "persist_on_startup": False,
                "socket_path": entry.DEFAULT_DAEMON_SOCKET,
            },
        )
    ]


def test_dispatch_cli_persistent_update_reinstalls_even_with_daemon_running(
    monkeypatch,
) -> None:
    # A running daemon must not short-circuit an explicit install: skipping the
    # binary refresh left users on a stale /usr/libexec daemon with
    # success-looking output (issue #30). The boot-profile apply happens inside
    # install_systemd_service, never as a bare apply-through-daemon.
    applied = []
    installed = []
    monkeypatch.setattr(entry.os, "geteuid", lambda: 0)
    monkeypatch.setattr(entry, "daemon_status", lambda **_kwargs: {"state": "idle"})
    monkeypatch.setattr(
        entry,
        "apply_runtime_intent",
        lambda intent, **kwargs: applied.append((intent, kwargs))
        or {"started": True, "runtime_mode": "adaptive"},
    )
    monkeypatch.setattr(
        entry,
        "install_systemd_service",
        lambda program_file, argv, **kwargs: installed.append(
            (program_file, list(argv), kwargs)
        ),
    )
    monkeypatch.setattr(entry, "read_auto_uv_profiles", lambda: [{"profile_id": "p1"}])
    monkeypatch.setattr(
        entry,
        "resolve_profile_tier_profiles",
        lambda profiles: {
            "efficiency": {"profile_id": "p1"},
            "performance": {"profile_id": "p2"},
        },
    )
    monkeypatch.setattr(
        entry,
        "available_adaptive_tiers",
        lambda _resolved: ["efficiency", "performance"],
    )

    exit_code = entry.dispatch_cli(
        program_file="/tmp/penguin_burner.py",
        main_callback=lambda *_args, **_kwargs: None,
        argv=["--install-systemd-service", "--adaptive-auto-uv"],
    )

    assert exit_code == 0
    assert installed[0][1] == ["--adaptive-auto-uv"]
    assert applied == []


def test_dispatch_cli_reexecs_daemon_lifecycle_commands_when_not_root(
    monkeypatch,
) -> None:
    reexeced = []
    monkeypatch.setattr(entry.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        entry,
        "reexec_daemon_lifecycle_with_root",
        lambda argv: reexeced.append(list(argv)) or 7,
    )
    for direct_call in (
        "install_systemd_service",
        "uninstall_systemd_service",
        "migrate_to_daemon_service",
    ):
        monkeypatch.setattr(
            entry,
            direct_call,
            lambda *_args, _name=direct_call, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"{_name} must not run without root")
            ),
        )

    lifecycle_argvs = [
        ["--install-systemd-service", "--adaptive-auto-uv"],
        ["--uninstall-systemd-service"],
        ["--migrate-to-daemon-service"],
    ]
    for argv in lifecycle_argvs:
        exit_code = entry.dispatch_cli(
            program_file="/tmp/penguin_burner.py",
            main_callback=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("main callback must not run")
            ),
            argv=list(argv),
        )
        assert exit_code == 7

    assert reexeced == lifecycle_argvs


def test_dispatch_cli_runs_daemon_lifecycle_directly_as_root(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(entry.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        entry,
        "reexec_daemon_lifecycle_with_root",
        lambda argv: (_ for _ in ()).throw(
            AssertionError("root must not re-elevate")
        ),
    )
    monkeypatch.setattr(
        entry,
        "uninstall_systemd_service",
        lambda **_kwargs: calls.append("uninstall"),
    )
    monkeypatch.setattr(
        entry,
        "migrate_to_daemon_service",
        lambda _program_file, **_kwargs: calls.append("migrate"),
    )

    for argv in (["--uninstall-systemd-service"], ["--migrate-to-daemon-service"]):
        exit_code = entry.dispatch_cli(
            program_file="/tmp/penguin_burner.py",
            main_callback=lambda *_args, **_kwargs: None,
            argv=list(argv),
        )
        assert exit_code == 0

    assert calls == ["uninstall", "migrate"]
