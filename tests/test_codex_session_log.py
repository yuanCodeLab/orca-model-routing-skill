"""Codex live quota source. No real Orca or real session logs: injected runner and temp rollouts."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import hermetic  # noqa: F401  (must precede anything that can reach Orca)
sys.path.insert(0, str(ROOT / 'scripts'))
import quota
import route

CONFIG = hermetic.read_routes()
NOW = 1793810000
SESSION_RESET = NOW + 6000
WEEKLY_RESET = NOW + 500000
SECRET = 'TOP-SECRET-SESSION-CONTENT'


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace('+00:00', 'Z')


def event(age=0, session=79.0, weekly=28.0, limit='codex', session_reset=SESSION_RESET, weekly_reset=WEEKLY_RESET,
          session_minutes=300):
    return json.dumps(dict(timestamp=stamp(NOW - age), type='event_msg', payload=dict(
        type='token_count', info={'note': SECRET},
        rate_limits=dict(limit_id=limit, primary=dict(used_percent=session, window_minutes=session_minutes,
                                                      resets_at=session_reset),
                         secondary=dict(used_percent=weekly, window_minutes=10080, resets_at=weekly_reset)))))


def orca_output(age=2000, session=29, weekly=20, account='acct-1', extra=None, reset_shift=0):
    codex = dict(status='ok', updatedAt=(NOW - age) * 1000,
                 session=dict(usedPercent=session, windowMinutes=300, resetsAt=(SESSION_RESET + reset_shift) * 1000),
                 weekly=dict(usedPercent=weekly, windowMinutes=10080, resetsAt=(WEEKLY_RESET + reset_shift) * 1000))
    rate = dict(codex=codex, claude=dict(status='ok', updatedAt=NOW * 1000,
                session=dict(usedPercent=30, windowMinutes=300, resetsAt=(NOW + 9000) * 1000),
                weekly=dict(usedPercent=30, windowMinutes=10080, resetsAt=(NOW + 90000) * 1000)))
    result = dict(rateLimits=rate, codex=dict(activeAccountId=account, accounts=[{'email': 'hidden@example.test'}]))
    result.update(extra or {})
    return json.dumps(dict(result=result))


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'sessions'
        self.day = self.root / '2026' / '11' / '04'
        self.day.mkdir(parents=True)
        self.config = copy.deepcopy(CONFIG)
        self.pol = quota.validate_policy(self.config['quota_policy'])

    def write(self, *lines, name='rollout-a.jsonl', mtime=None):
        path = self.day / name
        path.write_text('\n'.join(lines) + '\n')
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def snap(self, output=None, roots=None, pol=None):
        output = orca_output() if output is None else output
        runner = lambda *a, **k: subprocess.CompletedProcess(a, 0, output, '')
        return quota.fetch_snapshot(pol or self.pol, runner=runner, clock=lambda: NOW,
                                    log_roots=[self.root] if roots is None else roots)

    def codex(self, *a, **k):
        return self.snap(*a, **k)['providers']['codex']


class SourceSelection(Base):
    def test_fresh_log_replaces_stale_orca_values_and_time_together(self):
        self.write(event(age=5))
        c = self.codex()
        self.assertEqual(c['data_source'], 'codex_session_log')
        self.assertEqual((c['session']['usedPercent'], c['weekly']['usedPercent']), (79.0, 28.0))
        self.assertEqual(c['updatedAt'], (NOW - 5) * 1000)
        self.assertEqual(c['orca_updated_at'], (NOW - 2000) * 1000)
        self.assertEqual(c['session']['resetsAt'], SESSION_RESET * 1000)

    def test_active_account_root_trusts_real_reset_values(self):
        with patch.dict(os.environ, {'ORCA_USER_DATA_PATH': str(self.root.parent)}):
            active = quota.codex_log_roots('acct-1')[0]
            day = active / '2026' / '11' / '04'
            day.mkdir(parents=True)
            (day / 'rollout-active.jsonl').write_text(event(age=5, session_reset=1793828092, weekly_reset=1794376114))
            data = json.loads(orca_output())
            data['result']['rateLimits']['codex']['session']['resetsAt'] = 1793827923000
            data['result']['rateLimits']['codex']['weekly']['resetsAt'] = 1794376114000
            c = self.codex(json.dumps(data), roots=[active])
            self.assertEqual((c['data_source'], c['log_status']), ('codex_session_log', 'ok'))
            self.assertEqual((c['session']['usedPercent'], c['session']['resetsAt']), (79.0, 1793828092000))
            self.assertEqual((c['weekly']['usedPercent'], c['weekly']['resetsAt']), (28.0, 1794376114000))
            (day / 'rollout-active.jsonl').write_text(event(age=5, weekly_reset=WEEKLY_RESET + 1711))
            self.assertEqual(self.codex(roots=[active])['data_source'], 'codex_session_log')
            self.assertEqual(self.codex(orca_output(account=None), roots=[active])['data_source'], 'orca')

    def test_missing_active_id_never_trusts_shared_root(self):
        self.write(event(age=5, weekly_reset=WEEKLY_RESET + 1711))
        with patch.object(quota, 'codex_log_roots', return_value=[self.root]):
            c = self.codex(orca_output(account=None))
        self.assertEqual((c['data_source'], c['log_status']), ('orca', 'account_mismatch'))

    def test_log_older_than_orca_keeps_orca(self):
        self.write(event(age=100))
        c = self.codex(orca_output(age=10))
        self.assertEqual(c['data_source'], 'orca')
        self.assertEqual(c['log_status'], 'older_than_orca')
        self.assertEqual(c['session']['usedPercent'], 29)
        self.assertEqual(c['updatedAt'], (NOW - 10) * 1000)

    def test_equal_timestamp_keeps_orca(self):
        self.write(event(age=10))
        self.assertEqual(self.codex(orca_output(age=10))['data_source'], 'orca')

    def test_other_account_is_ignored(self):
        self.write(event(age=5, weekly_reset=WEEKLY_RESET - 1711))
        c = self.codex()
        self.assertEqual((c['data_source'], c['log_status']), ('orca', 'account_mismatch'))
        self.assertEqual(c['session']['usedPercent'], 29)

    def test_mismatch_on_session_window_alone_is_accepted(self):
        self.write(event(age=5, session_reset=SESSION_RESET + 3600))
        self.assertEqual(self.codex()['data_source'], 'codex_session_log')

    def test_resets_at_jitter_within_tolerance_is_accepted(self):
        # Real pairs: 1793809904/1793809911 and 1794376114/1794376115.
        self.write(event(age=5, session_reset=SESSION_RESET - 7, weekly_reset=WEEKLY_RESET + 1))
        self.assertEqual(self.codex()['data_source'], 'codex_session_log')

    def test_tolerance_boundary(self):
        self.write(event(age=5, weekly_reset=WEEKLY_RESET + 60))
        self.assertEqual(self.codex()['data_source'], 'codex_session_log')
        self.write(event(age=5, weekly_reset=WEEKLY_RESET + 61))
        self.assertEqual(self.codex()['data_source'], 'orca')

    def test_other_limit_id_is_skipped_for_the_earlier_codex_event(self):
        self.write(event(age=50, session=70.0), event(age=5, session=1.0, limit='premium'))
        c = self.codex()
        self.assertEqual(c['data_source'], 'codex_session_log')
        self.assertEqual(c['session']['usedPercent'], 70.0)

    def test_only_other_limit_id_falls_back(self):
        self.write(event(age=5, limit='premium'))
        self.assertEqual(self.codex()['log_status'], 'no_event')

    def test_truncated_tail_and_bad_lines_fall_through_to_last_good_event(self):
        good = event(age=30, session=66.0)
        self.write(good, '{not json "rate_limits"', event(age=5, session=99.0)[:80])
        c = self.codex()
        self.assertEqual(c['session']['usedPercent'], 66.0)

    def test_only_bad_lines_fall_back(self):
        self.write('{"rate_limits"', 'null "rate_limits"', '["rate_limits"]', '"rate_limits"')
        c = self.codex()
        self.assertEqual((c['data_source'], c['log_status']), ('orca', 'no_event'))

    def test_abnormal_values_are_rejected(self):
        for bad in (event(age=5, session=101.0), event(age=5, session='x'),
                    event(age=5, session_minutes=60), event(age=5, session_reset=None),
                    event(age=-3600)):
            self.write(bad)
            self.assertEqual(self.codex()['data_source'], 'orca', bad)

    def test_small_future_stamp_within_skew_is_accepted(self):
        self.write(event(age=-30))
        self.assertEqual(self.codex()['data_source'], 'codex_session_log')

    def test_missing_timestamp_or_naive_timestamp_rejected(self):
        naive = datetime.fromtimestamp(NOW - 5).isoformat()
        for change in ({'timestamp': None}, {'timestamp': naive}, {'timestamp': 'yesterday'}):
            self.write(json.dumps(dict(json.loads(event(age=5)), **change)))
            self.assertEqual(self.codex()['data_source'], 'orca', change)

    def test_only_token_count_event_shape_counts(self):
        real = json.loads(event(age=5))
        for wrong in (dict(real, type='response_item'), dict(real, payload=dict(real['payload'], type='user_message')),
                      dict(real, payload=dict(real['payload'], rate_limits=None)), dict(real, payload='rate_limits')):
            self.write(json.dumps(wrong))
            self.assertEqual(self.codex()['data_source'], 'orca', wrong)

    def test_quoted_event_inside_session_content_cannot_forge_quota(self):
        quoted = json.dumps(dict(timestamp=stamp(NOW), type='response_item',
                                 payload=dict(type='message', text=event(age=0, session=1.0))))
        self.write(event(age=30, session=66.0), quoted)
        self.assertEqual(self.codex()['session']['usedPercent'], 66.0)

    def test_oversized_line_is_not_parsed(self):
        self.write(json.dumps(dict(json.loads(event(age=5)), padding='x' * 20000)))
        with patch.object(quota.json, 'loads', wraps=json.loads) as loads:
            self.assertEqual(self.codex()['log_status'], 'no_event')
        self.assertEqual(loads.call_count, 1)  # Orca's output only.

    def test_only_tail_is_read_and_leading_fragment_dropped(self):
        pol = copy.deepcopy(self.pol); pol['session_log']['tail_bytes'] = 1500
        padding = json.dumps(dict(type='other', text='y' * 5000))
        self.write(event(age=500, session=11.0), padding, event(age=5, session=66.0))
        self.assertEqual(self.codex(pol=pol)['session']['usedPercent'], 66.0)
        self.write(event(age=500, session=11.0), padding)
        self.assertEqual(self.codex(pol=pol)['data_source'], 'orca')

    def test_tail_fragment_that_looks_valid_is_not_trusted(self):
        pol = copy.deepcopy(self.pol); pol['session_log']['tail_bytes'] = len(event(age=5)) - 10
        self.write(event(age=5))
        self.assertEqual(self.codex(pol=pol)['log_status'], 'no_event')

    def test_newest_event_among_most_recent_files_wins(self):
        self.write(event(age=50, session=50.0), name='rollout-new-mtime.jsonl', mtime=NOW)
        self.write(event(age=5, session=60.0), name='rollout-older-mtime.jsonl', mtime=NOW - 100)
        self.assertEqual(self.codex()['session']['usedPercent'], 60.0)

    def test_files_beyond_max_files_are_not_opened(self):
        pol = copy.deepcopy(self.pol); pol['session_log']['max_files'] = 1
        self.write(event(age=50, session=50.0), name='rollout-recent.jsonl', mtime=NOW)
        self.write(event(age=5, session=60.0), name='rollout-old.jsonl', mtime=NOW - 1000)
        self.assertEqual(self.codex(pol=pol)['session']['usedPercent'], 50.0)

    def test_non_rollout_files_and_symlinks_are_ignored(self):
        (self.day / 'notes.jsonl').write_text(event(age=5) + '\n')
        outside = self.day.parent / 'outside.jsonl'
        outside.write_text(event(age=5) + '\n')
        (self.day / 'rollout-link.jsonl').symlink_to(outside)
        self.assertEqual(self.codex()['log_status'], 'no_logs')

    def test_no_logs_falls_back(self):
        c = self.codex()
        self.assertEqual((c['data_source'], c['log_status']), ('orca', 'no_logs'))
        self.assertEqual(self.codex(roots=[self.root / 'missing'])['log_status'], 'no_logs')

    def test_read_error_falls_back(self):
        self.write(event(age=5))
        with patch.object(quota, '_tail_lines_newest_first', side_effect=PermissionError('fake')):
            self.assertEqual(self.codex()['log_status'], 'no_event')
        with patch.object(quota, 'read_codex_rate_limits', side_effect=OSError('fake')):
            c = self.codex()
        self.assertEqual((c['data_source'], c['log_status']), ('orca', 'error'))

    def test_disabled_in_config_never_reads(self):
        pol = copy.deepcopy(self.pol); pol['session_log']['enabled'] = False
        self.write(event(age=5))
        with patch.object(quota, 'read_codex_rate_limits', side_effect=AssertionError('must not read')):
            self.assertEqual(self.codex(pol=pol)['log_status'], 'disabled')

    def test_policy_without_block_is_opt_out(self):
        raw = copy.deepcopy(self.config['quota_policy']); del raw['codex_session_log']
        self.assertFalse(quota.validate_policy(raw)['session_log']['enabled'])

    def test_invalid_block_is_policy_error(self):
        raw = copy.deepcopy(self.config['quota_policy'])
        raw['codex_session_log']['resets_at_tolerance_seconds'] = -1
        with self.assertRaises(quota.PolicyError):
            quota.validate_policy(raw)

    def test_output_without_active_account_field_does_not_read(self):
        output = json.loads(orca_output()); del output['result']['codex']
        with patch.object(quota, 'read_codex_rate_limits', side_effect=AssertionError('must not read')):
            self.assertEqual(self.codex(json.dumps(output))['log_status'], 'no_account')
            output['result']['codex'] = {}
            self.assertEqual(self.codex(json.dumps(output))['log_status'], 'no_account')

    def test_other_account_events_do_not_hide_this_accounts_older_event(self):
        self.write(event(age=50, session=70.0), event(age=5, session=1.0, weekly_reset=WEEKLY_RESET - 1711))
        c = self.codex()
        self.assertEqual((c['data_source'], c['session']['usedPercent']), ('codex_session_log', 70.0))

    def test_orca_query_failure_never_reads_logs(self):
        self.write(event(age=5))
        runner = lambda *a, **k: subprocess.CompletedProcess(a, 1, '', '')
        with patch.object(quota, 'read_codex_rate_limits', side_effect=AssertionError('must not read')):
            self.assertFalse(quota.fetch_snapshot(self.pol, runner=runner, clock=lambda: NOW, log_roots=[self.root])['ok'])

    def test_claude_and_other_pools_are_unchanged(self):
        self.write(event(age=5))
        snap = self.snap()
        claude = snap['providers']['claude']
        self.assertNotIn('data_source', claude)
        self.assertEqual(claude['session']['usedPercent'], 30)

    def test_nothing_but_numbers_survives_the_reader(self):
        self.write(event(age=5))
        text = json.dumps(self.snap(), ensure_ascii=False)
        for leak in (SECRET, 'acct-1', 'hidden@example.test', str(self.root), 'rollout-'):
            self.assertNotIn(leak, text)


class AccountRoots(unittest.TestCase):
    def test_active_account_uses_its_home_then_system_codex(self):
        with patch.dict(os.environ, {'ORCA_USER_DATA_PATH': '/data'}):
            roots = quota.codex_log_roots('44d6a223-feea-47ca')
        self.assertEqual(roots[0], Path('/data/codex-accounts/44d6a223-feea-47ca/home/sessions'))
        self.assertEqual(roots[1], Path.home() / '.codex' / 'sessions')

    def test_system_default_account_has_only_system_codex(self):
        self.assertEqual(quota.codex_log_roots(None), [Path.home() / '.codex' / 'sessions'])

    def test_unsafe_account_id_reads_nothing(self):
        for bad in ('../../etc', 'a/b', '', 7, {'id': 1}):
            self.assertEqual(quota.codex_log_roots(bad), [], bad)

    def test_second_root_can_supply_the_newest_event(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d, 'a'), Path(d, 'b')
            a.mkdir(); b.mkdir()
            (a / 'rollout-1.jsonl').write_text(event(age=50, session=50.0) + '\n')
            (b / 'rollout-1.jsonl').write_text(event(age=5, session=60.0) + '\n')
            pol = quota.validate_policy(CONFIG['quota_policy'])
            runner = lambda *x, **k: subprocess.CompletedProcess(x, 0, orca_output(), '')
            snap = quota.fetch_snapshot(pol, runner=runner, clock=lambda: NOW, log_roots=[a, b])
        self.assertEqual(snap['providers']['codex']['session']['usedPercent'], 60.0)


class Staleness(Base):
    def candidate(self, snap):
        pol = self.pol
        return quota.evaluate_candidate('sol', self.config['models']['sol'], 'primary', pol, snap,
                                        pol['budgets']['normal'], NOW * 1000)

    def test_stale_orca_without_log_is_unknown(self):
        cand = self.candidate(self.snap(orca_output(age=901)))
        self.assertEqual(cand['status'], 'unknown')
        self.assertEqual(cand['data_source'], 'orca')

    def test_stale_orca_with_fresh_log_is_usable(self):
        self.write(event(age=5))
        cand = self.candidate(self.snap(orca_output(age=2000)))
        self.assertEqual(cand['status'], 'eligible')
        self.assertEqual(cand['data_source'], 'codex_session_log')
        self.assertEqual(cand['data_age_seconds'], 5)
        self.assertEqual(cand['windows']['session']['remaining_pp'], 21)

    def test_log_past_the_stale_limit_is_still_unknown(self):
        self.write(event(age=901))
        cand = self.candidate(self.snap(orca_output(age=2000)))
        self.assertEqual((cand['status'], cand['data_source']), ('unknown', 'codex_session_log'))

    def test_fresh_orca_is_unaffected_when_logs_are_absent(self):
        cand = self.candidate(self.snap(orca_output(age=60)))
        self.assertEqual((cand['status'], cand['data_source']), ('eligible', 'orca'))


class PlanOutput(Base):
    def run_plan(self, output):
        """route.py plan through main(), with injected Orca output and session logs."""
        import contextlib
        import io
        real = quota.fetch_snapshot
        fake = lambda pol, runner=None, clock=None, log_roots=None: real(
            pol, runner=lambda *a, **k: subprocess.CompletedProcess(a, 0, output, ''), clock=lambda: NOW,
            log_roots=[self.root])
        buf = io.StringIO()
        with patch.object(route, 'load_config', return_value=self.config), \
             patch.object(sys, 'argv', ['route.py', 'plan', '--kind', 'bugfix', '--complexity', 'normal']), \
             patch.object(quota, 'fetch_snapshot', fake), patch.object(quota.time, 'time', return_value=NOW), \
             contextlib.redirect_stdout(buf):
            route.main()
        return json.loads(buf.getvalue())

    def test_plan_shows_log_values_and_labels_the_source(self):
        self.write(event(age=5))
        out = self.run_plan(orca_output(age=2000))
        sol = next(c for c in out['quota']['candidates'] if c['profile'] == 'sol')
        self.assertEqual(sol['data_source'], 'codex_session_log')
        self.assertEqual(sol['windows']['session']['remaining_pp'], 21)
        self.assertEqual(sol['windows']['weekly']['remaining_pp'], 72)
        self.assertEqual(out['quota']['provider_data_source']['codex'], 'codex_session_log')
        self.assertEqual(out['quota']['codex_log_status'], 'ok')
        self.assertIsNotNone(out['quota']['codex_orca_updated_at'])
        self.assertEqual(out['quota']['provider_data_source']['claude'], 'orca')

    def test_plan_falls_back_and_labels_orca(self):
        out = self.run_plan(orca_output(age=60))
        sol = next(c for c in out['quota']['candidates'] if c['profile'] == 'sol')
        self.assertEqual(sol['data_source'], 'orca')
        self.assertEqual(sol['windows']['session']['remaining_pp'], 71)
        self.assertEqual(out['quota']['codex_log_status'], 'no_logs')


if __name__ == '__main__':
    unittest.main()
