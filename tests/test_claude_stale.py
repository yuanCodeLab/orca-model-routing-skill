#!/usr/bin/env python3
"""Stale quota: bounded refresh, the guard-only bounded takeover and the weight-restore reminder."""
import contextlib
import copy
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import hermetic  # noqa: F401
sys.path.insert(0, str(ROOT / 'scripts'))
import quota
import quota_guard as qg
import route
import test_quota_guard as t

CONFIG = hermetic.read_routes()
NOW = 1800000000
# Generated, so a shell or editor holding this source in its own arguments cannot look like a leak.
TOKEN = 'SECRET-' + uuid.uuid4().hex


def window(used, minutes, reset):
    return dict(usedPercent=used, windowMinutes=minutes, resetsAt=reset * 1000)


def entry(age=0, session=10, weekly=20, status='ok', session_reset=NOW + 6000, weekly_reset=NOW + 100000, **extra):
    data = dict(status=status, updatedAt=(NOW - age) * 1000,
                session=window(session, 300, session_reset), weekly=window(weekly, 10080, weekly_reset))
    data.update(extra)
    return data


def output(claude=None, codex=None, antigravity=None, account='acct-1'):
    rate = dict(claude=claude if claude is not None else entry(),
                codex=codex if codex is not None else entry(),
                antigravity=antigravity if antigravity is not None else entry())
    return json.dumps(dict(result=dict(rateLimits=rate, claude=dict(activeAccountId=account))))


def runner_of(*outputs):
    """Answers each read with the next output; the last one repeats."""
    queue = list(outputs)
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, (queue.pop(0) if len(queue) > 1 else queue[0]).encode(), b'')
    run.calls = calls
    return run


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(os.path.realpath(tmp.name))
        patcher = patch.dict(os.environ, {'ORCA_ROUTING_STATE_DIR': str(self.dir / 'state'),
                                          'ORCA_USER_DATA_PATH': str(self.dir / 'orca-data')})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = copy.deepcopy(CONFIG)
        self.pol = quota.validate_policy(self.config['quota_policy'])
        self.refreshes = []

    def refresher(self, outcome='ok', attempted=True):
        def call(pol, clock=None):
            self.refreshes.append(outcome)
            return dict(attempted=attempted, outcome=outcome)
        return call

    def select(self, kind='feature', explicit=None, reads=None, refresher=None, takeover=None, now=NOW):
        run = runner_of(*(reads or [output()]))
        return quota.select(self.config, kind, 'normal', explicit_profile=explicit, runner=run,
                            clock=lambda: now, refresher=refresher, stale_takeover=takeover)

    def cand(self, result, profile):
        return next(c for c in result['candidates'] if c['profile'] == profile)


class RefreshTrigger(Case):
    def fresh(self, keys, *reads, refresher=None):
        return quota.fresh_snapshot(self.pol, keys, runner=runner_of(*reads), clock=lambda: NOW,
                                    refresher=refresher or self.refresher(), sleep=lambda s: None)

    def test_not_stale_is_never_refreshed(self):
        snap = self.fresh(['claude', 'codex'], output(claude=entry(age=899), codex=entry(age=899)))
        self.assertEqual(self.refreshes, [])
        self.assertEqual(snap['refresh']['outcome'], 'not_stale')

    def test_just_past_stale_refreshes_and_rereads(self):
        snap = self.fresh(['claude'], output(claude=entry(age=901)), output(claude=entry(age=1)))
        self.assertEqual(self.refreshes, ['ok'])
        self.assertEqual(snap['providers']['claude']['updatedAt'], (NOW - 1) * 1000)
        self.assertTrue(snap['refresh']['providers']['claude']['advanced'])

    def test_stale_provider_outside_the_comparison_does_not_refresh(self):
        self.fresh(['codex'], output(claude=entry(age=5000), codex=entry(age=1)))
        self.assertEqual(self.refreshes, [])

    def test_a_stale_candidate_recovers_a_blocked_bugfix_plan(self):
        reads = [output(claude=entry(age=2000)), output(claude=entry(age=1))]
        self.assertEqual(self.select('bugfix', reads=reads[:1])['selection']['blocked_code'], 'quota_unknown')
        result = self.select('bugfix', reads=reads, refresher=self.refresher())
        self.assertEqual(result['selection']['selected'], 'opus_high')
        self.assertEqual(result['source']['refresh']['outcome'], 'ok')

    def test_no_refresher_or_disabled_means_a_plain_single_read(self):
        run = runner_of(output(claude=entry(age=5000)))
        snap = quota.fresh_snapshot(self.pol, ['claude'], runner=run, clock=lambda: NOW)
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(snap['refresh']['outcome'], 'disabled')
        config = copy.deepcopy(CONFIG)
        del config['quota_policy']['usage_refresh']
        pol = quota.validate_policy(config['quota_policy'])
        self.assertFalse(pol['refresh']['enabled'])
        quota.fresh_snapshot(pol, ['claude'], runner=run, clock=lambda: NOW, refresher=self.refresher())
        self.assertEqual(self.refreshes, [])

    def test_failed_query_does_not_refresh(self):
        def run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, b'', b'')
        snap = quota.fresh_snapshot(self.pol, ['claude'], runner=run, clock=lambda: NOW, refresher=self.refresher())
        self.assertEqual(self.refreshes, [])
        self.assertEqual(snap['refresh']['outcome'], 'query_failed')

    def test_active_retry_after_blocks_the_request(self):
        # Orca's force flag skips its own retry-after gate, so this is the only thing honoring it.
        for key in ('claude', 'antigravity'):
            over = {key: entry(age=2000, usageMetadata=dict(retryAtMs=(NOW + 120) * 1000))}
            over.setdefault('claude', entry(age=2000))
            snap = self.fresh(['claude'], output(**over))
            self.assertEqual(self.refreshes, [])
            self.assertEqual(snap['refresh']['outcome'], 'retry_after_active')
            self.assertEqual(snap['refresh']['seconds_until_allowed'], 120)

    def test_expired_retry_after_allows_it(self):
        self.fresh(['claude'], output(claude=entry(age=2000, usageMetadata=dict(retryAtMs=(NOW - 1) * 1000))))
        self.assertEqual(self.refreshes, ['ok'])

    def test_codex_keeps_the_newer_of_orca_and_session_log_after_refresh(self):
        log = dict(timestamp=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(NOW - 10)), type='event_msg', payload=dict(type='token_count', rate_limits=dict(
            limit_id='codex', primary=dict(used_percent=42.0, window_minutes=300, resets_at=NOW + 6000),
            secondary=dict(used_percent=33.0, window_minutes=10080, resets_at=NOW + 100000))))
        root = self.dir / 'sessions' / 'x'
        root.mkdir(parents=True)
        (root / 'rollout-1.jsonl').write_text(json.dumps(log) + '\n')
        raw = json.loads(output(codex=entry(age=2000), claude=entry(age=2000)))
        raw['result']['codex'] = dict(activeAccountId=None)
        after = copy.deepcopy(raw)
        after['result']['rateLimits']['codex'] = entry(age=100)
        with patch.object(quota, 'codex_log_roots', return_value=[self.dir / 'sessions']):
            snap = self.fresh(['claude', 'codex'], json.dumps(raw), json.dumps(after))
        codex = snap['providers']['codex']
        self.assertEqual(codex['data_source'], quota.SOURCE_CODEX_LOG)
        self.assertEqual(codex['session']['usedPercent'], 42.0)

    def test_status_error_on_old_orca_data_is_retried_even_if_log_numbers_look_fresh(self):
        snap = dict(ok=True, providers=dict(codex=dict(status='error', updatedAt=(NOW - 5) * 1000,
                                                       orca_updated_at=(NOW - 2000) * 1000)))
        self.assertEqual(quota._stale_keys(snap, ['codex'], self.pol, NOW * 1000), ['codex'])
        # status=error 允许提前刷新，仍受限频和 retry-after 约束。
        snap['providers']['codex']['orca_updated_at'] = (NOW - 5) * 1000
        self.assertEqual(quota._stale_keys(snap, ['codex'], self.pol, NOW * 1000), ['codex'])


class RefreshDegrades(Case):
    """Every failure leaves the original judgment; nothing raises."""
    def test_timeout_and_errors_fall_back_silently(self):
        for outcome in ('timeout', 'unavailable', 'rpc_error', 'bad_response', 'error'):
            result = self.select('bugfix', reads=[output(claude=entry(age=2000))], refresher=self.refresher(outcome))
            self.assertEqual(result['selection']['blocked_code'], 'quota_unknown', outcome)
            self.assertEqual(result['source']['refresh']['outcome'], outcome)

    def test_raising_refresher_does_not_escape(self):
        def boom(pol, clock=None):
            raise RuntimeError('boom')
        result = self.select('bugfix', reads=[output(claude=entry(age=2000))], refresher=boom)
        self.assertEqual(result['selection']['blocked_code'], 'quota_unknown')
        self.assertEqual(result['source']['refresh']['outcome'], 'error')

    def test_429_keeps_the_previous_reading_and_does_not_worsen_other_providers(self):
        before = output(claude=entry(age=2000), codex=entry(age=10))
        retained = entry(age=2000, status='error', usageMetadata=dict(failureKind='rate-limited'))
        codex_err = entry(age=10, status='error', usageMetadata=dict(failureKind='server'))
        result = self.select('feature', reads=[before, output(claude=retained, codex=codex_err)],
                             refresher=self.refresher())
        info = result['source']['refresh']
        self.assertEqual(info['providers']['claude'],
                         dict(status='error', failure_kind='rate-limited', advanced=False))
        self.assertIn('codex', info['kept_previous'])
        # Codex stayed as good as it was before the refresh in this process.
        self.assertEqual(self.cand(result, 'sol')['status'], 'eligible')
        self.assertEqual(result['selection']['selected'], 'sol')

    def test_failure_that_wipes_windows_keeps_what_was_read(self):
        wiped = dict(status='error', updatedAt=NOW * 1000, session=None, weekly=None,
                     usageMetadata=dict(failureKind='server'))
        snap = quota.fresh_snapshot(self.pol, ['claude'], runner=runner_of(output(claude=entry(age=2000)), output(claude=wiped)),
                                    clock=lambda: NOW, refresher=self.refresher())
        kept = snap['providers']['claude']
        self.assertEqual(kept['updatedAt'], (NOW - 2000) * 1000)
        self.assertEqual(kept['refresh_status'], 'error')
        # Kept, but still judged as stale by the ordinary rules.
        probe = quota.evaluate_candidate('opus_high', self.config['models']['opus_high'], 'primary', self.pol, snap,
                                         self.pol['budgets']['normal'], NOW * 1000)
        self.assertEqual(probe['status'], 'unknown')

    def test_unreadable_second_read_keeps_the_first(self):
        good = output(claude=entry(age=2000)).encode()
        answers = [subprocess.CompletedProcess([], 0, good, b''), subprocess.CompletedProcess([], 1, b'', b'')]
        snap = quota.fresh_snapshot(self.pol, ['claude'], runner=lambda c, **k: answers.pop(0) if len(answers) > 1 else answers[0],
                                    clock=lambda: NOW, refresher=self.refresher())
        self.assertTrue(snap['ok'])
        self.assertTrue(snap['refresh']['reread_failed'])


class Fetching(Case):
    def fresh(self, *reads, refresher=None):
        run = runner_of(*reads)
        return quota.fresh_snapshot(self.pol, ['claude'], runner=run, clock=lambda: NOW,
                                    refresher=refresher or self.refresher(), sleep=lambda s: None), run

    def test_fetching_waits_for_the_answer_without_refreshing(self):
        snap, run = self.fresh(output(claude=entry(status='fetching')), output(claude=entry()))
        self.assertEqual(snap['providers']['claude']['status'], 'ok')
        self.assertEqual(self.refreshes, [])
        self.assertEqual(len(run.calls), 2)

    def test_fetching_never_blocks_a_plan_once_answered(self):
        reads = [output(claude=entry(status='fetching')), output(claude=entry())]
        with patch.object(quota.time, 'sleep'):
            result = self.select('bugfix', reads=reads, refresher=self.refresher())
        self.assertEqual(result['selection']['selected'], 'opus_high')

    def test_fetching_forever_is_bounded_and_ends_in_the_old_rule(self):
        self.pol['refresh']['fetching_wait'] = 0.05
        begun = time.monotonic()
        snap, run = self.fresh(output(claude=entry(status='fetching')))
        self.assertLess(time.monotonic() - begun, 2)
        self.assertEqual(snap['providers']['claude']['status'], 'fetching')
        probe = quota.evaluate_candidate('opus_high', self.config['models']['opus_high'], 'primary', self.pol, snap,
                                         self.pol['budgets']['normal'], NOW * 1000)
        self.assertEqual(probe['status'], 'unknown')
        self.assertEqual(self.refreshes, [])

    def test_fetching_status_with_no_data_is_not_stale(self):
        snap = dict(ok=True, providers=dict(claude=dict(status='fetching', updatedAt=0)))
        self.assertEqual(quota._stale_keys(snap, ['claude'], self.pol, NOW * 1000), [])


class FakeRuntime:
    """An Orca-shaped unix socket and runtime file; records every request it receives."""
    def __init__(self, data_dir, behavior='ok', delay=0.0, on_call=None):
        self.requests, self.behavior, self.delay, self.on_call = [], behavior, delay, on_call
        # AF_UNIX paths are short on macOS, so the socket gets its own short directory.
        self.home = tempfile.mkdtemp(prefix='orq', dir='/tmp')
        self.path = os.path.join(self.home, 's.sock')
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(16)
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        (Path(data_dir) / 'orca-runtime.json').write_text(json.dumps(dict(
            runtimeId='r', authToken=TOKEN, transports=[dict(kind='unix', endpoint=self.path),
                                                        dict(kind='websocket', endpoint='ws://0.0.0.0:1')])))
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        with conn:
            buffer = b''
            while b'\n' not in buffer:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer += chunk
            request = json.loads(buffer.split(b'\n')[0])
            self.requests.append(request)
            if self.on_call:
                self.on_call()
            if self.behavior == 'hang':
                time.sleep(3)
                return
            time.sleep(self.delay)
            frames = [dict(_keepalive=True)]
            if self.behavior == 'ok':
                frames.append(dict(id=request['id'], ok=True, result=dict(claude=dict(email='x@example.test')), _meta=dict(runtimeId='r')))
            elif self.behavior == 'denied':
                # Even a reply echoing the token must not reach any output.
                frames.append(dict(id=request['id'], ok=False, error=dict(code='forbidden', message='bad token ' + TOKEN)))
            elif self.behavior == 'wrong_id':
                frames.append(dict(id='other', ok=True, result={}))
            elif self.behavior == 'garbage':
                conn.sendall(b'not json\n')
                return
            elif self.behavior == 'close':
                return
            conn.sendall(b''.join(json.dumps(f).encode() + b'\n' for f in frames))

    def close(self):
        self.server.close()
        shutil.rmtree(self.home, ignore_errors=True)


class Rpc(Case):
    def runtime(self, **kw):
        fake = FakeRuntime(self.dir / 'orca-data', **kw)
        self.addCleanup(fake.close)
        return fake

    def test_request_uses_the_unix_transport_and_refresh_param(self):
        fake = self.runtime()
        self.assertEqual(quota._call_accounts_refresh(2), 'ok')
        request = fake.requests[0]
        self.assertEqual((request['method'], request['params'], request['authToken']),
                         ('accounts.list', dict(refreshUsage=True), TOKEN))

    def test_bad_replies_are_classified(self):
        for behavior, expected in (('denied', 'rpc_error'), ('wrong_id', 'bad_response'),
                                   ('garbage', 'bad_response'), ('close', 'unavailable')):
            fake = self.runtime(behavior=behavior)
            self.assertEqual(quota._call_accounts_refresh(2), expected, behavior)
            fake.close()

    def test_timeout_is_hard(self):
        self.runtime(behavior='hang')
        begun = time.monotonic()
        self.assertEqual(quota._call_accounts_refresh(0.2), 'timeout')
        self.assertLess(time.monotonic() - begun, 1)

    def test_endless_keepalives_cannot_outlive_the_deadline(self):
        fake = self.runtime()
        def flood(conn):
            with conn:
                try:
                    while True:
                        conn.sendall(b'{"_keepalive":true}\n')
                        time.sleep(0.01)
                except OSError:
                    pass
        fake.handle = flood
        begun = time.monotonic()
        self.assertEqual(quota._call_accounts_refresh(0.2), 'timeout')
        self.assertLess(time.monotonic() - begun, 1)

    def test_missing_or_broken_runtime_file(self):
        self.assertEqual(quota._call_accounts_refresh(1), 'unavailable')
        data = self.dir / 'orca-data'
        data.mkdir()
        (data / 'orca-runtime.json').write_text('{oops')
        self.assertEqual(quota._call_accounts_refresh(1), 'bad_response')
        (data / 'orca-runtime.json').write_text(json.dumps(dict(authToken=TOKEN, transports=[dict(kind='websocket', endpoint='x')])))
        self.assertEqual(quota._call_accounts_refresh(1), 'unavailable')

    def test_token_never_reaches_state_or_results(self):
        for behavior in ('denied', 'garbage', 'wrong_id', 'hang'):
            fake = self.runtime(behavior=behavior)
            self.pol['refresh'].update(min_interval=0, timeout=0.3)
            result = quota.refresh_usage_rpc(self.pol)
            self.assertNotIn(TOKEN, json.dumps(result))
            for path in (self.dir / 'state').iterdir():
                self.assertNotIn(TOKEN, path.read_text(errors='replace'))
            fake.close()

    def test_token_absent_from_outputs_state_and_process_args(self):
        scripts = self.dir / 'skill' / 'scripts'
        scripts.mkdir(parents=True)
        for name in ('quota.py', 'quota_guard.py', 'route.py', 'jev_route.py'):
            shutil.copy(ROOT / 'scripts' / name, scripts / name)
        (self.dir / 'skill' / 'routes.json').write_text(json.dumps(CONFIG))
        seen = []
        def sample():
            ps = subprocess.run(['ps', '-axww', '-o', 'command='], capture_output=True, text=True)
            seen.append(ps.stdout)
        self.runtime(on_call=sample, behavior='denied')
        (self.dir / 'state').mkdir()
        env = dict(os.environ, HOME=str(self.dir), PYTHONDONTWRITEBYTECODE='1')
        code = ("import sys,json,subprocess;sys.path.insert(0,sys.argv[1]);import quota;"
                "pol=quota.validate_policy(json.load(open(sys.argv[2]))['quota_policy']);"
                "out=json.dumps(dict(result=dict(rateLimits=dict(claude=dict(status='ok',updatedAt=1)))));"
                "run=lambda c,**k:subprocess.CompletedProcess(c,0,out.encode(),b'');"
                "print(json.dumps(quota.fresh_snapshot(pol,['claude'],runner=run,refresher=quota.refresh_usage_rpc)))")
        proc = subprocess.run([sys.executable, '-c', code, str(scripts), str(self.dir / 'skill' / 'routes.json')],
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('rpc_error', proc.stdout)
        self.assertNotIn(TOKEN, proc.stdout + proc.stderr)
        self.assertTrue(seen)
        self.assertNotIn(TOKEN, seen[0])
        for path in (self.dir / 'state').rglob('*'):
            if path.is_file():
                self.assertNotIn(TOKEN, path.read_text(errors='replace'))


class RateLimit(Case):
    def call(self, clock, **kw):
        self.calls = getattr(self, 'calls', [])
        def fake(timeout):
            self.calls.append(timeout)
            return kw.get('outcome', 'ok')
        return quota.refresh_usage_rpc(self.pol, clock=lambda: clock, call=fake)

    def test_second_call_inside_the_interval_sends_nothing(self):
        self.assertEqual(self.call(NOW)['outcome'], 'ok')
        result = self.call(NOW + 299)
        self.assertEqual((result['outcome'], result['attempted']), ('rate_limited', False))
        self.assertEqual(result['seconds_until_allowed'], 1)
        self.assertEqual(result['last_outcome'], 'ok')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.call(NOW + 300)['outcome'], 'ok')
        self.assertEqual(len(self.calls), 2)

    def test_a_failure_still_uses_the_slot(self):
        self.assertEqual(self.call(NOW, outcome='timeout')['outcome'], 'timeout')
        self.assertEqual(self.call(NOW + 10)['outcome'], 'rate_limited')
        self.assertEqual(len(self.calls), 1)

    def test_a_raising_call_is_contained_and_still_uses_the_slot(self):
        def boom(timeout):
            raise ValueError('secret detail')
        result = quota.refresh_usage_rpc(self.pol, clock=lambda: NOW, call=boom)
        self.assertEqual(result, dict(attempted=True, outcome='error'))
        self.assertEqual(self.call(NOW + 1)['outcome'], 'rate_limited')

    def test_a_crash_after_the_claim_still_spaces_the_next_attempt(self):
        state = self.dir / 'state'
        state.mkdir()
        (state / quota.REFRESH_STATE).write_text(json.dumps(dict(last_attempt_at=NOW, last_outcome='in_flight')))
        self.assertEqual(self.call(NOW + 5)['outcome'], 'rate_limited')

    def test_clock_going_backwards_or_broken_state_does_not_wedge(self):
        state = self.dir / 'state'
        state.mkdir()
        (state / quota.REFRESH_STATE).write_text(json.dumps(dict(last_attempt_at=NOW + 99999)))
        self.assertEqual(self.call(NOW)['outcome'], 'ok')
        (state / quota.REFRESH_STATE).write_text('{broken')
        self.assertEqual(self.call(NOW + 1)['outcome'], 'ok')

    def test_unwritable_state_sends_nothing(self):
        with patch.object(quota, '_write_state', side_effect=OSError('read-only')):
            result = self.call(NOW)
        self.assertEqual(result, dict(attempted=False, outcome='state_error'))
        self.assertEqual(self.calls if hasattr(self, 'calls') else [], [])

    def test_holder_blocks_with_a_bound(self):
        self.pol['refresh']['timeout'] = 0.2
        (self.dir / 'state').mkdir()
        fd = os.open(self.dir / 'state' / quota.REFRESH_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        begun = time.monotonic()
        self.assertEqual(self.call(NOW)['outcome'], 'busy')
        self.assertLess(time.monotonic() - begun, 1)

    def test_state_file_is_private_and_holds_no_more_than_timing(self):
        self.call(NOW)
        path = self.dir / 'state' / quota.REFRESH_STATE
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(json.loads(path.read_text())), {'last_attempt_at', 'last_outcome'})

    def test_concurrent_processes_send_one_request(self):
        fake = FakeRuntime(self.dir / 'orca-data', delay=0.4)
        self.addCleanup(fake.close)
        code = ("import json,sys;sys.path.insert(0,sys.argv[1]);import quota;"
                "pol=quota.validate_policy(json.load(open(sys.argv[2]))['quota_policy']);"
                "print(json.dumps(quota.refresh_usage_rpc(pol)))")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        procs = [subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'scripts'), str(hermetic.CONFIG_PATH)],
                                  env=env, stdout=subprocess.PIPE, text=True) for _ in range(8)]
        results = [json.loads(p.communicate(timeout=30)[0]) for p in procs]
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(sorted(r['outcome'] for r in results), ['ok'] + ['rate_limited'] * 7)


class Takeover(Case):
    """opus_high under feature: Claude as a guard's target, with a clock at NOW."""
    SESSION_RESET = NOW + 3000   # 50 minutes: prorated reserve 10*50/300 = 1.6667
    WEEKLY_RESET = NOW + 100000  # prorated reserve 5*1666.67/10080 = 0.8267

    def run_it(self, phase='start', now=NOW, reads=None, **kw):
        kw.setdefault('session_reset', self.SESSION_RESET)
        kw.setdefault('weekly_reset', self.WEEKLY_RESET)
        reads = reads or [output(claude=entry(**kw))]
        result = self.select('feature', explicit='opus_high', reads=reads, takeover=phase, now=now)
        return result, self.cand(result, 'opus_high')

    def test_just_enough_passes(self):
        # 30 minutes at 1.5/min: remaining 48.6667 - 45 - reserve 1.6667 = budget 2.
        result, cand = self.run_it(age=1800, session=51.3333, weekly=10)
        self.assertEqual(cand['status'], 'eligible')
        self.assertEqual(result['selection']['selected'], 'opus_high')
        self.assertEqual(cand['stale_takeover']['age_seconds'], 1800)
        self.assertEqual(cand['stale_takeover']['charges']['session']['deducted_pp'], 45)
        self.assertAlmostEqual(cand['windows']['session']['available_pp'], 2.0, places=3)
        self.assertEqual(cand['reasons'], [])

    def test_just_short_on_session_fails_closed(self):
        result, cand = self.run_it(age=1800, session=51.34, weekly=10)
        self.assertEqual(cand['status'], 'unknown')
        self.assertIn('session 保守剩余', cand['stale_takeover_refused'])
        self.assertIsNone(result['selection']['selected'])
        self.assertEqual(result['selection']['blocked_code'], 'quota_unknown')

    def test_weekly_boundary(self):
        # 30 minutes at 0.25/min: remaining - 7.5 - reserve 0.8267 >= budget 5.
        self.assertEqual(self.run_it(age=1800, session=10, weekly=86.67)[1]['status'], 'eligible')
        cand = self.run_it(age=1800, session=10, weekly=86.68)[1]
        self.assertEqual(cand['status'], 'unknown')
        self.assertIn('weekly 保守剩余', cand['stale_takeover_refused'])

    def test_age_limit_is_inclusive_and_hard(self):
        self.assertEqual(self.run_it(age=3600, session=2, weekly=2)[1]['status'], 'eligible')
        cand = self.run_it(age=3601, session=2, weekly=2)[1]
        self.assertEqual(cand['status'], 'unknown')
        self.assertIn('超过上限', cand['stale_takeover_refused'])

    def test_passed_reset_refuses(self):
        cand = self.run_it(age=1800, session_reset=NOW - 1)[1]
        self.assertEqual(cand['status'], 'unknown')
        self.assertIn('resetsAt 已过去', cand['stale_takeover_refused'])
        cand = self.run_it(age=1800, weekly_reset=NOW - 1)[1]
        self.assertEqual(cand['status'], 'unknown')

    def test_missing_data_refuses(self):
        for field in ('session', 'weekly'):
            claude = entry(age=1800)
            claude[field] = None
            _, cand = self.run_it(reads=[output(claude=claude)])
            self.assertEqual(cand['status'], 'unknown', field)
            self.assertIn('没有可用的历史读数', cand['stale_takeover_refused'])
        raw = json.loads(output())
        del raw['result']['rateLimits']['claude']
        _, cand = self.run_it(reads=[json.dumps(raw)])
        self.assertEqual(cand['status'], 'unknown')

    def test_future_timestamp_refuses(self):
        cand = self.run_it(age=-120)[1]
        self.assertEqual(cand['status'], 'unknown')

    def test_unavailable_and_fetching_refuse(self):
        for status in ('unavailable', 'fetching', 'idle'):
            cand = self.run_it(reads=[output(claude=entry(age=1800, status=status))])[1]
            self.assertEqual(cand['status'], 'unknown', status)
            self.assertIn('不能接手', cand['stale_takeover_refused'])

    def test_rate_limited_error_with_retained_windows_is_accepted(self):
        claude = entry(age=1800, status='error', usageMetadata=dict(failureKind='rate-limited'), session_reset=NOW + 3000)
        cand = self.run_it(reads=[output(claude=claude)])[1]
        self.assertEqual(cand['status'], 'eligible')
        self.assertEqual(cand['data_source'], quota.SOURCE_ORCA)

    def test_sign_in_problems_stay_closed(self):
        for kind in ('missing-credentials', 'stale-token', 'missing-scope', 'no-subscription', 'delegated-refresh-required', None):
            meta = dict(failureKind=kind) if kind else {}
            claude = entry(age=1800, status='error', usageMetadata=meta, session_reset=NOW + 3000)
            cand = self.run_it(reads=[output(claude=claude)])[1]
            self.assertEqual(cand['status'], 'unknown', kind)

    def wiped(self, kind='server', account='acct-1'):
        claude = dict(status='error', updatedAt=NOW * 1000, session=None, weekly=None,
                      usageMetadata=dict(failureKind=kind))
        return output(claude=claude, account=account)

    def remember(self, account='acct-1', age=1800):
        snap = quota.fetch_snapshot(self.pol, runner=runner_of(output(claude=entry(age=age, session_reset=self.SESSION_RESET), account=account)),
                                    clock=lambda: NOW)
        quota.remember_good_reading(self.pol, snap)

    def test_last_good_reading_covers_a_failure_that_wiped_orca(self):
        self.remember()
        cand = self.run_it(reads=[self.wiped()])[1]
        self.assertEqual(cand['status'], 'eligible')
        self.assertEqual(cand['data_source'], 'last_good_reading')
        self.assertEqual(cand['stale_takeover']['source'], 'last_good_reading')

    def test_last_good_reading_of_another_account_is_ignored(self):
        self.remember(account='acct-1')
        cand = self.run_it(reads=[self.wiped(account='acct-2')])[1]
        self.assertEqual(cand['status'], 'unknown')

    def test_last_good_reading_does_not_override_a_sign_in_problem(self):
        self.remember()
        cand = self.run_it(reads=[self.wiped(kind='missing-credentials')])[1]
        self.assertEqual(cand['status'], 'unknown')

    def test_last_good_reading_is_held_to_the_same_limits(self):
        self.remember(age=3601)
        self.assertEqual(self.run_it(reads=[self.wiped()])[1]['status'], 'unknown')

    def test_a_refresh_failure_in_the_same_process_still_counts_its_failure_kind(self):
        before = output(claude=entry(age=1800, session_reset=self.SESSION_RESET))
        result = self.select('feature', explicit='opus_high', takeover='start', refresher=self.refresher(),
                             reads=[before, self.wiped(kind='missing-credentials')])
        self.assertEqual(self.cand(result, 'opus_high')['status'], 'unknown')
        result = self.select('feature', explicit='opus_high', takeover='start', refresher=self.refresher(),
                             reads=[before, self.wiped(kind='server')])
        self.assertEqual(self.cand(result, 'opus_high')['status'], 'eligible')

    def test_other_agents_and_disabled_config_stay_closed(self):
        result = self.select('bugfix', explicit='sol', takeover='start', reads=[output(codex=entry(age=1800))])
        self.assertNotEqual(self.cand(result, 'sol').get('status'), 'eligible')
        self.assertNotIn('stale_takeover', self.cand(result, 'sol'))
        self.config['quota_policy']['guard_stale_takeover']['enabled'] = False
        cand = self.run_it(age=1800)[1]
        self.assertEqual(cand['status'], 'unknown')
        self.assertNotIn('stale_takeover', cand)
        del self.config['quota_policy']['guard_stale_takeover']
        self.assertEqual(self.run_it(age=1800)[1]['status'], 'unknown')

    def test_fresh_data_ignores_the_switch(self):
        cand = self.run_it(age=5)[1]
        self.assertEqual(cand['status'], 'eligible')
        self.assertNotIn('stale_takeover', cand)

    def test_budget_multiplier_and_cap_window_count(self):
        fable = entry(age=1800, session=10, weekly=10, session_reset=self.SESSION_RESET,
                      fableWeekly=window(99.9, 10080, self.WEEKLY_RESET))
        result = self.select('complex', explicit='fable_high', reads=[output(claude=fable)], takeover='start')
        self.assertEqual(self.cand(result, 'fable_high')['status'], 'unknown')
        self.assertIn('fableWeekly', self.cand(result, 'fable_high')['stale_takeover_refused'])

    def test_invalid_config_is_rejected(self):
        for bad in (dict(max_age_minutes=-1), dict(conservative_burn_pp_per_min=dict(session=0, weekly=1)),
                    dict(conservative_burn_pp_per_min=dict(session=1)), dict(conservative_burn_pp_per_min='fast')):
            config = copy.deepcopy(CONFIG)
            config['quota_policy']['guard_stale_takeover'].update(bad)
            with self.assertRaises(quota.PolicyError, msg=str(bad)):
                quota.validate_policy(config['quota_policy'])


class PlanAndStartAgree(Takeover):
    """Same function either way; a plan charges the handoff still ahead of it."""
    def test_same_clock_without_margin_same_verdict(self):
        self.config['quota_policy']['guard_stale_takeover']['plan_margin_minutes'] = 0
        for used, expected in ((51.3333, 'eligible'), (51.34, 'unknown')):
            plan = self.run_it('plan', age=1800, session=used, weekly=10)[1]
            start = self.run_it('start', age=1800, session=used, weekly=10)[1]
            self.assertEqual((plan['status'], start['status']), (expected, expected))
            self.assertEqual(plan.get('stale_takeover'), start.get('stale_takeover'))

    def test_a_passing_plan_is_not_refused_when_start_comes_after_the_margin(self):
        margin = CONFIG['quota_policy']['guard_stale_takeover']['plan_margin_minutes'] * 60
        # Just enough for the plan's own charge of age + margin.
        used = 100 - (1.5 * (1800 + margin) / 60 + 1.6667 + 2)
        plan = self.run_it('plan', age=1800, session=used, weekly=10)[1]
        self.assertEqual(plan['status'], 'eligible')
        reads = [output(claude=entry(age=1800, session=used, weekly=10, session_reset=self.SESSION_RESET,
                                     weekly_reset=self.WEEKLY_RESET))]
        start = self.run_it('start', now=NOW + margin, reads=reads)[1]
        self.assertEqual(start['status'], 'eligible')

    def test_plan_is_the_stricter_of_the_two(self):
        plan = self.run_it('plan', age=1800, session=51.3333, weekly=10)[1]
        start = self.run_it('start', age=1800, session=51.3333, weekly=10)[1]
        self.assertEqual((plan['status'], start['status']), ('unknown', 'eligible'))
        self.assertEqual(self.run_it('plan', age=3301, session=2, weekly=2)[1]['status'], 'unknown')


def route_main(argv, reads, config=None, refresher=None, clock=NOW, register=False):
    """route.main with injected reads; returns (exit code, stdout json or None, stderr, worker-start calls)."""
    run, real_fetch = runner_of(*reads), quota.fetch_snapshot
    fake_fetch = lambda pol, runner=None, clock=None, log_roots=None: real_fetch(pol, runner=run, clock=clock)
    starts = []
    def fake_run(cmd, **kw):
        starts.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(dict(ok=True, result=dict(dispatchId='ctx_new', taskId='task_new'))))
    out, err = io.StringIO(), io.StringIO()
    text = json.dumps(config or CONFIG)
    real_read = Path.read_text
    def read(path, *a, **k):
        return text if path.name == 'routes.json' else real_read(path, *a, **k)
    with patch.object(sys, 'argv', ['route.py'] + list(argv)), patch.object(route.Path, 'read_text', autospec=True, side_effect=read), \
            patch.object(quota, 'fetch_snapshot', fake_fetch), patch.object(quota.time, 'time', return_value=clock), \
            patch.object(quota, 'refresh_usage_rpc', refresher or (lambda pol, clock=None: dict(attempted=False, outcome='rate_limited'))), \
            patch.object(route.subprocess, 'run', fake_run), patch.object(route, 'log_start'), \
            patch.object(qg, 'prepare_context', return_value={}), \
            patch.object(qg, 'register_start', return_value=dict(started=False, reason='test')), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = route.main()
        except SystemExit as exc:
            code = exc.code
    try:
        parsed = json.loads(out.getvalue())
    except ValueError:
        parsed = None
    return code, parsed, err.getvalue(), [c for c in starts if 'worker-start' in c]


class OrdinaryPathsStayStrict(Case):
    STALE = [output(claude=entry(age=1800, session_reset=NOW + 3000))]

    def setUp(self):
        super().setUp()
        self.state = self.dir / 'guard'
        self.state.mkdir()
        (self.state / 'ctx_g.json').write_text(json.dumps(dict(
            dispatch_id='ctx_g', task_id='task_g', run_id='run_g', kind='feature', complexity='normal', agent='codex')))
        patcher = patch.object(qg, 'STATE', self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.spec = self.dir / 'spec.md'
        self.spec.write_text('任务')

    def plan(self, *extra, reads=None, guard_pid=None):
        guards = [dict(dispatch_id='ctx_g', pid=guard_pid)] if guard_pid else []
        with patch.object(qg, 'active_guards', return_value=guards):
            return route_main(['plan', '--kind', 'feature', '--complexity', 'normal'] + list(extra), reads or self.STALE)

    def start(self, *extra, guard_pid=None):
        guards = [dict(dispatch_id='ctx_g', pid=guard_pid)] if guard_pid else []
        with patch.object(qg, 'active_guards', return_value=guards), \
             patch.object(qg, 'retry_refused_by_guard', return_value='rejected-by-guard-check'):
            return route_main(['start', '--kind', 'feature', '--complexity', 'normal', '--run', 'run_g',
                               '--spec-file', str(self.spec)] + list(extra), self.STALE)

    def test_ordinary_plan_and_start_on_the_same_data_still_block(self):
        code, out, _, _ = self.plan('--profile', 'opus_high')
        self.assertEqual((code, out['launchable'], out['plan']['blocked_code']), (2, False, 'quota_unknown'))
        self.assertNotIn('stale_takeover', out['quota']['candidates'][0])
        code, out, _, starts = self.start('--profile', 'opus_high')
        self.assertEqual((code, out['launched'], starts), (2, False, []))

    def test_ordinary_task_with_reserve_holding_primary_still_blocks(self):
        code, out, _, _ = route_main(['plan', '--kind', 'bugfix', '--complexity', 'normal'], self.STALE)
        self.assertEqual((code, out['plan']['blocked_code']), (2, 'quota_unknown'))

    def test_flag_from_something_other_than_the_guard_is_refused(self):
        for pid in (None, os.getppid() + 12345):
            code, out, _, _ = self.plan('--profile', 'opus_high', '--quota-guard-takeover', 'ctx_g', guard_pid=pid)
            self.assertEqual((code, out['launchable']), (2, False))
            self.assertIn('存活守卫', out['blocked_reason'])

    def test_flag_for_an_unknown_dispatch_is_refused(self):
        code, out, _, _ = self.plan('--profile', 'opus_high', '--quota-guard-takeover', 'ctx_nobody', guard_pid=os.getppid())
        self.assertEqual(code, 2)
        self.assertIn('没有', out['blocked_reason'])
        code, out, _, _ = self.plan('--profile', 'opus_high', '--quota-guard-takeover', '../../etc', guard_pid=os.getppid())
        self.assertEqual(code, 2)

    def test_flag_with_a_mismatched_task_or_pool_is_refused(self):
        me = os.getppid()
        for argv, word in ((['--kind', 'bugfix', '--profile', 'opus_high'], '不符'),
                           (['--kind', 'feature', '--profile', 'sol'], '其他账户池'),
                           (['--kind', 'feature'], '必须显式指定')):
            with patch.object(qg, 'active_guards', return_value=[dict(dispatch_id='ctx_g', pid=me)]):
                code, out, _, _ = route_main(['plan', '--complexity', 'normal', '--quota-guard-takeover', 'ctx_g'] + argv, self.STALE)
            self.assertEqual(code, 2, argv)
            self.assertIn(word, out['blocked_reason'])

    def test_start_flag_requires_a_retry_of_that_dispatch(self):
        code, out, _, starts = self.start('--profile', 'opus_high', '--quota-guard-takeover', 'ctx_g', guard_pid=os.getppid())
        self.assertEqual((code, starts), (2, []))
        self.assertIn('重试', out['blocked_reason'])

    def test_real_parent_guard_plan_passes(self):
        code, out, _, _ = self.plan('--profile', 'opus_high', '--quota-guard-takeover', 'ctx_g', guard_pid=os.getppid())
        self.assertEqual((code, out['launchable'], out['plan']['profile']), (0, True, 'opus_high'))
        self.assertEqual(out['plan']['stale_takeover']['age_seconds'], 1800)
        self.assertEqual(out['quota']['candidates'][0]['stale_takeover']['age_seconds'], 1800)
        self.assertFalse(out['plan']['fast_mode']['apply'])
        self.assertTrue(any('过期但有界' in w for w in out['plan']['warnings']))

    def test_argparse_does_not_accept_the_flag_by_a_prefix_without_checks(self):
        code, out, _, _ = self.plan('--profile', 'opus_high', '--quota-guard', 'ctx_g')
        self.assertEqual(code, 2)
        self.assertIn('存活守卫', out['blocked_reason'])

    def test_usage_log_records_the_stale_data_and_its_age(self):
        report = dict(candidates=[dict(profile='opus_high', windows={}, caps={}, stale_takeover=dict(age_seconds=1800, age_minutes=30))])
        plan = dict(profile='opus_high', kind='feature', complexity='normal', agent='claude', model='m', effort='high')
        log = self.dir / 'usage.jsonl'
        with patch.object(route, 'USAGE_LOG', log):
            route.log_start(plan, report, dict(result=dict(dispatchId='d', taskId='t')), None)
            route.log_start(plan, dict(candidates=[dict(profile='opus_high', windows={}, caps={})]), dict(result=dict(dispatchId='e', taskId='t')), None)
        first, second = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(first['stale_takeover']['age_seconds'], 1800)
        self.assertNotIn('stale_takeover', second)


class GuardBackend(Case):
    def backend(self, stdout, code=2):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, code, stdout, '')
        backend = qg.Backend(CONFIG, dict(t.META), self.dir, runner=run)
        return backend, calls

    def test_plan_passes_the_switch_and_start_too(self):
        backend, calls = self.backend(json.dumps(dict(launchable=False)))
        self.assertIsNone(backend.plan())
        self.assertIn('--quota-guard-takeover', calls[0])
        self.assertEqual(calls[0][calls[0].index('--quota-guard-takeover') + 1], 'ctx_old')
        backend.start('opus_high', 'x')
        self.assertEqual(calls[-1][calls[-1].index('--quota-guard-takeover') + 1], 'ctx_old')

    def test_dry_run_previews_ordinary_rules(self):
        backend, calls = self.backend(json.dumps(dict(launchable=False)))
        self.assertIsNone(backend.plan(takeover=False))
        self.assertNotIn('--quota-guard-takeover', calls[0])
        self.assertIn('--preview', calls[0])

    def test_blocked_retry_command_given_to_the_coordinator_has_no_switch(self):
        source = (ROOT / 'scripts' / 'quota_guard.py').read_text()
        block = source[source.index("state = '交接文件缺失'"):source.index('if marker_error:')]
        self.assertIn("'route.py'", block)
        self.assertNotIn('--quota-guard-takeover', block)

    def test_detail_says_how_stale_and_what_refresh_did(self):
        result = dict(launchable=False, blocked_reason='x', quota=dict(
            usage_refresh=dict(attempted=False, outcome='rate_limited', seconds_until_allowed=120),
            candidates=[dict(profile='opus_high', status='unknown', reasons=['claude 额度数据过期（2410s > 900s）'])]))
        backend, _ = self.backend(json.dumps(result))
        self.assertIsNone(backend.plan())
        self.assertIn('2410s', backend.plan_detail)
        self.assertIn('限频中', backend.plan_detail)
        self.assertIn('120 秒后', backend.plan_detail)

    def test_unparseable_output_is_a_no_target_not_a_crash(self):
        backend, _ = self.backend('not json', code=1)
        self.assertIsNone(backend.plan())
        self.assertIn('不可派发', backend.plan_detail)

    def test_describe_refresh_names_failed_providers(self):
        text = quota.describe_refresh(dict(outcome='ok', providers=dict(claude=dict(advanced=False, status='error', failure_kind='rate-limited'))))
        self.assertIn('claude 未更新', text)
        self.assertIn('rate-limited', text)
        self.assertEqual(quota.describe_refresh(dict(outcome='ok', providers=dict(claude=dict(advanced=True)))), '已请求刷新')


class NoTargetThenRecover(t.GuardCase):
    def setUp(self):
        super().setUp()
        self.backend.plan_detail = 'opus_high：claude 额度数据过期（4000s > 900s）；刷新结果：超时（Orca 可能仍在刷新）'

    def test_alerts_once_keeps_worker_then_hands_off_when_target_returns(self):
        self.backend.target = None
        low = t.snapshot(used=95)
        self.assertEqual(self.guard.tick(low), 'continue')
        self.assertEqual(self.guard.tick(low), 'continue')
        self.assertEqual(self.guard.tick(low), 'continue')
        alerts = [b for to, b, high in self.backend.sent if high]
        self.assertEqual(len(alerts), 1)
        self.assertIn('无其他账户池的可派发目标', alerts[0])
        self.assertIn('4000s', alerts[0])
        self.assertIn('刷新结果', alerts[0])
        self.assertIn('下一轮轮询', alerts[0])
        self.assertAbsent('stop', 'fail_task', 'start')
        self.backend.target = 'opus'
        self.assertEqual(self.guard.tick(low), 'handoff')
        self.assertIn('start', self.backend.events)
        self.assertEqual(self.backend.events.count('quota_handoff'), 1)

    def test_handoff_notice_and_event_name_the_stale_data(self):
        self.backend.target_stale = dict(age_seconds=1800, age_minutes=30.0)
        self.assertEqual(self.guard.tick(t.snapshot(used=95)), 'handoff')
        self.assertEqual(self.backend.recorded['target_stale_data']['age_minutes'], 30.0)
        self.assertIn('过期的额度数据（年龄 30.0 分钟）', self.backend.bodies('run:')[-1])

    def test_fresh_target_message_does_not_mention_stale(self):
        self.assertEqual(self.guard.tick(t.snapshot(used=95)), 'handoff')
        self.assertNotIn('target_stale_data', self.backend.recorded)
        self.assertNotIn('过期的额度数据', self.backend.bodies('run:')[-1])


class RealGuardProcess(unittest.TestCase):
    """Real guard, real route.py children, fake orca whose Claude age comes from the db."""
    FAKE = t.RealWiring.FAKE.replace(
        "    print(json.dumps(dict(result=dict(rateLimits=dict(codex=entry(95), claude=entry(10), antigravity=entry(10))))))\n",
        "    rate = dict(codex=entry(95), claude=entry(10), antigravity=entry(10))\n"
        "    rate['claude']['updatedAt'] = now - db.get('claude_age_s', 0) * 1000\n"
        "    rate['codex']['updatedAt'] = now - db.get('codex_age_s', 0) * 1000\n"
        "    print(json.dumps(dict(result=dict(rateLimits=rate, claude=dict(activeAccountId='acct')))))\n").replace(
        "    if db.get('report') and '请求交接' in opt('--body'):\n",
        "    if db.get('recover_on_alert') and '无其他账户池' in opt('--body'):\n"
        "        db['claude_age_s'] = 0\n"
        "    if db.get('report') and '请求交接' in opt('--body'):\n")
    setUp = t.RealWiring.setUp
    write_db = t.RealWiring.write_db
    change_db = t.RealWiring.change_db
    report = t.RealWiring.report
    run_guard = t.RealWiring.run_guard
    called = t.RealWiring.called
    bodies = t.RealWiring.bodies

    def isolate(self, poll=None):
        self.env['ORCA_ROUTING_STATE_DIR'] = str(self.dir / 'routing-state')
        self.env['ORCA_USER_DATA_PATH'] = str(self.dir / 'orca-data')
        if poll:
            self.config['quota_policy']['quota_guard']['poll_seconds'] = poll
            (self.dir / 'skill' / 'routes.json').write_text(json.dumps(self.config))

    def test_no_target_alerts_then_next_poll_hands_off(self):
        self.isolate()
        self.change_db(report=self.report(), claude_age_s=7200, recover_on_alert=True)
        db = self.run_guard(0)
        self.assertEqual(self.phase['phase'], 'handoff_done')
        self.assertEqual(self.phase['new_dispatch'], 'ctx_new')
        alerts = [b for b in self.bodies() if '无其他账户池' in b]
        self.assertEqual(len(alerts), 1)
        self.assertRegex(alerts[0], r'额度数据过期（7\d\d\ds > 900s）')
        self.assertIn('刷新结果：连不上 Orca', alerts[0])
        self.assertEqual(len(self.called('worker-start')), 1)
        self.assertEqual(db['task']['dispatch_id'], 'ctx_new')

    def test_bounded_stale_takeover_completes_and_is_recorded(self):
        self.isolate()
        self.change_db(report=self.report(), claude_age_s=1800)
        db = self.run_guard(0)
        self.assertEqual(self.phase['phase'], 'handoff_done')
        self.assertEqual(db['task']['dispatch_id'], 'ctx_new')
        final = self.bodies()[-1]
        self.assertIn('过期的额度数据', final)
        log = (self.home / '.config' / 'orca-model-routing' / 'usage-log.jsonl').read_text().splitlines()
        records = [json.loads(line) for line in log]
        start = next(r for r in records if r['event'] == 'start')
        handoff = next(r for r in records if r['event'] == 'quota_handoff')
        self.assertGreaterEqual(start['stale_takeover']['age_seconds'], 1800)
        self.assertGreaterEqual(handoff['target_stale_data']['age_seconds'], 1800)

    def test_coordinator_on_the_same_data_stays_blocked_and_cannot_use_the_switch(self):
        self.isolate()
        self.change_db(claude_age_s=1800)
        cmd = [sys.executable, str(self.scripts / 'route.py'), 'plan', '--kind', 'feature', '--complexity', 'normal', '--profile', 'opus_high']
        proc = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(json.loads(proc.stdout)['launchable'])
        proc = subprocess.run(cmd + ['--quota-guard-takeover', 'ctx_wire'], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)
        self.assertIn('存活守卫', json.loads(proc.stdout)['blocked_reason'])

    def test_guard_tick_refreshes_its_own_stale_pool(self):
        self.isolate()
        hits = []
        def clear_age():
            hits.append(1)
            self.change_db(codex_age_s=0)
        fake = FakeRuntime(self.dir / 'orca-data', on_call=clear_age)
        self.addCleanup(fake.close)
        self.change_db(report=self.report(), codex_age_s=2000)
        self.run_guard(0)
        self.assertEqual(self.phase['phase'], 'handoff_done')
        self.assertEqual(len(hits), 1)


class EndToEndPlan(unittest.TestCase):
    """A real route.py plan process: stale Claude, real socket client, fake Orca that then reports fresh numbers."""
    FAKE = r"""#!{python}
import json, sys
print(open({data!r}).read())
"""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(os.path.realpath(tmp.name))
        scripts = self.dir / 'skill' / 'scripts'
        scripts.mkdir(parents=True)
        self.data = self.dir / 'usage.json'
        fake = self.dir / 'orca'
        fake.write_text(self.FAKE.format(python=sys.executable, data=str(self.data)))
        fake.chmod(0o755)
        for name in ('quota.py', 'quota_guard.py', 'route.py', 'jev_route.py', 'setup_jev.py'):
            (scripts / name).write_text((ROOT / 'scripts' / name).read_text().replace('/usr/local/bin/orca', str(fake)))
        config = copy.deepcopy(CONFIG)
        config['quota_policy']['source']['command'][0] = str(fake)
        config['quota_policy']['usage_refresh']['timeout_seconds'] = 2
        (self.dir / 'skill' / 'routes.json').write_text(json.dumps(config))
        self.scripts = scripts
        self.env = dict(os.environ, HOME=str(self.dir), PYTHONDONTWRITEBYTECODE='1',
                        ORCA_ROUTING_STATE_DIR=str(self.dir / 'state'), ORCA_USER_DATA_PATH=str(self.dir / 'orca-data'))

    def usage(self, age):
        now = time.time()
        def window_(minutes):
            return dict(usedPercent=10, windowMinutes=minutes, resetsAt=int((now + 6000) * 1000))
        def item():
            return dict(status='ok', updatedAt=int((now - age) * 1000), session=window_(300), weekly=window_(10080))
        self.data.write_text(json.dumps(dict(result=dict(rateLimits=dict(claude=item(), codex=item(), antigravity=item())))))

    def plan(self):
        return subprocess.run([sys.executable, str(self.scripts / 'route.py'), 'plan', '--kind', 'bugfix', '--complexity', 'normal'],
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_refresh_recovers_the_plan_and_the_next_one_sends_nothing(self):
        self.usage(2000)
        fake = FakeRuntime(self.dir / 'orca-data', on_call=lambda: self.usage(1))
        self.addCleanup(fake.close)
        first = self.plan()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        out = json.loads(first.stdout)
        self.assertTrue(out['launchable'])
        self.assertEqual(out['quota']['usage_refresh']['outcome'], 'ok')
        self.assertTrue(out['quota']['usage_refresh']['providers']['claude']['advanced'])
        self.assertEqual(len(fake.requests), 1)
        self.usage(2000)  # stale again, inside the interval
        second = self.plan()
        self.assertEqual(second.returncode, 2)
        self.assertEqual(json.loads(second.stdout)['quota']['usage_refresh']['outcome'], 'rate_limited')
        self.assertEqual(len(fake.requests), 1)
        self.assertNotIn(TOKEN, first.stdout + first.stderr + second.stdout + second.stderr)

    def test_hanging_orca_degrades_within_the_timeout(self):
        self.usage(2000)
        fake = FakeRuntime(self.dir / 'orca-data', behavior='hang')
        self.addCleanup(fake.close)
        begun = time.monotonic()
        proc = self.plan()
        elapsed = time.monotonic() - begun
        self.assertEqual(proc.returncode, 2)
        self.assertLess(elapsed, 2 + 5)
        out = json.loads(proc.stdout)
        self.assertEqual(out['quota']['usage_refresh']['outcome'], 'timeout')
        self.assertEqual(out['plan']['blocked_code'], 'quota_unknown')

    def test_no_orca_at_all_degrades(self):
        self.usage(2000)
        proc = self.plan()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)['quota']['usage_refresh']['outcome'], 'unavailable')


MINUTES = 10080


def weekly_at(fraction, used, age=0, status='ok'):
    reset = NOW + int(round((1 - fraction) * MINUTES * 60))
    return output(claude=entry(age=age, status=status, weekly=used, weekly_reset=reset, session_reset=NOW + 3000))


class Reminder(Case):
    def setUp(self):
        super().setUp()
        # Pin the fixture copy below the restore value (4).
        self.config['quota_policy']['capacity_factors']['claude'] = 2
        self.pol = quota.validate_policy(self.config['quota_policy'])

    def view(self, read, config=None):
        pol = quota.validate_policy((config or self.config)['quota_policy'])
        snap = quota.fetch_snapshot(pol, runner=runner_of(read), clock=lambda: NOW)
        return quota.restore_reminder(pol, snap, NOW * 1000)

    def test_all_three_thresholds_met_at_their_boundaries(self):
        view = self.view(weekly_at(0.25, 18.75))
        self.assertTrue(view['active'], view)
        self.assertEqual(view['projected_unused_pp'], 25)
        self.assertEqual(view['used_pp'], 18.75)
        self.assertEqual(view['remaining_pp'], 81.25)
        self.assertEqual(view['elapsed_fraction'], 0.25)
        self.assertEqual(view['minutes_to_reset'], 7560)
        self.assertIn('恢复 Claude 权重', view['message'])

    def test_elapsed_fraction_just_below(self):
        view = self.view(weekly_at(0.2499, 1))
        self.assertFalse(view['active'])
        self.assertIn('已过时间比例', view['reason'])

    def test_remaining_boundary(self):
        self.assertTrue(self.view(weekly_at(0.9, 60))['active'])
        view = self.view(weekly_at(0.9, 60.01))
        self.assertFalse(view['active'])
        self.assertIn('剩余', view['reason'])

    def test_projected_unused_boundary(self):
        view = self.view(weekly_at(0.25, 18.76))
        self.assertFalse(view['active'])
        self.assertIn('预计未用', view['reason'])

    def test_projected_unused_has_a_floor_of_zero(self):
        view = self.view(weekly_at(0.5, 59))
        self.assertEqual(view['projected_unused_pp'], 0)
        self.assertFalse(view['active'])

    def test_restored_weight_never_reminds(self):
        config = copy.deepcopy(self.config)
        config['quota_policy']['capacity_factors']['claude'] = 4
        view = self.view(weekly_at(0.5, 10), config)
        self.assertFalse(view['active'])
        self.assertIn('恢复值', view['reason'])
        config['quota_policy']['capacity_factors']['claude'] = 5
        self.assertFalse(self.view(weekly_at(0.5, 10), config)['active'])

    def test_stale_or_unknown_data_never_reminds(self):
        self.assertFalse(self.view(weekly_at(0.5, 10, age=901))['active'])
        self.assertTrue(self.view(weekly_at(0.5, 10, age=899))['active'])
        self.assertFalse(self.view(weekly_at(0.5, 10, status='error'))['active'])
        self.assertFalse(self.view(output(claude=entry(weekly_reset=NOW - 1)))['active'])
        claude = entry()
        claude['weekly'] = None
        self.assertFalse(self.view(output(claude=claude))['active'])
        pol = self.pol
        self.assertFalse(quota.restore_reminder(pol, dict(ok=False, providers={}), NOW * 1000)['active'])

    def test_session_window_is_not_consulted(self):
        read = json.loads(weekly_at(0.5, 10))
        read['result']['rateLimits']['claude']['session']['usedPercent'] = 99
        self.assertTrue(self.view(json.dumps(read))['active'])

    def test_disabled_or_absent_config(self):
        config = copy.deepcopy(self.config)
        config['quota_policy']['restore_reminder']['enabled'] = False
        self.assertFalse(self.view(weekly_at(0.5, 10), config)['active'])
        del config['quota_policy']['restore_reminder']
        self.assertFalse(self.view(weekly_at(0.5, 10), config)['active'])

    def output_view(self, read, may_claim=None):
        snap = quota.fetch_snapshot(self.pol, runner=runner_of(read), clock=lambda: NOW)
        return quota.reminder_for_output(self.pol, snap, NOW * 1000, may_claim)

    def test_first_only_once_per_window(self):
        read = weekly_at(0.5, 10)
        first = self.output_view(read)
        self.assertTrue(first['active'] and first['first_in_window'])
        second = self.output_view(read)
        self.assertTrue(second['active'])
        self.assertFalse(second['first_in_window'])
        self.assertTrue(second['already_notified'])

    def test_small_reset_jitter_is_the_same_window(self):
        self.assertTrue(self.output_view(weekly_at(0.5, 10))['first_in_window'])
        read = json.loads(weekly_at(0.5, 10))
        read['result']['rateLimits']['claude']['weekly']['resetsAt'] += 5 * 60000
        self.assertFalse(self.output_view(json.dumps(read))['first_in_window'])

    def test_new_window_reminds_again(self):
        self.assertTrue(self.output_view(weekly_at(0.5, 10))['first_in_window'])
        read = json.loads(weekly_at(0.5, 10))
        read['result']['rateLimits']['claude']['weekly']['resetsAt'] += 86400000
        self.assertTrue(self.output_view(json.dumps(read))['first_in_window'])

    def test_inactive_never_uses_the_window_notice(self):
        self.assertFalse(self.output_view(weekly_at(0.2, 1))['first_in_window'])
        self.assertTrue(self.output_view(weekly_at(0.5, 10))['first_in_window'])

    def test_caller_that_may_not_claim_leaves_it_for_the_coordinator(self):
        read = weekly_at(0.5, 10)
        self.assertFalse(self.output_view(read, lambda: False)['first_in_window'])
        self.assertTrue(self.output_view(read)['first_in_window'])

    def test_concurrent_claims_have_one_winner(self):
        code = "import sys;sys.path.insert(0,sys.argv[1]);import quota;print(quota.claim_reminder(1234567890000))"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        procs = [subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'scripts')], env=env, stdout=subprocess.PIPE, text=True) for _ in range(8)]
        results = [p.communicate(timeout=30)[0].strip() for p in procs]
        self.assertEqual(results.count('True'), 1)
        self.assertEqual(results.count('False'), 7)

    def test_unwritable_state_is_silent_rather_than_nagging(self):
        with patch.object(quota, '_write_state', side_effect=OSError('read-only')):
            view = self.output_view(weekly_at(0.5, 10))
        self.assertTrue(view['active'])
        self.assertFalse(view['first_in_window'])


class ReminderInRouting(Case):
    def setUp(self):
        super().setUp()
        # Pin the fixture copy below the restore value (4).
        self.config['quota_policy']['capacity_factors']['claude'] = 2

    def route(self, argv, reads, config=None, **kw):
        return route_main(argv, reads, config=config or self.config, **kw)

    def test_plan_output_has_the_independent_field_and_marks_first_once(self):
        args = ['plan', '--kind', 'feature', '--complexity', 'normal']
        with patch.object(qg, 'active_guards', return_value=[]):
            code, out, _, _ = self.route(args, [weekly_at(0.5, 10)])
            self.assertEqual(code, 0)
            reminder = out['restore_reminder']
            self.assertTrue(reminder['first_in_window'])
            for key in ('used_pp', 'remaining_pp', 'minutes_to_reset', 'projected_unused_pp', 'message', 'resets_at'):
                self.assertIn(key, reminder)
            self.assertIn('恢复 Claude 权重', reminder['message'])
            code, out, _, _ = self.route(args, [weekly_at(0.5, 10)])
            self.assertFalse(out['restore_reminder']['first_in_window'])
            self.assertTrue(out['restore_reminder']['active'])

    def test_restored_weight_never_reminds_in_routing(self):
        # 10 exceeds the restore value and 4 equals it. Neither may trigger a reminder.
        args = ['plan', '--kind', 'feature', '--complexity', 'normal']
        for weight in (4, 10):
            config = copy.deepcopy(self.config)
            config['quota_policy']['capacity_factors']['claude'] = weight
            with patch.object(qg, 'active_guards', return_value=[]):
                code, out, _, _ = self.route(args, [weekly_at(0.5, 10)], config=config)
            self.assertFalse(out['restore_reminder']['active'], weight)
            self.assertFalse(out['restore_reminder']['first_in_window'], weight)
            self.assertIn('恢复值', out['restore_reminder']['reason'], weight)

    def test_it_changes_no_routing_field(self):
        args = ['plan', '--kind', 'bugfix', '--complexity', 'normal']
        results = []
        for reminder_on in (True, False):
            config = copy.deepcopy(self.config)
            config['quota_policy']['restore_reminder']['enabled'] = reminder_on
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ORCA_ROUTING_STATE_DIR': tmp}), \
                    patch.object(qg, 'active_guards', return_value=[]):
                results.append(self.route(args, [weekly_at(0.5, 10)], config=config))
        (code_on, on, _, _), (code_off, off, _, _) = results
        self.assertTrue(on['restore_reminder']['active'])
        self.assertFalse(off['restore_reminder']['active'])
        self.assertEqual((code_on, on['launchable'], on['plan']), (code_off, off['launchable'], off['plan']))
        self.assertEqual(on['quota'], off['quota'])

    def test_blocked_plan_still_reports_it_and_keeps_its_exit_code(self):
        # A budget neither pool can cover: blocked, while Claude's weekly window still earns the reminder.
        args = ['plan', '--kind', 'bugfix', '--complexity', 'normal', '--budget-session', '100']
        with patch.object(qg, 'active_guards', return_value=[]):
            code, out, _, _ = self.route(args, [weekly_at(0.5, 10)])
        self.assertEqual(code, 2)
        self.assertFalse(out['launchable'])
        self.assertTrue(out['restore_reminder']['active'])

    def test_broken_reminder_never_touches_the_plan(self):
        args = ['plan', '--kind', 'feature', '--complexity', 'normal']
        with patch.object(quota, 'reminder_for_output', side_effect=RuntimeError('boom')):
            code, out, _, _ = self.route(args, [weekly_at(0.5, 10)])
        self.assertEqual((code, out['launchable'], out['restore_reminder']), (0, True, None))

    def test_preview_neither_refreshes_nor_uses_up_the_notice(self):
        args = ['plan', '--kind', 'bugfix', '--complexity', 'normal', '--preview']
        def boom(pol, clock=None):
            raise AssertionError('preview must not refresh')
        with patch.object(qg, 'active_guards', return_value=[]):
            code, out, _, _ = self.route(args, [output(claude=entry(age=2000))], refresher=boom)
            self.assertEqual(out['quota']['usage_refresh']['outcome'], 'disabled')
            code, out, _, _ = self.route(args, [weekly_at(0.5, 10)])
            self.assertTrue(out['restore_reminder']['active'])
            self.assertFalse(out['restore_reminder']['first_in_window'])

    def test_a_guard_child_plan_does_not_use_up_the_notice(self):
        args = ['plan', '--kind', 'feature', '--complexity', 'normal']
        with patch.object(qg, 'active_guards', return_value=[dict(dispatch_id='x', pid=os.getppid())]):
            code, out, _, _ = self.route(args, [weekly_at(0.5, 10)])
        self.assertFalse(out['restore_reminder']['first_in_window'])
        self.assertTrue(out['restore_reminder']['active'])
        with patch.object(qg, 'active_guards', return_value=[]):
            code, out, _, _ = self.route(args, [weekly_at(0.5, 10)])
        self.assertTrue(out['restore_reminder']['first_in_window'])

    def test_start_reports_it_on_stderr_and_keeps_the_receipt_on_stdout(self):
        spec = self.dir / 'spec.md'
        spec.write_text('任务')
        args = ['start', '--kind', 'feature', '--complexity', 'normal', '--run', 'run_x', '--spec-file', str(spec)]
        with patch.object(qg, 'active_guards', return_value=[]):
            code, out, err, starts = self.route(args, [weekly_at(0.5, 10)])
        self.assertEqual((code, len(starts)), (0, 1))
        self.assertEqual(out['result']['dispatchId'], 'ctx_new')
        self.assertNotIn('restore_reminder', out)
        line = json.loads(err.splitlines()[0])
        self.assertTrue(line['restore_reminder']['first_in_window'])

    def test_blocked_start_has_it_in_the_result(self):
        spec = self.dir / 'spec.md'
        spec.write_text('任务')
        args = ['start', '--kind', 'feature', '--complexity', 'normal', '--run', 'run_x', '--spec-file', str(spec), '--budget-session', '100']
        with patch.object(qg, 'active_guards', return_value=[]):
            code, out, _, starts = self.route(args, [weekly_at(0.5, 10)])
        self.assertEqual((code, starts), (2, []))
        self.assertTrue(out['restore_reminder']['active'])

    def test_guard_status_shows_it_without_marking(self):
        read = weekly_at(0.5, 10)
        # Give the guard's status command this test's pinned config at its temporary root.
        root = self.dir / 'guard-root'
        root.mkdir()
        (root / 'routes.json').write_text(json.dumps(self.config))
        real_fetch = quota.fetch_snapshot
        fake = lambda pol: real_fetch(pol, runner=runner_of(read), clock=lambda: NOW)
        out = io.StringIO()
        state = self.dir / 'g'
        state.mkdir()
        with patch.object(qg, 'ROOT', root), patch.object(quota, 'fetch_snapshot', fake), patch.object(quota.time, 'time', return_value=NOW), \
                contextlib.redirect_stdout(out):
            self.assertEqual(qg.main(['status', '--state-dir', str(state)]), 0)
        report = json.loads(out.getvalue())
        self.assertTrue(report['restore_reminder']['active'])
        self.assertFalse(report['restore_reminder']['already_notified'])
        self.assertFalse((self.dir / 'state' / quota.REMINDER_STATE).exists())
        self.assertIn('active_guards', report)
        self.assertTrue(quota.claim_reminder(report['restore_reminder']['resets_at_ms']))
        out = io.StringIO()
        with patch.object(qg, 'ROOT', root), patch.object(quota, 'fetch_snapshot', fake), patch.object(quota.time, 'time', return_value=NOW), \
                contextlib.redirect_stdout(out):
            qg.main(['status', '--state-dir', str(state)])
        self.assertTrue(json.loads(out.getvalue())['restore_reminder']['already_notified'])

    def test_guard_status_survives_orca_being_down(self):
        out = io.StringIO()
        state = self.dir / 'g'
        state.mkdir()
        with patch.object(qg, 'load_config', return_value=self.config), \
                patch.object(quota, 'fetch_snapshot', side_effect=RuntimeError('down')) as fetch, \
                contextlib.redirect_stdout(out):
            self.assertEqual(qg.main(['status', '--state-dir', str(state)]), 0)
        fetch.assert_called_once()
        self.assertFalse(json.loads(out.getvalue())['restore_reminder']['active'])


if __name__ == '__main__':
    unittest.main()
