# Audit metrics contract, version 1

Status: frozen for the first release (step C). Source: `research/B3.md` (rules and golden values), `research/metrics_ref.py` (reference computation), `tests/fixtures/audit/golden/cards.json`. The first-release implementation must reproduce the golden file exactly on the step A fixture. Results are returned by `query_metrics` and explained by `explain_metric` ([query.md](query.md)).

## 1. Common rules

- **Units.** An assignment is a message id of kind `task`. A root task has no `parent`; a child assignment has one. Roots and children are always counted apart. Task id strings are attributes, never join keys.
- **Window.** Half-open `[since, until)`; `cutoff = min(until, now)` is shown on every card. Each card names the event time that places a record in the window.
- **Ratios** are `[numerator, denominator]`; a zero denominator gives `null` plus a reason.
- **Every card carries** one common block, required by `schemas/query-v1.json` (`$defs/cards`): `coverage` (`[records with the needed field, eligible]`), `source_quality[]` (emitter, backfill, transcript, session counter), `confidence` (`{verified, mapped, unassigned}` counts of the card's unit: assignments, or tokens for the tokens card), `event_time` (the time that places a record in the window) and `cutoff`. Card-specific extras: assigned work `refusals_by_reason`; evaluation `not_completed` per row; tokens `cache_write_split`, `cost_usd_api_equivalent`, `price_state`, `unpriced_tokens`, `unpriced_share`.
- **Dimensions** (when recorded): professional, revision, model, scope, direct (sent by the user) vs delegated (`parent` set). `task_class` is not recorded in v1 events; grouping by it returns `unclassified` until an additive tag exists (events.md, rule on additive changes).
- **Pairs.** No card is shown without its partner: pass rate with coverage, durations with n, tokens with the unavailable share. Activity volume is never a quality or ranking signal. Within-class comparison is the only comparison offered.
- **Statuses are independent.** Delivery, deadline and evaluation statuses are separate fields.

## 2. The five first-release cards

### 2.1 Assigned work (`assigned_work`)

Numerator: distinct assignment ids created in the window. Output: `assignments`, `root_tasks`, `child_assignments`, `refusals_by_reason`. Event time: `created`. Excluded: replies, notes, sends refused before a message exists (`msg_refused`, counted by reason in `refusals_by_reason`). Decision: which professionals receive real work. Golden: 2 = 1 root + 1 child.

### 2.2 Evaluation coverage and pass rate (`evaluation`)

- **Eligible**: completed tasks, delivery `answered`, late answers included. Roots and children on separate rows. Not completed (failed, cancelled, in flight) are outside the denominator and shown as `not_completed`.
- **Event time** of this card: `answered_at` of the assignment.
- **Effective verdict** per target: computed over the evaluations with `effective_at <= cutoff` **and** recorded time (event `t`) `<= cutoff`, so a verdict that lands after the cutoff leaves the earlier one effective and a back-dated verdict cannot rewrite a saved report. Among those: the latest non-superseded evaluation (tie on `effective_at`: the higher event `seq`); an operator verdict outranks an automated check on the same target; every evaluation stays visible. `supersedes` must name an evaluation of the same target (rejected at write time otherwise); an automated evaluation may not supersede an operator one (it is recorded but the operator verdict stays effective). `target_kind=task` requires a root message. Fixture check: at cutoff 100 s the root's effective verdict is e2 `passed` (e3 is effective at 120 s).
- **Conclusive**: `passed`, `partial`, `failed`. Not conclusive: `inconclusive`, `unreviewed` (no evaluation).
- **Coverage** = conclusive / completed eligible. **Pass rate** = passed / conclusive. The counts of passed, partial, failed, inconclusive and unreviewed always sit beside both ratios.
- **Rubric consistency** (D4 validation): `passed` when every required criterion passed; `failed` when every required criterion failed; `partial` otherwise; `inconclusive` when any required criterion has no outcome (`not_assessed`); `inconclusive` is checked first, so C1 failed with C2 `not_assessed` is `inconclusive`. A stored status that contradicts its criteria is rejected at write time. Versioned rubrics (criterion ids and `required` flags) are stored as files under `$PRO_HOME/audit/rubrics/<rubric>/<rubric_version>.json`, written by `pros audit rubric add` (owner only), and a verdict names rubric and version.
- Golden: root coverage [1,1], pass rate [0,1], partial 1 (e3 supersedes e2, C1 failed); child coverage [1,1], pass rate [1,1] (automated e1).

### 2.3 Deadline miss rate (`deadline_miss`)

Eligible: assignments whose `deadline <= cutoff`, cancellations excluded and counted. Missed: no answer at or before `deadline`; a late answer is a miss and is also counted as `late_answers_among_missed`. Rate = missed / eligible, roots and children apart; `not_yet_due` is shown. Computed from `deadline` and the answer time, never from the runtime `overdue` flag (set only when some pump runs; evidence only). Event time: `deadline`. Golden: all [1,2], root [1,1] with 1 late answer, child [0,1].

### 2.4 Queue and work duration (`durations`)

- Queue time = first observed delivery (first `msg_injected`, or backfilled `injected_at`) minus `created`.
- Reply latency = answer time minus first delivery.
- Active time = per assignment, the union of its `model_request` and `tool` spans (events.md section 6, quality `derived_wall`); `wait` and `delegation` spans are not active. Event time: `answered_at`. Assignments with no verified exchange are outside coverage, never inferred from latency.
- p50 and p95 by nearest rank, with `n` and `coverage [n, eligible]` beside each. Wall times from records; monotonic durations only where one process measured both ends (`wait_end.dur_ms`).
- Golden (contract fixture): queue p50 0.38 s, p95 0.39 s (n 2/2); reply latency p50 0.09 s, p95 40.69 s (n 2/2); active p50 1.0 s, p95 40.9 s (n 2/2: root 40.9, child 1.0). The B3 file `golden/cards.json` stays the pre-span reference (active n 0) and is not changed.

### 2.5 Accounted tokens (`accounted_tokens`)

Sum of unique exchanges in the window, by category (input, output, cache write, cache read; never one blended number without the split). Split: linked task usage (own per assignment, inclusive per root), session-only usage, and unavailable usage (session counter minus transcript sum, null when no counter). Event time: exchange timestamp. Surfaces compare totals only at the same scope (assignment own, root inclusive, session). Cost is derived from the pinned dated table (`snapshot.pricing`, events.md section 6) in USD API-equivalent; a model missing from the table is `unpriced` (tokens counted, cost null, `unpriced_tokens` and `unpriced_share` shown), never a guessed rate; a mix is `partial`. Cache write cost uses the `usage.cache_creation` 5m/1h split, stored in `cache_write_5m` and `cache_write_1h` (B2: a 5m-only price understates real 1h writes by 37.5 %); tokens whose split was not reported are counted in `cache_write_unsplit`, priced at 5m, and make the cost a lower bound. Linked usage is `verified` only; `confidence` shows the token split. Golden: 250 unique = 220 linked (root own 140 + child 80, inclusive 220) + 30 session-only; unavailable null.

## 2b. v1 catalog (not cards)

Every first-release metric of the brief outside the five cards. Each is a ratio or count with numerator, denominator, event time, coverage and source, returned by `explain_metric` under its name and listed with the cards in `query_metrics` only where marked. Window and `cutoff` as in section 1; no catalog entry ranks professionals.

| Name | Numerator / denominator | Event time | Coverage | Source |
|---|---|---|---|---|
| `sessions_with_work` | distinct sessions with at least one assignment in the window / sessions that existed | assignment `created` | runs with a session id | emitter, history |
| `active_days` | distinct UTC days with at least one assignment or exchange / days in the window | `created` or exchange time | all | emitter, transcript |
| `work_per_active_week` | assignments / active ISO weeks | `created` | all | emitter |
| `direct_delegated_mix` | direct (no `parent`) vs delegated (`parent` set) assignments / all | `created` | assignments with a known parent state | emitter |
| `cache_read_share` | cache read tokens / (input + cache write + cache read) | exchange time | exchanges with usage | transcript |
| `cost_estimate` | USD API-equivalent sum / exchanges priced | exchange time | priced / all exchanges | transcript + pricing table |
| `unpriced_share` | unpriced tokens / all tokens | exchange time | all | transcript + pricing table |
| `attribution_share` | tokens of `verified` assignments / all tokens (mapped and unassigned shown) | exchange time | all | transcript + events |
| `reply_rate` | assignments answered / assignments delivered | `answered_at` or window end | delivered | emitter |
| `execution_time` | active time per assignment, p50 and p95 (same figures as the durations card) | `answered_at` | assignments with spans | spans |
| `failures_by_reason` | `msg_failed` and `msg_refused` per enum reason / assignments | event `t` | all | emitter |
| `concurrent_occupancy` | per professional: max and time-weighted mean of assignments in flight (delivered, not yet answered) / window length | delivery to answer | assignments with both times | emitter |
| `delivery_retries` | `msg_injected` events beyond the first per message / assignments (`dup` shown beside, as evidence) | event `t` | delivered | emitter |
| `context_footprint` | per exchange `input + cache_write + cache_read` by `turn` index in its session; p50 and max per session | exchange time | exchanges with a turn index | transcript |

**Deferred with reason:** *compaction events* (no verified marker in the transcripts examined in B1 and B2, so any rule would be a guess), *cancellation* (see below) and *context window size per model* (needs a model catalog that v1 does not keep). Each returns in a later contract version when the data exists.

**Cancellation is not observable in v1.** `pros` has no cancel transition and no event produces `cancelled`. Cards show `cancelled: null` (not `0`), and the `deadline_status` value `cancelled` is reserved.

## 3. Pairing and counting rules (usage)

1. **Dedup before any sum**: key `(message.id, requestId)`, first wins. Order of the candidates: the session with no `parent_session` in the spawn lineage first (the original), then the earliest first-row timestamp, then the file name; so an original beats a fork or resume copy even when the copy has identical row timestamps. A repeated key with different usage is counted once (first) and reported as a `conflict`. Rows missing either id are counted with a `no_key` flag and a coverage note.
2. **Own vs inclusive**: own = exchanges attributed to the assignment; inclusive = own of the root plus every descendant found by walking `parent`. Residual is shown at the scope where it can be measured: session-only per session, and unavailable per session from the session counter.
3. **Attribution** follows the join-confidence policy in [events.md](events.md) section 6; overlap exchanges are unassigned.
4. **Sampling and censoring**: windows cut a task in two; a task counts in the window of its event time for each card, and usage rows count in the window of their own timestamp. Cards may therefore differ at window edges; `explain_metric` shows included ids.

## 4. Evaluation records

Fields: target (task or assignment message id), evaluator kind and name and version, rubric and rubric version, criterion outcomes (`passed`, `failed`, `not_assessed`), evidence refs (opaque `kind:token`), `effective_at`, `supersedes` (a link, never an overwrite). First-release evidence kinds: operator note, acceptance-test or check result, missing requirement, human rework, documented acceptance. Later (not v1): task-class rubrics, local trace grading, benchmark datasets, model graders as separate evaluators that never send content off the machine without an explicit operator choice.

## 5. Later lenses (not frozen)

Repeat use and cohort retention, cost per verified success, first-pass acceptance, rework, funnel (assigned, delivered, answered, evaluated, passed; always with coverage), latency control charts within one task class. Each needs data v1 does not collect (eligibility events, spans, task class) and stays a follow-up.

## 6. Note on the B3 reference

`research/metrics_ref.py` picks the effective verdict without checking `effective_at <= cutoff` (the contract test applies the filter before calling it), and its golden window ends before the operator verdict (effective 120 s, window end 100 s). The contract requires the cutoff check, so the shared query fixture uses a window ending at 200 s; the card numbers are identical. D3 must implement the cutoff rule and the reference should be fixed to match when it is next touched.
