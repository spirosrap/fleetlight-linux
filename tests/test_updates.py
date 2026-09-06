import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from fleetlight import update_job, updates
from fleetlight.config import validate
from fleetlight.monitor import issues


class UpdateTests(unittest.TestCase):
    def test_batch_filters_and_freezes_reviewed_releases(self):
        hosts = [{"id": str(i), "name": "Host " + str(i), "local": True} for i in range(5)]
        snapshots = {h["id"]: {"status": "online"} for h in hosts}
        checks = {h["id"]: {"cli": dict(updates.plan("1.0.0", "1.1.0", "standalone"), checked_at=100)} for h in hosts}
        checks["1"]["cli"]["state"] = "protected"
        checks["2"]["cli"]["state"] = "current"
        snapshots["3"]["status"] = "offline"
        checks["4"]["cli"]["checked_at"] = -2000
        pending, skipped = updates.batch_candidates(hosts, snapshots, checks, "cli", now=200)
        self.assertEqual([x["host"]["id"] for x in pending], ["0"])
        self.assertEqual([x["reason"] for x in skipped], ["protected", "current", "offline", "check again"])
        checks["0"]["cli"]["latest"] = "9.0.0"
        self.assertEqual(pending[0]["checked"]["latest"], "1.1.0")

    def test_receipts_require_marker_version_and_verification(self):
        good = ['FLEETLIGHT_CODEX_UPDATE', 'ACTIVE_VERSION:1.2.3', 'VERIFY:ok']
        self.assertEqual(update_job.parse_result('cli', '1.2.3', '', 0, good)[0], 'succeeded')
        for lines, code in [(good[1:], 0), (good[:-1], 0), (good, 1)]:
            self.assertEqual(update_job.parse_result('cli', '1.2.3', '', code, lines)[0], 'failed')
        self.assertEqual(update_job.parse_result('cli', '1.2.4', '', 0, good)[0], 'failed')

    def test_app_build_and_relaunch_verified(self):
        receipt = ['FLEETLIGHT_CODEX_APP_UPDATE', 'AFTER_VERSION:26.1.2', 'AFTER_BUILD:123', 'VERIFY:current']
        self.assertEqual(update_job.parse_result('desktop', '26.1.2', '123', 0, receipt)[0], 'succeeded')
        self.assertEqual(update_job.parse_result('desktop', '26.1.2', '124', 0, receipt)[0], 'failed')
        self.assertEqual(update_job.parse_result('desktop', '26.1.2', '123', 0, receipt + ['RELAUNCH:failed'])[0], 'failed')

    def test_modified_packages_protected_and_revision_updates(self):
        receipt = 'FLEETLIGHT_CODEX_APP_RELEASE_CHECK\nINSTALLED_VERSION:26.1.2\nAVAILABLE_VERSION:26.1.2\nUPDATE_AVAILABLE:1\nVERIFY:ok\n'
        self.assertEqual(updates.parse_desktop_check(0, receipt)['state'], 'available')
        protected = updates.parse_desktop_check(0, receipt + 'INSTALLATION:modified\n')
        self.assertEqual(protected['state'], 'protected')
        with self.assertRaises(ValueError):
            updates.start_job({'id': 'test', 'name': 'Test', 'local': True}, 'desktop', protected)

    def test_optional_services_not_failures(self):
        snapshot = {'status': 'online', 'services': {'docker': 'inactive', 'smb': 'not installed'}, 'optional_services': ['docker', 'smb']}
        self.assertEqual(issues(snapshot), [])
        snapshot['services']['docker'] = 'failed'
        self.assertEqual(issues(snapshot), ['docker: failed'])
        snapshot['optional_services'] = []
        self.assertEqual(len(issues(snapshot)), 2)
        with self.assertRaises(ValueError):
            validate({'version': 1, 'hosts': [{'id':'test', 'name':'Test', 'local':True, 'optional_services':['unknown']}]})

    def test_macos_release_build(self):
        self.assertEqual(updates.plan('26.1.2', '26.1.2', 'macos', build='124', installed_build='123')['state'], 'available')
        with self.assertRaises(ValueError):
            updates.parse_appcast('<rss><channel><item><enclosure url="https://example.org/app.zip"/></item></channel></rss>')

    def test_durable_job_lock_idempotency_and_verbose_receipt(self):
        source = Path(update_job.__file__).read_text()
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, HOME=home)
            def request(payload):
                result = subprocess.run([sys.executable, '-c', source], input=json.dumps(payload), env=env, text=True, capture_output=True, check=True)
                return json.loads(result.stdout.split('FLEETLIGHT_JOB=', 1)[1])
            ident = uuid.uuid4().hex
            script = "printf 'FLEETLIGHT_CODEX_UPDATE\\n'; sleep 1; i=0; while [ $i -lt 700 ]; do echo noise; i=$((i+1)); done; printf 'ACTIVE_VERSION:1.2.3\\nVERIFY:ok\\n'"
            start = {'operation':'start', 'id':ident, 'kind':'cli', 'target':'1.2.3', 'script':script, 'worker_source':source}
            self.assertEqual(request(start)['state'], 'queued')
            duplicate = request(start)
            self.assertIn(duplicate['state'], ('queued','running'))
            self.assertEqual(request(dict(start, id=uuid.uuid4().hex))['state'], 'busy')
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                result = request({'operation':'status', 'id':ident})
                if result['state'] not in ('queued','running'):
                    break
                time.sleep(.1)
            self.assertEqual(result['state'], 'succeeded', result)
            self.assertEqual(request(start)['state'], 'succeeded')

    def test_cli_same_or_newer_version_never_installs(self):
        script = Path(updates.ROOT / 'updaters/cli.sh').read_text()
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'codex'
            binary.write_text('#!/bin/sh\nif [ "$1" = --version ]; then echo codex-cli 1.2.4; else exit 99; fi\n')
            binary.chmod(0o700)
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH'], SHELL='/bin/sh', FLEETLIGHT_EXPECTED_VERSION='1.2.3')
            result = subprocess.run(['/bin/sh', '-c', script], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('UPDATE:current', result.stdout)


if __name__ == '__main__':
    unittest.main()
