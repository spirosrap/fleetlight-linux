import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import urllib.request

from fleetlight import actions, agents, config, net, probe
from fleetlight.monitor import History


class HistoryTests(unittest.TestCase):
    def test_old_samples_are_averaged_and_week_old_ones_dropped(self):
        now = 1_000_000_000.0
        rows = [[now - age, 1, 50, 40, 10, 1.0, None, 300] for age in range(0, 3 * 3600, 60)]
        rows += [[now - 8 * 86400, 1, 50, 40, 10, 1.0, None, 300]]
        kept = History.compact(rows, now)
        recent = [row for row in kept if now - row[0] <= History.RAW]
        older = [row for row in kept if now - row[0] > History.RAW]
        self.assertEqual(len(recent), 121)
        self.assertTrue(10 <= len(older) <= 13, len(older))
        self.assertTrue(all(now - row[0] <= History.WEEK for row in kept))
        self.assertEqual(older[0][2:5], [50, 40, 10])
        self.assertIsNone(older[0][6])
        # Compacting again changes nothing: averaged rows stay inside their own step.
        self.assertEqual(History.compact(kept, now), kept)

    def test_outage_lowers_availability_only_where_it_happened(self):
        with tempfile.TemporaryDirectory() as directory:
            history = History(Path(directory) / "history.json")
            now = time.time()
            history.series["server"] = [[now - 3500, 1, 10, 10, 5, 0.1, None, 200], [now - 1700, 0, None, None, None, None, None, None],
                                        [now - 1600, 0, None, None, None, None, None, None], [now - 100, 1, 10, 10, 5, 0.1, None, 200]]
            self.assertEqual(history.availability("server", now - 3600, now, 2), [1.0, 1 / 3])
            self.assertEqual(history.availability("server", now - 7200, now - 3600, 2), [None, None])
            self.assertEqual([value for _, value in history.points("server", "cpu", now - 2000)], [None, None, 5])

    def test_version_one_file_is_migrated_and_saves_are_spaced_out(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            now = time.time()
            path.write_text(json.dumps({"samples": [{"time": now - 60, "host": "server", "status": "online", "disk": 41, "memory": 22},
                                                    {"time": now - 30, "host": "server", "status": "offline", "disk": None, "memory": None}],
                                        "events": [{"time": now - 30, "host": "server", "message": "SSH connection unavailable"}]}))
            history = History(path)
            self.assertEqual([row[:4] for row in history.series["server"]], [[now - 60, 1, 41, 22], [now - 30, 0, None, None]])
            good = {"status": "online", "disk_percent": 42, "memory_percent": 23, "cpu_percent": 7, "load": 0.2, "cpus": 4, "check_ms": 310}
            history.record({"server": good}, {"server": {"status": "offline"}})
            first_write = path.read_text()
            saved = json.loads(first_write)
            self.assertEqual((saved["version"], saved["samples"], len(saved["events"])), (2, [], 2))
            self.assertEqual(saved["series"]["server"][-1][1:5], [1, 42, 23, 7])
            # A quiet check right afterwards stays in memory until the next periodic save.
            history.record({"server": good}, {"server": good})
            self.assertEqual(path.read_text(), first_write)
            history.save()
            self.assertEqual(len(History(path).series["server"]), 4)

    def test_load_stands_in_for_cpu_on_older_probes(self):
        history = History(Path("/nonexistent/fleetlight-test/history.json"))
        history.sample("mac", {"status": "online", "load": 2.0, "cpus": 8}, 100.0)
        history.sample("mac", {"status": "offline"}, 160.0)
        self.assertEqual([row[4] for row in history.series["mac"]], [25, None])


class ProbeTests(unittest.TestCase):
    def test_cpu_usage_from_two_readings(self):
        self.assertEqual(probe.cpu_usage((1000, 800), (1100, 850)), 50)
        self.assertEqual(probe.cpu_usage((1000, 800), (1100, 900)), 0)
        self.assertIsNone(probe.cpu_usage(None, (1100, 900)))
        self.assertIsNone(probe.cpu_usage((1100, 900), (1100, 900)))

    def test_live_metrics_report_cpu_after_the_first_reading(self):
        probe.collect_metrics("Linux")
        time.sleep(0.05)
        metrics = probe.collect_metrics("Linux")
        self.assertTrue(0 <= metrics["cpu_percent"] <= 100)
        self.assertGreater(metrics["memory_total"], metrics["memory_used"])
        self.assertGreater(metrics["disk_total"], metrics["disk_free"])

    def test_disks_keep_one_mount_per_device_and_skip_system_mounts(self):
        mounts = ("/dev/nvme0n1p2 / btrfs rw 0 0\n/dev/nvme0n1p2 /home btrfs rw 0 0\n/dev/nvme0n1p1 /boot vfat rw 0 0\n"
                  "tmpfs /tmp tmpfs rw 0 0\n/dev/sda1 /srv/media\\040library ext4 rw 0 0\n/dev/loop3 /snap/core/1 squashfs ro 0 0\n"
                  "/dev/sdb1 /bootleg ext4 rw 0 0\n")
        usage = type("Usage", (), {"total": 8 * 1024**3, "used": 2 * 1024**3, "free": 6 * 1024**3})()
        with patch("fleetlight.probe.shutil.disk_usage", return_value=usage) as measured:
            found = probe.disks("Linux", mounts)
        self.assertEqual([item["mount"] for item in found], ["/", "/srv/media library", "/bootleg"])
        self.assertEqual(found[0]["percent"], 25)
        self.assertEqual(measured.call_count, 3)

    def test_battery_reads_first_battery_only(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(probe.battery("Linux", root))
            supply = Path(root, "class/power_supply/BAT0")
            supply.mkdir(parents=True)
            (supply / "capacity").write_text("87\n")
            (supply / "status").write_text("Discharging\n")
            self.assertEqual(probe.battery("Linux", root), {"percent": 87, "state": "discharging"})
            (supply / "capacity").write_text("unknown")
            self.assertIsNone(probe.battery("Linux", root))

    def test_top_processes_group_by_name(self):
        before = {"1": ("worker", 100, 10), "2": ("worker", 50, 10), "3": ("idle", 5, 1)}
        after = {"1": ("worker", 150, 10), "2": ("worker", 75, 10), "3": ("idle", 5, 1), "4": ("new", 400, 0)}
        with patch("fleetlight.probe.process_times", return_value=after), \
                patch("fleetlight.probe.os.sysconf", side_effect=lambda name: {"SC_CLK_TCK": 100, "SC_PAGE_SIZE": 4096}[name]):
            result = probe.top_processes("Linux", before, 0.5)
        self.assertEqual(result["cpu"], [["worker", 150]])
        self.assertEqual(result["memory"], [["worker", 20 * 4096, 2], ["idle", 4096, 1]])
        self.assertIsNone(probe.top_processes("Windows"))

    def test_full_receipt_carries_the_new_facts(self):
        result = probe.collect([])
        self.assertTrue(result["disks"] and result["disks"][0]["mount"] == "/")
        self.assertIn("memory", result["processes"])
        self.assertTrue(result["failed_units"] is None or isinstance(result["failed_units"], list))
        self.assertTrue(result["cpu_percent"] is None or 0 <= result["cpu_percent"] <= 100)


class WakeAndSettingsTests(unittest.TestCase):
    def test_magic_packet_layout(self):
        packet = actions.magic_packet("02:00:5e:10:00:ff")
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:12], bytes.fromhex("02005e1000ff"))
        self.assertEqual(packet[-6:], bytes.fromhex("02005e1000ff"))
        for bad in (None, "", "02:00:5e:10:00", "02-00-5e-10-00-ff", "server; id"):
            with self.subTest(mac=bad), self.assertRaises(ValueError):
                actions.magic_packet(bad)

    def test_wake_only_sends_to_broadcast_addresses(self):
        sent = []

        class Socket:
            def __init__(self, *_):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def setsockopt(self, *_):
                pass

            def sendto(self, packet, address):
                sent.append(address)

        with patch("fleetlight.actions.socket.socket", Socket):
            actions.wake("02:00:5e:10:00:ff", "203.0.113.255")
            actions.wake("02:00:5e:10:00:ff", "not an address")
        self.assertEqual({address[0] for address in sent[:4]}, {"255.255.255.255", "203.0.113.255"})
        self.assertEqual({address[0] for address in sent[4:]}, {"255.255.255.255"})
        self.assertEqual({address[1] for address in sent}, {7, 9})

    def test_appearance_is_validated(self):
        self.assertEqual(config.appearance(config.default_config()), "auto")
        value = config.default_config()
        value["appearance"] = "light"
        self.assertEqual(config.appearance(config.validate(value)), "light")
        value["appearance"] = "sepia"
        self.assertEqual(config.appearance(value), "auto")
        with self.assertRaises(ValueError):
            config.validate(value)

    def test_quota_windows_carry_their_reset_time(self):
        reset = time.time() + 7200
        windows = agents.summarize_codex({"primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": reset}})
        self.assertEqual(windows[0]["reset_at"], reset)
        self.assertEqual(agents.epoch(str(int(reset * 1000))), int(reset * 1000) / 1000)
        self.assertIsNone(agents.epoch("soon"))
        demo = agents.demo_usage()
        self.assertEqual([item["label"] for item in demo["claude"]["windows"]], ["5h", "weekly"])
        self.assertEqual(demo["claude"]["remaining_percent"], 72)


class NetworkTests(unittest.TestCase):
    def test_every_https_request_shares_one_tls_context(self):
        shared = net.context()
        self.assertIs(net.context(), shared)
        self.assertTrue(shared.check_hostname)

        def contexts(opener):
            return [handler._context for handler in opener.handlers if isinstance(handler, urllib.request.HTTPSHandler)]

        self.assertEqual(contexts(net.opener()), [shared])
        self.assertEqual(contexts(net.opener(urllib.request.HTTPRedirectHandler)), [shared])
        # Importing the quota module installs the shared opener for plain urlopen calls.
        self.assertEqual(contexts(urllib.request._opener), [shared])


if __name__ == "__main__":
    unittest.main()
