# Audit benchmark budgets, version 1

Status: frozen for the first release (step C). Evidence: `research/B4.md` and `tests/bench_audit.py` (host: 12 CPUs, ext4, Python 3.14.4, SQLite 3.46.1) and the baseline suite time recorded in step A. A budget is a limit a later step must stay under; measured values are what B4 saw. Budgets are checked by the D1 and F benchmarks, not by the default unit-test run (timings are host dependent).

| Area | Measured (B4/A) | Budget (v1) | Check |
|---|---|---|---|
| Emit one event (open O_APPEND 0600, one write, close, no fsync) | p50 0.011 ms, p95 0.020 ms | **p95 < 0.1 ms** on the PRO_HOME file system; hard cap 5 ms (FR-001) | `bench_audit.py` emit |
| Emit with fsync per event | p95 4.96 ms | not used (would consume the 5 ms cap) | decision: no fsync |
| Floor already paid by one `pros` process | p50 73 ms | emit adds < 0.15 % | ratio |
| Baseline test suite (stub claude) | 162 tests, 251.5 s | grows < 10 % from the audit tests (about 25 s) | suite wall time at step E and G |
| Importer throughput (one transaction, batches of about 100) | ~385,000 events/s insert | **>= 100,000 events/s**; 10,000-event re-import with `INSERT OR IGNORE` < 1 s | D1 bench |
| Commit latency, 4 writers + 2 readers, 100 events per transaction | p95 1.68 ms, 0 busy retries | p95 < 10 ms, busy retries 0 with a 5 s busy timeout | D1 bench |
| Snapshot consistency under writers | 0 mismatches in ~2,800 read transactions | 0 mismatches | D1 test |
| WAL size | 35 MB for 10,000 events with one pinned reader | **< 64 MB**; reads use short transactions and the importer runs `wal_checkpoint(TRUNCATE)` after a drain, so the steady state is 0 bytes | D1 bench |
| Store size | 284 B/event stored, 242 B/event spooled | 100,000 events per month about 52 MB before retention; budget **< 100 MB per 100,000 events** | D1 bench |
| Spool backlog | not measured | importer drains 10^6 queued events in bounded memory (< 256 MB RSS), batches of about 100 | D1 bench |
| Spool cap | torn line at file-size limit | cap 64 MB; at the cap the emitter drops events and sets degraded; **a torn last line never precedes a valid event** (new file after any error) | D1 test |
| Query latency on 100,000 events | not measured | each method p95 < 250 ms (one short read transaction); explain p95 < 500 ms | D3 bench |
| Crash safety | SIGKILL mid-transaction: integrity ok, committed rows only | 0 lost, 0 doubled events across a crash between capture and import | D1 test (SC-003) |
| Migration | not measured | a no-op migration of a 100,000-event store < 5 s; failure leaves version N readable | D1 test |
| Dashboard payload | not measured | first view < 250 KB JSON, no query holds a transaction | F1 test |

Open measurements carried forward: macOS fsync and file-mode costs, sandbox-on write path, real ENOSPC on a small file system (root needed), spool backlog of 10^6.
