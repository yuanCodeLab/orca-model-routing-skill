"""Quota-aware choice between a task's existing primary/alternate profiles.

Usage comes from one bounded `orca account list --json` subprocess whose output is
reduced to status/updatedAt/session/weekly numbers, plus Orca's retry-after instant
and failure kind. Account identity (beyond a short digest to tell accounts apart),
other usageMetadata and raw receipts are never kept or printed. Nothing here
launches a worker, resets credits, switches accounts or retries.

This is not read-only. With a refresher, fresh_snapshot may make Orca fetch every
account's usage (see below) and writes numbers-and-digests state files under
routing_state_dir(): usage-refresh.json, last-good-usage.json; a plan or start may
also write restore-reminder.json. Without a refresher (--preview, status) it only reads.

Codex only: Orca's numbers can lag by tens of minutes while Codex session logs
are live, so the newest token_count rate_limits numbers replace Orca's when they
are newer and their reset instants prove the same account. Only those numbers
leave the log reader; session content is never kept, logged or printed. While that
log is live it also outranks Orca's status=error (log_covers_orca_error).

Unknown (cannot be verified) and insufficient (verified too low) are distinct
states; unknown is never treated as 0 or 100 and never lets the alternate win.

Stale data: Orca polls usage only while its main window is focused, so numbers go
stale whenever the user is elsewhere. fresh_snapshot then asks Orca for one forced
refresh over its local socket (accounts.list {refreshUsage:true}), rate-limited across
processes; status=error also qualifies, because a failed fetch can pass before its data
ages out. Every failure silently leaves the original judgment in place. The runtime
token is read into memory for that one request and is never logged, stored or printed.
Only a live guard's own takeover may use stale numbers, bounded by apply_stale_takeover.
"""
import fcntl
import hashlib
import heapq
import json
import math
import os
import re
import socket
import stat
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

ORCA_ACCOUNT_CMD = ['/usr/local/bin/orca', 'account', 'list', '--json']
ORCA_PATH_ACCOUNT_CMD = ['orca', 'account', 'list', '--json']


def load_config(root):
    """Read the private override when present, otherwise load the public template."""
    try:
        text = (root / 'routes.json').read_text()
    except FileNotFoundError:
        text = (root / 'routes.example.json').read_text()
    return json.loads(text)
WINDOWS = ('session', 'weekly')
# Independently reported model-specific limits within a pool's weekly window.
CAP_WINDOWS = ('fableWeekly',)
EPS = 1e-9
SOURCE_ORCA = 'orca'
SOURCE_CODEX_LOG = 'codex_session_log'
CODEX_LIMIT_ID = 'codex'
LOG_WINDOWS = (('session', 'primary'), ('weekly', 'secondary'))
ACCOUNT_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
ORCA_RUNTIME_FILE = 'orca-runtime.json'
RPC_MAX_BYTES = 16 * 1024 * 1024
REFRESH_STATE = 'usage-refresh.json'
REFRESH_LOCK = 'usage-refresh.lock'
LAST_GOOD_STATE = 'last-good-usage.json'
REMINDER_STATE = 'restore-reminder.json'
REMINDER_LOCK = 'restore-reminder.lock'
# A weekly window cannot roll over this fast; small provider jitter is still the same window.
SAME_WINDOW_MS = 10 * 60000
# Orca failures that say nothing about the account itself. Sign-in and plan problems stay closed.
TRANSIENT_FAILURES = ('rate-limited', 'server', 'network', 'usage-unavailable')


class PolicyError(ValueError):
    pass


def _num(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _safe(text):
    return re.sub(r'[^A-Za-z0-9_.-]', '?', str(text))[:32]


def _nonneg(value, label, hi=None):
    if not _num(value) or value < 0 or (hi is not None and value > hi):
        raise PolicyError('quota_policy.%s 无效：%r' % (label, value))
    return float(value)


def validate_policy(policy):
    """Return a normalized copy or raise PolicyError. Read afresh on every call."""
    if not isinstance(policy, dict):
        raise PolicyError('缺少 quota_policy。')
    src = policy.get('source') or {}
    command = src.get('command')
    if command not in (ORCA_ACCOUNT_CMD, ORCA_PATH_ACCOUNT_CMD):
        raise PolicyError('quota_policy.source.command 必须是 %s 或 %s。'
                          % (' '.join(ORCA_PATH_ACCOUNT_CMD), ' '.join(ORCA_ACCOUNT_CMD)))
    timeout = _nonneg(src.get('timeout_seconds'), 'source.timeout_seconds')
    if not 0 < timeout <= 120:
        raise PolicyError('quota_policy.source.timeout_seconds 需在 (0,120]。')
    out = dict(
        enabled=bool(policy.get('enabled', True)),
        command=list(command), timeout=timeout,
        max_bytes=int(_nonneg(src.get('max_output_bytes', 2097152), 'source.max_output_bytes')),
        stale=_nonneg(policy.get('stale_after_seconds'), 'stale_after_seconds'),
        skew=_nonneg(policy.get('max_future_skew_seconds', 60), 'max_future_skew_seconds'),
        provider_keys=dict(policy.get('provider_keys') or {}),
        factors={}, reserves={}, windows={}, budgets={},
        unknown_primary=(policy.get('unknown_policy') or {}).get('primary_without_reserve', 'allow'),
        primary_first=dict(policy.get('primary_first') or {}))
    if out['stale'] <= 0:
        raise PolicyError('quota_policy.stale_after_seconds 必须为正。')
    if out['unknown_primary'] not in ('allow', 'block'):
        raise PolicyError('unknown_policy.primary_without_reserve 只能是 allow/block。')
    out['reserve_scaling'] = policy.get('reserve_scaling', 'prorated')
    if out['reserve_scaling'] not in ('prorated', 'fixed'):
        raise PolicyError('quota_policy.reserve_scaling 只能是 prorated/fixed。')
    for agent, value in (policy.get('capacity_factors') or {}).items():
        if _nonneg(value, 'capacity_factors.' + agent) <= 0:
            raise PolicyError('capacity_factors.%s 必须为正。' % agent)
        out['factors'][agent] = float(value)
    out['horizons'] = {}
    for agent, per in (policy.get('reset_horizon_minutes') or {}).items():
        if not isinstance(per, dict):
            raise PolicyError('reset_horizon_minutes.%s 必须是对象。' % agent)
        for w, value in per.items():
            if w not in WINDOWS:
                raise PolicyError('reset_horizon_minutes.%s 只能包含 %s。' % (agent, '/'.join(WINDOWS)))
            if _nonneg(value, 'reset_horizon_minutes.%s.%s' % (agent, w)) <= 0:
                raise PolicyError('reset_horizon_minutes.%s.%s 必须为正。' % (agent, w))
        out['horizons'][agent] = {w: float(v) for w, v in per.items()}
    for agent, per in (policy.get('reserves') or {}).items():
        out['reserves'][agent] = {w: _nonneg((per or {}).get(w, 0), 'reserves.%s.%s' % (agent, w), 100)
                                  for w in WINDOWS}
    spec = policy.get('windows') or {}
    for w in WINDOWS:
        item = spec.get(w)
        if not isinstance(item, dict):
            raise PolicyError('缺少 quota_policy.windows.' + w)
        expected = item.get('expected_minutes')
        if not isinstance(expected, int) or isinstance(expected, bool) or expected <= 0:
            raise PolicyError('windows.%s.expected_minutes 无效。' % w)
        floor = _nonneg(item.get('time_floor_minutes'), 'windows.%s.time_floor_minutes' % w)
        if floor <= 0:
            raise PolicyError('windows.%s.time_floor_minutes 必须为正。' % w)
        out['windows'][w] = dict(expected=expected, floor=floor,
                                 weight=_nonneg(item.get('weight'), 'windows.%s.weight' % w))
    if sum(v['weight'] for v in out['windows'].values()) <= 0:
        raise PolicyError('窗口权重之和必须为正。')
    log = policy.get('codex_session_log') or {}
    # Absence is opt-out for older/private route configurations.
    out['session_log'] = dict(
        enabled=bool(log.get('enabled', False)),
        tolerance=_nonneg(log.get('resets_at_tolerance_seconds', 60), 'codex_session_log.resets_at_tolerance_seconds'),
        max_files=int(_nonneg(log.get('max_files', 3), 'codex_session_log.max_files')),
        tail_bytes=int(_nonneg(log.get('tail_bytes', 262144), 'codex_session_log.tail_bytes')),
        max_line_bytes=int(_nonneg(log.get('max_line_bytes', 16384), 'codex_session_log.max_line_bytes')))
    # Absence is opt-out, like the other optional sections below.
    refresh = policy.get('usage_refresh') or {}
    out['refresh'] = dict(
        enabled=bool(refresh.get('enabled', False)),
        min_interval=_nonneg(refresh.get('min_interval_seconds', 300), 'usage_refresh.min_interval_seconds'),
        timeout=_nonneg(refresh.get('timeout_seconds', 12), 'usage_refresh.timeout_seconds'),
        fetching_wait=_nonneg(refresh.get('fetching_wait_seconds', 5), 'usage_refresh.fetching_wait_seconds'))
    if not 0 < out['refresh']['timeout'] <= 30:
        raise PolicyError('quota_policy.usage_refresh.timeout_seconds 需在 (0,30]。')
    take = policy.get('guard_stale_takeover') or {}
    burn = take.get('conservative_burn_pp_per_min', dict(session=1.5, weekly=0.25))
    if not isinstance(burn, dict):
        raise PolicyError('quota_policy.guard_stale_takeover.conservative_burn_pp_per_min 必须是对象。')
    out['stale_takeover'] = dict(
        enabled=bool(take.get('enabled', False)),
        agents=list(take.get('agents', ['claude'])),
        max_age=_nonneg(take.get('max_age_minutes', 60), 'guard_stale_takeover.max_age_minutes') * 60,
        margin=_nonneg(take.get('plan_margin_minutes', 5), 'guard_stale_takeover.plan_margin_minutes'),
        recheck_margin=_nonneg(take.get('recheck_margin_minutes', 1), 'guard_stale_takeover.recheck_margin_minutes'),
        burn={w: _nonneg(burn.get(w), 'guard_stale_takeover.conservative_burn_pp_per_min.' + w) for w in WINDOWS})
    if any(v <= 0 for v in out['stale_takeover']['burn'].values()):
        raise PolicyError('guard_stale_takeover.conservative_burn_pp_per_min 必须为正。')
    rem = policy.get('restore_reminder') or {}
    out['reminder'] = dict(
        enabled=bool(rem.get('enabled', False)),
        agent=rem.get('agent', 'claude'),
        min_elapsed=_nonneg(rem.get('min_elapsed_fraction', 0.25), 'restore_reminder.min_elapsed_fraction', 1),
        min_remaining=_nonneg(rem.get('min_remaining_pp', 40), 'restore_reminder.min_remaining_pp', 100),
        min_unused=_nonneg(rem.get('min_projected_unused_pp', 25), 'restore_reminder.min_projected_unused_pp', 100),
        restore_factor=_nonneg(rem.get('restore_capacity_factor', 4), 'restore_reminder.restore_capacity_factor'))
    for level in ('normal', 'hard'):
        per = (policy.get('task_budget_points') or {}).get(level)
        if not isinstance(per, dict):
            raise PolicyError('缺少 task_budget_points.' + level)
        out['budgets'][level] = {w: _nonneg(per.get(w), 'task_budget_points.%s.%s' % (level, w), 100)
                                 for w in WINDOWS}
    out['session_headroom'] = _validate_session_headroom(policy)
    by_pool = out['primary_first'].get('min_weekly_pace_ratio_by_pool') or {}
    if not isinstance(by_pool, dict):
        raise PolicyError('primary_first.min_weekly_pace_ratio_by_pool 必须是对象。')
    out['primary_first']['min_weekly_pace_ratio_by_pool'] = {
        agent: _nonneg(v, 'primary_first.min_weekly_pace_ratio_by_pool.' + agent) for agent, v in by_pool.items()}
    return out


def _validate_session_headroom(policy):
    """Session headroom for pools whose 5-hour window runs out mid-task. Absence is opt-out."""
    raw = policy.get('session_headroom')
    if not raw:
        return dict(enabled=False, pools=[], levels={}, by_kind={}, burn=1.5, floor=5.0)
    if not isinstance(raw, dict):
        raise PolicyError('quota_policy.session_headroom 必须是对象。')
    pools = raw.get('pools', [])
    if not isinstance(pools, list) or not all(isinstance(p, str) for p in pools):
        raise PolicyError('quota_policy.session_headroom.pools 必须是账户池名列表。')
    levels = {level: _nonneg(raw.get(level), 'session_headroom.' + level, 100) for level in ('normal', 'hard')}
    by_kind = {}
    for kind, value in (raw.get('by_kind') or {}).items():
        # A number covers both complexities; {normal, hard} sets them apart.
        per = value if isinstance(value, dict) else dict(normal=value, hard=value)
        by_kind[kind] = {level: _nonneg(per.get(level), 'session_headroom.by_kind.%s.%s' % (kind, level), 100)
                         for level in ('normal', 'hard')}
    # The near-reset exemption uses the quota guard's own default session burn and floor.
    guard = policy.get('quota_guard') or {}
    burn = (guard.get('default_burn_pp_per_min') or {}).get('session', 1.5)
    floor = guard.get('floor_pp', 5)
    if _nonneg(burn, 'quota_guard.default_burn_pp_per_min.session') <= 0:
        raise PolicyError('quota_guard.default_burn_pp_per_min.session 必须为正。')
    return dict(enabled=bool(raw.get('enabled', True)), pools=list(pools), levels=levels, by_kind=by_kind,
                burn=float(burn), floor=_nonneg(floor, 'quota_guard.floor_pp', 100))


def session_headroom_for(pol, agent, kind, complexity):
    """The configured session headroom (pp) for this pool and task, before the near-reset exemption; None if none."""
    cfg = pol.get('session_headroom') or {}
    if not cfg.get('enabled') or agent not in cfg.get('pools', ()) or kind is None or complexity not in ('normal', 'hard'):
        return None
    if kind in cfg['by_kind']:
        return dict(base_pp=cfg['by_kind'][kind][complexity], basis='by_kind.%s.%s' % (kind, complexity))
    return dict(base_pp=cfg['levels'][complexity], basis=complexity)


def apply_session_headroom(info, headroom, pol):
    """Raise a session window's required pp to the headroom; the task budget stays in budget_pp.

    Near reset the window refills before a task could drain it, so the headroom is capped at
    minutes_to_reset × default session burn + floor. Required = max(task budget × multiplier, headroom).
    """
    if headroom is None or info is None:
        return info
    cfg = pol['session_headroom']
    near_reset = info['minutes_to_reset'] * cfg['burn'] + cfg['floor']
    effective = min(headroom['base_pp'], near_reset)
    required = max(info['budget_pp'], effective)
    info['session_headroom'] = dict(base_pp=headroom['base_pp'], basis=headroom['basis'],
                                    near_reset_cap_pp=round(near_reset, 4),
                                    burn_pp_per_min=cfg['burn'], floor_pp=cfg['floor'],
                                    near_reset_exempt=near_reset < headroom['base_pp'],
                                    effective_pp=round(effective, 4), task_budget_pp=info['budget_pp'],
                                    required_pp=round(required, 4))
    info['required_pp'] = round(required, 4)
    return info


def _required(info):
    return info.get('required_pp', info['budget_pp'])


def note_headroom_verdict(info):
    """Spell out the session headroom judgement on the window itself, passing or not."""
    sh = info.get('session_headroom') if info else None
    if not sh:
        return
    ok = info['available_pp'] > EPS and info['available_pp'] + EPS >= sh['required_pp']
    sh['passed'] = ok
    sh['verdict'] = ('5 小时余量%s：可用 %.4g 点 %s 所需 %.4g 点 = max(任务预算 %.4g, 门槛 %.4g)；门槛依据 session_headroom.%s=%.4g%s'
                     % ('满足' if ok else '不足', info['available_pp'], '≥' if ok else '<', sh['required_pp'],
                        sh['task_budget_pp'], sh['effective_pp'], sh['basis'], sh['base_pp'],
                        '，临近重置豁免（距重置 %.1f 分钟×%.4g+%.4g=%.4g）' % (
                            info['minutes_to_reset'], sh['burn_pp_per_min'], sh['floor_pp'], sh['near_reset_cap_pp'])
                        if sh['near_reset_exempt'] else ''))


def _shortfall(w, info):
    """Why a window cannot take the task, or None."""
    need = _required(info)
    if info['available_pp'] <= EPS:
        return '%s 扣除预留后可派发额度为 0（预算 %.4g 点）' % (w, need)
    if info['available_pp'] + EPS >= need:
        return None
    sh = info.get('session_headroom')
    if sh and need > info['budget_pp'] + EPS:
        exempt = ''
        if sh['near_reset_exempt']:
            exempt = '，临近重置豁免后为 %.4g 点（距重置 %.1f 分钟×%.4g 点/分钟+%.4g）' % (
                sh['effective_pp'], info['minutes_to_reset'], sh['burn_pp_per_min'], sh['floor_pp'])
        return ('%s 余量不足：可用 %.4g 点 < 所需 %.4g 点（5 小时余量门槛 %.4g 点，依据 session_headroom.%s%s；'
                '任务预算 %.4g 点）' % (w, info['available_pp'], need, sh['base_pp'], sh['basis'], exempt,
                                    info['budget_pp']))
    return '%s 可派发 %.4g 点 < 预算 %.4g 点' % (w, info['available_pp'], need)


def codex_log_roots(active_account):
    """Session-log roots for the active account; identity is used only to build the path."""
    roots = []
    if isinstance(active_account, str) and ACCOUNT_ID.fullmatch(active_account):
        data = os.environ.get('ORCA_USER_DATA_PATH') or str(Path.home() / 'Library' / 'Application Support' / 'orca')
        roots.append(Path(data) / 'codex-accounts' / active_account / 'home' / 'sessions')
    elif active_account is not None:
        return []
    roots.append(Path.home() / '.codex' / 'sessions')
    return roots


def _latest_rollouts(root, limit):
    found = []
    for folder, _, names in os.walk(root):
        for name in names:
            if name.startswith('rollout-') and name.endswith('.jsonl'):
                path = os.path.join(folder, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    found.append((info.st_mtime, path))
    return [path for _, path in heapq.nlargest(limit, found)]


def _tail_lines_newest_first(path, tail_bytes):
    with open(path, 'rb') as fh:
        size = fh.seek(0, os.SEEK_END)
        start = max(0, size - tail_bytes)
        fh.seek(start)
        # A leading fragment of a longer line, like a half-written last line, fails JSON parsing.
        return reversed(fh.read(tail_bytes).split(b'\n'))


def _parse_rate_event(line, pol):
    """Numbers from one Codex token_count event, or None. Nothing else leaves this function."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    payload = event.get('payload') if isinstance(event, dict) else None
    if not isinstance(payload, dict) or event.get('type') != 'event_msg' or payload.get('type') != 'token_count':
        return None
    limits = payload.get('rate_limits')
    stamp = event.get('timestamp')
    if not isinstance(limits, dict) or limits.get('limit_id') != CODEX_LIMIT_ID or not isinstance(stamp, str):
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    parsed = dict(updated_ms=int(moment.timestamp() * 1000))
    for wname, field in LOG_WINDOWS:
        win = limits.get(field)
        if not isinstance(win, dict):
            return None
        used, mins, reset = win.get('used_percent'), win.get('window_minutes'), win.get('resets_at')
        if (not _num(used) or not 0 <= used <= 100 or not _num(reset) or reset <= 0
                or mins != pol['windows'][wname]['expected']):
            return None
        parsed[wname] = dict(usedPercent=used, windowMinutes=mins, resetsAt=int(reset * 1000))
    return parsed


def _same_account(event, reference, tolerance_ms):
    # Shared sessions may contain other accounts; only weekly reset identifies the pool.
    win = reference.get('weekly')
    reset = win.get('resetsAt') if isinstance(win, dict) else None
    return _num(reset) and abs(event['weekly']['resetsAt'] - reset) <= tolerance_ms


def read_codex_rate_limits(roots, reference, pol, now_ms, trusted_root=None):
    """Newest verified rate_limits across the most recently modified rollouts.

    Returns (event|None, status). Only tails are read and only lines naming
    rate_limits are parsed; events from another account or limit are skipped.
    """
    cfg = pol['session_log']
    needle, tolerance_ms = b'"rate_limits"', cfg['tolerance'] * 1000
    newest, saw_file, saw_other_account = None, False, False
    for root in roots:
        for path in _latest_rollouts(root, cfg['max_files']):
            saw_file = True
            try:
                # Backfilled/resumed shared rollouts are hardlinked into the account home.
                # The directory alone cannot attest later events after a login change.
                private_file = root == trusted_root and os.stat(path).st_nlink == 1
                lines = _tail_lines_newest_first(path, cfg['tail_bytes'])
            except OSError:
                continue
            for line in lines:
                if needle not in line or len(line) > cfg['max_line_bytes']:
                    continue
                event = _parse_rate_event(line, pol)
                if event is None or event['updated_ms'] > now_ms + pol['skew'] * 1000:
                    continue
                if not private_file and not _same_account(event, reference, tolerance_ms):
                    saw_other_account = True
                    continue
                if newest is None or event['updated_ms'] > newest['updated_ms']:
                    newest = event
                break
    if newest:
        return newest, 'ok'
    return None, 'account_mismatch' if saw_other_account else 'no_event' if saw_file else 'no_logs'


def merge_codex_log(entry, account, pol, now_ms, roots=None):
    """Replace Orca's windows and updatedAt together when a verified log event is newer."""
    entry['data_source'] = SOURCE_ORCA
    entry['log_status'] = 'disabled'
    if not pol['session_log']['enabled']:
        return
    if not isinstance(account, dict) or 'activeAccountId' not in account:
        entry['log_status'] = 'no_account'
        return
    try:
        account_id = account['activeAccountId']
        account_roots = codex_log_roots(account_id)
        trusted_root = account_roots[0] if isinstance(account_id, str) and ACCOUNT_ID.fullmatch(account_id) else None
        event, status = read_codex_rate_limits(
            account_roots if roots is None else roots, entry, pol, now_ms, trusted_root)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        entry['log_status'] = 'error'
        return
    entry['log_status'] = status
    if event is None:
        return
    orca_updated = entry.get('updatedAt')
    # A failure past Orca's retention clears the windows and stamps updatedAt=now: no reading to be older than.
    wiped = entry.get('status') == 'error' and not all(isinstance(entry.get(w), dict) for w in WINDOWS)
    if not wiped and _num(orca_updated) and event['updated_ms'] <= orca_updated:
        entry['log_status'] = 'older_than_orca'
        return
    entry['orca_updated_at'] = orca_updated
    entry['updatedAt'] = event['updated_ms']
    for wname, _ in LOG_WINDOWS:
        entry[wname] = event[wname]
    entry['data_source'] = SOURCE_CODEX_LOG


def log_covers_orca_error(prov, pol, now_ms):
    """Orca's fetch failed, but this account's own session log is live, so the numbers are still known.

    data_source only says the log is newer and proven to be this account's; age is checked here,
    so a log that went quiet falls back to unknown. The guard and plan both judge through this.
    """
    if not isinstance(prov, dict) or prov.get('status') != 'error' or prov.get('data_source') != SOURCE_CODEX_LOG:
        return False
    updated = prov.get('updatedAt')
    return _num(updated) and -pol['skew'] <= (now_ms - updated) / 1000.0 <= pol['stale']


def fetch_snapshot(pol, runner=None, clock=None, log_roots=None):
    """One read-only, time-bounded query. Failure becomes a reason, never a number."""
    runner, clock = runner or subprocess.run, clock or time.time
    queried = int(clock() * 1000)
    snap = dict(ok=False, reason=None, queried_at_ms=queried, providers={},
                source='orca account list --json')
    try:
        proc = runner(pol['command'], capture_output=True, timeout=pol['timeout'],
                      stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        snap['reason'] = '额度查询超时（%gs）' % pol['timeout']
        return snap
    except OSError:
        snap['reason'] = '额度查询无法执行'
        return snap
    if proc.returncode != 0:
        snap['reason'] = '额度查询失败（退出码 %s）' % proc.returncode
        return snap
    raw = proc.stdout or b''
    if isinstance(raw, str):
        raw = raw.encode()
    if len(raw) > pol['max_bytes']:
        snap['reason'] = '额度查询输出过大'
        return snap
    try:
        result = json.loads(raw)['result']
        rate = result['rateLimits']
        if not isinstance(rate, dict):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        snap['reason'] = '额度查询输出缺少 result.rateLimits 或不是有效 JSON'
        return snap
    for key in set(pol['provider_keys'].values()):
        entry = rate.get(key)
        if not isinstance(entry, dict):
            continue
        kept = dict(status=entry.get('status'), updatedAt=entry.get('updatedAt'))
        # Two facts from usageMetadata, nothing else: a forced refresh ignores Orca's own retry-after,
        # and the failure kind tells a passing outage from a signed-out account.
        meta = entry.get('usageMetadata')
        if isinstance(meta, dict):
            if _num(meta.get('retryAtMs')):
                kept['retry_at_ms'] = meta['retryAtMs']
            if meta.get('failureKind') is not None:
                kept['failure_kind'] = _safe(meta['failureKind'])
        for w in WINDOWS:
            win = entry.get(w)
            if key == 'antigravity':
                # Current Antigravity profiles route Gemini models. Aggregate
                # weekly may describe the separate Claude/GPT pool instead.
                # Missing/ambiguous Gemini buckets remain unknown, never borrow
                # capacity from another model group or from aggregate fields.
                buckets = entry.get('buckets')
                matches = [b for b in buckets
                           if isinstance(b, dict)
                           and b.get('group') == 'Gemini Models'
                           and b.get('windowMinutes') == pol['windows'][w]['expected']
                           and not isinstance(b.get('windowMinutes'), bool)] if isinstance(buckets, list) else []
                win = matches[0] if len(matches) == 1 else None
                kept['quota_group'] = 'Gemini Models'
            kept[w] = ({k: win.get(k) for k in ('usedPercent', 'windowMinutes', 'resetsAt')}
                       if isinstance(win, dict) else None)
        for cap in CAP_WINDOWS:
            win = entry.get(cap)
            kept[cap] = ({k: win.get(k) for k in ('usedPercent', 'windowMinutes', 'resetsAt')}
                         if isinstance(win, dict) else None)
        if key == CODEX_LIMIT_ID:
            merge_codex_log(kept, result.get('codex'), pol, int(clock() * 1000), log_roots)
        # Tells whose numbers a remembered reading holds; the identity itself is not kept.
        kept['account_digest'] = account_digest(result.get(key))
        snap['providers'][key] = kept
    snap['ok'] = True
    return snap


def _digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:16]


def account_digest(account):
    """Whose numbers these are, or None when that cannot be told.

    A null activeAccountId means the system default login, which can change under the same null;
    an organization alone is shared by several people, so the email is required there.
    """
    if not isinstance(account, dict):
        return None
    active = account.get('activeAccountId')
    if active not in (None, ''):
        return _digest(active)
    email = account.get('systemDefaultEmail')
    if not isinstance(email, str) or not email:
        return None
    return _digest('system-default\0%s\0%s' % (email, account.get('systemDefaultOrganizationUuid') or ''))


def routing_state_dir():
    """Where the cross-process state lives; ORCA_ROUTING_STATE_DIR isolates tests and copies."""
    override = os.environ.get('ORCA_ROUTING_STATE_DIR')
    return Path(override) if override else Path.home() / '.config' / 'orca-model-routing'


def _write_state(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(json.dumps(data, ensure_ascii=False))
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


def _read_state(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _bounded_flock(fd, seconds):
    """Wait at most this long; a holder that dies releases the lock with its process."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _orca_user_data():
    return os.environ.get('ORCA_USER_DATA_PATH') or str(Path.home() / 'Library' / 'Application Support' / 'orca')


def _call_accounts_refresh(timeout):
    """One accounts.list {refreshUsage:true}; returns an outcome word and discards the reply.

    Exception text is never used: the token must not be able to reach an output.
    """
    deadline = time.monotonic() + timeout
    sock = None
    try:
        meta = json.loads((Path(_orca_user_data()) / ORCA_RUNTIME_FILE).read_text())
        endpoint = next(t['endpoint'] for t in meta['transports'] if t.get('kind') == 'unix')
        request_id = str(uuid.uuid4())
        frame = json.dumps(dict(id=request_id, authToken=meta['authToken'], method='accounts.list',
                                params=dict(refreshUsage=True))) + '\n'
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(endpoint)
        sock.sendall(frame.encode())
        buffer, total = b'', 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:  # Keepalives alone must not outlive the deadline.
                return 'timeout'
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                return 'unavailable'
            total += len(chunk)
            if total > RPC_MAX_BYTES:
                return 'bad_response'
            buffer += chunk
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                if not line.strip():
                    continue
                reply = json.loads(line)
                if not isinstance(reply, dict) or '_keepalive' in reply:
                    continue
                if reply.get('id') != request_id:
                    return 'bad_response'
                return 'ok' if reply.get('ok') is True else 'rpc_error'
    except (TimeoutError, socket.timeout):
        return 'timeout'
    except (OSError, StopIteration):
        return 'unavailable'
    except (ValueError, KeyError, TypeError, AttributeError):
        return 'bad_response'
    finally:
        if sock is not None:
            sock.close()


def refresh_usage_rpc(pol, clock=None, call=None):
    """At most one forced refresh per min_interval across all processes. Never raises.

    The slot is taken before the request, so a failure or a crash still spaces the next one:
    a usage endpoint answering 429 must not be retried by every plan and guard. The lock is
    held through the request, which makes a concurrent caller wait (boundedly) and then read
    the fresh numbers instead of asking again.
    """
    clock, call = clock or time.time, call or _call_accounts_refresh
    cfg, begun = pol['refresh'], time.monotonic()
    fd = None
    try:
        root = routing_state_dir()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / REFRESH_STATE
        fd = os.open(root / REFRESH_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        if not _bounded_flock(fd, cfg['timeout']):
            return dict(attempted=False, outcome='busy')
        state, now = _read_state(path), clock()
        last = state.get('last_attempt_at')
        if _num(last) and 0 <= now - last < cfg['min_interval']:
            return dict(attempted=False, outcome='rate_limited', last_outcome=state.get('last_outcome'),
                        seconds_until_allowed=round(cfg['min_interval'] - (now - last), 1))
        _write_state(path, dict(last_attempt_at=now, last_outcome='in_flight'))
        try:
            outcome = call(max(0.001, cfg['timeout'] - (time.monotonic() - begun)))
        except Exception:
            outcome = 'error'
        try:
            _write_state(path, dict(last_attempt_at=now, last_outcome=outcome))
        except OSError:
            pass  # The in_flight claim on disk still spaces the next attempt.
        return dict(attempted=True, outcome=outcome)
    except OSError:
        return dict(attempted=False, outcome='state_error')
    finally:
        # Closing releases the lock; a long-lived guard must not keep it past this call.
        if fd is not None:
            os.close(fd)


def _stale_keys(snap, keys, pol, now_ms):
    """Providers among these past stale_after_seconds or in status=error. Nothing else may trigger a refresh."""
    if not snap.get('ok'):
        return []
    stale, limit = [], pol['stale'] * 1000
    for key in keys:
        prov = snap['providers'].get(key)
        if prov is not None and prov.get('status') == 'fetching':
            continue  # Someone is already fetching; asking again helps nobody.
        updated = prov.get('updatedAt') if prov else None
        if not _num(updated) or updated <= 0 or now_ms - updated > limit:
            stale.append(key)
            continue
        # A failed fetch can pass long before its data ages out. A live log needs nothing from Orca.
        if prov.get('status') == 'error' and not log_covers_orca_error(prov, pol, now_ms):
            stale.append(key)
    return stale


def _quality(prov):
    if not isinstance(prov, dict):
        return 0
    complete = all(isinstance(prov.get(w), dict) for w in WINDOWS)
    return 2 if complete and prov.get('status') == 'ok' else 1 if complete else 0


def _kept_after_refresh(before, after):
    """A failed refresh can turn Orca's ok into error or wipe the windows (see report); keep what was read before."""
    kept = []
    for key, old in before['providers'].items():
        new = after['providers'].get(key)
        if _quality(new) < _quality(old):
            failure = (new or {}).get('failure_kind')
            after['providers'][key] = dict(old, refresh_failure_kind=failure,
                                           refresh_status=(new or {}).get('status'))
            kept.append(key)
    return kept


def _wait_while_fetching(snap, keys, pol, runner, clock, sleep):
    """`fetching` means an answer is coming, not that quota is unknown; wait a bounded moment for it."""
    deadline = time.monotonic() + pol['refresh']['fetching_wait']
    def busy(current):
        return current.get('ok') and any((current['providers'].get(k) or {}).get('status') == 'fetching' for k in keys)
    while busy(snap) and time.monotonic() < deadline:
        sleep(min(1, max(0, deadline - time.monotonic())))
        newer = fetch_snapshot(pol, runner=runner, clock=clock)
        if not newer.get('ok'):
            break
        snap = newer
    return snap


def _usable_remembered(value, latest_ms):
    """A stored reading, or None for a damaged one or one from the future: either counts as no reading."""
    if isinstance(value, dict) and _num(value.get('updatedAt')) and value['updatedAt'] <= latest_ms:
        return value
    return None


def remember_good_reading(pol, snap):
    """Numbers only, for the guard's bounded takeover: a failed refresh may later wipe them from Orca."""
    cfg = pol['stale_takeover']
    if not cfg['enabled'] or not snap.get('ok'):
        return
    try:
        now_ms = snap['queried_at_ms'] if _num(snap.get('queried_at_ms')) else time.time() * 1000
        latest_ms = now_ms + pol['skew'] * 1000
        path = routing_state_dir() / LAST_GOOD_STATE
        stored, changed = _read_state(path), False
        for agent in cfg['agents']:
            key = pol['provider_keys'].get(agent)
            prior = _usable_remembered(stored.get(key), latest_ms)
            if prior is None and key in stored:
                del stored[key]  # Heals a damaged or poisoned file even if no new reading arrives.
                changed = True
            prov = snap['providers'].get(key)
            if (_quality(prov) == 2 and _num(prov.get('updatedAt')) and prov['updatedAt'] <= latest_ms
                    and prov.get('account_digest') and prov['updatedAt'] > (prior or {}).get('updatedAt', 0)):
                stored[key] = {k: prov.get(k) for k in ('updatedAt', 'account_digest') + WINDOWS + CAP_WINDOWS}
                changed = True
        if changed:
            _write_state(path, stored)
    except (OSError, TypeError, ValueError, AttributeError, KeyError):
        pass  # The takeover then simply has less to go on and stays closed.


def fresh_snapshot(pol, keys, runner=None, clock=None, refresher=None, sleep=None):
    """The shared read for plan, start and the guard. Refreshes only when one of these providers is stale.

    Everything here fails toward the original read: no refresher, a failed or rate-limited
    refresh, or an unreadable second read all leave the plain snapshot and its original judgment.
    """
    clock, sleep = clock or time.time, sleep or time.sleep
    snap = fetch_snapshot(pol, runner=runner, clock=clock)
    info = dict(attempted=False, outcome='disabled', stale_providers=[])
    snap['refresh'] = info
    if not pol['refresh']['enabled'] or refresher is None:
        return snap
    begun = time.monotonic()
    snap = _wait_while_fetching(snap, keys, pol, runner, clock, sleep)
    snap['refresh'] = info
    remember_good_reading(pol, snap)
    now_ms = int(clock() * 1000)
    stale = _stale_keys(snap, keys, pol, now_ms)
    info.update(stale_providers=stale, outcome='not_stale' if snap.get('ok') else 'query_failed')
    if not stale:
        return snap
    waiting = [p['retry_at_ms'] for p in snap['providers'].values() if _num(p.get('retry_at_ms')) and p['retry_at_ms'] > now_ms]
    if waiting:
        # Orca's force flag ignores its own retry-after, so honoring it is on us.
        info.update(outcome='retry_after_active', seconds_until_allowed=round((max(waiting) - now_ms) / 1000, 1))
        return snap
    try:
        info.update(refresher(pol, clock))
    except Exception:
        info.update(attempted=False, outcome='error')
    if info['outcome'] in ('state_error', 'error') and not info['attempted']:
        return snap
    after = fetch_snapshot(pol, runner=runner, clock=clock)
    after = _wait_while_fetching(after, keys, pol, runner, clock, sleep)
    info['elapsed_seconds'] = round(time.monotonic() - begun, 2)
    if not after.get('ok'):
        info['reread_failed'] = True
        return snap
    advanced = {k: _advanced(snap, after, k) for k in stale}
    info['kept_previous'] = _kept_after_refresh(snap, after)
    info['providers'] = {}
    for key in stale:
        prov = after['providers'].get(key) or {}
        info['providers'][key] = dict(status=prov.get('refresh_status') or prov.get('status'), advanced=advanced[key],
                                      failure_kind=prov.get('refresh_failure_kind') or prov.get('failure_kind'))
    after['refresh'] = info
    remember_good_reading(pol, after)
    return after


REFRESH_TEXT = dict(
    disabled='未启用', not_stale='数据未过期，未刷新', query_failed='额度查询失败，未刷新', ok='已请求刷新',
    timeout='超时（Orca 可能仍在刷新）', unavailable='连不上 Orca', rpc_error='Orca 拒绝', bad_response='Orca 回复异常',
    error='内部错误', busy='另一进程正在刷新', state_error='限频状态不可写，未刷新', rate_limited='限频中，未发请求',
    retry_after_active='Orca 正在退避（429），未发请求')


def describe_refresh(info):
    """One line for an alert: what the refresh did and what each stale provider ended up as."""
    info = info or {}
    text = REFRESH_TEXT.get(info.get('outcome'), str(info.get('outcome')))
    if info.get('seconds_until_allowed') is not None:
        text += '（%.0f 秒后可再试）' % info['seconds_until_allowed']
    for key, prov in (info.get('providers') or {}).items():
        failed = prov.get('status') not in (None, 'ok')
        if failed or not prov.get('advanced'):
            text += '；%s %s（状态 %s%s）' % (key, '已更新' if prov.get('advanced') else '未更新', _safe(prov.get('status')),
                                           '，' + _safe(prov['failure_kind']) if prov.get('failure_kind') else '')
    return text


def _advanced(before, after, key):
    old, new = (before['providers'].get(key) or {}).get('updatedAt'), (after['providers'].get(key) or {}).get('updatedAt')
    # A wipe stamps updatedAt=now without bringing any numbers: that is not an advance.
    complete = all(isinstance((after['providers'].get(key) or {}).get(w), dict) for w in WINDOWS)
    return complete and _num(new) and (not _num(old) or new > old)


def _window(pol, wname, win, now_ms, reserve, factor, budget, horizon=None):
    """Validate one window. Returns (info, reason); reason means unknown."""
    spec = pol['windows'][wname]
    if not isinstance(win, dict):
        return None, '%s 窗口缺失' % wname
    used, mins, reset = win.get('usedPercent'), win.get('windowMinutes'), win.get('resetsAt')
    if not _num(used) or not 0 <= used <= 100:
        return None, '%s usedPercent 异常' % wname
    if not _num(mins) or mins != spec['expected']:
        return None, '%s 窗口长度不匹配（需 %d 分钟）' % (wname, spec['expected'])
    if not _num(reset) or reset <= 0:
        return None, '%s resetsAt 缺失或异常' % wname
    to_reset = (reset - now_ms) / 60000.0
    if to_reset <= 0:
        return None, '%s resetsAt 已过去' % wname
    if to_reset > mins + pol['skew'] / 60.0:
        return None, '%s resetsAt 超出窗口长度' % wname
    remaining = 100.0 - used
    configured_reserve = reserve
    if pol['reserve_scaling'] == 'prorated':
        # A reserve covers the coordinator for a full window; only the share until reset still needs protecting.
        reserve = reserve * min(1.0, to_reset / mins)
    available = max(0.0, remaining - reserve)
    # A provider that habitually resets early is scored as if its reset were no further than the horizon.
    scoring_minutes = max(min(to_reset, horizon) if horizon else to_reset, spec['floor'])
    raw = factor * available / scoring_minutes
    pool_remaining = 100.0 - reserve
    pace_ratio = (None if pool_remaining <= EPS else
                  (available / pool_remaining) / (scoring_minutes / spec['expected']))
    available_per_day = available / (to_reset / 1440.0)
    return dict(remaining_pp=round(remaining, 4), reserve_pp=round(reserve, 4),
                reserve_configured_pp=configured_reserve, available_pp=round(available, 4),
                budget_pp=budget[wname], minutes_to_reset=round(to_reset, 2),
                scoring_minutes=round(scoring_minutes, 2), reset_horizon_minutes=horizon,
                window_minutes=int(mins), time_floor_minutes=spec['floor'],
                weight=spec['weight'], raw=raw,
                pace_ratio=None if pace_ratio is None else round(pace_ratio, 4),
                available_pp_per_day=round(available_per_day, 4)), None


def _takeover_reading(prov, key, pol, now_ms):
    """(reading, source, refusal) for a stale takeover. Fails closed on anything but a passing outage."""
    # What Orca says now, even when a failed refresh left the earlier reading in this snapshot.
    status = prov.get('refresh_status') or prov.get('status')
    failure = prov.get('refresh_failure_kind') or prov.get('failure_kind')
    if status not in ('ok', 'error'):
        return None, None, '%s 额度状态 %s，不能接手' % (key, _safe(status))
    if status == 'error' and failure not in TRANSIENT_FAILURES:
        return None, None, '%s 失败类型 %s 不属于暂时故障，不能接手' % (key, _safe(failure))
    readings = []
    if all(isinstance(prov.get(w), dict) for w in WINDOWS) and _num(prov.get('updatedAt')):
        readings.append((prov, SOURCE_ORCA))
    remembered = _usable_remembered(_read_state(routing_state_dir() / LAST_GOOD_STATE).get(key),
                                    now_ms + pol['skew'] * 1000)
    # Not knowing whose numbers these are is the same as knowing they are someone else's.
    if (remembered is not None and prov.get('account_digest')
            and remembered.get('account_digest') == prov.get('account_digest')):
        readings.append((remembered, 'last_good_reading'))
    if not readings:
        return None, None, '%s 没有可用的历史读数' % key
    reading, source = max(readings, key=lambda item: item[0]['updatedAt'])
    return reading, source, None


def _takeover_judgement(agent, profile, pol, prov, key, budget, now_ms, margin_minutes, headroom=None):
    """(details, refusal): the one judgement for bounded stale numbers, as they are now or margin_minutes from now.

    Conservative remaining = remaining - rate * data age. After the reserve it must still cover the task
    budget. Past resets, excess age or missing data stay unknown: fail closed.
    """
    cfg, factor = pol['stale_takeover'], pol['factors'].get(agent)
    if factor is None or not isinstance(prov, dict):
        return None, '%s 数据缺失' % key
    reading, source, why = _takeover_reading(prov, key, pol, now_ms)
    if why:
        return None, why
    age = (now_ms - reading['updatedAt']) / 1000.0
    if age < -pol['skew']:
        return None, '%s updatedAt 在未来' % key
    # Inside the skew a reading is simply new: a clock difference must not add quota.
    age = max(0.0, age)
    charged = age + margin_minutes * 60
    if charged > cfg['max_age'] + EPS:
        return None, '%s 数据年龄 %.0f 秒（含交接余量 %.0f 秒）超过上限 %.0f 秒' % (key, age, margin_minutes * 60, cfg['max_age'])
    scaled = {w: v * profile.get('budget_multiplier', 1) for w, v in budget.items()}
    reserves = pol['reserves'].get(agent, {})
    windows, caps, charges = {}, {}, {}
    cap = profile.get('quota_cap_window')
    for name, wname, reserve, target in ([(w, w, reserves.get(w, 0.0), windows) for w in WINDOWS] +
                                         ([(cap, 'weekly', 0.0, caps)] if cap else [])):
        info, bad = _window(pol, wname, reading.get(name), now_ms, reserve, factor, scaled,
                            pol['horizons'].get(agent, {}).get(wname) if name in WINDOWS else None)
        if bad:
            return None, '%s: %s' % (name, bad)
        if margin_minutes and info['minutes_to_reset'] <= margin_minutes:
            return None, _reset_within_margin(name, info, margin_minutes)
        if name == 'session':
            apply_session_headroom(info, headroom, pol)
        deducted = cfg['burn'][wname] * charged / 60.0
        conservative = info['remaining_pp'] - deducted
        available = conservative - info['reserve_pp']
        if available <= EPS or available + EPS < _required(info):
            return None, ('%s 保守剩余 %.4g 点（剩余 %.4g − 按 %.4g 点/分钟扣 %.4g）扣预留 %.4g 后不足%s %.4g'
                          % (name, conservative, info['remaining_pp'], cfg['burn'][wname], deducted,
                             info['reserve_pp'], '所需（含 5 小时余量门槛）' if 'required_pp' in info else '预算',
                             _required(info)))
        info['available_pp'] = round(available, 4)
        note_headroom_verdict(info)
        info['raw'] = factor * available / info['scoring_minutes']
        target[name] = info
        charges[name] = dict(remaining_pp=info['remaining_pp'], deducted_pp=round(deducted, 4),
                             conservative_remaining_pp=round(conservative, 4), reserve_pp=info['reserve_pp'],
                             budget_pp=info['budget_pp'], required_pp=_required(info))
    return dict(windows=windows, caps=caps, source=source, updated_ms=int(reading['updatedAt']), age=age, charges=charges), None


def _reset_within_margin(name, info, margin_minutes):
    return '%s 距重置 %.1f 分钟，不超过交接余量 %g 分钟；窗口重置后这份读数作废，start 会拒绝' % (
        name, info['minutes_to_reset'], margin_minutes)


def apply_stale_takeover(cand, profile, pol, snap, budget, now_ms, margin_minutes=0.0, headroom=None):
    """Let a guard's own takeover use bounded stale numbers, charged at a conservative burn rate.

    margin_minutes charges the handoff still ahead of a plan, so a plan that passes is not refused by its start.
    """
    cfg, agent = pol['stale_takeover'], cand['agent']
    if not cfg['enabled'] or cand['status'] != 'unknown' or agent not in cfg['agents'] or not snap.get('ok'):
        return cand
    key = pol['provider_keys'].get(agent)
    details, why = _takeover_judgement(agent, profile, pol, snap['providers'].get(key), key, budget, now_ms,
                                       margin_minutes, headroom)
    if why:
        cand['stale_takeover_refused'] = why
        cand['reasons'] = cand['reasons'] + ['守卫接手不可用：' + why]
        return cand
    age, source = details['age'], details['source']
    cand.update(status='eligible', windows=details['windows'], caps=details['caps'], data_source=source,
                data_updated_at_ms=details['updated_ms'], data_age_seconds=round(age, 1),
                reasons=[r for r in cand['reasons'] if r not in cand.get('unknown_reasons', ())],
                stale_takeover=dict(age_seconds=round(age, 1), age_minutes=round(age / 60, 1),
                                    plan_margin_minutes=margin_minutes, max_age_seconds=cfg['max_age'],
                                    source=source, charges=details['charges']))
    return cand


def apply_handoff_margin(cand, profile, pol, snap, budget, now_ms, margin_minutes, headroom=None):
    """A candidate that passes now, but whose start comes margin_minutes later: would that start still pass?

    Only for a pool whose unknown quota blocks a start; elsewhere a start cannot be refused for this.
    A window resetting within the margin is refused rather than treated as "reset, so usable": the reading
    says nothing about the next window, and a changed resetsAt cannot tell a reset from another account.
    The worker keeps running and the next poll looks again, so this costs minutes, never a stopped worker.
    """
    cfg, agent = pol['stale_takeover'], cand['agent']
    holds_reserve = any(v > 0 for v in pol['reserves'].get(agent, {}).values())
    if (not cfg['enabled'] or not margin_minutes or cand['status'] != 'eligible' or cand.get('stale_takeover')
            or not holds_reserve or not snap.get('ok')):
        return cand

    def refuse(why):
        cand.update(status='unknown', stale_takeover_refused=why,
                    reasons=cand['reasons'] + ['守卫接手不可用：' + why])
        return cand

    for name, info in list(cand['windows'].items()) + list(cand['caps'].items()):
        if info['minutes_to_reset'] <= margin_minutes:
            return refuse(_reset_within_margin(name, info, margin_minutes))
    if cand['data_age_seconds'] + margin_minutes * 60 <= pol['stale']:
        return cand
    key = pol['provider_keys'].get(agent)
    if agent not in cfg['agents']:
        return refuse('%s 数据加交接余量 %g 分钟后会过期，且该账户池不适用有界过期接手' % (key, margin_minutes))
    details, why = _takeover_judgement(agent, profile, pol, snap['providers'].get(key), key, budget, now_ms,
                                       margin_minutes, headroom)
    if why:
        return refuse(why)
    cand['stale_takeover_expected'] = dict(age_seconds=round(details['age'], 1), plan_margin_minutes=margin_minutes,
                                           source=details['source'], charges=details['charges'])
    return cand


def evaluate_candidate(name, profile, role, pol, snap, budget, now_ms, task_rule=None, kind=None, complexity=None):
    # Models that burn the shared pool faster (Fable) need a proportionally larger budget.
    budget = {w: v * profile.get('budget_multiplier', 1) for w, v in budget.items()}
    cand = dict(profile=name, agent=profile.get('agent'), model=profile.get('model'), role=role,
                status=None, reasons=[], auto_selectable=True, windows={}, caps={}, score=None,
                capacity_factor=None, data_updated_at_ms=None, data_age_seconds=None, data_source=None)
    if not profile.get('enabled') or not profile.get('model'):
        cand.update(status='disabled', reasons=[profile.get('blocked_reason') or '候选已禁用，不参与比较'])
        return cand
    if (role == 'alternate' and task_rule is not None
            and 'alternate_capability_confirmed' in task_rule
            and not task_rule['alternate_capability_confirmed']):
        cand['auto_selectable'] = False
        cand['reasons'].append('备选所需工具/账户能力未独立确认；额度不足或占优都不能自动选用')
    agent = profile['agent']
    factor = pol['factors'].get(agent)
    key = pol['provider_keys'].get(agent)
    cand['capacity_factor'] = factor
    unknown = []
    if factor is None or key is None:
        unknown.append('配置缺少 %s 的 capacity_factor 或 provider_keys' % agent)
    elif not snap['ok']:
        unknown.append(snap['reason'])
    elif key not in snap['providers']:
        unknown.append('额度数据缺少 %s' % key)
    else:
        prov = snap['providers'][key]
        status, upd = prov.get('status'), prov.get('updatedAt')
        cand['data_source'] = prov.get('data_source', SOURCE_ORCA)
        if log_covers_orca_error(prov, pol, now_ms):
            cand['orca_error_covered_by_log'] = dict(orca_status=_safe(status), failure_kind=prov.get('failure_kind'))
        elif status != 'ok':
            failure = prov.get('failure_kind')
            unknown.append('%s 额度状态不是 ok（%s%s）' % (key, _safe(status), '，' + _safe(failure) if failure else ''))
        if not _num(upd) or upd <= 0:
            unknown.append('%s updatedAt 缺失或异常' % key)
        else:
            age = (now_ms - upd) / 1000.0
            cand['data_updated_at_ms'], cand['data_age_seconds'] = int(upd), round(age, 1)
            if age < -pol['skew']:
                unknown.append('%s updatedAt 在未来（%.0fs）' % (key, -age))
            elif age > pol['stale']:
                unknown.append('%s 额度数据过期（%.0fs > %.0fs）' % (key, age, pol['stale']))
        reserves = pol['reserves'].get(agent, {})
        for w in WINDOWS:
            info, why = _window(pol, w, prov.get(w), now_ms, reserves.get(w, 0.0), factor, budget,
                                pol['horizons'].get(agent, {}).get(w))
            if why:
                unknown.append(why)
            else:
                if w == 'session':
                    apply_session_headroom(info, session_headroom_for(pol, agent, kind, complexity), pol)
                cand['windows'][w] = info
        cap = profile.get('quota_cap_window')
        if cap:
            # The cap shares the weekly window length; no reserve, same (multiplied) weekly budget.
            info, why = _window(pol, 'weekly', prov.get(cap), now_ms, 0.0, factor, budget)
            if why:
                unknown.append('%s: %s' % (cap, why))
            else:
                cand['caps'][cap] = info
    if unknown:
        cand.update(status='unknown', reasons=cand['reasons'] + unknown, unknown_reasons=unknown)
        return cand
    note_headroom_verdict(cand['windows'].get('session'))
    short = [why for why in (_shortfall(w, i) for w, i in list(cand['windows'].items()) + list(cand['caps'].items()))
             if why]
    if short:
        cand.update(status='insufficient', reasons=cand['reasons'] + short)
    else:
        cand['status'] = 'eligible'
    return cand


def score_candidates(cands, pol):
    """Normalize each window over eligible candidates, then weight. Others get no score."""
    eligible = [c for c in cands if c['status'] == 'eligible']
    wsum = sum(v['weight'] for v in pol['windows'].values())
    for w in WINDOWS:
        total = sum(c['windows'][w]['raw'] for c in eligible)
        for c in eligible:
            info = c['windows'][w]
            info['normalized'] = (info['raw'] / total) if total > 0 else 0.0
    for c in eligible:
        c['score'] = sum(c['windows'][w]['weight'] * c['windows'][w]['normalized'] for w in WINDOWS) / wsum
    for c in cands:
        for info in c['windows'].values():
            info['raw'] = round(info['raw'], 6)
            if 'normalized' in info:
                info['normalized'] = round(info['normalized'], 6)
        if c['score'] is not None:
            c['score'] = round(c['score'], 6)


def primary_first_threshold(pf, agent):
    """(min weekly pace_ratio, which setting) for a primary in this pool: a per-pool value beats the default."""
    by_pool = pf.get('min_weekly_pace_ratio_by_pool') or {}
    if agent in by_pool:
        return by_pool[agent], 'min_weekly_pace_ratio_by_pool.%s' % agent
    return pf.get('min_weekly_pace_ratio', 1.0), 'min_weekly_pace_ratio'


def resolve(cands, pol, prefer_primary=False):
    """Pick among [primary, alternate?] candidates. Returns dict(selected|blocked)."""
    primary = cands[0]
    alt = cands[1] if len(cands) > 1 else None
    holds_reserve = lambda c: any(v > 0 for v in pol['reserves'].get(c['agent'], {}).values())
    alt_ok = alt is not None and alt['status'] == 'eligible' and alt['auto_selectable']

    def blocked(code, why):
        return dict(selected=None, blocked_code=code, blocked_reason=why, reason=why, warnings=[])

    def pick(c, why, warnings=()):
        return dict(selected=c['profile'], blocked_code=None, blocked_reason=None, reason=why,
                    warnings=list(warnings))

    def detail(*items):
        return '；'.join('%s[%s]: %s' % (c['profile'], c['status'], '，'.join(c['reasons']) or '-')
                         for c in items if c is not None)

    st = primary['status']
    if st == 'eligible':
        if alt is None:
            return pick(primary, '唯一候选额度合格')
        if alt_ok:
            pf = pol.get('primary_first') or {}
            pace = (primary['windows'].get('weekly') or {}).get('pace_ratio')
            threshold, basis = primary_first_threshold(pf, primary['agent'])
            if (not prefer_primary and pf.get('enabled') and pace is not None and pace >= threshold):
                # Both pools usable and the primary's pool is not ahead of schedule: route by capability.
                return pick(primary, '主选优先：主选额度合格且 weekly pace_ratio %.2f ≥ %s（%s；主选 %.4f，备选 %.4f 仅供参考）'
                            % (pace, threshold, basis, primary['score'], alt['score']))
            if prefer_primary:
                # Task opts out of score comparison: the alternate only covers a primary that cannot run.
                return pick(primary, '任务配置 prefer_primary：主选额度合格即选用（主选 %.4f，备选 %.4f 仅供参考）'
                            % (primary['score'], alt['score']))
            if alt['score'] > primary['score'] + EPS:
                return pick(alt, '首次额度选择：备选综合分 %.4f > 主选 %.4f（非失败重试）' % (alt['score'], primary['score']))
            why = ('同分保主（%.4f）' % primary['score'] if abs(alt['score'] - primary['score']) <= EPS
                   else '主选综合分 %.4f ≥ 备选 %.4f' % (primary['score'], alt['score']))
            return pick(primary, why)
        warn = []
        if alt['status'] == 'unknown':
            warn.append('备选额度未知，比较不完整，保持主选：' + detail(alt))
        elif not alt['auto_selectable']:
            warn.append('备选不可自动选用：' + detail(alt))
        return pick(primary, '主选额度合格；备选未参与比较', warn)
    if st == 'insufficient':
        if alt_ok:
            return pick(alt, '首次额度选择：主选已知额度不足，备选合格（非失败重试）：' + detail(primary))
        return blocked('quota_insufficient' if alt is None or alt['status'] in ('insufficient', 'disabled')
                       else ('capability_unconfirmed' if alt['status'] == 'eligible' else 'quota_unknown'),
                       '主选额度不足且无可自动选用的备选，交回协调者：' + detail(primary, alt))
    if st == 'disabled':
        if alt_ok:
            return pick(alt, '主选已在配置中禁用，不参与；备选额度合格：' + detail(primary))
        return blocked('no_candidate', '主选已禁用且备选不可用，交回协调者：' + detail(primary, alt))
    # primary unknown: alternate never wins by default.
    if holds_reserve(primary):
        return blocked('quota_unknown',
                       '%s 属于带调度预留的共享池，额度未知无法保障预留；备选不会因此自动胜出，交回协调者：%s'
                       % (primary['agent'], detail(primary)))
    if pol['unknown_primary'] == 'block':
        return blocked('quota_unknown', '主选额度未知，策略为阻断：' + detail(primary))
    return pick(primary, '主选额度未知但该账户无调度预留，按原路由保持主选（未经额度校验）',
                ['额度未校验：' + detail(primary)])


def explicit_resolve(cand, pol):
    st = cand['status']
    if st == 'eligible' and not cand['auto_selectable']:
        return dict(selected=None, blocked_code='capability_unconfirmed', reason='; '.join(cand['reasons']),
                    blocked_reason='显式指定的 %s 所需工具/账户能力未独立确认，额度合格也不能启动：%s'
                    % (cand['profile'], '；'.join(cand['reasons'])), warnings=[])
    if st == 'eligible':
        return dict(selected=cand['profile'], blocked_code=None, blocked_reason=None,
                    reason='用户显式指定；仍通过预留与预算检查', warnings=[])
    why = '；'.join(cand['reasons']) or st
    if st == 'unknown' and not any(v > 0 for v in pol['reserves'].get(cand['agent'], {}).values()):
        if pol['unknown_primary'] == 'allow':
            return dict(selected=cand['profile'], blocked_code=None, blocked_reason=None,
                        reason='用户显式指定；该账户无调度预留，额度未校验', warnings=['额度未校验：' + why])
    code = {'unknown': 'quota_unknown', 'insufficient': 'quota_insufficient'}.get(st, 'no_candidate')
    return dict(selected=None, blocked_code=code,
                blocked_reason='显式指定的 %s 不能绕过调度预留/预算：%s' % (cand['profile'], why),
                reason=why, warnings=[])


def select(config, kind, complexity, budget_override=None, explicit_profile=None,
           runner=None, clock=None, refresher=None, stale_takeover=None):
    """Evaluate candidates for a task. Reads policy fresh; never launches anything.

    refresher: the bounded refresh, or None for a plain read. stale_takeover ('plan'/'recheck'/'start'): passed only
    by route.py after verifying a live guard is the caller; nothing else widens the unknown-quota rules.
    """
    pol = validate_policy(config.get('quota_policy'))
    rule = config['tasks'][kind]
    if explicit_profile:
        allowed = (rule['primary'], rule['alternate'], rule.get('escalation'))
        if explicit_profile not in allowed:
            raise ValueError('--profile 只能是该任务的 primary(%s)、alternate(%s) 或 escalation(%s)，不能扩展候选。'
                             % (rule['primary'], rule['alternate'], rule.get('escalation')))
        names = [(explicit_profile, 'primary' if explicit_profile == rule['primary'] else
                  'alternate' if explicit_profile == rule['alternate'] else 'escalation')]
    else:
        names = [(rule['primary'], 'primary'), (rule['alternate'], 'alternate')]
    result = dict(policy_enabled=pol['enabled'], candidates=[], source=None, selection=None)
    if not pol['enabled']:
        result['selection'] = dict(selected=names[0][0], blocked_code=None, blocked_reason=None,
                                   reason='quota_policy.enabled=false，按原固定路由', warnings=['额度策略已关闭'])
        return result
    budget = {w: v for w, v in pol['budgets'][complexity].items()}
    for w, v in (budget_override or {}).items():
        if v is not None:
            budget[w] = _nonneg(v, 'budget.' + w, 100)
    clock = clock or time.time
    keys = list(dict.fromkeys(pol['provider_keys'].get(config['models'][n].get('agent')) for n, _ in names
                              if config['models'][n].get('enabled') and config['models'][n].get('model')))
    snap = fresh_snapshot(pol, [k for k in keys if k], runner=runner, clock=clock, refresher=refresher)
    now_ms = int(clock() * 1000)  # evaluated after the query returns, so a reset passed meanwhile is caught
    cands = [evaluate_candidate(n, config['models'][n], role, pol, snap, budget, now_ms, rule, kind, complexity)
             for n, role in names]
    if stale_takeover is not None and explicit_profile:
        # A plan has the whole handoff ahead of it, a recheck only the last steps; a start is the moment itself.
        margin = dict(plan=pol['stale_takeover']['margin'], recheck=pol['stale_takeover']['recheck_margin']).get(stale_takeover, 0.0)
        headroom = session_headroom_for(pol, config['models'][explicit_profile].get('agent'), kind, complexity)
        apply_stale_takeover(cands[0], config['models'][explicit_profile], pol, snap, budget, now_ms, margin, headroom)
        apply_handoff_margin(cands[0], config['models'][explicit_profile], pol, snap, budget, now_ms, margin, headroom)
    score_candidates(cands, pol)
    result['candidates'] = cands
    result['budget_points'] = budget
    result['source'] = dict(command='orca account list --json (read-only); Codex 另读当前账户会话日志的 rate_limits', queried_at_ms=now_ms,
                            query_started_at_ms=snap['queried_at_ms'],
                            ok=snap['ok'], failure=snap['reason'],
                            stale_after_seconds=pol['stale'], refresh=snap.get('refresh'),
                            provider_updated_at_ms={k: v.get('updatedAt') for k, v in snap['providers'].items()},
                            provider_data_source={k: v.get('data_source', SOURCE_ORCA) for k, v in snap['providers'].items()},
                            codex_log_status=(snap['providers'].get(CODEX_LIMIT_ID) or {}).get('log_status'),
                            codex_orca_updated_at_ms=(snap['providers'].get(CODEX_LIMIT_ID) or {}).get('orca_updated_at'))
    # Not printed: route.py derives the reminder from it.
    result['snapshot'] = snap
    if explicit_profile:
        sel = explicit_resolve(cands[0], pol)
        taken = cands[0].get('stale_takeover')
        if taken and sel['selected']:
            note = '守卫接手使用了过期但有界的额度数据（年龄 %.1f 分钟）' % taken['age_minutes']
            sel = dict(sel, reason=sel['reason'] + '；' + note, warnings=sel['warnings'] + [note])
    else:
        sel = resolve(cands, pol, prefer_primary=bool(rule.get('prefer_primary')))
    result['selection'] = sel
    return result


def restore_reminder(pol, snap, now_ms):
    """Whether a lowered capacity factor is wasting this weekly window. Read-only, never affects routing."""
    cfg = pol['reminder']
    if not cfg['enabled']:
        return dict(active=False, reason='restore_reminder 未启用')
    agent, factor = cfg['agent'], pol['factors'].get(cfg['agent'])
    if factor is None or factor >= cfg['restore_factor']:
        return dict(active=False, reason='%s 权重已是恢复值' % agent)
    prov = (snap or {}).get('providers', {}).get(pol['provider_keys'].get(agent)) if (snap or {}).get('ok') else None
    if not isinstance(prov, dict) or prov.get('status') != 'ok' or not _num(prov.get('updatedAt')):
        return dict(active=False, reason='%s 额度未知' % agent)
    if (now_ms - prov['updatedAt']) / 1000.0 > pol['stale'] or prov['updatedAt'] - now_ms > pol['skew'] * 1000:
        return dict(active=False, reason='%s 额度数据过期' % agent)
    info, why = _window(pol, 'weekly', prov.get('weekly'), now_ms, 0.0, factor, dict(weekly=0.0))
    if why:
        return dict(active=False, reason='%s 额度未知：%s' % (agent, why))
    used = 100.0 - info['remaining_pp']
    elapsed = 1.0 - info['minutes_to_reset'] / info['window_minutes']
    if elapsed <= 0 or elapsed + EPS < cfg['min_elapsed']:
        return dict(active=False, reason='weekly 已过时间比例 %.3f < %s' % (elapsed, cfg['min_elapsed']))
    unused = max(0.0, 100.0 - used / elapsed)
    numbers = dict(used_pp=round(used, 2), remaining_pp=round(info['remaining_pp'], 2),
                   minutes_to_reset=info['minutes_to_reset'], hours_to_reset=round(info['minutes_to_reset'] / 60, 1),
                   elapsed_fraction=round(elapsed, 4), projected_unused_pp=round(unused, 2),
                   resets_at_ms=int(prov['weekly']['resetsAt']))
    if info['remaining_pp'] + EPS < cfg['min_remaining']:
        return dict(numbers, active=False, reason='剩余 %.1f 点 < %s' % (info['remaining_pp'], cfg['min_remaining']))
    if unused + EPS < cfg['min_unused']:
        return dict(numbers, active=False, reason='预计未用 %.1f 点 < %s' % (unused, cfg['min_unused']))
    return dict(numbers, active=True, current_capacity_factor=factor, restore_capacity_factor=cfg['restore_factor'],
                message='%s 周额度按当前进度到重置时预计还有约 %.0f%% 用不完（已用 %.0f%%、剩余 %.0f%%、%.1f 小时后重置）；'
                        '当前权重 %g。告诉协调者“恢复 Claude 权重”即可恢复到 %g。'
                        % (agent.capitalize(), unused, used, info['remaining_pp'], info['minutes_to_reset'] / 60,
                           factor, cfg['restore_factor']))


def reminder_noted(resets_at_ms):
    """Read-only: was this weekly window already passed on to the coordinator?"""
    stored = _read_state(routing_state_dir() / REMINDER_STATE).get('notified_resets_at')
    return _num(stored) and abs(stored - resets_at_ms) <= SAME_WINDOW_MS


def claim_reminder(resets_at_ms, clock=None):
    """True for exactly one caller per weekly window. If it cannot be recorded, False: better silent than nagging."""
    clock, fd = clock or time.time, None
    try:
        root = routing_state_dir()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(root / REMINDER_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        if not _bounded_flock(fd, 2):
            return False
        if reminder_noted(resets_at_ms):
            return False
        _write_state(root / REMINDER_STATE, dict(notified_resets_at=resets_at_ms, notified_at=clock()))
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)


def reminder_for_output(pol, snap, now_ms, may_claim=None):
    """The independent output field. may_claim() is asked only when a first notice is actually due."""
    view = restore_reminder(pol, snap, now_ms)
    view['first_in_window'] = False
    if view['active']:
        view['already_notified'] = bool(reminder_noted(view['resets_at_ms']))
        if not view['already_notified'] and (may_claim is None or may_claim()):
            view['first_in_window'] = claim_reminder(view['resets_at_ms'])
    return view
