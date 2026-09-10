import unittest
from unittest.mock import patch

from fleetlight.probe import collect_metrics


class LocalMetricsTests(unittest.TestCase):
    def test_linux_metrics_do_not_launch_commands(self):
        with patch("fleetlight.probe.command", side_effect=AssertionError("Unexpected subprocess")):
            metrics = collect_metrics("Linux")
        self.assertGreater(metrics["metrics_checked_at"], 0)
        self.assertGreater(metrics["uptime"], 0)
        self.assertGreaterEqual(metrics["memory_percent"], 0)
        self.assertLessEqual(metrics["memory_percent"], 100)
        self.assertNotIn("checked_at", metrics)
        self.assertNotIn("services", metrics)
