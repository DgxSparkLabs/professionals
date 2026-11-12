# Operating the audit

How to turn the local audit on and off, look at it, back it up, restore it and delete from it. Everything here runs as you, the
operator, in a normal terminal: every `pros audit` command refuses to run inside a professional's session. The design and the
contracts are in [spec/audit.md](spec/audit.md) and [spec/audit/](spec/audit/); this page is the how-to.

All audit data lives in one directory, `$PRO_HOME/audit/` (`~/.professionals/audit/` unless `PRO_HOME` is set):

| Path | What it is |
|---|---|
| `spool/` | events written by `pros` as things happen, not yet imported (one JSON line each, metadata only) |
| `claimed/` | spool files an import has taken and not finished; the next import reads them again |
| `audit.db` (+ `-wal`, `-shm`) | the SQLite store |
| `audit.db.bak-vN` | the copy taken before a schema migration from version N |
| `reports/snapshots/` | rendered saved reports |
| `rubrics/` | rubric versions (`pros audit rubric add`) |
| `http.token` | the owner token for the dashboard and its API |
| `import.lock` | held while an import runs |

The directory is mode 0700 and its files 0600. Exports go wherever you send them (`--out`); the store keeps only a record and the
manifest hash of each export.

## Enable

Capture is on by default in **metadata** mode: allowlisted ids, states, counts, times and hashes. No prompt, reply, tool argument or
result, path or stack text is recorded. If you turned it off earlier, turn it back on with either

- the dashboard: `pros audit serve --manage`, open the printed URL, Manage, Capture mode `metadata`; or
- `config.json` in `$PRO_HOME`: remove `"capture_mode": "off"` (or set it to `"metadata"`), and make sure `PRO_AUDIT` is not set to
  `off` or `0` in your environment.

Events collect in `spool/` until you import them. Import is on demand:

```
pros audit import            # spool -> store, then usage from the Claude transcripts of known sessions
pros audit import --backfill # once, to add what the older run/ history still holds (marked as backfill, with a cutoff)
```

An import is idempotent: running it again, or after a crash, adds nothing twice. If the disk is full or the store cannot be written it
stops with exit 5 and one line; committed batches stay and unread spool files are kept for the next import.

## Inspect

```
pros audit status                          # row counts, snapshot id, coverage and gaps
pros audit summary --since 7d [--by professional]
pros audit trace <task id or message id>   # delegation tree, waits, deadlines, usage, evaluation
pros audit explain <metric> --since 7d     # how a figure was counted, with exclusions
pros audit verdict <id> --status partial --criterion C1=failed --criterion C2=passed
pros audit report save weekly --since 7d --by revision; pros audit report run weekly
pros audit compare --by revision --since 30d
pros audit serve [--manage]                # loopback dashboard on 127.0.0.1, owner token required
```

Add `--json` for the machine-readable answer; the dashboard shows the same JSON. Every answer carries a snapshot id and a coverage
block: a figure with incomplete coverage says so instead of looking complete.

## Disable

Set capture to `off` on the dashboard (Manage, Capture mode) or put `"capture_mode": "off"` in `$PRO_HOME/config.json`; for one
shell, `export PRO_AUDIT=off`. `pros` then records nothing new and works as before. Disabling deletes nothing: the store, the
reports and anything still in `spool/` stay until you purge them. A running session picks up a config change when its next `pros`
process starts.

## Back up

The store is a SQLite file in WAL mode, so copy it with SQLite's backup API rather than `cp` (a plain copy can miss the WAL):

```
umask 077; mkdir -p ~/audit-backup
python3 -c 'import os,sqlite3,sys; h=os.environ.get("PRO_HOME") or os.path.expanduser("~/.professionals"); s=sqlite3.connect(os.path.join(h,"audit","audit.db")); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close(); s.close()' ~/audit-backup/audit-$(date +%Y%m%d).db
```

This is safe while `pros` runs. Also keep `rubrics/` and `reports/` if you use them (plain files, copy them). The backup holds what
the store holds: metadata, verdicts and the evaluation evidence references, so keep it as private as the store (0600, your own
directory). `pros` also takes a backup by itself before each schema migration (`audit.db.bak-vN`).

## Restore

1. Stop `pros audit serve` and make sure no `pros audit import` is running.
2. Move the current `audit.db`, `audit.db-wal` and `audit.db-shm` aside (into a new folder, not deleted, until you are satisfied).
3. Copy the backup to `$PRO_HOME/audit/audit.db` and `chmod 600` it.
4. Run `pros audit import`. Events still in `spool/` or `claimed/` and the usage in transcripts that still exist are added again;
   nothing is counted twice.
5. Check with `pros audit status` and a `pros audit trace` you know.

Two limits. Events that were imported after the backup was taken are gone from the spool, so they are not in the restored store
(usage comes back from the transcripts while those exist). A purge made after the backup is undone by the restore: purge again.

## Purge

Deleting is an explicit owner action on the dashboard's management server and is itself recorded (an `audit_purge` event, a purge
watermark and tombstones for every deleted event id), so a later import or backfill never brings the rows back.

1. Start `pros audit serve --manage` and open the printed URL.
2. Manage, Purge: choose a class (`operational` = events, assignments, usage and their evaluations; `content` = reserved, empty in
   this release; `reports` = saved report definitions and rendered snapshots) and a time (empty = the retention you set for that
   class, in Manage, Retention).
3. Without the word `purge` in the confirmation box the request is a dry run that only counts rows; type `purge` to delete.

The same from a script, against the running `--manage` server (the port is printed when it starts):

```
curl -s -H "Authorization: Bearer $(cat "$PRO_HOME/audit/http.token")" -H 'Content-Type: application/json' \
  -d '{"class": "operational", "dry_run": true}' http://127.0.0.1:<port>/manage/v1/purge
```

Retention settings define the default cutoff of a purge; nothing is deleted on a timer. To remove the audit completely, disable
capture first, then delete the `$PRO_HOME/audit/` directory and any exports you wrote. Saved report definitions alone can be removed
with `pros audit report delete <name>`.

## What the protection covers: the same-user limitation

The store is private to your OS user (0700/0600 files), the dashboard binds to 127.0.0.1 and needs the owner token for every request,
professional sessions are denied every `pros audit` command, and `audit/` is in every professional's deny rules. That keeps the audit
away from other users and from professionals working within their Claude Code permissions.

It does not protect against your own user. Professionals run as you, so a process of the same user that is not bound by those
permissions (a persona given wider tools, a manual shell, another program) can read, change or delete the files, including audit
events. Corrections and deletions made through `pros` are recorded, which gives accountability for honest use; it is not
tamper-proofing. If you need that, run professionals as a separate OS user or keep backups where they cannot reach.

The runtime's own message files (`$PRO_HOME/run/msgs/`) hold full task bodies; they are mode 0600 in 0700 directories and are not
part of the audit store, its retention or its purge.
