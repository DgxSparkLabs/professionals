# Changelog

## Unreleased

## 0.5.0

- Failover: when the professional holding a task dies, or the task passes its deadline while the target is gone, stuck, in a dialog or at a usage limit, `pros` re-runs the task on a fresh session of the same professional (the broken one is stopped first), or on a fallback the sender named with `pros send|ask --fallback <name>`. With a fallback, a deadline miss re-runs even when the target is only busy; without one, a busy target keeps the timeout note and the late reply.
- At most `max_attempts` runs per task (config.json, default 2 including the first; 1 turns failover off and keeps the 0.4.0 behaviour). When no attempt is left the task fails with one final note; a later manual summon does not resend it.
- The re-run is a new message: same task id and sender, `attempt`, `retry_of`, `fo_via`; its header ends `via=<name> attempt=<n>`; with a fallback the chain ends in the fallback. It is stored and queued before the failed attempt is closed (store-first), and started by a separate `pros summon` with a clean environment, so the allowlist and the caps apply: a refused start keeps it queued and the note says why.
- The sender gets one note per failed attempt. The note is progress: it does not end `pros wait`/`ask`, which follow the re-run (also to a fallback). The first reply wins; a later reply from another attempt is stored, not delivered.
- `--fallback` is refused with exit 4 when the name is not allowlisted or is already in the chain, or when the sender has `may_summon: false`; with exit 2 when it names the target or the sender.
- `pros wait` returns the final note instead of `stuck: gone` when the pump wrote it in the same pass.
- Audit (additive, event schema stays v1, store stays v2): `msg_created` has optional `attempt`, `retry_of`, `via_pro` (pseudonymised in redacted exports); `msg_failed` has reasons `recipient_died` and `deadline_missed`; the derived tree puts a re-run in its first attempt's tree, and `pros audit trace` shows `failover: {attempt, of, via}` (query schema: optional field). Event data is stored as JSON and no derived column was added, so no store migration is needed (the store stays at version 2). An older `pros` importing these events would quarantine them (its allowlist predates the fields); upgrade before importing.

## 0.4.0

- Wake on message: `pros send` to an allowlisted professional that is not running starts it, as `ask` already did, instead of exit 5. The caps are checked first and still refuse with exit 4 before anything is stored. Otherwise the message is stored and queued before the start, so a start that fails keeps it queued (for the next start, or until its deadline sends the usual timeout note). `--no-wake` keeps the old refusal.
- Resend counter: after a restart each unanswered task is resent once (`resends` on the message, `max_resends` in config.json, default 1). A further restart, or a passed deadline, fails the task with one note to the sender instead of redelivering it again; before, a crash loop redelivered it on every start.
- `pros wait` and `ask` return the newest real reply to the latest ask of a task id: a late reply now wins over the deadline notice (exit 0 instead of 5), and a reused task id no longer returns an earlier ask's reply. The notice is returned only when no real reply exists.
- A wait ends with exit 5 (`stuck: <reason>`) when its target is stuck (busy with no activity for `stuck_min`), instead of waiting to the timeout; the audit `wait_end` outcome is `pending_target_stuck`.
- `pros audit status` before the first `pros audit import` prints one line ("no audit data yet; run: pros audit import") and exits 5, without a traceback.
- A failed start no longer fails the messages queued for that professional.
- Audit fixture `ask_delegation.json` updated for the late-reply wait (it now exits 0 and reads the reply; the inbox is empty). Only the records this change affects were taken from a fresh `tests/audit_fixtures.py` run, because a full rerun shifts timings that the golden cards and contract fixtures are pinned to; `schema.json` is unchanged (a full rerun yields the same schema).

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
