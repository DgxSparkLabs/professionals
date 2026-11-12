# Audit privacy, retention and export, version 1

Status: frozen for the first release (step C). Evidence: `research/B2.md` (OTel identifiers), `research/B4.md` (modes, access, same-user limit). Manifest contract: [schemas/export-manifest-v1.json](schemas/export-manifest-v1.json); examples in `tests/fixtures/audit/contracts/export/`.

## 1. Principle

Collection is local and the default records metadata only. Nothing leaves the machine except by an explicit export the operator starts. The core stores only allowlisted fields ([events.md](events.md)); there is no switch that turns the allowlist off. Content capture, when it exists, is a separate table with its own switch and retention.

## 2. Capture modes

| Mode | What is recorded | How it is set |
|---|---|---|
| `off` | Nothing is emitted; the emitter returns at once | Default until the release gate passes |
| `metadata` | Allowlisted events only (ids, enums, counts, times, hashes). No prompts, replies, tool arguments or results, paths, stack text | Operator setting (`audit_config capture_mode`) |
| `content` | Metadata plus the redacted-by-length bodies of messages in a separate `content` table, for one debugging purpose | Separately enabled, time-limited (`content_capture_until`, maximum 7 days, then it falls back to `metadata` by itself), own retention, never exported unless `raw` mode is chosen |

Every mode change is an `audit_config` event. Content capture is not in the first release; the table and switch are reserved so adding it is additive.

## 3. Retention classes

Each class has its own limit (days); `0` means keep nothing. A purge deletes rows, appends an `audit_purge` event, writes a **purge watermark** (`purge_mark.purged_before`, per class, in the same transaction) and removes spool files after a confirmed import. The importer and the backfill skip every event older than the watermark and every tombstoned id (`event_tombstone`), so a re-import or a backfill from `run/msgs/*.json` (which has no retention) never restores purged rows. The `audit.db.bak-vN` migration copies are purged with the class and stay mode 0600.

| Class | Contents | Default | Notes |
|---|---|---|---|
| operational | events, derived tables, exchanges, evaluations | 400 days | evaluations are kept as long as the events they judge |
| spool | not yet imported lines | until imported, cap 64 MB | after the cap the emitter drops new events and sets the degraded flag |
| content | `content` table | 7 days | only exists while content capture is on |
| reports | saved report definitions and rendered snapshots | until the operator deletes | snapshots hold aggregates, not rows with ids unless filtered by the operator |
| exports | files written by `pros audit export` | the operator's directory; the store keeps only the `audit_export` record and manifest hash | the store never keeps a copy of an export |
| audit_log | corrections, deletions, config changes, exports | 400 days | a deletion of audit data is itself recorded |

Message files in the runtime state (`run/msgs/*.json`) are outside this class: they hold task bodies and today have no retention and mode 0644. Tightening them (mode 0600, pruning) is a runtime follow-up (T-X2), not part of the audit store.

## 4. Export

1. `pros audit export` first prints a **preview**: scope, row counts per class, redaction mode, destination. Nothing is written until the operator confirms (`--yes` for scripts).
2. **Redacted is the default.** Dropped: tmux names, transcript refs, evidence ref tokens, body-derived values. Pseudonymized (the manifest lists them in `redaction.pseudonymized`): every user-chosen token (task ids, evaluator, rubric, criterion ids, evidence tokens kept only as pseudonyms where a join needs them, `audit_config` values), project hashes, session ids, and professional names that are not marketplace names (a local-folder name can be private). Marketplace names stay. Kept: counts, enums, times, durations, token counts, scores, statuses, and relations.
3. **Pseudonyms are per export.** Each pseudonym is `HMAC-SHA256(key, class + ":" + value)` truncated to 12 hex, where `key` is 32 random bytes generated for this one export and **not stored** (`salt_stored: false` in the manifest). The pseudonym class is the id family (message, session, task, professional), not the field name, so the same message id in `parent` and `root` maps alike. Within one export the same value maps to the same pseudonym, so joins (parent links, root membership, session membership) survive. Across two exports the pseudonyms differ, but exact float timestamps could still link two exports, so exports bucket times to whole seconds relative to the export start; the manifest records the bucket.
4. **Manifest** (always written beside the data): export id, mode, format, creation time, schema version, snapshot, scope, counts per table, redaction summary (dropped fields, pseudonym scheme), file names with sha256 and size, coverage block. The manifest itself contains no ids from the store beyond pseudonyms.
5. **Raw export** is a separate explicit command and flag (`--raw`, plus a typed confirmation), is never the default, never includes content unless the content class is selected, and is recorded as an `audit_export` event with `mode=raw`.
6. **Verification (D4 test):** the fixture's planted canary strings must appear in no exported file, the manifest, or the embedded HTML (text, attributes, comments, script data), and a joined export must reproduce the parent and root relations under pseudonyms. **D4 generates this export from the release-fixture store (sessions, evaluations, exchanges, two exports)**; the hand-written `export/assignments.jsonl` is a shape sample only and does not prove the canary or join rule. HTML exports are self-contained and escape every value.
7. Formats: sanitized JSONL (one table row or event per line), CSV for tables, self-contained HTML for sharing.

## 5. Access and isolation (FR-040)

- Store directory `$PRO_HOME/audit/` mode 0700; spool files, `audit.db`, `-wal` and `-shm` mode 0600. The db is pre-created 0600 before SQLite opens it (SQLite creates 0644 under umask 022).
- `audit/` joins `pro_home_denies()` so every professional session's Read/Edit deny rules and, when the sandbox is on, `denyRead` cover it.
- Every `pros audit` read or management command calls `require_user`, since a professional may run `Bash(pros *)` and would otherwise read the audit through the CLI. The emitter inside a professional's `pros` call only appends to the spool; it never reads. `require_user` keys on the environment and tmux, so D1 adds denied cases from inside a professional tmux: `PRO_SESSION= pros audit summary` and `env -u PRO_SESSION pros audit summary` must both fail.
- **HTTP access.** The adapter binds 127.0.0.1 only (any other bind address is refused) and accepts no write unless the operator starts it with `--manage`. A per-install owner token (32 random bytes as hex) lives in `$PRO_HOME/audit/http.token`, mode 0600, created on first `pros audit serve` and rotated by `pros audit serve --rotate`. **Every endpoint, reads included, requires `Authorization: Bearer <token>`** (constant-time compare; no token in a query string), so another OS user, a professional session with `curl` or a web fetch tool, and a page in the operator's browser cannot read the audit. The `Host` header must be exactly `127.0.0.1:<port>` or `localhost:<port>` (else 400), which stops DNS rebinding; a foreign `Origin` is 403; no CORS headers are sent and `OPTIONS` is 405. The dashboard receives the token in the URL fragment, which is never sent to the server. D1/F1 tests: no token -> 401, wrong token -> 401, foreign Host -> 400, foreign Origin -> 403, POST -> 405 (`tests/fixtures/audit/contracts/http_gate.json`).
- **Dashboard and management (F1).** `GET /` (plus `/dashboard.js`, `/dashboard.css`) is a static page with no store data; Host and Origin are checked there too, and it is served with a strict Content-Security-Policy (same-origin scripts and requests only, no inline script). Management (capture mode, retention, verdict entry, saved report definitions, export preview, purge) is `POST /manage/v1/<action>` with a JSON body, accepted only when the operator started `pros audit serve --manage`; without it every POST is 405. Each write needs the same owner token, `Content-Type: application/json` (so a cross-site form cannot send it) and passes the same owner check as reads; it is recorded as an `audit_config`, `audit_evaluation` or `audit_purge` event (or an `audit_log` row for report definitions) with one generation bump. The export preview writes nothing; writing an export stays a terminal command. A purge needs `confirm=purge` (otherwise use `dry_run`), deletes an assignment with all its events and evaluations, tombstones every deleted event id and moves the purge watermark, so a re-import or backfill does not restore it. `pros audit serve` itself runs `require_user`, so a professional session cannot start it or create the token.
- **Same-OS-user limitation.** Professionals run as the operator's own user. File modes protect against other users, not against a process of the same user that is not bound by Claude Code permissions (a persona granted wider tools, a manual shell, another program). The owner can always read, edit or delete the store, and a same-user process can remove audit events too. Corrections and deletions are recorded as audit events, which is accountability for honest use, not tamper-proofing. Unverified: whether a sandboxed professional's `pros` call can append to the spool (the sandbox probe fails on the test host).

## 6. Third-party telemetry (OpenTelemetry)

Not used by default. If enabled later (opt-in, local receiver only), ingest keeps an allowlist: request id, model, token counts, cache split, duration. The identifiers `user.email`, `user.account_uuid`, `organization.id` and any other account or prompt identifier are **dropped before storing**. Console exporters must not be used with a TUI session.

## 7. Rollout

`off` → `metadata` capture only (spool and importer) → CLI (`status`, `summary`, `trace`, `explain`, `verdict`) → dashboard (loopback) → reports and export. Each stage is behind the earlier one passing its tests; disabling returns to `off` without deleting data, and purge is a separate action. Operations (enable, inspect, disable, back up, restore, purge) are written in step G.
