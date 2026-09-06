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
