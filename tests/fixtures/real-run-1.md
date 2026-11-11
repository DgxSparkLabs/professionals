# Real run 1 (sanitized)

Real Claude Code 2.1.322, model sonnet, private tmux socket, temporary state directory. Paths are replaced by placeholders.

Findings from this run, all fixed in pro:
- A different program named pros (a distribution's client) came first on PATH and answered the reply command. Prompts now carry the full path of the command.
- A model that wrote P=<path>; $P send ... was denied, because permission rules match the literal command. The protocol now asks for one literal command per call.
- Deny rules written as Write(...) never match; Edit(...) covers Write.

## 1. Summon (global scope) and ask

```
$ pros summon reliability-reviewer --scope global
$ pros ask reliability-reviewer "List 3 failure cases of a tmux message bus in one line each" --wait 90
sent m-0b2f4c09d2 task=t-7279b8a7 status=queued
[pro-msg id=m-b72b1ec540 from=reliability-reviewer to=user task=t-7279b8a7 kind=reply reply-to=reliability-reviewer chain=reliability-reviewer]
1. Lost message: send-keys into a busy, copy-mode or dead pane drops or garbles the text, and the sender gets no ack (P0). 2. Dead hang: the sender waits for a reply from a crashed or restarted session with no deadline and blocks forever (P1). 3. Duplicate or reordered delivery: a restart or retry replays a message and concurrent senders interleave keystrokes, with no idempotency key to stop a non-repeatable action running twice (P1).
```

## 2. Busy queue: two sends back to back

```
$ pros send reliability-reviewer "Review this design in 5 short bullets: a retry loop around an HTTP call with no timeout."
$ pros send reliability-reviewer "Second question: name 2 ways a lock file can go stale, one line each."
sent m-967dafcd27 task=t-5e77535b to=reliability-reviewer status=delivered chain=reliability-reviewer
the answer arrives as a message; or: pros wait t-5e77535b
sent m-9cf05a59d1 task=t-4a0e03c9 to=reliability-reviewer status=queued chain=reliability-reviewer
the answer arrives as a message; or: pros wait t-4a0e03c9

Result from the message log: first message via=paste, second via=stop-hook (it arrived while the first was being worked on); both answered.
```

## 3. Delegation: reliability-reviewer hands the writing part to docs-designer

```
$ pros ask reliability-reviewer "Mixed task, two parts. Part A: ... Part B: write a two-sentence README introduction ... delegate it to the right professional, then combine both parts" --wait 100
sent m-c81b9ec151 task=t-dc52c2b8 status=queued
[pro-msg id=m-5f46ab190d from=reliability-reviewer to=user task=t-dc52c2b8 kind=reply reply-to=reliability-reviewer chain=reliability-reviewer]
PART A (worst failure of a file-based message queue): a crash between claiming a message and acknowledging it loses it or leaves it stuck, and the sender then hangs with no deadline. Steps: 1) consumer claims the message file (rename into its inbox, or read then delete); 2) the process is killed, or a non-atomic writer left a half-written file; 3) no lease, timeout or requeue exists, so the message is gone (at-most-once) or stuck forever in a stale claim, and the sender waits for a reply indefinitely. Effect P0: silent data loss plus an unrecoverable hang. One-line fix: write temp file + fsync + atomic rename, claim with a lease timestamp, requeue expired claims, ack by delete, dedupe by message id, and put a deadline on every wait. Test: kill -9 the consumer between claim and ack (and the writer mid-write); assert the message is redelivered with a single effect and the sender times out with a clear error.

PART B (written by docs-designer, delegated in the earlier task t-93490ccb, unchanged): pros is a command-line tool that summons AI specialists, each a separate Claude session with its own focus, such as design, docs or reliability review. Type pros summon <name> to start one, then send it a task and read its reply, and specialists can summon each other when a job needs more than one skill.

Checked: generic queue design only. Not checked: the real pros queue code (none supplied). Note: this task repeats t-e46bd771; I reused docs-designer's earlier answer instead of asking again.
```

## 4. Message log and team afterwards (chain shows reliability-reviewer>docs-designer; the dup=True rows are tasks resent after the restart)

```
user -> reliability-reviewer task answered via=paste dup=True chain=reliability-reviewer | List 3 failure cases of a tmux message bus in one line each
user -> reliability-reviewer task answered via=paste dup=True chain=reliability-reviewer | Mixed task, two parts. Part A: give the single worst failure case of a
user -> reliability-reviewer task answered via=stop-hook dup=False chain=reliability-reviewer | Mixed task, two parts. Part A: give the single worst failure case of a
reliability-reviewer -> docs-designer task answered via=paste dup=False chain=reliability-reviewer>docs-designer | Write a two-sentence README introduction for a command-line tool calle
docs-designer -> reliability-reviewer reply answered via=None dup=False chain=reliability-reviewer>docs-designer | pros is a command-line tool that summons AI specialists, each a separat
reliability-reviewer -> user reply queued via=None dup=False chain=reliability-reviewer | PART A (worst failure of a file-based message queue): a crash between 
reliability-reviewer -> user reply answered via=None dup=False chain=reliability-reviewer | PART A (worst failure of a file-based message queue): a crash between
```

