-- Audit store, schema version 1 (docs/spec/audit/events.md, "Store and migrations").
-- SQLite >= 3.24 floor: no RETURNING, no STRICT, no JSON functions. Created by the importer only, file mode 0600.
-- Every table is rebuildable from the spool + backfill sources except `evaluation`, `report_def`, `audit_log`.
CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at REAL NOT NULL, note TEXT);
CREATE TABLE import_batch (batch TEXT PRIMARY KEY, started REAL NOT NULL, finished REAL, spool_files INTEGER, events INTEGER, duplicates INTEGER, gaps INTEGER, last_seq INTEGER);
CREATE TABLE event (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, t REAL NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL, origin TEXT NOT NULL, provenance TEXT, pro TEXT, run TEXT, session TEXT, msg TEXT, data TEXT NOT NULL, batch TEXT REFERENCES import_batch(batch));
CREATE INDEX event_msg ON event(msg, t);
CREATE INDEX event_pro_t ON event(pro, t);
CREATE TABLE professional (pro TEXT PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL, first_seen REAL, last_seen REAL, removed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE revision (pro TEXT NOT NULL, revision TEXT NOT NULL, first_seen REAL, PRIMARY KEY (pro, revision));
CREATE TABLE run (run TEXT PRIMARY KEY, pro TEXT NOT NULL, session TEXT, kind TEXT NOT NULL, started REAL NOT NULL, ended REAL, end_reason TEXT, parent_run TEXT, revision TEXT);
CREATE INDEX run_session ON run(session);
CREATE TABLE assignment (msg TEXT PRIMARY KEY, root TEXT NOT NULL, parent TEXT, depth INTEGER NOT NULL, task TEXT NOT NULL, to_pro TEXT, to_name TEXT NOT NULL, from_role TEXT NOT NULL, created REAL NOT NULL, deadline REAL, first_delivery REAL, answered_at REAL, delivery TEXT NOT NULL, run TEXT, session TEXT, confidence TEXT NOT NULL DEFAULT 'unassigned', orphan INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0);
CREATE INDEX assignment_root ON assignment(root);
CREATE INDEX assignment_created ON assignment(created);
CREATE TABLE attempt (msg TEXT NOT NULL, n INTEGER NOT NULL, via TEXT NOT NULL, t REAL NOT NULL, run TEXT, session TEXT, dup INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (msg, n));
CREATE TABLE wait (wait TEXT PRIMARY KEY, caller TEXT NOT NULL, task TEXT NOT NULL, target_msg TEXT, start REAL NOT NULL, end REAL, outcome TEXT, exit INTEGER, returned TEXT, returned_msg TEXT, dur_ms REAL);
CREATE TABLE exchange (key TEXT PRIMARY KEY, session TEXT NOT NULL, msg TEXT, confidence TEXT NOT NULL, overlap INTEGER NOT NULL DEFAULT 0, model TEXT, t REAL, input INTEGER NOT NULL, output INTEGER NOT NULL, cache_write INTEGER NOT NULL, cache_read INTEGER NOT NULL, cost_micros INTEGER, price_state TEXT NOT NULL, no_key INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL);
CREATE INDEX exchange_msg ON exchange(msg);
CREATE INDEX exchange_session ON exchange(session, t);
CREATE TABLE session_counter (session TEXT PRIMARY KEY, total_tokens INTEGER, cost_micros INTEGER, read_at REAL);
CREATE TABLE evaluation (eval TEXT PRIMARY KEY, target_kind TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL, evaluator_kind TEXT NOT NULL, evaluator TEXT NOT NULL, evaluator_version TEXT NOT NULL, rubric TEXT NOT NULL, rubric_version TEXT NOT NULL, effective_at REAL NOT NULL, supersedes TEXT, criteria TEXT NOT NULL, evidence TEXT);
CREATE INDEX evaluation_target ON evaluation(target, effective_at);
CREATE TABLE content (id TEXT PRIMARY KEY, msg TEXT, kind TEXT NOT NULL, expires REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE report_def (id TEXT PRIMARY KEY, name TEXT NOT NULL, definition TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE audit_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL, target TEXT, outcome TEXT NOT NULL, detail TEXT);
CREATE TABLE gap (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, source TEXT NOT NULL, from_t REAL, to_t REAL, count INTEGER NOT NULL, batch TEXT);
