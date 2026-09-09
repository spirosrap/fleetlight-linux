import unittest
from unittest.mock import patch
from types import SimpleNamespace
from fleetlight import system_ops, update_job


def result(code=0, output=''):
    return SimpleNamespace(returncode=code, stdout=output)


class SystemTests(unittest.TestCase):
    def test_arch_protects_modified_app_when_update_would_replace_it(self):
        with patch.object(system_ops.platform, 'system', return_value='Linux'), patch.object(system_ops.shutil, 'which', return_value='/bin/tool'), patch.object(system_ops, 'reboot_status', return_value={'required':False}), patch.object(system_ops, 'run', side_effect=[result(0, 'linux 1 -> 2\n'), result(0), result(1)]):
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
