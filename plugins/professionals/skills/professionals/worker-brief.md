# Worker brief template

Use this when you send a task to a worker professional, for example a cheap model on the Pi harness. A worker follows steps; it does not decide. You check the result before you use it, then record a verdict with `pros evaluate <task> passed|partial|failed`.

Two example kinds (examples, not fixed roles; the user registers them with `pros add <dir> --harness pi --model ...`):

- **Real work** (for example `cursor/cursor-grok-4.6-medium`, thinking medium): a bounded change or a review with a clear goal. Give the goal, the files and the check.
- **Grunt work** (for example `cursor/gpt-5.6-luna-max`): mechanical steps written out in full. Give every step and the exact output format, and check the result yourself.

Copy, fill in, and send with `pros send <worker> @brief.md` or `pros ask <worker> @brief.md`:

```
GOAL: <one sentence: what done looks like>
CONTEXT: <folder, files, versions; only what the steps need>
STEPS:
1. <exact action, e.g. "open src/a.py and list every function that calls open()">
2. <exact action>
3. <exact action>
DO NOT: <what is out of bounds: files not to touch, no commits, no network, no credentials>
OUTPUT: <exact format, e.g. "one line per hit: path:line name", or "a unified diff only">
CHECK: <how the caller will verify it, e.g. "I rerun the tests" or "I grep for the same thing">
IF STUCK: say what blocked you and stop; do not guess.
```

Keep a brief short. If a step needs judgement, it is not grunt work: give it to a real-work professional or do it yourself.
