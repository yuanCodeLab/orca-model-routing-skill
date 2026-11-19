---
name: orca-model-routing
description: Route an authorized Orca task through configured worker profiles, review quota-aware plans, and optionally classify task type with Jev.
---

# Orca Model Routing

This skill provides reusable task-routing guidance and scripts. Its checked-in [`routes.example.json`](routes.example.json) is a configurable template; copy it to an ignored checkout-local `routes.json` for private settings. When that file is absent, the scripts load the example file. Review every model ID, enabled flag, capability requirement, budget, and reserve for your environment.

## Workflow

1. Read the current Orca orchestration guidance and this skill's local configuration before routing a task.
2. Define one responsible worker per task, with authorized scope and observable acceptance criteria.
3. Review a plan before launch: `python3 <skill-dir>/scripts/route.py plan --kind <kind> --complexity <normal|hard> --spec-file <task-file>`. `--kind` is required; use `--kind auto` only when Jev is enabled.
4. For a launch, use an existing Orca Run (`orca orchestration run-create` if none), pass the same `--kind`/`--complexity` and the reviewed `--expected-profile`, and ensure the task specification authorizes the exact work.
5. Exit code `2` means blocked (see `blocked_code`/`blocked_reason`) and exit code `3` means a fresh quota check changed the selection. In both cases nothing was launched: report to the user and do not pick a model or retry on your own.
6. Verify the worker receipt and completion through Orca's lifecycle tools. A plan or accepted launch request alone does not establish completion.

## Routing behavior

- Automatic selection compares the configured primary and alternate. An explicit `--profile` can also select the task's escalation profile, subject to eligibility checks. Use `budget_multiplier` to scale a profile's task budget and `quota_cap_window` to require capacity in a provider sub-limit.
- `primary_first` keeps an eligible primary at or above its weekly pace threshold. A pool override takes precedence over the global threshold. `prefer_primary` keeps an eligible primary whenever possible for that task.
- Reserves reduce available quota. With `reserve_scaling: prorated`, the reserve decreases with the time left in the window. Use `fixed` to deduct the full amount. The optional `reset_horizon_minutes` caps the minutes used in scoring.
- `session_headroom` sets a floor for available quota in the 5-hour window by pool, complexity, and optionally task. Near reset, the threshold is capped by the guard's projected burn until reset plus its floor. The task budget still applies.
- Quota that is missing, stale, malformed, or inconsistent follows the unknown-data policy. A plan can request a rate-limited usage refresh. `plan --preview` reads existing data and skips the refresh and reminder-state write.
- Use `browser_check` for local headless checks, `browser_flow` for local interaction flows, `browser_debug` for local diagnosis, and `browser_external` for external or signed-in sites. Treat `browser` as the legacy external kind when configured that way. Verify each assigned profile's tools and permissions. Leave unverified alternates disabled or unconfirmed.
- Jev is optional and disabled in the template. Use TypeSafe credentials for automatic classification and keep credentials in private user configuration.

## Opt-in quota guard

- `quota_policy.quota_guard` monitors dispatches started by this router in configured pools. An authorized handoff requires a viable candidate in another pool, a complete worker handoff report, confirmed exit of the old worker, and a fresh routing check. Keep the worker's context entry available until it settles.
- Inspect monitoring and handoff status with `python3 scripts/quota_guard.py status`. Use `python3 scripts/quota_guard.py --dry-run` to inspect current predictions without starting a monitor or mutating worker state.
- `restore_reminder` can flag when projected unused weekly quota supports restoring a configured scoring weight. Surface `first_in_window: true` to the user. Weight changes are manual.

Run the unit suite with `python3 -m unittest discover -s tests -p 'test_*.py'`. See [README.md](README.md) for setup and privacy guidance.
