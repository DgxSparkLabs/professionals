# Specification sheet

The public specification of `professionals`, version 1 (marketplace professionals) plus version 2 (your own folders as professionals, resumable, with an allowlist).

- [spec.md](spec.md): idea, user stories, requirements, message protocol, safety model, entry states, test map.
- [audit.md](audit.md): audit, observability and evaluation (planned; runtime map, fixtures, spikes and frozen v1 contracts done).
- [audit/](audit/events.md): the v1 contracts: [events](audit/events.md), [query](audit/query.md), [metrics](audit/metrics.md), [privacy](audit/privacy.md), [budgets](audit/budgets.md), machine-readable schemas in `audit/schemas/`.

Contributors: change behaviour in `plugins/professionals/bin/pros`, update this sheet in the same pull request, and add or extend a test in `tests/`.
