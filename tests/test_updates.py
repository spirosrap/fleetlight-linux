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

from fleetlight import probe, update_job, updates
from fleetlight.config import validate
from fleetlight.monitor import issues, linux_update_issues


class UpdateTests(unittest.TestCase):
    def test_restart_receipt_comes_back_on_the_starting_connection(self):
        # The next login is refused once the restart is scheduled, so "start" must carry the result.
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            receipt = update_job.handle({"operation": "start", "id": uuid.uuid4().hex, "kind": "restart", "target": "0.0.0",
                                         "script": "echo FLEETLIGHT_RESTART\necho VERIFY:ok\n",
                                         "worker_source": (Path(update_job.__file__)).read_text()})
        self.assertEqual(receipt["state"], "succeeded")

    def test_other_jobs_return_at_once_without_waiting_for_the_installer(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            ident = uuid.uuid4().hex
            receipt = update_job.handle({"operation": "start", "id": ident, "kind": "system", "target": "0.0.0",
                                         "script": "sleep 3\necho FLEETLIGHT_SYSTEM_UPDATE\n",
                                         "worker_source": (Path(update_job.__file__)).read_text()})
            self.assertEqual(receipt["state"], "queued")
            state_file = Path(home) / ".local/state/fleetlight/update-jobs" / ident / "state.json"
            for _ in range(100):
                if json.loads(state_file.read_text())["state"] not in ("queued", "running"):
                    break
                time.sleep(0.1)

    def test_only_a_long_silent_restart_is_given_up_on(self):
        job = {"kind": "restart", "started_at": 1000}
        silent = {"state": "disconnected"}
        self.assertFalse(updates.restart_unanswered(job, silent, now=1000 + updates.RESTART_RECEIPT_SECONDS))
        self.assertTrue(updates.restart_unanswered(job, silent, now=1001 + updates.RESTART_RECEIPT_SECONDS))
        self.assertFalse(updates.restart_unanswered(job, {"state": "running"}, now=99999))
        self.assertFalse(updates.restart_unanswered({"kind": "system", "started_at": 1000}, silent, now=99999))
        self.assertFalse(updates.restart_unanswered({"kind": "restart"}, silent, now=99999))

    def test_transient_auto_failures_retry_with_backoff_then_daily(self):
        attempted, retries, key = set(), {}, ("tb", "system", "0.0.0", "omarchy 1 -> 2")
        network = update_job.ERRORS["network-failed"]
        for tries, delay in enumerate(updates.AUTO_RETRY_DELAYS, 1):
            updates.record_auto_result(attempted, retries, list(key), "failed", network, now=1000)
            self.assertEqual(retries[key], {"tries": tries, "after": 1000 + delay})
            self.assertIn(key, updates.auto_skipped(attempted, retries, now=1000))
            self.assertNotIn(key, updates.auto_skipped(attempted, retries, now=1000 + delay))
        # Past the listed delays the last one repeats: the update is never given up for good.
        updates.record_auto_result(attempted, retries, key, "failed", network, now=1000)
        self.assertEqual(retries[key]["after"], 1000 + updates.AUTO_RETRY_DELAYS[-1])
        self.assertEqual(attempted, set())

    def test_installer_failures_retry_slowly_and_successes_are_not_repeated(self):
        attempted, retries = set(), {("b",): {"tries": 2, "after": 0}}
        failed = update_job.ERRORS["install-failed"]
        updates.record_auto_result(attempted, retries, ["a"], "failed", failed, now=0)
        self.assertEqual(retries[("a",)], {"tries": 1, "after": updates.AUTO_SLOW_RETRY_DELAYS[0]})
        updates.record_auto_result(attempted, retries, ["a"], "failed", failed, now=0)
        updates.record_auto_result(attempted, retries, ["a"], "failed", failed, now=0)
        self.assertEqual(retries[("a",)], {"tries": 3, "after": updates.AUTO_SLOW_RETRY_DELAYS[-1]})
        updates.record_auto_result(attempted, retries, ["b"], "succeeded", "System packages verified", now=0)
        self.assertEqual((attempted, set(retries)), ({("b",)}, {("a",)}))
        # Another installer was running: soon, not in six hours.
        updates.record_auto_result(attempted, retries, ["c"], "busy", "Another update is running", now=0)
        self.assertEqual(retries[("c",)]["after"], updates.AUTO_RETRY_DELAYS[0])

    def test_failures_only_a_person_can_fix_are_not_retried(self):
        attempted, retries = set(), {}
        updates.record_auto_result(attempted, retries, ["a"], "failed", update_job.ERRORS["rollback-failed"], now=0)
        self.assertEqual((attempted, retries), ({("a",)}, {}))

    def test_package_change_after_the_check_marks_it_stale(self):
        snapshots = {"tb": {"status": "online", "package_activity": {"at": 2000}},
                     "mini": {"status": "online"},
                     "off": {"status": "offline", "package_activity": {"at": 2000}},
                     "done": {"status": "online", "package_activity": {"at": 2000}}}
        checks = {"tb": {"system": {"checked_at": 1500}}, "off": {"system": {"checked_at": 1500}},
                  "done": {"system": {"checked_at": 2100}}}
        self.assertEqual(updates.packages_changed_since_check(snapshots, checks, now=2200), ["tb"])
        self.assertEqual(updates.packages_changed_since_check(snapshots, checks, now=2050), [])  # still settling

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

    def test_failed_system_attempt_does_not_block_the_next_release_of_that_package(self):
        host = {"id": "a", "name": "A", "local": True}
        first = {"state": "available", "latest": "0.0.0", "checked_at": 200, "packages": ["claude-desktop"],
                 "changes": ["claude-desktop 3.1623.3-1 -> 3.1623.4-1"]}
        later = dict(first, changes=["claude-desktop 3.1623.4-1 -> 3.6556.1-1"])
        attempted = {updates.auto_target_key(host, "system", first)}
        self.assertNotIn(updates.auto_target_key(host, "system", later), attempted)
        kind, pending = updates.next_auto_batch([host], {"a": {"status": "online"}}, {"a": {"system": later}}, attempted, now=200)
        self.assertEqual(kind, "system")
        self.assertEqual(pending[0]["host"]["id"], "a")
        # Older checks without versions still key on the package names.
        self.assertEqual(updates.auto_target_key(host, "system", {"packages": ["x", "y"]}), ("a", "system", "x,y"))

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

    def test_manual_restart_runs_the_unconditional_reboot(self):
        host = {'id': 'test', 'name': 'Test', 'local': True}
        sent = []
        with patch.object(updates, 'job_request', side_effect=lambda _, request: sent.append(request) or {}):
            updates.start_job(host, 'restart', {'state': 'available', 'latest': '0.0.0', 'manual': True})
            updates.start_job(host, 'restart', {'state': 'available', 'latest': '0.0.0'})
        self.assertTrue(sent[0]['script'].endswith(' reboot'))
        self.assertTrue(sent[1]['script'].endswith(' restart'))

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

    def test_grokbot_plan_tells_official_release_from_omarchy_package(self):
        linux = {"status": "online", "os": "Linux",
                 "grokbot": {"version": "0.75.8", "provider": "pacman", "candidate": "0.75.8", "status": "current in cached repository"}}
        ahead = updates.grokbot_plan(linux, {"grokbot": {"version": "0.77.0"}})
        self.assertEqual((ahead["state"], ahead["latest"], ahead["packaged"]), ("available", "0.77.0", False))
        self.assertIn("Omarchy still packages 0.75.8", ahead["detail"])
        packaged = dict(linux, grokbot=dict(linux["grokbot"], candidate="0.77.0", status="update available"))
        ready = updates.grokbot_plan(packaged, {"grokbot": {"version": "0.77.0"}})
        self.assertEqual((ready["state"], ready["packaged"]), ("available", True))
        self.assertIn("Linux packages", ready["detail"])
        self.assertEqual(updates.grokbot_plan(linux, {"grokbot": {"version": "0.75.8"}})["state"], "current")
        failed = updates.grokbot_plan(linux, {"grokbot": {"error": "Could not check the official Grok Bot release"}})
        self.assertEqual((failed["state"], failed["detail"]), ("unknown", "Could not check the official Grok Bot release"))
        mac = updates.grokbot_plan({"status": "online", "os": "Darwin", "grokbot": {"version": None, "status": "not detected"}},
                                   {"grokbot": {"version": "0.77.0"}})
        self.assertEqual((mac["state"], mac["detail"]), ("missing", "Not installed"))
        self.assertEqual(updates.grokbot_plan(dict(linux, status="offline"), {})["state"], "offline")

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

    def test_manual_restart_on_a_new_boot_retires_the_request(self):
        checked = {'state': 'available', 'boot_id': 'old-boot', 'detail': 'The operating system requests a restart'}
        self.assertTrue(updates.restart_superseded(checked, {'status': 'online', 'boot_id': 'new-boot'}))
        self.assertTrue(updates.restart_superseded(dict(checked, state='unknown'), {'status': 'online', 'boot_id': 'new-boot'}))
        self.assertFalse(updates.restart_superseded(checked, {'status': 'online', 'boot_id': 'old-boot'}))
        self.assertFalse(updates.restart_superseded(checked, {'status': 'offline', 'boot_id': 'new-boot'}))
        self.assertFalse(updates.restart_superseded(checked, {'status': 'online'}))
        # A verdict recorded without a boot identity can only be cleared by the next check.
        self.assertFalse(updates.restart_superseded({'state': 'available'}, {'status': 'online', 'boot_id': 'new-boot'}))
        self.assertFalse(updates.restart_superseded(dict(checked, state='current'), {'status': 'online', 'boot_id': 'new-boot'}))
        self.assertFalse(updates.restart_superseded(None, {'status': 'online', 'boot_id': 'new-boot'}))
        verified = updates.restart_verified('new-boot')
        self.assertEqual((verified['state'], verified['boot_id']), ('current', 'new-boot'))
        self.assertFalse(updates.restart_superseded(verified, {'status': 'online', 'boot_id': 'third-boot'}))

    def test_system_check_records_the_boot_it_was_observed_on(self):
        host = {'id': 'test', 'name': 'Test', 'local': True}
        payload = {'manager': 'apt', 'packages': [], 'state': 'current', 'detail': '0 package updates',
                   'restart': {'required': True, 'reason': 'The operating system requests a restart', 'boot_id': 'boot-1'}}
        with patch('fleetlight.updates.run_process', return_value=(0, 'FLEETLIGHT_SYSTEM_CHECK=' + json.dumps(payload), '')):
            checked = updates.check_system(host)
        self.assertEqual(checked['restart']['state'], 'available')
        self.assertEqual(checked['restart']['boot_id'], 'boot-1')
        payload['restart'] = {'required': False, 'reason': 'Not Linux'}
        with patch('fleetlight.updates.run_process', return_value=(0, 'FLEETLIGHT_SYSTEM_CHECK=' + json.dumps(payload), '')):
            checked = updates.check_system(host)
        self.assertEqual(checked['restart']['state'], 'current')
        self.assertNotIn('boot_id', checked['restart'])

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

    def test_cli_mise_retries_directly_when_versions_host_lags(self):
        script = Path(updates.ROOT / 'updaters/cli.sh').read_text()
        with tempfile.TemporaryDirectory() as directory:
            shims = Path(directory) / 'mise/shims'
            shims.mkdir(parents=True)
            installed = Path(directory) / 'installed'
            installed.write_text('1.2.2\n')
            codex = shims / 'codex'
            codex.write_text(f'#!/bin/sh\necho codex-cli $(cat {installed})\n')
            codex.chmod(0o700)
            mise = shims / 'mise'
            mise.write_text('#!/bin/sh\n'
                            '[ "$1" = reshim ] && exit 0\n'
                            '[ "$MISE_USE_VERSIONS_HOST" = false ] || exit 1\n'
                            f'echo 1.2.3 > {installed}\n')
            mise.chmod(0o700)
            env = dict(os.environ, PATH=str(shims) + os.pathsep + os.environ['PATH'], SHELL='/bin/sh', FLEETLIGHT_EXPECTED_VERSION='1.2.3')
            result = subprocess.run(['/bin/sh', '-c', script], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('directly from GitHub', result.stdout)
            self.assertIn('ACTIVE_VERSION:1.2.3', result.stdout)
            self.assertIn('UPDATE:ok', result.stdout)

    def test_cli_mise_failing_both_ways_is_a_transient_failure(self):
        script = Path(updates.ROOT / 'updaters/cli.sh').read_text()
        with tempfile.TemporaryDirectory() as directory:
            shims = Path(directory) / 'mise/shims'
            shims.mkdir(parents=True)
            for name, body in (('codex', 'echo codex-cli 1.2.2'), ('mise', 'exit 1')):
                (shims / name).write_text(f'#!/bin/sh\n{body}\n')
                (shims / name).chmod(0o700)
            env = dict(os.environ, PATH=str(shims) + os.pathsep + os.environ['PATH'], SHELL='/bin/sh', FLEETLIGHT_EXPECTED_VERSION='1.2.3')
            result = subprocess.run(['/bin/sh', '-c', script], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('UPDATE:download-failed', result.stdout)
            self.assertIn(update_job.ERRORS['download-failed'], updates.TRANSIENT_FAILURES)

    def test_standalone_update_pins_the_checked_release(self):
        script = Path(updates.ROOT / 'updaters/cli.sh').read_text()
        self.assertIn('CODEX_RELEASE="$target_version"', script)
        self.assertNotIn('"$active_path" update', script)

    def test_claude_same_or_newer_version_never_installs(self):
        script = Path(updates.ROOT / 'updaters/claude.sh').read_text()
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'claude'
            binary.write_text('#!/bin/sh\nif [ "$1" = --version ]; then echo 1.2.4 \\(Claude Code\\); else exit 99; fi\n')
            binary.chmod(0o700)
            env = dict(os.environ, PATH=directory + os.pathsep + os.environ['PATH'], SHELL='/bin/sh', FLEETLIGHT_EXPECTED_VERSION='1.2.3')
            result = subprocess.run(['/bin/sh', '-c', script], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('UPDATE:current', result.stdout)
            self.assertIn('bash -s "$target_version"', script)
            self.assertIn('claude@$target_version', script)
            self.assertIn("grep -q 'mise '", script)

    def test_claude_native_binary_mentioning_mise_is_not_a_mise_launcher(self):
        script = Path(updates.ROOT / 'updaters/claude.sh').read_text()
        with tempfile.TemporaryDirectory() as home:
            local_bin = Path(home) / '.local/bin'
            local_bin.mkdir(parents=True)
            versions = Path(home) / '.local/share/claude/versions'
            versions.mkdir(parents=True)
            native = versions / '1.2.2'
            native.write_bytes(b'\x7fELF' + b'\0' * 16 + b'run mise use' + b'\0' * 16)
            native.chmod(0o700)
            (local_bin / 'claude').symlink_to(native)
            tools = Path(home) / 'tools'
            tools.mkdir()
            # The fake native installer succeeds without touching the network.
            (tools / 'curl').write_text('#!/bin/sh\necho "exit 0"\n')
            (tools / 'curl').chmod(0o700)
            env = dict(os.environ, HOME=home, PATH=str(tools) + os.pathsep + os.environ['PATH'],
                       SHELL='/bin/sh', FLEETLIGHT_EXPECTED_VERSION='1.2.3')
            result = subprocess.run(['/bin/sh', '-c', script], env=env, text=True, capture_output=True)
            self.assertIn('UPDATE_MODE:native', result.stdout, result.stdout + result.stderr)


    def agy_home(self, directory, version='1.2.2', served='1.2.3'):
        """A home folder with Google's own agy in ~/.local/bin, whose `update` moves it to `served`."""
        home = Path(directory)
        local_bin = home / '.local/bin'
        local_bin.mkdir(parents=True)
        installed = home / 'installed'
        installed.write_text(version + '\n')
        (local_bin / 'agy').write_text('#!/bin/sh\n'
                                       f'if [ "$1" = --version ]; then cat {installed}; exit 0; fi\n'
                                       f'if [ "$1" = update ]; then cp "$0" "$0.1795619401735436200.old"; echo {served} > {installed}; exit 0; fi\n'
                                       'exit 99\n')
        (local_bin / 'agy').chmod(0o700)
        return dict(os.environ, HOME=str(home), PATH=str(local_bin) + os.pathsep + os.environ['PATH'], SHELL='/bin/sh')

    def run_agy_updater(self, env, target='1.2.3'):
        return self.run_tool_updater('agy', env, target)

    def run_tool_updater(self, tool, env, target='1.2.3'):
        script = 'tool=' + tool + '\n' + Path(updates.ROOT / 'updaters/cli_tool.sh').read_text()
        return subprocess.run(['/bin/sh', '-c', script], env=dict(env, FLEETLIGHT_EXPECTED_VERSION=target), text=True, capture_output=True)

    def test_agy_native_install_uses_its_own_updater(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_agy_updater(self.agy_home(directory))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for line in ('FLEETLIGHT_AGY_UPDATE', 'BEFORE_VERSION:1.2.2', 'UPDATE_MODE:native', 'ACTIVE_VERSION:1.2.3', 'UPDATE:ok', 'VERIFY:ok'):
                self.assertIn(line, result.stdout.splitlines())
            lines = [line for line in result.stdout.splitlines() if line.startswith(('FLEETLIGHT_', 'ACTIVE_VERSION:', 'VERIFY:', 'UPDATE:'))]
            self.assertEqual(update_job.parse_result('agy', '1.2.3', '', 0, lines), ('succeeded', 'Verified 1.2.3', '1.2.3'))
            # The replaced program the updater leaves behind is removed once the new one is verified.
            self.assertEqual(os.listdir(Path(directory) / '.local/bin'), ['agy'])

    def test_agy_same_or_newer_version_never_installs(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_agy_updater(self.agy_home(directory, version='1.2.4', served='0.0.1'))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('UPDATE:current', result.stdout)
            self.assertIn('ACTIVE_VERSION:1.2.4', result.stdout)

    def test_agy_updater_that_stays_behind_is_not_a_success(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_agy_updater(self.agy_home(directory, served='1.2.2'))
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('UPDATE:verification-failed', result.stdout)
            lines = result.stdout.splitlines()
            self.assertEqual(update_job.parse_result('agy', '1.2.3', '', 1, lines)[0], 'failed')
            self.assertEqual(len(os.listdir(Path(directory) / '.local/bin')), 2)

    def test_agy_under_mise_is_installed_without_running_the_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            shims, data, ran = home / 'mise/shims', home / 'data', home / 'shim-ran'
            shims.mkdir(parents=True)
            old = data / 'installs/antigravity-cli/1.2.2'
            old.mkdir(parents=True)
            (old / 'agy').write_text('#!/bin/sh\n')
            (old / 'agy').chmod(0o700)
            (data / 'installs/antigravity-cli/latest').symlink_to(old)
            # Running the real shim would download the newest release; this one records that it ran.
            (shims / 'agy').write_text(f'#!/bin/sh\ntouch {ran}\nexit 99\n')
            (shims / 'mise').write_text('#!/bin/sh\n'
                                        '[ "$1" = reshim ] && exit 0\n'
                                        '[ "$3" = --yes ] && [ "$4" = antigravity-cli@1.2.3 ] || exit 1\n'
                                        f'mkdir -p {data}/installs/antigravity-cli/1.2.3 && cp {old}/agy {data}/installs/antigravity-cli/1.2.3/agy\n')
            for name in ('agy', 'mise'):
                (shims / name).chmod(0o700)
            env = dict(os.environ, HOME=str(home), MISE_DATA_DIR=str(data), SHELL='/bin/sh',
                       PATH=str(shims) + os.pathsep + os.environ['PATH'])
            result = self.run_agy_updater(env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for line in ('BEFORE_VERSION:1.2.2', 'UPDATE_MODE:mise', 'ACTIVE_VERSION:1.2.3', 'UPDATE:ok'):
                self.assertIn(line, result.stdout.splitlines())
            self.assertFalse(ran.exists())
            self.assertEqual(probe.mise_tool_version('agy', data / 'installs/antigravity-cli'), '1.2.3')

    def test_agy_installed_some_other_way_needs_a_manual_update(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            elsewhere = home / 'opt/bin'
            elsewhere.mkdir(parents=True)
            (elsewhere / 'agy').write_text('#!/bin/sh\necho 1.2.2\n')
            (elsewhere / 'agy').chmod(0o700)
            env = dict(os.environ, HOME=str(home), SHELL='/bin/sh', PATH=str(elsewhere) + os.pathsep + os.environ['PATH'])
            result = self.run_agy_updater(env)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn('UPDATE:unsupported-installation', result.stdout)
            self.assertEqual(probe.tool_method('agy', str(elsewhere / 'agy')), 'unknown')

    def test_agy_status_reads_mise_folder_instead_of_running_the_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'installs/antigravity-cli'
            for name in ('1.5.9', '1.5.17'):
                (root / name).mkdir(parents=True)
                (root / name / 'antigravity').write_text('')
                (root / name / 'antigravity').chmod(0o700)
            (root / '1.6.0').mkdir()  # A download that never finished has no program in it.
            (root / 'latest').symlink_to(root / '1.5.17')
            self.assertEqual(probe.mise_tool_version('agy', root), '1.5.17')
            self.assertIsNone(probe.mise_tool_version('agy', root / 'missing'))
            shim = Path(directory) / 'mise/shims/agy'
            shim.parent.mkdir(parents=True)
            shim.write_text('#!/bin/sh\nexit 99\n')
            shim.chmod(0o700)
            with patch.object(probe, 'tool_executable', return_value=str(shim)), \
                    patch.object(probe, 'command', side_effect=AssertionError('The shim was run')), \
                    patch.dict(os.environ, {'MISE_DATA_DIR': directory}):
                self.assertEqual(probe.tool_status('agy'), ('1.5.17', {'method': 'mise'}))
            with patch.object(probe, 'tool_executable', return_value=None):
                self.assertEqual(probe.tool_status('agy'), (None, {}))

    def test_agy_status_asks_a_native_install_for_its_version(self):
        with tempfile.TemporaryDirectory() as directory:
            native = Path(directory) / '.local/bin/agy'
            native.parent.mkdir(parents=True)
            native.write_bytes(b'\x7fELF' + b'\0' * 16 + b'run mise use' + b'\0' * 16)
            native.chmod(0o700)
            with patch.object(probe.Path, 'home', return_value=Path(directory)), \
                    patch.object(probe, 'tool_executable', return_value=str(native)), \
                    patch.object(probe, 'command', return_value='1.5.16') as command:
                self.assertEqual(probe.tool_status('agy'), ('1.5.16', {'method': 'native'}))
            self.assertEqual(command.call_args.args[0], [str(native), '--version'])

    def test_agy_release_and_plan(self):
        page = 'https://github.com/google-antigravity/antigravity-cli/releases/tag/'
        self.assertEqual(updates.parse_agy_release(page + '1.5.17'), '1.5.17')
        self.assertEqual(updates.parse_agy_release(page + 'v1.6.0'), '1.6.0')
        for address in (page + '1.5', page + '1.5.17/extra', 'https://example.org/releases/tag/1.5.17', None):
            with self.assertRaises(ValueError):
                updates.parse_agy_release(address)
        official = {'agy': {'version': '1.5.17'}}
        plan = lambda **snapshot: updates.tool_plan('agy', {'status': 'online', **snapshot}, official)
        native = plan(agy='1.5.16', agy_installation={'method': 'native'})
        self.assertEqual((native['state'], native['installed'], native['latest'], native['provider']), ('available', '1.5.16', '1.5.17', 'native'))
        self.assertEqual(plan(agy='1.5.17', agy_installation={'method': 'mise'})['state'], 'current')
        # Listed by mise but never downloaded: nothing to update, and Fleetlight does not install it.
        lazy = plan(agy=None, agy_installation={'method': 'mise'})
        self.assertEqual((lazy['state'], lazy['detail']), ('missing', 'Not installed · mise downloads it on first use'))
        self.assertEqual(plan()['detail'], 'Not installed')
        self.assertEqual(plan(agy='1.5.16', agy_installation={'method': 'unknown'})['state'], 'unsupported')
        self.assertEqual(updates.tool_plan('agy', {'status': 'offline', 'agy': '1.5.16'}, official)['state'], 'offline')
        failed = updates.tool_plan('agy', {'status': 'online', 'agy': '1.5.16', 'agy_installation': {'method': 'native'}},
                                  {'agy': {'error': 'Could not check the official Antigravity CLI release'}})
        self.assertEqual((failed['state'], failed['detail']), ('unknown', 'Could not check the official Antigravity CLI release'))

    def test_agy_is_updated_like_the_other_command_line_tools(self):
        host = {'id': 'desk', 'name': 'Desk', 'local': True}
        snapshots = {'desk': {'status': 'online', 'agy': '1.5.16', 'agy_installation': {'method': 'native'}}}
        checks = {'desk': {'agy': updates.tool_plan('agy', snapshots['desk'], {'agy': {'version': '1.5.17'}})}}
        eligible, skipped = updates.batch_candidates([host], snapshots, checks, 'agy')
        self.assertEqual(([item['checked']['latest'] for item in eligible], skipped), (['1.5.17'], []))
        self.assertEqual(updates.next_auto_batch([host], snapshots, checks)[0], 'agy')
        self.assertEqual(updates.auto_target_key(host, 'agy', checks['desk']['agy']), ('desk', 'agy', '1.5.17', ''))
        with patch.object(updates, 'job_request', side_effect=lambda host, request: request) as sent:
            request = updates.start_job(host, 'agy', checks['desk']['agy'])
        self.assertEqual((request['kind'], request['target']), ('agy', '1.5.17'))
        self.assertTrue(request['script'].startswith('tool=agy\n'))
        self.assertIn('FLEETLIGHT_AGY_UPDATE', request['script'])
        self.assertEqual(sent.call_count, 1)
        receipt = {'kind': 'agy', 'log': 'BEFORE_VERSION:1.5.16\nACTIVE_VERSION:1.5.17\n'}
        self.assertEqual(update_job.installation_changes(receipt), ['Antigravity CLI 1.5.16 → 1.5.17'])
        record = {'id': 'a', 'kind': 'agy', 'state': 'succeeded', 'changes': ['Antigravity CLI 1.5.16 → 1.5.17']}
        self.assertEqual(updates.parse_install_history('FLEETLIGHT_HISTORY=' + json.dumps([record]))[0]['kind'], 'agy')
        self.assertEqual(update_job.visible_installs([record], 'Darwin'), [record])
        self.assertIsNone(updates.relevant_job({'kind': 'agy', 'state': 'failed', 'target': '1.5.17'},
                                               {'agy': {'state': 'available', 'installed': '1.5.17'}}))


    def test_grok_bot_is_found_on_a_mac(self):
        import plistlib
        with tempfile.TemporaryDirectory() as directory:
            missing, other, apps = Path(directory) / 'none', Path(directory) / 'other', Path(directory) / 'Applications'
            for folder, identifier in ((other, 'org.example.lookalike'), (apps, 'com.anysphere.sand')):
                (folder / 'Grok Bot.app/Contents').mkdir(parents=True)
                with (folder / 'Grok Bot.app/Contents/Info.plist').open('wb') as stream:
                    plistlib.dump({'CFBundleIdentifier': identifier, 'CFBundleShortVersionString': '1.2.3'}, stream)
            found = probe.grokbot('Darwin', folders=(missing, other, apps))
            self.assertEqual((found['version'], found['provider'], found['status']), ('1.2.3', 'macOS app', 'installed'))
            self.assertEqual(probe.grokbot('Darwin', folders=(missing, other))['status'], 'not detected')
        behind = updates.grokbot_plan({'status': 'online', 'os': 'Darwin', 'grokbot': found}, {'grokbot': {'version': '1.3.0'}})
        self.assertEqual((behind['state'], behind['installed'], behind['latest'], behind['provider']),
                         ('available', '1.2.3', '1.3.0', 'macOS app'))
        self.assertEqual(updates.grokbot_plan({'status': 'online', 'grokbot': found}, {'grokbot': {'version': '1.2.3'}})['state'], 'current')


    def test_opencode_native_install_is_upgraded_to_the_checked_release(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            native = home / '.opencode/bin'
            native.mkdir(parents=True)
            installed, asked = home / 'installed', home / 'asked'
            installed.write_text('opencode v1.2.2\n')
            (native / 'opencode').write_text('#!/bin/sh\n'
                                             f'if [ "$1" = --version ]; then cat {installed}; exit 0; fi\n'
                                             f'if [ "$1" = upgrade ]; then echo "$2" > {asked}; echo "opencode v$2" > {installed}; exit 0; fi\n'
                                             'exit 99\n')
            (native / 'opencode').chmod(0o700)
            env = dict(os.environ, HOME=str(home), SHELL='/bin/sh', PATH=str(native) + os.pathsep + os.environ['PATH'])
            result = self.run_tool_updater('opencode', env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for line in ('FLEETLIGHT_OPENCODE_UPDATE', 'BEFORE_VERSION:1.2.2', 'UPDATE_MODE:native', 'ACTIVE_VERSION:1.2.3', 'UPDATE:ok'):
                self.assertIn(line, result.stdout.splitlines())
            self.assertEqual(asked.read_text().strip(), '1.2.3')
            lines = [line for line in result.stdout.splitlines() if line.startswith(('FLEETLIGHT_', 'ACTIVE_VERSION:', 'VERIFY:', 'UPDATE:'))]
            self.assertEqual(update_job.parse_result('opencode', '1.2.3', '', 0, lines), ('succeeded', 'Verified 1.2.3', '1.2.3'))
            self.assertEqual(update_job.parse_result('agy', '1.2.3', '', 0, lines)[0], 'failed')

    def test_opencode_under_mise_is_read_from_its_npm_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'installs/opencode'
            program = root / '2.4.1/node_modules/.bin/opencode'
            program.parent.mkdir(parents=True)
            program.write_text('#!/bin/sh\n')
            program.chmod(0o700)
            (root / '2.5.0').mkdir()
            (root / 'latest').symlink_to(root / '2.4.1')
            self.assertEqual(probe.mise_tool_version('opencode', root), '2.4.1')
            self.assertEqual(probe.tool_method('opencode', str(Path(directory) / 'mise/installs/opencode/latest/node_modules/.bin/opencode')), 'mise')

    def test_tools_are_looked_up_in_one_shell_start_each(self):
        calls = []

        def command(args, timeout=4):
            calls.append(args)
            found = {'-lc': '/usr/bin/true', '-ic': '/usr/bin/env'}[args[1]]
            return f'a greeting from the shell file\nFLEETLIGHT_PATH:agy=\nFLEETLIGHT_PATH:opencode={found}\nFLEETLIGHT_PATH:other=/usr/bin/true'

        with patch.object(probe, 'command', side_effect=command):
            shells = probe.shell_commands(['agy', 'opencode'])
        self.assertEqual(shells, {'-lc': {'opencode': '/usr/bin/true'}, '-ic': {'opencode': '/usr/bin/env'}})
        self.assertEqual(len(calls), 2)
        self.assertIn('command -v agy', calls[0][2])
        self.assertIn('command -v opencode', calls[0][2])
        # OpenCode trusts the interactive shell first, agy the login shell.
        self.assertEqual(probe.tool_executable('opencode', shells), '/usr/bin/env')
        self.assertEqual(probe.tool_executable('agy', {'-lc': {'agy': '/usr/bin/true'}, '-ic': {'agy': '/usr/bin/env'}}), '/usr/bin/true')

    def test_opencode_plan(self):
        official = {'opencode': {'version': '2.4.1'}}
        plan = lambda **snapshot: updates.tool_plan('opencode', {'status': 'online', **snapshot}, official)
        native = plan(opencode='2.4.0', opencode_installation={'method': 'native'})
        self.assertEqual((native['state'], native['latest'], native['detail']), ('available', '2.4.1', 'Official OpenCode release'))
        self.assertEqual(plan(opencode='2.4.1', opencode_installation={'method': 'mise'})['state'], 'current')
        # A distribution package is brought up to date by the Linux packages update, not by a job of its own.
        packaged = plan(opencode='2.4.0', opencode_installation={'method': 'pacman'})
        self.assertEqual((packaged['state'], packaged['detail']), ('packaged', 'Update available · installs with Linux packages'))
        host = {'id': 'desk', 'name': 'Desk', 'local': True}
        snapshots = {'desk': {'status': 'online'}}
        self.assertEqual(updates.batch_candidates([host], snapshots, {'desk': {'opencode': packaged}}, 'opencode')[0], [])
        self.assertEqual(len(updates.batch_candidates([host], snapshots, {'desk': {'opencode': native}}, 'opencode')[0]), 1)
        with patch.object(updates, 'job_request', side_effect=lambda host, request: request):
            self.assertTrue(updates.start_job(host, 'opencode', native)['script'].startswith('tool=opencode\n'))
        self.assertIn('opencode', updates.AUTO_UPDATE_KINDS)

    def test_grok_bot_on_a_mac_is_installed_by_a_job_and_on_linux_by_the_packages(self):
        official = {'grokbot': {'version': '1.3.0'}}
        mac_app = {'version': '1.2.3', 'provider': 'macOS app', 'candidate': None, 'status': 'installed', 'writable': True,
                   'path': '/Applications/Grok Bot.app'}
        mac = updates.grokbot_plan({'status': 'online', 'os': 'Darwin', 'grokbot': mac_app}, official)
        linux = updates.grokbot_plan({'status': 'online', 'os': 'Linux', 'grokbot': {
            'version': '1.2.3', 'provider': 'pacman', 'candidate': '1.3.0', 'status': 'update available'}}, official)
        self.assertEqual((mac['state'], mac['detail']), ('available', 'Official Grok Bot release'))
        self.assertEqual((linux['state'], linux['detail']), ('available', 'Update available · installs with Linux packages'))
        hosts = [{'id': 'air', 'name': 'Air', 'alias': 'air'}, {'id': 'desk', 'name': 'Desk', 'local': True}]
        snapshots = {'air': {'status': 'online'}, 'desk': {'status': 'online'}}
        checks = {'air': {'grokbot': mac}, 'desk': {'grokbot': linux}}
        eligible, skipped = updates.batch_candidates(hosts, snapshots, checks, 'grokbot')
        self.assertEqual([item['host']['id'] for item in eligible], ['air'])
        self.assertEqual(skipped, [{'name': 'Desk', 'reason': 'updates with Linux packages'}])
        # Unattended updates pick it up, after the command-line tools and ChatGPT.
        self.assertEqual(updates.next_auto_batch(hosts, snapshots, checks)[0], 'grokbot')
        with patch.object(updates, 'job_request', side_effect=lambda host, request: request):
            request = updates.start_job(hosts[0], 'grokbot', mac)
            with self.assertRaises(ValueError):
                updates.start_job(hosts[1], 'grokbot', linux)
        self.assertEqual((request['kind'], request['target']), ('grokbot', '1.3.0'))
        for expected in ('FLEETLIGHT_GROKBOT_UPDATE', 'com.anysphere.sand', 'team=DCNK4UB866', 'spctl --assess', 'restore_previous_app'):
            self.assertIn(expected, request['script'])
        locked = updates.grokbot_plan({'status': 'online', 'grokbot': dict(mac_app, writable=False)}, official)
        self.assertEqual((locked['state'], locked['detail']), ('protected', 'This account cannot replace /Applications/Grok Bot.app'))

    def test_grok_bot_job_results(self):
        done = ['FLEETLIGHT_GROKBOT_UPDATE', 'BEFORE_VERSION:1.2.3', 'AFTER_VERSION:1.3.0', 'RELAUNCH:not-needed', 'UPDATE:ok', 'VERIFY:updated']
        self.assertEqual(update_job.parse_result('grokbot', '1.3.0', '', 0, done), ('succeeded', 'Verified 1.3.0', '1.3.0'))
        closed = [line.replace('not-needed', 'failed') for line in done]
        self.assertEqual(update_job.parse_result('grokbot', '1.3.0', '', 0, closed),
                         ('failed', 'Installed 1.3.0, but Grok Bot did not reopen. Open it manually.', '1.3.0'))
        forged = ['FLEETLIGHT_GROKBOT_UPDATE', 'BEFORE_VERSION:1.2.3', 'UPDATE:signature-invalid', 'VERIFY:failed']
        self.assertEqual(update_job.parse_result('grokbot', '1.3.0', '', 3, forged)[:2], ('failed', update_job.ERRORS['signature-invalid']))
        self.assertEqual(update_job.installation_changes({'kind': 'grokbot', 'log': '\n'.join(done)}), ['Grok Bot 1.2.3 → 1.3.0'])
        # The Mac installer refuses to run anywhere else.
        script = Path(updates.ROOT / 'updaters/grokbot_mac.sh').read_text()
        result = subprocess.run(['/bin/sh', '-c', script], env=dict(os.environ, FLEETLIGHT_EXPECTED_VERSION='1.3.0'), text=True, capture_output=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('UPDATE:unsupported', result.stdout)


    def test_grok_cli_native_install_is_updated_to_the_checked_release(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            downloads, links = home / '.grok/downloads', home / '.grok/bin'
            downloads.mkdir(parents=True)
            links.mkdir()
            asked = home / 'asked'
            # x.ai's layout: the command is a link to the release, and an update points the link at a new one.
            for release in ('1.2.2', '1.2.3'):
                program = downloads / f'grok-{release}-linux-x86_64'
                program.write_text('#!/bin/sh\n'
                                   f'if [ "$1" = --version ]; then echo "grok {release} (abc123) [alpha]"; exit 0; fi\n'
                                   f'if [ "$1" = update ]; then echo "$2 $3" > {asked}; ln -sf ../downloads/grok-$3-linux-x86_64 {links}/grok; exit 0; fi\n'
                                   'exit 99\n')
                program.chmod(0o700)
            (links / 'grok').symlink_to('../downloads/grok-1.2.2-linux-x86_64')
            # A bare environment: the developer's own may point the login shell at a mise wrapper for grok.
            env = {'HOME': str(home), 'SHELL': '/bin/sh', 'PATH': str(links) + os.pathsep + '/usr/bin:/bin'}
            self.assertEqual(probe.tool_method('grok', str(links / 'grok')), 'unknown')  # Not this account's home folder.
            with patch.object(probe.Path, 'home', return_value=home):
                self.assertEqual(probe.tool_method('grok', str(links / 'grok')), 'native')
            result = self.run_tool_updater('grok', env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for line in ('FLEETLIGHT_GROK_UPDATE', 'BEFORE_VERSION:1.2.2', 'UPDATE_MODE:native', 'ACTIVE_VERSION:1.2.3', 'UPDATE:ok'):
                self.assertIn(line, result.stdout.splitlines())
            self.assertEqual(asked.read_text().strip(), '--version 1.2.3')
            lines = [line for line in result.stdout.splitlines() if line.startswith(('FLEETLIGHT_', 'ACTIVE_VERSION:', 'VERIFY:', 'UPDATE:'))]
            self.assertEqual(update_job.parse_result('grok', '1.2.3', '', 0, lines), ('succeeded', 'Verified 1.2.3', '1.2.3'))

    def test_grok_cli_release_and_plan(self):
        self.assertEqual(updates.parse_grok_release('1.0.92\n'), '1.0.92')
        self.assertEqual(updates.parse_grok_release('1.0.89\n1.0.92\n1.0.9\n0.1.151-alpha.2\n'), '1.0.92')
        for text in ('', '<html>Not found</html>', 'latest'):
            with self.assertRaises(ValueError):
                updates.parse_grok_release(text)
        official = {'grok': {'version': '1.0.92'}}
        behind = updates.tool_plan('grok', {'status': 'online', 'grok': '1.0.89', 'grok_installation': {'method': 'native'}}, official)
        self.assertEqual((behind['state'], behind['latest'], behind['detail']), ('available', '1.0.92', 'Official Grok CLI release'))
        lazy = updates.tool_plan('grok', {'status': 'online', 'grok': None, 'grok_installation': {'method': 'mise'}}, official)
        self.assertEqual(lazy['detail'], 'Not installed · mise downloads it on first use')
        host = {'id': 'desk', 'name': 'Desk', 'local': True}
        with patch.object(updates, 'job_request', side_effect=lambda host, request: request):
            self.assertTrue(updates.start_job(host, 'grok', behind)['script'].startswith('tool=grok\n'))
        # Every tool the probe looks for has a job kind, a name and a place in the unattended queue.
        for kind in probe.CLI_TOOLS:
            self.assertIn(kind, update_job.CLI_KINDS)
            self.assertIn(kind, update_job.KIND_NAMES)
            self.assertIn(kind, update_job.MARKERS)
            self.assertIn(kind, updates.AUTO_UPDATE_KINDS)
            self.assertIn(update_job.MARKERS[kind], Path(updates.ROOT / 'updaters/cli_tool.sh').read_text())
        self.assertEqual(update_job.installation_changes({'kind': 'grok', 'log': 'BEFORE_VERSION:1.0.89\nACTIVE_VERSION:1.0.92\n'}),
                         ['Grok CLI 1.0.89 → 1.0.92'])


if __name__ == '__main__':
    unittest.main()
