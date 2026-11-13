from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from fleetlight import system_ops, update_job, updates


def result(code=0, output=''):
    return SimpleNamespace(returncode=code, stdout=output)


class SystemTests(unittest.TestCase):
    def test_reboot_status_names_the_current_boot_on_linux(self):
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops, 'current_boot_id', return_value='boot-1'), \
                patch.object(system_ops, 'reboot_requirement', return_value={'required': True, 'reason': 'test'}):
            self.assertEqual(system_ops.reboot_status(), {'required': True, 'reason': 'test', 'boot_id': 'boot-1'})
        with patch.object(system_ops.platform, 'system', return_value='Darwin'):
            self.assertNotIn('boot_id', system_ops.reboot_status())

    def test_arch_protects_modified_app_when_update_would_replace_it(self):
        def which(name):
            return '/bin/' + name if name in ('omarchy-update', 'pacman', 'checkupdates') else None
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops.shutil, 'which', side_effect=which), \
                patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops, 'run', side_effect=[result(0, 'linux 1 -> 2\n'), result(0), result(1)]):
            self.assertEqual(system_ops.check()['state'], 'protected')

    def test_metadata_failure_never_reported_current(self):
        with patch.object(system_ops.platform, 'system', return_value='Linux'), patch.object(system_ops.shutil, 'which', return_value='/bin/tool'), patch.object(system_ops, 'reboot_status', return_value={'required':False}), patch.object(system_ops, 'run', return_value=result(1)), patch.object(system_ops.time, 'sleep'):
            self.assertEqual(system_ops.check()['state'], 'unknown')

    def test_arch_metadata_refresh_retried_after_mirror_stall(self):
        def which(name):
            return '/bin/' + name if name in ('pacman', 'checkupdates') else None
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops.shutil, 'which', side_effect=which), \
                patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops, 'run', side_effect=[result(1), result(2)]), \
                patch.object(system_ops.time, 'sleep') as sleep:
            self.assertEqual(system_ops.check()['state'], 'current')
        sleep.assert_called_once()

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

    def test_restart_refuses_when_not_required_but_manual_reboot_proceeds(self):
        with patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops, 'run', return_value=result()) as run:
            self.assertEqual(system_ops.restart(), 1)
            run.assert_not_called()
            self.assertEqual(system_ops.restart(required=False), 0)
        run.assert_called_once_with(['sudo', '-n', 'shutdown', '-r', '+1'])

    def test_pacman_update_allows_omarchy_guarded_upgrades(self):
        checked = {'state': 'available', 'packages': ['aether'], 'manager': 'pacman'}
        verified = {'state': 'current', 'packages': [], 'manager': 'pacman', 'restart': {'required': False}}
        with patch.object(system_ops, 'check', side_effect=[checked, verified]), \
                patch.object(system_ops, 'call_watching', return_value=(0, False)) as install:
            self.assertEqual(system_ops.update(), 0)
        install.assert_called_once_with(
            ['sudo', '-n', 'env', 'OMARCHY_ALLOW_DIRECT_PACMAN=1', 'pacman', '-Syu', '--noconfirm'])

    def test_apt_update_uses_full_upgrade_for_new_packages(self):
        checked = {'state': 'available', 'packages': ['linux-generic'], 'manager': 'apt', 'changes': ['linux-generic 6.8.0-139.139 -> 6.8.0-142.142']}
        verified = {'state': 'current', 'packages': [], 'manager': 'apt', 'restart': {'required': False}}
        with patch.object(system_ops, 'check', side_effect=[checked, verified]), \
                patch.object(system_ops, 'call_watching', return_value=(0, False)) as install:
            self.assertEqual(system_ops.update(), 0)
        self.assertEqual(install.call_args.args[0][-1], 'full-upgrade')

    def test_omarchy_update_uses_full_omarchy_path(self):
        checked = {'state': 'available', 'packages': ['omarchy', 'yay-pkg'], 'manager': 'omarchy'}
        verified = {'state': 'current', 'packages': [], 'manager': 'omarchy', 'restart': {'required': False}}
        with patch.object(system_ops, 'check', side_effect=[checked, verified]), \
                patch.object(system_ops, 'run', return_value=result()), \
                patch.dict(system_ops.os.environ, {'PATH': '/usr/bin'}, clear=True), \
                patch.object(system_ops, 'call_watching', return_value=(0, False)) as install:
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
                    result(0, '{"node":{"current":"20.0.0","latest":"22.0.0"},'
                              '"amp":{"current":null,"latest":"0.0.1"}}'),
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
                patch.object(system_ops, 'call_watching') as install:
            self.assertEqual(system_ops.update(), 0)
        install.assert_not_called()
        sidecars.assert_called_once()

    def test_update_succeeds_when_an_unrelated_release_appears_during_install(self):
        checked = {'state': 'available', 'packages': ['claude-desktop'], 'manager': 'omarchy',
                   'changes': ['claude-desktop 3.1623.3-1 -> 3.1623.4-1']}
        verified = {'state': 'available', 'packages': ['brave-bin'], 'manager': 'omarchy',
                    'changes': ['brave-bin 1:1.97.59-1 -> 1:1.98.60-1'], 'restart': {'required': False}}
        with patch.object(system_ops, 'check', side_effect=[checked, verified]), \
                patch.object(system_ops, 'run', return_value=result()), \
                patch.object(system_ops, 'call_watching', return_value=(0, False)):
            self.assertEqual(system_ops.update(), 0)

    def test_update_fails_when_a_requested_package_is_still_pending(self):
        checked = {'state': 'available', 'packages': ['claude-desktop', 'brave-bin'], 'manager': 'omarchy'}
        verified = {'state': 'available', 'packages': ['claude-desktop'], 'manager': 'omarchy', 'restart': {'required': False}}
        with patch.object(system_ops, 'check', side_effect=[checked, verified]), \
                patch.object(system_ops, 'run', return_value=result()), \
                patch.object(system_ops, 'call_watching', return_value=(0, False)):
            self.assertEqual(system_ops.update(), 1)
        self.assertFalse(system_ops.installed_everything({'packages': ['a']}, {'state': 'unknown', 'packages': []}))

    def test_installer_network_failure_is_reported_as_retryable(self):
        checked = {'state': 'available', 'packages': ['omarchy'], 'manager': 'omarchy'}
        with patch.object(system_ops, 'check', return_value=checked), \
                patch.object(system_ops, 'run', return_value=result()), \
                patch.object(system_ops, 'call_watching', return_value=(1, 'network')), \
                patch('builtins.print') as printed:
            self.assertEqual(system_ops.update(), 1)
        self.assertIn('UPDATE:network-failed', ' '.join(str(c.args[0]) for c in printed.call_args_list))
        self.assertEqual(update_job.parse_result('system', '', '', 1, ['UPDATE:network-failed', 'VERIFY:failed'])[1],
                         update_job.ERRORS['network-failed'])
        with patch.object(system_ops, 'check', return_value=checked), \
                patch.object(system_ops, 'run', return_value=result()), \
                patch.object(system_ops, 'call_watching', return_value=(1, '')), \
                patch('builtins.print') as printed:
            self.assertEqual(system_ops.update(), 1)
        self.assertIn('UPDATE:install-failed', ' '.join(str(c.args[0]) for c in printed.call_args_list))

    def test_call_watching_passes_output_through_and_spots_mirror_stalls(self):
        import io, sys
        buffer = io.BytesIO()
        fake = SimpleNamespace(buffer=buffer, flush=lambda: None)
        script = "print('ok'); print(\"error: failed retrieving file 'omarchy.db' : Operation too slow\"); raise SystemExit(1)"
        with patch.object(system_ops.sys, 'stdout', fake):
            code, network = system_ops.call_watching([sys.executable, '-c', script])
        self.assertEqual((code, network), (1, 'network'))
        self.assertIn(b'failed retrieving file', buffer.getvalue())
        with patch.object(system_ops.sys, 'stdout', SimpleNamespace(buffer=io.BytesIO(), flush=lambda: None)):
            self.assertEqual(system_ops.call_watching([sys.executable, '-c', "print('error: conflicting files')"]), (0, ''))

    def test_call_watching_spots_a_download_that_fails_its_checksum(self):
        import io, sys
        corrupt = ("print(':: File /var/cache/pacman/pkg/yt-dlp-2026.10.26-1-any.pkg.tar.zst is corrupted "
                   "(invalid or corrupted package (checksum)).'); raise SystemExit(1)")
        stalled = corrupt.replace("raise", "print('error: failed retrieving file'); raise")
        with patch.object(system_ops.sys, 'stdout', SimpleNamespace(buffer=io.BytesIO(), flush=lambda: None)):
            self.assertEqual(system_ops.call_watching([sys.executable, '-c', corrupt]), (1, 'corrupt'))
            # A mirror that also stalls is left to the slower network retry.
            self.assertEqual(system_ops.call_watching([sys.executable, '-c', stalled]), (1, 'network'))

    def test_corrupted_download_is_fetched_again_once_then_reported_as_retryable(self):
        checked = {'state': 'available', 'packages': ['yt-dlp'], 'manager': 'omarchy'}
        verified = {'state': 'current', 'packages': [], 'manager': 'omarchy', 'restart': {'required': False}}
        with patch.object(system_ops, 'check', side_effect=[checked, verified]), \
                patch.object(system_ops, 'run', return_value=result()), \
                patch.object(system_ops, 'call_watching', side_effect=[(1, 'corrupt'), (0, '')]) as install:
            self.assertEqual(system_ops.update(), 0)
        self.assertEqual(install.call_count, 2)
        self.assertEqual(install.call_args_list[0], install.call_args_list[1])
        with patch.object(system_ops, 'check', return_value=checked), \
                patch.object(system_ops, 'run', return_value=result()), \
                patch.object(system_ops, 'call_watching', return_value=(1, 'corrupt')) as install, \
                patch('builtins.print') as printed:
            self.assertEqual(system_ops.update(), 1)
        self.assertEqual(install.call_count, 2)
        self.assertIn('UPDATE:package-corrupted', ' '.join(str(c.args[0]) for c in printed.call_args_list))
        phase = update_job.parse_result('system', '', '', 1, ['UPDATE:package-corrupted', 'VERIFY:failed'])[1]
        self.assertEqual(phase, update_job.ERRORS['package-corrupted'])
        self.assertIn(phase, updates.TRANSIENT_FAILURES)
        for failure in ('network', ''):
            with patch.object(system_ops, 'check', return_value=checked), \
                    patch.object(system_ops, 'run', return_value=result()), \
                    patch.object(system_ops, 'call_watching', return_value=(1, failure)) as install:
                self.assertEqual(system_ops.update(), 1)
            self.assertEqual(install.call_count, 1)

    def test_kernel_kept_by_modules_hook_still_counts_as_replaced(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            modules = system_ops.Path(root)
            self.assertTrue(system_ops.running_kernel_replaced(modules, '7.2.8-9-omarchy'))  # directory gone
            kept = modules / '7.2.8-9-omarchy'
            kept.mkdir()
            self.assertFalse(system_ops.running_kernel_replaced(modules, '7.2.8-9-omarchy'))  # custom kernel, no pkgbase
            (kept / 'pkgbase').write_text('linux-omarchy\n')
            installed = lambda name: name == 'linux-omarchy'
            self.assertTrue(system_ops.running_kernel_replaced(modules, '7.2.8-9-omarchy', owned=lambda p: False, installed=installed))
            self.assertFalse(system_ops.running_kernel_replaced(modules, '7.2.8-9-omarchy', owned=lambda p: True, installed=installed))
            self.assertFalse(system_ops.running_kernel_replaced(modules, '7.2.8-9-omarchy', owned=lambda p: False, installed=lambda n: False))

    def test_no_update_when_protected(self):
        with patch.object(system_ops, 'check', return_value={'state':'protected'}), patch.object(system_ops, 'call_watching') as install:
            self.assertEqual(system_ops.update(), 1)
            install.assert_not_called()

    def test_restart_rechecks_need_and_schedules_not_immediate(self):
        with patch.object(system_ops, 'reboot_status', return_value={'required':False}), patch.object(system_ops, 'run') as run:
            self.assertEqual(system_ops.restart(), 1)
            run.assert_not_called()
        with patch.object(system_ops, 'reboot_status', return_value={'required':True}), patch.object(system_ops, 'run', return_value=result()) as run:
            self.assertEqual(system_ops.restart(), 0)
            run.assert_called_once_with(['sudo','-n','shutdown','-r','+1'])

    # --- AUR packages that cannot be installed --------------------------------------------------

    YAY_MISSING_DEPENDENCY = (
        ":: Searching AUR for updates...\n"
        " -> No AUR package found for openssl-3 (required by: python312)\n"
        ":: 1 package to upgrade/install.\n"
        "1  aur/python312  3.12.16-1 -> 3.12.16-2 [4m]\n"
        " -> could not find all required packages: openssl-3 \n")

    def holds_file(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(system_ops, 'HELD_FILE', Path(directory.name) / 'held-packages.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_yay_failures_name_the_package_and_the_reason(self):
        self.assertEqual(system_ops.aur_failures(self.YAY_MISSING_DEPENDENCY),
                         {'python312': 'needs openssl-3, which the repositories here do not have yet'})
        build = (" -> error making: zoom-bin-exit status 4\n"
                 " -> Failed to install the following packages. Manual intervention is required:\n"
                 "zoom-bin - exit status 4\n")
        self.assertEqual(system_ops.aur_failures(build), {'zoom-bin': 'did not build'})
        self.assertEqual(system_ops.aur_failures(" -> error downloading sources: \x1b[36mfoo\x1b[0m \n\t context: exit status 1"),
                         {'foo': 'its sources could not be downloaded'})
        self.assertEqual(system_ops.aur_failures("error: failed to commit transaction (conflicting files)"), {})

    def test_aur_package_whose_dependency_no_repository_has_is_held_back(self):
        self.holds_file()
        rpc = {('info', 'arg%5B%5D=python312&arg%5B%5D=yay-pkg'): [
                   {'Name': 'python312', 'Depends': ['bzip2', 'openssl-3'], 'MakeDepends': ['tk']},
                   {'Name': 'yay-pkg', 'Depends': ['glibc', 'aur-only-lib']}],
               ('info', 'arg%5B%5D=aur-only-lib&arg%5B%5D=openssl-3'): [{'Name': 'aur-only-lib'}],
               ('search/openssl-3', 'by=provides'): []}
        runs = []

        def run(args, env=None):
            runs.append(args)
            if args[:2] == ['pacman', '-T']:
                return result(127, 'openssl-3\naur-only-lib\ntk\n')
            return result(0 if args[-1] == 'tk' else 1)  # only tk is in a repository
        lines = ['python312 3.12.16-1 -> 3.12.16-2', 'yay-pkg 1 -> 2']
        with patch.object(system_ops, 'aur_rpc', side_effect=lambda path, query='': rpc[(path, query)]), \
                patch.object(system_ops, 'run', side_effect=run):
            installable, held = system_ops.held_updates(lines, '/tmp/fresh-db')
        self.assertEqual(installable, ['yay-pkg 1 -> 2'])
        self.assertEqual(held, [{'name': 'python312', 'change': 'python312 3.12.16-1 -> 3.12.16-2',
                                 'reason': 'needs openssl-3, which the repositories here do not have yet'}])
        self.assertIn(['pacman', '--dbpath', '/tmp/fresh-db', '-Sddp', '--print-format', '%n', '--noconfirm', 'openssl-3'], runs)

    def test_unreachable_aur_holds_nothing_back(self):
        self.holds_file()
        with patch.object(system_ops, 'aur_rpc', return_value=None), patch.object(system_ops, 'run') as run:
            self.assertEqual(system_ops.held_updates(['python312 1 -> 2']), (['python312 1 -> 2'], []))
        run.assert_not_called()

    def test_failed_aur_build_is_set_aside_longer_each_time_and_released_by_a_new_version(self):
        self.holds_file()
        checked = {'aur': ['zoom-bin'], 'changes': ['linux 1 -> 2', 'zoom-bin 6.1-1 -> 6.2-1']}
        blocked = {'zoom-bin': 'did not build', 'not-pending': 'did not build'}
        self.assertEqual(system_ops.hold_failed(blocked, checked, now=1000), ['zoom-bin'])
        self.assertEqual(system_ops.hold_failed({'linux': 'did not build'}, checked, now=1000), [])
        with patch.object(system_ops, 'missing_dependencies', return_value={}):
            line = 'zoom-bin 6.1-1 -> 6.2-1'
            self.assertEqual(system_ops.held_updates([line], now=1001),
                             ([], [{'name': 'zoom-bin', 'change': line, 'reason': 'did not build'}]))
            # The wait is over: it is offered again, and a second failure waits twice as long.
            self.assertEqual(system_ops.held_updates([line], now=1000 + system_ops.HOLD_DELAYS[0]), ([line], []))
            system_ops.hold_failed(blocked, checked, now=5000)
            self.assertEqual(system_ops.load_holds()['zoom-bin']['after'], 5000 + system_ops.HOLD_DELAYS[1])
            # A new AUR version is tried straight away.
            newer = 'zoom-bin 6.1-1 -> 6.3-1'
            self.assertEqual(system_ops.held_updates([newer], now=5001), ([newer], []))
            self.assertEqual(system_ops.load_holds(), {})

    def test_check_reports_held_packages_without_calling_them_updates(self):
        held = [{'name': 'python312', 'change': 'python312 1 -> 2', 'reason': 'needs openssl-3'}]
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops.shutil, 'which', side_effect=lambda name: '/bin/' + name if name in ('omarchy-update', 'pacman', 'checkupdates') else None), \
                patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops, 'run', return_value=result(2)), \
                patch.object(system_ops, 'aur_updates', return_value=['python312 1 -> 2', 'yay-pkg 1 -> 2']), \
                patch.object(system_ops, 'held_updates', return_value=(['yay-pkg 1 -> 2'], held)), \
                patch.object(system_ops, 'omarchy_extra_updates', return_value=([], [])):
            checked = system_ops.check()
            self.assertEqual((checked['state'], checked['packages'], checked['aur'], checked['held']),
                             ('available', ['yay-pkg'], ['yay-pkg'], held))
            self.assertEqual(checked['detail'], '1 package updates · 1 held back')
        with patch.object(system_ops.platform, 'system', return_value='Linux'), \
                patch.object(system_ops.shutil, 'which', side_effect=lambda name: '/bin/' + name if name in ('omarchy-update', 'pacman', 'checkupdates') else None), \
                patch.object(system_ops, 'reboot_status', return_value={'required': False}), \
                patch.object(system_ops, 'run', return_value=result(2)), \
                patch.object(system_ops, 'aur_updates', return_value=['python312 1 -> 2']), \
                patch.object(system_ops, 'held_updates', return_value=([], held)), \
                patch.object(system_ops, 'omarchy_extra_updates', return_value=([], [])):
            self.assertEqual(system_ops.check()['state'], 'current')

    def test_update_finishes_without_an_aur_package_that_fails_to_build(self):
        self.holds_file()
        # Not a core package: update() records a restart request in the real state directory for those.
        checked = {'state': 'available', 'packages': ['brave-bin', 'zoom-bin'], 'aur': ['zoom-bin'], 'manager': 'omarchy',
                   'changes': ['brave-bin 1 -> 2', 'zoom-bin 6.1-1 -> 6.2-1']}
        verified = {'state': 'current', 'packages': [], 'manager': 'omarchy', 'restart': {'required': False},
                    'held': [{'name': 'zoom-bin'}]}
        environments = []

        def install(argv, env=None, blocked=None):
            environments.append(env['PATH'])
            if blocked is not None:
                blocked['zoom-bin'] = 'did not build'
                return 1, ''
            wrapper = Path(env['PATH'].split(':')[0]) / 'yay'
            self.assertEqual(wrapper.read_text(), '#!/bin/sh\nexec /usr/bin/yay "$@" --ignore zoom-bin\n')
            return 0, ''
        with patch.object(system_ops, 'check', side_effect=[checked, verified]), \
                patch.object(system_ops, 'run', return_value=result()), \
                patch.object(system_ops, 'state_directory', return_value=None), \
                patch.object(system_ops.shutil, 'which', return_value='/usr/bin/yay'), \
                patch.object(system_ops, 'call_watching', side_effect=install), \
                patch('builtins.print') as printed:
            self.assertEqual(system_ops.update(), 0)
        output = [str(call.args[0]) for call in printed.call_args_list]
        self.assertIn('HELD:zoom-bin did not build', output)
        self.assertEqual(len(environments), 2)
        self.assertEqual(system_ops.load_holds()['zoom-bin']['version'], '6.2-1')
        lines = ['FLEETLIGHT_SYSTEM_UPDATE', 'CHANGED:brave-bin 1 -> 2', 'CHANGED:zoom-bin 6.1-1 -> 6.2-1', 'HELD:zoom-bin did not build', 'VERIFY:ok']
        self.assertEqual(update_job.parse_result('system', '0.0.0', '', 0, lines)[1], 'System packages verified · held back: zoom-bin')
        self.assertEqual(update_job.installation_changes({'kind': 'system', 'log': '\n'.join(lines)}), ['brave-bin 1 → 2'])

    def test_update_skips_packages_the_check_already_holds_back(self):
        checked = {'state': 'available', 'packages': ['brave-bin'], 'aur': [], 'manager': 'omarchy',
                   'held': [{'name': 'python312', 'change': 'python312 1 -> 2', 'reason': 'needs openssl-3'}]}
        verified = dict(checked, state='current', packages=[], restart={'required': False})

        def install(argv, env=None, blocked=None):
            self.assertIn('--ignore python312', (Path(env['PATH'].split(':')[0]) / 'yay').read_text())
            return 0, ''
        with patch.object(system_ops, 'check', side_effect=[checked, verified]), \
                patch.object(system_ops, 'run', return_value=result()), \
                patch.object(system_ops, 'state_directory', return_value=None), \
                patch.object(system_ops.shutil, 'which', return_value='/usr/bin/yay'), \
                patch.object(system_ops, 'call_watching', side_effect=install) as installer:
            self.assertEqual(system_ops.update(), 0)
        installer.assert_called_once()

    def test_call_watching_collects_the_aur_packages_a_failed_run_blames(self):
        script = "import sys; sys.stdout.write(%r); sys.exit(1)" % self.YAY_MISSING_DEPENDENCY
        blocked = {}
        with patch.object(system_ops.sys, 'stdout'):
            self.assertEqual(system_ops.call_watching([sys.executable, '-c', script], blocked=blocked), (1, ''))
        self.assertEqual(list(blocked), ['python312'])

    def test_restart_receipt_does_not_claim_reboot_verified(self):
        status, detail, _ = update_job.parse_result('restart','0.0.0','',0,['FLEETLIGHT_RESTART','VERIFY:ok'])
        self.assertEqual(status, 'succeeded')
        self.assertIn('not yet verified', detail)
        self.assertEqual(update_job.parse_result('system','0.0.0','',1,['FLEETLIGHT_SYSTEM_UPDATE','VERIFY:ok'])[0], 'failed')
