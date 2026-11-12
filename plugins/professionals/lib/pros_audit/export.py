"""Audit export (docs/spec/audit/privacy.md section 4). Redacted by default; raw is a separate, explicitly confirmed mode.

Redaction is an allowlist: a field not named in the policy below is dropped. User-chosen tokens (task ids, evaluator, rubric and
criterion ids, config values, local professional names, project hashes, session and message ids) become per-export pseudonyms
HMAC-SHA256(key, class + ":" + value)[:12] with a key of 32 random bytes that is generated for this export and never stored,
so joins hold inside one export and do not carry over to another. Times are floored to whole seconds. Tmux names, transcript refs,
evidence tokens and every unknown string are dropped. HTML is self-contained: no script, no link, every value escaped.
Standard library only."""
import csv
import hashlib
import hmac
import html
import io
import json
import os
import re
import sqlite3
import time

from . import evaluation as ev

SCHEMA_VERSION = 1
TABLES = ("events", "assignments", "evaluations", "exchanges")
PREFIX = {"message": "a", "session": "s", "task": "t", "professional": "p", "run": "r", "label": "l", "project": "j", "revision": "v",
          "evaluation": "e", "criterion": "c", "wait": "w", "event": "x", "model": "k"}
# data field -> pseudonym class. Strings not listed here and not in KEEP_STR are dropped.
PSEUDO = {"parent": "message", "target_msg": "message", "returned_msg": "message", "reply_msg": "message", "target": "message", "task": "task",
          "to": "professional", "from_pro": "professional", "caller": "professional", "renamed_from": "professional", "wait": "wait",
          "run": "run", "parent_run": "run", "session": "session", "origin_session": "session", "parent_session": "session", "project": "project",
          "revision": "revision", "eval": "evaluation", "supersedes": "evaluation", "evaluator": "label", "evaluator_version": "label",
          "rubric": "label", "rubric_version": "label", "value": "label", "target_event": "event"}
NAME_KEYS = ("to", "from_pro", "caller", "renamed_from", "reader")
KEEP_STR = {"task_form", "returned", "kind", "from_role", "via", "answered_by", "reason", "outcome", "origin", "scope", "status", "evaluator_kind", "target_kind",
            "observed_by", "source", "class", "change", "mode", "format", "hook", "setting", "fields"}
DROP = {"tmux", "transcript_ref", "evidence", "manifest_sha256", "export", "batch"}
TIME_KEYS = {"t", "deadline", "effective_at", "from_t", "to_t", "before", "created", "answered_at", "first_delivery", "started", "ended"}
DROPPED_FIELDS = ["tmux", "transcript_ref", "evidence", "exchange_key", "manifest_sha256", "unlisted_strings"]
PSEUDONYMIZED = ["task", "project", "session", "run", "message", "evaluator", "rubric", "criterion", "config_value", "local_professional_name", "wait", "revision"]


class ExportError(Exception):
    pass


def parse_when(text, now=None):
    """--since/--until: epoch seconds, Nd/Nh/Nm relative to now, or YYYY-MM-DD (UTC)."""
    now = time.time() if now is None else now
    if text is None:
        return None
    m = re.fullmatch(r"(\d+)([dhm])", text)
    if m:
        return now - int(m.group(1)) * {"d": 86400, "h": 3600, "m": 60}[m.group(2)]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        import calendar
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%d")))
    try:
        return float(text)
    except ValueError:
        raise ExportError("cannot read the time %r (use 7d, 12h, 2027-01-15 or epoch seconds)" % text)


class Redactor:
    def __init__(self, raw, marketplace_names=(), key=None):
        self.raw = raw
        self.key = key if key is not None else os.urandom(32)  # never stored
        self.market = set(marketplace_names)

    def pseudo(self, cls, value):
        if value is None:
            return None
        if self.raw:
            return value
        d = hmac.new(self.key, ("%s:%s" % (cls, value)).encode("utf-8"), hashlib.sha256).hexdigest()[:12]
        return "%s-%s" % (PREFIX.get(cls, "z"), d)

    def name(self, value):
        if value is None or value == "user":
            return value
        return value if (self.raw or value in self.market) else self.pseudo("professional", value)

    def time(self, v):
        if v is None or self.raw:
            return v
        return float(int(v // 1))

    def value(self, key, v, cls=None):
        """One data field -> redacted value, or the sentinel DROPPED."""
        if self.raw:
            return v
        if key in DROP:
            return DROPPED
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return self.time(v) if key in TIME_KEYS else v
        if isinstance(v, str):
            if key in NAME_KEYS:
                return self.name(v)
            if key in PSEUDO:
                return self.pseudo(PSEUDO[key], v)
            if key in KEEP_STR:
                return v if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", v) else DROPPED
        return DROPPED


DROPPED = object()


def clean_data(red, data):
    out = {}
    for k, v in (data or {}).items():
        if k == "criteria" and isinstance(v, list):
            out[k] = [{"id": red.pseudo("criterion", c.get("id")), "outcome": c.get("outcome")} for c in v if isinstance(c, dict)] if not red.raw else v
            continue
        r = red.value(k, v)
        if r is not DROPPED:
            out[k] = r
    return out


def snapshot(conn, now=None):
    sv = conn.execute("SELECT max(version) FROM schema_version").fetchone()[0] or 1
    b = conn.execute("SELECT batch FROM import_batch ORDER BY started DESC, rowid DESC LIMIT 1").fetchone()
    mx = conn.execute("SELECT COALESCE(max(seq), 0) FROM event").fetchone()[0]
    gen, ptab, pdate = conn.execute("SELECT generation, pricing_table, pricing_dated FROM store_state").fetchone()
    key = "|".join([str(sv), b[0] if b else "", str(mx), str(gen), ptab or ""])
    return {"id": "s-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12], "schema_version": sv, "last_batch": b[0] if b else None, "max_seq": mx,
            "generation": gen, "pricing": {"table_id": ptab or "none", "dated": pdate or "1970-01-01"}, "taken_at": round(now or time.time(), 3)}


def _window(col, since, until):
    q, a = [], []
    if since is not None:
        q.append("%s >= ?" % col)
        a.append(since)
    if until is not None:
        q.append("%s < ?" % col)
        a.append(until)
    return (" WHERE " + " AND ".join(q) if q else ""), a


def collect(conn, since, until, cutoff):
    """Raw rows per table (dicts), in the window. Raw ids here; redaction happens afterwards."""
    t = {}
    w, a = _window("t", since, until)
    t["events"] = [dict(zip(("id", "t", "action", "actor", "origin", "provenance", "pro", "run", "session", "msg", "data"), r)) for r in
                   conn.execute("SELECT id, t, action, actor, origin, provenance, pro, run, session, msg, data FROM event%s ORDER BY seq" % w, a)]
    w, a = _window("created", since, until)
    cols = ("msg", "root", "parent", "depth", "task", "to_name", "from_role", "created", "deadline", "first_delivery", "answered_at", "delivery", "run", "session", "confidence", "orphan", "cancelled")
    rows = [dict(zip(cols, r)) for r in conn.execute("SELECT %s FROM assignment%s ORDER BY created, msg" % (",".join(cols), w), a)]
    toks = {}
    for msg, n in conn.execute("SELECT msg, SUM(input + output + cache_write_5m + cache_write_1h + cache_read) FROM exchange WHERE confidence='verified' AND msg IS NOT NULL GROUP BY msg"):
        toks[msg] = n
    for r in rows:
        r["own_tokens"] = toks.get(r["msg"], 0)
        r["evaluation"] = ev.effective_status(conn, r["msg"], cutoff)
        d, ans = r["deadline"], r["answered_at"]
        r["deadline_status"] = "cancelled" if r["cancelled"] else "none" if d is None else ("met" if ans <= d else "late") if ans is not None else ("missed" if cutoff > d else "not_yet_due")
    t["assignments"] = rows
    ids = {r["msg"] for r in rows}
    w, a = _window("effective_at", since, until)
    evals = []
    for r in conn.execute("SELECT eval, target_kind, target, status, evaluator_kind, evaluator, evaluator_version, rubric, rubric_version, effective_at, supersedes, criteria, evidence FROM evaluation%s ORDER BY effective_at, eval" % w, a):
        d = dict(zip(("eval", "target_kind", "target", "status", "evaluator_kind", "evaluator", "evaluator_version", "rubric", "rubric_version", "effective_at", "supersedes", "criteria", "evidence"), r))
        d["criteria"] = json.loads(d["criteria"])
        d["evidence"] = json.loads(d["evidence"]) if d["evidence"] else []
        evals.append(d)
    t["evaluations"] = evals
    w, a = _window("t", since, until)
    xc = ("key", "session", "msg", "confidence", "overlap", "model", "t", "input", "output", "cache_write_5m", "cache_write_1h", "cache_write_unsplit", "cache_read", "turn", "cost_micros", "price_state", "source")
    t["exchanges"] = [dict(zip(xc, r)) for r in conn.execute("SELECT %s FROM exchange%s ORDER BY t, key" % (",".join(xc), w), a)]
    return t


def redact_tables(red, tables):
    out = {}
    out["events"] = [{"id": red.pseudo("event", r["id"]), "t": red.time(r["t"]), "action": r["action"], "actor": r["actor"], "origin": r["origin"],
                      "provenance": r["provenance"], "pro": red.pseudo("professional", r["pro"]), "run": red.pseudo("run", r["run"]),
                      "session": red.pseudo("session", r["session"]), "msg": red.pseudo("message", r["msg"]),
                      "data": clean_data(red, json.loads(r["data"]))} for r in tables["events"]] if not red.raw else tables["events"]
    if red.raw:
        for r in out["events"]:
            r["data"] = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
    out["assignments"] = [{"assignment": red.pseudo("message", r["msg"]), "root": red.pseudo("message", r["root"]), "parent": red.pseudo("message", r["parent"]),
                           "depth": r["depth"], "task": red.pseudo("task", r["task"]), "to": red.name(r["to_name"]), "from_role": r["from_role"],
                           "created": red.time(r["created"]), "deadline": red.time(r["deadline"]), "first_delivery": red.time(r["first_delivery"]),
                           "answered_at": red.time(r["answered_at"]), "delivery": r["delivery"], "deadline_status": r["deadline_status"], "evaluation": r["evaluation"],
                           "confidence": r["confidence"], "run": red.pseudo("run", r["run"]), "session": red.pseudo("session", r["session"]),
                           "orphan": r["orphan"], "cancelled": r["cancelled"], "tokens": r["own_tokens"]} for r in tables["assignments"]]
    out["evaluations"] = [{"evaluation": red.pseudo("evaluation", r["eval"]), "target_kind": r["target_kind"], "target": red.pseudo("message", r["target"]),
                           "status": r["status"], "evaluator_kind": r["evaluator_kind"], "evaluator": red.pseudo("label", r["evaluator"]),
                           "evaluator_version": red.pseudo("label", r["evaluator_version"]), "rubric": red.pseudo("label", r["rubric"]),
                           "rubric_version": red.pseudo("label", r["rubric_version"]), "effective_at": red.time(r["effective_at"]),
                           "supersedes": red.pseudo("evaluation", r["supersedes"]),
                           "criteria": r["criteria"] if red.raw else [{"id": red.pseudo("criterion", c["id"]), "outcome": c["outcome"]} for c in r["criteria"]],
                           "evidence": r["evidence"] if red.raw else len(r["evidence"]) + sum(1 for c in r["criteria"] if c.get("evidence"))} for r in tables["evaluations"]]
    out["exchanges"] = [{"exchange": red.pseudo("event", r["key"]) if not red.raw else r["key"], "session": red.pseudo("session", r["session"]),
                         "msg": red.pseudo("message", r["msg"]), "confidence": r["confidence"], "overlap": r["overlap"],
                         "model": r["model"] if (red.raw or re.fullmatch(r"claude-[a-z0-9.-]{1,40}", r["model"] or "")) else None, "t": red.time(r["t"]),
                         "input": r["input"], "output": r["output"], "cache_write_5m": r["cache_write_5m"], "cache_write_1h": r["cache_write_1h"],
                         "cache_write_unsplit": r["cache_write_unsplit"], "cache_read": r["cache_read"], "turn": r["turn"], "cost_micros": r["cost_micros"],
                         "price_state": r["price_state"], "source": r["source"]} for r in tables["exchanges"]]
    return out


def preview(conn, since=None, until=None, now=None):
    """Row counts per table for the scope, before anything is written."""
    now = time.time() if now is None else now
    cutoff = min(until, now) if until is not None else now
    t = collect(conn, since, until, cutoff)
    return {"counts": {k: len(v) for k, v in t.items()}, "cutoff": cutoff}


def _coverage(conn, cutoff, degraded):
    def q(sql, *a):
        return conn.execute(sql, a).fetchone()
    live = q("SELECT count(*), min(t), max(t) FROM event WHERE origin='live'")
    back = q("SELECT count(*), min(t), max(t) FROM event WHERE origin='backfill'")
    tr = q("SELECT count(*), min(t), max(t) FROM exchange WHERE source IN ('transcript', 'transcripts')")  # usage.py writes 'transcripts'
    sc = q("SELECT count(*) FROM session_counter")
    src = [{"source": "emitter", "quality": "observed" if live[0] else "missing", "from_t": live[1], "to_t": live[2], "rows": live[0]}]
    if back[0]:
        src.append({"source": "backfill", "quality": "reconstructed", "from_t": back[1], "to_t": back[2], "rows": back[0]})
    src.append({"source": "transcript", "quality": "observed" if tr[0] else "missing", "from_t": tr[1], "to_t": tr[2], "rows": tr[0]})
    src.append({"source": "session_counter", "quality": "observed" if sc[0] else "missing", "from_t": None, "to_t": None, "rows": sc[0]})
    try:  # store v2: source_meta is the one coverage record the query layer also reads (usage.refresh_source_meta)
        meta = {r[0]: r for r in conn.execute("SELECT source, quality, from_t, to_t, rows FROM source_meta")}
    except sqlite3.Error:
        meta = {}
    if meta:
        src = [{"source": k, "quality": meta[k][1], "from_t": meta[k][2], "to_t": meta[k][3], "rows": meta[k][4]}
               for k in ("emitter", "backfill", "transcript", "session_counter") if k in meta and (k != "backfill" or meta[k][4])]
    gaps = [{"kind": k[:32], "count": n} for k, n in conn.execute("SELECT kind, SUM(count) FROM gap GROUP BY kind ORDER BY kind")]
    cov = {"degraded": bool(degraded), "cutoff": cutoff, "pre_audit_before": live[1] if back[0] and live[0] else None, "sources": src, "gaps": gaps}
    return cov


def _fmt_csv(rows):
    buf = io.StringIO()
    if not rows:
        return b""
    cols = list(rows[0].keys())
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(r[c]) for c in cols])
    return buf.getvalue().encode("utf-8")


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        v = json.dumps(v, sort_keys=True)
    v = str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def _fmt_html(export_id, mode, tables, counts, cutoff):
    e = lambda x: html.escape("" if x is None else (json.dumps(x, sort_keys=True) if isinstance(x, (dict, list)) else str(x)), quote=True)
    parts = ['<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
             '<meta name="generator" content="pros audit export"><meta name="pros-audit-export" content="%s %s schema %d">' % (e(export_id), e(mode), SCHEMA_VERSION),
             "<title>Audit export %s</title>" % e(export_id),
             "<style>body{font:14px system-ui,sans-serif;margin:16px}table{border-collapse:collapse;margin:8px 0 24px}td,th{border:1px solid #888;padding:2px 6px;text-align:left;vertical-align:top}</style>",
             "</head><body><h1>Audit export %s</h1><p>Mode %s. Cutoff %s. Counts: %s.</p>" % (e(export_id), e(mode), e(cutoff), e(counts))]
    for name in TABLES:
        rows = tables[name]
        parts.append("<h2>%s (%d)</h2>" % (e(name), len(rows)))
        if rows:
            cols = list(rows[0].keys())
            parts.append("<table><tr>%s</tr>" % "".join("<th>%s</th>" % e(c) for c in cols))
            for r in rows:
                parts.append("<tr>%s</tr>" % "".join("<td>%s</td>" % e(r[c]) for c in cols))
            parts.append("</table>")
    parts.append("</body></html>\n")
    return "".join(parts).encode("utf-8")


def write_export(conn, out_dir, fmt="jsonl", raw=False, since=None, until=None, now=None, degraded=False, key=None):
    """Write the data files and manifest.json into out_dir, record the audit_export event; returns the manifest dict."""
    if fmt not in ("jsonl", "csv", "html"):
        raise ExportError("format must be jsonl, csv or html")
    now = time.time() if now is None else now
    cutoff = min(until, now) if until is not None else now
    market = [r[0] for r in conn.execute("SELECT name FROM professional WHERE origin='marketplace'")]
    names = [r[0] for r in conn.execute("SELECT name FROM professional ORDER BY name")]
    red = Redactor(raw, market, key)
    snap = snapshot(conn, now)
    tables = redact_tables(red, collect(conn, since, until, cutoff))
    counts = {k: len(v) for k, v in tables.items()}
    export_id = "x-" + os.urandom(4).hex()
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, mode=0o700)
    os.chmod(out_dir, 0o700)
    files = []
    if fmt == "html":
        files.append(("export.html", _fmt_html(export_id, "raw" if raw else "redacted", tables, counts, cutoff)))
    else:
        for name in TABLES:
            if fmt == "jsonl":
                body = b"".join((json.dumps(r, sort_keys=True) + "\n").encode("utf-8") for r in tables[name])
            else:
                body = _fmt_csv(tables[name])
            files.append(("%s.%s" % (name, fmt), body))
    entries = []
    for name, body in files:
        p = os.path.join(out_dir, name)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        entries.append({"name": name, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)})
    snap_out = dict(snap, last_batch=red.pseudo("label", snap["last_batch"]))
    redaction = {"pseudonyms": "per-export-hmac", "dropped_fields": [] if raw else DROPPED_FIELDS, "salt_stored": False,
                 "pseudonymized": [] if raw else PSEUDONYMIZED, "time_bucket_s": 1}
    if raw:
        redaction.pop("time_bucket_s")
    prof = [red.name(n) for n in names]
    manifest = {"manifest_version": 1, "export": export_id, "mode": "raw" if raw else "redacted", "format": fmt, "created_at": round(now, 3),
                "schema_version": snap["schema_version"], "snapshot": snap_out,
                "scope": {"from": since, "to": until, "professionals": prof, "classes": ["operational", "evaluations"]},
                "counts": counts, "redaction": redaction, "files": entries, "coverage": _coverage(conn, cutoff, degraded)}
    mbody = (json.dumps(manifest, sort_keys=True, indent=1) + "\n").encode("utf-8")
    fd = os.open(os.path.join(out_dir, "manifest.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(mbody)
    record_export(conn, manifest, hashlib.sha256(mbody).hexdigest(), sum(counts.values()), now)
    return manifest


def record_export(conn, manifest, msha, rows, now):
    """The store keeps only the audit_export record and the manifest hash (never a copy of the export)."""
    import os as _os
    data = {"export": manifest["export"], "mode": manifest["mode"], "format": manifest["format"], "rows": rows, "schema_version": manifest["schema_version"], "manifest_sha256": msha}
    evt = {"v": 1, "id": "%s-1" % _os.urandom(8).hex(), "t": round(now, 3), "action": "audit_export", "actor": "operator", "origin": "live", "data": data}
    conn.execute("BEGIN IMMEDIATE")
    g0 = conn.execute("SELECT generation FROM store_state").fetchone()[0]
    conn.execute("INSERT INTO event (id, payload_sha, t, action, actor, origin, data) VALUES (?,?,?,?,?,?,?)",
                 (evt["id"], ev.payload_sha(evt), evt["t"], "audit_export", "operator", "live", json.dumps(data, sort_keys=True)))
    conn.execute("INSERT INTO audit_log (t, action, actor, target, outcome, detail) VALUES (?, 'audit_export', 'operator', ?, 'ok', ?)", (evt["t"], manifest["export"], manifest["mode"]))
    conn.execute("UPDATE store_state SET generation = ?", (g0 + 1,))
    conn.execute("COMMIT")
