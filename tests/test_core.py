import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fleetlight import config
from fleetlight.actions import terminal_command
from fleetlight.monitor import History, classify_error, issues, probe_host, run_process, tailscale_issues
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

    def test_notifications_default_on(self):
        self.assertTrue(config.notifications_enabled(config.default_config()))
        self.assertTrue(config.notifications_enabled({"version": 1, "hosts": []}))
        value = config.default_config()
        value["notifications"] = False
        self.assertFalse(config.notifications_enabled(config.validate(value)))
        value["notifications"] = "no"
        with self.assertRaises(ValueError):
            config.validate(value)

    def test_claude_billing_day_is_optional_and_a_day_of_the_month(self):
        self.assertIsNone(config.claude_billing_day(config.default_config()))
        value = config.default_config()
        value["claude_billing_day"] = 18
        self.assertEqual(config.claude_billing_day(config.validate(value)), 18)
        value["claude_billing_day"] = None
        self.assertIsNone(config.claude_billing_day(config.validate(value)))
        for bad in (0, 32, "18", 18.0, True):
            value["claude_billing_day"] = bad
            with self.assertRaises(ValueError):
                config.validate(value)

    def test_auto_updates_default_off(self):
        self.assertFalse(config.auto_updates_enabled(config.default_config()))
        self.assertFalse(config.auto_updates_enabled({"version": 1, "hosts": []}))
        value = config.default_config()
        value["auto_updates"] = True
        self.assertTrue(config.auto_updates_enabled(config.validate(value)))
        value["auto_updates"] = "yes"
        with self.assertRaises(ValueError):
            config.validate(value)


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
            remote = args[-1]
            self.assertTrue(remote.startswith("PATH=/opt/homebrew/bin:"))
            self.assertIn("python3 -c", remote)
            self.assertEqual(result["status"], "access")

    def test_xcode_license_is_unsupported_python(self):
        detail, status = classify_error(
            "You have not agreed to the Xcode license agreements. Please run 'sudo xcodebuild -license'")
        self.assertEqual(status, "unsupported")
        self.assertIn("Xcode license", detail)

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

    def test_grokbot_reports_pacman_versions(self):
        def command(args, timeout=4):
            return {("pacman", "-Q", "grok-bot"): "grok-bot 0.75.8-1",
                    ("pacman", "-Si", "grok-bot"): "Repository : omarchy\nVersion : 0.76.0-1",
                    ("vercmp", "0.75.8-1", "0.76.0-1"): "-1"}.get(tuple(args), "")
        with patch("fleetlight.probe.shutil.which", return_value="/usr/bin/pacman"), \
                patch("fleetlight.probe.command", side_effect=command):
            app = probe.grokbot("Linux")
            self.assertEqual(app, {"version": "0.75.8", "provider": "pacman", "candidate": "0.76.0", "status": "update available"})
            self.assertEqual(probe.grokbot("Darwin")["status"], "not detected")
        with patch("fleetlight.probe.shutil.which", return_value="/usr/bin/pacman"), \
                patch("fleetlight.probe.command", return_value=""):
            self.assertIsNone(probe.grokbot("Linux")["version"])


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

    def test_tailscale_key_expiry_is_an_issue_within_a_week(self):
        now = 1_760_000_000
        soon = {"backend_state": "Running", "key_expiry": "2025-10-11T08:53:20Z"}  # now + 2 days
        self.assertEqual(tailscale_issues(soon, now=now), ["Tailscale login expires in 2 days"])
        self.assertEqual(tailscale_issues({"key_expiry": "2025-10-09T20:00:00Z"}, now=now), ["Tailscale login expires today"])
        self.assertEqual(tailscale_issues({"key_expiry": "2025-10-01T00:00:00Z"}, now=now), ["Tailscale login has expired"])
        self.assertEqual(tailscale_issues({"key_expiry": "2026-01-01T00:00:00Z"}, now=now), [])
        self.assertEqual(tailscale_issues({"key_expiry": None}, now=now), [])
        self.assertEqual(tailscale_issues({"backend_state": "NeedsLogin"}, now=now), ["Tailscale is logged out"])
        self.assertEqual(tailscale_issues(None), [])
        snapshot = {"status": "online", "services": {}, "tailscale": soon, "checked_at": now}
        with patch("fleetlight.monitor.time.time", return_value=now):
            self.assertIn("Tailscale login expires in 2 days", issues(snapshot))

    def test_tailscale_probe_ignores_disabled_expiry(self):
        payload = json.dumps({"BackendState": "Running", "Version": "1.104.1-t1234", "Self": {"KeyExpiry": "0001-01-01T00:00:00Z"}})
        with patch("fleetlight.probe.command", return_value=payload), patch("fleetlight.probe.shutil.which", return_value="/usr/bin/tailscale"), \
                patch("fleetlight.probe.os.path.exists", return_value=True):
            self.assertEqual(probe.tailscale("Linux"), {"backend_state": "Running", "version": "1.104.1", "key_expiry": None})
        with patch("fleetlight.probe.command", return_value=""), patch("fleetlight.probe.shutil.which", return_value="/usr/bin/tailscale"), \
                patch("fleetlight.probe.os.path.exists", return_value=True):
            self.assertIsNone(probe.tailscale("Linux"))
        with patch("fleetlight.probe.shutil.which", return_value=None), patch("fleetlight.probe.os.path.exists", return_value=False):
            self.assertIsNone(probe.tailscale("Linux"))

    def test_fixed_update_command_requires_known_manager(self):
        host = {"id": "server", "name": "Server", "alias": "server"}
        command = terminal_command(host, "pacman")
        self.assertEqual(command[command.index("--") + 1], "server")
        self.assertTrue(command[-1].startswith("sudo env OMARCHY_ALLOW_DIRECT_PACMAN=1 pacman -Syu"))
        self.assertNotIn("--noconfirm", command[-1])
        omarchy = terminal_command(host, "omarchy")
        self.assertTrue(omarchy[-1].startswith("omarchy update"))
        self.assertNotIn("-y", omarchy[-1])
        with self.assertRaises(ValueError):
            terminal_command(host, "arbitrary")

    def test_corrupt_history_does_not_break_live_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_text('{"events":null}')
            self.assertEqual(History(path).events, [])


if __name__ == "__main__":
    unittest.main()


class DistributionNameTest(unittest.TestCase):
    def test_omarchy_gets_its_version_from_version_id(self):
        release = 'NAME="Omarchy"\nPRETTY_NAME="Omarchy"\nID=omarchy\nBUILD_ID="4.5.0rc2"\nVERSION_ID="4.5.0rc2"\n'
        self.assertEqual(probe.distribution_name(release), "Omarchy 4.5.0rc2")

    def test_versioned_pretty_names_are_kept(self):
        release = 'PRETTY_NAME="Ubuntu 24.04.3 LTS"\nVERSION_ID="24.04"\n'
        self.assertEqual(probe.distribution_name(release), "Ubuntu 24.04.3 LTS")

    def test_rolling_releases_without_a_version(self):
        self.assertEqual(probe.distribution_name('PRETTY_NAME="Arch Linux"\nID=arch\n'), "Arch Linux")
        self.assertEqual(probe.distribution_name(""), "")


    def test_mise_command_wrapper_counts_as_mise_codex(self):
        wrapper = "/home/u/.local/share/mise/command-wrappers/bin/codex"
        with patch.object(probe, "codex_executable", return_value=wrapper), \
                patch.object(probe.os.path, "realpath", return_value="/usr/bin/mise"):
            self.assertEqual(probe.codex_installation(), {"method": "mise"})

    def test_codex_version_leaves_path_for_the_claude_check(self):
        with patch.dict(probe.os.environ, {"PATH": "/usr/bin"}), \
                patch.object(probe, "codex_executable", return_value="/x/codex"), \
                patch.object(probe, "command", return_value="codex-cli 1.2.3"):
            self.assertEqual(probe.codex_version(), "1.2.3")
            self.assertEqual(probe.os.environ["PATH"], "/usr/bin")
