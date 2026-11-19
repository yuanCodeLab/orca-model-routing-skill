"""Findings of the independent guard review (F1-F5, F13, F14). Hermetic: no real Orca, terminal or worker."""
import copy
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from test_quota_guard import GuardCase, FakeBackend, META, worker, after_worker_stop, qg, snapshot, COMPLETE_HANDOFF, CONFIG
from test_guard_start_unknown import unknown_view, CONFIRMED, SHELL, UNREADABLE

HANDLE = 'term_test'
INC = 'inc_test'
UI = ['• Working (1m 00s • esc to interrupt)', '› Ask Codex to do anything',
      '  GPT-6.1-Sol high · ~/project · task', '  ? for shortcuts       ⚠ 4 warnings · f2 to view']
HANDOFF_FILE = 'ctx_old-handoff.md'
REQUEST = '请求交接'


def terminal(**changes):
    return dict(dict(handle=HANDLE, connected=True, writable=True, orphaned=False, agentIdentity='codex',
                     incarnationId=INC, executionHostId='local'), **changes)


def screen(lines=None, **changes):
    return dict(dict(handle=HANDLE, status='running', source='screen', tail=UI if lines is None else lines), **changes)


def live_view(**terminal_changes):
    return dict(worker(), terminal=terminal(**terminal_changes))


def frames_from_sample():
    """Synthetic running, idle, and exited terminal frames."""
    frames, current = [], None
    for line in (Path(__file__).parent/'fixtures'/'guard_screens_synthetic.log').read_text().splitlines():
        if line.startswith('--- '):
            current = [line]
            frames.append(current)
        elif current is not None:
            current.append(line)
    return [(frame[0], frame[1:]) for frame in frames]


def decide(lines=None, meta=None, registered=None, **screen_changes):
    return qg.codex_screen_decision(terminal() if meta is None else meta, screen(lines, **screen_changes), HANDLE, registered)


class Flow(GuardCase):
    """A dispatch whose mail is stuck, with screen evidence and input under the test's control."""
    def setUp(self):
        super().setUp()
        self.config['quota_policy']['quota_guard']['delivery_fallback'] = dict(enabled=True, wait_seconds=20)
        self.meta = dict(META, context_file=str(self.dir/'context.md'))
        (self.dir/'context.md').write_text('原始任务')
        self.guard = qg.Guard(self.config, self.meta, self.backend, self.dir)
        self.backend.handoff_on_notify = None
        self.backend.views = [live_view()]
        self.probes, self.inputs, self.lookups = [], [], []
        self.answers = [dict(safe=True, reason='Codex 界面', handle=HANDLE)]
        self.delivery_state = 'pending'
        self.on_input = None
        real_send = self.backend.send
        def send(to, body, high=False):
            real_send(to, body, high)
            return dict(messageId='msg_req' if REQUEST in body else 'msg_note')
        self.backend.send = send
        self.backend.delivery_status = self.lookup
        self.backend.codex_running = self.probe
        self.backend.terminal_input = self.enter

    def lookup(self, message_id, handle):
        self.lookups.append((message_id, handle, self.backend.mono))
        return dict(state=self.delivery_state, source='fake')

    def probe(self, view):
        self.probes.append(self.backend.mono)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    def enter(self, handle, text, retry_request=None):
        self.inputs.append(text)
        if self.on_input:
            self.on_input()
        return dict(accepted=True, request_id='12345678-1234-1234-1234-123456789abc')

    def phase(self):
        return json.loads((self.dir/'ctx_old-phase.json').read_text())

    def delivery(self):
        return self.phase()['handoff_delivery']


class StaleReport(GuardCase):
    """F1: a report only counts as an answer to this handoff request."""
    OLD = 'worktree: /tmp/worktree\n上一次交接写的旧内容\nbackground_pids: none\n' + qg.HANDOFF_END + '\n'

    def setUp(self):
        super().setUp()
        self.report = self.dir/HANDOFF_FILE

    def archived(self):
        return [p for p in self.dir.glob('ctx_old-handoff.stale-*')]

    def crash_while_waiting(self, written=COMPLETE_HANDOFF):
        """The guard dies after the worker answered; the phase on disk still says this handoff is open."""
        self.backend.handoff_on_notify = written
        self.backend.views = [worker()]
        real_show, calls = self.backend.show, []
        def show():
            calls.append(1)
            # tick's own read, then the recheck; the first read inside the wait dies.
            if len(calls) >= 3:
                raise OSError('guard crashed')
            return real_show()
        self.backend.show = show
        with self.assertRaises(OSError):
            self.guard.tick(snapshot())
        self.backend.show = real_show
        self.assertEqual(json.loads((self.dir/'ctx_old-phase.json').read_text())['phase'], 'waiting')
        self.backend.events.clear()
        self.backend.sent.clear()

    def test_probe_old_report_never_triggers_a_takeover(self):
        """Report's probe: a preset report made the guard stop and take over with no wait."""
        self.report.write_text(self.OLD)
        self.backend.handoff_on_notify = None
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertAbsent('fail_task', 'start', 'quota_handoff', 'context')
        events = self.backend.events
        self.assertIn('notify:dispatch', events)
        self.assertGreater(events.count('wait'), 5, 'the whole grace period is waited out')
        self.assertLess(events.index('notify:dispatch'), events.index('stop'))
        self.assertIn('没有完整交接报告', self.backend.bodies('run:')[-1])

    def test_old_report_is_kept_under_another_name_before_the_request(self):
        self.report.write_text(self.OLD)
        seen = []
        real = self.backend.send
        def send(to, body, high=False):
            seen.append((to, self.report.exists()))
            real(to, body, high)
        self.backend.send = send
        self.backend.handoff_on_notify = None
        self.guard.tick(snapshot())
        self.assertEqual(seen[0], ('run:run_x', False), 'gone before anyone hears of the handoff')
        self.assertEqual([p.read_text() for p in self.archived()], [self.OLD])

    def test_answer_to_this_request_is_used_and_the_old_text_is_not(self):
        self.report.write_text(self.OLD)
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertIn('已完成 A', self.backend.context_text)
        self.assertNotIn('旧内容', self.backend.context_text)
        self.assertEqual(len(self.archived()), 1)

    def test_scenario_cancelled_handoff_then_rebuilt_guard(self):
        """Report's scenario: cancel with a finished report on disk, then a new guard on the same dispatch."""
        self.backend.handoff_on_notify = None
        def both():
            self.report.write_text(self.OLD)
            self.guard.cancel_path().touch()
        self.backend.on_sleep = both
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertEqual(self.report.read_text(), self.OLD)
        self.backend.on_sleep = None
        self.backend.events.clear()
        self.backend.mono += 300; self.backend.time += 300
        guard = qg.Guard(self.config, META, self.backend, self.dir)
        self.assertEqual(guard.tick(snapshot()), 'blocked')
        self.assertAbsent('fail_task', 'start', 'quota_handoff')
        self.assertEqual([p.read_text() for p in self.archived()], [self.OLD])

    def test_crash_recovery_keeps_the_report_of_the_same_handoff(self):
        self.crash_while_waiting()
        self.assertTrue(self.report.exists())
        guard = qg.Guard(self.config, META, self.backend, self.dir)
        self.backend.handoff_on_notify = None
        self.assertEqual(guard.tick(snapshot()), 'handoff')
        self.assertEqual(self.archived(), [])
        self.assertEqual(self.backend.events.count('wait'), 0)

    def test_crash_recovery_has_a_time_limit(self):
        self.crash_while_waiting()
        self.backend.time += (self.guard.cfg['grace_minutes'] + 5) * 60 + 1
        self.backend.mono += (self.guard.cfg['grace_minutes'] + 5) * 60 + 1
        guard = qg.Guard(self.config, META, self.backend, self.dir)
        self.backend.handoff_on_notify = None
        self.assertEqual(guard.tick(snapshot()), 'blocked')
        self.assertAbsent('fail_task', 'start')
        self.assertEqual([p.read_text() for p in self.archived()], [COMPLETE_HANDOFF])

    def test_recovery_does_not_extend_its_own_limit(self):
        self.crash_while_waiting(written=None)
        first = json.loads((self.dir/'ctx_old-handoff-request.json').read_text())
        guard = qg.Guard(self.config, META, self.backend, self.dir)
        self.backend.handoff_on_notify = None
        self.backend.time += 10
        guard.pool_lock.release()
        self.backend.task_script = []
        self.backend.on_sleep = lambda: guard.cancel_path().touch()
        guard.tick(snapshot())
        self.assertEqual(json.loads((self.dir/'ctx_old-handoff-request.json').read_text()), first)

    def test_cancel_touched_while_the_guard_was_dead_is_honoured(self):
        self.crash_while_waiting()
        self.guard.cancel_path().touch()
        guard = qg.Guard(self.config, META, self.backend, self.dir)
        self.backend.handoff_on_notify = None
        self.assertEqual(guard.tick(snapshot()), 'abandoned')
        self.assertAbsent('stop', 'fail_task', 'start')

    def test_a_concluded_phase_leaves_nothing_to_recover(self):
        for final in qg.FINAL_PHASES:
            with self.subTest(final):
                (self.dir/'ctx_old-phase.json').write_text(json.dumps(dict(phase=final)))
                (self.dir/'ctx_old-handoff-request.json').write_text(json.dumps(dict(until=self.backend.time + 999)))
                self.assertFalse(self.guard.request_still_open())

    def test_no_request_on_record_leaves_nothing_to_recover(self):
        (self.dir/'ctx_old-phase.json').write_text(json.dumps(dict(phase='waiting')))
        self.assertFalse(self.guard.request_still_open())

    def test_report_that_cannot_be_moved_stops_everything_before_any_message(self):
        self.report.write_text(self.OLD)
        with patch.object(qg.os, 'rename', side_effect=OSError('read-only')):
            self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertFalse(self.backend.bodies('dispatch:'))
        self.assertAbsent('stop', 'fail_task', 'start')
        self.assertIn('未通知也未停止 worker', self.backend.bodies('run:')[-1])
        self.assertEqual(self.report.read_text(), self.OLD)


class NoteDelivery(Flow):
    """F2: what the coordinator hears about a note is what is known about it."""
    def setUp(self):
        super().setUp()
        self.note_state = 'pending'
        lookup = self.lookup
        def status(message_id, handle):
            lookup(message_id, handle)
            return dict(state=self.note_state if message_id == 'msg_note' else 'pending', source='fake')
        self.backend.delivery_status = status

    def alert(self):
        return self.backend.bodies('run:')[-1]

    def cancel(self):
        self.backend.on_sleep = lambda: self.guard.cancel_path().touch()
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')

    def rebind(self):
        self.backend.task_script = [dict(status='dispatched', result=None)] * 2
        self.backend.task = dict(status='dispatched', result=None, dispatch_id='ctx_other')
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')

    def test_probe_resume_stuck_in_the_mailbox_is_not_reported_as_received(self):
        self.cancel()
        self.assertEqual(self.backend.bodies('dispatch:')[-1], qg.RESUME_NOTE)
        self.assertNotIn('已收到', self.alert())
        self.assertIn('未确认送达', self.alert())
        self.assertIn('核对终端', self.alert())
        self.assertIn(HANDLE, self.alert())
        self.assertEqual(self.phase()['worker_told_delivery']['state'], 'pending')

    def test_delivered_resume_says_delivered_and_only_that(self):
        self.note_state = 'delivered'
        self.cancel()
        self.assertIn('已送达', self.alert())
        self.assertNotIn('未确认送达', self.alert())
        self.assertNotIn('已收到', self.alert())
        self.assertEqual(self.phase()['worker_told_delivery']['state'], 'delivered')

    def test_hold_stuck_in_the_mailbox_warns_of_a_possible_second_writer(self):
        self.rebind()
        self.assertIn('不要恢复工作', self.backend.bodies('dispatch:')[-1])
        self.assertIn('未确认送达', self.alert())
        self.assertIn('核对终端', self.alert())
        self.assertEqual(self.phase()['worker_told_delivery']['state'], 'pending')

    def test_delivered_hold_says_delivered(self):
        self.note_state = 'delivered'
        self.rebind()
        self.assertIn('已送达', self.alert())
        self.assertNotIn('未确认送达', self.alert())

    def test_receipt_without_an_id_cannot_be_confirmed(self):
        real = self.backend.send
        self.backend.send = lambda to, body, high=False: (real(to, body, high), {})[1]
        self.cancel()
        self.assertIn('未确认送达', self.alert())

    def test_a_lookup_that_fails_cannot_be_confirmed(self):
        self.backend.delivery_status = lambda *a: (_ for _ in ()).throw(RuntimeError('db locked'))
        self.cancel()
        self.assertIn('未确认送达', self.alert())
        self.assertEqual(self.phase()['worker_told_delivery']['state'], 'unknown')

    def test_confirmation_is_bounded_and_adds_no_terminal_input(self):
        marks = []
        real = self.backend.send
        def send(to, body, high=False):
            if body == qg.RESUME_NOTE:
                marks.append(self.backend.mono)
            return real(to, body, high)
        self.backend.send = send
        self.cancel()
        note_lookups = [at for mid, _, at in self.lookups if mid == 'msg_note']
        self.assertGreater(len(note_lookups), 1, 'looked more than once')
        self.assertLessEqual(max(note_lookups) - marks[0], 15)
        self.assertEqual(self.inputs, [])

    def test_late_delivery_inside_the_window_counts(self):
        def status(message_id, handle):
            self.lookups.append((message_id, handle, self.backend.mono))
            late = [1 for mid, _, _ in self.lookups if mid == 'msg_note']
            return dict(state='delivered' if message_id == 'msg_note' and len(late) >= 3 else 'pending', source='fake')
        self.backend.delivery_status = status
        self.cancel()
        self.assertIn('已送达', self.alert())


class TakeoverOutcome(GuardCase):
    """F3: a takeover whose start Orca could not conclude is not reported as finished."""
    def unknown_start(self):
        calls = []
        def start(target, spec):
            calls.append(target)
            self.backend.events.append('start')
            return 0, dict(ok=True, result=dict(dispatchId='ctx_new', taskId='task_x', state='outcome_unknown',
                                                terminal='term_new'))
        self.backend.start = start
        return calls

    def test_probe_message_does_not_say_finished(self):
        self.unknown_start()
        self.assertEqual(self.guard.tick(snapshot()), qg.TAKEOVER_UNCERTAIN)
        body = self.backend.bodies('run:')[-1]
        self.assertNotIn('已完成额度交接', body)
        for part in ('启动结果不确定', 'ctx_new', 'worker-show --dispatch ctx_new', 'term_new', '不要重发'):
            self.assertIn(part, body)

    def test_phase_says_so_and_status_treats_it_as_concluded(self):
        self.unknown_start()
        self.guard.tick(snapshot())
        phase = json.loads((self.dir/'ctx_old-phase.json').read_text())
        self.assertEqual(phase['phase'], qg.TAKEOVER_UNCERTAIN)
        self.assertEqual((phase['new_dispatch'], phase['start_state']), ('ctx_new', 'outcome_unknown'))
        self.assertIn('启动结果不确定', phase['reason'])
        self.assertIn(qg.TAKEOVER_UNCERTAIN, qg.FINAL_PHASES)
        self.assertEqual(qg.interrupted_handoffs(self.dir), [])
        self.assertIsNone(self.guard.phase_limit(qg.TAKEOVER_UNCERTAIN))

    def test_no_second_start_and_task_left_to_orca(self):
        calls = self.unknown_start()
        self.guard.tick(snapshot())
        self.assertEqual(calls, ['opus'])
        self.assertEqual(self.backend.events.count('fail_task'), 1, 'only the write before the start')
        self.assertEqual(self.backend.events.count('stop'), 1, 'only the old worker')

    def test_usage_log_keeps_the_link_and_the_uncertainty(self):
        self.unknown_start()
        self.guard.tick(snapshot())
        self.assertEqual(self.backend.recorded['new_dispatch'], 'ctx_new')
        self.assertEqual(self.backend.recorded['start_state'], 'outcome_unknown')

    def test_undelivered_alert_stays_listed(self):
        self.unknown_start()
        real = self.backend.send
        def send(to, body, high=False):
            if to.startswith('run:') and '启动结果不确定' in body:
                raise RuntimeError('mail down')
            return real(to, body, high)
        self.backend.send = send
        self.backend.send_status = lambda to, body: (_ for _ in ()).throw(RuntimeError('mail down'))
        self.guard.tick(snapshot())
        self.assertEqual([r['dispatch_id'] for r in qg.undelivered_outcomes(self.dir)], ['ctx_old'])

    def test_watch_exits_nonzero_so_it_is_not_counted_as_a_clean_handoff(self):
        self.unknown_start()
        code = qg.watch(self.guard, META, self.backend, lambda: self.config, lambda pol: snapshot())
        self.assertEqual(code, 1)

    def test_any_state_but_ready_is_uncertain(self):
        self.backend.start = lambda target, spec: (0, dict(result=dict(dispatchId='ctx_new', state='booting')))
        self.assertEqual(self.guard.tick(snapshot()), qg.TAKEOVER_UNCERTAIN)
        self.assertNotIn('已完成额度交接', ''.join(self.backend.bodies('run:')))

    def test_guard_knows_the_outcome_before_anything_can_fail(self):
        self.unknown_start()
        real = self.guard.phase
        def phase(name, **extra):
            if name == qg.TAKEOVER_UNCERTAIN:
                raise OSError('disk full')
            return real(name, **extra)
        self.guard.phase = phase
        with self.assertRaises(OSError):
            self.guard.tick(snapshot())
        self.assertEqual(self.guard.finished, qg.TAKEOVER_UNCERTAIN)

    def test_it_still_counts_for_the_pool_spacing(self):
        # A new writer may be running on this pool: the next guard must wait as after any handoff.
        self.unknown_start()
        self.guard.tick(snapshot())
        self.assertFalse(self.guard.pool_lock.acquire(self.backend.clock(), 3600))
        self.addCleanup(self.guard.pool_lock.release)

    def test_ready_receipt_is_still_reported_as_finished(self):
        self.backend.start = lambda target, spec: (0, dict(result=dict(dispatchId='ctx_new', state='ready')))
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertIn('已完成额度交接', self.backend.bodies('run:')[-1])
        self.assertEqual(json.loads((self.dir/'ctx_old-phase.json').read_text())['phase'], 'handoff_done')


class PreRequestRecheck(GuardCase):
    """F4: the checks are read again right before the first message."""
    def setUp(self):
        super().setUp()
        self.backend.handoff_on_notify = COMPLETE_HANDOFF
        self.answers = [CONFIRMED]
        self.probes = []
        self.backend.codex_running = self.probe
        self.during = None
        real = self.backend.fingerprint
        def fingerprint():
            # Taken last in prerequisites: this is the window the review found.
            if self.during:
                action, self.during = self.during, None
                action()
            return real()
        self.backend.fingerprint = fingerprint

    def probe(self, view):
        self.probes.append(view)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return dict(answer)

    def uncertain_start(self):
        self.backend.views = [unknown_view()]
        self.backend.task = dict(status='blocked', result='', dispatch_id=None)

    def assertNothingSent(self):
        self.assertFalse(self.backend.bodies('dispatch:'), 'the worker hears nothing')
        self.assertFalse([b for b in self.backend.bodies('run:') if '交接进行中' in b])
        self.assertAbsent('stop', 'fail_task', 'start')

    def test_probe_task_rebound_during_prerequisites_of_an_uncertain_start(self):
        self.uncertain_start()
        def rebind():
            self.backend.task = dict(status='blocked', result='', dispatch_id='ctx_other')
        self.during = rebind
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertNothingSent()
        self.assertIn('额度交接已放弃', self.backend.bodies('run:')[-1])

    def test_probe_task_concluded_during_prerequisites_of_a_normal_attempt(self):
        def concluded():
            self.backend.task = dict(status='completed', result='有结论')
        self.during = concluded
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertNothingSent()

    def test_probe_worker_stopped_during_prerequisites(self):
        self.uncertain_start()
        self.during = lambda: setattr(self.backend, 'views', [after_worker_stop()])
        self.assertEqual(self.guard.tick(snapshot()), 'done')
        self.assertNothingSent()

    def test_worker_succeeded_during_prerequisites(self):
        self.during = lambda: setattr(self.backend, 'views', [worker('succeeded')])
        self.assertEqual(self.guard.tick(snapshot()), 'done')
        self.assertNothingSent()

    def test_attempt_ended_during_prerequisites(self):
        self.during = lambda: setattr(self.backend, 'views', [worker('stopping')])
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertNothingSent()

    def test_probe_codex_gone_during_prerequisites_only_alerts(self):
        self.uncertain_start()
        self.answers = [CONFIRMED, SHELL]
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertNothingSent()
        self.assertEqual(len(self.probes), 2, 'the screen is read again before the request')
        self.assertIn(SHELL['reason'], self.backend.bodies('run:')[-1])
        self.assertIsNone(self.guard.finished)

    def test_worker_read_failure_sends_nothing(self):
        real, calls = self.backend.show, []
        def show():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('worker-show down')
            return real()
        self.backend.show = show
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertNothingSent()
        self.assertIn('worker-show down', self.backend.bodies('run:')[-1])
        self.assertIsNone(self.guard.finished)

    def test_task_read_failure_sends_nothing(self):
        real, calls = self.backend.task_state, []
        def task_state():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('task-list down')
            return real()
        self.backend.task_state = task_state
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertNothingSent()
        self.assertIn('task-list down', self.backend.bodies('run:')[-1])

    def test_task_turning_blocked_during_prerequisites_waits(self):
        self.during = lambda: setattr(self.backend, 'task', dict(status='blocked', result='', dispatch_id=None))
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertNothingSent()
        self.assertIsNotNone(self.guard.uncertain_since)

    def test_worker_turning_start_unknown_during_prerequisites_needs_evidence(self):
        def flip():
            self.backend.views = [unknown_view()]
            self.backend.task = dict(status='blocked', result='', dispatch_id=None)
        self.during = flip
        self.answers = [SHELL]
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertNothingSent()
        self.assertEqual(len(self.probes), 1)

    def test_blocked_only_after_the_evidence_read_needs_the_next_round(self):
        # F13 m03, second place: this recheck read no screen because the Task was still bound when it looked.
        self.uncertain_start()
        self.during = lambda: setattr(self.backend, 'task_script', [dict(status='dispatched', result='')])
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertNothingSent()
        self.assertEqual(len(self.probes), 1, 'only the read before the lock')

    def test_unchanged_state_goes_on_and_the_baseline_is_the_fresh_read(self):
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertIn('notify:dispatch', self.backend.events)

    def test_recheck_stays_ahead_of_every_message(self):
        order = []
        real_show, real_send = self.backend.show, self.backend.send
        self.backend.show = lambda: (order.append('show'), real_show())[1]
        self.backend.send = lambda to, body, high=False: (order.append('send'), real_send(to, body, high))[1]
        self.guard.tick(snapshot())
        self.assertEqual(order[:3], ['show', 'show', 'send'])


class EvidenceLostInTheWait(Flow):
    """F4: at the end of the grace period an uncertain start with no evidence left is not stopped."""
    def setUp(self):
        super().setUp()
        self.backend.views = [unknown_view()]
        self.backend.task = dict(status='blocked', result='', dispatch_id=None)
        self.answers = [CONFIRMED]
        self.delivery_state = 'delivered'

    def test_probe_codex_gone_at_the_deadline_leaves_the_worker_alone(self):
        confirmed = [CONFIRMED, CONFIRMED]
        self.answers = confirmed + [SHELL]
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertAbsent('stop', 'fail_task', 'start', 'quota_handoff')
        alert = self.backend.bodies('run:')[-1]
        self.assertIn('额度交接已放弃', alert)
        self.assertIn(SHELL['reason'], alert)
        self.assertEqual(self.backend.mono, 1180, 'decided at the deadline, not before')

    def test_unreadable_at_the_deadline_counts_as_lost(self):
        self.answers = [CONFIRMED, CONFIRMED, RuntimeError('boom')]
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertAbsent('stop')

    def test_worker_is_told_to_hold_because_it_may_still_be_there(self):
        self.answers = [CONFIRMED, CONFIRMED, SHELL]
        self.guard.tick(snapshot())
        self.assertIn('不要恢复工作', self.backend.bodies('dispatch:')[-1])
        self.assertIn('守卫没有停止它', self.backend.bodies('run:')[-1])

    def test_evidence_still_good_at_the_deadline_stops_as_before(self):
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertIn('stop', self.backend.events)
        self.assertAbsent('fail_task', 'start')

    def test_a_complete_report_is_itself_evidence(self):
        self.answers = [CONFIRMED, CONFIRMED, SHELL]
        self.backend.handoff_on_notify = COMPLETE_HANDOFF
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(len(self.probes), 2, 'no third read once the report is in')

    def test_ordinary_attempt_is_stopped_without_reading_the_screen_again(self):
        self.backend.views = [live_view()]
        self.backend.task = dict(status='dispatched', result=None)
        self.answers = [SHELL]
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertIn('stop', self.backend.events)


class ScreenJudgement(unittest.TestCase):
    """F5.2: running Codex output is not a shell prompt, and the safe direction still holds."""
    def test_probe_running_codex_output_is_not_a_shell(self):
        for row in ('# 修复计划', '$ git status', 'src/foo/bar.py 100%', '% done', '100%'):
            result = decide([row, *UI])
            self.assertTrue(result['safe'], (row, result))
        # The conservative R1 rule rejects even ordinary output ending in a prompt symbol.
        self.assertFalse(decide(['cost: 3$', *UI])['safe'])

    def test_probe_footer_with_a_percentage(self):
        for footer in (['  GPT-6.1-Sol high · ~/p · 85%', '  ? for shortcuts'],
                       ['  GPT-6.1-Sol high · ~/p · task', '  ? for shortcuts     85% context left'],
                       ['  GPT-6.1-Sol high · ~/p · task', '  ? for shortcuts     85%']):
            result = decide([UI[0], UI[1], *footer])
            self.assertTrue(result['safe'], (footer, result))

    def test_shell_prompt_under_leftover_ui_is_refused(self):
        for prompt in ('project >', 'user@host ~/p>', 'user@host project % ', '❯ ', '$ ', '➜ project ',
                       '/tmp/worktree # ', 'project%', 'project$', 'project#', '%', '#', '$'):
            result = decide([*UI, 'account/read failed during TUI bootstrap', prompt])
            self.assertFalse(result['safe'], prompt)
            result = decide([*UI, prompt])
            self.assertFalse(result['safe'], prompt)

    def test_shell_prompt_drawn_over_the_ui_above_the_composer_is_refused(self):
        for prompt in ('user@host project % ', '❯ ', '➜ project', 'user@host:~/p$', '%', '$', '#'):
            result = decide([prompt, *UI[1:]])
            self.assertFalse(result['safe'], prompt)

    def test_prompt_a_few_rows_above_the_composer_is_refused_too(self):
        # Review F13 m05: looking at only one row is not enough, the crashed frame has several.
        prompt = 'user@host project % '
        for filler in (1, 2, 4):
            rows = [prompt, *['output'] * filler, *UI[1:]]
            self.assertFalse(decide(rows)['safe'], filler)
        self.assertFalse(decide([prompt, *['output'] * 5, *UI[1:]])['safe'], 'every visible row remains evidence')

    def test_the_lowest_composer_is_the_one_judged(self):
        # A submitted message is also drawn with the marker, so one in the scrollback is normal.
        self.assertTrue(decide(['› 修复这个问题', '›', 'output', UI[0], *UI[1:]])['safe'])
        self.assertFalse(decide([*UI, '$ ls', '›', 'output'])['safe'])

    def test_footer_has_to_be_the_lowest_content(self):
        for tail in (['? for shortcuts', 'something printed after'], ['? for shortcuts', '$ ls'],
                     ['GPT-6.1 · ~/p · x']):
            self.assertFalse(decide([UI[0], UI[1], *tail])['safe'], tail)

    def test_every_row_under_the_composer_has_to_be_footer(self):
        for row in ('$ ls', 'done', 'user@host p %', 'gpt-tool --help', 'codex exec'):
            self.assertFalse(decide([UI[0], UI[1], row, UI[3]])['safe'], row)

    def test_footer_has_at_most_three_rows(self):
        model = '  GPT-6.1-Sol high · ~/p · task'
        self.assertTrue(decide([UI[0], UI[1], model, model, UI[3]])['safe'])
        self.assertFalse(decide([UI[0], UI[1], model, model, model, UI[3]])['safe'])

    def test_a_screen_with_no_codex_marker_is_refused(self):
        self.assertFalse(decide(['›', UI[2], UI[3]])['safe'])

    def test_rendered_screens_from_the_synthetic_sample(self):
        frames = frames_from_sample()
        self.assertEqual(len(frames), 15)
        safe = []
        for title, lines in frames:
            result = decide(lines)
            rows = [line for line in lines if line.strip()]
            composer = any(r.strip() in ('›', '› Ask Codex to do anything') for r in rows)
            expected = bool(rows) and composer and rows[-1].strip().startswith('? for shortcuts')
            self.assertEqual(result['safe'], expected, (title, result))
            self.assertNotIn('似乎未运行', result['reason'])
            safe.append(title) if result['safe'] else None
        self.assertEqual(len(safe), 9)

    def test_exited_codex_is_refused_with_a_specific_reason(self):
        exited = [lines for title, lines in frames_from_sample() if title.startswith('--- exited')]
        self.assertEqual(len(exited), 2)
        for lines in exited:
            result = decide(lines)
            self.assertFalse(result['safe'])
            self.assertIn('无法确认 Codex 在运行', result['reason'])
            self.assertIn('shell', result['reason'])

    def test_the_composer_while_input_is_landing_is_not_called_empty(self):
        landing = [lines for _, lines in frames_from_sample() if lines and lines[-1].strip() == 'tab to queue message']
        self.assertEqual(len(landing), 2)
        for lines in landing:
            self.assertFalse(decide(lines)['safe'])

    def test_refusal_wording_only_says_not_running_when_identity_says_so(self):
        refusals = [decide(['$ ']), decide([]), decide(['esc to interrupt', 'unknown prompt']),
                    decide(dict(status='exited') and None, status='exited'), decide(source='stream')]
        for result in refusals:
            self.assertFalse(result['safe'])
            self.assertTrue(result['reason'].startswith('无法确认 Codex 在运行：'), result['reason'])
        other = decide(meta=terminal(agentIdentity='zsh'))
        self.assertEqual(other['reason'], 'Codex 未运行：终端身份是 zsh')
        missing = decide(meta=terminal(agentIdentity=None))
        self.assertTrue(missing['reason'].startswith('无法确认 Codex 在运行：'), missing['reason'])

    def test_draft_still_refuses(self):
        self.assertFalse(decide(draft='user text')['safe'])
        self.assertFalse(decide([UI[0], '› unfinished', UI[2], UI[3]])['safe'])

    def test_screen_of_another_handle_is_refused(self):
        self.assertFalse(decide(handle='term_other')['safe'])


class InputPolling(Flow):
    """F5.1: an unsafe first read does not end the fallback; the latch does not change."""
    def test_probe_timeout_then_normal_inputs_once(self):
        self.answers = [dict(safe=False, reason='无法确认 Codex 在运行：读屏超时', handle=HANDLE),
                        dict(safe=True, reason='Codex', handle=HANDLE)]
        self.guard.tick(snapshot())
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(len(self.probes), 2)
        self.assertTrue(self.delivery()['terminal_input_used'])

    def test_probe_raising_keeps_polling(self):
        self.answers = [RuntimeError('boom'), dict(safe=True, reason='Codex', handle=HANDLE)]
        self.guard.tick(snapshot())
        self.assertEqual(len(self.inputs), 1)

    def test_no_read_or_input_after_the_one_input(self):
        self.guard.tick(snapshot())
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(len(self.probes), 1)

    def test_input_deadline_leaves_the_worker_time_to_write(self):
        self.assertEqual(self.guard.input_until(1000.0), 1120.0, '3 minute grace: last input at 2 minutes')
        self.guard.cfg['grace_minutes'] = 1
        self.assertEqual(self.guard.input_until(1000.0), 1040.0, 'short grace keeps a third of it')

    def test_unsafe_to_the_input_deadline_records_why_and_stops_reading(self):
        reason = '无法确认 Codex 在运行：屏幕底部缺少空输入框'
        self.answers = [dict(safe=False, reason=reason, handle=HANDLE)]
        self.guard.tick(snapshot())
        self.assertEqual(self.inputs, [])
        self.assertLess(max(self.probes), 1120)
        self.assertGreater(len(self.probes), 3, 'read again at each poll')
        delivery = self.delivery()
        self.assertEqual(delivery['skip_reason'], reason)
        self.assertEqual(delivery['screen_checks'], len(self.probes))
        self.assertEqual(delivery['input_deadline_seconds'], 120)
        self.assertEqual(self.backend.mono, 1180)
        self.assertIn(reason, self.backend.bodies('run:')[-1])

    def test_safe_only_after_the_input_deadline_never_inputs(self):
        unsafe = dict(safe=False, reason='无法确认 Codex 在运行：布局不明', handle=HANDLE)
        def probe(view):
            self.probes.append(self.backend.mono)
            return dict(unsafe) if self.backend.mono < 1125 else dict(safe=True, reason='Codex', handle=HANDLE)
        self.backend.codex_running = probe
        self.guard.tick(snapshot())
        self.assertEqual(self.inputs, [])

    def test_polling_does_not_spin(self):
        self.answers = [dict(safe=False, reason='无法确认 Codex 在运行：布局不明', handle=HANDLE)]
        self.guard.tick(snapshot())
        self.assertLessEqual(self.backend.events.count('wait'), 40)

    def test_delivery_while_polling_ends_the_fallback(self):
        self.answers = [dict(safe=False, reason='无法确认 Codex 在运行：布局不明', handle=HANDLE)]
        def slept():
            if self.backend.mono >= 1050:
                self.delivery_state = 'delivered'
        self.backend.on_sleep = slept
        self.guard.tick(snapshot())
        self.assertEqual(self.inputs, [])
        self.assertLessEqual(max(self.probes), 1050)
        self.assertEqual(self.delivery()['skip_reason'], '消息已送达')

    def test_report_while_polling_ends_the_fallback(self):
        self.answers = [dict(safe=False, reason='无法确认 Codex 在运行：布局不明', handle=HANDLE)]
        def slept():
            if self.backend.mono >= 1050:
                (self.dir/HANDOFF_FILE).write_text(COMPLETE_HANDOFF)
        self.backend.on_sleep = slept
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(self.inputs, [])

    def test_latch_from_an_earlier_guard_holds_while_polling(self):
        (self.dir/'ctx_old-terminal-input.json').write_text(json.dumps(dict(terminal_input_used=True)))
        self.answers = [dict(safe=False, reason='无法确认 Codex 在运行：布局不明', handle=HANDLE),
                        dict(safe=True, reason='Codex', handle=HANDLE)]
        self.guard.tick(snapshot())
        self.assertEqual(self.inputs, [])
        self.assertIn('已尝试', self.delivery()['skip_reason'])

    def test_failed_input_is_never_tried_again(self):
        def enter(*a, **k):
            self.inputs.append(a)
            raise subprocess.TimeoutExpired('terminal send', 12)
        self.backend.terminal_input = enter
        self.guard.tick(snapshot())
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(len(self.probes), 1)

    def test_rejected_input_is_never_tried_again(self):
        def enter(*a, **k):
            self.inputs.append(a)
            return dict(accepted=False, request_id=None)
        self.backend.terminal_input = enter
        self.guard.tick(snapshot())
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(len(self.probes), 1)


class InputRechecks(Flow):
    """F13 m08, m10-m13, m02: each check of the last recheck before input, on its own."""
    def setUp(self):
        super().setUp()
        lookup = self.lookup
        self.armed = None
        def status(message_id, handle):
            result = lookup(message_id, handle)
            # Reached only by the final lookup: the earlier recheck has passed by now.
            if self.probes and self.armed:
                action, self.armed = self.armed, None
                action()
            return result
        self.backend.delivery_status = status

    def assertNoInput(self):
        self.assertEqual(self.inputs, [])
        self.assertFalse((self.dir/'ctx_old-terminal-input.json').exists())

    def test_report_written_at_the_last_moment(self):
        self.armed = lambda: (self.dir/HANDOFF_FILE).write_text(COMPLETE_HANDOFF)
        self.guard.tick(snapshot())
        self.assertNoInput()

    def test_input_deadline_passing_at_the_last_moment(self):
        def late():
            self.backend.mono = 1125
        self.armed = late
        self.guard.tick(snapshot())
        self.assertNoInput()

    def test_cancel_at_the_last_moment(self):
        self.armed = lambda: self.guard.cancel_path().touch()
        self.guard.tick(snapshot())
        self.assertNoInput()

    def test_task_moved_at_the_last_moment(self):
        def moved():
            self.backend.task = dict(status='dispatched', result=None, dispatch_id='ctx_other')
        self.armed = moved
        self.guard.tick(snapshot())
        self.assertNoInput()

    def test_succeeded_at_the_last_moment(self):
        # Same terminal, and not one of the ended states: only the success itself can stop this input.
        self.armed = lambda: setattr(self.backend, 'views', [dict(live_view(), projection=dict(outcome='succeeded'))])
        self.guard.tick(snapshot())
        self.assertNoInput()

    def test_attempt_ended_at_the_last_moment(self):
        self.armed = lambda: setattr(self.backend, 'views', [dict(worker('stopping'), terminal=terminal())])
        self.guard.tick(snapshot())
        self.assertNoInput()

    def test_stopped_at_the_last_moment(self):
        self.armed = lambda: setattr(self.backend, 'views', [dict(worker(stopped=True), terminal=terminal())])
        self.guard.tick(snapshot())
        self.assertNoInput()

    def test_process_replaced_during_the_screen_read(self):
        self.answers = [dict(safe=True, reason='Codex', handle=HANDLE, incarnation_id=INC)]
        real = self.probe
        def probe(view):
            result = real(view)
            self.backend.views = [live_view(incarnationId='inc_new')]
            return result
        self.backend.codex_running = probe
        self.guard.tick(snapshot())
        self.assertNoInput()
        self.assertIn('替换', self.delivery()['skip_reason'])

    def test_process_replaced_at_the_last_moment(self):
        self.answers = [dict(safe=True, reason='Codex', handle=HANDLE, incarnation_id=INC)]
        self.armed = lambda: setattr(self.backend, 'views', [live_view(incarnationId='inc_new')])
        self.guard.tick(snapshot())
        self.assertNoInput()

    def test_phase_is_on_disk_before_the_input(self):
        seen = []
        self.on_input = lambda: seen.append(self.delivery())
        self.guard.tick(snapshot())
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]['terminal_input_attempted'])
        self.assertEqual(seen[0]['terminal_handle'], HANDLE)

    def test_latch_is_on_disk_before_the_input(self):
        seen = []
        self.on_input = lambda: seen.append((self.dir/'ctx_old-terminal-input.json').exists())
        self.guard.tick(snapshot())
        self.assertEqual(seen, [True])


class Replies(unittest.TestCase):
    """Backend against scripted Orca replies."""
    def backend(self, runner):
        return qg.Backend(CONFIG, META, runner=runner)

    def replies(self, meta=None, frame=None, show_code=0, read_code=0, show_ok=True, read_ok=True, calls=None):
        def run(args, **kw):
            if calls is not None:
                calls.append(args)
            action = args[2]
            body, code, ok = ((frame or screen(), read_code, read_ok) if action == 'read'
                              else (meta or terminal(), show_code, show_ok))
            return subprocess.CompletedProcess(args, code, json.dumps(dict(ok=ok, result=dict(terminal=body))), '')
        return run

    def view(self, registered=None, **terminal_changes):
        return dict(worker(), terminal=terminal(**terminal_changes),
                    dispatch=dict(status='dispatched', processIncarnation=registered))


class RealBackendReads(Replies):
    """F13 m01, m09, m19, m25."""
    def test_matching_reads_pass(self):
        self.assertTrue(self.backend(self.replies()).codex_running(dict(terminal=terminal()))['safe'])

    def test_replaced_process_compared_with_the_worker_view(self):
        result = self.backend(self.replies()).codex_running(dict(terminal=terminal(incarnationId='inc_earlier')))
        self.assertFalse(result['safe'])
        self.assertIn('替换', result['reason'])

    def test_replaced_process_compared_with_the_registered_one(self):
        result = self.backend(self.replies()).codex_running(self.view('wt@@pty:inc_registered'))
        self.assertFalse(result['safe'])
        self.assertIn('替换', result['reason'])

    def test_failed_show_or_read_refuses(self):
        for changes in (dict(show_code=1), dict(read_code=1), dict(show_ok=False), dict(read_ok=False)):
            result = self.backend(self.replies(**changes)).codex_running(dict(terminal=terminal()))
            self.assertFalse(result['safe'], changes)
            self.assertIn('无法确认 Codex 在运行', result['reason'], changes)

    def test_screen_of_another_handle_refuses(self):
        run = self.replies(frame=screen(handle='term_other'))
        self.assertFalse(self.backend(run).codex_running(dict(terminal=terminal()))['safe'])

    def test_screen_read_uses_screen_mode(self):
        calls = []
        self.backend(self.replies(calls=calls)).codex_running(dict(terminal=terminal()))
        self.assertTrue(any('--screen' in c for c in calls))
        self.assertFalse(any('send' in c for c in calls))

    def test_unsafe_text_never_reaches_orca(self):
        calls = []
        b = self.backend(self.replies(calls=calls))
        for text in ('额度守卫请求交接 请立即读取交接入口 /tmp/a;rm', 'plain words', '', None,
                     qg.delivery_input_text('/tmp/ok.md') + '\n', qg.delivery_input_text('/tmp/ok.md') + ' /tmp/ok.md'):
            with self.assertRaises((ValueError, AttributeError)):
                b.terminal_input(HANDLE, text)
        self.assertEqual(calls, [])

    def test_retry_id_is_dropped_when_orca_says_retry_is_unsafe(self):
        request = '12345678-1234-1234-1234-123456789abc'
        def run(args, **kw):
            return subprocess.CompletedProcess(args, 1, json.dumps(dict(ok=False, error=dict(
                code='transport_error', data=dict(orchestrationRequestId=request, retrySafe=False)))), '')
        self.assertIsNone(self.backend(run).terminal_input(HANDLE, qg.delivery_input_text('/tmp/c.md'))['request_id'])


class Orphaned(Replies):
    """F14: orphaned=true alone is not a refusal, but needs the registered process to match."""
    REGISTERED = 'repo::/tmp/example/wt@@43c9f144:' + INC

    def test_registered_incarnation_parses_the_real_shape(self):
        self.assertEqual(qg.registered_incarnation(dict(dispatch=dict(processIncarnation=self.REGISTERED))), INC)
        for view in ({}, dict(dispatch={}), dict(dispatch=dict(processIncarnation=None)), dict(dispatch=dict(processIncarnation=''))):
            self.assertIsNone(qg.registered_incarnation(view))

    def test_decision_orphaned_with_matching_process_passes(self):
        result = decide(meta=terminal(orphaned=True), registered=INC)
        self.assertTrue(result['safe'], result)
        self.assertEqual(result['incarnation_id'], INC)

    def test_decision_orphaned_without_a_matching_process_refuses(self):
        for registered in (None, '', 'inc_other'):
            result = decide(meta=terminal(orphaned=True), registered=registered)
            self.assertFalse(result['safe'], registered)
            self.assertIn('orphaned', result['reason'])

    def test_any_terminal_without_a_current_process_refuses(self):
        for orphaned in (True, False):
            for meta in (terminal(orphaned=orphaned, incarnationId=None), terminal(orphaned=orphaned, incarnationId='')):
                result = decide(meta=meta, registered=INC if orphaned else None)
                self.assertFalse(result['safe'])
                self.assertIn('incarnationId', result['reason'])

    def test_metadata_of_another_handle_refuses(self):
        result = decide(meta=terminal(handle='term_other'))
        self.assertFalse(result['safe'])
        self.assertIn('句柄', result['reason'])

    def test_decision_orphaned_without_a_current_process_refuses(self):
        self.assertFalse(decide(meta=terminal(orphaned=True, incarnationId=None), registered=INC)['safe'])

    def test_reasons_are_separate(self):
        reasons = [decide(meta=terminal(connected=False), registered=INC)['reason'],
                   decide(meta=terminal(writable=False), registered=INC)['reason'],
                   decide(meta=terminal(orphaned=True), registered=None)['reason'],
                   decide(meta=terminal(incarnationId=None))['reason'],
                   decide(meta=terminal(handle='other'))['reason']]
        self.assertEqual(len(set(reasons)), len(reasons), reasons)
        self.assertIn('未连接', reasons[0])
        self.assertIn('不可写', reasons[1])
        self.assertIn('orphaned', reasons[2])
        for reason in reasons:
            self.assertNotIn('存活证据不足', reason)

    def test_disconnected_or_read_only_refuses_even_with_a_matching_process(self):
        for changes in (dict(connected=False), dict(writable=False), dict(connected=None), dict(writable=None)):
            self.assertFalse(decide(meta=terminal(orphaned=True, **changes), registered=INC)['safe'], changes)

    def test_unknown_orphaned_value_refuses(self):
        for value in (None, 'true', 1):
            self.assertFalse(decide(meta=terminal(orphaned=value), registered=INC)['safe'], value)

    def test_not_orphaned_does_not_need_a_registered_process(self):
        self.assertTrue(decide(registered=None)['safe'])

    def test_backend_passes_with_the_registered_process(self):
        run = self.replies(meta=terminal(orphaned=True))
        self.assertTrue(self.backend(run).codex_running(self.view(self.REGISTERED, orphaned=True))['safe'])

    def test_backend_refuses_when_the_registered_process_differs(self):
        run = self.replies(meta=terminal(orphaned=True))
        result = self.backend(run).codex_running(self.view('wt@@pty:inc_other', orphaned=True))
        self.assertFalse(result['safe'])

    def test_backend_refuses_when_nothing_is_registered(self):
        run = self.replies(meta=terminal(orphaned=True))
        for registered in (None, ''):
            result = self.backend(run).codex_running(self.view(registered, orphaned=True))
            self.assertFalse(result['safe'], registered)
            self.assertIn('orphaned', result['reason'])

    def test_worker_view_alone_is_not_registered_evidence(self):
        """view['terminal'] is read live, so a replaced process would agree with itself."""
        run = self.replies(meta=terminal(orphaned=True))
        self.assertFalse(self.backend(run).codex_running(dict(worker(), terminal=terminal(orphaned=True)))['safe'])

    def test_disconnected_or_read_only_refuses_through_the_backend(self):
        for changes in (dict(connected=False), dict(writable=False)):
            run = self.replies(meta=terminal(orphaned=True, **changes))
            self.assertFalse(self.backend(run).codex_running(self.view(self.REGISTERED))['safe'], changes)


class OrphanedPaths(Flow):
    """F14: both users of the shared screen judgement, through the real read path."""
    REGISTERED = 'repo::/tmp/example/wt@@43c9f144:' + INC

    def use_real_reads(self, meta):
        def run(args, **kw):
            body = screen() if args[2] == 'read' else meta
            return subprocess.CompletedProcess(args, 0, json.dumps(dict(ok=True, result=dict(terminal=body))), '')
        self.backend.codex_running = qg.Backend(CONFIG, META, runner=run).codex_running

    def view(self, base, registered):
        base = copy.deepcopy(base)
        base['terminal'] = terminal(orphaned=True)
        base['worker']['agentTerminalHandle'] = HANDLE
        base['dispatch']['processIncarnation'] = registered
        return base

    def uncertain_start(self, registered):
        self.backend.views = [self.view(unknown_view(), registered)]
        self.backend.task = dict(status='blocked', result='', dispatch_id=None)
        self.use_real_reads(terminal(orphaned=True))

    def test_uncertain_start_with_matching_process_is_handed_off(self):
        self.uncertain_start(self.REGISTERED)
        self.backend.handoff_on_notify = COMPLETE_HANDOFF
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertIn(REQUEST, self.backend.bodies('dispatch:')[0])

    def test_uncertain_start_without_a_match_only_alerts(self):
        for registered in ('wt@@pty:inc_other', None):
            with self.subTest(registered):
                self.setUp()
                self.uncertain_start(registered)
                self.assertEqual(self.guard.tick(snapshot()), 'continue')
                self.assertFalse(self.backend.bodies('dispatch:'))
                self.assertAbsent('stop', 'fail_task', 'start')
                self.assertIn('启动结果不确定', self.backend.bodies('run:')[-1])

    def test_uncertain_start_disconnected_or_read_only_only_alerts(self):
        for changes, word in ((dict(connected=False), '未连接'), (dict(writable=False), '不可写')):
            with self.subTest(changes):
                self.setUp()
                self.uncertain_start(self.REGISTERED)
                self.use_real_reads(terminal(orphaned=True, **changes))
                self.assertEqual(self.guard.tick(snapshot()), 'continue')
                self.assertFalse(self.backend.bodies('dispatch:'))
                self.assertIn(word, self.backend.bodies('run:')[-1])

    def test_fallback_inputs_once_with_matching_process(self):
        self.backend.views = [self.view(worker(), self.REGISTERED)]
        self.use_real_reads(terminal(orphaned=True))
        self.guard.tick(snapshot())
        self.assertEqual(len(self.inputs), 1)

    def test_fallback_never_inputs_without_a_match(self):
        for registered in ('wt@@pty:inc_other', None):
            with self.subTest(registered):
                self.setUp()
                self.backend.views = [self.view(worker(), registered)]
                self.use_real_reads(terminal(orphaned=True))
                self.guard.tick(snapshot())
                self.assertEqual(self.inputs, [])
                self.assertIn('skip_reason', self.delivery())

    def test_fallback_never_inputs_disconnected_or_read_only(self):
        for changes in (dict(connected=False), dict(writable=False)):
            with self.subTest(changes):
                self.setUp()
                self.backend.views = [self.view(worker(), self.REGISTERED)]
                self.use_real_reads(terminal(orphaned=True, **changes))
                self.guard.tick(snapshot())
                self.assertEqual(self.inputs, [])


class EvidenceRoundRules(GuardCase):
    """F13 m03: the locked branch never hands off an uncertain start without this round's evidence."""
    def test_task_turning_blocked_between_evidence_and_lock_waits_for_next_round(self):
        probes = []
        self.backend.codex_running = lambda view: (probes.append(view), dict(CONFIRMED))[1]
        self.backend.views = [unknown_view()]
        # Evidence read: still an ordinary bound Task, so no evidence is collected this round.
        self.backend.task_script = [dict(status='dispatched', result='')]
        self.backend.task = dict(status='blocked', result='', dispatch_id=None)
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertEqual(probes, [])
        self.assertFalse(self.backend.bodies('dispatch:'))
        self.assertAbsent('stop', 'fail_task', 'start', 'attempts')


if __name__ == '__main__':
    unittest.main()
