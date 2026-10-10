#!/usr/bin/env python3
"""Map a classified task to one Orca worker, choosing between the task's own
primary/alternate by account quota. No hidden retries, no auto account actions."""
import argparse
import json
import subprocess
import sys
import shutil
from datetime import datetime, timezone
from pathlib import Path

from jev_route import classify
import quota

ROOT = Path(__file__).resolve().parents[1]
ORCA = shutil.which('orca') or 'orca'


def load_config():
    """Load a checkout-local private override when present; otherwise use the template."""
    local = ROOT / 'routes.json'  # ignored, optional per-checkout override
    example = ROOT / 'routes.example.json'
    for path in (local, example):
        if path.is_file():
            return json.loads(path.read_text())
    raise FileNotFoundError('No routes config found; expected routes.example.json.')

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
        '“首次额度选择”（本脚本，基于额度的 primary 对比 alternate）与“失败后重试”是两件事：重试由协调者重新评估并按 Orca --retry-of 处理，本脚本不代办、不自动切备选。',
        '运行中任务不迁移；每个新任务重新选择。',
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
        report['task_budget_points'] = result.get('budget_points')
    for c in result['candidates']:
        report['candidates'].append(dict(
            profile=c['profile'], role=c['role'], agent=c['agent'], model=c['model'],
            status=c['status'], auto_selectable=c['auto_selectable'], reasons=c['reasons'],
            capacity_factor=c['capacity_factor'], score=c['score'],
            data_updated_at=iso(c['data_updated_at_ms']), data_age_seconds=c['data_age_seconds'],
            windows=c['windows']))
    return report


def evaluate(config, kind, complexity, args):
    """Return (plan, report, selection). Never launches; blocked -> plan['profile'] is None."""
    rule = config['tasks'][kind]
    try:
        result = quota.select(config, kind, complexity,
                              budget_override=dict(session=args.budget_session, weekly=args.budget_weekly),
                              explicit_profile=args.profile)
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
    return plan, build_report(result), sel


def argv_for(plan, args):
    argv = [ORCA, 'orchestration', 'worker-start', '--agent', plan['agent'], '--worktree', args.worktree]
    if plan['model']:
        argv += ['--model', plan['model']]
    if plan['effort']:
        argv += ['--effort', plan['effort']]
    for flag, value in (('--run', args.run), ('--from', args.coordinator),
                        ('--name', args.name), ('--repo', args.repo)):
        if value:
            argv += [flag, value]
    return argv


def main():
    config = load_config()  # reread on every invocation
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
    args = parser.parse_args()
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
    if args.profile and args.profile not in (config['tasks'][kind]['primary'], config['tasks'][kind]['alternate']):
        parser.error('--profile 只能是任务 %s 的 primary(%s) 或 alternate(%s)，不能扩展候选。'
                     % (kind, config['tasks'][kind]['primary'], config['tasks'][kind]['alternate']))
    plan, report, sel = evaluate(config, kind, complexity, args)
    if decision:
        plan['jev_decision'] = decision
    blocked = plan['profile'] is None
    argv = None if blocked else argv_for(plan, args)
    head = {'plan': plan, 'quota': report, 'decision_mode': config['decision_mode']}
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
    argv += ['--spec', spec, '--task-title', plan['description'], '--json']
    print(json.dumps({'routing_plan': plan, 'quota': report}, ensure_ascii=False), file=sys.stderr)
    # Do not impose an outer timeout or resend: worker-start owns its launch deadline.
    # Stream the authoritative receipt unchanged, including failures/residual resources.
    return subprocess.run(argv).returncode


if __name__ == '__main__':
    sys.exit(main())
