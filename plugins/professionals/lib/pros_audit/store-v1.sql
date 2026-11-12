-- Audit store, schema version 1 (docs/spec/audit/events.md, "Store and migrations").
-- SQLite >= 3.24 floor: no RETURNING, no STRICT, no JSON functions. Created by the importer only, file mode 0600.
-- Authority (events.md rule 4): `event` (with `event_conflict`, `event_tombstone`), `evaluation`, `report_def`, `content`, `audit_log`, `purge_mark`
-- are authoritative once imported (the spool is deleted after a confirmed import). Derived tables (professional, revision, run, assignment,
-- attempt, wait, span, span_link, exchange) rebuild from `event` + transcripts. Every audit_* action lives in `event`; management actions
-- also write an `audit_log` row in the same transaction so they outlive an event purge.
-- Generation (query.md section 2): every write transaction bumps store_state.generation once; the triggers below are the safety net on the
-- low-volume tables. High-volume writers (importer batches, exchange and derived rebuilds) bump explicitly once per transaction.
CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at REAL NOT NULL, note TEXT);
CREATE TABLE store_state (id INTEGER PRIMARY KEY CHECK (id = 1), generation INTEGER NOT NULL, pricing_table TEXT, pricing_dated TEXT);
INSERT INTO store_state VALUES (1, 0, NULL, NULL);
CREATE TABLE import_batch (batch TEXT PRIMARY KEY, started REAL NOT NULL, finished REAL, spool_files INTEGER, events INTEGER, duplicates INTEGER, conflicts INTEGER NOT NULL DEFAULT 0, purged_skipped INTEGER NOT NULL DEFAULT 0, gaps INTEGER, last_seq INTEGER);
CREATE TABLE event (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, payload_sha TEXT NOT NULL, t REAL NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL, origin TEXT NOT NULL, provenance TEXT, pro TEXT, run TEXT, session TEXT, msg TEXT, data TEXT NOT NULL, batch TEXT REFERENCES import_batch(batch));
CREATE TABLE event_conflict (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, payload_sha TEXT NOT NULL, t REAL NOT NULL, action TEXT NOT NULL, data TEXT NOT NULL, batch TEXT);
CREATE TABLE event_tombstone (id TEXT PRIMARY KEY, removed_at REAL NOT NULL, reason TEXT NOT NULL);
CREATE TABLE purge_mark (class TEXT PRIMARY KEY, purged_before REAL NOT NULL, rows INTEGER NOT NULL, at REAL NOT NULL);
CREATE TABLE quarantine (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, action TEXT, envelope TEXT NOT NULL, raw_data TEXT NOT NULL, batch TEXT, keep_until_release TEXT);
CREATE INDEX event_msg ON event(msg, t);
CREATE INDEX event_pro_t ON event(pro, t);
CREATE TABLE professional (pro TEXT PRIMARY KEY, name TEXT NOT NULL, origin TEXT NOT NULL, first_seen REAL, last_seen REAL, removed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE revision (pro TEXT NOT NULL, revision TEXT NOT NULL, first_seen REAL, PRIMARY KEY (pro, revision));
CREATE TABLE run (run TEXT PRIMARY KEY, pro TEXT NOT NULL, session TEXT, kind TEXT NOT NULL, started REAL NOT NULL, ended REAL, end_reason TEXT, parent_run TEXT, revision TEXT);
CREATE INDEX run_session ON run(session);
CREATE TABLE assignment (msg TEXT PRIMARY KEY, root TEXT NOT NULL, parent TEXT, depth INTEGER NOT NULL, task TEXT NOT NULL, task_form TEXT, to_pro TEXT, to_name TEXT NOT NULL, from_role TEXT NOT NULL, created REAL NOT NULL, deadline REAL, first_delivery REAL, answered_at REAL, delivery TEXT NOT NULL, run TEXT, session TEXT, confidence TEXT NOT NULL DEFAULT 'unassigned', orphan INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0);
CREATE INDEX assignment_root ON assignment(root);
CREATE INDEX assignment_created ON assignment(created);
CREATE TABLE attempt (msg TEXT NOT NULL, n INTEGER NOT NULL, via TEXT NOT NULL, t REAL NOT NULL, run TEXT, session TEXT, dup INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (msg, n));
CREATE TABLE wait (wait TEXT PRIMARY KEY, caller TEXT NOT NULL, task TEXT NOT NULL, target_msg TEXT, start REAL NOT NULL, end REAL, outcome TEXT, exit INTEGER, returned TEXT, returned_msg TEXT, dur_ms REAL);
CREATE TABLE exchange (key TEXT PRIMARY KEY, session TEXT NOT NULL, msg TEXT, confidence TEXT NOT NULL, overlap INTEGER NOT NULL DEFAULT 0, model TEXT, t REAL, input INTEGER NOT NULL, output INTEGER NOT NULL, cache_write_5m INTEGER NOT NULL, cache_write_1h INTEGER NOT NULL, cache_write_unsplit INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL, turn INTEGER, cost_micros INTEGER, price_state TEXT NOT NULL, pricing_table TEXT, no_key INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL);
CREATE TABLE span (span TEXT PRIMARY KEY, kind TEXT NOT NULL, msg TEXT, session TEXT, parent_span TEXT, start REAL NOT NULL, end REAL, name TEXT, quality TEXT NOT NULL DEFAULT 'derived_wall');
CREATE INDEX span_msg ON span(msg, start);
CREATE TABLE span_link (span TEXT NOT NULL, linked_span TEXT NOT NULL, PRIMARY KEY (span, linked_span));
CREATE INDEX exchange_msg ON exchange(msg);
CREATE INDEX exchange_session ON exchange(session, t);
CREATE TABLE session_counter (session TEXT PRIMARY KEY, total_tokens INTEGER, cost_micros INTEGER, read_at REAL);
CREATE TABLE evaluation (eval TEXT PRIMARY KEY, target_kind TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL, evaluator_kind TEXT NOT NULL, evaluator TEXT NOT NULL, evaluator_version TEXT NOT NULL, rubric TEXT NOT NULL, rubric_version TEXT NOT NULL, effective_at REAL NOT NULL, supersedes TEXT, criteria TEXT NOT NULL, evidence TEXT);
CREATE INDEX evaluation_target ON evaluation(target, effective_at);
CREATE TABLE content (id TEXT PRIMARY KEY, msg TEXT, kind TEXT NOT NULL, expires REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE report_def (id TEXT PRIMARY KEY, name TEXT NOT NULL, definition TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE audit_log (seq INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL, target TEXT, outcome TEXT NOT NULL, detail TEXT);
CREATE TABLE gap (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, source TEXT NOT NULL, from_t REAL, to_t REAL, count INTEGER NOT NULL, batch TEXT);
CREATE TRIGGER gen_evaluation_insert AFTER INSERT ON evaluation BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_evaluation_update AFTER UPDATE ON evaluation BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_evaluation_delete AFTER DELETE ON evaluation BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_audit_log_insert AFTER INSERT ON audit_log BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_audit_log_update AFTER UPDATE ON audit_log BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_audit_log_delete AFTER DELETE ON audit_log BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_gap_insert AFTER INSERT ON gap BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_gap_update AFTER UPDATE ON gap BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_gap_delete AFTER DELETE ON gap BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_purge_mark_insert AFTER INSERT ON purge_mark BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_purge_mark_update AFTER UPDATE ON purge_mark BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_purge_mark_delete AFTER DELETE ON purge_mark BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_event_conflict_insert AFTER INSERT ON event_conflict BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_event_conflict_update AFTER UPDATE ON event_conflict BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_event_conflict_delete AFTER DELETE ON event_conflict BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_event_tombstone_insert AFTER INSERT ON event_tombstone BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_event_tombstone_update AFTER UPDATE ON event_tombstone BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_event_tombstone_delete AFTER DELETE ON event_tombstone BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_quarantine_insert AFTER INSERT ON quarantine BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_quarantine_update AFTER UPDATE ON quarantine BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_quarantine_delete AFTER DELETE ON quarantine BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_session_counter_insert AFTER INSERT ON session_counter BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_session_counter_update AFTER UPDATE ON session_counter BEGIN UPDATE store_state SET generation = generation + 1; END;
CREATE TRIGGER gen_session_counter_delete AFTER DELETE ON session_counter BEGIN UPDATE store_state SET generation = generation + 1; END;
