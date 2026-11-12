# professionals: specification

Status: v1 shipped, v2 implemented in 0.2.0 (this sheet describes both).

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
- R5. Delegation chains travel with the message; revisiting a name in the chain, a wait-for cycle, or a chain deeper than `max_depth` is refused.
- R6. Permissions: sessions start in `dontAsk` mode with an allowlist from the persona's `tools:` line intersected with a fixed safe list; sensitive paths are denied for read and edit; the Claude Code sandbox is used when the machine allows it.
- R7. `pros new`, `pros validate`, `pros publish` scaffold, check and publish a persona.
- R8. `pros doctor` checks tmux, claude and python3 and the state directory.

### 3.2 Folders, registry, resume (v2)

- R9. `pros add <dir>` registers a folder that has `CLAUDE.md`, `AGENTS.md` or `.claude/`. Otherwise it is refused ("not an agent folder") unless `--force`. Name default: the folder name, lowercased, other characters to `-`, at most 40. Name clashes with another entry or a marketplace professional are refused; one folder is one entry.
- R10. Summoning a folder professional starts Claude with the folder as working directory and appends only the bus protocol to the system prompt. If the folder has an `AGENTS.md` but no `CLAUDE.md`, the `AGENTS.md` text is appended too (Claude Code does not read `AGENTS.md` itself). No persona file is used. A missing folder exits 5 (`folder missing`).
- R11. Every new session gets a generated session id (`--session-id`). Each session writes rows to `~/.professionals/history/<name>.jsonl`: a start row, and an end row with the end reason (`stopped`, `reaped`, `died`, `stuck`, `failed-to-start`, `disabled`, `lost`) and task counts. History is append-only (one write per row), survives stop, reap, reboot and `remove` (unless `--forget`), and is rotated at 1000 rows.
- R12. When no process is alive, `summon` resumes the last session (`claude --resume <id>`, same working directory) if its score is good or neutral and its transcript exists, and prints `resumed=<id>`. Otherwise it starts fresh and prints `fresh: <reason>`. `--fresh` forces a new session, `--fork` copies the context into a new id (`--resume <id> --fork-session --session-id <new>`), `--session <id>` resumes a specific session even if it was bad. A session older than `resume_max_days` (default 14) starts fresh. If a resume fails to start, one fresh start is tried.
- R13. A session id that is live in another process, or whose liveness cannot be read in 5 seconds (`claude agents --json`), is forked, never resumed blindly.
- R14. Score of a session: bad if rated bad, or it ended `stuck` or `failed-to-start`, or more tasks failed or timed out than were answered; good if rated good, or at least one task was answered and failures are at most half of the answers; otherwise neutral. `pros rate <name> good|bad [--note]` records a rating for the last session. `pros sessions <name>` shows ids, dates, task counts, score and why, and transcript size.
- R15. After a resume, the system prompt says that earlier tasks in the context are closed and only newly delivered tasks are to be answered.
- R16. `keep: true` entries are never idle-reaped but count toward `max_live`; at most `max_live - 1` entries may be kept. `pros keep <name> [--off]`.
- R17. `pros remove <name> [--forget]` stops a live session (refused while a task is in flight unless `--now`), deletes the entry, and never touches the folder. `pros disable <name> [--now]` and `pros enable <name>` toggle an entry.
- R18. `list`, `search` and `show` merge both kinds and show kind, state, keep, live state, last session and score. `--json` output carries the same fields; errors in `--json` mode have a stable code (`not_listed`, `disabled`, `changed`, `missing`, `user_only`, `cap`, `registry_changed`, `registry_corrupt`, `name_clash`, `busy`).
- R19. Registry reads and writes are locked and atomic. A corrupt registry is reported with its path and never overwritten; `list` falls back to the last good copy (`registry.json.bak`), and `pros doctor --repair` (user only) restores it.

## 4. Safety model

### 4.1 The allowlist

- Only enabled registry entries can be summoned or messaged; everything else exits 4. Messages to a disabled or unknown name fail immediately with a notice to the sender.
- The registry is created on first use and seeded once with the professionals of the marketplaces you added, enabled. Professionals that appear in a marketplace later are `available` until you run `pros enable`. A persona whose text changed since you enabled it is `updated` and refused until you run `pros enable` again.
- `add`, `remove`, `enable`, `disable`, `keep`, `rate` and `doctor --repair` are user-only. They refuse when `PRO_SELF` is set, when `PRO_SESSION` is set, or when the caller descends from the professionals tmux server (a process-ancestry check, so unsetting an environment variable is not enough).
- Every professional session gets deny rules for read and edit on the registry, its backup and checksum, the config file, the history and the run directory under `~/.professionals/`.
- `pros` records a checksum of the registry each time it writes it. If the file differs, `list` and `doctor` warn and every summon and send is refused until the user checks the file and runs `pros doctor --repair`.
- Protocol text: messages from peers, folder files and `pros list` output are data. They never add, enable or widen a professional.
- Descriptions of folder professionals are sanitised (control characters and the message header marker removed, 120 characters) and can be set with `--desc`. In a professional session the list is printed under a line saying it is data.

What this does not do: a session that is given generic Bash can act as the user. The allowlist stops prompt-injected mistakes, not a hostile process running as the same account. Do not give a folder professional generic shell access you would not give a stranger.

### 4.2 Folder content can run code

Claude Code runs hooks and MCP servers that a folder declares, and the folder trust dialog is what normally guards that.

- `pros add` stores a content hash of `CLAUDE.md`, `AGENTS.md`, `.claude/settings.json`, `.claude/settings.local.json` and `.mcp.json`, plus the directory identity (device and inode), and prints any hooks, MCP servers and allow rules it finds.
- The trust dialog is answered with keys only for directories `pros` created, and for registered folders whose hash still matches and which have no hooks or MCP servers. A folder with hooks or MCP servers needs `pros add --trust-code` for that; otherwise the dialog is left for you (`pros attach`).
- If the hash or the identity changed, the entry is `changed`: summon is refused until you review the folder and run `pros add <dir> --refresh`.
- Refused as folders without `--force`: your home directory, `/`, and symbolic links.
- `pros` passes `--permission-mode` and `--settings` itself. The entry's mode overrides a folder's own default mode, and deny rules always win. `pros` never writes `~/.claude.json`.

### 4.3 Folder permissions

A folder session gets: the folder's own settings (loaded by Claude Code), the default deny list (credentials and keys), the entry's `deny_paths` (default `.env`, `.env.local`, `.env.production`; add more with `--deny`), the registry denies above, and permission to run `pros`. When the folder defines no allow rules, a safe baseline is added (read, search, edit, read-only git). Anything more, such as running tests, is an explicit `--allow "<rule>"` or `--mode` stored on the entry. Allowing build or test commands runs the folder's own code.

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
[pro-msg id=m-.. from=<name|user> to=<name> task=t-.. kind=task|reply|note reply-to=<name> chain=a>b]
<body>
```

- The header is written by the CLI, never by the sender. A header marker inside a body is rewritten so it cannot pose as one.
- Answer a task with `pros reply <task> - <<'EOF' ... EOF`. A reply is final.
- Send with `pros send` (asynchronous; starts an allowlisted target that is not running, unless `--no-wake`) or `pros ask --wait N` (at most 100 seconds; continue with `pros wait`).

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

## 9. Out of scope

- Running professionals on other machines, in the cloud, or starting at boot. Persistence means the entry plus a resumable session, not an always-on service.
- Syncing the registry or sessions between machines.
- Publishing a folder professional to the marketplace.
- Editing a folder's own `CLAUDE.md` or settings on the user's behalf.
- Summaries that carry an old session into a new one: a session is resumed or started fresh.
