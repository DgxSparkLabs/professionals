# Audit metrics contract, version 1

Status: frozen for the first release (step C). Source: `research/B3.md` (rules and golden values), `research/metrics_ref.py` (reference computation), `tests/fixtures/audit/golden/cards.json`. The first-release implementation must reproduce the golden file exactly on the step A fixture. Results are returned by `query_metrics` and explained by `explain_metric` ([query.md](query.md)).

## 1. Common rules

- **Units.** An assignment is a message id of kind `task`. A root task has no `parent`; a child assignment has one. Roots and children are always counted apart. Task id strings are attributes, never join keys.
- **Window.** Half-open `[since, until)`; `cutoff = min(until, now)` is shown on every card. Each card names the event time that places a record in the window.
- **Ratios** are `[numerator, denominator]`; a zero denominator gives `null` plus a reason.
- **Every card carries** coverage (`[records with the needed field, eligible]`), source quality (emitter events, backfill, transcript, session counter) and the join confidence split (`verified`, `mapped`, `unassigned`).
- **Dimensions** (when recorded): professional, revision, model, scope, direct (sent by the user) vs delegated (`parent` set). `task_class` is not recorded in v1 events; grouping by it returns `unclassified` until an additive tag exists (events.md, rule on additive changes).
- **Pairs.** No card is shown without its partner: pass rate with coverage, durations with n, tokens with the unavailable share. Activity volume is never a quality or ranking signal. Within-class comparison is the only comparison offered.
- **Statuses are independent.** Delivery, deadline and evaluation statuses are separate fields.

## 2. The five first-release cards

### 2.1 Assigned work (`assigned_work`)

Numerator: distinct assignment ids created in the window. Output: `assignments`, `root_tasks`, `child_assignments`. Event time: `created`. Excluded: replies, notes, sends refused before a message exists (`msg_refused`, shown as a separate refusal count by reason). Decision: which professionals receive real work. Golden: 2 = 1 root + 1 child.

### 2.2 Evaluation coverage and pass rate (`evaluation`)

- **Eligible**: completed tasks, delivery `answered`, late answers included. Roots and children on separate rows. Not completed (failed, cancelled, in flight) are outside the denominator and shown as a count.
- **Effective verdict** per target: the latest non-superseded evaluation; an operator verdict outranks an automated check on the same target at any time; every evaluation stays visible. A verdict's `effective_at` must be at or before the cutoff.
- **Conclusive**: `passed`, `partial`, `failed`. Not conclusive: `inconclusive`, `unreviewed` (no evaluation).
- **Coverage** = conclusive / completed eligible. **Pass rate** = passed / conclusive. The counts of passed, partial, failed, inconclusive and unreviewed always sit beside both ratios.
- **Rubric consistency** (D4 validation): `passed` when every required criterion passed; `failed` when every required criterion failed; `partial` otherwise; `inconclusive` when any required criterion has no outcome (`not_assessed`). A stored status that contradicts its criteria is rejected at write time.
- Golden: root coverage [1,1], pass rate [0,1], partial 1 (e3 supersedes e2, C1 failed); child coverage [1,1], pass rate [1,1] (automated e1).

### 2.3 Deadline miss rate (`deadline_miss`)

Eligible: assignments whose `deadline <= cutoff`, cancellations excluded and counted. Missed: no answer at or before `deadline`; a late answer is a miss and is also counted as `late_answers_among_missed`. Rate = missed / eligible, roots and children apart; `not_yet_due` is shown. Computed from `deadline` and the answer time, never from the runtime `overdue` flag (set only when some pump runs; evidence only). Event time: `deadline`. Golden: all [1,2], root [1,1] with 1 late answer, child [0,1].

### 2.4 Queue and work duration (`durations`)

- Queue time = first observed delivery (first `msg_injected`, or backfilled `injected_at`) minus `created`.
- Reply latency = answer time minus first delivery.
- Active time = sum of known model/tool spans. **Not available in v1** (no span events): shown as `n=0` with coverage, never inferred from latency. Revisit when D1 or a later release adds span events.
- p50 and p95 by nearest rank, with `n` and `coverage [n, eligible]` beside each. Wall times from records; monotonic durations only where one process measured both ends (`wait_end.dur_ms`).
- Golden: queue p50 0.38 s, p95 0.39 s (n 2/2); reply latency p50 0.09 s, p95 40.69 s (n 2/2); active n 0/2.

### 2.5 Accounted tokens (`accounted_tokens`)

Sum of unique exchanges in the window, by category (input, output, cache write, cache read; never one blended number without the split). Split: linked task usage (own per assignment, inclusive per root), session-only usage, and unavailable usage (session counter minus transcript sum, null when no counter). Event time: exchange timestamp. Surfaces compare totals only at the same scope (assignment own, root inclusive, session). Cost is derived from the pinned dated table in USD API-equivalent; a model missing from the table is `unpriced` (tokens counted, cost null), never a guessed rate. Cache write cost must use the `usage.cache_creation` 5m/1h split (B2: a 5m-only price understates real 1h writes by 37.5 %). Golden: 250 unique = 220 linked (root own 140 + child 80, inclusive 220) + 30 session-only; unavailable null.

## 3. Pairing and counting rules (usage)

1. **Dedup before any sum**: key `(message.id, requestId)`, first wins, earliest file first, so an original beats a fork or resume copy. A repeated key with different usage is counted once (first) and reported as a `conflict`. Rows missing either id are counted with a `no_key` flag and a coverage note.
2. **Own vs inclusive**: own = exchanges attributed to the assignment; inclusive = own of the root plus every descendant found by walking `parent`. Residual is shown at the scope where it can be measured: session-only per session, and unavailable per session from the session counter.
3. **Attribution** follows the join-confidence policy in [events.md](events.md) section 6; overlap exchanges are unassigned.
4. **Sampling and censoring**: windows cut a task in two; a task counts in the window of its event time for each card, and usage rows count in the window of their own timestamp. Cards may therefore differ at window edges; `explain_metric` shows included ids.

## 4. Evaluation records

Fields: target (task or assignment message id), evaluator kind and name and version, rubric and rubric version, criterion outcomes (`passed`, `failed`, `not_assessed`), evidence refs (opaque `kind:token`), `effective_at`, `supersedes` (a link, never an overwrite). First-release evidence kinds: operator note, acceptance-test or check result, missing requirement, human rework, documented acceptance. Later (not v1): task-class rubrics, local trace grading, benchmark datasets, model graders as separate evaluators that never send content off the machine without an explicit operator choice.

## 5. Later lenses (not frozen)

Repeat use and cohort retention, cost per verified success, first-pass acceptance, rework, funnel (assigned, delivered, answered, evaluated, passed; always with coverage), latency control charts within one task class. Each needs data v1 does not collect (eligibility events, spans, task class) and stays a follow-up.

## 6. Note on the B3 reference

`research/metrics_ref.py` picks the effective verdict without checking `effective_at <= cutoff`, and its golden window ends before the operator verdict (effective 120 s, window end 100 s). The contract requires the cutoff check, so the shared query fixture uses a window ending at 200 s; the card numbers are identical. D3 must implement the cutoff rule and the reference should be fixed to match when it is next touched.
