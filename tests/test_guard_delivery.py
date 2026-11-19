"""Hermetic delivery checks: never send to any real Orca terminal."""
import copy
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_quota_guard import GuardCase, FakeBackend, META, worker, qg, snapshot, CONFIG

HANDLE = 'term_test'
# Synthetic footer, composer, and status rows.
UI = ['• Working (12s • esc to interrupt)', '› Ask Codex to do anything',
      '  GPT-6.1-Sol high · ~/project · task', '  ? for shortcuts       ⚠ 1 warning · f2 to view']


def terminal():
    return dict(handle=HANDLE, connected=True, writable=True, orphaned=False,
                agentIdentity='codex', incarnationId='inc_test', executionHostId='local')


def screen(lines=None):
    return dict(handle=HANDLE, status='running', source='screen', tail=UI if lines is None else lines)


def receipt(result, ok=True):
    return subprocess.CompletedProcess([], 0 if ok else 1, json.dumps(dict(ok=ok, result=result)), '')


class ScreenSafety(unittest.TestCase):
    def check(self, meta=None, frame=None):
        return qg.codex_screen_decision(terminal() if meta is None else meta, screen() if frame is None else frame, HANDLE)

    def test_running_codex_current_composer(self):
        self.assertTrue(self.check()['safe'])

    def test_idle_codex_current_composer(self):
        self.assertTrue(self.check(frame=screen(UI[1:]))['safe'])

    def test_bootstrap_failure_then_zsh_even_with_stale_ui(self):
        for prompt in ('user@host project % ', '❯ ', '$ ', '➜ project ', '/tmp/worktree # '):
            result = self.check(frame=screen(UI + ['account/read failed during TUI bootstrap', prompt]))
            self.assertFalse(result['safe'], prompt)
            self.assertIn('无法确认 Codex 在运行', result['reason'])
            self.assertNotIn('似乎未运行', result['reason'])

    def test_unknown_cursor_or_stream_or_missing_screen_refuses(self):
        for frame in ({}, dict(screen(), source='stream'), dict(screen(), source='screen-unavailable'),
                      screen(['esc to interrupt', 'unknown prompt']), screen(['Ask Codex is in old output'])):
            self.assertFalse(self.check(frame=frame)['safe'])

    def test_dead_mismatched_or_orphaned_handle_refuses(self):
        for changes in (dict(connected=False), dict(writable=False), dict(orphaned=True),
                        dict(handle='other'), dict(agentIdentity='zsh'), dict(incarnationId=None)):
            self.assertFalse(self.check(meta=dict(terminal(), **changes))['safe'])
        self.assertFalse(self.check(frame=dict(screen(), status='exited'))['safe'])

    def test_existing_draft_or_nonempty_composer_refuses(self):
        self.assertFalse(self.check(frame=dict(screen(), draft='user text'))['safe'])
        self.assertFalse(self.check(frame=screen([*UI[:1], '› unfinished user text', *UI[2:]]))['safe'])

    def test_input_text_strict_path_allowlist(self):
        text = qg.delivery_input_text('/tmp/context.md')
        self.assertIn('额度守卫请求交接', text)
        self.assertIn('/tmp/context.md', text)
        self.assertFalse(set('><|;&$`\n\r\x1b\\\'"(){}[]*?!~').intersection(text))
        for bad in ('/tmp/a>b', '/tmp/a<b', '/tmp/a|b', '/tmp/a;b', '/tmp/a&b', '/tmp/a$b',
                    '/tmp/a`b', '/tmp/a\nb', '/tmp/a\rb', '/tmp/a b', '/tmp/a\x1bb',
                    '/tmp/a(b)', '/tmp/a\\b', '/tmp/a*', '/tmp/中.md', 'relative.md'):
            self.assertIsNone(qg.delivery_input_text(bad), repr(bad))


class BackendDelivery(unittest.TestCase):
    def backend(self, runner):
        return qg.Backend(CONFIG, META, runner=runner)

    def test_send_returns_message_id(self):
        calls = []
        def run(args, **kw): calls.append(args); return receipt(dict(messageId='msg_test'))
        self.assertEqual(self.backend(run).send('dispatch:ctx_old', 'body')['messageId'], 'msg_test')
        self.assertIn('--json', calls[0])

    def test_full_send_receipt_normalizes_message_id(self):
        # Backend.command requests --full; Orca 1.4.248 returns message.id, not concise messageId.
        for full in (dict(message=dict(id='msg_full',delivered_at=None)),
                     dict(relay=dict(messageId='msg_full',destination='worker'))):
            b=self.backend(lambda *a, **k: receipt(full))
            self.assertEqual(b.send('dispatch:ctx_old','body')['messageId'],'msg_full')

    def test_cli_first_delivered_and_pending(self):
        for value, state in [('2026-11-05T09:00:00Z', 'delivered'), (None, 'pending')]:
            b = self.backend(lambda *a, **k: receipt(dict(messages=[dict(id='msg_test', delivered_at=value)])))
            with patch.object(qg, 'read_message_delivery', side_effect=AssertionError('CLI sufficient')):
                self.assertEqual(b.delivery_status('msg_test', HANDLE)['state'], state)

    def test_cli_absent_or_failed_falls_back_readonly_db(self):
        for run in (lambda *a, **k: receipt(dict(messages=[])),
                    lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired('inbox', 3))):
            with patch.object(qg, 'read_message_delivery', return_value=dict(state='delivered', source='sqlite')) as db:
                self.assertEqual(self.backend(run).delivery_status('msg_test', HANDLE)['state'], 'delivered')
                db.assert_called_once_with('msg_test')

    def test_readonly_db_pending_delivered_missing_and_failure(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'orchestration.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE messages (id TEXT, delivered_at TEXT)')
                db.executemany('INSERT INTO messages VALUES (?,?)', [('msg_pending', None), ('msg_ok', 'stamp')])
            self.assertEqual(qg.read_message_delivery('msg_pending', path)['state'], 'pending')
            self.assertEqual(qg.read_message_delivery('msg_ok', path)['state'], 'delivered')
            self.assertEqual(qg.read_message_delivery('msg_absent', path)['state'], 'unknown')
            self.assertEqual(qg.read_message_delivery('msg_ok', Path(d)/'missing.db')['state'], 'unknown')
            self.assertFalse((Path(d)/'missing.db').exists())
            with patch.object(qg.sqlite3, 'connect', side_effect=sqlite3.OperationalError('locked')):
                self.assertEqual(qg.read_message_delivery('msg_ok', path)['state'], 'unknown')
            real_connect = sqlite3.connect
            def connect(db_uri, **kw):
                self.assertIn('?mode=ro', db_uri)
                self.assertTrue(kw['uri']); self.assertLessEqual(kw['timeout'], 2)
                return real_connect(db_uri, **kw)
            with patch.object(qg.sqlite3, 'connect', side_effect=connect):
                qg.read_message_delivery('msg_ok', path)

    def test_terminal_error_retains_safe_retry_id_without_resending(self):
        request='12345678-1234-1234-1234-123456789abc'
        calls=[]
        def run(args, **kw):
            calls.append(args)
            return subprocess.CompletedProcess([],1,json.dumps(dict(ok=False,error=dict(
                code='transport_error',data=dict(orchestrationRequestId=request)))), '')
        result=self.backend(run).terminal_input(HANDLE,qg.delivery_input_text('/tmp/context.md'))
        self.assertEqual(result['request_id'],request)
        self.assertFalse(result['accepted']);self.assertEqual(len(calls),1)

    def test_screen_reads_fail_closed(self):
        def fail(*a, **k): raise subprocess.TimeoutExpired('read', 5)
        self.assertFalse(self.backend(fail).codex_running(dict(terminal=terminal()))['safe'])

    def test_read_screen_and_metadata_must_match(self):
        calls = []
        def run(args, **kw):
            calls.append(args)
            return receipt(dict(terminal=screen() if 'read' in args else terminal()))
        b = self.backend(run)
        self.assertTrue(b.codex_running(dict(terminal=terminal()))['safe'])
        self.assertTrue(any('--screen' in c for c in calls))
        self.assertFalse(any('send' in c for c in calls))
        # And when they do not match. Each of the two reads names its own handle.
        def disagreeing(meta, frame):
            return lambda args, **kw: receipt(dict(terminal=frame if 'read' in args else meta))
        for meta, frame, why in ((dict(terminal(), handle='term_other'), screen(), '句柄'),
                                 (terminal(), dict(screen(), handle='term_other'), '屏幕')):
            result = self.backend(disagreeing(meta, frame)).codex_running(dict(terminal=terminal()))
            self.assertFalse(result['safe'], why)
            self.assertIn(why, result['reason'])
        result = self.backend(disagreeing(dict(terminal(), incarnationId='inc_new'), screen())).codex_running(dict(terminal=terminal()))
        self.assertFalse(result['safe'])
        self.assertIn('替换', result['reason'])

    def test_send_warning_is_accepted_and_retry_uses_exact_request_id(self):
        calls = []
        def run(args, **kw):
            calls.append(args)
            return receipt(dict(send=dict(accepted=True, prompt=dict(requestId='12345678-1234-1234-1234-123456789abc', stages=['input_accepted'])),
                                warnings=['no turn start was observed']))
        b = self.backend(run)
        text = qg.delivery_input_text('/tmp/context.md')
        self.assertTrue(b.terminal_input(HANDLE, text)['accepted'])
        self.assertEqual(len(calls), 1)  # warning must never cause a fresh send
        b.terminal_input(HANDLE, text, retry_request='12345678-1234-1234-1234-123456789abc')
        self.assertEqual(calls[1], calls[0][:-1] + ['--retry-request', '12345678-1234-1234-1234-123456789abc', '--json'])
        self.assertIn('--wait-submit', calls[0]); self.assertIn('--enter', calls[0])


class DeliveryFlow(GuardCase):
    def setUp(self):
        super().setUp()
        self.config['quota_policy']['quota_guard']['delivery_fallback'] = dict(enabled=True, wait_seconds=20)
        self.meta = dict(META, context_file=str(self.dir/'original-context.md'))
        Path(self.meta['context_file']).write_text('原始任务，不得自动接手')
        self.guard = qg.Guard(self.config, self.meta, self.backend, self.dir)
        self.backend.handoff_on_notify = None
        show = self.backend.show
        def showing():
            view=show()
            if 'terminal' not in view: view=dict(view,terminal=terminal())
            return view
        self.backend.show=showing
        send = self.backend.send
        def sending(to, body, high=False):
            send(to, body, high)
            return dict(messageId='msg_test')
        self.backend.send = sending
        self.lookups, self.probes, self.inputs = [], [], []
        self.delivery_state = 'pending'
        def lookup(mid, handle):
            self.lookups.append((mid,handle)); return dict(state=self.delivery_state, source='fake')
        def probe(view):
            self.probes.append(self.backend.mono); return dict(safe=True, reason='Codex 界面', handle=HANDLE)
        def enter(handle, text, retry_request=None):
            self.inputs.append((handle,text,retry_request)); return dict(accepted=True, request_id='12345678-1234-1234-1234-123456789abc')
        self.backend.delivery_status, self.backend.codex_running, self.backend.terminal_input = lookup, probe, enter

    def run_handoff(self): return self.guard.tick(snapshot())
    def phase(self): return json.loads((self.dir/'ctx_old-phase.json').read_text())['handoff_delivery']

    def test_delivered_never_inputs(self):
        self.delivery_state='delivered'
        self.run_handoff()
        self.assertEqual(self.inputs, []); self.assertEqual(self.probes, [])
        self.assertEqual(len(self.backend.bodies('run:')), 2)  # original start notice and final alert only
        self.assertEqual(self.phase()['state'], 'delivered')
        self.assertEqual(self.phase()['skip_reason'], '消息已送达')

    def test_pending_codex_inputs_once_after_wait_without_extending_grace(self):
        self.run_handoff()
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(self.probes, [1020])
        self.assertEqual(self.backend.mono, 1180)
        d=self.phase()
        self.assertEqual(d['message_id'], 'msg_test'); self.assertTrue(d['terminal_input_used'])
        self.assertTrue(d['terminal_input_attempted']); self.assertEqual(d['elapsed_seconds'], 20)
        self.assertIn('请求交接', self.inputs[0][1])
        entry=Path(self.inputs[0][1].split()[-1])
        self.assertIn(qg.HANDOFF_END, entry.read_text())
        self.assertIn('原始任务', entry.read_text())

    def test_unknown_delivery_can_input_only_verified_codex(self):
        self.delivery_state='unknown'; self.run_handoff()
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(self.phase()['state'], 'unknown')

    def test_shell_unreadable_and_invalid_handle_never_input_and_alert(self):
        for reason in ('无法确认 Codex 在运行：屏幕最底部像 shell 提示符', '无法读屏', '终端句柄与登记的不一致'):
            with self.subTest(reason=reason):
                self.setUp()
                self.backend.codex_running=lambda view: dict(safe=False, reason=reason)
                self.run_handoff()
                self.assertEqual(self.inputs, [])
                self.assertEqual(self.phase()['skip_reason'], reason)
                alert=self.backend.bodies('run:')[-1]
                for part in ('msg_test', 'pending', '20', 'terminal_input_used', reason): self.assertIn(part, alert)
                self.guard.finished=None
                self.guard.pool_lock.path.unlink(missing_ok=True)

    def test_disabled_matches_old_flow(self):
        self.guard.cfg['delivery_fallback']['enabled']=False
        self.run_handoff()
        self.assertEqual(self.lookups, []); self.assertEqual(self.probes, []); self.assertEqual(self.inputs, [])
        self.assertEqual(self.backend.mono, 1180)
        self.assertNoTakeover()

    def test_completion_cancellation_report_during_wait_prevents_input(self):
        for action in ('done', 'cancel', 'report'):
            with self.subTest(action=action):
                self.setUp()
                def slept():
                    if action=='done':self.backend.views=[worker('succeeded')]
                    if action=='cancel':self.guard.cancel_path().touch()
                    if action=='report':
                        from test_quota_guard import COMPLETE_HANDOFF
                        (self.dir/'ctx_old-handoff.md').write_text(COMPLETE_HANDOFF)
                self.backend.on_sleep=slept
                self.run_handoff()
                self.assertEqual(self.inputs, [])
                self.assertFalse(self.phase()['terminal_input_attempted'])

    def test_unsafe_entry_path_refuses_without_altering_path(self):
        unsafe=self.dir/'bad$dir'; unsafe.mkdir()
        self.guard.state=unsafe
        self.run_handoff()
        self.assertEqual(self.inputs, [])
        self.assertIn('路径',self.guard.facts['handoff_delivery']['skip_reason'])

    def test_input_error_does_not_resend_and_records_unknown(self):
        def enter(*a, **k):self.inputs.append(a); raise subprocess.TimeoutExpired('terminal send', 15)
        self.backend.terminal_input=enter
        self.run_handoff()
        self.assertEqual(len(self.inputs),1)
        self.assertTrue(self.phase()['terminal_input_attempted']);self.assertFalse(self.phase()['terminal_input_used'])
        self.assertIn('未知', self.phase()['skip_reason'])

    def test_late_delivery_before_input_never_inputs(self):
        def lookup(*a):
            self.lookups.append(a)
            return dict(state='delivered' if self.backend.mono>=1020 else 'pending', source='fake')
        self.backend.delivery_status=lookup
        self.run_handoff();self.assertEqual(self.inputs, [])

    def test_completion_or_cancel_while_screen_read_prevents_input(self):
        for action in ('done', 'cancel'):
            self.setUp()
            def probe(view):
                if action=='done':self.backend.views=[worker('succeeded')]
                else:self.guard.cancel_path().touch()
                return dict(safe=True, reason='Codex', handle=HANDLE)
            self.backend.codex_running=probe
            self.run_handoff();self.assertEqual(self.inputs, [])

    def test_message_delivered_while_reading_screen_never_inputs(self):
        def probe(view):
            self.delivery_state='delivered'
            return dict(safe=True, reason='Codex', handle=HANDLE)
        self.backend.codex_running=probe
        self.run_handoff(); self.assertEqual(self.inputs, [])
        self.assertEqual(self.phase()['state'], 'delivered')

    def test_dispatch_terminal_replaced_during_screen_read_refuses(self):
        def probe(view):
            fresh=worker();fresh['terminal']=dict(terminal(),handle='term_replaced')
            self.backend.views=[fresh]
            return dict(safe=True, reason='Codex', handle=HANDLE)
        self.backend.codex_running=probe
        self.run_handoff();self.assertEqual(self.inputs, [])

    def test_terminal_replaced_during_final_delivery_lookup_refuses(self):
        def lookup(*args):
            self.lookups.append(args)
            if len(self.lookups)==6:
                fresh=worker();fresh['terminal']=dict(terminal(),handle='term_replaced')
                self.backend.views=[fresh]
            return dict(state='pending',source='fake')
        self.backend.delivery_status=lookup
        self.run_handoff();self.assertEqual(self.inputs, [])

    def test_persistent_once_latch_survives_new_guard(self):
        self.run_handoff();self.assertEqual(len(self.inputs),1)
        self.backend.views=[worker()]
        self.guard=qg.Guard(self.config,self.meta,self.backend,self.dir)
        self.guard.pool_lock.path.unlink(missing_ok=True)
        self.run_handoff()
        self.assertEqual(len(self.inputs),1)
        self.assertIn('已尝试',self.phase()['skip_reason'])

    def test_wait_longer_than_grace_never_inputs(self):
        self.guard.cfg['delivery_fallback']['wait_seconds']=240
        self.run_handoff();self.assertEqual(self.inputs, [])
        self.assertEqual(self.backend.mono,1180)

    def test_alert_refreshes_late_orchestration_delivery_without_more_input(self):
        def slept():
            if self.backend.mono>=1030:self.delivery_state='delivered'
        self.backend.on_sleep=slept
        self.run_handoff()
        self.assertEqual(len(self.inputs),1)
        self.assertEqual(self.phase()['state'],'delivered')
        self.assertEqual(self.phase()['last_checked_elapsed_seconds'],180)
        self.assertEqual(self.phase()['elapsed_seconds'],20)

    def test_settings_defaults_and_invalid_wait(self):
        self.assertEqual(qg.settings({})['delivery_fallback'], dict(enabled=True, wait_seconds=20))
        for bad in (-1, float('inf'), '20', True):
            c=copy.deepcopy(self.config); c['quota_policy']['quota_guard']['delivery_fallback']['wait_seconds']=bad
            with self.assertRaises(ValueError):qg.settings(c)


if __name__=='__main__':unittest.main()
