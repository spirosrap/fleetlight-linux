import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fleetlight import config
from fleetlight.actions import terminal_command
from fleetlight.monitor import History, classify_error, issues, probe_host, run_process
from fleetlight import probe


class ConfigurationTests(unittest.TestCase):
    def test_defaults_are_local_only(self):
        value = config.validate(config.default_config())
        self.assertEqual(len(value["hosts"]), 1)
        self.assertTrue(value["hosts"][0]["local"])
        self.assertNotIn("alias", value["hosts"][0])

    def test_no_shell_options_or_commands(self):
        for alias in ("-oProxyCommand=anything", "server; id", "$(whoami)", "server\nother"):
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                config.validate({"version": 1, "hosts": [{"id": "server", "name": "Server", "alias": alias}]})

    def test_no_service_options(self):
        for service in ("--user", "docker; true", "../other"):
            value = config.default_config()
            value["hosts"][0]["services"] = [service]
            with self.assertRaises(ValueError):
                config.validate(value)

    def test_duplicates_rejected(self):
        value = config.default_config()
        value["hosts"] *= 2
        with self.assertRaises(ValueError):
            config.validate(value)

    def test_atomic_roundtrip_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fleet.json"
            config.atomic_json(path, config.default_config())
            self.assertEqual(config.load(path), config.default_config())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_corrupt_config_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fleet.json"
            path.write_text("broken")
            with self.assertRaises(ValueError):
                config.load(path)
            self.assertEqual(path.read_text(), "broken")

    def test_interval_bounds(self):
        for value in (0, 1, 3601, "60", True):
            configuration = config.default_config()
            configuration["refresh_seconds"] = value
            with self.assertRaises(ValueError):
                config.validate(configuration)


class CollectorTests(unittest.TestCase):
    def test_actual_local_receipt(self):
        result = probe_host({"id": "local", "name": "Local", "local": True})
        self.assertEqual(result["status"], "online", result)
        self.assertGreaterEqual(result["disk_percent"], 0)
        self.assertLessEqual(result["disk_percent"], 100)
        self.assertIn("hostname", result)

    def test_spoofed_or_missing_receipt_rejected(self):
        for output in ('hello', 'FLEETLIGHT_V1={"schema":1,"nonce":"wrong"}'):
            with patch("fleetlight.monitor.run_process", return_value=(0, output, "")):
                result = probe_host({"id": "test", "name": "Test", "alias": "server"})
                self.assertNotEqual(result["status"], "online")

    def test_ssh_stays_strict(self):
        with patch("fleetlight.monitor.run_process", return_value=(255, "", "Permission denied")) as run:
            result = probe_host({"id": "server", "name": "Server", "alias": "user@example.org"})
            args = run.call_args.args[0]
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("BatchMode=yes", args)
            self.assertEqual(args[args.index("--") + 1], "user@example.org")
            self.assertEqual(result["status"], "access")

    def test_timeout_is_bounded(self):
        import sys
        with self.assertRaises(TimeoutError):
            run_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05)

    def test_unknown_host_key_is_access_issue(self):
        self.assertEqual(classify_error("Host key verification failed")[1], "access")

    def test_modified_app_metadata_still_reports_version(self):
        metadata = '{"version":"1.2.3","codexAppBrand":"chatgpt"}'
        def command(args, timeout=4):
            return {("pacman", "-Q", "openai-codex-desktop"): "openai-codex-desktop 1.2.3-1",
                    ("pacman", "-Si", "openai-codex-desktop"): "Version : 1.2.3-1",
                    ("vercmp", "1.2.3-1", "1.2.3-1"): "0"}.get(tuple(args), "")
        with patch("fleetlight.probe.shutil.which", return_value="/usr/bin/pacman"), \
                patch("fleetlight.probe.command", side_effect=command), patch("fleetlight.probe.text", return_value=metadata):
            app = probe.desktop_app("Linux")
            self.assertEqual(app["version"], "1.2.3")
            self.assertEqual(app["status"], "current in cached repository")


class HistoryAndActionTests(unittest.TestCase):
    def test_service_failure_and_recovery_events(self):
        with tempfile.TemporaryDirectory() as directory:
            history = History(Path(directory) / "history.json")
            good = {"status": "online", "services": {"docker": "active"}}
            bad = {"status": "online", "services": {"docker": "failed"}}
            history.record({"server": bad}, {"server": good})
            history.record({"server": good}, {"server": bad})
            reloaded = History(history.path)
            self.assertEqual(len(reloaded.events), 2)
            self.assertIn("healthy", reloaded.events[-1]["message"])

    def test_attention_is_not_just_connectivity(self):
        self.assertTrue(issues({"status": "online", "disk_percent": 95}))
        self.assertFalse(issues({"status": "online", "services": {"docker": "active"}}))

    def test_fixed_update_command_requires_known_manager(self):
        host = {"id": "server", "name": "Server", "alias": "server"}
        command = terminal_command(host, "pacman")
        self.assertEqual(command[command.index("--") + 1], "server")
        self.assertTrue(command[-1].startswith("sudo pacman -Syu"))
        self.assertNotIn("--noconfirm", command[-1])
        with self.assertRaises(ValueError):
            terminal_command(host, "arbitrary")

    def test_corrupt_history_does_not_break_live_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text('{"events":null}')
            self.assertEqual(History(path).events, [])


if __name__ == "__main__":
    unittest.main()
