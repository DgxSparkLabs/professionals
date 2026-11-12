"""Single importer: spool files -> SQLite, idempotent by event id (docs/spec/audit/events.md rule 3, section 5).

Flow: take the import lock, claim spool files by rename (a writer that still has one open recreates a fresh file on its
next event), read every claimed file in a streaming pass, insert in transactions of about 100 events, write the batch
row and the audit_import event, then delete claimed files that are settled and fully read. A crash anywhere leaves the
claimed files; the next drain reads them again and the id check makes the second pass a no-op (0 lost, 0 doubled).
Equal id with an equal payload hash is a duplicate; an equal id with another payload is kept in event_conflict."""
import contextlib
import hashlib
import json
import os
import signal
import time
import uuid

from . import actions as A
from . import store as S

SETTLE_S = 10.0
BATCH = 100


def canonical_sha(ev):
    body = {k: v for k, v in ev.items() if k != "id"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def check(ev):
    """Structural allowlist check. Returns None when valid, else a short reason."""
    if not isinstance(ev, dict):
        return "not an object"
    extra = set(ev) - set(A.ENVELOPE)
    if extra:
        return "unknown envelope key"
    if any(k not in ev for k in A.ENVELOPE_REQUIRED):
        return "missing envelope key"
    if ev["v"] != 1:
        return "unknown schema version"
    spec = A.ACTIONS.get(ev["action"])
    if spec is None:
        return "unknown action"
    if ev["actor"] not in A.ACTORS or ev["origin"] not in ("live", "backfill"):
        return "bad actor or origin"
    if ev["origin"] == "backfill" and not ev.get("provenance"):
        return "backfill without provenance"
    if not isinstance(ev["id"], str) or not isinstance(ev["t"], (int, float)) or isinstance(ev["t"], bool):
        return "bad id or time"
    d = ev["data"]
    if not isinstance(d, dict):
        return "data is not an object"
    req, opt, env_req = spec
    if set(d) - set(req) - set(opt):
        return "data key outside the allowlist"
    if any(k not in d for k in req):
        return "missing data key"
    if any(k not in ev for k in env_req):
        return "missing envelope stamp"
    return None


def _die_seam(where):
    if os.environ.get("PROS_AUDIT_TEST_DIE") == where:
        os.kill(os.getpid(), signal.SIGKILL)


@contextlib.contextmanager
def import_lock(home):
    import fcntl
    S.ensure_dir(S.audit_dir(home))
    fd = os.open(os.path.join(S.audit_dir(home), "import.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def claim(home):
    spool, claimed = os.path.join(S.audit_dir(home), "spool"), os.path.join(S.audit_dir(home), "claimed")
    S.ensure_dir(claimed)
    n = 0
    if os.path.isdir(spool):
        for f in sorted(os.listdir(spool)):
            if f.endswith(".jsonl"):
                try:
                    os.rename(os.path.join(spool, f), os.path.join(claimed, "%s.%d" % (f, time.time_ns())))
                    n += 1
                except OSError:
                    pass
    return claimed, n


class Run:
    def __init__(self, conn, batch):
        self.c, self.batch = conn, batch
        self.events = self.dups = self.conflicts = self.skipped = self.gaps = 0
        self.last_seq = 0
        self.rows, self.t_lo, self.t_hi = [], None, None
        m = conn.execute("SELECT purged_before FROM purge_mark WHERE class='operational'").fetchone()
        self.mark = m[0] if m else None
        self.tomb = {r[0] for r in conn.execute("SELECT id FROM event_tombstone")}

    def add(self, ev, raw_data):
        self.rows.append((ev, raw_data))
        if len(self.rows) >= BATCH:
            self.flush()

    def gap(self, kind, source, f, t, count):
        row = self.c.execute("SELECT 1 FROM gap WHERE kind=? AND source=? AND from_t IS ? AND to_t IS ? AND count=?", (kind, source, f, t, count)).fetchone()
        if not row:
            self.c.execute("INSERT INTO gap (kind, source, from_t, to_t, count, batch) VALUES (?,?,?,?,?,?)", (kind, source, f, t, count, self.batch))
            self.gaps += 1

    def flush(self):
        rows, self.rows = self.rows, []
        if not rows:
            return
        c = self.c
        c.execute("BEGIN IMMEDIATE")
        try:
            ids = [e["id"] for e, _ in rows]
            q = ",".join("?" * len(ids))
            have = dict(c.execute("SELECT id, payload_sha FROM event WHERE id IN (%s)" % q, ids))
            conf = set(c.execute("SELECT id, payload_sha FROM event_conflict WHERE id IN (%s)" % q, ids))
            for ev, _raw in rows:
                sha = canonical_sha(ev)
                eid = ev["id"]
                if eid in self.tomb or (self.mark is not None and ev["t"] < self.mark):
                    self.skipped += 1
                    self.t_lo = ev["t"] if self.t_lo is None else min(self.t_lo, ev["t"])
                    self.t_hi = ev["t"] if self.t_hi is None else max(self.t_hi, ev["t"])
                    continue
                data = json.dumps(ev["data"], sort_keys=True, ensure_ascii=False)
                if eid in have:
                    if have[eid] == sha:
                        self.dups += 1
                    elif (eid, sha) in conf:
                        self.dups += 1
                    else:
                        c.execute("INSERT INTO event_conflict (id, payload_sha, t, action, data, batch) VALUES (?,?,?,?,?,?)", (eid, sha, ev["t"], ev["action"], data, self.batch))
                        conf.add((eid, sha))
                        self.conflicts += 1
                        self.gap("id_conflict", "spool", ev["t"], ev["t"], 1)
                    continue
                cur = c.execute(
                    "INSERT INTO event (id, payload_sha, t, action, actor, origin, provenance, pro, pro_name, run, session, msg, data, batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, sha, ev["t"], ev["action"], ev["actor"], ev["origin"], ev.get("provenance"), ev.get("pro"), ev.get("pro_name"), ev.get("run"), ev.get("session"), ev.get("msg"), data, self.batch))
                have[eid] = sha
                self.events += 1
                self.last_seq = cur.lastrowid
                if ev["action"] == "audit_gap":
                    d = ev["data"]
                    self.gap(d["kind"], d["source"], d["from_t"], d["to_t"], d["count"])
            c.execute("UPDATE store_state SET generation = generation + 1")
            _die_seam("before_commit")
            c.execute("COMMIT")
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise


def _read_file(path, run, settled):
    """Stream one claimed file. Returns (fully_read, bad_lines, last_t)."""
    bad, last_t, torn = 0, None, False
    with open(path, "rb") as f:
        for raw in f:
            if not raw.endswith(b"\n"):
                torn = True
                break
            try:
                ev = json.loads(raw)
            except ValueError:
                bad += 1
                continue
            why = check(ev)
            if why:
                bad += 1
                _quarantine(run, ev, raw, why)
                continue
            last_t = ev["t"]
            run.add(ev, raw)
    return torn, bad, last_t


def _quarantine(run, ev, raw, why):
    if isinstance(ev, dict) and isinstance(ev.get("data"), dict) and ev.get("action") not in A.ACTIONS:
        env = {k: v for k, v in ev.items() if k != "data"}
        run.flush()
        run.c.execute("BEGIN IMMEDIATE")
        run.c.execute("INSERT INTO quarantine (id, action, envelope, raw_data, batch, keep_until_release) VALUES (?,?,?,?,?,?)",
                      (str(ev.get("id")), str(ev.get("action")), json.dumps(env, sort_keys=True), json.dumps(ev["data"], sort_keys=True), run.batch, "v1"))
        run.c.execute("UPDATE store_state SET generation = generation + 1")
        run.c.execute("COMMIT")


def drain(home):
    """Import everything in the spool. Returns a summary dict (never raises for bad input, only for store errors)."""
    with import_lock(home) as got:
        if not got:
            return {"skipped": "another import is running"}
        claimed_dir, n_claimed = claim(home)
        files = sorted(os.listdir(claimed_dir))
        if not files:
            return {"events": 0, "spool_files": 0}
        _die_seam("after_claim")
        conn = S.open_store(home)
        try:
            batch = "b-" + uuid.uuid4().hex[:10]
            t0 = time.time()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO import_batch (batch, started) VALUES (?, ?)", (batch, t0))
            conn.execute("COMMIT")
            run = Run(conn, batch)
            done = []
            for name in files:
                path = os.path.join(claimed_dir, name)
                settled = time.time() - os.path.getmtime(path) >= SETTLE_S
                torn, bad, last_t = _read_file(path, run, settled)
                run.flush()
                if settled:
                    if torn or bad:
                        conn.execute("BEGIN IMMEDIATE")
                        run.gap("torn_line" if torn and not bad else "unparseable", "spool", last_t, last_t, (1 if torn else 0) + bad)
                        conn.execute("UPDATE store_state SET generation = generation + 1")
                        conn.execute("COMMIT")
                    done.append(path)
            if run.skipped:
                conn.execute("BEGIN IMMEDIATE")
                run.gap("purged_skipped", "spool", run.t_lo, run.t_hi, run.skipped)
                conn.execute("COMMIT")
            fin = time.time()
            wid = os.urandom(8).hex()
            data = {"batch": batch, "spool_files": len(files), "events": run.events, "duplicates": run.dups, "gaps": run.gaps, "last_seq": run.last_seq}
            if run.conflicts:
                data["conflicts"] = run.conflicts
            if run.skipped:
                data["purged_skipped"] = run.skipped
            ev = {"v": 1, "id": wid + "-1", "t": round(fin, 3), "action": "audit_import", "actor": "importer", "origin": "live", "data": data}
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO event (id, payload_sha, t, action, actor, origin, provenance, pro, run, session, msg, data, batch) VALUES (?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,?,?)",
                         (ev["id"], canonical_sha(ev), ev["t"], ev["action"], "importer", "live", json.dumps(data, sort_keys=True), batch))
            conn.execute("UPDATE import_batch SET finished=?, spool_files=?, events=?, duplicates=?, conflicts=?, purged_skipped=?, gaps=?, last_seq=? WHERE batch=?",
                         (fin, len(files), run.events, run.dups, run.conflicts, run.skipped, run.gaps, run.last_seq, batch))
            conn.execute("UPDATE store_state SET generation = generation + 1")
            conn.execute("COMMIT")
            for p in done:
                with contextlib.suppress(OSError):
                    os.unlink(p)
            with contextlib.suppress(Exception):
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return {"batch": batch, "spool_files": len(files), "events": run.events, "duplicates": run.dups, "conflicts": run.conflicts,
                    "gaps": run.gaps, "purged_skipped": run.skipped, "last_seq": run.last_seq, "seconds": round(fin - t0, 3)}
        finally:
            conn.close()
