import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fleetlight import config, probe
from fleetlight.monitor import ServiceWatch, issues, service_label

SUNSHINE = "user:app-dev.lizardbyte.app.Sunshine.service"


def snapshot(services, boot="b1", at=100.0, changes=(), status="online"):
    return {"id": "pop-os", "status": status, "boot_id": boot, "services": dict(services),
            "package_activity": {"at": at, "changes": list(changes), "count": len(changes)}}


class ServiceSpecTests(unittest.TestCase):
    def test_accepted_entries(self):
        for spec in ("sshd", SUNSHINE, "com.spiros.WithingsScaleBar", "port:8766",
                     "http://127.0.0.1:8766/bridge/v2", "https://pop-os.taild9dfaf.ts.net/"):
            self.assertTrue(probe.valid_service(spec), spec)
        for spec in ("port:0", "port:70000", "-x", "a b", "user:", "ftp://x", "http://a,b", "/usr/bin/sshd"):
            self.assertFalse(probe.valid_service(spec), spec)

    def test_config_accepts_the_new_kinds(self):
        config.validate({"version": 1, "hosts": [{"id": "a", "name": "A", "local": True,
                                                   "services": [SUNSHINE, "port:22", "http://localhost/"]}]})
        with self.assertRaises(ValueError):
            config.validate({"version": 1, "hosts": [{"id": "a", "name": "A", "local": True, "services": ["--now"]}]})

    def test_labels(self):
        self.assertEqual(service_label(SUNSHINE), "Sunshine")
        self.assertEqual(service_label("com.spiros.WithingsScaleBar"), "WithingsScaleBar")
        self.assertEqual(service_label("plex-open-web.service"), "plex-open-web")
        self.assertEqual(service_label("port:5055"), "port 5055")
        self.assertEqual(service_label("https://pop-os.example/"), "pop-os.example")

    def test_pacman_transaction_groups_nearby_upgrades(self):
        log = ("[2026-10-20T10:00:00+0200] [ALPM] upgraded old (1-1 -> 2-1)\n"
               "[2026-10-29T21:13:40+0200] [ALPM] upgraded sunshine (2026.914-1 -> 2026.1029-1)\n"
               "[2026-10-29T21:13:48+0200] [ALPM] installed newdep (1.0-1)\n")
        with patch.object(probe, "log_tail", side_effect=lambda path, size=0: log if "pacman" in path else ""):
            activity = probe.package_activity("Linux")
        self.assertEqual(activity["changes"], ["sunshine 2026.914-1 → 2026.1029-1", "newdep (installed)"])
        self.assertEqual(activity["count"], 2)

    def test_dpkg_upgrades(self):
        log = "2026-10-29 20:23:04 upgrade mailcap:all 3.70 3.71\n2026-10-29 20:23:05 status installed mailcap:all 3.71\n"
        with patch.object(probe, "log_tail", side_effect=lambda path, size=0: log if "dpkg" in path else ""):
            self.assertEqual(probe.package_activity("Linux")["changes"], ["mailcap 3.70 → 3.71"])


class ServiceWatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "watch.json"
        self.watch = ServiceWatch(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def test_service_down_after_package_update_names_the_update(self):
        self.assertEqual(self.watch.observe(snapshot({SUNSHINE: "active", "sshd": "active"}), now=1000), {})
        after = snapshot({SUNSHINE: "failed", "sshd": "active"}, at=2000,
                         changes=["linux 6.1 → 6.2", "sunshine 2026.914-1 → 2026.1029-1"])
        regressions = self.watch.observe(after, now=2010)
        self.assertEqual(list(regressions), [SUNSHINE])
        self.assertEqual(regressions[SUNSHINE]["reason"], "packages")
        self.assertEqual(regressions[SUNSHINE]["changes"][0], "sunshine 2026.914-1 → 2026.1029-1")
        message = issues({**after, "service_regressions": regressions})[0]
        self.assertTrue(message.startswith("Sunshine: failed since the package update at "), message)
        self.assertIn("sunshine 2026.914-1 → 2026.1029-1", message)

    def test_restart_and_recovery(self):
        self.watch.observe(snapshot({SUNSHINE: "active"}), now=1000)
        self.assertIn(SUNSHINE, self.watch.observe(snapshot({SUNSHINE: "inactive"}, boot="b2"), now=1100))
        self.assertEqual(ServiceWatch(self.path).observe(snapshot({SUNSHINE: "inactive"}, boot="b2"), now=1200)[SUNSHINE]["reason"], "restart")
        self.assertEqual(self.watch.observe(snapshot({SUNSHINE: "active"}, boot="b2"), now=1300), {})

    def test_slow_start_inside_the_window_still_counts(self):
        self.watch.observe(snapshot({SUNSHINE: "active"}), now=1000)
        self.assertEqual(self.watch.observe(snapshot({SUNSHINE: "active"}, boot="b2"), now=1100), {})
        self.assertIn(SUNSHINE, self.watch.observe(snapshot({SUNSHINE: "failed"}, boot="b2"), now=1500))

    def test_stopping_long_after_the_update_is_not_blamed_on_it(self):
        self.watch.observe(snapshot({SUNSHINE: "active"}), now=1000)
        self.watch.observe(snapshot({SUNSHINE: "active"}, boot="b2"), now=1100)
        self.assertEqual(self.watch.observe(snapshot({SUNSHINE: "failed"}, boot="b2"), now=1100 + 4000), {})
        self.assertEqual(issues(snapshot({SUNSHINE: "failed"})), ["Sunshine: failed"])

    def test_offline_keeps_the_last_regressions(self):
        self.watch.observe(snapshot({SUNSHINE: "active"}), now=1000)
        self.watch.observe(snapshot({SUNSHINE: "failed"}, boot="b2"), now=1100)
        self.assertIn(SUNSHINE, self.watch.observe({"id": "pop-os", "status": "offline"}, now=1200))


if __name__ == "__main__":
    unittest.main()


class RestartScriptTests(unittest.TestCase):
    def test_scripts_quote_the_unit_and_refuse_ports(self):
        from fleetlight import actions
        script = actions.service_restart_script("Linux", SUNSHINE)
        self.assertIn("systemctl --user restart app-dev.lizardbyte.app.Sunshine.service", script)
        self.assertIn("sudo -n systemctl restart plex-open-web.service",
                      actions.service_restart_script("Linux", "plex-open-web.service"))
        self.assertIn('launchctl kickstart -k "gui/$(id -u)/"com.spiros.WithingsScaleBar',
                      actions.service_restart_script("Darwin", "com.spiros.WithingsScaleBar"))
        for system, spec in (("Linux", "port:22"), ("Linux", "https://x/"), ("Darwin", SUNSHINE), ("Linux", "a;b")):
            with self.assertRaises(ValueError):
                actions.service_restart_script(system, spec)

    def test_restart_reports_missing_sudo(self):
        from fleetlight import actions
        host = {"id": "a", "name": "A", "local": True}
        with patch("fleetlight.monitor.run_process", return_value=(3, "sudo: a password is required\n", "")):
            ok, message = actions.restart_service(host, "Linux", "plex-open-web.service")
        self.assertFalse(ok)
        self.assertIn("passwordless sudo", message)
        with patch("fleetlight.monitor.run_process", return_value=(0, "STATE:active\n", "")):
            self.assertEqual(actions.restart_service(host, "Linux", SUNSHINE), (True, "Sunshine restarted and is running"))


class MemoryFilesystemTests(unittest.TestCase):
    def test_only_this_users_runtime_directory_and_shared_tmpfs(self):
        mounts = ("tmpfs /run/user/%d tmpfs rw 0 0\ntmpfs /run/user/4242 tmpfs rw 0 0\n"
                  "tmpfs /tmp tmpfs rw 0 0\n/dev/sda1 /home ext4 rw 0 0\n") % probe.os.getuid()
        Usage = type("Usage", (), {"total": 100, "free": 5, "used": 95})
        with patch.object(probe.shutil, "disk_usage", return_value=Usage()):
            found = probe.memory_filesystems("Linux", mounts)
        self.assertEqual([item["mount"] for item in found], ["/run/user/%d" % probe.os.getuid(), "/tmp"])
        message = issues({"status": "online", "memory_filesystems": found})[0]
        self.assertTrue(message.endswith("is 95% full (memory-backed)"), message)


class MemoryWatchTests(unittest.TestCase):
    GIB = 1024**3

    def feed(self, watch, sizes, step=300, total=16 * 1024**3, name="fseventsd"):
        result = []
        for index, size in enumerate(sizes):
            result = watch.observe({"id": "air", "status": "online", "memory_total": total,
                                    "processes": {"memory": [[name, size, 1]]}}, now=1000 + index * step)
        return result

    def test_steady_growth_is_reported(self):
        from fleetlight.monitor import MemoryWatch
        found = self.feed(MemoryWatch(), [int((1 + 0.3 * i) * self.GIB) for i in range(12)])
        self.assertEqual([item["name"] for item in found], ["fseventsd"])
        message = issues({"status": "online", "memory_runaways": found})[0]
        self.assertTrue(message.startswith("fseventsd memory grew from 1.0 GB to 4.3 GB in 55 min"), message)

    def test_too_short_too_small_or_jumpy_is_not(self):
        from fleetlight.monitor import MemoryWatch
        self.assertEqual(self.feed(MemoryWatch(), [int((1 + 0.5 * i) * self.GIB) for i in range(6)]), [])
        self.assertEqual(self.feed(MemoryWatch(), [int((1 + 0.02 * i) * self.GIB) for i in range(12)]), [])
        jumpy = [self.GIB * (4 if i % 2 else 1) + i * 50 * 1024**2 for i in range(12)]
        self.assertEqual(self.feed(MemoryWatch(), jumpy), [])
        # Large growth on a big machine that stays under 15% of memory is not a runaway either.
        self.assertEqual(self.feed(MemoryWatch(), [int((1 + 0.3 * i) * self.GIB) for i in range(12)], total=64 * self.GIB), [])


class ClaudeLookupTests(unittest.TestCase):
    def test_interactive_shell_path_is_used_when_the_login_shell_misses_it(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "claude"
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            answers = {"-lc": "", "-ic": "zsh noise\n" + str(binary)}
            with patch.object(probe, "command", side_effect=lambda args, timeout=4: answers.get(args[1], "")), \
                    patch.object(probe.shutil, "which", return_value=None):
                self.assertEqual(probe.claude_executable(), str(binary))
