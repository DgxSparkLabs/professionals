# Harness trials: which model gets which job

Card 1589, 2026-11-26. Each professional got the same two small real jobs through `pros ask` (isolated `PRO_HOME`, a throwaway copy of the repo at f571f5d as its folder). The judge was the pros-dev professional (Claude Code, Opus 5.5), which answered both jobs itself first and compared. Time is from send to reply, including start-up.

Jobs:
- **review**: code review of commit f571f5d (`pros team --json`: task id on waiting rows, new `title` field, tests).
- **grunt**: list every pros CLI subcommand that no test under `tests/` calls directly.

| Model / harness | Job | Time | Result quality (against the judge's answer) | Good at | Should not get |
|---|---|---|---|---|---|
| `cursor/grok-4.7-medium` on Pi (Cursor login) | review | 607 s | Good. Found a real gap the judge missed: joined (ad hoc) rows still clear `task` on `waiting`, against the spec line. Two smaller points held up: `title` comes from the newest message for that task id; the ready check in the test is weak. One flake worry was overstated. No false bugs. | Careful code reading; review notes with file:line | Anything private or outside its folder. Its tools run in Cursor's own agent, so the pros deny guard does not apply, and it did read outside its folder (`git grep` in the dev worktree). No jobs where a fast answer matters. |
| `cursor/grok-4.7-medium` on Pi (Cursor login) | grunt | 292 s | Exact: 39 subcommands; no direct test for `attach`, `bridge` or `deliver-idle`; method stated. Same as the judge's answer. | Bounded counting and listing jobs | Same as above |
| `gpt-5.6-luna-max`, Codex plan login | both | not run | Not run: needs his Codex login on Pi. Pi has no openai-codex login here, and Luna through the Cursor login bills a different plan. | - | - |

Notes:
- Both Grok answers came back as auto-replies from the final message. Its own `pros reply` shell calls were refused by Cursor's command allowlist, and it retried several times, which adds to the time.
- Effort on Cursor models is part of the model name (`-medium`). Pi's `--thinking` does not apply to them.
