# Audit query contract, version 1

Status: frozen for the first release (step C). Machine-readable: [schemas/query-v1.json](schemas/query-v1.json) (response envelope and the result shape of each method). Examples: `tests/fixtures/audit/contracts/query/`. The CLI (`pros audit … --json`), the saved reports and the loopback dashboard all call this one layer, so one fixture gives one answer on every surface.

## 1. Shape

A Python module `pros_audit.query` (stdlib, loaded only by `pros audit …`) exposes six typed methods. Each takes plain arguments and returns one JSON-serializable envelope:

```
{"api_version": 1, "method": "...", "snapshot": {...}, "coverage": {...}, "units": {...}, "result": {...}}
```

The loopback HTTP adapter maps `GET /api/v1/<method>?<args>` to the same call and returns the same envelope byte for byte (sorted keys). It validates every argument against the tables below, rejects unknown ones (HTTP 400, `error.code=bad_request`) and exposes no write.

Errors use `{"api_version": 1, "error": {"code": "...", "message": "...", "field": "..."}}` with codes `bad_request`, `not_found`, `store_unavailable`, `forbidden`, `too_large`. A degraded store still answers; the degraded flag is in `coverage`.

## 2. Common rules

| Topic | Rule |
|---|---|
| Snapshot | Every answer names a snapshot `{id, schema_version, last_batch, max_seq, taken_at}`. The id is `s-` + hash12 of `(schema_version, last_batch, max_seq)`, not a held transaction (B4: a held reader blocks checkpoints and grows the WAL). Each call uses one short read transaction, so an answer is internally consistent. There is no `snapshot` argument: a caller compares ids across calls. Two calls with equal snapshot ids over equal arguments are equal. |
| Coverage | `coverage = {degraded, degraded_cause?, cutoff, pre_audit_before, sources[], gaps[]}`. `sources` lists each source (`emitter`, `backfill`, `transcript`, `session_counter`) with `quality` (`observed`, `reconstructed`, `partial`, `missing`), time range, row count and tool versions. `gaps` counts the gap kinds from [events.md](events.md). `pre_audit_before` is the coverage cutoff before which only backfilled data exists. |
| Time | Arguments `since`/`until` accept epoch seconds or a relative `Nd`/`Nh`; the response always reports resolved UTC epoch seconds. The window is half-open `[since, until)`. `cutoff = min(until, now)`. The event time that places a record in a window is named by each card. |
| Units | `units` maps each numeric field family to `seconds`, `tokens`, `usd_api_equivalent`, `count` or `epoch_s_utc`. Costs are API-equivalent, never a bill. Ratios are `[numerator, denominator]` and never a bare percentage; a zero denominator gives `null` with a reason. |
| Filters | `professional` (name or id), `revision`, `model`, `task_class`, `direct` (`direct`, `delegated`), `status` fields where a method lists them. Filters combine with AND. Unknown names return an empty set plus a coverage note, never an error. |
| Order and cursor | Lists order by an explicit key with the id as tie-break (stable). `limit` is 1..500 (default 50). `next` in `page` is an opaque cursor: base64 of `(order key, id)` of the last item; pass it as `cursor`. A cursor is valid only with the same filters and order, and a changed snapshot does not invalidate it (keyset pagination, no offsets). |
| Bounds | At most 500 items per list, `included_ids` capped at 500 with `included_total`, window at most 400 days, a request that exceeds a bound is `too_large`. |
| Access | Every method requires the owner: the CLI calls `require_user` (a professional session can run `pros` but must not read the audit), the HTTP adapter binds 127.0.0.1 only and checks a per-start token for management calls. |
| Missing values | A value that is not observed is `null` with its coverage entry, never `0`. Provider counts, derived values and estimates are separate fields (`own_tokens` provider counts; cost is derived and says `price_state`). |

## 3. Methods

### `list_professionals(since?, until?, origin?, cursor?, limit?)`

Items: `pro`, `name`, `origin`, `revisions[]`, `first_seen`, `last_seen`, `assignments`, `runs`, `removed`. Order: `last_seen` descending, then `pro`. Activity volume is shown but never sorted into a ranking label.

### `list_runs(professional?, session?, kind?, since?, until?, cursor?, limit?)`

Items: `run`, `pro`, `session` (Claude id or null), `kind` (fresh/resume/fork), `started`, `ended`, `end_reason`, `parent_run`, `assignments`. Order: `started` descending, then `run`. A resume is a second run with the same `session`; a fork has a new session and `parent_run`.

### `task_trace(id, depth?)`

`id` is a message id or a task id string. A task id string that matches several root assignments returns each as a separate trace candidate (`error.code=bad_request` with the candidates listed in `field`); assignments are never merged. Result: `root`, `tasks[]` (task id attributes seen), `assignments[]` (a tree flattened with `parent` and `depth`; each row has independent `delivery`, `deadline_status`, `attempts[]`, `confidence`, `run`, `session`, times, `own_tokens`, `evaluation`), `waits[]` (waiter outcomes, separate from the task outcome; `shadowed_reply`), `usage` (`own` per assignment, `inclusive` for the trace, `session_only[]`, `unavailable`, `residual_note`), `evaluation` (effective verdict of the root), `gaps[]` (orphans, missing transcripts). Statuses:

- `delivery`: `queued`, `injected`, `answered`, `failed`, `unknown`.
- `deadline_status`: `met`, `missed`, `late` (answered after the deadline; counts as a miss), `not_yet_due`, `cancelled`, `none`.
- Evaluation status is a third field (`passed`, `partial`, `failed`, `inconclusive`, `unreviewed`). The three never collapse into one.

### `query_metrics(since, until, group_by?, filters…)`

`group_by` is a list of `professional`, `revision`, `model`, `task_class`, `direct_vs_delegated`, `day`. Result: `window`, `group_by`, `cards` (the five cards, [metrics.md](metrics.md)) and, with grouping, `groups[]` (each with its key and the same cards). Group sample sizes are always visible; groups below `n=5` carry `small_sample=true`.

### `explain_metric(metric, since, until, filters…)`

Metrics: `assigned_work`, `evaluation_coverage`, `pass_rate`, `deadline_miss_rate`, `queue_time`, `reply_latency`, `accounted_tokens`. Result: `definition`, `numerator`, `denominator`, `unit`, `exclusions[]` (reason and count), `included_ids[]` (assignment ids, capped), `included_total`, `coverage_note`, `source_quality[]`. The numbers equal the matching card for the same snapshot and filters (tested).

### `evaluation_evidence(target)`

`target` is a message id. Result: `effective` (the verdict used by summaries), `history[]` (every evaluation, superseded ones marked), `precedence_note`. Evidence refs are opaque (`kind:token`); the method never dereferences them into content.

## 4. CLI mapping

| Command | Method |
|---|---|
| `pros audit status` | store version, snapshot, coverage block, degraded flag |
| `pros audit summary --since 30d [--by professional]` | `query_metrics` |
| `pros audit trace <id> [--json]` | `task_trace` |
| `pros audit explain <metric> [--since …]` | `explain_metric` |
| `pros audit verdict <target> --status … --criterion C1=failed --rubric … --evidence …` | write (owner only, records `audit_evaluation`) |
| `pros audit import` | run the importer once |

`--json` prints the envelope. Without it, text output is rendered from the same envelope and always prints the snapshot id, the coverage cutoff and any degraded note. Later commands (`compare`, `report save`, `export`) extend the same layer.

## 5. Versioning

`api_version` is `1`. Adding optional result fields or methods does not change it; removing or retyping a field does (version 2, served alongside 1 for one release). The contract test validates the golden envelopes against `schemas/query-v1.json`.
