"""Owner management actions for the loopback dashboard (step F1; docs/spec/audit/privacy.md sections 2-5).

Capture mode, retention, verdict entry, saved report definitions, export preview and purge. Every change is recorded in the
store as an event (audit_config, audit_evaluation, audit_purge) plus an audit_log row, with one generation bump per write
transaction. The HTTP adapter calls these only when it was started with --manage and the request carries the owner token.
Standard library only."""
import json
import os
import sqlite3
import time

from . import evaluation as ev
from . import export as ex
from . import query as Q
from . import store as S

CAPTURE_MODES = ("off", "metadata")  # "content" is reserved, not in the first release (privacy.md section 2)
RETENTION = {"operational": ("retention_metadata_days", 90), "content": ("retention_content_days", 7), "reports": ("retention_reports_days", 0)}
PURGE_CLASSES = ("operational", "content", "reports")
LOG_ACTIONS = ("audit_config", "audit_export", "audit_purge", "audit_correction", "audit_migrate")
MAX_DAYS = 3650


class ManageError(Exception):
    def __init__(self, msg, code="bad_request", field=None):
        Exception.__init__(self, msg)
        self.code, self.field = code, field


def _open(home):
    p = S.db_path(home)
    if not os.path.exists(p):
        raise ManageError("no audit store yet", "store_unavailable")
    try:
        return S.open_store(home, create=False)
    except (S.StoreError, sqlite3.Error) as e:
        raise ManageError("store not writable: %s" % type(e).__name__, "store_unavailable")


def _event(conn, action, data, now, msg=None, log_target=None, log_detail=None):
    """Insert one operator event + audit_log row inside the caller's transaction."""
    evt = {"v": 1, "id": "%s-1" % os.urandom(8).hex(), "t": round(now, 3), "action": action, "actor": "operator", "origin": "live", "data": data}
    if msg:
        evt["msg"] = msg
    conn.execute("INSERT INTO event (id, payload_sha, t, action, actor, origin, msg, data) VALUES (?,?,?,?,?,?,?,?)",
                 (evt["id"], ev.payload_sha(evt), evt["t"], action, "operator", "live", msg, json.dumps(data, sort_keys=True)))
    conn.execute("INSERT INTO audit_log (t, action, actor, target, outcome, detail) VALUES (?, ?, 'operator', ?, 'ok', ?)", (evt["t"], action, log_target, log_detail))
    return evt["id"]


def _tx(conn, fn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        g0 = conn.execute("SELECT generation FROM store_state").fetchone()[0]
        out = fn()
        conn.execute("UPDATE store_state SET generation = ?", (g0 + 1,))  # once per write transaction (query.md section 2)
        conn.execute("COMMIT")
        return out
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------- settings
def _config_path(home):
    return os.path.join(str(home), "config.json")


def _read_config(home):
    try:
        with open(_config_path(home), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_config(home, d):
    """Atomic rewrite of the pros config, keeping every other key and the file mode (0600 when new)."""
    p = _config_path(home)
    mode = 0o600
    try:
        mode = os.stat(p).st_mode & 0o777
    except OSError:
        pass
    tmp = p + ".audit.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, p)


def _latest_config(conn):
    out = {}
    for (d,) in conn.execute("SELECT data FROM event WHERE action='audit_config' ORDER BY t, seq"):
        try:
            d = json.loads(d)
        except ValueError:
            continue
        out[d.get("setting")] = d.get("value")
    return out


def settings(home, writable):
    """Capture mode (what the emitter reads), retention per class, and whether this server accepts management writes."""
    cfg = _read_config(home)
    mode = os.environ.get("PRO_AUDIT") in ("0", "off") and "off" or cfg.get("capture_mode") or "metadata"
    ret, marks = {}, {}
    if os.path.exists(S.db_path(home)):
        c = _open(home)
        try:
            latest = _latest_config(c)
            marks = {r[0]: {"purged_before": r[1], "rows": r[2], "at": r[3]} for r in c.execute("SELECT class, purged_before, rows, at FROM purge_mark")}
        finally:
            c.close()
    else:
        latest = {}
    for cls, (key, dflt) in RETENTION.items():
        v = latest.get(key)
        ret[cls] = int(v) if v is not None and str(v).isdigit() else dflt
    return {"writable": bool(writable), "capture_mode": mode, "capture_modes": list(CAPTURE_MODES), "retention_days": ret, "purge_marks": marks,
            "env_override": os.environ.get("PRO_AUDIT") in ("0", "off")}


def set_capture(home, mode, now):
    if mode not in CAPTURE_MODES:
        raise ManageError("capture mode must be one of %s" % ", ".join(CAPTURE_MODES), field="mode")
    c = _open(home)
    try:
        _tx(c, lambda: _event(c, "audit_config", {"setting": "capture_mode", "value": mode}, now, log_target="capture_mode", log_detail=mode))
    finally:
        c.close()
    cfg = _read_config(home)
    cfg["capture_mode"] = mode
    _write_config(home, cfg)
    return {"capture_mode": mode}


def set_retention(home, cls, days, now):
    if cls not in RETENTION:
        raise ManageError("retention class must be one of %s" % ", ".join(RETENTION), field="class")
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= MAX_DAYS:
        raise ManageError("days must be an integer 0..%d" % MAX_DAYS, field="days")
    key = RETENTION[cls][0]
    c = _open(home)
    try:
        _tx(c, lambda: _event(c, "audit_config", {"setting": key, "value": str(days)}, now, log_target=key, log_detail=str(days)))
    finally:
        c.close()
    return {"class": cls, "days": days}


# ---------------------------------------------------------------------------- verdict
def verdict(home, body, now):
    """Same rules as `pros audit verdict` (evaluation.record_verdict); criteria as {"C1": "failed"} or [["C1", "failed", "kind:token"?], ...]."""
    crit = body.get("criteria") or {}
    if isinstance(crit, dict):
        crit = [(k, v) for k, v in sorted(crit.items())]
    if not isinstance(crit, list) or not all(isinstance(x, (list, tuple)) and 2 <= len(x) <= 3 and all(isinstance(y, str) for y in x) for x in crit):
        raise ManageError("criteria must map ID to passed|failed|not_assessed", field="criteria")
    for k in body:
        if k not in ("target", "status", "criteria", "rubric", "rubric_version", "evidence", "evaluator", "rest", "supersedes", "no_supersede", "target_kind"):
            raise ManageError("unknown field", field=k)
    evid = body.get("evidence") or []
    if not isinstance(evid, list):
        raise ManageError("evidence must be a list of kind:token refs", field="evidence")
    sup = None if body.get("no_supersede") else (body.get("supersedes") or "auto")
    c = _open(home)
    try:
        e = ev.record_verdict(c, home, body.get("target"), body.get("status"), [tuple(x) for x in crit], rubric=body.get("rubric"),
                              rubric_version=body.get("rubric_version"), evidence=evid, evaluator=body.get("evaluator") or "owner",
                              supersedes=sup, target_kind=body.get("target_kind"), rest=body.get("rest"), now=now)
        best, _ = ev.effective(ev.evaluations(c, body["target"]), None)
    except ev.EvalError as x:
        raise ManageError(str(x), x.code if x.code in Q.CODES else "bad_request")
    finally:
        c.close()
    d = e["data"]
    return {"eval": d["eval"], "status": d["status"], "supersedes": d["supersedes"], "effective": best["eval"] if best else None}


# ---------------------------------------------------------------------------- saved reports
# One implementation: reports.py (F2) owns definitions, versions, snapshots and the audit_log rows; these wrappers only map errors
# and open the store, so the dashboard and `pros audit report ...` read and write the same rows (test_audit_reports parity test).
def _reports_call(home, fn):
    from . import reports as R
    c = _open(home)
    try:
        return fn(R, c)
    except R.ReportError as e:
        raise ManageError(str(e), e.code, "name" if "name" in str(e) else "spec")
    finally:
        c.close()


def save_report(home, query, name, spec, now):
    if not isinstance(spec, dict) or not all(isinstance(v, (str, int, float, list)) for v in spec.values()):
        raise ManageError("spec must be a flat object", field="spec")
    return _reports_call(home, lambda R, c: R.save_definition(c, name, spec, now, query))


def list_reports(home):
    if not os.path.exists(S.db_path(home)):
        return {"reports": [], "snapshots": []}
    return _reports_call(home, lambda R, c: R.list_payload(c, home))


def delete_report(home, name, now):
    return _reports_call(home, lambda R, c: R.delete_definition(c, name, now))


def run_report(home, query, name, now):
    """Render a snapshot of a saved definition (a local file under audit/reports/snapshots, 0600)."""
    def go(R, c):
        snap, _ = R.render_snapshot(query, c, home, R.load_definition(c, name), now)
        return {"snapshot": snap["id"], "cutoff": snap["cutoff"], "rows": R.snapshot_rows(snap)}
    return _reports_call(home, go)


# ---------------------------------------------------------------------------- export preview
def export_preview(home, since, until, fmt, raw, now):
    if fmt not in ("jsonl", "csv", "html"):
        raise ManageError("format must be jsonl, csv or html", field="format")
    try:
        s, u = ex.parse_when(None if since is None else str(since), now), ex.parse_when(None if until is None else str(until), now)
    except ex.ExportError as e:
        raise ManageError(str(e), field="since")
    c = _open(home)
    try:
        pv = ex.preview(c, s, u, now)
    finally:
        c.close()
    return {"mode": "raw" if raw else "redacted", "format": fmt, "since": s, "until": u, "cutoff": pv["cutoff"], "counts": pv["counts"],
            "note": "preview only: nothing is written; write the export with `pros audit export` in a terminal"}


# ---------------------------------------------------------------------------- purge
def _purge_operational(c, before):
    """An assignment created before the cutoff goes as a whole (all its events, its evaluations); other events go by their own time."""
    keep = ",".join("?" * len(LOG_ACTIONS))
    ids = {r[0] for r in c.execute("SELECT id FROM event WHERE t < ? AND action NOT IN (%s) AND action != 'audit_evaluation'" % keep, (before,) + LOG_ACTIONS)}
    msgs = [r[0] for r in c.execute("SELECT DISTINCT msg FROM event WHERE t < ? AND action = 'msg_created' AND msg IS NOT NULL ORDER BY msg", (before,))]
    for i in range(0, len(msgs), 400):
        part = msgs[i:i + 400]
        sql = "SELECT id FROM event WHERE msg IN (%s) AND action NOT IN (%s)" % (",".join("?" * len(part)), keep)
        ids.update(r[0] for r in c.execute(sql, tuple(part) + LOG_ACTIONS))
    return sorted(ids), msgs


def purge(home, cls, before, now, dry_run=False, reason="operator"):
    """Delete rows of one class older than `before` (epoch). Operational: events (not the audit-log actions), the evaluations of purged
    assignments, exchanges, spans and header rows; tombstones + purge watermark so no re-import or backfill restores them; derived tables rebuilt."""
    if cls not in PURGE_CLASSES:
        raise ManageError("purge class must be one of %s" % ", ".join(PURGE_CLASSES), field="class")
    if isinstance(before, bool) or not isinstance(before, (int, float)) or before > now:
        raise ManageError("before must be an epoch time not in the future", field="before")
    c = _open(home)
    try:
        if cls == "operational":
            ids, msgs = _purge_operational(c, before)
            nx = c.execute("SELECT COUNT(*) FROM exchange WHERE t < ?", (before,)).fetchone()[0]
            counts = {"events": len(ids), "assignments": len(msgs), "exchanges": nx}
        elif cls == "content":
            counts = {"content": c.execute("SELECT COUNT(*) FROM content WHERE expires < ?", (before,)).fetchone()[0]}
        else:
            from . import reports as R
            old_snaps = [x["id"] for x in R.list_snapshots(home) if x["created_at"] < before]
            counts = {"reports": c.execute("SELECT COUNT(*) FROM report_def WHERE created < ?", (before,)).fetchone()[0], "snapshots": len(old_snaps)}
        total = sum(counts.values())
        if dry_run:
            return {"class": cls, "before": before, "counts": counts, "dry_run": True}

        def go():
            if cls == "operational":
                ids2, msgs2 = _purge_operational(c, before)
                for i in range(0, len(ids2), 400):
                    part = ids2[i:i + 400]
                    q = ",".join("?" * len(part))
                    c.execute("DELETE FROM event WHERE id IN (%s)" % q, part)
                    c.executemany("INSERT OR IGNORE INTO event_tombstone (id, removed_at, reason) VALUES (?,?,?)", [(x, now, "purge") for x in part])
                for i in range(0, len(msgs2), 400):
                    part = msgs2[i:i + 400]
                    q = ",".join("?" * len(part))
                    for sql in ("DELETE FROM evaluation WHERE target IN (%s)", "DELETE FROM header_seen WHERE msg IN (%s)",
                                "DELETE FROM span_link WHERE span IN (SELECT span FROM span WHERE msg IN (%s))", "DELETE FROM span WHERE msg IN (%s)"):
                        c.execute(sql % q, part)
                c.execute("DELETE FROM exchange WHERE t < ?", (before,))
            elif cls == "content":
                c.execute("DELETE FROM content WHERE expires < ?", (before,))
            else:
                c.execute("DELETE FROM report_def WHERE created < ?", (before,))
            old = c.execute("SELECT purged_before FROM purge_mark WHERE class = ?", (cls,)).fetchone()
            mark = max(before, old[0]) if old else before
            c.execute("INSERT OR REPLACE INTO purge_mark (class, purged_before, rows, at) VALUES (?,?,?,?)", (cls, mark, total, now))
            _event(c, "audit_purge", {"class": cls, "before": before, "rows": total, "reason": reason}, now, log_target=cls, log_detail=str(total))
        _tx(c, go)
        if cls == "operational":
            from . import derive
            derive.rebuild(c)
        elif cls == "reports":
            for sid in old_snaps:  # rendered snapshot files are the same retention class as their definitions
                try:
                    os.remove(os.path.join(R.reports_dir(home), "snapshots", sid + ".json"))
                except FileNotFoundError:
                    pass
    finally:
        c.close()
    return {"class": cls, "before": before, "counts": counts, "dry_run": False}


def retention_cutoff(home, cls, now):
    days = settings(home, False)["retention_days"].get(cls)
    if days is None:
        raise ManageError("no retention for this class", field="class")
    return now - days * 86400
