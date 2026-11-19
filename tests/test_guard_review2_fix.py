"""R1-R4 review probes: actual Guard/Backend code, hermetic I/O and clocks only."""
import json
import subprocess
import unittest

import test_guard_review_fix as review
from test_quota_guard import GuardCase, META, COMPLETE_HANDOFF, qg, snapshot
from test_guard_start_unknown import unknown_view, SHELL, CONFIRMED


PROMPTS = ('~/project %', '/tmp/worktree #', '(venv) ~/project $', 'project >',
           'user@host ~/p %', 'user@host:~/p>', '%', '$', '#', '>', 'output %', 'output $',
           'output #', 'output >', '❯', '➜ project')


class PromptSafety(review.Flow):
    def actual_backend(self, prompt, filler=0, footer=None):
        frames = [review.UI[0], prompt, *(['ordinary output'] * filler), *review.UI[1:]]
        if footer is not None:
            frames[-1] = footer
        def runner(args, **kwargs):
            self.assertNotIn('send', args, 'this probe must never send to a real terminal')
            value = review.terminal(orphaned=True) if args[2] == 'show' else review.screen(frames)
            return subprocess.CompletedProcess(args, 0, json.dumps(dict(ok=True, result=dict(terminal=value))), '')
        backend = qg.Backend(self.config, self.meta, self.dir, runner=runner)
        view = review.live_view(orphaned=True)
        view['dispatch']['processIncarnation'] = 'pty_test:' + review.INC
        self.backend.views = [view]
        self.backend.codex_running = backend.codex_running
        return backend, view

    def test_r1_probe_actual_backend_and_tick_never_input_into_shell(self):
        for prompt in PROMPTS:
            with self.subTest(prompt=prompt):
                # Each dispatch owns its independent latch/state and runs the actual tick-to-input path.
                self.setUp()
                backend, view = self.actual_backend(prompt)
                decision = backend.codex_running(view)
                self.assertEqual(self.guard.tick(snapshot()), 'blocked')
                self.assertEqual(self.inputs, [])
                self.assertFalse(decision['safe'], prompt)
                self.assertFalse((self.dir/'ctx_old-terminal-input.json').exists())

    def test_r1_attached_prompts_actual_backend_and_tick_never_input(self):
        for prompt in ('[user@host project]$', '[root@host ~]#', 'bash-3.2$',
                       'zsh%', 'PS /tmp/worktree>'):
            for trailing in ('', '  \t'):
                with self.subTest(prompt=prompt, trailing=repr(trailing)):
                    self.setUp()
                    backend, view = self.actual_backend(prompt + trailing)
                    decision = backend.codex_running(view)
                    self.assertEqual(self.guard.tick(snapshot()), 'blocked')
                    self.assertEqual(self.inputs, [], prompt)
                    self.assertFalse(decision['safe'], prompt)
                    self.assertIn('输入框上方出现 shell 提示符', decision['reason'])
                    self.assertFalse((self.dir/'ctx_old-terminal-input.json').exists())

    def test_r1_output_and_footer_actual_backend_remain_safe(self):
        for row in ('# 修复计划', '$ git status', 'src/foo/bar.py 100%'):
            with self.subTest(row=row):
                backend, view = self.actual_backend(row)
                self.assertTrue(backend.codex_running(view)['safe'], row)
        backend, view = self.actual_backend('ordinary output', footer='? for shortcuts · 25%')
        self.assertTrue(backend.codex_running(view)['safe'])

    def test_r1_checks_every_visible_row_above_composer(self):
        for filler in (5, 6, 40, 110):
            backend, view = self.actual_backend('user@host ~/p %', filler)
            self.assertFalse(backend.codex_running(view)['safe'], filler)

    def test_r1_output_and_percentage_footer_are_still_safe(self):
        for row in ('# 修复计划', '$ git status', 'src/foo/bar.py 100%'):
            self.assertTrue(review.decide([review.UI[0], row, *review.UI[1:]])['safe'], row)
        self.assertTrue(review.decide([*review.UI[:3], '? for shortcuts · 25% left'])['safe'])


class RequestClosure(GuardCase):
    def test_r2_probe_resume_delivery_crash_cannot_recover_old_report(self):
        self.backend.enabled = [False]
        real_send = self.backend.send
        def send(to, body, high=False):
            real_send(to, body, high)
            return dict(messageId='msg_note')
        self.backend.send = send
        def crash(*args):
            raise SystemExit('hard death while confirming RESUME')
        self.backend.delivery_status = crash
        with self.assertRaises(SystemExit):
            self.guard.tick(snapshot())
        self.assertIn(qg.RESUME_NOTE, self.backend.bodies('dispatch:'))
        rebuilt = qg.Guard(self.config, META, self.backend, self.dir)
        self.assertFalse(rebuilt.request_still_open())
        self.backend.enabled = [True]
        self.backend.handoff_on_notify = None
        self.backend.delivery_status = lambda *args: dict(state='pending')
        self.backend.fp['status'] = 'worker resumed and changed files'
        self.backend.events.clear()
        self.assertEqual(rebuilt.tick(snapshot()), 'blocked')
        self.assertNoTakeover()
        self.assertEqual(len(list(self.dir.glob('ctx_old-handoff.stale-*'))), 1)

    def test_r2_hold_send_also_sees_closed_request_before_crash(self):
        self.backend.handoff_on_notify = COMPLETE_HANDOFF.replace('/tmp/worktree', '/tmp/wrong')
        real_send = self.backend.send
        def send(to, body, high=False):
            if body.startswith('额度交接已放弃（'):
                disk = json.loads((self.dir/'ctx_old-phase.json').read_text())
                self.assertEqual(disk['phase'], 'abandoned')
                self.assertFalse(qg.Guard(self.config, META, self.backend, self.dir).request_still_open())
                raise SystemExit('hard death during HOLD send')
            return real_send(to, body, high)
        self.backend.send = send
        with self.assertRaises(SystemExit):
            self.guard.tick(snapshot())

    def test_r2_failed_terminal_phase_write_never_sends_resume(self):
        self.backend.enabled = [False]
        real_phase = self.guard.phase
        def phase(name, **facts):
            if name == 'abandoned':
                raise OSError('cannot persist closure')
            return real_phase(name, **facts)
        self.guard.phase = phase
        with self.assertRaises(OSError):
            self.guard.tick(snapshot())
        self.assertNotIn(qg.RESUME_NOTE, self.backend.bodies('dispatch:'))


class UnknownRecovery(GuardCase):
    def setUp(self):
        super().setUp()
        self.backend.views = [unknown_view()]
        self.backend.task = dict(status='blocked', result='', dispatch_id=None)
        self.backend.handoff_on_notify = None
        self.probes = []
        def probe(view):
            self.probes.append(view)
            return dict(SHELL)
        self.backend.codex_running = probe
        self.guard.phase('waiting')
        self.until = self.backend.clock() + 30
        qg.write_private(self.guard.request_path(), json.dumps(dict(opened_at=self.backend.clock(), until=self.until)))
        self.report = self.dir/'ctx_old-handoff.md'
        self.guard = qg.Guard(self.config, META, self.backend, self.dir)

    def test_r3_probe_valid_complete_report_bypasses_both_screen_gates(self):
        self.report.write_text(COMPLETE_HANDOFF)
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(self.probes, [])
        self.assertEqual(self.backend.events.count('stop'), 1)
        self.assertEqual(self.backend.events.count('start'), 1)

    def test_r3_report_arriving_during_prerequisites_bypasses_second_gate(self):
        self.backend.codex_running = lambda view: (self.probes.append(view) or dict(CONFIRMED))
        real_fp = self.backend.fingerprint
        def fingerprint():
            self.report.write_text(COMPLETE_HANDOFF)
            self.backend.codex_running = lambda view: (self.probes.append(view) or dict(SHELL))
            return real_fp()
        self.backend.fingerprint = fingerprint
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(len(self.probes), 1)

    def test_r3_valid_request_without_report_eventually_abandons(self):
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.backend.sleep(31)
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertEqual(self.guard.finished, 'abandoned')
        self.assertIn('恢复', self.backend.bodies('run:')[-1])
        self.assertAbsent('stop', 'start', 'fail_task')

    def test_r3_incomplete_report_is_never_recovery_evidence(self):
        self.report.write_text('worktree: /tmp/worktree\nunfinished')
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertTrue(self.probes)
        self.assertAbsent('stop', 'start', 'fail_task')

    def test_r3_complete_report_does_not_bypass_task_ownership(self):
        self.report.write_text(COMPLETE_HANDOFF)
        self.backend.task['dispatch_id'] = 'ctx_other'
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertAbsent('stop', 'start', 'fail_task')

    def test_r3_complete_report_does_not_bypass_worktree_check(self):
        self.report.write_text(COMPLETE_HANDOFF.replace('/tmp/worktree', '/tmp/wrong'))
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertAbsent('stop', 'start', 'fail_task')

    def test_r3_complete_report_does_not_bypass_stop_confirmation(self):
        self.report.write_text(COMPLETE_HANDOFF)
        self.backend.stop_works = False
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertIn('stop', self.backend.events)
        self.assertAbsent('start', 'fail_task')


class SlowConfirmation(GuardCase):
    def test_r4_probe_no_new_query_at_or_after_deadline(self):
        calls = []
        start = self.backend.monotonic()
        def query(*args):
            calls.append(self.backend.monotonic()-start)
            self.backend.sleep(5)
            return dict(state='pending', source='slow')
        self.backend.delivery_status = query
        status = self.guard.note_delivery(dict(messageId='msg_note'))
        self.assertEqual(calls, [0, 8])
        self.assertEqual(self.backend.monotonic()-start, 15)
        self.assertEqual(status['state'], 'pending')

    def test_r4_one_in_flight_query_may_finish_after_poll_window(self):
        calls = []
        start = self.backend.monotonic()
        def query(*args):
            calls.append(self.backend.monotonic()-start)
            self.backend.sleep(7)
            return dict(state='unknown', source='slow')
        self.backend.delivery_status = query
        self.assertEqual(self.guard.note_delivery(dict(messageId='msg_note'))['state'], 'unknown')
        self.assertEqual(calls, [0, 10])
        self.assertEqual(self.backend.monotonic()-start, 17)


if __name__ == '__main__':
    unittest.main()
