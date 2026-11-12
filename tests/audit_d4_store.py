"""Test scaffold for audit D4, on the real store since step E: store.open_store creates it (current schema), the release fixture goes
through the spool and importer.drain (so audit_evaluation events project the way they do live), derive.rebuild builds the derived
tables; only the exchange rows are hand-written (usage reading has its own tests). Private tokens are planted so export tests can prove
they never leave: see PLANTED."""
import io
import json
import os
import sqlite3
import sys

import helpers

sys.path.insert(0, os.path.join(helpers.HERE, "..", "plugins", "professionals", "lib"))
from pros_audit import derive  # noqa: E402
from pros_audit import evaluation as ev  # noqa: E402
from pros_audit import importer  # noqa: E402
from pros_audit import store  # noqa: E402

CF = os.path.join(helpers.HERE, "fixtures", "audit", "contracts")
DDL = os.path.join(helpers.HERE, "..", "docs", "spec", "audit", "schemas", "store-v1.sql")
BODY_CANARIES = ["CANARY-ROOT-BODY-41", "CANARY-CHILD-BODY-42", "CANARY-ROOT-REPLY-43", "CANARY-TOOL-ARG-44", "CANARY-TOOL-RESULT-45", "CANARY-PATH-46", "CANARY-RESUME-BODY-47"]
# User-chosen tokens that the allowlist lets into the store and that a redacted export must therefore hide.
PLANTED = ["CANARY-TASK-67", "canary-local-66", "CANARY-EVAL-61", "CANARY-RUBRIC-63", "CANARY-CRIT-64", "CANARY-NOTE-65", "canary-tmux-68", "CANARY-CFG-69"]
ROOT, CHILD, LOCAL = "m-0000000001", "m-0000000005", "m-00000000a9"
LOCAL_ROOT = "m-00000000a9"


def make_store(home):
    return store.open_store(home)


def load_release(conn, with_evaluation_events=False):
    """Events from the release scenario (planted tokens substituted), plus hand-built assignment/exchange/professional rows."""
    with open(os.path.join(CF, "events", "release_scenario.jsonl"), encoding="utf-8") as f:
        text = f.read().replace("fx-root", "CANARY-TASK-67").replace("pro-system-designer--000001", "pro-canary-tmux-68--000001")
    events = [json.loads(x) for x in text.splitlines() if x.strip()]
    events.append({"v": 1, "id": "a1b2c3d4e5f60718-90", "t": 1800000070.0, "action": "spawn", "actor": "system", "origin": "live", "pro": "p-0000000000a3", "pro_name": "canary-local-66",
                   "run": "r-0000000000b9", "session": "00000000-0000-4000-8000-000000000009",
                   "data": {"kind": "fresh", "live_count": 1, "origin": "local", "parent_session": None, "revision": "333333333333", "scope": "project", "tmux": "pro-canary-tmux-68--000009"}})
    events.append({"v": 1, "id": "a1b2c3d4e5f60718-91", "t": 1800000071.0, "action": "msg_created", "actor": "user", "origin": "live", "msg": LOCAL_ROOT, "pro": "p-0000000000a3",
                   "data": {"body_bytes": 10, "chain_len": 1, "deadline": None, "from_role": "user", "kind": "task", "parent": None, "project": "0a1b2c3d4e5f", "task": "CANARY-TASK-67", "to": "canary-local-66"}})
    events.append({"v": 1, "id": "a1b2c3d4e5f60718-92", "t": 1800000072.0, "action": "audit_config", "actor": "operator", "origin": "live", "data": {"setting": "capture_mode", "value": "CANARY-CFG-69"}})
    home = os.path.dirname(os.path.dirname(conn.execute("PRAGMA database_list").fetchone()[2]))
    spool = os.path.join(home, "audit", "spool")
    os.makedirs(spool, mode=0o700, exist_ok=True)
    path = os.path.join(spool, "fixture-1.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            if e["action"] == "audit_evaluation" and not with_evaluation_events:
                continue
            f.write(json.dumps(e, sort_keys=True) + "\n")
    os.utime(path, (1, 1))  # settled: the importer deletes it after the import
    r = importer.drain(home)
    assert r.get("events") and not r.get("conflicts"), r
    derive.rebuild(conn)
    conn.execute("BEGIN IMMEDIATE")
    ex = [("k1", "00000000-0000-4000-8000-000000000002", ROOT, "verified", "claude-sonnet-4-5", 1800000002.3, 50, 20, 0, 0, 30),
          ("k2", "00000000-0000-4000-8000-000000000002", ROOT, "verified", "claude-sonnet-4-5", 1800000042.2, 10, 30, 0, 0, 0),
          ("k3", "00000000-0000-4000-8000-000000000001", CHILD, "verified", "claude-sonnet-4-5", 1800000001.6, 30, 50, 0, 0, 0),
          ("k4", "00000000-0000-4000-8000-000000000002", None, "mapped", "claude-sonnet-4-5", 1800000090.0, 10, 20, 0, 0, 0)]
    for k, s, m, c, mo, t, i, o, w5, w1, cr in ex:
        conn.execute("INSERT INTO exchange (key, session, msg, confidence, model, t, input, output, cache_write_5m, cache_write_1h, cache_read, price_state, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,'priced','transcript')",
                     (k, s, m, c, mo, t, i, o, w5, w1, cr))
    conn.execute("UPDATE store_state SET generation = generation + 1, pricing_table='fx-2027-01', pricing_dated='2027-01-01'")
    conn.execute("COMMIT")


def add_rubrics(home):
    ev.rubric_add(home, "architecture-review", "r1", [("C1", True), ("C2", True), ("C3", True)])
    ev.rubric_add(home, "CANARY-RUBRIC-63", "r1", [("C1", True), ("CANARY-CRIT-64", True), ("C3", False)])


def cli(home, *argv, now=None, stdin=None):
    from pros_audit import cli as c
    o, e = io.StringIO(), io.StringIO()
    code = c.run(list(argv), home, o, e, stdin, now)
    return code, o.getvalue(), e.getvalue()
