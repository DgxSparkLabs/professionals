# Changelog

## Unreleased

- Audit capture (step D1): `pros` appends allowlisted events (messages, deliveries, waits, spawns, stops, failures with time, registry changes) to a private spool under `$PRO_HOME/audit/`; a single importer loads them idempotently into a private SQLite store. Capture never blocks or breaks a command; `capture_mode: off` in config.json or `PRO_AUDIT=off` turns it off. Registry entries now carry a stable `pro_id`.
- Five messaging professionals: `whatsapp-engineer`, `telegram-engineer`, `linkedin-messaging-engineer`, `instagram-messaging-engineer`, `facebook-messenger-engineer` (official versus unofficial routes, limits, ban risk, sessions, consent gates).
- Summoning a folder professional prints a warning when another live Claude session already runs in that folder.
- A session id held by two live processes (a double resume) is treated as live elsewhere: `pros` forks instead of resuming, and `pros doctor` reports it. `claude agents --json` rows are de-duplicated by pid.
- A folder's content hash now also covers files pulled in by `@` imports in CLAUDE.md/AGENTS.md that lie outside the folder (depth 3, 256 KB each, 20 files). Folders with such imports show `changed` once after upgrading; re-add with `--refresh`.

## 0.2.0

- Allowlist: only professionals you added (`pros add`, `pros enable`) can be summoned or messaged; the registry is checksummed and a change made outside `pros` blocks everything until you review it (`pros doctor --repair`).
- Folder professionals: `pros add <dir>` turns any folder with a `CLAUDE.md` into a professional. Content is hashed; a changed folder must be re-added with `--refresh`; hooks and MCP servers need `--trust-code`.
- Sessions: a professional resumes its last session, keeps a history (`pros sessions`), and can be pinned with `pros keep`.
- `pros ask` starts an allowlisted professional that is not running instead of failing with "summon it first".
- `pros ask --task ID`: asking again with the same id and the same text returns the stored answer and sends nothing new.
- The "allow external imports" dialog that appears for a CLAUDE.md with `@` imports outside the folder is answered only for folders you registered (unchanged, and without hooks or MCP unless `--trust-code`). Anywhere else the summon stops and tells you to run `pros attach`.
- Public spec sheet in `docs/spec/`.

## 0.1.0

- First release: `pros` command, core skill, ten professionals, tmux message bus.
