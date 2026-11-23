import json
import unittest
import urllib.error
import urllib.request

from fleetlight import config, mobile_feed
from fleetlight.monitor import History

NOW = 1_795_000_000.0


def fleet():
    return {"version": 1, "mobile_feed": True, "hosts": [
        {"id": "desk", "name": "Desk", "local": True, "services": ["tailscaled", "docker"], "optional_services": ["docker"]},
        {"id": "server", "name": "Server", "alias": "server", "feed_id": "server-restored", "services": ["smb"]},
        {"id": "studio", "name": "Studio", "alias": "studio"},
        {"id": "attic", "name": "Attic", "alias": "attic"}]}


def online(ident, **extra):
    return {"id": ident, "status": "online", "checked_at": NOW - 5, "os": "Linux", "distribution": "Omarchy 4.5.0",
            "package_manager": "omarchy", "uptime": 86400, "disk_percent": 29, "memory_percent": 47.4, "load": 1.5, "cpus": 8,
            "swap_total": 1000, "swap_used": 100, "cpu_temperature": 52.6, "battery": {"percent": 99, "state": "full"},
            "failed_units": [], "processes": {"cpu": [["chromium", 14]]}, "codex": "0.171.0", "claude": "2.1.330",
            "check_ms": 2228, "services": {}, **extra}


def history():
    store = History("/nonexistent/fleetlight-test/history.json")
    for step in range(100):
        moment = NOW - 86400 + step * 864
        for ident in ("desk", "server", "studio"):
            store.series.setdefault(ident, []).append([moment, 1, 29, 47, 12, 1.5, 52, 2000 + step])
    store.events = [
        {"time": NOW - 900, "host": "server", "message": "smb: failed; Memory usage is high"},
        {"time": NOW - 840, "host": "server", "message": "smb: failed; Memory usage is high"},
        {"time": NOW - 600, "host": "gone", "message": "SSH connection unavailable"},
        {"time": NOW - 300, "host": "server", "message": "Connection and services healthy"}]
    return store


class MobileFeedTests(unittest.TestCase):
    def build(self):
        snapshots = {
            "desk": online("desk", services={"tailscaled": "active", "docker": "inactive"}, optional_services=["docker"]),
            "server": online("server", services={"smb": "failed"}, memory_percent=97,
                             error="unused", os="Linux"),
            "studio": {"id": "studio", "status": "offline", "checked_at": NOW - 5,
                       "error": "SSH connection to 192.0.2.15 unavailable"}}
        checks = {
            "desk": {"system": {"state": "available", "detail": "2 package updates", "packages": ["linux", "flatpak:org.gnome.Platform"],
                                "checked_at": NOW - 60},
                     "restart": {"state": "available", "detail": "Restart required"},
                     "desktop": {"state": "available", "installed": "1.0", "latest": "1.1", "provider": "linux-pacman", "checked_at": NOW - 60}},
            "server": {"system": {"state": "current", "detail": "0 package updates", "packages": [], "checked_at": NOW - 60}}}
        last_seen = {"studio": {"status": "online", "os": "Linux", "package_manager": "apt"}}
        return mobile_feed.document(fleet(), snapshots, checks, history(), "0.9.0", now=NOW, refresh_ms=4100, last_seen=last_seen)

    def test_document_matches_the_schema_the_phone_reads(self):
        feed = self.build()
        self.assertEqual(feed["schemaVersion"], 1)
        self.assertEqual(feed["generatedAt"], "2026-11-18T11:06:40Z")
        self.assertEqual(feed["observer"], {"id": "desk", "name": "Desk", "appVersion": "v0.9.0 (Linux)", "status": "reporting",
                                            "lastRefreshDurationMilliseconds": 4100})
        self.assertEqual(feed["summary"], {"total": 4, "online": 2, "offline": 1, "accessIssues": 0, "slowConnections": 0,
                                           "alerts": 1, "updatesAvailable": 1, "restartRequired": 1})
        json.loads(mobile_feed.encode(feed))

    def test_healthy_computer_with_updates_waiting(self):
        desk = self.build()["hosts"][0]
        self.assertEqual((desk["id"], desk["state"], desk["status"], desk["issueTypes"]), ("desk", "online", "online", ["restart", "updates"]))
        self.assertEqual(desk["detail"], "Root disk 29% used")
        self.assertEqual((desk["platform"], desk["operatingSystem"], desk["health"]), ("Linux", "Omarchy 4.5.0", 100))
        self.assertEqual((desk["memoryPercent"], desk["swapPercent"], desk["cpuTemperatureCelsius"], desk["fullProbeMs"]), (47, 10, 53, 2228))
        self.assertEqual((desk["topProcess"], desk["fleetlightVersion"], desk["restartRequired"]), ("chromium 14%", "v0.9.0 (Linux)", True))
        self.assertEqual(desk["services"], [{"kind": "service", "name": "tailscaled", "state": "healthy", "detail": "Active"},
                                            {"kind": "service", "name": "docker", "state": "ok", "detail": "Not running (optional)"}])
        self.assertEqual((desk["codexDesktopAppState"], desk["codexDesktopAppUpdateAvailable"], desk["codexDesktopAppAvailableVersion"]),
                         ("update-available", True, "1.1"))

    def test_trouble_is_named_once(self):
        server = self.build()["hosts"][1]
        self.assertEqual((server["id"], server["state"], server["issueTypes"]), ("server-restored", "attention", ["alerts"]))
        self.assertEqual(server["detail"], "Memory usage is high; smb: failed")
        self.assertEqual(server["services"], [{"kind": "service", "name": "smb", "state": "failed", "detail": "Failed"}])
        # The stopped service is told by its own entry, so only the other finding is a warning.
        self.assertEqual(server["warnings"], [{"kind": "alert", "title": "Memory usage is high"}])
        self.assertEqual(server["health"], 80)
        self.assertNotIn("fleetlightVersion", server)

    def test_unreachable_and_unchecked_computers(self):
        feed = self.build()
        studio, attic = feed["hosts"][2], feed["hosts"][3]
        self.assertEqual((studio["state"], studio["issueTypes"], studio["health"], studio["platform"]), ("offline", ["offline"], 0, "Linux"))
        self.assertEqual(studio["detail"], "SSH connection to [address] unavailable")
        self.assertEqual((attic["state"], attic["detail"], attic["issueTypes"]), ("unknown", "Not checked yet", []))
        self.assertNotIn("health", attic)
        # A Linux computer that is offline now keeps its package entry from the last time it was seen.
        self.assertEqual([item["hostId"] for item in feed["linuxUpdates"]], ["desk", "server-restored", "studio"])

    def test_linux_updates(self):
        desk, server, _ = self.build()["linuxUpdates"]
        self.assertEqual((desk["state"], desk["availableCount"], desk["flatpakCount"], desk["restartRequired"], desk["packageManager"]),
                         ("updateAvailable", 2, 1, True, "omarchy"))
        self.assertEqual((server["state"], server["availableCount"], server["restartRequired"]), ("current", 0, False))

    def test_incidents_fold_repeats_and_skip_removed_computers(self):
        found = self.build()["incidents"]
        self.assertEqual([(item["hostId"], item["kind"], item["severity"], item["title"], item["detail"]) for item in found],
                         [("server-restored", "recovered", "info", "Healthy again", ""),
                          ("server-restored", "attention", "warning", "smb: failed", "Memory usage is high")])
        self.assertEqual(found[1]["startedAt"], mobile_feed.iso(NOW - 900))

    def test_metrics_and_timing(self):
        feed = self.build()
        samples = [item for item in feed["metrics"] if item["hostId"] == "desk"]
        self.assertTrue(40 <= len(samples) <= 49)
        self.assertEqual({item["hostId"] for item in feed["metrics"]}, {"desk", "server-restored", "studio"})
        self.assertEqual((samples[-1]["state"], samples[-1]["diskPercent"], samples[-1]["memoryPercent"], samples[-1]["loadAverage"]),
                         ("online", 29, 47, 1.5))
        timing = [item for item in feed["timingComparisons"] if item["hostId"] == "server-restored"]
        self.assertEqual([item["windowHours"] for item in timing], [1, 6, 24])
        self.assertTrue(all(item["metric"] == "fullProbe" for item in timing))
        day = timing[-1]
        # The oldest sample sits exactly a day back, which belongs to the period before.
        self.assertEqual((day["currentSampleCount"], day["previousSampleCount"], day["previousAverageMs"]), (99, 1, 2000))
        self.assertIsNone(day["previousCoverageSeconds"])
        # The computer running Fleetlight has no connection to time.
        self.assertFalse([item for item in feed["timingComparisons"] if item["hostId"] == "desk"])

    def test_redaction(self):
        self.assertEqual(mobile_feed.redact("fetch https://example.org/x from 192.0.2.7 as me@example.org in /home/alex/src"),
                         "fetch [url] from [address] as [account] in /home/[user]/src")

    def test_configuration(self):
        self.assertTrue(config.mobile_feed_enabled(config.validate(fleet())))
        self.assertFalse(config.mobile_feed_enabled({"version": 1}))
        with self.assertRaises(ValueError):
            config.validate({**fleet(), "mobile_feed": "yes"})
        broken = fleet()
        broken["hosts"][1]["feed_id"] = "has space"
        with self.assertRaises(ValueError):
            config.validate(broken)


class FeedServerTests(unittest.TestCase):
    def fetch(self, port, path, method="GET"):
        request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.read(), response.headers.get("Cache-Control")
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, b"", None

    def test_serves_only_the_feed_and_only_once_published(self):
        server = mobile_feed.FeedServer()
        self.assertTrue(server.start(port=0))
        self.addCleanup(server.stop)
        port = server.server.server_address[1]
        self.assertEqual(server.server.server_address[0], "127.0.0.1")
        self.assertEqual(self.fetch(port, "/fleetlight/mobile-feed.json")[0], 503)
        server.publish(b'{"schemaVersion":1}')
        self.assertEqual(self.fetch(port, "/fleetlight/mobile-feed.json"), (200, b'{"schemaVersion":1}', "no-store"))
        self.assertEqual(self.fetch(port, "/mobile-feed.json?x=1")[:2], (200, b'{"schemaVersion":1}'))
        self.assertEqual(self.fetch(port, "/fleetlight/mobile-feed.json", "HEAD")[:2], (200, b""))
        self.assertEqual(self.fetch(port, "/fleet.json")[0], 404)
        self.assertEqual(self.fetch(port, "/")[0], 404)
        self.assertTrue(server.start(port=0))

    def test_reports_a_port_that_is_taken(self):
        first, second = mobile_feed.FeedServer(), mobile_feed.FeedServer()
        self.assertTrue(first.start(port=0))
        self.addCleanup(first.stop)
        self.assertFalse(second.start(port=first.server.server_address[1]))
        self.assertFalse(second.running)


if __name__ == "__main__":
    unittest.main()
