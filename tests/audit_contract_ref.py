"""Reference implementations of the step C v1.1 contract rules (stdlib only).

Not the production code: a small, readable statement of each rule so the fixtures in tests/fixtures/audit/contracts/ can be checked against it.
Step D must produce the same answers on the same fixtures. Rules: docs/spec/audit/events.md, query.md, privacy.md, metrics.md."""
import hashlib
import hmac
import json
import os
import re
import sqlite3

SCHEMAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "spec", "audit", "schemas")
TASK_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
HEADER = re.compile(r'^(?:Stop hook feedback:\n|\s*<pasted_content id="[0-9a-f]+">\n)?\[pro-msg id=(m-[0-9a-f]{10}) ')


def canon_task(raw):
    """events.md rule 3b: a task id that does not match the bounded token form is stored as h-<hash12> with task_form=hashed. Never dropped."""
    if isinstance(raw, str) and TASK_OK.match(raw):
        return raw, None
    return "h-" + hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:12], "hashed"


def snapshot_id(schema_version, last_batch, max_seq, generation, pricing_table):
    """query.md section 2: the id covers every write class through the generation counter, and the pinned pricing table."""
    key = "|".join([str(schema_version), last_batch or "", str(max_seq), str(generation), pricing_table or ""])
    return "s-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def payload_sha(event):
    body = {k: v for k, v in event.items() if k not in ("id",)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def new_store():
    db = sqlite3.connect(":memory:", isolation_level=None)
    with open(os.path.join(SCHEMAS, "store-v1.sql"), encoding="utf-8") as f:
        db.executescript(f.read())
    db.execute("INSERT INTO schema_version VALUES (1, 0, 'v1')")
    return db


def generation(db):
    return db.execute("SELECT generation FROM store_state").fetchone()[0]


def import_events(db, events, batch="b1"):
    """Idempotent import (events.md rules 3 and privacy.md section 3). One transaction, one generation bump.
    Same id + same payload = duplicate. Same id + different payload = recorded conflict (event_conflict row + id_conflict gap), never a silent drop.
    An event older than the purge mark of its class, or tombstoned, is skipped and counted, so a purge survives re-import and backfill."""
    out = {"inserted": 0, "duplicates": 0, "conflicts": 0, "purged_skipped": 0}
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT OR IGNORE INTO import_batch (batch, started) VALUES (?, 0)", (batch,))
    mark = db.execute("SELECT purged_before FROM purge_mark WHERE class='operational'").fetchone()
    mark = mark[0] if mark else None
    for e in events:
        sha = payload_sha(e)
        dead = db.execute("SELECT 1 FROM event_tombstone WHERE id=?", (e["id"],)).fetchone()
        if dead or (mark is not None and e["t"] < mark):
            out["purged_skipped"] += 1
            continue
        row = db.execute("SELECT payload_sha FROM event WHERE id=?", (e["id"],)).fetchone()
        if row is None:
            db.execute("INSERT INTO event (id, payload_sha, t, action, actor, origin, data, batch) VALUES (?,?,?,?,?,?,?,?)",
                       (e["id"], sha, e["t"], e["action"], e["actor"], e["origin"], json.dumps(e["data"], sort_keys=True), batch))
            out["inserted"] += 1
        elif row[0] == sha:
            out["duplicates"] += 1
        else:
            db.execute("INSERT INTO event_conflict (id, payload_sha, t, action, data, batch) VALUES (?,?,?,?,?,?)",
                       (e["id"], sha, e["t"], e["action"], json.dumps(e["data"], sort_keys=True), batch))
            db.execute("INSERT INTO gap (kind, source, from_t, to_t, count, batch) VALUES ('id_conflict','spool',?,?,1,?)", (e["t"], e["t"], batch))
            out["conflicts"] += 1
    db.execute("UPDATE store_state SET generation = generation + 1")
    db.execute("UPDATE import_batch SET events=?, duplicates=?, conflicts=?, purged_skipped=? WHERE batch=?",
               (out["inserted"], out["duplicates"], out["conflicts"], out["purged_skipped"], batch))
    db.execute("COMMIT")
    return out


def purge(db, before, now):
    """Delete operational rows older than `before`, write the watermark and the audit_log row in one transaction."""
    db.execute("BEGIN IMMEDIATE")
    n = db.execute("DELETE FROM event WHERE t < ?", (before,)).rowcount
    old = db.execute("SELECT purged_before FROM purge_mark WHERE class='operational'").fetchone()
    db.execute("INSERT OR REPLACE INTO purge_mark VALUES ('operational', ?, ?, ?)", (max(before, old[0]) if old else before, n, now))
    db.execute("INSERT INTO audit_log (t, action, actor, target, outcome) VALUES (?, 'audit_purge', 'operator', 'operational', 'ok')", (now,))
    db.execute("UPDATE store_state SET generation = generation + 1")
    db.execute("COMMIT")
    return n


def insert_evaluation(db, ev):
    """A verdict written through the importer path (query.md section 2): the row insert bumps the generation."""
    db.execute("INSERT INTO evaluation (eval,target_kind,target,status,evaluator_kind,evaluator,evaluator_version,rubric,rubric_version,effective_at,supersedes,criteria,evidence)"
               " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (ev["eval"], ev["target_kind"], ev["target"], ev["status"], ev["evaluator_kind"], ev["evaluator"], ev["evaluator_version"],
                ev["rubric"], ev["rubric_version"], ev["effective_at"], ev.get("supersedes"), json.dumps(ev["criteria"]), None))


def http_gate(method, headers, port, token, origin_ok=True):
    """privacy.md section 5: loopback HTTP reads. Returns (status, error_code). Order: method, Host, Origin, token."""
    h = {k.lower(): v for k, v in headers.items()}
    if method not in ("GET", "HEAD"):
        return 405, "bad_request"
    if h.get("host") not in ("127.0.0.1:%d" % port, "localhost:%d" % port):
        return 400, "bad_request"
    if "origin" in h and h["origin"] not in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port):
        return 403, "forbidden"
    auth = h.get("authorization", "")
    if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:].encode(), token.encode()):
        return 401, "unauthorized"
    return 200, None


def join_level(has_header, delivery_has_run_session, plausible_runs):
    """events.md section 6: verified needs a header; mapped places the delivery only; unassigned otherwise."""
    if has_header:
        return "verified"
    if delivery_has_run_session or plausible_runs == 1:
        return "mapped"
    return "unassigned"


def usage_owner(level):
    """Only a verified delivery owns exchanges; a mapped one never does (its usage stays session-only)."""
    return "assignment" if level == "verified" else "session_only"


def active_seconds(spans):
    """metrics.md 2.4: union of model_request and tool spans (overlaps counted once); wait and delegation spans are not active time."""
    iv = sorted((s["start"], s["end"]) for s in spans if s["kind"] in ("model_request", "tool") and s["end"] is not None)
    total, cur = 0.0, None
    for a, b in iv:
        if cur is None or a > cur[1]:
            if cur:
                total += cur[1] - cur[0]
            cur = [a, b]
        else:
            cur[1] = max(cur[1], b)
    if cur:
        total += cur[1] - cur[0]
    return round(total, 6)


def cost_usd(tokens, table, model):
    """USD API-equivalent from a pinned table (USD per million tokens). Unknown model -> (None, 'unpriced')."""
    r = table["models"].get(model)
    if r is None:
        return None, "unpriced"
    c = (tokens["input"] * r["input"] + tokens["output"] * r["output"] + tokens["cache_write_5m"] * r["cache_write_5m"]
         + tokens["cache_write_1h"] * r["cache_write_1h"] + tokens["cache_read"] * r["cache_read"]) / 1e6
    return round(c, 6), "priced"
