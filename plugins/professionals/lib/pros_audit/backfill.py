"""Historical backfill (events.md section 4): events with origin=backfill built from the records that survive in $PRO_HOME.

Sources: run/msgs/m-*.json (created, injected, answered, read, failed, overdue) and history/<name>.jsonl (spawn start and end rows).
Rules: ids are deterministic (a rerun inserts nothing new); a message or session that already has live events is skipped (live wins);
fields the source lacks are left out or filled with an explicit placeholder (revision 000000000000, live_count 0, tmux pro-<name>); the importer's
purge mark and tombstones are honoured (same Run class); one `pre_audit` gap per run states the coverage cutoff = the first live event.
History files that hold the rotation limit (500 rows) get a `history_rotation` gap. `task_counts` is never read. Counts only, no text is stored."""
import glob
import hashlib
import json
import os
import time
import uuid

from . import emit as E
from . import importer as I
from . import store as S

ROTATION = 500
UNKNOWN_REV = "000000000000"


def _eid(key, n=1):
    return "%s-%d" % (hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16], n)


def pro_id(name, reg):
    e = ((reg or {}).get("entries") or {}).get(name) or {}
    return e.get("pro_id") or "p-" + hashlib.sha1(("%s:%s" % ("folder" if e.get("kind") == "folder" else "marketplace", name)).encode()).hexdigest()[:12]


def _origin(name, reg):
    return "folder" if (((reg or {}).get("entries") or {}).get(name) or {}).get("kind") == "folder" else "marketplace"


def run_id(hist_id, t):
    return "r-" + E.hash12("%s:%s" % (hist_id, t))


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _history(home):
    out = {}
    for p in sorted(glob.glob(os.path.join(str(home), "history", "*.jsonl"))):
        rows = []
        try:
            with open(p, encoding="utf-8") as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(r, dict):
                        rows.append(r)
        except OSError:
            continue
        out[os.path.basename(p)[:-6]] = rows
    return out


def _runs(hist, reg):
    """Run records minted from history start rows: {name, sid, start, end, reason, kind, parent_session, run}."""
    out = []
    for name, rows in hist.items():
        open_runs = []
        for r in rows:
            if r.get("ev") == "start" and isinstance(r.get("t"), (int, float)) and r.get("id"):
                par = r.get("forked_from") or r.get("resumed_from")
                run = {"name": name, "sid": r["id"], "start": r["t"], "end": None, "reason": None, "run": run_id(r["id"], r["t"]),
                       "kind": "fork" if r.get("forked_from") else ("resume" if r.get("resumed_from") else "fresh"), "parent": par}
                open_runs.append(run)
                out.append(run)
            elif r.get("ev") == "end":
                for run in reversed(open_runs):
                    if run["sid"] == r.get("id") and run["end"] is None:
                        run["end"], run["reason"] = r.get("t"), r.get("reason")
                        break
    return out


def _failed_reason(txt):
    t = str(txt or "")
    if "deadline" in t:
        return "deadline_before_delivery"
    if "stopped" in t or "recipient" in t:
        return "recipient_stopped"
    if "expired" in t:
        return "expired_unread"
    if "input box" in t or "deliver" in t:
        return "paste_failed"
    return "other"


def build(home, live_msgs, live_sessions):
    """-> (events, info). Pure: reads files only."""
    reg = _read_json(os.path.join(str(home), "registry.json"))
    hist = _history(home)
    runs = _runs(hist, reg)
    evs, info = [], {"history_rotation": [], "min_t": None, "msgs": 0, "runs": 0}

    def add(key, t, action, actor, prov, data, **env):
        ev = {"v": 1, "id": _eid(key), "t": round(float(t), 3), "action": action, "actor": actor, "origin": "backfill", "provenance": prov, "data": data}
        ev.update({k: v for k, v in env.items() if v is not None or k == "session"})
        evs.append(ev)
        info["min_t"] = ev["t"] if info["min_t"] is None else min(info["min_t"], ev["t"])

    for name, rows in hist.items():
        if len(rows) >= ROTATION:
            info["history_rotation"].append((name, min([r["t"] for r in rows if isinstance(r.get("t"), (int, float))] or [0])))
    for r in runs:
        if r["sid"] in live_sessions:
            continue
        info["runs"] += 1
        par = r["parent"] if r["parent"] else None
        add("spawn:%s" % r["run"], r["start"], "spawn", "system", "history",
            {"kind": r["kind"], "live_count": 0, "origin": _origin(r["name"], reg), "parent_session": par, "revision": UNKNOWN_REV, "scope": "project", "tmux": "pro-" + r["name"]},
            pro=pro_id(r["name"], reg), pro_name=r["name"], run=r["run"], session=r["sid"])
        if r["end"] is not None:
            reason = r["reason"] if r["reason"] in ("stopped", "reaped", "crashed", "failed_spawn", "unknown") else "unknown"
            add("end:%s" % r["run"], r["end"], "session_end", "system", "history", {"answered": 0, "failed": 0, "reason": reason, "timeout": 0},
                pro=pro_id(r["name"], reg), pro_name=r["name"], run=r["run"], session=r["sid"])
    for p in sorted(glob.glob(os.path.join(str(home), "run", "msgs", "m-*.json"))):
        m = _read_json(p)
        if not isinstance(m, dict) or not m.get("id") or m["id"] in live_msgs or not isinstance(m.get("created"), (int, float)):
            continue
        info["msgs"] += 1
        mid, to, frm = m["id"], m.get("to"), m.get("from")
        actor = "user" if frm == "user" else ("system" if frm == "system" else "professional")
        task, form = E.task_token(m.get("task"))
        d = {"body_bytes": len(str(m.get("body", "")).encode("utf-8")), "chain_len": len(m.get("chain") or []), "deadline": m.get("deadline"), "from_role": actor,
             "kind": m.get("kind", "task"), "parent": m.get("parent"), "project": E.hash12(m.get("project", "")), "task": task, "to": to}
        if actor == "professional":
            d["from_pro"] = frm
        if form:
            d["task_form"] = form
        topro = pro_id(to, reg) if to and to != "user" else None
        add("created:" + mid, m["created"], "msg_created", actor, "msgs", d, msg=mid, pro=topro, pro_name=to if topro else None)
        t_inj = m.get("injected_at")
        if isinstance(t_inj, (int, float)):
            cand = [r for r in runs if r["name"] == to and r["start"] - 1 <= t_inj <= (r["end"] or 9e12) + 1]
            one = cand[0] if len({r["sid"] for r in cand}) == 1 else None
            via = m.get("via") if m.get("via") in ("paste", "stop-hook") else "paste"
            add("injected:" + mid, t_inj, "msg_injected", "pump", "msgs", {"attempt": max(int(m.get("attempts") or 1), 1), "dup": bool(m.get("dup")), "tmux": "pro-%s" % to, "via": via},
                msg=mid, pro=topro, pro_name=to, run=one["run"] if one else None, session=one["sid"] if one else None)
        if isinstance(m.get("answered_at"), (int, float)):
            add("answered:" + mid, m["answered_at"], "msg_answered", "professional", "msgs", {"answered_by": "reply", "reply_msg": None}, msg=mid, pro=topro, pro_name=to)
        if isinstance(m.get("read_at"), (int, float)):
            add("read:" + mid, m["read_at"], "msg_read", "user" if to == "user" else "professional", "msgs", {"reader": to}, msg=mid, pro=topro, pro_name=to)
        if m.get("state") == "failed":
            t_f = max(x for x in (m["created"], t_inj if isinstance(t_inj, (int, float)) else 0) if x)
            add("failed:" + mid, t_f, "msg_failed", "system", "msgs", {"attempts": int(m.get("attempts") or 0), "reason": _failed_reason(m.get("fail_reason"))}, msg=mid, pro=topro, pro_name=to)
        if m.get("overdue") and isinstance(m.get("deadline"), (int, float)):
            add("overdue:" + mid, m["deadline"], "msg_overdue", "pump", "msgs", {"deadline": m["deadline"], "observed_by": "pump"}, msg=mid, pro=topro, pro_name=to)
    return evs, info


def backfill(home):
    """Import the surviving history into the store. Idempotent. Returns a summary dict."""
    with I.import_lock(home) as got:
        if not got:
            return {"skipped": "another import is running"}
        conn = S.open_store(home)
        try:
            live_msgs = {r[0] for r in conn.execute("SELECT DISTINCT msg FROM event WHERE origin='live' AND action='msg_created' AND msg IS NOT NULL")}
            live_sessions = {r[0] for r in conn.execute("SELECT DISTINCT session FROM event WHERE origin='live' AND action='spawn' AND session IS NOT NULL")}
            evs, info = build(home, live_msgs, live_sessions)
            batch = "bf-" + uuid.uuid4().hex[:10]
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO import_batch (batch, started) VALUES (?, ?)", (batch, time.time()))
            conn.execute("COMMIT")
            run = I.Run(conn, batch)
            bad = 0
            for ev in evs:
                if I.check(ev):
                    bad += 1
                    continue
                run.add(ev, None)
            run.flush()
            first_live = conn.execute("SELECT min(t) FROM event WHERE origin='live' AND action NOT IN ('audit_import','audit_gap','audit_config')").fetchone()[0]
            nb, lo = conn.execute("SELECT count(*), min(t) FROM event WHERE origin='backfill'").fetchone()
            conn.execute("BEGIN IMMEDIATE")
            if nb and first_live is not None:  # the coverage cutoff: everything before the first live event is reconstructed
                conn.execute("DELETE FROM gap WHERE kind='pre_audit' AND source='msgs'")
                conn.execute("INSERT INTO gap (kind, source, from_t, to_t, count, batch) VALUES ('pre_audit', 'msgs', ?, ?, ?, ?)", (lo, first_live, nb, batch))
            for name, t0 in info["history_rotation"]:
                row = conn.execute("SELECT 1 FROM gap WHERE kind='history_rotation' AND source='history' AND from_t IS ? AND count=?", (t0, ROTATION)).fetchone()
                if not row:
                    conn.execute("INSERT INTO gap (kind, source, from_t, to_t, count, batch) VALUES ('history_rotation', 'history', ?, NULL, ?, ?)", (t0, ROTATION, batch))
            if run.skipped:
                run.gap("purged_skipped", "msgs", run.t_lo, run.t_hi, run.skipped)
            fin = time.time()
            conn.execute("UPDATE import_batch SET finished=?, events=?, duplicates=?, conflicts=?, purged_skipped=?, gaps=?, last_seq=? WHERE batch=?",
                         (fin, run.events, run.dups, run.conflicts, run.skipped, run.gaps, run.last_seq, batch))
            conn.execute("UPDATE store_state SET generation = generation + 1")
            conn.execute("COMMIT")
            from . import usage as U
            U.refresh_source_meta(conn)
            return {"batch": batch, "events": run.events, "duplicates": run.dups, "purged_skipped": run.skipped, "invalid": bad, "msgs": info["msgs"], "runs": info["runs"], "cutoff": first_live}
        finally:
            conn.close()
