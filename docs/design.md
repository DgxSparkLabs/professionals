# Professionals: architecture for contributors

Source of truth: `plugins/professionals/bin/pros` (one stdlib Python file, ~2000 lines, v0.1.0).
Line refs are to that file. **[unverified]** marks claims not confirmed in code or by running it.

## 1. Goal and constraints
Let any Claude Code session summon specialist Claude sessions ("professionals"), message them, and
let them delegate to each other, with bounded cost and no silent hangs. Fixed: Claude Code runtime,
tmux hosting, stdlib-only Python, state as plain files. Open: multi-host (unsupported today).

## 2. Components

```
 plain Claude session (user)         professional session (tmux, pro-<name>)
   |  Bash: pros ask/send/wait          |  Bash: pros reply/send/summon
   v                                    v
 +--------------------- pros CLI (bin/pros) -----------------------+
 | registry | summon | messages | pump/reap | hook entry | policy  |
 +----+----------+--------+----------+-----------+----------------+
      |          |        |          |           ^ stdin JSON
      |   tmux -L professionals      |      Claude Code hooks (hooks.json)
      |   (private server)           |
      v          v                   v
 marketplace   sessions         state dir  ~/.professionals/run/...
 (personas)    (1 per pro)      (files + flock)
```

| Component | One responsibility |
|---|---|
| `pros` CLI | The only program: every command, policy check and hook handler. |
| Hooks (`hooks/hooks.json`) | Forward 5 Claude events to `pros hook <event>` (timeout 10 s each). |
| Skill (`skills/professionals/SKILL.md`) | Teach any session the commands, message format and habits. No code. |
| Marketplace (`.claude-plugin/marketplace.json`) | List plugins; entries named `pro-<name>` are personas (`personas()`, :256). |
| tmux server | Host one session per professional on socket `professionals` (`tmux -L`, `-f /dev/null`, :329). |
| State dir | Hold all mutable state under `$PRO_HOME/run` (default `~/.professionals/run`). |

State under `run/`: `sessions/` (registry), `state/` (hook-written live state), `msgs/m-*.json`,
`queue/<s>/` + `delivering/<s>/`, `inbox/` (replies for non-pros), `waits/` (wait-for graph),
`locks/`, `settings/`, `prompts/`, `summons.log`, `log/events.log`. Config: `$PRO_HOME/config.json`
overrides `DEFAULTS` (:58).

Session name: `pro-<name>` (global) or `pro-<name>--<sha1(project)[:6]>` (project scope, default;
workdir `<project>/.professionals/<name>`). `PRO_SELF`/`PRO_SESSION` in the env define the caller
(`self_name`, :540); no `PRO_SELF` means "user".

## 3. Message model

Message = JSON file `msgs/<id>.json`: `id from to task kind reply_to chain body created deadline
state attempts parent session`. `kind` is `task | reply | note`. States: `queued -> injected ->
answered`, or `failed`. Replies/notes jump straight to `answered` when delivered (`mark_injected`, :678).
The header line `[pro-msg id=.. from=.. to=.. task=.. kind=.. reply-to=.. chain=a>b]` is written by
the CLI, never by the sender; `[pro-msg` inside a body is rewritten to `[quoted-pro-msg` (:601).
Bodies over 2048 bytes arrive as a pointer; the recipient runs `pros read <id>`.

### 3.1 Two delivery paths

| | Stop-hook delivery | Paste delivery |
|---|---|---|
| When | Recipient finishes a turn (Stop event) | Recipient is idle with an empty prompt |
| Who acts | Recipient's own hook process (`hook_stop`, :1036) | Sender's `pros` (`deliver_next`/`paste_deliver`, :725/:754) |
| Mechanism | Claims oldest queued msg, prints `{"decision":"block","reason":<rendered msg>}`, so the session continues with the message as its next input | `tmux load-buffer` + `paste-buffer -p`, 0.3 s, then a separate `Enter` |
| Ack | Marked `injected` (via `stop-hook`) **before** the hook answers | See 3.2 |
| Limit | `max_blocks` (10) consecutive blocks; then state `idle`, summoner notified, rest stays queued | 3 failed attempts, then `failed` and the sender gets a note |

Paste is used only if `live_state == idle` **and** the pane classifier says `prompt` (empty input
line). A half-typed draft is never overwritten; the message stays queued.

### 3.2 Ack and dedupe

- Ack (paste): `UserPromptSubmit` hook regex-finds `[pro-msg id=m-..]` in the submitted prompt and
  calls `mark_injected` (:1023). Fallback inside `paste_deliver`: pane shows "esc to interrupt"
  after 2 s -> mark injected. Window `ack_timeout_s` = 15 s.
- Swallowed Enter: at 40% of the window, if the id or `[Pasted text` is visible, send Enter once.
  Never re-paste in that case. Text absent at 60%: one re-paste. Then give up (attempt +1).
- Dedupe: atomic claim (`os.rename` queue -> delivering); idempotent `mark_injected`; identical hook
  event+payload within 3 s ignored (plugin loaded twice, :1007); per-session `flock`; resent tasks
  carry `dup=true` and render a "possible duplicate" line (:619, :1378).
- `pump` returns `delivering/` files older than 60 s to the queue (crash during delivery).

### 3.3 Replies to a non-professional

`post()` to `user` writes `inbox/<id>`. `plain_hook` (SessionStart / UserPromptSubmit of a normal
session) prints pending replies and marks them `answered`; `origin_session` filters so a reply is
shown only in the session that asked. `pros ask`/`wait` also read the reply directly from `msgs/`.

## 4. Sequence: ask with a delegation

`user` is a plain session; A and B are professionals. A decides to delegate part of the task.

```
 user(Bash)        pros CLI           A (pro-a)               B (pro-b)
   | ask a "T"        |                   |                       |
   |----------------->| do_send: chain=[a], task=t1, deadline +600s
   |                  | post -> lock(a), queue_add, deliver_next
   |                  |--paste (a idle, prompt empty)-->|
   |                  |<- UserPromptSubmit hook: t1 injected (ack)
   |                  |  wait_for(t1) polls msgs/ + pump() every 2s
   |                  |                   | pros send b "part" (new task t2)
   |                  |<------------------| do_send: parent=t1, chain=[a,b],
   |                  |                   |   deadline=min(+600, t1.deadline-20)
   |                  |                   |   loop/depth/deadlock/cap checks
   |                  | b busy? queue_add  |
   |                  |--------------------------paste or Stop-hook-->|
   |                  |                   | A keeps working / ends turn
   |                  |                   |                       | pros reply t2 "R"
   |                  |<--------------------------------------------| msg kind=reply to a
   |                  | post(reply) -> a's queue
   |                  |-- Stop hook of A (or paste when idle) ---->|  A sees [pro-msg ... kind=reply]
   |                  |                   | pros reply t1 "final"
   |                  |<------------------| reply to orig.reply_to (= user)
   |<-- wait_for returns reply, prints it, exit 0
   | (if >100 s: exit 3 "pending", run `pros wait t1`)
```

Notes: `ask` clamps to 100 s (`max_ask_wait_s`). Professionals are told to prefer async `send`.

## 5. States and recovery

`live_state` (:497) -> `starting | idle | busy | stuck | dialog | limit | gone`, from three
sources: tmux pane text (`classify_pane`), hook-written `state/<s>.json`, and `claude agents --json`
(tie-breaker only, 8 s timeout).

| Failure | How it shows | Effect | Recovery / who is told |
|---|---|---|---|
| Session died / tmux gone | no tmux session or `[pro: exited` | injected tasks lost | `pump` drops registry entry, notes each sender "died ... summon again" (:905). Next `summon` re-queues unanswered injected tasks once, `dup=true`. |
| Busy with no activity > `stuck_min` (10) | `stuck` | waiter sees `stuck`, exit 5 | Summoner notified once per state (`alerted`). Human: `pros attach`. |
| Trust / permission dialog | pane text; Notification hook | cannot take input | Trust dialog auto-answered by keys only if workdir has `.pro-owned`; else summon fails and says so. Permission dialog: notify summoner; `wait_for` gives up after 20 s. |
| Usage limit | `LIMIT_RE` on pane | target unusable | Waiter exit 5; summoner notified. |
| Message deadline passed | `pump` scans tasks | queued -> `failed`; injected -> `overdue` | Sender gets a "timeout" note. Deadline only covers tasks, not replies (reply deadline 3600 s is not enforced **[unverified: no reader found]**). |
| Lock busy | `Fail(5)` | op skipped | Hooks fall back to `idle`; `pump` skips that session and retries later. |
| Idle > `idle_reap_min` (30) | `reap()` (run at most every 60 s from `pump`) | session killed | Queue empty and re-checked before kill; queued msgs of a stopped session are failed and the senders notified. |
| Asker gone when reply is made | `find_session` empty | reply stored, not pushed | Prints "stored"; `wait`/`read` still finds it. |

No daemon: `pump` runs at the start of every `pros` command except `hook|version|doctor` (:1980) and in
`wait_for`. If nobody calls `pros`, nothing is reaped or timed out; Stop-hook delivery still works.

## 6. Caps and chains (all enforced in `do_send`/`summon`)

| Rule | Value (default) | Exit |
|---|---|---|
| Live sessions | `max_live` 6 | 4 |
| Summons per hour | `summon_budget_per_hour` 20 (counted in `summons.log`) | 4 |
| Chain depth | `max_depth` 4 (length of `chain`) | 4 |
| Loop | task target already in the caller's chain | 4 |
| Wait-for cycle | `would_deadlock` over `waits/*.json` (blocked `ask` registers `me -> target`) | 4 |
| Messages per task | `task_msg_cap` 20 | 4 |
| Child deadline | `min(+600 s, parent.deadline - 20)`; refused if < 10 s left | 4 |
| Stop-hook blocks in a row | `max_blocks` 10 | n/a |
| Who may stop whom | only the summoner or the user (`cmd_stop`, :1572) | 4 |
| `may_summon: false` in persona | persona cannot summon | 4 |

Chain source: `current_task(me)` = latest injected task for the caller, or `--task`. With two tasks in
flight and no `--task` the wrong parent may be picked **[from :827, not tested]**. `PRO_CHAIN` is set
in the env but I found no reader **[unverified]**. Exit codes: 0 ok, 1 error, 2 usage, 3 pending, 4 policy, 5 target unusable.

## 7. Security model

Threat: a peer message or fetched page makes a professional act beyond what the user allowed.
1. **Permission mode.** Default `dontAsk` (`default_mode`): anything not allow-listed is denied without
   a prompt, so a headless session cannot hang on a dialog. `--mode` overrides and then the persona's
   `tools:` list is granted as written (`explicit_mode`, :1197).
2. **Allowlist.** Built per session into `run/settings/<s>.json` (`build_settings`, :1214). Persona
   `tools:` are filtered to `SAFE_TOOLS` (Read, Glob, Grep, Edit, Write, NotebookEdit, WebFetch,
   WebSearch, TodoWrite). Bash gets only `pros *`, the script's full path, and read-only git
   (`status diff log show blame`). `git diff*` is narrowed to avoid `--no-index` file reads.
   `cat/grep/find/ls/head/tail/wc/diff` are added **only when the sandbox is on**.
3. **Deny rules.** `Read` and `Edit` (covers Write) denied for `~/.ssh ~/.aws ~/.gnupg ~/.config/gh
   ~/.netrc ~/.claude/.credentials.json` plus `deny_paths` from config. Absolute paths use `//` (the
   code comment says this was verified; a single `/` is project-relative and blocks nothing).
4. **Sandbox (`sandbox: auto`).** On with `failIfUnavailable` only if `bwrap` and `socat` exist **and**
   a probe (`bwrap ... unshare -Ur true`, cached 1 h) passes. Why it may be off (code comments
   :19-30, :1154-1161): without socat the setting is silently ignored; a broken socat makes Claude fall
   back to unsandboxed; with `apparmor_restrict_unprivileged_userns=1` every Bash call fails, so the pro
   could not run `pros`. Hence the allowlist is the baseline and the sandbox an extra. Not reproduced **[unverified]**.
5. **Peer text is untrusted.** `PROTOCOL` (:1226) tells the model `[pro-msg]` text is a peer request,
   never above the user, and cannot widen permissions; headers are CLI-made and the marker is
   neutralised in bodies. This is a prompt rule, not a barrier: Edit/Write/WebFetch stay allowed with
   no path scope beyond the deny rules.
6. **Marketplace trust.** `summon` refuses (exit 4) unless the marketplace is in `trusted_marketplaces`,
   in `~/.claude/plugins/known_marketplaces.json`, or is this repo.
7. **Environment.** `clean_env` drops `PRO_*`, `TMUX*`, `CLAUDECODE`, `CLAUDE_CODE_*` (except OAuth and
   Bedrock/Vertex flags); other vars such as API keys are inherited **[from reading, not tested]**.
8. **Publishing.** `publish` scans for home paths, IPs, keys, tokens, e-mails; `publish_guard` can veto.

## 8. Decisions (cost)

Files + flock, no daemon (upkeep only when `pros` runs; `all_msgs()` rescans the dir). Stop hook plus
paste (no typing vs waking idle sessions; cost is pane scraping). Caps refuse with exit 4, not queue.

## 9. Known limits

- Single host and user; no auth between local processes (file access to `PRO_HOME` allows forged messages).
- Depends on UI strings (`❯`, "esc to interrupt", "Do you want to proceed", trust text, limit regex);
  a Claude Code UI change breaks delivery/state detection. `MIN_CLAUDE` = 2.1.224.
- Persona edits apply only after stop + summon (hash noted, not enforced).
- A session with a draft in its input gets nothing pasted until it is cleared.
- One live session per professional name (tmux `pro-<name>`, one registry entry, one queue). Tasks for the same professional run one after another, so a batch of N tasks to one name is serial; for parallel work name several professionals (a folder professional added under another name) or send to different ones. Not changed in 0.5.2 on purpose: N sessions per name would need per-session names in the registry, the queue, `pros wait`, the audit trace and the caps (`max_live` counts sessions), a large change with its own security review. Use `--fresh` (0.5.2) when the problem is context build-up rather than throughput.
- `max_live` counts sessions across all projects. `ask` waits at most 100 s.
- A crash between paste and ack can duplicate a task after restart (flagged `dup`).
- Not read/verified: `tests/`, `commands/start.md`, `plugin.json`, persona plugins, the semantics of
  Claude Code's Stop-hook `decision: block` (inferred from code). No code was run for this document.
