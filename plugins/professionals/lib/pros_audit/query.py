"""Audit query API v1 (docs/spec/audit/query.md, metrics.md). Read-only, owner-only, bounded, stdlib only.

One class, six typed methods; each returns one JSON-serializable envelope. The CLI, the saved reports and the loopback HTTP adapter all call it,
so one store gives one answer on every surface. Reads use one short read transaction on a connection opened read-only; nothing here writes."""
import base64
import datetime
import hashlib
import json
import math
import os
import re
import sqlite3
import time

API_VERSION = 1
MAX_LIMIT, MAX_IDS, MAX_WINDOW_DAYS, MAX_DEPTH = 500, 500, 400, 50
CONCLUSIVE = ("passed", "partial", "failed")
VOCAB = ("passed", "partial", "failed", "inconclusive", "unreviewed")
GROUPS = ("professional", "revision", "model", "task_class", "direct_vs_delegated", "day")
FILTERS = ("professional", "revision", "model", "task_class", "direct")
CODES = ("bad_request", "unauthorized", "not_found", "store_unavailable", "forbidden", "too_large")
UNITS = {"list_professionals": {"count": "count", "time": "epoch_s_utc"}, "list_runs": {"count": "count", "time": "epoch_s_utc"},
         "task_trace": {"time": "epoch_s_utc", "tokens": "tokens"}, "query_metrics": {"count": "count", "time": "seconds", "tokens": "tokens"},
         "explain_metric": {"count": "count"}, "evaluation_evidence": {"time": "epoch_s_utc"}}
WINDOW = {"since": "time", "until": "time"}
ARGS = {
    "list_professionals": dict(WINDOW, origin="str", cursor="str", limit="int"),
    "list_runs": dict(WINDOW, professional="str", session="str", kind="str", cursor="str", limit="int"),
    "task_trace": {"id": "str!", "depth": "int"},
    "query_metrics": dict(WINDOW, since="time!", group_by="list", **{k: "str" for k in FILTERS}),
    "explain_metric": dict(WINDOW, metric="str!", since="time!", **{k: "str" for k in FILTERS}),
    "evaluation_evidence": {"target": "str!"},
}
METRICS = ("assigned_work", "evaluation_coverage", "pass_rate", "deadline_miss_rate", "queue_time", "reply_latency", "active_time", "accounted_tokens", "sessions_with_work",
           "active_days", "work_per_active_week", "direct_delegated_mix", "cache_read_share", "cost_estimate", "unpriced_share", "attribution_share", "reply_rate",
           "execution_time", "failures_by_reason", "concurrent_occupancy", "delivery_retries", "context_footprint")
PRECEDENCE = "latest non-superseded; operator outranks automated; all kept"


class AuditError(Exception):
    def __init__(self, code, message, field=None):
        Exception.__init__(self, message)
        self.code, self.message, self.field = code, message, field

    def envelope(self):
        e = {"code": self.code, "message": self.message}
        if self.field is not None or self.code == "bad_request":
            e["field"] = self.field
        return {"api_version": API_VERSION, "error": e}


def snapshot_id(schema_version, last_batch, max_seq, generation, pricing_table):
    key = "|".join([str(schema_version), last_batch or "", str(max_seq), str(generation), pricing_table or ""])
    return "s-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def nearest_rank(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    return xs[max(1, int(-(-p * len(xs) // 100))) - 1]


def dist(xs, eligible):
    r = lambda v: None if v is None else round(v, 2)
    return {"n": len(xs), "eligible": eligible, "coverage": [len(xs), eligible], "p50_s": r(nearest_rank(xs, 50)), "p95_s": r(nearest_rank(xs, 95))}


def active_seconds(spans):
    """Union of model_request and tool spans (overlaps once); waits and delegations are not active time."""
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


def _day(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d")


def _enc_cursor(key, ident):
    return base64.urlsafe_b64encode(json.dumps([key, ident]).encode()).decode()


def _dec_cursor(cur):
    try:
        k, i = json.loads(base64.urlsafe_b64decode(cur.encode()).decode())
        if isinstance(i, str) and (k is None or isinstance(k, (int, float, str))):
            return k, i
    except (ValueError, TypeError):
        pass
    raise AuditError("bad_request", "invalid cursor", "cursor")


class Query:
    """`owner_check` is called before every method and raises (or returns False) unless the caller is the owner; without one every call is forbidden."""

    def __init__(self, store, owner_check=None, now=None):
        self.store, self.owner_check, self._now = store, owner_check, now or time.time

    # ------------------------------------------------------------------ plumbing
    def _guard(self):
        if self.owner_check is None:
            raise AuditError("forbidden", "the audit is readable by its owner only")
        try:
            ok = self.owner_check()
        except AuditError:
            raise
        except Exception as e:  # a failing check never grants access
            raise AuditError("forbidden", str(e)[:120] or "owner check failed")
        if ok is False:
            raise AuditError("forbidden", "the audit is readable by its owner only")

    def _open(self):
        if isinstance(self.store, sqlite3.Connection):
            return self.store, False
        if not os.path.exists(str(self.store)):
            raise AuditError("store_unavailable", "no audit data yet; run: pros audit import")
        try:
            c = sqlite3.connect("file:%s?mode=ro" % self.store, uri=True, isolation_level=None, timeout=5)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA query_only=ON")
            c.execute("SELECT 1 FROM store_state").fetchone()
            return c, True
        except sqlite3.Error:
            raise AuditError("store_unavailable", "no readable audit store")

    def call(self, method, args=None):
        """Dispatch by name; always returns an envelope (errors as the error envelope)."""
        try:
            if method not in ARGS:
                raise AuditError("not_found", "unknown method", "method")
            return getattr(self, method)(**self._validate(method, args or {}))
        except AuditError as e:
            return e.envelope()
        except sqlite3.Error as e:
            return AuditError("store_unavailable", "store error: %s" % type(e).__name__).envelope()

    def _validate(self, method, args):
        spec, out = ARGS[method], {}
        for k in args:
            if k not in spec:
                raise AuditError("bad_request", "unknown argument", k)
        for k, kind in spec.items():
            if k not in args or args[k] is None:
                if kind.endswith("!"):
                    raise AuditError("bad_request", "missing argument", k)
                continue
            v, base = args[k], kind.rstrip("!")
            if base == "int":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise AuditError("bad_request", "not an integer", k)
            elif base == "list":
                v = [x for x in (v.split(",") if isinstance(v, str) else list(v)) if x]
            elif base == "str":
                if not isinstance(v, str) or len(v) > 200:
                    raise AuditError("bad_request", "not a short string", k)
            out[k] = v
        return out

    def _time(self, v, field, now):
        if isinstance(v, bool):
            raise AuditError("bad_request", "not a time", field)
        if isinstance(v, (int, float)):
            return float(v)
        m = re.match(r"^(\d{1,5})([dh])$", str(v))
        if m:
            return now - int(m.group(1)) * (86400 if m.group(2) == "d" else 3600)
        try:
            return float(v)
        except ValueError:
            raise AuditError("bad_request", "use epoch seconds or Nd/Nh", field)

    def _window(self, a, now, need_since):
        until = self._time(a["until"], "until", now) if a.get("until") is not None else now
        since = self._time(a["since"], "since", now) if a.get("since") is not None else (until - 30 * 86400 if need_since else None)
        if since is not None:
            if since >= until:
                raise AuditError("bad_request", "since must be before until", "since")
            if until - since > MAX_WINDOW_DAYS * 86400:
                raise AuditError("too_large", "window longer than %d days" % MAX_WINDOW_DAYS, "since")
        return since, until

    def _limit(self, a):
        n = a.get("limit", 50)
        if not 1 <= n <= MAX_LIMIT:
            raise AuditError("bad_request", "limit must be 1..%d" % MAX_LIMIT, "limit")
        return n

    def _run(self, method, args, fn):
        self._guard()
        now = self._now()
        conn, close = self._open()
        try:
            conn.execute("BEGIN")
            try:
                snap = self._snapshot(conn, now)
                cov = self._coverage(conn, now)
                result, cov_extra = fn(conn, now, cov)
                cov["gaps"] = sorted(cov["gaps"] + cov_extra, key=lambda g: g["kind"])
            finally:
                conn.execute("ROLLBACK")
        finally:
            if close:
                conn.close()
        return {"api_version": API_VERSION, "method": method, "snapshot": snap, "coverage": cov, "units": dict(UNITS[method]), "result": result}

    def _q(self, conn, sql, args=()):
        return [dict(r) if isinstance(r, sqlite3.Row) else dict(zip([d[0] for d in cur.description], r)) for cur in [conn.execute(sql, args)] for r in cur.fetchall()]

    def _snapshot(self, conn, now):
        q = lambda s: conn.execute(s).fetchone()
        ver = q("SELECT MAX(version) FROM schema_version")[0] or 1
        st = q("SELECT generation, pricing_table, pricing_dated FROM store_state")
        gen, ptab, pdate = st[0], st[1], st[2]
        lb = q("SELECT batch FROM import_batch ORDER BY started DESC, rowid DESC LIMIT 1")
        lb = lb[0] if lb else None
        ms = q("SELECT MAX(seq) FROM event")[0] or 0
        return {"id": snapshot_id(ver, lb, ms, gen, ptab), "schema_version": ver, "last_batch": lb, "max_seq": ms, "generation": gen,
                "pricing": {"table_id": ptab or "none", "dated": pdate or "1970-01-01"}, "taken_at": now}

    def _coverage(self, conn, now, cutoff=None):
        src = {}
        try:
            for r in self._q(conn, "SELECT source, quality, from_t, to_t, rows, versions FROM source_meta"):
                src[r["source"]] = {"source": r["source"], "quality": r["quality"], "from_t": r["from_t"], "to_t": r["to_t"], "rows": r["rows"]}
                if r["versions"]:
                    src[r["source"]]["versions"] = json.loads(r["versions"])
        except sqlite3.Error:
            e = conn.execute("SELECT COUNT(*), MIN(t), MAX(t) FROM event WHERE origin='live'").fetchone()
            b = conn.execute("SELECT COUNT(*), MIN(t), MAX(t) FROM event WHERE origin='backfill'").fetchone()
            x = conn.execute("SELECT COUNT(*), MIN(t), MAX(t) FROM exchange").fetchone()
            src = {"emitter": {"source": "emitter", "quality": "observed" if e[0] else "missing", "from_t": e[1], "to_t": e[2], "rows": e[0]},
                   "backfill": {"source": "backfill", "quality": "reconstructed", "from_t": b[1], "to_t": b[2], "rows": b[0]},
                   "transcript": {"source": "transcript", "quality": "observed" if x[0] else "missing", "from_t": x[1], "to_t": x[2], "rows": x[0]}}
        for name in ("emitter", "transcript", "session_counter"):
            src.setdefault(name, {"source": name, "quality": "missing", "from_t": None, "to_t": None, "rows": 0})
        if src.get("backfill", {}).get("rows", 0) == 0:
            src.pop("backfill", None)
        gaps = [{"kind": r["kind"], "count": r["count"]} for r in self._q(conn, "SELECT kind, SUM(count) AS count FROM gap GROUP BY kind")]
        pre = conn.execute("SELECT MAX(to_t) FROM gap WHERE kind='pre_audit'").fetchone()[0]
        bad = [g["kind"] for g in gaps if g["kind"] in ("spool_overflow", "write_error")]
        order = ("emitter", "backfill", "transcript", "session_counter")
        cov = {"degraded": bool(bad), "cutoff": now if cutoff is None else cutoff, "pre_audit_before": pre, "sources": [src[k] for k in order if k in src], "gaps": gaps}
        if bad:
            cov["degraded_cause"] = bad[0]
        return cov

    # ------------------------------------------------------------------ lists
    def _pro_ids(self, conn, who):
        rows = self._q(conn, "SELECT pro FROM professional WHERE pro=? OR name=?", (who, who))
        return [r["pro"] for r in rows]

    def list_professionals(self, since=None, until=None, origin=None, cursor=None, limit=50):
        self._guard()
        a = {"since": since, "until": until, "limit": limit}
        limit = self._limit(a)

        def fn(conn, now, cov):
            lo, hi = self._window(a, now, False)
            hi = hi if until is not None else None
            sql, args = ["SELECT * FROM professional WHERE 1=1"], []
            if lo is not None:
                sql.append("AND COALESCE(last_seen, 0) >= ?")
                args.append(lo)
            if hi is not None:
                sql.append("AND COALESCE(first_seen, 0) < ?")
                args.append(hi)
            if origin:
                sql.append("AND origin=?")
                args.append(origin)
            if cursor:
                k, i = _dec_cursor(cursor)
                sql.append("AND (COALESCE(last_seen, 0) < ? OR (COALESCE(last_seen, 0) = ? AND pro > ?))")
                args += [k or 0, k or 0, i]
            sql.append("ORDER BY COALESCE(last_seen, 0) DESC, pro LIMIT ?")
            args.append(limit + 1)
            rows = self._q(conn, " ".join(sql), args)
            more, rows = len(rows) > limit, rows[:limit]
            items = []
            for r in rows:
                cw, ca = ("", []) if lo is None else (" AND created >= ?", [lo])
                n_a = conn.execute("SELECT COUNT(*) FROM assignment WHERE to_pro=?" + cw + ("" if hi is None else " AND created < ?"), [r["pro"]] + ca + ([] if hi is None else [hi])).fetchone()[0]
                n_r = conn.execute("SELECT COUNT(*) FROM run WHERE pro=?", (r["pro"],)).fetchone()[0]
                revs = [x["revision"] for x in self._q(conn, "SELECT revision FROM revision WHERE pro=? ORDER BY first_seen, revision", (r["pro"],))]
                items.append({"pro": r["pro"], "name": r["name"], "origin": r["origin"], "revisions": revs, "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                              "assignments": n_a, "runs": n_r, "removed": bool(r["removed"])})
            nxt = _enc_cursor(rows[-1]["last_seen"] or 0, rows[-1]["pro"]) if more else None
            return {"items": items, "page": {"limit": limit, "next": nxt, "order": "last_seen desc, pro"}}, []
        return self._run("list_professionals", a, fn)

    def list_runs(self, professional=None, session=None, kind=None, since=None, until=None, cursor=None, limit=50):
        self._guard()
        a = {"since": since, "until": until, "limit": limit}
        limit = self._limit(a)

        def fn(conn, now, cov):
            lo, hi = self._window(a, now, False)
            hi = hi if until is not None else None
            sql, args, extra = ["SELECT * FROM run WHERE 1=1"], [], []
            if professional:
                ids = self._pro_ids(conn, professional)
                if not ids:
                    extra.append({"kind": "unknown_filter", "count": 1})
                sql.append("AND pro IN (%s)" % ",".join("?" * len(ids)) if ids else "AND 0")
                args += ids
            for col, v in (("session", session), ("kind", kind)):
                if v:
                    sql.append("AND %s=?" % col)
                    args.append(v)
            if lo is not None:
                sql.append("AND started >= ?")
                args.append(lo)
            if hi is not None:
                sql.append("AND started < ?")
                args.append(hi)
            if cursor:
                k, i = _dec_cursor(cursor)
                sql.append("AND (started < ? OR (started = ? AND run > ?))")
                args += [k, k, i]
            sql.append("ORDER BY started DESC, run LIMIT ?")
            args.append(limit + 1)
            rows = self._q(conn, " ".join(sql), args)
            more, rows = len(rows) > limit, rows[:limit]
            items = [{"run": r["run"], "pro": r["pro"], "session": r["session"], "kind": r["kind"], "started": r["started"], "ended": r["ended"], "end_reason": r["end_reason"],
                      "parent_run": r["parent_run"], "assignments": conn.execute("SELECT COUNT(*) FROM assignment WHERE run=?", (r["run"],)).fetchone()[0]} for r in rows]
            nxt = _enc_cursor(rows[-1]["started"], rows[-1]["run"]) if more else None
            return {"items": items, "page": {"limit": limit, "next": nxt, "order": "started desc, run"}}, extra
        return self._run("list_runs", a, fn)

    # ------------------------------------------------------------------ evaluations
    def _evals(self, conn):
        """All evaluations by target, with the recorded time and sequence of the audit_evaluation event that wrote them."""
        rec = {}
        for r in conn.execute("SELECT seq, t, data FROM event WHERE action='audit_evaluation'").fetchall():
            try:
                rec[json.loads(r[2]).get("eval")] = (r[1], r[0])
            except ValueError:
                pass
        out = {}
        for r in self._q(conn, "SELECT * FROM evaluation ORDER BY effective_at, eval"):
            r["criteria"] = json.loads(r["criteria"] or "[]")
            r["recorded"], r["seq"] = rec.get(r["eval"], (r["effective_at"], 0))
            out.setdefault(r["target"], []).append(r)
        return out

    @staticmethod
    def _effective(evs, cutoff):
        """Latest non-superseded evaluation at the cutoff; operator outranks automated; an automated one never supersedes an operator one."""
        vis = [e for e in evs if e["effective_at"] <= cutoff and e["recorded"] <= cutoff]
        by = {e["eval"]: e for e in vis}
        gone = set()
        for e in vis:
            s = by.get(e["supersedes"]) if e["supersedes"] else None
            if s and not (e["evaluator_kind"] != "operator" and s["evaluator_kind"] == "operator"):
                gone.add(s["eval"])
        live = [e for e in vis if e["eval"] not in gone]
        return max(live, key=lambda e: (e["evaluator_kind"] == "operator", e["effective_at"], e["seq"])) if live else None

    @staticmethod
    def _ev_view(e):
        return {"eval": e["eval"], "status": e["status"], "evaluator": e["evaluator"], "evaluator_kind": e["evaluator_kind"], "rubric": e["rubric"], "rubric_version": e["rubric_version"],
                "effective_at": e["effective_at"], "supersedes": e["supersedes"], "criteria": [{"id": c["id"], "outcome": c["outcome"]} for c in e["criteria"]]}

    def evaluation_evidence(self, target):
        self._guard()

        def fn(conn, now, cov):
            evs = self._evals(conn).get(target, [])
            if not evs and not conn.execute("SELECT 1 FROM assignment WHERE msg=?", (target,)).fetchone():
                raise AuditError("not_found", "no such message", "target")
            eff = self._effective(evs, now)
            sup = {}
            for e in evs:
                if e["supersedes"]:
                    sup[e["supersedes"]] = e["eval"]
            hist = []
            for e in sorted(evs, key=lambda e: (e["effective_at"], e["seq"])):
                hist.append({"eval": e["eval"], "status": e["status"], "evaluator_kind": e["evaluator_kind"], "effective_at": e["effective_at"], "superseded_by": sup[e["eval"]]}
                            if e["eval"] in sup else self._ev_view(e))
            return {"target": target, "effective": self._ev_view(eff) if eff else None, "history": hist, "precedence_note": PRECEDENCE}, []
        return self._run("evaluation_evidence", {"target": target}, fn)

    # ------------------------------------------------------------------ usage helpers
    @staticmethod
    def _tok(xs):
        t = {"input": 0, "output": 0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_write_unsplit": 0, "cache_read": 0}
        micros, unpriced, partial, priced = 0, 0, False, 0
        for x in xs:
            for k in t:
                t[k] += x[k] or 0
            n = x["input"] + x["output"] + x["cache_write_5m"] + x["cache_write_1h"] + x["cache_read"]
            if x["price_state"] == "unpriced" or x["cost_micros"] is None:
                unpriced += n
            else:
                micros += x["cost_micros"]
                priced += 1
                partial = partial or x["price_state"] == "partial"
        cw = t["cache_write_5m"] + t["cache_write_1h"]
        state = "unpriced" if xs and not priced else ("partial" if unpriced or partial else "priced")
        t.update({"cache_write": cw, "total": t["input"] + t["output"] + cw + t["cache_read"], "unpriced_tokens": unpriced, "price_state": state,
                  "cost_usd_api_equivalent": None if state == "unpriced" else round(micros / 1e6, 6)})
        return {k: t[k] for k in ("input", "output", "cache_write", "cache_write_5m", "cache_write_1h", "cache_write_unsplit", "cache_read", "total", "cost_usd_api_equivalent", "price_state", "unpriced_tokens")}

    @staticmethod
    def _status(a, cut):
        if a["cancelled"]:
            return "cancelled"
        if a["deadline"] is None:
            return "none"
        if a["answered_at"] is not None and a["answered_at"] <= cut:
            return "met" if a["answered_at"] <= a["deadline"] else "late"
        return "missed" if a["deadline"] <= cut else "not_yet_due"

    # ------------------------------------------------------------------ trace
    def task_trace(self, id, depth=None):
        self._guard()
        if depth is not None and not 0 <= int(depth) <= MAX_DEPTH:
            raise AuditError("bad_request", "depth must be 0..%d" % MAX_DEPTH, "depth")

        def fn(conn, now, cov):
            root = self._find_root(conn, id)
            rows = self._q(conn, "SELECT * FROM assignment WHERE root=? ORDER BY depth, created, msg", (root,))
            if not rows:
                raise AuditError("not_found", "no such task", "id")
            gaps = []
            if depth is not None:
                cut = [r for r in rows if r["depth"] > int(depth)]
                rows = [r for r in rows if r["depth"] <= int(depth)]
                if cut:
                    gaps.append({"kind": "depth_limit", "count": len(cut)})
            msgs = [r["msg"] for r in rows]
            qm = ",".join("?" * len(msgs))
            evals = self._evals(conn)
            ex = self._q(conn, "SELECT * FROM exchange WHERE msg IN (%s) AND confidence='verified'" % qm, msgs)
            att = self._q(conn, "SELECT * FROM attempt WHERE msg IN (%s) ORDER BY msg, n" % qm, msgs)
            fo = {}  # B2: failover re-runs carry attempt, retry_of and via on msg_created (optional fields)
            for e in self._q(conn, "SELECT msg, data FROM event WHERE action='msg_created' AND msg IN (%s)" % qm, msgs):
                d = json.loads(e["data"])
                if d.get("attempt"):
                    fo[e["msg"]] = {"attempt": d["attempt"], "of": d.get("retry_of"), "via": d.get("via_pro")}
            asg = []
            for r in rows:
                e = self._effective(evals.get(r["msg"], []), now)
                mine = [x for x in ex if x["msg"] == r["msg"]]
                a = {"msg": r["msg"], "parent": r["parent"], "depth": r["depth"], "task": r["task"], "to": r["to_name"], "kind": "task", "delivery": r["delivery"],
                     "deadline_status": self._status(r, now), "attempts": [self._attempt(x) for x in att if x["msg"] == r["msg"]], "confidence": r["confidence"], "run": r["run"],
                     "session": r["session"], "created": r["created"], "first_delivery": r["first_delivery"], "answered_at": r["answered_at"], "deadline": r["deadline"],
                     "orphan": bool(r["orphan"]), "evaluation": self._ev_view(e) if e else None}
                if r["task_form"]:
                    a["task_form"] = r["task_form"]
                if r["msg"] in fo:
                    a["failover"] = fo[r["msg"]]
                if mine:
                    a["own_tokens"] = self._tok(mine)
                asg.append(a)
                if r["orphan"]:
                    gaps.append({"kind": "orphan", "count": 1, "msg": r["msg"]})
                elif r["first_delivery"] is not None and r["confidence"] != "verified":
                    gaps.append({"kind": "unverified_join", "count": 1, "msg": r["msg"]})
            tasks = []
            for r in rows:
                if r["task"] not in tasks:
                    tasks.append(r["task"])
            sess = sorted({r["session"] for r in rows if r["session"]})
            so = []
            for s in sess:
                for reason, flag in (("plain_prompt", 0), ("overlap", 1)):
                    xs = self._q(conn, "SELECT * FROM exchange WHERE session=? AND msg IS NULL AND overlap=?", (s, flag))
                    if xs:
                        so.append({"session": s, "reason": reason, "tokens": self._tok(xs)})
            unavailable, note = self._unavailable(conn, sess)
            usage = {"own": {a["msg"]: a["own_tokens"] for a in asg if "own_tokens" in a}, "inclusive": self._tok(ex), "session_only": so, "unavailable": unavailable, "residual_note": note}
            waits = [self._wait(w, rows) for w in self._q(conn, "SELECT * FROM wait WHERE target_msg IN (%s) ORDER BY start, wait" % qm, msgs)]
            spans = self._spans(conn, msgs)
            res = {"root": root, "tasks": tasks, "assignments": asg, "waits": waits, "spans": spans, "usage": usage, "evaluation": next((a["evaluation"] for a in asg if a["msg"] == root), None), "gaps": gaps}
            return res, []
        return self._run("task_trace", {"id": id}, fn)

    @staticmethod
    def _attempt(x):
        return {"n": x["n"], "via": x["via"], "t": x["t"], "run": x["run"], "session": x["session"], "dup": bool(x["dup"]), "verified": bool(x["verified"])}

    @staticmethod
    def _wait(w, rows):
        tgt = next((r for r in rows if r["msg"] == w["target_msg"]), None)
        shadow = bool(tgt and tgt["answered_at"] is not None and w["end"] is not None and tgt["answered_at"] <= w["end"] and w["returned"] != "reply")
        return {"wait": w["wait"], "caller": w["caller"], "task": w["task"], "start": w["start"], "end": w["end"], "outcome": w["outcome"], "exit": w["exit"], "returned": w["returned"],
                "returned_msg": w["returned_msg"], "shadowed_reply": shadow}

    def _spans(self, conn, msgs):
        qm = ",".join("?" * len(msgs))
        links = {}
        for r in conn.execute("SELECT span, linked_span FROM span_link ORDER BY span, linked_span").fetchall():
            links.setdefault(r[0], []).append(r[1])
        out = []
        for s in self._q(conn, "SELECT * FROM span WHERE msg IN (%s) ORDER BY span" % qm, msgs):
            d = {"span": s["span"], "kind": s["kind"], "msg": s["msg"], "session": s["session"], "parent_span": s["parent_span"], "start": s["start"], "end": s["end"], "quality": s["quality"], "links": links.get(s["span"], [])}
            if s["name"]:
                d["name"] = s["name"]
            out.append(d)
        return out

    @staticmethod
    def _unavailable(conn, sessions):
        got = 0
        for s in sessions:
            c = conn.execute("SELECT total_tokens FROM session_counter WHERE session=?", (s,)).fetchone()
            if c and c[0] is not None:
                seen = conn.execute("SELECT COALESCE(SUM(input+output+cache_write_5m+cache_write_1h+cache_read), 0) FROM exchange WHERE session=?", (s,)).fetchone()[0]
                got += max(0, c[0] - seen)
        have = any(conn.execute("SELECT 1 FROM session_counter WHERE session=?", (s,)).fetchone() for s in sessions)
        return ({"tokens": got}, "session counter minus transcript sum") if have else (None, "no session counter in source")

    def _find_root(self, conn, ident):
        if re.match(r"^m-[0-9a-f]{10}$", ident):
            r = conn.execute("SELECT root FROM assignment WHERE msg=?", (ident,)).fetchone()
            if r:
                return r[0]
            for e in conn.execute("SELECT data FROM event WHERE msg=? AND action='msg_created'", (ident,)).fetchall():
                p = json.loads(e[0]).get("parent")
                r = p and conn.execute("SELECT root FROM assignment WHERE msg=?", (p,)).fetchone()
                if r:
                    return r[0]
            raise AuditError("not_found", "no such message", "id")
        roots = sorted({r[0] for r in conn.execute("SELECT root FROM assignment WHERE task=?", (ident,)).fetchall()})
        if not roots:
            raise AuditError("not_found", "no such task id", "id")
        if len(roots) > 1:
            raise AuditError("bad_request", "task id matches several root assignments", ",".join(roots[:20]))
        return roots[0]

    # ------------------------------------------------------------------ metrics
    def query_metrics(self, since, until=None, group_by=None, professional=None, revision=None, model=None, task_class=None, direct=None):
        self._guard()
        gb = [g for g in (group_by.split(",") if isinstance(group_by, str) else (group_by or [])) if g and g != "none"]
        for g in gb:
            if g not in GROUPS:
                raise AuditError("bad_request", "unknown group", "group_by")
        flt = {"professional": professional, "revision": revision, "model": model, "task_class": task_class, "direct": direct}
        a = {"since": since, "until": until}

        def fn(conn, now, cov):
            c, extra = self._context(conn, a, now, cov, flt)
            res = {"window": {"from": c["since"], "to": c["until"], "cutoff": c["cutoff"], "event_time": "per_card"}, "group_by": gb or ["none"], "cards": self._cards(c, c["A"], c["X"])}
            if gb:
                res["groups"] = self._groups(c, gb)
            return res, extra
        return self._run("query_metrics", a, fn)

    def _context(self, conn, a, now, cov, flt):
        since, until = self._window(a, now, True)
        cut = min(until, now)
        sess = {}
        for r in self._q(conn, "SELECT r.session, r.revision, p.name FROM run r LEFT JOIN professional p ON p.pro=r.pro WHERE r.session IS NOT NULL ORDER BY r.started"):
            sess[r["session"]] = (r["name"], r["revision"])
        rev = {r["run"]: r["revision"] for r in self._q(conn, "SELECT run, revision FROM run")}
        A = self._q(conn, "SELECT * FROM assignment WHERE (created>=? AND created<?) OR (answered_at>=? AND answered_at<?) OR (deadline>=? AND deadline<?)", (since, until, since, until, since, until))
        X = self._q(conn, "SELECT * FROM exchange WHERE t>=? AND t<?", (since, cut))
        byid = {x["msg"]: x for x in A}
        first_model = {}
        for x in sorted(X, key=lambda x: x["t"] or 0):
            if x["msg"] and x["model"]:
                first_model.setdefault(x["msg"], x["model"])
        for r in A:
            r["professional"], r["revision"], r["model"] = r["to_name"], rev.get(r["run"]), first_model.get(r["msg"])
            r["task_class"], r["direct"], r["day"] = "unclassified", "delegated" if r["parent"] else "direct", _day(r["created"])
        for x in X:
            a_ = byid.get(x["msg"])
            nm, rv = (a_["professional"], a_["revision"]) if a_ else sess.get(x["session"], (None, None))
            x["professional"], x["revision"] = nm, rv
            x["task_class"], x["direct"], x["day"] = "unclassified", (a_["direct"] if a_ else "session_only"), _day(x["t"]) if x["t"] is not None else None
        extra = []
        for k, v in flt.items():
            if not v:
                continue
            if k == "professional":
                names = {r["name"] for r in self._q(conn, "SELECT name FROM professional WHERE pro=? OR name=?", (v, v))}
                if not names:
                    extra.append({"kind": "unknown_filter", "count": 1})
                A, X = [y for y in A if y["professional"] in names], [y for y in X if y["professional"] in names]
                continue
            A, X = [y for y in A if y.get(k) == v], [y for y in X if y.get(k) == v]
        evals = self._evals(conn)
        srcs = {s["source"]: s["quality"] for s in cov["sources"]}
        counters = self._q(conn, "SELECT session, total_tokens FROM session_counter")
        allx = {r["session"]: r["n"] for r in self._q(conn, "SELECT session, SUM(input+output+cache_write_5m+cache_write_1h+cache_read) AS n FROM exchange GROUP BY session")}
        spans = self._q(conn, "SELECT msg, kind, start, end FROM span WHERE kind IN ('model_request','tool') AND msg IS NOT NULL")
        waits = self._q(conn, "SELECT start FROM wait")
        c = {"since": since, "until": until, "cutoff": cut, "now": now, "A": A, "X": X, "evals": evals, "srcs": srcs, "counters": counters, "all_x": allx, "spans": spans, "conn": conn, "waits": waits}
        return c, extra

    def _groups(self, c, gb):
        attr = ["direct" if g == "direct_vs_delegated" else g for g in gb]
        keys = {}
        for r in c["A"]:
            if c["since"] <= r["created"] < c["until"]:
                keys.setdefault(tuple(r.get(g) for g in attr), None)
        for r in c["A"]:
            keys.setdefault(tuple(r.get(g) for g in attr), None)
        for r in c["X"]:
            keys.setdefault(tuple(r.get(g) for g in attr), None)
        out = []
        for k in sorted(keys, key=lambda t: tuple("" if v is None else str(v) for v in t)):
            A = [r for r in c["A"] if tuple(r.get(g) for g in attr) == k]
            X = [r for r in c["X"] if tuple(r.get(g) for g in attr) == k]
            n = sum(1 for r in A if c["since"] <= r["created"] < c["until"])
            out.append({"key": {g: ("unknown" if v is None else v) for g, v in zip(gb, k)}, "n": n, "small_sample": n < 5, "cards": self._cards(c, A, X)})
        return out

    def _sq(self, c, *names):
        return [{"source": n, "quality": c["srcs"][n]} for n in names if n in c["srcs"]]

    @staticmethod
    def _conf(rows):
        d = {"verified": 0, "mapped": 0, "unassigned": 0}
        for r in rows:
            d[r["confidence"]] = d.get(r["confidence"], 0) + 1
        return d

    def _eligible_eval(self, c, A):
        return [r for r in A if r["delivery"] == "answered" and r["answered_at"] is not None and c["since"] <= r["answered_at"] < c["until"] and r["answered_at"] <= c["cutoff"]]

    def _verdict_counts(self, c, group):
        counts = dict.fromkeys(VOCAB, 0)
        for r in group:
            e = self._effective(c["evals"].get(r["msg"], []), c["cutoff"])
            counts[e["status"] if e else "unreviewed"] += 1
        return counts

    def _deadline_sets(self, c, A):
        pop = [r for r in A if (c["since"] <= r["created"] < c["until"]) or (r["deadline"] is not None and c["since"] <= r["deadline"] < c["until"])]
        due = [r for r in pop if r["deadline"] is not None and c["since"] <= r["deadline"] <= c["cutoff"] and r["deadline"] < c["until"] and not r["cancelled"]]
        nyd = [r for r in A if c["since"] <= r["created"] < c["until"] and r["deadline"] is not None and r["deadline"] > c["cutoff"] and not r["cancelled"]]
        return pop, due, nyd

    @staticmethod
    def _missed(due, cut):
        return [r for r in due if not (r["answered_at"] is not None and r["answered_at"] <= r["deadline"] and r["answered_at"] <= cut)]

    def _durations(self, c, A):
        done = self._eligible_eval(c, A)
        q = [r["first_delivery"] - r["created"] for r in done if r["first_delivery"] is not None]
        rl = [r["answered_at"] - r["first_delivery"] for r in done if r["first_delivery"] is not None]
        per = {}
        for s in c["spans"]:
            per.setdefault(s["msg"], []).append(s)
        act = [active_seconds(per[r["msg"]]) for r in done if r["msg"] in per]
        return done, q, rl, act

    def _cards(self, c, A, X):
        since, until, cut = c["since"], c["until"], c["cutoff"]
        win = [r for r in A if since <= r["created"] < until]
        roots, kids = [r for r in win if not r["parent"]], [r for r in win if r["parent"]]
        refusals = {}
        for e in c["conn"].execute("SELECT data FROM event WHERE action='msg_refused' AND t>=? AND t<?", (since, until)).fetchall():
            rs = str(json.loads(e[0]).get("reason", "unknown"))[:32]
            refusals[rs] = refusals.get(rs, 0) + 1
        cards = {"assigned_work": {"assignments": len(win), "root_tasks": len(roots), "child_assignments": len(kids), "refusals_by_reason": refusals, "coverage": [len(win), len(win)], "confidence": self._conf(win),
                                   "event_time": "assignment created", "cutoff": cut, "source_quality": self._sq(c, "emitter", "backfill")}}
        done = self._eligible_eval(c, A)
        units, conc = {}, 0
        for key, unit, grp in (("root_tasks", "root task", roots), ("child_assignments", "child assignment", kids)):
            el = [r for r in done if bool(r["parent"]) == (key == "child_assignments")]
            counts = self._verdict_counts(c, el)
            concl = sum(counts[k] for k in CONCLUSIVE)
            conc += concl
            units[key] = {"unit": unit, "completed_eligible": len(el), "conclusive": concl, "counts": counts, "coverage": [concl, len(el)], "pass_rate": [counts["passed"], concl] if concl else None,
                          "not_completed": sum(1 for r in grp if r["delivery"] != "answered")}
        cards["evaluation"] = dict(units, coverage=[conc, len(done)], confidence=self._conf(done), event_time="answered_at", cutoff=cut, source_quality=self._sq(c, "emitter", "backfill"))
        pop, due, nyd = self._deadline_sets(c, A)

        def miss(group_due, group_nyd):
            m = self._missed(group_due, cut)
            return {"eligible_elapsed": len(group_due), "missed": len(m), "late_answers_among_missed": sum(1 for r in m if r["answered_at"] is not None and r["answered_at"] <= cut),
                    "rate": [len(m), len(group_due)], "cancelled": None, "not_yet_due": len(group_nyd)}
        cards["deadline_miss"] = {"all": miss(due, nyd), "root_tasks": miss([r for r in due if not r["parent"]], [r for r in nyd if not r["parent"]]),
                                  "child_assignments": miss([r for r in due if r["parent"]], [r for r in nyd if r["parent"]]), "rule": "deadline vs answered_at; the overdue flag is evidence only",
                                  "coverage": [sum(1 for r in pop if r["deadline"] is not None), len(pop)], "confidence": self._conf(due), "event_time": "deadline", "cutoff": cut,
                                  "source_quality": self._sq(c, "emitter", "backfill")}
        dn, q, rl, act = self._durations(c, A)
        cards["durations"] = {"queue": dist(q, len(dn)), "reply_latency_from_first_delivery": dist(rl, len(dn)), "active_model_tool_time": dist(act, len(dn)), "percentile": "nearest rank",
                              "coverage": [len(q), len(dn)], "confidence": self._conf(dn), "event_time": "answered_at", "cutoff": cut, "source_quality": self._sq(c, "emitter", "backfill", "transcript")}
        cards["accounted_tokens"] = self._token_card(c, X)
        return cards

    def _token_card(self, c, X):
        linked = [x for x in X if x["msg"] and x["confidence"] == "verified"]
        sess = [x for x in X if x not in linked]
        tot = self._tok(X)
        cw = {"5m": tot["cache_write_5m"], "1h": tot["cache_write_1h"], "unsplit": tot["cache_write_unsplit"]}
        root_of = {r["msg"]: r["root"] for r in c["conn"].execute("SELECT msg, root FROM assignment").fetchall()}
        incl = {}
        for x in linked:
            r = root_of.get(x["msg"], x["msg"])
            incl[r] = incl.get(r, 0) + self._tok([x])["total"]
        conf = {"verified": 0, "mapped": 0, "unassigned": 0}
        for x in X:
            conf[x["confidence"]] = conf.get(x["confidence"], 0) + self._tok([x])["total"]
        seen_sessions = {x["session"] for x in X}
        unavail = None
        if c["counters"]:
            unavail = sum(max(0, k["total_tokens"] - c["all_x"].get(k["session"], 0)) for k in c["counters"] if k["total_tokens"] is not None and k["session"] in seen_sessions) or 0
        return {"total_unique": tot["total"], "exchanges": len(X), "linked_task": self._tok(linked)["total"], "session_only": self._tok(sess)["total"], "unavailable": unavail,
                "unavailable_note": "no session counter in source" if not c["counters"] else "session counter minus transcript sum",
                "by_category": {"input_tokens": tot["input"], "output_tokens": tot["output"], "cache_creation_input_tokens": tot["cache_write"], "cache_read_input_tokens": tot["cache_read"]},
                "root_inclusive": incl, "cache_write_split": cw, "cost_usd_api_equivalent": tot["cost_usd_api_equivalent"], "price_state": tot["price_state"], "unpriced_tokens": tot["unpriced_tokens"],
                "unpriced_share": [tot["unpriced_tokens"], tot["total"]], "coverage": [sum(1 for x in X if not x["no_key"]), len(X)], "confidence": conf, "event_time": "exchange timestamp", "cutoff": c["cutoff"],
                "source_quality": self._sq(c, "transcript", "session_counter")}

    # ------------------------------------------------------------------ explain
    def explain_metric(self, metric, since, until=None, professional=None, revision=None, model=None, task_class=None, direct=None):
        self._guard()
        if metric not in METRICS:
            raise AuditError("bad_request", "unknown metric", "metric")
        flt = {"professional": professional, "revision": revision, "model": model, "task_class": task_class, "direct": direct}
        a = {"since": since, "until": until}

        def fn(conn, now, cov):
            c, extra = self._context(conn, a, now, cov, flt)
            d = getattr(self, "_m_" + metric)(c)
            ids = d.get("ids", [])
            res = {"metric": metric, "definition": d["definition"], "numerator": d["num"], "denominator": d["den"], "unit": d["unit"], "exclusions": d.get("excl", []),
                   "included_ids": ids[:MAX_IDS], "included_total": len(ids), "coverage_note": d["note"], "source_quality": self._sq(c, *d["src"])}
            return res, extra
        return self._run("explain_metric", a, fn)

    def _win(self, c):
        return [r for r in c["A"] if c["since"] <= r["created"] < c["until"]]

    def _ids(self, rows):
        return sorted({r["msg"] for r in rows})

    def _m_assigned_work(self, c):
        w = self._win(c)
        return {"definition": "distinct assignment ids created in the window", "num": len(w), "den": None, "unit": "assignments", "ids": self._ids(w), "src": ("emitter", "backfill"),
                "note": "created time observed for %d of %d" % (len(w), len(w)), "excl": [{"reason": "replies_and_notes", "count": 0}]}

    def _m_evaluation_coverage(self, c):
        done = self._eligible_eval(c, c["A"])
        n = sum(1 for r in done if (self._effective(c["evals"].get(r["msg"], []), c["cutoff"]) or {"status": "unreviewed"})["status"] in CONCLUSIVE)
        return {"definition": "completed assignments with a conclusive verdict / completed assignments (late answers included)", "num": n, "den": len(done), "unit": "assignments", "ids": self._ids(done),
                "src": ("emitter",), "note": "verdicts effective at the cutoff; unreviewed %d" % (len(done) - n), "excl": [{"reason": "not_completed", "count": sum(1 for r in self._win(c) if r["delivery"] != "answered")}]}

    def _m_pass_rate(self, c):
        done = self._eligible_eval(c, c["A"])
        cnt = self._verdict_counts(c, done)
        concl = sum(cnt[k] for k in CONCLUSIVE)
        return {"definition": "passed / conclusive verdicts (passed, partial, failed)", "num": cnt["passed"], "den": concl or None, "unit": "assignments", "ids": self._ids(done), "src": ("emitter",),
                "note": "%d conclusive of %d completed" % (concl, len(done)), "excl": [{"reason": "inconclusive", "count": cnt["inconclusive"]}, {"reason": "unreviewed", "count": cnt["unreviewed"]}]}

    def _m_deadline_miss_rate(self, c):
        pop, due, nyd = self._deadline_sets(c, c["A"])
        m = self._missed(due, c["cutoff"])
        return {"definition": "assignments with no answer by the deadline / assignments whose deadline has elapsed; late answers count as misses", "num": len(m), "den": len(due), "unit": "assignments",
                "ids": self._ids(due), "src": ("emitter", "backfill"), "note": "deadline and answer time observed for %d of %d" % (len(due), len(due)),
                "excl": [{"reason": "cancelled", "count": 0}, {"reason": "not_yet_due", "count": len(nyd)}]}

    def _dur(self, c, which, definition):
        done, q, rl, act = self._durations(c, c["A"])
        xs = {"queue": q, "reply": rl, "active": act}[which]
        d = dist(xs, len(done))
        ids = self._ids(done)
        return {"definition": definition, "num": d["p50_s"] if d["p50_s"] is not None else 0, "den": d["n"], "unit": "seconds", "ids": ids, "src": ("emitter", "transcript") if which == "active" else ("emitter",),
                "note": "p50 over n; p95 %s s; n %d of %d" % (d["p95_s"], d["n"], d["eligible"]), "excl": [{"reason": "not_measured", "count": d["eligible"] - d["n"]}]}

    def _m_queue_time(self, c):
        return self._dur(c, "queue", "p50 of first delivery minus created, nearest rank, over assignments answered in the window")

    def _m_reply_latency(self, c):
        return self._dur(c, "reply", "p50 of answer time minus first delivery, nearest rank")

    def _m_active_time(self, c):
        return self._dur(c, "active", "p50 of the union of model_request and tool spans per assignment, nearest rank")

    _m_execution_time = _m_active_time

    def _m_accounted_tokens(self, c):
        t = self._tok(c["X"])
        return {"definition": "tokens of unique exchanges in the window (input + output + cache write + cache read)", "num": t["total"], "den": None, "unit": "tokens", "ids": self._ids([x for x in c["X"] if x["msg"]]),
                "src": ("transcript", "session_counter"), "note": "%d exchanges counted" % len(c["X"]), "excl": [{"reason": "no_key", "count": sum(1 for x in c["X"] if x["no_key"])}]}

    def _m_cache_read_share(self, c):
        t = self._tok(c["X"])
        return {"definition": "cache read tokens / (input + cache write + cache read tokens) of unique exchanges", "num": t["cache_read"], "den": t["input"] + t["cache_write"] + t["cache_read"], "unit": "tokens",
                "ids": self._ids([x for x in c["X"] if x["msg"]]), "src": ("transcript",), "note": "%d of %d exchanges counted" % (len(c["X"]), len(c["X"])), "excl": [{"reason": "no_key", "count": sum(1 for x in c["X"] if x["no_key"])}]}

    def _m_cost_estimate(self, c):
        pr = [x for x in c["X"] if x["price_state"] != "unpriced" and x["cost_micros"] is not None]
        return {"definition": "USD API-equivalent sum from the pinned price table / exchanges priced", "num": round(sum(x["cost_micros"] for x in pr) / 1e6, 6), "den": len(pr), "unit": "usd_api_equivalent",
                "ids": self._ids([x for x in c["X"] if x["msg"]]), "src": ("transcript",), "note": "%d of %d exchanges priced; a lower bound when cache writes are unsplit" % (len(pr), len(c["X"])),
                "excl": [{"reason": "unpriced", "count": len(c["X"]) - len(pr)}]}

    def _m_unpriced_share(self, c):
        t = self._tok(c["X"])
        return {"definition": "unpriced tokens / all tokens of unique exchanges", "num": t["unpriced_tokens"], "den": t["total"], "unit": "tokens", "ids": self._ids([x for x in c["X"] if x["msg"]]),
                "src": ("transcript",), "note": "%d exchanges counted" % len(c["X"]), "excl": []}

    def _m_attribution_share(self, c):
        v = [x for x in c["X"] if x["msg"] and x["confidence"] == "verified"]
        m = sum(self._tok([x])["total"] for x in c["X"] if x["confidence"] == "mapped")
        return {"definition": "tokens of verified assignments / all tokens (mapped and unassigned shown apart)", "num": self._tok(v)["total"], "den": self._tok(c["X"])["total"], "unit": "tokens",
                "ids": self._ids(v), "src": ("transcript", "emitter"), "note": "mapped tokens %d; unassigned %d" % (m, self._tok([x for x in c["X"] if x["confidence"] == "unassigned"])["total"]), "excl": []}

    def _m_sessions_with_work(self, c):
        w = self._win(c)
        have = {r["session"] for r in w if r["session"]}
        allr = c["conn"].execute("SELECT COUNT(DISTINCT session) FROM run WHERE session IS NOT NULL AND started<?", (c["until"],)).fetchone()[0]
        return {"definition": "distinct sessions with at least one assignment / sessions that existed", "num": len(have), "den": allr, "unit": "sessions", "ids": self._ids(w), "src": ("emitter",),
                "note": "%d of %d assignments name a session" % (sum(1 for r in w if r["session"]), len(w)), "excl": []}

    def _m_active_days(self, c):
        days = {r["day"] for r in self._win(c)} | {x["day"] for x in c["X"] if x["day"]}
        return {"definition": "distinct UTC days with an assignment or exchange / days in the window", "num": len(days), "den": max(1, math.ceil((c["cutoff"] - c["since"]) / 86400)), "unit": "days",
                "ids": self._ids(self._win(c)), "src": ("emitter", "transcript"), "note": "window cut at the cutoff", "excl": []}

    def _m_work_per_active_week(self, c):
        w = self._win(c)
        weeks = {datetime.datetime.fromtimestamp(r["created"], datetime.timezone.utc).isocalendar()[:2] for r in w}
        return {"definition": "assignments / active ISO weeks", "num": len(w), "den": len(weeks) or None, "unit": "assignments", "ids": self._ids(w), "src": ("emitter",), "note": "%d active weeks" % len(weeks), "excl": []}

    def _m_direct_delegated_mix(self, c):
        w = self._win(c)
        d = [r for r in w if r["parent"]]
        return {"definition": "delegated assignments (parent set) / all assignments; direct are the rest", "num": len(d), "den": len(w), "unit": "assignments", "ids": self._ids(w), "src": ("emitter",),
                "note": "direct %d, delegated %d" % (len(w) - len(d), len(d)), "excl": []}

    def _m_reply_rate(self, c):
        dl = [r for r in self._win(c) if r["first_delivery"] is not None]
        an = [r for r in dl if r["answered_at"] is not None and r["answered_at"] <= c["cutoff"]]
        return {"definition": "assignments answered / assignments delivered", "num": len(an), "den": len(dl), "unit": "assignments", "ids": self._ids(dl), "src": ("emitter",),
                "note": "delivered %d of %d created" % (len(dl), len(self._win(c))), "excl": [{"reason": "never_delivered", "count": len(self._win(c)) - len(dl)}]}

    def _m_failures_by_reason(self, c):
        by = {}
        for e in c["conn"].execute("SELECT action, data FROM event WHERE action IN ('msg_failed','msg_refused') AND t>=? AND t<?", (c["since"], c["until"])).fetchall():
            k = "%s:%s" % (e[0][4:], str(json.loads(e[1]).get("reason", "unknown"))[:24])
            by[k] = by.get(k, 0) + 1
        w = self._win(c)
        return {"definition": "msg_failed and msg_refused events / assignments; reasons in the note", "num": sum(by.values()), "den": len(w) or None, "unit": "count", "ids": self._ids(w), "src": ("emitter",),
                "note": ("; ".join("%s %d" % kv for kv in sorted(by.items())) or "none")[:190], "excl": []}

    def _m_concurrent_occupancy(self, c):
        iv = [(r["first_delivery"], r["answered_at"] if r["answered_at"] is not None else c["cutoff"]) for r in c["A"] if r["first_delivery"] is not None]
        ev = sorted([(a, 1) for a, b in iv] + [(b, -1) for a, b in iv], key=lambda e: (e[0], e[1]))
        cur = peak = 0
        for _, d in ev:
            cur += d
            peak = max(peak, cur)
        span = sum(max(0, min(b, c["cutoff"]) - max(a, c["since"])) for a, b in iv)
        mean = round(span / max(1e-9, c["cutoff"] - c["since"]), 4)
        return {"definition": "maximum assignments in flight (delivered, not yet answered); time-weighted mean in the note", "num": peak, "den": None, "unit": "assignments", "ids": self._ids([r for r in c["A"] if r["first_delivery"] is not None]),
                "src": ("emitter",), "note": "time-weighted mean %s over the window" % mean, "excl": []}

    def _m_delivery_retries(self, c):
        w = [r["msg"] for r in self._win(c) if r["first_delivery"] is not None]
        n = dup = 0
        if w:
            for a in self._q(c["conn"], "SELECT n, dup FROM attempt WHERE msg IN (%s)" % ",".join("?" * len(w)), w):
                n += 1 if a["n"] > 1 else 0
                dup += 1 if a["dup"] else 0
        return {"definition": "deliveries beyond the first per message / assignments delivered", "num": n, "den": len(w), "unit": "count", "ids": sorted(w), "src": ("emitter",), "note": "dup flags %d (evidence only)" % dup, "excl": []}

    def _m_context_footprint(self, c):
        xs = [x for x in c["X"] if x["turn"] is not None]
        fp = [x["input"] + x["cache_write_5m"] + x["cache_write_1h"] + x["cache_read"] for x in xs]
        return {"definition": "largest per-exchange input + cache write + cache read tokens; p50 in the note", "num": max(fp) if fp else 0, "den": len(xs), "unit": "tokens", "ids": self._ids([x for x in xs if x["msg"]]),
                "src": ("transcript",), "note": "p50 %s over %d exchanges with a turn index" % (nearest_rank(fp, 50), len(xs)), "excl": [{"reason": "no_turn_index", "count": len(c["X"]) - len(xs)}]}

    # ------------------------------------------------------------------ status
    def status(self):
        """Store version, snapshot, coverage and the degraded flag (the `pros audit status` data), as one envelope-shaped answer."""
        self._guard()

        def fn(conn, now, cov):
            n = lambda t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            return {"events": n("event"), "assignments": n("assignment"), "exchanges": n("exchange"), "evaluations": n("evaluation"), "runs": n("run")}, []
        try:
            env = self._run("list_professionals", {}, fn)
        except AuditError as e:  # before the first import: one line from the CLI, never a traceback
            return e.envelope()
        except sqlite3.Error as e:
            return AuditError("store_unavailable", "store error: %s" % type(e).__name__).envelope()
        env["method"] = "status"
        return env
