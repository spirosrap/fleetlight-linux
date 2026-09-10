import tempfile
import unittest
from pathlib import Path

from fleetlight.probe import cpu_temperature


class TemperatureTests(unittest.TestCase):
    def test_cpu_only_and_invalid_readings(self):
        with tempfile.TemporaryDirectory() as root:
            device = Path(root, "class/hwmon/hwmon0")
            device.mkdir(parents=True)
            (device / "name").write_text("amdgpu")
            (device / "temp1_input").write_text("90000")
            self.assertIsNone(cpu_temperature("Linux", root))
            (device / "name").write_text("coretemp")
            (device / "temp2_input").write_text("65000")
            (device / "temp1_input").write_text("42000")
            self.assertEqual(cpu_temperature("Linux", root), 65.0)
            (device / "temp2_input").write_text("invalid")
            self.assertEqual(cpu_temperature("Linux", root), 42.0)
            (device / "temp1_input").write_text("999999")
            self.assertIsNone(cpu_temperature("Linux", root))

    def test_amd_prefers_die_over_control_offset(self):
        with tempfile.TemporaryDirectory() as root:
            device = Path(root, "class/hwmon/hwmon0")
            device.mkdir(parents=True)
            (device / "name").write_text("k10temp")
            (device / "temp1_label").write_text("Tctl")
            (device / "temp1_input").write_text("85000")
            (device / "temp2_label").write_text("Tdie")
            (device / "temp2_input").write_text("65000")
            self.assertEqual(cpu_temperature("Linux", root), 65.0)

    def test_thermal_fallback_and_unsupported(self):
        with tempfile.TemporaryDirectory() as root:
            zone = Path(root, "class/thermal/thermal_zone0")
            zone.mkdir(parents=True)
            (zone / "type").write_text("acpitz")
            (zone / "temp").write_text("50000")
            self.assertIsNone(cpu_temperature("Linux", root))
            (zone / "type").write_text("x86_pkg_temp")
            self.assertEqual(cpu_temperature("Linux", root), 50.0)
            self.assertIsNone(cpu_temperature("Darwin", root))
