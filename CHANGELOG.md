# Changelog

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
