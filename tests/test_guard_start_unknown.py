"""A start Orca recorded as outcome_unknown: hermetic, no real Orca, terminal or worker."""
import copy
import json
import subprocess
import unittest

from test_quota_guard import GuardCase, META, worker, qg, snapshot, provider, CONFIG

HANDLE = 'term_unknown'
REAL_ERROR = "Dispatch input (text and Enter) was written, but codex's turn start was not observed (waited up to 30s)."
CONFIRMED = dict(safe=True, reason='当前 Codex 空输入框与页脚已确认，输入行不是 shell', handle=HANDLE, incarnation_id='inc_1')
SHELL = dict(safe=False, reason='无法确认 Codex 在运行：屏幕最底部像 shell 提示符', handle=HANDLE)
UNREADABLE = dict(safe=False, reason='无法确认 Codex 在运行：读屏失败：TimeoutExpired', handle=HANDLE)


def unknown_view():
    """Field shape of a real worker-show; a start_unknown dispatch has not left pending."""
    view = worker('start_unknown', dispatch='pending')
    view['worker'].update(stage='turn_start_unobserved', agentTerminalHandle=HANDLE, lastError=REAL_ERROR, effects=[
        dict(kind='dispatch_input', role='agent', id=HANDLE, state='accepted'),
        dict(kind='dispatch_input', role='agent', id=HANDLE, state='turn_unobserved')])
    view['terminal'] = dict(handle=HANDLE, incarnationId='inc_1', connected=True, writable=True, orphaned=False,
                            agentIdentity='codex')
    return view


class Case(GuardCase):
    def setUp(self):
        super().setUp()
        self.backend.views = [unknown_view()]
        self.backend.task = dict(status='blocked', result='', dispatch_id=None)
        self.evidence = [CONFIRMED]
        self.probes = []
        self.backend.codex_running = self.probe

    def probe(self, view):
        self.probes.append(view)
        answer = self.evidence.pop(0) if len(self.evidence) > 1 else self.evidence[0]
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    def highs(self):
        return [body for _, body, high in self.backend.sent if high]

    def assertUntouched(self):
        self.assertFalse(self.backend.bodies('dispatch:'), 'the worker must hear nothing')
        self.assertAbsent('stop', 'fail_task', 'start', 'quota_handoff', 'context')
        self.assertIsNone(self.guard.finished)


class Classification(Case):
    def test_not_ours_names_each_case_by_orca_record(self):
        mine = self.guard.meta['dispatch_id']
        cases = [
            (unknown_view(), dict(status='blocked', result='', dispatch_id=None), qg.START_UNKNOWN),
            (unknown_view(), dict(status='blocked', result=None, dispatch_id=''), qg.START_UNKNOWN),
            (unknown_view(), dict(status='blocked', result=None, dispatch_id=mine), qg.START_UNKNOWN),
            # A retry reopens the Task with the guard's own note still on it.
            (unknown_view(), dict(status='blocked', result=qg.HANDOFF_RESULT, dispatch_id=None), qg.START_UNKNOWN),
            (worker(), dict(status='blocked', result='', dispatch_id=None), 'uncertain'),
            (unknown_view(), dict(status='dispatched', result='', dispatch_id=mine), None),
        ]
        for view, task, expected in cases:
            self.assertEqual(self.guard.not_ours(view, task), expected, (view['worker']['state'], task))

    def test_foreign_dispatch_or_conclusion_is_never_ours(self):
        for task in (dict(status='blocked', result='', dispatch_id='ctx_other'),
                     dict(status='blocked', result='有结论', dispatch_id=None),
                     dict(status='failed', result='cancelled', dispatch_id=None)):
            why = self.guard.not_ours(unknown_view(), task)
            self.assertNotIn(why, (None, 'uncertain', qg.START_UNKNOWN), task)


class Confirmed(Case):
    def test_running_worker_gets_the_ordinary_handoff(self):
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(len(self.probes), 2, 'read in the round, then again right before the request')
        self.assertIn('请求交接', self.backend.bodies('dispatch:')[0])
        events = self.backend.events
        self.assertLess(events.index('notify:dispatch'), events.index('stop'))
        self.assertLess(events.index('stop'), events.index('fail_task'))
        self.assertLess(events.index('fail_task'), events.index('start'))

    def test_task_row_naming_this_dispatch_works_the_same(self):
        self.backend.task = dict(status='blocked', result=None, dispatch_id='ctx_old')
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')

    def test_evidence_reaches_the_phase_record(self):
        self.guard.tick(snapshot())
        phase = json.loads((self.dir/'ctx_old-phase.json').read_text())
        self.assertEqual(phase['start_unknown']['evidence'], CONFIRMED['reason'])
        self.assertEqual(phase['start_unknown']['stage'], 'turn_start_unobserved')

    def test_retried_attempt_with_the_guards_note_is_handed_off(self):
        self.backend.task = dict(status='blocked', result=qg.HANDOFF_RESULT, dispatch_id=None)
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')

    def test_no_report_stops_and_leaves_the_task_alone(self):
        self.backend.handoff_on_notify = None
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertIn('stop', self.backend.events)
        self.assertAbsent('fail_task', 'start', 'quota_handoff')
        self.assertIn('没有完整交接报告', self.backend.bodies('run:')[-1])

    def test_failed_prerequisite_leaves_the_worker_uninformed_and_is_retried(self):
        self.backend.target = None
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertUntouched()
        self.assertIn('无其他账户池', self.highs()[-1])
        self.backend.target = 'opus'
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')

    def test_worker_done_during_the_wait_cancels_the_switch(self):
        self.backend.handoff_on_notify = None
        def finished():
            self.backend.views = [worker('succeeded')]
        self.backend.on_sleep = finished
        self.assertEqual(self.guard.tick(snapshot()), 'done')
        self.assertAbsent('stop', 'fail_task', 'start')

    def test_cancel_during_the_wait_tells_the_worker_to_resume(self):
        self.backend.handoff_on_notify = None
        self.backend.on_sleep = lambda: self.guard.cancel_path().touch()
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertEqual(self.backend.bodies('dispatch:')[-1], qg.RESUME_NOTE)
        self.assertAbsent('stop', 'fail_task', 'start')

    def test_someone_binding_the_task_during_the_wait_abandons(self):
        self.backend.handoff_on_notify = None
        def taken():
            self.backend.task = dict(status='dispatched', result='', dispatch_id='ctx_other')
        self.backend.on_sleep = taken
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertAbsent('stop', 'fail_task', 'start')


class NotConfirmed(Case):
    def test_shell_alerts_in_the_trigger_round_and_changes_nothing(self):
        self.evidence = [SHELL]
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertUntouched()
        self.assertAbsent('attempts', 'target')
        self.assertEqual(len(self.highs()), 1)
        for part in ('启动结果不确定', 'turn_start_unobserved', SHELL['reason'], 'ctx_old'):
            self.assertIn(part, self.highs()[0])
        self.assertEqual(self.backend.task['status'], 'blocked')

    def test_alert_does_not_repeat_while_the_condition_lasts(self):
        self.evidence = [SHELL, UNREADABLE, SHELL]
        for _ in range(5):
            self.backend.mono += 901
            self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertEqual(len(self.highs()), 1)
        self.assertEqual(len(self.probes), 5, 'evidence is read again every round')
        self.assertUntouched()

    def test_no_fifteen_minute_uncertain_alert_on_top(self):
        self.evidence = [SHELL]
        self.guard.tick(snapshot())
        self.backend.mono += 1000
        self.guard.tick(snapshot())
        self.assertFalse([b for b in self.highs() if '保护暂时无法生效' in b])
        self.assertIsNone(self.guard.uncertain_since)

    def test_unreadable_evidence_then_recovered_goes_on_to_handoff(self):
        self.evidence = [UNREADABLE, CONFIRMED]
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertUntouched()
        self.assertEqual(len(self.highs()), 1)
        self.assertIn(UNREADABLE['reason'], self.highs()[0])
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertIn('请求交接', self.backend.bodies('dispatch:')[0])

    def test_probe_raising_counts_as_no_evidence(self):
        self.evidence = [RuntimeError('boom'), CONFIRMED]
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertUntouched()
        self.assertEqual(len(self.highs()), 1)
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')

    def test_undelivered_alert_is_tried_again_next_round_then_stops(self):
        self.evidence = [SHELL]
        real, attempts = self.backend.send, []
        def send(to, body, high=False):
            attempts.append(body)
            if len(attempts) <= 2:
                raise RuntimeError('mail down')
            return real(to, body, high)
        self.backend.send = send
        self.backend.send_status = lambda to, body: send(to, body, True)
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertEqual(len(self.highs()), 1)

    def test_new_trigger_after_it_cleared_alerts_again(self):
        self.evidence = [SHELL]
        self.guard.tick(snapshot())
        self.guard.tick(snapshot(used=10))
        self.guard.tick(snapshot())
        self.assertEqual(len(self.highs()), 2)

    def test_alert_does_not_wait_behind_another_guards_pool_lock(self):
        self.evidence = [SHELL]
        other = qg.HandoffLock(self.dir, 'codex')
        self.assertTrue(other.acquire(self.backend.time, 120))
        self.addCleanup(other.release)
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertEqual(len(self.highs()), 1)
        self.assertUntouched()

    def test_confirmed_but_pool_busy_waits_quietly_for_the_next_round(self):
        other = qg.HandoffLock(self.dir, 'codex')
        self.assertTrue(other.acquire(self.backend.time, 120))
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertFalse(self.highs())
        self.assertUntouched()
        other.release()
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')

    def test_unreadable_task_alerts_as_a_failed_check_and_retries(self):
        real, failures = self.backend.task_state, [RuntimeError('task-list down')]
        def task_state():
            if failures:
                raise failures.pop()
            return real()
        self.backend.task_state = task_state
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertUntouched()
        self.assertEqual(self.probes, [])
        self.assertIn('task-list down', self.highs()[0])
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')


class OtherBlocked(Case):
    """Not recorded as an uncertain start: the behaviour from before this change."""
    def setUp(self):
        super().setUp()
        self.backend.views = [worker()]

    def test_waits_fifteen_minutes_without_looking_for_evidence(self):
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertFalse(self.highs())
        self.backend.mono += 901
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertEqual(len(self.highs()), 1)
        self.assertIn('保护暂时无法生效', self.highs()[0])
        self.assertEqual(self.probes, [])
        self.assertUntouched()

    def test_recovers_when_the_task_becomes_active(self):
        self.guard.tick(snapshot())
        self.backend.task = dict(status='dispatched', result='')
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(self.probes, [])

    def test_earlier_uncertainty_cannot_starve_a_start_unknown_attempt(self):
        """The early return in tick must let a recorded uncertain start reach the quota check."""
        self.guard.tick(snapshot())
        self.assertIsNotNone(self.guard.uncertain_since)
        self.backend.views = [unknown_view()]
        self.evidence = [SHELL]
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertIsNone(self.guard.uncertain_since)
        self.assertEqual(len(self.highs()), 1)
        self.assertIn('启动结果不确定', self.highs()[0])
        self.evidence = [CONFIRMED]
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')


class ForeignDispatch(Case):
    def test_task_bound_elsewhere_abandons_without_a_word_to_the_worker(self):
        self.backend.task = dict(status='blocked', result='', dispatch_id='ctx_other')
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertFalse(self.backend.bodies('dispatch:'))
        self.assertEqual(self.probes, [], 'not ours: no evidence is even read')
        self.assertAbsent('stop', 'fail_task', 'start', 'attempts')
        self.assertIn('额度交接已放弃', self.backend.bodies('run:')[-1])

    def test_a_concluded_task_abandons_too(self):
        self.backend.task = dict(status='blocked', result='有结论', dispatch_id=None)
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertFalse(self.backend.bodies('dispatch:'))
        self.assertEqual(self.probes, [])


class NotTriggered(Case):
    def test_nothing_happens_below_the_threshold(self):
        def fail():
            raise AssertionError('task read while not triggered')
        self.backend.task_state = fail
        self.assertEqual(self.guard.tick(snapshot(used=10)), 'continue')
        self.assertEqual(self.probes, [])
        self.assertEqual(self.backend.sent, [])
        self.assertUntouched()


class Takeover(Case):
    """Models the rule Orca enforces: a Task with an active supervised worker refuses --status failed."""
    def setUp(self):
        super().setUp()
        self.written = []
        def fail_task():
            if self.backend.views[0]['worker']['state'] not in ('failed', 'succeeded', 'stopped', 'abandoned'):
                raise RuntimeError('task_not_startable: stop or settle its worker first')
            self.written.append(dict(self.backend.task))
            self.backend.task = dict(status='failed', result=qg.HANDOFF_RESULT, dispatch_id=None)
        self.backend.fail_task = fail_task

    def test_blocked_task_with_no_dispatch_is_rewritten_after_the_report(self):
        refusals = []
        def start(target, spec):
            refusals.append(qg.retry_refusal(self.backend.task_state(), 'ctx_old', 'ctx_old'))
            return 0, {'result': {'dispatchId': 'ctx_new'}}
        self.backend.start = start
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(self.written, [dict(status='blocked', result='', dispatch_id=None)])
        self.assertEqual(self.backend.task['status'], 'failed')
        self.assertEqual(refusals, [None])
        self.assertEqual(self.guard.meta['dispatch_id'], 'ctx_old')

    def test_failed_rewrite_keeps_the_task_and_starts_nothing(self):
        def down():
            raise RuntimeError('task-update down')
        self.backend.fail_task = down
        with self.assertRaises(RuntimeError):
            self.guard.tick(snapshot())
        self.assertAbsent('start', 'quota_handoff')
        self.assertEqual(self.backend.task, dict(status='blocked', result='', dispatch_id=None))
        # Left blocked and empty, which is what a coordinator retry accepts; the old worker is confirmed gone.
        self.assertIsNone(qg.retry_refusal(self.backend.task_state(), 'ctx_old', 'ctx_old'))
        phase = json.loads((self.dir/'ctx_old-phase.json').read_text())
        self.assertNotIn('start_attempted', phase)
        self.assertTrue(phase['old_worker_stopped'])

    def test_watch_reports_a_failed_rewrite(self):
        def down():
            raise RuntimeError('task-update down')
        self.backend.fail_task = down
        code = qg.watch(self.guard, META, self.backend, lambda: self.config, lambda pol: snapshot())
        self.assertEqual(code, 1)
        self.assertIn('task-update down', self.highs()[-1])
        self.assertAbsent('start')

    def test_real_backend_sends_the_shared_result_without_needing_a_bound_dispatch(self):
        calls = []
        b = qg.Backend(CONFIG, dict(META, coordinator='term'), self.dir,
                       runner=lambda cmd, **kw: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0, '{"ok": true}', ''))[1])
        b.fail_task()
        self.assertEqual(calls[0][calls[0].index('--status')+1], 'failed')
        self.assertEqual(calls[0][calls[0].index('--result')+1], qg.HANDOFF_RESULT)


class TerminalFallback(Case):
    """The existing delivery check, reached from an uncertain start: it still inputs once and only once."""
    def setUp(self):
        super().setUp()
        self.config['quota_policy']['quota_guard']['delivery_fallback'] = dict(enabled=True, wait_seconds=20)
        self.meta = dict(META, context_file=str(self.dir/'context.md'))
        (self.dir/'context.md').write_text('原始任务')
        self.guard = qg.Guard(self.config, self.meta, self.backend, self.dir)
        self.backend.handoff_on_notify = None
        self.inputs = []
        self.backend.delivery_status = lambda *a: dict(state='pending', source='fake')
        def enter(handle, text, retry_request=None):
            self.inputs.append(text)
            return dict(accepted=True, request_id='12345678-1234-1234-1234-123456789abc')
        self.backend.terminal_input = enter
        real = self.backend.send
        self.backend.send = lambda to, body, high=False: (real(to, body, high), dict(messageId='msg_test'))[1]

    def test_pending_mail_inputs_once_and_the_text_stays_clean(self):
        self.guard.tick(snapshot())
        self.assertEqual(len(self.inputs), 1)
        self.assertFalse(set('><|;&$`\n\r\x1b\\\'"(){}[]*?!~').intersection(self.inputs[0]))
        self.assertTrue((self.dir/'ctx_old-terminal-input.json').exists())

    def test_a_new_guard_does_not_input_again(self):
        self.guard.tick(snapshot())
        self.backend.views = [unknown_view()]
        guard = qg.Guard(self.config, self.meta, self.backend, self.dir)
        guard.pool_lock.path.unlink(missing_ok=True)
        guard.tick(snapshot())
        self.assertEqual(len(self.inputs), 1)

    def test_shell_at_input_time_never_inputs(self):
        self.evidence = [CONFIRMED, CONFIRMED, SHELL]
        self.guard.tick(snapshot())
        self.assertEqual(self.inputs, [])
        self.assertAbsent('stop', 'fail_task', 'start')


class NonCodex(GuardCase):
    def setUp(self):
        super().setUp()
        self.config = copy.deepcopy(self.config)
        self.config['quota_policy']['quota_guard']['pools'] = ['codex', 'claude']
        self.meta = dict(META, agent='claude', profile='opus')
        self.guard = qg.Guard(self.config, self.meta, self.backend, self.dir)
        self.backend.views = [unknown_view()]
        self.backend.task = dict(status='blocked', result='', dispatch_id=None)
        def refuse(view):
            raise AssertionError('no Codex evidence for another agent')
        self.backend.codex_running = refuse
        self.snapshot = dict(ok=True, providers={'claude': provider()})

    def test_only_alerts_once(self):
        self.assertTrue(qg.predict(self.config, 'claude', self.snapshot, self.backend.clock())['trigger_windows'])
        for _ in range(3):
            self.assertEqual(self.guard.tick(self.snapshot), 'continue')
        highs = [body for _, body, high in self.backend.sent if high]
        self.assertEqual(len(highs), 1)
        self.assertIn('启动结果不确定', highs[0])
        self.assertIn('claude', highs[0])
        self.assertFalse(self.backend.bodies('dispatch:'))
        self.assertAbsent('stop', 'fail_task', 'start', 'attempts', 'target')
        self.assertIsNone(self.guard.finished)

    def test_not_triggered_stays_silent(self):
        quiet = dict(ok=True, providers={'claude': provider(used=10)})
        self.assertEqual(self.guard.tick(quiet), 'continue')
        self.assertEqual(self.backend.sent, [])


if __name__ == '__main__':
    unittest.main()
