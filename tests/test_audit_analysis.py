"""Audit F3: task-class rubrics, stratified summaries, benchmark datasets, optional statsmodels (GEE). Core never imports statsmodels."""
import ast
import glob
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import warnings

import helpers
warnings.filterwarnings("ignore", category=ResourceWarning)
import audit_d4_store as st

sys.path.insert(0, helpers.HERE)
from pros_audit import analysis as A  # noqa: E402
from pros_audit import cli as audit_cli  # noqa: E402
from pros_audit import evaluation as ev  # noqa: E402

try:
    import statsmodels.api  # noqa: F401
    import numpy  # noqa: F401
    HAVE_SM = True
except Exception:
    HAVE_SM = False

NOW = 1800000300.0
LIB = os.path.join(helpers.HERE, "..", "plugins", "professionals", "lib")


def run_cli(home, *argv, now=NOW):
    o, e = io.StringIO(), io.StringIO()
    code = audit_cli.main(list(argv), home, lambda *a: None, now=lambda: now, out=o, err=e)
    return code, o.getvalue(), e.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="auditf3-")
        self.home = os.path.join(self.tmp, "home")
        self.conn = st.make_store(self.home)
        st.load_release(self.conn)
        st.add_rubrics(self.home)
        self.addCleanup(self.conn.close)
        ev.record_verdict(self.conn, self.home, st.ROOT, "partial", [("C1", "failed"), ("C2", "passed"), ("C3", "passed")], rubric="architecture-review",
                          evaluator="check", effective_at=1800000120.0, now=1800000120.0, supersedes=None)
        ev.record_verdict(self.conn, self.home, st.CHILD, "passed", [("C1", "passed"), ("CANARY-CRIT-64", "passed"), ("C3", "passed")], rubric="CANARY-RUBRIC-63",
                          evaluator="check", effective_at=1800000125.0, now=1800000125.0, supersedes=None)


class TestTaskClasses(Base):
    def test_class_maps_rubric_and_classifies_effective_verdicts(self):
        A.class_add(self.home, "design-review", "architecture-review", "r1")
        self.assertEqual(A.classes(self.home), {"design-review": {"class": "design-review", "rubric": "architecture-review", "version": "r1"}})
        c = A.classify(self.conn, self.home, NOW)
        self.assertEqual(c, {st.ROOT: "design-review"})
        rows = {r["msg"]: r for r in A.outcome_rows(self.conn, self.home, cutoff=NOW)}
        self.assertEqual(rows[st.ROOT]["task_class"], "design-review")
        self.assertEqual(rows[st.CHILD]["task_class"], "unclassified")

    def test_class_add_can_create_the_rubric_and_refuses_bad_input(self):
        A.class_add(self.home, "triage", "triage-check", "v1", [("T1", True), ("T2", False)])
        self.assertEqual([c["id"] for c in ev.rubric_load(self.home, "triage-check", "v1")["criteria"]], ["T1", "T2"])
        with self.assertRaises(A.AnalysisError):
            A.class_add(self.home, "triage", "triage-check", "v1")
        with self.assertRaises(A.AnalysisError):
            A.class_add(self.home, "x", "no-such", "v1")
        with self.assertRaises(A.AnalysisError):
            A.class_add(self.home, "bad name", "architecture-review", "r1")


class TestStrata(Base):
    def test_stratified_counts_rule_and_small_sample(self):
        A.class_add(self.home, "design-review", "architecture-review", "r1")
        r = A.stratified(self.conn, self.home, "task_class", cutoff=NOW)
        by = {s["stratum"]: s for s in r["strata"]}
        self.assertEqual(by["design-review"]["counts"]["partial"], 1)
        self.assertEqual(by["design-review"]["pass_rate"], [0, 1])
        self.assertEqual(by["unclassified"]["pass_rate"], [1, 1])
        self.assertTrue(all(s["small_sample"] for s in r["strata"]))
        self.assertIn("passed / conclusive", r["rule"])
        rv = {s["stratum"] for s in A.stratified(self.conn, self.home, "revision", cutoff=NOW)["strata"]}
        self.assertEqual(rv, {"111111111111", "222222222222"})
        self.assertEqual(len(A.stratified(self.conn, self.home, "direct", cutoff=NOW, roots_only=True)["strata"]), 1)
        with self.assertRaises(A.AnalysisError):
            A.stratified(self.conn, self.home, "nope")

    def test_wilson_interval(self):
        self.assertIsNone(A.wilson(0, 0))
        lo, hi = A.wilson(8, 10)
        self.assertAlmostEqual(lo, 0.4902, 3)
        self.assertAlmostEqual(hi, 0.9433, 3)
        self.assertEqual(A.wilson(0, 3)[0], 0.0)

    def test_unreviewed_and_unanswered_handled(self):
        ev_rows = A.outcome_rows(self.conn, self.home, since=NOW + 1, cutoff=NOW + 2)
        self.assertEqual(ev_rows, [])
        r = A.outcome_rows(self.conn, self.home, cutoff=1800000010.0)  # before the root was answered
        self.assertEqual({x["msg"] for x in r}, {st.CHILD})


DS = {"format": A.DATASET_FORMAT, "version": 1, "name": "arch-gold", "task_class": "design-review", "rubric": "architecture-review", "rubric_version": "r1",
      "cases": [{"id": "c1", "expected": {"C1": "passed", "C2": "passed", "C3": "passed"}, "status": "passed"},
                {"id": "c2", "expected": {"C1": "failed", "C2": "passed", "C3": "passed"}, "status": "partial", "ref": "note:gold-2"},
                {"id": "c3", "expected": {"C1": "failed", "C2": "failed", "C3": "failed"}}]}


class TestBenchmark(Base):
    def test_valid_dataset(self):
        self.assertEqual(A.validate_dataset(DS, self.home), [])
        self.assertEqual(A.validate_dataset(DS), [])

    def test_problems_reported(self):
        import copy
        d = copy.deepcopy(DS)
        d["cases"][0]["status"] = "failed"
        d["cases"][1]["id"] = "c1"
        d["cases"][2]["expected"]["ZZ"] = "passed"
        d["cases"].append({"id": "c4", "expected": {"C1": "maybe"}})
        d["cases"].append({"id": "c5", "expected": {"C1": "passed"}, "prompt": "free text"})
        bad = "\n".join(A.validate_dataset(d, self.home))
        for frag in ("contradicts", "repeated", "not in the rubric", "expected must map", "unknown or missing fields"):
            self.assertIn(frag, bad)
        self.assertTrue(A.validate_dataset({"format": "x"}))
        self.assertTrue(A.validate_dataset(dict(DS, rubric="no-such"), self.home))
        self.assertTrue(A.validate_dataset(dict(DS, cases=[])))

    def test_score(self):
        res = A.score_dataset(DS, {"c1": {"C1": "passed", "C2": "passed", "C3": "passed"}, "c2": {"C1": "passed", "C2": "passed", "C3": "passed"}}, self.home)
        self.assertEqual((res["scored"], res["missing"]), (2, ["c3"]))
        self.assertEqual(res["case_agreement"], [1, 2])
        self.assertEqual(res["criterion_agreement"], [5, 6])
        self.assertEqual(res["status_agreement"], [1, 2])
        self.assertEqual([r for r in res["per_case"] if not r["agree"]][0]["differs"], ["C1"])

    def test_cli_check_and_score(self):
        p, q = os.path.join(self.tmp, "ds.json"), os.path.join(self.tmp, "res.json")
        with open(p, "w") as f:
            json.dump(DS, f)
        with open(q, "w") as f:
            json.dump({"c1": {"C1": "passed", "C2": "passed", "C3": "passed"}}, f)
        code, out, err = run_cli(self.home, "analyze", "benchmark-check", p)
        self.assertEqual((code, "3 case" in out), (0, True), err)
        code, out, err = run_cli(self.home, "analyze", "benchmark-score", p, q, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["case_agreement"], [1, 1])
        code, _, err = run_cli(self.home, "analyze", "benchmark-check", os.path.join(self.tmp, "missing.json"))
        self.assertEqual(code, 3)


class TestCli(Base):
    def test_class_and_strata_commands(self):
        code, out, err = run_cli(self.home, "analyze", "class-add", "design-review", "--rubric", "architecture-review", "--rubric-version", "r1")
        self.assertEqual(code, 0, err)
        code, out, _ = run_cli(self.home, "analyze", "class-list")
        self.assertIn("design-review -> architecture-review r1", out)
        code, out, err = run_cli(self.home, "analyze", "strata", "--since", "1799999000", "--by", "task_class")
        self.assertEqual(code, 0, err)
        self.assertIn("design-review: n=1 (small sample)", out)
        code, out, _ = run_cli(self.home, "analyze", "strata", "--since", "1799999000", "--json")
        self.assertEqual(json.loads(out)["by"], "task_class")


def synthetic(n_clusters, per=4):
    rows = []
    for c in range(n_clusters):
        for i in range(per):
            rev = "rev-a" if i % 2 == 0 else "rev-b"
            rows.append({"msg": "m", "status": "passed" if (c + i) % 3 else "failed", "task_class": "unclassified", "professional": "p%d" % c, "revision": rev, "direct": "direct", "root": True})
    return rows


class TestGee(Base):
    def test_frame_uses_conclusive_outcomes_only(self):
        f = A.gee_frame(self.conn, self.home, cutoff=NOW)
        self.assertEqual(sorted((d["y"], d["x"]) for d in f), [(0, "111111111111"), (1, "222222222222")])
        with self.assertRaises(A.AnalysisError):
            A.gee_frame(self.conn, self.home, cluster_by="nope")

    def test_reports_why_it_cannot_run(self):
        r = A.gee_pass(self.conn, self.home, cutoff=NOW)
        self.assertFalse(r["ok"])
        self.assertEqual((r["n"], r["clusters"]), (2, 2))
        self.assertTrue(("not installed" in r["reason"]) if not HAVE_SM else ("only 2 cluster" in r["reason"]))

    def test_too_few_clusters_with_statsmodels_stubbed_present(self):
        orig = A.statsmodels_available
        A.statsmodels_available = lambda: True
        try:
            r = A.gee_pass(self.conn, self.home, cutoff=NOW, min_clusters=10)
        finally:
            A.statsmodels_available = orig
        self.assertFalse(r["ok"])
        self.assertIn("need at least 10", r["reason"])

    def test_cli_gee_exit_zero_with_reason(self):
        code, out, err = run_cli(self.home, "analyze", "gee", "--since", "1799999000")
        self.assertEqual(code, 0, err)
        self.assertFalse(json.loads(out)["ok"])
        self.assertIn("GEE not run", err)

    @unittest.skipUnless(HAVE_SM, "statsmodels not installed")
    def test_gee_fits_with_enough_clusters(self):
        orig = A.outcome_rows
        A.outcome_rows = lambda *a, **k: synthetic(20)
        try:
            r = A.gee_pass(self.conn, self.home)
        finally:
            A.outcome_rows = orig
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["clusters"], r["levels"]), (20, ["rev-a", "rev-b"]))
        self.assertEqual(len(r["terms"]), 2)
        self.assertEqual(r["se"], "robust")

    @unittest.skipUnless(HAVE_SM, "statsmodels not installed")
    def test_single_level_exposure_reported(self):
        orig = A.outcome_rows
        A.outcome_rows = lambda *a, **k: [dict(r, revision="rev-a") for r in synthetic(12)]
        try:
            r = A.gee_pass(self.conn, self.home)
        finally:
            A.outcome_rows = orig
        self.assertFalse(r["ok"])
        self.assertIn("single level", r["reason"])


class TestCoreIndependence(unittest.TestCase):
    def test_no_module_imports_statsmodels_numpy_or_pandas_at_top_level(self):
        for p in glob.glob(os.path.join(LIB, "pros_audit", "*.py")):
            with open(p, encoding="utf-8") as fh:
                body = ast.parse(fh.read()).body
            for node in body:
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for n in names:
                    self.assertNotIn(n.split(".")[0], ("statsmodels", "numpy", "pandas", "scipy"), "%s imports %s at module level" % (os.path.basename(p), n))

    def test_core_works_with_statsmodels_blocked(self):
        code = ("import sys\nfor m in ('statsmodels','statsmodels.api','numpy','pandas','scipy'): sys.modules[m]=None\n"
                "sys.path.insert(0, %r)\n"
                "from pros_audit import cli, analysis, reports, query, export, evaluation\n"
                "cli.build_parser()\nassert analysis.statsmodels_available() is False\nprint('ok')\n" % LIB)
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"), r.stderr)


if __name__ == "__main__":
    unittest.main()
