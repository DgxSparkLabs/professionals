# Changelog

## Unreleased

## 0.8.8

- The 0.8.7 deadline rule also covers a professional still in its turn when it shows activity in the last 2 minutes (a
  hook, or a write to its transcript: tool calls and results): the deadline moves, one note, same `deadline_extend_max_s`
  cap. A turn quiet for longer still gets the timeout note (`target state: working`); one quiet for `stuck_min` is stuck.
  A task sent with `--fallback` is never extended: its fallback still runs in parallel at the deadline.

## 0.8.7

- A task timed out with "target state: idle" while its professional had ended its turn and waited on a test run in a
  background shell (pane: "1 shell still running"). Such a professional is not idle: at the deadline pros moves the
  deadline by `deliver_deadline_s` (up to `deadline_extend_max_s`, default 7200 s, past the first one) and tells the
  sender once; after the cap the timeout note says `target state: waiting`. One still in its turn keeps the timeout note.
- `pros pick`: when the only professionals with verdicts on the kind are busy, it names them (`busy: NAME (working; ...)`,
  JSON `busy`) and picks none, instead of an arbitrary free one.

## 0.8.6

- Verdicts are used (card 1563, learn and grow). `pros pick --for "<kind of job>"` returns the best free professional for
  that kind from its verdicts, with a one-line reason: recent good before neutral before bad (each verdict halved per
  7 days), ties to the description that fits the job best, then to the fewest busy minutes in 24 h; `--json` gives the ranking. Two bad verdicts on a kind since the last
  good one there keep a professional from being picked for that kind until a good one. `pros rate --kind K` names the kind;
  older verdicts match by their reason, task id and task title.
- `pros team` prints a `good at / weak at` line per professional from its verdict reasons (`--json`: `good_at`, `weak_at`).

## 0.8.5.1

- A professional whose record pros dropped while its pane still lived (the wrong "gone" fixed in 0.8.5) could not be
  reached: `pros send` said "not running; starting it", then "duplicate session". Summon (and so send and its wake) now
  adopts a live pane with no record: it takes back the dropped record (pros keeps a copy under `run/dropped/`) or
  rebuilds it from the program's own argv and its history row, writes an `adopt` history row (the run shows open again),
  and delivers there. A second session is never started; a live pane pros cannot rebuild fails with how to attach or close it.

## 0.8.5

- A summon right after a professional's program exited started nothing: the exited pane stays open for 20 s
  (`[pro: exited N]`) and tmux refused the new session ("duplicate session"). pros now closes an exited pane before it
  starts the new run; a live session is still reused. The new run keeps the model and thinking it was summoned with
  (checked live on two voice pros: `--model claude-opus-5-5 --effort high` after the restart, transcript model the same).
- A live professional was taken for exited when its screen showed the text `[pro: exited` (a grep of pros, a test it
  wrote): summon dropped its record as "died" and tried to start it again. Only the shell's own last line
  `[pro: exited N]` counts now.
- A restart resent tasks answered long before. One reply closed only one message of a task, so add-ons sent under the same
  task id stayed open. A reply now closes every delivered message of its task, and a restart never resends a task
  message older than the professional's last reply to that task; only the task in flight is resent.

## 0.8.4

No professional on a model nobody chose (card 1586: professionals were seen on Fable).

- Model precedence ends in a built-in default `opus` (after `--model`, the entry, the persona and config `model`). Before, with none of them, pros passed no `--model` and the session ran on Claude Code's own default, which the `fable` block never saw.
- The pump reads the model each Claude session really runs on, from its transcript: `pros team` shows it (`--json` `seen_model`; in brackets in the table when it differs, with `(blocked)`). A blocked model the user did not store on that professional sends one note to the user and the summoner; the session is not stopped.
- Tests: `tests/test_v084.py`.

## 0.8.3

Worker verdicts are hard to forget (audit-24h: the lead's log of what each worker is good at stayed empty).

- `pros rate <name> good|neutral|bad "why" [--task T]`: neutral and a positional why are new; the verdict names its task (default: that professional's last DONE task for you). `--note` still works.
- While a task you sent has a DONE reply and no verdict, `pros inbox` and `pros read` of the reply print `rate: pros rate NAME good|neutral|bad "why" --task T`. Professionals are not nudged; they use `pros evaluate`.
- `pros evaluate` also logs its verdict there (passed=good, partial=neutral, failed=bad), so it counts as a rating; it does not change session scoring.
- `pros team` shows each professional's last verdict (`verdict` column, `--json` field `verdict`).
- Tests: `tests/test_v083.py`.

## 0.8.2

Unblock restarts keep the run, and automatic unblock never widens past a script.

- An unblock restart (automatic or `pros unblock`) keeps the model, thinking and harness the live session was summoned with. Before, it passed no model, so the entry, persona or config model won (seen live: a `--model claude-sonnet-5-5` professional came back on opus).
- `auto_unblock` names the script for an interpreter call (`Bash(python3 tools/check.py *)`, not `Bash(python3 *)`) and never adds a too-wide rule by itself: bare `Bash` (a call that starts with `$(...)` or a path-less word) or a runner with no script (`python3 -c ...`, `bash`). Those denials get the usual note with the `pros unblock --allow` line, and the user decides. Rules already on an entry are not touched.
- `pros team --json`: a joined (ad hoc) session's `waiting` row keeps its `task`, as 0.8.1 did for professionals (found by the Pi/Grok review trial).
- Every other start pros makes by itself keeps the asked model and thinking too: a wake on send after the session died or was reaped, a re-run, `pros summon` of a dead one (history rows carry `asked_model`/`asked_thinking`). `pros stop`/`disable` resets to the defaults; an explicit `--model` wins.
- Allow ceiling: a folder added with `--allow` rules keeps them as its ceiling; auto-unblock adds nothing there (the note says so) until the user runs `pros unblock`. A module run gets `Bash(python3 -m mod *)`. Rules pros adds are also listed in the entry's `auto_allow`.
- Re-add: `pros add <dir> --allow ...` sets the allow list to the rules given (before: merged into the old list, so a wide auto rule survived until remove + add); `--allow` or `--refresh` drops the rules auto-unblock added, unless `--keep-auto-rules`. The output lists the rules and what was dropped. `tests/test_v060.py` `test_refresh_updates_allow_list` now expects the set list instead of the merged one (it asserted the old merge).
- A re-add of a changed folder without `--refresh` printed `updated` (or `nothing changed`) and left it `changed`; it now says `NAME is still changed ... pros add DIR --refresh`.
- Fix: a denial seen while an earlier auto-unblock restart still held its lock was dropped (no rule, no note); the pump now retries it on a later pass.
- Guard mark: `pros team` and `pros list` show `guard` (`pros`, `paths`, `none`, `unknown`); a Pi professional on a `cursor/` model (harness field `unguarded_models`) is `guard: none`, because Cursor's agent runs its tools where the pros guard never sees them. `pros add`/`enable` warn. No other behaviour change.
- Docs: the harness trial table (`docs/harness-trials.md`, card 1589).
- Tests: `tests/test_v082.py`.

## 0.8.1

Delivery cap without the pump wait (spec 064 T113) and no reply noise from a waiting professional.

- `max_blocks`: when the cap ends a chain of Stop-hook deliveries and messages wait, the Stop hook starts `pros deliver-idle <session>` (internal), which pastes the next one as soon as the session is idle at its prompt. Before, it waited for the next pump pass (up to `pump_interval_s`, 5 s): every ~10th back-to-back voice turn paid that. The "hit the delivery cap" note is gone; the capped turn's final message now still goes out as the auto-reply.
- A professional whose turn ends while a command it started still runs (`waiting`) sends its final message as `kind=status`: in the inbox, never pasted or woken, not returned by `pros ask`/`pros wait`; the task stays open and the next turn end or `pros reply` answers it.
- Hook dedupe (the same hook fired twice) adds the transcript size to its key: two consecutive turns with an identical payload were taken for twins and the second Stop hook was dropped.
- `pros team --json` (card 1601): a `waiting` row keeps its `task` id (before: only `busy` rows had one), and working/waiting rows add `title`, the first line of that task's message, cut at 120 characters.
- Spec 4.8. Tests: `tests/test_v081.py`. The stub `claude` no longer fires UserPromptSubmit on a Stop-hook continuation (Claude Code does not); that reset the block count, so `test_block_cap_then_pump_finishes` never reached the cap before.

## 0.8.0

Ad hoc professionals and three live states (task adhoc-1126, card 1606).

- `pros join <name>`: any plain Claude Code session (for example the lead) joins the bus under a name. It sends as that name; professionals message it with `pros send|ask <name>`; replies, tasks and summoner notes reach it by themselves. Delivery: Stop hook when busy; while idle the new `asyncRewake` Stop hook `pros hook listen` (wakes the model with the message, exit 2), or a paste into its tmux pane (pane checked to still hold the joined process). `pros leave` (or the session's end) moves unread mail to the user's inbox. Not on the list: never summoned or started.
- `pros team` / `pros list`: `working`, `waiting` (turn over, a command it started still runs: checked in the process tree, not guessed) and `ready`. JSON rows add `activity` and `bg`. A waiting professional is not idle-reaped and not replaced by `--fresh`.
- Config `adhoc_listen_s` (default 3300). hooks.json gains the listener. Spec 4.7, R6a.
- Tests: `tests/test_v080.py`; the stub `claude` exports `CLAUDE_CODE_SESSION_ID`/`CLAUDE_PID`, runs typed `RUN <pros args>`, starts `BG <n>` background shells and emulates asyncRewake (`FAKE_REWAKE=1`).

## 0.7.1

A second name for the same folder (spec 026, 064 T200).

- `pros add <folder already registered> --name b` now registers `b` as a second entry for that folder, with its own `pro_id`, history, live session `pro-b` and session ids; the two names never share a session. Before, it said "already registered" and ignored `--name`.
- `pros add <dir>` with no `--name`, or with the name the folder already has, behaves as before: nothing changes, or the given flags update that one entry.
- Test: `tests/test_v071.py`. Updated: `tests/test_registry.py` `test_same_path_twice_changes_nothing` asserted the old ignore-`--name` behaviour; it now checks the repeat add with no name and with the same name.

## 0.7.0

pros is harness-agnostic (task harness-1126): a professional can run on Claude Code or on the Pi agent with any model Pi has.

- Harness registry: built-ins `claude` (unchanged behaviour) and `pi` (`~/.local/node/bin/pi --mode rpc`, `~/.local/node/bin` put in front of the session's PATH). Config `harnesses` overrides or adds; new `pros harness list [--json]` and, user only, `pros harness add|remove`.
- Per professional: `pros add <dir> --harness H --model M --thinking T` and `pros enable <name> --harness/--thinking` store `harness`, `model`, `thinking` on the entry; `pros summon --harness/--thinking` for one run. On claude `--thinking` is `--effort`.
- Pi messaging path is an RPC bridge (`pros bridge`, in the tmux pane): busy/idle from `agent_start`/`agent_settled`, final text as the auto-reply, queued messages delivered as RPC prompts, dialogs cancelled. send/ask/reply/inbox/task ids work across claude and pi both ways.
- Pi sessions under `PRO_HOME/pi-sessions`, resumed by id (`--session-id`), forked with `--fork`, only within the harness that made them.
- Models: the no-Fable block (`blocked_models`) applies to claude only; on pi the model comes from the entry or summon, else Pi's own config. Persona and config models are not passed to pi.
- Deny rules on pi: a per-session Pi extension blocks Pi tool calls that name a protected path, counted in DENIED with the usual note. Known limit: Pi's cursor provider runs tools inside Cursor's own agent, out of reach of that extension; Cursor's own permission config applies.
- Caller evaluations: new `pros evaluate <task> passed|partial|failed|inconclusive` (the task's caller or the user) records an audit evaluation; new `pros evaluations [name]` (anyone) shows verdicts per professional with harness, model and thinking. The session protocol tells every caller to read them before choosing and to evaluate every answer.
- `pros team` has HARNESS and MODEL columns; `pros show` prints the harness line.
- New `skills/professionals/worker-brief.md` (worker brief template, with Grok-medium real-work and Luna-max grunt-work examples); SKILL.md, README, spec 4.6 updated. Tests: `tests/test_v070.py` with a stand-in Pi, `tests/fake_pi.py`.

## 0.6.1

Automatic unblock (task unblock-auto-1126): a denied call no longer waits for someone to run `pros unblock`.

- The pump applies the unblock itself for a new denied call that no deny rule caused: it adds the suggested rule to the entry, restarts the professional with its history (open task resent once), tells it to retry, and sends the summoner one line. It runs in a process of its own (`pros auto-unblock`, hidden), since the pump can run inside the session being restarted.
- Deny-rule hits (credentials, `DEFAULT_DENY`, `deny_paths`, pros state) stay blocked and are reported as before. A rule already on the entry is not applied again (no restart loop): the usual note goes out.
- Switch: config `auto_unblock` (default `true`); per professional `pros add <dir> --auto-unblock on|off` and `pros enable <name> --auto-unblock on|off` (user only); `pros show` prints it.
- Audit: `registry_change` gains change `unblocked` (actor `pump` for automatic, `user` for `pros unblock`); `docs/spec/audit/schemas/event-v1.json` updated.
- Tests: `tests/test_v061.py`. Two 0.6.0 tests of the manual path now set `auto_unblock: false`, since the default changed.

## 0.6.0

Professionals run in `bypassPermissions` by default (task bypass-1126): professionals were stuck on denied `wc -l`, heredocs and a Write in their own home.

- Default mode `bypassPermissions` for every professional, marketplace or folder (`DEFAULTS.default_mode`). A stricter mode only when the user sets it: `pros summon --mode`, the entry's mode (`pros add <dir> --mode`, new `pros enable <name> --mode`; `--mode ''` clears it), or `default_mode` in config. In bypass the persona `tools:` narrowing to the safe list is skipped (Claude Code ignores allow rules there); it stays the path for stricter modes. As root, where Claude Code refuses bypass, an unchosen default falls back to `dontAsk` with a note.
- Deny rules are unchanged in every mode: credentials (`DEFAULT_DENY`), config and entry `deny_paths`, pros state files, sandbox `denyRead`.
- No first-run dialog: bypass settings files set `skipDangerousModePermissionPrompt: true` (settings reference: honoured in user, local, managed settings "and files passed with `--settings`").
- Unblock system: the pump reads each live professional's transcript and sends one note per new denied call (tool + exact command or path) to the summoner, and to the user when the summoner is a professional, with the rule that blocked it and the fix. New `pros unblock <name> [--allow RULE]` (user only) adds the rule to the entry's allow list, restarts the live session with its history (settings rewritten, open task resent once, marked possible duplicate) and tells the professional to retry. Rules or denials touching a protected path are refused with exit 4 (`protected`); `pros add --allow` refuses them too.
- Models: `pros add --model` and `pros enable --model` store a model on the entry (only `summon --model` existed, per run). New config `blocked_models` (default `["fable"]`): a matching model is refused (exit 4, `model_blocked`) unless the user stored exactly that model on the professional's entry.
- Marketplace entries' `allow` list is now used when building their settings (it was ignored).
- Fix: `pros add <dir> --refresh --allow R` (and `--deny`, `--mode`) did not update a registered folder's entry. They now do, with or without `--refresh`; only `--refresh` re-reads the files' hash, so a changed folder still needs review.
- `pros team --help` explains the DENIED column; `pros team` prints the unblock hint when a denial is counted. `pros show` prints the mode.
- Spec 4.5 "Permission model", R6, README, design notes and the core SKILL.md updated. Tests: `tests/test_v060.py`; `tests/fixtures/audit/schema.json` knows the new state keys. Tests that asserted the old `dontAsk` default now pin `default_mode: dontAsk` to keep covering the strict path.

## 0.5.4

Docs and version only; no code change. Task 311: one live session per professional.

- Decision: not adding `max_sessions` (N sessions per name). The registry, queue, `pros wait`, audit trace and the `max_live` cap are all keyed by one session per name, so N sessions is far more than a small change and needs its own security review (see `docs/design.md`). Default behaviour is unchanged.
- Documented the supported ways to batch: for parallel work name several professionals (register the same folder under other names) and send to each; for a serial batch where each item needs a clean context, `pros send|ask --fresh` per item (an idle session is replaced; a busy one is refused with exit 5, so wait for the answer, then send the next). README section "Batches and parallel work".
- Test: `tests/test_v054.py` keeps that README section and the design note in place.

## 0.5.3

Docs and version only; no code change.

- Three generic monitoring roles added to the marketplace (commit 82ffe8d): `access-researcher` (official, permitted ways to read a platform, with limits, terms and fallbacks), `source-scout` (finds and vets public sources for a topic, with trial questions), `editorial-investigator` (traces a claim or trend to its origin and gives a verdict with sources).
- Upgrade note: message pruning (0.5.2) is on by default with a 30-day retention (`msg_retention_days`; 0 keeps everything). See "Upgrading from 0.3.x" in the README.

## 0.5.2

Fixes from the delegation pilot.

- `pros send|ask --fresh`: the task runs on a new session. An idle session with nothing queued is replaced; a busy one is refused with exit 5 and left alone. Fixes coverage dropping as one session takes more repos (the fresh-per-task rule is now one flag).
- Message store pruning: `pros prune [--days N]` (user only) and the pump (at most hourly) delete every message of a task that finished (all answered or failed) more than `msg_retention_days` ago, default 30; 0 keeps everything. Open and unread messages are never touched; audit events keep their own retention.
- `pros sessions` counted 0 tasks for a live session (counts were written only when it ended); it now counts answered, failed and timed-out tasks live, and counts a task that was queued before the session started (wake on send) when it was delivered in that session.
- `pros doctor --link` refusals print their message and exit 2 (the arguments were swapped: it printed `pros: 2` and exited 1).
- The imports-dialog start failure for a project-scope summon names `--scope global` as the way out.
- Auto-reply (used when the professional's `pros reply` is refused) now appends the text of every file the session wrote with the Write tool in that turn (read from its transcript, capped at 24 KB), so a spec written to a file is no longer lost behind a one-line summary.
- Persona `exhaustive-researcher` gains Edit (it had Write only and could not fix its own slips). A session is told which tools it was granted and to say so when one is missing. `reliability-reviewer` stays read-only on purpose.
- Tests: `tests/test_v052.py`; `tests/fake_claude.py` gains `WRITE` and records user turns in its transcript.
- Checked and already fixed by 0.4.0/0.5.x: `pros wait` on a queued task blocks until the answer (test only; no code change).
- Not changed: one live session per professional name (design note in `docs/design.md`); Grep/Glob missing in some fresh sessions is a Claude Code side effect that `pros` cannot see from outside (the allow rules always contain both).

## 0.5.1

- Config switches `wake_on_send` and `failover` (both default true; set false in `$PRO_HOME/config.json`). `wake_on_send: false` makes `pros send` and `pros ask` refuse (exit 5) when the target is not running, as before 0.4.0. `failover: false` turns the 0.5.0 re-run off (same as `max_attempts: 1`).
- A failover re-run now tells the new session: "previous attempt failed: check what is already done before repeating side effects".
- README and this file: "Upgrading from 0.3.x".

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
