"""Browser split: quota picks inside each kind; external/legacy kinds never yield Terra. No worker is launched."""
import itertools
import json
import subprocess
import sys
import unittest
from pathlib import Path
import hermetic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import quota

CONFIG = hermetic.read_routes()
NOW = 1800000000


def win(used, minutes, elapsed_share=0.5):
    return dict(usedPercent=used, windowMinutes=minutes,
                resetsAt=int((NOW + minutes * 60 * (1 - elapsed_share)) * 1000))


def pool(state):
    """state: ok | ahead (pace>1) | behind (pace<1) | short (insufficient) | stale (unknown) | missing."""
    if state == 'missing':
        return None
    used = {'ok': 30, 'ahead': 20, 'behind': 80, 'short': 99.5, 'stale': 30}[state]
    upd = (NOW - 5000) * 1000 if state == 'stale' else NOW * 1000
    return dict(status='ok', updatedAt=upd, session=win(10, 300), weekly=win(used, 10080))


def ag_pool(state):
    """Antigravity entry: quota is read from the 'Gemini Models' buckets (see quota.fetch_snapshot)."""
    if state == 'missing':
        return None
    used = {'ok': 30, 'ahead': 20, 'behind': 80, 'short': 99.5, 'stale': 30}[state]
    upd = (NOW - 5000) * 1000 if state == 'stale' else NOW * 1000
    return dict(status='ok', updatedAt=upd, buckets=[
        dict(group='Gemini Models', **win(10, 300)),
        dict(group='Gemini Models', **win(used, 10080))])


def runner_for(claude, codex, antigravity='missing'):
    def runner(*args, **kwargs):
        rate = {k: v for k, v in (('claude', pool(claude)), ('codex', pool(codex)),
                                  ('antigravity', ag_pool(antigravity))) if v is not None}
        return subprocess.CompletedProcess(args, 0, json.dumps({'result': {'rateLimits': rate}}).encode())
    return runner


def sel(kind, claude, codex, complexity='normal', profile=None, antigravity='missing'):
    r = quota.select(CONFIG, kind, complexity, explicit_profile=profile,
                     runner=runner_for(claude, codex, antigravity), clock=lambda: NOW)
    return r['selection']


STATES = ('ok', 'ahead', 'behind', 'short', 'stale', 'missing')
EXTERNAL = [k for k, t in CONFIG['tasks'].items() if t.get('browser_boundary') == 'external_logged_in']


class Tests(unittest.TestCase):
    def test_external_kinds_exist_including_legacy(self):
        self.assertEqual(sorted(EXTERNAL), ['browser', 'browser_external'])

    def test_external_never_terra_any_quota_state(self):
        for kind, c, x, cx in itertools.product(EXTERNAL, STATES, STATES, ('normal', 'hard')):
            s = sel(kind, c, x, cx)
            if s['selected']:
                m = CONFIG['models'][s['selected']]
                self.assertEqual(m['agent'], 'claude', (kind, c, x, cx, s))
                self.assertNotIn('terra', m['model'])
                self.assertNotIn(m['model'], CONFIG['tasks'][kind].get('forbidden_models', []))

    def test_external_explicit_terra_rejected(self):
        for kind in EXTERNAL:
            with self.assertRaises(ValueError):
                sel(kind, 'ok', 'ok', profile='terra_medium')

    def test_external_claude_short_blocks_instead_of_crossing_boundary(self):
        s = sel('browser_external', 'short', 'ok')
        self.assertIsNone(s['selected'])
        self.assertEqual(s['blocked_code'], 'quota_insufficient')

    def test_external_claude_unknown_blocks(self):
        s = sel('browser_external', 'stale', 'ok')
        self.assertIsNone(s['selected'])
        self.assertEqual(s['blocked_code'], 'quota_unknown')

    def test_check_prefers_sonnet_then_terra_when_claude_short(self):
        self.assertEqual(sel('browser_check', 'behind', 'ahead')['selected'], 'sonnet_high')
        self.assertEqual(sel('browser_check', 'short', 'ok')['selected'], 'terra_medium')

    def test_check_unknown_claude_blocks_not_terra(self):
        self.assertEqual(sel('browser_check', 'stale', 'ok')['blocked_code'], 'quota_unknown')

    def test_debug_capability_first_when_claude_on_pace(self):
        s = sel('browser_debug', 'ahead', 'ok')
        self.assertEqual(s['selected'], 'opus_xhigh')
        self.assertIn('主选优先', s['reason'])

    def test_debug_falls_to_sol_when_claude_short(self):
        self.assertEqual(sel('browser_debug', 'short', 'ok')['selected'], 'sol_high')

    def test_debug_efforts(self):
        sys.argv = ['route.py']
        import route
        rule = CONFIG['tasks']['browser_debug']
        prof = CONFIG['models'][rule['primary']]
        self.assertEqual(rule['primary'], 'opus_xhigh')
        self.assertEqual(route.effort_for(rule, prof, 'normal'), 'xhigh')
        self.assertEqual(route.effort_for(rule, prof, 'hard'), 'xhigh')

    def test_flow_gemini_when_antigravity_ok(self):
        for cx in ('normal', 'hard'):
            s = sel('browser_flow', 'ok', 'ok', cx, antigravity='ok')
            self.assertEqual(s['selected'], 'gemini', cx)
            self.assertIn('prefer_primary', s['reason'])

    def test_flow_opus_xhigh_when_antigravity_short(self):
        s = sel('browser_flow', 'ok', 'ok', antigravity='short')
        self.assertEqual(s['selected'], 'opus_xhigh')

    def test_flow_gemini_effort_is_embedded(self):
        sys.argv = ['route.py']
        import route
        rule = CONFIG['tasks']['browser_flow']
        prof = CONFIG['models']['gemini']
        self.assertIsNone(route.effort_for(rule, prof, 'normal'))
        self.assertIsNone(route.effort_for(rule, prof, 'hard'))

    def test_debug_opus_xhigh_on_pace_both_complexities(self):
        for cx in ('normal', 'hard'):
            s = sel('browser_debug', 'ok', 'ok', cx)
            self.assertEqual(s['selected'], 'opus_xhigh', cx)
            self.assertIn('主选优先', s['reason'])

    def test_external_never_gemini_or_terra_with_antigravity_states(self):
        for kind, c, x, a, cx in itertools.product(EXTERNAL, STATES, STATES, STATES, ('normal', 'hard')):
            s = sel(kind, c, x, cx, antigravity=a)
            if s['selected']:
                m = CONFIG['models'][s['selected']]
                self.assertEqual(m['agent'], 'claude', (kind, c, x, a, cx, s))
                self.assertNotIn('gemini', s['selected'])
                self.assertNotIn('terra', m['model'])

    def test_external_claude_short_does_not_fall_to_gemini(self):
        s = sel('browser_external', 'short', 'ok', antigravity='ok')
        self.assertIsNone(s['selected'])
        self.assertEqual(s['blocked_code'], 'quota_insufficient')


if __name__ == '__main__':
    unittest.main()
