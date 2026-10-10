"""Quota-aware choice between a task's existing primary/alternate profiles.

Read-only: one bounded `orca account list --json` subprocess whose output is
reduced to status/updatedAt/session/weekly numbers. Account identity,
usageMetadata and raw receipts are never kept or printed. Nothing here
launches a worker, resets credits, switches accounts or retries.

Unknown (cannot be verified) and insufficient (verified too low) are distinct
states; unknown is never treated as 0 or 100 and never lets the alternate win.
"""
import json
import math
import re
import shutil
import subprocess
import time

ORCA_ACCOUNT_CMD = [shutil.which('orca') or 'orca', 'account', 'list', '--json']
WINDOWS = ('session', 'weekly')
EPS = 1e-9


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
    generic_command = ['orca', 'account', 'list', '--json']
    if src.get('command') not in (generic_command, ORCA_ACCOUNT_CMD):
        raise PolicyError('quota_policy.source.command 必须调用 Orca 的 account list --json。')
    timeout = _nonneg(src.get('timeout_seconds'), 'source.timeout_seconds')
    if not 0 < timeout <= 120:
        raise PolicyError('quota_policy.source.timeout_seconds 需在 (0,120]。')
    out = dict(
        enabled=bool(policy.get('enabled', True)),
        timeout=timeout,
        max_bytes=int(_nonneg(src.get('max_output_bytes', 2097152), 'source.max_output_bytes')),
        stale=_nonneg(policy.get('stale_after_seconds'), 'stale_after_seconds'),
        skew=_nonneg(policy.get('max_future_skew_seconds', 60), 'max_future_skew_seconds'),
        provider_keys=dict(policy.get('provider_keys') or {}),
        factors={}, reserves={}, windows={}, budgets={},
        unknown_primary=(policy.get('unknown_policy') or {}).get('primary_without_reserve', 'block'))
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
    for level in ('normal', 'hard'):
        per = (policy.get('task_budget_points') or {}).get(level)
        if not isinstance(per, dict):
            raise PolicyError('缺少 task_budget_points.' + level)
        out['budgets'][level] = {w: _nonneg(per.get(w), 'task_budget_points.%s.%s' % (level, w), 100)
                                 for w in WINDOWS}
    return out


def fetch_snapshot(pol, runner=None, clock=None):
    """One read-only, time-bounded query. Failure becomes a reason, never a number."""
    runner, clock = runner or subprocess.run, clock or time.time
    queried = int(clock() * 1000)
    snap = dict(ok=False, reason=None, queried_at_ms=queried, providers={},
                source='orca account list --json')
    try:
        proc = runner(ORCA_ACCOUNT_CMD, capture_output=True, timeout=pol['timeout'],
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
        rate = json.loads(raw)['result']['rateLimits']
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
        for w in WINDOWS:
            win = entry.get(w)
            kept[w] = ({k: win.get(k) for k in ('usedPercent', 'windowMinutes', 'resetsAt')}
                       if isinstance(win, dict) else None)
        snap['providers'][key] = kept
    snap['ok'] = True
    return snap


def _window(pol, wname, win, now_ms, reserve, factor, budget):
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
    scoring_minutes = max(to_reset, spec['floor'])
    raw = factor * available / scoring_minutes
    pool_remaining = 100.0 - reserve
    pace_ratio = (None if pool_remaining <= EPS else
                  (available / pool_remaining) / (scoring_minutes / spec['expected']))
    available_per_day = available / (to_reset / 1440.0)
    return dict(remaining_pp=round(remaining, 4), reserve_pp=round(reserve, 4),
                reserve_configured_pp=configured_reserve, available_pp=round(available, 4),
                budget_pp=budget[wname], minutes_to_reset=round(to_reset, 2),
                window_minutes=int(mins), time_floor_minutes=spec['floor'],
                weight=spec['weight'], raw=raw,
                pace_ratio=None if pace_ratio is None else round(pace_ratio, 4),
                available_pp_per_day=round(available_per_day, 4)), None


def evaluate_candidate(name, profile, role, pol, snap, budget, now_ms, task_rule=None):
    cand = dict(profile=name, agent=profile.get('agent'), model=profile.get('model'), role=role,
                status=None, reasons=[], auto_selectable=True, windows={}, score=None,
                capacity_factor=None, data_updated_at_ms=None, data_age_seconds=None)
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
        if status != 'ok':
            unknown.append('%s 额度状态不是 ok（%s）' % (key, _safe(status)))
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
            info, why = _window(pol, w, prov.get(w), now_ms, reserves.get(w, 0.0), factor, budget)
            if why:
                unknown.append(why)
            else:
                cand['windows'][w] = info
    if unknown:
        cand.update(status='unknown', reasons=cand['reasons'] + unknown)
        return cand
    short = []
    for w, i in cand['windows'].items():
        if i['available_pp'] <= EPS:
            short.append('%s 扣除预留后可派发额度为 0（预算 %.4g 点）' % (w, i['budget_pp']))
        elif i['available_pp'] + EPS < i['budget_pp']:
            short.append('%s 可派发 %.4g 点 < 预算 %.4g 点' % (w, i['available_pp'], i['budget_pp']))
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


def resolve(cands, pol):
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
           runner=None, clock=None):
    """Evaluate candidates for a task. Reads policy fresh; never launches anything."""
    pol = validate_policy(config.get('quota_policy'))
    rule = config['tasks'][kind]
    if explicit_profile:
        if explicit_profile not in (rule['primary'], rule['alternate']):
            raise ValueError('--profile 只能是该任务的 primary(%s) 或 alternate(%s)，不能扩展候选。'
                             % (rule['primary'], rule['alternate']))
        names = [(explicit_profile, 'primary' if explicit_profile == rule['primary'] else 'alternate')]
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
    snap = fetch_snapshot(pol, runner=runner, clock=clock)
    now_ms = int(clock() * 1000)  # evaluated after the query returns, so a reset passed meanwhile is caught
    cands = [evaluate_candidate(n, config['models'][n], role, pol, snap, budget, now_ms, rule)
             for n, role in names]
    score_candidates(cands, pol)
    result['candidates'] = cands
    result['budget_points'] = budget
    result['source'] = dict(command='orca account list --json (read-only)', queried_at_ms=now_ms,
                            query_started_at_ms=snap['queried_at_ms'],
                            ok=snap['ok'], failure=snap['reason'],
                            stale_after_seconds=pol['stale'],
                            provider_updated_at_ms={k: v.get('updatedAt') for k, v in snap['providers'].items()})
    if explicit_profile:
        sel = explicit_resolve(cands[0], pol)
    else:
        sel = resolve(cands, pol)
    result['selection'] = sel
    return result
