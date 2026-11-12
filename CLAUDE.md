# Contributing notes for Claude sessions in this repository

This repo is a public marketplace of Claude Code plugins plus the `pros` command that summons them. Read `README.md` and `CONTRIBUTING.md` first.

## Layout
- `plugins/professionals/bin/pros`: the whole command, one Python 3 file, standard library only.
- `plugins/professionals/skills/professionals/SKILL.md`: the skill every session reads; keep it in step with the command.
- `plugins/pro-<name>/skills/<name>/SKILL.md`: one professional each, listed in `.claude-plugin/marketplace.json`.
- `tests/`: unittest suite with a stub `claude` (`tests/fake_claude.py`) and a private tmux socket.
- `docs/spec/`: the public spec sheet.

## Rules
- Public repo: no personal names, private hostnames, absolute home paths, tokens or business data anywhere, including tests and docs.
- Every behavior change gets a test; a bug fix gets the test that would have caught it. Never loosen a test to make it pass.
- Run the whole suite before committing: `python3 -m unittest discover -s tests`.
- Keep source lines short (under 400 bytes) and use only the standard library.
- Safety rules of `pros` are not optional: the allowlist gate, folder trust by content hash, dialogs answered only for folders the user registered, never blanket-accepted.
- Bump the version in `pros`, `plugins/professionals/.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` together, and add a `CHANGELOG.md` entry.
- Do not run `pros` commands that start real sessions from tests without the stub; use `PRO_CLAUDE_BIN`.
