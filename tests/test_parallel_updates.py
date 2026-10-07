import json
from pathlib import Path
import tempfile
import threading
import time
from gi.repository import GLib
from types import SimpleNamespace, MethodType
import unittest
from unittest.mock import Mock, patch

from fleetlight.app import Fleetlight
from fleetlight import updates


def candidate(i, local=False):
    return {'host': {'id': f'host{i}', 'name': f'Computer {i}', 'local': local,
                     'alias': f'host{i}', 'services': []},
            'checked': {'latest': '1.2.3', 'state': 'available'}}


def controller(path, pending=(), kind='desktop', automatic=True):
    c = SimpleNamespace(active_jobs={}, job_polls=set(), last_jobs={}, snapshots={},
                        app_updates={}, auto_attempted=set(), update_history_expanded={},
                        pending_restarts={}, busy=False, update_checks_running=False,
                        journal_path=path, refresh_button=Mock(), spinner=Mock(),
                        toast=Mock(), render_detail=Mock(), render_batch=Mock(),
                        check=Mock(), refresh_install_history=Mock(), refresh_tray=Mock())
    c.batch = {'kind': kind, 'pending': list(pending), 'results': [], 'total': len(pending),
               'running': True, 'automatic': automatic}
    for name in ('job_for_host', 'persist_jobs', 'begin_update', 'advance_batch',
                 'receive_job', 'watch_job', 'cancel_batch'):
        setattr(c, name, MethodType(getattr(Fleetlight, name), c))
    c.configuration = {'auto_updates': False}
    return c


class ParallelUpdates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'journal.json'
        self.thread = patch('fleetlight.app.threading.Thread')
        self.thread_mock = self.thread.start()
        self.addCleanup(self.thread.stop)
        self.timer = patch('fleetlight.app.GLib.timeout_add_seconds')
        self.timer_mock = self.timer.start()
        self.addCleanup(self.timer.stop)

    def finish(self, c, ident, state='succeeded'):
        c.receive_job(ident, {'id': ident, 'state': state, 'phase': state})

    def test_three_start_and_out_of_order_completion_refills_immediately(self):
        c = controller(self.path, [candidate(i) for i in range(7)])
        c.advance_batch()
        self.assertEqual(len(c.active_jobs), 3)
        self.assertEqual(len(c.batch['pending']), 4)
        self.assertEqual(self.thread_mock.call_count, 3)
        initial = list(c.active_jobs)
        # The third computer completes first, without waiting for the first.
        self.finish(c, initial[2])
        self.assertEqual(len(c.active_jobs), 3)
        self.assertEqual(len(c.batch['pending']), 3)
        self.assertIn(initial[0], c.active_jobs)
        self.assertIn(initial[1], c.active_jobs)
        saved = json.loads(self.path.read_text())
        self.assertEqual(len(saved['active_jobs']), 3)
        while c.active_jobs:
            self.finish(c, next(reversed(c.active_jobs)))
        self.assertEqual(len(c.batch['results']), 7)
        self.assertEqual(len({x['host'] for x in c.batch['results']}), 7)
        self.assertFalse(c.batch['running'])
        c.check.assert_called_once()

    def test_failed_manual_batch_drains_in_flight_without_starting_more(self):
        c = controller(self.path, [candidate(i) for i in range(6)], automatic=False)
        c.advance_batch()
        ident = next(iter(c.active_jobs))
        self.finish(c, ident, 'failed')
        self.assertEqual(len(c.active_jobs), 2)
        self.assertEqual(c.batch['pending'], [])
        while c.active_jobs:
            self.finish(c, next(iter(c.active_jobs)))
        self.assertEqual(len(c.batch['results']), 3)
        self.assertEqual(self.thread_mock.call_count, 3)

    def test_automatic_batch_continues_after_one_computer_fails(self):
        c = controller(self.path, [candidate(i) for i in range(4)])
        c.advance_batch()
        self.finish(c, next(iter(c.active_jobs)), 'failed')
        self.assertEqual(len(c.active_jobs), 3)
        self.assertEqual(self.thread_mock.call_count, 4)
        self.assertEqual(len(c.auto_attempted), 1)

    def test_cancellation_keeps_running_installers_and_starts_no_more(self):
        c = controller(self.path, [candidate(i) for i in range(6)])
        c.advance_batch()
        c.cancel_batch()
        self.assertEqual(len(c.active_jobs), 3)
        self.assertEqual(c.batch['pending'], [])
        while c.active_jobs:
            self.finish(c, next(iter(c.active_jobs)))
        self.assertEqual(self.thread_mock.call_count, 3)

    def test_receipts_and_late_callbacks_cannot_complete_another_job(self):
        c = controller(self.path, [candidate(i) for i in range(4)])
        c.advance_batch()
        a, b, _ = list(c.active_jobs)
        c.receive_job(a, {'id': b, 'state': 'succeeded'})
        self.assertEqual(len(c.active_jobs), 3)
        self.assertEqual(c.batch['results'], [])
        self.finish(c, a)
        before = json.loads(json.dumps(c.active_jobs))
        self.finish(c, a)
        self.assertEqual(c.active_jobs, before)
        self.assertEqual(len(c.batch['results']), 1)

    def test_restart_batches_remain_sequential_with_controller_last(self):
        c = controller(self.path, [candidate(0), candidate(1), candidate(2, local=True)], kind='restart')
        c.advance_batch()
        self.assertEqual(len(c.active_jobs), 1)
        for expected in range(3):
            ident, job = next(iter(c.active_jobs.items()))
            self.assertEqual(job['host']['id'], f'host{expected}')
            self.finish(c, ident)
        self.assertEqual(len(c.pending_restarts), 3)

    def test_one_job_per_host_and_capacity_guard(self):
        c = controller(self.path, [candidate(0), candidate(0), candidate(1), candidate(2)])
        c.advance_batch()
        self.assertEqual({j['host']['id'] for j in c.active_jobs.values()}, {'host0', 'host1', 'host2'})
        self.assertEqual(len(c.batch['pending']), 1)
        self.assertIsNone(c.begin_update(candidate(0)['host'], 'cli', candidate(0)['checked'], from_batch=True))
        self.assertIsNone(c.begin_update(candidate(4)['host'], 'desktop', candidate(4)['checked'], from_batch=True))
        self.assertEqual(len(c.active_jobs), 3)

    def test_no_dispatch_if_recovery_journal_cannot_be_saved(self):
        c = controller(self.path, [candidate(0)])
        c.persist_jobs = Mock(side_effect=OSError('disk full'))
        c.advance_batch()
        self.assertEqual(c.active_jobs, {})
        self.assertEqual(len(c.batch['pending']), 1)
        self.assertFalse(c.batch['running'])
        self.thread_mock.assert_not_called()

    def test_disconnected_computer_retains_slot_and_other_completions_continue(self):
        c = controller(self.path, [candidate(i) for i in range(4)])
        c.advance_batch()
        a, b, _ = list(c.active_jobs)
        c.receive_job(a, {'id': a, 'state': 'disconnected'})
        self.timer_mock.assert_called_with(10, c.watch_job, a)
        self.finish(c, b)
        self.assertIn(a, c.active_jobs)
        self.assertEqual(len(c.active_jobs), 3)
        self.assertEqual(self.thread_mock.call_count, 4)

    def test_recovery_polls_each_computer_and_fills_free_slots(self):
        c = controller(self.path, [candidate(i) for i in range(4)])
        c.advance_batch()
        saved = json.loads(self.path.read_text())
        recovered = controller(self.path, saved['batch']['pending'])
        recovered.batch = saved['batch']
        recovered.active_jobs = updates.restore_active_jobs(saved)
        recovered.watch_job()
        recovered.watch_job()  # Do not issue a second poll while one is outstanding.
        self.assertEqual(len(recovered.job_polls), 3)
        self.assertEqual(self.thread_mock.call_count, 6)
        ident = next(iter(recovered.active_jobs))
        self.finish(recovered, ident)
        self.assertEqual(len(recovered.active_jobs), 3)
        self.assertEqual(self.thread_mock.call_count, 7)

    def test_legacy_journal_recovery_keeps_original_id_and_installer(self):
        legacy = {'id': 'a' * 32, 'host': candidate(0)['host'], 'kind': 'desktop', 'state': 'running'}
        jobs = updates.restore_active_jobs({'active_job': legacy})
        self.assertEqual(jobs, {'a' * 32: legacy})
        c = controller(self.path, [candidate(1), candidate(2), candidate(3)])
        c.active_jobs = jobs
        c.advance_batch()
        self.assertEqual(len(c.active_jobs), 3)
        self.assertEqual(c.active_jobs['a' * 32], legacy)
        self.assertEqual(self.thread_mock.call_count, 2)

    def test_corrupt_or_duplicate_recovery_records_rejected(self):
        job = {'id': 'a' * 32, 'host': candidate(0)['host']}
        with self.assertRaises(ValueError):
            updates.restore_active_jobs({'active_jobs': {'z' * 32: job}})
        with self.assertRaises(ValueError):
            updates.restore_active_jobs({'active_jobs': {'a' * 32: job, 'b' * 32: dict(job, id='b' * 32)}})

    def test_real_threads_and_main_loop_overlap_three_jobs(self):
        self.thread.stop()
        c = controller(self.path, [candidate(i) for i in range(7)])
        lock = threading.Lock()
        counters = {'running': 0, 'peak': 0}
        def harmless_update(host, kind, checked, ident):
            with lock:
                counters['running'] += 1
                counters['peak'] = max(counters['peak'], counters['running'])
            time.sleep(0.08)
            with lock:
                counters['running'] -= 1
            return {'id': ident, 'state': 'succeeded', 'phase': 'Verified'}
        with patch('fleetlight.app.updates.start_job', side_effect=harmless_update):
            c.advance_batch()
            context = GLib.MainContext.default()
            deadline = time.monotonic() + 3
            while c.active_jobs and time.monotonic() < deadline:
                while context.pending():
                    context.iteration(False)
                time.sleep(0.005)
        self.assertFalse(c.active_jobs)
        self.assertEqual(counters['peak'], 3)
        self.assertEqual(len(c.batch['results']), 7)
        self.assertEqual(len(c.last_jobs), 7)

    def test_all_update_types_use_three_slots(self):
        for kind in ('cli', 'claude', 'desktop', 'system'):
            with self.subTest(kind=kind):
                c = controller(self.path, [candidate(i) for i in range(5)], kind=kind)
                c.advance_batch()
                self.assertEqual(len(c.active_jobs), 3)


if __name__ == '__main__':
    unittest.main()
