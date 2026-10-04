---
name: orca-model-routing
description: Route an authorized Orca task through configured worker profiles, review quota-aware plans, and optionally classify task type with Jev.
---

# Orca Model Routing

This skill provides reusable task-routing guidance and scripts. Its checked-in [`routes.example.json`](routes.example.json) is a configurable template; copy it to an ignored checkout-local `routes.json` for private settings. When that file is absent, the scripts load the example file. Review every model ID, enabled flag, capability requirement, budget, and reserve for your environment.

## Workflow

1. Read the current Orca orchestration guidance and this skill's local configuration before routing a task.
2. Define one responsible worker per task, with authorized scope and observable acceptance criteria.
3. Review a plan before launch: `python3 <skill-dir>/scripts/route.py plan --spec-file <task-file>`. Use `--kind` and `--complexity` to provide an explicit classification, or use `--kind auto` when Jev is configured.
4. For a launch, use an existing Orca Run, pass the reviewed `--expected-profile`, and ensure the task specification authorizes the exact work. A changed fresh selection blocks the launch for coordinator review.
5. Verify the worker receipt and completion through Orca's lifecycle tools. A plan or accepted launch request alone does not establish completion.

## Routing behavior

- Each task compares only its configured primary and alternate profiles. A quota-based alternate is an initial selection, not an automatic retry.
- Current quota reads are bounded and read-only. Unknown, stale, malformed, or mismatched quota data is distinct from known insufficiency and follows the configured unknown-data policy.
- Reserves and task budgets are configurable hard gates. Example values are illustrative and are not calibrated recommendations.
- Browser and other tool capabilities must be independently verified before enabling a profile or alternate.
- Jev is optional and disabled in the example. Credential setup stores keys and verification status in private user configuration and never writes them into tracked routing files.
- This skill runs only when invoked; it is not an Orca-wide interceptor or background service.

See [README.md](README.md) for setup and privacy guidance.
