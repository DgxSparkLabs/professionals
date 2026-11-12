-- Audit store, schema version 2: additive delta applied by the v1 -> v2 migration (store.py). Statements end with a semicolon at line end.
-- event.pro_name: the display name at event time (lead decision 11-12; the envelope already carries it).
ALTER TABLE event ADD COLUMN pro_name TEXT;
-- source_meta: one row per source (emitter, backfill, transcript, session_counter) for the coverage block of every query answer (query.md section 2).
CREATE TABLE source_meta (source TEXT PRIMARY KEY, rows INTEGER NOT NULL, from_t REAL, to_t REAL, versions TEXT NOT NULL DEFAULT '[]', quality TEXT NOT NULL, updated REAL NOT NULL);
-- header_seen: a delivery header row seen in a transcript of the session the delivery event names (the only source of `verified`). Kept after the transcript expires.
CREATE TABLE header_seen (msg TEXT NOT NULL, session TEXT NOT NULL, t REAL, via TEXT NOT NULL, PRIMARY KEY (msg, session));
-- exchange_conflict: the same (message.id, requestId) seen again with different usage; the first one is counted, the other is kept here.
CREATE TABLE exchange_conflict (key TEXT NOT NULL, other_session TEXT NOT NULL, first_total INTEGER NOT NULL, other_total INTEGER NOT NULL, seen REAL NOT NULL, PRIMARY KEY (key, other_session, other_total));
-- session_residual: Claude Code's own session counter minus what the transcripts hold. "Session counter, not request-level": never joined to assignments.
CREATE VIEW session_residual AS SELECT c.session AS session, c.total_tokens AS counter_tokens, c.cost_micros AS counter_cost_micros, COALESCE(x.tokens, 0) AS transcript_tokens, c.total_tokens - COALESCE(x.tokens, 0) AS residual_tokens, c.read_at AS read_at FROM session_counter c LEFT JOIN (SELECT session, sum(input + output + cache_write_5m + cache_write_1h + cache_read) AS tokens FROM exchange GROUP BY session) x ON x.session = c.session;
