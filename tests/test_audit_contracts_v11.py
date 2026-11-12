"""Audit step C v1.1: one contract test per finding of the fresh review (specs/037 analyze.md, "Fresh review (step C)").

Each test names its finding. Stdlib only. The reference rules live in tests/audit_contract_ref.py."""
import copy
import datetime
import glob
import json
import os
import re
import sys
import unittest

import helpers

sys.path.insert(0, helpers.HERE)
sys.path.insert(0, os.path.join(helpers.HERE, "..", "research"))
import audit_contract_ref as ref  # noqa: E402
import audit_schema as sch  # noqa: E402
import metrics_ref  # noqa: E402

FX = os.path.join(helpers.HERE, "fixtures", "audit")
CF = os.path.join(FX, "contracts")
DOCS = os.path.join(helpers.HERE, "..", "docs", "spec", "audit")
EVENT = sch.load("event-v1.json")
QUERY = sch.load("query-v1.json")
MANIFEST = sch.load("export-manifest-v1.json")


def rd(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def jf(*parts):
    return json.loads(rd(CF, *parts))


def jl(*parts):
    return [json.loads(x) for x in rd(CF, *parts).splitlines() if x.strip()]


def doc(name):
    return rd(DOCS, name)


def rows(trace):
    return {a["msg"]: a for a in trace["result"]["assignments"]}


class TestP0IdCollision(unittest.TestCase):
    """C-P0-1"""

    def test_writer_is_64_bit_in_schema_and_fixtures(self):
        evt = EVENT["$defs"]["evt"]
        self.assertEqual(sch.validate("a1b2c3d4e5f60718-1", evt), [])
        self.assertTrue(sch.validate("a1b2c3d4-1", evt), "8 hex writer ids are no longer valid")
        for path in glob.glob(os.path.join(CF, "events", "*.jsonl")) + glob.glob(os.path.join(CF, "scenarios", "*.jsonl")):
            for e in jl(os.path.relpath(path, CF)):
                self.assertRegex(e["id"], r"^[0-9a-f]{16}-[0-9]+$", path)

    def test_same_id_different_data_is_a_recorded_conflict(self):
        a, b, a2 = jl("events", "id_conflict.jsonl")
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(a["data"], b["data"])
        db = ref.new_store()
        out = ref.import_events(db, [a, b, a2])
        self.assertEqual(out, {"inserted": 1, "duplicates": 1, "conflicts": 1, "purged_skipped": 0})
        self.assertEqual(db.execute("SELECT count(*) FROM event").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM event_conflict WHERE id=?", (a["id"],)).fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM gap WHERE kind='id_conflict'").fetchone()[0], 1)
        self.assertIn("system-designer", db.execute("SELECT data FROM event_conflict").fetchone()[0], "the second event stays visible")

    def test_conflict_is_describable_in_events(self):
        gap = {"v": 1, "id": "a1b2c3d4e5f60718-900", "t": 1800000600.0, "action": "audit_gap", "actor": "importer", "origin": "live",
               "data": {"kind": "id_conflict", "source": "spool", "from_t": 1800000400.0, "to_t": 1800000401.0, "count": 1}}
        self.assertEqual(sch.validate(gap, EVENT), [])
        imp = {"v": 1, "id": "a1b2c3d4e5f60718-901", "t": 1800000601.0, "action": "audit_import", "actor": "importer", "origin": "live",
               "data": {"batch": "b2", "spool_files": 1, "events": 1, "duplicates": 1, "gaps": 1, "last_seq": 3, "conflicts": 1, "purged_skipped": 0}}
        self.assertEqual(sch.validate(imp, EVENT), [])
        self.assertIn("id_conflict", doc("events.md"))
        self.assertNotIn("INSERT OR IGNORE", doc("events.md").split("3b.")[0])


class TestP1TaskIds(unittest.TestCase):
    """C-P1-1"""

    def test_normalisation_cases(self):
        cases = jf("events", "task_ids.json")
        self.assertGreaterEqual(len(cases), 8)
        kinds = {c["task_form"] for c in cases}
        self.assertEqual(kinds, {None, "hashed"})
        for c in cases:
            self.assertEqual(ref.canon_task(c["raw"]), (c["task"], c["task_form"]), c["raw"])
            self.assertEqual(sch.validate(c["task"], EVENT["$defs"]["task"]), [], "stored form always passes the schema")
            if c["task_form"]:
                self.assertRegex(c["task"], r"^h-[0-9a-f]{12}$")
                self.assertTrue(sch.validate(c["raw"], EVENT["$defs"]["task"]), "raw form would be rejected, hence hashed")
        raws = " ".join(c["raw"] for c in cases)
        for needle in (" ", "/", "@", "מ"):
            self.assertIn(needle, raws, "space, slash, at sign and Hebrew are all covered")

    def test_hashed_events_validate_and_never_carry_the_raw_id(self):
        evs = jl("events", "task_forms.jsonl")
        self.assertGreaterEqual(len(evs), 3)
        raws = [c["raw"] for c in jf("events", "task_ids.json") if c["task_form"]]
        for e in evs:
            self.assertEqual(sch.validate(e, EVENT), [])
            self.assertEqual(e["data"]["task_form"], "hashed")
            self.assertNotIn(e["data"]["task"], raws)
        self.assertTrue(sch.validate(dict(evs[0], data=dict(evs[0]["data"], task_form="raw")), EVENT))

    def test_honest_wording(self):
        t = doc("events.md")
        self.assertIn("user-chosen", t)
        self.assertIn("sensitive", t)
        self.assertNotIn("names/e-mails cannot be represented", t)
        self.assertNotIn("hostnames and e-mail addresses cannot be represented", t)
        p = doc("privacy.md")
        for tok in ("task ids", "evaluator", "rubric", "criterion ids", "pseudonymized"):
            self.assertIn(tok, p)


class TestP1Snapshot(unittest.TestCase):
    """C-P1-2"""

    def test_fixture_snapshot_ids_follow_the_formula(self):
        for p in glob.glob(os.path.join(CF, "query", "*.json")):
            o = json.loads(rd(p))
            if "snapshot" not in o:
                continue
            s = o["snapshot"]
            self.assertEqual(s["id"], ref.snapshot_id(s["schema_version"], s["last_batch"], s["max_seq"], s["generation"], s["pricing"]["table_id"]), p)

    def test_every_write_class_changes_the_id(self):
        db = ref.new_store()
        ev = jl("events", "release_scenario.jsonl")

        def sid():
            g = ref.generation(db)
            mx = db.execute("SELECT coalesce(max(seq),0) FROM event").fetchone()[0]
            return ref.snapshot_id(1, "b1", mx, g, "fx-2027-01"), mx

        ref.import_events(db, ev)
        seen = [sid()]
        evaluation = {"eval": "ev-x", "target_kind": "task", "target": "m-0000000001", "status": "passed", "evaluator_kind": "operator", "evaluator": "owner",
                      "evaluator_version": "1", "rubric": "r", "rubric_version": "1", "effective_at": 1800000100.0, "criteria": []}
        ref.insert_evaluation(db, evaluation)  # verdict: no event row, max_seq unchanged
        seen.append(sid())
        self.assertEqual(seen[0][1], seen[1][1], "max_seq did not move")
        db.execute("INSERT INTO session_counter VALUES ('s', 10, 1, 1800000200.0)")  # session counter read
        seen.append(sid())
        db.execute("INSERT INTO event_tombstone VALUES ('x-1', 1800000300.0, 'correction')")  # correction
        seen.append(sid())
        ref.purge(db, 1800000050.0, 1800000400.0)  # purge
        seen.append(sid())
        ref.import_events(db, [])  # a transcript usage import or derived rebuild is one bump per transaction
        seen.append(sid())
        self.assertEqual(len({s[0] for s in seen}), len(seen), "each write class gives a new snapshot id")

    def test_pricing_table_change_changes_the_id(self):
        self.assertNotEqual(ref.snapshot_id(1, "b1", 5, 5, "fx-2027-01"), ref.snapshot_id(1, "b1", 5, 5, "fx-2027-02"))

    def test_verdicts_go_through_the_importer(self):
        self.assertIn("never writes `evaluation` directly", doc("events.md"))
        self.assertIn("generation", doc("query.md"))
        ddl = rd(os.path.join(ref.SCHEMAS, "store-v1.sql"))
        for t in ("evaluation", "audit_log", "purge_mark", "event_tombstone", "session_counter"):
            self.assertIn("TRIGGER gen_%s_insert" % t, ddl)


class TestP1HttpAccess(unittest.TestCase):
    """C-P1-3"""

    def test_gate_cases(self):
        g = jf("http_gate.json")
        port, token = g["port"], g["token"]
        for c in g["cases"]:
            hdr = {k: v.replace("LOOPBACK", "127.0.0.1") for k, v in c["headers"].items()}
            self.assertEqual(ref.http_gate(c["method"], hdr, port, token), (c["status"], c["code"]), c["why"])

    def test_docs_require_the_token_on_reads_and_the_host_check(self):
        for name in ("query.md", "privacy.md"):
            t = doc(name)
            self.assertIn("Host", t)
            self.assertIn("unauthorized" if name == "query.md" else "http.token", t)
        self.assertIn("reads included", doc("privacy.md"))
        self.assertIn("0600", doc("privacy.md"))
        self.assertNotIn("read-only by default", doc("privacy.md"))


class TestP1Spans(unittest.TestCase):
    """C-P1-4"""

    @staticmethod
    def derive_from_transcript(session):
        """Rule of events.md section 6: model_request = preceding input row to the first assistant row of a message id;
        tool = tool_use row to its tool_result row."""
        out, prev, seen, tools = [], None, set(), {}
        ep = lambda t: datetime.datetime.strptime(t, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc).timestamp()
        with open(os.path.join(FX, "usage", "transcripts", session + ".jsonl"), encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                t, m = ep(r["timestamp"]), r["message"]
                c = m["content"]
                if r["type"] == "assistant" and m["id"] not in seen:
                    seen.add(m["id"])
                    out.append({"kind": "model_request", "start": prev, "end": t})
                if isinstance(c, list):
                    for b in c:
                        if b["type"] == "tool_use":
                            tools[b["id"]] = (t, b["name"])
                        if b["type"] == "tool_result":
                            a, name = tools[b["tool_use_id"]]
                            out.append({"kind": "tool", "start": a, "end": t, "name": name})
                if r["type"] == "user":
                    prev = t
        return out

    def test_trace_has_spans_and_schema_requires_them(self):
        tr = jf("query", "task_trace.json")
        spans = tr["result"]["spans"]
        self.assertEqual({s["kind"] for s in spans}, {"model_request", "tool", "wait", "delegation"})
        self.assertTrue(any(len(s["links"]) >= 2 for s in spans), "multi-predecessor links are exercised")
        ids = {s["span"] for s in spans}
        self.assertEqual(len(ids), len(spans))
        for s in spans:
            self.assertTrue(set(s["links"]) <= ids)
            self.assertTrue(s["parent_span"] is None or s["parent_span"] in ids)
            self.assertEqual(s["quality"], "derived_wall")
        bad = copy.deepcopy(tr)
        del bad["result"]["spans"]
        self.assertTrue(sch.validate(bad, QUERY))
        bad = copy.deepcopy(tr)
        bad["result"]["spans"][0]["kind"] = "thinking"
        self.assertTrue(sch.validate(bad, QUERY))

    def test_spans_match_the_transcripts(self):
        spans = jf("query", "task_trace.json")["result"]["spans"]
        for sess in ("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"):
            want = [(s["kind"], round(s["start"] - 1800000000.0, 2), round(s["end"] - 1800000000.0, 2)) for s in spans
                    if s["session"] == sess and s["kind"] in ("model_request", "tool")]
            got = [(d["kind"], round(d["start"] - 1800000000.0, 2), round(d["end"] - 1800000000.0, 2)) for d in self.derive_from_transcript(sess)]
            # the session-only exchange (before the first task header) is not part of a trace
            got = [g for g in got if g in want or g[1] > 0]
            self.assertEqual(sorted(want), sorted(got), sess)

    def test_active_time_is_the_union_and_feeds_the_card(self):
        spans = jf("query", "task_trace.json")["result"]["spans"]
        per = {}
        for m in ("m-0000000001", "m-0000000005"):
            per[m] = ref.active_seconds([s for s in spans if s["msg"] == m])
        self.assertEqual(per, {"m-0000000001": 40.9, "m-0000000005": 1.0})
        card = jf("query", "query_metrics.json")["result"]["cards"]["durations"]["active_model_tool_time"]
        xs = sorted(per.values())
        self.assertEqual((card["n"], card["p50_s"], card["p95_s"]), (2, metrics_ref.nearest_rank(xs, 50), metrics_ref.nearest_rank(xs, 95)))
        overlap = [{"kind": "model_request", "start": 0.0, "end": 10.0}, {"kind": "tool", "start": 5.0, "end": 12.0}, {"kind": "wait", "start": 0.0, "end": 99.0}]
        self.assertEqual(ref.active_seconds(overlap), 12.0, "overlaps once, waits not active")

    def test_store_and_docs(self):
        ddl = rd(os.path.join(ref.SCHEMAS, "store-v1.sql"))
        self.assertIn("CREATE TABLE span ", ddl)
        self.assertIn("CREATE TABLE span_link", ddl)
        self.assertIn("derived_wall", doc("events.md"))
        self.assertNotIn("Not available in v1", doc("metrics.md"))


class TestP1Catalog(unittest.TestCase):
    """C-P1-5"""

    NAMES = ["sessions_with_work", "active_days", "work_per_active_week", "direct_delegated_mix", "cache_read_share", "cost_estimate",
             "unpriced_share", "attribution_share", "reply_rate", "execution_time", "failures_by_reason", "concurrent_occupancy", "delivery_retries",
             "context_footprint"]

    def test_every_brief_metric_is_covered_or_deferred(self):
        m = doc("metrics.md")
        sec = m.split("## 2b.")[1].split("## 3.")[0]
        listed = set(re.findall(r"^\| `([a-z_]+)` \|", sec, re.M))
        self.assertEqual(listed, set(self.NAMES))
        self.assertIn("Deferred with reason", sec)
        self.assertIn("compaction", sec)
        explain = next(s for s in QUERY["allOf"] if s["if"]["properties"]["method"]["const"] == "explain_metric")
        enum = explain["then"]["properties"]["result"]["properties"]["metric"]["enum"]
        for n in self.NAMES:
            self.assertIn(n, enum, n)
            self.assertIn("`%s`" % n, doc("query.md") + m)

    def test_cache_read_share_fixture_matches_the_card(self):
        ex = jf("query", "explain_cache_read_share.json")
        self.assertEqual(sch.validate(ex, QUERY), [])
        cat = jf("query", "query_metrics.json")["result"]["cards"]["accounted_tokens"]["by_category"]
        den = cat["input_tokens"] + cat["cache_creation_input_tokens"] + cat["cache_read_input_tokens"]
        self.assertEqual((ex["result"]["numerator"], ex["result"]["denominator"]), (cat["cache_read_input_tokens"], den))


class TestP1Cost(unittest.TestCase):
    """C-P1-6"""

    def test_cost_is_recomputed_from_the_pinned_table(self):
        table = jf("pricing", "fx-2027-01.json")
        tr = jf("query", "task_trace.json")
        self.assertEqual(tr["snapshot"]["pricing"], {"table_id": table["table_id"], "dated": table["dated"]})
        for a in tr["result"]["assignments"]:
            t = a["own_tokens"]
            self.assertEqual(ref.cost_usd(t, table, "claude-sonnet-4-5"), (t["cost_usd_api_equivalent"], t["price_state"]), a["msg"])
        u = tr["result"]["usage"]
        own = u["own"]
        self.assertAlmostEqual(sum(x["cost_usd_api_equivalent"] for x in own.values()), u["inclusive"]["cost_usd_api_equivalent"], 6)
        card = jf("query", "query_metrics.json")["result"]["cards"]["accounted_tokens"]
        self.assertAlmostEqual(u["inclusive"]["cost_usd_api_equivalent"] + u["session_only"][0]["tokens"]["cost_usd_api_equivalent"], card["cost_usd_api_equivalent"], 6)

    def test_unknown_model_is_unpriced_not_guessed(self):
        table = jf("pricing", "fx-2027-01.json")
        t = jf("query", "task_trace.json")["result"]["assignments"][0]["own_tokens"]
        self.assertEqual(ref.cost_usd(t, table, "model-not-in-table"), (None, "unpriced"))

    def test_cache_write_is_split(self):
        for a in jf("query", "task_trace.json")["result"]["assignments"]:
            t = a["own_tokens"]
            self.assertEqual(t["cache_write"], t["cache_write_5m"] + t["cache_write_1h"])
            self.assertLessEqual(t["cache_write_unsplit"], t["cache_write_5m"])
            self.assertEqual(t["total"], t["input"] + t["output"] + t["cache_write"] + t["cache_read"])
        bad = jf("query", "task_trace.json")
        del bad["result"]["assignments"][0]["own_tokens"]["price_state"]
        self.assertTrue(sch.validate(bad, QUERY), "cost fields are required at every usage scope")
        bad = jf("query", "task_trace.json")
        del bad["snapshot"]["pricing"]
        self.assertTrue(sch.validate(bad, QUERY))
        ddl = rd(os.path.join(ref.SCHEMAS, "store-v1.sql"))
        self.assertIn("cache_write_5m INTEGER", ddl)
        self.assertIn("cache_write_1h INTEGER", ddl)


class TestP1CardShapes(unittest.TestCase):
    """C-P1-7"""

    COMMON = ("coverage", "source_quality", "confidence", "event_time", "cutoff")

    def test_every_card_carries_the_common_block(self):
        r = jf("query", "query_metrics.json")["result"]
        for k, card in r["cards"].items():
            for x in self.COMMON:
                self.assertIn(x, card, k)
            self.assertEqual(card["cutoff"], r["window"]["cutoff"])
        c = r["cards"]
        self.assertEqual(c["accounted_tokens"]["confidence"], {"verified": 220, "mapped": 0, "unassigned": 30})
        self.assertEqual(sum(c["accounted_tokens"]["confidence"].values()), c["accounted_tokens"]["total_unique"])
        self.assertEqual(c["accounted_tokens"]["unpriced_share"], [0, 250])
        self.assertEqual(c["assigned_work"]["refusals_by_reason"], {})
        self.assertEqual(c["evaluation"]["root_tasks"]["not_completed"], 0)

    def test_schema_rejects_incomplete_or_open_cards(self):
        base = jf("query", "query_metrics.json")
        self.assertEqual(sch.validate(base, QUERY), [])
        for card, drop in (("assigned_work", "event_time"), ("evaluation", "confidence"), ("deadline_miss", "source_quality"), ("durations", "coverage"),
                           ("accounted_tokens", "unpriced_share"), ("accounted_tokens", "cache_write_split"), ("assigned_work", "refusals_by_reason")):
            bad = copy.deepcopy(base)
            del bad["result"]["cards"][card][drop]
            self.assertTrue(sch.validate(bad, QUERY), "%s without %s" % (card, drop))
        bad = copy.deepcopy(base)
        bad["result"]["cards"]["assigned_work"]["rank"] = 1
        self.assertTrue(sch.validate(bad, QUERY), "cards are closed")
        bad = copy.deepcopy(base)
        del bad["result"]["cards"]["evaluation"]["root_tasks"]["not_completed"]
        self.assertTrue(sch.validate(bad, QUERY))
        bad = copy.deepcopy(base)
        bad["result"]["cards"]["deadline_miss"]["all"]["cancelled"] = 0.5
        self.assertTrue(sch.validate(bad, QUERY))


class TestP1Mapped(unittest.TestCase):
    """C-P1-8"""

    def test_mapped_places_the_delivery_and_owns_no_usage(self):
        exp = jf("scenarios", "expected.json")["mapped_no_header"]
        ev = jl("scenarios", "mapped_no_header.jsonl")
        inj = [e for e in ev if e["action"] == "msg_injected"][0]
        level = ref.join_level(exp["transcript_header"], bool(inj.get("run") and inj.get("session")), 1)
        self.assertEqual((level, ref.usage_owner(level)), (exp["confidence"], exp["usage_owner"]))
        self.assertEqual(ref.usage_owner("verified"), "assignment")
        self.assertEqual(ref.usage_owner("unassigned"), "session_only")

    def test_header_regex_is_frozen_exactly(self):
        t = doc("events.md")
        self.assertIn(ref.HEADER.pattern.replace("(?:", "(?:", 1), t.replace("\\\\", "\\"))
        ok = "[pro-msg id=m-0123456789 from=user to=x task=t kind=task]\nbody"
        self.assertTrue(ref.HEADER.match(ok))
        self.assertTrue(ref.HEADER.match("Stop hook feedback:\n" + ok))
        self.assertTrue(ref.HEADER.match('<pasted_content id="ab12">\n' + ok))
        self.assertFalse(ref.HEADER.match("please read this: " + ok))
        self.assertFalse(ref.HEADER.match("[pro-msg id=m-01 "))

    def test_docs_agree(self):
        t = doc("events.md")
        self.assertIn("owns no usage", t)
        self.assertIn("Linked task usage is `verified` usage only", t)
        self.assertNotIn("Cards count `verified` and `mapped` as linked", t)


class TestP1Purge(unittest.TestCase):
    """C-P1-9"""

    def test_purge_then_backfill_keeps_the_count(self):
        db = ref.new_store()
        ev = jl("events", "release_scenario.jsonl")
        ref.import_events(db, ev)
        total = db.execute("SELECT count(*) FROM event").fetchone()[0]
        cut = sorted(e["t"] for e in ev)[10]
        gone = ref.purge(db, cut, 1800000900.0)
        left = db.execute("SELECT count(*) FROM event").fetchone()[0]
        self.assertEqual((gone > 0, left), (True, total - gone))
        again = ref.import_events(db, [dict(e, origin="backfill", provenance="msgs") for e in ev], batch="b2")
        self.assertEqual(db.execute("SELECT count(*) FROM event").fetchone()[0], left, "purged rows are not restored")
        self.assertEqual(again["purged_skipped"], gone)
        self.assertEqual(db.execute("SELECT purged_before FROM purge_mark").fetchone()[0], cut)
        self.assertEqual(db.execute("SELECT count(*) FROM audit_log WHERE action='audit_purge'").fetchone()[0], 1)

    def test_tombstoned_id_is_never_reimported(self):
        db = ref.new_store()
        ev = jl("events", "release_scenario.jsonl")
        db.execute("INSERT INTO event_tombstone VALUES (?, 1.0, 'correction')", (ev[0]["id"],))
        out = ref.import_events(db, ev)
        self.assertEqual((out["purged_skipped"], out["inserted"]), (1, len(ev) - 1))

    def test_docs(self):
        p = doc("privacy.md")
        self.assertIn("purge watermark", p)
        self.assertIn("bak-vN", p)
        self.assertIn("never restores purged rows", p)
        self.assertIn("0600", p)


class TestP2Cheap(unittest.TestCase):
    """P2 items that fit the contract files"""

    def test_cutoff_selects_the_effective_verdict(self):
        ev = [e for e in jl("events", "release_scenario.jsonl") if e["action"] == "audit_evaluation"]

        def eff(cutoff):
            rs = [{"id": e["data"]["eval"], "target": {"id": e["data"]["target"]}, "supersedes": e["data"]["supersedes"],
                   "evaluator": {"kind": e["data"]["evaluator_kind"]}, "effective_at": e["data"]["effective_at"], "status": e["data"]["status"]}
                  for e in ev if e["data"]["effective_at"] <= cutoff and e["t"] <= cutoff]
            return metrics_ref.effective(rs, "m-0000000001")["status"]
        self.assertEqual(eff(1800000100.0), "passed", "e3 (effective 120 s) is after the cutoff, e2 stays effective")
        self.assertEqual(eff(1800000200.0), "partial")
        self.assertIn("recorded time", doc("metrics.md"))

    def test_cancellation_is_null(self):
        d = jf("query", "query_metrics.json")["result"]["cards"]["deadline_miss"]
        self.assertTrue(all(d[k]["cancelled"] is None for k in ("all", "root_tasks", "child_assignments")))
        self.assertIn("not observable in v1", doc("metrics.md"))

    def test_dedup_tie_break_and_migration_text(self):
        self.assertIn("no `parent_session`", doc("metrics.md"))
        self.assertIn("upcasts", doc("events.md"))
        self.assertIn("quarantine", doc("events.md"))
        ddl = rd(os.path.join(ref.SCHEMAS, "store-v1.sql"))
        self.assertNotIn("rebuildable from the spool", ddl)
        self.assertIn("CREATE TABLE quarantine", ddl)

    def test_export_manifest_lists_pseudonymized_classes(self):
        m = jf("export", "manifest.json")
        self.assertEqual(sch.validate(m, MANIFEST), [])
        self.assertIn("task", m["redaction"]["pseudonymized"])
        self.assertNotIn("project", m["redaction"]["dropped_fields"])
        bad = copy.deepcopy(m)
        del bad["redaction"]["pseudonymized"]
        self.assertTrue(sch.validate(bad, MANIFEST))
        self.assertIn("D4 generates this export", doc("privacy.md"))


if __name__ == "__main__":
    unittest.main()
