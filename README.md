# Professionals

**A team of AI specialists for Claude Code. Summon one, hand it work, and let it call in the others.**

<p align="center"><img src="docs/assets/how-it-works.svg" alt="You and your Claude session summon professionals on a shared team, delegate between them, and publish new ones to the marketplace" width="900"></p>

## What is this

- A **professional** is a specialist Claude session with a narrow job: system design, reliability review, docs, voice pipelines, Linux ops, and more.
- Any Claude session with the `professionals` skill can look one up, summon it, send it work and get answers back. Professionals can summon each other.
- When nobody fits, a new professional is written, tested and published here for everyone.

## Start in one paste

Paste this into Claude Code:

```
Install the Professionals system for me: run `claude plugin marketplace add DgxSparkLabs/professionals` and `claude plugin install professionals@professionals`, then run `~/.claude/plugins/marketplaces/professionals/plugins/professionals/bin/pros doctor --link` and show me `pros list`.
```

You need Claude Code 2.1.224 or newer, `tmux` and `python3`. If one is missing, `pros doctor` says which and how to install it.

## The professionals

| Professional | What it does | Install name |
|---|---|---|
| `system-designer` | Turns a goal into components, interfaces, failure modes and a build order | `pro-system-designer` |
| `reliability-reviewer` | Finds dead hangs, races, lost messages and missing deadlines | `pro-reliability-reviewer` |
| `docs-designer` | READMEs that explain at a glance, with diagrams | `pro-docs-designer` |
| `voice-pipeline-engineer` | Real-time voice agents: VAD, end-of-turn, barge-in, latency | `pro-voice-pipeline-engineer` |
| `linux-ops` | systemd units, hardening, timers, journald, VMs | `pro-linux-ops` |
| `speckit-driver` | Drives the spec-kit flow from specify to implement | `pro-speckit-driver` |
| `exhaustive-researcher` | Complete, dated, sourced research with coverage counts | `pro-exhaustive-researcher` |
| `professional-smith` | Creates, trains, validates and publishes new professionals | `pro-professional-smith` |

Each one is also a plain skill, so you can install a single persona on its own: `claude plugin install pro-linux-ops@professionals`.

## How it works

1. **Summon.** `pros summon reliability-reviewer` starts the professional in its own live session (or reuses the one already running). Sessions live on one tmux server, so every professional can see the team.
2. **Talk.** `pros ask reliability-reviewer "Give me three failure cases for this design"`. Messages carry a header with sender, task id and reply address, and the answer comes back tagged with the same task id. A busy professional gets the message queued and delivered when it is free. Nothing is typed into the middle of its work.
3. **Delegate.** Every professional checks the task against its scope. Outside it, the professional hands that part to someone better, or declines with a reason. The delegation chain travels with the message, so loops are refused.
4. **Grow.** No fit? `professional-smith` writes a new definition from the template, practices it on sample tasks, validates it and publishes it.

Useful commands: `pros list`, `pros search <words>`, `pros team`, `pros attach <name>`, `pros stop <name>`, `pros doctor`. The full list is in the skill: `plugins/professionals/skills/professionals/SKILL.md`.

## Example session

```
you> I need to add a retry queue to our worker. Get a review first.

claude> pros summon reliability-reviewer
        reliability-reviewer: ready (project scope, 14 s)
claude> pros ask reliability-reviewer "Review worker/queue.py for failure cases" --wait 90
        pending: pros wait t-41a2          (the reviewer is still reading)
claude> pros wait t-41a2 --timeout 90
        [pro-msg id=m-77 from=reliability-reviewer to=you task=t-41a2 kind=reply]
        P0 queue.py:58  retry has no cap; a poison message loops forever. Test: send a failing item, expect 5 tries then dead-letter.
        P1 queue.py:91  ack is written before the handler finishes; a crash loses the item.
        P1 queue.py:120 two workers can claim one item (check then rename, no lock).
        Not checked: the deployment unit file.
        I can ask linux-ops about that. Want me to?
```

## Safety and limits

- **Caps.** At most 6 live professionals, chain depth at most 4, a budget of summons per hour, and idle professionals stop after 30 minutes. All are configurable in `~/.professionals/config.json`.
- **Deadlines.** Every request has one. A stuck or dead target is reported as `stuck: <reason>`, never waited on forever.
- **Permissions.** Professionals start in `dontAsk` mode with an allowlist taken from their `tools:` line, intersected with a fixed safe list. A missing permission is a clear denial, never a hidden dialog. Your own Claude Code permission settings always apply, and nothing here widens them.
- **Sandbox.** Professionals run in the Claude Code sandbox where available, with read access denied to sensitive paths you list in `deny_paths`.
- **Trust.** Only professionals from marketplaces you have added can be summoned. A professional is text that instructs an agent, so read one before you add a marketplace you do not know.
- **Peers are not bosses.** A message from another professional is a request. It never outranks what you told the session.

## Add your own professional

1. Ask your session: "Create a professional that audits systemd unit hardening." It summons `professional-smith`, which does the rest.
2. Or by hand: `pros new my-pro --purpose "..." --tags a,b`, edit `skills/my-pro/SKILL.md` (use `templates/professional/` as the guide), run `pros validate my-pro`, then `pros publish my-pro`.
3. You publish through a pull request. See [CONTRIBUTING.md](CONTRIBUTING.md).

## FAQ

**Does this cost extra?** Each professional is a full Claude Code session, so it uses your usage. Idle ones stop after 30 minutes and personas default to the `sonnet` model.

**Does it run in the cloud?** No. Every session runs on your own machine.

**Can I watch a professional work?** Yes. `pros attach <name>` prints the tmux command to join its session.

**What if tmux is missing?** `pros doctor` tells you and shows the install line for your system.

**Does it work without the paste?** Yes. Run the two `claude plugin` commands yourself, then `pros doctor --link`.

## License

MIT. See [LICENSE](LICENSE).
