"""Regression checks that also run with the standard-library unittest runner."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from integrations.heroic import config_store, process
from integrations.heroic.library_source import HeroicLibrarySource
from integrations.heroic.manager import HeroicIntegrationManager
from integrations.launchers import host_process
from integrations.launchers.game_settings import GameSettingsError
from integrations.launchers.wrapper_manager import WrapperManager
from overlay.wrapper_tokens import GAME_KEY_ENV


class HeroicConfigRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / ".config" / "heroic"
        (self.root / "GamesConfig").mkdir(parents=True)
        self.global_path = self.root / "config.json"
        self.global_path.write_text(json.dumps({
            "defaultSettings": {"wrapperOptions": [{"exe": "gamemoderun", "args": ""}]}
        }))
        self.game_path = self.root / "GamesConfig" / "Turkey.json"
        self.settings_path = self.home / "settings.json"
        self.enterContext(patch.object(WrapperManager, "_ensure_wrapper_installed", return_value=""))

    def manager(self, wrappers):
        settings = {} if wrappers is None else {"wrapperOptions": wrappers}
        self.game_path.write_text(json.dumps({"Turkey": settings, "version": "v0"}, indent=2))
        manager = HeroicIntegrationManager(home=self.home, settings_path=self.settings_path)
        self.enterContext(patch.object(manager, "read_games", return_value=(
            SimpleNamespace(game_id="Turkey", display_name="Test game"),
        )))
        manager.refresh()
        return manager

    def test_explicit_empty_override_survives_toggles_refresh_and_option_changes(self):
        manager = self.manager([])
        original = self.game_path.read_bytes()
        self.assertEqual(manager.row("Turkey").command, "")
        self.assertFalse(manager.row("Turkey").inherited)
        for _ in range(2):
            self.assertTrue(manager.set_game_enabled("Turkey", True).ok)
            self.assertNotIn("gamemoderun", manager.row("Turkey").command)
            self.assertTrue(manager.set_game_overlay("Turkey", True).ok)
            manager.refresh()
            self.assertTrue(manager.set_game_enabled("Turkey", False).ok)
            self.assertEqual(self.game_path.read_bytes(), original)
            manager.refresh()

    def test_missing_override_resumes_updated_globals(self):
        manager = self.manager(None)
        self.assertTrue(manager.set_game_enabled("Turkey", True).ok)
        self.global_path.write_text(json.dumps({
            "defaultSettings": {"wrapperOptions": [{"exe": "gamescope", "args": "--"}]}
        }))
        manager.refresh()
        self.assertTrue(manager.set_game_enabled("Turkey", False).ok)
        self.assertIsNone(config_store.read_game_entries("Turkey", self.home))
        self.assertEqual(manager.row("Turkey").command, "gamescope --")

    def test_setting_a_disabled_game_does_not_create_an_empty_override(self):
        manager = self.manager(None)
        self.assertTrue(manager.set_game_overlay("Turkey", True).ok)
        self.assertIsNone(config_store.read_game_entries("Turkey", self.home))

    def test_clearing_the_command_field_resumes_inheritance(self):
        manager = self.manager([])
        self.assertTrue(manager.set_game_command("Turkey", "").ok)
        self.assertIsNone(config_store.read_game_entries("Turkey", self.home))
        self.assertEqual(manager.row("Turkey").command, "gamemoderun")

    def test_external_empty_override_stays_explicit_after_disabling(self):
        manager = self.manager(None)
        self.assertTrue(manager.set_game_enabled("Turkey", True).ok)
        self.game_path.write_text(json.dumps({"Turkey": {"wrapperOptions": []}, "version": "v0"}))
        self.assertTrue(manager.set_game_enabled("Turkey", False).ok)
        self.assertTrue(manager.set_game_overlay("Turkey", True).ok)
        self.assertEqual(config_store.read_game_entries("Turkey", self.home), [])

    def test_bad_quoting_preserves_both_config_and_saved_preferences(self):
        manager = self.manager([{"exe": "gamescope", "args": "--"}])
        self.assertTrue(manager.set_game_enabled("Turkey", True).ok)
        before = self.game_path.read_bytes(), self.settings_path.read_bytes()
        for text in ('gamescope "unclosed', "PENGUIN_BURNER --pb-overlay=1 'bad", "gamescope \\"):
            with self.subTest(command=text):
                result = manager.set_game_command("Turkey", text)
                self.assertFalse(result.ok)
                self.assertIn("Invalid wrapper command", result.message)
                self.assertEqual((self.game_path.read_bytes(), self.settings_path.read_bytes()), before)

    def test_readback_distinguishes_empty_override_from_removed_key(self):
        self.manager([])
        def overwritten(path, text, **kwargs):
            document = json.loads(text)
            document["Turkey"].pop("wrapperOptions", None)
            path.write_text(json.dumps(document))
        with patch.object(config_store, "atomic_write_text", side_effect=overwritten):
            result = config_store.write_wrapper_command("Turkey", "", self.home)
        self.assertFalse(result.ok)
        self.assertIn("overwrote", result.message)

    def test_save_failure_reports_the_command_that_already_landed(self):
        for raw in (True, False):
            with self.subTest(raw=raw):
                manager = self.manager([])
                with patch.object(manager._store, "store", side_effect=GameSettingsError("disk full")):
                    result = (manager.set_game_command("Turkey", "gamemoderun") if raw
                              else manager.set_game_enabled("Turkey", True))
                self.assertFalse(result.ok)
                self.assertIn("launch command written", result.message)
                self.assertIn("disk full", result.message)
                self.assertEqual(result.command, config_store.effective_wrapper_command("Turkey", self.home).value)

    def test_preserved_settings_warning_survives_both_write_paths(self):
        for raw in (True, False):
            with self.subTest(raw=raw):
                manager = self.manager([])
                self.settings_path.write_text("{broken")
                result = (manager.set_game_command("Turkey", "gamemoderun") if raw
                          else manager.set_game_enabled("Turkey", True))
                self.assertTrue(result.ok)
                self.assertIn("was kept as", result.message)
                self.assertTrue(any(self.home.glob("settings.json.corrupt-*")))


class HeroicSessionRegressions(unittest.TestCase):
    def test_permission_denied_is_reported_by_the_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "41").mkdir()
            output = io.StringIO()
            with patch.object(sys, "argv", ["probe", tmp, GAME_KEY_ENV]), \
                 patch.object(Path, "read_bytes", side_effect=PermissionError), \
                 redirect_stdout(output):
                exec(process._SESSION_PROBE, {})  # noqa: S102 - execute our fixed probe with mocked proc access
            self.assertEqual(json.loads(output.getvalue()), {"sessions": [], "external": [], "unreadable": [41]})

    def test_daemon_watches_recover_inaccessible_sessions_without_reviving_exits(self):
        probe = subprocess.CompletedProcess([], 0, json.dumps({
            "sessions": [[42, "heroic:Readable"]], "unreadable": [41, 43],
        }))
        watches = {"game_runtime": {"watched": [
            {"pid": 41, "app_id": "heroic:Protected"},
            {"pid": 43, "app_id": "lutris:27"},
            {"pid": 44, "app_id": "heroic:ExitedDuringGrace"},
        ]}}
        with patch.object(process, "run_on_host", return_value=probe), \
             patch.object(process, "daemon_status", return_value=watches):
            sessions = process.probe_heroic_sessions(known_pids=(41,))
            assert sessions is not None
            self.assertEqual(sessions.wrapped, {
                "Readable": (42,), "Protected": (41,),
            })

    def test_unreadable_known_session_is_unknown_until_access_recovers_or_it_exits(self):
        source = HeroicLibrarySource()
        def answer(sessions=(), unreadable=()):
            return subprocess.CompletedProcess([], 0, json.dumps({
                "sessions": sessions, "unreadable": unreadable,
            }))
        results = [
            answer([(41, "heroic:Turkey")]),
            answer(unreadable=[41]),
            answer(unreadable=[41]),  # Stop must not signal an unconfirmed PID
            answer([(41, "heroic:Turkey")]),
            answer(unreadable=[999]),  # unrelated denied process cannot hide exit
        ]
        with patch.object(process, "run_on_host", side_effect=results), \
             patch.object(process, "daemon_status", side_effect=RuntimeError("unavailable")), \
             patch("integrations.heroic.library_source.stop_heroic_game") as stop:
            self.assertEqual(source.running_game_ids(), frozenset({"Turkey"}))
            self.assertIsNone(source.running_game_ids())
            self.assertFalse(source.stop("Turkey")[0])
            stop.assert_not_called()
            self.assertEqual(source.running_game_ids(), frozenset({"Turkey"}))
            self.assertEqual(source.running_game_ids(), frozenset())

    def test_missing_watch_does_not_turn_an_unreadable_known_session_into_an_exit(self):
        probe = subprocess.CompletedProcess([], 0, json.dumps({
            "sessions": [], "unreadable": [41],
        }))
        with patch.object(process, "run_on_host", return_value=probe), \
             patch.object(process, "daemon_status", return_value={}):
            self.assertIsNone(process.probe_heroic_sessions(known_pids=(41,)))

    def test_probe_reads_exec_environment_and_excludes_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            for pid, session, key in (
                (41, 41, "heroic:Sid Meier"),
                (42, 41, "heroic:Sid Meier"),  # inherited by a helper
                (43, 43, "lutris:27"),
                (44, 44, "heroic:Sid Meier"),  # separate session of same game
                (45, 45, ""),
            ):
                directory = proc / str(pid)
                directory.mkdir()
                (directory / "environ").write_bytes(
                    f"{GAME_KEY_ENV}={key}\0PENGUIN_BURNER_TELEMETRY_SESSION={session}\0".encode()
                )
                (directory / "cmdline").write_bytes(b"/bin/sleep\0")
            (proc / "46").mkdir()  # process disappeared during the scan
            def run_probe(command, **kwargs):
                command[-2] = str(proc)
                return subprocess.run(command, capture_output=True, text=True, check=False)
            with patch.object(process, "run_on_host", side_effect=run_probe):
                sessions = process.probe_heroic_sessions()
                assert sessions is not None
                running = sessions.wrapped
                self.assertEqual(set(running), {"Sid Meier"})
                self.assertCountEqual(running["Sid Meier"], (41, 44))

    def test_probe_failure_is_unknown_not_no_sessions(self):
        for result in (None, subprocess.CompletedProcess([], 1, ""),
                       subprocess.CompletedProcess([], 0, "invalid JSON")):
            with self.subTest(result=result), patch.object(process, "run_on_host", return_value=result):
                self.assertIsNone(process.probe_heroic_sessions())

    def test_flatpak_probe_uses_host_python_and_host_proc(self):
        with patch.object(process, "running_in_flatpak", return_value=True), \
             patch.object(host_process, "running_in_flatpak", return_value=True), \
             patch.object(host_process.shutil, "which", return_value="/usr/bin/flatpak-spawn"), \
             patch.dict(os.environ, {"PENGUIN_BURNER_HOST_PYTHON": "/host/python3"}), \
             patch.object(host_process.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 0, '{"sessions": [], "unreadable": []}'
             )) as run:
            sessions = process.probe_heroic_sessions()
            assert sessions is not None
            self.assertEqual(sessions.wrapped, {})
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["/usr/bin/flatpak-spawn", "--host", "--directory=/tmp", "/host/python3", "-c"])
        self.assertEqual(command[-2:], ["/proc", GAME_KEY_ENV])

    @unittest.skipUnless(sys.platform == "linux", "requires Linux /proc")
    def test_real_wrapper_exec_is_detected_and_stopped_without_a_profile(self):
        code = """
from overlay import launcher
launcher.clear_overlay_override = lambda: None
launcher.configure_penguin_burner_environment = lambda *a, **k: False
launcher._prepare_overlay_paths = lambda env: None
launcher._apply_game_profile = lambda env: None
launcher.ingame_latency_enabled = lambda env: False
launcher.main()
"""
        child = subprocess.Popen([
            sys.executable, "-c", code, "--pb-overlay=0",
            "--pb-game-id=heroic:ReviewProbe", "/bin/sleep", "30",
        ])
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if child.pid in (process.probe_heroic_sessions() or process.HeroicSessions()).wrapped.get("ReviewProbe", ()):
                    break
                time.sleep(0.05)
            else:
                self.fail("session was not detected after exec")
            self.assertNotIn(b"--pb-game-id", Path(f"/proc/{child.pid}/cmdline").read_bytes())
            self.assertTrue(process.stop_heroic_game(child.pid))
            child.wait(timeout=5)
            sessions = process.probe_heroic_sessions()
            assert sessions is not None
            self.assertNotIn("ReviewProbe", sessions.wrapped)
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)


class HeroicAvailabilityRegressions(unittest.TestCase):
    def test_native_heroic_does_not_require_flatpak(self):
        with patch.object(process, "host_has_command", side_effect=lambda name: name == "heroic"), \
             patch.object(process, "run_on_host") as probe:
            self.assertTrue(process.heroic_available())
            self.assertEqual(process.launch_command("gog", "123")[:2], ["heroic", "--no-gui"])
            probe.assert_not_called()

    def test_flatpak_without_heroic_cannot_launch(self):
        for result in (None, subprocess.CompletedProcess([], 1)):
            with self.subTest(result=result), \
                 patch.object(process, "host_has_command", side_effect=lambda name: name == "flatpak"), \
                 patch.object(process, "run_on_host", return_value=result) as probe, \
                 patch.object(process, "launch_with_current_settings") as start:
                self.assertFalse(process.heroic_available())
                self.assertFalse(process.launch_heroic_game("gog", "123"))
                probe.assert_called_with(["flatpak", "info", process.FLATPAK_APP_ID])
                start.assert_not_called()

    def test_installed_flatpak_is_checked_again_at_launch(self):
        with patch.object(process, "host_has_command", side_effect=lambda name: name == "flatpak"), \
             patch.object(process, "run_on_host", side_effect=[
                 subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 1),
             ]):
            self.assertTrue(process.heroic_available())
            self.assertIsNone(process.launch_command("gog", "123"))

    def test_installed_flatpak_can_launch(self):
        with patch.object(process, "host_has_command", side_effect=lambda name: name == "flatpak"), \
             patch.object(process, "run_on_host", return_value=subprocess.CompletedProcess([], 0)):
            self.assertTrue(process.heroic_available())
            self.assertEqual(process.launch_command("gog", "123"), [
                "flatpak", "run", process.FLATPAK_APP_ID, "--no-gui",
                "heroic://launch/gog/123?gui=false",
            ])


if __name__ == "__main__":
    unittest.main()
