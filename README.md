# Orca Model Routing

An open-source Orca skill and command-line helper for routing a task between its configured primary and alternate worker profiles. It can classify a task with Jev, compare current quota windows, explain a plan, and start a worker through Orca. Routing configuration is a template: model IDs, capability flags, budgets, and capacity weights must be reviewed for your environment.

## Requirements

- Python 3.10 or later; the scripts use only the Python standard library.
- Orca CLI installed and available on `PATH` for quota reads and worker starts.
- Optional: a TypeSafe API key for Jev classification. Jev is disabled by default.

## Setup

1. Clone or copy this directory and review `routes.example.json`.
2. Set model IDs, enabled profiles, task mappings, quota budgets, and any reserve values to match your own policy. The checked-in file is an example, not a recommendation.
3. Optionally create a checkout-local private override: `cp routes.example.json routes.json`. This file is ignored by Git. If it is absent, the scripts load `routes.example.json` automatically.
4. Use `python3 scripts/route.py plan --kind feature --complexity normal --spec-file /path/to/task.txt` to review a plan. A plan does not launch a worker.
5. For Jev, run `python3 scripts/setup_jev.py` in an interactive terminal. The API key and setup status are stored in the private user config directory, separately from routing files. Enable Jev in your private config only after reviewing the implications.

Starting a worker requires an existing Orca Run and an explicit reviewed plan. Use `python3 scripts/route.py start` with `--run`, `--spec-file`, and `--expected-profile`; see `python3 scripts/route.py --help` for options. The tool refuses to start if a fresh quota check changes the selected profile.

## Configuration and privacy

Never commit keys, credentials, status files, live quota snapshots, or local `routes.json`. The quota reader requests Orca account rate-limit data at runtime, reduces it to routing fields, and does not retain account identities or raw receipts. Keep actual quota policy and credentials in local configuration outside version control.

The routing helper does not install a background service or intercept ordinary Orca chats. It runs only when invoked.

## License

MIT; see [LICENSE](LICENSE).
