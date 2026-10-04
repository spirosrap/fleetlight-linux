import json
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch

from fleetlight import actions, agents, config, net, probe
from fleetlight.monitor import History


class DashboardMetricsTests(unittest.TestCase):
    def test_cpu_percent_measures_activity_between_samples(self):
        self.assertEqual(probe.cpu_usage((1000, 600), (1100, 670)), 30)
        self.assertIsNone(probe.cpu_usage(None, (1100, 670)))
        self.assertIsNone(probe.cpu_usage((1100, 670), (1100, 670)))
        with patch('fleetlight.probe._cpu_last', None), patch('fleetlight.probe.cpu_times', side_effect=[(1000, 600), (1100, 670)]):
            self.assertIsNone(probe.cpu_percent('Linux'))
            self.assertEqual(probe.cpu_percent('Linux'), 30)

    def test_memory_readings_preserve_byte_counts_and_swap(self):
        raw = 'MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 500 kB\nSwapFree: 200 kB\n'
        with patch('fleetlight.probe.text', return_value=raw):
            result = probe.memory_info('Linux')
        self.assertEqual(result['memory_percent'], 60)
        self.assertEqual(result['memory_used'], 600 * 1024)
        self.assertEqual(result['memory_total'], 1000 * 1024)
        self.assertEqual(result['swap_used'], 300 * 1024)

    def test_fan_reports_rpm_before_thermal_control_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hwmon = root / 'class/hwmon/hwmon0'
            hwmon.mkdir(parents=True)
            (hwmon / 'fan1_input').write_text('1200')
            (hwmon / 'fan2_input').write_text('2200')
            (hwmon / 'fan3_input').write_text('99999')
            self.assertEqual(probe.fan_status('Linux', root), {'rpm': 2200})
            for p in hwmon.glob('fan*_input'):
                p.unlink()
            cooling = root / 'class/thermal/cooling_device0'
            cooling.mkdir(parents=True)
            (cooling / 'type').write_text('Fan')
            (cooling / 'cur_state').write_text('2')
            (cooling / 'max_state').write_text('4')
            self.assertEqual(probe.fan_status('Linux', root), {'running': True, 'percent': 50})
            self.assertIsNone(probe.fan_status('Darwin', root))

    def test_battery_is_cached_between_live_metric_refreshes(self):
        with patch('fleetlight.probe._battery_last', {'at': 0, 'value': None}), patch('fleetlight.probe.time.time', side_effect=[100, 102, 132]), patch('fleetlight.probe.battery', side_effect=[{'percent': 75}, {'percent': 74}]) as read:
            self.assertEqual(probe.battery_cached('Linux'), {'percent': 75})
            self.assertEqual(probe.battery_cached('Linux'), {'percent': 75})
            self.assertEqual(probe.battery_cached('Linux'), {'percent': 74})
            self.assertEqual(read.call_count, 2)

    def test_storage_deduplicates_devices_and_excludes_virtual_mounts(self):
        mounts = '/dev/a / btrfs rw 0 0\n/dev/a /var btrfs rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n/dev/b /srv/media ext4 rw 0 0\n'
        with patch('fleetlight.probe.shutil.disk_usage', return_value=Mock(total=10 * 1024**3, used=4 * 1024**3, free=6 * 1024**3)):
            result = probe.disks('Linux', mounts)
        self.assertEqual([row['mount'] for row in result], ['/', '/srv/media'])
        self.assertTrue(all(row['percent'] == 40 for row in result))


class DashboardHistoryTests(unittest.TestCase):
    def test_previous_history_format_migrates_without_losing_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            event = {'host': 'example', 'time': 100, 'message': 'Healthy'}
            path.write_text(json.dumps({'samples': [{'host': 'example', 'time': 100, 'status': 'online', 'disk': 34, 'memory': 26}], 'events': [event]}))
            history = History(path)
            self.assertEqual(history.points('example', 'disk'), [(100, 34)])
            self.assertEqual(history.points('example', 'up'), [(100, 1)])
            history.save()
            restored = History(path)
            self.assertEqual(restored.events, [event])
            self.assertEqual(restored.points('example', 'disk'), [(100, 34)])
            self.assertEqual(json.loads(path.read_text())['version'], 2)

    def test_old_samples_are_averaged_and_week_old_samples_expire(self):
        now = 2000000
        rows = [[now - 8 * 86400, 1, 10, 10, None, None, None, None],
                [now - 10800, 1, 20, 40, None, None, None, None],
                [now - 10790, 0, 40, 60, None, None, None, None],
                [now - 60, 1, 50, 70, None, None, None, None],
                [now - 30, 1, 60, 80, None, None, None, None]]
        compact = History.compact(rows, now)
        self.assertEqual(len(compact), 3)
        self.assertEqual(compact[0][1:4], [0.5, 30, 50])
        self.assertEqual(compact[1:], rows[-2:])

    def test_availability_distinguishes_outages_from_missing_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            history = History(Path(directory) / 'history.json')
            history.series['example'] = [[5, 1], [10, 0], [80, 1]]
            self.assertEqual(history.availability('example', 0, 100, 4), [0.5, None, None, 1.0])

    def test_unchanged_checks_do_not_write_history_every_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            history = History(Path(directory) / 'history.json')
            online = {'example': {'status': 'online', 'disk_percent': 34, 'memory_percent': 26}}
            with patch('fleetlight.monitor.time.time', return_value=10000):
                history.record(online, {})
            with patch.object(history, 'save', wraps=history.save) as save, patch('fleetlight.monitor.time.time', return_value=10060):
                history.record(online, online)
                save.assert_not_called()
            with patch.object(history, 'save', wraps=history.save) as save, patch('fleetlight.monitor.time.time', return_value=10300):
                history.record(online, online)
                save.assert_called_once()


class DashboardConfigurationTests(unittest.TestCase):
    def test_appearance_preferences_validate_and_default(self):
        self.assertEqual(config.appearance(config.default_config()), 'auto')
        for choice in ('auto', 'dark', 'light'):
            settings = config.default_config()
            settings['appearance'] = choice
            self.assertEqual(config.appearance(config.validate(settings)), choice)
        with self.assertRaises(ValueError):
            config.validate(dict(config.default_config(), appearance='invalid'))

    def test_quota_reset_timestamps_handle_seconds_and_milliseconds(self):
        self.assertEqual(agents.epoch(1790000000), 1790000000)
        self.assertEqual(agents.epoch(1790000000000), 1790000000)
        self.assertIsNone(agents.epoch('invalid'))
        self.assertIsNone(agents.epoch(-1))

    def test_https_openers_reuse_a_verified_tls_context(self):
        context = net.context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        first, second = net.opener(), net.opener()
        from urllib.request import HTTPSHandler
        a = next(h for h in first.handlers if isinstance(h, HTTPSHandler))
        b = next(h for h in second.handlers if isinstance(h, HTTPSHandler))
        self.assertIs(a._context, context)
        self.assertIs(b._context, context)

    def test_wake_packet_is_exact_and_rejects_invalid_addresses(self):
        address = '02:00:00:00:00:01'
        self.assertEqual(actions.magic_packet(address), b'\xff' * 6 + bytes.fromhex(address.replace(':', '')) * 16)
        self.assertEqual(len(actions.magic_packet(address)), 102)
        for invalid in (None, '', 'invalid', '02:00:00:00:00', '02:00:00:00:00:GG'):
            with self.assertRaises(ValueError):
                actions.magic_packet(invalid)

    def test_wake_uses_broadcast_socket_without_real_network_access(self):
        with patch('fleetlight.actions.socket.socket') as factory:
            handle = factory.return_value.__enter__.return_value
            self.assertEqual(actions.wake('02:00:00:00:00:01', '203.0.113.255'), ['255.255.255.255', '203.0.113.255'])
            handle.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self.assertEqual(handle.sendto.call_count, 4)
            self.assertEqual({call.args[1] for call in handle.sendto.call_args_list}, {('255.255.255.255', 9), ('255.255.255.255', 7), ('203.0.113.255', 9), ('203.0.113.255', 7)})
