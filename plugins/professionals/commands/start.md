---
description: Check that the Professionals system works, list who is available, and offer a first summon.
---

Get the user started with Professionals.

1. Run `pros doctor`. If `pros` is not found, run `~/.claude/plugins/marketplaces/professionals/plugins/professionals/bin/pros doctor --link` and then retry. If something is missing (tmux, claude version, python3), say exactly what and how to install it, then stop.
2. Run `pros list` and show the result as a short table: name, one-line purpose.
3. Offer a first summon. Pick the professional that best fits what the user is working on right now, say why in one sentence, and ask whether to run `pros summon <name>`.
4. After a summon, show `pros team` and explain in two lines how to send it a task (`pros ask <name> "..."`) and how to watch it (`pros attach <name>`).
