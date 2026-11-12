# Changelog

## Unreleased

## 0.3.1

- README section "Audit data on your machine": what the default metadata capture records, where it is stored, file modes, retention, and how to turn it off, export and purge.
- A one-time notice on stderr the first time capture writes in a given `$PRO_HOME` (marker `audit/notice-shown`). It is printed only for the operator's own terminal calls, never inside a professional session or hook, and never on stdout.
- Default retention for audit events is now 90 days (was 400). Still configurable; nothing is deleted on a timer.

## 0.3.0

- Audit release (step G): `tests/test_audit_release.py` runs the release acceptance scenario through the real `pros` process (delegation, pending wait, late reply, operator partial verdict entered on the dashboard, resumed transcript, CLI and dashboard JSON byte-equal, 140/80/220/30 tokens, reimport after restart, redacted export without planted strings); `tests/test_audit_release_supplementary.py` adds crash between capture and import, fork duplicates, missing ids, overlapping tasks, denied access, retention, failed migration, disk pressure, and the documented backup and restore. Operations guide: `docs/audit-operations.md` (enable, inspect, disable, back up, restore, purge, same-user limitation).
- `pros audit import` on a full disk or an unwritable or newer store exits 5 with one line instead of a traceback; committed batches stay and unread spool files are kept.
- Runtime message files and session state under `$PRO_HOME/run/` are written 0600 in 0700 directories (they hold full task bodies); `run/log/events.log` is created 0600.
- Audit dashboard and management (step F1): `pros audit serve [--manage]` serves a static loopback dashboard (overview, professional drill-down, trace with delegation tree, waits, timeline and spend, quality and coverage) with the same JSON as the CLI; management (capture mode, retention, verdict entry, saved reports, export preview, purge) needs `--manage` and the owner token and is recorded as audit events.
- Saved reports and analysis (steps F2, F3): `pros audit report save|run|list|show|export|delete` (versioned definitions, reproducible local snapshots, redacted CSV and self-contained HTML), `pros audit compare --by revision`, `pros audit analyze` (task-class rubrics, benchmark datasets, stratified summaries, optional GEE when statsmodels is installed). The dashboard's saved reports use the same definitions as the CLI; purging the reports class also removes old snapshots.
- Audit capture (step D1): `pros` appends allowlisted events (messages, deliveries, waits, spawns, stops, failures with time, registry changes) to a private spool under `$PRO_HOME/audit/`; a single importer loads them idempotently into a private SQLite store. Capture never blocks or breaks a command; `capture_mode: off` in config.json or `PRO_AUDIT=off` turns it off. Registry entries now carry a stable `pro_id`.
- Audit usage and backfill (step D2): `pros audit import` reads the transcripts of known sessions (deduplicated by message id + request id, fork and resume copies counted once, verified joins only from delivery headers), rebuilds the derived tables, and can backfill the surviving history once. Store schema v2 (additive): `pro_name`, `source_meta`, `header_seen`, `exchange_conflict`, `session_residual` view.
- Audit queries (step D3): `pros audit status|summary|trace|explain|serve` with `--json`, snapshot ids and a coverage block on every answer; a loopback HTTP adapter with an owner token.
- Audit verdicts and export (step D4): `pros audit verdict` records an owner-only evaluation (criteria, opaque evidence refs, rubric versions, supersession; status must agree with the criteria; the effective verdict honours the report cutoff). `pros audit export` previews, then writes a redacted export (JSONL, CSV or self-contained HTML) with per-export pseudonyms that keep joins and a manifest; `--raw` is a separate, typed-confirmation mode.
- Audit integration (step E): one `pros audit` parser for reads, import, verdicts and export (owner only); the importer projects `audit_evaluation` events into evaluations in its own transaction; export coverage reads `source_meta` and its token sums match the trace; an end-to-end test drives spool, import, trace, verdict, reimport and redacted export through the real store.
- Five messaging professionals: `whatsapp-engineer`, `telegram-engineer`, `linkedin-messaging-engineer`, `instagram-messaging-engineer`, `facebook-messenger-engineer` (official versus unofficial routes, limits, ban risk, sessions, consent gates).
- Summoning a folder professional prints a warning when another live Claude session already runs in that folder.
- A session id held by two live processes (a double resume) is treated as live elsewhere: `pros` forks instead of resuming, and `pros doctor` reports it. `claude agents --json` rows are de-duplicated by pid.
- A folder's content hash now also covers files pulled in by `@` imports in CLAUDE.md/AGENTS.md that lie outside the folder (depth 3, 256 KB each, 20 files). Folders with such imports show `changed` once after upgrading; re-add with `--refresh`.

## 0.2.0

- Allowlist: only professionals you added (`pros add`, `pros enable`) can be summoned or messaged; the registry is checksummed and a change made outside `pros` blocks everything until you review it (`pros doctor --repair`).
- Folder professionals: `pros add <dir>` turns any folder with a `CLAUDE.md` into a professional. Content is hashed; a changed folder must be re-added with `--refresh`; hooks and MCP servers need `--trust-code`.
- Sessions: a professional resumes its last session, keeps a history (`pros sessions`), and can be pinned with `pros keep`.
- `pros ask` starts an allowlisted professional that is not running instead of failing with "summon it first".
- `pros ask --task ID`: asking again with the same id and the same text returns the stored answer and sends nothing new.
- The "allow external imports" dialog that appears for a CLAUDE.md with `@` imports outside the folder is answered only for folders you registered (unchanged, and without hooks or MCP unless `--trust-code`). Anywhere else the summon stops and tells you to run `pros attach`.
- Public spec sheet in `docs/spec/`.

## 0.1.0

- First release: `pros` command, core skill, ten professionals, tmux message bus.
