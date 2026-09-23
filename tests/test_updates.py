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
from fleetlight.monitor import issues, linux_update_issues


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

    def test_next_auto_batch_installs_apps_then_packages_never_restarts(self):
        hosts = [{"id": "a", "name": "A", "local": True}, {"id": "b", "name": "B", "local": True}]
        snapshots = {host["id"]: {"status": "online", "boot_id": "boot"} for host in hosts}
        now = 200
        available = dict(updates.plan("1.0.0", "1.1.0", "standalone"), checked_at=now)
        checks = {
            "a": {
                "cli": dict(available),
                "desktop": dict(updates.plan("1.0.0", "1.1.0", "macos-appcast"), checked_at=now),
                "system": {"state": "available", "latest": "0.0.0", "checked_at": now, "packages": ["linux"]},
                "restart": {"state": "available", "latest": "0.0.0", "checked_at": now, "detail": "kernel"},
            },
            "b": {"system": {"state": "available", "latest": "0.0.0", "checked_at": now, "packages": ["glibc"]}},
        }
        kind, pending = updates.next_auto_batch(hosts, snapshots, checks, now=now)
        self.assertEqual(kind, "cli")
        self.assertEqual([item["host"]["id"] for item in pending], ["a"])
        attempted = {updates.auto_target_key(pending[0]["host"], "cli", pending[0]["checked"])}
        kind, pending = updates.next_auto_batch(hosts, snapshots, checks, attempted, now=now)
        self.assertEqual(kind, "desktop")
        attempted.add(updates.auto_target_key(pending[0]["host"], "desktop", pending[0]["checked"]))
        kind, pending = updates.next_auto_batch(hosts, snapshots, checks, attempted, now=now)
        self.assertEqual(kind, "system")
        self.assertEqual([item["host"]["id"] for item in pending], ["a", "b"])
        attempted.update(updates.auto_target_key(item["host"], "system", item["checked"]) for item in pending)
        kind, pending = updates.next_auto_batch(hosts, snapshots, checks, attempted, now=now)
        self.assertIsNone(kind)
        self.assertEqual(pending, [])

    def test_installation_changes_lists_packages_and_versions(self):
        packages = update_job.installation_changes({
            "kind": "system",
            "changes": ["linux 6.1 -> 6.2"],
            "log": "CHANGED:linux 6.1 -> 6.2\n",
        })
        self.assertEqual(packages, ["linux 6.1 → 6.2"])
        omarchy = """
Package (2)             Old Version  New Version  Net Change  Download Size

omarchy/mise-bin        2026.9.11-1  2026.9.12-1    2.23 MiB      35.89 MiB
extra/gd                         2.3.3-9        0.64 MiB       0.15 MiB

1  aur/spotifast-bin  0.8.0-1 -> 0.9.0-1
upgrading mise-bin...
"""
        self.assertEqual(update_job.package_changes(omarchy), [
            "mise-bin 2026.9.11-1 → 2026.9.12-1",
            "gd → 2.3.3-9",
            "spotifast-bin 0.8.0-1 → 0.9.0-1",
        ])
        upgraded = update_job.installation_changes({
            "kind": "desktop",
            "log": "BEFORE_VERSION:26.915.31029\nAFTER_VERSION:26.915.31945\n" + omarchy,
        })
        self.assertEqual(upgraded[0], "ChatGPT 26.915.31029 → 26.915.31945")
        self.assertIn("spotifast-bin 0.8.0-1 → 0.9.0-1", upgraded)

    def test_history_is_kept_for_every_computer_and_macs_skip_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory) / "abc" 
            job.mkdir()
            (job / "state.json").write_text(json.dumps({
                "id": "a" * 32, "kind": "system", "state": "succeeded", "phase": "System packages verified",
                "finished_at": 100,
            }))
            (job / "output.log").write_text("extra/linux  6.1-1  6.2-1    1.00 MiB\nOld Version  New Version\n")
            # Table header must precede the package row.
            (job / "output.log").write_text("Old Version  New Version\n\nextra/linux        6.1-1  6.2-1    1.00 MiB\n")
            desktop = Path(directory) / "desk"
            desktop.mkdir()
            (desktop / "state.json").write_text(json.dumps({
                "id": "b" * 32, "kind": "desktop", "state": "succeeded", "phase": "Verified",
                "finished_at": 200,
            }))
            (desktop / "output.log").write_text("BEFORE_VERSION:1.0.0\nAFTER_VERSION:1.2.0\n")
            report = update_job.history_report(directory)
            self.assertEqual([item["kind"] for item in report], ["desktop", "system"])
            self.assertEqual(update_job.visible_installs(report, "Darwin"), [report[0]])
            self.assertEqual(len(update_job.visible_installs(report, "Linux")), 2)
        parsed = updates.parse_install_history('noise\nFLEETLIGHT_HISTORY=' + json.dumps(report))
        self.assertEqual(parsed[0]["kind"], "desktop")
        host = {"id": "server", "name": "Server", "alias": "server"}
        with patch("fleetlight.updates.run_process", return_value=(0, "FLEETLIGHT_HISTORY=" + json.dumps(report), "")) as run:
            records = updates.read_install_history(host)
        self.assertTrue(run.call_args.args[0][-1].endswith(" history"))
        self.assertEqual(records[1]["changes"][0], "linux 6.1-1 → 6.2-1")

    def test_unrecorded_mac_install_is_listed_at_its_file_time(self):
        report = [{
            "kind": "desktop", "state": "succeeded", "phase": "Verified 26.911.61220",
            "finished_at": 100, "changes": ["ChatGPT 26.908.70816 → 26.911.61220"],
        }, {
            "kind": "cli", "state": "succeeded", "phase": "Verified 0.153.4",
            "finished_at": 90, "changes": ["Codex CLI 0.153.4"],
        }]
        merged = update_job.merge_current_installs(report, [
            {"kind": "desktop", "version": "26.915.31945", "finished_at": 300},
            {"kind": "cli", "version": "0.155.1", "finished_at": 400},
            {"kind": "cli", "version": "0.153.4", "finished_at": 50},
        ])
        self.assertEqual(merged[0]["changes"], ["Codex CLI 0.153.4 → 0.155.1"])
        self.assertEqual(merged[1]["changes"], ["ChatGPT 26.911.61220 → 26.915.31945"])
        self.assertEqual(merged[0]["finished_at"], 400)

    def test_apt_unpacking_lines_list_old_and_new_versions(self):
        log = """
The following packages will be upgraded:
  ghostscript libexpat1
Unpacking libexpat1:amd64 (2.6.1-2ubuntu0.5) over (2.6.1-2ubuntu0.4)…
Unpacking rsyslog (8.2312.0-3ubuntu9.4) over (8.2312.0-3ubuntu9.3)…
Unpacking hello (1.0)…
"""
        self.assertEqual(update_job.package_changes(log), [
            "libexpat1 2.6.1-2ubuntu0.4 → 2.6.1-2ubuntu0.5",
            "rsyslog 8.2312.0-3ubuntu9.3 → 8.2312.0-3ubuntu9.4",
            "hello → 1.0",
        ])

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
        self.assertEqual(linux_update_issues({'system': {'state': 'available', 'detail': '2 package updates'}}),
                         ['2 package updates'])
        self.assertEqual(linux_update_issues({'system': {'state': 'current'}}), [])
        with self.assertRaises(ValueError):
            validate({'version': 1, 'hosts': [{'id':'test', 'name':'Test', 'local':True, 'optional_services':['unknown']}]})

    def test_macos_release_build(self):
        self.assertEqual(updates.plan('26.1.2', '26.1.2', 'macos', build='124', installed_build='123')['state'], 'available')
        with self.assertRaises(ValueError):
            updates.parse_appcast('<rss><channel><item><enclosure url="https://example.org/app.zip"/></item></channel></rss>')

    def test_failed_job_hides_once_the_release_is_current(self):
        last = {"state": "failed", "kind": "desktop", "target": "26.911.61220",
                "phase": "The package manager reported a failure. Inspect the update log before retrying."}
        current = {"desktop": {"state": "current", "installed": "26.911.61220", "latest": "26.911.61220"}}
        self.assertIsNone(updates.relevant_job(last, current))
        pending = {"desktop": {"state": "available", "installed": "26.908.70816", "latest": "26.911.61220"}}
        self.assertEqual(updates.relevant_job(last, pending), last)
        self.assertEqual(updates.relevant_job({"state": "succeeded", "kind": "desktop", "phase": "Verified 26.911.61220"}, current)["state"], "succeeded")

    def test_system_check_timeout_explains_what_to_retry(self):
        host = {'id': 'test', 'name': 'Test', 'local': True}
        with patch('fleetlight.updates.run_process', side_effect=TimeoutError):
            checked = updates.check_system(host)
        for kind in ('system', 'restart'):
            self.assertEqual(checked[kind]['state'], 'unknown')
            self.assertIn('timed out', checked[kind]['detail'])
            self.assertIn('package-manager or network', checked[kind]['detail'])

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

    def test_standalone_update_pins_the_checked_release(self):
        script = Path(updates.ROOT / 'updaters/cli.sh').read_text()
        self.assertIn('CODEX_RELEASE="$target_version"', script)
        self.assertNotIn('"$active_path" update', script)


if __name__ == '__main__':
    unittest.main()
