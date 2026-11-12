# Audit event schema, version 1

Status: frozen for the first release (step C). Machine-readable contract: [schemas/event-v1.json](schemas/event-v1.json) (JSON Schema 2020-12 subset: `type`, `enum`, `const`, `pattern`, `maxLength`, `minimum`, `required`, `additionalProperties`, `items`, `$ref`, `anyOf`, `if/then`). Store tables: [schemas/store-v1.sql](schemas/store-v1.sql). Examples that the contract test validates: `tests/fixtures/audit/contracts/events/`. Evidence: `research/B1.md`, `B2.md`, `B4.md`.

## 1. Rules

1. **Allowlist.** An event is the envelope plus a `data` object. Each action lists its fields; any other key is invalid (`additionalProperties: false`). Every string is an id, a hash, an enum or a pattern-bounded token. There is no free-text field. Prompts, message bodies, reply text, tool arguments or results, paths, stack text, hostnames and e-mail addresses cannot be represented.
2. **Emitting never raises and never blocks.** One `write()` of one JSON line to this process's spool file, no `fsync`. After any write error the emitter sets the degraded flag, stops appending to that file and starts a new one on the next event (B4: a torn last line must never be followed by a valid event).
3. **Stable ids.** `id` = `<writer>-<seq>`: `writer` is 8 hex chars minted once per process, `seq` counts up from 1 inside it. The importer is idempotent on `id` (`INSERT OR IGNORE`). Ids are never reused and never carry content.
4. **Times.** `t` is UTC epoch seconds (float) from the wall clock. `dur_ms` exists only where one process measured both ends with a monotonic clock (`wait_end`). A duration between two events from different processes is computed from `t` and labelled wall time.
5. **Stamping.** Every event that concerns a session carries `run` (the local run id minted by `pros` at spawn) and `session` (the Claude session id, `null` if not yet known). `msg_injected` requires both keys: the run windows of a fast resume or fork overlap, so time alone cannot place the delivery (B1 §3). The run id is distinct from the Claude session id: a resume reuses the session id, a fork gets a new one.
6. **Origin and provenance.** `origin` is `live` (emitted at the transition) or `backfill` (derived by the importer from surviving records). A backfill event must name `provenance` (`msgs`, `history`, `events_log`, `summons_log`, `registry`, `transcript`). Backfilled and live data are never mixed without that tag; the coverage block shows the cutoff before which only backfill exists.
7. **Facts, not conclusions.** Events record what was observed at a transition. Delivery status, deadline status, attempts, joins and verdicts are computed from them by the importer and the query layer, so a later rule change needs no re-emit.

## 2. Envelope

| Field | Required | Meaning |
|---|---|---|
| `v` | yes | Schema version, integer `1` |
| `id` | yes | Event id (rule 3) |
| `t` | yes | UTC epoch seconds |
| `action` | yes | One of the actions in section 3 |
| `actor` | yes | `user`, `professional`, `system`, `hook`, `pump`, `operator`, `importer` (role only, never a name) |
| `origin` | yes | `live` or `backfill` |
| `provenance` | when `origin=backfill` | source family |
| `pro` | when known | stable professional id `p-<12 hex>` |
| `pro_name` | optional | display name at event time (a rename never rewrites events) |
| `run`, `session` | per rule 5 | local run id `r-<12 hex>`; Claude session id (uuid) or `null` |
| `msg` | for message events | message id `m-<10 hex>` = the assignment id |
| `data` | yes | Per-action allowlisted object |

### Identity

- **Professional id** is minted once when the registry entry is first added and stored in it; a rename keeps it (`registry_change` with `change=renamed`, `renamed_from`), remove and re-add keeps it when the folder content identity or marketplace name matches (the entry is tombstoned, not forgotten), otherwise it is a new professional. Backfill derives `p-` + the first 12 hex of `sha1(origin + ":" + name)` and links ids it later sees merged. Project scope is a property of the run (`project` hash on messages), not of the professional.
- **Revision** is `hash12` of the persona hash (marketplace) or the folder content identity. A revision change is an event, so metrics group by revision without re-reading files.
- **Task** is a logical root: walk `parent` message links (a missing parent is an orphan with a `missing_parent` gap, never a guess). Task id strings are attributes, never join keys across assignments.

## 3. Actions and allowlisted `data` fields

All fields below are required unless marked optional. `msg` is the envelope field. Enum values are exhaustive for v1.

| Action | Emitted at | `data` fields |
|---|---|---|
| `msg_created` | message written (queued) | `kind` task/reply/note, `from_role`, `to`, `task`, `parent` (msg or null), `deadline` (epoch or null), `chain_len`, `project` (hash12), `body_bytes`; optional `from_pro`, `origin_session` |
| `msg_claimed` | delivery claim taken | `via` paste/stop-hook, `attempt` |
| `msg_injected` | text reached the session (`mark_injected`) | `via`, `attempt`, `tmux`, `dup`; envelope `run` and `session` required |
| `msg_answered` | reply, auto-reply or read closed the task | `answered_by` reply/auto-reply/read, `reply_msg` |
| `msg_read` | a reply or note was read | `reader` |
| `msg_failed` | delivery or task failed (T-X5: carries the time the record lacks) | `reason` (deadline_before_delivery, recipient_stopped, expired_unread, paste_failed, target_stuck, other), `attempts` |
| `msg_overdue` | a pump saw the deadline pass unanswered | `deadline`, `observed_by` pump/wait/hook (evidence only; the miss is computed from `deadline` and the answer time) |
| `msg_refused` | `pros ask` refused before any message exists | `reason` (duplicate, cap_live, cap_hourly, not_allowed, no_target), `to`, `task` |
| `wait_start` | caller starts `pros wait` or `ask --wait` | `wait` (`w-<hex>`), `caller`, `task`, `target_msg` (or null), `timeout_s` |
| `wait_end` | wait returns | `wait`, `outcome` (reply, note_only, pending, pending_target_stuck, failed), `exit`, `returned` (reply/note/none), `returned_msg`, `dur_ms` |
| `spawn` | session launched | `kind` fresh/resume/fork, `tmux`, `revision`, `scope`, `origin`, `parent_session` (resume/fork source or null), `live_count`; envelope `run`, `session`, `pro` |
| `session_end` | stop, reap or crash seen | `reason`, `answered`, `failed`, `timeout` (the history end-row counters, evidence only) |
| `hook_session` | any hook stored session ids | `hook`, `transcript_ref` (hash12 of the path, never the path), `deduped` |
| `rate` | the summon budget was checked | `summons_last_hour`, `limit`, `live`, `live_limit`, `allowed` |
| `registry_change` | add, remove, rename, new revision, trust change | `change`, `origin`, `revision`; optional `renamed_from` |
| `reap` | idle or cap reap | `reason` |
| `audit_config` | operator changed a capture or retention setting | `setting`, `value` (token) |
| `audit_import` | importer finished a batch | `batch`, `spool_files`, `events`, `duplicates`, `gaps`, `last_seq` |
| `audit_gap` | the importer or a source found a hole | `kind` (torn_line, unparseable, spool_overflow, write_error, missing_parent, missing_transcript, history_rotation, clock_skew, pre_audit), `source`, `from_t`, `to_t`, `count` |
| `audit_evaluation` | a verdict was recorded | `eval`, `target_kind` task/assignment, `target` (msg id), `status`, `evaluator_kind`, `evaluator`, `evaluator_version`, `rubric`, `rubric_version`, `effective_at`, `supersedes`, `criteria` (id, outcome passed/failed/not_assessed, optional evidence ref); optional `evidence` refs. Refs are opaque `kind:token`, never text or paths |
| `audit_export` | an export was written | `export`, `mode`, `format`, `rows`, `schema_version`, `manifest_sha256` |
| `audit_purge` | rows were deleted | `class`, `before`, `rows`, `reason` |
| `audit_correction` | an event or record was corrected or removed | `target_event`, `kind`, `fields` (names only) |
| `audit_migrate` | a store migration ran | `from_version`, `to_version`, `outcome` ok/rolled_back |

Notes from the evidence:

- **Attempt** = one observed delivery: a `msg_injected` event, or one verified transcript header row in a session (B1 §4). `dup` is evidence of a possible double delivery, not a count. Restart re-sends (`resend_unanswered`) are further `msg_injected` events on the same message id. A genuine re-execution is a new message (new id) and a new assignment; the runtime refuses same task id, same body and same target.
- **Waits are not task outcomes.** Waiter outcomes live in `wait_start`/`wait_end` and never change a message's delivery or deadline status. `shadowed_reply` is derived by the query layer: a wait that returned `note` while a non-system reply for the same task already existed.
- **`wait_end.outcome`** distinguishes `pending` from `pending_target_stuck` (a stuck target exits 3, not 5; B10 E-261). `exit` is stored as returned.
- Fail/refusal events carry the failure reason as an enum so the "failures by reason" card counts them without text.

## 4. Backfill

The importer builds `origin=backfill` events from `run/msgs/*.json` (created, injected, answered, read, failed without time, overdue flag), `history/<name>.jsonl` (spawn start/end rows with `resumed_from`/`forked_from`), `events.log` (auto-replies, errors) and `summons.log` (summon times). It mints run ids deterministically (`r-` + hash12 of `history id + ":" + start t`), marks fields the source lacks as absent (no guesses), and writes one `pre_audit` gap whose `to_t` is the first live event: the coverage cutoff. History rotation (newest 500 rows kept) is reported as `history_rotation` gaps. `task_counts` from history is never used as truth (it counts by name within a ±1 s window).

## 5. Migration rules

1. The store has a `schema_version` table. The version is the highest `version` row. The event schema version `v` and the store version are independent: events are immutable and are stored with the `v` they were written with.
2. A migration is a numbered step from N to N+1 that runs in **one transaction** (`BEGIN IMMEDIATE`; DDL and data changes together). On any error the transaction rolls back, an `audit_migrate` row with `outcome=rolled_back` is appended to `audit_log`, the importer stays stopped and every read keeps working against version N. The file is copied to `audit.db.bak-vN` before a destructive step (column drop, rebuild) and the copy is removed only after the next successful batch.
3. **Additive changes only inside a version:** new optional `data` fields, new enum values, new actions need no `v` bump when readers ignore unknown optional fields and the importer maps unknown actions to a `gap` row (`unparseable`). A required field, a changed type or meaning, a removed enum value or a changed id format bumps `v` to 2. Readers support the current and the previous `v` for one release.
4. Derived tables (`assignment`, `attempt`, `run`, `wait`, `exchange`, `professional`, `revision`) can be rebuilt from `event` + transcripts. A migration may therefore drop and rebuild them. `event`, `evaluation`, `report_def`, `content` and `audit_log` are authoritative and are only altered additively.
5. The importer refuses to open a store whose version is higher than it knows (read-only for queries with a coverage note), so a downgrade never writes.
6. Migration tests (D1): a failing step leaves the file at version N and readable; a re-run after fixing it reaches N+1; a crash mid-step (SIGKILL) leaves integrity ok.

## 6. Decisions recorded in step C

- **Adapter strategy.** The core counts usage with its **own stdlib transcript reader** (B2: exact on the fixture, equal to the tracker on 889 real exchanges, and the tracker over-counts forked or resumed copies and refuses unknown models). Usage sources sit behind one small interface (`name`, `read(session_ids) -> exchanges`, `quality`). Adapters: `transcripts` (core, default, feeds all totals), `session_counter` (Claude Code `cost-state`; core; feeds only the *unavailable* residual, never assignment totals), `ccost` (optional, Python 3.12+, never imported by the core; a cross-check command that compares session totals and reports differences, never feeds a figure), `otel` (later, opt-in, allowlisted attributes only; `user.email`, `user.account_uuid`, `organization.id` and any `session.id`/`prompt.id` not already in the store are dropped at ingest). An adapter result is never merged into a total silently: each exchange row carries `source`.
- **Task-join confidence policy.** Three levels, stored on each assignment and each exchange row.
  - `verified`: a delivery header row (`^(Stop hook feedback:\n|<pasted_content…>\n)?\[pro-msg id=` in a `type=user` row of the session the delivery event names, or of a session whose run window covers it). Header text in `tool_result`, `api-request`, `last-prompt` rows or in the middle of a human prompt never counts.
  - `mapped`: no transcript header, and the delivery event carries `run` and `session` (or exactly one run of the recipient covers the delivery time).
  - `unassigned`: everything else, including two plausible runs. Usage then stays at session/run level.
  - Usage after a verified header belongs to that message until the next plain prompt. **Overlap rule (brief wins over the B1 reader):** if another assignment to the same session is in flight at the time of the exchange, the exchange is **not** attributed: it is `unassigned` with `overlap=1`, reported in the session-only share and counted in the coverage block. The B1 reference reader attributes to the header message and only flags overlap; D2 must implement this stricter rule. No real overlap exists in the local data (0 of 110), so the stricter rule costs nothing today.
  - Cards count `verified` and `mapped` as linked and always show the split. `mapped` is never presented as verified.
- **Unrecorded task class.** No `task_class` field exists in v1 events. `task_class` is an optional operator tag on an assignment (`audit_evaluation` rubric name is the only class evidence). Group-by `task_class` returns `unclassified` until tagging exists.
