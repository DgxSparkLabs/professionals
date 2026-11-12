"""Audit D4: verdicts. Release fixture (operator partial verdict with one failed criterion), supersession, cutoff, rubric consistency, denied access."""
import json
import os
import sys
import unittest

import helpers
import audit_d4_store as st

sys.path.insert(0, helpers.HERE)
import audit_schema as sch  # noqa: E402
from pros_audit import evaluation as ev  # noqa: E402

EVENT = sch.load("event-v1.json")


class Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="auditd4-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.conn = st.make_store(self.home)
        st.load_release(self.conn)
        st.add_rubrics(self.home)
        self.addCleanup(self.conn.close)

    def gen(self):
        return self.conn.execute("SELECT generation FROM store_state").fetchone()[0]

    def auto_pass(self, target=st.ROOT, t=1800000060.0, status="passed", **kw):
        return ev.record_verdict(self.conn, self.home, target, status, [("C1", "passed"), ("C2", "passed"), ("C3", "passed")], rubric="architecture-review", rubric_version="r1",
                                 evaluator="check", evaluator_kind="automated", effective_at=t, now=t, supersedes=kw.pop("supersedes", None), **kw)


class TestReleaseVerdict(Base):
    def test_operator_partial_verdict_with_one_failed_criterion(self):
        e2 = self.auto_pass()
        g = self.gen()
        code, out, err = st.cli(self.home, "verdict", st.ROOT, "--status", "partial", "--criterion", "C1=failed@note:fx-3", "--rest", "passed",
                                "--rubric", "architecture-review", "--rubric-version", "r1", "--effective-at", "1800000120", "--evidence", "check-run:fx-2", now=1800000120.0)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.gen(), g + 1, "one generation bump per verdict")
        evs = ev.evaluations(self.conn, st.ROOT)
        self.assertEqual([e["status"] for e in evs], ["passed", "partial"])
        e3 = evs[1]
        self.assertEqual(e3["supersedes"], e2["data"]["eval"], "an operator verdict supersedes the current effective one by default")
        self.assertEqual([(c["id"], c["outcome"]) for c in e3["criteria"]], [("C1", "failed"), ("C2", "passed"), ("C3", "passed")])
        self.assertEqual(e3["criteria"][0]["evidence"], "note:fx-3")
        self.assertEqual(e3["evidence"], ["check-run:fx-2"])
        self.assertEqual(ev.effective_status(self.conn, st.ROOT, 1800000200.0), "partial")
        self.assertEqual(ev.effective_status(self.conn, st.ROOT, 1800000100.0), "passed", "the cutoff leaves the earlier verdict effective")
        best, hist = ev.effective(evs, 1800000200.0)
        self.assertEqual([h["superseded"] for h in hist], [True, False])
        self.assertIn("recorded", out)

    def test_recorded_as_audit_event_and_valid(self):
        self.auto_pass()
        st.cli(self.home, "verdict", st.ROOT, "--status", "partial", "--criterion", "C1=failed", "--rest", "passed", "--rubric", "architecture-review", now=1800000120.0)
        rows = self.conn.execute("SELECT id, t, action, actor, origin, msg, data FROM event WHERE action='audit_evaluation' ORDER BY seq").fetchall()
        self.assertEqual(len(rows), 2)
        for r in rows:
            e = {"v": 1, "id": r[0], "t": r[1], "action": r[2], "actor": r[3], "origin": r[4], "msg": r[5], "data": json.loads(r[6])}
            self.assertEqual(sch.validate(e, EVENT), [], e)
        self.assertEqual(rows[1][3], "operator")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM audit_log WHERE action='audit_evaluation'").fetchone()[0], 2)

    def test_status_must_agree_with_criteria(self):
        g = self.gen()
        code, _, err = st.cli(self.home, "verdict", st.ROOT, "--status", "passed", "--criterion", "C1=failed", "--rest", "passed", "--rubric", "architecture-review")
        self.assertEqual(code, 2)
        self.assertIn("contradicts", err)
        code, _, err = st.cli(self.home, "verdict", st.ROOT, "--status", "partial", "--criterion", "C1=failed", "--rubric", "architecture-review")
        self.assertEqual(code, 2)
        self.assertIn("no outcome", err)
        code, _, err = st.cli(self.home, "verdict", st.ROOT, "--status", "partial", "--criterion", "C1=failed", "--criterion", "C2=not_assessed", "--rest", "passed", "--rubric", "architecture-review")
        self.assertEqual(code, 2, "failed + not_assessed is inconclusive, never partial")
        self.assertEqual(self.gen(), g)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM evaluation").fetchone()[0], 0)
        code, _, _ = st.cli(self.home, "verdict", st.ROOT, "--status", "inconclusive", "--criterion", "C1=failed", "--criterion", "C2=not_assessed", "--rest", "passed", "--rubric", "architecture-review")
        self.assertEqual(code, 0)

    def test_optional_criterion_does_not_count(self):
        code, _, err = st.cli(self.home, "verdict", st.ROOT, "--status", "passed", "--criterion", "C1=passed", "--criterion", "CANARY-CRIT-64=passed", "--criterion", "C3=failed",
                              "--rubric", "CANARY-RUBRIC-63", "--rubric-version", "r1")
        self.assertEqual(code, 0, err)

    def test_bad_inputs_rejected(self):
        for argv in (("m-ffffffffff", "--status", "passed", "--criterion", "C1=passed"),
                     (st.ROOT, "--status", "passed", "--criterion", "C1=maybe"),
                     (st.ROOT, "--status", "passed", "--criterion", "C1=passed", "--evidence", "not a ref"),
                     (st.ROOT, "--status", "passed", "--criterion", "C1=passed", "--rubric", "nope"),
                     (st.ROOT, "--status", "passed", "--criterion", "C1=passed", "--supersedes", "e-missing"),
                     (st.CHILD, "--status", "passed", "--criterion", "C1=passed", "--kind", "task"),
                     (st.ROOT, "--status", "passed")):
            code, _, err = st.cli(self.home, "verdict", *argv)
            self.assertIn(code, (2, 3), argv)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM evaluation").fetchone()[0], 0)

    def test_supersedes_must_name_same_target(self):
        e = self.auto_pass(target=st.CHILD, t=1800000050.0)
        code, _, err = st.cli(self.home, "verdict", st.ROOT, "--status", "passed", "--criterion", "C1=passed", "--supersedes", e["data"]["eval"])
        self.assertEqual(code, 3)
        self.assertIn("same target", err)

    def test_child_gets_assignment_kind(self):
        code, _, err = st.cli(self.home, "verdict", st.CHILD, "--status", "passed", "--criterion", "T1=passed")
        self.assertEqual(code, 0, err)
        self.assertEqual(ev.evaluations(self.conn, st.CHILD)[0]["target_kind"], "assignment")
        self.assertEqual(ev.evaluations(self.conn, st.CHILD)[0]["rubric"], "ad-hoc")

    def test_attach_check_result_stores_only_a_hash_ref(self):
        p = os.path.join(self.tmp, "result.txt")
        with open(p, "w") as f:
            f.write("PRIVATE-TEST-OUTPUT line 1\n")
        code, _, err = st.cli(self.home, "verdict", st.ROOT, "--status", "passed", "--criterion", "T1=passed", "--attach", "suite1=" + p)
        self.assertEqual(code, 0, err)
        e = ev.evaluations(self.conn, st.ROOT)[0]
        self.assertRegex(e["evidence"][0], r"^check-run:suite1\.[0-9a-f]{12}$")
        dump = "".join(str(r) for r in self.conn.execute("SELECT data FROM event"))
        self.assertNotIn("PRIVATE-TEST-OUTPUT", dump)


class TestPrecedenceAndCutoff(Base):
    def test_operator_outranks_later_automated(self):
        self.auto_pass(t=1800000060.0)
        st.cli(self.home, "verdict", st.ROOT, "--status", "partial", "--criterion", "C1=failed", "--rest", "passed", "--rubric", "architecture-review", "--no-supersede", "--effective-at", "1800000070", now=1800000070.0)
        self.auto_pass(t=1800000130.0, status="passed")
        self.assertEqual(ev.effective_status(self.conn, st.ROOT, 1800000200.0), "partial")
        self.assertEqual(len(ev.evaluations(self.conn, st.ROOT)), 3, "every evaluation stays visible")

    def test_automated_cannot_supersede_operator(self):
        st.cli(self.home, "verdict", st.ROOT, "--status", "partial", "--criterion", "C1=failed", "--rest", "passed", "--rubric", "architecture-review", now=1800000120.0)
        op = ev.evaluations(self.conn, st.ROOT)[0]
        self.auto_pass(t=1800000150.0, supersedes=op["eval"])
        self.assertEqual(ev.effective_status(self.conn, st.ROOT, 1800000200.0), "partial")

    def test_back_dated_verdict_cannot_rewrite_a_saved_report(self):
        self.auto_pass(t=1800000060.0)
        saved = ev.effective_status(self.conn, st.ROOT, 1800000100.0)
        st.cli(self.home, "verdict", st.ROOT, "--status", "failed", "--criterion", "C1=failed", "--criterion", "C2=failed", "--criterion", "C3=failed", "--rubric", "architecture-review",
               "--effective-at", "1800000010", now=1800000300.0)
        self.assertEqual(ev.effective_status(self.conn, st.ROOT, 1800000100.0), saved, "recorded after the cutoff: not eligible for it")
        self.assertEqual(ev.effective_status(self.conn, st.ROOT, 1800000400.0), "failed")

    def test_tie_on_effective_at_goes_to_higher_seq(self):
        self.auto_pass(t=1800000060.0)
        self.auto_pass(t=1800000060.0, status="passed")
        evs = ev.evaluations(self.conn, st.ROOT)
        best, _ = ev.effective(evs, None)
        self.assertEqual(best["eval"], evs[1]["eval"])

    def test_unreviewed_when_no_evaluation(self):
        self.assertEqual(ev.effective_status(self.conn, st.CHILD, None), "unreviewed")


class TestProjection(Base):
    def test_projects_fixture_events_and_matches_golden(self):
        import sqlite3
        c2 = st.make_store(os.path.join(self.tmp, "second"))
        self.addCleanup(c2.close)
        st.load_release(c2, with_evaluation_events=True)
        # step E: the importer projects audit_evaluation events in its final transaction; a later project_missing finds nothing
        self.assertEqual(c2.execute("SELECT count(*) FROM evaluation").fetchone()[0], 3)
        self.assertEqual(ev.project_missing(c2), 0, "idempotent")
        c2.execute("DELETE FROM evaluation")  # a lost projection (e.g. restored store) is rebuilt from the events
        self.assertEqual(ev.project_missing(c2), 3)
        self.assertEqual(ev.project_missing(c2), 0, "idempotent")
        self.assertEqual(ev.effective_status(c2, ST_ROOT, 1800000100.0), "passed")
        self.assertEqual(ev.effective_status(c2, ST_ROOT, 1800000200.0), "partial")
        self.assertEqual(ev.effective_status(c2, st.CHILD, 1800000200.0), "passed")

    def test_store_event_is_idempotent(self):
        e = ev.build_verdict(self.conn, self.home, st.ROOT, "passed", [("C1", "passed")], now=1800000060.0)
        self.assertTrue(ev.store_event(self.conn, e))
        g = self.gen()
        self.assertFalse(ev.store_event(self.conn, e))
        self.assertEqual(self.gen(), g)


ST_ROOT = st.ROOT


class TestDenied(helpers.ProTestCase):
    names = ("alpha",)

    def setUp(self):
        super().setUp()
        self.conn = st.make_store(self.home)
        st.load_release(self.conn)
        self.addCleanup(self.conn.close)

    def test_owner_can_record_through_the_real_cli(self):
        r = self.ok("audit", "verdict", st.ROOT, "--status", "partial", "--criterion", "C1=failed", "--criterion", "C2=passed", "--json")
        self.assertEqual(json.loads(r.stdout)["status"], "partial")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM evaluation").fetchone()[0], 1)

    def test_professional_is_denied_every_audit_write(self):
        for argv in (("audit", "verdict", st.ROOT, "--status", "passed", "--criterion", "C1=passed"), ("audit", "rubric", "add", "x", "v1", "--criterion", "C1"),
                     ("audit", "export", "--yes", "--out", os.path.join(self.tmp, "o"))):
            r = self.pro(*argv, as_pro="alpha")
            self.assertEqual(r.returncode, 4, (argv, r.stdout, r.stderr))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM evaluation").fetchone()[0], 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "o")))

    def test_session_env_is_denied_too(self):
        r = self.pro("audit", "verdict", st.ROOT, "--status", "passed", "--criterion", "C1=passed", env={"PRO_SESSION": "pro-alpha--000001"})
        self.assertEqual(r.returncode, 4)


if __name__ == "__main__":
    unittest.main()
