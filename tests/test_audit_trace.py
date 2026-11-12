"""Audit step D3: task_trace on the release fixture (delegation, pending wait, late reply) and on edge cases. Stdlib only."""
import json
import os
import sys
import unittest

import helpers

HERE = helpers.HERE
sys.path[:0] = [HERE, os.path.join(HERE, "..", "plugins", "professionals", "lib")]
import audit_query_fixture as F  # noqa: E402
import audit_schema as sch  # noqa: E402
from pros_audit import query as Q  # noqa: E402

QUERY = sch.load("query-v1.json")
ROOT, CHILD = "m-0000000001", "m-0000000005"


def mk(db=None, now=F.NOW):
    return Q.Query(db or F.load_release(), owner_check=lambda: True, now=lambda: now)


class TestTraceGolden(unittest.TestCase):
    def test_by_message_id_task_id_and_child_id_give_the_same_trace(self):
        q = mk()
        a, b, c = (q.task_trace(x) for x in (ROOT, "fx-root", CHILD))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(F.jf("query", "task_trace.json"), sort_keys=True))
        self.assertEqual(a["result"], b["result"])
        self.assertEqual(a["result"], c["result"], "a child id resolves to its root trace")
        self.assertEqual(sch.validate(a, QUERY), [])

    def test_reply_message_id_resolves_to_the_assignment_it_answers(self):
        self.assertEqual(mk().task_trace("m-0000000003")["result"]["root"], ROOT)

    def test_three_statuses_stay_separate(self):
        r = mk().task_trace(ROOT)["result"]["assignments"]
        self.assertEqual([(a["delivery"], a["deadline_status"], a["evaluation"]["status"]) for a in r], [("answered", "late", "partial"), ("answered", "met", "passed")])

    def test_pending_wait_and_shadowed_reply_are_not_task_outcomes(self):
        r = mk().task_trace(ROOT)["result"]
        self.assertEqual([(w["outcome"], w["shadowed_reply"]) for w in r["waits"]], [("pending", False), ("note_only", False), ("note_only", True)])
        self.assertEqual(r["assignments"][0]["delivery"], "answered")

    def test_deadline_status_follows_the_clock_not_the_overdue_flag(self):
        st = lambda now: mk(now=now).task_trace(ROOT)["result"]["assignments"][0]["deadline_status"]
        self.assertEqual((st(F.T0 + 30), st(F.T0 + 40), st(F.NOW)), ("not_yet_due", "missed", "late"))

    def test_spans_form_a_tree_with_links(self):
        r = mk().task_trace(ROOT)["result"]
        ids = {s["span"] for s in r["spans"]}
        for s in r["spans"]:
            self.assertTrue(set(s["links"]) <= ids)
            self.assertTrue(s["parent_span"] is None or s["parent_span"] in ids)
        self.assertEqual({s["kind"] for s in r["spans"]}, {"model_request", "tool", "wait", "delegation"})
        self.assertNotIn("name", [s for s in r["spans"] if s["kind"] == "model_request"][0])

    def test_no_content_or_path_in_the_answer(self):
        text = json.dumps(mk().task_trace(ROOT))
        for c in json.load(open(os.path.join(F.FX, "usage", "expected.json")))["canaries"]:
            self.assertNotIn(c, text)
        self.assertNotIn("$PROJECT", text)


class TestTraceEdges(unittest.TestCase):
    def test_ambiguous_task_id_lists_candidates_never_merges(self):
        db = F.load_release()
        db.execute("INSERT INTO assignment (msg,root,parent,depth,task,to_name,from_role,created,delivery,confidence) VALUES ('m-00000000aa','m-00000000aa',NULL,0,'fx-root','alpha','user',?,'queued','unassigned')", (F.T0 + 50,))
        env = mk(db).call("task_trace", {"id": "fx-root"})
        self.assertEqual(env["error"]["code"], "bad_request")
        self.assertEqual(env["error"]["field"].split(","), ["m-0000000001", "m-00000000aa"])
        self.assertEqual(mk(db).task_trace("m-00000000aa")["result"]["assignments"][0]["delivery"], "queued")

    def test_depth_limit_reports_a_gap(self):
        env = mk().task_trace(ROOT, depth=0)
        self.assertEqual([a["msg"] for a in env["result"]["assignments"]], [ROOT])
        self.assertEqual(env["result"]["gaps"], [{"kind": "depth_limit", "count": 1}])
        self.assertEqual(sch.validate(env, QUERY), [])

    def test_orphan_and_unverified_join_are_gaps_and_usage_is_absent_not_zero(self):
        db = F.load_release()
        db.execute("UPDATE assignment SET orphan=1 WHERE msg=?", (CHILD,))
        db.execute("UPDATE exchange SET msg=NULL, confidence='unassigned' WHERE msg=?", (CHILD,))
        db.execute("UPDATE assignment SET confidence='mapped' WHERE msg=?", (CHILD,))
        env = mk(db).task_trace(ROOT)
        child = env["result"]["assignments"][1]
        self.assertNotIn("own_tokens", child, "no verified exchange: not observed, not zero")
        self.assertEqual(child["confidence"], "mapped")
        self.assertIn({"kind": "orphan", "count": 1, "msg": CHILD}, env["result"]["gaps"])
        self.assertEqual(env["result"]["usage"]["inclusive"]["total"], 140)
        self.assertEqual(sch.validate(env, QUERY), [])

    def test_hashed_task_form_is_shown(self):
        db = F.load_release()
        db.execute("UPDATE assignment SET task='h-0123456789ab', task_form='hashed' WHERE msg=?", (ROOT,))
        env = mk(db).task_trace(ROOT)
        self.assertEqual(env["result"]["assignments"][0]["task_form"], "hashed")
        self.assertEqual(sch.validate(env, QUERY), [])
        self.assertEqual(mk(db).task_trace("h-0123456789ab")["result"]["root"], ROOT)

    def test_trace_is_bounded_in_time(self):
        db = F.load_release()
        import time
        t = time.time()
        for i in range(300):
            db.execute("INSERT INTO assignment (msg,root,parent,depth,task,to_name,from_role,created,delivery,confidence) VALUES (?,?,?,?,?,'alpha','user',?,'queued','unassigned')",
                       ("m-%010x" % (0x1000 + i), ROOT, ROOT, 1, "t-%d" % i, F.T0 + 2))
        env = mk(db).task_trace(ROOT)
        self.assertEqual(len(env["result"]["assignments"]), 302)
        self.assertLess(time.time() - t, 5)
        self.assertEqual(sch.validate(env, QUERY), [])


if __name__ == "__main__":
    unittest.main()
