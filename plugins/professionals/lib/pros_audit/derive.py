"""Derived tables (events.md section 5 rule 4): professional, revision, run, assignment, attempt, wait, rebuilt from `event` + `header_seen`.

One transaction, one generation bump. Exchanges and header_seen come from transcripts (usage.py) and are never dropped here.
Confidence: `verified` when a delivery header was seen in the session the delivery names; `mapped` when the delivery names a run and session
without a header; `unassigned` otherwise. A mapped delivery owns no usage. Placeholder zeros from the backfill (revision 000000000000) are not shown as facts."""
import json

from . import store as S

PLACEHOLDER_REV = "000000000000"
INF = 9e15


def _walk(msgs, mid):
    """(root, depth, orphan).  A failover re-run (retry_of, B2) sits in the tree of its first attempt, at the same depth."""
    seen, cur, depth = set(), mid, 0
    while True:
        prev = msgs[cur].get("retry_of")
        if prev and prev in msgs and prev not in seen:
            seen.add(cur)
            cur = prev
            continue
        par = msgs[cur]["parent"]
        if not par:
            return cur, depth, 0
        if par in seen or par not in msgs:
            return cur, depth, 1
        seen.add(cur)
        cur, depth = par, depth + 1


def rebuild(conn):
    """Rebuild all derived tables except exchange. The caller must not hold a transaction."""
    rows = conn.execute("SELECT t, action, actor, origin, pro, pro_name, run, session, msg, data FROM event ORDER BY t, seq").fetchall()
    ev = [(t, a, actor, o, p, pn, r, s, m, json.loads(d)) for t, a, actor, o, p, pn, r, s, m, d in rows]
    prof, revs, runs, msgs, att, waits = {}, {}, {}, {}, {}, {}
    ended = {}
    for t, a, actor, o, p, pn, r, s, m, d in ev:
        if p:
            x = prof.setdefault(p, {"name": None, "origin": None, "first": t, "last": t, "removed": 0})
            x["last"] = t
            if pn:
                x["name"] = pn
            if a == "spawn" and not x["origin"]:
                x["origin"] = d["origin"]
            if a == "registry_change":
                x["origin"] = x["origin"] or d["origin"]
                x["removed"] = 1 if d["change"] in ("remove", "removed") else 0
        rev = d.get("revision") if a in ("spawn", "registry_change") else None
        if p and rev and rev != PLACEHOLDER_REV:
            revs.setdefault((p, rev), t)
        if a == "spawn":
            runs[r] = {"pro": p, "session": s, "kind": d["kind"], "start": t, "end": None, "reason": None, "parent_session": d.get("parent_session"), "rev": None if rev == PLACEHOLDER_REV else rev}
        elif a == "session_end" and r in runs:
            runs[r]["end"], runs[r]["reason"] = t, d["reason"]
            ended.setdefault(s, []).append(t)
        elif a == "msg_created":
            msgs[m] = {"kind": d["kind"], "parent": d["parent"], "task": d["task"], "form": d.get("task_form"), "to": d["to"], "to_pro": p, "from_role": d["from_role"], "created": t,
                       "deadline": d["deadline"], "inj": [], "answered": None, "failed": None, "retry_of": d.get("retry_of")}
        elif m in msgs and a == "msg_injected":
            msgs[m]["inj"].append((t, r, s, d))
        elif m in msgs and a == "msg_answered":
            msgs[m]["answered"] = msgs[m]["answered"] or t
        elif m in msgs and a == "msg_failed":
            msgs[m]["failed"] = msgs[m]["failed"] or t
        elif a == "wait_start":
            waits.setdefault(d["wait"], {})["start"] = (t, d)
        elif a == "wait_end":
            waits.setdefault(d["wait"], {})["end"] = (t, d)
    hdr = {(m, s) for m, s in conn.execute("SELECT msg, session FROM header_seen")}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for tb in ("professional", "revision", "run", "assignment", "attempt", "wait"):
            conn.execute("DELETE FROM " + tb)
        for p, x in prof.items():
            conn.execute("INSERT INTO professional (pro, name, origin, first_seen, last_seen, removed) VALUES (?,?,?,?,?,?)", (p, x["name"] or p, x["origin"] or "unknown", x["first"], x["last"], x["removed"]))
        for (p, rev), t in revs.items():
            conn.execute("INSERT INTO revision (pro, revision, first_seen) VALUES (?,?,?)", (p, rev, t))
        by_sid = {}
        for rid, x in runs.items():
            by_sid.setdefault(x["session"], []).append((x["start"], rid))
        for rid, x in runs.items():
            parent = None
            if x["parent_session"]:
                c = [(t0, r0) for t0, r0 in by_sid.get(x["parent_session"], []) if t0 <= x["start"] and r0 != rid]
                parent = max(c)[1] if c else None
            conn.execute("INSERT INTO run (run, pro, session, kind, started, ended, end_reason, parent_run, revision) VALUES (?,?,?,?,?,?,?,?,?)",
                         (rid, x["pro"] or "", x["session"], x["kind"], x["start"], x["end"], x["reason"], parent, x["rev"]))
        for mid, m in msgs.items():
            if m["kind"] != "task":
                continue
            root, depth, orphan = _walk(msgs, mid)
            first = m["inj"][0] if m["inj"] else None
            if m["answered"] is not None:
                delivery = "answered"
            elif m["failed"] is not None:
                delivery = "failed"
            elif first:
                delivery = "injected"
            else:
                delivery = "queued"
            conf = "unassigned"
            if first and first[2] and first[1]:
                conf = "mapped"
            if any((mid, i[2]) in hdr for i in m["inj"] if i[2]):
                conf = "verified"
            conn.execute("INSERT INTO assignment (msg, root, parent, depth, task, task_form, to_pro, to_name, from_role, created, deadline, first_delivery, answered_at, delivery, run, session, confidence, orphan, cancelled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                         (mid, root, m["parent"], depth, m["task"], m["form"], m["to_pro"], m["to"], m["from_role"], m["created"], m["deadline"], first[0] if first else None, m["answered"], delivery,
                          first[1] if first else None, first[2] if first else None, conf, orphan))
        for mid, m in msgs.items():
            for n, (t, r, s, d) in enumerate(m["inj"], 1):
                conn.execute("INSERT INTO attempt (msg, n, via, t, run, session, dup, verified) VALUES (?,?,?,?,?,?,?,?)", (mid, n, d["via"], t, r, s, 1 if d["dup"] else 0, 1 if s and (mid, s) in hdr else 0))
        for w, x in waits.items():
            if "start" not in x:
                continue
            t0, d0 = x["start"]
            t1, d1 = x.get("end", (None, {}))
            conn.execute("INSERT INTO wait (wait, caller, task, target_msg, start, end, outcome, exit, returned, returned_msg, dur_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (w, d0["caller"], d0["task"], d0["target_msg"], t0, t1, d1.get("outcome"), d1.get("exit"), d1.get("returned"), d1.get("returned_msg"), d1.get("dur_ms")))
        conn.execute("UPDATE store_state SET generation = generation + 1")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return {"professionals": len(prof), "runs": len(runs), "assignments": sum(1 for m in msgs.values() if m["kind"] == "task")}
