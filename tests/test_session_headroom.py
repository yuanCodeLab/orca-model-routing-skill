"""Codex 5-hour session headroom before dispatch, and the per-pool primary_first pace threshold.

Hermetic: a fake `orca account list --json` runner, a fixed clock, and Codex session logs turned off in a copy
of the test fixture, so no real account data is read.
"""
import hermetic  # noqa: F401  (must come first: keeps tests away from the real Orca state)
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import quota  # noqa: E402
import route  # noqa: E402

CONFIG = hermetic.read_routes()
NOW = 1800000000


def base_config():
    config = copy.deepcopy(CONFIG)
    config['quota_policy']['codex_session_log']['enabled'] = False
    return config


def session(available, minutes_to_reset=150):
    return dict(usedPercent=100 - available, windowMinutes=300, resetsAt=(NOW + minutes_to_reset * 60) * 1000)


def weekly(used, elapsed_share=0.5):
    return dict(usedPercent=used, windowMinutes=10080, resetsAt=int((NOW + 10080 * 60 * (1 - elapsed_share)) * 1000))


def pool(sess, week):
    return dict(status='ok', updatedAt=NOW * 1000, session=sess, weekly=week)


# Codex weekly used 75% at half the window: pace_ratio 0.5, far below the default 1.3.
CODEX_SLOW = weekly(75)
CLAUDE_OK = pool(session(90), weekly(10))


def runner_for(codex, claude=CLAUDE_OK):
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps(
            {'result': {'rateLimits': {'codex': codex, 'claude': claude}}}).encode())
    return runner


def select(kind, complexity, codex, claude=CLAUDE_OK, config=None, profile=None):
    return quota.select(config or base_config(), kind, complexity, explicit_profile=profile,
                        runner=runner_for(codex, claude), clock=lambda: NOW)


def cand(result, name):
    return next(c for c in result['candidates'] if c['profile'] == name)


class Config(unittest.TestCase):
    def test_routes_values(self):
        qp = CONFIG['quota_policy']
        sh = qp['session_headroom']
        self.assertEqual(sh['pools'], ['codex'])
        self.assertEqual((sh['normal'], sh['hard']), (40, 60))
        self.assertEqual(sh['by_kind'], {'bounded': 20, 'mechanical': 20, 'browser_check': 20})
        self.assertEqual(sh['_comment'], 'Synthetic thresholds for session headroom tests.')
        self.assertEqual(qp['primary_first']['min_weekly_pace_ratio'], 1.3)
        self.assertEqual(qp['primary_first']['min_weekly_pace_ratio_by_pool'], {'codex': 0})
        self.assertEqual(qp['primary_first']['_comment'], 'Synthetic per-pool pace override.')
        # Controlled weights and budgets for selection tests.
        self.assertEqual(qp['capacity_factors'], {'codex': 1, 'claude': 5, 'antigravity': 2})
        self.assertEqual(qp['task_budget_points'], {'normal': {'session': 2, 'weekly': 5},
                                                    'hard': {'session': 5, 'weekly': 10}})

    def test_invalid_config_is_rejected(self):
        for bad in (dict(pools='codex', normal=40, hard=60), dict(pools=['codex'], normal=140, hard=60),
                    dict(pools=['codex'], normal=40), dict(pools=['codex'], normal=40, hard=60, by_kind={'x': -1})):
            config = base_config()
            config['quota_policy']['session_headroom'] = bad
            with self.assertRaises(quota.PolicyError):
                quota.validate_policy(config['quota_policy'])
        config = base_config()
        config['quota_policy']['primary_first']['min_weekly_pace_ratio_by_pool'] = {'codex': -1}
        with self.assertRaises(quota.PolicyError):
            quota.validate_policy(config['quota_policy'])

    def test_near_reset_uses_guard_defaults(self):
        pol = quota.validate_policy(CONFIG['quota_policy'])
        guard = CONFIG['quota_policy']['quota_guard']
        self.assertEqual(pol['session_headroom']['burn'], guard['default_burn_pp_per_min']['session'])
        self.assertEqual(pol['session_headroom']['floor'], guard['floor_pp'])


class FeatureNormal(unittest.TestCase):
    def test_codex_session_30_blocks_sol_and_picks_opus(self):
        r = select('feature', 'normal', pool(session(30), weekly(10)))
        sol = cand(r, 'sol')
        self.assertEqual(sol['status'], 'insufficient')
        why = '；'.join(sol['reasons'])
        self.assertIn('余量不足', why)
        self.assertIn('可用 30 点 < 所需 40 点', why)
        self.assertEqual(sol['windows']['session']['required_pp'], 40)
        self.assertEqual(sol['windows']['session']['budget_pp'], 2)
        self.assertFalse(sol['windows']['session']['session_headroom']['passed'])
        self.assertEqual(r['selection']['selected'], 'opus_high')
        self.assertIn('主选已知额度不足', r['selection']['reason'])

    def test_codex_session_45_slow_week_keeps_sol_primary(self):
        r = select('feature', 'normal', pool(session(45), CODEX_SLOW))
        sol = cand(r, 'sol')
        self.assertEqual(sol['status'], 'eligible')
        self.assertAlmostEqual(sol['windows']['weekly']['pace_ratio'], 0.5, places=3)
        self.assertTrue(sol['windows']['session']['session_headroom']['passed'])
        self.assertIn('满足', sol['windows']['session']['session_headroom']['verdict'])
        self.assertEqual(r['selection']['selected'], 'sol')
        self.assertIn('主选优先', r['selection']['reason'])
        self.assertIn('min_weekly_pace_ratio_by_pool.codex', r['selection']['reason'])
        # The per-pool override is what keeps Sol: without it the score decides and Claude (factor 7) wins.
        config = base_config()
        config['quota_policy']['primary_first'].pop('min_weekly_pace_ratio_by_pool')
        self.assertEqual(select('feature', 'normal', pool(session(45), CODEX_SLOW), config=config)
                         ['selection']['selected'], 'opus_high')

    def test_exactly_at_threshold_is_enough(self):
        self.assertEqual(cand(select('feature', 'normal', pool(session(40), CODEX_SLOW)), 'sol')['status'], 'eligible')


class FeatureHard(unittest.TestCase):
    def test_codex_session_50_blocks_sol(self):
        r = select('feature', 'hard', pool(session(50), weekly(10)))
        sol = cand(r, 'sol')
        self.assertEqual(sol['status'], 'insufficient')
        self.assertEqual(sol['windows']['session']['required_pp'], 60)
        self.assertIn('session_headroom.hard', '；'.join(sol['reasons']))
        self.assertEqual(r['selection']['selected'], 'opus_high')

    def test_codex_session_65_allows_sol(self):
        r = select('feature', 'hard', pool(session(65), CODEX_SLOW))
        self.assertEqual(r['selection']['selected'], 'sol')


class Bounded(unittest.TestCase):
    def test_session_25_picks_terra(self):
        r = select('bounded', 'normal', pool(session(25), CODEX_SLOW))
        terra = cand(r, 'terra_medium')
        self.assertEqual(terra['status'], 'eligible')
        self.assertEqual(terra['windows']['session']['session_headroom']['basis'], 'by_kind.bounded.normal')
        self.assertEqual(terra['windows']['session']['required_pp'], 20)
        self.assertEqual(r['selection']['selected'], 'terra_medium')

    def test_session_15_falls_back_to_haiku(self):
        r = select('bounded', 'normal', pool(session(15), CODEX_SLOW))
        self.assertEqual(cand(r, 'terra_medium')['status'], 'insufficient')
        self.assertEqual(r['selection']['selected'], 'haiku_high')

    def test_mechanical_and_browser_check_use_by_kind(self):
        r = select('mechanical', 'normal', pool(session(25), CODEX_SLOW))
        self.assertEqual(r['selection']['selected'], 'terra_low')
        r = select('browser_check', 'normal', pool(session(25), weekly(10)))
        self.assertEqual(cand(r, 'terra_medium')['windows']['session']['required_pp'], 20)

    def test_hard_by_kind_number_applies_and_never_drops_below_task_budget(self):
        # A plain number covers hard too: bounded hard needs max(hard task budget 5, 20) = 20.
        r = select('bounded', 'hard', pool(session(25), CODEX_SLOW))
        info = cand(r, 'terra_medium')['windows']['session']
        self.assertEqual(info['session_headroom']['basis'], 'by_kind.bounded.hard')
        self.assertEqual(info['required_pp'], 20)
        # An object form sets hard apart; a value under the hard task budget is floored at that budget.
        config = base_config()
        config['quota_policy']['session_headroom']['by_kind']['bounded'] = {'normal': 1, 'hard': 2}
        r = select('bounded', 'hard', pool(session(4), CODEX_SLOW), config=config)
        terra = cand(r, 'terra_medium')
        self.assertEqual(terra['windows']['session']['required_pp'], 5)
        self.assertEqual(terra['status'], 'insufficient')
        self.assertIn('可派发 4 点 < 预算 5 点', '；'.join(terra['reasons']))


class NearReset(unittest.TestCase):
    def test_eight_minutes_to_reset_lowers_threshold_to_17(self):
        r = select('feature', 'normal', pool(session(20, minutes_to_reset=8), CODEX_SLOW))
        sol = cand(r, 'sol')
        sh = sol['windows']['session']['session_headroom']
        self.assertTrue(sh['near_reset_exempt'])
        self.assertAlmostEqual(sh['effective_pp'], 1.5 * 8 + 5)
        self.assertAlmostEqual(sol['windows']['session']['required_pp'], 17)
        self.assertIn('临近重置豁免', sh['verdict'])
        self.assertEqual(sol['status'], 'eligible')
        self.assertEqual(r['selection']['selected'], 'sol')

    def test_exemption_still_needs_the_lowered_threshold(self):
        r = select('feature', 'normal', pool(session(15, minutes_to_reset=8), CODEX_SLOW))
        sol = cand(r, 'sol')
        self.assertEqual(sol['status'], 'insufficient')
        self.assertIn('临近重置豁免后为 17 点', '；'.join(sol['reasons']))

    def test_exemption_never_goes_below_task_budget(self):
        # 0.5 min to reset: cap 5.75 > hard task budget 5, still floored by max().
        r = select('feature', 'hard', pool(session(5.5, minutes_to_reset=0.5), CODEX_SLOW))
        self.assertAlmostEqual(cand(r, 'sol')['windows']['session']['required_pp'], 5.75)


class OtherPoolsUnchanged(unittest.TestCase):
    def test_claude_session_10_judged_by_task_budget_only(self):
        # Claude session: used 85 at half the window, prorated reserve 5, so 10 pp available (budget 2).
        claude = pool(session(15), weekly(10))
        r = select('bugfix', 'normal', pool(session(90), weekly(10)), claude=claude)
        opus = cand(r, 'opus_high')
        self.assertAlmostEqual(opus['windows']['session']['available_pp'], 10, places=3)
        self.assertNotIn('session_headroom', opus['windows']['session'])
        self.assertNotIn('required_pp', opus['windows']['session'])
        self.assertEqual(opus['status'], 'eligible')

    def test_claude_as_codex_alternate_unaffected(self):
        claude = pool(session(15), weekly(10))
        r = select('feature', 'normal', pool(session(30), weekly(10)), claude=claude)
        self.assertEqual(cand(r, 'opus_high')['status'], 'eligible')
        self.assertEqual(r['selection']['selected'], 'opus_high')

    def test_guard_probe_without_task_has_no_headroom(self):
        pol = quota.validate_policy(base_config()['quota_policy'])
        snap = quota.fetch_snapshot(pol, runner=runner_for(pool(session(10), weekly(10))), clock=lambda: NOW)
        probe = quota.evaluate_candidate('guard', dict(agent='codex', model='guard', enabled=True), 'primary',
                                         pol, snap, pol['budgets']['normal'], NOW * 1000)
        self.assertEqual(probe['status'], 'eligible')
        self.assertNotIn('session_headroom', probe['windows']['session'])

    def test_absent_section_restores_old_behaviour(self):
        config = base_config()
        del config['quota_policy']['session_headroom']
        r = select('feature', 'normal', pool(session(30), CODEX_SLOW), config=config)
        self.assertEqual(cand(r, 'sol')['status'], 'eligible')
        self.assertEqual(r['selection']['selected'], 'sol')


class WeeklyBudgetUnchanged(unittest.TestCase):
    def test_codex_weekly_below_task_budget_blocks(self):
        r = select('feature', 'normal', pool(session(90), weekly(97)))
        sol = cand(r, 'sol')
        self.assertEqual(sol['windows']['weekly']['budget_pp'], 5)
        self.assertNotIn('required_pp', sol['windows']['weekly'])
        self.assertEqual(sol['status'], 'insufficient')
        self.assertIn('weekly 可派发 3 点 < 预算 5 点', '；'.join(sol['reasons']))
        self.assertEqual(r['selection']['selected'], 'opus_high')

    def test_codex_weekly_at_task_budget_passes(self):
        self.assertEqual(cand(select('feature', 'normal', pool(session(90), weekly(95))), 'sol')['status'],
                         'eligible')


class PrimaryFirstOtherPools(unittest.TestCase):
    def test_threshold_lookup(self):
        pf = quota.validate_policy(CONFIG['quota_policy'])['primary_first']
        self.assertEqual(quota.primary_first_threshold(pf, 'codex'), (0.0, 'min_weekly_pace_ratio_by_pool.codex'))
        self.assertEqual(quota.primary_first_threshold(pf, 'claude'), (1.3, 'min_weekly_pace_ratio'))
        self.assertEqual(quota.primary_first_threshold(pf, 'antigravity'), (1.3, 'min_weekly_pace_ratio'))

    def claude_pace(self, pace):
        # Weekly reserve 5 prorated to 2.5 at half the window: pace = available/97.5/0.5.
        available = pace * 97.5 * 0.5
        return pool(session(90), weekly(100 - 2.5 - available))

    def test_claude_primary_below_1_3_is_not_primary_first(self):
        r = select('bugfix', 'normal', pool(session(90), weekly(10)), claude=self.claude_pace(1.2))
        self.assertAlmostEqual(cand(r, 'opus_high')['windows']['weekly']['pace_ratio'], 1.2, places=3)
        self.assertNotIn('主选优先', r['selection']['reason'])

    def test_claude_primary_above_1_3_is_primary_first(self):
        r = select('bugfix', 'normal', pool(session(90), weekly(10)), claude=self.claude_pace(1.4))
        self.assertIn('主选优先', r['selection']['reason'])
        self.assertIn('≥ 1.3（min_weekly_pace_ratio；', r['selection']['reason'])


class StaleTakeoverPath(unittest.TestCase):
    """The fixture enables stale takeover for Claude; a copy that adds Codex shows the headroom is charged there too."""
    def judge(self, available, headroom):
        config = base_config()
        config['quota_policy']['guard_stale_takeover']['agents'] = ['claude', 'codex']
        pol = quota.validate_policy(config['quota_policy'])
        prov = pool(session(available), weekly(10))
        prov['updatedAt'] = (NOW - 600) * 1000  # 10 minutes old
        return quota._takeover_judgement('codex', config['models']['sol'], pol, prov, 'codex',
                                         pol['budgets']['normal'], NOW * 1000, 5, headroom)

    def test_conservative_remaining_must_cover_headroom(self):
        pol = quota.validate_policy(CONFIG['quota_policy'])
        headroom = quota.session_headroom_for(pol, 'codex', 'feature', 'normal')
        # 60 − 1.5×15 min = 37.5 < 40.
        details, why = self.judge(60, headroom)
        self.assertIsNone(details)
        self.assertIn('所需（含 5 小时余量门槛） 40', why)
        details, why = self.judge(70, headroom)
        self.assertIsNone(why)
        self.assertEqual(details['charges']['session']['required_pp'], 40)
        self.assertEqual(details['charges']['weekly']['required_pp'], 5)
        # Without a task the old budget alone applies.
        self.assertIsNone(self.judge(60, None)[1])


class ExplicitAndOutput(unittest.TestCase):
    def test_explicit_codex_profile_blocked_as_insufficient(self):
        r = select('feature', 'normal', pool(session(30), weekly(10)), profile='sol')
        self.assertIsNone(r['selection']['selected'])
        self.assertEqual(r['selection']['blocked_code'], 'quota_insufficient')
        self.assertIn('余量不足', r['selection']['blocked_reason'])

    def test_explicit_codex_profile_with_headroom_starts(self):
        r = select('feature', 'normal', pool(session(50), weekly(10)), profile='sol')
        self.assertEqual(r['selection']['selected'], 'sol')

    def test_plan_report_shows_threshold(self):
        report = route.build_report(select('feature', 'normal', pool(session(30), weekly(10))))
        sol = next(c for c in report['candidates'] if c['profile'] == 'sol')
        sh = sol['windows']['session']['session_headroom']
        self.assertEqual((sh['base_pp'], sh['required_pp'], sh['task_budget_pp']), (40, 40, 2))
        self.assertIn('不足', sh['verdict'])
        json.dumps(report, ensure_ascii=False)

    def test_fast_mode_still_uses_task_budget(self):
        # Codex weekly pace 1.4 so fast mode can apply; the 40 pp headroom is not doubled into an 80 pp need.
        r = select('feature', 'normal', pool(session(50), weekly(30)))
        plan = dict(profile='sol', agent='codex')
        self.assertTrue(route.fast_decision(base_config(), plan, r)['apply'])


if __name__ == '__main__':
    unittest.main()
