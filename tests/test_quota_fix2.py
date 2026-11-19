#!/usr/bin/env python3
"""Second round on stale quota: S1-S6 and O1-O3, on fixtures shaped like the live machine.

Live shape, unlike the earlier fixtures: Claude has activeAccountId=null with a system default login,
result.codex names the Codex account, and a failed fetch goes through Orca's applyStalePolicy.
"""
import contextlib
import copy
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import hermetic  # noqa: F401
sys.path.insert(0, str(ROOT / 'scripts'))
import quota
import quota_guard as qg
import route
import test_claude_stale as cs
import test_quota_guard as t

CONFIG = cs.CONFIG
NOW = cs.NOW
entry = cs.entry
CODEX_ACCOUNT = 'acct-codex'
RETENTION_SECONDS = 30 * 60
RATE_LIMITED_RETENTION_SECONDS = 24 * 3600


def apply_stale_policy(prior, kind, now=NOW):
    """Orca 1.4.248 applyStalePolicy: within retention keep windows and the old updatedAt, past it clear both."""
    keep = RATE_LIMITED_RETENTION_SECONDS if kind == 'rate-limited' else RETENTION_SECONDS
    if now - prior['updatedAt'] / 1000 > keep:
        return dict(status='error', updatedAt=now * 1000, session=None, weekly=None, usageMetadata=dict(failureKind=kind))
    return dict(prior, status='error', usageMetadata=dict(failureKind=kind))


def live_output(claude=None, codex=None, antigravity=None, email='a@example.test', org='org-1',
                claude_active=None, codex_active=CODEX_ACCOUNT):
    claude_account = dict(activeAccountId=claude_active, activeAccountIdsByRuntime=dict(host=claude_active, wsl={}),
                          systemDefaultEmail=email, systemDefaultOrganizationUuid=org)
    rate = dict(claude=claude if claude is not None else entry(),
                codex=codex if codex is not None else entry(),
                antigravity=antigravity if antigravity is not None else entry())
    return json.dumps(dict(result=dict(rateLimits=rate, claude=claude_account,
                                       codex=dict(activeAccountId=codex_active))))


class LiveCase(cs.Case):
    def log_root(self):
        return self.dir / 'orca-data' / 'codex-accounts' / CODEX_ACCOUNT / 'home' / 'sessions'

    def write_log(self, age, session=96, weekly=50, root=None, weekly_reset=NOW + 100000, now=NOW):
        root = root or self.log_root()
        folder = root / '2026' / '11' / '05'
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(now - age, timezone.utc).isoformat().replace('+00:00', 'Z')
        event = dict(timestamp=stamp, type='event_msg', payload=dict(type='token_count', rate_limits=dict(
            limit_id='codex',
            primary=dict(used_percent=session, window_minutes=300, resets_at=NOW + 6000),
            secondary=dict(used_percent=weekly, window_minutes=10080, resets_at=weekly_reset))))
        (folder / 'rollout-a.jsonl').write_text(json.dumps(event) + '\n')

    def snapshot(self, *reads, now=NOW):
        return quota.fetch_snapshot(self.pol, runner=cs.runner_of(*reads), clock=lambda: now)

    def predict(self, *reads, now=NOW):
        return qg.predict(self.config, 'codex', self.snapshot(*reads, now=now), now)

    def real_refresher(self):
        self.rpc_calls = []
        def call(timeout):
            self.rpc_calls.append(timeout)
            return 'ok'
        return lambda pol, clock=None: quota.refresh_usage_rpc(pol, clock=clock, call=call)


class S1LiveLogOutranksOrcaError(LiveCase):
    def test_wiped_windows_do_not_discard_the_live_log(self):
        self.write_log(age=30)
        wiped = apply_stale_policy(entry(age=40 * 60), 'network')
        self.assertIsNone(wiped['session'])
        self.assertEqual(wiped['updatedAt'], NOW * 1000)
        codex = self.snapshot(live_output(codex=wiped))['providers']['codex']
        self.assertEqual((codex['data_source'], codex['log_status']), (quota.SOURCE_CODEX_LOG, 'ok'))
        pred = self.predict(live_output(codex=wiped))
        self.assertTrue(pred['valid'], pred['reasons'])
        self.assertEqual(pred['trigger_windows'], ['session'])

    def test_retained_error_with_a_newer_live_log_stays_valid(self):
        self.write_log(age=30)
        kept = apply_stale_policy(entry(age=20 * 60), 'server')
        self.assertEqual(kept['status'], 'error')
        self.assertTrue(self.predict(live_output(codex=kept))['valid'])

    def test_plan_shares_the_judgment(self):
        self.write_log(age=30, session=10, weekly=10)
        wiped = apply_stale_policy(entry(age=40 * 60), 'network')
        result = self.select('feature', reads=[live_output(codex=wiped)])
        sol = self.cand(result, 'sol')
        self.assertEqual(sol['status'], 'eligible')
        self.assertEqual(sol['data_source'], quota.SOURCE_CODEX_LOG)
        self.assertEqual(sol['orca_error_covered_by_log'], dict(orca_status='error', failure_kind='network'))
        self.assertIn('orca_error_covered_by_log', route.build_report(result)['candidates'][0])

    def test_a_log_that_went_quiet_stays_unknown(self):
        self.write_log(age=901)
        for codex in (apply_stale_policy(entry(age=40 * 60), 'network'), apply_stale_policy(entry(age=20 * 60), 'server')):
            pred = self.predict(live_output(codex=codex))
            self.assertFalse(pred['valid'])
        self.write_log(age=900)
        self.assertTrue(self.predict(live_output(codex=apply_stale_policy(entry(age=40 * 60), 'network')))['valid'])

    def test_log_of_unproven_account_stays_unknown(self):
        shared = self.dir / 'shared'
        self.write_log(age=30, root=shared)
        wiped = apply_stale_policy(entry(age=40 * 60), 'network')
        with patch.object(quota, 'codex_log_roots', return_value=[shared]):
            snap = self.snapshot(live_output(codex=wiped, codex_active=None))
        codex = snap['providers']['codex']
        self.assertEqual((codex['data_source'], codex['log_status']), (quota.SOURCE_ORCA, 'account_mismatch'))
        self.assertFalse(qg.predict(self.config, 'codex', snap, NOW)['valid'])

    def test_log_of_another_account_with_retained_windows_stays_unknown(self):
        shared = self.dir / 'shared'
        self.write_log(age=30, root=shared, weekly_reset=NOW + 50000)
        kept = apply_stale_policy(entry(age=20 * 60), 'server')
        with patch.object(quota, 'codex_log_roots', return_value=[shared]):
            snap = self.snapshot(live_output(codex=kept, codex_active=None))
        self.assertEqual(snap['providers']['codex']['log_status'], 'account_mismatch')
        self.assertFalse(qg.predict(self.config, 'codex', snap, NOW)['valid'])

    def test_only_codex_log_data_is_rescued(self):
        bad = apply_stale_policy(entry(age=5 * 60), 'server')
        result = self.select('bugfix', reads=[live_output(claude=bad)])
        self.assertEqual(self.cand(result, 'opus_high')['status'], 'unknown')

    def test_only_status_error_is_rescued(self):
        self.write_log(age=30)
        for status in ('fetching', 'unavailable'):
            pred = self.predict(live_output(codex=entry(age=40 * 60, status=status)))
            self.assertFalse(pred['valid'], status)

    def test_log_older_than_a_real_orca_reading_still_loses(self):
        self.write_log(age=100)
        kept = apply_stale_policy(entry(age=10), 'server')
        codex = self.snapshot(live_output(codex=kept))['providers']['codex']
        self.assertEqual((codex['data_source'], codex['log_status']), (quota.SOURCE_ORCA, 'older_than_orca'))
        self.assertFalse(self.predict(live_output(codex=kept))['valid'])

    def test_incomplete_ok_reading_is_not_treated_as_wiped(self):
        self.write_log(age=100)
        partial = dict(entry(age=10), session=None)
        self.assertEqual(self.snapshot(live_output(codex=partial))['providers']['codex']['log_status'], 'older_than_orca')

    def test_guard_tick_hands_off_on_wiped_orca_and_live_log(self):
        self.write_log(age=30)
        snap = self.snapshot(live_output(codex=apply_stale_policy(entry(age=40 * 60), 'network')))
        guard_dir = self.dir / 'guard'
        guard_dir.mkdir()
        backend = t.FakeBackend(guard_dir)
        guard = qg.Guard(t.enabled_config(), t.META, backend, guard_dir)
        self.assertEqual(guard.tick(snap), 'handoff')
        self.assertIsNone(guard.invalid_since)


class S2PlanLooksAtTheStartItLeadsTo(LiveCase):
    def plan(self, reads, takeover='plan', now=NOW):
        return self.select('feature', explicit='opus_high', reads=reads, refresher=self.refresher('rate_limited', False),
                           takeover=takeover, now=now)

    def test_unexpired_plan_that_would_expire_uses_the_same_judgment_as_start(self):
        claude = entry(age=13 * 60, session=70)
        result = self.plan([live_output(claude=claude)])
        cand = self.cand(result, 'opus_high')
        self.assertEqual((cand['status'], result['selection']['blocked_code']), ('unknown', 'quota_unknown'))
        self.assertIn('保守剩余', cand['stale_takeover_refused'])
        # Same reading, same words: both came from one function.
        start = self.plan([live_output(claude=claude)], takeover='start', now=NOW + 5 * 60)
        self.assertIn('保守剩余', self.cand(start, 'opus_high')['stale_takeover_refused'])

    def test_enough_quota_passes_and_records_what_it_expects(self):
        result = self.plan([live_output(claude=entry(age=13 * 60, session=10))])
        cand = self.cand(result, 'opus_high')
        self.assertEqual(cand['status'], 'eligible')
        self.assertNotIn('stale_takeover', cand)
        self.assertEqual(cand['stale_takeover_expected']['plan_margin_minutes'], 5)

    def test_age_plus_margin_at_exactly_the_limit_needs_no_judgment(self):
        result = self.plan([live_output(claude=entry(age=10 * 60, session=80))])
        cand = self.cand(result, 'opus_high')
        self.assertEqual(cand['status'], 'eligible')
        self.assertNotIn('stale_takeover_expected', cand)
        self.assertEqual(self.cand(self.plan([live_output(claude=entry(age=10 * 60 + 1, session=80))]), 'opus_high')['status'], 'unknown')

    def test_window_resetting_within_the_margin_is_refused_on_fresh_data(self):
        result = self.plan([live_output(claude=entry(age=0, session_reset=NOW + 5 * 60))])
        cand = self.cand(result, 'opus_high')
        self.assertEqual(cand['status'], 'unknown')
        self.assertIn('距重置', cand['stale_takeover_refused'])
        self.assertEqual(self.cand(self.plan([live_output(claude=entry(age=0, session_reset=NOW + 5 * 60 + 1))]), 'opus_high')['status'], 'eligible')

    def test_window_resetting_within_the_margin_is_refused_on_stale_data(self):
        result = self.plan([live_output(claude=entry(age=20 * 60, session_reset=NOW + 2 * 60))])
        cand = self.cand(result, 'opus_high')
        self.assertEqual(cand['status'], 'unknown')
        self.assertIn('距重置', cand['stale_takeover_refused'])

    def test_a_cap_window_counts_too(self):
        profile = dict(self.config['models']['opus_high'], quota_cap_window='fableWeekly')
        claude = dict(entry(age=0), fableWeekly=cs.window(10, 10080, NOW + 120))
        snap = self.snapshot(live_output(claude=claude))
        budget = self.pol['budgets']['normal']
        cand = quota.evaluate_candidate('x', dict(profile, enabled=True), 'primary', self.pol, snap, budget, NOW * 1000)
        self.assertEqual(cand['status'], 'eligible')
        quota.apply_handoff_margin(cand, profile, self.pol, snap, budget, NOW * 1000, 5)
        self.assertIn('fableWeekly', cand['stale_takeover_refused'])

    def test_every_plan_that_passes_is_not_refused_by_a_start_within_the_margin(self):
        passed = 0
        for age in (0, 300, 540, 600, 660, 780, 899, 901, 1200, 1800, 3000, 3300, 3301, 3600):
            for used in (0, 10, 40, 55, 70, 80, 90):
                for reset in (60, 180, 299, 300, 301, 360, 1800, 6000):
                    claude = live_output(claude=entry(age=age, session=used, session_reset=NOW + reset))
                    if self.plan([claude])['selection']['selected'] != 'opus_high':
                        continue
                    passed += 1
                    for later in (1, 60, 180, 299, 300):
                        start = self.plan([claude], takeover='start', now=NOW + later)
                        self.assertEqual(start['selection']['selected'], 'opus_high', (age, used, reset, later))
        self.assertGreater(passed, 50)

    def test_recheck_passing_means_start_within_its_margin_passes(self):
        passed = 0
        for age in (0, 600, 839, 840, 841, 1200, 3540, 3541):
            for used in (0, 50, 80):
                for reset in (30, 60, 61, 300, 6000):
                    claude = live_output(claude=entry(age=age, session=used, session_reset=NOW + reset))
                    if self.plan([claude], takeover='recheck')['selection']['selected'] != 'opus_high':
                        continue
                    passed += 1
                    start = self.plan([claude], takeover='start', now=NOW + 59)
                    self.assertEqual(start['selection']['selected'], 'opus_high', (age, used, reset))
        self.assertGreater(passed, 5)

    def test_recheck_is_looser_than_plan_so_the_two_are_not_the_same_check(self):
        reads = [live_output(claude=entry(age=55 * 60 + 30, session=0, weekly=0))]
        self.assertEqual(self.cand(self.plan(reads), 'opus_high')['status'], 'unknown')
        self.assertEqual(self.cand(self.plan(reads, takeover='recheck'), 'opus_high')['status'], 'eligible')

    def test_handoff_longer_than_the_margin_is_caught_by_recheck(self):
        claude = live_output(claude=entry(age=54 * 60 + 30, session=0, weekly=0))
        self.assertEqual(self.cand(self.plan([claude]), 'opus_high')['status'], 'eligible')
        cand = self.cand(self.plan([claude], takeover='recheck', now=NOW + 6 * 60), 'opus_high')
        self.assertEqual(cand['status'], 'unknown')
        self.assertIn('超过上限', cand['stale_takeover_refused'])

    def test_ordinary_plan_is_neither_loosened_nor_tightened(self):
        fresh_near_reset = live_output(claude=entry(age=0, session_reset=NOW + 60))
        self.assertEqual(self.select('bugfix', reads=[fresh_near_reset])['selection']['selected'], 'opus_high')
        aging = live_output(claude=entry(age=13 * 60, session=70))
        self.assertEqual(self.select('bugfix', reads=[aging])['selection']['selected'], 'opus_high')
        stale = live_output(claude=entry(age=20 * 60))
        self.assertEqual(self.select('bugfix', reads=[stale])['selection']['blocked_code'], 'quota_unknown')

    def test_off_switch_restores_the_earlier_plan(self):
        self.config['quota_policy']['guard_stale_takeover']['enabled'] = False
        reads = [live_output(claude=entry(age=13 * 60, session=70, session_reset=NOW + 60))]
        self.assertEqual(self.cand(self.plan(reads), 'opus_high')['status'], 'eligible')

    def test_pool_without_a_reserve_cannot_be_refused_by_start_so_is_left_alone(self):
        snap = self.snapshot(live_output(codex=entry(age=13 * 60, session_reset=NOW + 60)))
        budget = self.pol['budgets']['normal']
        profile = self.config['models']['sol']
        cand = quota.evaluate_candidate('sol', profile, 'primary', self.pol, snap, budget, NOW * 1000)
        quota.apply_handoff_margin(cand, profile, self.pol, snap, budget, NOW * 1000, 5)
        self.assertEqual(cand['status'], 'eligible')

    def test_reserve_holding_pool_outside_the_takeover_list_is_refused_when_it_would_expire(self):
        self.config['quota_policy']['guard_stale_takeover']['agents'] = ['antigravity']
        pol = quota.validate_policy(self.config['quota_policy'])
        snap = self.snapshot(live_output(claude=entry(age=13 * 60)))
        budget = pol['budgets']['normal']
        profile = self.config['models']['opus_high']
        cand = quota.evaluate_candidate('opus_high', profile, 'primary', pol, snap, budget, NOW * 1000)
        quota.apply_handoff_margin(cand, profile, pol, snap, budget, NOW * 1000, 5)
        self.assertIn('不适用', cand['stale_takeover_refused'])

    def test_reset_already_past_is_never_a_way_in_even_with_last_good(self):
        first = live_output(claude=entry(age=0, session_reset=NOW + 120))
        self.plan([first])
        wiped = apply_stale_policy(entry(age=40 * 60), 'server', NOW + 300)
        wiped_out = live_output(claude=wiped)
        for phase in ('plan', 'recheck', 'start'):
            cand = self.cand(self.plan([wiped_out], takeover=phase, now=NOW + 300), 'opus_high')
            self.assertEqual(cand['status'], 'unknown', phase)
            self.assertIn('已过去', cand['stale_takeover_refused'])

    def test_account_switch_does_not_look_like_a_reset(self):
        # Account A's window resets soon; account B has another resetsAt. B's wiped reading must not reuse A's numbers.
        self.plan([live_output(claude=entry(age=0, session=5, weekly=5), email='a@example.test')])
        wiped = apply_stale_policy(entry(age=40 * 60), 'server', NOW + 60)
        for phase in ('plan', 'recheck', 'start'):
            cand = self.cand(self.plan([live_output(claude=wiped, email='b@example.test')], takeover=phase, now=NOW + 60), 'opus_high')
            self.assertEqual(cand['status'], 'unknown', phase)
            self.assertIn('没有可用的历史读数', cand['stale_takeover_refused'])


class S2RouteFlags(LiveCase):
    def setUp(self):
        super().setUp()
        state = self.dir / 'guard'
        state.mkdir()
        (state / 'ctx_g.json').write_text(json.dumps(dict(
            dispatch_id='ctx_g', task_id='task_g', run_id='run_g', kind='feature', complexity='normal', agent='codex')))
        patcher = patch.object(qg, 'STATE', state)
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = self.dir / 'spec.md'
        spec.write_text('任务')
        self.spec = spec

    def plan(self, *extra, reads=None, guard=True):
        guards = [dict(dispatch_id='ctx_g', pid=os.getppid())] if guard else []
        reads = reads or [live_output(claude=entry(age=55 * 60 + 30, session=0, weekly=0))]
        with patch.object(qg, 'active_guards', return_value=guards):
            return cs.route_main(['plan', '--kind', 'feature', '--complexity', 'normal', '--profile', 'opus_high'] + list(extra), reads)

    def test_recheck_uses_the_small_margin_only_for_the_guard(self):
        code, out, _, _ = self.plan('--quota-guard-takeover', 'ctx_g')
        self.assertEqual((code, out['launchable']), (2, False))
        code, out, _, _ = self.plan('--quota-guard-takeover', 'ctx_g', '--handoff-recheck')
        self.assertEqual((code, out['launchable']), (0, True))
        self.assertEqual(out['plan']['stale_takeover']['plan_margin_minutes'], 1)

    def test_recheck_flag_alone_or_on_start_is_refused(self):
        code, _, err, _ = self.plan('--handoff-recheck')
        self.assertEqual(code, 2)
        self.assertIn('--handoff-recheck', err)
        with patch.object(qg, 'active_guards', return_value=[dict(dispatch_id='ctx_g', pid=os.getppid())]):
            code, _, err, starts = cs.route_main(['start', '--kind', 'feature', '--complexity', 'normal', '--run', 'run_g',
                                                  '--spec-file', str(self.spec), '--profile', 'opus_high',
                                                  '--quota-guard-takeover', 'ctx_g', '--handoff-recheck'],
                                                 [live_output()])
        self.assertEqual((code, starts), (2, []))
        self.assertIn('--handoff-recheck', err)

    def test_recheck_from_something_other_than_the_guard_is_refused(self):
        code, out, _, _ = self.plan('--quota-guard-takeover', 'ctx_g', '--handoff-recheck', guard=False)
        self.assertEqual(code, 2)
        self.assertIn('存活守卫', out['blocked_reason'])

    def test_fast_mode_stays_off_on_stale_data(self):
        config = copy.deepcopy(CONFIG)
        fast = config['quota_policy'].setdefault('fast_mode', {})
        fast.update(enabled=True, agents=['claude'], usage_multiplier=1, min_weekly_pace_ratio=0)
        result = dict(candidates=[dict(profile='opus_high', status='eligible', stale_takeover=dict(age_minutes=30),
                                       windows=dict(session=dict(budget_pp=1, available_pp=50, pace_ratio=9),
                                                    weekly=dict(budget_pp=1, available_pp=50, pace_ratio=9)))])
        plan = dict(profile='opus_high', agent='claude')
        decision = route.fast_decision(config, plan, result)
        self.assertFalse(decision['apply'])
        self.assertIn('过期', decision['reason'])
        del result['candidates'][0]['stale_takeover']
        self.assertTrue(route.fast_decision(config, plan, result)['apply'])


class S2GuardRecheck(t.GuardCase):
    def phase(self):
        return json.loads((self.dir / 'ctx_old-phase.json').read_text())

    def test_failed_recheck_keeps_the_task_and_says_why(self):
        self.backend.recheck_ok = False
        self.backend.plan_detail = 'opus：claude 失败类型 stale-token 不属于暂时故障'
        self.assertEqual(self.guard.tick(t.snapshot(used=95)), 'blocked')
        self.assertIn('stop', self.backend.events)
        self.assertAbsent('context', 'fail_task', 'start', 'quota_handoff')
        alert = [b for to, b, high in self.backend.sent if high][-1]
        self.assertIn('stale-token', alert)
        self.assertIn('未改 Task', alert)
        self.assertIn('未占尝试次数', alert)
        self.assertIn('route.py', alert)
        self.assertNotIn('--quota-guard-takeover', alert)
        self.assertEqual((self.phase()['phase'], self.phase()['old_worker_stopped']), ('blocked', True))
        self.assertNotIn('start_attempted', self.phase())

    def test_recheck_that_raises_counts_as_no(self):
        def boom(target):
            raise RuntimeError('plan changed (3)')
        self.backend.recheck = boom
        self.assertEqual(self.guard.tick(t.snapshot(used=95)), 'blocked')
        self.assertAbsent('context', 'fail_task', 'start')
        self.assertIn('plan changed', [b for to, b, high in self.backend.sent if high][-1])

    def test_recheck_runs_after_the_stop_and_before_anything_changes(self):
        self.assertEqual(self.guard.tick(t.snapshot(used=95)), 'handoff')
        events = self.backend.events
        self.assertLess(events.index('stop'), events.index('recheck'))
        self.assertLess(events.index('recheck'), events.index('context'))
        self.assertLess(events.index('context'), events.index('fail_task'))
        self.assertEqual(events.count('recheck'), 1)

    def test_refused_start_alert_carries_the_reason(self):
        self.backend.start = lambda target, spec: (2, dict(blocked_reason='显式指定的 opus 不能绕过调度预留/预算：session resetsAt 已过去',
                                                           quota=dict(usage_refresh=dict(attempted=False, outcome='rate_limited', seconds_until_allowed=120))))
        self.assertEqual(self.guard.tick(t.snapshot(used=95)), 'blocked')
        alert = [b for to, b, high in self.backend.sent if high][-1]
        self.assertIn('resetsAt 已过去', alert)
        self.assertIn('限频中', alert)
        self.assertIn('Task 已标 failed', alert)
        self.assertIn('route.py', alert)
        self.assertNotIn('--quota-guard-takeover', alert)

    def test_start_refusal_reads_every_shape(self):
        self.assertEqual(qg.start_refusal(dict(profile_changed=dict(expected='a', current='b'))), '额度重新选择结果与复核不同（期望 a，实际 b）')
        self.assertEqual(qg.start_refusal(dict(error=dict(message='boom'))), 'boom')
        self.assertEqual(qg.start_refusal(dict(blocked_reason='x')), 'x')
        self.assertEqual(qg.start_refusal([]), '')
        self.assertEqual(qg.start_refusal({}), '')
        self.assertLessEqual(len(qg.start_refusal(dict(blocked_reason='x' * 5000))), 1500)


class BackendReadsStart(LiveCase):
    def backend(self, stderr, stdout='{}', code=0):
        run = lambda cmd, **kw: subprocess.CompletedProcess(cmd, code, stdout, stderr)
        return qg.Backend(CONFIG, dict(t.META), self.dir, runner=run)

    def test_start_replaces_what_the_plan_expected(self):
        stale = dict(age_seconds=1000, age_minutes=16.7)
        backend = self.backend(json.dumps(dict(routing_plan=dict(stale_takeover=stale))) + '\n')
        backend.target_stale = None
        backend.start('opus_high', 'x')
        self.assertEqual(backend.target_stale, stale)
        backend = self.backend(json.dumps(dict(routing_plan=dict(profile='opus_high'))) + '\nnot json\n')
        backend.target_stale = stale
        backend.start('opus_high', 'x')
        self.assertIsNone(backend.target_stale)

    def test_non_object_receipt_does_not_crash_the_guard(self):
        code, receipt = self.backend('', stdout='[1]', code=2).start('opus_high', 'x')
        self.assertEqual((code, receipt), (2, {}))

    def test_recheck_passes_its_flags_and_reports_why(self):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 2, json.dumps(dict(launchable=False, blocked_reason='不行')), '')
        backend = qg.Backend(CONFIG, dict(t.META), self.dir, runner=run)
        self.assertFalse(backend.recheck('opus_high'))
        self.assertIn('不行', backend.plan_detail)
        self.assertEqual(calls[0][calls[0].index('--profile') + 1], 'opus_high')
        self.assertEqual(calls[0][calls[0].index('--quota-guard-takeover') + 1], 'ctx_old')
        self.assertIn('--handoff-recheck', calls[0])


class RealGuard(unittest.TestCase):
    """Real guard, real route.py children, fake orca shaped like the live machine; numbers change mid-handoff."""
    FAKE = t.RealWiring.FAKE.replace(
        "    print(json.dumps(dict(result=dict(rateLimits=dict(codex=entry(95), claude=entry(10), antigravity=entry(10))))))\n",
        "    rate = dict(codex=entry(95), claude=entry(10), antigravity=entry(10))\n"
        "    rate['claude']['updatedAt'] = now - db.get('claude_age_s', 0) * 1000\n"
        "    rate['claude'].update(db.get('claude_extra', dict()))\n"
        "    rate['codex'].update(db.get('codex_extra', dict()))\n"
        "    if db.get('codex_wiped'):\n"
        "        rate['codex'].update(status='error', updatedAt=now, session=None, weekly=None, usageMetadata=dict(failureKind='network'))\n"
        "    print(json.dumps(dict(result=dict(rateLimits=rate, claude=dict(activeAccountId=None, systemDefaultEmail='a@example.test', systemDefaultOrganizationUuid='o'),\n"
        "                                      codex=dict(activeAccountId=db.get('codex_account'))))))\n").replace(
        "    db['stopped'] = True\n",
        "    db['stopped'] = True\n    db.update(db.get('after_stop', dict()))\n").replace(
        "elif cmd == 'task-update':\n",
        "elif cmd == 'task-update':\n    db.update(db.get('after_task_update', dict()))\n")
    setUp_ = t.RealWiring.setUp
    write_db = t.RealWiring.write_db
    change_db = t.RealWiring.change_db
    report = t.RealWiring.report
    run_guard = t.RealWiring.run_guard
    called = t.RealWiring.called
    bodies = t.RealWiring.bodies

    def setUp(self):
        self.setUp_()
        self.routing = self.dir / 'routing-state'
        self.env['ORCA_ROUTING_STATE_DIR'] = str(self.routing)
        self.env['ORCA_USER_DATA_PATH'] = str(self.dir / 'orca-data')
        self.change_db(report=self.report())

    def records(self):
        log = self.home / '.config' / 'orca-model-routing' / 'usage-log.jsonl'
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def test_target_going_bad_after_the_stop_changes_nothing(self):
        bad = dict(status='error', usageMetadata=dict(failureKind='stale-token'))
        self.change_db(claude_age_s=1800, after_stop=dict(claude_extra=bad))
        db = self.run_guard(1)
        self.assertEqual(self.phase['phase'], 'blocked')
        self.assertEqual(len(self.called('worker-stop')), 1)
        self.assertEqual(self.called('task-update'), [])
        self.assertEqual(self.called('worker-start'), [])
        self.assertEqual(db['task'], dict(status='blocked', result=None, dispatch_id=None))
        self.assertEqual(db['dispatches'], ['ctx_wire'])
        alert = self.bodies()[-1]
        self.assertIn('接手前复核未通过', alert)
        self.assertIn('stale-token', alert)
        self.assertIn('未改 Task', alert)
        self.assertTrue(self.handoff.exists())
        self.assertNotIn('已完成 A', (self.state / 'c.md').read_text())

    def test_target_going_bad_after_the_recheck_says_why_in_the_alert(self):
        bad = dict(status='error', usageMetadata=dict(failureKind='stale-token'))
        self.change_db(claude_age_s=1800, after_task_update=dict(claude_extra=bad))
        db = self.run_guard(1)
        self.assertEqual((self.phase['phase'], self.phase['start_exit']), ('blocked', 2))
        self.assertEqual(self.called('worker-start'), [])
        self.assertEqual(len(self.called('task-update')), 1)
        self.assertEqual(db['task']['status'], 'failed')
        alert = self.bodies()[-1]
        self.assertIn('接手 start 返回 2', alert)
        self.assertIn('stale-token', alert)

    def test_plan_stale_but_start_fresh_is_recorded_as_fresh(self):
        self.change_db(claude_age_s=1800, after_task_update=dict(claude_age_s=0))
        self.run_guard(0)
        self.assertEqual(self.phase['phase'], 'handoff_done')
        handoff = next(r for r in self.records() if r['event'] == 'quota_handoff')
        self.assertNotIn('target_stale_data', handoff)
        self.assertNotIn('过期的额度数据', self.bodies()[-1])

    def test_plan_fresh_but_start_stale_is_recorded_as_stale(self):
        self.change_db(claude_age_s=0, after_task_update=dict(claude_age_s=1000))
        self.run_guard(0)
        self.assertEqual(self.phase['phase'], 'handoff_done')
        records = self.records()
        start = next(r for r in records if r['event'] == 'start')
        handoff = next(r for r in records if r['event'] == 'quota_handoff')
        # Orca's number aged past the limit; start judged the remembered reading from the plan a moment ago.
        self.assertEqual(start['stale_takeover']['source'], 'last_good_reading')
        self.assertEqual(handoff['target_stale_data']['source'], 'last_good_reading')
        self.assertEqual(handoff['target_stale_data']['age_seconds'], start['stale_takeover']['age_seconds'])
        self.assertIn('过期的额度数据', self.bodies()[-1])

    def test_damaged_last_good_does_not_stop_a_guard(self):
        for bad in ('{"claude":"x"}', '{"claude":[1]}', '[]', 'garbage'):
            self.setUp()
            self.routing.mkdir(exist_ok=True)
            (self.routing / 'last-good-usage.json').write_text(bad)
            self.change_db(claude_age_s=1800)
            self.run_guard(0)
            self.assertEqual(self.phase['phase'], 'handoff_done', bad)
            self.assertFalse(any('守卫异常' in b for b in self.bodies()), bad)

    def test_wiped_orca_with_a_live_log_still_hands_off(self):
        root = self.dir / 'orca-data' / 'codex-accounts' / 'acct-codex' / 'home' / 'sessions' / 'x'
        root.mkdir(parents=True)
        now = time.time()
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat().replace('+00:00', 'Z')
        (root / 'rollout-a.jsonl').write_text(json.dumps(dict(timestamp=stamp, type='event_msg', payload=dict(
            type='token_count', rate_limits=dict(limit_id='codex',
                                                 primary=dict(used_percent=96, window_minutes=300, resets_at=int(now + 6000)),
                                                 secondary=dict(used_percent=50, window_minutes=10080, resets_at=int(now + 100000)))))) + '\n')
        self.change_db(codex_account='acct-codex', codex_wiped=True)
        self.run_guard(0)
        self.assertEqual(self.phase['phase'], 'handoff_done')
        self.assertFalse(any('额度快照不可用' in b for b in self.bodies()))


class RealRoute(unittest.TestCase):
    """route.py as a process on a damaged last-good file: plan and start must answer, never crash."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(os.path.realpath(tmp.name))
        scripts = self.dir / 'skill' / 'scripts'
        scripts.mkdir(parents=True)
        self.data = self.dir / 'usage.json'
        fake = self.dir / 'orca'
        fake.write_text('#!%s\nimport json, sys\nargs = sys.argv[1:]\n'
                        'if args[:2] == ["account", "list"]:\n    print(open(%r).read())\n'
                        'else:\n    print(json.dumps(dict(ok=True, result=dict(dispatchId="ctx_n", taskId="task_n"))))\n'
                        % (sys.executable, str(self.data)))
        fake.chmod(0o755)
        for name in ('quota.py', 'quota_guard.py', 'route.py', 'jev_route.py', 'setup_jev.py'):
            (scripts / name).write_text((ROOT / 'scripts' / name).read_text().replace('/usr/local/bin/orca', str(fake)))
        config = copy.deepcopy(CONFIG)
        config['quota_policy']['source']['command'][0] = str(fake)
        config['quota_policy']['quota_guard']['enabled'] = False
        # Pin the fixture copy below the restore value (4) so the reminder is in force.
        config['quota_policy']['capacity_factors']['claude'] = 2
        (self.dir / 'skill' / 'routes.json').write_text(json.dumps(config))
        self.scripts = scripts
        self.state = self.dir / 'state'
        self.env = dict(os.environ, HOME=str(self.dir), PYTHONDONTWRITEBYTECODE='1',
                        ORCA_ROUTING_STATE_DIR=str(self.state), ORCA_USER_DATA_PATH=str(self.dir / 'orca-data'))
        self.env.pop('ORCA_TERMINAL_HANDLE', None)
        self.spec = self.dir / 'spec.md'
        self.spec.write_text('任务')

    def usage(self, age=0, weekly_used=10, weekly_left=4000 * 60):
        now = time.time()
        def item(a):
            return dict(status='ok', updatedAt=int((now - a) * 1000),
                        session=dict(usedPercent=10, windowMinutes=300, resetsAt=int((now + 6000) * 1000)),
                        weekly=dict(usedPercent=weekly_used, windowMinutes=10080, resetsAt=int((now + weekly_left) * 1000)))
        self.data.write_text(json.dumps(dict(result=dict(
            rateLimits=dict(claude=item(age), codex=item(0), antigravity=item(0)),
            claude=dict(activeAccountId=None, systemDefaultEmail='a@example.test', systemDefaultOrganizationUuid='o')))))

    def route(self, *args):
        return subprocess.run([sys.executable, str(self.scripts / 'route.py')] + list(args), env=self.env, capture_output=True,
                              text=True, timeout=60, cwd=self.dir, stdin=subprocess.DEVNULL)

    def test_plan_and_start_answer_on_every_damaged_shape_and_the_file_heals(self):
        self.usage()
        for bad in ('{"claude":"x"}', '{"claude":5}', '{"claude":[1]}', '{"claude":{"updatedAt":"x"}}', '[]', '\xff'):
            self.state.mkdir(exist_ok=True)
            (self.state / 'last-good-usage.json').write_text(bad, errors='surrogateescape')
            proc = self.route('plan', '--kind', 'bugfix', '--complexity', 'normal')
            self.assertEqual(proc.returncode, 0, bad + proc.stderr[-500:])
            self.assertTrue(json.loads(proc.stdout)['launchable'])
            self.assertIsInstance(json.loads((self.state / 'last-good-usage.json').read_text())['claude'], dict)
            (self.state / 'last-good-usage.json').write_text(bad, errors='surrogateescape')
            proc = self.route('start', '--run', 'run_x', '--kind', 'bugfix', '--complexity', 'normal', '--spec-file', str(self.spec))
            self.assertEqual(proc.returncode, 0, bad + proc.stderr[-500:])

    def test_failed_retry_validation_keeps_the_first_reminder(self):
        self.usage(weekly_used=10, weekly_left=5040 * 60)
        proc = self.route('start', '--run', 'run_x', '--kind', 'bugfix', '--complexity', 'normal', '--spec-file', str(self.spec),
                          '--task', 'task_x', '--retry-of', 'ctx_nonexistent')
        self.assertEqual(proc.returncode, 2)
        out = json.loads(proc.stdout)
        self.assertFalse(out['launched'])
        self.assertIs(out['restore_reminder']['first_in_window'], True)
        self.assertIn('恢复 Claude 权重', out['restore_reminder']['message'])
        later = json.loads(self.route('plan', '--kind', 'bugfix', '--complexity', 'normal').stdout)
        self.assertIs(later['restore_reminder']['first_in_window'], False)


class S3ErrorMayRefreshEarly(LiveCase):
    def test_error_on_unexpired_data_refreshes_and_recovers(self):
        bad = apply_stale_policy(entry(age=300), 'server')
        result = self.select('bugfix', reads=[live_output(claude=bad), live_output(claude=entry(age=0))],
                             refresher=self.refresher())
        self.assertEqual(self.refreshes, ['ok'])
        self.assertEqual(result['selection']['selected'], 'opus_high')

    def test_wiped_error_refreshes_too(self):
        bad = apply_stale_policy(entry(age=40 * 60), 'network')
        result = self.select('bugfix', reads=[live_output(claude=bad), live_output(claude=entry(age=0))],
                             refresher=self.refresher())
        self.assertEqual(self.refreshes, ['ok'])
        self.assertEqual(result['selection']['selected'], 'opus_high')

    def test_ok_and_unexpired_still_never_refreshes(self):
        self.select('bugfix', reads=[live_output(claude=entry(age=899))], refresher=self.refresher())
        self.assertEqual(self.refreshes, [])

    def test_still_bound_by_the_shared_rate_limit(self):
        refresher = self.real_refresher()
        bad = live_output(claude=apply_stale_policy(entry(age=300), 'server'))
        result = self.select('bugfix', reads=[bad], refresher=refresher)
        self.assertEqual(len(self.rpc_calls), 1)
        self.assertEqual(result['selection']['blocked_code'], 'quota_unknown')
        for later, sent in ((60, 1), (299, 1), (300, 2)):
            self.select('bugfix', reads=[bad], refresher=refresher, now=NOW + later)
            self.assertEqual(len(self.rpc_calls), sent, later)

    def test_still_bound_by_retry_after(self):
        bad = apply_stale_policy(entry(age=300), 'rate-limited')
        bad['usageMetadata']['retryAtMs'] = (NOW + 600) * 1000
        result = self.select('bugfix', reads=[live_output(claude=bad)], refresher=self.refresher())
        self.assertEqual(self.refreshes, [])
        self.assertEqual(result['source']['refresh']['outcome'], 'retry_after_active')

    def test_a_live_log_needs_no_refresh_but_a_quiet_one_does(self):
        wiped = live_output(codex=apply_stale_policy(entry(age=40 * 60), 'network'))
        self.write_log(age=30)
        snap = self.snapshot(wiped)
        self.assertEqual(quota._stale_keys(snap, ['codex'], self.pol, NOW * 1000), [])
        self.write_log(age=901)
        snap = self.snapshot(wiped)
        self.assertEqual(quota._stale_keys(snap, ['codex'], self.pol, NOW * 1000), ['codex'])

    def test_retry_after_does_not_blind_a_guard_with_a_live_log(self):
        self.write_log(age=30)
        kept = apply_stale_policy(entry(age=40 * 60), 'rate-limited')
        kept['usageMetadata']['retryAtMs'] = (NOW + 3600) * 1000
        snap = quota.fresh_snapshot(self.pol, ['codex'], runner=cs.runner_of(live_output(codex=kept)), clock=lambda: NOW,
                                    refresher=self.refresher(), sleep=lambda s: None)
        self.assertEqual(self.refreshes, [])
        self.assertTrue(qg.predict(self.config, 'codex', snap, NOW)['valid'])


class S4AccountDigest(LiveCase):
    def stored(self):
        return json.loads((self.dir / 'state' / quota.LAST_GOOD_STATE).read_text())

    def remember(self, *reads):
        quota.remember_good_reading(self.pol, self.snapshot(*reads))

    def takeover(self, **account):
        wiped = apply_stale_policy(entry(age=40 * 60), 'server', NOW + 60)
        result = self.select('feature', explicit='opus_high', reads=[live_output(claude=wiped, **account)], takeover='start', now=NOW + 60)
        return self.cand(result, 'opus_high')

    def test_same_system_login_uses_the_remembered_reading(self):
        self.remember(live_output(claude=entry(age=10 * 60)))
        cand = self.takeover()
        self.assertEqual(cand['status'], 'eligible')
        self.assertEqual(cand['stale_takeover']['source'], 'last_good_reading')

    def test_changed_system_login_does_not(self):
        self.remember(live_output(claude=entry(age=10 * 60)))
        self.assertEqual(self.takeover(email='b@example.test')['status'], 'unknown')
        self.assertEqual(self.takeover(org='org-2')['status'], 'unknown')

    def test_no_digest_means_no_last_good_at_all(self):
        self.remember(live_output(claude=entry(age=10 * 60)))
        self.assertEqual(self.takeover(email=None, org=None)['status'], 'unknown')
        self.assertEqual(self.takeover(email=None)['status'], 'unknown')

    def test_reading_without_a_digest_is_not_stored_and_does_not_replace_one(self):
        self.remember(live_output(claude=entry(age=100)))
        before = self.stored()
        self.remember(live_output(claude=entry(age=1), email=None))
        self.assertEqual(self.stored(), before)

    def test_missing_account_block_is_no_digest(self):
        self.assertIsNone(quota.account_digest(None))
        self.assertIsNone(quota.account_digest(dict(activeAccountId=None)))
        self.assertIsNone(quota.account_digest(dict(activeAccountId=None, systemDefaultOrganizationUuid='o')))
        self.assertTrue(quota.account_digest(dict(activeAccountId=None, systemDefaultEmail='a@example.test')))

    def test_a_real_active_id_still_decides(self):
        same = quota.account_digest(dict(activeAccountId='x', systemDefaultEmail='a@example.test'))
        self.assertEqual(same, quota.account_digest(dict(activeAccountId='x', systemDefaultEmail='b@example.test')))
        self.assertNotEqual(same, quota.account_digest(dict(activeAccountId='y', systemDefaultEmail='a@example.test')))

    def test_only_the_digest_reaches_the_file(self):
        self.remember(live_output(claude=entry(age=10), email='secret-person@example.test', org='secret-org'))
        text = (self.dir / 'state' / quota.LAST_GOOD_STATE).read_text()
        self.assertNotIn('secret-person', text)
        self.assertNotIn('secret-org', text)
        self.assertRegex(json.loads(text)['claude']['account_digest'], r'^[0-9a-f]{16}$')


class S5S6LastGoodFile(LiveCase):
    def path(self):
        return self.dir / 'state' / quota.LAST_GOOD_STATE

    def put(self, text):
        self.path().parent.mkdir(parents=True, exist_ok=True)
        self.path().write_text(text)

    def stored(self):
        return json.loads(self.path().read_text())

    def test_every_damaged_shape_counts_as_no_reading(self):
        digest = quota.account_digest(dict(activeAccountId=None, systemDefaultEmail='a@example.test', systemDefaultOrganizationUuid='org-1'))
        shapes = ['{"claude":"x"}', '{"claude":5}', '{"claude":[1]}', '{"claude":null}', '[]', '"x"', 'garbage', '',
                  '{"claude":{}}', '{"claude":{"updatedAt":"x"}}', '{"claude":{"updatedAt":true}}',
                  json.dumps(dict(claude=dict(updatedAt=(NOW - 60) * 1000, account_digest=digest, session='x', weekly=[1])))]
        wiped = apply_stale_policy(entry(age=40 * 60), 'server')
        for bad in shapes:
            self.put(bad)
            result = self.select('feature', explicit='opus_high', reads=[live_output(claude=wiped)],
                                 refresher=self.refresher('rate_limited', False), takeover='start')
            self.assertEqual(self.cand(result, 'opus_high')['status'], 'unknown', bad)
            self.put(bad)
            result = self.select('bugfix', reads=[live_output()], refresher=self.refresher('rate_limited', False))
            self.assertEqual(result['selection']['selected'], 'opus_high', bad)

    def test_damaged_entry_heals_even_without_a_usable_new_reading(self):
        self.put('{"claude":"x","other":"kept"}')
        quota.remember_good_reading(self.pol, self.snapshot(live_output(claude=apply_stale_policy(entry(age=300), 'server'))))
        self.assertEqual(self.stored(), dict(other='kept'))

    def test_future_reading_is_not_stored_but_one_inside_the_skew_is(self):
        quota.remember_good_reading(self.pol, self.snapshot(live_output(claude=entry(age=-61))))
        self.assertFalse(self.path().exists())
        quota.remember_good_reading(self.pol, self.snapshot(live_output(claude=entry(age=-60))))
        self.assertEqual(self.stored()['claude']['updatedAt'], (NOW + 60) * 1000)

    def test_poisoned_file_heals_with_the_next_real_reading(self):
        self.put(json.dumps(dict(claude=dict(updatedAt=(NOW + 86400) * 1000, account_digest='d'))))
        quota.remember_good_reading(self.pol, self.snapshot(live_output(claude=entry(age=0, session=33))))
        stored = self.stored()['claude']
        self.assertEqual(stored['updatedAt'], NOW * 1000)
        self.assertEqual(stored['session']['usedPercent'], 33)

    def test_poisoned_file_heals_with_no_new_reading_and_does_not_block_a_takeover(self):
        digest = quota.account_digest(dict(activeAccountId=None, systemDefaultEmail='a@example.test', systemDefaultOrganizationUuid='org-1'))
        self.put(json.dumps(dict(claude=dict(entry(age=-86400), account_digest=digest))))
        stale = live_output(claude=entry(age=20 * 60))
        result = self.select('feature', explicit='opus_high', reads=[stale], refresher=self.refresher('rate_limited', False), takeover='start')
        self.assertEqual(self.cand(result, 'opus_high')['status'], 'eligible')
        self.assertEqual(self.cand(result, 'opus_high')['stale_takeover']['source'], quota.SOURCE_ORCA)

    def test_poisoned_file_does_not_block_a_takeover_read_directly(self):
        digest = quota.account_digest(dict(activeAccountId=None, systemDefaultEmail='a@example.test', systemDefaultOrganizationUuid='org-1'))
        self.put(json.dumps(dict(claude=dict(entry(age=-86400), account_digest=digest))))
        result = self.select('feature', explicit='opus_high', reads=[live_output(claude=entry(age=20 * 60))], takeover='start')
        self.assertEqual(self.cand(result, 'opus_high')['stale_takeover']['source'], quota.SOURCE_ORCA)


class SurvivorsOfTheLastMutationRun(LiveCase):
    """Each of these pins behaviour a hand-made mutant got past."""
    def path(self):
        return self.dir / 'state' / quota.LAST_GOOD_STATE

    def stored(self):
        return json.loads(self.path().read_text())['claude']

    def test_reading_is_saved_before_the_refresh_is_sent(self):
        seen = []
        def refresher(pol, clock=None):
            seen.append(self.path().exists())
            return dict(attempted=True, outcome='ok')
        quota.fresh_snapshot(self.pol, ['claude'], runner=cs.runner_of(live_output(claude=entry(age=2000))), clock=lambda: NOW,
                             refresher=refresher, sleep=lambda s: None)
        self.assertEqual(seen, [True])

    def test_reading_is_saved_when_no_refresh_happens_at_all(self):
        quota.fresh_snapshot(self.pol, ['claude'], runner=cs.runner_of(live_output(claude=entry(age=10))), clock=lambda: NOW,
                             refresher=self.refresher(), sleep=lambda s: None)
        self.assertEqual(self.refreshes, [])
        self.assertEqual(self.stored()['updatedAt'], (NOW - 10) * 1000)

    def test_reading_survives_a_refresh_process_that_dies(self):
        def die(pol, clock=None):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            quota.fresh_snapshot(self.pol, ['claude'], runner=cs.runner_of(live_output(claude=entry(age=2000))), clock=lambda: NOW,
                                 refresher=die, sleep=lambda s: None)
        self.assertEqual(self.stored()['updatedAt'], (NOW - 2000) * 1000)

    def test_a_failed_reading_never_replaces_a_good_one(self):
        quota.remember_good_reading(self.pol, self.snapshot(live_output(claude=entry(age=2000, session=33))))
        for bad in (apply_stale_policy(entry(age=2000), 'server', NOW + 10),            # past retention: wiped, updatedAt=now
                    dict(entry(age=1, session=99), status='error'),                    # complete but failed, and newer
                    dict(entry(age=1, session=99), session=None),                      # ok but incomplete
                    dict(entry(age=1, session=99), status='fetching')):
            quota.remember_good_reading(self.pol, self.snapshot(live_output(claude=bad), now=NOW + 10))
            self.assertEqual(self.stored()['session']['usedPercent'], 33, bad)
            self.assertEqual(self.stored()['updatedAt'], (NOW - 2000) * 1000)

    def test_an_older_reading_never_replaces_a_newer_one(self):
        quota.remember_good_reading(self.pol, self.snapshot(live_output(claude=entry(age=10, session=33))))
        quota.remember_good_reading(self.pol, self.snapshot(live_output(claude=entry(age=500, session=99))))
        self.assertEqual(self.stored()['session']['usedPercent'], 33)
        quota.remember_good_reading(self.pol, self.snapshot(live_output(claude=entry(age=10, session=99))))
        self.assertEqual(self.stored()['session']['usedPercent'], 33)
        quota.remember_good_reading(self.pol, self.snapshot(live_output(claude=entry(age=9, session=44))))
        self.assertEqual(self.stored()['session']['usedPercent'], 44)

    def takeover_with(self, orca_age, remembered_age):
        quota.remember_good_reading(self.pol, self.snapshot(live_output(claude=entry(age=remembered_age, session=5, weekly=5))))
        kept = apply_stale_policy(entry(age=orca_age, session=30, weekly=30), 'server')
        result = self.select('feature', explicit='opus_high', reads=[live_output(claude=kept)], takeover='start')
        return self.cand(result, 'opus_high')

    def test_takeover_uses_the_newer_of_orca_and_last_good(self):
        cand = self.takeover_with(orca_age=25 * 60, remembered_age=20 * 60)
        self.assertEqual(cand['stale_takeover']['source'], 'last_good_reading')
        self.assertEqual(cand['data_age_seconds'], 20 * 60)
        self.assertEqual(cand['windows']['session']['remaining_pp'], 95)

    def test_takeover_prefers_orca_when_it_is_the_newer(self):
        cand = self.takeover_with(orca_age=20 * 60, remembered_age=25 * 60)
        self.assertEqual(cand['stale_takeover']['source'], quota.SOURCE_ORCA)
        self.assertEqual(cand['data_age_seconds'], 20 * 60)
        self.assertEqual(cand['windows']['session']['remaining_pp'], 70)

    def test_claim_waits_for_the_lock_and_gives_up_rather_than_race(self):
        root = self.dir / 'state'
        root.mkdir(parents=True, exist_ok=True)
        holder = os.open(root / quota.REMINDER_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        begun = time.monotonic()
        self.assertFalse(quota.claim_reminder(NOW * 1000, clock=lambda: NOW))
        self.assertGreaterEqual(time.monotonic() - begun, 1.9)
        self.assertFalse((root / quota.REMINDER_STATE).exists())
        fcntl.flock(holder, fcntl.LOCK_UN)
        self.assertTrue(quota.claim_reminder(NOW * 1000, clock=lambda: NOW))

    def test_budget_multiplier_counts_in_the_stale_judgment(self):
        reads = [live_output(claude=entry(age=20 * 60, session=60))]
        snap = self.snapshot(*reads)
        budget = dict(session=4, weekly=5)
        for multiplier, status in ((1, 'eligible'), (2.5, 'unknown')):
            profile = dict(self.config['models']['opus_high'], budget_multiplier=multiplier)
            cand = dict(status='unknown', agent='claude', reasons=[], profile='x')
            quota.apply_stale_takeover(cand, profile, self.pol, snap, budget, NOW * 1000)
            self.assertEqual(cand['status'], status, multiplier)

    def test_dry_run_previews_ordinary_rules_only(self):
        seen = []
        out = io.StringIO()
        meta = dict(t.META)
        with patch.object(qg, 'load_config', return_value=CONFIG), \
                patch.object(qg, 'load_meta', return_value=meta), patch.object(qg.Backend, 'show', return_value=dict(worker=dict(state='ready'))), \
                patch.object(qg.Backend, 'attempts', return_value=1), patch.object(qg.Backend, 'original', return_value='x'), \
                patch.object(qg.Backend, 'plan', lambda self, takeover=True: seen.append(takeover)), \
                patch.object(quota, 'fetch_snapshot', return_value=dict(ok=False, reason='x', providers={})), \
                contextlib.redirect_stdout(out):
            qg.main(['watch', '--dry-run', '--dispatch', 'ctx_old', '--state-dir', str(self.dir)])
        self.assertEqual(seen, [False])


class LastThreeSurvivors(LiveCase):
    def test_a_fresh_shortage_is_a_shortage_not_a_takeover_question(self):
        result = self.select('feature', explicit='opus_high', reads=[live_output(claude=entry(age=0, session=99))], takeover='start')
        cand = self.cand(result, 'opus_high')
        self.assertEqual(cand['status'], 'insufficient')
        self.assertNotIn('stale_takeover_refused', cand)

    def test_two_missing_digests_do_not_count_as_the_same_account(self):
        # The earlier version stored null for a system default login; those files are still on disk.
        path = self.dir / 'state' / quota.LAST_GOOD_STATE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(claude=dict(entry(age=10 * 60), account_digest=None))))
        wiped = apply_stale_policy(entry(age=40 * 60), 'server')
        for account in (dict(email=None), dict()):
            result = self.select('feature', explicit='opus_high', reads=[live_output(claude=wiped, **account)], takeover='start')
            self.assertIn('没有可用的历史读数', self.cand(result, 'opus_high')['stale_takeover_refused'], account)

    def test_an_unwritable_state_directory_only_costs_the_memory(self):
        blocked = self.dir / 'not-a-directory'
        blocked.write_text('x')
        snap = self.snapshot(live_output(claude=entry(age=10)))
        with patch.dict(os.environ, ORCA_ROUTING_STATE_DIR=str(blocked)):
            quota.remember_good_reading(self.pol, snap)


class O1AgeFloor(LiveCase):
    def test_a_slightly_future_reading_is_charged_nothing_and_given_nothing(self):
        kept = entry(age=-55, session=90, status='error', usageMetadata=dict(failureKind='server'))
        result = self.select('feature', explicit='opus_high', reads=[live_output(claude=kept)], takeover='start')
        cand = self.cand(result, 'opus_high')
        self.assertEqual(cand['status'], 'eligible')
        charge = cand['stale_takeover']['charges']['session']
        self.assertEqual(charge['deducted_pp'], 0)
        self.assertEqual(charge['conservative_remaining_pp'], charge['remaining_pp'])
        self.assertEqual(cand['data_age_seconds'], 0)
        self.assertEqual(cand['stale_takeover']['age_seconds'], 0)

    def test_past_the_skew_is_still_refused(self):
        kept = entry(age=-61, status='error', usageMetadata=dict(failureKind='server'))
        result = self.select('feature', explicit='opus_high', reads=[live_output(claude=kept)], takeover='start')
        self.assertIn('在未来', self.cand(result, 'opus_high')['stale_takeover_refused'])


class O3AlertsNameTheFailure(LiveCase):
    def test_wipe_is_not_reported_as_an_advance(self):
        before = entry(age=40 * 60)
        wiped = apply_stale_policy(before, 'network')
        snap = quota.fresh_snapshot(self.pol, ['claude'], runner=cs.runner_of(live_output(claude=before), live_output(claude=wiped)),
                                    clock=lambda: NOW, refresher=self.refresher(), sleep=lambda s: None)
        info = snap['refresh']['providers']['claude']
        self.assertEqual(info, dict(status='error', advanced=False, failure_kind='network'))
        text = quota.describe_refresh(snap['refresh'])
        self.assertIn('error', text)
        self.assertIn('network', text)

    def test_advanced_but_failed_still_names_the_failure(self):
        text = quota.describe_refresh(dict(outcome='ok', providers=dict(codex=dict(advanced=True, status='error', failure_kind='network'))))
        self.assertIn('codex 已更新', text)
        self.assertIn('error', text)
        self.assertIn('network', text)

    def test_candidate_reason_names_the_failure_kind(self):
        bad = apply_stale_policy(entry(age=300), 'server')
        cand = self.cand(self.select('bugfix', reads=[live_output(claude=bad)]), 'opus_high')
        self.assertIn('claude 额度状态不是 ok（error，server）', cand['reasons'])

    def test_guard_notice_carries_the_refresh_result(self):
        guard_dir = self.dir / 'guard'
        guard_dir.mkdir()
        backend = t.FakeBackend(guard_dir)
        guard = qg.Guard(t.enabled_config(), t.META, backend, guard_dir)
        snap = self.snapshot(live_output(codex=apply_stale_policy(entry(age=40 * 60), 'network')))
        snap['refresh'] = dict(outcome='ok', providers=dict(codex=dict(advanced=False, status='error', failure_kind='network')))
        self.assertEqual(guard.tick(snap), 'continue')
        notice = backend.bodies('run:')[-1]
        self.assertIn('额度状态不是 ok（error，network）', notice)
        self.assertIn('刷新结果', notice)
        self.assertIn('codex 未更新', notice)


class O5ModuleDoc(unittest.TestCase):
    def test_it_does_not_claim_to_be_read_only(self):
        self.assertNotIn('Read-only:', quota.__doc__)
        for word in ('usage-refresh.json', 'last-good-usage.json', 'restore-reminder.json'):
            self.assertIn(word, quota.__doc__)


if __name__ == '__main__':
    unittest.main()
