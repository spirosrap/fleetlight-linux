import unittest
from unittest.mock import patch
from types import SimpleNamespace
from fleetlight import system_ops, update_job


def result(code=0, output=''):
    return SimpleNamespace(returncode=code, stdout=output)


class SystemTests(unittest.TestCase):
    def test_arch_protects_modified_app_when_update_would_replace_it(self):
        def which(name):
            return '/bin/' + name if name in ('omarchy-update', 'pacman', 'checkupdates') else None
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops.shutil, 'which', side_effect=which), \
                patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops, 'run', side_effect=[result(0, 'linux 1 -> 2\n'), result(0), result(1)]):
            self.assertEqual(system_ops.check()['state'], 'protected')

    def test_metadata_failure_never_reported_current(self):
        with patch.object(system_ops.platform, 'system', return_value='Linux'), patch.object(system_ops.shutil, 'which', return_value='/bin/tool'), patch.object(system_ops, 'reboot_status', return_value={'required':False}), patch.object(system_ops, 'run', return_value=result(1)):
            self.assertEqual(system_ops.check()['state'], 'unknown')

    def apt_check(self, responses):
        with patch.object(system_ops.platform, 'system', return_value='Linux'), patch.object(system_ops.shutil, 'which', side_effect=lambda name: '/bin/apt' if name == 'apt' else None), patch.object(system_ops, 'reboot_status', return_value={'required': False}), patch.object(system_ops, 'run', side_effect=responses), patch.object(system_ops.time, 'sleep'):
            return system_ops.check()

    def test_apt_lock_retries_without_blaming_sudo(self):
        lock = result(100, 'E: Could not get lock /var/lib/apt/lists/lock. It is held by process 123')
        self.assertEqual(self.apt_check([lock, result(), result()])['state'], 'current')
        failure = self.apt_check([lock, lock, lock])
        self.assertIn('Another package operation', failure['detail'])
        self.assertIn('held by process', failure['error_output'])

    def test_apt_permission_and_repository_failures_are_distinct(self):
        denied = self.apt_check([result(1, 'sudo: a password is required')])
        self.assertIn('Passwordless sudo', denied['detail'])
        repo = self.apt_check([result(100, 'E: Repository has no Release file')])
        self.assertNotIn('sudo', repo['detail'])
        self.assertIn('Repository', repo['error_output'])

    def test_apt_check_bounds_network_and_lock_waits(self):
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops.shutil, 'which', side_effect=lambda name: '/bin/apt' if name == 'apt' else None), \
                patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops, 'run', side_effect=[result(), result()]) as run:
            self.assertEqual(system_ops.check()['state'], 'current')
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[:10], ['sudo', '-n', 'timeout', '--signal=TERM', '--kill-after=5s', '90s',
                                        'env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', '-q'])
        self.assertIn('APT::Update::Error-Mode=any', command)
        self.assertIn('Acquire::Retries=2', command)
        self.assertIn('Acquire::http::Timeout=15', command)
        self.assertIn('Acquire::https::Timeout=15', command)
        self.assertIn('DPkg::Lock::Timeout=0', command)
        self.assertEqual(command[-1], 'update')

    def test_apt_refresh_timeout_is_reported(self):
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops.shutil, 'which', side_effect=lambda name: '/bin/apt' if name == 'apt' else None), \
                patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops, 'run', return_value=result(124, 'timed out')):
            checked = system_ops.check()
        self.assertEqual(checked['state'], 'unknown')
        self.assertIn('timed out', checked['detail'])
        self.assertIn('timed out', checked['error_output'])

    def test_pacman_update_allows_omarchy_guarded_upgrades(self):
        checked = {'state': 'available', 'packages': ['aether'], 'manager': 'pacman'}
        verified = {'state': 'current', 'packages': [], 'manager': 'pacman', 'restart': {'required': False}}
        with patch.object(system_ops, 'check', side_effect=[checked, verified]), \
                patch.object(system_ops.subprocess, 'call', return_value=0) as install:
            self.assertEqual(system_ops.update(), 0)
        install.assert_called_once_with(
            ['sudo', '-n', 'env', 'OMARCHY_ALLOW_DIRECT_PACMAN=1', 'pacman', '-Syu', '--noconfirm'])

    def test_omarchy_update_uses_full_omarchy_path(self):
        checked = {'state': 'available', 'packages': ['omarchy', 'yay-pkg'], 'manager': 'omarchy'}
        verified = {'state': 'current', 'packages': [], 'manager': 'omarchy', 'restart': {'required': False}}
        with patch.object(system_ops, 'check', side_effect=[checked, verified]), \
                patch.object(system_ops, 'run', return_value=result()), \
                patch.dict(system_ops.os.environ, {'PATH': '/usr/bin'}, clear=True), \
                patch.object(system_ops.subprocess, 'call', return_value=0) as install:
            self.assertEqual(system_ops.update(), 0)
        install.assert_called_once()
        self.assertEqual(install.call_args.args[0], ['omarchy-update', '-y'])
        env = install.call_args.kwargs['env']
        self.assertEqual(env['OMARCHY_UPDATE_LOGGED'], '1')
        self.assertEqual(env['OMARCHY_PATH'], '/usr/share/omarchy')
        self.assertTrue(env['PATH'].startswith('/usr/share/omarchy/bin:'))

    def test_omarchy_env_defaults_path_for_ssh_jobs(self):
        with patch.dict(system_ops.os.environ, {'PATH': '/usr/bin'}, clear=True):
            env = system_ops.omarchy_env()
        self.assertEqual(env['OMARCHY_PATH'], '/usr/share/omarchy')
        self.assertEqual(env['OMARCHY_UPDATE_LOGGED'], '1')
        self.assertTrue(env['PATH'].startswith('/usr/share/omarchy/bin:'))

    def test_omarchy_env_keeps_existing_checkout_path(self):
        with patch.dict(system_ops.os.environ, {'OMARCHY_PATH': '/home/user/omarchy', 'PATH': '/usr/bin'}, clear=True):
            env = system_ops.omarchy_env()
        self.assertEqual(env['OMARCHY_PATH'], '/home/user/omarchy')
        self.assertTrue(env['PATH'].startswith('/home/user/omarchy/bin:'))

    def test_omarchy_check_includes_aur_packages(self):
        def which(name):
            return '/usr/bin/' + name if name in ('omarchy-update', 'pacman', 'checkupdates', 'yay') else None
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops.shutil, 'which', side_effect=which), \
                patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops, 'run', side_effect=[
                    result(0, 'linux 6.1 -> 6.2\n'), result(0), result(0),
                    result(0, 'yay-pkg 1-1\n'), result(0, 'yay-pkg 1-1 -> 1-2\n'),
                    result(1)]):
            checked = system_ops.check()
        self.assertEqual(checked['manager'], 'omarchy')
        self.assertEqual(checked['packages'], ['linux', 'yay-pkg'])
        self.assertEqual(checked['changes'], ['linux 6.1 -> 6.2', 'yay-pkg 1-1 -> 1-2'])
        self.assertEqual(checked['state'], 'available')

    def test_yay_error_arrow_is_not_a_package(self):
        self.assertEqual(system_ops.upgrade_lines(" -> 1 error occurred:\nlinux 6.1 -> 6.2\n"),
                         [("linux", "linux 6.1 -> 6.2")])

    def test_omarchy_check_includes_mise_and_pending_migrations(self):
        def which(name):
            return '/usr/bin/' + name if name in (
                'omarchy-update', 'pacman', 'checkupdates', 'mise', 'omarchy-migrate') else None
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops.shutil, 'which', side_effect=which), \
                patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops, 'run', side_effect=[
                    result(2),
                    result(0, '{"node":{"current":"20.0.0","latest":"22.0.0"}}'),
                    result(0, '1780000000.sh\n'),
                    result(1)]):
            checked = system_ops.check()
        self.assertEqual(checked['packages'], ['mise:node', 'omarchy:migrations'])
        self.assertEqual(checked['state'], 'available')

    def test_omarchy_check_includes_official_cursor(self):
        class CursorApi:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"version":"3.21.16"}'

        def which(name):
            return '/usr/bin/' + name if name in ('omarchy-update', 'pacman', 'checkupdates') else None
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops.shutil, 'which', side_effect=which), \
                patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops.urllib.request, 'urlopen', return_value=CursorApi()), \
                patch.object(system_ops, 'run', side_effect=[
                    result(2), result(0, 'cursor-bin 3.21.12-1\n'), result(0, '-1\n')]):
            checked = system_ops.check()
        self.assertEqual(checked['packages'], ['cursor:official'])
        self.assertEqual(checked['state'], 'available')

    def test_omarchy_check_ignores_current_cursor(self):
        class CursorApi:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"version":"3.21.16"}'

        def which(name):
            return '/usr/bin/' + name if name in ('omarchy-update', 'pacman', 'checkupdates') else None
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops.shutil, 'which', side_effect=which), \
                patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops.urllib.request, 'urlopen', return_value=CursorApi()), \
                patch.object(system_ops, 'run', side_effect=[
                    result(2), result(0, 'cursor-bin 3.21.16-1\n'), result(0, '0\n')]):
            checked = system_ops.check()
        self.assertEqual(checked['packages'], [])
        self.assertEqual(checked['state'], 'current')

    def test_apt_check_includes_flatpak_updates(self):
        def which(name):
            return '/bin/' + name if name in ('apt', 'flatpak') else None
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops.shutil, 'which', side_effect=which), \
                patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops, 'run', side_effect=[
                    result(), result(), result(0, 'com.brave.Browser\n'), result(0, 'org.gnome.Platform\n')]):
            checked = system_ops.check()
        self.assertEqual(checked['packages'], ['flatpak:com.brave.Browser', 'flatpak:org.gnome.Platform'])
        self.assertEqual(checked['state'], 'available')
        self.assertIn('2 package updates', checked['detail'])

    def test_sidecar_only_update_skips_the_distribution_upgrade(self):
        checked = {'state': 'available', 'packages': ['flatpak:org.gnome.Platform'], 'manager': 'apt'}
        verified = {'state': 'current', 'packages': [], 'manager': 'apt', 'restart': {'required': False}}
        with patch.object(system_ops, 'check', side_effect=[checked, verified]), \
                patch.object(system_ops, 'install_sidecars', return_value=0) as sidecars, \
                patch.object(system_ops.subprocess, 'call') as install:
            self.assertEqual(system_ops.update(), 0)
        install.assert_not_called()
        sidecars.assert_called_once()

    def test_no_update_when_protected(self):
        with patch.object(system_ops, 'check', return_value={'state':'protected'}), patch.object(system_ops.subprocess, 'call') as install:
            self.assertEqual(system_ops.update(), 1)
            install.assert_not_called()

    def test_restart_rechecks_need_and_schedules_not_immediate(self):
        with patch.object(system_ops, 'reboot_status', return_value={'required':False}), patch.object(system_ops, 'run') as run:
            self.assertEqual(system_ops.restart(), 1)
            run.assert_not_called()
        with patch.object(system_ops, 'reboot_status', return_value={'required':True}), patch.object(system_ops, 'run', return_value=result()) as run:
            self.assertEqual(system_ops.restart(), 0)
            run.assert_called_once_with(['sudo','-n','shutdown','-r','+1'])

    def test_restart_receipt_does_not_claim_reboot_verified(self):
        status, detail, _ = update_job.parse_result('restart','0.0.0','',0,['FLEETLIGHT_RESTART','VERIFY:ok'])
        self.assertEqual(status, 'succeeded')
        self.assertIn('not yet verified', detail)
        self.assertEqual(update_job.parse_result('system','0.0.0','',1,['FLEETLIGHT_SYSTEM_UPDATE','VERIFY:ok'])[0], 'failed')
