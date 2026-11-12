"""Audit step C: the frozen v1 contracts (docs/spec/audit/) and their shared fixtures (tests/fixtures/audit/contracts/).

Stdlib only. Validates fixtures against the JSON schemas with tests/audit_schema.py, folds the release-scenario event stream
into the B3 golden numbers, checks the scenario expectations, the redacted-export sample, the store DDL and the migration rule."""
import glob
import json
import os
import re
import sqlite3
import sys
import unittest

import helpers

sys.path.insert(0, helpers.HERE)
sys.path.insert(0, os.path.join(helpers.HERE, "..", "research"))
import audit_schema as sch  # noqa: E402
import metrics_ref  # noqa: E402

FX = os.path.join(helpers.HERE, "fixtures", "audit")
CF = os.path.join(FX, "contracts")
DOCS = os.path.join(helpers.HERE, "..", "docs", "spec", "audit")
EVENT = sch.load("event-v1.json")
QUERY = sch.load("query-v1.json")
MANIFEST = sch.load("export-manifest-v1.json")


def rd(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def jl(*parts):
    with open(os.path.join(CF, *parts), encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def jf(*parts):
    with open(os.path.join(CF, *parts), encoding="utf-8") as f:
        return json.load(f)


def canaries():
    with open(os.path.join(FX, "usage", "expected.json"), encoding="utf-8") as f:
        return json.load(f)["canaries"]


def fold(events):
    """Minimal fold of an event stream into assignment facts: enough to check the contract, not the importer."""
    a = {}
    for e in events:
        d, m = e["data"], e.get("msg")
        if e["action"] == "msg_created" and d["kind"] == "task":
            a[m] = {"created": e["t"], "deadline": d["deadline"], "parent": d["parent"], "injects": [], "answered": None, "dups": 0}
        elif e["action"] == "msg_injected":
            a[m]["injects"].append(e["t"])
            a[m]["dups"] += 1 if d["dup"] else 0
        elif e["action"] == "msg_answered" and m in a:
            a[m]["answered"] = e["t"]
    return a


class TestSchemaFiles(unittest.TestCase):
    def test_every_action_documented_and_in_schema(self):
        actions = set(EVENT["properties"]["action"]["enum"])
        with open(os.path.join(DOCS, "events.md"), encoding="utf-8") as f:
            text = f.read()
        sec = text.split("## 3.")[1].split("## 4.")[0]
        documented = set(re.findall(r"^\| `([a-z_]+)` \|", sec, re.M))
        self.assertEqual(actions, documented)
        then_actions = {s["if"]["properties"]["action"]["const"] for s in EVENT["allOf"] if "action" in s["if"]["properties"]}
        self.assertEqual(actions, then_actions)

    def test_no_free_text_fields(self):
        """Every string in an event is an enum, const or has a pattern: nothing can carry prose, a path or a body."""
        def walk(s, where):
            s = EVENT["$defs"][s["$ref"].rsplit("/", 1)[1]] if "$ref" in s else s
            for alt in s.get("anyOf", []):
                walk(alt, where)
            t = s.get("type")
            ts = t if isinstance(t, list) else [t]
            if "string" in ts and not ("enum" in s or "const" in s or "pattern" in s):
                self.fail("free-text string at %s" % where)
            for k, v in s.get("properties", {}).items():
                walk(v, where + "." + k)
            if "items" in s:
                walk(s["items"], where + "[]")
        for rule in EVENT["allOf"]:
            if "then" in rule and "properties" in rule["then"]:
                walk(rule["then"]["properties"]["data"], rule["if"]["properties"].get("action", {}).get("const", "?"))

    def test_schemas_are_closed(self):
        for name, s in (("event", EVENT), ("manifest", MANIFEST), ("query", QUERY)):
            self.assertIs(s["additionalProperties"], False, name)

    def test_store_ddl_and_migration_rollback(self):
        db = sqlite3.connect(":memory:", isolation_level=None)
        with open(os.path.join(sch.SCHEMAS, "store-v1.sql"), encoding="utf-8") as f:
            db.executescript(f.read())
        db.execute("INSERT INTO schema_version VALUES (1, 0, 'v1')")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for need in ("event", "assignment", "attempt", "wait", "exchange", "evaluation", "run", "audit_log", "import_batch", "span", "span_link", "purge_mark", "event_conflict", "event_tombstone", "store_state"):
            self.assertIn(need, tables)
        db.execute("INSERT INTO event (id,payload_sha,t,action,actor,origin,data) VALUES ('x-1','h',1,'a','b','live','{}')")
        db.execute("INSERT OR IGNORE INTO event (id,payload_sha,t,action,actor,origin,data) VALUES ('x-1','h',1,'a','b','live','{}')")
        self.assertEqual(db.execute("SELECT count(*) FROM event").fetchone()[0], 1)
        # migration rule: one transaction, DDL included; a failing step leaves version 1 intact and readable
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("ALTER TABLE assignment ADD COLUMN task_class TEXT")
            db.execute("INSERT INTO schema_version VALUES (2, 1, 'v2')")
            db.execute("INSERT INTO no_such_table VALUES (1)")
            db.execute("COMMIT")
        except sqlite3.Error:
            db.execute("ROLLBACK")
        self.assertEqual(db.execute("SELECT max(version) FROM schema_version").fetchone()[0], 1)
        cols = [r[1] for r in db.execute("PRAGMA table_info(assignment)")]
        self.assertNotIn("task_class", cols)
        self.assertEqual(db.execute("SELECT count(*) FROM event").fetchone()[0], 1)

    def test_validator_rejects(self):
        self.assertTrue(sch.validate({"a": 1}, {"type": "object", "additionalProperties": False}))
        self.assertTrue(sch.validate("x", {"type": "string", "pattern": "^y$"}))


class TestEventFixtures(unittest.TestCase):
    def test_valid_streams(self):
        seen = set()
        for name in ("release_scenario.jsonl", "other_actions.jsonl"):
            ids = set()
            for e in jl("events", name):
                self.assertEqual(sch.validate(e, EVENT), [], e["id"])
                self.assertNotIn(e["id"], ids)
                ids.add(e["id"])
                seen.add(e["action"])
        for path in sorted(glob.glob(os.path.join(CF, "scenarios", "*.jsonl"))):
            for e in [json.loads(x) for x in rd(path).splitlines() if x.strip()]:
                self.assertEqual(sch.validate(e, EVENT), [], os.path.basename(path))
                seen.add(e["action"])
        self.assertEqual(seen, set(EVENT["properties"]["action"]["enum"]), "every action has a valid example")

    def test_invalid_events_rejected(self):
        bad = jf("events", "invalid.json")
        self.assertGreaterEqual(len(bad), 10)
        for case in bad:
            self.assertTrue(sch.validate(case["event"], EVENT), case["why"])

    def test_no_canary_or_path_in_any_event(self):
        blob = ""
        for path in glob.glob(os.path.join(CF, "events", "*.jsonl")) + glob.glob(os.path.join(CF, "scenarios", "*.jsonl")):
            blob += rd(path)
        for c in canaries():
            self.assertNotIn(c, blob)
        self.assertNotRegex(blob, r"/home/|/tmp/|@[a-z]+\.")

    def test_release_scenario_folds_to_golden(self):
        ev = jl("events", "release_scenario.jsonl")
        a = fold(ev)
        roots = [m for m, x in a.items() if not x["parent"]]
        self.assertEqual((len(a), len(roots)), (2, 1))
        late = [m for m, x in a.items() if x["answered"] > x["deadline"]]
        self.assertEqual(late, roots, "root answered after its deadline (a miss), child on time")
        golden = json.loads(rd(os.path.join(FX, "golden", "cards.json")))
        self.assertEqual(golden["deadline_miss"]["all"]["rate"], [len(late), len(a)])
        q = sorted(x["injects"][0] - x["created"] for x in a.values())
        self.assertEqual(round(metrics_ref.nearest_rank(q, 50), 2), golden["durations"]["queue"]["p50_s"])
        waits = [(e["data"]["outcome"], e["data"]["exit"]) for e in ev if e["action"] == "wait_end"]
        self.assertEqual(waits, [("pending", 3), ("note_only", 5), ("note_only", 5)])
        evals = [e for e in ev if e["action"] == "audit_evaluation"]
        rows = [{"id": e["data"]["eval"], "target": {"id": e["data"]["target"]}, "supersedes": e["data"]["supersedes"],
                 "evaluator": {"kind": e["data"]["evaluator_kind"]}, "effective_at": e["data"]["effective_at"], "status": e["data"]["status"]} for e in evals]
        self.assertEqual(metrics_ref.effective(rows, "m-0000000001")["status"], "partial")
        self.assertEqual(metrics_ref.effective(rows, "m-0000000005")["status"], "passed")

    def test_msg_injected_is_stamped(self):
        for e in jl("events", "release_scenario.jsonl"):
            if e["action"] == "msg_injected":
                self.assertIn("run", e)
                self.assertIn("session", e)

    def test_scenarios_match_expectations(self):
        exp = jf("scenarios", "expected.json")
        self.assertEqual(set(exp), {os.path.basename(p)[:-6] for p in glob.glob(os.path.join(CF, "scenarios", "*.jsonl"))})
        a = fold(jl("scenarios", "dup_redelivery.jsonl"))["m-0000000001"]
        self.assertEqual((len(a["injects"]), a["dups"]), (exp["dup_redelivery"]["attempts"], exp["dup_redelivery"]["dup_flags"]))
        self.assertEqual(len(fold(jl("scenarios", "restart_resend.jsonl"))["m-0000000001"]["injects"]), exp["restart_resend"]["attempts"])
        ov = fold(jl("scenarios", "overlap.jsonl"))
        self.assertTrue(max(ov["m-0000000001"]["created"], ov["m-0000000002"]["created"]) < min(x["answered"] for x in ov.values()))
        mp = jl("scenarios", "missing_parent.jsonl")
        created = {e["msg"] for e in mp if e["action"] == "msg_created"}
        self.assertTrue(all(e["data"]["parent"] not in created for e in mp if e["action"] == "msg_created"))
        self.assertEqual(sorted(created), exp["missing_parent"]["orphans"])
        w = [e for e in jl("scenarios", "stuck_target_wait.jsonl") if e["action"] == "wait_end"][0]["data"]
        self.assertEqual((w["outcome"], w["exit"]), (exp["stuck_target_wait"]["wait_outcome"], exp["stuck_target_wait"]["exit"]))
        sr = jl("scenarios", "stale_first_reply.jsonl")
        end = [e for e in sr if e["action"] == "wait_end"][0]["data"]
        reply = [e for e in sr if e["action"] == "msg_created" and e["msg"] == end["returned_msg"]][0]
        self.assertEqual(reply["data"]["parent"], exp["stale_first_reply"]["belongs_to_assignment"])
        self.assertNotEqual(reply["data"]["parent"], [e for e in sr if e["action"] == "wait_start"][0]["data"]["target_msg"])
        runs = [e for e in jl("scenarios", "fork_resume_run_ids.jsonl")]
        self.assertEqual(len({e["run"] for e in runs}), exp["fork_resume_run_ids"]["runs"])
        self.assertEqual(len({e["session"] for e in runs}), exp["fork_resume_run_ids"]["sessions"])
        hr = jl("scenarios", "history_rotation.jsonl")
        first_live = min(e["t"] for e in hr if e["action"] == "spawn")
        pre = [e for e in hr if e["action"] == "audit_gap" and e["data"]["kind"] == "pre_audit"][0]
        self.assertLessEqual(pre["data"]["to_t"], first_live)
        cap = jl("scenarios", "cap_refusal.jsonl")
        self.assertEqual([e["data"]["reason"] for e in cap if e["action"] == "msg_refused"], ["cap_hourly"])


class TestQueryFixtures(unittest.TestCase):
    def envelopes(self):
        for p in sorted(glob.glob(os.path.join(CF, "query", "*.json"))):
            if os.path.basename(p).startswith("error_"):
                continue
            yield os.path.basename(p), json.loads(rd(p))

    def test_all_methods_have_a_valid_envelope(self):
        methods = set()
        for name, env in self.envelopes():
            self.assertEqual(sch.validate(env, QUERY), [], name)
            methods.add(env["method"])
        self.assertEqual(methods, set(QUERY["properties"]["method"]["enum"]))

    def test_envelope_negatives(self):
        env = jf("query", "query_metrics.json")
        self.assertTrue(sch.validate(dict(env, extra=1), QUERY))
        self.assertTrue(sch.validate({k: v for k, v in env.items() if k != "coverage"}, QUERY))
        bad = json.loads(json.dumps(env))
        del bad["result"]["cards"]["durations"]
        self.assertTrue(sch.validate(bad, QUERY))
        tr = jf("query", "task_trace.json")
        tr["result"]["assignments"][0]["delivery"] = "done"
        self.assertTrue(sch.validate(tr, QUERY))
        self.assertTrue(sch.validate({"api_version": 1, "method": "list_runs", "snapshot": env["snapshot"], "coverage": env["coverage"], "units": {}, "result": {"items": [], "page": {"limit": 9999, "order": "x", "next": None}}}, QUERY))

    def test_error_envelope_shape(self):
        e = jf("query", "error_bad_request.json")
        self.assertEqual(set(e["error"]), {"code", "message", "field"})

    def test_metrics_equal_golden_cards(self):
        golden = json.loads(rd(os.path.join(FX, "golden", "cards.json")))
        cards = json.loads(json.dumps(jf("query", "query_metrics.json")["result"]["cards"]))
        # the B3 golden is the pre-v1.1 reference: compare its core, minus the contract-only blocks (common block, spans, cost, cancelled)
        for k, c in cards.items():
            for x in ("coverage", "source_quality", "confidence", "event_time", "cutoff"):
                del c[x]
        del cards["assigned_work"]["refusals_by_reason"]
        for u in ("root_tasks", "child_assignments"):
            del cards["evaluation"][u]["not_completed"]
        for u in ("all", "root_tasks", "child_assignments"):
            self.assertIsNone(cards["deadline_miss"][u].pop("cancelled"), "cancellation is not observable in v1")
            cards["deadline_miss"][u]["cancelled"] = 0
        del cards["durations"]["active_model_tool_time"]
        for x in ("cache_write_split", "cost_usd_api_equivalent", "price_state", "unpriced_tokens", "unpriced_share"):
            del cards["accounted_tokens"][x]
        golden = json.loads(json.dumps(golden))
        del golden["durations"]["active_model_tool_time"]
        for k in ("assigned_work", "evaluation", "deadline_miss", "durations", "accounted_tokens"):
            self.assertEqual(cards[k], golden[k], k)
        w = jf("query", "query_metrics.json")["result"]["window"]
        self.assertGreaterEqual(w["cutoff"], 1800000120.0, "the operator verdict (effective 120 s) must be at or before the cutoff")

    def test_explain_equals_card(self):
        ex = jf("query", "explain_deadline_miss_rate.json")["result"]
        card = jf("query", "query_metrics.json")["result"]["cards"]["deadline_miss"]["all"]["rate"]
        self.assertEqual([ex["numerator"], ex["denominator"]], card)

    def test_trace_tokens_140_80_220_30(self):
        with open(os.path.join(FX, "usage", "expected.json"), encoding="utf-8") as f:
            t = json.load(f)["tokens"]
        r = jf("query", "task_trace.json")["result"]
        own = {a["msg"]: a["own_tokens"]["total"] for a in r["assignments"]}
        self.assertEqual((own["m-0000000001"], own["m-0000000005"]), (t["root_task_own"], t["child_task_own"]))
        self.assertEqual(r["usage"]["inclusive"]["total"], t["trace_total"])
        self.assertEqual(r["usage"]["session_only"][0]["tokens"]["total"], t["root_session_only"])
        for a in r["assignments"]:
            x = a["own_tokens"]
            self.assertEqual(x["total"], x["input"] + x["output"] + x["cache_write"] + x["cache_read"])

    def test_three_statuses_are_separate(self):
        r = jf("query", "task_trace.json")["result"]["assignments"][0]
        self.assertEqual((r["delivery"], r["deadline_status"], r["evaluation"]["status"]), ("answered", "late", "partial"))

    def test_trace_waits_are_not_task_outcomes(self):
        r = jf("query", "task_trace.json")["result"]
        self.assertEqual([w["outcome"] for w in r["waits"]], ["pending", "note_only", "note_only"])
        self.assertTrue(r["waits"][2]["shadowed_reply"])
        self.assertEqual(r["assignments"][0]["delivery"], "answered")


class TestExportFixtures(unittest.TestCase):
    def test_manifest_valid(self):
        m = jf("export", "manifest.json")
        self.assertEqual(sch.validate(m, MANIFEST), [])
        self.assertTrue(sch.validate(dict(m, redaction=dict(m["redaction"], salt_stored=True)), MANIFEST))
        self.assertTrue(sch.validate(dict(m, mode="everything"), MANIFEST))

    def test_redacted_rows_keep_joins_and_leak_nothing(self):
        rows = jl("export", "assignments.jsonl")
        text = rd(os.path.join(CF, "export", "assignments.jsonl")) + rd(os.path.join(CF, "export", "manifest.json"))
        for c in canaries():
            self.assertNotIn(c, text)
        self.assertNotRegex(text, r"m-[0-9a-f]{10}|fx-root|[0-9a-f]{8}-[0-9a-f]{4}-4")
        ids = {r["assignment"] for r in rows}
        for r in rows:
            self.assertIn(r["root"], ids)
            self.assertTrue(r["parent"] is None or r["parent"] in ids)
        self.assertEqual(sum(r["tokens"] for r in rows), 220)


class TestDocs(unittest.TestCase):
    def test_docs_exist_and_link_schemas(self):
        for n in ("events.md", "query.md", "metrics.md", "privacy.md", "budgets.md"):
            self.assertTrue(os.path.getsize(os.path.join(DOCS, n)) > 1500, n)
        for n in ("event-v1.json", "query-v1.json", "export-manifest-v1.json", "store-v1.sql"):
            self.assertTrue(os.path.exists(os.path.join(sch.SCHEMAS, n)), n)
        with open(os.path.join(DOCS, "events.md"), encoding="utf-8") as f:
            t = f.read()
        for term in ("verified", "mapped", "unassigned", "Adapter strategy", "Task-join confidence policy", "Migration rules"):
            self.assertIn(term, t)


if __name__ == "__main__":
    unittest.main()
