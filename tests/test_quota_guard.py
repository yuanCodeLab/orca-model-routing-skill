"""No real Orca: injected quota snapshots and command recorder only."""
import argparse
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import hermetic  # noqa: F401  (must precede anything that can reach Orca)
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import quota_guard as qg
except ImportError:
    qg = None
import quota
import route

CONFIG = hermetic.read_routes()
NOW = 1800000000
SESSION_RESET = NOW + 6000
WEEKLY_RESET = NOW + 100000


def enabled_config():
    config = copy.deepcopy(CONFIG)
    config['quota_policy']['quota_guard']['enabled'] = True
    return config


def disabled_config():
    config = copy.deepcopy(CONFIG)
    config['quota_policy']['quota_guard']['enabled'] = False
    return config


def provider(used=95, weekly=10, age=0, session_reset=SESSION_RESET, weekly_reset=WEEKLY_RESET, source=None):
    entry = dict(status='ok', updatedAt=(NOW-age)*1000,
                 session=dict(usedPercent=used, windowMinutes=300, resetsAt=session_reset*1000),
                 weekly=dict(usedPercent=weekly, windowMinutes=10080, resetsAt=weekly_reset*1000))
    if source:
        entry['data_source'] = source
    return entry


def snapshot(*args, **kw):
    return dict(ok=True, providers={'codex': provider(*args, **kw)})


def worker(state='ready', stopped=False, dispatch=None):
    return dict(worker={'state': state},
                dispatch={'status': dispatch or ('completed' if state == 'succeeded' else 'dispatched')},
                projection={'outcome': 'succeeded' if state == 'succeeded' else 'in_progress',
                            'liveness': {'verdict': 'exited' if stopped else 'live'}},
                terminalResource={'releaseState': 'released' if stopped else 'not_requested'})


def after_worker_stop():
    """Real view after worker-stop: worker stopped, dispatch failed, terminal released."""
    return worker('stopped', stopped=True, dispatch='failed')


REAL_LOAD_META = qg.load_meta if qg else None
COMPLETE_HANDOFF = 'worktree: /tmp/worktree\n已完成 A\nbackground_pids: none\n' + (qg.HANDOFF_END if qg else '') + '\n'


class FakeBackend:
    """Default script: worker writes a complete handoff when asked, then the guard stops it."""
    def __init__(self, state, dispatch='ctx_old'):
        self.state = Path(state)
        self.dispatch = dispatch
        self.alive = []
        self.events, self.logs, self.sent = [], [], []
        self.views = [worker()]
        self.stop_works = True
        self.handoff_on_notify = COMPLETE_HANDOFF
        self.target = 'opus'
        self.recheck_ok = True
        self.attempt_count = 1
        self.code = 0
        self.spec = 'Original task: acceptance must pass'
        self.context_valid = True
        self.time = NOW
        self.mono = 1000.0
        self.fp = dict(head='abc', status='s0', diff='d0')
        self.fp_script = []
        self.task = dict(status='dispatched', result=None)
        self.task_script = []
        self.enabled = [True]
        self.left = [[]]
        self.on_sleep = None
        self.context_text = None
        self.recheck_pending, self.scripted_view, self.scripted_task = {}, None, None
    def clock(self): return self.time
    def monotonic(self): return self.mono
    def sleep(self, seconds):
        self.events.append('wait'); self.time += seconds; self.mono += seconds
        if self.on_sleep:
            self.on_sleep()
    def show(self):
        self.events.append('show')
        # A script says what tick and the wait see. handoff's re-read right after the prerequisites sees what tick's
        # scripted read saw, so scripts keep their meaning; tests that change state in that window assign state directly.
        if self.recheck_pending.pop('show', None) and self.scripted_view is not None:
            return self.scripted_view
        if len(self.views) > 1:
            self.scripted_view = self.views.pop(0)
            return self.scripted_view
        self.scripted_view = None
        return self.views[0]
    def attempts(self): self.events.append('attempts'); return self.attempt_count
    def original(self): self.events.append('original'); return self.spec if self.context_valid else None
    def plan(self): self.events.append('target'); return self.target
    def recheck(self, target): self.events.append('recheck'); return self.recheck_ok
    def fingerprint(self):
        self.events.append('fingerprint')
        if 'notify:dispatch' not in self.events:
            self.recheck_pending = dict(show=True, task=True)
        return self.fp_script.pop(0) if self.fp_script else dict(self.fp)
    def task_state(self):
        # Bound to this dispatch unless a test says otherwise, as Orca's task-list row is while it is active.
        if self.recheck_pending.pop('task', None) and self.scripted_task is not None:
            return dict(dict(dispatch_id=self.dispatch), **self.scripted_task)
        self.scripted_task = self.task_script.pop(0) if self.task_script else None
        return dict(dict(dispatch_id=self.dispatch), **(self.scripted_task or self.task))
    def live_pids(self, pids):
        self.events.append('live_pids')
        return [pid for pid in pids if pid in self.alive]
    def guard_enabled(self): return self.enabled.pop(0) if len(self.enabled) > 1 else self.enabled[0]
    def leftovers(self):
        self.events.append('scan')
        return self.left.pop(0) if len(self.left) > 1 else self.left[0]
    def send(self, to, body, high=False):
        self.events.append('high' if high else 'notify:' + to.split(':')[0])
        self.sent.append((to, body, high))
        if to.startswith('dispatch:') and self.handoff_on_notify is not None and '请求交接' in body:
            (self.state/(self.dispatch+'-handoff.md')).write_text(self.handoff_on_notify)
    def stop(self):
        self.events.append('stop')
        if self.stop_works:
            self.views = [after_worker_stop()]
    def context(self, original, handoff):
        self.events.append('context')
        self.context_text = original + '\n接手说明\n' + handoff
        return self.context_text
    def fail_task(self): self.events.append('fail_task')
    def start(self, target, spec): self.events.append('start'); return self.code, {'result': {'dispatchId': 'ctx_new'}}
    def record(self, event): self.events.append(event['event']); self.recorded = event
    def log(self, message): self.events.append('log'); self.logs.append(message)
    def bodies(self, prefix): return [b for to, b, _ in self.sent if to.startswith(prefix)]


META = dict(dispatch_id='ctx_old', run_id='run_x', task_id='task_x', profile='sol', agent='codex',
            kind='feature', complexity='normal', worktree='id:w', worktree_path='/tmp/worktree', original_spec_file='/tmp/original spec.md')


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(qg, 'quota_guard module must be implemented')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.backend = FakeBackend(self.dir)
        self.config = enabled_config()
        self.guard = qg.Guard(self.config, META, self.backend, self.dir)
    def predict(self, snap, samples=None):
        return qg.predict(self.config, 'codex', snap, NOW, samples)
    def session(self, snap, samples=None):
        return self.predict(snap, samples)['windows']['session']
    def assertAbsent(self, *events):
        for event in events:
            self.assertNotIn(event, self.backend.events)
    def assertNoTakeover(self):
        self.assertAbsent('fail_task', 'start', 'quota_handoff')


class TriggerFormula(GuardCase):
    def test_shipped_defaults_are_per_window_and_lag_settings_are_gone(self):
        raw = CONFIG['quota_policy']['quota_guard']
        self.assertEqual(raw['default_burn_pp_per_min'], dict(session=1.5, weekly=0.25))
        self.assertEqual(qg.DEFAULTS['default_burn_pp_per_min'], dict(session=1.5, weekly=0.25))
        self.assertEqual((raw['rate_window_minutes'], raw['rate_min_dt_minutes'], raw['rate_min_delta_pp']), (10, 3, 2))
        for gone in ('burn_pp_per_min', 'max_lag_minutes'):
            self.assertNotIn(gone, raw); self.assertNotIn(gone, qg.DEFAULTS)
    def test_session_threshold_uses_session_default(self):
        self.assertEqual(self.predict(snapshot(used=90.5))['trigger_windows'], ['session'])
        self.assertEqual(self.predict(snapshot(used=90.4))['trigger_windows'], [])
        self.assertEqual(self.session(snapshot(used=90))['projected_remaining_pp'], 10 - 4.5)
    def test_weekly_threshold_uses_weekly_default(self):
        self.assertEqual(self.predict(snapshot(used=10, weekly=94.25))['trigger_windows'], ['weekly'])
        self.assertEqual(self.predict(snapshot(used=10, weekly=94.24))['trigger_windows'], [])
    def test_ample_weekly_does_not_trigger_with_session_rate(self):
        # Report B5: weekly 28/31 remaining used to trigger at the session rate.
        for weekly in (72, 69):
            self.assertEqual(self.predict(snapshot(used=10, weekly=weekly, age=600))['trigger_windows'], [])
    def test_data_age_is_not_charged_as_consumption(self):
        fresh = self.session(snapshot(used=29))['projected_remaining_pp']
        old = self.session(snapshot(used=29, age=868))['projected_remaining_pp']
        self.assertEqual(fresh, old)
        self.assertEqual(self.predict(snapshot(used=29, age=868))['trigger_windows'], [])
    def test_expired_and_invalid_data_do_not_trigger(self):
        r = self.predict(snapshot(used=99, age=901))
        self.assertFalse(r['valid']); self.assertEqual(r['trigger_windows'], [])
        self.assertFalse(self.predict(snapshot(age=-61))['valid'])
        s = snapshot(); s['providers']['codex']['session']['usedPercent'] = float('nan')
        self.assertFalse(self.predict(s)['valid'])
        s = snapshot(); s['providers']['codex']['weekly'] = None
        self.assertFalse(self.predict(s)['valid'])
    def test_reports_source_and_rate_kind(self):
        r = self.predict(snapshot(used=10, source='codex_session_log'))
        self.assertEqual(r['data_source'], 'codex_session_log')
        self.assertEqual(r['windows']['session']['burn_source'], 'default')
    def test_reset_exemption_boundary_is_inclusive_and_per_window(self):
        self.assertEqual(self.predict(snapshot(used=99, session_reset=NOW+180))['trigger_windows'], [])
        self.assertEqual(self.predict(snapshot(used=99, session_reset=NOW+181))['trigger_windows'], ['session'])
        self.assertEqual(self.predict(snapshot(used=99, weekly=99, session_reset=NOW+120))['trigger_windows'], ['weekly'])
    def test_invalid_settings_are_rejected(self):
        for key, bad in (('default_burn_pp_per_min', dict(session=1)), ('default_burn_pp_per_min', dict(session=0, weekly=1)),
                         ('rate_min_dt_minutes', 11), ('rate_min_dt_minutes', 0), ('rate_min_delta_pp', 0),
                         ('rate_window_minutes', 'x'), ('grace_minutes', 0), ('floor_pp', 101), ('poll_seconds', 0),
                         ('handoff_spacing_minutes', -1), ('pools', 'codex')):
            config = enabled_config()
            config['quota_policy']['quota_guard'][key] = bad
            with self.assertRaises(ValueError, msg=(key, bad)):
                qg.settings(config)
    def test_absent_config_is_opt_out(self):
        self.assertFalse(qg.settings({})['enabled'])
    def test_trigger_math_replays_this_morning(self):
        # Coordinator's 14:23 reading: 79/28 used, ~1.4 points/min. Far from any threshold.
        self.assertEqual(self.predict(snapshot(used=79, weekly=28))['trigger_windows'], [])
        self.assertEqual(self.session(snapshot(used=91))['remaining_pp'], 9)


class MeasuredRate(GuardCase):
    def rate(self, used, sample, **kw):
        s = self.session(snapshot(used=used, **kw), [sample])
        return s['burn_pp_per_min'], s['burn_source']
    def test_measured_rate_over_recent_samples(self):
        self.assertEqual(self.rate(40, provider(used=30, age=300)), (2, 'measured'))
    def test_measured_rate_is_used_even_below_default(self):
        self.assertEqual(self.rate(32, provider(used=30, age=600)), (.2, 'measured'))
    def test_high_measured_rate_can_trigger_earlier(self):
        samples = [provider(used=25, age=300)]
        r = self.predict(snapshot(used=70), samples)
        self.assertEqual(r['windows']['session']['burn_pp_per_min'], 9)
        self.assertEqual(r['windows']['session']['projected_remaining_pp'], 3)
        self.assertEqual(r['trigger_windows'], ['session'])
    def test_short_dt_is_not_measured(self):
        # Report S1: 6 s and +1 point became 10 points/min and triggered at 99 remaining.
        self.assertEqual(self.rate(1, provider(used=0, age=6)), (1.5, 'default'))
        self.assertEqual(self.rate(50, provider(used=0, age=179)), (1.5, 'default'))
    def test_minimum_dt_boundary_is_measured(self):
        self.assertEqual(self.rate(6, provider(used=0, age=180)), (2, 'measured'))
    def test_small_delta_is_not_measured(self):
        self.assertEqual(self.rate(31, provider(used=30, age=300)), (1.5, 'default'))
    def test_minimum_delta_boundary_is_measured(self):
        self.assertEqual(self.rate(32, provider(used=30, age=300))[1], 'measured')
    def test_sample_outside_window_is_not_used(self):
        self.assertEqual(self.rate(50, provider(used=10, age=601)), (1.5, 'default'))
        self.assertEqual(self.rate(50, provider(used=10, age=600))[1], 'measured')
    def test_oldest_sample_inside_window_is_the_reference(self):
        samples = [provider(used=10, age=601), provider(used=20, age=600), provider(used=48, age=60)]
        self.assertEqual(self.session(snapshot(used=50), samples)['burn_pp_per_min'], 3)
    def test_resets_at_jitter_still_measures(self):
        # Report S2: real jitter must not discard a real rate.
        self.assertEqual(self.rate(40, provider(used=30, age=300, session_reset=SESSION_RESET-7))[1], 'measured')
    def test_rollover_is_not_measured(self):
        self.assertEqual(self.rate(40, provider(used=30, age=300, session_reset=SESSION_RESET-18000)), (1.5, 'default'))
        self.assertEqual(self.rate(5, provider(used=90, age=300, session_reset=SESSION_RESET)), (1.5, 'default'))
    def test_sources_are_not_compared(self):
        sample = provider(used=30, age=300, source='orca')
        self.assertEqual(self.rate(79, sample, source='codex_session_log'), (1.5, 'default'))
        same = provider(used=70, age=300, source='codex_session_log')
        self.assertEqual(self.rate(79, same, source='codex_session_log')[1], 'measured')
    def test_each_window_measures_independently(self):
        r = self.predict(snapshot(used=40, weekly=11), [provider(used=30, weekly=10, age=300)])
        self.assertEqual(r['windows']['session']['burn_source'], 'measured')
        self.assertEqual(r['windows']['weekly']['burn_pp_per_min'], .25)
    def test_malformed_sample_is_ignored(self):
        for bad in (dict(updatedAt=(NOW-300)*1000), dict(provider(age=300), session=None),
                    dict(provider(age=300), session=dict(usedPercent=float('nan'), resetsAt=SESSION_RESET*1000)),
                    dict(provider(age=300), session=dict(usedPercent=-5, resetsAt=SESSION_RESET*1000)),
                    dict(updatedAt='x')):
            self.assertEqual(self.session(snapshot(used=40), [bad])['burn_source'], 'default', bad)
    def test_guard_collects_samples_across_ticks(self):
        self.backend.target = None
        self.guard.tick(snapshot(used=10, age=300))
        self.guard.tick(snapshot(used=20))
        self.guard.tick(snapshot(used=20))
        self.assertEqual(json.loads(self.backend.logs[-1])['windows']['session']['burn_pp_per_min'], 2)
        self.assertEqual(len(self.guard.samples), 2)
    def test_guard_drops_old_samples(self):
        self.guard.tick(snapshot(used=10, age=800))
        self.guard.tick(snapshot(used=20))
        self.assertEqual(len(self.guard.samples), 2)
        self.guard.remember(dict(provider(used=30), updatedAt=(NOW+1000)*1000))
        self.assertEqual([s['session']['usedPercent'] for s in self.guard.samples], [20, 30])


class HandoffFlow(GuardCase):
    def test_graceful_order_and_event(self):
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        e = self.backend.events
        order = ['attempts', 'original', 'target', 'fingerprint', 'notify:run', 'notify:dispatch', 'stop', 'scan',
                 'context', 'fail_task', 'start', 'quota_handoff']
        for a, b in zip(order, order[1:]):
            self.assertLess(e.index(a), e.index(b), (a, b))
        self.assertEqual(self.backend.recorded['old_dispatch'], 'ctx_old')
        self.assertEqual(self.backend.recorded['new_dispatch'], 'ctx_new')
        self.assertIn('session', self.backend.recorded['trigger']['trigger_windows'])
        self.assertIn('已完成额度交接', self.backend.bodies('run:')[-1])
    def test_marker_ends_the_wait_without_using_the_grace_period(self):
        self.guard.tick(snapshot())
        self.assertEqual(self.backend.mono, 1000.0)
    def test_worker_is_told_not_to_send_failed_and_how_to_finish(self):
        self.guard.tick(snapshot())
        body = self.backend.bodies('dispatch:')[0]
        self.assertIn('不要发 worker_done --outcome failed', body)
        self.assertNotIn('然后发 worker_done', body)
        for needed in ('ctx_old-handoff.md', 'git rev-parse --show-toplevel', 'background_pids', qg.HANDOFF_END,
                       '结束回合', '等待后台任务结束或终止'):
            self.assertIn(needed, body)
    def test_coordinator_hears_first_with_cancel_instructions(self):
        self.guard.tick(snapshot())
        first_to, first, _ = self.backend.sent[0]
        self.assertTrue(first_to.startswith('run:'))
        self.assertIn('请勿接手', first)
        self.assertIn('ended its turn without sending worker_done', first)
        self.assertIn('Worker stopped without reporting', first)
        self.assertIn('预期现象，不要据此重试或停止，等待守卫结论', first)
        self.assertIn(str(self.guard.cancel_path()), first)
        self.assertIn('task-update --status failed --result cancelled', first)
    def test_coordinator_notice_failure_leaves_worker_untouched(self):
        real = self.backend.send
        def send(to, body, high=False):
            if not high:
                raise RuntimeError('fake delivery failure')
            real(to, body, high)
        self.backend.send = send
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertIsNone(self.guard.finished)
        self.assertFalse(self.backend.bodies('dispatch:'))
        self.assertIn('high', self.backend.events)
    def test_handoff_text_reaches_the_successor(self):
        self.guard.tick(snapshot())
        self.assertIn('已完成 A', self.backend.context_text)
    def test_no_report_stops_worker_alerts_and_takes_nothing_over(self):
        self.backend.handoff_on_notify = None
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        for flag in ('--task task_x', '--retry-of ctx_old', '--worktree id:w', '--profile opus', '--expected-profile opus', "--spec-file '/tmp/original spec.md'"):
            self.assertIn(flag, self.backend.bodies('run:')[-1])
        self.assertIn('stop', self.backend.events)
        self.assertEqual(self.backend.mono, 1180.0)
        # Neither the Task nor the second attempt is touched, and the attempt count is not even re-read.
        self.assertAbsent('fail_task', 'start', 'quota_handoff', 'context')
        self.assertEqual(self.backend.events.count('attempts'), 1)
        self.assertEqual(self.backend.task, dict(status='dispatched', result=None))
        body = self.backend.bodies('run:')[-1]
        self.assertTrue(self.backend.sent[-1][2], 'must be high priority')
        for needed in ('没有完整交接报告', '交接文件缺失', '已停止旧 worker', '未改 Task', '未占尝试次数', '未启动接手者', '协调者处理'):
            self.assertIn(needed, body)
    def test_report_without_end_marker_is_no_report(self):
        self.backend.handoff_on_notify = 'worktree: /tmp/worktree\nbackground_pids: none\n写到一半'
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertAbsent('fail_task', 'start', 'quota_handoff', 'context')
        self.assertIn('没有结束标记', self.backend.bodies('run:')[-1])
        self.assertIn('ctx_old-handoff.md', self.backend.bodies('run:')[-1])
    def test_no_report_with_unconfirmed_stop_says_so_instead(self):
        self.backend.handoff_on_notify = None
        self.backend.stop_works = False
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertIn('无法确认旧 worker 已停止', self.backend.bodies('run:')[-1])
        self.assertNotIn('确认退出', self.backend.bodies('run:')[-1])
    def test_grace_uses_monotonic_clock_across_sleep(self):
        # O1: a sleeping Mac jumps wall time; the grace period must not collapse.
        self.backend.handoff_on_notify = None
        polls = []
        def on_sleep():
            polls.append(self.backend.mono)
            self.backend.time += 100000
        self.backend.on_sleep = on_sleep
        self.guard.tick(snapshot())
        self.assertEqual(len(polls), 36)
        self.assertEqual(polls[-1], 1180.0)
    def test_stop_unconfirmed_never_starts(self):
        self.backend.stop_works = False
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertIn('stop', self.backend.events); self.assertNoTakeover()
        self.assertIn('无法确认旧 worker 已停止', self.backend.bodies('run:')[-1])
    def test_stop_evidence_vanishing_never_starts(self):
        def stop():
            self.backend.events.append('stop')
            self.backend.views = [after_worker_stop(), worker()]
        self.backend.stop = stop
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertNoTakeover()
    def test_task_failed_alone_is_not_exit_proof(self):
        view = worker(); view['dispatch']['status'] = 'failed'; view['worker']['state'] = 'failed'
        self.assertFalse(qg.is_stopped(view))
        self.assertTrue(qg.attempt_ended(view))
    def test_success_during_grace_abandons_switch(self):
        self.backend.handoff_on_notify = None
        self.backend.views = [worker(), worker(), worker('succeeded')]
        self.assertEqual(self.guard.tick(snapshot()), 'done')
        self.assertAbsent('stop'); self.assertNoTakeover()
        self.assertIn('已报告 succeeded', self.backend.bodies('run:')[-1])
    def test_success_while_confirming_stop_abandons_switch(self):
        self.backend.handoff_on_notify = None
        def stop():
            self.backend.events.append('stop')
            self.backend.views = [worker('succeeded')]
        self.backend.stop = stop
        self.assertEqual(self.guard.tick(snapshot()), 'done')
        self.assertNoTakeover()
        self.assertEqual(self.backend.events[self.backend.events.index('stop'):].count('wait'), 0)
    def test_success_after_stop_before_start_abandons_switch(self):
        def stop():
            self.backend.events.append('stop')
            self.backend.views = [after_worker_stop(), worker('succeeded')]
        self.backend.stop = stop
        self.assertEqual(self.guard.tick(snapshot()), 'done')
        self.assertNoTakeover()
    def test_success_notice_failure_still_finishes(self):
        real = self.backend.send
        def send(to, body, high=False):
            if '已报告 succeeded' in body:
                raise RuntimeError('fake')
            real(to, body, high)
        self.backend.send = send
        self.backend.handoff_on_notify = None
        self.backend.views = [worker(), worker('succeeded')]
        self.assertEqual(self.guard.tick(snapshot()), 'done')
    def test_attempts_filling_during_wait_blocks(self):
        counts = [1, 2]
        self.backend.attempts = lambda: counts.pop(0)
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertNoTakeover()
    def test_start_exit_2_or_3_alerts_once_without_retry(self):
        for code in (2, 3):
            backend = FakeBackend(self.dir); backend.code = code
            (self.dir/'ctx_old-handoff.md').unlink(missing_ok=True)
            (self.dir/'handoff-codex.flock').unlink(missing_ok=True)
            guard = qg.Guard(self.config, META, backend, self.dir)
            self.assertEqual(guard.tick(snapshot()), 'blocked')
            self.assertEqual(guard.tick(snapshot()), 'blocked')
            self.assertEqual(backend.events.count('start'), 1)
            self.assertIn('high', backend.events); self.assertNotIn('quota_handoff', backend.events)
    def test_start_without_dispatch_id_is_blocked(self):
        self.backend.start = lambda target, spec: (0, {})
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertAbsent('quota_handoff')


class ExternalTakeover(GuardCase):
    """Report B1/B2: nobody may be overridden by a guard that was only waiting."""
    def wait_views(self, *views):
        self.backend.handoff_on_notify = None
        self.backend.views = [worker()] + list(views)
    def assertAbandoned(self, result):
        self.assertEqual(result, 'abandoned')
        self.assertEqual(self.guard.finished, 'abandoned')
        self.assertNoTakeover()
        self.assertIn('额度交接已放弃', self.backend.bodies('run:')[-1])
    def test_worker_done_failed_with_live_terminal_is_not_overridden(self):
        # The incident's real view: dispatch failed, worker failed, terminal still live until autoRelease.
        self.wait_views(worker('failed', dispatch='failed'))
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertAbsent('stop')
    def test_external_stop_without_report_is_abandoned(self):
        self.wait_views(after_worker_stop())
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertAbsent('stop')
    def test_cancelled_abandoned_or_completed_dispatch_is_abandoned(self):
        for status in ('cancelled', 'abandoned', 'completed', 'failed'):
            backend = FakeBackend(self.dir); backend.handoff_on_notify = None
            backend.views = [worker(), worker('ready', dispatch=status)]
            guard = qg.Guard(self.config, META, backend, self.dir)
            self.assertEqual(guard.tick(snapshot()), 'abandoned', status)
            self.assertNotIn('start', backend.events)
    def test_worker_state_alone_is_enough(self):
        for state in ('stopping', 'stopped', 'stop_unknown', 'abandoned'):
            backend = FakeBackend(self.dir); backend.handoff_on_notify = None
            backend.views = [worker(), worker(state)]
            guard = qg.Guard(self.config, META, backend, self.dir)
            self.assertEqual(guard.tick(snapshot()), 'abandoned', state)
    def test_external_stop_with_report_is_abandoned(self):
        # Report B1: blocked only ever means the guard's own stop if the guard actually sent one.
        self.backend.views = [worker(), after_worker_stop()]
        self.backend.task_script = [dict(status='dispatched', result=None), dict(status='blocked', result=None)]
        self.backend.task = dict(status='blocked', result=None)
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertAbsent('stop')
    def test_blocked_task_with_live_view_and_report_is_abandoned_without_own_stop(self):
        self.backend.task_script = [dict(status='dispatched', result=None)]
        self.backend.task = dict(status='blocked', result=None)
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertAbsent('stop')
    def test_terminal_exit_without_report_is_abandoned_at_once(self):
        # The guard sent no stop, so someone else ended it: not a case for the no-report alert after the grace period.
        self.wait_views(worker(stopped=True))
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertAbsent('stop')
        self.assertEqual(self.backend.mono, 1000.0)
    def test_terminal_exit_after_report_needs_no_stop_and_may_continue(self):
        # The terminal went on its own, the Task and dispatch say nothing was decided.
        self.backend.views = [worker(), worker(stopped=True)]
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertAbsent('stop')
    def test_terminal_exit_after_report_with_blocked_task_is_abandoned(self):
        self.backend.views = [worker(), worker(stopped=True)]
        self.backend.task_script = [dict(status='dispatched', result=None)]
        self.backend.task = dict(status='blocked', result=None)
        self.assertAbandoned(self.guard.tick(snapshot()))
    def test_explicit_cancel_with_complete_report_is_abandoned(self):
        for status in ('cancelled', 'abandoned', 'circuit_broken', 'failed', 'completed'):
            for state in ('ready', 'stopped'):
                backend = FakeBackend(self.dir)
                (self.dir/'ctx_old-handoff.md').unlink(missing_ok=True)
                backend.views = [worker(), worker(state, stopped=state == 'stopped', dispatch=status)]
                guard = qg.Guard(self.config, META, backend, self.dir)
                self.assertEqual(guard.tick(snapshot()), 'abandoned', (status, state))
                self.assertNotIn('stop', backend.events)
                self.assertNotIn('fail_task', backend.events); self.assertNotIn('start', backend.events)
    def test_abandoned_worker_state_with_report_is_abandoned(self):
        self.backend.views = [worker(), worker('abandoned')]
        self.assertAbandoned(self.guard.tick(snapshot()))
    def test_cancelled_dispatch_seen_after_our_stop_is_abandoned(self):
        for status, state in (('cancelled', 'stopped'), ('abandoned', 'stopped'), ('failed', 'abandoned')):
            backend = FakeBackend(self.dir)
            (self.dir/'ctx_old-handoff.md').unlink(missing_ok=True)
            def stop(b=backend, status=status, state=state):
                b.events.append('stop')
                b.views = [worker(state, stopped=True, dispatch=status)]
            backend.stop = stop
            guard = qg.Guard(self.config, META, backend, self.dir)
            self.assertEqual(guard.tick(snapshot()), 'abandoned', status)
            # Caught before the shared context entry, which a coordinator retry also reads, is rewritten.
            self.assertNotIn('context', backend.events)
            self.assertNotIn('fail_task', backend.events)
            self.assertIn('需要协调者接管', backend.bodies('run:')[-1])
    def test_cancel_at_the_last_look_is_abandoned(self):
        real = self.backend.context
        def context(original, handoff):
            self.backend.views = [worker('stopped', stopped=True, dispatch='cancelled')]
            return real(original, handoff)
        self.backend.context = context
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertIn('context', self.backend.events)
    def test_task_rebound_to_another_dispatch_is_abandoned(self):
        self.backend.task_script = [dict(status='dispatched', result=None)]
        self.backend.task = dict(status='dispatched', result=None, dispatch_id='ctx_someone_else')
        self.assertAbandoned(self.guard.tick(snapshot()))
    def test_dispatched_task_losing_its_binding_is_abandoned(self):
        self.backend.task_script = [dict(status='dispatched', result=None)]
        self.backend.task = dict(status='dispatched', result=None, dispatch_id=None)
        self.assertAbandoned(self.guard.tick(snapshot()))
    def test_stop_clearing_the_binding_is_expected(self):
        # Orca's real row after worker-stop: blocked, no dispatch_id, empty result.
        active = dict(status='dispatched', result='', dispatch_id='ctx_old')
        self.backend.task_script = [dict(active), dict(active)]
        self.backend.task = dict(status='blocked', result=None, dispatch_id=None)
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
    def test_blocked_task_bound_elsewhere_after_our_stop_is_abandoned(self):
        self.backend.task_script = [dict(status='dispatched', result=None), dict(status='dispatched', result=None)]
        self.backend.task = dict(status='blocked', result=None, dispatch_id='ctx_someone_else')
        self.assertAbandoned(self.guard.tick(snapshot()))
    def test_worker_done_failed_with_report_changes_task_and_is_abandoned(self):
        self.backend.views = [worker(), worker('failed', dispatch='failed')]
        self.backend.task_script = [dict(status='dispatched', result=None)]
        self.backend.task = dict(status='blocked', result='{"provenance":"worker_report"}')
        self.assertAbandoned(self.guard.tick(snapshot()))
    def test_task_status_change_while_worker_live_is_abandoned_and_worker_holds(self):
        self.backend.handoff_on_notify = None
        self.backend.task_script = [dict(status='dispatched', result=None)]
        self.backend.task = dict(status='blocked', result=None)
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertIn('停止新动作', self.backend.bodies('dispatch:')[-1])
        self.assertNotIn('继续原任务', self.backend.bodies('dispatch:')[-1])
    def test_cancelled_after_our_stop_is_abandoned(self):
        # Cancel flow: worker-stop + task-update failed/cancelled, racing the guard's own stop.
        self.backend.task_script = [dict(status='dispatched', result=None), dict(status='dispatched', result=None)]
        self.backend.task = dict(status='failed', result='cancelled')
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertIn('stop', self.backend.events)
    def test_each_terminal_task_status_is_abandoned_after_stop(self):
        for status in ('completed', 'failed', 'ready', 'pending'):
            backend = FakeBackend(self.dir)
            (self.dir/'ctx_old-handoff.md').unlink(missing_ok=True)
            backend.task_script = [dict(status='dispatched', result=None), dict(status='dispatched', result=None)]
            backend.task = dict(status=status, result=None)
            guard = qg.Guard(self.config, META, backend, self.dir)
            self.assertEqual(guard.tick(snapshot()), 'abandoned', status)
            self.assertNotIn('fail_task', backend.events)
    def test_own_stop_moving_task_to_blocked_is_expected(self):
        self.backend.task_script = [dict(status='dispatched', result=None), dict(status='dispatched', result=None)]
        self.backend.task = dict(status='blocked', result=None)
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
    def test_cancel_file_during_wait(self):
        self.backend.handoff_on_notify = None
        def on_sleep():
            self.guard.cancel_path().write_text('')
        self.backend.on_sleep = on_sleep
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertAbsent('stop')
        self.assertIn('继续原任务', self.backend.bodies('dispatch:')[-1])
    def test_stale_cancel_file_from_before_does_not_cancel(self):
        self.guard.cancel_path().write_text('')
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
    def test_disabling_during_wait_stops_the_handoff(self):
        self.backend.handoff_on_notify = None
        self.backend.enabled = [False]
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertAbsent('stop')
    def test_disabling_after_stop_stops_the_handoff(self):
        self.backend.enabled = [True, False]
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertIn('stop', self.backend.events)
    def test_disabling_at_the_last_look_stops_the_handoff(self):
        self.backend.enabled = [True, True, False]
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertIn('context', self.backend.events)
    def test_commit_during_grace_is_abandoned(self):
        # Incident: coordinator committed at 14:03:21 while the guard waited.
        self.backend.fp_script = [dict(self.backend.fp)]
        self.backend.fp = dict(head='def', status='s0', diff='d0')
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertAbsent('context')
    def test_edit_between_marker_and_stop_is_abandoned(self):
        real_stop = self.backend.stop
        def stop():
            real_stop()
            self.backend.fp = dict(head='abc', status='s1', diff='d1')
        self.backend.stop = stop
        self.assertAbandoned(self.guard.tick(snapshot()))
    def test_edit_after_handoff_marker_is_abandoned(self):
        marker = dict(self.backend.fp)
        self.backend.fp_script = [dict(self.backend.fp), marker, dict(head='abc', status='s1', diff='d1')]
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertIn('context', self.backend.events)
    def test_worker_edits_before_marker_are_allowed(self):
        self.backend.fp_script = [dict(head='abc', status='before', diff='before')]
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
    def test_abandon_with_undeliverable_resume_note_still_alerts(self):
        real = self.backend.send
        def send(to, body, high=False):
            if to.startswith('dispatch:') and '继续原任务' in body:
                raise RuntimeError('fake')
            real(to, body, high)
        self.backend.send = send
        self.backend.handoff_on_notify = None
        self.backend.enabled = [False]
        self.assertAbandoned(self.guard.tick(snapshot()))
        self.assertIn('通知没有送达', self.backend.bodies('run:')[-1])


class AbandonNotice(GuardCase):
    """Report R-B1: dropping a handoff and losing the Task are different, and only the first lets a worker go on."""
    TRANSITIONS = (('cancelled', dict(status='failed', result='cancelled'), 'Task'),
                   ('completed', dict(status='completed', result='done'), 'Task'),
                   ('rebound', dict(status='dispatched', result=None, dispatch_id='ctx_other'), '不再绑定 ctx_old'),
                   ('released', dict(status='dispatched', result=None, dispatch_id=None), 'Task'),
                   ('concluded', dict(status='dispatched', result='done'), 'Task'),
                   ('blocked', dict(status='blocked', result=None), 'Task'))
    def phase(self, directory=None):
        return json.loads(((directory or self.dir)/'ctx_old-phase.json').read_text())
    def assertHeld(self, backend, directory=None):
        """Told to stop, never to go on, and the guard itself changed nothing."""
        notes = backend.bodies('dispatch:')
        self.assertEqual(len(notes), 2, notes)
        self.assertIn('请求交接', notes[0])
        self.assertIn('停止新动作', notes[1])
        self.assertIn('不要恢复工作', notes[1])
        self.assertFalse(any('继续原任务' in note for note in notes), notes)
        alert = backend.bodies('run:')[-1]
        self.assertIn('额度交接已放弃', alert)
        self.assertIn('已要求它停止新动作', alert)
        self.assertNotIn('继续原任务', alert)
        for event in ('stop', 'fail_task', 'start', 'quota_handoff'):
            self.assertNotIn(event, backend.events)
        self.assertEqual(self.phase(directory)['worker_told'], 'hold')
    def assertResumed(self, backend):
        notes = backend.bodies('dispatch:')
        self.assertEqual(notes[-1], qg.RESUME_NOTE)
        self.assertEqual(len(notes), 2, notes)
        # FakeBackend.send returns no receipt, so delivery cannot be confirmed; see NoteDelivery for the rest.
        self.assertIn('继续原任务的通知未确认送达', backend.bodies('run:')[-1])
        for event in ('stop', 'fail_task', 'start', 'quota_handoff'):
            self.assertNotIn(event, backend.events)
        self.assertEqual(self.phase()['worker_told'], 'resume')
    def test_each_task_transition_during_the_wait_holds_the_worker_with_the_reason(self):
        for name, task, named in self.TRANSITIONS:
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                backend = FakeBackend(tmp); backend.handoff_on_notify = None
                backend.task_script = [dict(status='dispatched', result=None)]
                backend.task = task
                guard = qg.Guard(self.config, META, backend, tmp)
                self.assertEqual(guard.tick(snapshot()), 'abandoned')
                self.assertHeld(backend, Path(tmp))
                self.assertIn(named, backend.bodies('dispatch:')[-1])
                self.assertIn(named, backend.bodies('run:')[-1])
    def cancel_during_wait(self):
        """The cancel file arrives on the first sleep, so one ordinary loop reads the Task first."""
        self.backend.handoff_on_notify = None
        self.backend.task_script = [dict(status='dispatched', result=None), dict(status='dispatched', result=None)]
        self.backend.on_sleep = lambda: self.guard.cancel_path().write_text('')
    def test_cancel_file_with_task_still_active_resumes(self):
        self.cancel_during_wait()
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertResumed(self.backend)
        self.assertIn('取消文件', self.backend.bodies('run:')[-1])
    def test_disabling_with_task_still_active_resumes(self):
        self.backend.handoff_on_notify = None
        self.backend.enabled = [False]
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertResumed(self.backend)
        self.assertIn('已被关闭', self.backend.bodies('run:')[-1])
    def test_cancel_file_with_a_changed_task_holds(self):
        # The recheck, not the earlier read: the Task changes between the loop and the notice.
        for name, task, _ in self.TRANSITIONS:
            with self.subTest(name):
                self.setUp()
                self.cancel_during_wait()
                self.backend.task = task
                self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
                self.assertHeld(self.backend)
    def test_disabling_with_a_changed_task_holds(self):
        self.backend.handoff_on_notify = None
        self.backend.enabled = [False]
        self.backend.task_script = [dict(status='dispatched', result=None)]
        self.backend.task = dict(status='failed', result='cancelled')
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertHeld(self.backend)
    def test_task_change_seen_once_is_not_undone_by_a_later_read(self):
        # Task change is not a dropped handoff, so nothing re-reads it into a resume even if it looks restored.
        self.backend.handoff_on_notify = None
        self.backend.task_script = [dict(status='dispatched', result=None), dict(status='failed', result='cancelled')]
        self.backend.task = dict(status='dispatched', result=None)
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertHeld(self.backend)
    def test_recheck_sees_an_ended_attempt_and_does_not_resume(self):
        for view in (worker('failed', dispatch='failed'), worker('stopping'), worker('ready', stopped=True),
                     dict(worker(), projection=dict(outcome='succeeded', liveness=dict(verdict='live')))):
            with self.subTest(view=view):
                self.setUp()
                self.cancel_during_wait()
                # Tick, first loop, second loop, then the recheck right before the notice.
                self.backend.views = [worker(), worker(), worker(), view]
                self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
                self.assertNotIn('继续原任务', self.backend.bodies('dispatch:')[-1])
    def test_unreadable_recheck_does_not_resume(self):
        # show: tick, re-read before the request, two loops, recheck. task_state: tick, re-read, first loop, recheck.
        for broken, recheck_call in (('show', 5), ('task_state', 4)):
            with self.subTest(broken):
                self.setUp()
                self.cancel_during_wait()
                real, calls = getattr(self.backend, broken), []
                def reader(real=real, calls=calls, recheck_call=recheck_call):
                    calls.append(1)
                    if len(calls) == recheck_call:
                        raise RuntimeError('offline')
                    return real()
                setattr(self.backend, broken, reader)
                self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
                self.assertEqual(len(calls), recheck_call)
                self.assertHeld(self.backend)
    def test_hold_that_cannot_be_delivered_says_so(self):
        real = self.backend.send
        def send(to, body, high=False):
            if to.startswith('dispatch:') and '不要恢复工作' in body:
                raise RuntimeError('offline')
            real(to, body, high)
        self.backend.send = send
        self.backend.handoff_on_notify = None
        self.backend.task_script = [dict(status='dispatched', result=None)]
        self.backend.task = dict(status='failed', result='cancelled')
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertIn('消息没有送达', self.backend.bodies('run:')[-1])
        self.assertIn('立刻处理', self.backend.bodies('run:')[-1])
        self.assertEqual(self.phase()['worker_told'], 'hold_undelivered')
    def test_resume_that_cannot_be_delivered_says_so(self):
        real = self.backend.send
        def send(to, body, high=False):
            if to.startswith('dispatch:') and body == qg.RESUME_NOTE:
                raise RuntimeError('offline')
            real(to, body, high)
        self.backend.send = send
        self.backend.handoff_on_notify = None
        self.backend.enabled = [False]
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertEqual(self.phase()['worker_told'], 'resume_undelivered')
    def test_worker_already_gone_gets_no_notice(self):
        self.backend.handoff_on_notify = None
        self.backend.views = [worker(), after_worker_stop()]
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertEqual(len(self.backend.bodies('dispatch:')), 1)
        self.assertNotIn('worker_told', self.phase())
    def test_nothing_after_our_own_stop_tells_the_worker_anything(self):
        self.backend.task_script = [dict(status='dispatched', result=None), dict(status='dispatched', result=None)]
        self.backend.task = dict(status='failed', result='cancelled')
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertIn('stop', self.backend.events)
        self.assertEqual(len(self.backend.bodies('dispatch:')), 1)
        self.assertIn('旧 worker 已被守卫停止', self.backend.bodies('run:')[-1])


class InitialOwnership(GuardCase):
    """Report B1: a Task that already has an owner or a conclusion is not the guard's to end."""
    def assertLeftAlone(self, result):
        self.assertEqual(result, 'abandoned')
        self.assertEqual(self.guard.finished, 'abandoned')
        # Not asked, not stopped, not replaced; no check that would precede asking ran either.
        self.assertAbsent('notify:dispatch', 'stop', 'fail_task', 'start', 'quota_handoff', 'context', 'attempts', 'target')
        self.assertFalse(self.backend.bodies('dispatch:'))
        self.assertIn('额度交接已放弃', self.backend.bodies('run:')[-1])
        self.assertTrue(self.backend.sent[-1][2], 'must be high priority')
    def test_terminal_or_inactive_task_status(self):
        for status, result in (('completed', 'done'), ('failed', 'cancelled'), ('blocked', 'coordinator taking over'),
                               ('ready', None), ('pending', None), (None, None)):
            self.setUp()
            self.backend.task = dict(status=status, result=result)
            self.assertLeftAlone(self.guard.tick(snapshot()))
    def test_blocked_unknown_waits_then_recovers_and_handoffs(self):
        self.backend.task = dict(status='blocked', result=None, dispatch_id=None)
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertAbsent('notify:dispatch', 'stop', 'attempts')
        self.backend.task = dict(status='dispatched', result=None)
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')

    def test_blocked_unknown_alerts_once_after_fifteen_minutes(self):
        self.backend.task = dict(status='blocked', result=None, dispatch_id=None)
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.backend.mono += 901
        self.assertEqual(self.guard.tick(snapshot(used=10)), 'continue')
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertEqual(len([x for x in self.backend.sent if x[2]]), 1)
        self.assertIn('保护暂时无法生效', self.backend.bodies('run:')[-1])
        self.assertAbsent('notify:dispatch', 'stop', 'attempts')

    def test_blocked_unknown_with_other_dispatch_or_ended_worker_abandons(self):
        for bound, view in [('ctx_other', worker()), (None, worker('stopping'))]:
            self.setUp()
            self.backend.task = dict(status='blocked', result=None, dispatch_id=bound)
            self.backend.views = [view]
            self.assertLeftAlone(self.guard.tick(snapshot()))

    def test_active_status_with_a_conclusion(self):
        self.backend.task = dict(status='dispatched', result='已经有结论')
        self.assertLeftAlone(self.guard.tick(snapshot()))
    def test_active_task_bound_to_another_dispatch(self):
        for bound in ('ctx_someone_else', None):
            self.setUp()
            self.backend.task = dict(status='dispatched', result=None, dispatch_id=bound)
            self.assertLeftAlone(self.guard.tick(snapshot()))
    def test_worker_already_ending_is_left_alone(self):
        for state in ('stopping', 'stop_unknown', 'failed', 'abandoned'):
            self.setUp()
            self.backend.views = [worker(state)]
            self.assertLeftAlone(self.guard.tick(snapshot()))
    def test_real_active_row_shapes_are_accepted(self):
        # A live row really carries an empty string for result.
        for result in (None, ''):
            self.setUp()
            self.backend.task = dict(status='dispatched', result=result)
            self.assertEqual(self.guard.tick(snapshot()), 'handoff', result)
    def test_a_reopened_task_still_showing_the_guards_note_is_not_a_conclusion(self):
        self.backend.task = dict(status='dispatched', result=qg.HANDOFF_RESULT)
        self.assertEqual(self.guard.not_ours(worker(), self.backend.task_state()), None)
    def test_unreadable_task_retries_next_tick_without_touching_the_worker(self):
        states = [RuntimeError('task-list down')]
        real = self.backend.task_state
        def task_state():
            if states:
                raise states.pop()
            return real()
        self.backend.task_state = task_state
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertIsNone(self.guard.finished)
        self.assertAbsent('notify:dispatch', 'stop', 'attempts')
        self.assertIn('task-list down', self.backend.bodies('run:')[-1])
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
    def test_not_triggered_never_reads_the_task(self):
        def fail():
            raise AssertionError('task read while not triggered')
        self.backend.task_state = fail
        self.assertEqual(self.guard.tick(snapshot(used=10)), 'continue')
    def test_abandon_releases_the_pool_without_spacing(self):
        self.backend.task = dict(status='completed', result='done')
        self.guard.tick(snapshot())
        probe = qg.HandoffLock(self.dir, 'codex')
        self.assertTrue(probe.acquire(self.backend.time, 120))
        probe.release()


class OwnStop(GuardCase):
    """Only a stop this guard actually sent may explain a blocked Task."""
    def test_flag_starts_clear_and_is_set_before_the_stop_goes_out(self):
        self.assertFalse(self.guard.stop_sent)
        seen = []
        real = self.backend.stop
        def stop():
            seen.append((self.guard.stop_sent, json.loads((self.dir/'ctx_old-phase.json').read_text())['stop_sent']))
            real()
        self.backend.stop = stop
        self.guard.tick(snapshot())
        self.assertEqual(seen, [(True, True)])
    def test_cancel_reason_treats_blocked_by_who_stopped(self):
        base = dict(status='dispatched', result=None)
        self.backend.task = dict(status='blocked', result=None, dispatch_id=None)
        self.assertIn('状态', self.guard.cancel_reason(base))
        self.guard.stop_sent = True
        self.assertIsNone(self.guard.cancel_reason(base))
    def test_other_status_is_not_excused_by_our_stop(self):
        self.guard.stop_sent = True
        self.backend.task = dict(status='failed', result=None, dispatch_id=None)
        self.assertIn('状态', self.guard.cancel_reason(dict(status='dispatched', result=None)))
    def test_empty_and_missing_result_are_the_same_result(self):
        self.backend.task = dict(status='dispatched', result='')
        self.assertIsNone(self.guard.cancel_reason(dict(status='dispatched', result=None)))
    def test_abandon_after_our_stop_tells_the_coordinator_the_worker_is_gone(self):
        self.backend.task_script = [dict(status='dispatched', result=None), dict(status='dispatched', result=None)]
        self.backend.task = dict(status='failed', result='cancelled', dispatch_id=None)
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertIn('需要协调者接管', self.backend.bodies('run:')[-1])
    def test_abandon_before_any_stop_does_not_claim_it(self):
        self.backend.enabled = [False]
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertNotIn('需要协调者接管', self.backend.bodies('run:')[-1])


class BackgroundPids(GuardCase):
    def report(self, line):
        self.backend.handoff_on_notify = 'worktree: /tmp/worktree\n已完成 A\n%s\n%s\n' % (line, qg.HANDOFF_END)
    def assertRefused(self, *needed):
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertAbsent('fail_task', 'start', 'quota_handoff', 'context')
        self.assertIn('stop', self.backend.events)
        for piece in needed:
            self.assertIn(piece, self.backend.bodies('run:')[-1])
    def test_parsing(self):
        for text, want in (('background_pids: none', []), ('background_pids:none', []), ('background_pids: NONE', []),
                           ('background_pids: 123', [123]), ('background_pids: 123, 456', [123, 456]),
                           ('background_pids: 123，456、789', [123, 456, 789]), ('background_pids: 123 456', [123, 456]),
                           ('x\nbackground_pids: 7000  \ny', [7000])):
            self.assertEqual(qg.parse_background_pids(text), want, text)
    def test_unreadable_forms_are_none(self):
        for text in ('', None, 'no such line', 'background_pids:', 'background_pids: ', 'background_pids: 没有',
                     'background_pids: none（都结束了）', 'background_pids: 123 (node)', 'background_pids: 12a',
                     'background_pids: 0', 'background_pids: 1', 'background_pids: 5, 1', '- background_pids: none',
                     'background_pids: none\nbackground_pids: 123', 'background_pids: 123,'):
            self.assertIsNone(qg.parse_background_pids(text), text)
    def test_live_pid_refuses(self):
        self.report('background_pids: 123, 456')
        self.backend.alive = [456]
        self.assertRefused('456', '仍存活', '协调者')
        self.assertNotIn('123', self.backend.bodies('run:')[-1])
    def test_all_listed_pids_gone_is_accepted(self):
        self.report('background_pids: 123, 456')
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertIn('live_pids', self.backend.events)
    def test_none_is_accepted(self):
        self.report('background_pids: none')
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
    def test_unparseable_refuses(self):
        self.report('background_pids: 大概没有')
        self.assertRefused('background_pids', '无法解析')
        self.assertAbsent('live_pids')
    def test_missing_line_refuses(self):
        self.backend.handoff_on_notify = 'worktree: /tmp/worktree\n已完成 A\n' + qg.HANDOFF_END
        self.assertRefused('无法解析')
    def test_checked_after_the_stop(self):
        # Children of the worker's terminal go with it, so an earlier look would refuse them for nothing.
        self.report('background_pids: 123')
        self.guard.tick(snapshot())
        self.assertLess(self.backend.events.index('stop'), self.backend.events.index('live_pids'))
    def test_unknown_liveness_refuses(self):
        self.report('background_pids: 123')
        def fail(pids):
            raise RuntimeError('process scan unavailable')
        self.backend.live_pids = fail
        with self.assertRaises(RuntimeError):
            self.guard.tick(snapshot())
        self.assertEqual(self.guard.finished, 'blocked')
        self.assertNoTakeover()
    def test_report_changed_after_stop_is_rechecked(self):
        real = self.backend.stop
        def stop():
            real()
            (self.dir/'ctx_old-handoff.md').write_text('worktree: /tmp/worktree\n被改过\n')
        self.backend.stop = stop
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertNoTakeover()
        self.assertIn('复核', self.backend.bodies('run:')[-1])
    def test_report_pointing_elsewhere_after_stop_is_refused(self):
        real = self.backend.stop
        def stop():
            real()
            (self.dir/'ctx_old-handoff.md').write_text('worktree: /tmp/elsewhere\nbackground_pids: none\n' + qg.HANDOFF_END)
        self.backend.stop = stop
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertNoTakeover()
        self.assertIn('工作目录不一致', self.backend.bodies('run:')[-1])
    def test_real_liveness(self):
        proc = subprocess.Popen(['sleep', '30'], stdin=subprocess.DEVNULL)
        self.addCleanup(proc.wait); self.addCleanup(proc.kill)
        b = qg.Backend(CONFIG, dict(worktree_path=str(self.dir)), self.dir, runner=lambda cmd, **kw: subprocess.run(cmd, **kw))
        gone = subprocess.Popen(['true'], stdin=subprocess.DEVNULL); gone.wait()
        self.assertEqual(b.live_pids([proc.pid, gone.pid]), [proc.pid])
        self.assertEqual(b.live_pids([]), [])
    def test_zombie_is_not_alive_and_unknown_table_raises(self):
        lstart = time.strftime('%a %b %d %H:%M:%S %Y', time.localtime(NOW))
        table = '123 1 %s <defunct>\n456 1 %s node\n' % (lstart, lstart)
        b = qg.Backend(CONFIG, dict(worktree_path=str(self.dir)), self.dir,
                       runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, table, ''))
        self.assertEqual(b.live_pids([123, 456, 789]), [456])
        bad = qg.Backend(CONFIG, dict(worktree_path=str(self.dir)), self.dir,
                         runner=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, '', 'ps: no'))
        with self.assertRaises(RuntimeError):
            bad.live_pids([123])


class LsofOutput(unittest.TestCase):
    """Report B2: a partial lsof result used to be taken as the whole picture."""
    def cwds(self, code, stdout, **kw):
        return qg.process_cwds(lambda cmd, **_: subprocess.CompletedProcess(cmd, code, stdout, ''), [], **kw)
    def test_good_output(self):
        self.assertEqual(self.cwds(0, 'p1\nfcwd\nn/a\np2\nfcwd\nn/b\n'), {1: '/a', 2: '/b'})
    def test_nonzero_exit_refuses_even_with_output(self):
        with self.assertRaises(RuntimeError):
            self.cwds(1, 'p1\nfcwd\nn/a\n')
    def test_record_without_cwd_refuses(self):
        for out in ('p1\nfcwd\np2\nfcwd\nn/b\n', 'p1\nfcwd\nn/a\np2\nfcwd\n', 'p1\n'):
            with self.assertRaises(RuntimeError, msg=out):
                self.cwds(0, out)
    def test_malformed_lines_refuse(self):
        for out in ('pabc\nn/a\n', 'p1\nn/a\nx?\n', 'n/a\n'):
            with self.assertRaises(RuntimeError, msg=out):
                self.cwds(0, out)
    def test_selected_pids_may_be_partial(self):
        self.assertEqual(self.cwds(1, 'p1\nfcwd\nn/a\n', partial_ok=True), {1: '/a'})
    def test_leftovers_refuses_on_partial_lsof(self):
        lstart = time.strftime('%a %b %d %H:%M:%S %Y', time.localtime(NOW))
        def runner(cmd, **kw):
            if cmd[0] == 'lsof':
                return subprocess.CompletedProcess(cmd, 1, 'p5\nfcwd\nn/elsewhere\n', 'lsof: partial')
            return subprocess.CompletedProcess(cmd, 0, '5 1 %s node\n' % lstart, '')
        b = qg.Backend(CONFIG, dict(worktree_path='/tmp/worktree', dispatch_started_at=NOW-5), '/tmp', runner=runner)
        with self.assertRaises(RuntimeError):
            b.leftovers()
    def test_coordinator_walk_tolerates_an_unreadable_ancestor(self):
        # Real lsof exits 1 when one named pid (root-owned login, say) cannot be examined.
        lstart = time.strftime('%a %b %d %H:%M:%S %Y', time.localtime(NOW))
        me = os.getppid()
        def runner(cmd, **kw):
            if cmd[0] == 'lsof':
                return subprocess.CompletedProcess(cmd, 1, 'p%d\nfcwd\nn/tmp/worktree\n' % me, '')
            return subprocess.CompletedProcess(cmd, 0, '%d 1 %s zsh\n' % (me, lstart), '')
        self.assertEqual([r['pid'] for r in qg.coordinator_roots('/tmp/worktree', runner)], [me])


class UntrackedFingerprint(unittest.TestCase):
    """Report B3, on a real repository."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(os.path.realpath(tmp.name))
        self.git('init', '-q')
        (self.dir/'tracked').write_text('base'); (self.dir/'.gitignore').write_text('ignored\n')
        self.git('add', '.'); self.git('commit', '-qm', 'base')
        self.b = qg.Backend(CONFIG, dict(worktree_path=str(self.dir)), self.dir, runner=lambda cmd, **kw: subprocess.run(cmd, **kw))
    def git(self, *args):
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.test'] + list(args), cwd=self.dir,
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)
    def test_content_change_with_the_same_status_line_is_seen(self):
        (self.dir/'new.py').write_text('worker content\n')
        before = self.b.fingerprint()
        (self.dir/'new.py').write_text('other writer changed content\n')
        after = self.b.fingerprint()
        self.assertNotEqual(before, after)
        self.assertEqual((before['status'], before['diff']), (after['status'], after['diff']))
    def test_stable_when_nothing_changed(self):
        (self.dir/'new.py').write_text('x')
        self.assertEqual(self.b.fingerprint(), self.b.fingerprint())
    def test_file_inside_an_untracked_directory_is_seen(self):
        (self.dir/'pkg').mkdir(); (self.dir/'pkg'/'a.py').write_text('one')
        before = self.b.fingerprint()
        (self.dir/'pkg'/'a.py').write_text('two')
        self.assertNotEqual(before, self.b.fingerprint())
        before = self.b.fingerprint()
        (self.dir/'pkg'/'b.py').write_text('new sibling')
        self.assertNotEqual(before, self.b.fingerprint())
    def test_path_and_type_count_not_only_content(self):
        (self.dir/'a').write_text('same')
        before = self.b.fingerprint()
        (self.dir/'a').rename(self.dir/'b')
        self.assertNotEqual(before['untracked'], self.b.fingerprint()['untracked'])
        (self.dir/'target').write_text('same')
        (self.dir/'b').unlink(); (self.dir/'b').symlink_to('target')
        link = self.b.fingerprint()['untracked']
        (self.dir/'b').unlink(); (self.dir/'b').write_text('target')
        self.assertNotEqual(link, self.b.fingerprint()['untracked'])
    def test_executable_bit_counts(self):
        (self.dir/'a').write_text('x')
        before = self.b.fingerprint()['untracked']
        (self.dir/'a').chmod(0o755)
        self.assertNotEqual(before, self.b.fingerprint()['untracked'])
    def test_symlink_is_hashed_not_followed(self):
        outside = self.dir.parent/(self.dir.name + '-outside.txt')
        outside.write_text('one'); self.addCleanup(outside.unlink)
        (self.dir/'link').symlink_to(outside)
        before = self.b.fingerprint()
        outside.write_text('two')
        self.assertEqual(before, self.b.fingerprint())
        (self.dir/'link').unlink(); (self.dir/'link').symlink_to(self.dir/'tracked')
        self.assertNotEqual(before, self.b.fingerprint())
    def test_odd_file_name_is_read(self):
        (self.dir/'空 格\n.txt').write_text('x')
        before = self.b.fingerprint()
        (self.dir/'空 格\n.txt').write_text('y')
        self.assertNotEqual(before, self.b.fingerprint())
    def test_listing_keeps_names_git_could_not_decode(self):
        seen = {}
        def runner(cmd, **kw):
            if 'ls-files' in cmd:
                seen.update(kw)
                return subprocess.CompletedProcess(cmd, 0, '', '')
            return subprocess.run(cmd, **kw)
        qg.Backend(CONFIG, dict(worktree_path=str(self.dir)), self.dir, runner=runner).fingerprint()
        self.assertEqual(seen['errors'], 'surrogateescape')
    def test_special_file_is_recorded_without_blocking(self):
        # git does not list these itself; one swapped in between listing and reading must not hang the guard.
        os.mkfifo(self.dir/'pipe')
        special = qg.untracked_digest(str(self.dir), ['pipe'])
        self.assertEqual(special, qg.untracked_digest(str(self.dir), ['pipe']))
        (self.dir/'pipe').unlink(); (self.dir/'pipe').write_text('')
        self.assertNotEqual(special, qg.untracked_digest(str(self.dir), ['pipe']))
    def looks_regular(self, path):
        # What an lstat before the swap would have seen: the race the open flags exist for.
        regular = os.lstat(self.dir/'tracked')
        real = os.lstat
        return patch.object(qg.os, 'lstat', side_effect=lambda p: regular if str(p) == str(path) else real(p))
    def test_file_swapped_for_a_symlink_is_not_followed(self):
        (self.dir/'swapped').symlink_to(self.dir/'tracked')
        with self.looks_regular(self.dir/'swapped'), self.assertRaisesRegex(RuntimeError, '读取未跟踪文件失败'):
            qg.untracked_digest(str(self.dir), ['swapped'])
    def test_file_swapped_for_a_fifo_refuses_without_blocking(self):
        os.mkfifo(self.dir/'swapped')
        with self.looks_regular(self.dir/'swapped'), self.assertRaisesRegex(RuntimeError, '被替换'):
            qg.untracked_digest(str(self.dir), ['swapped'])
    def test_too_many_files_refuses(self):
        for i in range(3):
            (self.dir/('f%d' % i)).write_text('x')
        with patch.object(qg, 'UNTRACKED_MAX_FILES', 2), self.assertRaisesRegex(RuntimeError, '上限'):
            self.b.fingerprint()
        with patch.object(qg, 'UNTRACKED_MAX_FILES', 3):
            self.b.fingerprint()
    def test_too_many_bytes_refuses(self):
        (self.dir/'a').write_text('xxxx'); (self.dir/'b').write_text('yyyy')
        with patch.object(qg, 'UNTRACKED_MAX_BYTES', 7), self.assertRaisesRegex(RuntimeError, '上限'):
            self.b.fingerprint()
        with patch.object(qg, 'UNTRACKED_MAX_BYTES', 8):
            self.b.fingerprint()
    def test_unreadable_file_refuses(self):
        (self.dir/'secret').write_text('x')
        (self.dir/'secret').chmod(0)
        self.addCleanup((self.dir/'secret').chmod, 0o600)
        with self.assertRaisesRegex(RuntimeError, '读取未跟踪文件失败'):
            self.b.fingerprint()
    def test_file_vanishing_between_listing_and_reading_refuses(self):
        with self.assertRaisesRegex(RuntimeError, '读取未跟踪文件失败'):
            qg.untracked_digest(str(self.dir), ['gone.txt'])
    def test_nested_repository_refuses(self):
        (self.dir/'sub').mkdir()
        subprocess.run(['git', 'init', '-q'], cwd=self.dir/'sub', check=True)
        (self.dir/'sub'/'x').write_text('x')
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.test', 'commit', '-q', '--allow-empty', '-m', 'x'],
                       cwd=self.dir/'sub', check=True)
        with self.assertRaisesRegex(RuntimeError, '嵌套仓库'):
            self.b.fingerprint()
    def test_listing_failure_refuses(self):
        def runner(cmd, **kw):
            if 'ls-files' in cmd:
                return subprocess.CompletedProcess(cmd, 128, '', 'fatal')
            return subprocess.run(cmd, **kw)
        b = qg.Backend(CONFIG, dict(worktree_path=str(self.dir)), self.dir, runner=runner)
        with self.assertRaisesRegex(RuntimeError, 'ls-files'):
            b.fingerprint()
    def test_ignored_files_stay_a_known_blind_spot(self):
        # Documented limit, pinned so a change to it is a decision.
        (self.dir/'ignored').write_text('old')
        before = self.b.fingerprint()
        (self.dir/'ignored').write_text('changed')
        self.assertEqual(before, self.b.fingerprint())
    def test_writes_nothing_to_the_index(self):
        (self.dir/'new').write_text('x')
        index = (self.dir/'.git'/'index').read_bytes()
        self.b.fingerprint()
        self.assertEqual(index, (self.dir/'.git'/'index').read_bytes())


def make_submodule_repo(root):
    """A real outer repository at root/outer tracking a real gitlink at module; returns (outer, child)."""
    child, outer = Path(root)/'child', Path(root)/'outer'
    for repo in (child, outer):
        repo.mkdir()
        git_in(repo, 'init', '-q')
    (child/'tracked.txt').write_text('base\n')
    git_in(child, 'add', '.'); git_in(child, 'commit', '-qm', 'base')
    (outer/'tracked').write_text('base\n')
    git_in(outer, 'add', '.'); git_in(outer, 'commit', '-qm', 'base')
    git_in(outer, 'submodule', 'add', '-q', str(child), 'module')
    git_in(outer, 'commit', '-qm', 'submodule')
    return outer, child


def git_in(repo, *args):
    subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.test', '-c', 'protocol.file.allow=always'] + list(args),
                   cwd=repo, check=True, capture_output=True, stdin=subprocess.DEVNULL)


class TrackedSubmodule(GuardCase):
    """Report R-B2, on real repositories: git cannot show what changed inside one, so the guard does not hand off."""
    def setUp(self):
        super().setUp()
        self.root = Path(os.path.realpath(self.dir))
    def real(self, outer):
        return qg.Backend(CONFIG, dict(worktree_path=str(outer)), self.dir)
    def guarded(self, outer):
        """Fake Orca, real fingerprint."""
        self.backend.fingerprint = self.real(outer).fingerprint
        self.backend.handoff_on_notify = 'worktree: %s\n已完成 A\nbackground_pids: none\n%s\n' % (outer, qg.HANDOFF_END)
        return qg.Guard(self.config, dict(META, worktree_path=str(outer)), self.backend, self.dir)
    def assertWorkerUntouched(self):
        self.assertEqual(self.backend.bodies('dispatch:'), [])
        self.assertAbsent('notify:dispatch', 'stop', 'context')
        self.assertNoTakeover()
    def test_fingerprint_refuses_and_names_the_submodule(self):
        outer, _ = make_submodule_repo(self.root)
        with self.assertRaisesRegex(qg.SubmodulesPresent, 'module'):
            self.real(outer).fingerprint()
    def test_change_inside_the_submodule_was_invisible_to_outer_git(self):
        # Why this refuses rather than hashes: outer git reports the same bytes before and after.
        outer, _ = make_submodule_repo(self.root)
        tracked = outer/'module'/'tracked.txt'
        def read(*args):
            return subprocess.run(['git'] + list(args), cwd=outer, capture_output=True, text=True, check=True).stdout
        tracked.write_text('first\n')
        before = read('status', '--porcelain'), read('diff', 'HEAD', '--binary')
        tracked.write_text('second\n')
        self.assertEqual(before, (read('status', '--porcelain'), read('diff', 'HEAD', '--binary')))
    def test_initial_check_only_alerts_and_leaves_the_worker_alone(self):
        outer, _ = make_submodule_repo(self.root)
        guard = self.guarded(outer)
        self.assertEqual(guard.tick(snapshot()), 'continue')
        self.assertIsNone(guard.finished)
        self.assertWorkerUntouched()
        alerts = self.backend.bodies('run:')
        self.assertEqual(len(alerts), 1)
        self.assertIn('Git 子模块', alerts[0])
        self.assertIn('module', alerts[0])
        self.assertIn('不受自动交接保护', alerts[0])
        self.assertIn('未通知也未停止 worker', alerts[0])
    def test_alert_repeats_only_after_leaving_the_trigger(self):
        outer, _ = make_submodule_repo(self.root)
        guard = self.guarded(outer)
        guard.tick(snapshot()); guard.tick(snapshot())
        self.assertEqual(len(self.backend.bodies('run:')), 1)
        guard.tick(snapshot(used=10))
        guard.tick(snapshot())
        self.assertEqual(len(self.backend.bodies('run:')), 2)
        self.assertWorkerUntouched()
    def test_alert_is_separate_from_other_check_failures(self):
        # An earlier unrelated check failure on this Run must not swallow the only word about this.
        outer, _ = make_submodule_repo(self.root)
        guard = self.guarded(outer)
        qg.claim_run_alert(self.dir, 'run_x', 'check_failed', self.backend.clock())
        guard.tick(snapshot())
        self.assertIn('Git 子模块', self.backend.bodies('run:')[-1])
    def test_submodule_only_in_the_index_is_refused(self):
        outer = self.root/'outer'; outer.mkdir()
        child = self.root/'child'; child.mkdir()
        for repo in (outer, child):
            git_in(repo, 'init', '-q')
            (repo/'f').write_text('x'); git_in(repo, 'add', '.'); git_in(repo, 'commit', '-qm', 'base')
        git_in(outer, 'submodule', 'add', '-q', str(child), 'staged')
        with self.assertRaisesRegex(qg.SubmodulesPresent, 'staged'):
            self.real(outer).fingerprint()
    def test_submodule_only_in_head_is_refused(self):
        outer, _ = make_submodule_repo(self.root)
        git_in(outer, 'rm', '-q', '--cached', 'module')
        with self.assertRaisesRegex(qg.SubmodulesPresent, 'module'):
            self.real(outer).fingerprint()
    def test_ordinary_repository_lists_none(self):
        outer = self.root/'plain'; outer.mkdir()
        git_in(outer, 'init', '-q')
        (outer/'160000 name').write_text('x')
        git_in(outer, 'add', '.'); git_in(outer, 'commit', '-qm', 'base')
        self.assertEqual(self.real(outer).submodule_paths(), [])
        self.real(outer).fingerprint()
    def test_unreadable_listing_is_not_taken_as_no_submodules(self):
        for word in ('ls-files', 'ls-tree'):
            def runner(cmd, word=word, **kw):
                if word in cmd:
                    return subprocess.CompletedProcess(cmd, 128, '', 'bad')
                return subprocess.CompletedProcess(cmd, 0, '', '')
            backend = qg.Backend(CONFIG, dict(worktree_path='/tmp'), self.dir, runner=runner)
            with self.assertRaises(RuntimeError, msg=word):
                backend.submodule_paths()
    def plain_repo_for_the_flow(self):
        outer = self.root/'outer'; child = self.root/'child'
        for repo in (outer, child):
            repo.mkdir(); git_in(repo, 'init', '-q')
            (repo/'f').write_text('x'); git_in(repo, 'add', '.'); git_in(repo, 'commit', '-qm', 'base')
        return outer, child
    def test_submodule_added_before_the_marker_never_starts(self):
        outer, child = self.plain_repo_for_the_flow()
        guard = self.guarded(outer)
        real = self.backend.send
        def send(to, body, high=False):
            if to.startswith('dispatch:'):
                git_in(outer, 'submodule', 'add', '-q', str(child), 'late')
            real(to, body, high)
        self.backend.send = send
        self.assertEqual(guard.tick(snapshot()), 'blocked')
        self.assertNoTakeover()
        self.assertIn('Git 子模块', self.backend.bodies('run:')[-1])
    def test_submodule_added_before_the_last_look_never_starts(self):
        outer, child = self.plain_repo_for_the_flow()
        guard = self.guarded(outer)
        real = self.backend.context
        def context(original, handoff):
            git_in(outer, 'submodule', 'add', '-q', str(child), 'late')
            return real(original, handoff)
        self.backend.context = context
        with self.assertRaises(qg.SubmodulesPresent):
            guard.tick(snapshot())
        self.assertEqual(guard.finished, 'blocked')
        self.assertNoTakeover()


class UntrackedChangeInHandoff(GuardCase):
    """The new fingerprint field goes through the same comparisons as the rest."""
    def changed(self):
        return dict(self.backend.fp, untracked='changed')
    def test_prerequisite_failure_keeps_the_worker(self):
        def fail():
            raise RuntimeError('未跟踪文件 9 个，超过指纹上限 5')
        self.backend.fingerprint = fail
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertAbsent('notify:dispatch', 'stop')
        self.assertIn('超过指纹上限', self.backend.bodies('run:')[-1])
    def test_change_after_marker_is_abandoned(self):
        self.backend.fp = dict(self.backend.fp, untracked='u0')
        self.backend.fp_script = [dict(self.backend.fp), dict(self.backend.fp), self.changed()]
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertNoTakeover()
        self.assertIn('又有变化', self.backend.bodies('run:')[-1])
    def test_failure_at_the_marker_stops_and_refuses(self):
        real, calls = self.backend.fingerprint, []
        def fingerprint():
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('读取未跟踪文件失败')
            return real()
        self.backend.fingerprint = fingerprint
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertIn('stop', self.backend.events); self.assertNoTakeover()
        self.assertIn('取不到 worktree 指纹', self.backend.bodies('run:')[-1])
    def test_failure_at_the_last_look_never_starts(self):
        real, calls = self.backend.fingerprint, []
        def fingerprint():
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError('读取未跟踪文件失败')
            return real()
        self.backend.fingerprint = fingerprint
        with self.assertRaises(RuntimeError):
            self.guard.tick(snapshot())
        self.assertEqual(self.guard.finished, 'blocked')
        self.assertNoTakeover()


class WorktreeAndProcesses(GuardCase):
    def test_reported_worktree_differs_from_registered(self):
        # Incident: worker worked in agent-bg-run, registered agent.
        self.backend.handoff_on_notify = 'worktree: /tmp/agent-bg-run\n' + qg.HANDOFF_END
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertAbsent('stop'); self.assertNoTakeover()
        # The guard cannot tell where this worker is working, so it is not told to carry on.
        self.assertIn('停止新动作', self.backend.bodies('dispatch:')[-1])
        self.assertNotIn('继续原任务', self.backend.bodies('dispatch:')[-1])
        self.assertIn('不一致', self.backend.bodies('run:')[-1])
    def test_complete_report_without_worktree_line_cannot_establish_placement(self):
        self.backend.handoff_on_notify = '已完成\n' + qg.HANDOFF_END
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertNoTakeover()
    def test_partial_report_naming_other_worktree_is_no_report(self):
        self.backend.handoff_on_notify = 'worktree: /tmp/agent-bg-run\n写到一半'
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertIn('stop', self.backend.events); self.assertNoTakeover()
    def test_real_paths_compare_after_symlink_resolution(self):
        link = self.dir/'link'
        link.symlink_to('/tmp/worktree')
        self.assertTrue(self.guard.same_worktree(dict(worktree=str(link))))
    def test_leftover_process_refuses_start(self):
        self.backend.left = [[dict(pid=123, command='node')]]
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertNoTakeover(); self.assertAbsent('context')
        self.assertIn('123', self.backend.bodies('run:')[-1])
    def test_refusal_alert_lists_pid_name_and_start_time(self):
        self.backend.left = [[dict(pid=123, command='node', started='2026-11-04 07:00:00')]]
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        body = self.backend.bodies('run:')[-1]
        for piece in ('123', 'node', '2026-11-04 07:00:00', '启动时间'):
            self.assertIn(piece, body)
    def test_short_lived_process_is_rescanned(self):
        self.backend.left = [[dict(pid=9, command='git')], []]
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(self.backend.events.count('scan'), 2)
    def test_process_scan_failure_is_terminal_not_ignored(self):
        def fail():
            raise RuntimeError('process scan unavailable')
        self.backend.leftovers = fail
        with self.assertRaises(RuntimeError):
            self.guard.tick(snapshot())
        self.assertEqual(self.guard.finished, 'blocked')
        self.assertNoTakeover()


class WindowTurnover(GuardCase):
    """Report S1: samples are filtered before the earliest is picked."""
    LOG = quota.SOURCE_CODEX_LOG
    def latest(self, used=85, **kw):
        return dict(ok=True, providers={'codex': provider(used=used, source=self.LOG, **kw)})
    def test_report_counterexample_triggers(self):
        # 10 min ago belongs to the old session; 3 min ago and now are the new one: 64 -> 85 is 7 points/min.
        before = provider(used=99, age=600, session_reset=NOW-240, source=self.LOG)
        after = provider(used=64, age=180, source=self.LOG)
        r = self.predict(self.latest(), [before, after])
        self.assertEqual(r['windows']['session']['burn_pp_per_min'], 7)
        self.assertEqual(r['windows']['session']['burn_source'], 'measured')
        self.assertEqual(r['windows']['session']['projected_remaining_pp'], -6)
        self.assertEqual(r['trigger_windows'], ['session'])
        self.guard.samples = [before, after]
        self.assertEqual(self.guard.tick(self.latest()), 'handoff')
    def test_order_of_samples_does_not_matter(self):
        before = provider(used=99, age=600, session_reset=NOW-240, source=self.LOG)
        after = provider(used=64, age=180, source=self.LOG)
        self.assertEqual(self.predict(self.latest(), [after, before])['trigger_windows'], ['session'])
    def test_earliest_qualifying_sample_wins(self):
        samples = [provider(used=60, age=300, source=self.LOG), provider(used=80, age=180, source=self.LOG)]
        self.assertEqual(self.session(self.latest(), samples)['burn_pp_per_min'], 5)
    def test_other_source_does_not_hide_a_good_sample(self):
        samples = [provider(used=0, age=600, source=quota.SOURCE_ORCA), provider(used=64, age=180, source=self.LOG)]
        self.assertEqual(self.session(self.latest(), samples)['burn_pp_per_min'], 7)
    def test_malformed_old_sample_does_not_hide_a_good_one(self):
        for mutate in (lambda s: s.update(session=None), lambda s: s['session'].update(usedPercent='x'),
                       lambda s: s['session'].update(usedPercent=101), lambda s: s['session'].update(usedPercent=-1),
                       lambda s: s['session'].update(resetsAt=None), lambda s: s.pop('updatedAt')):
            old = provider(used=10, age=600, source=self.LOG)
            mutate(old)
            samples = [old, provider(used=64, age=180, source=self.LOG)]
            self.assertEqual(self.session(self.latest(), samples)['burn_pp_per_min'], 7)
    def test_sample_older_than_the_rate_window_does_not_hide_a_good_one(self):
        samples = [provider(used=0, age=601, source=self.LOG), provider(used=64, age=180, source=self.LOG)]
        self.assertEqual(self.session(self.latest(), samples)['burn_pp_per_min'], 7)
    def test_recent_too_close_sample_is_not_used(self):
        self.assertEqual(self.session(self.latest(), [provider(used=64, age=179, source=self.LOG)])['burn_source'], 'default')
        self.assertEqual(self.session(self.latest(), [provider(used=64, age=180, source=self.LOG)])['burn_source'], 'measured')
    def test_only_old_window_samples_is_a_cold_start(self):
        s = self.session(self.latest(), [provider(used=99, age=600, session_reset=NOW-240, source=self.LOG)])
        self.assertEqual((s['burn_pp_per_min'], s['burn_source']), (1.5, 'default'))
    def test_weekly_turnover_is_judged_on_its_own(self):
        before = provider(used=10, weekly=99, age=600, weekly_reset=NOW-240, source=self.LOG)
        after = provider(used=20, weekly=30, age=180, source=self.LOG)
        latest = dict(ok=True, providers={'codex': provider(used=30, weekly=33, source=self.LOG)})
        r = self.predict(latest, [before, after])
        self.assertEqual(r['windows']['weekly']['burn_pp_per_min'], 1)
        # The session window did not turn over, so its earliest sample still counts.
        self.assertEqual(r['windows']['session']['burn_pp_per_min'], 2)
    def test_flat_new_window_falls_back_to_the_default(self):
        samples = [provider(used=99, age=600, session_reset=NOW-240, source=self.LOG), provider(used=84, age=180, source=self.LOG)]
        self.assertEqual(self.session(self.latest(), samples)['burn_source'], 'default')


class Prerequisites(GuardCase):
    def test_not_triggered(self):
        self.assertEqual(self.guard.tick(snapshot(used=10)), 'continue')
        self.assertAbsent('target', 'attempts')
    def test_no_target_keeps_worker_and_alerts_once(self):
        self.backend.target = None
        self.guard.tick(snapshot()); self.guard.tick(snapshot())
        self.assertEqual(self.backend.events.count('high'), 1)
        self.assertAbsent('stop', 'start', 'notify:dispatch')
    def test_attempt_limit_keeps_worker(self):
        self.backend.attempt_count = 2
        self.guard.tick(snapshot())
        self.assertAbsent('notify:dispatch', 'start', 'target')
        self.assertIn('high', self.backend.events)
    def test_missing_original_keeps_worker(self):
        self.backend.context_valid = False
        self.guard.tick(snapshot())
        self.assertAbsent('notify:dispatch', 'stop', 'target')
    def test_worktree_fingerprint_failure_keeps_worker(self):
        def fail():
            raise RuntimeError('not a git worktree')
        self.backend.fingerprint = fail
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertAbsent('notify:dispatch', 'stop')
        self.assertIn('not a git worktree', self.backend.bodies('run:')[-1])
    def test_transient_check_failure_is_retried_next_tick(self):
        # Report S4: one timeout used to switch judgement off for good.
        calls = []
        def attempts():
            calls.append(1)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired('worker-list', 30)
            return 1
        self.backend.attempts = attempts
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertEqual(self.backend.events.count('high'), 1)
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
    def test_target_appearing_later_still_hands_off(self):
        self.backend.target = None
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.assertEqual(self.guard.tick(snapshot()), 'continue')
        self.backend.target = 'opus'
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(self.backend.events.count('high'), 1)
    def test_alert_rearms_after_trigger_clears(self):
        self.backend.target = None
        self.guard.tick(snapshot())
        self.guard.tick(snapshot(used=10))
        self.guard.tick(snapshot())
        self.assertEqual(self.backend.events.count('high'), 2)
    def test_manual_stop_before_trigger_exits_quietly(self):
        self.backend.views = [worker(stopped=True)]
        self.assertEqual(self.guard.tick(snapshot()), 'done')
        self.assertAbsent('target', 'notify:run')
    def test_incident_replay_takes_no_action(self):
        self.assertEqual(self.guard.tick(snapshot(used=29, age=868)), 'continue')
        self.assertAbsent('attempts', 'target', 'notify:dispatch', 'high', 'stop', 'fail_task', 'start')
    def test_near_reset_takes_no_action(self):
        self.assertEqual(self.guard.tick(snapshot(used=99, session_reset=NOW+120)), 'continue')
        self.assertAbsent('notify:dispatch', 'target')


class NoFreshData(GuardCase):
    UNKNOWN = dict(ok=False, reason='fake unknown', providers={})
    def test_no_switch_and_alert_after_15_minutes_once(self):
        self.guard.tick(self.UNKNOWN); self.backend.time += 900
        self.guard.tick(self.UNKNOWN)
        self.assertEqual(self.backend.events.count('high'), 0)
        self.backend.time += 1
        self.guard.tick(self.UNKNOWN); self.guard.tick(self.UNKNOWN)
        self.assertAbsent('target', 'notify:dispatch')
        self.assertEqual(self.backend.events.count('high'), 1)
    def test_recovery_rearms(self):
        self.guard.tick(self.UNKNOWN); self.backend.time += 901; self.guard.tick(self.UNKNOWN)
        fresh = snapshot(used=10); fresh['providers']['codex']['updatedAt'] = self.backend.time * 1000
        fresh['providers']['codex']['session']['resetsAt'] += 901000
        self.guard.tick(fresh)
        self.guard.tick(self.UNKNOWN); self.backend.time += 901; self.guard.tick(self.UNKNOWN)
        self.assertEqual(self.backend.events.count('high'), 2)
    def test_same_guard_does_not_repeat_while_condition_persists(self):
        self.guard.tick(self.UNKNOWN); self.backend.time += 901; self.guard.tick(self.UNKNOWN)
        self.backend.time += qg.RUN_ALERT_DEDUP_SECONDS + 1; self.guard.tick(self.UNKNOWN)
        self.assertEqual(self.backend.events.count('high'), 1)
    def test_only_one_alert_per_run_across_guards(self):
        other = FakeBackend(self.dir)
        second = qg.Guard(self.config, dict(META, dispatch_id='ctx_other'), other, self.dir)
        for guard, backend in ((self.guard, self.backend), (second, other)):
            guard.tick(self.UNKNOWN); backend.time += 901; guard.tick(self.UNKNOWN)
        self.assertEqual(self.backend.events.count('high') + other.events.count('high'), 1)
    def test_other_runs_still_alert(self):
        other = FakeBackend(self.dir)
        second = qg.Guard(self.config, dict(META, dispatch_id='ctx_other', run_id='run_y'), other, self.dir)
        for guard, backend in ((self.guard, self.backend), (second, other)):
            guard.tick(self.UNKNOWN); backend.time += 901; guard.tick(self.UNKNOWN)
        self.assertEqual((self.backend.events.count('high'), other.events.count('high')), (1, 1))
    def test_dedup_expires(self):
        self.assertTrue(qg.claim_run_alert(self.dir, 'run_x', 'k', NOW))
        self.assertFalse(qg.claim_run_alert(self.dir, 'run_x', 'k', NOW + 3599))
        self.assertTrue(qg.claim_run_alert(self.dir, 'run_x', 'k', NOW + 3600))
    def test_prerequisite_alerts_dedupe_per_run(self):
        self.backend.target = None
        other = FakeBackend(self.dir, 'ctx_other'); other.target = None
        second = qg.Guard(self.config, dict(META, dispatch_id='ctx_other'), other, self.dir)
        self.guard.tick(snapshot()); second.tick(snapshot())
        self.assertEqual(self.backend.events.count('high') + other.events.count('high'), 1)
    def test_unsafe_run_id_is_refused(self):
        with self.assertRaises(ValueError):
            qg.claim_run_alert(self.dir, '../x', 'k', NOW)


class OneHandoffAtATime(GuardCase):
    def other(self, dispatch='ctx_other'):
        backend = FakeBackend(self.dir, dispatch)
        backend.time = self.backend.time
        return backend, qg.Guard(self.config, dict(META, dispatch_id=dispatch), backend, self.dir)
    def test_second_guard_defers_while_first_holds_the_pool(self):
        backend, second = self.other()
        self.assertTrue(self.guard.pool_lock.acquire(NOW, 0))
        self.addCleanup(self.guard.pool_lock.release)
        self.assertEqual(second.tick(snapshot()), 'continue')
        self.assertNotIn('target', backend.events); self.assertNotIn('notify:dispatch', backend.events)
    def test_second_guard_waits_for_spacing_then_reevaluates(self):
        backend, second = self.other()
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        backend.time = self.backend.time + 119
        self.assertEqual(second.tick(snapshot(age=-119)), 'continue')
        self.assertNotIn('target', backend.events)
        backend.time = self.backend.time + 120
        self.assertEqual(second.tick(snapshot(age=-60)), 'continue' if backend.target is None else 'handoff')
        self.assertIn('target', backend.events)
    def test_second_guard_can_find_no_target_after_first_used_it(self):
        backend, second = self.other()
        backend.target = None
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        backend.time = self.backend.time + 121
        self.assertEqual(second.tick(snapshot(age=-60)), 'continue')
        self.assertNotIn('notify:dispatch', backend.events)
    def test_different_pools_do_not_block_each_other(self):
        self.assertTrue(self.guard.pool_lock.acquire(NOW, 0))
        self.addCleanup(self.guard.pool_lock.release)
        other = qg.HandoffLock(self.dir, 'claude')
        self.assertTrue(other.acquire(NOW, 0))
        other.release()
    def test_lock_released_when_prerequisites_fail(self):
        self.backend.target = None
        self.guard.tick(snapshot())
        probe = qg.HandoffLock(self.dir, 'codex')
        self.assertTrue(probe.acquire(NOW, 120))
        probe.release()
    def test_failed_or_abandoned_handoff_does_not_impose_spacing(self):
        self.backend.handoff_on_notify = None
        self.backend.enabled = [False]
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        probe = qg.HandoffLock(self.dir, 'codex')
        self.assertTrue(probe.acquire(self.backend.time, 120))
        probe.release()
    def test_lock_released_when_handoff_raises(self):
        def fail():
            raise RuntimeError('fake')
        self.backend.leftovers = fail
        with self.assertRaises(RuntimeError):
            self.guard.tick(snapshot())
        probe = qg.HandoffLock(self.dir, 'codex')
        self.assertTrue(probe.acquire(NOW, 120))
        probe.release()
    def test_corrupt_lock_content_is_harmless(self):
        (self.dir/'handoff-codex.flock').write_text('garbage')
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
    def test_pool_lock_is_not_listed_as_a_guard(self):
        self.guard.pool_lock.acquire(NOW, 0)
        self.addCleanup(self.guard.pool_lock.release)
        self.assertEqual(qg.active_guards(self.dir), [])


class OutcomeVisibility(GuardCase):
    """Report B4: status has to show what the alerts could not deliver."""
    def undeliverable(self):
        def fail(*a, **k):
            raise RuntimeError('Orca offline')
        self.backend.send_status = fail
        real = self.backend.send
        def send(to, body, high=False):
            if high:
                fail()
            real(to, body, high)
        self.backend.send = send
    def phase(self):
        return json.loads((self.dir/'ctx_old-phase.json').read_text())
    def assertListed(self, phase, **facts):
        report = qg.status_report(self.dir, now=NOW)
        self.assertEqual([r['dispatch_id'] for r in report['undelivered_outcomes']], ['ctx_old'])
        record = report['undelivered_outcomes'][0]
        self.assertEqual(record['phase'], phase)
        self.assertFalse(record['alert_delivered'])
        self.assertTrue(record['reason'])
        for key, value in facts.items():
            self.assertEqual(record[key], value, key)
    def test_no_report_with_failed_alert_is_visible(self):
        self.backend.handoff_on_notify = None
        self.undeliverable()
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertListed('blocked', stop_sent=True, old_worker_stopped=True)
    def test_abandon_with_failed_alert_is_visible_and_says_worker_not_stopped(self):
        self.backend.handoff_on_notify = None
        self.backend.enabled = [False]
        self.undeliverable()
        self.assertEqual(self.guard.tick(snapshot()), 'abandoned')
        self.assertListed('abandoned', stop_sent=False, old_worker_stopped=False)
    def test_failed_start_with_failed_alert_shows_the_possible_dispatch(self):
        self.backend.start = lambda target, spec: (2, {'result': {'dispatchId': 'ctx_maybe'}})
        self.undeliverable()
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertListed('blocked', new_dispatch='ctx_maybe', start_exit=2, task_rewritten=True, start_attempted=True,
                          old_worker_stopped=True)
    def test_delivered_outcomes_leave_the_list(self):
        self.backend.handoff_on_notify = None
        self.guard.tick(snapshot())
        self.assertEqual(self.phase()['alert_delivered'], True)
        self.assertEqual(qg.undelivered_outcomes(self.dir), [])
    def test_falling_back_to_status_type_counts_as_delivered(self):
        def fail(to, body, high=False):
            if high:
                raise RuntimeError('rejected')
        self.backend.send = fail
        self.backend.send_status = lambda to, body: None
        self.assertTrue(self.guard.alert('x'))
    def test_alert_reports_failure(self):
        self.undeliverable()
        self.assertFalse(self.guard.alert('x'))
    def test_crash_during_alert_is_listed(self):
        # On disk as undelivered before the attempt, so dying mid-send still leaves it visible.
        real = self.guard.alert
        seen = []
        def alert(message):
            seen.append(self.phase()['alert_delivered'])
            return real(message)
        self.guard.alert = alert
        self.guard.block('why')
        self.assertEqual(seen, [False])
    def test_unfinished_record_belongs_to_the_interrupted_list(self):
        qg.write_private(self.dir/'ctx_a-phase.json', json.dumps(dict(dispatch_id='ctx_a', phase='starting', alert_delivered=False)))
        self.assertEqual(qg.undelivered_outcomes(self.dir), [])
        self.assertEqual([r['dispatch_id'] for r in qg.interrupted_handoffs(self.dir)], ['ctx_a'])
    def test_records_from_before_this_field_are_left_out(self):
        qg.write_private(self.dir/'ctx_a-phase.json', json.dumps(dict(dispatch_id='ctx_a', phase='blocked', reason='old')))
        self.assertEqual(qg.undelivered_outcomes(self.dir), [])
    def test_done_with_failed_notice_is_not_listed(self):
        self.backend.handoff_on_notify = None
        self.backend.views = [worker(), worker('succeeded')]
        self.undeliverable()
        self.assertEqual(self.guard.tick(snapshot()), 'done')
        self.assertEqual(qg.undelivered_outcomes(self.dir), [])
    def test_live_guard_is_not_listed_yet(self):
        qg.write_private(self.dir/'ctx_old-phase.json', json.dumps(dict(dispatch_id='ctx_old', phase='blocked', alert_delivered=False)))
        lock = qg.DispatchLock(self.dir, 'ctx_old')
        self.assertTrue(lock.acquire()); self.addCleanup(lock.close)
        self.assertEqual(qg.undelivered_outcomes(self.dir), [])
    def test_unreadable_phase_file_is_skipped(self):
        (self.dir/'x-phase.json').write_text('{')
        self.assertEqual(qg.undelivered_outcomes(self.dir), [])
    def test_facts_survive_into_interrupted_records(self):
        self.backend.handoff_on_notify = None
        self.backend.start = lambda *a: (_ for _ in ()).throw(KeyboardInterrupt())
        self.backend.handoff_on_notify = COMPLETE_HANDOFF
        with self.assertRaises(KeyboardInterrupt):
            self.guard.tick(snapshot())
        record = qg.interrupted_handoffs(self.dir)[0]
        self.assertEqual(record['phase'], 'starting')
        self.assertEqual((record['target'], record['stop_sent'], record['old_worker_stopped'], record['start_attempted'], record['task_rewritten']),
                         ('opus', True, True, True, True))
    def test_status_command_prints_the_new_lists(self):
        self.backend.handoff_on_notify = None
        self.undeliverable()
        self.guard.tick(snapshot())
        buf = io.StringIO()
        with patch.object(qg, 'restore_reminder_status', return_value={'active': False}), contextlib.redirect_stdout(buf):
            self.assertEqual(qg.main(['status', '--state-dir', str(self.dir)]), 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out['undelivered_outcomes'][0]['dispatch_id'], 'ctx_old')
        self.assertEqual(out['overdue_guards'], [])


class StuckGuardVisibility(GuardCase):
    """Report B4: a live guard whose lock is held looked the same however stuck it was."""
    def hold_lock(self, dispatch='ctx_old'):
        lock = qg.DispatchLock(self.dir, dispatch)
        self.assertTrue(lock.acquire()); self.addCleanup(lock.close)
    def write_phase(self, name, at, **extra):
        qg.write_private(self.dir/'ctx_old-phase.json', json.dumps(dict(extra, dispatch_id='ctx_old', phase=name, at=at)))
    def row(self, now=NOW):
        report = qg.status_report(self.dir, now=now)
        return report, report['active_guards'][0]
    def test_healthy_guard_shows_phase_and_ages(self):
        self.hold_lock()
        self.write_phase('waiting', NOW-30, limit_seconds=480)
        qg.write_private(self.dir/'ctx_old-heartbeat.json', json.dumps(dict(at=NOW-5, limit_seconds=300)))
        report, row = self.row()
        self.assertEqual((row['phase'], row['phase_age_seconds'], row['last_heartbeat_age_seconds']), ('waiting', 30, 5))
        self.assertFalse(row['overdue']); self.assertEqual(report['overdue_guards'], [])
    def test_overdue_phase_is_flagged_separately(self):
        self.hold_lock()
        self.write_phase('starting', NOW-601, limit_seconds=600, target='opus', start_attempted=True)
        report, row = self.row()
        self.assertTrue(row['overdue'])
        self.assertEqual(report['overdue_guards'], ['ctx_old'])
        self.assertEqual(row['target'], 'opus')
        self.assertIn('starting', row['overdue_reasons'][0])
    def test_limit_is_inclusive_of_the_boundary(self):
        self.hold_lock()
        self.write_phase('starting', NOW-600, limit_seconds=600)
        self.assertFalse(self.row()[1]['overdue'])
    def test_old_records_use_the_default_limit(self):
        self.hold_lock()
        self.write_phase('stopping', NOW-qg.DEFAULT_PHASE_LIMIT_SECONDS-1)
        self.assertTrue(self.row()[1]['overdue'])
        self.write_phase('stopping', NOW-qg.DEFAULT_PHASE_LIMIT_SECONDS)
        self.assertFalse(self.row()[1]['overdue'])
    def test_stale_heartbeat_is_flagged_without_any_phase(self):
        self.hold_lock()
        qg.write_private(self.dir/'ctx_old-heartbeat.json', json.dumps(dict(at=NOW-301, limit_seconds=300)))
        report, row = self.row()
        self.assertIsNone(row['phase'])
        self.assertEqual(report['overdue_guards'], ['ctx_old'])
        self.assertIn('心跳', row['overdue_reasons'][0])
    def test_heartbeat_limit_is_inclusive_of_the_boundary(self):
        self.hold_lock()
        qg.write_private(self.dir/'ctx_old-heartbeat.json', json.dumps(dict(at=NOW-300, limit_seconds=300)))
        self.assertFalse(self.row()[1]['overdue'])
    def test_final_phase_is_not_overdue(self):
        self.hold_lock()
        self.write_phase('blocked', NOW-9000, limit_seconds=None)
        self.assertFalse(self.row()[1]['overdue'])
    def test_guard_without_records_is_listed_plainly(self):
        self.hold_lock()
        report, row = self.row()
        self.assertEqual(row['dispatch_id'], 'ctx_old')
        self.assertIsNone(row['last_heartbeat_age_seconds']); self.assertFalse(row['overdue'])
    def test_corrupt_records_do_not_break_status(self):
        self.hold_lock()
        (self.dir/'ctx_old-phase.json').write_text('{'); (self.dir/'ctx_old-heartbeat.json').write_text('[]')
        self.assertFalse(self.row()[1]['overdue'])
    def test_phase_records_their_limit(self):
        self.assertEqual(self.guard.phase_limit('waiting'), (self.guard.cfg['grace_minutes'] + 5) * 60)
        self.assertEqual(self.guard.phase_limit('starting'), qg.START_OVERDUE_SECONDS)
        self.assertIsNone(self.guard.phase_limit('blocked'))
        self.guard.phase('waiting', target='opus')
        self.assertEqual(json.loads((self.dir/'ctx_old-phase.json').read_text())['limit_seconds'], 480)
    def test_age_counts_from_entering_the_phase_not_from_the_last_fact(self):
        self.guard.phase('starting')
        self.backend.time += 100
        self.guard.phase('starting', start_attempted=True)
        self.assertEqual(json.loads((self.dir/'ctx_old-phase.json').read_text())['at'], NOW)
        self.guard.phase('verifying')
        self.assertEqual(json.loads((self.dir/'ctx_old-phase.json').read_text())['at'], NOW + 100)
    def test_every_tick_beats_and_the_wait_loop_too(self):
        self.guard.tick(snapshot(used=10))
        beat = json.loads((self.dir/'ctx_old-heartbeat.json').read_text())
        self.assertEqual(beat['at'], NOW)
        self.backend.handoff_on_notify = None
        seen = []
        real_show = self.backend.show
        def show():
            if (self.dir/'ctx_old-phase.json').exists():
                seen.append(json.loads((self.dir/'ctx_old-heartbeat.json').read_text())['at'])
            return real_show()
        self.backend.show = show
        self.guard.tick(snapshot())
        self.assertGreater(len(set(seen)), 10)
    def test_heartbeat_failure_does_not_stop_the_guard(self):
        with patch.object(qg, 'write_private', side_effect=OSError('disk full')):
            self.assertEqual(self.guard.tick(snapshot(used=10)), 'continue')
        self.assertTrue(any('heartbeat failed' in text for text in self.backend.logs))
    def test_lookup_fills_in_a_launch_that_never_reported_back(self):
        self.write_phase('starting', NOW-5, start_attempted=True)
        calls = []
        def lookup(record, state):
            calls.append(record['dispatch_id'])
            return 'ctx_found'
        report = qg.status_report(self.dir, now=NOW, lookup=lookup)
        self.assertEqual(report['interrupted_handoffs'][0]['possible_new_dispatch'], 'ctx_found')
        self.assertEqual(calls, ['ctx_old'])
    def test_status_command_looks_up_the_unreported_launch(self):
        self.write_phase('starting', NOW-5, start_attempted=True)
        buf = io.StringIO()
        with patch.object(qg, 'restore_reminder_status', return_value={'active': False}), contextlib.redirect_stdout(buf):
            self.assertEqual(qg.main(['status', '--state-dir', str(self.dir)]), 0)
        # No meta here, so Orca cannot be asked: it says unknown rather than nothing, and asked nobody.
        self.assertEqual(json.loads(buf.getvalue())['interrupted_handoffs'][0]['possible_new_dispatch'], 'unknown')
    def test_lookup_only_runs_when_it_is_still_unknown(self):
        def lookup(record, state):
            raise AssertionError('should not ask')
        self.write_phase('starting', NOW-5)
        qg.status_report(self.dir, now=NOW, lookup=lookup)
        self.write_phase('starting', NOW-5, start_attempted=True, new_dispatch='ctx_known')
        qg.status_report(self.dir, now=NOW, lookup=lookup)
        self.write_phase('starting', NOW-5, start_attempted=True, possible_new_dispatch='ctx_known')
        qg.status_report(self.dir, now=NOW, lookup=lookup)
    def test_bound_dispatch_reads_only_and_never_raises(self):
        self.assertEqual(qg.bound_dispatch(dict(dispatch_id='ctx_old'), self.dir), 'unknown')
        (self.dir/'ctx_old.json').write_text(json.dumps(dict(META)))
        with patch.object(qg.Backend, 'task_state', return_value=dict(dispatch_id='ctx_new')):
            self.assertEqual(qg.bound_dispatch(dict(dispatch_id='ctx_old'), self.dir), 'ctx_new')
        with patch.object(qg.Backend, 'task_state', return_value=dict(dispatch_id='ctx_old')):
            self.assertIsNone(qg.bound_dispatch(dict(dispatch_id='ctx_old'), self.dir))
    def test_real_hung_guard_is_flagged_while_alive_and_listed_after_it_dies(self):
        # The evidence hang probe, kept: a real process holding the real lock, blocked inside start.
        code = """import sys, time
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import quota_guard as qg, test_quota_guard as t
state = Path(sys.argv[3]); b = t.FakeBackend(state)
b.start = lambda *a: time.sleep(120)
lock = qg.DispatchLock(state, 'ctx_old'); assert lock.acquire()
qg.Guard(t.enabled_config(), t.META, b, state).tick(t.snapshot())
"""
        proc = subprocess.Popen([sys.executable, '-c', code, str(ROOT/'scripts'), str(ROOT/'tests'), str(self.dir)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.addCleanup(proc.wait); self.addCleanup(proc.kill)
        path = self.dir/'ctx_old-phase.json'
        record = {}
        for _ in range(400):
            if path.exists():
                record = json.loads(path.read_text())
                if record.get('start_attempted'):
                    break
            self.assertIsNone(proc.poll(), 'probe exited early')
            time.sleep(.05)
        self.assertEqual(record['phase'], 'starting')
        record['at'] = time.time() - qg.START_OVERDUE_SECONDS - 1
        path.write_text(json.dumps(record))
        report = qg.status_report(self.dir)
        self.assertEqual(report['overdue_guards'], ['ctx_old'])
        self.assertEqual(report['interrupted_handoffs'], [])
        self.assertEqual((report['active_guards'][0]['phase'], report['active_guards'][0]['stop_sent']), ('starting', True))
        proc.kill(); proc.wait()
        report = qg.status_report(self.dir)
        self.assertEqual(report['active_guards'], [])
        self.assertEqual(report['interrupted_handoffs'][0]['phase'], 'starting')


class StartSupervision(GuardCase):
    """Report B4: start has no outer timeout on purpose, so something else must say when it hangs."""
    def setUp(self):
        super().setUp()
        for name, value in (('START_OVERDUE_SECONDS', .03), ('HEARTBEAT_SECONDS', .01)):
            patcher = patch.object(qg, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
    def slow_start(self, code=0, dispatch='ctx_new'):
        release = threading.Event()
        starts = []
        real_send = self.backend.send
        def send(to, body, high=False):
            real_send(to, body, high)
            if '未返回' in body:
                # Long enough for many more supervisor rounds, which must stay silent.
                threading.Timer(.15, release.set).start()
        self.backend.send = send
        def start(target, spec):
            starts.append(target)
            self.assertTrue(release.wait(10), 'supervisor never spoke')
            return code, {'result': {'dispatchId': dispatch}}
        self.backend.start = start
        return starts
    def test_overdue_start_alerts_once_and_is_never_resent(self):
        starts = self.slow_start()
        self.backend.task_script = [dict(status='dispatched', result=None), dict(status='dispatched', result=None)]
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(starts, ['opus'])
        alerts = [b for to, b, high in self.backend.sent if high and '未返回' in b]
        self.assertEqual(len(alerts), 1)
        self.assertIn('不会重发', alerts[0])
        self.assertEqual(self.backend.events.count('fail_task'), 1)
        phase = json.loads((self.dir/'ctx_old-phase.json').read_text())
        self.assertTrue(phase['start_overdue'])
        self.assertEqual(phase['new_dispatch'], 'ctx_new')
    def test_supervisor_names_a_dispatch_it_can_see(self):
        self.slow_start()
        real = self.backend.task_state
        def task_state():
            path = self.dir/'ctx_old-phase.json'
            if path.exists() and json.loads(path.read_text()).get('start_attempted'):
                return dict(status='dispatched', result=None, dispatch_id='ctx_seen')
            return real()
        self.backend.task_state = task_state
        self.guard.tick(snapshot())
        body = next(b for _, b, _ in self.backend.sent if '未返回' in b)
        self.assertIn('ctx_seen', body)
        self.assertEqual(json.loads((self.dir/'ctx_old-phase.json').read_text())['possible_new_dispatch'], 'ctx_seen')
    def test_supervisor_does_not_name_the_old_dispatch(self):
        self.slow_start()
        self.guard.tick(snapshot())
        body = next(b for _, b, _ in self.backend.sent if '未返回' in b)
        self.assertNotIn('可能的新 dispatch：ctx_old', body)
        self.assertIn('未知', body)
    def test_supervisor_read_failure_still_alerts(self):
        self.slow_start()
        real = self.backend.task_state
        def task_state():
            if (self.dir/'ctx_old-phase.json').exists() and json.loads((self.dir/'ctx_old-phase.json').read_text()).get('start_attempted'):
                raise RuntimeError('task-list down')
            return real()
        self.backend.task_state = task_state
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertTrue(any('未返回' in b for _, b, _ in self.backend.sent))
    def test_quick_start_says_nothing(self):
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertFalse(any('未返回' in b for _, b, _ in self.backend.sent))
        self.assertNotIn('start_overdue', json.loads((self.dir/'ctx_old-phase.json').read_text()))
    def test_supervisor_heartbeats_while_start_runs(self):
        beats = []
        def start(target, spec):
            before = json.loads((self.dir/'ctx_old-heartbeat.json').read_text())['at']
            self.backend.time += 50
            for _ in range(500):
                if json.loads((self.dir/'ctx_old-heartbeat.json').read_text())['at'] != before:
                    beats.append(1)
                    break
                time.sleep(.01)
            return 0, {'result': {'dispatchId': 'ctx_new'}}
        self.backend.start = start
        self.guard.tick(snapshot())
        self.assertEqual(beats, [1])
    def test_watcher_cannot_overwrite_the_phase_after_start_returned(self):
        done = threading.Event(); done.set()
        self.guard.phase('handoff_done')
        self.guard.report_slow_start(done)
        self.assertEqual(json.loads((self.dir/'ctx_old-phase.json').read_text())['phase'], 'handoff_done')
        self.assertFalse(any('未返回' in b for _, b, _ in self.backend.sent))


class IdentityFirst(GuardCase):
    """Report B4: a new dispatch id reaches the disk before anything that can fail."""
    def phase(self):
        return json.loads((self.dir/'ctx_old-phase.json').read_text())
    def test_usage_log_failure_keeps_the_handoff(self):
        seen = []
        def record(event):
            seen.append((self.phase()['new_dispatch'], self.phase()['phase'], self.phase()['alert_delivered']))
            raise OSError('disk full')
        self.backend.record = record
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(seen, [('ctx_new', 'handoff_done', False)])
        self.assertEqual(self.phase()['new_dispatch'], 'ctx_new')
        self.assertEqual(self.phase()['alert_delivered'], True)
        self.assertIn('已完成额度交接', self.backend.bodies('run:')[-1])
    def test_notice_failure_keeps_the_handoff_and_falls_back_to_an_alert(self):
        real = self.backend.send
        def send(to, body, high=False):
            if '已完成额度交接' in body and not high:
                raise RuntimeError('rejected')
            real(to, body, high)
        self.backend.send = send
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(self.guard.finished, 'handoff')
        self.assertEqual(self.phase()['alert_delivered'], True)
        self.assertTrue(self.backend.sent[-1][2])
    def test_all_notices_failing_shows_the_finished_handoff_in_status(self):
        real = self.backend.send
        def send(to, body, high=False):
            if '已完成额度交接' in body:
                raise RuntimeError('rejected')
            real(to, body, high)
        def fail(to, body):
            raise RuntimeError('rejected')
        self.backend.send, self.backend.send_status = send, fail
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        record = qg.undelivered_outcomes(self.dir)[0]
        self.assertEqual((record['phase'], record['new_dispatch']), ('handoff_done', 'ctx_new'))
    def test_spacing_is_marked_even_if_the_log_fails(self):
        def record(event):
            raise OSError('disk full')
        self.backend.record = record
        self.guard.tick(snapshot())
        probe = qg.HandoffLock(self.dir, 'codex')
        self.assertFalse(probe.acquire(self.backend.time + 119, 120))
    def test_failed_start_keeps_the_id_it_did_return(self):
        self.backend.start = lambda target, spec: (3, {'result': {'dispatchId': 'ctx_partial'}})
        self.assertEqual(self.guard.tick(snapshot()), 'blocked')
        self.assertEqual((self.phase()['new_dispatch'], self.phase()['start_exit']), ('ctx_partial', 3))
    def test_task_write_failure_says_no_start_was_attempted(self):
        def fail():
            raise RuntimeError('task-update down')
        self.backend.fail_task = fail
        with self.assertRaises(RuntimeError):
            self.guard.tick(snapshot())
        self.assertNotIn('start_attempted', self.phase())
        self.assertAbsent('start')
    def test_task_is_rewritten_with_the_shared_result(self):
        calls = []
        b = qg.Backend(CONFIG, dict(META, coordinator='term'), self.dir,
                       runner=lambda cmd, **kw: (calls.append(cmd), subprocess.CompletedProcess(cmd, 0, '{"ok": true}', ''))[1])
        b.fail_task()
        self.assertEqual(calls[0][calls[0].index('--result')+1], qg.HANDOFF_RESULT)


class RetryRefusal(unittest.TestCase):
    OK_TASK = dict(status='failed', result=qg.HANDOFF_RESULT if qg else '')
    def test_guard_note_and_stop_trace_are_the_only_open_states(self):
        self.assertIsNone(qg.retry_refusal(self.OK_TASK, 'ctx_old', 'ctx_old'))
        for result in (None, ''):
            self.assertIsNone(qg.retry_refusal(dict(status='blocked', result=result), 'ctx_old', 'ctx_old'))
    def test_everything_else_is_refused(self):
        for status, result in (('failed', 'cancelled'), ('failed', None), ('failed', ''), ('completed', 'x'),
                               ('completed', qg.HANDOFF_RESULT), ('blocked', 'x'), ('blocked', qg.HANDOFF_RESULT),
                               ('dispatched', None), ('ready', None), ('pending', None), (None, None)):
            self.assertTrue(qg.retry_refusal(dict(status=status, result=result), 'ctx_old', 'ctx_old'), (status, result))
    def test_latest_dispatch_must_be_the_one_being_retried(self):
        self.assertTrue(qg.retry_refusal(self.OK_TASK, 'ctx_new', 'ctx_old'))
        self.assertTrue(qg.retry_refusal(self.OK_TASK, None, 'ctx_old'))
    def test_latest_dispatch_reads_dispatch_show(self):
        calls = []
        def runner(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(dict(ok=True, result=dict(dispatch=dict(id='ctx_z')))), '')
        b = qg.Backend(CONFIG, dict(META, coordinator='term'), '/tmp', runner=runner)
        self.assertEqual(b.latest_dispatch(), 'ctx_z')
        self.assertEqual(calls[0][calls[0].index('--task')+1], 'task_x')
        self.assertIn('dispatch-show', calls[0])
        none = qg.Backend(CONFIG, dict(META), '/tmp', runner=lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, json.dumps(dict(ok=True, result=dict(dispatch=None))), ''))
        self.assertIsNone(none.latest_dispatch())


class LogPermissions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
    def spawn(self):
        class Proc:
            returncode = 0
            def poll(self):
                return 0
        with patch.object(qg.subprocess, 'Popen', return_value=Proc()) as popen:
            result = qg.spawn_guard(dict(META, dispatch_id='ctx_log'), self.dir)
        self.assertFalse(result['started'])
        return popen
    def test_new_log_is_private_despite_a_loose_umask(self):
        old = os.umask(0)
        self.addCleanup(os.umask, old)
        self.spawn()
        self.assertEqual(os.stat(self.dir/'ctx_log.log').st_mode & 0o777, 0o600)
    def test_existing_loose_log_is_tightened_and_kept(self):
        self.dir.joinpath('ctx_log.log').write_text('earlier\n')
        os.chmod(self.dir/'ctx_log.log', 0o644)
        self.spawn()
        self.assertEqual(os.stat(self.dir/'ctx_log.log').st_mode & 0o777, 0o600)
        self.assertEqual((self.dir/'ctx_log.log').read_text(), 'earlier\n')


class ProtectionDegraded(GuardCase):
    """Report S2: the first moment without a usable snapshot gets said, not only the fifteenth minute."""
    UNKNOWN = dict(ok=False, reason='额度查询失败（退出码 1）', providers={})
    def beat(self):
        return json.loads((self.dir/'ctx_old-heartbeat.json').read_text())
    def test_first_unknown_is_recorded_and_noticed_at_normal_priority(self):
        self.assertEqual(self.guard.tick(self.UNKNOWN), 'continue')
        self.assertEqual(self.beat()['protection_degraded']['since'], NOW)
        self.assertTrue(self.beat()['protection_degraded']['reasons'])
        self.assertTrue(any('保护降级' in text for text in self.backend.logs))
        to, body, high = self.backend.sent[-1]
        self.assertTrue(to.startswith('run:')); self.assertFalse(high)
        self.assertIn('没有保护', body)
        self.assertAbsent('high', 'notify:dispatch', 'stop')
    def test_notice_is_not_repeated_while_it_lasts(self):
        for _ in range(3):
            self.guard.tick(self.UNKNOWN)
        self.assertEqual(self.backend.events.count('notify:run'), 1)
    def test_escalates_at_fifteen_minutes_from_the_first_unknown(self):
        self.guard.tick(self.UNKNOWN)
        self.backend.time += qg.NO_DATA_ALERT_SECONDS
        self.guard.tick(self.UNKNOWN)
        self.assertEqual(self.backend.events.count('high'), 0)
        self.backend.time += 1
        self.guard.tick(self.UNKNOWN)
        self.assertEqual(self.backend.events.count('high'), 1)
        self.assertIn('没有保护', self.backend.sent[-1][1])
    def test_recovery_clears_the_record_and_is_logged(self):
        self.guard.tick(self.UNKNOWN)
        self.guard.tick(snapshot(used=10))
        self.assertIsNone(self.beat()['protection_degraded'])
        self.assertTrue(any('保护恢复' in text for text in self.backend.logs))
    def test_healthy_ticks_do_not_log_recovery(self):
        self.guard.tick(snapshot(used=10))
        self.assertFalse(any('保护恢复' in text for text in self.backend.logs))
    def test_flapping_source_notifies_once_per_run_window(self):
        for _ in range(3):
            self.guard.tick(self.UNKNOWN)
            self.guard.tick(snapshot(used=10))
        self.assertEqual(self.backend.events.count('notify:run'), 1)
        self.backend.time += qg.RUN_ALERT_DEDUP_SECONDS + 1
        self.guard.tick(self.UNKNOWN)
        self.assertEqual(self.backend.events.count('notify:run'), 2)
    def test_undeliverable_notice_does_not_stop_the_guard(self):
        def fail(*a, **k):
            raise RuntimeError('Orca offline')
        self.backend.send = fail
        self.assertEqual(self.guard.tick(self.UNKNOWN), 'continue')
        self.assertTrue(any('notice failed' in text for text in self.backend.logs))
    def test_status_shows_degraded_guard(self):
        self.guard.tick(self.UNKNOWN)
        lock = qg.DispatchLock(self.dir, 'ctx_old')
        self.assertTrue(lock.acquire()); self.addCleanup(lock.close)
        row = qg.status_report(self.dir, now=NOW)['active_guards'][0]
        self.assertEqual(row['protection_degraded']['since'], NOW)
    def test_failed_account_list_really_gives_an_unknown_tick(self):
        # S2 is documented, not bypassed: without a snapshot there is no protection.
        with tempfile.TemporaryDirectory() as logs:
            with patch.object(quota, 'read_codex_rate_limits') as reader:
                snap = quota.fetch_snapshot(quota.validate_policy(self.config['quota_policy']),
                                            runner=lambda *a, **k: subprocess.CompletedProcess(a, 1, b'', b''),
                                            clock=lambda: NOW, log_roots=[Path(logs)])
        self.assertFalse(reader.called)
        self.assertEqual(self.guard.tick(snap), 'continue')
        self.assertAbsent('stop', 'notify:dispatch')
        self.assertIn('没有保护', self.backend.sent[-1][1])


class InterruptedHandoff(GuardCase):
    def phase(self):
        return json.loads((self.dir/'ctx_old-phase.json').read_text())
    def test_no_phase_before_a_handoff_begins(self):
        self.guard.tick(snapshot(used=10))
        self.assertFalse((self.dir/'ctx_old-phase.json').exists())
    def test_crash_while_waiting_is_visible_in_status(self):
        def crash():
            raise KeyboardInterrupt
        self.backend.handoff_on_notify = None
        self.backend.on_sleep = crash
        with self.assertRaises(KeyboardInterrupt):
            self.guard.tick(snapshot())
        self.assertEqual(self.phase()['phase'], 'waiting')
        self.assertEqual(self.phase()['target'], 'opus')
        self.assertEqual([r['dispatch_id'] for r in qg.interrupted_handoffs(self.dir)], ['ctx_old'])
    def test_phase_is_on_disk_before_the_worker_is_notified(self):
        seen = []
        real = self.backend.send
        def send(to, body, high=False):
            if to.startswith('dispatch:') and not seen:
                seen.append(self.phase()['phase'])
            real(to, body, high)
        self.backend.send = send
        self.guard.tick(snapshot())
        self.assertEqual(seen, ['notifying'])
    def test_phase_is_on_disk_before_task_is_failed(self):
        seen = []
        self.backend.fail_task = lambda: seen.append(self.phase()['phase'])
        self.guard.tick(snapshot())
        self.assertEqual(seen, ['starting'])
    def test_final_phases_are_not_reported(self):
        self.assertEqual(self.guard.tick(snapshot()), 'handoff')
        self.assertEqual(self.phase()['phase'], 'handoff_done')
        self.assertEqual(self.phase()['new_dispatch'], 'ctx_new')
        self.assertEqual(qg.interrupted_handoffs(self.dir), [])
    def test_blocked_and_abandoned_record_reason(self):
        self.backend.stop_works = False
        self.guard.tick(snapshot())
        self.assertEqual(self.phase()['phase'], 'blocked')
        self.assertIn('无法确认', self.phase()['reason'])
    def test_live_guard_is_not_interrupted(self):
        qg.write_private(self.dir/'ctx_old-phase.json', json.dumps(dict(dispatch_id='ctx_old', phase='waiting')))
        lock = qg.DispatchLock(self.dir, 'ctx_old')
        self.assertTrue(lock.acquire()); self.addCleanup(lock.close)
        self.assertEqual(qg.interrupted_handoffs(self.dir), [])
    def test_unreadable_phase_file_is_skipped(self):
        (self.dir/'x-phase.json').write_text('{')
        self.assertEqual(qg.interrupted_handoffs(self.dir), [])
    def test_status_command_reports_it(self):
        qg.write_private(self.dir/'ctx_old-phase.json', json.dumps(dict(dispatch_id='ctx_old', phase='stopping')))
        buf = io.StringIO()
        with patch.object(qg, 'restore_reminder_status', return_value={'active': False}), contextlib.redirect_stdout(buf):
            self.assertEqual(qg.main(['status', '--state-dir', str(self.dir)]), 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out['interrupted_handoffs'][0]['phase'], 'stopping')
        self.assertEqual(out['active_guards'], [])


class WatchLoop(GuardCase):
    """Report S7 and the main() loop."""
    class Stub:
        def __init__(self, script, finished=None):
            self.script, self.finished = list(script), finished
            self.config, self.cfg, self.alerts = None, None, []
        def tick(self, snapshot):
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        def alert(self, message):
            self.alerts.append(message)
    def run_watch(self, guard, config=None):
        config = config or self.config
        return qg.watch(guard, META, self.backend, lambda: config, lambda pol: snapshot(used=10))
    def test_outcomes_map_to_exit_codes(self):
        for outcome, code in (('done', 0), ('handoff', 0), ('abandoned', 0), ('blocked', 1)):
            self.assertEqual(self.run_watch(self.Stub([outcome])), code, outcome)
    def test_polls_until_outcome(self):
        self.assertEqual(self.run_watch(self.Stub(['continue', 'continue', 'done'])), 0)
        self.assertEqual(self.backend.events.count('wait'), 2)
    def test_exits_when_disabled_or_pool_removed(self):
        disabled = copy.deepcopy(self.config); disabled['quota_policy']['quota_guard']['enabled'] = False
        self.assertEqual(self.run_watch(self.Stub([]), disabled), 0)
        other = copy.deepcopy(self.config); other['quota_policy']['quota_guard']['pools'] = ['claude']
        self.assertEqual(self.run_watch(self.Stub([]), other), 0)
    def test_reloads_config_each_poll(self):
        stub = self.Stub(['continue', 'done'])
        self.assertEqual(self.run_watch(stub), 0)
        self.assertIs(stub.config, self.config)
        self.assertIs(self.backend.config, self.config)
    def test_repeated_errors_alert_then_exit(self):
        stub = self.Stub([RuntimeError('worker-show down')] * 40)
        self.assertEqual(self.run_watch(stub), 1)
        self.assertEqual(len(stub.alerts), 2)
        self.assertIn('仍在重试', stub.alerts[0]); self.assertIn('已退出', stub.alerts[1])
        self.assertEqual(len(stub.script), 40 - qg.ERROR_EXIT_AFTER)
    def test_errors_below_threshold_stay_quiet_and_reset_on_success(self):
        script = [RuntimeError('x')] * (qg.ERROR_ALERT_AFTER - 1) + ['continue'] + [RuntimeError('x')] * (qg.ERROR_ALERT_AFTER - 1) + ['done']
        stub = self.Stub(script)
        self.assertEqual(self.run_watch(stub), 0)
        self.assertEqual(stub.alerts, [])
    def test_error_after_handoff_began_is_terminal(self):
        stub = self.Stub([RuntimeError('mid handoff'), 'done'], finished='blocked')
        self.assertEqual(self.run_watch(stub), 1)
        self.assertEqual(len(stub.alerts), 1)
        self.assertIn('不重试', stub.alerts[0])
    def test_undeliverable_alert_does_not_kill_the_loop(self):
        stub = self.Stub([RuntimeError('x')] * qg.ERROR_EXIT_AFTER)
        def alert(message):
            raise RuntimeError('delivery down')
        stub.alert = alert
        self.assertEqual(self.run_watch(stub), 1)
    def test_failed_alerts_preserve_blocked_reason_and_exit(self):
        self.backend.stop_works = False
        attempts = []
        original_send = self.backend.send
        def send(to, body, high=False):
            if high:
                attempts.append(body)
                raise RuntimeError('handoff failed')
            return original_send(to, body, high)
        def send_status(to, body):
            attempts.append(body)
            raise RuntimeError('status failed')
        self.backend.send = send
        self.backend.send_status = send_status
        self.assertEqual(qg.watch(self.guard, META, self.backend, lambda: self.config, lambda pol: snapshot()), 1)
        phase = json.loads((self.dir / 'ctx_old-phase.json').read_text())
        self.assertEqual(phase['phase'], 'blocked')
        self.assertIn('无法确认旧 worker 已停止', phase['reason'])
        self.assertEqual(attempts, [phase['reason'], phase['reason']])
        self.assertFalse(any('流程异常' in text for text in attempts))
        self.assertTrue(any('alert failed' in text for text in self.backend.logs))

    def test_real_guard_through_watch_alerts_on_show_failure(self):
        def show():
            raise RuntimeError('dispatch cleaned up')
        self.backend.show = show
        self.assertEqual(self.run_watch(self.guard), 1)
        self.assertEqual(self.backend.events.count('high'), 2)
    def main(self, config, *argv):
        with patch.object(qg.Path, 'read_text', return_value=json.dumps(config)), \
             patch.object(qg, 'load_meta', return_value=dict(META)), \
             patch.object(qg, 'watch', return_value=7) as watch:
            result = qg.main(list(argv) + ['--state-dir', str(self.dir)])
        return result, watch
    def test_main_runs_watch_with_lock_only_when_protected(self):
        result, watch = self.main(self.config, 'watch', '--dispatch', 'ctx_old')
        self.assertEqual(result, 7); watch.assert_called_once()
        disabled = copy.deepcopy(self.config); disabled['quota_policy']['quota_guard']['enabled'] = False
        result, watch = self.main(disabled, 'watch', '--dispatch', 'ctx_old')
        self.assertEqual(result, 0); watch.assert_not_called()
    def test_main_refuses_duplicate_guard(self):
        lock = qg.DispatchLock(self.dir, 'ctx_old')
        self.assertTrue(lock.acquire()); self.addCleanup(lock.close)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result, watch = self.main(self.config, 'watch', '--dispatch', 'ctx_old')
        self.assertEqual(result, 0); watch.assert_not_called()
        self.assertIn('already active', buf.getvalue())


class BackendReads(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.calls = []
    def backend(self, responder, meta=None):
        def runner(cmd, **kw):
            self.calls.append((cmd, kw))
            return responder(cmd, kw)
        base = dict(dispatch_id='ctx_x', run_id='run_x', task_id='task_x', coordinator='term_lead', kind='feature',
                    complexity='normal', agent='codex', worktree_path=str(self.dir))
        return qg.Backend(CONFIG, dict(base, **(meta or {})), self.dir, runner=runner)
    def test_send_types_and_priorities(self):
        b = self.backend(lambda c, k: subprocess.CompletedProcess(c, 0, '{"ok": true}', ''))
        b.send('run:r', 'alert', high=True)
        b.send('dispatch:d', 'notice')
        for (cmd, _), kind, priority in zip(self.calls, ('handoff', 'handoff'), ('high', 'normal')):
            self.assertEqual(cmd[cmd.index('--type') + 1], kind)
            self.assertEqual(cmd[cmd.index('--priority') + 1], priority)

    def test_alert_falls_back_to_high_status(self):
        def responder(cmd, kw):
            return subprocess.CompletedProcess(cmd, 1 if len(self.calls) == 1 else 0, '{"ok": true}', 'failure')
        b = self.backend(responder)
        qg.Guard(enabled_config(), META, b, self.dir).alert('original reason')
        self.assertEqual(len(self.calls), 2)
        cmd = self.calls[1][0]
        self.assertEqual(cmd[cmd.index('--type') + 1], 'status')
        self.assertEqual(cmd[cmd.index('--priority') + 1], 'high')
        self.assertEqual(cmd[cmd.index('--body') + 1], 'original reason')

    def test_git_state_uses_read_only_commands_without_shell(self):
        b = self.backend(lambda c, k: subprocess.CompletedProcess(c, 0, ' M existing.py\n', ''))
        text = b.git_state()
        self.assertEqual([c[0] for c in self.calls], [['git', 'status', '--short'], ['git', 'diff', 'HEAD', '--stat']])
        self.assertTrue(all(not c[1].get('shell') for c in self.calls))
        self.assertTrue(all(c[1]['env']['GIT_OPTIONAL_LOCKS'] == '0' for c in self.calls))
        self.assertIn('existing.py', text)
    def fingerprint_backend(self, **over):
        outputs = {'--show-toplevel': str(self.dir)+'\n', '--porcelain': ' M a\n', '--binary': 'diff\n', '--others': '',
                   '--stage': '', 'ls-tree': '', 'HEAD': 'abc\n'}
        outputs.update(over)
        def respond(cmd, kw):
            value = next(v for k, v in outputs.items() if k in cmd)
            return subprocess.CompletedProcess(cmd, 1 if value is None else 0, value or '', '')
        return self.backend(respond)
    def test_fingerprint_is_read_only_and_detects_changes(self):
        base = self.fingerprint_backend().fingerprint()
        self.assertEqual(base['head'], 'abc')
        read_only = {'rev-parse', 'status', 'diff', 'ls-files', 'ls-tree'}
        self.assertTrue(all(c[0][1] in read_only for c in self.calls))
        self.assertNotEqual(base, self.fingerprint_backend(**{'--porcelain': ' M b\n'}).fingerprint())
        self.assertNotEqual(base, self.fingerprint_backend(**{'--binary': 'other\n'}).fingerprint())
        self.assertEqual(base, self.fingerprint_backend().fingerprint())
    def test_fingerprint_on_a_real_repository_sees_edits_and_commits_and_writes_nothing(self):
        def git(*args):
            subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.test'] + list(args), cwd=self.dir,
                           check=True, capture_output=True, stdin=subprocess.DEVNULL)
        git('init', '-q')
        (self.dir/'a.bin').write_bytes(b'\xff\xfe one\n'); (self.dir/'b.txt').write_text('one\n')
        git('add', '.'); git('commit', '-qm', 'first')
        b = qg.Backend(CONFIG, dict(worktree_path=str(self.dir)), self.dir, runner=lambda cmd, **kw: subprocess.run(cmd, **kw))
        clean = b.fingerprint()
        index = (self.dir/'.git'/'index').read_bytes()
        (self.dir/'b.txt').write_text('two\n')
        edited = b.fingerprint()
        self.assertNotEqual(clean, edited); self.assertEqual(clean['head'], edited['head'])
        (self.dir/'b.txt').write_text('three\n')
        self.assertNotEqual(edited, b.fingerprint())  # same porcelain line, different content
        (self.dir/'a.bin').write_bytes(b'\xff\xfe two\n')
        b.fingerprint()  # non-UTF-8 diff must not raise
        (self.dir/'new.txt').write_text('x')
        self.assertIn('new.txt', b.git_state())
        self.assertEqual(index, (self.dir/'.git'/'index').read_bytes())
        git('add', '.'); git('commit', '-qm', 'second')
        self.assertNotEqual(clean['head'], b.fingerprint()['head'])
    def test_fingerprint_fails_outside_a_git_worktree(self):
        with self.assertRaises(RuntimeError):
            self.fingerprint_backend(**{'--porcelain': None}).fingerprint()
    def test_fingerprint_fails_when_registered_path_is_not_root(self):
        with self.assertRaises(RuntimeError):
            self.fingerprint_backend(**{'--show-toplevel': '/somewhere/else\n'}).fingerprint()
    def scan(self, ps_out, lsof_out, started=NOW):
        def respond(cmd, kw):
            self.assertEqual(kw['env']['LC_ALL'], 'C')
            return subprocess.CompletedProcess(cmd, 0, lsof_out if cmd[0] == 'lsof' else ps_out, '')
        return self.backend(respond, dict(dispatch_started_at=started)).leftovers()
    def test_leftovers_filters_by_cwd_and_start_time(self):
        lstart = lambda t: time.strftime('%a %b %d %H:%M:%S %Y', time.localtime(t))
        other = str(self.dir) + '-sibling'
        lsof = ('p11\nfcwd\nn%s\np12\nfcwd\nn%s/sub\np13\nfcwd\nn%s\np14\nfcwd\nn%s\n' % (self.dir, self.dir, self.dir, other))
        ps = ('11 1 %s old-esbuild\n12 1 %s node            \n13 1 %s demo runtime\n14 1 %s sibling\n99 1 %s nocwd\n'
              % (lstart(NOW-3600), lstart(NOW+5), lstart(NOW), lstart(NOW+5), lstart(NOW+5)))
        found = self.scan(ps, lsof)
        self.assertEqual([p['pid'] for p in found], [12, 13])
        self.assertEqual([p['command'] for p in found], ['node', 'demo runtime'])
    def test_leftovers_ignores_itself(self):
        lstart = time.strftime('%a %b %d %H:%M:%S %Y', time.localtime(NOW+5))
        me = os.getpid()
        self.assertEqual(self.scan('%d 1 %s /bin/python\n' % (me, lstart), 'p%d\nfcwd\nn%s\n' % (me, self.dir)), [])
    def test_leftovers_needs_a_baseline_and_working_tools(self):
        b = self.backend(lambda c, k: subprocess.CompletedProcess(c, 0, 'x', ''))
        with self.assertRaises(RuntimeError):
            b.leftovers()
        with self.assertRaises(RuntimeError):
            self.scan('', 'p1\n')
        with self.assertRaises(RuntimeError):
            self.scan('1 x\n', '')
    def test_leftovers_finds_a_real_process(self):
        proc = subprocess.Popen(['sleep', '30'], cwd=self.dir)
        self.addCleanup(proc.wait); self.addCleanup(proc.kill)
        real = lambda cmd, **kw: subprocess.run(cmd, **kw)
        meta = dict(worktree_path=str(self.dir), dispatch_started_at=time.time() - 5)
        found = qg.Backend(CONFIG, meta, self.dir, runner=real).leftovers()
        self.assertIn(proc.pid, [p['pid'] for p in found])
        meta['dispatch_started_at'] = time.time() + 5
        self.assertEqual(qg.Backend(CONFIG, meta, self.dir, runner=real).leftovers(), [])
    def context_backend(self, responder):
        ctx = self.dir/'entry.md'; ctx.write_text('initial')
        b = self.backend(responder, dict(context_file=str(ctx)))
        b.log = lambda m: None
        return b, ctx
    def test_context_extracts_text_only_and_keeps_authorisation_with_original_task(self):
        transcript = dict(transcript=dict(messages=[
            dict(role='user', blocks=[dict(type='text', text='PROMPT-NOISE')]),
            dict(role='assistant', blocks=[dict(type='text', text='跑到第三步'), dict(type='tool-call', args='TOOL-NOISE')]),
            dict(role='tool', blocks=[dict(type='tool-result', text='RESULT-NOISE')])]))
        def respond(cmd, kw):
            if cmd[0] == 'git':
                return subprocess.CompletedProcess(cmd, 0, ' M work.py\n', '')
            return subprocess.CompletedProcess(cmd, 0, json.dumps(dict(ok=True, result=transcript)), '')
        b, ctx = self.context_backend(respond)
        text = b.context('原任务：全绿才能提交', 'worker 报告')
        self.assertEqual(ctx.read_text(), text)
        for part in ('原任务：全绿才能提交', 'worker 报告', 'work.py', '跑到第三步', '继续完成原验收条件'):
            self.assertIn(part, text)
        for noise in ('PROMPT-NOISE', 'TOOL-NOISE', 'RESULT-NOISE', '"blocks"'):
            self.assertNotIn(noise, text)
        # S6: the successor keeps the original task's commit authority but must not discard work.
        self.assertIn('不得 reset、stash 或丢弃旧改动', text)
        self.assertIn('提交和删除按原任务授权', text)
        self.assertNotIn('不 commit', text)
    def test_context_survives_worker_read_failures(self):
        # O2: the old worker is already gone when this runs.
        def nonzero(cmd, kw):
            if cmd[0] == 'git':
                return subprocess.CompletedProcess(cmd, 0, '', '')
            return subprocess.CompletedProcess(cmd, 1, '', 'archive unavailable')
        def raises(cmd, kw):
            if cmd[0] == 'git':
                return subprocess.CompletedProcess(cmd, 0, '', '')
            raise subprocess.TimeoutExpired(cmd, 30)
        for responder in (nonzero, raises):
            b, _ = self.context_backend(responder)
            self.assertIn('无可用摘录', b.context('原任务', '报告'))
    def test_last_output_text_bounds_and_alternative_shapes(self):
        self.assertEqual(qg.last_output_text({'lines': ['a', 'b', 3]}), 'a\nb')
        self.assertEqual(qg.last_output_text({'tail': ['z']}), 'z')
        self.assertEqual(qg.last_output_text(None), '')
        long = {'transcript': {'messages': [dict(role='assistant', blocks=[dict(type='text', text='x'*9000)])]}}
        self.assertEqual(len(qg.last_output_text(long)), 6000)
    def task_backend(self, tasks):
        return self.backend(lambda c, k: subprocess.CompletedProcess(c, 0, json.dumps(dict(ok=True, result=dict(tasks=tasks))), ''))
    def test_task_state_reads_status_and_result(self):
        b = self.task_backend([dict(id='task_x', status='blocked', result='r', spec='long')])
        self.assertEqual(b.task_state(), dict(status='blocked', result='r', dispatch_id=None))
        b = self.task_backend([dict(id='task_x', status='dispatched', result='', dispatch_id='ctx_x')])
        self.assertEqual(b.task_state(), dict(status='dispatched', result='', dispatch_id='ctx_x'))
        with self.assertRaises(RuntimeError):
            self.task_backend([]).task_state()
    def test_guard_enabled_reads_current_file(self):
        on = copy.deepcopy(CONFIG); on['quota_policy']['quota_guard']['enabled'] = True
        b = self.backend(lambda c, k: None)
        with patch.object(qg.Path, 'read_text', return_value=json.dumps(on)):
            self.assertTrue(b.guard_enabled())
        with patch.object(qg.Path, 'read_text', return_value=json.dumps(disabled_config())):
            self.assertFalse(b.guard_enabled())
    def test_original_requires_entry_in_immutable_task(self):
        ctx, orig = self.dir/'c.md', self.dir/'o.md'
        ctx.write_text('c'); orig.write_text('original acceptance')
        meta = dict(context_file=str(ctx), original_spec_file=str(orig))
        def respond(spec):
            return lambda c, k: subprocess.CompletedProcess(c, 0, json.dumps(dict(ok=True, result=dict(tasks=[dict(id='task_x', spec=spec)]))), '')
        good = 'x' + qg.context_instruction(ctx)
        self.assertEqual(self.backend(respond(good), meta).original(), 'original acceptance')
        self.assertIsNone(self.backend(respond('no entry'), meta).original())
        self.assertIsNone(self.backend(respond(good), dict(meta, context_file=str(self.dir/'missing'))).original())
    def test_pagination_counts_all_attempts(self):
        responses = [{'workers': [{'dispatchId': 'ctx_new', 'taskId': 'task_x'}], 'page': {'hasMore': True, 'nextCursor': 'next'}},
                     {'workers': [{'dispatchId': 'ctx_x', 'taskId': 'task_x'}, {'dispatchId': 'ctx_y', 'taskId': 'other'}], 'page': {'hasMore': False}}]
        b = self.backend(lambda c, k: subprocess.CompletedProcess(c, 0, json.dumps({'ok': True, 'result': responses.pop(0)}), ''))
        self.assertEqual(b.attempts(), 2)
    def plan_backend(self, config, candidate, launchable=True, profile='opus', code=0):
        def respond(cmd, kw):
            self.assertIn('plan', cmd)
            out = json.dumps({'launchable': launchable, 'plan': {'profile': profile}, 'quota': {'candidates': [candidate]}})
            return subprocess.CompletedProcess(cmd, code, out, '')
        return qg.Backend(config, {'agent': 'codex', 'kind': 'feature', 'complexity': 'normal'}, self.dir,
                          runner=lambda cmd, **kw: (self.calls.append((cmd, kw)), respond(cmd, kw))[1])
    def test_plan_requires_known_eligible_other_pool(self):
        config = copy.deepcopy(CONFIG); config['tasks']['feature']['alternate'] = 'opus'
        ok = dict(profile='opus', status='eligible', auto_selectable=True)
        self.assertEqual(self.plan_backend(config, ok).plan(), 'opus')
        self.assertIsNone(self.plan_backend(config, dict(ok, status='unknown')).plan())
        self.assertIsNone(self.plan_backend(config, dict(ok, auto_selectable=False)).plan())
        self.assertIsNone(self.plan_backend(config, ok, launchable=False).plan())
        self.assertIsNone(self.plan_backend(config, ok, code=2).plan())
        with self.assertRaises(RuntimeError):
            self.plan_backend(config, ok, code=3).plan()
    def test_plan_never_considers_same_pool_or_escalation(self):
        config = copy.deepcopy(CONFIG)
        config['tasks']['feature'].update(primary='sol', alternate='sol', escalation='opus')
        self.assertIsNone(self.plan_backend(config, dict(profile='opus', status='eligible', auto_selectable=True)).plan())
        self.assertEqual(self.calls, [])
    def test_plan_passes_budget_and_explicit_profile(self):
        config = copy.deepcopy(CONFIG); config['tasks']['feature']['alternate'] = 'opus'
        b = self.plan_backend(config, dict(profile='opus', status='eligible', auto_selectable=True))
        b.meta.update(budget_session=4, budget_weekly=None)
        b.plan()
        cmd = self.calls[-1][0]
        self.assertEqual(cmd[cmd.index('--profile')+1], 'opus')
        self.assertEqual(cmd[cmd.index('--budget-session')+1], '4')
        self.assertNotIn('--budget-weekly', cmd)
    def test_start_uses_recorded_placement_without_outer_timeout(self):
        meta = dict(worktree='id:w::/x', coordinator='term_lead', budget_session=None, budget_weekly=3)
        def respond(cmd, kw):
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'result': {'dispatchId': 'n'}}), 'err')
        b = self.backend(respond, meta); b.log = lambda m: None
        code, receipt = b.start('opus', 'text')
        cmd, kw = self.calls[-1]
        self.assertEqual(cmd[cmd.index('--worktree')+1], 'id:w::/x')
        for flag, value in (('--task', 'task_x'), ('--retry-of', 'ctx_x'), ('--profile', 'opus'), ('--expected-profile', 'opus'),
                            ('--from', 'term_lead'), ('--budget-weekly', '3')):
            self.assertEqual(cmd[cmd.index(flag)+1], value)
        self.assertNotIn('timeout', kw)
        self.assertEqual((code, receipt['result']['dispatchId']), (0, 'n'))
    def test_send_and_stop_argv(self):
        b = self.backend(lambda c, k: subprocess.CompletedProcess(c, 1, 'not json', 'boom'))
        b.stop()
        self.assertIn('worker-stop', self.calls[-1][0])
        with self.assertRaises(RuntimeError):
            b.send('run:run_x', 'body', high=True)
        cmd = self.calls[-1][0]
        self.assertEqual(cmd[cmd.index('--priority')+1], 'high')
        self.assertEqual(cmd[cmd.index('--from')+1], 'term_lead')


class Registration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.wt = self.dir/'wt'; (self.wt/'.git').mkdir(parents=True)
        self.other = self.dir/'other'; (self.other/'.git').mkdir(parents=True)
        self.config = enabled_config()
        self.plan = dict(agent='codex', kind='feature', complexity='normal', profile='sol')
    def args(self, **over):
        base = dict(run='run_x', coordinator='term_lead', budget_session=None, budget_weekly=None,
                    worktree='id:wt', retry_of=None, task=None)
        return argparse.Namespace(**dict(base, **over))
    def context(self, spec='在 worktree 里修 bug'):
        return qg.prepare_context(spec, self.config, self.args(), 'codex', self.dir)
    def register(self, context, args=None, config=None, plan=None, **kw):
        info = {'worker': {'worktreeId': 'repo::' + str(self.wt)}, 'dispatch': {'hostScope': {'kind': 'local'}}}
        with patch.object(qg.Backend, 'command', side_effect=[info, {'dispatch': {}}]), \
             patch.object(qg, 'spawn_guard', return_value={'started': True}) as spawn:
            result = qg.register_start(config or self.config, plan or self.plan, args or self.args(),
                                       {'result': {'dispatchId': 'ctx_x', 'taskId': 'task_x'}}, context, self.dir, **kw)
        return result, spawn
    def test_registers_real_placement_and_baseline(self):
        result, spawn = self.register(self.context(), started_at=NOW)
        self.assertTrue(result['started'])
        meta = spawn.call_args[0][0]
        self.assertEqual(meta['worktree_path'], str(self.wt))
        self.assertEqual(meta['worktree'], 'id:repo::' + str(self.wt))
        self.assertEqual(meta['dispatch_started_at'], NOW)
    def test_default_baseline_is_now(self):
        with patch.object(qg.time, 'time', return_value=NOW):
            _, spawn = self.register(self.context())
        self.assertEqual(spawn.call_args[0][0]['dispatch_started_at'], NOW)
    def test_spec_naming_other_git_worktree_is_refused(self):
        result, spawn = self.register(self.context('改动请在 %s/src/a.py 完成' % self.other))
        self.assertFalse(result['started']); spawn.assert_not_called()
        self.assertIn(os.path.realpath(self.other), result['reason'])
    def test_spec_naming_own_worktree_or_plain_paths_is_fine(self):
        spec = '修 %s/src/a.py，参考 /usr/local/bin/orca 与 https://example.test/a/b' % self.wt
        self.assertTrue(self.register(self.context(spec))[0]['started'])
    def test_unprotected_pool_and_disabled_do_not_spawn(self):
        result, spawn = self.register(self.context(), plan=dict(self.plan, agent='claude'))
        self.assertFalse(result['started']); spawn.assert_not_called()
        result, spawn = self.register({'context_file': 'x', 'original_spec_file': 'y'}, config=disabled_config())
        self.assertFalse(result['started']); spawn.assert_not_called()
    def test_missing_context_or_remote_host_do_not_spawn(self):
        self.assertFalse(self.register({})[0]['started'])
        info = {'worker': {'worktreeId': 'repo::' + str(self.wt)}, 'dispatch': {'hostScope': {'kind': 'ssh'}}}
        with patch.object(qg.Backend, 'command', side_effect=[info]), patch.object(qg, 'spawn_guard') as spawn:
            r = qg.register_start(self.config, self.plan, self.args(), {'result': {'dispatchId': 'd', 'taskId': 't'}}, self.context(), self.dir)
        self.assertFalse(r['started']); spawn.assert_not_called()
    def test_receipt_without_ids_does_not_spawn(self):
        with patch.object(qg, 'spawn_guard') as spawn:
            r = qg.register_start(self.config, self.plan, self.args(), {'result': {}}, self.context(), self.dir)
        self.assertFalse(r['started']); spawn.assert_not_called()
    def test_coordinator_falls_back_to_creator_then_refuses(self):
        info = {'worker': {'worktreeId': 'repo::' + str(self.wt)}, 'dispatch': {}}
        for creator, started in (({'dispatch': {'creator_handle': 'term_c'}}, True), ({'dispatch': {}}, False)):
            with patch.object(qg.Backend, 'command', side_effect=[info, creator]), \
                 patch.object(qg, 'spawn_guard', return_value={'started': True}):
                r = qg.register_start(self.config, self.plan, self.args(coordinator=None),
                                      {'result': {'dispatchId': 'd', 'taskId': 't'}}, self.context(), self.dir)
            self.assertEqual(r['started'], started)
    def test_retry_is_not_rescanned(self):
        ctx = self.context('原任务在 %s' % self.other)
        result, _ = self.register(ctx, self.args(retry_of='ctx_old', task='task_x'))
        self.assertTrue(result['started'])
    def test_foreign_worktrees_ignores_urls_and_bounds_tokens(self):
        self.assertEqual(qg.foreign_worktrees('https://h/%s ftp:%s' % (self.other, self.other), str(self.wt)), [])
    def test_context_files_are_private(self):
        ctx = self.context('secret spec')
        for path in ctx.values():
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class PrepareContext(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.on = enabled_config()
    def ns(self, **over):
        return argparse.Namespace(**dict(dict(retry_of=None, worktree='id:w'), **over))
    def test_only_protected_pools_get_an_entry(self):
        self.assertTrue(qg.prepare_context('s', self.on, self.ns(), 'codex', self.dir))
        for agent in ('claude', 'antigravity'):
            self.assertEqual(qg.prepare_context('s', self.on, self.ns(), agent, self.dir), {})
    def test_disabled_gives_no_entry_and_writes_nothing(self):
        self.assertEqual(qg.prepare_context('s', disabled_config(), self.ns(), 'codex', self.dir), {})
        self.assertEqual(list(self.dir.iterdir()), [])
    def test_entry_is_stable_and_original_is_archived(self):
        first = qg.prepare_context('original acceptance', self.on, self.ns(), 'codex', self.dir)
        Path(self.dir, 'ctx_x.json').write_text(json.dumps(dict(first, task_id='task_x', run_id='run_x')))
        second = qg.prepare_context('original acceptance\n接手说明\nM file', self.on,
                                    self.ns(retry_of='ctx_x', task='task_x', run='run_x'), 'claude', self.dir)
        self.assertEqual(first, second)
        self.assertEqual(Path(first['original_spec_file']).read_text(), 'original acceptance')
        self.assertIn('M file', Path(first['context_file']).read_text())
    def test_retry_with_other_task_or_missing_entry_fails(self):
        first = qg.prepare_context('o', self.on, self.ns(), 'codex', self.dir)
        Path(self.dir, 'ctx_x.json').write_text(json.dumps(dict(first, task_id='task_x', run_id='run_x')))
        with self.assertRaises(ValueError):
            qg.prepare_context('o', self.on, self.ns(retry_of='ctx_x', task='other', run='run_x'), 'claude', self.dir)
        Path(first['context_file']).unlink()
        with self.assertRaises(ValueError):
            qg.prepare_context('o', self.on, self.ns(retry_of='ctx_x', task='task_x', run='run_x'), 'claude', self.dir)


class FakeRunner:
    def __init__(self, stdout='{}', code=0):
        self.calls, self.stdout, self.code = [], stdout, code
    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.code, self.stdout, '')


class CurrentPlacement(unittest.TestCase):
    """Default --worktree current: preflight from the coordinator terminal, confirmed from the worker's."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(os.path.realpath(tmp.name))
        self.wt = self.dir/'wt'; (self.wt/'.git').mkdir(parents=True)
        self.other = self.dir/'other'; (self.other/'.git').mkdir(parents=True)
        self.wid = 'repo::%s' % self.wt
        self.on = enabled_config()
        self.state = self.dir/'state'
        self.plan = dict(agent='codex', kind='feature', complexity='normal', profile='sol')
    def ns(self, **over):
        return argparse.Namespace(**dict(dict(worktree='current', coordinator='term_lead', retry_of=None, task=None,
                                              run='run_x', budget_session=None, budget_weekly=None), **over))
    def terminal(self, **over):
        return dict(dict(handle='term_x', worktreeId=self.wid, worktreePath=str(self.wt), executionHostId='local'), **over)
    def prepare(self, spec='修 bug', terminal=None, args=None, agent='codex', config=None):
        terminal = self.terminal() if terminal is None else terminal
        with patch.object(qg, 'terminal_info', return_value=terminal) as self.info:
            return qg.prepare_context(spec, config or self.on, args or self.ns(), agent, self.state)
    def assertRefused(self, fragment=None, **kw):
        with self.assertRaises(ValueError) as caught:
            self.prepare(**kw)
        self.assertIn('不受保护', str(caught.exception))
        if fragment:
            self.assertIn(fragment, str(caught.exception))
        self.assertFalse(self.state.exists(), 'a refused preflight must leave nothing behind')

    # --- preflight
    def test_preflight_gives_entry_and_records_expected_worktree(self):
        context = self.prepare()
        self.assertEqual(context['preflight_worktree_id'], self.wid)
        self.assertTrue(Path(context['context_file']).is_file())
        self.assertEqual(Path(context['original_spec_file']).read_text(), '修 bug')
    def test_preflight_asks_about_the_coordinator_named_by_from(self):
        self.prepare()
        self.info.assert_called_once_with('term_lead')
    def test_preflight_falls_back_to_the_callers_terminal(self):
        with patch.dict(os.environ, {'ORCA_TERMINAL_HANDLE': 'term_env'}):
            self.prepare(args=self.ns(coordinator=None))
        self.info.assert_called_once_with('term_env')
    def test_preflight_without_any_handle_is_unprotected(self):
        env = {k: v for k, v in os.environ.items() if k != 'ORCA_TERMINAL_HANDLE'}
        with patch.dict(os.environ, env, clear=True):
            self.assertRefused(args=self.ns(coordinator=None))
    def test_spec_naming_other_git_dir_is_unprotected(self):
        self.assertRefused(os.path.realpath(self.other), spec='去 %s/src/a.py 改' % self.other)
    def test_spec_naming_other_git_dir_under_home_is_unprotected(self):
        with patch.dict(os.environ, {'HOME': str(self.dir)}):
            self.assertRefused(os.path.realpath(self.other), spec='cd ~/other/src 再改')
    def test_spec_naming_own_worktree_or_plain_dirs_is_fine(self):
        with patch.dict(os.environ, {'HOME': str(self.dir)}):
            self.assertTrue(self.prepare(spec='改 %s/a.py 和 ~/wt/b.py，参考 ~/nothing 与 /usr/local/bin/orca' % self.wt))
    def test_unresolvable_placements_are_unprotected(self):
        (self.dir/'folder').mkdir()
        cases = dict(
            no_id=dict(worktreeId=None), no_path=dict(worktreePath=None), number_id=dict(worktreeId=7),
            number_path=dict(worktreePath=7), no_separator=dict(worktreeId=str(self.wt)),
            empty_repo=dict(worktreeId='::%s' % self.wt), relative_id=dict(worktreeId='repo::wt'),
            relative_path=dict(worktreePath='wt'), id_elsewhere=dict(worktreeId='repo::%s' % self.other),
            not_git_root=dict(worktreeId='repo::%s' % (self.wt/'sub'), worktreePath=str(self.wt/'sub')),
            folder=dict(worktreeId='repo::%s' % (self.dir/'folder'), worktreePath=str(self.dir/'folder')),
            missing=dict(worktreeId='repo::%s' % (self.dir/'gone'), worktreePath=str(self.dir/'gone')),
            remote=dict(executionHostId='ssh:box'))
        for name, over in cases.items():
            with self.subTest(name):
                self.assertRefused(terminal=self.terminal(**over))
    def test_no_terminal_information_is_unprotected(self):
        with patch.object(qg, 'terminal_info', return_value=None):
            with self.assertRaises(ValueError):
                qg.prepare_context('s', self.on, self.ns(), 'codex', self.state)
        self.assertFalse(self.state.exists())
    def test_id_may_name_the_worktree_through_a_symlink(self):
        link = self.dir/'link'; link.symlink_to(self.wt)
        self.assertTrue(self.prepare(terminal=self.terminal(worktreeId='repo::%s' % link)))
    def test_unprotected_pool_and_disabled_never_ask_orca(self):
        self.assertEqual(self.prepare(agent='claude'), {})
        self.info.assert_not_called()
        self.assertEqual(self.prepare(config=disabled_config()), {})
        self.info.assert_not_called()
        self.assertFalse(self.state.exists())
    def test_explicit_selectors_are_not_preflighted_or_scanned(self):
        for placement in ('id:repo::x', 'new-child', 'new-top-level'):
            context = self.prepare(spec='见 %s' % self.other, args=self.ns(worktree=placement))
            self.info.assert_not_called()
            self.assertNotIn('preflight_worktree_id', context)

    # --- terminal_info
    def fake_orca(self, body):
        path = self.dir/'orca'
        path.write_text('#!/bin/sh\necho "$@" > %s/argv\n%s\n' % (self.dir, body))
        path.chmod(0o755)
        return patch.object(qg, 'ORCA', str(path))
    def test_terminal_info_reads_a_real_process(self):
        answer = json.dumps({'ok': True, 'result': {'terminal': {'worktreeId': 'r::/x'}}})
        with self.fake_orca("cat <<'E'\n%s\nE" % answer):
            self.assertEqual(qg.terminal_info('term_a'), {'worktreeId': 'r::/x'})
        self.assertEqual((self.dir/'argv').read_text().split(), ['terminal', 'show', '--terminal', 'term_a', '--json'])
    def test_terminal_info_failures_are_none(self):
        ok = json.dumps({'ok': True, 'result': {'terminal': {'a': 1}}})
        refused = json.dumps({'ok': False, 'result': {'terminal': {'a': 1}}})
        for body in ("cat <<'E'\n%s\nE\nexit 1" % ok, "cat <<'E'\n%s\nE" % refused, 'echo not-json', 'echo "[1]"', 'echo \'{"ok": true}\'',
                     'echo \'{"ok": true, "result": {"terminal": "x"}}\''):
            with self.subTest(body), self.fake_orca(body):
                self.assertIsNone(qg.terminal_info('term_a'))
        with patch.object(qg, 'ORCA', str(self.dir/'absent')):
            self.assertIsNone(qg.terminal_info('term_a'))
        with patch.object(qg.subprocess, 'run', side_effect=subprocess.TimeoutExpired('orca', 1)):
            self.assertIsNone(qg.terminal_info('term_a'))

    def test_terminal_info_cannot_hang_or_read_the_callers_stdin(self):
        with patch.object(qg.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}')) as run:
            qg.terminal_info('term_a')
        self.assertGreater(run.call_args.kwargs['timeout'], 0)
        self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
    def test_relative_placement_is_refused_even_when_it_matches_from_this_cwd(self):
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.dir)
        self.assertRefused(terminal=self.terminal(worktreeId='repo::wt'))
        self.assertRefused(terminal=self.terminal(worktreePath='wt'))
    def test_home_paths_only_count_when_asked(self):
        # Explicit selectors keep the absolute-path-only scan.
        with patch.dict(os.environ, {'HOME': str(self.dir)}):
            self.assertEqual(qg.foreign_worktrees('cd ~/other', str(self.wt)), [])
            self.assertEqual(qg.foreign_worktrees('cd ~/other', str(self.wt), home=True), [os.path.realpath(self.other)])
    def test_home_token_scan_is_bounded(self):
        spec = ' '.join('~/x%d' % i for i in range(500))
        with patch.object(qg, 'git_root', return_value=None) as root:
            qg.foreign_worktrees(spec, str(self.wt), home=True)
        self.assertEqual(root.call_count, 200)

    # --- registration
    def register(self, context, terminal='same', worker_id='same', host=None, args=None):
        terminal = self.terminal() if terminal == 'same' else terminal
        worker_id = self.wid if worker_id == 'same' else worker_id
        info = {'worker': {'worktreeId': worker_id}, 'terminal': terminal, 'dispatch': {'hostScope': host or {'kind': 'local'}}}
        with patch.object(qg.Backend, 'command', side_effect=[info, {'dispatch': {}}]), \
             patch.object(qg, 'spawn_guard', return_value={'started': True}) as spawn:
            result = qg.register_start(self.on, self.plan, args or self.ns(), {'result': {'dispatchId': 'ctx_x', 'taskId': 'task_x'}},
                                       context, self.state, started_at=NOW)
        return result, spawn
    def assertNoProtection(self, result, spawn, context):
        self.assertFalse(result['started'])
        self.assertIn('不受保护', result['reason'])
        spawn.assert_not_called()
        self.assertFalse((self.state/'ctx_x.json').exists())
        self.assertEqual(qg.active_guards(self.state), [])
        # The Task spec already points here and tells the worker to stop if it is missing.
        self.assertTrue(Path(context['context_file']).is_file())
    def test_matching_placement_registers_the_resolved_id(self):
        result, spawn = self.register(self.prepare())
        self.assertTrue(result['started'])
        meta = spawn.call_args[0][0]
        self.assertEqual(meta['worktree'], 'id:' + self.wid)
        self.assertEqual(meta['worktree_path'], str(self.wt))
        self.assertEqual(meta['dispatch_started_at'], NOW)
    def test_current_records_coordinator_roots_for_the_resolved_worktree(self):
        roots = [dict(pid=500, started_at=NOW-100)]
        with patch.object(qg, 'coordinator_roots', return_value=roots) as walked:
            result, spawn = self.register(self.prepare())
        walked.assert_called_once_with(str(self.wt))
        self.assertEqual(spawn.call_args[0][0]['coordinator_roots'], roots)
        self.assertEqual(result['coordinator_roots'], roots)
    def test_mismatch_with_preflight_registers_nothing(self):
        context = self.prepare()
        moved = 'repo::%s' % self.other
        result, spawn = self.register(context, self.terminal(worktreeId=moved, worktreePath=str(self.other)), moved)
        self.assertNoProtection(result, spawn, context)
        self.assertIn(moved, result['reason'])
        self.assertIn(self.wid, result['reason'])
    def test_context_without_preflight_on_current_registers_nothing(self):
        context = self.prepare()
        del context['preflight_worktree_id']
        self.assertNoProtection(*self.register(context), context)
    def test_missing_worker_terminal_registers_nothing(self):
        # worker.worktreeId alone is not enough for current: no id-derived path.
        context = self.prepare()
        self.assertNoProtection(*self.register(context, terminal=None), context)
    def test_unreliable_worker_terminal_registers_nothing(self):
        context = self.prepare()
        self.assertNoProtection(*self.register(context, terminal=self.terminal(worktreePath=str(self.other))), context)
    def test_worker_id_disagreeing_with_its_terminal_registers_nothing(self):
        context = self.prepare()
        self.assertNoProtection(*self.register(context, worker_id='repo::%s' % self.other), context)
    def test_remote_worker_registers_nothing(self):
        context = self.prepare()
        result, spawn = self.register(context, host={'kind': 'ssh'})
        self.assertFalse(result['started']); spawn.assert_not_called()
    def test_foreign_scan_still_runs_after_start(self):
        context = self.prepare()
        Path(context['original_spec_file']).write_text('去 %s 改' % self.other)
        self.assertNoProtection(*self.register(context), context)
    def test_explicit_worktree_keeps_its_registration_rules(self):
        # No worker terminal in the view, id-derived path: still fine for an explicit selector.
        args = self.ns(worktree='id:' + self.wid)
        context = self.prepare(args=args)
        result, spawn = self.register(context, terminal=None, args=args)
        self.assertTrue(result['started'])
        self.assertEqual(spawn.call_args[0][0]['worktree'], 'id:' + self.wid)


class PlanWarnings(unittest.TestCase):
    def cand(self, agent='codex', **windows):
        return dict(agent=agent, windows={w: dict(remaining_pp=v, minutes_to_reset=120) for w, v in windows.items()})
    def test_warns_only_when_enabled_for_a_protected_pool_at_threshold(self):
        on = enabled_config()
        self.assertEqual(qg.plan_warnings(on, self.cand(session=50, weekly=50)), [])
        self.assertIn('session 剩余 9', qg.plan_warnings(on, self.cand(session=9, weekly=50))[0])
        self.assertIn('weekly', qg.plan_warnings(on, self.cand(session=50, weekly=5))[0])
        self.assertEqual(qg.plan_warnings(on, self.cand('claude', session=1, weekly=1)), [])
        self.assertEqual(qg.plan_warnings(disabled_config(), self.cand(session=1, weekly=1)), [])
    def test_plan_output_carries_the_warning(self):
        args = argparse.Namespace(budget_session=None, budget_weekly=None, profile='sol', effort=None)
        config = enabled_config()
        # Codex session headroom (40 pp for feature normal) would block Sol at 8 pp before the
        # guard's warning could be seen; this test is about the warning path, so its own copy turns headroom off.
        config['quota_policy']['session_headroom']['enabled'] = False
        with patch.object(quota, 'fetch_snapshot', return_value=dict(snapshot(used=92), queried_at_ms=NOW*1000, reason=None)), patch.object(quota.time, 'time', return_value=NOW):
            plan, _, _ = route.evaluate(config, 'feature', 'normal', args)
        self.assertEqual(plan['profile'], 'sol')
        self.assertTrue(any('额度守卫' in w for w in plan['warnings']), plan['warnings'])
    def test_route_adds_warning_and_never_fails_on_preview_error(self):
        plan = dict(profile='sol')
        result = dict(candidates=[dict(profile='sol', agent='codex', windows={'session': dict(remaining_pp=2, minutes_to_reset=60)})])
        self.assertEqual(len(route.guard_warnings(enabled_config(), plan, result)), 1)
        self.assertEqual(route.guard_warnings(enabled_config(), dict(profile=None), result), [])
        with patch.object(qg, 'plan_warnings', side_effect=RuntimeError('fake')):
            self.assertEqual(route.guard_warnings(enabled_config(), plan, result), [])


class RouteMain(unittest.TestCase):
    """route.main with a fake Orca: every branch of the retry check and the appended entry."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.spec = self.dir/'spec.md'; self.spec.write_text('authorized task')
        self.calls = []
        self.clock_now = self.clock_at_start = 0
        self.state = self.dir/'state'
        self.state.mkdir()
        self.config = enabled_config()
        self.receipt = json.dumps({'ok': True, 'result': {'dispatchId': 'ctx_new', 'taskId': 'task_new'}})
        self.orca_reads = {}
        self.use_real_register = False
    def plan_for(self, profile):
        plan = route.choose(self.config, 'feature', 'normal', profile)
        plan['fast_mode'] = {'apply': False}
        return plan
    def run_main(self, profile='sol', extra=(), kind='feature'):
        argv = ['route.py', 'start', '--kind', kind, '--run', 'run_x', '--spec-file', str(self.spec)] + list(extra)
        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            if 'worker-start' in cmd:
                self.clock_at_start = self.clock_now
            for word, stdout in self.orca_reads.items():
                if word in cmd:
                    return subprocess.CompletedProcess(cmd, 0, stdout)
            return subprocess.CompletedProcess(cmd, 0, self.receipt)
        out, err = io.StringIO(), io.StringIO()
        real = qg.prepare_context
        real_register = qg.register_start
        register = (patch.object(qg, 'register_start', lambda *a, **k: real_register(*a, state=self.state, **k))
                    if self.use_real_register else
                    patch.object(qg, 'register_start', return_value={'started': False, 'reason': 'test'}))
        with patch.object(sys, 'argv', argv), \
             patch.object(route.Path, 'read_text', autospec=True, side_effect=self.read), \
             patch.object(route, 'evaluate', return_value=(self.plan_for(profile), {'candidates': []}, {})), \
             register as self.register, \
             patch.object(qg, 'prepare_context', lambda s, c, a, agent: real(s, c, a, agent, self.state)), \
             patch.object(qg, 'load_meta', lambda d, state=None: REAL_LOAD_META(d, self.state)), \
             patch.object(qg, 'STATE', self.state), \
             patch.object(route, 'log_start'), patch.object(route.subprocess, 'run', fake_run), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = route.main()
        return code, out.getvalue(), err.getvalue()
    _read_text = Path.read_text
    def read(self, path, *a, **k):
        if path.name == 'routes.json':
            return json.dumps(self.config)
        return RouteMain._read_text(path, *a, **k)
    def spec_sent(self):
        argv = self.calls[-1]
        return argv[argv.index('--spec')+1]
    def worker_starts(self):
        return [c for c in self.calls if 'worker-start' in c]
    def test_codex_worker_gets_mandatory_entry_when_enabled(self):
        code, _, _ = self.run_main('sol', ['--worktree', 'id:w'])
        self.assertEqual(code, 0)
        self.assertIn('额度交接入口', self.spec_sent())
        self.assertTrue(self.register.called)
    def test_claude_and_antigravity_specs_are_unchanged_when_enabled(self):
        # Report B6.
        for profile in ('opus_high', 'gemini'):
            self.run_main(profile, ['--worktree', 'id:w'])
            self.assertNotIn('额度交接入口', self.spec_sent(), profile)
            self.assertIn('authorized task', self.spec_sent())
        self.assertEqual(list(self.state.iterdir()), [])
    def test_disabled_codex_spec_is_unchanged(self):
        self.config = disabled_config()
        self.run_main('sol', ['--worktree', 'id:w'])
        self.assertNotIn('额度交接入口', self.spec_sent())
    # --- default --worktree current
    def real_worktree(self, name='wt'):
        path = self.dir/name
        (path/'.git').mkdir(parents=True, exist_ok=True)
        return 'repo::%s' % path, str(path)
    def answer_terminal(self, wid, path):
        self.orca_reads['show'] = json.dumps({'ok': True, 'result': {'terminal': dict(
            handle='term_lead', worktreeId=wid, worktreePath=path, executionHostId='local')}})
    def answer_worker(self, wid, path):
        terminal = dict(handle='term_w', worktreeId=wid, worktreePath=path, executionHostId='local')
        self.orca_reads['worker-show'] = json.dumps({'ok': True, 'result': {
            'worker': {'worktreeId': wid}, 'terminal': terminal, 'dispatch': {'hostScope': {'kind': 'local'}}}})
    def terminal_shows(self):
        return [c for c in self.calls if c[1:3] == ['terminal', 'show']]
    def guard_reason(self, err):
        lines = [json.loads(l) for l in err.splitlines() if l.startswith('{"quota_guard"')]
        self.assertEqual(len(lines), 1, err)
        return lines[0]['quota_guard']
    def worker_argv(self):
        return self.worker_starts()[0]
    def test_current_without_a_resolvable_worktree_launches_unprotected(self):
        # The fake answers `terminal show` with a receipt that has no terminal in it.
        code, _, err = self.run_main('sol', ['--from', 'term_lead'])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.worker_starts()), 1)
        self.assertNotIn('额度交接入口', self.spec_sent())
        guard = self.guard_reason(err)
        self.assertFalse(guard['started'])
        self.assertIn('不受保护', guard['reason'])
    def test_current_gets_the_entry_when_preflight_resolves(self):
        wid, path = self.real_worktree()
        self.answer_terminal(wid, path)
        code, _, _ = self.run_main('sol', ['--from', 'term_lead'])
        self.assertEqual(code, 0)
        self.assertIn('额度交接入口', self.spec_sent())
        argv = self.worker_argv()
        self.assertEqual(argv[argv.index('--worktree')+1], 'current')
        # Preflight has to finish before the immutable Task spec is sent.
        self.assertLess(self.calls.index(self.terminal_shows()[0]), self.calls.index(argv))
        self.assertEqual(self.register.call_args[0][4]['preflight_worktree_id'], wid)
    def test_current_with_spec_naming_other_git_dir_launches_unprotected(self):
        wid, path = self.real_worktree()
        _, other = self.real_worktree('other')
        self.answer_terminal(wid, path)
        self.spec.write_text('去 %s/src 修改' % other)
        code, _, err = self.run_main('sol', ['--from', 'term_lead'])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.worker_starts()), 1)
        self.assertNotIn('额度交接入口', self.spec_sent())
        self.assertIn(os.path.realpath(other), self.guard_reason(err)['reason'])
        self.assertEqual(list(self.state.iterdir()), [])
    def test_claude_worker_on_current_is_untouched(self):
        wid, path = self.real_worktree()
        self.answer_terminal(wid, path)
        for profile in ('opus_high', 'gemini'):
            self.calls.clear()
            self.config = disabled_config()
            self.run_main(profile, ['--from', 'term_lead'])
            off_spec, off_argv = self.spec_sent(), self.worker_argv()
            self.calls.clear()
            self.config = enabled_config()
            self.run_main(profile, ['--from', 'term_lead'])
            self.assertEqual(self.spec_sent(), off_spec)
            self.assertEqual(self.worker_argv(), off_argv)
            self.assertEqual(self.terminal_shows(), [])
        self.assertEqual(list(self.state.iterdir()), [])
    def test_disabled_codex_on_current_is_untouched(self):
        wid, path = self.real_worktree()
        self.answer_terminal(wid, path)
        self.config = disabled_config()
        self.run_main('sol', ['--from', 'term_lead'])
        self.assertEqual(self.terminal_shows(), [])
        self.assertNotIn('额度交接入口', self.spec_sent())
        self.assertEqual(list(self.state.iterdir()), [])
    def test_explicit_worktrees_are_not_preflighted(self):
        for placement in (['--worktree', 'id:w'], ['--worktree', 'new-child', '--name', 'n', '--repo', 'id:r'],
                          ['--worktree', 'new-top-level', '--name', 'n', '--repo', 'id:r']):
            self.calls.clear()
            self.run_main('sol', placement)
            self.assertEqual(self.terminal_shows(), [], placement)
            self.assertIn('额度交接入口', self.spec_sent())
    def test_current_end_to_end_registers_and_hands_off_to_the_resolved_id(self):
        wid, path = self.real_worktree()
        self.answer_terminal(wid, path)
        self.answer_worker(wid, path)
        self.orca_reads['dispatch-show'] = json.dumps({'ok': True, 'result': {'dispatch': {}}})
        self.use_real_register = True
        with patch.object(qg, 'spawn_guard', return_value={'started': True}) as spawn:
            code, _, err = self.run_main('sol', ['--from', 'term_lead'])
        self.assertEqual(code, 0)
        self.assertTrue(self.guard_reason(err)['started'])
        meta = spawn.call_args[0][0]
        self.assertEqual(meta['worktree'], 'id:' + wid)
        self.assertEqual(meta['worktree_path'], path)
        # What the guard would run for the successor, and what route.py accepts for that retry.
        runner = FakeRunner()
        qg.Backend(self.config, meta, self.state, runner=runner).start('opus_high', 'x')
        cmd = runner.calls[0]
        self.assertEqual(cmd[cmd.index('--worktree')+1], 'id:' + wid)
        self.assertNotIn('current', cmd)
    def test_current_end_to_end_mismatch_starts_but_leaves_no_protection(self):
        wid, path = self.real_worktree()
        moved, moved_path = self.real_worktree('moved')
        self.answer_terminal(wid, path)
        self.answer_worker(moved, moved_path)
        self.use_real_register = True
        with patch.object(qg, 'spawn_guard', return_value={'started': True}) as spawn:
            code, _, err = self.run_main('sol', ['--from', 'term_lead'])
        self.assertEqual(code, 0)
        spawn.assert_not_called()
        guard = self.guard_reason(err)
        self.assertFalse(guard['started'])
        self.assertIn(moved, guard['reason'])
        self.assertFalse((self.state/'ctx_new.json').exists())
        self.assertEqual(qg.active_guards(self.state), [])
        # The Task already points at this entry and tells the worker to stop if it is missing.
        argv = self.worker_argv()
        entry = argv[argv.index('--spec')+1].split('额度交接入口（每次接单、任何动作前必须读取）：')[1].splitlines()[0]
        self.assertTrue(Path(entry).is_file())
    def test_baseline_is_taken_before_worker_start(self):
        def tick():
            self.clock_now += 1
            return self.clock_now
        self.clock_now = 1000
        with patch.object(route.time, 'time', side_effect=tick):
            self.run_main('sol', ['--worktree', 'id:w'])
        self.assertLessEqual(self.register.call_args.kwargs['started_at'], self.clock_at_start)
    def test_guard_prepare_failure_does_not_block_first_launch(self):
        with patch.object(route, 'evaluate', side_effect=AssertionError):
            pass
        self.config['quota_policy']['quota_guard']['pools'] = 'broken'
        code, _, err = self.run_main('sol', ['--worktree', 'id:w'])
        self.assertEqual(code, 0)
        self.assertIn('quota_guard', err)
        self.assertEqual(len(self.worker_starts()), 1)
    def write_meta(self, **over):
        ctx, orig = self.state/'c.md', self.state/'o.md'
        ctx.write_text('c'); orig.write_text('o')
        meta = dict(dispatch_id='ctx_old', task_id='task_x', run_id='run_x', worktree='id:w', coordinator='term_lead',
                    agent='codex', kind='feature', complexity='normal', context_file=str(ctx), original_spec_file=str(orig))
        meta.update(over)
        (self.state/'ctx_old.json').write_text(json.dumps(meta))
    RETRY = ['--task', 'task_x', '--retry-of', 'ctx_old', '--worktree', 'id:w', '--from', 'term_lead']
    def retry(self, original='spec', view=None, attempts=1, extra=None, task=None, latest='ctx_old'):
        view = view or after_worker_stop()
        task = task or dict(status='failed', result=qg.HANDOFF_RESULT, dispatch_id=None)
        with patch.object(qg.Backend, 'original', return_value=original), \
             patch.object(qg.Backend, 'show', return_value=view), \
             patch.object(qg.Backend, 'task_state', return_value=task), \
             patch.object(qg.Backend, 'latest_dispatch', return_value=latest), \
             patch.object(qg.Backend, 'attempts', return_value=attempts):
            return self.run_main('opus_high', extra or self.RETRY)
    def assertRefused(self, result):
        code, out, _ = result
        self.assertEqual(code, 2)
        self.assertIn('额度交接重试前置校验失败', out)
        self.assertEqual(self.worker_starts(), [])
    def test_retry_coordinator_falls_back_to_environment(self):
        self.write_meta()
        with patch.dict(os.environ, {'ORCA_TERMINAL_HANDLE': 'term_lead'}):
            code, _, _ = self.retry(extra=self.RETRY[:-2], task=dict(status='blocked', result=None, dispatch_id=None))
        self.assertEqual(code, 0)

    def test_retry_coordinator_mismatch_names_condition(self):
        self.write_meta()
        with patch.dict(os.environ, {'ORCA_TERMINAL_HANDLE': 'term_other'}):
            result = self.retry(extra=self.RETRY[:-2])
        self.assertRefused(result)
        self.assertIn('coordinator', result[1])

    def test_retry_success_reuses_task_without_spec(self):
        self.write_meta()
        code, _, _ = self.retry()
        self.assertEqual(code, 0)
        argv = self.worker_starts()[0]
        self.assertNotIn('--spec', argv); self.assertNotIn('--task-title', argv)
        self.assertEqual(argv[argv.index('--task')+1], 'task_x')
        self.assertEqual(argv[argv.index('--retry-of')+1], 'ctx_old')
        self.assertEqual(argv[argv.index('--worktree')+1], 'id:w')
    def test_retry_updates_the_recorded_entry(self):
        self.write_meta()
        self.retry()
        self.assertEqual((self.state/'c.md').read_text(), 'authorized task')
    def test_retry_refused_on_mismatched_task(self):
        self.write_meta(task_id='task_other'); self.assertRefused(self.retry())
    def test_retry_refused_on_mismatched_run(self):
        self.write_meta(run_id='run_other'); self.assertRefused(self.retry())
    def test_retry_refused_on_mismatched_worktree(self):
        self.write_meta(worktree='id:elsewhere'); self.assertRefused(self.retry())
    def test_retry_refused_on_mismatched_coordinator(self):
        self.write_meta(coordinator='term_other'); self.assertRefused(self.retry())
    def test_retry_refused_without_verified_entry(self):
        self.write_meta(); self.assertRefused(self.retry(original=None))
    def test_retry_refused_while_old_worker_alive(self):
        self.write_meta(); self.assertRefused(self.retry(view=worker('failed', dispatch='failed')))
    def test_retry_refused_at_attempt_limit(self):
        self.write_meta(); self.assertRefused(self.retry(attempts=2))
    def test_retry_refused_without_meta(self):
        self.assertRefused(self.retry())
    def test_retry_refused_on_a_task_with_someone_elses_conclusion(self):
        # Report B1: the check used to look only at the meta, the old terminal and the count.
        self.write_meta()
        for status, result in (('failed', 'cancelled'), ('completed', 'done'), ('dispatched', None), ('dispatched', ''),
                               ('ready', None), ('pending', None), ('blocked', 'coordinator taking over'),
                               ('failed', None), ('failed', '')):
            self.calls.clear()
            self.assertRefused(self.retry(task=dict(status=status, result=result, dispatch_id=None)))
    def test_retry_refused_when_another_dispatch_already_took_the_task(self):
        self.write_meta()
        self.assertRefused(self.retry(latest='ctx_someone_else'))
        self.assertRefused(self.retry(latest=None))
    def test_retry_refused_when_task_state_is_unreadable(self):
        self.write_meta()
        with patch.object(qg.Backend, 'task_state', side_effect=RuntimeError('down')), \
             patch.object(qg.Backend, 'original', return_value='spec'), \
             patch.object(qg.Backend, 'show', return_value=after_worker_stop()), \
             patch.object(qg.Backend, 'attempts', return_value=1):
            self.assertRefused(self.run_main('opus_high', self.RETRY))
    def test_retry_allowed_from_a_stop_left_for_the_coordinator(self):
        self.write_meta()
        for result in (None, ''):
            self.calls.clear()
            code, _, _ = self.retry(task=dict(status='blocked', result=result, dispatch_id=None))
            self.assertEqual(code, 0, result)
    def hold_lock(self, dispatch='ctx_old'):
        lock = qg.DispatchLock(self.state, dispatch)
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.close)
        return lock
    def test_retry_refused_while_the_guard_holds_the_lock(self):
        # Report R-S2: everything else about this retry is in order, so the lock is what refuses it.
        self.write_meta(); self.hold_lock()
        code, out, _ = result = self.retry()
        self.assertRefused(result)
        reason = json.loads(out)['blocked_reason']
        self.assertIn('额度守卫仍在运行', reason)
        self.assertIn(str(os.getpid()), reason)
        self.assertIn('一个重试操作者', reason)
        self.assertEqual((self.state/'c.md').read_text(), 'c')
    def test_guards_own_retry_passes_its_lock(self):
        self.write_meta(); self.hold_lock()
        with patch.object(qg.os, 'getppid', return_value=os.getpid()):
            code, _, _ = self.retry()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.worker_starts()), 1)
    def test_retry_refused_when_the_lock_holder_is_not_the_parent(self):
        self.write_meta(); self.hold_lock()
        with patch.object(qg.os, 'getppid', return_value=os.getpid() + 1):
            self.assertRefused(self.retry())
    def test_retry_refused_when_the_lock_holder_cannot_be_named(self):
        self.write_meta()
        path = self.state/'ctx_old.lock'; path.write_text('not json')
        with path.open('r') as held:
            qg.fcntl.flock(held, qg.fcntl.LOCK_EX)
            code, out, _ = result = self.retry()
        self.assertRefused(result)
        self.assertIn('额度守卫仍在运行', out)
    def test_retry_allowed_once_the_guard_has_exited(self):
        self.write_meta(); self.hold_lock().close()
        self.assertTrue((self.state/'ctx_old.lock').exists())
        self.assertEqual(self.retry()[0], 0)
    def test_another_dispatchs_guard_does_not_refuse(self):
        self.write_meta(); self.hold_lock('ctx_other')
        self.assertEqual(self.retry()[0], 0)
    def test_task_and_retry_of_must_come_together(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.run_main('sol', ['--task', 'task_x'])


class StartSurvivesGuardFailure(unittest.TestCase):
    def test_start_success_survives_guard_exception_and_preserves_receipt(self):
        cfg = copy.deepcopy(CONFIG)
        plan = route.choose(cfg, 'feature', 'normal', 'sol')
        plan['fast_mode'] = {'apply': False}; plan['profile'] = 'sol'
        receipt = json.dumps({'ok': True, 'result': {'dispatchId': 'ctx_x', 'taskId': 'task_x'}})
        with tempfile.TemporaryDirectory() as d:
            spec = Path(d, 'spec.md'); spec.write_text('authorized task')
            argv = ['route.py', 'start', '--kind', 'feature', '--run', 'run_x', '--spec-file', str(spec)]
            out, err = io.StringIO(), io.StringIO()
            with patch.object(route, 'load_config', return_value=CONFIG), patch.object(sys, 'argv', argv), \
                 patch.object(route, 'evaluate', return_value=(plan, {'candidates': []}, {})), \
                 patch.object(qg, 'prepare_context', return_value={}), \
                 patch.object(qg, 'register_start', side_effect=RuntimeError('fake guard failure')), \
                 patch.object(route, 'log_start'), \
                 patch.object(route.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, receipt)), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(route.main(), 0)
            self.assertEqual(out.getvalue(), receipt)
            self.assertIn('fake guard failure', err.getvalue())


class ProcessIsolation(unittest.TestCase):
    def test_lock_excludes_duplicate_and_status(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = qg.DispatchLock(Path(d), 'ctx_x'), qg.DispatchLock(Path(d), 'ctx_x')
            self.assertTrue(a.acquire()); self.addCleanup(a.close)
            self.assertFalse(b.acquire())
            self.assertEqual(qg.active_guards(Path(d))[0]['dispatch_id'], 'ctx_x')
            a.close(); self.assertEqual(qg.active_guards(Path(d)), [])
    def test_spawn_failure_is_isolated(self):
        with tempfile.TemporaryDirectory() as d, patch.object(qg.subprocess, 'Popen', side_effect=OSError('fake spawn error')):
            r = qg.spawn_guard({'dispatch_id': 'ctx_x'}, Path(d))
            self.assertFalse(r['started']); self.assertIn('fake spawn error', r['reason'])
    def test_spawn_detaches_all_terminal_streams(self):
        with tempfile.TemporaryDirectory() as d, \
             patch.object(qg.subprocess, 'Popen') as popen, \
             patch.object(qg, 'active_guards', return_value=[{'dispatch_id': 'ctx_x', 'pid': 123}]):
            self.assertTrue(qg.spawn_guard({'dispatch_id': 'ctx_x'}, Path(d))['started'])
        kw = popen.call_args.kwargs
        self.assertTrue(kw['start_new_session']); self.assertTrue(kw['close_fds'])
        self.assertEqual(kw['stdin'], subprocess.DEVNULL)
        self.assertIs(kw['stdout'], kw['stderr']); self.assertEqual(kw['cwd'], '/')
    def test_dry_run_reads_quota_without_mutations(self):
        buf = io.StringIO()
        with patch.object(qg, 'load_config', return_value=CONFIG), \
             patch.object(qg.quota, 'fetch_snapshot', return_value=snapshot()), \
             patch.object(qg.time, 'time', return_value=NOW), \
             patch.object(qg.subprocess, 'Popen', side_effect=AssertionError('must not spawn')), \
             patch.object(qg.Backend, 'command', side_effect=AssertionError('must not mutate')), \
             contextlib.redirect_stdout(buf):
            self.assertEqual(qg.main(['--dry-run']), 0)
        report = json.loads(buf.getvalue())
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['predictions']['codex']['trigger_windows'], ['session'])
        self.assertNotIn('worker_done --outcome failed', json.dumps(report, ensure_ascii=False))


class RealSubprocessGuard(unittest.TestCase):
    """A copy of the scripts against a fake orca executable, in a real detached process."""
    FAKE = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
state = {state!r}
if args[:2] == ['account', 'list']:
    print(json.dumps(dict(result=dict(rateLimits=dict())))); sys.exit(0)
done = os.path.exists(state + '/worker-finished')
view = dict(worker=dict(state='succeeded' if done else 'ready'),
            dispatch=dict(status='completed' if done else 'dispatched'),
            projection=dict(outcome='succeeded' if done else 'in_progress', liveness=dict(verdict='live')),
            terminalResource=dict(releaseState='not_requested'))
print(json.dumps(dict(ok=True, result=view)))
'''
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(os.path.realpath(tmp.name))
        self.state = self.dir/'state'; self.state.mkdir()
        scripts = self.dir/'skill'/'scripts'; scripts.mkdir(parents=True)
        fake = self.dir/'orca'
        fake.write_text(self.FAKE.format(python=sys.executable, state=str(self.state)))
        fake.chmod(0o755)
        for name in ('quota.py', 'quota_guard.py', 'route.py', 'jev_route.py'):
            text = (ROOT/'scripts'/name).read_text().replace('/usr/local/bin/orca', str(fake))
            (scripts/name).write_text(text)
        config = enabled_config()
        config['quota_policy']['source']['command'][0] = str(fake)
        config['quota_policy']['quota_guard']['poll_seconds'] = 0.05
        (self.dir/'skill'/'routes.json').write_text(json.dumps(config))
        self.scripts = scripts
        self.env = dict(os.environ, HOME=str(self.dir), PYTHONDONTWRITEBYTECODE='1')
        self.meta = dict(META, dispatch_id='ctx_real', worktree_path=str(self.dir))
    def spawn(self):
        code = ('import json,sys,quota_guard as q;'
                'print(json.dumps(q.spawn_guard(json.loads(sys.argv[1]), sys.argv[2])))')
        out = subprocess.run([sys.executable, '-c', code, json.dumps(self.meta), str(self.state)], cwd=self.scripts,
                             env=self.env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)
    def wait_until(self, condition, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(.05)
        return False
    def test_detached_guard_holds_lock_refuses_duplicate_and_exits_when_worker_succeeds(self):
        started = self.spawn()
        self.addCleanup(lambda: (self.state/'worker-finished').write_text(''))
        self.assertTrue(started['started'], started)
        self.assertEqual(os.getsid(started['pid']), started['pid'])
        self.assertEqual(os.stat(self.state/'ctx_real.json').st_mode & 0o777, 0o600)
        self.assertEqual(qg.active_guards(self.state)[0]['pid'], started['pid'])
        duplicate = subprocess.run([sys.executable, str(self.scripts/'quota_guard.py'), 'watch', '--dispatch', 'ctx_real',
                                    '--state-dir', str(self.state)], env=self.env, capture_output=True, text=True,
                                   timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(duplicate.returncode, 0)
        self.assertIn('already active', duplicate.stdout)
        (self.state/'worker-finished').write_text('')
        self.assertTrue(self.wait_until(lambda: qg.active_guards(self.state) == []))
        self.assertIn('guard exit: done', (self.state/'ctx_real.log').read_text())
    def test_killed_guard_releases_lock(self):
        started = self.spawn()
        self.assertTrue(started['started'], started)
        os.kill(started['pid'], 9)
        self.assertTrue(self.wait_until(lambda: qg.active_guards(self.state) == []))


class RealWiring(unittest.TestCase):
    """The whole path in real processes: guard, Backend, route.py retry and real git/ps/lsof, against a fake orca.

    The fake keeps a Task row the way real Orca does: dispatch_id only while active, and a stop leaving it blocked.
    """
    FAKE = r"""#!{python}
import json, os, sys, time
args = sys.argv[1:]
db_path, log_path = {db!r}, {log!r}
db = json.load(open(db_path))
now = int(time.time() * 1000)
def out(result):
    print(json.dumps(dict(ok=True, result=result)))
def opt(name):
    return args[args.index(name) + 1] if name in args else None
def window(used, minutes):
    return dict(usedPercent=used, windowMinutes=minutes, resetsAt=now + 100 * 60 * 1000)
if args[:2] == ['account', 'list']:
    def entry(used):
        return dict(status='ok', updatedAt=now, session=window(used, 300), weekly=window(10, 10080))
    print(json.dumps(dict(result=dict(rateLimits=dict(codex=entry(95), claude=entry(10), antigravity=entry(10))))))
    sys.exit(0)
cmd = args[1]
open(log_path, 'a').write(json.dumps(args) + '\n')
if cmd == 'worker-show':
    stopped = db['stopped'] and opt('--dispatch') == 'ctx_wire'
    out(dict(worker=dict(state='stopped' if stopped else 'ready', worktreeId='w'),
             dispatch=dict(status='failed' if stopped else 'dispatched', hostScope=dict(kind='local')),
             projection=dict(outcome='in_progress', liveness=dict(verdict='exited' if stopped else 'live')),
             terminalResource=dict(releaseState='released' if stopped else 'not_requested'),
             terminal=dict(worktreePath=db['worktree'])))
elif cmd == 'worker-list':
    out(dict(workers=[dict(dispatchId=d, taskId='task_wire') for d in db['dispatches']], page=dict(hasMore=False)))
elif cmd == 'task-list':
    out(dict(tasks=[dict(db['task'], id='task_wire', spec=db['spec'])]))
elif cmd == 'dispatch-show':
    out(dict(dispatch=dict(id=db['dispatches'][-1], creator_handle='term_lead')))
elif cmd == 'worker-stop':
    db['stopped'] = True
    db['task'] = dict(status='blocked', result=None, dispatch_id=None)
    if db.get('cancel_on_stop'):
        db['task'] = dict(status='failed', result='cancelled', dispatch_id=None)
    out(dict())
elif cmd == 'task-update':
    db['task'] = dict(status=opt('--status'), result=opt('--result'), dispatch_id=None)
    out(dict())
elif cmd == 'send':
    if db.get('report') and '请求交接' in opt('--body'):
        open(db['handoff'], 'w').write(db['report'])
    out(dict())
elif cmd == 'worker-read':
    out(dict(lines=['旧 worker 输出']))
elif cmd == 'worker-start':
    db['dispatches'].append('ctx_new')
    db['task'] = dict(status='dispatched', result=None, dispatch_id='ctx_new')
    out(dict(dispatchId='ctx_new', taskId='task_wire'))
else:
    out(dict())
json.dump(db, open(db_path, 'w'))
"""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(os.path.realpath(tmp.name))
        self.home = self.dir/'home'
        self.state = self.home/'.config'/'orca-model-routing'/'quota-guard'
        self.state.mkdir(parents=True, mode=0o700)
        self.wt = self.dir/'wt'; self.wt.mkdir()
        run = dict(cwd=self.wt, check=True, capture_output=True, stdin=subprocess.DEVNULL)
        subprocess.run(['git', 'init', '-q'], **run)
        (self.wt/'tracked').write_text('base')
        subprocess.run(['git', 'add', '.'], **run)
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.test', 'commit', '-qm', 'base'], **run)
        scripts = self.dir/'skill'/'scripts'; scripts.mkdir(parents=True)
        self.db, self.calls = self.dir/'db.json', self.dir/'calls.jsonl'
        self.calls.write_text('')
        fake = self.dir/'orca'
        fake.write_text(self.FAKE.format(python=sys.executable, db=str(self.db), log=str(self.calls)))
        fake.chmod(0o755)
        for name in ('quota.py', 'quota_guard.py', 'route.py', 'jev_route.py', 'setup_jev.py'):
            (scripts/name).write_text((ROOT/'scripts'/name).read_text().replace('/usr/local/bin/orca', str(fake)))
        config = enabled_config()
        config['quota_policy']['source']['command'][0] = str(fake)
        guard = config['quota_policy']['quota_guard']
        guard.update(poll_seconds=0.05, grace_minutes=0.02)
        (self.dir/'skill'/'routes.json').write_text(json.dumps(config))
        self.scripts, self.config = scripts, config
        self.env = dict(os.environ, HOME=str(self.home), PYTHONDONTWRITEBYTECODE='1')
        self.env.pop('ORCA_TERMINAL_HANDLE', None)
        original, context = self.state/'o.md', self.state/'c.md'
        original.write_text('原始任务'); context.write_text('原始任务')
        self.handoff = self.state/'ctx_wire-handoff.md'
        meta = dict(dispatch_id='ctx_wire', run_id='run_wire', task_id='task_wire', profile='sol', agent='codex', kind='feature',
                    complexity='normal', worktree='id:w', worktree_path=str(self.wt), coordinator='term_lead',
                    dispatch_started_at=time.time(), coordinator_roots=[], budget_session=None, budget_weekly=None,
                    context_file=str(context), original_spec_file=str(original))
        (self.state/'ctx_wire.json').write_text(json.dumps(meta))
        self.write_db(dict(stopped=False, worktree=str(self.wt), dispatches=['ctx_wire'], handoff=str(self.handoff),
                           task=dict(status='dispatched', result='', dispatch_id='ctx_wire'),
                           spec='原始任务' + qg.context_instruction(context)))
    def write_db(self, data):
        self.db.write_text(json.dumps(data))
    def change_db(self, **over):
        data = json.loads(self.db.read_text()); data.update(over); self.write_db(data)
    def report(self, pids='none'):
        return 'worktree: %s\n已完成 A\nbackground_pids: %s\n%s\n' % (self.wt, pids, qg.HANDOFF_END)
    def run_guard(self, expected):
        proc = subprocess.run([sys.executable, str(self.scripts/'quota_guard.py'), 'watch', '--dispatch', 'ctx_wire',
                               '--state-dir', str(self.state)], env=self.env, capture_output=True, text=True, timeout=40,
                              stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, expected, proc.stdout[-2000:] + proc.stderr[-2000:])
        self.phase = qg._read_json(self.state/'ctx_wire-phase.json')
        return json.loads(self.db.read_text())
    def called(self, name):
        return [json.loads(line) for line in self.calls.read_text().splitlines() if json.loads(line)[1] == name]
    def bodies(self):
        return [c[c.index('--body')+1] for c in self.called('send')]
    def assertNoTakeover(self, db):
        self.assertEqual(self.called('worker-start'), [])
        self.assertEqual(self.called('task-update'), [])
        self.assertEqual(db['dispatches'], ['ctx_wire'])
    def test_complete_report_goes_through_route_retry(self):
        self.change_db(report=self.report())
        db = self.run_guard(0)
        self.assertEqual(self.phase['phase'], 'handoff_done')
        self.assertEqual(self.phase['new_dispatch'], 'ctx_new')
        self.assertTrue(self.phase['alert_delivered'])
        start = self.called('worker-start')
        self.assertEqual(len(start), 1)
        self.assertEqual(start[0][start[0].index('--retry-of')+1], 'ctx_wire')
        update = self.called('task-update')[0]
        self.assertEqual(update[update.index('--result')+1], qg.HANDOFF_RESULT)
        self.assertEqual(db['task']['dispatch_id'], 'ctx_new')
        self.assertIn('已完成 A', (self.state/'c.md').read_text())
    def test_no_report_stops_alerts_and_leaves_the_task(self):
        db = self.run_guard(1)
        self.assertEqual(len(self.called('worker-stop')), 1)
        self.assertNoTakeover(db)
        self.assertEqual(db['task'], dict(status='blocked', result=None, dispatch_id=None))
        self.assertEqual((self.phase['phase'], self.phase['old_worker_stopped'], self.phase['alert_delivered']), ('blocked', True, True))
        self.assertIn('没有完整交接报告', self.bodies()[-1])
        high = self.called('send')[-1]
        self.assertEqual(high[high.index('--priority')+1], 'high')
    def test_live_background_pid_refuses_with_real_ps(self):
        proc = subprocess.Popen(['sleep', '60'], cwd='/', stdin=subprocess.DEVNULL)
        self.addCleanup(proc.wait); self.addCleanup(proc.kill)
        self.change_db(report=self.report(proc.pid))
        db = self.run_guard(1)
        self.assertNoTakeover(db)
        self.assertIn(str(proc.pid), self.bodies()[-1])
        self.assertIn('仍存活', self.bodies()[-1])
    def test_task_already_decided_touches_nothing(self):
        self.change_db(report=self.report(), task=dict(status='failed', result='cancelled', dispatch_id=None))
        db = self.run_guard(0)
        self.assertEqual(self.phase['phase'], 'abandoned')
        self.assertEqual(self.called('worker-stop'), [])
        self.assertNoTakeover(db)
        self.assertFalse(any('请求交接' in body for body in self.bodies()))
    def test_cancel_racing_the_stop_is_not_overridden(self):
        self.change_db(report=self.report(), cancel_on_stop=True)
        db = self.run_guard(0)
        self.assertEqual(self.phase['phase'], 'abandoned')
        self.assertNoTakeover(db)
        self.assertEqual(db['task']['result'], 'cancelled')
    def test_route_refuses_a_retry_of_a_cancelled_task_by_itself(self):
        self.change_db(stopped=True, task=dict(status='failed', result='cancelled', dispatch_id=None))
        spec = self.dir/'spec.md'; spec.write_text('续作')
        proc = subprocess.run([sys.executable, str(self.scripts/'route.py'), 'start', '--run', 'run_wire', '--kind', 'feature',
                               '--complexity', 'normal', '--profile', 'opus_high', '--expected-profile', 'opus_high',
                               '--worktree', 'id:w', '--task', 'task_wire', '--retry-of', 'ctx_wire', '--from', 'term_lead',
                               '--spec-file', str(spec)], env=self.env, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn('额度交接重试前置校验失败', proc.stdout)
        self.assertEqual(self.called('worker-start'), [])
    def test_manual_retry_during_a_real_guards_own_start_is_refused(self):
        # Task, attempts and the old terminal all look retryable at this moment: only the lock can refuse it.
        spec = self.dir/'spec.md'; spec.write_text('续作')
        out = self.dir/'manual.json'
        manual = [sys.executable, str(self.scripts/'route.py'), 'start', '--run', 'run_wire', '--kind', 'feature',
                  '--complexity', 'normal', '--profile', 'opus_high', '--expected-profile', 'opus_high', '--worktree', 'id:w',
                  '--task', 'task_wire', '--retry-of', 'ctx_wire', '--from', 'term_lead', '--spec-file', str(spec)]
        fake = self.dir/'orca'
        fake.write_text(fake.read_text().replace(
            "elif cmd == 'worker-start':\n",
            "elif cmd == 'worker-start':\n"
            "    manual = db.pop('manual', None)\n"
            "    if manual:\n"
            "        json.dump(db, open(db_path, 'w'))\n"
            "        import subprocess\n"
            "        done = subprocess.run(manual, capture_output=True, text=True, stdin=subprocess.DEVNULL)\n"
            "        json.dump([done.returncode, done.stdout], open(db['manual_out'], 'w'))\n"
            "        db = json.load(open(db_path))\n"))
        self.change_db(report=self.report(), manual=manual, manual_out=str(out))
        db = self.run_guard(0)
        code, stdout = json.loads(out.read_text())
        self.assertEqual(code, 2, stdout)
        self.assertIn('额度守卫仍在运行', stdout)
        self.assertEqual(self.phase['phase'], 'handoff_done')
        self.assertEqual(len(self.called('worker-start')), 1)
        self.assertEqual(db['dispatches'], ['ctx_wire', 'ctx_new'])
    def test_real_submodule_alerts_without_touching_the_worker(self):
        child = self.dir/'child'; child.mkdir()
        git_in(child, 'init', '-q')
        (child/'tracked.txt').write_text('base\n'); git_in(child, 'add', '.'); git_in(child, 'commit', '-qm', 'base')
        git_in(self.wt, 'submodule', 'add', '-q', str(child), 'module')
        git_in(self.wt, 'commit', '-qm', 'submodule')
        # The worker finishes by itself once the coordinator has heard: that is what ends this guard.
        fake = self.dir/'orca'
        fake.write_text(fake.read_text().replace(
            "elif cmd == 'send':\n",
            "elif cmd == 'send':\n    db['stopped'] = '子模块' in opt('--body')\n"))
        self.change_db(report=self.report())
        db = self.run_guard(0)
        self.assertNoTakeover(db)
        self.assertEqual(self.called('worker-stop'), [])
        self.assertEqual(self.phase, {})
        self.assertEqual(len(self.bodies()), 1)
        self.assertIn('Git 子模块', self.bodies()[0])
        self.assertIn('不受自动交接保护', self.bodies()[0])
    def test_untracked_edit_after_the_report_abandons_with_real_git(self):
        (self.wt/'new.py').write_text('worker version')
        self.change_db(report=self.report())
        # A different writer edits the file after the marker: the fake stop is the seam the edit goes through.
        fake = self.dir/'orca'
        fake.write_text(fake.read_text().replace(
            "    db['stopped'] = True\n",
            "    db['stopped'] = True\n    open(os.path.join(db['worktree'], 'new.py'), 'w').write('other writer')\n"))
        db = self.run_guard(0)
        self.assertEqual(self.phase['phase'], 'abandoned')
        self.assertNoTakeover(db)
        self.assertIn('又有变化', self.bodies()[-1])


class CoordinatorProcessTree(unittest.TestCase):
    """The scan skips the coordinator's own tree and nothing else."""
    ROOT, HOST = 500, 400
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(os.path.realpath(tmp.name))
        self.wt = self.dir/'wt'; self.wt.mkdir()
        self.pids, self.procs = [], []
        self.addCleanup(self.kill_all)
    def kill_all(self):
        for pid in self.pids:
            with contextlib.suppress(OSError):
                os.kill(pid, 9)
        for proc in self.procs:
            proc.wait()
            if proc.stdout:
                proc.stdout.close()
    @staticmethod
    def lstart(t):
        return time.strftime('%a %b %d %H:%M:%S %Y', time.localtime(t))
    def fake_runner(self, rows, cwds):
        """rows: (pid, ppid, start, comm); cwds: pid -> directory, absent for the unreadable."""
        ps = ''.join('%d %d %s %s\n' % (p, pp, self.lstart(t), c) for p, pp, t, c in rows)
        lsof = ''.join('p%d\nfcwd\nn%s\n' % item for item in cwds.items())
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, lsof if cmd[0] == 'lsof' else ps, '')
    def scan(self, rows, in_worktree, roots=None, log=None):
        cwds = {pid: str(self.wt) for pid in in_worktree}
        meta = dict(worktree_path=str(self.wt), dispatch_started_at=NOW)
        if roots is not None:
            meta['coordinator_roots'] = roots
        b = qg.Backend(CONFIG, meta, self.dir, runner=self.fake_runner(rows, cwds))
        b.log = log or (lambda message: None)
        return [p['pid'] for p in b.leftovers()]
    def tree(self):
        # root (older than the dispatch) -> shell whose cwd moved away -> check --wait in the worktree
        return [(self.ROOT, self.HOST, NOW-100, '/bin/claude'), (501, self.ROOT, NOW+1, '/bin/zsh'),
                (502, 501, NOW+2, '/usr/local/bin/orca')]
    def root(self, pid=None, started=NOW-100):
        return dict(pid=pid or self.ROOT, started_at=int(time.mktime(time.strptime(self.lstart(started), '%a %b %d %H:%M:%S %Y'))))

    # --- scan rules on fake tables
    def test_descendant_through_an_outside_cwd_parent_is_excluded(self):
        self.assertEqual(self.scan(self.tree(), [502], [self.root()]), [])
    def test_same_tree_is_refused_when_no_root_was_recorded(self):
        self.assertEqual(self.scan(self.tree(), [502]), [502])
        self.assertEqual(self.scan(self.tree(), [502], []), [502])
    def test_a_root_started_after_the_dispatch_is_itself_excluded(self):
        rows = [(self.ROOT, self.HOST, NOW+1, '/bin/claude')]
        self.assertEqual(self.scan(rows, [self.ROOT], [self.root(started=NOW+1)]), [])
    def test_orphan_is_refused(self):
        rows = self.tree() + [(1, 0, NOW-999, '/sbin/launchd'), (600, 1, NOW+3, '/x/demo-runtime')]
        self.assertEqual(self.scan(rows, [600], [self.root()]), [600])
    def test_child_of_an_orphan_is_refused(self):
        rows = self.tree() + [(1, 0, NOW-999, '/sbin/launchd'), (600, 1, NOW+3, '/x/demo-runtime'), (601, 600, NOW+4, '/x/helper')]
        self.assertEqual(self.scan(rows, [600, 601], [self.root()]), [600, 601])
    def test_init_cannot_stand_in_as_a_root(self):
        rows = [(1, 0, NOW-100, '/sbin/launchd'), (600, 1, NOW+3, '/x/demo-runtime')]
        self.assertEqual(self.scan(rows, [600], [self.root(pid=1)]), [600])
    def test_reused_pid_is_not_excluded(self):
        for recorded in (NOW-200, NOW-50):
            self.assertEqual(self.scan(self.tree(), [502], [self.root(started=recorded)]), [502])
    def test_exited_root_excludes_nothing(self):
        rows = self.tree()[1:]  # the root's row is gone
        self.assertEqual(self.scan(rows, [502], [self.root()]), [502])
    def test_one_exited_root_does_not_disable_a_live_one(self):
        self.assertEqual(self.scan(self.tree(), [502], [self.root(pid=999), self.root()]), [])
    def test_other_process_in_the_worktree_is_refused(self):
        rows = self.tree() + [(700, self.HOST, NOW+1, '/bin/zsh'), (701, 700, NOW+2, '/bin/codex')]
        self.assertEqual(self.scan(rows, [502, 701], [self.root()]), [701])
    def test_scan_terminates_on_a_parent_cycle(self):
        rows = [(800, 801, NOW+1, '/x/a'), (801, 800, NOW+1, '/x/b')]
        self.assertEqual(self.scan(rows, [800], [self.root()]), [800])
    def test_refused_process_reports_pid_name_and_start_only(self):
        rows = [(600, 1, NOW+3, 'demo-runtime    ')]
        b = qg.Backend(CONFIG, dict(worktree_path=str(self.wt), dispatch_started_at=NOW), self.dir,
                       runner=self.fake_runner(rows, {600: str(self.wt)}))
        self.assertEqual(b.leftovers(), [dict(pid=600, command='demo-runtime',
                                              started=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(NOW+3)))])
    def test_exclusions_are_logged_with_the_root_count(self):
        lines = []
        self.scan(self.tree(), [502], [self.root(), self.root(pid=999)], log=lines.append)
        self.assertEqual(len(lines), 1)
        self.assertIn('登记协调者根 2 个，仍有效 1 个', lines[0])
        self.assertIn('"pid": 502', lines[0])
    def test_dead_roots_are_logged_even_when_nothing_is_excluded(self):
        lines = []
        self.assertEqual(self.scan(self.tree(), [502], [self.root(pid=999)], log=lines.append), [502])
        self.assertIn('仍有效 0 个', lines[0])
    def test_no_roots_logs_nothing(self):
        lines = []
        self.scan(self.tree(), [502], log=lines.append)
        self.assertEqual(lines, [])
    def test_malformed_ps_row_fails_the_scan_instead_of_hiding_a_process(self):
        for bad in ('600 1 not-a-date\n', 'x 1 Wed Nov  4 07:00:00 2026 /a\n', '600\n'):
            runner = lambda cmd, bad=bad, **kw: subprocess.CompletedProcess(cmd, 0, 'p600\nn%s\n' % self.wt if cmd[0] == 'lsof' else bad, '')
            b = qg.Backend(CONFIG, dict(worktree_path=str(self.wt), dispatch_started_at=NOW), self.dir, runner=runner)
            with self.assertRaises(RuntimeError):
                b.leftovers()
    def test_empty_lsof_fails_the_scan(self):
        runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, '' if cmd[0] == 'lsof' else '600 1 %s /x\n' % self.lstart(NOW+1), '')
        b = qg.Backend(CONFIG, dict(worktree_path=str(self.wt), dispatch_started_at=NOW), self.dir, runner=runner)
        with self.assertRaises(RuntimeError):
            b.leftovers()
    def test_ps_failure_fails_the_scan(self):
        row = '600 1 %s /x\n' % self.lstart(NOW)
        for code, out in ((1, row), (0, '')):
            runner = lambda cmd, code=code, out=out, **kw: subprocess.CompletedProcess(cmd, code, out, '')
            with self.assertRaises(RuntimeError):
                qg.process_table(runner)
    def test_missing_command_name_is_reported_as_unknown(self):
        table = qg.process_table(lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, '600 1 %s\n' % self.lstart(NOW), ''))
        self.assertEqual(table[600]['command'], '?')

    # --- recording roots on fake tables
    def roots(self, rows, cwds, me=300):
        with patch.object(qg.os, 'getppid', return_value=me):
            return qg.coordinator_roots(str(self.wt), self.fake_runner(rows, cwds))
    def chain(self):
        # tool shell -> claude -> login shell -> login (cwd unreadable) -> terminal host -> launchd
        return [(300, 301, NOW-1, 'zsh'), (301, 302, NOW-50, 'claude'), (302, 303, NOW-60, '-/bin/zsh'), (303, 304, NOW-60, 'login'),
                (304, 1, NOW-999, 'orca-terminal-host'), (1, 0, NOW-9999, 'launchd')]
    def test_records_ancestors_in_the_worktree_with_start_times(self):
        cwds = {300: str(self.wt), 301: str(self.wt), 302: str(self.wt), 304: str(self.dir)}
        self.assertEqual(self.roots(self.chain(), cwds), [dict(pid=300, started_at=NOW-1), dict(pid=301, started_at=NOW-50), dict(pid=302, started_at=NOW-60)])
    def test_subdirectory_counts_and_sibling_prefix_does_not(self):
        sub = self.wt/'sub'; sub.mkdir()
        sibling = self.dir/'wt-sibling'; sibling.mkdir()
        self.assertEqual([r['pid'] for r in self.roots(self.chain(), {300: str(sub)})], [300])
        self.assertEqual(self.roots(self.chain(), {300: str(sibling)}), [])
    def test_worktree_reached_through_a_symlink_still_matches(self):
        link = self.dir/'link'; link.symlink_to(self.wt)
        with patch.object(qg.os, 'getppid', return_value=300):
            roots = qg.coordinator_roots(str(link), self.fake_runner(self.chain(), {300: str(self.wt)}))
        self.assertEqual([r['pid'] for r in roots], [300])
    def test_shell_that_moved_away_still_finds_the_coordinator(self):
        cwds = {300: '/', 301: str(self.wt), 302: str(self.wt)}
        self.assertEqual([r['pid'] for r in self.roots(self.chain(), cwds)], [301, 302])
    def test_ancestors_above_a_gap_are_not_recorded(self):
        # Orca itself launched from the worktree would otherwise cover every worker it hosts.
        cwds = {300: str(self.wt), 304: str(self.wt)}
        self.assertEqual([r['pid'] for r in self.roots(self.chain(), cwds)], [300])
    def test_outside_cwd_above_the_run_stops_it(self):
        cwds = {300: str(self.wt), 301: '/elsewhere', 302: str(self.wt)}
        self.assertEqual([r['pid'] for r in self.roots(self.chain(), cwds)], [300])
    def test_no_ancestor_in_the_worktree_records_nothing(self):
        self.assertEqual(self.roots(self.chain(), {304: str(self.dir)}), [])
    def test_init_is_never_recorded(self):
        rows = [(300, 1, NOW, 'zsh'), (1, 0, NOW-9, 'launchd')]
        self.assertEqual([r['pid'] for r in self.roots(rows, {300: str(self.wt), 1: str(self.wt)})], [300])
    def test_parent_already_init_skips_the_lsof_call(self):
        calls = []
        def runner(cmd, **kw):
            calls.append(cmd[0])
            return subprocess.CompletedProcess(cmd, 0, '1 0 %s launchd\n' % self.lstart(NOW), '')
        with patch.object(qg.os, 'getppid', return_value=1):
            self.assertEqual(qg.coordinator_roots(str(self.wt), runner), [])
        self.assertEqual(calls, ['ps'])
    def test_only_ancestors_are_queried_for_their_cwd(self):
        seen = []
        real = self.fake_runner(self.chain(), {300: str(self.wt)})
        def runner(cmd, **kw):
            seen.append(cmd)
            return real(cmd, **kw)
        with patch.object(qg.os, 'getppid', return_value=301):
            qg.coordinator_roots(str(self.wt), runner)
        lsof = next(c for c in seen if c[0] == 'lsof')
        self.assertIn('-a', lsof)
        self.assertEqual(lsof[lsof.index('-p')+1], '301,302,303,304')

    # --- registration
    def write_meta(self, dispatch, **fields):
        (self.dir/(dispatch+'.json')).write_text(json.dumps(fields))
    def test_recorded_roots_tolerates_a_failed_walk(self):
        for error in (RuntimeError('x'), OSError('x'), subprocess.TimeoutExpired('ps', 1), ValueError('x')):
            with patch.object(qg, 'coordinator_roots', side_effect=error):
                self.assertEqual(qg.recorded_roots(str(self.wt), None, self.dir), [])
    def test_retry_keeps_the_old_roots(self):
        old = self.root()
        self.write_meta('ctx_old', coordinator_roots=[old])
        with patch.object(qg, 'coordinator_roots', return_value=[]):
            self.assertEqual(qg.recorded_roots(str(self.wt), 'ctx_old', self.dir), [old])
    def test_retry_merges_without_duplicates(self):
        old, new = self.root(), self.root(pid=777)
        self.write_meta('ctx_old', coordinator_roots=[old, new])
        with patch.object(qg, 'coordinator_roots', return_value=[new]):
            self.assertEqual(qg.recorded_roots(str(self.wt), 'ctx_old', self.dir), [new, old])
    def test_retry_survives_a_failed_walk(self):
        old = self.root()
        self.write_meta('ctx_old', coordinator_roots=[old])
        with patch.object(qg, 'coordinator_roots', side_effect=RuntimeError('x')):
            self.assertEqual(qg.recorded_roots(str(self.wt), 'ctx_old', self.dir), [old])
    def test_retry_without_usable_old_meta_keeps_what_was_walked(self):
        found = self.root()
        (self.dir/'ctx_bad.json').write_text('{not json')
        self.write_meta('ctx_older')
        with patch.object(qg, 'coordinator_roots', return_value=[found]):
            for dispatch in ('ctx_missing', 'ctx_bad', 'ctx_older'):
                self.assertEqual(qg.recorded_roots(str(self.wt), dispatch, self.dir), [found])
    def test_first_dispatch_reads_no_old_meta(self):
        with patch.object(qg, 'coordinator_roots', return_value=[]), patch.object(qg, 'load_meta') as load:
            qg.recorded_roots(str(self.wt), None, self.dir)
        load.assert_not_called()
    def register(self, retry_of=None, enabled=True, walk=None, walk_error=None):
        config = enabled_config() if enabled else disabled_config()
        wid = 'repo::' + str(self.wt)
        info = {'worker': {'worktreeId': wid}, 'terminal': dict(worktreePath=str(self.wt)), 'dispatch': {'hostScope': {'kind': 'local'}}}
        original = self.dir/'o.md'; original.write_text('spec')
        context = dict(original_spec_file=str(original), context_file=str(self.dir/'c.md'))
        args = argparse.Namespace(worktree='id:'+wid, run='run_x', coordinator='term_lead', retry_of=retry_of,
                                  budget_session=None, budget_weekly=None)
        plan = dict(agent='codex', kind='feature', complexity='normal', profile='p')
        with patch.object(qg.Backend, 'command', side_effect=[info, {'dispatch': {}}]), \
             patch.object(qg, 'coordinator_roots', return_value=walk or [], side_effect=walk_error) as walked, \
             patch.object(qg, 'spawn_guard', return_value={'started': True}) as spawn:
            result = qg.register_start(config, plan, args, {'result': {'dispatchId': 'ctx_x', 'taskId': 'task_x'}}, context, self.dir, started_at=NOW)
        return result, spawn, walked
    def test_explicit_selector_records_roots_for_the_real_worktree(self):
        result, spawn, walked = self.register(walk=[self.root()])
        walked.assert_called_once_with(str(self.wt))
        self.assertEqual(spawn.call_args[0][0]['coordinator_roots'], [self.root()])
        self.assertEqual(result['coordinator_roots'], [self.root()])
    def test_failed_walk_still_registers_with_nothing_excluded(self):
        result, spawn, _ = self.register(walk_error=RuntimeError('process scan unavailable'))
        self.assertTrue(result['started'])
        self.assertEqual(spawn.call_args[0][0]['coordinator_roots'], [])
    def test_retry_registration_carries_the_old_roots(self):
        self.write_meta('ctx_old', coordinator_roots=[self.root()])
        _, spawn, _ = self.register(retry_of='ctx_old')
        self.assertEqual(spawn.call_args[0][0]['coordinator_roots'], [self.root()])
    def test_disabled_guard_never_walks_the_process_tree(self):
        result, spawn, walked = self.register(enabled=False)
        self.assertFalse(result['started'])
        walked.assert_not_called(); spawn.assert_not_called()

    # --- real processes
    def real_backend(self, roots=None):
        meta = dict(worktree_path=str(self.wt), dispatch_started_at=time.time() - 5)
        if roots is not None:
            meta['coordinator_roots'] = roots
        b = qg.Backend(CONFIG, meta, self.dir, runner=lambda cmd, **kw: subprocess.run(cmd, **kw))
        b.log = lambda message: None
        return b
    def real_root(self, script):
        """A real coordinator stand-in; each `echo` in the script yields one pid to read."""
        proc = subprocess.Popen(['sh', '-c', script], cwd=self.wt, stdout=subprocess.PIPE, text=True)
        self.pids.append(proc.pid); self.procs.append(proc)
        return proc
    def read_pid(self, proc):
        pid = int(proc.stdout.readline())
        self.pids.append(pid)
        return pid
    def recorded(self, pid):
        return dict(pid=pid, started_at=qg.process_table(subprocess.run)[pid]['begun'])
    def wait_ppid(self, pid, expected):
        for _ in range(100):
            if qg.process_table(subprocess.run)[pid]['ppid'] == expected:
                return
            time.sleep(.05)
        self.fail('pid %d never reached parent %d' % (pid, expected))
    def found(self, roots=None):
        return [p['pid'] for p in self.real_backend(roots).leftovers()]
    def test_real_descendants_are_excluded_and_other_processes_are_not(self):
        root = self.real_root('sleep 60 & echo $!; wait')
        child = self.read_pid(root)
        stranger = subprocess.Popen(['sleep', '60'], cwd=self.wt)
        self.pids.append(stranger.pid); self.procs.append(stranger)
        unprotected = self.found()
        self.assertTrue({root.pid, child, stranger.pid} <= set(unprotected))
        scanned = self.found([self.recorded(root.pid)])
        self.assertNotIn(root.pid, scanned); self.assertNotIn(child, scanned)
        self.assertIn(stranger.pid, scanned)
    def test_real_orphan_and_its_child_are_refused(self):
        root = self.real_root("(sh -c 'echo $$; sleep 60 & echo $!; wait' &) ; sleep 60 & echo $!; wait")
        pids = [self.read_pid(root) for _ in range(3)]
        attached = qg.process_table(subprocess.run)
        attached_child = next(p for p in pids if attached[p]['ppid'] == root.pid)
        orphan_child = next(p for p in pids if attached[p]['ppid'] in pids)
        orphan = attached[orphan_child]['ppid']
        self.wait_ppid(orphan, 1)
        scanned = self.found([self.recorded(root.pid)])
        self.assertIn(orphan, scanned); self.assertIn(orphan_child, scanned)
        self.assertNotIn(attached_child, scanned)
    def test_real_pid_reuse_is_not_excluded(self):
        root = self.real_root('sleep 60 & echo $!; wait')
        child = self.read_pid(root)
        stale = self.recorded(root.pid); stale['started_at'] -= 100
        self.assertIn(child, self.found([stale]))
    def test_real_exited_root_excludes_nothing(self):
        root = self.real_root('sleep 60 & echo $!; wait')
        child = self.read_pid(root)
        recorded = self.recorded(root.pid)
        os.kill(root.pid, 9); root.wait()
        self.wait_ppid(child, 1)
        self.assertIn(child, self.found([recorded]))
    def test_real_scan_never_shows_command_arguments(self):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', '--token=hunter2'], cwd=self.wt)
        self.pids.append(proc.pid); self.procs.append(proc)
        entry = next(p for p in self.real_backend().leftovers() if p['pid'] == proc.pid)
        self.assertEqual(sorted(entry), ['command', 'pid', 'started'])
        self.assertNotIn('hunter2', json.dumps(entry))
        self.assertNotIn('time.sleep', json.dumps(entry))
    def test_real_scan_ignores_a_rewritten_process_title(self):
        # Incident-adjacent: a real MCP server showed `name --project-ref=...` through ps comm.
        proc = subprocess.Popen(['bash', '-c', 'exec -a "foo --token=hunter2" sleep 60'], cwd=self.wt)
        self.pids.append(proc.pid); self.procs.append(proc)
        for _ in range(100):
            if qg.process_table(subprocess.run)[proc.pid]['command'] == 'sleep':
                break
            time.sleep(.05)
        entry = next(p for p in self.real_backend().leftovers() if p['pid'] == proc.pid)
        self.assertEqual(entry['command'], 'sleep')
    def real_walk(self, script):
        """Runs the real walk in a process under `script`; returns the outermost pid and what it recorded."""
        walker = self.dir/'walk.py'
        walker.write_text('import json, sys\nsys.path.insert(0, %r)\nimport quota_guard\nprint(json.dumps(quota_guard.coordinator_roots(%r)))\n'
                          % (str(ROOT/'scripts'), str(self.wt)))
        env = dict(os.environ, PY=sys.executable, WALK=str(walker))
        proc = subprocess.Popen(['sh', '-c', script], cwd=self.wt, stdout=subprocess.PIPE, text=True, env=env)
        out = proc.communicate()[0]
        self.assertEqual(proc.returncode, 0)
        return proc.pid, json.loads(out)
    def test_real_walk_records_the_shell_but_not_this_test_process(self):
        outer, roots = self.real_walk('"$PY" "$WALK"; :')
        self.assertEqual([r['pid'] for r in roots], [outer])
    def test_real_walk_finds_the_root_past_a_shell_that_moved_away(self):
        outer, roots = self.real_walk("""sh -c 'cd / && "$PY" "$WALK"; :'; :""")
        self.assertEqual([r['pid'] for r in roots], [outer])
    def test_real_walk_outside_the_worktree_records_nothing(self):
        _, roots = self.real_walk('cd / && "$PY" "$WALK"; :')
        self.assertEqual(roots, [])
    def test_real_walk_result_excludes_the_real_tree(self):
        script = """sleep 60 & echo $!; "$PY" "$WALK"; wait"""
        walker = self.dir/'walk.py'
        walker.write_text('import json, sys\nsys.path.insert(0, %r)\nimport quota_guard\nprint(json.dumps(quota_guard.coordinator_roots(%r)), flush=True)\n'
                          % (str(ROOT/'scripts'), str(self.wt)))
        proc = subprocess.Popen(['sh', '-c', script], cwd=self.wt, stdout=subprocess.PIPE, text=True,
                                env=dict(os.environ, PY=sys.executable, WALK=str(walker)))
        self.pids.append(proc.pid); self.procs.append(proc)
        child = self.read_pid(proc)
        roots = json.loads(proc.stdout.readline())
        self.assertEqual([r['pid'] for r in roots], [proc.pid])
        stranger = subprocess.Popen(['sleep', '60'], cwd=self.wt)
        self.pids.append(stranger.pid); self.procs.append(stranger)
        scanned = self.found(roots)
        self.assertNotIn(child, scanned); self.assertNotIn(proc.pid, scanned)
        self.assertIn(stranger.pid, scanned)


if __name__ == '__main__':
    unittest.main()
