#!/usr/bin/env python3
"""Map a classified task to one Orca worker, choosing between the task's own
primary/alternate by account quota. No hidden retries, no auto account actions."""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from jev_route import classify
import quota
from quota import load_config

ROOT = Path(__file__).resolve().parents[1]
ORCA = '/usr/local/bin/orca'
if not Path(ORCA).is_file():
    ORCA = 'orca'

def explain(config):
    policy = config['quota_policy']
    windows = policy['windows']
    window_text = ' 与 '.join('%s(%s分钟，权重%s)' % (
        name, item['expected_minutes'], item['weight'])
        for name, item in windows.items())
    reserve_parts = ['%s: %s' % (agent, ', '.join('%s=%s' % (w, v) for w, v in per.items()))
                     for agent, per in policy.get('reserves', {}).items()
                     if any(v > 0 for v in per.values())]
    reserves = '；'.join(reserve_parts) if reserve_parts else '无'
    unknown = policy.get('unknown_policy', {}).get('primary_without_reserve', 'block')
    return [
        '规则每次 plan/start 都从本地配置或 routes.example.json 重读；额度只在该任务既有 primary/alternate 间比较，额度不发给 Jev。',
        '各窗原始分=capacity_factor*available_pp/max(距重置分钟,时间下限)；%s 分别在可比候选间归一化后按窗口权重加权；仅比较窗口长度一致的数据；同分选主。' % window_text,
        'pace_ratio=(available_pp/(100-reserve_pp))/(max(距重置分钟,时间下限)/窗口分钟)，每天可用消耗百分点由可用额度除以实际剩余天数得到；两者只作解释指标，不重复乘入原始分；reserve=100 时 pace_ratio=null 且额度不足阻断。',
        'capacity_factor 是经验示例权重，不是真实 token 数或已验证容量；预算点数未经测量校准，预算够不代表足以完成任务。',
        '调度预留（%s）是协调会话一个完整窗口的用量；reserve_scaling=%s：prorated 时实际预留=配置值×距重置分钟/窗口分钟，越接近重置预留越少，fixed 时全程按配置值扣除；可派发额度=剩余点-实际预留点；低于预算则候选不可派发。' % (reserves, policy.get('reserve_scaling', 'prorated')),
        '额度未知（查询失败/缺失/过期/异常/窗口不符）与已知不足是两种状态：未知不当 0 或 100，也不让备选自动胜出；无预留 primary 未知时的策略为 %s，带预留的池永远阻断。' % unknown,
        '额度数据：Codex 取 Orca 与当前账户会话日志（token_count 的 rate_limits；账户专属目录的单链接文件按来源信任，硬链接文件和公共目录校验 weekly resets_at）中较新的一方，日志缺失或异常时回退 Orca；日志新鲜时 Orca 的 status=error 不判 unknown；其他账户池只用 Orca。',
        '首次额度选择与失败重试分开；普通重试由协调者按 --retry-of 处理。唯一自动例外是已授权的 quota_guard 额度交接：先确认候选与旧进程退出，再经本脚本检查额度后重试。',
        '仅 route.py 启动且守卫成功注册的受保护账户池任务可在运行中额度交接；不拦截其他 worker。',
        '参与比较的候选数据过期或 status=error 时先请 Orca 刷新一次（accounts.list refreshUsage，跨进程限频，遵守 retry-after）再重读；状态 ok 且不过期时不刷新，刷新失败按原规则判断。',
        '过期但有界的额度数据只对存活守卫自己发起的接手生效（--quota-guard-takeover，由 route.py 校验调用方）；普通 plan/start 的未知额度规则不放宽。',
        'restore_reminder 只提醒是否恢复权重，不影响选路、退出码和 launchable；first_in_window=true 时才需要转告用户。',
    ]


def effort_for(rule, profile, complexity):
    if profile.get('effort') is None and 'hard_effort' not in profile:
        return None  # effort is embedded in the model id (e.g. Gemini High)
    if complexity == 'hard':
        return profile.get('hard_effort', profile.get('effort'))
    return rule.get('effort', profile.get('effort'))


def choose(config, kind, complexity, profile_name=None, effort=None):
    """Fixed-profile mapping (primary unless a profile is given). Used for the plan body."""
    rule = config['tasks'][kind]
    name = profile_name or rule['primary']
    profile = config['models'][name]
    return dict(kind=kind, complexity=complexity, profile=name,
                agent=profile['agent'], model=profile['model'],
                effort=effort or effort_for(rule, profile, complexity),
                enabled=profile['enabled'], read_only=rule.get('read_only', False),
                alternate=rule['alternate'], description=rule['description'],
                blocked_reason=profile.get('blocked_reason'))


def iso(ms):
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    except (OverflowError, OSError, ValueError):
        return None


def build_report(result):
    """Quota view for output: only derived numbers, never identities or raw receipts."""
    src = result.get('source')
    report = dict(policy_enabled=result['policy_enabled'], candidates=[])
    if src:
        report['data_source'] = src['command']
        report['queried_at'] = iso(src['queried_at_ms'])
        report['query_ok'] = src['ok']
        report['query_failure'] = src['failure']
        report['stale_after_seconds'] = src['stale_after_seconds']
        report['provider_updated_at'] = {k: iso(v) for k, v in src['provider_updated_at_ms'].items()}
        report['provider_data_source'] = src.get('provider_data_source') or {}
        report['codex_log_status'] = src.get('codex_log_status')
        report['codex_orca_updated_at'] = iso(src.get('codex_orca_updated_at_ms'))
        report['task_budget_points'] = result.get('budget_points')
        report['usage_refresh'] = src.get('refresh')
    for c in result['candidates']:
        report['candidates'].append(dict(
            profile=c['profile'], role=c['role'], agent=c['agent'], model=c['model'],
            status=c['status'], auto_selectable=c['auto_selectable'], reasons=c['reasons'],
            capacity_factor=c['capacity_factor'], score=c['score'],
            data_source=c.get('data_source'),
            data_updated_at=iso(c['data_updated_at_ms']), data_age_seconds=c['data_age_seconds'],
            windows=c['windows'], caps=c.get('caps') or {},
            **{k: c[k] for k in ('stale_takeover', 'stale_takeover_refused', 'stale_takeover_expected',
                                 'orca_error_covered_by_log') if k in c}))
    return report


def reminder_view(config, result, may_claim=True):
    """Independent of routing: whatever happens here changes no selection, exit code or launchable."""
    if not result.get('snapshot'):
        return None
    try:
        pol = quota.validate_policy(config['quota_policy'])
        view = quota.reminder_for_output(pol, result['snapshot'], int(time.time() * 1000),
                                         caller_may_claim if may_claim else (lambda: False))
        if view.get('resets_at_ms'):
            view['resets_at'] = iso(view['resets_at_ms'])
        return view
    except Exception:
        return None


def caller_may_claim():
    """A guard's own plan reaches nobody, so it must not use up the window's one notice."""
    try:
        import quota_guard
        return not quota_guard.called_by_guard()
    except Exception:
        return False


def evaluate(config, kind, complexity, args):
    """Return (plan, report, selection). Never launches; blocked -> plan['profile'] is None."""
    rule = config['tasks'][kind]
    # main() has verified the caller before this attribute can be set; it never defaults on.
    takeover = None
    if getattr(args, 'quota_guard_takeover', None):
        takeover = 'recheck' if getattr(args, 'handoff_recheck', False) else args.action
    try:
        result = quota.select(config, kind, complexity,
                              budget_override=dict(session=args.budget_session, weekly=args.budget_weekly),
                              explicit_profile=args.profile,
                              refresher=None if getattr(args, 'preview', False) else quota.refresh_usage_rpc,
                              stale_takeover=takeover)
    except quota.PolicyError as exc:
        sel = dict(selected=None, blocked_code='policy_invalid', warnings=[],
                   blocked_reason='quota_policy 配置无效，不派发：%s' % exc, reason=str(exc))
        result = dict(policy_enabled=True, candidates=[], source=None, selection=sel)
    sel = result['selection']
    name = sel['selected']
    if name and not (config['models'][name]['enabled'] and config['models'][name]['model']):
        sel = dict(sel, selected=None, blocked_code='no_candidate',
                   blocked_reason=config['models'][name].get('blocked_reason') or '所选档位已禁用')
        name = None
    if name and args.effort and config['models'][name].get('effort') is None:
        sel = dict(sel, selected=None, blocked_code='effort_unsupported',
                   blocked_reason='所选档位 %s 的推理强度已内嵌在模型 ID，不接受 --effort；不派发。' % name)
        name = None
    plan = choose(config, kind, complexity, name or rule['primary'], args.effort)
    plan['task_primary'], plan['task_alternate'] = rule['primary'], rule['alternate']
    if name:
        plan['alternate'] = rule['alternate'] if name == rule['primary'] else rule['primary']
        if name == rule.get('escalation'):
            plan['escalation_note'] = ('升级档：仅在 %s 已尝试但不足、或用户明确要求时使用；与主选同属 Claude 周额度，消耗更快。'
                                       % rule['primary'])
        plan['blocked_reason'] = None
    else:
        for key in ('profile', 'agent', 'model', 'effort'):
            plan[key] = None
        plan['blocked_reason'] = sel['blocked_reason']
    plan['blocked_code'] = sel['blocked_code']
    # Unknown quota: keep the original choice only as an unconfirmed suggestion, never as a launch.
    plan['suggested_profile'] = ((args.profile or rule['primary'])
                                 if sel['blocked_code'] == 'quota_unknown' else None)
    if plan['suggested_profile']:
        plan['suggestion_note'] = '额度未知，预算/预留检查未通过验证；此档位仅为待协调者确认的建议，未启动。'
    plan['selection_basis'] = ('explicit_profile' if args.profile else
                               'quota_first_selection' if result['policy_enabled'] else 'fixed_primary')
    plan['selection_reason'] = sel['reason']
    plan['warnings'] = sel['warnings']
    picked = next((c for c in result['candidates'] if c['profile'] == plan['profile']), None)
    if picked and picked.get('stale_takeover'):
        plan['stale_takeover'] = picked['stale_takeover']
    plan['fast_mode'] = fast_decision(config, plan, result)
    plan['warnings'] = plan['warnings'] + guard_warnings(config, plan, result)
    report = build_report(result)
    report['restore_reminder'] = reminder_view(config, result, not getattr(args, 'preview', False))
    return plan, report, sel


def guard_warnings(config, plan, result):
    """Tell the coordinator when a chosen pool would hand off as soon as the worker starts."""
    cand = next((c for c in result['candidates'] if c['profile'] == plan['profile']), None)
    if not plan['profile'] or not cand:
        return []
    try:
        import quota_guard
        return quota_guard.plan_warnings(config, cand)
    except Exception:
        return []  # A preview must never change a routing decision.


def fast_decision(config, plan, result):
    """Fast tier only when the pool is ahead of pace and the task's fast-mode cost still fits."""
    fm = config.get('quota_policy', {}).get('fast_mode') or {}
    if not fm.get('enabled'):
        return dict(apply=False, reason='fast_mode 未启用')
    if not plan['profile'] or plan['agent'] not in fm.get('agents', []):
        return dict(apply=False, reason='所选档位不适用快速模式')
    cand = next((c for c in result['candidates'] if c['profile'] == plan['profile']), None)
    if not cand or cand['status'] != 'eligible' or set(cand['windows']) != {'session', 'weekly'}:
        return dict(apply=False, reason='额度未知或不完整，不开快速模式')
    if cand.get('stale_takeover'):
        return dict(apply=False, reason='使用过期数据接手，不开快速模式')
    mult = fm['usage_multiplier']
    for wname, win in cand['windows'].items():
        need = win['budget_pp'] * mult
        if need > win['available_pp']:
            return dict(apply=False, reason='%s 预计快速模式消耗 %.1f 点 > 可用 %.1f 点'
                        % (wname, need, win['available_pp']))
    weekly = cand['windows']['weekly']['pace_ratio']
    if weekly is None or weekly < fm['min_weekly_pace_ratio']:
        return dict(apply=False, reason='weekly pace_ratio %s < %s，额度不算充足'
                    % (weekly, fm['min_weekly_pace_ratio']))
    return dict(apply=True, reason='weekly pace_ratio %.2f ≥ %s，且预计消耗（预算×%s）在各窗可用额度内'
                % (weekly, fm['min_weekly_pace_ratio'], mult))


def find_handle(receipt):
    """Worker terminal from the worker-start receipt: the 'terminal' effect, else worker-list by dispatchId."""
    result = (receipt or {}).get('result') or {}
    for effect in result.get('effects') or []:
        if effect.get('kind') == 'terminal' and str(effect.get('id', '')).startswith('term_'):
            return effect['id']
    dispatch = result.get('dispatchId')
    if not dispatch:
        return None
    out = subprocess.run([ORCA, 'orchestration', 'worker-list', '--json'], capture_output=True, text=True, timeout=20)
    try:
        workers = json.loads(out.stdout)['result']['workers']
    except (ValueError, KeyError, TypeError):
        return None
    return next((w.get('agentTerminalHandle') for w in workers if w.get('dispatchId') == dispatch), None)


def status_tier(handle):
    """'fast' / 'default' from the Codex status line, or None if it is not visible yet."""
    out = subprocess.run([ORCA, 'terminal', 'read', '--terminal', handle, '--screen', '--json'],
                         capture_output=True, text=True, timeout=20)
    try:
        tail = json.loads(out.stdout)['result']['terminal']['tail']
    except (ValueError, KeyError, TypeError):
        return None
    for line in reversed(tail):
        m = re.search(r'\b(?:low|medium|high|xhigh)( fast)? · ', line)
        if m:
            return 'fast' if m.group(1) else 'default'
    return None


CODEX_CONFIG = Path.home() / '.codex' / 'config.toml'
TIER_LINE = re.compile(r'^service_tier\s*=.*$', re.M)


def restore_tier(before):
    """/fast also persists service_tier to the user's Codex config; put the user's own value back."""
    try:
        now = CODEX_CONFIG.read_text()
    except OSError:
        return
    old, new = TIER_LINE.search(before or ''), TIER_LINE.search(now)
    if old and new and old.group(0) != new.group(0):
        CODEX_CONFIG.write_text(now[:new.start()] + old.group(0) + now[new.end():])
    elif new and not old and before is not None:
        CODEX_CONFIG.write_text(now[:new.start()] + now[new.end():].lstrip('\n'))


def enable_fast(handle):
    """/fast toggles, so only send it when the status line shows the default tier."""
    tier = None
    for _ in range(10):
        tier = status_tier(handle)
        if tier:
            break
        time.sleep(2)
    if tier is None:
        return dict(applied=False, reason='未读到 Codex 状态栏，未发送 /fast')
    if tier == 'fast':
        return dict(applied=True, reason='worker 已处于快速模式，未重复切换')
    try:
        before = CODEX_CONFIG.read_text()
    except OSError:
        before = None
    try:
        subprocess.run([ORCA, 'terminal', 'send', '--terminal', handle, '--text', '/fast', '--enter', '--json'],
                       capture_output=True, text=True, timeout=20)
        time.sleep(3)
        tier = status_tier(handle)
    finally:
        restore_tier(before)
    return dict(applied=tier == 'fast', reason='已发送 /fast，状态栏显示 %s；~/.codex/config.toml 的 service_tier 已恢复原值' % tier)


def argv_for(plan, args):
    argv = [ORCA, 'orchestration', 'worker-start', '--agent', plan['agent'], '--worktree', args.worktree]
    if plan['model']:
        argv += ['--model', plan['model']]
    if plan['effort']:
        argv += ['--effort', plan['effort']]
    for flag, value in (('--run', args.run), ('--from', args.coordinator),
                        ('--name', args.name), ('--repo', args.repo),
                        ('--task', getattr(args, 'task', None)),
                        ('--retry-of', getattr(args, 'retry_of', None))):
        if value:
            argv += [flag, value]
    return argv


USAGE_LOG = Path.home() / '.config' / 'orca-model-routing' / 'usage-log.jsonl'


def pool_view(windows, caps, now_ms):
    """Remaining points and reset instant per window; enough to diff two observations."""
    return {w: dict(remaining_pp=i['remaining_pp'], resets_at_ms=int(now_ms + i['minutes_to_reset'] * 60000))
            for w, i in list(windows.items()) + list(caps.items())}


def append_log(record):
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_LOG.open('a') as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + '\n')


def log_start(plan, report, receipt, fast, context=None):
    """Pool snapshot at dispatch; `settle` diffs against it to measure what a task really used."""
    result = (receipt or {}).get('result') or {}
    cand = next((c for c in report['candidates'] if c['profile'] == plan['profile']), None)
    now_ms = int(time.time() * 1000)
    append_log(dict(event='start', at=iso(now_ms), at_ms=now_ms, dispatch_id=result.get('dispatchId'),
                    task_id=result.get('taskId'), kind=plan['kind'], complexity=plan['complexity'],
                    profile=plan['profile'], agent=plan['agent'], model=plan['model'], effort=plan['effort'],
                    fast=bool(fast and fast.get('applied')),
                    context=context,
                    pool=pool_view(cand['windows'], cand.get('caps') or {}, now_ms) if cand else None,
                    **({'stale_takeover': cand['stale_takeover']} if cand and cand.get('stale_takeover') else {})))


def settle(dispatch_id):
    """Record the pool's change since a dispatch started. Other sessions on the same pool add noise."""
    config = load_config(ROOT)
    starts = []
    if USAGE_LOG.exists():
        for line in USAGE_LOG.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get('event') == 'start' and rec.get('dispatch_id') == dispatch_id:
                starts.append(rec)
    if not starts or not starts[-1].get('pool'):
        print(json.dumps({'settled': False, 'reason': '日志里没有该 dispatch 的 start 记录'}, ensure_ascii=False))
        return 2
    start = starts[-1]
    pol = quota.validate_policy(config['quota_policy'])
    snap = quota.fetch_snapshot(pol)
    now_ms = int(time.time() * 1000)
    prov = snap['providers'].get(pol['provider_keys'].get(start['agent'])) if snap['ok'] else None
    consumed = {}
    for w, before in start['pool'].items():
        win = (prov or {}).get(w) or {}
        used, reset = win.get('usedPercent'), win.get('resetsAt')
        if not isinstance(used, (int, float)) or not isinstance(reset, (int, float)):
            consumed[w] = None
        elif abs(reset - before['resets_at_ms']) > 10 * 60000:
            consumed[w] = 'reset'  # window rolled over meanwhile: no usable difference
        else:
            consumed[w] = round(before['remaining_pp'] - (100.0 - used), 2)
    record = dict(event='settle', at=iso(now_ms), at_ms=now_ms, dispatch_id=dispatch_id,
                  kind=start['kind'], complexity=start['complexity'], profile=start['profile'],
                  agent=start['agent'], fast=start.get('fast'),
                  minutes=round((now_ms - start['at_ms']) / 60000, 1), consumed_pp=consumed,
                  query_ok=snap['ok'])
    append_log(record)
    print(json.dumps(dict(record, settled=True, note='消耗含同一账户池里其他会话（协调者、你自己的会话）的用量，仅作校准参考。'),
                     ensure_ascii=False, indent=2))
    return 0


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == 'settle':
        sp = argparse.ArgumentParser(prog='route.py settle', description='record pool usage of a finished dispatch')
        sp.add_argument('--dispatch', required=True)
        return settle(sp.parse_args(sys.argv[2:]).dispatch)
    config = load_config(ROOT)  # reread on every invocation
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'start'])
    parser.add_argument('--kind', choices=['auto'] + list(config['tasks']), required=True,
                        help='task kind; auto needs Jev enabled and --spec-file')
    parser.add_argument('--complexity', choices=['normal', 'hard'], default=None)
    parser.add_argument('--profile', choices=list(config['models']), default=None,
                        help='explicit model profile; still subject to reserve and budget')
    parser.add_argument('--effort', default=None, help='explicit reasoning effort for the profile')
    parser.add_argument('--budget-session', type=float, default=None, help='task budget override, percentage points')
    parser.add_argument('--budget-weekly', type=float, default=None, help='task budget override, percentage points')
    parser.add_argument('--expected-profile', choices=list(config['models']), default=None,
                        help='start only: profile shown by the reviewed plan; refuse if the fresh choice differs')
    parser.add_argument('--run')
    parser.add_argument('--from', dest='coordinator')
    parser.add_argument('--spec-file', type=Path)
    parser.add_argument('--worktree', default='current')
    parser.add_argument('--name')
    parser.add_argument('--repo')
    parser.add_argument('--task', help='existing failed Task (requires --retry-of and recorded context entry)')
    parser.add_argument('--retry-of', help='previous dispatch; does not inherit placement')
    parser.add_argument('--preview', action='store_true',
                        help='internal: guard --dry-run; no refresh request and no reminder claim')
    parser.add_argument('--quota-guard-takeover', metavar='DISPATCH',
                        help='internal: only a live quota_guard, for its own dispatch; anything else is refused')
    parser.add_argument('--handoff-recheck', action='store_true',
                        help='internal: the guard\'s last plan before it changes the Task; needs --quota-guard-takeover')
    args = parser.parse_args()
    if args.handoff_recheck and (args.action != 'plan' or not args.quota_guard_takeover):
        parser.error('--handoff-recheck 只用于带 --quota-guard-takeover 的 plan。')
    if args.effort is not None:
        allowed = config.get('explicit_overrides', {}).get('allowed_efforts', [])
        if args.effort not in allowed:
            parser.error('--effort 需为 %s。' % ', '.join(allowed))
        if args.profile and config['models'][args.profile].get('effort') is None:
            parser.error('该档位的推理强度已内嵌在模型 ID，不接受 --effort。')
    for flag in ('budget_session', 'budget_weekly'):
        value = getattr(args, flag)
        if value is not None and not 0 <= value <= 100:
            parser.error('--%s 需在 0 到 100 个百分点之间。' % flag.replace('_', '-'))
    spec_text = None
    if bool(args.task) != bool(args.retry_of):
        parser.error('--task and --retry-of must be supplied together.')
    if args.action == 'start':
        if not args.run or not args.spec_file:
            parser.error('start requires --run and --spec-file; no Run is created implicitly.')
        if args.worktree not in ['new-child', 'new-top-level'] and (args.name or args.repo):
            parser.error('--name/--repo are only valid with a new worktree.')
        spec_text = args.spec_file.expanduser().resolve().read_text().strip()
        if not spec_text:
            parser.error('Task specification must not be empty.')
    decision = None
    if args.kind == 'auto':
        if not args.spec_file:
            parser.error('auto requires --spec-file containing an authorized task specification.')
        try:
            selected, decision = classify(config, args.spec_file.expanduser().resolve().read_text())
        except (ValueError, OSError, KeyError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if decision['needs_coordinator']:
            print(json.dumps({'will_launch': False, 'jev_decision': decision,
                              'reason': '分类不明确，交回协调者；不得静默选用备用模型。'}, ensure_ascii=False, indent=2))
            return 2
        kind = selected['kind']
        complexity = args.complexity or selected['complexity']
    else:
        kind, complexity = args.kind, args.complexity or 'normal'
    rule = config['tasks'][kind]
    if args.profile and args.profile not in (rule['primary'], rule['alternate'], rule.get('escalation')):
        parser.error('--profile 只能是任务 %s 的 primary(%s)、alternate(%s) 或 escalation(%s)，不能扩展候选。'
                     % (kind, rule['primary'], rule['alternate'], rule.get('escalation')))
    if args.quota_guard_takeover:
        # Refused outright rather than downgraded to ordinary rules: nothing but the guard should ever pass this.
        try:
            import quota_guard
            refusal = quota_guard.takeover_refusal(args.quota_guard_takeover, args.action, kind, complexity, args, config)
        except Exception:
            refusal = '无法校验调用方'
        if refusal:
            print(json.dumps(dict(will_launch=False, launched=False, launchable=False,
                                  blocked_reason='--quota-guard-takeover 被拒绝：' + refusal), ensure_ascii=False, indent=2))
            return 2
    plan, report, sel = evaluate(config, kind, complexity, args)
    reminder = report.pop('restore_reminder', None)
    if decision:
        plan['jev_decision'] = decision
    blocked = plan['profile'] is None
    argv = None if blocked else argv_for(plan, args)
    head = {'plan': plan, 'quota': report, 'decision_mode': config['decision_mode'], 'restore_reminder': reminder}
    if args.action == 'plan':
        print(json.dumps(dict(head, argv_prefix=argv, will_launch=False, launchable=not blocked,
                              blocked_reason=plan['blocked_reason'], explain=explain(config)),
                         ensure_ascii=False, indent=2))
        return 2 if blocked else 0
    if blocked:
        print(json.dumps(dict(head, will_launch=False, launched=False, blocked_reason=plan['blocked_reason'],
                              action_required='交回协调者；未启动工作 Agent，未自动切换备选或重试。'),
                         ensure_ascii=False, indent=2))
        return 2
    if args.expected_profile and args.expected_profile != plan['profile']:
        print(json.dumps(dict(head, will_launch=False, launched=False,
                              profile_changed={'expected': args.expected_profile, 'current': plan['profile']},
                              action_required='派发前最新额度使所选档位与已审 plan 不同；未启动。协调者重审后用 '
                                              '--expected-profile %s 重新 start。' % plan['profile']),
                         ensure_ascii=False, indent=2))
        return 3
    boundary = ('本任务仅分析/审查，只返回报告，不修改任何文件。' if plan['read_only'] else
                '本任务由你单独执行；仅修改任务说明授权的范围，不自行派发其他 Agent。')
    spec = spec_text + '\n\n路由约束：' + boundary + '\n以说明中的可观察验收条件验证结果，遵守实时注入的 Orca Dispatch 生命周期。'
    context, guard_preparation_error = {}, None
    try:
        import quota_guard
        if args.retry_of:
            old = quota_guard.load_meta(args.retry_of)
            refusal = quota_guard.retry_refused_by_guard(args.retry_of)
            if refusal:
                raise ValueError(refusal)
            backend = quota_guard.Backend(config, old)
            checks = (
                (old['task_id'] == args.task, 'Task 不匹配'),
                (old['run_id'] == args.run, 'Run 不匹配'),
                (args.worktree == old['worktree'], 'worktree 不匹配'),
                (old.get('coordinator') == (args.coordinator or os.environ.get('ORCA_TERMINAL_HANDLE')), 'coordinator 不匹配'),
            )
            for valid, reason in checks:
                if not valid:
                    raise ValueError(reason)
            if not backend.original():
                raise ValueError('原始上下文不可验证')
            if not quota_guard.is_stopped(backend.show()):
                raise ValueError('旧 worker 未确认退出')
            if backend.attempts() >= config['constraints']['max_attempts_per_task']:
                raise ValueError('已达到尝试次数上限')
            refusal = quota_guard.retry_refusal(backend.task_state(), backend.latest_dispatch(), args.retry_of)
            if refusal:
                raise ValueError(refusal)
        context = quota_guard.prepare_context(spec_text, config, args, plan['agent'])
        if context:
            spec += quota_guard.context_instruction(context['context_file'])
    except Exception as exc:
        if args.retry_of:
            # The reminder was claimed by evaluate(): this is its one chance to reach anyone.
            print(json.dumps({'launched': False, 'blocked_reason': '额度交接重试前置校验失败：' + str(exc),
                              'restore_reminder': reminder}, ensure_ascii=False))
            return 2
        # Initial dispatch remains authorized even if protection could not be prepared.
        guard_preparation_error = str(exc)
    if not args.task:
        argv += ['--spec', spec, '--task-title', plan['description']]
    argv += ['--json']
    print(json.dumps({'routing_plan': plan, 'quota': report, 'restore_reminder': reminder}, ensure_ascii=False), file=sys.stderr)
    # Baseline for the handoff's leftover-process check: taken before the worker can spawn anything.
    dispatch_started_at = time.time()
    # Do not impose an outer timeout or resend: worker-start owns its launch deadline.
    # Stream the authoritative receipt unchanged, including failures/residual resources.
    proc = subprocess.run(argv, stdout=subprocess.PIPE, text=True)
    sys.stdout.write(proc.stdout)
    sys.stdout.flush()
    try:
        receipt = json.loads(proc.stdout)
    except ValueError:
        receipt = None
    fast = None
    if plan['fast_mode']['apply']:
        fast = dict(applied=False, reason='worker-start 未成功，未开快速模式')
        if proc.returncode == 0:
            handle = find_handle(receipt)
            fast = enable_fast(handle) if handle else dict(applied=False, reason='回执中未找到 worker 终端句柄')
        print(json.dumps({'fast_mode': fast}, ensure_ascii=False), file=sys.stderr)
    if proc.returncode == 0:
        try:
            guard = (dict(started=False, reason=guard_preparation_error) if guard_preparation_error else
                     quota_guard.register_start(config, plan, args, receipt, context,
                                                started_at=dispatch_started_at))
        except Exception as exc:
            guard = dict(started=False, reason=str(exc))
        print(json.dumps({'quota_guard': guard}, ensure_ascii=False), file=sys.stderr)
        try:
            log_start(plan, report, receipt, fast, context)
        except OSError as exc:
            print(json.dumps({'usage_log_error': str(exc)}, ensure_ascii=False), file=sys.stderr)
    return proc.returncode


if __name__ == '__main__':
    sys.exit(main())
