"""Test scaffolding for the audit query layer (step D3): builds an audit store from the contract fixtures.

Not the importer (D1) or the usage reader (D2): a small fold of tests/fixtures/audit/contracts/events/release_scenario.jsonl plus the usage transcripts into
every table of store-v1.sql, so the query layer can be tested against the frozen goldens before those parts land. Stdlib only."""
import datetime
import json
import os
import re
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.join(HERE, "fixtures", "audit")
CF = os.path.join(FX, "contracts")
DDL = os.path.join(HERE, "..", "docs", "spec", "audit", "schemas", "store-v1.sql")
T0 = 1800000000.0
NOW = 1800000200.0
HEADER = re.compile(r'^(?:Stop hook feedback:\n|\s*<pasted_content id="[0-9a-f]+">\n)?\[pro-msg id=(m-[0-9a-f]{10}) ')
SOURCE_META = ("CREATE TABLE IF NOT EXISTS source_meta (source TEXT PRIMARY KEY, quality TEXT NOT NULL, from_t REAL, to_t REAL, rows INTEGER NOT NULL, versions TEXT)")


def jl(*parts):
    with open(os.path.join(CF, *parts), encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def jf(*parts):
    with open(os.path.join(CF, *parts), encoding="utf-8") as f:
        return json.load(f)


def epoch(ts):
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc).timestamp()


def new_db(path=":memory:"):
    db = sqlite3.connect(path, isolation_level=None)
    db.row_factory = sqlite3.Row
    with open(DDL, encoding="utf-8") as f:
        db.executescript(f.read())
    db.execute("INSERT INTO schema_version VALUES (1, 0, 'v1')")
    db.execute(SOURCE_META)
    return db


def read_transcripts(price):
    """-> (exchanges, headers seen as {(session, msg)}, spans) following events.md sections 5 and 6 for the fixture."""
    tdir = os.path.join(FX, "usage", "transcripts")
    order = ["00000000-0000-4000-8000-000000000002", "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-0000000000f1"]
    seen, ex, headers, spans, raw = set(), [], set(), [], 0
    for sid in order:
        cur, prev, tools, first = None, None, {}, set()
        with open(os.path.join(tdir, sid + ".jsonl"), encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                t, m = epoch(r["timestamp"]), r["message"]
                c = m["content"]
                if r["type"] == "user":
                    prev = t
                    if isinstance(c, str):
                        h = HEADER.match(c)
                        cur = h.group(1) if h else None
                        if h and not sid.endswith("f1"):
                            headers.add((sid, cur))
                if r["type"] == "assistant":
                    raw += 1
                    key = (m["id"], r["requestId"])
                    if key not in seen:
                        seen.add(key)
                        u = m["usage"]
                        cw = u["cache_creation_input_tokens"]
                        x = {"key": "%s:%s" % key, "session": sid, "msg": cur, "confidence": "verified" if cur else "unassigned", "model": m["model"], "t": max(t, T0 + 1.0) if not cur else t,
                             "input": u["input_tokens"], "output": u["output_tokens"], "cache_write_5m": cw, "cache_write_1h": 0, "cache_write_unsplit": cw, "cache_read": u["cache_read_input_tokens"]}
                        rate = price["models"][m["model"]]
                        micros = x["input"] * rate["input"] + x["output"] * rate["output"] + cw * rate["cache_write_5m"] + x["cache_read"] * rate["cache_read"]
                        x["cost_micros"] = int(round(micros))
                        ex.append(x)
                    if m["id"] not in first and not sid.endswith("f1"):
                        first.add(m["id"])
                        spans.append({"session": sid, "msg": cur, "kind": "model_request", "start": prev, "end": t})
                if isinstance(c, list):
                    for b in c:
                        if b["type"] == "tool_use":
                            tools[b["id"]] = (t, b["name"])
                        if b["type"] == "tool_result" and not sid.endswith("f1"):
                            a, name = tools[b["tool_use_id"]]
                            spans.append({"session": sid, "msg": cur, "kind": "tool", "start": a, "end": t, "name": name})
    return ex, headers, spans, raw


def load_release(db=None, generation=31):
    """The release-scenario store: events, derived tables, exchanges, evaluations, spans, source facts. Generation pinned to the golden's 31."""
    db = db or new_db()
    price = jf("pricing", "fx-2027-01.json")
    events = jl("events", "release_scenario.jsonl")
    ex, headers, tspans, raw = read_transcripts(price)
    db.execute("BEGIN")
    db.execute("INSERT INTO import_batch (batch, started, finished, events) VALUES ('b1', ?, ?, ?)", (T0 + 130, T0 + 130, len(events)))
    for e in events:
        db.execute("INSERT INTO event (id, payload_sha, t, action, actor, origin, pro, run, session, msg, data, batch) VALUES (?,?,?,?,?,?,?,?,?,?,?,'b1')",
                   (e["id"], "x", e["t"], e["action"], e["actor"], e["origin"], e.get("pro"), e.get("run"), e.get("session"), e.get("msg"), json.dumps(e["data"], sort_keys=True)))
    names, asg, atts, waits, runs, evals = {}, {}, [], {}, {}, []
    for e in events:
        d, m, a = e["data"], e.get("msg"), e["action"]
        if a == "spawn":
            names[e["pro"]] = e["pro_name"]
            runs[e["run"]] = {"pro": e["pro"], "session": e["session"], "kind": d["kind"], "started": e["t"], "ended": None, "end_reason": None, "revision": d["revision"], "origin": d["origin"], "name": e["pro_name"]}
        elif a == "session_end":
            runs[e["run"]].update(ended=e["t"], end_reason=d["reason"])
        elif a == "msg_created" and d["kind"] == "task":
            asg[m] = {"msg": m, "parent": d["parent"], "task": d["task"], "to_pro": e.get("pro"), "to_name": d["to"], "from_role": d["from_role"], "created": e["t"], "deadline": d["deadline"],
                      "first_delivery": None, "answered_at": None, "run": None, "session": None, "attempt_verified": False}
        elif a == "msg_injected":
            asg[m]["first_delivery"] = asg[m]["first_delivery"] or e["t"]
            ok = (e["session"], m) in headers
            atts.append((m, d["attempt"], d["via"], e["t"], e["run"], e["session"], int(d["dup"]), int(ok)))
            if ok:
                asg[m].update(run=e["run"], session=e["session"], attempt_verified=True)
        elif a == "msg_answered" and m in asg:
            asg[m]["answered_at"] = e["t"]
        elif a == "wait_start":
            waits[d["wait"]] = {"wait": d["wait"], "caller": d["caller"], "task": d["task"], "target_msg": d["target_msg"], "start": e["t"]}
        elif a == "wait_end":
            waits[d["wait"]].update(end=e["t"], outcome=d["outcome"], exit=d["exit"], returned=d["returned"], returned_msg=d["returned_msg"], dur_ms=d["dur_ms"])
        elif a == "audit_evaluation":
            evals.append(d)
    for pro, name in names.items():
        rs = [r for r in runs.values() if r["pro"] == pro]
        last = max(e["t"] for e in events if e.get("pro") == pro)
        db.execute("INSERT INTO professional VALUES (?,?,?,?,?,0)", (pro, name, rs[0]["origin"], min(r["started"] for r in rs), last))
        for r in rs:
            db.execute("INSERT OR IGNORE INTO revision VALUES (?,?,?)", (pro, r["revision"], r["started"]))
    for rid, r in runs.items():
        db.execute("INSERT INTO run VALUES (?,?,?,?,?,?,?,?,?)", (rid, r["pro"], r["session"], r["kind"], r["started"], r["ended"], r["end_reason"], None, r["revision"]))
    for m, a in asg.items():
        root, depth, cur = m, 0, a
        while cur["parent"]:
            cur, depth = asg[cur["parent"]], depth + 1
            root = cur["msg"]
        delivery = "answered" if a["answered_at"] else ("injected" if a["first_delivery"] else "queued")
        db.execute("INSERT INTO assignment (msg,root,parent,depth,task,to_pro,to_name,from_role,created,deadline,first_delivery,answered_at,delivery,run,session,confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (m, root, a["parent"], depth, a["task"], a["to_pro"], a["to_name"], a["from_role"], a["created"], a["deadline"],
                                                                                        a["first_delivery"], a["answered_at"], delivery, a["run"], a["session"], "verified" if a["attempt_verified"] else "unassigned"))
    db.executemany("INSERT INTO attempt (msg, n, via, t, run, session, dup, verified) VALUES (?,?,?,?,?,?,?,?)", atts)
    for w in waits.values():
        db.execute("INSERT INTO wait VALUES (?,?,?,?,?,?,?,?,?,?,?)", (w["wait"], w["caller"], w["task"], w["target_msg"], w["start"], w["end"], w["outcome"], w["exit"], w["returned"], w["returned_msg"], w["dur_ms"]))
    for i, x in enumerate(ex):
        db.execute("INSERT INTO exchange VALUES (?,?,?,?,0,?,?,?,?,?,?,?,?,?,?,'priced',?,0,'transcript')",
                   (x["key"], x["session"], x["msg"], x["confidence"], x["model"], x["t"], x["input"], x["output"], x["cache_write_5m"], x["cache_write_1h"], x["cache_write_unsplit"], x["cache_read"], i, x["cost_micros"], price["table_id"]))
    sid = iter(range(1, 99))
    ids = {}
    mk = lambda: "sp-%010d" % next(sid)
    root_sess = "00000000-0000-4000-8000-000000000002"
    for s in tspans:
        if s["msg"] is None:
            continue
        s["span"] = ids[id(s)] = mk()
    deleg = None
    for s in tspans:
        if s.get("span") and s["kind"] == "model_request" and s["msg"] == "m-0000000001" and deleg is None:
            deleg = s["span"]
    spans = [(s["span"], s["kind"], s["msg"], s["session"], None, s["start"], s["end"], s.get("name")) for s in tspans if s["msg"]]
    child = asg["m-0000000005"]
    dsp = mk()
    spans.append((dsp, "delegation", "m-0000000005", root_sess, deleg, child["created"], child["answered_at"], None))
    for w in sorted(waits.values(), key=lambda w: w["start"]):
        spans.append((mk(), "wait", w["target_msg"], None, None, w["start"], w["end"], None))
    for sp in spans:
        db.execute("INSERT INTO span (span, kind, msg, session, parent_span, start, end, name) VALUES (?,?,?,?,?,?,?,?)", sp)
    tool = [s for s in spans if s[1] == "tool"][0][0]
    last_model = [s for s in spans if s[1] == "model_request" and s[2] == "m-0000000001"][-1][0]
    for l in sorted([tool, dsp]):
        db.execute("INSERT INTO span_link VALUES (?,?)", (last_model, l))
    for d in evals:
        db.execute("INSERT INTO evaluation (eval,target_kind,target,status,evaluator_kind,evaluator,evaluator_version,rubric,rubric_version,effective_at,supersedes,criteria,evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                   (d["eval"], d["target_kind"], d["target"], d["status"], d["evaluator_kind"], d["evaluator"], d["evaluator_version"], d["rubric"], d["rubric_version"], d["effective_at"], d["supersedes"], json.dumps(d["criteria"])))
    for row in (("emitter", "observed", T0, T0 + 130, len(events), '["0.3.0"]'), ("transcript", "observed", T0 + 1, T0 + 42, raw, '["2.1.322"]'), ("session_counter", "missing", None, None, 0, None)):
        db.execute("INSERT INTO source_meta VALUES (?,?,?,?,?,?)", row)
    db.execute("UPDATE store_state SET generation=?, pricing_table=?, pricing_dated=?", (generation, price["table_id"], price["dated"]))
    db.execute("COMMIT")
    return db
