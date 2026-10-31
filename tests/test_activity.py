import json
import time
import unittest
from unittest.mock import patch

from fleetlight import probe
from fleetlight.monitor import OomWatch, crash_issues, issues, presence_summary, pressure_issues


class PresenceSummaryTests(unittest.TestCase):
    def test_stream_wins_over_a_locked_desktop(self):
        text, in_use = presence_summary({"desktop": "Hyprland", "locked": True,
                                         "stream": {"streaming": True, "since": time.time()}})
        self.assertTrue(text.startswith("Streaming to Moonlight since "))
        self.assertTrue(in_use)

    def test_unlocked_hyprland_counts_as_in_use(self):
        self.assertEqual(presence_summary({"desktop": "wayland", "locked": False, "displays_off": False}), ("Desktop unlocked", True))

    def test_idle_locked_and_displays_off(self):
        self.assertEqual(presence_summary({"desktop": "macOS", "locked": True, "idle_seconds": 1751}), ("Locked · idle 29 min", False))
        self.assertEqual(presence_summary({"desktop": "cinnamon", "idle_seconds": 806300}), ("Idle 9 d", False))
        self.assertEqual(presence_summary({"desktop": "Hyprland", "displays_off": True}), ("Displays off", False))
        self.assertEqual(presence_summary({"desktop": "macOS", "idle_seconds": 30}), ("In use", True))

    def test_remote_viewers_and_terminals(self):
        self.assertEqual(presence_summary({"desktop": "macOS", "locked": True, "remote_desktop": [{"kind": "Screen Sharing", "from": "192.0.2.20"}]}),
                         ("Screen Sharing viewer connected from 192.0.2.20", True))
        self.assertEqual(presence_summary({"desktop": None, "terminals": [{"from": "laptop"}]}), ("SSH terminal from laptop", True))
        self.assertEqual(presence_summary({"desktop": None, "remote_sessions": [{"kind": "RDP"}]}),
                         ("Remote desktop session open, no viewer", False))
        self.assertEqual(presence_summary(None), (None, False))


class CrashIssueTests(unittest.TestCase):
    def test_recent_crashes_name_the_latest_time_per_program(self):
        now = 1_800_000_000
        crashes = {"items": [{"at": now - 60, "name": "adb"}, {"at": now - 7200, "name": "adb"},
                             {"at": now - 3 * 86400, "name": "old"}]}
        found = crash_issues(crashes, now=now)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("adb crashed at "))
        # Ageing out the older adb crash does not change the line, so no new notification fires.
        self.assertEqual(crash_issues({"items": crashes["items"][:1]}, now=now), found)

    def test_many_programs_collapse(self):
        now = 1_800_000_000
        items = [{"at": now - index, "name": "p%d" % index} for index in range(5)]
        found = crash_issues({"items": items}, now=now)
        self.assertEqual(found[-1], "2 more programs crashed today")

    def test_oom_watch_only_alerts_on_increases(self):
        watch = OomWatch()
        base = {"id": "a", "status": "online", "crashes": {"since": 1, "oom_kills": 3}}
        self.assertEqual(watch.observe(base, now=100), [])
        self.assertEqual(watch.observe({**base, "crashes": {"since": 1, "oom_kills": 4}}, now=200), [200])
        snapshot = {**base, "crashes": {"since": 1, "oom_kills": 4}, "oom_events": [time.time() - 60]}
        self.assertIn("Out of memory", issues(snapshot)[-1])
        # A new boot starts over.
        self.assertEqual(watch.observe({**base, "crashes": {"since": 2, "oom_kills": 0}}, now=300), [])


class PressureTests(unittest.TestCase):
    def test_thresholds(self):
        quiet = {"some": {"avg60": 1.0, "avg300": 1.0}, "full": {"avg60": 0.0}}
        self.assertEqual(pressure_issues({"cpu": quiet, "memory": quiet, "io": quiet}), [])
        found = pressure_issues({"memory": {"some": {"avg60": 30}, "full": {"avg60": 12.4}},
                                 "cpu": {"some": {"avg300": 85}}})
        self.assertEqual(found, ["Memory is thrashing: tasks stalled 12% of the last minute",
                                 "CPU is saturated: tasks waited 85% of the last 5 minutes"])
        self.assertEqual(pressure_issues({"memory_level": "critical"}), ["Memory pressure is critical"])
        self.assertEqual(pressure_issues({"memory_level": "warning"}), [])

    def test_probe_parses_psi_files(self):
        files = {"/proc/pressure/cpu": "some avg10=1.50 avg60=2.25 avg300=0.10 total=5\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
                 "/proc/pressure/memory": "some avg10=0.00 avg60=0.40 avg300=0.20 total=9\nfull avg10=0.00 avg60=0.30 avg300=0.10 total=8\n"}
        with patch.object(probe, "text", side_effect=lambda path: files.get(path, "")):
            found = probe.pressure("Linux")
        self.assertEqual(found["cpu"]["some"], {"avg10": 1.5, "avg60": 2.25, "avg300": 0.1})
        self.assertEqual(found["memory"]["full"]["avg60"], 0.3)
        self.assertNotIn("io", found)


class ProbeParsingTests(unittest.TestCase):
    def test_sunshine_log_last_event_wins(self):
        log = ("[2026-10-31 08:20:22.466]: Info: CLIENT CONNECTED\n"
               "[2026-10-31 08:24:23.590]: Info: CLIENT DISCONNECTED\n"
               "[2026-10-31 11:51:14.736]: Info: CLIENT CONNECTED\n")
        with patch.object(probe, "running_programs", return_value={"sunshine"}), \
                patch.object(probe, "log_tail", side_effect=lambda path, size=0: log if path.endswith("/.config/sunshine/sunshine.log") else ""):
            found = probe.sunshine_stream("Linux")
        self.assertTrue(found["streaming"])
        self.assertEqual(time.strftime("%H:%M", time.localtime(found["since"])), "11:51")
        with patch.object(probe, "running_programs", return_value=set()):
            self.assertIsNone(probe.sunshine_stream("Linux"))

    def test_who_keeps_only_remote_terminals(self):
        who = ("alex     tty1         2026-10-31 16:49\n"
               "alex     pts/0        2026-10-31 17:02 (192.0.2.15)\n"
               "alex     pts/1        2026-10-31 17:03 (:0)\n")
        with patch.object(probe, "command", return_value=who):
            self.assertEqual(probe.remote_terminals(), [{"user": "alex", "tty": "pts/0", "from": "192.0.2.15"}])

    def test_proc_net_tcp_address(self):
        self.assertEqual(probe.hex_address("0100007F:1F90"), "127.0.0.1")
        self.assertEqual(probe.hex_address("0000000000000000FFFF00000100007F:1F90"), "127.0.0.1")

    def test_coredumps_since_boot(self):
        rows = [{"time": 1_793_000_000_000_000, "pid": 1, "sig": 11, "exe": "/usr/bin/light-locker"}]
        with patch.object(probe, "command", return_value=json.dumps(rows)), \
                patch.object(probe.shutil, "which", return_value="/usr/bin/coredumpctl"), \
                patch.object(probe, "text", return_value="oom_kill 2\n"):
            found = probe.crashes("Linux", 1_792_000_000)
        self.assertEqual(found["oom_kills"], 2)
        self.assertEqual(found["items"], [{"at": 1_793_000_000.0, "name": "light-locker", "signal": 11}])


if __name__ == "__main__":
    unittest.main()
