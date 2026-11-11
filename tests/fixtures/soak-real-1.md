# Soak run 1 (real Claude Code, model sonnet)

Command: `python3 tests/soak_real.py` (private tmux socket, temp PRO_HOME, PRO_MARKETPLACE=repo, claude 2.1.322).
Two professionals (reliability-reviewer, docs-designer, global scope). 50 tasks "Reply with exactly: ACK <n>" sent
alternately with `pros send` (no --wait, 0.4 s apart, so most arrived while the target was busy), answers collected with `pros wait`.
At n=25 the sender kept feeding reliability-reviewer until it held 1 injected + 7 queued tasks, then its claude process
was killed (SIGKILL) and it was re-summoned 4 s later (resend path).

| Result | Value |
|---|---|
| Distinct n answered exactly once | 50 / 50 |
| Duplicates answered | 0 |
| Task messages total (50 + 1 extra load task) | 51 |
| Resends flagged dup=true after restart | 1 |
| Delivery via stop-hook / paste | 45 / 6 |
| Auto-replies | 0 (professionals replied with `pros reply`) |
| Waits past deadline without notice | 0 |
| Latency, send to reply (queue wait included) p50 / p95 / max | 19.4 s / 37.6 s / 40.6 s |
| Wall time | 74 s |

Failures: none. No bug found in pros; no code changed. A first run (53 messages, kill with only 3 tasks in flight, no dup
resend triggered) also passed with p50 17.7 s, p95 35.7 s; the driver was then changed to wait until the victim really held
injected plus queued work before the kill.
