#!/usr/bin/env python3
"""Detached, fail-closed quota handoff for route.py-owned dispatches only.

All worker mutations are behind Backend; --dry-run only reads quota/state and
prints actions. No git mutations. A settled Task alone never proves process exit,
and anything that suggests someone else took over abandons the handoff.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import quota
from quota import load_config

ROOT = Path(__file__).resolve().parents[1]
STATE = Path.home() / '.config' / 'orca-model-routing' / 'quota-guard'
ORCA = '/usr/local/bin/orca'
if not Path(ORCA).is_file():
    ORCA = 'orca'
DEFAULTS = dict(enabled=True, pools=['codex'], poll_seconds=60,
                default_burn_pp_per_min=dict(session=1.5, weekly=0.25),
                rate_window_minutes=10, rate_min_dt_minutes=3, rate_min_delta_pp=2,
                grace_minutes=3, floor_pp=5, handoff_spacing_minutes=2)
HANDOFF_END = '<!-- quota-handoff-complete -->'
NO_DATA_ALERT_SECONDS = 900
RUN_ALERT_DEDUP_SECONDS = 3600
ERROR_ALERT_AFTER = 5
ERROR_EXIT_AFTER = 30
PREREQ_ALERTS = ('attempts', 'original', 'target', 'worktree', 'check_failed', 'submodule')
# A takeover whose start Orca could not conclude: the old worker is gone, the new one may or may not be running.
TAKEOVER_UNCERTAIN = 'handoff_start_unknown'
FINAL_PHASES = ('done', 'handoff_done', TAKEOVER_UNCERTAIN, 'abandoned', 'blocked')
ENDED_DISPATCH = ('completed', 'failed', 'cancelled', 'abandoned', 'circuit_broken')
ENDED_WORKER = ('failed', 'stopping', 'stopped', 'stop_unknown', 'abandoned')
# Someone gave this attempt up on purpose; not even a complete report overrides that.
CANCELLED = ('cancelled', 'abandoned')
# Only this Task status, bound to this dispatch and without a conclusion, is an attempt the guard may end.
ACTIVE_TASK = 'dispatched'
# After any worker-stop Orca moves the Task to blocked; every other change is someone's decision.
STATUS_AFTER_STOP = 'blocked'
# Orca's worker state for a start it could not conclude; it moves the Task to blocked in the same transaction.
START_UNKNOWN = 'start_unknown'
HANDOFF_RESULT = '额度交接，非模型失败；旧写入者已确认退出。'
RESUME_NOTE = '额度交接已取消，请继续原任务；已写的交接文件可保留。'
HOLD_NOTE = '额度交接已放弃（%s）。现在不能确认这个 Task 仍由你负责：立即停止新动作，不要恢复工作，不要写文件或提交；等协调者指示。'
GITLINK_MODE = '160000'
UNTRACKED_MAX_FILES = 5000
UNTRACKED_MAX_BYTES = 256 * 1024 * 1024
START_OVERDUE_SECONDS = 600
HEARTBEAT_SECONDS = 30
START_SUPERVISOR_JOIN_SECONDS = 90
# Leave time after the last terminal input for the worker to read the context and write its report.
REPORT_RESERVE_SECONDS = 60
# Bound delivery confirmation before the guard exits so a delayed note cannot hold up the outcome.
NOTE_CONFIRM_SECONDS = 15
NOTE_CONFIRM_POLL_SECONDS = 3
CANNOT_CONFIRM = '无法确认 Codex 在运行：'
DEFAULT_PHASE_LIMIT_SECONDS = 480
ABS_PATH = re.compile(r"""(?<![\w./:~-])/[^\s'"`<>|,;:，。；：、（）()\[\]{}]+""")
HOME_PATH = re.compile(r"""(?<![\w./:~-])~/[^\s'"`<>|,;:，。；：、（）()\[\]{}]+""")


def settings(config):
    raw = config.get('quota_policy', {}).get('quota_guard') or {}
    cfg = dict(DEFAULTS, **raw)
    # Absence is opt-out for older/private route configurations.
    cfg['enabled'] = bool(raw.get('enabled', False))
    if not isinstance(cfg['pools'], list) or not all(isinstance(x, str) for x in cfg['pools']):
        raise ValueError('quota_guard.pools must be a list of account pools')
    for key in ('poll_seconds', 'rate_window_minutes', 'rate_min_dt_minutes', 'rate_min_delta_pp',
                'grace_minutes', 'floor_pp', 'handoff_spacing_minutes'):
        if not quota._num(cfg[key]) or cfg[key] < 0:
            raise ValueError('invalid quota_guard.' + key)
    burn = cfg['default_burn_pp_per_min']
    if not isinstance(burn, dict) or not all(quota._num(burn.get(w)) and burn[w] > 0 for w in quota.WINDOWS):
        raise ValueError('invalid quota_guard.default_burn_pp_per_min')
    if (cfg['poll_seconds'] <= 0 or cfg['grace_minutes'] <= 0 or cfg['floor_pp'] > 100 or
            cfg['rate_min_delta_pp'] <= 0 or not 0 < cfg['rate_min_dt_minutes'] <= cfg['rate_window_minutes']):
        raise ValueError('invalid quota_guard timing/floor')
    fallback = raw.get('delivery_fallback', {})
    if not isinstance(fallback, dict):
        raise ValueError('invalid quota_guard.delivery_fallback')
    cfg['delivery_fallback'] = dict(dict(enabled=True, wait_seconds=20), **fallback)
    wait = cfg['delivery_fallback']['wait_seconds']
    if not quota._num(wait) or not 0 <= wait <= 3600:
        raise ValueError('invalid quota_guard.delivery_fallback.wait_seconds')
    if not isinstance(cfg['delivery_fallback']['enabled'], bool):
        raise ValueError('invalid quota_guard.delivery_fallback.enabled')
    return cfg


def safe_dispatch(dispatch):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', dispatch):
        raise ValueError('invalid dispatch id')
    return dispatch


def write_private(path, text):
    """Atomic context replacement, readable only by the user."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as fh:
        fh.write(text)
    os.replace(temp, path)


def context_instruction(path):
    return ('\n\n额度交接入口（每次接单、任何动作前必须读取）：' + str(path) +
            '\n其中为原始任务及最新接手说明；若入口缺失或不可读，停止动作并向协调者报告。'
            '\n先核对已有改动，不要假定它们正确，继续完成原验收条件；不得自行派发。')


def load_meta(dispatch, state=STATE):
    return json.loads((Path(state) / (safe_dispatch(dispatch) + '.json')).read_text())


def protected(cfg, agent):
    return cfg['enabled'] and agent in cfg['pools']


def prepare_context(spec, config, args, agent, state=STATE):
    """Persist the original spec before first launch; retry updates the same entry.

    Orca cannot replace a Task spec on retry. Its immutable spec contains this
    mandatory context entry, so no post-start mail race is needed. Workers the
    guard will not protect get an unchanged spec.
    """
    cfg = settings(config)
    if getattr(args, 'retry_of', None):
        meta = load_meta(args.retry_of, state)
        if meta['task_id'] != args.task or meta['run_id'] != args.run:
            raise ValueError('retry task/run differs from recorded dispatch')
        path = Path(meta['context_file'])
        if not path.is_file():
            raise ValueError('retry context missing')
        # Called only after the guard verified the persisted Task entry.
        write_private(path, spec)
        return dict(context_file=str(path), original_spec_file=meta['original_spec_file'])
    if not protected(cfg, agent):
        return {}
    # The entry goes into the immutable Task spec, so current has to be decided before worker-start.
    extra = (dict(preflight_worktree_id=preflight_current(spec, args))
             if getattr(args, 'worktree', None) == 'current' else {})
    ident = uuid.uuid4().hex
    original = Path(state) / (ident + '-original.md')
    context = Path(state) / (ident + '-context.md')
    write_private(original, spec)
    write_private(context, spec + '\n\n接手说明：首次派发，尚无额度交接。\n')
    return dict(context_file=str(context), original_spec_file=str(original), **extra)


def terminal_info(handle):
    """Orca's record of one terminal, or None when it cannot be read."""
    try:
        proc = subprocess.run([ORCA, 'terminal', 'show', '--terminal', handle, '--json'], capture_output=True,
                              text=True, timeout=20, stdin=subprocess.DEVNULL)
        data = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None
    if proc.returncode or not isinstance(data, dict) or not data.get('ok'):
        return None
    terminal = (data.get('result') or {}).get('terminal')
    return terminal if isinstance(terminal, dict) else None


def local_placement(terminal):
    """(worktree id, path) of a terminal in a local git worktree; None if any part cannot be trusted."""
    terminal = terminal if isinstance(terminal, dict) else {}
    wid, path = terminal.get('worktreeId'), terminal.get('worktreePath')
    if terminal.get('executionHostId') != 'local' or not isinstance(wid, str) or not isinstance(path, str):
        return None
    repo, _, id_path = wid.partition('::')
    if not repo or not os.path.isabs(id_path) or not os.path.isabs(path):
        return None
    real = os.path.realpath(path)
    # A folder workspace has no fingerprint, so it could never pass the handoff checks.
    if os.path.realpath(id_path) != real or git_root(real) != real:
        return None
    return wid, path


def preflight_current(spec, args):
    """Worktree id worker-start --worktree current is expected to use, or ValueError saying why it is unprotected.

    Orca resolves current from the coordinator terminal (--from, else this shell's handle),
    so the same record predicts it. register_start compares this against the real worker.
    """
    handle = getattr(args, 'coordinator', None) or os.environ.get('ORCA_TERMINAL_HANDLE')
    placement = local_placement(terminal_info(handle)) if handle else None
    if not placement:
        raise ValueError('无法从协调者终端可靠解析 --worktree current 的实际 worktree，本次不受保护')
    foreign = foreign_worktrees(spec, placement[1], home=True)
    if foreign:
        raise ValueError('说明里出现 current 之外的 git 目录 %s，无法确定实际工作目录，本次不受保护'
                         % json.dumps(foreign, ensure_ascii=False))
    return placement[0]


def inside(path, root):
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def process_table(runner):
    """pid -> parent, start second and executable name; a row we cannot read fails the whole scan."""
    ps = runner(['ps', '-ax', '-o', 'pid=', '-o', 'ppid=', '-o', 'lstart=', '-o', 'ucomm='], capture_output=True,
                text=True, errors='replace', timeout=30, stdin=subprocess.DEVNULL, env=dict(os.environ, LC_ALL='C'))
    if ps.returncode or not ps.stdout:
        raise RuntimeError('process scan unavailable')
    table = {}
    try:
        for line in ps.stdout.splitlines():
            parts = line.split(None, 7)
            # The kernel's name, not comm: on macOS comm follows argv[0] and process titles, which can hold secrets.
            # ps pads it, and it never carries a directory.
            table[int(parts[0])] = dict(ppid=int(parts[1]),
                                        begun=int(time.mktime(time.strptime(' '.join(parts[2:7]), '%a %b %d %H:%M:%S %Y'))),
                                        command=parts[7].strip() if len(parts) > 7 else '?')
    except (IndexError, ValueError) as exc:
        raise RuntimeError('process scan unavailable') from exc
    return table


def process_cwds(runner, selector, partial_ok=False):
    """pid -> cwd. An lsof error or a record it did not finish fails the whole scan: a partial list clears nobody.

    partial_ok is for naming specific pids, where lsof exits 1 if it cannot examine one (a root-owned ancestor, say).
    That only ever means fewer processes get excluded, so it errs toward refusing.
    """
    lsof = runner(['lsof', '-nP'] + selector + ['-d', 'cwd', '-Fpn'], capture_output=True, text=True, errors='replace',
                  timeout=30, stdin=subprocess.DEVNULL, env=dict(os.environ, LC_ALL='C'))
    if lsof.returncode and not partial_ok:
        raise RuntimeError('process scan unavailable: lsof exit %s' % lsof.returncode)
    cwd, pid = {}, None
    try:
        for line in (lsof.stdout or '').splitlines():
            if line.startswith('p'):
                if pid is not None and pid not in cwd:
                    raise ValueError('record without a cwd')
                pid = int(line[1:])
            elif line.startswith('n') and pid is not None:
                cwd[pid] = line[1:]
            elif line and not line.startswith('f'):
                raise ValueError('unexpected field')
    except ValueError as exc:
        raise RuntimeError('process scan incomplete: lsof %s' % exc) from exc
    if pid is not None and pid not in cwd:
        raise RuntimeError('process scan incomplete: lsof record without a cwd')
    return cwd


def lineage(pid, table):
    """A process and its ancestors, nearest first. Stops before init, so an orphan's chain reaches nobody."""
    chain = []
    while pid > 1 and pid in table and pid not in chain:
        chain.append(pid)
        pid = table[pid]['ppid']
    return chain


def coordinator_roots(worktree, runner=subprocess.run):
    """Ancestors of this `route.py start` working in the worktree: the coordinator, not its terminal host.

    Only the nearest unbroken run counts. The shell may have cd'd away, so a gap before the run is fine, but
    anything above a gap (a login wrapper, the terminal host) also parents other workers.
    """
    table = process_table(runner)
    chain = lineage(os.getppid(), table)
    if not chain:
        return []
    cwd = process_cwds(runner, ['-a', '-p', ','.join(map(str, chain))], partial_ok=True)
    root, roots = os.path.realpath(worktree), []
    for pid in chain:
        if pid in cwd and inside(cwd[pid], root):
            roots.append(dict(pid=pid, started_at=table[pid]['begun']))
        elif roots:
            break
    return roots


def recorded_roots(worktree, retry_of, state):
    """No roots means nothing is excluded, so neither failure here costs the worker its protection.

    A retry comes from the detached guard, whose chain holds no coordinator, so it keeps the old dispatch's roots;
    route.py has already checked that the retry is on the same worktree.
    """
    roots = []
    try:
        roots = coordinator_roots(worktree)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
        pass
    if retry_of:
        try:
            roots += [r for r in load_meta(retry_of, state).get('coordinator_roots', []) if r not in roots]
        except (OSError, ValueError):
            pass
    return roots


def git_root(path):
    """Nearest enclosing git worktree of a possibly truncated path, without running git."""
    current = Path(path)
    for folder in [current] + list(current.parents):
        try:
            if (folder / '.git').exists():
                return os.path.realpath(folder)
        except OSError:
            continue
    return None


def foreign_worktrees(spec, worktree, home=False):
    """Other git worktrees a spec names: the worker may really be working there."""
    own = os.path.realpath(worktree)
    tokens = ABS_PATH.findall(spec)[:200]
    if home:
        tokens += [os.path.expanduser(token) for token in HOME_PATH.findall(spec)[:200]]
    roots = {git_root(token) for token in tokens}
    return sorted(root for root in roots if root and root != own)


def measured_burn(cfg, window, latest, samples, tolerance_ms):
    """Points per minute over the recent samples of one window, or None if it cannot be measured.

    Samples are filtered first, so one from before a window reset cannot hide the valid ones after it.
    Log and Orca readings of one moment can differ, so only one source is compared.
    """
    win = latest.get(window)
    oldest, oldest_dt = None, 0
    for sample in samples or ():
        if sample.get('data_source', quota.SOURCE_ORCA) != latest.get('data_source', quota.SOURCE_ORCA):
            continue
        if not quota._num(sample.get('updatedAt')):
            continue
        old = sample.get(window)
        if (not isinstance(old, dict) or not quota._num(old.get('usedPercent')) or not 0 <= old['usedPercent'] <= 100 or
                not quota._num(old.get('resetsAt')) or abs(old['resetsAt'] - win['resetsAt']) > tolerance_ms):
            continue
        dt = (latest['updatedAt'] - sample['updatedAt']) / 60000
        if cfg['rate_min_dt_minutes'] <= dt <= cfg['rate_window_minutes'] and dt > oldest_dt:
            oldest, oldest_dt = sample, dt
    if oldest is None:
        return None
    delta = win['usedPercent'] - oldest[window]['usedPercent']
    return delta / oldest_dt if delta >= cfg['rate_min_delta_pp'] else None


def window_trigger(cfg, window, remaining, to_reset, burn):
    projected = remaining - burn * cfg['grace_minutes']
    # Handoff takes up to grace_minutes; a window resetting by then restores itself first.
    reset_exempt = to_reset <= cfg['grace_minutes']
    return projected, reset_exempt, projected <= cfg['floor_pp'] and not reset_exempt


def predict(config, agent, snapshot, now, samples=None):
    """Validate using existing quota rules; project each window independently.

    Data age is never charged as consumption: the snapshot already carries the
    freshest reading available, and data older than stale_after_seconds is unknown.
    """
    cfg = settings(config)
    pol = quota.validate_policy(config['quota_policy'])
    prov = snapshot.get('providers', {}).get(pol['provider_keys'].get(agent), {})
    probe = quota.evaluate_candidate('guard', dict(agent=agent, model='guard', enabled=True),
                                    'primary', pol, snapshot, pol['budgets']['normal'], int(now*1000))
    result = dict(valid=probe['status'] != 'unknown', reasons=probe['reasons'],
                  data_age_minutes=probe['data_age_seconds'] / 60 if probe['data_age_seconds'] is not None else None,
                  data_source=probe['data_source'], windows={}, trigger_windows=[])
    if not result['valid']:
        return result
    tolerance_ms = pol['session_log']['tolerance'] * 1000
    for w in quota.WINDOWS:
        win = prov[w]
        measured = measured_burn(cfg, w, prov, samples, tolerance_ms)
        burn = float(cfg['default_burn_pp_per_min'][w]) if measured is None else measured
        remaining = 100 - win['usedPercent']
        to_reset = (win['resetsAt'] - now * 1000) / 60000
        projected, reset_exempt, triggered = window_trigger(cfg, w, remaining, to_reset, burn)
        result['windows'][w] = dict(remaining_pp=remaining, burn_pp_per_min=burn,
                                    burn_source='default' if measured is None else 'measured',
                                    projected_remaining_pp=projected,
                                    minutes_to_reset=to_reset, reset_exempt=reset_exempt)
        if triggered:
            result['trigger_windows'].append(w)
    return result


def plan_warnings(config, cand):
    """Plan-time view of the guard's trigger, using the default rate for a worker not yet started."""
    cfg = settings(config)
    if not protected(cfg, cand.get('agent')):
        return []
    hits = []
    for w, info in (cand.get('windows') or {}).items():
        burn = cfg['default_burn_pp_per_min'][w]
        if window_trigger(cfg, w, info['remaining_pp'], info['minutes_to_reset'], burn)[2]:
            hits.append('%s 剩余 %.4g 点' % (w, info['remaining_pp']))
    if not hits:
        return []
    return ['额度守卫：%s，已达交接阈值；此档位的 worker 启动后会很快被交接，建议改用其他账户池。' % '、'.join(hits)]


def succeeded(view):
    return (view.get('worker', {}).get('state') == 'succeeded' or
            view.get('projection', {}).get('outcome') == 'succeeded')


def is_stopped(view):
    # Exact exit evidence only; failed/stopped Task/worker bookkeeping is not enough.
    resource = view.get('terminalResource') or {}
    projection = view.get('projection') or {}
    return (resource.get('releaseState') == 'released' or
            projection.get('liveness', {}).get('verdict') == 'exited')


def settled(view):
    return (succeeded(view) or is_stopped(view) or
            view.get('dispatch', {}).get('status') in ENDED_DISPATCH)


def attempt_ended(view):
    """Orca's record says this attempt ended or is ending; a stopped terminal alone does not say that."""
    return (view.get('dispatch', {}).get('status') in ENDED_DISPATCH or
            (view.get('worker') or {}).get('state') in ENDED_WORKER)


def cancelled_view(view):
    return (view.get('dispatch', {}).get('status') in CANCELLED or
            (view.get('worker') or {}).get('state') in CANCELLED)


def start_unknown_view(view):
    return (view.get('worker') or {}).get('state') == START_UNKNOWN


def task_has_conclusion(task):
    # A reopened Task may still carry the guard's own note from the previous attempt.
    return bool(task.get('result')) and task['result'] != HANDOFF_RESULT


def retry_refusal(task, latest_dispatch, retry_of):
    """Why --retry-of may not reopen this Task, or None.

    Only two states mean nobody has decided yet: the guard's own note, or what a stop leaves behind when the
    guard handed the Task to the coordinator. A cancellation or any other conclusion is not reopened here.
    """
    if latest_dispatch != retry_of:
        return 'Task 的最新 dispatch 是 %s，不是 %s' % (quota._safe(latest_dispatch), retry_of)
    status, result = task.get('status'), task.get('result')
    if status == 'failed' and result == HANDOFF_RESULT:
        return None
    if status == STATUS_AFTER_STOP and not result:
        return None
    return 'Task 状态 %s、结果 %s，不是可接手状态' % (quota._safe(status), quota._safe(result))


def parse_background_pids(text):
    """[] for an explicit none, the listed pids, or None when the line cannot be read (the caller refuses)."""
    values = re.findall(r'^background_pids:[ \t]*(.*?)[ \t]*$', text or '', re.M)
    if len(values) != 1:
        return None
    if values[0].lower() == 'none':
        return []
    if not re.fullmatch(r'\d+(?:[ \t]*[,，、 \t][ \t]*\d+)*', values[0]):
        return None
    pids = [int(pid) for pid in re.findall(r'\d+', values[0])]
    return pids if all(pid > 1 for pid in pids) else None


def read_handoff(path):
    """Handoff file the worker wrote: complete only when it ends with the marker."""
    try:
        text = Path(path).read_text(errors='replace')
    except OSError:
        return dict(text=None, complete=False, worktree=None, background_pids=None)
    match = re.search(r'^worktree:[ \t]*(\S.*?)[ \t]*$', text, re.M)
    return dict(text=text, complete=text.rstrip().endswith(HANDOFF_END),
                worktree=match.group(1) if match else None, background_pids=parse_background_pids(text))


def untracked_digest(root, names, max_files=None, max_bytes=None):
    """Hash of each untracked file's path, type and content; raises rather than fingerprint part of them.

    git status shows only the names, so the content has to be read here. Symlinks are never followed.
    """
    max_files = UNTRACKED_MAX_FILES if max_files is None else max_files
    max_bytes = UNTRACKED_MAX_BYTES if max_bytes is None else max_bytes
    if len(names) > max_files:
        raise RuntimeError('未跟踪文件 %d 个，超过指纹上限 %d' % (len(names), max_files))
    digest, total = hashlib.sha256(), 0
    try:
        for name in sorted(names):
            # git lists a nested repository as a directory, whose contents it does not enumerate.
            if name.endswith('/'):
                raise RuntimeError('未跟踪的嵌套仓库 %s 无法取指纹' % quota._safe(name))
            path, body = os.path.join(root, name), hashlib.sha256()
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                kind = 'link'
                body.update(os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(info.st_mode):
                kind = 'file:%o' % (info.st_mode & 0o111)
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as fh:
                    if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                        raise RuntimeError('文件在读取时被替换')
                    for chunk in iter(lambda: fh.read(1 << 20), b''):
                        total += len(chunk)
                        if total > max_bytes:
                            raise RuntimeError('未跟踪文件总大小超过指纹上限 %d 字节' % max_bytes)
                        body.update(chunk)
            else:
                kind = 'other:%o' % stat.S_IFMT(info.st_mode)
            digest.update(os.fsencode(name) + b'\0' + kind.encode() + b'\0' + body.digest())
    except OSError as exc:
        raise RuntimeError('读取未跟踪文件失败：%s' % exc) from exc
    return digest.hexdigest()


def last_output_text(output, limit=6000):
    """Plain text of the worker's last output; drops JSON structure and the task prompt."""
    output = output if isinstance(output, dict) else {}
    messages = (output.get('transcript') or {}).get('messages') if isinstance(output.get('transcript'), dict) else None
    texts = []
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and message.get('role') == 'assistant':
                texts += [b['text'] for b in message.get('blocks') or []
                          if isinstance(b, dict) and b.get('type') == 'text' and isinstance(b.get('text'), str)]
    else:
        for key in ('lines', 'tail'):
            if isinstance(output.get(key), list):
                texts = [line for line in output[key] if isinstance(line, str)]
                break
    return '\n'.join(texts)[-limit:]


def target_failure(name, result, cand):
    """Why one candidate cannot take over, for the coordinator: how stale, and what the refresh did."""
    why = '，'.join(cand.get('reasons') or ()) or result.get('blocked_reason') or '不可派发'
    usage = (result.get('quota') or {}).get('usage_refresh')
    return '%s：%s%s' % (name, why, '；刷新结果：' + quota.describe_refresh(usage) if usage else '')


def start_refusal(receipt):
    """What route.py start said when it did not launch: its stated reason and what the refresh did."""
    if not isinstance(receipt, dict):
        return ''
    changed = receipt.get('profile_changed')
    if isinstance(changed, dict):
        why = '额度重新选择结果与复核不同（期望 %s，实际 %s）' % (changed.get('expected'), changed.get('current'))
    else:
        why = receipt.get('blocked_reason') or receipt.get('error') or receipt.get('action_required') or ''
        if isinstance(why, dict):
            why = why.get('message') or json.dumps(why, ensure_ascii=False)
    usage = (receipt.get('quota') or {}).get('usage_refresh') if isinstance(receipt.get('quota'), dict) else None
    text = str(why) + ('；刷新结果：' + quota.describe_refresh(usage) if usage else '')
    return text[:1500]


def start_judgement(stderr):
    """The stale-data record route.py start printed for the worker it launched, or None."""
    for line in (stderr or '').splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            continue
        plan = data.get('routing_plan') if isinstance(data, dict) else None
        if isinstance(plan, dict):
            return plan.get('stale_takeover')
    return None


class SubmodulesPresent(RuntimeError):
    """Git records only a sub-repository's commit and a -dirty mark, so no fingerprint here can see inside one."""


def read_message_delivery(message_id, db_path=None):
    """Bounded read-only fallback when inbox cannot find this specific message."""
    path = Path(db_path) if db_path is not None else Path(
        os.environ.get('ORCA_USER_DATA_PATH') or Path.home()/'Library'/'Application Support'/'orca')/'orchestration.db'
    try:
        until = time.monotonic() + 2
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.set_progress_handler(lambda: int(time.monotonic() >= until), 1000)
            row = db.execute('SELECT delivered_at FROM messages WHERE id = ? LIMIT 1', (message_id,)).fetchone()
        return dict(state=('delivered' if row[0] else 'pending') if row else 'unknown', source='sqlite')
    except (OSError, ValueError, sqlite3.Error) as exc:
        return dict(state='unknown', source='sqlite', reason='送达读库失败：' + type(exc).__name__)


def delivery_input_text(path):
    """Refuse unsafe paths rather than escaping a shell metacharacter into the PTY."""
    if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', path):
        return None
    return '额度守卫请求交接 请立即读取交接入口 ' + path


def terminal_handle(view):
    """Terminal this attempt runs in, per Orca's view of it."""
    return (view.get('worker') or {}).get('agentTerminalHandle') or (view.get('terminal') or {}).get('handle')


def registered_incarnation(view):
    """The process Orca durably recorded for this dispatch at start (`<ptyId>:<incarnationId>`), or None.

    view['terminal'] is read live, so a replaced process would agree with itself there.
    """
    raw = (view.get('dispatch') or {}).get('processIncarnation')
    return (raw.rsplit(':', 1)[-1] or None) if isinstance(raw, str) and raw else None


# Reject any visible row above the composer ending in $, #, %, or > after whitespace stripping.
# Only a digit immediately before % is a progress exception; footer rows are checked separately.
SHELL_PROMPT_OVER_UI = re.compile(r'^[❯➜](?:$|\s)|[#$>]$|(?<!\d)%$')
LOOKS_LIKE_PROMPT = re.compile(r'(?:^[❯➜$#%](?:\s|$)|[#$%>]$)')


def terminal_refusal(terminal, handle, registered):
    """Why Orca's record of this terminal cannot stand for the attempt's process, or None.

    orphaned only says the last renderer window graph does not hold this PTY at its recorded pane: a live worker in a
    tab the app never attached is orphaned from start to finish. The PTY is still there and written directly, so here
    the flag only raises the bar: the process Orca registered for the dispatch must be the one running now.
    """
    current = terminal.get('incarnationId')
    if not handle or terminal.get('handle') != handle:
        return CANNOT_CONFIRM + '终端句柄与登记的不一致'
    if terminal.get('connected') is not True:
        return CANNOT_CONFIRM + '终端未连接'
    if terminal.get('writable') is not True:
        return CANNOT_CONFIRM + '终端不可写'
    if not current:
        return CANNOT_CONFIRM + '终端没有 incarnationId'
    orphaned = terminal.get('orphaned')
    if orphaned is not True and orphaned is not False:
        return CANNOT_CONFIRM + '终端 orphaned 字段缺失或无法识别'
    if registered and registered != current:
        return CANNOT_CONFIRM + '终端进程已替换（orphaned=%s，登记的进程与当前不一致）' % str(orphaned).lower()
    if orphaned and not registered:
        return CANNOT_CONFIRM + '终端 orphaned=true，且 dispatch 没有登记进程可比对'
    return None


def codex_screen_decision(terminal, screen, handle, registered=None):
    """Only a current rendered Codex composer proves an input line; stale preview/stream never does.

    Orca 1.4.248 exposes no cursor coordinate. Require its observed empty composer with the footer as the lowest
    content on screen, and reject shell prompts over any visible row above it. Metadata agentIdentity alone can survive
    failed bootstrap, so it is insufficient. registered: the incarnation Orca recorded at dispatch.
    """
    no = lambda why: dict(safe=False, reason=why, handle=handle)
    cannot = lambda why: no(CANNOT_CONFIRM + why)
    refused = terminal_refusal(terminal, handle, registered)
    if refused:
        return no(refused)
    identity = terminal.get('agentIdentity')
    if identity != 'codex':
        # Only a recorded other identity says it is not running; no record says nothing either way.
        return no('Codex 未运行：终端身份是 %s' % quota._safe(identity)) if identity else cannot('终端没有 Codex 身份记录')
    if (screen.get('handle') != handle or screen.get('status') != 'running'
            or screen.get('source') != 'screen' or not isinstance(screen.get('tail'), list)
            or not all(isinstance(x, str) for x in screen['tail'])):
        return cannot('读不到当前渲染屏幕')
    rows = [x.strip() for x in screen['tail'] if x.strip()]
    if not rows:
        return cannot('当前屏幕没有内容')
    if screen.get('draft'):
        return no('当前输入框已有草稿，不能输入')
    composers = [i for i, row in enumerate(rows) if row in ('›', '› Ask Codex to do anything')]
    if not composers:
        return cannot('屏幕最底部像 shell 提示符' if LOOKS_LIKE_PROMPT.search(rows[-1]) else '屏幕上没有空输入框')
    index = composers[-1]
    footer = rows[index+1:]
    if (not 1 <= len(footer) <= 3 or not footer[-1].startswith('? for shortcuts')
            or any(not (x.startswith('? for shortcuts') or (re.match(r'(?i)^(?:gpt-|codex)', x) and ' · ' in x))
                   for x in footer)):
        return cannot('输入框下方最底部不是 Codex 页脚，最后一行像 shell 提示符' if LOOKS_LIKE_PROMPT.search(rows[-1])
                      else '输入框下方最底部不是 Codex 页脚')
    # A crashed TUI leaves the cursor inside its own frame, so a prompt can be drawn over the rows above the composer.
    if any(SHELL_PROMPT_OVER_UI.search(x) for x in rows[:index]):
        return cannot('输入框上方出现 shell 提示符')
    if not any('esc to interrupt' in x.lower() or 'Ask Codex' in x for x in rows):
        return cannot('屏幕上没有 Codex 界面标记')
    return dict(safe=True, reason='当前 Codex 空输入框与页脚已确认，输入行不是 shell',
                handle=handle, incarnation_id=terminal['incarnationId'])


class Backend:
    def __init__(self, config, meta, state=STATE, runner=None):
        self.config, self.meta, self.state = config, meta, Path(state)
        self.runner = runner or subprocess.run
        self.clock, self.sleep, self.monotonic = time.time, time.sleep, time.monotonic

    def command(self, args, check=True, timeout=30):
        proc = self.runner([ORCA, 'orchestration'] + args + ['--json', '--full'],
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        try:
            data = json.loads(proc.stdout)
        except (ValueError, TypeError):
            data = {}
        if check and (proc.returncode or not data.get('ok')):
            raise RuntimeError('Orca %s failed (%s): %s' % (args[0], proc.returncode, (proc.stderr or '')[-1000:]))
        return data.get('result') or {}

    def sender(self):
        return ['--from', self.meta['coordinator']] if self.meta.get('coordinator') else []

    def show(self):
        return self.command(['worker-show', '--dispatch', self.meta['dispatch_id']])

    def workers(self):
        rows, cursor = [], None
        while True:
            args = ['worker-list', '--run', self.meta['run_id'], '--limit', '100']
            if cursor:
                args += ['--cursor', cursor]
            result = self.command(args)
            if not isinstance(result.get('workers'), list):
                raise RuntimeError('worker-list has no workers list; attempt count unknown')
            rows.extend(result['workers'])
            page = result.get('page') or {}
            if not page.get('hasMore'):
                return rows
            next_cursor = page.get('nextCursor')
            if not next_cursor or next_cursor == cursor:
                raise RuntimeError('worker-list pagination incomplete')
            cursor = next_cursor

    def attempts(self):
        rows = self.workers()
        ids = {r['dispatchId'] for r in rows if r.get('taskId') == self.meta['task_id']}
        if self.meta['dispatch_id'] not in ids:
            raise RuntimeError('old dispatch missing in attempt count')
        return len(ids)

    def original(self):
        """Verify the immutable Task has our preinstalled mandatory entry."""
        path = self.meta.get('original_spec_file')
        context = self.meta.get('context_file')
        if not path or not context or not Path(path).is_file() or not Path(context).is_file():
            return None
        tasks = self.command(['task-list', '--run', self.meta['run_id']] + self.sender()).get('tasks')
        if not isinstance(tasks, list):
            return None
        task = next((t for t in tasks if t.get('id') == self.meta['task_id']), {})
        if context_instruction(context) not in task.get('spec', ''):
            return None
        spec = Path(path).read_text().strip()
        return spec or None

    def task_state(self):
        tasks = self.command(['task-list', '--run', self.meta['run_id']] + self.sender()).get('tasks')
        task = next((t for t in tasks or [] if t.get('id') == self.meta['task_id']), None)
        if task is None:
            raise RuntimeError('task missing from task-list')
        # The row names a dispatch only while one is active; a stop clears it.
        return dict(status=task.get('status'), result=task.get('result'), dispatch_id=task.get('dispatch_id'))

    def latest_dispatch(self):
        dispatch = self.command(['dispatch-show', '--task', self.meta['task_id']] + self.sender()).get('dispatch') or {}
        return dispatch.get('id')

    def guard_enabled(self):
        cfg = settings(load_config(ROOT))
        return protected(cfg, self.meta['agent'])

    def plan(self, takeover=True):
        """A target in another pool, or None with plan_detail saying why. takeover=False previews ordinary rules."""
        # Explicitly evaluate only normal task candidates, never escalation.
        rule = self.config['tasks'][self.meta['kind']]
        self.plan_detail, self.target_stale = '', None
        failures = []
        for name in dict.fromkeys((rule['primary'], rule['alternate'])):
            profile = self.config['models'][name]
            if profile['agent'] == self.meta['agent']:
                continue
            flags = ['--preview'] if not takeover else (
                ['--quota-guard-takeover', self.meta['dispatch_id']] if self.meta.get('dispatch_id') else [])
            cand, failure = self.plan_target(name, flags)
            if failure is None:
                self.target_stale = cand.get('stale_takeover')
                return name
            failures.append(failure)
        self.plan_detail = '；'.join(failures)
        return None

    def recheck(self, target):
        """The chosen target once more, just before the Task changes. False with plan_detail saying why."""
        self.plan_detail = ''
        cand, failure = self.plan_target(target, ['--quota-guard-takeover', self.meta['dispatch_id'], '--handoff-recheck'])
        if failure is not None:
            self.plan_detail = failure
            return False
        self.target_stale = cand.get('stale_takeover')
        return True

    def plan_target(self, name, flags):
        """(candidate, None) when route.py plan says this profile can run, else ({}, why)."""
        cmd = [sys.executable, str(ROOT/'scripts/route.py'), 'plan', '--kind', self.meta['kind'],
               '--complexity', self.meta['complexity'], '--profile', name] + flags
        for key, flag in [('budget_session','--budget-session'),('budget_weekly','--budget-weekly')]:
            if self.meta.get(key) is not None:
                cmd += [flag, str(self.meta[key])]
        proc = self.runner(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        if proc.returncode == 3:
            raise RuntimeError('plan changed (3); coordinator required')
        try:
            result = json.loads(proc.stdout)
        except (ValueError, TypeError):
            result = {}
        result = result if isinstance(result, dict) else {}
        cand = next((c for c in result.get('quota', {}).get('candidates', []) if c['profile'] == name), {})
        if (not proc.returncode and result.get('launchable') and result['plan']['profile'] == name and
                cand.get('status') == 'eligible' and cand.get('auto_selectable')):
            return cand, None
        return {}, target_failure(name, result, cand)

    def send(self, to, body, high=False):
        result = self.command(['send', '--to', to, '--type', 'handoff',
                      '--priority', 'high' if high else 'normal', '--subject', '额度守卫交接',
                      '--body', body] + self.sender())
        # command uses --full: local receipts carry message.id and relays carry relay.messageId.
        message_id = (result.get('messageId') or (result.get('message') or {}).get('id') or
                      (result.get('relay') or {}).get('messageId'))
        return dict(result, messageId=message_id)

    def delivery_status(self, message_id, handle):
        if not isinstance(message_id, str) or not re.fullmatch(r'msg_[A-Za-z0-9_-]+', message_id):
            return dict(state='unknown', source='receipt', reason='发送回执没有有效 messageId')
        # The CLI has no message-show/id filter. Inbox is sufficient when the row is present.
        try:
            args = ['inbox', '--limit', '100'] + (['--terminal', handle] if handle else [])
            rows = self.command(args, timeout=3).get('messages')
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get('id') == message_id and 'delivered_at' in row:
                        return dict(state='delivered' if row['delivered_at'] else 'pending', source='cli')
        except Exception:
            pass
        return read_message_delivery(message_id)

    def codex_running(self, view):
        handle = terminal_handle(view)
        if not handle:
            return dict(safe=False, reason=CANNOT_CONFIRM + '终端句柄缺失')
        try:
            frames = []
            for action in ('show', 'read'):
                args = [ORCA, 'terminal', action, '--terminal', handle]
                if action == 'read':
                    args += ['--screen', '--limit', '120']
                proc = self.runner(args + ['--json'], capture_output=True, text=True, timeout=3, stdin=subprocess.DEVNULL)
                data = json.loads(proc.stdout)
                if proc.returncode or data.get('ok') is not True:
                    raise ValueError('terminal ' + action + ' failed')
                frames.append((data.get('result') or {}).get('terminal') or {})
            result = codex_screen_decision(frames[0], frames[1], handle, registered_incarnation(view))
            # Refuse a replaced process, even when its new screen happens to look like Codex.
            registered = view.get('terminal') or {}
            if registered.get('incarnationId') and registered['incarnationId'] != frames[0].get('incarnationId'):
                return dict(safe=False, reason=CANNOT_CONFIRM + '终端进程已替换', handle=handle)
            return result
        except Exception as exc:
            return dict(safe=False, reason=CANNOT_CONFIRM + '读屏失败：' + type(exc).__name__, handle=handle)

    def terminal_input(self, handle, text, retry_request=None):
        # No automatic resend. An explicit recovery must replay this exact receipt ID.
        if not text or delivery_input_text(text.split()[-1]) != text:
            raise ValueError('unsafe terminal fallback text')
        args = [ORCA, 'terminal', 'send', '--terminal', handle, '--text', text, '--enter', '--wait-submit', '1']
        if retry_request:
            args += ['--retry-request', retry_request]
        proc = self.runner(args + ['--json'], capture_output=True, text=True, timeout=12, stdin=subprocess.DEVNULL)
        data = json.loads(proc.stdout)
        result = data.get('result') or {}
        send = result.get('send') or {}
        prompt = send.get('prompt') or {}
        recovery = (data.get('error') or {}).get('data') or {}
        request_id = prompt.get('requestId') or (recovery.get('orchestrationRequestId') if recovery.get('retrySafe') is not False else None)
        # The normal no-turn-start warning is not a rejection and never causes another send.
        return dict(accepted=send.get('accepted') is True, request_id=request_id,
                    warnings=result.get('warnings') or [], exit_code=proc.returncode)

    def send_status(self, to, body):
        self.command(['send', '--to', to, '--type', 'status', '--priority', 'high',
                      '--subject', '额度守卫交接', '--body', body] + self.sender())

    def stop(self):
        # A nonzero stop receipt is not exit proof; always re-observe after it.
        self.command(['worker-stop', '--dispatch', self.meta['dispatch_id']], check=False, timeout=60)

    def git(self, argv, errors='replace'):
        return self.runner(argv, cwd=self.meta['worktree_path'], capture_output=True, text=True, errors=errors,
                           timeout=20, stdin=subprocess.DEVNULL, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))

    def git_state(self):
        pieces = []
        for argv in (['git', 'status', '--short'], ['git', 'diff', 'HEAD', '--stat']):
            proc = self.git(argv)
            pieces.append('$ ' + ' '.join(argv) + '\n' +
                          ((proc.stdout or '') if proc.returncode == 0 else '读取失败：' + (proc.stderr or '')))
        return '\n'.join(pieces)

    def submodule_paths(self):
        """Gitlinks in the index or HEAD: one can be staged or committed without showing up in the other."""
        found = set()
        for argv in (['git', 'ls-files', '--stage', '-z'], ['git', 'ls-tree', '-r', '-z', 'HEAD']):
            proc = self.git(argv, errors='surrogateescape')
            if proc.returncode:
                raise RuntimeError('git %s failed in registered worktree' % argv[1])
            for row in (proc.stdout or '').split('\0'):
                fields, _, path = row.partition('\t')
                if fields.split(' ')[0] == GITLINK_MODE:
                    found.add(path)
        return sorted(found)

    def fingerprint(self):
        """Read-only identity of the worktree state; raises when it cannot be established."""
        out = {}
        for key, argv in (('toplevel', ['git', 'rev-parse', '--show-toplevel']), ('head', ['git', 'rev-parse', 'HEAD']),
                          ('status', ['git', 'status', '--porcelain']), ('diff', ['git', 'diff', 'HEAD', '--binary'])):
            proc = self.git(argv)
            if proc.returncode:
                raise RuntimeError('git %s failed in registered worktree' % argv[1])
            out[key] = proc.stdout or ''
        if os.path.realpath(out['toplevel'].strip()) != os.path.realpath(self.meta['worktree_path']):
            raise RuntimeError('registered path is not the worktree root')
        submodules = self.submodule_paths()
        if submodules:
            raise SubmodulesPresent('仓库含 Git 子模块（%d 个：%s），无法取指纹' % (
                len(submodules), '、'.join(quota._safe(path) for path in submodules[:5])))
        # Individually listed, not collapsed into directories; surrogateescape keeps odd names openable.
        listed = self.git(['git', 'ls-files', '--others', '--exclude-standard', '-z'], errors='surrogateescape')
        if listed.returncode:
            raise RuntimeError('git ls-files failed in registered worktree')
        names = [name for name in (listed.stdout or '').split('\0') if name]
        return dict(head=out['head'].strip(), status=hashlib.sha256(out['status'].encode()).hexdigest(),
                    diff=hashlib.sha256(out['diff'].encode()).hexdigest(),
                    untracked=untracked_digest(self.meta['worktree_path'], names))

    def leftovers(self):
        """Processes in the worktree started after this dispatch began, other than the coordinator's own."""
        started = self.meta.get('dispatch_started_at')
        if not quota._num(started):
            raise RuntimeError('dispatch has no process baseline')
        root = os.path.realpath(self.meta['worktree_path'])
        cwd = process_cwds(self.runner, [])
        table = process_table(self.runner)
        if not cwd:
            raise RuntimeError('process scan unavailable')
        roots = self.meta.get('coordinator_roots') or []
        # A reused pid has another start time; an exited root matches nothing and its children went to init.
        live = {r['pid'] for r in roots if r['pid'] in table and table[r['pid']]['begun'] == r['started_at']}
        found, excluded = [], []
        for pid, row in table.items():
            if pid == os.getpid() or pid not in cwd or not inside(cwd[pid], root) or row['begun'] < int(started):
                continue
            entry = dict(pid=pid, command=row['command'],
                         started=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['begun'])))
            (excluded if live.intersection(lineage(pid, table)) else found).append(entry)
        if roots:
            self.log('进程扫描：登记协调者根 %d 个，仍有效 %d 个，排除 %s' % (len(roots), len(live), json.dumps(excluded, ensure_ascii=False)))
        return found

    def live_pids(self, pids):
        """Which of these are still running; raises when that cannot be told. A zombie writes nothing."""
        if not pids:
            return []
        table = process_table(self.runner)
        return [pid for pid in pids if pid in table and table[pid]['command'] != '<defunct>']

    def context(self, original, handoff):
        git = self.git_state()
        try:
            tail = last_output_text(self.command(['worker-read', '--dispatch', self.meta['dispatch_id'], '--limit', '60'],
                                                 check=False))
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            # The old worker is gone: a missing excerpt must not end the handoff here.
            self.log('worker-read unavailable: ' + repr(exc))
            tail = ''
        text = (original + '\n\n## 接手说明\n旧 dispatch：' + self.meta['dispatch_id'] +
                '\n额度交接，非模型失败。\n' + handoff + '\n\n### Git 状态\n' + git +
                '\n\n### 旧 worker 最后输出摘要（末尾摘录，需核实）\n' + (tail or '（无可用摘录）') +
                '\n\n先核对已有改动，不要假定正确，继续完成原验收条件。'
                '\n在同一 worktree 接续；不得 reset、stash 或丢弃旧改动；提交和删除按原任务授权。\n')
        write_private(self.state/(self.meta['dispatch_id']+'-resume.md'), text)
        # The same entry is already in the immutable Task prompt and must be read before acting.
        write_private(self.meta['context_file'], text)
        self.log(git)
        return text

    def fail_task(self):
        self.command(['task-update', '--id', self.meta['task_id'], '--status', 'failed',
                      '--result', HANDOFF_RESULT, '--run', self.meta['run_id']] + self.sender())

    def start(self, target, spec):
        path = self.state/(self.meta['dispatch_id']+'-resume.md')
        cmd = [sys.executable, str(ROOT/'scripts/route.py'), 'start', '--run', self.meta['run_id'],
               '--kind', self.meta['kind'], '--complexity', self.meta['complexity'], '--profile', target,
               '--expected-profile', target, '--worktree', self.meta['worktree'],
               '--task', self.meta['task_id'], '--retry-of', self.meta['dispatch_id'], '--spec-file', str(path),
               '--quota-guard-takeover', self.meta['dispatch_id']]
        if self.meta.get('coordinator'):
            cmd += ['--from', self.meta['coordinator']]
        for key, flag in [('budget_session','--budget-session'),('budget_weekly','--budget-weekly')]:
            if self.meta.get(key) is not None:
                cmd += [flag, str(self.meta[key])]
        # No outer timeout/resend: an uncertain launch may already own a live writer.
        proc = self.runner(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           cwd=self.meta['worktree_path'])
        self.log('route start stderr: ' + (proc.stderr or '')[-6000:])
        # What this start itself judged, not what the plan minutes ago expected it to.
        self.target_stale = start_judgement(proc.stderr)
        try:
            receipt = json.loads(proc.stdout)
        except (ValueError, TypeError):
            receipt = {}
        return proc.returncode, receipt if isinstance(receipt, dict) else {}

    def record(self, event):
        import route
        route.append_log(dict(event, at_ms=int(self.clock()*1000)))

    def log(self, message):
        print(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), message, flush=True)


class Guard:
    def __init__(self, config, meta, backend, state=STATE):
        self.config, self.meta, self.b, self.state = config, meta, backend, Path(state)
        self.cfg = settings(config)
        self.samples = []
        self.invalid_since = None
        self.uncertain_since = None
        self.warned = set()
        self.finished = None
        self.pool_lock = HandoffLock(self.state, meta['agent'])
        # Set only when this guard itself issued worker-stop: nothing else explains a blocked Task.
        self.stop_sent = False
        self.degraded = None
        self.facts = dict(stop_sent=False, old_worker_stopped=False)
        self.phase_name, self.phase_at = None, None
        self.phase_lock = threading.RLock()
        self.delivery = None
        self.delivery_decided = False
        self.delivery_started = None
        self.worker_handle = None

    def alert(self, message):
        """True when one of the two deliveries was accepted."""
        to = 'run:' + self.meta['run_id']
        message += self.delivery_summary()
        try:
            self.b.send(to, message, high=True)
            return True
        except Exception as exc:
            self.b.log('alert failed: ' + repr(exc))
            try:
                self.b.send_status(to, message)
                return True
            except Exception as fallback:
                self.b.log('status alert failed: ' + repr(fallback))
        return False

    def alert_once(self, key, message):
        """A repeating condition alerts once per Run, however many guards see it."""
        if key in self.warned:
            return None
        self.warned.add(key)
        if claim_run_alert(self.state, self.meta['run_id'], key, self.b.clock()):
            return self.alert(message)
        return None

    def rearm(self, key):
        if key in self.warned:
            self.warned.discard(key)
            clear_run_alert(self.state, self.meta['run_id'], key)

    def notice_once(self, key, message):
        """Normal-priority counterpart of alert_once, for what the coordinator should know but need not act on."""
        if key in self.warned:
            return
        self.warned.add(key)
        if claim_run_alert(self.state, self.meta['run_id'], key, self.b.clock()):
            try:
                self.b.send('run:' + self.meta['run_id'], message)
            except Exception as exc:
                self.b.log('notice failed: ' + repr(exc))

    def phase_limit(self, name):
        """Seconds a live guard may stay in this phase before status flags it; final phases have no limit."""
        if name in FINAL_PHASES:
            return None
        return START_OVERDUE_SECONDS if name == 'starting' else (self.cfg['grace_minutes'] + 5) * 60

    def phase(self, name, **extra):
        # Lets status and the coordinator see a handoff that died with this process.
        # What was learned earlier stays on the record, so it still says what state the stop left things in.
        with self.phase_lock:
            if name != self.phase_name:
                self.phase_name, self.phase_at = name, self.b.clock()
            self.facts.update(extra)
            write_private(self.state/(self.meta['dispatch_id']+'-phase.json'), json.dumps(
                dict(self.facts, dispatch_id=self.meta['dispatch_id'], run_id=self.meta['run_id'],
                     phase=name, at=self.phase_at, limit_seconds=self.phase_limit(name)), ensure_ascii=False))

    def beat(self):
        """A failed heartbeat costs status a reading, never the guard its work."""
        try:
            write_private(self.state/(self.meta['dispatch_id']+'-heartbeat.json'), json.dumps(
                dict(at=self.b.clock(), limit_seconds=max(self.cfg['poll_seconds'] * 3, 300),
                     protection_degraded=self.degraded), ensure_ascii=False))
        except OSError as exc:
            self.b.log('heartbeat failed: ' + repr(exc))

    def observe(self, view):
        self.facts['old_worker_stopped'] = is_stopped(view)

    def protection_lost(self, pred, refresh=None):
        now = self.b.clock()
        if self.invalid_since is None:
            self.invalid_since = now
            self.degraded = dict(since=now, reasons=pred['reasons'])
            self.beat()
            reasons = '；'.join(map(str, pred['reasons'])) or '无可用快照'
            if refresh:
                reasons += '；刷新结果：' + quota.describe_refresh(refresh)
            self.b.log('额度保护降级：' + reasons)
            self.notice_once('protection_degraded', '额度快照不可用（%s）：此期间额度守卫没有保护，%s 继续运行；数据恢复后自动继续判断，'
                             '连续超过 15 分钟仍不可用会再发 high 告警。' % (reasons, self.meta['dispatch_id']))
        if now - self.invalid_since > NO_DATA_ALERT_SECONDS:
            self.alert_once('no_fresh_data', '连续超过 15 分钟额度快照不可用（account list 失败、读数过期或窗口缺失），'
                            '未触发切换，worker 继续运行；此期间额度守卫没有保护。')

    def protection_restored(self):
        if self.invalid_since is None:
            return
        self.invalid_since, self.degraded = None, None
        self.b.log('额度保护恢复')
        self.beat()
        # The Run-level claim stays, so a flapping source does not notify on every return.
        self.warned.discard('protection_degraded')
        self.rearm('no_fresh_data')

    def remember(self, current):
        known = {s.get('updatedAt') for s in self.samples}
        if current.get('updatedAt') not in known:
            self.samples.append(current)
        horizon = current['updatedAt'] - self.cfg['rate_window_minutes'] * 2 * 60000
        self.samples = [s for s in self.samples if s['updatedAt'] >= horizon]

    def tick(self, snapshot):
        if self.finished:
            return self.finished
        self.beat()
        view = self.b.show()
        if settled(view):
            return 'done'
        # A recovered uncertain start must not wait on screen evidence forever. Do this before quota/screen gates;
        # no stop or takeover follows an expired request, even if the terminal still has its old Codex frame.
        pending = self.pending_request()
        if start_unknown_view(view) and pending and self.b.clock() > pending['until']:
            return self.abandon('恢复交接请求已到期，未完成接手；请协调者核对旧 worker 和交接报告', True)
        if self.uncertain_since is not None:
            try:
                task = self.b.task_state()
            except Exception as exc:
                self.b.log('blocked Task recheck failed: ' + repr(exc))
                task = None
            if task is not None:
                why = self.not_ours(view, task)
                if why == 'uncertain':
                    if self.b.monotonic() - self.uncertain_since >= 900:
                        self.alert_once('task_uncertain:' + self.meta['dispatch_id'], 'Task 暂时处于无结论的 blocked，额度保护暂时无法生效；守卫继续监视。')
                    return 'continue'
                self.uncertain_since = None
                # A recorded uncertain start still has to reach the quota check below.
                if why and why != START_UNKNOWN:
                    return self.abandon(why, False)
        provider = quota.validate_policy(self.config['quota_policy'])['provider_keys'][self.meta['agent']]
        current = snapshot.get('providers', {}).get(provider) or {}
        pred = predict(self.config, self.meta['agent'], snapshot, self.b.clock(), self.samples)
        self.b.log(json.dumps(pred, ensure_ascii=False))
        if not pred['valid']:
            self.protection_lost(pred, snapshot.get('refresh'))
            return 'continue'
        self.protection_restored()
        self.remember(current)
        if not pred['trigger_windows']:
            for key in PREREQ_ALERTS:
                self.rearm(key)
            self.rearm(self.start_unknown_key())
            return 'continue'
        # Before the pool lock: an alert changes nothing, so it does not wait behind another guard's handoff.
        running = ((True if self.recovery_report() else self.start_unknown_evidence(view, pred))
                   if start_unknown_view(view) else None)
        if running is False:
            return 'continue'
        # One handoff at a time per pool; the next guard re-evaluates on fresh numbers.
        if not self.pool_lock.acquire(self.b.clock(), self.cfg['handoff_spacing_minutes'] * 60):
            self.b.log('handoff deferred: another guard holds this pool or finished one just now')
            return 'continue'
        try:
            try:
                task = self.b.task_state()
            except Exception as exc:
                self.alert_once('check_failed', '交接前置检查失败：%s；未停止 worker。' % exc)
                return 'continue'
            why = self.not_ours(view, task)
            if why == 'uncertain':
                return self.wait_on_uncertain()
            self.uncertain_since = None
            if why == START_UNKNOWN:
                if not running:
                    self.b.log('start_unknown attempt without evidence from this round; judging again next round')
                    return 'continue'
            elif why:
                # Nobody was asked to do anything yet, so the worker gets no message.
                return self.abandon(why, False)
            ready = self.prerequisites()
            return self.handoff(pred, *ready) if ready else 'continue'
        finally:
            self.pool_lock.release()

    def wait_on_uncertain(self):
        if self.uncertain_since is None:
            self.uncertain_since = self.b.monotonic()
        if self.b.monotonic() - self.uncertain_since >= 900:
            self.alert_once('task_uncertain:' + self.meta['dispatch_id'], 'Task 暂时处于无结论的 blocked，额度保护暂时无法生效；守卫继续监视。')
        return 'continue'

    def start_unknown_key(self):
        return 'start_unknown:' + self.meta['dispatch_id']

    def start_unknown_evidence(self, view, pred):
        """True: the attempt is confirmed running. False: stay out this round. None: not this state after all.

        Orca's record says only that the start was not concluded, and a failed start looks the same there. Without
        evidence nothing reaches the worker or the Task; this runs every triggered round, so new evidence is picked up.
        """
        try:
            task = self.b.task_state()
        except Exception as exc:
            self.alert_once('check_failed', '交接前置检查失败：%s；未停止 worker。' % exc)
            return False
        if self.not_ours(view, task) != START_UNKNOWN:
            return None
        worker = view.get('worker') or {}
        codex = self.meta['agent'] == 'codex'
        if codex:
            try:
                probe = self.b.codex_running(view)
            except Exception as exc:
                probe = dict(safe=False, reason=CANNOT_CONFIRM + type(exc).__name__)
        else:
            probe = dict(safe=False, reason='%s worker 没有可靠的存活证据' % self.meta['agent'])
        reason = probe.get('reason') or '不能确认 worker 正在运行'
        self.b.log('start_unknown evidence: %s' % json.dumps(dict(safe=bool(probe.get('safe')), reason=reason), ensure_ascii=False))
        if probe.get('safe'):
            self.facts['start_unknown'] = dict(stage=worker.get('stage'), evidence=reason)
            return True
        handle = terminal_handle(view) or '未知'
        then = ('守卫继续监视，每轮重新读取证据，确认运行后自动进入交接。' if codex else
                '守卫继续监视，但不会自动交接。')
        message = ('额度已触发（%s），但 %s 启动结果不确定（Orca 记录 worker state=start_unknown，stage=%s）：%s。'
                   '未向 worker 发送任何内容，未停止 worker，未改 Task；%s'
                   '它如果其实在运行，额度耗尽时会中断，请核对终端 %s。'
                   % ('、'.join(pred['trigger_windows']), self.meta['dispatch_id'], quota._safe(worker.get('stage')),
                      reason, then, handle))
        # This alert is all the protection left here, so one nobody received is tried again next round.
        if self.alert_once(self.start_unknown_key(), message) is False:
            self.rearm(self.start_unknown_key())
        return False

    def not_ours(self, view, task):
        """Why this is not a live attempt the guard may end, or None. Asked before anyone hears of a handoff.

        Two answers mean "ours, but not yet actionable": START_UNKNOWN is a blocked Task whose worker Orca records as
        an unconcluded start (needs running evidence, see start_unknown_evidence); 'uncertain' is any other empty
        blocked (wait and watch). A Task bound to another dispatch is neither.
        """
        if (task.get('status') == 'blocked' and task.get('dispatch_id') in (None, '', self.meta['dispatch_id'])
                and not attempt_ended(view) and not is_stopped(view)):
            if start_unknown_view(view) and not task_has_conclusion(task):
                return START_UNKNOWN
            if not task.get('result'):
                return 'uncertain'
        if task.get('status') != ACTIVE_TASK:
            return 'Task 状态是 %s，不是活跃状态' % quota._safe(task.get('status'))
        if task.get('dispatch_id') != self.meta['dispatch_id']:
            return 'Task 绑定的 dispatch 是 %s，不是 %s' % (quota._safe(task.get('dispatch_id')), self.meta['dispatch_id'])
        if task_has_conclusion(task):
            return 'Task 已有结论'
        if attempt_ended(view):
            return '旧 worker 已结束或正在结束'
        return None

    def prerequisites(self):
        """All must be known before asking the live writer to stop. Re-evaluated on every tick."""
        limit = self.config['constraints']['max_attempts_per_task']
        try:
            if self.b.attempts() >= limit:
                self.alert_once('attempts', '已满 max_attempts_per_task=%s；未停止 worker，让它继续运行。' % limit)
                return None
            original = self.b.original()
            if not original:
                self.alert_once('original', '原始任务/强制接手入口不可取；未停止 worker，让它继续运行。')
                return None
            target = self.b.plan()
            if not target:
                detail = getattr(self.b, 'plan_detail', '')
                self.alert_once('target', '无其他账户池的可派发目标；未停止 worker，让它继续运行，下一轮轮询重新判断，目标恢复后继续交接。'
                                + ('原因：' + detail if detail else ''))
                return None
            before = self.b.fingerprint()
        except SubmodulesPresent as exc:
            self.alert_once('submodule', '%s，不受自动交接保护；未通知也未停止 worker，让它继续运行。' % exc)
            return None
        except Exception as exc:
            self.alert_once('check_failed', '交接前置检查失败：%s；未停止 worker。' % exc)
            return None
        return target, original, before

    def cancel_reason(self, task):
        """Why this handoff must not go on, or None. Evaluated afresh at each step."""
        return self.called_off() or self.task_moved(task)

    def called_off(self):
        """Someone dropped this handoff; that says nothing about who owns the Task."""
        if self.cancel_path().exists():
            return '协调者用取消文件取消了交接'
        if not self.b.guard_enabled():
            return 'quota_guard 已被关闭'
        return None

    def task_moved(self, task):
        """Why someone else now owns this Task or has concluded it, or None."""
        now = self.b.task_state()
        if (now['result'] or None) != (task['result'] or None):
            return 'Task 结果已被改动（可能已取消或已有结论）'
        bound = now.get('dispatch_id')
        if bound != self.meta['dispatch_id'] and (bound or now['status'] == ACTIVE_TASK):
            return 'Task 已不再绑定 %s' % self.meta['dispatch_id']
        if now['status'] != task['status'] and not (self.stop_sent and now['status'] == STATUS_AFTER_STOP):
            return 'Task 状态已变为 %s' % quota._safe(now['status'])
        return None

    def cancel_path(self):
        return self.state/(self.meta['dispatch_id']+'-handoff.cancel')

    def handoff_message(self, handoff):
        return ('额度守卫请求交接，非模型失败。如原任务已全部完成且验证通过，照常发 worker_done --outcome succeeded，守卫放弃切换。'
                '否则：停止新动作；等待后台任务结束或终止它们；然后写 ' + str(handoff) + '：'
                '第一行写 worktree: 加 git rev-parse --show-toplevel 的输出；'
                '再写已完成、未完成、当前改动、下一步，并单独一行写 background_pids: none（没有后台进程），'
                '或 background_pids: 123, 456（已结束或已终止的 PID，逗号分隔，不加别的文字）；'
                '最后一行原样写 ' + HANDOFF_END + '。写完后结束回合。'
                '报告没写完、background_pids 写不清或进程还活着，守卫都不会安排接手，只交给协调者处理。'
                '不要发 worker_done --outcome failed，由守卫结束本次尝试。不得 commit/reset/stash/删除文件。')

    def coordinator_message(self, target):
        return ('额度交接进行中：%s 写完完整交接报告后将在 %s 分钟内交给 %s，由守卫完成全部收尾；'
                '到时没有完整报告，守卫只停止旧 worker 并告警，由你处理。请勿接手、提交或重试此 Task，否则会有两个写入者。'
                'worker 结束回合后 Orca 可能发出升级消息“ended its turn without sending worker_done”或“Worker stopped without reporting”，这是预期现象，不要据此重试或停止，等待守卫结论。'
                '要取消：touch %s，或按 Orca 取消流程 worker-stop 加 task-update --status failed --result cancelled；守卫会放弃接手。'
                '超过 %s 分钟没有后续消息时，运行 quota_guard.py status，守卫已中断的话由你接管。'
                % (self.meta['dispatch_id'], self.cfg['grace_minutes'], target, self.cancel_path(),
                   self.cfg['grace_minutes'] + 5))

    def worker_succeeded(self):
        self.finished = 'done'
        self.phase('done')
        try:
            self.b.send('run:' + self.meta['run_id'], '额度交接已取消：%s 已报告 succeeded，无需接手。' % self.meta['dispatch_id'] + self.delivery_summary())
        except Exception as exc:
            self.b.log('cancel notice failed: ' + repr(exc))
        return 'done'

    def conclude(self, name, why, message):
        """The outcome is on disk before delivery, and stays listed by status if delivery fails."""
        self.finished = name
        self.phase(name, reason=why + self.delivery_summary(), alert_delivered=False)
        self.phase(name, alert_delivered=self.alert(message))
        return name

    def may_resume(self):
        """Read ownership again right before telling a worker to go on: earlier views may be out of date."""
        try:
            view, task = self.b.show(), self.b.task_state()
        except Exception as exc:
            self.b.log('ownership recheck failed: ' + repr(exc))
            return False
        # A start_unknown attempt stays blocked for life, so that state is still its own Task.
        return not settled(view) and self.not_ours(view, task) in (None, START_UNKNOWN)

    def note_delivery(self, receipt):
        """What is known about one note within a bounded wait: `send` accepting it is not delivery."""
        message_id = (receipt or {}).get('messageId')
        if not message_id:
            return dict(state='unknown', reason='发送回执没有 messageId')
        until = self.b.monotonic() + NOTE_CONFIRM_SECONDS
        status = dict(state='unknown')
        while True:
            # Polling lasts 15 seconds; one query already in flight may finish later. Never begin another at expiry.
            if self.b.monotonic() >= until:
                return dict(message_id=message_id, state=status.get('state', 'unknown'), source=status.get('source'),
                            reason=status.get('reason'))
            try:
                status = self.b.delivery_status(message_id, self.worker_handle)
            except Exception as exc:
                status = dict(state='unknown', source='error', reason=type(exc).__name__)
            remaining = until - self.b.monotonic()
            if status.get('state') == 'delivered' or remaining <= 0:
                return dict(message_id=message_id, state=status.get('state', 'unknown'), source=status.get('source'),
                            reason=status.get('reason'))
            self.b.sleep(min(NOTE_CONFIRM_POLL_SECONDS, remaining))

    def tell_old_worker(self, why, called_off):
        """A worker asked to hand off is waiting on this. Only a dropped handoff on a Task still its own resumes it."""
        resume = called_off and self.may_resume()
        try:
            receipt = self.b.send('dispatch:' + self.meta['dispatch_id'], RESUME_NOTE if resume else HOLD_NOTE % why)
            sent = True
        except Exception as exc:
            self.b.log('old worker notice failed: ' + repr(exc))
            sent = False
        self.facts['worker_told'] = ('resume' if resume else 'hold') + ('' if sent else '_undelivered')
        if not sent:
            return ('继续原任务的通知没有送达，旧 worker 可能停在等待状态；' if resume else
                    '要求旧 worker 停止新动作的消息没有送达，它可能仍在运行，请协调者立刻处理；')
        known = self.note_delivery(receipt)
        self.facts['worker_told_delivery'] = known
        check = '请协调者核对终端 %s' % (self.worker_handle or '（句柄未知）')
        if known['state'] == 'delivered':
            return ('继续原任务的通知已送达（送达不代表已执行）；' if resume else
                    '旧 worker 可能仍在运行，已要求它停止新动作、不要恢复工作，通知已送达（送达不代表已执行），守卫没有停止它，由协调者决定；')
        fact = '未确认送达（state=%s）' % known['state']
        if resume:
            return '继续原任务的通知%s，旧 worker 可能仍停在等待状态；%s，确认它是否继续；' % (fact, check)
        return '旧 worker 可能仍在运行，已要求它停止新动作、不要恢复工作，但这条通知%s，守卫没有停止它；%s并立刻决定；' % (fact, check)

    def abandon(self, why, worker_live, called_off=False):
        """Not ours any more: change nothing else and say so. worker_live: it was asked to hand off and may still act."""
        self.finished = 'abandoned'
        # RESUME invalidates the report: persist the final phase before sending it (or HOLD), including any wait for
        # delivery. A hard death in send/confirmation must never turn a closed request back into a recoverable one.
        self.phase('abandoned', reason=why, alert_delivered=False)
        told = self.tell_old_worker(why, called_off) if worker_live else ''
        stopped = '旧 worker 已被守卫停止，需要协调者接管；' if self.stop_sent else ''
        return self.conclude('abandoned', why, '额度交接已放弃：%s。未改 Task、未启动接手者；%s%s守卫退出，%s 不再受额度保护。'
                             % (why, told, stopped, self.meta['dispatch_id']))

    def block(self, why):
        return self.conclude('blocked', why, why)

    def same_worktree(self, hand):
        return bool(hand['worktree']) and (
            os.path.realpath(hand['worktree']) == os.path.realpath(self.meta['worktree_path']))

    def stop_worker(self):
        """Bounded confirmation; no second writer if stop is ambiguous. Returns the final view."""
        self.stop_sent = True
        self.phase('stopping', stop_sent=True)
        self.b.stop()
        view = {}
        for _ in range(6):
            view = self.b.show()
            self.observe(view)
            if succeeded(view) or is_stopped(view):
                break
            self.b.sleep(5)
        return view

    def clean_exit(self):
        """Short-lived processes (git, file watchers) vanish; what remains is a second writer."""
        left = self.b.leftovers()
        for _ in range(2):
            if not left:
                break
            self.b.sleep(5)
            left = self.b.leftovers()
        return left

    def start_supervised(self, target, text):
        """The launch has no timeout of its own, so it gets a read-only watcher that can only tell someone."""
        done = threading.Event()
        watcher = threading.Thread(target=self.supervise_start, args=(done,), daemon=True)
        watcher.start()
        try:
            return self.b.start(target, text)
        finally:
            done.set()
            watcher.join(START_SUPERVISOR_JOIN_SECONDS)

    def supervise_start(self, done):
        """Never kills, retries or resends: an uncertain launch may already own a live writer."""
        begun, reported = time.monotonic(), False
        while not done.wait(min(HEARTBEAT_SECONDS, START_OVERDUE_SECONDS)):
            self.beat()
            if not reported and time.monotonic() - begun >= START_OVERDUE_SECONDS:
                reported = True
                try:
                    self.report_slow_start(done)
                except Exception as exc:
                    self.b.log('start supervision failed: ' + repr(exc))

    def report_slow_start(self, done):
        try:
            bound = self.b.task_state().get('dispatch_id')
        except Exception as exc:
            self.b.log('start supervision read failed: ' + repr(exc))
            bound = None
        possible = bound if bound and bound != self.meta['dispatch_id'] else None
        with self.phase_lock:
            if done.is_set():
                return
            self.phase('starting', start_overdue=True, possible_new_dispatch=possible)
        self.alert('接手 start 已超过 %d 分钟未返回（可能的新 dispatch：%s）。守卫不会重发也不会杀掉它，'
                   '旧 worker 已退出；请协调者核对，不要在确认前再次发起重试。'
                   % (START_OVERDUE_SECONDS // 60, possible or '未知'))

    def target_recheck(self, target):
        """Why the target can no longer take over, or None. Any failure to find out counts as a no."""
        try:
            if self.b.recheck(target):
                return None
            return getattr(self.b, 'plan_detail', '') or '目标不再可派发'
        except Exception as exc:
            self.b.log('target recheck failed: ' + repr(exc))
            return '复核本身失败：%s' % exc

    def coordinator_retry(self, target):
        return shlex.join([sys.executable, str(ROOT/'scripts'/'route.py'), 'start', '--run', self.meta['run_id'],
                           '--kind', self.meta['kind'], '--complexity', self.meta['complexity'],
                           '--spec-file', self.meta.get('original_spec_file', ''), '--task', self.meta['task_id'],
                           '--retry-of', self.meta['dispatch_id'], '--worktree', self.meta['worktree'],
                           '--profile', target, '--expected-profile', target])

    def delivery_summary(self):
        return ('\n交接通知事实：' + json.dumps(self.delivery, ensure_ascii=False)) if self.delivery else ''

    def delivery_record(self):
        self.phase('waiting', handoff_delivery=dict(self.delivery))
        self.b.log('handoff delivery ' + json.dumps(self.delivery, ensure_ascii=False))

    def finish_delivery(self, reason=None):
        if not self.delivery or self.delivery_decided:
            return
        self.delivery_decided = True
        self.delivery['elapsed_seconds'] = round(max(0, self.b.monotonic()-self.delivery_started), 3)
        if reason is not None:
            self.delivery['skip_reason'] = reason
        self.delivery_record()

    def check_delivery(self, view, task, handoff_path, deadline):
        """Called only after success/cancel/report checks. This does not move the grace deadline."""
        if self.delivery_decided:
            return
        if not self.cfg['delivery_fallback']['enabled']:
            self.finish_delivery('delivery_fallback 已关闭')
            return
        handle = terminal_handle(view)
        try:
            status = self.b.delivery_status(self.delivery['message_id'], handle)
        except Exception as exc:
            status = dict(state='unknown', source='error', reason=type(exc).__name__)
        self.delivery.update(state=status.get('state', 'unknown'), lookup_source=status.get('source'),
                             lookup_reason=status.get('reason'),
                             elapsed_seconds=round(max(0, self.b.monotonic()-self.delivery_started), 3))
        if self.delivery['state'] == 'delivered':
            self.finish_delivery('消息已送达')
            return
        self.delivery_record()
        if self.b.monotonic()-self.delivery_started < self.cfg['delivery_fallback']['wait_seconds']:
            return
        if self.b.monotonic() >= deadline:
            # What kept it from going in, if a read happened; only now is that the reason it was skipped.
            self.finish_delivery(self.delivery.get('screen_reason') or '已到输入截止时间，不再输入')
            return
        try:
            probe = self.b.codex_running(view)
        except Exception as exc:
            probe = dict(safe=False, reason=CANNOT_CONFIRM + type(exc).__name__)
        self.delivery['screen_reason'] = probe.get('reason')
        self.delivery['screen_checks'] = self.delivery.get('screen_checks', 0) + 1
        if not probe.get('safe'):
            # One bad read says little: a slow Orca or a passing frame. The next poll looks again.
            self.delivery['screen_reason'] = probe.get('reason') or CANNOT_CONFIRM + '读屏没有给出结论'
            self.delivery_record()
            return
        # Read-only screen/CLI calls may take time; ownership, report and deadline must still hold.
        fresh = self.b.show()
        why = self.cancel_reason(task)
        if succeeded(fresh) or attempt_ended(fresh) or is_stopped(fresh) or why or read_handoff(handoff_path)['complete']:
            self.finish_delivery(why or '等待或读屏期间 worker 已完成、停止或报告已写成')
            return
        if self.b.monotonic() >= deadline:
            self.finish_delivery('已到输入截止时间，不再输入')
            return
        def same_terminal(current):
            current_handle = terminal_handle(current)
            current_incarnation = (current.get('terminal') or {}).get('incarnationId')
            return (current_handle == probe.get('handle') and (not probe.get('incarnation_id') or
                    current_incarnation == probe['incarnation_id']))
        if not same_terminal(fresh):
            self.finish_delivery('读屏期间终端句柄或进程已替换，不输入')
            return
        # A separate entry leaves the immutable original/latest entry intact, including after cancellation.
        entry = self.state/(self.meta['dispatch_id']+'-delivery-context.md')
        text = delivery_input_text(str(entry))
        if text is None:
            self.finish_delivery('交接入口路径含不安全字符，不输入')
            return
        try:
            original = Path(self.meta['context_file']).read_text()
            write_private(entry, original + '\n\n## 当前额度守卫交接请求\n' + self.handoff_message(handoff_path))
        except (OSError, KeyError) as exc:
            self.finish_delivery('交接入口不可读写：' + type(exc).__name__)
            return
        # Final delivery/ownership recheck after all reads and entry preparation.
        try:
            status = self.b.delivery_status(self.delivery['message_id'], probe['handle'])
        except Exception as exc:
            status = dict(state='unknown', source='error', reason=type(exc).__name__)
        self.delivery.update(state=status.get('state', 'unknown'), lookup_source=status.get('source'),
                             lookup_reason=status.get('reason'))
        if self.delivery['state'] == 'delivered':
            self.finish_delivery('消息已送达')
            return
        fresh = self.b.show()
        why = self.cancel_reason(task)
        if (why or succeeded(fresh) or attempt_ended(fresh) or is_stopped(fresh)
                or read_handoff(handoff_path)['complete'] or self.b.monotonic() >= deadline or not same_terminal(fresh)):
            self.finish_delivery(why or '输入前 worker 已完成、停止、报告已写成或已到输入截止时间')
            return
        latch = self.state/(self.meta['dispatch_id']+'-terminal-input.json')
        try:
            fd = os.open(latch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            previous = _read_json(latch)
            self.delivery.update(terminal_input_attempted=True, terminal_input_used=previous.get('terminal_input_used', False),
                                 retry_request=previous.get('retry_request'))
            self.finish_delivery('本次交接已尝试终端输入，不重发；恢复只能用原 Orca --retry-request')
            return
        self.delivery.update(terminal_input_attempted=True, terminal_handle=probe['handle'],
                             terminal_incarnation=probe.get('incarnation_id'), entry_file=str(entry), input_text=text)
        with os.fdopen(fd, 'w') as fh:
            json.dump(self.delivery, fh, ensure_ascii=False)
        # Persist before the one mutating call: an ambiguous failure never retries with a fresh request.
        self.delivery_record()
        try:
            receipt = self.b.terminal_input(probe['handle'], text)
            self.delivery.update(terminal_input_used=receipt.get('accepted') is True,
                                 retry_request=receipt.get('request_id'), input_warnings=receipt.get('warnings', []))
            write_private(latch, json.dumps(self.delivery, ensure_ascii=False))
            self.finish_delivery('' if receipt.get('accepted') else '终端输入未确认接受，不重发')
        except Exception as exc:
            self.finish_delivery('终端输入结果未知，不重发；仅允许按 Orca 回执的 --retry-request 恢复：' + type(exc).__name__)

    def input_until(self, started):
        """Last moment for terminal input: after it the worker could not finish a report inside the grace period."""
        grace = self.cfg['grace_minutes'] * 60
        return started + grace - min(REPORT_RESERVE_SECONDS, grace / 3)

    def request_path(self):
        return self.state/(self.meta['dispatch_id']+'-handoff-request.json')

    def pending_request(self):
        """The nonfinal request on disk, including an expired one so recovery can explicitly give up."""
        phase = _read_json(self.state/(self.meta['dispatch_id']+'-phase.json')).get('phase')
        request = _read_json(self.request_path())
        if phase and phase not in FINAL_PHASES and quota._num(request.get('until')):
            return request
        return None

    def request_still_open(self):
        """A recent crash in this request; final phase is persisted before any RESUME/HOLD notification.

        The request is recorded only after retiring older reports. Its grace + 5 minute window is never extended.
        """
        request = self.pending_request()
        return bool(request and self.b.clock() <= request['until'])

    def recovery_report(self):
        """A complete response to this still-open request replaces screen evidence, never ownership/exit checks."""
        return self.request_still_open() and read_handoff(self.state/(self.meta['dispatch_id']+'-handoff.md'))['complete']

    def retire_old_report(self, handoff_path):
        """A report answers one request, so nothing older may stand in for the answer to this one. Content is kept."""
        if handoff_path.exists():
            stem = '%s-handoff.stale-%d-%s.md' % (self.meta['dispatch_id'], int(self.b.clock()), uuid.uuid4().hex[:8])
            os.rename(handoff_path, self.state/stem)

    def confirm_before_request(self, pred):
        """Fresh worker, Task and (for an uncertain start) screen, right before anyone hears of a handoff.

        The prerequisites can take minutes, so what tick read may be stale. (task, None) to go on, (None, outcome) to
        stop: nobody has been asked to do anything yet, so no outcome here touches the worker.
        """
        try:
            view = self.b.show()
            if settled(view):
                return None, 'done'
            running = ((True if self.recovery_report() else self.start_unknown_evidence(view, pred))
                       if start_unknown_view(view) else None)
            if running is False:
                return None, 'continue'
            task = self.b.task_state()
        except Exception as exc:
            self.alert_once('check_failed', '交接前置检查失败：%s；未通知也未停止 worker。' % exc)
            return None, 'continue'
        why = self.not_ours(view, task)
        if why == 'uncertain':
            return None, self.wait_on_uncertain()
        self.uncertain_since = None
        if why == START_UNKNOWN:
            if not running:
                self.b.log('start_unknown attempt without evidence right before the request; judging again next round')
                return None, 'continue'
        elif why:
            return None, self.abandon(why, False)
        self.worker_handle = terminal_handle(view)
        return dict(task, view=view), None

    def evidence_lost(self, view):
        """Why an uncertain start can no longer be said to be running, or None. Only asked at the end of the grace period."""
        try:
            probe = self.b.codex_running(view)
        except Exception as exc:
            probe = dict(safe=False, reason=CANNOT_CONFIRM + type(exc).__name__)
        if probe.get('safe'):
            return None
        return probe.get('reason') or CANNOT_CONFIRM + '读屏没有给出结论'

    def handoff(self, pred, target, original, before):
        handoff_path = self.state/(self.meta['dispatch_id']+'-handoff.md')
        limit = self.config['constraints']['max_attempts_per_task']
        fresh, outcome = self.confirm_before_request(pred)
        if fresh is None:
            return outcome
        uncertain_start = start_unknown_view(fresh.pop('view'))
        task = fresh
        recovering = self.request_still_open()
        try:
            if not recovering:
                self.retire_old_report(handoff_path)
                write_private(self.request_path(), json.dumps(dict(
                    opened_at=self.b.clock(), until=self.b.clock() + (self.cfg['grace_minutes'] + 5) * 60)))
                self.cancel_path().unlink(missing_ok=True)
            self.b.send('run:' + self.meta['run_id'], self.coordinator_message(target))
        except Exception as exc:
            # Nothing was asked of the worker yet, so this stays an ordinary failed prerequisite.
            self.alert_once('check_failed', '无法准备或通知协调者交接开始：%s；未通知也未停止 worker。' % exc)
            return 'continue'
        # Once the worker is notified, any ambiguity is terminal for this guard.
        self.finished = 'blocked'
        self.phase('notifying', target=target)
        self.delivery_started = self.b.monotonic()
        self.delivery = dict(message_id=None, state='unknown', elapsed_seconds=0,
                             terminal_input_attempted=False, terminal_input_used=False, skip_reason='等待送达检查',
                             input_deadline_seconds=round(self.input_until(0)))
        self.phase('notifying', handoff_delivery=dict(self.delivery))
        receipt = self.b.send('dispatch:' + self.meta['dispatch_id'], self.handoff_message(handoff_path))
        self.delivery['message_id'] = (receipt or {}).get('messageId')
        self.phase('waiting', handoff_delivery=dict(self.delivery))
        deadline = self.delivery_started + self.cfg['grace_minutes']*60
        input_until = self.input_until(self.delivery_started)
        at_marker, marker_error = None, None
        while True:
            self.beat()
            view = self.b.show()
            self.observe(view)
            self.worker_handle = terminal_handle(view) or self.worker_handle
            if succeeded(view):
                self.finish_delivery('等待期间 worker 已完成')
                return self.worker_succeeded()
            hand = read_handoff(handoff_path)
            # The guard has sent no stop yet, so all of this is someone else's doing.
            if attempt_ended(view) or (is_stopped(view) and not hand['complete']):
                self.finish_delivery('等待期间 worker 已停止或结算')
                return self.abandon('旧 worker 被外部停止、取消或已结算', False)
            called_off = self.called_off()
            why = called_off or self.task_moved(task)
            if why:
                self.finish_delivery(why)
                return self.abandon(why, not is_stopped(view), called_off=bool(called_off))
            if hand['complete']:
                self.finish_delivery('完整交接报告已收到，不再输入')
                if not self.same_worktree(hand):
                    return self.abandon('交接报告的工作目录与登记的 worktree 不一致或缺失，无法确定现场', not is_stopped(view))
                # Whatever changes from here on was not written by the worker's handoff.
                try:
                    at_marker = self.b.fingerprint()
                except Exception as exc:
                    marker_error = exc
                break
            self.check_delivery(view, task, handoff_path, input_until)
            if self.b.monotonic() >= deadline:
                # The final alert must not present the 20-second observation as current delivery state.
                if self.cfg['delivery_fallback']['enabled'] and self.delivery['state'] != 'delivered':
                    try:
                        status = self.b.delivery_status(self.delivery['message_id'], terminal_handle(view))
                    except Exception as exc:
                        status = dict(state='unknown', source='error', reason=type(exc).__name__)
                    self.delivery.update(state=status.get('state', 'unknown'), lookup_source=status.get('source'),
                                         lookup_reason=status.get('reason'),
                                         last_checked_elapsed_seconds=round(max(0, self.b.monotonic()-self.delivery_started), 3))
                    self.delivery_record()
                self.finish_delivery('宽限期已结束，不再输入')
                # Entering the handoff needed proof this start is running. Without it now, no stop: evidence missing
                # leaves the worker alone at every point, and a stop here would end a process no one can vouch for.
                if uncertain_start:
                    lost = self.evidence_lost(view)
                    if lost:
                        return self.abandon('宽限期结束没有完整交接报告，且启动结果不确定的 worker 已无运行证据（%s）' % lost, True)
                break
            wait_until = deadline
            if not self.delivery_decided:
                # Sleep to the end of the mail wait, then poll again up to the last moment for input.
                mail_wait = self.delivery_started + self.cfg['delivery_fallback']['wait_seconds']
                wait_until = min(deadline, mail_wait if self.b.monotonic() < mail_wait else input_until)
            self.b.sleep(min(5, max(0.01, wait_until-self.b.monotonic())))
        if not is_stopped(view):
            view = self.stop_worker()
            if succeeded(view):
                return self.worker_succeeded()
            if not is_stopped(view):
                return self.block('无法确认旧 worker 已停止，禁止启动第二个写入者；请协调者处理。')
        if not hand['complete']:
            state = '交接文件缺失' if hand['text'] is None else '交接文件没有结束标记，可能未写完：%s' % handoff_path
            cmd = [sys.executable, str(ROOT/'scripts'/'route.py'), 'start', '--run', self.meta['run_id'],
                   '--kind', self.meta['kind'], '--complexity', self.meta['complexity'],
                   '--spec-file', self.meta.get('original_spec_file', ''), '--task', self.meta['task_id'],
                   '--retry-of', self.meta['dispatch_id'], '--worktree', self.meta['worktree'],
                   '--profile', target, '--expected-profile', target]
            return self.block('宽限期结束仍没有完整交接报告（%s）。守卫已停止旧 worker 并确认退出；未改 Task、未占尝试次数、未启动接手者。'
                              '任务需要协调者处理：核对 worktree 现场后可重试：%s' % (state, shlex.join(cmd)))
        if marker_error:
            return self.block('旧 worker 已停止，但取不到 worktree 指纹（%s），无法判断之后有没有其他写入，未启动接手 worker；请协调者处理。' % marker_error)
        self.phase('verifying')
        # Re-read exact state immediately before changing Task ownership.
        view = self.b.show()
        self.observe(view)
        if succeeded(view):
            return self.worker_succeeded()
        if cancelled_view(view):
            return self.abandon('dispatch 已被显式取消或放弃', False)
        if not is_stopped(view):
            return self.block('停止证据不再成立，未启动接手 worker。')
        if self.b.attempts() >= limit:
            return self.block('交接等待期间尝试数已满，未启动接手 worker。')
        # Before overwriting the shared context entry, which a coordinator retry would also read.
        why = self.cancel_reason(task)
        if why:
            return self.abandon(why, False)
        hand = read_handoff(handoff_path)
        if not hand['complete'] or not self.same_worktree(hand):
            return self.block('交接报告在复核时已不完整或工作目录不一致，未启动接手 worker；请协调者处理。')
        pids = hand['background_pids']
        if pids is None:
            return self.block('交接报告的 background_pids 无法解析（须为 none 或 PID 列表），不能确认后台进程已退出，未启动接手 worker；请协调者处理。')
        alive = self.b.live_pids(pids)
        if alive:
            return self.block('交接报告列出的后台 PID %s 仍存活，未启动接手 worker；请协调者处理。' % alive)
        left = self.clean_exit()
        if left:
            return self.block('旧 worker 结束后 worktree 内仍有在 dispatch 之后启动、不属于协调者进程树的进程（pid、命令名、启动时间）%s，'
                              '未启动接手 worker；请协调者确认它们是谁的。' % json.dumps(left, ensure_ascii=False))
        if at_marker['head'] != before['head']:
            return self.abandon('交接期间 HEAD 已变化（有人提交了）', False)
        # The last moment a no costs nothing: before the shared context entry and the Task change.
        refused = self.target_recheck(target)
        if refused:
            return self.block('旧 worker 已停止，但接手前复核未通过：%s。未改 Task、未占尝试次数、未启动接手者，worktree 现场和交接报告 %s 保留。'
                              '任务需要协调者处理：目标额度恢复后可重试（普通规则，额度未知时仍会阻断）：%s'
                              % (refused, handoff_path, self.coordinator_retry(target)))
        text = self.b.context(original, hand['text'])
        # Last look: from here on the Task and a new writer change hands.
        if cancelled_view(self.b.show()):
            return self.abandon('dispatch 已被显式取消或放弃', False)
        why = self.cancel_reason(task)
        if why:
            return self.abandon(why, False)
        if self.b.fingerprint() != at_marker:
            return self.abandon('旧 worker 结束后 worktree 又有变化，说明已有其他写入者', False)
        self.phase('starting')
        self.b.fail_task()
        self.phase('starting', task_rewritten=True, start_attempted=True)
        code, receipt = self.start_supervised(target, text)
        new_dispatch = (receipt.get('result') or {}).get('dispatchId')
        # Identity first: everything after this can fail, and none of it may lose track of a live writer.
        self.phase('starting', new_dispatch=new_dispatch, start_exit=code)
        if code or not new_dispatch:
            refused = start_refusal(receipt)
            return self.block('接手 start 返回 %s（dispatch=%s）%s，不重试、不自行换档；Task 已标 failed，旧 worker 已停止；请核对可能的残留资源。'
                              '目标额度恢复后可重试（普通规则，额度未知时仍会阻断）：%s'
                              % (code, new_dispatch, '，原因：' + refused if refused else '', self.coordinator_retry(target)))
        start_state = (receipt.get('result') or {}).get('state')
        # Concise view: exit 0 with a dispatch id also covers a start Orca did not see begin. Only ready says it did.
        uncertain = start_state not in (None, 'ready')
        self.pool_lock.mark_handoff(self.b.clock())
        if uncertain:
            self.finished = TAKEOVER_UNCERTAIN
            self.phase(TAKEOVER_UNCERTAIN, start_state=start_state, alert_delivered=False)
        else:
            self.finished = 'handoff'
            self.phase('handoff_done', alert_delivered=False)
        stale = getattr(self.b, 'target_stale', None)
        event = dict(event='quota_handoff', old_dispatch=self.meta['dispatch_id'], new_dispatch=new_dispatch,
                     old_profile=self.meta['profile'], new_profile=target, task_id=self.meta['task_id'],
                     run_id=self.meta['run_id'], trigger=pred)
        if uncertain:
            event['start_state'] = start_state
        if stale:
            event['target_stale_data'] = stale
        try:
            self.b.record(event)
        except Exception as exc:
            self.b.log('usage-log failed: ' + repr(exc))
        if uncertain:
            # The Task stays as Orca left it: Orca blocks it in the same step, and only the new attempt's report or the
            # coordinator may conclude it. Nothing here can tell a live worker from a failed start, so nothing is touched.
            terminal = (receipt.get('result') or {}).get('terminal')
            why = ('旧 worker 已确认退出，接手 start 回执 state=%s：%s (%s) 的启动结果不确定，可能在运行，也可能已经失败退回 shell。'
                   % (quota._safe(start_state), new_dispatch, target))
            return self.conclude(TAKEOVER_UNCERTAIN, why, (
                '额度交接未确认完成：' + why + '守卫不重发、不杀进程，Task 保持 Orca 留下的状态（通常是 blocked），不要重发启动。'
                '只读核对：orca orchestration worker-show --dispatch %s --json --full；'
                'orca terminal read --terminal %s --screen --json。'
                '在运行就跟踪它，它会自己报告；确认没有运行且 quota_guard.py status 没有它的活动守卫后，才由唯一的重试操作者决定是否重试。'
                % (new_dispatch, terminal or '<worker-show 里的终端句柄>')))
        message = '已完成额度交接：%s (%s) → %s (%s)；同一 worktree，旧 worker 已确认退出。%s' % (
            self.meta['dispatch_id'], self.meta['profile'], new_dispatch, target,
            '接手判断用了过期的额度数据（年龄 %.1f 分钟），实际额度未核实。' % stale['age_minutes'] if stale else '')
        try:
            self.b.send('run:' + self.meta['run_id'], message + self.delivery_summary())
            delivered = True
        except Exception as exc:
            self.b.log('handoff notice failed: ' + repr(exc))
            delivered = self.alert(message)
        self.phase('handoff_done', alert_delivered=delivered)
        return 'handoff'


class HandoffLock:
    """At most one handoff per pool at a time, spaced so plan sees the previous one's effect."""
    def __init__(self, state, pool):
        self.path = Path(state)/('handoff-'+safe_dispatch(pool)+'.flock')
        self.fh = None

    def acquire(self, now, spacing):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fh = self.path.open('a+')
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close(); self.fh = None
            return False
        self.fh.seek(0)
        try:
            last = float(self.fh.read() or 0)
        except ValueError:
            last = 0
        if now - last < spacing:
            self.release()
            return False
        return True

    def mark_handoff(self, now):
        if self.fh:
            self.fh.seek(0); self.fh.truncate()
            self.fh.write(str(now))
            self.fh.flush()

    def release(self):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close(); self.fh = None


def _run_alert_store(state, run_id, change):
    path = Path(state)/('run-'+safe_dispatch(run_id)+'-alerts.json')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix('.flock').open('a+') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        result = change(data)
        write_private(path, json.dumps(data))
    return result


def claim_run_alert(state, run_id, key, now):
    def change(data):
        if now - data.get(key, 0) < RUN_ALERT_DEDUP_SECONDS:
            return False
        data[key] = now
        return True
    return _run_alert_store(state, run_id, change)


def clear_run_alert(state, run_id, key):
    _run_alert_store(state, run_id, lambda data: data.pop(key, None))


class DispatchLock:
    def __init__(self, state, dispatch):
        self.path = Path(state)/(safe_dispatch(dispatch)+'.lock')
        self.dispatch, self.fh = dispatch, None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fh = self.path.open('a+')
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fh.close(); self.fh = None
            return False
        self.fh.seek(0); self.fh.truncate()
        self.fh.write(json.dumps(dict(dispatch_id=self.dispatch, pid=os.getpid(), started_at=time.time())))
        self.fh.flush()
        return True

    def close(self):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close(); self.fh = None
        # Never unlink a lock inode; a concurrent process could hold the old inode.


def active_guards(state=STATE):
    active = []
    for path in Path(state).glob('*.lock'):
        with path.open('r+') as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                try:
                    active.append(json.loads(fh.read()))
                except ValueError:
                    active.append(dict(dispatch_id=path.stem, status='starting'))
            else:
                fcntl.flock(fh, fcntl.LOCK_UN)
    return active


def called_by_guard():
    """True when this process's direct parent is a live guard."""
    return any(g.get('pid') == os.getppid() for g in active_guards(STATE))


def takeover_refusal(dispatch, action, kind, complexity, args, config):
    """Why this route.py call may not use stale quota, or None. Only a live guard, as direct parent, for its own Task."""
    try:
        meta = load_meta(dispatch, STATE)
    except (OSError, ValueError, TypeError):
        return '没有 %s 的守卫记录' % quota._safe(dispatch)
    holder = next((g for g in active_guards(STATE) if g.get('dispatch_id') == dispatch), None)
    if holder is None or holder.get('pid') != os.getppid():
        return '调用方不是 %s 的存活守卫' % quota._safe(dispatch)
    if meta.get('kind') != kind or meta.get('complexity') != complexity:
        return '任务类型或复杂度与守卫记录不符'
    rule = config['tasks'][kind]
    if args.profile not in (rule['primary'], rule['alternate']):
        return '必须显式指定任务的 primary 或 alternate'
    if config['models'][args.profile].get('agent') == meta.get('agent'):
        return '接手目标必须在其他账户池'
    if action == 'start' and (args.retry_of != dispatch or args.task != meta.get('task_id')):
        return 'start 必须是对该 dispatch 的重试'
    return None


def retry_refused_by_guard(dispatch):
    """Why a retry of this dispatch has to wait for its live guard, or None.

    The lock is held for the guard's whole life, so while it is held the guard may be starting this same retry.
    The one caller let through is the guard itself, which runs route.py as its direct child.
    """
    holder = next((g for g in active_guards(STATE) if g.get('dispatch_id') == dispatch), None)
    if holder is None or holder.get('pid') == os.getppid():
        return None
    return ('旧 dispatch %s 的额度守卫仍在运行（pid %s），它可能正在或将要发起同一次重试，同一 Task 只能有一个重试操作者；'
            '运行 quota_guard.py status 查看，等守卫退出并核对现场后再重试' % (dispatch, holder.get('pid')))


def spawn_guard(meta, state=STATE):
    """Failure never propagates to route.start; output is isolated from its PTY."""
    try:
        dispatch = safe_dispatch(meta['dispatch_id'])
        state = Path(state)
        write_private(state/(dispatch+'.json'), json.dumps(meta, ensure_ascii=False, indent=2))
        log_path = state/(dispatch+'.log')
        # Holds git status and the old worker's output excerpts.
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'a') as log:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'watch', '--dispatch', dispatch,
                                     '--state-dir', str(state)], cwd='/', stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, start_new_session=True, close_fds=True)
        # Verify acquisition instead of claiming a successful Popen means a working guard.
        for _ in range(20):
            active = next((r for r in active_guards(state) if r['dispatch_id'] == dispatch), None)
            if active:
                return dict(started=True, pid=active.get('pid'), log=str(log_path))
            if proc.poll() is not None:
                return dict(started=False, reason='guard exited %s; inspect %s' % (proc.returncode, log_path))
            time.sleep(.1)
        return dict(started=False, reason='guard startup unconfirmed; inspect '+str(log_path))
    except Exception as exc:
        return dict(started=False, reason=str(exc))


def register_start(config, plan, args, receipt, context, state=STATE, started_at=None):
    cfg = settings(config)
    if not protected(cfg, plan['agent']):
        return dict(started=False, reason='disabled or unprotected account pool')
    result = (receipt or {}).get('result') or {}
    dispatch = result.get('dispatchId')
    if not dispatch or not result.get('taskId'):
        return dict(started=False, reason='launch receipt lacks dispatch/task id')
    # Resolve actual placement, especially new-child; never retry as "current".
    info = Backend(config, {}, state).command(['worker-show', '--dispatch', dispatch])
    worker = info.get('worker') or {}
    terminal = info.get('terminal') or {}
    path = terminal.get('worktreePath')
    wid = worker.get('worktreeId')
    if getattr(args, 'worktree', None) == 'current':
        # The entry already went out, so a miss only leaves the worker unguarded; its context file stays readable.
        placement = local_placement(terminal)
        if not placement or placement[0] != wid:
            return dict(started=False, reason='无法从 worker 终端可靠解析实际 worktree，本次不受保护')
        expected = context.get('preflight_worktree_id')
        if placement[0] != expected:
            return dict(started=False, reason='worker 实际 worktree %s 与预检结果 %s 不符，本次不受保护' % (placement[0], expected))
    if not path and isinstance(wid, str) and '::' in wid:
        candidate = wid.split('::', 1)[1]
        if Path(candidate).is_absolute() and Path(candidate).is_dir():
            path = candidate
    host = (info.get('dispatch') or {}).get('hostScope') or {}
    if host.get('kind', 'local') != 'local' or not path or not wid or not context:
        return dict(started=False, reason='exact local worktree/context unavailable; coordinator required')
    if not getattr(args, 'retry_of', None):
        foreign = foreign_worktrees(Path(context['original_spec_file']).read_text(), path)
        if foreign:
            return dict(started=False, reason='说明里出现登记 worktree 之外的 git 目录 %s，无法确定实际工作目录，本次不受保护'
                        % json.dumps(foreign, ensure_ascii=False))
    creator = Backend(config, {}, state).command(['dispatch-show', '--task', result['taskId']]).get('dispatch') or {}
    coordinator = args.coordinator or creator.get('creator_handle') or creator.get('creatorHandle')
    if not coordinator:
        return dict(started=False, reason='coordinator identity unavailable')
    meta = dict(dispatch_id=dispatch, task_id=result['taskId'], run_id=args.run,
                kind=plan['kind'], complexity=plan['complexity'], profile=plan['profile'], agent=plan['agent'],
                worktree='id:'+wid, worktree_path=path, coordinator=coordinator,
                dispatch_started_at=time.time() if started_at is None else started_at,
                coordinator_roots=recorded_roots(path, getattr(args, 'retry_of', None), state),
                budget_session=args.budget_session, budget_weekly=args.budget_weekly, **context)
    return dict(spawn_guard(meta, state), coordinator_roots=meta['coordinator_roots'])


def interrupted_handoffs(state=STATE):
    """Handoffs that began but whose guard is gone: the coordinator must take over."""
    live = {r.get('dispatch_id') for r in active_guards(state)}
    found = []
    for path in Path(state).glob('*-phase.json'):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get('phase') not in FINAL_PHASES and record.get('dispatch_id') not in live:
            found.append(record)
    return found


def undelivered_outcomes(state=STATE):
    """Finished handoffs whose alert no one confirmed receiving: the coordinator has not heard of them.

    Records written before this field existed say nothing about delivery and are left out.
    """
    live = {r.get('dispatch_id') for r in active_guards(state)}
    found = []
    for path in Path(state).glob('*-phase.json'):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (record.get('phase') in FINAL_PHASES and record.get('alert_delivered') is False and
                record.get('dispatch_id') not in live):
            found.append(record)
    return found


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def guard_progress(record, state, now):
    """One live guard with its phase, how long it has been there and its last heartbeat. Flags, never acts."""
    dispatch = record.get('dispatch_id') or ''
    phase = _read_json(Path(state)/(dispatch+'-phase.json'))
    beat = _read_json(Path(state)/(dispatch+'-heartbeat.json'))
    view = dict(record, phase=phase.get('phase'), phase_age_seconds=None, last_heartbeat_age_seconds=None,
                overdue=False, overdue_reasons=[], protection_degraded=beat.get('protection_degraded'))
    if quota._num(phase.get('at')):
        view['phase_age_seconds'] = int(now - phase['at'])
        if phase.get('phase') not in FINAL_PHASES:
            limit = phase.get('limit_seconds') or DEFAULT_PHASE_LIMIT_SECONDS
            if now - phase['at'] > limit:
                view['overdue_reasons'].append('阶段 %s 超过 %d 秒' % (phase.get('phase'), limit))
    if quota._num(beat.get('at')):
        view['last_heartbeat_age_seconds'] = int(now - beat['at'])
        limit = beat.get('limit_seconds') or DEFAULT_PHASE_LIMIT_SECONDS
        if now - beat['at'] > limit:
            view['overdue_reasons'].append('心跳超过 %d 秒' % limit)
    for key in ('target', 'stop_sent', 'old_worker_stopped', 'start_attempted', 'new_dispatch', 'possible_new_dispatch', 'start_state', 'handoff_delivery', 'start_unknown'):
        if key in phase:
            view[key] = phase[key]
    view['overdue'] = bool(view['overdue_reasons'])
    return view


def bound_dispatch(record, state):
    """Read-only: the dispatch the Task is bound to now, 'unknown' if Orca cannot say."""
    try:
        bound = Backend({}, load_meta(record['dispatch_id'], state), state).task_state().get('dispatch_id')
    except Exception:
        return 'unknown'
    return bound if bound != record['dispatch_id'] else None


def status_report(state=STATE, now=None, lookup=None):
    now = time.time() if now is None else now
    active = [guard_progress(r, state, now) for r in active_guards(state)]
    interrupted, undelivered = interrupted_handoffs(state), undelivered_outcomes(state)
    if lookup:
        for record in interrupted + undelivered + active:
            # A launch that never reported back may still have produced a live writer.
            if record.get('start_attempted') and not record.get('new_dispatch') and not record.get('possible_new_dispatch'):
                record['possible_new_dispatch'] = lookup(record, state)
    return dict(active_guards=active, overdue_guards=[r.get('dispatch_id') for r in active if r['overdue']],
                interrupted_handoffs=interrupted, undelivered_outcomes=undelivered)


def restore_reminder_status():
    """Read-only for `status`: no refresh, and it never uses up the window's one notice."""
    try:
        pol = quota.validate_policy(load_config(ROOT)['quota_policy'])
        view = quota.restore_reminder(pol, quota.fetch_snapshot(pol), int(time.time() * 1000))
        if view.get('active'):
            view['already_notified'] = bool(quota.reminder_noted(view['resets_at_ms']))
        return view
    except Exception as exc:
        return dict(active=False, reason='读取失败：%s' % type(exc).__name__)


def watch(guard, meta, backend, load_config, fetch_snapshot):
    """Poll until the attempt ends. Repeated errors alert, then end, so silence never means protected."""
    errors = 0
    while True:
        try:
            # Reload disable/config thresholds while preserving one-dispatch ownership.
            config = load_config()
            guard.config, guard.cfg = config, settings(config)
            backend.config = config
            if not protected(guard.cfg, meta['agent']):
                return 0
            outcome = guard.tick(fetch_snapshot(quota.validate_policy(config['quota_policy'])))
            errors = 0
            if outcome != 'continue':
                backend.log('guard exit: ' + outcome)
                return 0 if outcome in ('done', 'handoff', 'abandoned') else 1
        except Exception as exc:
            backend.log('guard error: ' + repr(exc))
            errors += 1
            message = None
            if guard.finished:
                message = '额度交接流程异常，守卫停止且不重试：%s' % exc
            elif errors >= ERROR_EXIT_AFTER:
                message = '额度守卫连续 %d 次检查失败，已退出；%s 不再受额度保护：%s' % (errors, meta['dispatch_id'], exc)
            elif errors == ERROR_ALERT_AFTER:
                message = '额度守卫连续 %d 次检查失败，仍在重试；此期间 %s 没有额度保护：%s' % (errors, meta['dispatch_id'], exc)
            if message:
                try:
                    guard.alert(message)
                except Exception as delivery:
                    backend.log('alert failed: ' + repr(delivery))
            if guard.finished or errors >= ERROR_EXIT_AFTER:
                return 1
        backend.sleep(guard.cfg['poll_seconds'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['watch','status'], nargs='?', default='watch')
    parser.add_argument('--dispatch')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--state-dir', type=Path, default=STATE)
    args = parser.parse_args(argv)
    if args.action == 'status':
        print(json.dumps(dict(status_report(args.state_dir, lookup=bound_dispatch),
                              restore_reminder=restore_reminder_status()), ensure_ascii=False, indent=2))
        return 0
    config = load_config(ROOT)
    cfg = settings(config)
    if args.dry_run:
        meta = load_meta(args.dispatch,args.state_dir) if args.dispatch else None
        pol = quota.validate_policy(config['quota_policy'])
        snap = quota.fetch_snapshot(pol)
        pools = [meta['agent']] if meta else cfg['pools']
        report = dict(dry_run=True, enabled=cfg['enabled'], actions=[], predictions={})
        for pool in pools:
            report['predictions'][pool] = predict(config,pool,snap,time.time())
        if meta:
            b = Backend(config,meta,args.state_dir)
            report['worker'] = dict(settled=settled(b.show()))
            report['attempts'] = b.attempts()
            report['original_available'] = bool(b.original())
            report['target'] = b.plan(takeover=False) if report['original_available'] else None
        report['actions'] = ['若有效数据触发且 Orca 记录启动结果不确定（worker state=start_unknown）：Codex 读屏确认在运行才继续，否则只告警一次、不碰 worker 和 Task；其他 agent 只告警',
                             '若有效数据触发：取得账户池交接锁；Task 必须活跃、仍绑定本 dispatch 且没有结论（上一条确认过的 start_unknown 例外：Task 是 blocked，未绑定其他 dispatch），否则放弃且不通知 worker；确认原始任务、尝试数、其他池可派发目标、worktree 指纹（含 Git 子模块则只告警）',
                             '第一条消息之前再读一次 worker 和 Task（启动结果不确定的尝试还要重新读屏），不符就放弃或只告警；把此前已有的交接文件改名归档（守卫崩溃后恢复同一次交接时除外）',
                             '先通知 Run 交接进行中（含取消方法），再通知旧 dispatch 写带结束标记的交接文件并结束回合',
                             '外部停止、取消、关闭守卫或 Task 变化则放弃；worker 成功则放弃；交接文件写完或宽限期到后 worker-stop 并确认退出（启动结果不确定的尝试到期已无运行证据时不 stop，只告警放弃）',
                             '宽限期结束仍没有完整报告：只告警后退出，不改 Task、不占尝试、不启动接手者',
                             '校验工作目录与 background_pids、扫描残留进程、核对 HEAD；再跑一次 plan 复核目标，不通过只告警，不改 Task、不占尝试；只读 git status --short / git diff HEAD --stat；写接手说明入口；再核对 worktree 指纹',
                             'Task 标 failed（额度交接，非模型失败）；route.py start --task --retry-of',
                             '返回 2/3 或不确定失败告警并写明拒绝原因；成功先落盘新 dispatch，再按 start 的实际判断记录 quota_handoff 并通知 run；回执 state=outcome_unknown 时只告警启动结果不确定和核对办法，不写已完成，不重发']
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0
    if not args.dispatch:
        parser.error('watch requires --dispatch (or use --dry-run for quota-only preview)')
    meta = load_meta(args.dispatch,args.state_dir)
    if not cfg['enabled'] or meta['agent'] not in cfg['pools']:
        return 0
    lock = DispatchLock(args.state_dir,args.dispatch)
    if not lock.acquire():
        print('guard already active',flush=True)
        return 0
    b = Backend(config,meta,args.state_dir)
    guard = Guard(config,meta,b,args.state_dir)
    try:
        return watch(guard, meta, b, lambda: load_config(ROOT),
                     lambda pol: quota.fresh_snapshot(pol, [pol['provider_keys'].get(meta['agent'])],
                                                      refresher=quota.refresh_usage_rpc))
    finally:
        lock.close()


if __name__ == '__main__':
    sys.exit(main())
