# professionals: specification

Status: v1 shipped, v2 implemented in 0.2.0 (this sheet describes both); failover (R4b) in 0.5.0.

## 1. Idea

A professional is a Claude Code session that is good at one thing. Any session can start one (`pros summon`), message it (`pros ask`, `pros send`), and every professional can hand work it cannot do to a better one. All sessions live on one private tmux server, so they outlive the session that started them.

There are two kinds of professional:

| Kind | What it is | Where it runs | Where its instructions come from |
|---|---|---|---|
| `marketplace` | A persona published as a plugin (`pro-<name>`) | `<project>/.professionals/<name>/` (project scope) or `~/.professionals/home/<name>/` (global scope) | The persona file, appended to the system prompt |
| `folder` | A directory you already work in: it has a `CLAUDE.md`, an `AGENTS.md` or a `.claude/` folder | The folder itself | The folder's own files, loaded by Claude Code as usual |

"Summon the professional that lives in this folder" means: start, or resume, a Claude session there, with that folder's own instructions, settings and tools, and connect it to the team.

## 2. User stories

1. **Ask a specialist.** I run `pros ask reliability-reviewer "..."` from my own session and get a tagged answer back.
2. **Delegate.** A professional that meets a task outside its scope searches for a better one, summons it and passes the work on. Loops and chains that are too deep are refused.
3. **Never hang.** Every request has a deadline. A stuck, dead or limited professional is reported, not waited on.
4. **Grow the team.** No fit? A professional creates, validates and publishes a new one from a template.
5. **See the team.** `pros team` shows who is live, what they are doing and who started them.
6. **Own folders.** I run `pros add ~/code/my-repo` once. After that any session can `pros summon my-repo` and `pros ask my-repo "..."`, and the answer follows that repository's own instructions.
7. **Resume.** Yesterday's reviewer learned my code base. Today `pros summon` resumes its session with its context. If that session went badly it starts fresh and says why.
8. **I choose who may run.** Only professionals on my list can be summoned or messaged. A professional cannot change the list.
9. **Manageable and persistent.** The list shows kind, state, keep-running flag and the last session. Stopping or idle-reaping ends the process, not the professional. One professional can be marked "keep running".
10. **Works out of the box.** After the one-paste install the marketplace professionals are already on the list, and adding a folder is one command.

## 3. Requirements

### 3.1 Core (v1)

- R1. `pros summon <name>` starts a professional in its own tmux session, or reuses the live one. One lock per session name covers decide, record and start.
- R2. Caps: live professionals (`max_live`, default 6), chain depth (`max_depth`, 4), summons per hour (20), idle reap (30 minutes). Configurable in `~/.professionals/config.json`.
- R3. Messages are files. Delivery to a busy professional is queued and delivered at the end of its turn (Stop hook) or pasted when it is idle. Bodies over 2 KB arrive as a pointer (`pros read <id>`). A message is acted on once; a restart resends each unanswered task once, marked as a possible duplicate, counted in `resends`; after `max_resends` (default 1) restarts, or once its deadline passed, the task fails and its sender gets one note, so a crash loop never redelivers it without end.
- R4. Every task has a deadline; an overdue task produces a timeout notice to its sender. `pros wait` (and `ask`) returns the newest real reply to the latest ask of that task id, and the timeout notice only when no real reply exists, so a late reply is never hidden by the notice and a reused task id never returns an earlier ask's answer. A wait ends with exit 5 (`stuck: <reason>`) when its target is gone, stuck (busy with no activity for `stuck_min`), at a usage limit, or in a dialog for 20 s.
- R4a. `pros send` to an allowlisted professional that is not running starts it, as `ask` does (wake on message). The caps are checked first and refuse with exit 4 before anything is stored; otherwise the message is stored and queued before the start, so a start that fails (exit 5) keeps it queued for the next start or until its deadline. `--no-wake` keeps the old refusal (exit 5, nothing stored).
- R4b. Failover. When the professional holding a task dies, or the task passes its deadline while its target is gone, stuck, in a dialog or at a usage limit, the task is re-run: on the fallback the sender named (`pros send|ask --fallback <name>`), else on a fresh session (`summon --fresh`) of the same professional, whose broken session is stopped first. With a fallback, a deadline miss re-runs even when the target is only busy; without one, a busy target keeps the R4 timeout note and late reply (a re-run would discard work in progress). At most `max_attempts` runs per task (default 2, including the first; 1 turns failover off and keeps the 0.4.0 behaviour); a queued task without a fallback is not re-run. The re-run is a new message (same task id and sender, `attempt` n, `retry_of` the failed message, `fo_via` the professional that failed; its header ends `via=<name> attempt=<n>`; with a fallback the chain ends in the fallback). It is stored and queued before the failed attempt is closed, and started by a separate `pros summon` in a clean environment, so the allowlist and the caps apply (a refused or failed start keeps it queued, as R4a). The sender gets one note per failed attempt (a progress note, which does not end a wait) and, when no attempt is left, one final note. The first reply wins: later replies from other attempts are stored, not delivered. The fallback must be allowlisted (exit 4), not the target or the sender (exit 2), and not already in the chain (exit 4); a professional with `may_summon: false` cannot name one (exit 4) and its tasks are not re-run. Audit: the re-run's `msg_created` carries `attempt`, `retry_of`, `via_pro`; the failed attempt's `msg_failed` has reason `recipient_died` or `deadline_missed`; `pros audit trace` shows the re-run in the first attempt's tree with `failover: {attempt, of, via}`.
- R5. Delegation chains travel with the message; revisiting a name in the chain, a wait-for cycle, or a chain deeper than `max_depth` is refused.
- R6. Permissions (0.6.0, see 4.5): sessions start in `bypassPermissions` mode unless the user chose a stricter mode for that professional or in `default_mode`; the protected paths (credentials, `deny_paths`, pros state) are denied for read and edit in every mode; a denied call reaches the summoner with its fix (`pros unblock`), and with `auto_unblock` (0.6.1, default on) pros applies that fix itself unless a deny rule caused the denial; the Claude Code sandbox is used when the machine allows it.
- R6a. Any plain Claude Code session can join the bus under a name (`pros join`, 4.7) and is woken by messages while idle; `team` and `list` tell `working`, `waiting` (on a command it started) and `ready` apart.
- R7. `pros new`, `pros validate`, `pros publish` scaffold, check and publish a persona.
- R8. `pros doctor` checks tmux, claude and python3 and the state directory.

### 3.2 Folders, registry, resume (v2)

- R9. `pros add <dir>` registers a folder that has `CLAUDE.md`, `AGENTS.md` or `.claude/`. Otherwise it is refused ("not an agent folder") unless `--force`. Name default: the folder name, lowercased, other characters to `-`, at most 40. Name clashes with another entry or a marketplace professional are refused. `pros add <dir>` again with no `--name`, or with the name it already has, changes that entry only (flags) or nothing; a new `--name` registers a second name for the same folder (0.7.1): a separate entry with its own `pro_id`, history, live session (`pro-<name>`) and session ids, so the two names never share a session.
- R10. Summoning a folder professional starts Claude with the folder as working directory and appends only the bus protocol to the system prompt. If the folder has an `AGENTS.md` but no `CLAUDE.md`, the `AGENTS.md` text is appended too (Claude Code does not read `AGENTS.md` itself). No persona file is used. A missing folder exits 5 (`folder missing`).
- R11. Every new session gets a generated session id (`--session-id`). Each session writes rows to `~/.professionals/history/<name>.jsonl`: a start row, and an end row with the end reason (`stopped`, `reaped`, `died`, `stuck`, `failed-to-start`, `disabled`, `lost`) and task counts. History is append-only (one write per row), survives stop, reap, reboot and `remove` (unless `--forget`), and is rotated at 1000 rows.
- R12. When no process is alive, `summon` resumes the last session (`claude --resume <id>`, same working directory) if its score is good or neutral and its transcript exists, and prints `resumed=<id>`. Otherwise it starts fresh and prints `fresh: <reason>`. `--fresh` forces a new session, `--fork` copies the context into a new id (`--resume <id> --fork-session --session-id <new>`), `--session <id>` resumes a specific session even if it was bad. A session older than `resume_max_days` (default 14) starts fresh. If a resume fails to start, one fresh start is tried.
- R13. A session id that is live in another process, or whose liveness cannot be read in 5 seconds (`claude agents --json`), is forked, never resumed blindly.
- R14. Score of a session: bad if rated bad, or it ended `stuck` or `failed-to-start`, or more tasks failed or timed out than were answered; good if rated good, or at least one task was answered and failures are at most half of the answers; otherwise neutral. `pros rate <name> good|neutral|bad ["why"] [--task T] [--note]` records a verdict (0.8.3: neutral, why and task; the task defaults to that professional's last DONE task for you); good and bad also rate the last session. 0.8.3, so the verdict log is not forgotten: while a task the user (or a joined session) sent has a DONE reply and no verdict, `pros inbox` and `pros read` of that reply print one line `rate: pros rate NAME good|neutral|bad "why" --task T` (at most 5 in the inbox); a professional is not nudged (it uses `pros evaluate`). `pros evaluate` also writes the verdict there (passed=good, partial=neutral, failed=bad; it does not score the session). `pros team` shows each professional's last verdict (`--json` field `verdict`: rating, note, task, t). `pros sessions <name>` shows ids, dates, task counts, score and why, and transcript size.
- R15. After a resume, the system prompt says that earlier tasks in the context are closed and only newly delivered tasks are to be answered.
- R16. `keep: true` entries are never idle-reaped but count toward `max_live`; at most `max_live - 1` entries may be kept. `pros keep <name> [--off]`.
- R17. `pros remove <name> [--forget]` stops a live session (refused while a task is in flight unless `--now`), deletes the entry, and never touches the folder. `pros disable <name> [--now]` and `pros enable <name>` toggle an entry.
- R18. `list`, `search` and `show` merge both kinds and show kind, state, keep, live state, last session and score. `--json` output carries the same fields; errors in `--json` mode have a stable code (`not_listed`, `disabled`, `changed`, `missing`, `user_only`, `cap`, `registry_changed`, `registry_corrupt`, `name_clash`, `busy`).
- R19. Registry reads and writes are locked and atomic. A corrupt registry is reported with its path and never overwritten; `list` falls back to the last good copy (`registry.json.bak`), and `pros doctor --repair` (user only) restores it.

## 4. Safety model

### 4.1 The allowlist

- Only enabled registry entries can be summoned or messaged; everything else exits 4. Messages to a disabled or unknown name fail immediately with a notice to the sender.
- The registry is created on first use and seeded once with the professionals of the marketplaces you added, enabled. Professionals that appear in a marketplace later are `available` until you run `pros enable`. A persona whose text changed since you enabled it is `updated` and refused until you run `pros enable` again.
- `add`, `remove`, `enable`, `disable`, `keep`, `rate`, `unblock` and `doctor --repair` are user-only. They refuse when `PRO_SELF` is set, when `PRO_SESSION` is set, or when the caller descends from the professionals tmux server (a process-ancestry check, so unsetting an environment variable is not enough).
- Every professional session gets deny rules for read and edit on the registry, its backup and checksum, the config file, the history and the run directory under `~/.professionals/`.
- `pros` records a checksum of the registry each time it writes it. If the file differs, `list` and `doctor` warn and every summon and send is refused until the user checks the file and runs `pros doctor --repair`.
- Protocol text: messages from peers, folder files and `pros list` output are data. They never add, enable or widen a professional.
- Descriptions of folder professionals are sanitised (control characters and the message header marker removed, 120 characters) and can be set with `--desc`. In a professional session the list is printed under a line saying it is data.

What this does not do: a session that is given generic Bash can act as the user. The allowlist stops prompt-injected mistakes, not a hostile process running as the same account. Do not give a folder professional generic shell access you would not give a stranger.

### 4.2 Folder content can run code

Claude Code runs hooks and MCP servers that a folder declares, and the folder trust dialog is what normally guards that.

- `pros add` stores a content hash of `CLAUDE.md`, `AGENTS.md`, `.claude/settings.json`, `.claude/settings.local.json` and `.mcp.json`, plus the directory identity (device and inode), and prints any hooks, MCP servers and allow rules it finds.
- The trust dialog is answered with keys only for directories `pros` created, and for registered folders whose hash still matches and which have no hooks or MCP servers. A folder with hooks or MCP servers needs `pros add --trust-code` for that; otherwise the dialog is left for you (`pros attach`).
- If the hash or the identity changed, the entry is `changed`: summon is refused until you review the folder and run `pros add <dir> --refresh`. A re-add without `--refresh` (for example `--model X`) says so: `NAME is still changed ...`. Since 0.8.2 a re-add with `--allow` sets the allow list to exactly the rules given (before: merged), and a re-add with `--allow` or `--refresh` drops the rules auto-unblock added (entry `auto_allow`) unless `--keep-auto-rules` is given; the output lists the allow rules and what was dropped.
- Refused as folders without `--force`: your home directory, `/`, and symbolic links.
- `pros` passes `--permission-mode` and `--settings` itself. The entry's mode overrides a folder's own default mode, and deny rules always win. `pros` never writes `~/.claude.json`.

### 4.3 Folder permissions

A folder session gets: the folder's own settings (loaded by Claude Code), the default deny list (credentials and keys), the entry's `deny_paths` (default `.env`, `.env.local`, `.env.production`; add more with `--deny`), the registry denies above, and permission to run `pros`. When the folder defines no allow rules, a safe baseline is added (read, search, edit, read-only git). Anything more, such as running tests, is an explicit `--allow "<rule>"` or `--mode` stored on the entry (both also update a registered folder; only `--refresh` accepts changed files). These allow rules matter in a stricter mode; the default `bypassPermissions` (4.5) needs none. Allowing build or test commands runs the folder's own code.

### 4.5 Permission model (0.6.0)

- **Default: `bypassPermissions`.** Every professional, marketplace or folder, starts with `--permission-mode bypassPermissions`, so Bash (`wc -l`, heredocs, pipes), Write and Edit in its own home work without a prompt and without a denial. `DEFAULTS.default_mode` is `bypassPermissions`.
- **The user decides otherwise, per professional.** Precedence: `pros summon --mode M` for one run, then the entry's `mode` (`pros add <dir> --mode M`, `pros add <dir> --refresh --mode M`, `pros enable <name> --mode M`; `--mode ''` clears it), then `default_mode` in `~/.professionals/config.json`, then `bypassPermissions`. In a stricter mode (`dontAsk`, `acceptEdits`, `default`, `plan`) the allowlist applies as before: persona `tools:` narrowed to the safe list (unless `--mode` or `--allow-persona-tools`), the folder baseline, the entry's `allow` rules. In bypass the narrowing is skipped: Claude Code ignores allow rules there ("Allow rules have no effect in `bypassPermissions`", permission-modes docs).
- **Deny rules never move.** Every settings file, in every mode, carries the Read/Edit deny rules for `DEFAULT_DENY` (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gh`, `~/.netrc`, `~/.claude/.credentials.json`), config `deny_paths`, the folder entry's `deny_paths` and the pros state files (registry, config, history, run, audit). Claude Code: "Deny rules block in every mode, including `bypassPermissions`." The sandbox's `denyRead` list is unchanged.
- **No first-run dialog.** A bypass settings file sets `"skipDangerousModePermissionPrompt": true`. Settings reference: "Skip the confirmation dialog Claude Code shows before a session enters `bypassPermissions` mode ... User, local, or managed, and files passed with `--settings`." `pros` passes its own file with `--settings`.
- **Root.** Claude Code refuses bypass as root or under sudo outside a recognized sandbox. When the user chose no mode, `pros` falls back to `dontAsk` there and says so; a mode the user chose is passed as is.
- **Unblock.** The pump reads each live professional's transcript. Each new denied call (deduplicated by tool and exact command or path) sends one note to the summoner, and to the user when the summoner is a professional, naming the professional, the tool, the exact command or path, the rule that blocked it and the one command that fixes it: `pros unblock <name> --allow '<rule>'`. `pros unblock <name> [--allow RULE]` (user only; default rule: the one the last denied call needed) adds the rule to the entry's `allow` list, restarts the live session with its history (resume, or fork when the id is live elsewhere; the open task is resent once, marked possible duplicate), which rewrites its settings file, and tells the professional to retry. A rule or a denied call that touches a protected path is refused with exit 4 (`protected`): no allow rule and no unblock lifts a deny rule. `pros team` counts denials per tool (DENIED).
- **Automatic unblock (0.6.1).** With `auto_unblock` on (config, default `true`; per professional `pros add <dir> --auto-unblock on|off` or `pros enable <name> --auto-unblock on|off`, user only, stored as the entry's `auto_unblock`), the pump does the unblock itself for a new denied call that no deny rule caused: it starts `pros auto-unblock <session>` as a process of its own (the pump may run inside the session it restarts), which adds the narrowest rule (`Bash(<first word> *)`, or the tool name) to the entry's `allow` list, records an audit `registry_change` with change `unblocked` (actor `pump`; a manual `pros unblock` records the same with actor `user`), sends the summoner one line (who, the call, the rule, how to turn it off), and restarts the session with its history as `pros unblock` does, telling the professional to retry. Not lifted, only reported as before: a call that touches a protected path, a call whose rule is already on the entry (something else denies it; no restart loop), and any professional with `auto_unblock` off. Only the first new rule per pump pass is applied; the restart rewrites the settings, and a later denial is handled on a later pass. The pump only acts on denials it reads in the transcript; `pros auto-unblock` does nothing without one pending. Since 0.8.2 the rule for an interpreter or runner (`python3`, `node`, `bash`, `env`, `xargs` and the like) names its script (`Bash(python3 tools/check.py *)`), and pros never adds by itself a rule wider than that: bare `Bash`, or a runner with no script (`Bash(python3 *)`); such a denial is only reported, with the `--allow` line for the user. Every unblock restart (automatic or `pros unblock`) keeps the model, thinking and harness of the run it replaces, not the entry or config defaults; so does every start pros makes by itself without `--model`/`--thinking` (a wake on send after the session died or was reaped, a re-run, `pros summon` of a dead one): it takes what the summoner asked for on the last run of that professional (history row `asked_model`, `asked_thinking`; same folder and harness), unless the user ended that run (`pros stop`, `pros disable`), which resets to the defaults. An explicit `--model` or `--thinking` always wins, a model blocked since falls back to the defaults, and a fork starts from the defaults. A folder added with `--allow` rules (entry `allow_ceiling`) keeps them as its ceiling: auto-unblock adds nothing to it, and the note says so; only the user widens it (`pros unblock`). Rules pros added itself are listed in the entry's `auto_allow` for review. A pending auto-unblock whose process found the restart lock busy is retried on a later pump pass.
- **Models.** Model precedence: `summon --model`, the entry's `model` (`pros add <dir> --model M`, `pros enable <name> --model M`, user only), the persona's `model:`, config `model`. A model whose name contains an item of `blocked_models` (default `["fable"]`) is refused with exit 4 (`model_blocked`), unless it is exactly the model the user stored on that professional's entry: the one per-professional exception.
- **Known limit.** Ask rules and Claude Code's own critical-path `rm`/`rmdir` checks still prompt in bypass; such a prompt shows as `dialog` and the summoner gets the usual "needs attention" note.

### 4.6 Harnesses (0.7.0)

- **Registry.** A harness is the program a professional runs on. Built in: `claude` (proto `claude`, Claude Code, everything above) and `pi` (proto `pi-rpc`, bin `~/.local/node/bin/pi`, args `--mode rpc`; `~/.local/node/bin` goes on the session's PATH in front of the user's PATH, because Pi's cursor provider runs `~/.local/bin/agent` and Node). Config `harnesses` overrides fields of a built-in or adds a harness; `pros harness list [--json]` shows them with `found`; `pros harness add NAME --proto P --bin B [--arg A] [--path-add DIR] [--mode-args MODE=ARGS] [--*-flag F]` and `pros harness remove NAME` are user only, and a harness still named by an entry is not removed (exit 4).
- **Per professional.** The entry stores `harness`, `model` and `thinking` (`pros add <dir> --harness pi --model cursor/cursor-grok-4.6-medium --thinking medium`, `pros enable <name> --harness ..`, user only). Precedence: `summon --harness`, the entry, config `default_harness`, `claude`. The entry's model counts only for its own harness. On `claude`, `--thinking` becomes `--effort` and `blocked_models` applies (4.5). On `pi` there is no model block; without a model on the summon or the entry no `--model` is passed and Pi's own config picks it; a persona's `model:` and config `model` are claude names and are not passed.
- **Messaging path: RPC bridge, not polling.** The tmux pane of a pi session runs `pros bridge <session>`, which owns `pi --mode rpc` (JSONL on stdin/stdout, split on LF only). The bridge is that harness's Stop hook: it marks the session busy at `agent_start` and idle at `agent_settled`, turns the final assistant text into the auto-reply (same rule as claude: exactly one open task, nothing delegated), and delivers the next queued message as an RPC `prompt` (claimed under the session lock, `via=rpc`). send, ask, reply, inbox and task ids are unchanged, so claude and pi professionals message each other both ways. Dialog requests (`extension_ui_request` select/confirm/input/editor) are answered `cancelled`, never yes. Text typed in `pros attach` is sent as a prompt or follow-up.
- **Sessions.** Pi sessions live in `PRO_HOME/pi-sessions` (`--session-dir`), id given by `--session-id`; resume reopens the id, fork uses `--fork`. A session is resumed only by the harness that made it; `--session` of another harness exits 2.
- **Deny rules on Pi.** Each pi session loads a pros extension (`-e run/bridge/<s>.guard.mjs`) that blocks any Pi tool call whose input names a protected path (4.5); a block counts in `pi_denials` (DENIED in `pros team`) and sends the usual note. **Known limit:** a model behind Pi's cursor provider runs its tools inside Cursor's own `agent`, so Pi sees no tool calls and the guard does not apply; Cursor's own permission config governs them, which pros does not change. Do not give such a worker access to private data. Since 0.8.2 pros says so: a harness field `unguarded_models` (built-in `pi`: `["cursor/"]`) names the model prefixes whose tools run outside the guard, and each row of `pros team` and `pros list` (`--json` field `guard`) shows who checks the tool calls: `pros` (Claude Code with the pros settings), `paths` (Pi's own tools, the protected-path guard only), `none` (a model matching `unguarded_models`; the text tables add a line `! NAME: guard: none (...)`) or `unknown` (no model set, so Pi's default provider decides). `pros add` and `pros enable` warn on stderr for `none` and `unknown`. Nothing else changes: such a professional still starts.
- **Plan mode on Pi** passes `--tools read,grep,find,ls`; other modes run Pi's default tools.
- **Caller evaluations.** Every caller records a verdict for each task it sent: `pros evaluate <task> passed|partial|failed|inconclusive [--criterion id=outcome]` (only the task's caller or the user; exit 4 otherwise, exit 3 for an unknown task). It is an audit evaluation (docs/spec/audit) with evaluator = the caller's name, kind `automated` for a professional and `operator` for the user, ad-hoc rubric. `pros evaluations [name] [--last N] [--json]` (anyone) groups them by professional with its current harness, model and thinking; callers read it before choosing whom to ask. These two commands are the only audit path open to professionals; the rest of `pros audit` stays user only. The harness and model shown are the entry's current ones, not a per-run record.

### 4.7 Ad hoc professionals and live states (0.8.0)

- **Join.** `pros join <name> [--no-pane]`, run by a plain Claude Code session through its Bash tool, binds the name to that session: `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PID` (both set by Claude Code for its Bash tool), plus `TMUX`/`TMUX_PANE` when it runs in tmux, stored in `run/adhoc/<name>.json`. Refused in a professional (exit 4, it has its name), for a name on the list or in a marketplace (exit 2, `name_clash`), for a name another live session holds (exit 2), and outside a Claude Code session without `--session-id` and `--pid` (exit 2). One name per session: joining again under another name moves it. The name is not on the list: `summon` refuses it and nothing starts it; it lives while its Claude Code process lives.
- **Send and receive as that name.** Every pros command the session runs carries the name (`self` is matched by the session id), so its tasks go out `from=<name>` and their replies, and the notes of professionals it summoned, come back to `<name>`. Professionals reach it with `pros send|ask <name>`; it answers with `pros reply`. The joined session keeps the user's rights (`require_user` is unchanged: it is not a professional). No auto-reply: its final message speaks to the person, so it answers with `pros reply`.
- **Delivery that wakes it.** Messages to the name are queued under `adhoc-<name>` and delivered by the first route that applies: (1) busy (the plain hooks track the turn): the Stop hook returns the next message as a block reason, as for a professional; (2) idle with the listener armed: the plugin's second Stop hook, `pros hook listen`, is `asyncRewake` (Claude Code: "runs in background and wakes the model on exit code 2"); in a joined session it waits up to `adhoc_listen_s` (default 3300 s; hook timeout 3600) after the turn, claims the next message, writes it to stderr and exits 2, which starts a new turn with it; a new turn or a newer listener makes it stand down; everywhere else it exits 0 at once; (3) idle without the listener: paste into its tmux pane as for a professional, only after checking that the pane still holds the Claude Code process that joined. The UserPromptSubmit hook adds anything still queued to the next prompt. The pump retries route (3) on each pass.
- **Leave.** `pros leave [name]`, or the session's end (SessionEnd hook, or its process gone), removes the name; unread mail moves to the user's inbox, and later mail to that name (a note of a professional it summoned) goes there too.
- **Live states.** `pros team` and `pros list` show, besides the internal state: `working` (in a turn), `waiting` (the turn is over but a command the session started still runs) and `ready` (free for input). The check is real, not a guess from the transcript: a Bash tool command of Claude Code runs as `bash -c source <home>/.claude/shell-snapshots/...`; a live such shell in the session's process tree (under the tmux pane, or under `CLAUDE_PID` for a joined session) after its turn ended is a background command, and its pids are named in the reason. `--json` rows keep `state` and add `activity` and `bg`. A waiting professional is not idle-reaped and not replaced by `--fresh` (exit 5). Delivery is unchanged: a waiting session takes input, so its messages are still delivered.
- **Known limits.** The listener covers `adhoc_listen_s` after each turn; a session idle longer is woken only by the tmux paste (or the next prompt). Background subagents (not shell commands) are not counted as `waiting`. A session without the plugin's hooks and outside tmux gets its mail at `pros inbox` only.

### 4.8 Delivery cap hand-off and status messages (0.8.1)

- **Cap hand-off (064 T113).** `max_blocks` (default 10) still ends a chain of Stop-hook deliveries, so one turn never runs on without end. When it ends one while messages wait, the Stop hook starts `pros deliver-idle <session>` in a process of its own: it waits (up to `ack_timeout_s`) for the session to be idle at its prompt and pastes the next message, a fresh turn. Before, that message waited for the next pump pass (`pump_interval_s`, 5 s), so every ~10th back-to-back voice turn paid up to 5 s. The capped turn's final message still goes out as the auto-reply, and no "delivery cap" note is sent. Why this and not a per-entry cap: it fixes every entry with nothing to tune, and the cap keeps its job.
- **Status, not reply, while waiting.** A professional whose turn ends while a command it started still runs (`waiting`, 4.7) sends its final message as `kind=status`: kept in the recipient's inbox (`pros inbox`, `pros read`), never pasted or woken, never returned by `pros ask`/`pros wait`, and not counted as the reply. The task stays open; the next turn end (Claude Code starts one when the command ends) or `pros reply` answers it. Status messages have no `msg_created` audit event (event-v1 has no status kind).
- **Team rows say what they wait on.** In `pros team --json` a `working` or `waiting` row carries `task` (the task id) and `title` (the first line of that task's message, cut at 120 characters); a `ready` row has both empty.
- **Hook twins.** The dedupe of a hook fired twice (plugin installed and `--plugin-dir`) adds the transcript size to its key, so two consecutive turns with the same payload are never taken for twins.

### 4.4 Shared context

A resumed folder session keeps its context for every summoner. `pros show` says so; `--fresh` is the opt-out.

## 5. Entry states

| State | Meaning | summon | enable | disable | keep | remove |
|---|---|---|---|---|---|---|
| enabled | listed, content matches | yes | no-op | yes | yes | yes |
| disabled | turned off by the user | refused | yes | no-op | refused | yes |
| available | in a marketplace, never enabled | refused | yes | n/a | n/a | n/a |
| updated / changed | persona or folder content differs from the stored hash | refused | `enable` (persona) / `add --refresh` (folder) | yes | flag stays | yes |
| missing | path or plugin gone | refused (exit 5) | refused | yes | refused | yes |

Live, reaped and lost describe the process and are independent of the state above. A history start row with no end row and no live process is closed as `lost` at the next start.

## 6. Message protocol

```
[pro-msg id=m-.. from=<name|user> to=<name> task=t-.. kind=task|reply|note|status reply-to=<name> chain=a>b]
<body>
```

- The header is written by the CLI, never by the sender. A header marker inside a body is rewritten so it cannot pose as one.
- Answer a task with `pros reply <task> - <<'EOF' ... EOF`. A reply is final.
- Send with `pros send` (asynchronous; starts an allowlisted target that is not running, unless `--no-wake`) or `pros ask --wait N` (at most 100 seconds; continue with `pros wait`). Both take `--fallback <name>` (R4b).
- A re-run (R4b) adds ` via=<name> attempt=<n>` at the end of the header.

## 7. Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | not found or generic failure |
| 2 | bad arguments or refused input (bad name, not an agent folder, name clash) |
| 3 | still pending (`ask`, `wait`) |
| 4 | refused by policy (allowlist, user-only command, caps, loops, `changed`) |
| 5 | environment problem (missing folder, tmux, lock busy, did not become ready, corrupt registry) |

## 8. Test map

All tests run with a stand-in `claude` program on a private tmux socket and a temporary `PRO_HOME`; no network, no tokens. `tests/soak_real.py` runs against real Claude and is run by hand.

| Area | Test file |
|---|---|
| Summon, reuse, lock race, trust dialog, startup failure | `tests/test_summon.py` |
| Messaging, queueing, ack and retry, long bodies | `tests/test_messaging.py`, `tests/test_reliability1.py` |
| Resend counter, latest reply on wait, stuck target ends a wait, audit status before import, wake on message | `tests/test_v040.py` |
| Failover: re-run on a fresh session or a fallback, attempt cap, one note per attempt, first answer wins, caps, audit trace | `tests/test_v050.py` |
| Chains, loops, deadlock refusal | `tests/test_chain.py` |
| Caps and budgets | `tests/test_caps.py` |
| Pane classifier | `tests/test_classifier.py` |
| Persona validation, skill text | `tests/test_validate.py` |
| Registry: add, remove, enable, disable, keep, name rules, corruption, atomic writes | `tests/test_registry.py` |
| Allowlist: refusal paths, user-only commands, ancestry check, checksum, deny rules, `available`/`updated` | `tests/test_allowlist.py` |
| First-use creation and seeding, v1 sessions surviving | `tests/test_migrate.py` |
| Folder summon: working directory, prompt, permissions | `tests/test_folder_summon.py` |
| Folder trust: content hash, `--trust-code`, identity, mode precedence | `tests/test_folder_trust.py` |
| Resume, fresh, fork, scoring, history, reap and keep, reboot, parallel summons | `tests/test_resume.py` |
| Bypass default, deny rules kept, no first-run dialog, per-professional mode, denial notes, `pros unblock`, `add --refresh --allow` | `tests/test_v060.py` |
| Automatic unblock: rule, restart with history, one-line note, audit event, protected paths kept, per-pro and config switch, no restart loop | `tests/test_v061.py` |
| Second name for the same folder: separate entries, sessions and session ids | `tests/test_v071.py` |
| Ad hoc professionals (join, send and receive as the name, paste, listener, Stop hook, leave) and live states working/waiting/ready | `tests/test_v080.py` |
| Cap hand-off without the pump (T113), kind=status while waiting, hook twins by transcript size | `tests/test_v081.py` |
| Harness registry, per-professional harness/model/thinking, Pi RPC bridge (stand-in `tests/fake_pi.py`), cross-harness messaging, guard, dialogs refused, evaluations | `tests/test_v070.py` |

## 9. Out of scope

- Running professionals on other machines, in the cloud, or starting at boot. Persistence means the entry plus a resumable session, not an always-on service.
- Syncing the registry or sessions between machines.
- Publishing a folder professional to the marketplace.
- Editing a folder's own `CLAUDE.md` or settings on the user's behalf.
- Summaries that carry an old session into a new one: a session is resumed or started fresh.
