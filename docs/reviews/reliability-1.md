# Reliability review 1 (by the reliability-reviewer professional, dogfood round 1)

RANKED OPEN FAILURES (read pros 1-1992, hooks.json, SKILL.md; nothing run)

1. P0 reply lost on a busy lock. bin/pros cmd_reply (l.1433-1439) marks the original task answered and saves it BEFORE post(). post() (l.710) takes lock(s,20); on timeout it raises Fail(5) before save_msg(m). Result: task shows answered, reply never stored, asker waits to its deadline, the work is lost.
 Trigger: asker is mid paste_deliver (holds the lock up to ack_timeout 15s) or pump holds it, while a reply lands.
 Test: hold run/locks/<asker-session>.lock with flock for 25s, run pros reply, assert a reply msg file exists. Fix: save_msg(m) first, mark orig answered after post.

2. P1 swallowed paste leaves a draft that blocks delivery forever. paste_deliver (l.754-782) / deliver_next (l.729): if Enter is lost twice, the text sits in the input box. classify_pane then says draft, and deliver_next needs prompt-empty, so nothing is delivered. Nothing clears the box (no C-u). The message is retried to 3 attempts or deadline, the pro looks idle, and the sender gets only a timeout note.
 Test: fake claude that ignores Enter; send a task; assert it is delivered or failed with a clear reason within ack_timeout*3, and the input is cleared.

3. P1 no daemon: delivery and deadlines depend on someone running a pros command. pump() only runs from auto_pump (l.936) at the start of a command and inside wait_for. hook_stop (l.1056) on lock busy sets idle and returns without claiming. A message queued for an idle pro whose Stop hook already fired stays queued, and an overdue timeout is never noticed, while nobody runs pros (user away, all pros idle).
 Test: make Stop hook hit a held lock, queue a msg, run no command for 60s, assert delivery. Fix: Stop-hook retry, or a tiny pump loop in the tmux server.

4. P1 message marked injected but never seen. hook_stop (l.1047-1053): queue_claim, mark_injected, unlink, and only then print the block JSON. Hook timeout is 10s (hooks.json). If it dies between claim and print (kill, stdout closed, 10s timeout under load), state=injected and nothing was shown. resend_unanswered only runs on a re-summon (l.1374). Dead: only the asker deadline notice remains. Also the dedupe (l.1007-1011) reads then writes state non-atomically: two concurrent duplicate Stop hooks can both claim, two messages marked injected, one block output lost.
 Test: SIGKILL the hook after claim; assert the message returns to queued. Concurrent duplicate hooks: assert exactly one output per message.

5. P1 runaway spawn cap is not global. summon() (l.1270) locks per session name; the max_live count (l.1281) and the summons.log budget read (l.1284-1288, append l.1333) are unlocked. Two concurrent summons of different names both see live<6 and both start, so max_live is exceeded; budget can be passed the same way. Also summon chain depth is not limited (max_depth guards only send chains), so a pro that may_summon can fan out to the cap and keep respawning after reaps within 20/hour.
 Test: launch 8 summons in parallel with max_live=3; assert live<=3. Add a global lock around count+start.

6. P1 permission dead-end on replies. Allow rules are Bash(pros *) and Bash(<realpath> *) (l.1218) under default_mode dontAsk. Replies with newlines, backticks,  or ; may fail the rule match and be denied silently. The PROTOCOL tells pros to use @file for long text, but personas without Write (this reviewer has none) cannot create the file. The pro cannot answer, then the sender waits to its deadline. Pane dialogs only count when the mode is not dontAsk. In an explicit --mode with dialogs, notify goes to summoner=user inbox (l.913), which is shown only on the user's next prompt (plain_hook), so it can sit unseen. Not verified without running claude.
 Test: reply text with markdown backticks, newlines and a quote under dontAsk; assert the reply is accepted. Add stdin form (pros reply <task> -) to the allowlist.

7. P2 reaper and stop leave injected tasks silent. stop_session (l.945-956) fails only queued messages; injected-but-unanswered tasks of a stopped or idle-reaped pro give no notice, so the asker waits for its deadline (600s default, equal to stuck_min). reply to a stopped asker is only stored, not shown. waits/<me>.json is one file per pro, so two concurrent asks overwrite each other and the first to finish deletes the other's wait-for edge (deadlock detection gap, l.818-824).
 Test: reap or stop a pro with an injected task; assert the asker gets a note within one pump.

CHECKED: all waits (lock, wait_for, wait_ready, paste_deliver, ask cap 100s: all have deadlines), queue claim rename, retry caps (3 attempts, task_msg_cap, max_blocks), loop and cycle refusals, deadline propagation.
NOT CHECKED: tests/ directory, real claude behavior (hook timeouts, rule matching), clock changes (uses time.time for deadlines; a backward step would extend them), security (suggest a security specialist for the trust-dialog key sending and publish). No delegation was needed.
