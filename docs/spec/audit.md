# professionals: audit, observability and evaluation (spec sheet)

Status: planned. Step A (runtime map and fixtures) is done; steps B to G are not built. Evidence pinned at professionals `c42af9f` and [claude-cost-tracker](https://github.com/talafek96/claude-cost-tracker) `bb5eb9f`.

## 1. Goal

A local audit capability for `pros`. An operator can see how often a professional is used, what work it does, where its time and tokens go, what it delegates, and which tasks met their acceptance criteria. The first release answers three questions about one task:

1. What happened on this task?
2. What did it consume?
3. Did the result pass, and who or what verified it?

Everything stays on the operator's machine. Export is an explicit action, redacted by default.

## 2. What `pros` records today (step A map, `plugins/professionals/bin/pros` at c42af9f)

| Area | Where | What it gives the audit | Gap |
| --- | --- | --- | --- |
| Commands | `main` 3087, `build_parser` 2980; `auto_pump` 1513 runs before every command except hook, version, doctor, pump | one entry point for capture | none |
| Hooks | `hooks/hooks.json` (SessionStart, UserPromptSubmit, Stop, Notification permission_prompt, SessionEnd; 10 s timeout); `cmd_hook` 1571, `pro_hook` 1591 (duplicate hook within 3 s ignored, 1594-1598), `hook_stop` 1657, `plain_hook` 1684 | Claude `session_id` and `transcript_path` per hook into `run/state/<session>.json` | the hook state is overwritten, no history |
| Messages | `new_msg` 1131, `save_msg` 1116, `post` 1232, `deliver_next` 1264, `paste_deliver` 1293, `mark_injected` 1214, `do_send` 1403, `cmd_reply` 2407, `auto_reply` 1625 | one JSON file per message: id, task, kind, from, to, parent (message id), chain, created, deadline, state, attempts, injected_at, via, answered_at, overdue, dup, fail_reason | the file is overwritten on each transition: only the last state and some timestamps survive |
| Delegation | `do_send` 1416-1442, `current_task` 1384 | a child task has `parent` = the parent task's message id and its own new task id; its deadline is at most the parent's minus 20 s | task id strings do not link a child to its root; callers may reuse a task id |
| Deadlines | `pump` 1491-1499 | an overdue task gets `overdue: true`; a queued one becomes `failed`; an injected one stays open and can still be answered (`answered_at` after `deadline`) | the miss is a flag, not an event with a time |
| Waits | `wait_for` 2437 (0 reply, 3 pending, 5 stuck), `reply_for` 2430, `finish_wait` 2485, `waiting` 1370 | wait files exist only while a wait runs | waiter outcomes are not stored; `reply_for` returns the first reply or note for a task, so a deadline note can shadow a later real reply (fixture `ask_delegation.json`) |
| Sessions | `spawn_session` 2134 (session record, `--session-id`, `--resume`, `--fork-session`), `decide_start` 763, `drop_session` 1092, `stop_session` 1522, `reap` 1540 | live record in `run/sessions/`, deleted when the session ends | a resume keeps the Claude session id: there is no id per run |
| History | `hist_append` 613 (mode 0600, one write per row, rotation to the newest 500 rows above 1000, 625-629), `hist_sessions` 644, `task_counts` 685, `hist_end` 697, `cmd_rate` 1994 | start (resumed_from, forked_from), end (reason, answered, failed, timeout), rate, alias rows | rotation loses old rows; task counts are a time window over all messages to that name (±1 s), not linked to the run |
| Logs | `log` 132 to `run/log/events.log`; `run/summons.log` | errors, auto-replies, summon times | most transitions are not logged |
| Identity | registry entries keyed by name (`reg_get` 392), `persona_hash` (`sha` 308), folder identity by content hash (`folder_identity` 545), session name `pro-<name>[--<project hash>]` (`session_name` 931) | stable name and a definition revision | rename or remove/re-add is not tracked as one professional |
| Permissions | `build_settings` 2085 and `pro_home_denies` 797 deny professional sessions Read/Edit of registry, config, history and run (and sandbox denyRead); `require_user` 454 checks env and process ancestry | the audit store can be denied the same way | files written by `write_json` 116 get the process umask (typically 0644); only history is 0600 |

Facts proved by the fixtures (`tests/fixtures/audit/`, test `tests/test_audit_fixtures.py`):

- A delegated child links to its root only through `parent` (message id); its task id differs.
- After a missed deadline the root stays open, is answered late, and keeps `overdue: true` with `answered_at > deadline`.
- The caller's short wait returns pending (exit 3); after the deadline, `pros wait` returns the timeout note (exit 5) even after the late reply arrived.
- A resume keeps the session id (`resumed_from` equals `id`); a fork gets a new id with `forked_from`.

## 3. Design

### 3.1 Capture path

`pros` emits small allowlisted events at the transitions above into an append-only spool under `$PRO_HOME/audit/spool/` (one file per writer process, stable event ids, a bounded append that never raises). One importer drains the spool in batches into SQLite (WAL) idempotently, enriches from transcripts, and reconciles gaps from the existing records. If recording or import fails, capture keeps moving and every audit surface shows a degraded flag with its cause and time range. Surviving legacy records (messages, history, logs) are backfilled with provenance and a visible coverage cutoff.

Alternatives considered: writing SQLite directly from hooks (rejected: hooks have a 10 s timeout and must never fail; writer contention lands in the session); deriving everything after the fact from the message files (rejected as the only source: transitions are overwritten and history rotates; kept for backfill and reconciliation).

Code layout: the emitter (a few dozen lines) lives in `pros`; the importer, query API, CLI subcommands and loopback HTTP adapter live in a standard-library package next to it, loaded only by `pros audit ...`.

### 3.2 Records

| Record | Identity and contents |
| --- | --- |
| Professional, revision | stable id across rename and remove/re-add, display name, origin (marketplace or folder), definition revision hash, project scope |
| Session, run | Claude session id and alias; a local run id per launch, resume or fork; start and end reasons; project; resume and fork lineage |
| Task, assignment | logical root task; assignment = message id; parent assignment; sender, assignee, deadline, task class when known |
| Attempt, span | attempt ordinal and redelivery reason; span types assignment, model request, tool, wait, delegation; links for several causal predecessors; unsupported spans stay unobserved |
| Usage | model, exchange id `(message.id, requestId)`, token classes (input, output, cache write, cache read), context window when known, source and attribution method, confidence |
| Evaluation | target (task or assignment), evaluator kind and version, rubric version, criterion outcomes, evidence references, effective time, supersedes link |
| Audit event | time, action, actor role, target, outcome, origin, schema version: configuration changes, retention, export, import, integrity and coverage gaps |

### 3.3 Linking

- Assignment joins use message ids; the root task is found by walking `parent`. Task id strings are attributes only.
- Usage maps to an assignment only through a verified link (the transcript prompt that carries that message's `[pro-msg id=...]` header) or a single assignment in flight in that session (mapped). Otherwise usage stays at the session or run level, reported as unassigned. Each usage row stores its confidence.
- Duplicate deliveries (`dup`) and genuine re-executions are recorded separately.
- Model usage is deduplicated on `(message.id, requestId)` before summing; Claude Code writes one row per content block, each repeating the usage. A conflicting usage for one key is counted first-wins and reported.

### 3.4 Accounting

Provider counts, derived values, estimates and missing values stay distinct. Token class semantics are fixed per model before summing. Usage rolls up once per exchange; each assignment shows its own and its inclusive cost, with a residual where it can be measured. Prices come from a pinned, dated table; costs are labelled API-equivalent; an unknown model is `unpriced`, never guessed. Times are UTC; durations come from a monotonic clock inside one process. Root elapsed time, queue time, reply latency, active span time and critical path are reported separately (parallel intervals can overlap).

### 3.5 Evaluation

Delivery, deadline and evaluation statuses are separate and queryable on their own. Evaluation values: `passed`, `partial`, `failed`, `inconclusive`, `unreviewed`. A summary uses the latest non-superseded verdict per target; an operator verdict outranks an automated check at the same target; both stay visible. Pass rate = passed / conclusive; coverage = conclusive / completed eligible; partial, failed, inconclusive and unreviewed counts are shown beside both.

### 3.6 First dashboard cards

| Metric | Counting rule |
| --- | --- |
| Assigned work | distinct assignment ids submitted in the window; root tasks and child assignments counted separately |
| Evaluation coverage, pass rate | as in 3.5 |
| Deadline miss rate | assignments without a reply by their deadline / eligible assignments whose deadline has passed; late answers count as misses; cancellations and the query cutoff shown |
| Queue and work duration | queue time = first delivery - queue entry; reply latency from first delivery; active time on known spans; sample size and coverage per percentile |
| Accounted tokens | unique exchanges in scope and window; linked task usage, session-only usage and unavailable coverage shown |

### 3.7 Interfaces

- **Query API**: one versioned Python API (professionals, runs, trace, metrics, explain, evaluations) with filters, cursor ordering, units, snapshot id and coverage in every answer; bounded queries.
- **CLI**: `pros audit summary --since 30d`, `pros audit trace <task> --json`, `pros audit explain <metric>`, `pros audit verdict ...`; later `compare`, `report save`, `export --redact`.
- **Dashboard**: loopback-only page on the same API, read-only by default; drill-down professional → task → span → evidence, delegation tree, timeline, spend and context, quality and coverage. Management (capture settings, retention, verdict entry, saved reports, export preview, purge) as owner-only actions.
- **Export**: preview scope, redacted by default, per-export pseudonyms that keep joins, manifest with counts and schema version; JSONL, CSV, self-contained HTML; raw export separately explicit.

### 3.8 Privacy, access, retention

The store lives in `$PRO_HOME/audit/` (directory 0700, files 0600) and is added to the deny rules and sandbox denyRead of professional sessions. All sessions run as one OS user, so this protects against a professional's tools, not against the owner; the owner can always edit the files. Default capture is metadata only: no prompts, tool arguments or results, paths or stack text. Content capture is a separate, time-limited opt-in with its own table and retention. Retention is set separately for metadata, content, reports and exports; corrections and deletions are audit events. Migrations are transactional and versioned.

## 4. Work plan

Sibling steps run in parallel; a step waits for all its inputs.

| Step | Output | Done when |
| --- | --- | --- |
| A | runtime map (section 2), baseline suite, fixtures `tests/fixtures/audit/` | done: 162 baseline tests pass; `test_audit_fixtures.py` passes |
| B1 | runtime joins proved: header-to-message mapping in transcripts, stop-hook delivery form, redelivery vs re-execution | each join rule has a fixture and a measured coverage figure |
| B2 | usage sources compared: own transcript reader, cost tracker JSON output (pinned), optional local OpenTelemetry | exact numbers on the usage fixture and one real sample; adapter decision recorded |
| B3 | metric catalog and rubric fixtures | each first-release metric has question, numerator, denominator, unit, exclusions, coverage and a golden value on the fixtures |
| B4 | storage and access tests | SQLite WAL writer contention, checkpoint, snapshot, recovery and spool overhead measured; denied access from a professional session proved |
| C | frozen contracts: event schema, store schema, query and metric contracts, privacy modes, retention, migration, rollout | contract files versioned; shared fixtures and benchmark budgets published |
| D1 | emitter, spool, importer, store | crash between capture and import loses and doubles nothing; capture overhead within budget |
| D2 | usage import and historical backfill | fixture totals exact; reimport idempotent; backfill coverage cutoff visible |
| D3 | query API and CLI (summary, trace, explain, status) | golden JSON on the fixtures |
| D4 | verdicts and redacted export | verdict precedence tests; export free of canaries |
| E | first local trace end to end (controlled run with the stub) | trace, usage with residual, verdict and redacted export agree after restart and reimport |
| F1 | dashboard and management | the same fixtures give the same JSON as the CLI |
| F2 | saved reports and advanced CLI | reports reproduce from definition and snapshot |
| F3 | benchmarks and analysis | rubric classes, benchmark datasets, optional statsmodels workflows |
| G | release | release acceptance fixture passes; permissions, export redaction, migration, recovery tested; operations documented (enable, inspect, disable, back up, restore, purge); version and changelog |

Follow-up found in step A (separate fix, not part of the audit): `pros wait` should return the real reply when one exists, not the first matching note.

## 5. Release acceptance fixture

Ask `system-designer` for an architecture review with one branch delegated to `reliability-reviewer`. The caller's short wait returns pending, the root replies after the task deadline, and an operator records a partial verdict with one failed criterion. A resumed transcript repeats an earlier exchange. The CLI and the dashboard show the same delegation, the pending wait, the late reply and the partial evaluation. On the usage fixture the root owns 140 tokens and the child 80 (trace total 220); 30 more tokens belong only to the root session. Reimport after a restart keeps those totals. A redacted export keeps the joins and contains none of the planted canary strings. Further tests: crash between capture and import, fork duplicates, missing ids, overlapping tasks, denied access, retention, failed migration, disk pressure.

## 6. Test map

| Requirement | Test |
| --- | --- |
| step A fixtures, record-shape drift | `tests/test_audit_fixtures.py` |
| capture, spool, importer, crash recovery | `tests/test_audit_capture.py` |
| usage dedup and attribution | `tests/test_audit_usage.py` |
| trace and joins | `tests/test_audit_trace.py` |
| evaluation | `tests/test_audit_eval.py` |
| export redaction | `tests/test_audit_export.py` |
| access, retention, migration | `tests/test_audit_access.py`, `tests/test_audit_migrate.py` |
| release acceptance fixture | `tests/test_audit_release.py` |

Regenerate the fixtures with `python3 tests/audit_fixtures.py` (stub claude and a private tmux socket; about one minute).
