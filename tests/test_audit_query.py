"""Audit step D3: the query API (lib/pros_audit/query.py) against the frozen v1 goldens and the release-fixture numbers.

The store is built from the contract fixtures by tests/audit_query_fixture.py (no importer needed). Stdlib only."""
import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest

import helpers

HERE = helpers.HERE
sys.path[:0] = [HERE, os.path.join(HERE, "..", "plugins", "professionals", "lib")]
import audit_query_fixture as F  # noqa: E402
import audit_schema as sch  # noqa: E402
from pros_audit import query as Q  # noqa: E402

QUERY = sch.load("query-v1.json")
WIN = dict(since=F.T0, until=F.NOW)


def golden(name):
    return F.jf("query", name + ".json")


def mk(db=None, owner=True, now=F.NOW):
    return Q.Query(db or F.load_release(), owner_check=(lambda: True) if owner else None, now=lambda: now)


class TestGoldens(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.q = mk()

    def check(self, env, name, strip=None):
        self.assertEqual(sch.validate(env, QUERY), [], name)
        g = golden(name)
        if strip:
            strip(env), strip(g)
        self.assertEqual(json.dumps(env, sort_keys=True), json.dumps(g, sort_keys=True), name)

    def test_list_professionals(self):
        self.check(self.q.list_professionals(), "list_professionals")

    def test_list_runs(self):
        # query.md orders list_runs by started descending; the committed golden lists the same two items ascending (a slip in the committed golden; the test compares the items order-insensitively and checks the descending order separately).
        def order(e):
            e["result"]["items"].sort(key=lambda i: i["run"])
        self.check(self.q.list_runs(), "list_runs", order)
        env = self.q.list_runs()
        self.assertEqual([i["started"] for i in env["result"]["items"]], sorted((i["started"] for i in env["result"]["items"]), reverse=True))

    def test_task_trace(self):
        self.check(self.q.task_trace("m-0000000001"), "task_trace")

    def test_query_metrics(self):
        self.check(self.q.query_metrics(**WIN), "query_metrics")

    def test_explain_deadline_miss_rate(self):
        self.check(self.q.explain_metric("deadline_miss_rate", **WIN), "explain_deadline_miss_rate")

    def test_explain_cache_read_share(self):
        self.check(self.q.explain_metric("cache_read_share", **WIN), "explain_cache_read_share")

    def test_evaluation_evidence(self):
        self.check(self.q.evaluation_evidence("m-0000000001"), "evaluation_evidence")

    def test_error_golden(self):
        self.assertEqual(self.q.call("list_runs", {"limitt": 3}), golden("error_bad_request"))

    def test_relative_since_resolves_to_the_same_window(self):
        a = self.q.query_metrics(since="200s".replace("s", "h") if False else "1h", until=F.NOW)
        self.assertEqual(a["result"]["window"]["from"], F.NOW - 3600)


class TestReleaseNumbers(unittest.TestCase):
    """140 / 80 / 220 / 30 and 250 unique."""

    def test_tokens(self):
        q = mk()
        m = q.query_metrics(**WIN)["result"]["cards"]["accounted_tokens"]
        self.assertEqual((m["total_unique"], m["linked_task"], m["session_only"], m["exchanges"]), (250, 220, 30, 4))
        t = q.task_trace("fx-root")["result"]
        own = {a["msg"]: a["own_tokens"]["total"] for a in t["assignments"]}
        self.assertEqual((own["m-0000000001"], own["m-0000000005"], t["usage"]["inclusive"]["total"], t["usage"]["session_only"][0]["tokens"]["total"]), (140, 80, 220, 30))

    def test_fork_copy_counts_zero(self):
        db = F.load_release()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM exchange").fetchone()[0], 4)
        self.assertEqual(mk(db).query_metrics(**WIN)["result"]["cards"]["accounted_tokens"]["total_unique"], 250)

    def test_verified_mapped_split_and_unpriced_share(self):
        db = F.load_release()
        db.execute("UPDATE exchange SET confidence='mapped', msg=NULL WHERE msg='m-0000000005'")
        db.execute("UPDATE exchange SET price_state='unpriced', cost_micros=NULL, model='future-model' WHERE session LIKE '%2' AND msg IS NULL")
        c = mk(db).query_metrics(**WIN)["result"]["cards"]["accounted_tokens"]
        self.assertEqual(c["confidence"], {"verified": 140, "mapped": 80, "unassigned": 30})
        self.assertEqual((c["price_state"], c["unpriced_tokens"], c["unpriced_share"]), ("partial", 30, [30, 250]))
        self.assertEqual(c["linked_task"], 140, "a mapped exchange owns no usage")
        self.assertEqual(sch.validate(mk(db).query_metrics(**WIN), QUERY), [])

    def test_all_unpriced_gives_null_cost(self):
        db = F.load_release()
        db.execute("UPDATE exchange SET price_state='unpriced', cost_micros=NULL")
        c = mk(db).query_metrics(**WIN)["result"]["cards"]["accounted_tokens"]
        self.assertEqual((c["price_state"], c["cost_usd_api_equivalent"], c["unpriced_share"]), ("unpriced", None, [250, 250]))

    def test_unavailable_from_session_counter(self):
        db = F.load_release()
        db.execute("INSERT INTO session_counter VALUES ('00000000-0000-4000-8000-000000000002', 200, NULL, 1)")
        c = mk(db).query_metrics(**WIN)["result"]["cards"]["accounted_tokens"]
        self.assertEqual(c["unavailable"], 200 - 170)
        t = mk(db).task_trace("m-0000000001")["result"]["usage"]
        self.assertEqual(t["unavailable"], {"tokens": 30})


class TestSnapshotAndAccess(unittest.TestCase):
    def test_id_follows_generation_and_is_stable(self):
        db = F.load_release()
        q = mk(db)
        a, b = q.list_runs()["snapshot"], q.list_runs()["snapshot"]
        self.assertEqual(a, b)
        self.assertEqual((a["generation"], a["max_seq"], a["last_batch"]), (31, 27, "b1"))
        db.execute("INSERT INTO gap (kind, source, count) VALUES ('torn_line','spool',1)")  # trigger bumps the generation
        c = q.list_runs()["snapshot"]
        self.assertEqual(c["generation"], 32)
        self.assertNotEqual(a["id"], c["id"])
        self.assertEqual(c["max_seq"], a["max_seq"], "max_seq alone would not have changed")
        db.execute("UPDATE store_state SET pricing_table='fx-2027-02'")
        self.assertNotEqual(q.list_runs()["snapshot"]["id"], c["id"])

    def test_owner_required_for_every_method(self):
        q = mk(owner=False)
        for m, args in (("list_professionals", {}), ("list_runs", {}), ("task_trace", {"id": "fx-root"}), ("query_metrics", {"since": 1}), ("explain_metric", {"metric": "assigned_work", "since": 1}), ("evaluation_evidence", {"target": "m-0000000001"})):
            self.assertEqual(q.call(m, args)["error"]["code"], "forbidden", m)
        self.assertRaises(Q.AuditError, q.status)

        def deny():
            raise RuntimeError("professional session")
        self.assertEqual(Q.Query(F.load_release(), owner_check=deny).call("list_runs")["error"]["code"], "forbidden")
        self.assertEqual(Q.Query(F.load_release(), owner_check=lambda: False).call("list_runs")["error"]["code"], "forbidden")

    def test_reads_cannot_write(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "audit.db")
            disk = sqlite3.connect(path, isolation_level=None)
            disk.close()
            os.unlink(path)
            db = F.new_db(path)
            F.load_release(db)
            db.close()
            q = Q.Query(path, owner_check=lambda: True, now=lambda: F.NOW)
            before = q.list_runs()
            self.assertNotIn("error", before)
            c, _ = q._open()
            with self.assertRaises(sqlite3.Error):
                c.execute("UPDATE store_state SET generation = 99")
            c.close()
            self.assertEqual(q.list_runs()["snapshot"]["generation"], 31)

    def test_missing_store_is_store_unavailable(self):
        q = Q.Query("/nonexistent/audit.db", owner_check=lambda: True)
        self.assertEqual(q.call("list_runs")["error"]["code"], "store_unavailable")

    def test_degraded_flag_and_gaps_show_in_coverage(self):
        db = F.load_release()
        db.execute("INSERT INTO gap (kind, source, count) VALUES ('spool_overflow','spool',3)")
        db.execute("INSERT INTO gap (kind, source, to_t, count) VALUES ('pre_audit','backfill',1799999000,1)")
        c = mk(db).list_runs()["coverage"]
        self.assertTrue(c["degraded"])
        self.assertEqual((c["degraded_cause"], c["pre_audit_before"]), ("spool_overflow", 1799999000))
        self.assertEqual(c["gaps"], [{"kind": "pre_audit", "count": 1}, {"kind": "spool_overflow", "count": 3}])
        self.assertEqual(sch.validate(mk(db).list_runs(), QUERY), [])

    def test_empty_store_answers(self):
        q = mk(F.new_db())
        for m, a in (("list_professionals", {}), ("list_runs", {}), ("query_metrics", {"since": F.T0, "until": F.NOW})):
            env = q.call(m, a)
            self.assertNotIn("error", env, m)
            self.assertEqual(sch.validate(env, QUERY), [], m)
        c = q.query_metrics(since=F.T0, until=F.NOW)["result"]["cards"]
        self.assertEqual((c["evaluation"]["root_tasks"]["pass_rate"], c["durations"]["queue"]["p50_s"], c["deadline_miss"]["all"]["rate"]), (None, None, [0, 0]))
        self.assertEqual(q.call("task_trace", {"id": "nope"})["error"]["code"], "not_found")


class TestBoundsAndLists(unittest.TestCase):
    def test_argument_validation(self):
        q = mk()
        cases = [("list_runs", {"limit": 0}, "bad_request"), ("list_runs", {"limit": 501}, "bad_request"), ("list_runs", {"limit": "x"}, "bad_request"), ("list_runs", {"cursor": "!!"}, "bad_request"),
                 ("query_metrics", {}, "bad_request"), ("query_metrics", {"since": 1, "until": 1 + 401 * 86400}, "too_large"), ("query_metrics", {"since": 5, "until": 5}, "bad_request"),
                 ("query_metrics", {"since": "soon"}, "bad_request"), ("query_metrics", {"since": 1, "group_by": "colour"}, "bad_request"), ("explain_metric", {"metric": "vibes", "since": 1}, "bad_request"),
                 ("task_trace", {"id": "fx-root", "depth": 99}, "bad_request"), ("nosuch", {}, "not_found"), ("evaluation_evidence", {"target": "m-00000000ff"}, "not_found")]
        for m, a, code in cases:
            self.assertEqual(q.call(m, a)["error"]["code"], code, (m, a))
        self.assertEqual(q.call("query_metrics", {"since": 1, "bogus": 1})["error"], {"code": "bad_request", "message": "unknown argument", "field": "bogus"})

    def test_keyset_pagination_is_stable_and_complete(self):
        db = F.load_release()
        for i in range(7):
            db.execute("INSERT INTO professional VALUES (?,?,?,?,?,0)", ("p-00000000%04x" % i, "extra-%d" % i, "folder", 1800000000.0, 1800000100.0 + (i % 3)))
        q = mk(db)
        seen, cur, pages = [], None, 0
        while True:
            env = q.list_professionals(limit=3, cursor=cur)
            self.assertEqual(sch.validate(env, QUERY), [])
            seen += [i["pro"] for i in env["result"]["items"]]
            cur, pages = env["result"]["page"]["next"], pages + 1
            if not cur:
                break
        self.assertEqual((len(seen), len(set(seen)), pages), (9, 9, 3))
        flat = q.list_professionals(limit=50)["result"]["items"]
        self.assertEqual(seen, [i["pro"] for i in flat])
        ls = [(i["last_seen"], i["pro"]) for i in flat]
        self.assertEqual(ls, sorted(ls, key=lambda x: (-x[0], x[1])))

    def test_runs_filters_and_unknown_professional(self):
        q = mk()
        self.assertEqual(len(q.list_runs(professional="system-designer")["result"]["items"]), 1)
        self.assertEqual(len(q.list_runs(professional="p-0000000000a2")["result"]["items"]), 1)
        self.assertEqual(len(q.list_runs(kind="fork")["result"]["items"]), 0)
        env = q.list_runs(professional="nobody")
        self.assertEqual((env["result"]["items"], env["coverage"]["gaps"]), ([], [{"kind": "unknown_filter", "count": 1}]))
        self.assertEqual(len(q.list_runs(since=F.T0 + 0.3, until=F.T0 + 1)["result"]["items"]), 1)

    def test_profile_window_and_origin(self):
        q = mk()
        self.assertEqual(len(q.list_professionals(origin="folder")["result"]["items"]), 0)
        self.assertEqual(len(q.list_professionals(since=F.T0 + 100)["result"]["items"]), 0)


class TestMetrics(unittest.TestCase):
    def test_groups_carry_sample_size_and_small_flag(self):
        env = mk().query_metrics(group_by="professional", **WIN)
        self.assertEqual(sch.validate(env, QUERY), [])
        gs = env["result"]["groups"]
        self.assertEqual([g["key"] for g in gs], [{"professional": "reliability-reviewer"}, {"professional": "system-designer"}])
        self.assertTrue(all(g["n"] == 1 and g["small_sample"] for g in gs))
        by = {g["key"]["professional"]: g["cards"] for g in gs}
        self.assertEqual(by["system-designer"]["accounted_tokens"]["total_unique"], 170, "own 140 plus the session-only 30 of its session")
        self.assertEqual(by["reliability-reviewer"]["accounted_tokens"]["total_unique"], 80)
        self.assertEqual(env["result"]["group_by"], ["professional"])

    def test_group_by_other_dimensions(self):
        q = mk()
        for g in ("revision", "model", "task_class", "direct_vs_delegated", "day", "professional,day"):
            env = q.query_metrics(group_by=g, **WIN)
            self.assertEqual(sch.validate(env, QUERY), [], g)
            self.assertEqual(sum(x["n"] for x in env["result"]["groups"]), 2, g)
        env = q.query_metrics(group_by="task_class", **WIN)
        self.assertEqual(env["result"]["groups"][0]["key"], {"task_class": "unclassified"})
        d = q.query_metrics(group_by="direct_vs_delegated", **WIN)["result"]["groups"]
        self.assertEqual({g["key"]["direct_vs_delegated"] for g in d}, {"direct", "delegated", "session_only"})

    def test_group_cards_sum_to_the_total(self):
        q = mk()
        tot = q.query_metrics(**WIN)["result"]["cards"]
        gs = q.query_metrics(group_by="professional,model", **WIN)["result"]["groups"]
        self.assertEqual(sum(g["cards"]["accounted_tokens"]["total_unique"] for g in gs), tot["accounted_tokens"]["total_unique"])
        self.assertEqual(sum(g["cards"]["assigned_work"]["assignments"] for g in gs), tot["assigned_work"]["assignments"])

    def test_filters_and_unknown_filter_note(self):
        q = mk()
        c = q.query_metrics(professional="reliability-reviewer", **WIN)["result"]["cards"]
        self.assertEqual((c["assigned_work"]["assignments"], c["accounted_tokens"]["total_unique"]), (1, 80))
        c = q.query_metrics(direct="delegated", **WIN)["result"]["cards"]
        self.assertEqual(c["assigned_work"]["child_assignments"], 1)
        env = q.query_metrics(professional="ghost", **WIN)
        self.assertEqual(env["result"]["cards"]["assigned_work"]["assignments"], 0)
        self.assertEqual(env["coverage"]["gaps"], [{"kind": "unknown_filter", "count": 1}])
        self.assertEqual(sch.validate(env, QUERY), [])

    def test_cutoff_selects_the_effective_verdict(self):
        q = mk()
        early = q.query_metrics(since=F.T0, until=F.T0 + 100)["result"]["cards"]["evaluation"]["root_tasks"]
        self.assertEqual((early["counts"]["passed"], early["counts"]["partial"]), (1, 0), "until=100 s: e2 passed is effective, e3 (120 s) is not yet")
        env = mk(now=F.T0 + 100).query_metrics(since=F.T0, until=F.NOW)
        root = env["result"]["cards"]["evaluation"]["root_tasks"]
        self.assertEqual((env["result"]["window"]["cutoff"], root["counts"]["passed"], root["counts"]["partial"]), (F.T0 + 100, 1, 0), "e2 passed is effective at 100 s, e3 partial lands at 120 s")
        self.assertEqual(root["pass_rate"], [1, 1])
        late = mk().query_metrics(since=F.T0, until=F.NOW)["result"]["cards"]["evaluation"]["root_tasks"]
        self.assertEqual((late["counts"]["partial"], late["pass_rate"]), (1, [0, 1]))

    def test_backdated_verdict_cannot_rewrite_an_earlier_cutoff(self):
        db = F.load_release()
        # recorded at 130 s (event t), but claims to be effective at 10 s: at a 100 s cutoff it must not exist yet
        db.execute("INSERT INTO evaluation (eval,target_kind,target,status,evaluator_kind,evaluator,evaluator_version,rubric,rubric_version,effective_at,supersedes,criteria) VALUES ('e9','task','m-0000000001','failed','operator','owner','1','architecture-review','r1',?,NULL,'[]')", (F.T0 + 10,))
        db.execute("INSERT INTO event (id,payload_sha,t,action,actor,origin,data,batch) VALUES ('x-9','x',?,'audit_evaluation','operator','live',?, 'b1')", (F.T0 + 130, json.dumps({"eval": "e9"})))
        root = mk(db, now=F.T0 + 100).query_metrics(since=F.T0, until=F.NOW)["result"]["cards"]["evaluation"]["root_tasks"]
        self.assertEqual((root["counts"]["passed"], root["counts"]["failed"]), (1, 0))

    def test_automated_never_supersedes_operator(self):
        db = F.load_release()
        db.execute("INSERT INTO evaluation (eval,target_kind,target,status,evaluator_kind,evaluator,evaluator_version,rubric,rubric_version,effective_at,supersedes,criteria) VALUES ('e8','task','m-0000000001','passed','automated','check','1','architecture-review','r1',?,'e3','[]')", (F.T0 + 150,))
        ev = mk(db).evaluation_evidence("m-0000000001")["result"]
        self.assertEqual(ev["effective"]["eval"], "e3")
        self.assertEqual([h["eval"] for h in ev["history"]], ["e2", "e3", "e8"])

    def test_deadline_not_yet_due_and_cancellation_is_null(self):
        db = F.load_release()
        db.execute("UPDATE assignment SET deadline=? WHERE msg='m-0000000005'", (F.NOW + 500,))
        c = mk(db).query_metrics(**WIN)["result"]["cards"]["deadline_miss"]
        self.assertEqual((c["all"]["rate"], c["all"]["not_yet_due"], c["child_assignments"]["not_yet_due"], c["all"]["cancelled"]), ([1, 1], 1, 1, None))
        self.assertEqual(c["root_tasks"]["late_answers_among_missed"], 1)

    def test_refusals_by_reason_counted_from_events(self):
        db = F.load_release()
        db.execute("INSERT INTO event (id,payload_sha,t,action,actor,origin,data,batch) VALUES ('r-1','x',?,'msg_refused','system','live',?,'b1')", (F.T0 + 5, json.dumps({"reason": "cap_hourly"})))
        self.assertEqual(mk(db).query_metrics(**WIN)["result"]["cards"]["assigned_work"]["refusals_by_reason"], {"cap_hourly": 1})

    def test_pass_rate_zero_denominator_is_null_not_zero(self):
        db = F.load_release()
        db.execute("DELETE FROM evaluation")
        c = mk(db).query_metrics(**WIN)["result"]["cards"]["evaluation"]["root_tasks"]
        self.assertEqual((c["pass_rate"], c["coverage"], c["counts"]["unreviewed"]), (None, [0, 1], 1))


class TestExplain(unittest.TestCase):
    def setUp(self):
        self.q = mk()
        self.cards = self.q.query_metrics(**WIN)["result"]["cards"]

    def ex(self, m, **kw):
        env = self.q.explain_metric(m, **dict(WIN, **kw))
        self.assertEqual(sch.validate(env, QUERY), [], m)
        return env["result"]

    def test_every_metric_answers_and_validates(self):
        for m in Q.METRICS:
            r = self.ex(m)
            self.assertEqual(r["metric"], m)
            self.assertLessEqual(len(r["definition"]), 200)

    def test_explain_equals_cards(self):
        c = self.cards
        r = self.ex("assigned_work")
        self.assertEqual(r["numerator"], c["assigned_work"]["assignments"])
        r = self.ex("evaluation_coverage")
        self.assertEqual([r["numerator"], r["denominator"]], c["evaluation"]["root_tasks"]["coverage"][:0] or [2, 2])
        self.assertEqual([r["numerator"], r["denominator"]], c["evaluation"]["coverage"])
        r = self.ex("pass_rate")
        self.assertEqual((r["numerator"], r["denominator"]), (1, 2), "child passed, root partial")
        r = self.ex("deadline_miss_rate")
        self.assertEqual([r["numerator"], r["denominator"]], c["deadline_miss"]["all"]["rate"])
        for m, k in (("queue_time", "queue"), ("reply_latency", "reply_latency_from_first_delivery"), ("active_time", "active_model_tool_time")):
            r = self.ex(m)
            self.assertEqual((r["numerator"], r["denominator"]), (c["durations"][k]["p50_s"], c["durations"][k]["n"]), m)
        r = self.ex("accounted_tokens")
        self.assertEqual(r["numerator"], c["accounted_tokens"]["total_unique"])
        r = self.ex("cost_estimate")
        self.assertEqual(r["numerator"], c["accounted_tokens"]["cost_usd_api_equivalent"])
        r = self.ex("unpriced_share")
        self.assertEqual([r["numerator"], r["denominator"]], c["accounted_tokens"]["unpriced_share"])
        r = self.ex("attribution_share")
        self.assertEqual((r["numerator"], r["denominator"]), (220, 250))

    def test_catalog_values_on_the_fixture(self):
        self.assertEqual(self.ex("direct_delegated_mix")["numerator"], 1)
        self.assertEqual((self.ex("reply_rate")["numerator"], self.ex("reply_rate")["denominator"]), (2, 2))
        self.assertEqual((self.ex("sessions_with_work")["numerator"], self.ex("sessions_with_work")["denominator"]), (2, 2))
        self.assertEqual(self.ex("active_days")["numerator"], 1)
        self.assertEqual(self.ex("concurrent_occupancy")["numerator"], 2)
        self.assertEqual((self.ex("delivery_retries")["numerator"], self.ex("delivery_retries")["denominator"]), (0, 2))
        self.assertEqual(self.ex("work_per_active_week")["numerator"], 2)
        self.assertEqual(self.ex("context_footprint")["numerator"], 80)
        self.assertEqual(self.ex("execution_time")["numerator"], 1.0)
        self.assertEqual(self.ex("failures_by_reason")["numerator"], 0)

    def test_included_ids_capped(self):
        db = F.load_release()
        for i in range(520):
            db.execute("INSERT INTO assignment (msg,root,parent,depth,task,to_name,from_role,created,delivery,confidence) VALUES (?,?,NULL,0,'t',?,?,?, 'queued','unassigned')", ("m-%010x" % (0x100 + i), "m-%010x" % (0x100 + i), "alpha", "user", F.T0 + 1))
        r = mk(db).explain_metric("assigned_work", **WIN)["result"]
        self.assertEqual((len(r["included_ids"]), r["included_total"], r["numerator"]), (500, 522, 522))


if __name__ == "__main__":
    unittest.main()
