"""Audit F2: saved report definitions, local snapshots (reproducible, versioned), compare by revision, CSV/HTML export without private tokens."""
import io
import json
import os
import re
import stat
import sys
import tempfile
import unittest
import warnings

import helpers
warnings.filterwarnings("ignore", category=ResourceWarning)
import audit_d4_store as st

sys.path.insert(0, helpers.HERE)
from pros_audit import cli as audit_cli  # noqa: E402
from pros_audit import evaluation as ev  # noqa: E402
from pros_audit import query as Q  # noqa: E402
from pros_audit import reports as R  # noqa: E402

NOW = 1800000300.0
SINCE = "1799999000"
REAL_ID = re.compile(r"m-[0-9a-f]{10}|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}|p-0000|r-0000|fx-root|a1b2c3d4e5f60718|0a1b2c3d4e5f|pro-[a-z]+-")


def run_cli(home, *argv, now=NOW, owner=True):
    o, e = io.StringIO(), io.StringIO()

    def require_user(what, why=None):
        if not owner:
            raise SystemExit("pros: %s is for the user only" % what)
    try:
        code = audit_cli.main(list(argv), home, require_user, now=lambda: now, out=o, err=e)
    except SystemExit as x:
        code = 4 if x.code else 0
        e.write(str(x.code))
    return code, o.getvalue(), e.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="auditf2-")
        self.home = os.path.join(self.tmp, "home")
        self.conn = st.make_store(self.home)
        st.load_release(self.conn)
        st.add_rubrics(self.home)
        self.addCleanup(self.conn.close)
        ev.record_verdict(self.conn, self.home, st.ROOT, "partial", [("C1", "failed"), ("C2", "passed"), ("C3", "passed")], rubric="architecture-review",
                          evaluator="CANARY-EVAL-61", effective_at=1800000120.0, now=1800000120.0, supersedes=None)
        ev.record_verdict(self.conn, self.home, st.CHILD, "partial", [("C1", "failed"), ("CANARY-CRIT-64", "passed"), ("C3", "passed")], rubric="CANARY-RUBRIC-63",
                          evaluator="CANARY-EVAL-61", effective_at=1800000125.0, now=1800000125.0, supersedes=None)
        self.q = Q.Query(audit_cli.store_path(self.home), owner_check=lambda: True, now=lambda: NOW)

    def save(self, name="weekly", **spec):
        return R.save_definition(self.conn, name, dict({"since": SINCE, "group_by": "revision"}, **spec), NOW)

    def snap(self, name="weekly", now=NOW):
        return R.render_snapshot(self.q, self.conn, self.home, R.load_definition(self.conn, name), now)


class TestDefinitions(Base):
    def test_save_is_idempotent_and_versioned(self):
        a = self.save()
        b = self.save()
        self.assertEqual(a["id"], b["id"])
        c = self.save(professional="system-designer")
        self.assertNotEqual(a["id"], c["id"])
        self.assertEqual(R.load_definition(self.conn, "weekly")["id"], c["id"], "newest version is current")
        listed = R.list_definitions(self.conn)
        self.assertEqual([(d["name"], d["versions"]) for d in listed], [("weekly", 2)])

    def test_bad_input_refused(self):
        with self.assertRaises(R.ReportError):
            R.save_definition(self.conn, "bad name!", {}, NOW)
        with self.assertRaises(R.ReportError):
            R.save_definition(self.conn, "x", {"unknown": 1}, NOW)
        with self.assertRaises(R.ReportError):
            R.save_definition(self.conn, "x", {"group_by": "nope"}, NOW)
        with self.assertRaises(R.ReportError) as c:
            R.load_definition(self.conn, "missing")
        self.assertEqual(c.exception.code, "not_found")


class TestSnapshots(Base):
    def test_snapshot_records_what_makes_it_reproducible(self):
        self.save()
        s, path = self.snap()
        self.assertEqual(s["metric_version"], R.METRIC_VERSION)
        self.assertEqual(s["cutoff"], NOW)
        self.assertEqual(s["spec"]["group_by"], ["revision"])
        self.assertEqual(s["rubric_versions"], {"CANARY-RUBRIC-63": ["r1"], "architecture-review": ["r1"]})
        self.assertEqual(s["source_revisions"], ["111111111111", "222222222222", "333333333333"])
        self.assertRegex(s["store_snapshot"]["id"], r"^s-[0-9a-f]{12}$")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)

    def test_same_state_same_snapshot_new_verdict_new_snapshot(self):
        self.save()
        a, _ = self.snap()
        b, _ = self.snap(now=NOW)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(R.list_snapshots(self.home, "weekly")), 1)
        ev.record_verdict(self.conn, self.home, st.ROOT, "failed", [("C1", "failed"), ("C2", "failed"), ("C3", "failed")], rubric="architecture-review",
                          evaluator="CANARY-EVAL-61", effective_at=1800000200.0, now=1800000200.0)
        c, _ = self.snap()
        self.assertNotEqual(a["id"], c["id"])
        self.assertEqual(len(R.list_snapshots(self.home, "weekly")), 2)
        self.assertEqual(R.load_snapshot(self.home, a["id"])["result"], a["result"], "the old snapshot stays as rendered")

    def test_snapshot_id_must_be_well_formed(self):
        with self.assertRaises(R.ReportError):
            R.load_snapshot(self.home, "../../etc/passwd")


class TestCompare(Base):
    def test_rows_per_revision_with_small_sample_guard(self):
        c = R.compare_by_revision(self.q, self.conn, {"since": SINCE}, NOW)
        revs = [r["revision"] for r in c["rows"]]
        self.assertEqual(revs, ["111111111111", "222222222222", "unknown"])
        self.assertTrue(c["rows"][0]["baseline"])
        self.assertTrue(all(r["small_sample"] for r in c["rows"]))
        self.assertTrue(all(r["delta_pass_rate"] is None and r["delta_deadline_miss_rate"] is None for r in c["rows"]), "no delta for groups under n=5")
        self.assertEqual(sum(r["assignments"] for r in c["rows"]), 3)
        self.assertIn("observational", c["note"])

    def test_cli_compare_text_and_json(self):
        code, out, err = run_cli(self.home, "compare", "--by", "revision", "--since", SINCE)
        self.assertEqual(code, 0, err)
        self.assertIn("(baseline)", out)
        self.assertIn("small sample", out)
        code, out, _ = run_cli(self.home, "compare", "--by", "revision", "--since", SINCE, "--json")
        self.assertEqual([r["revision"] for r in json.loads(out)["rows"]][0], "111111111111")


class TestExport(Base):
    def export(self, fmt, name="o"):
        self.save(name="CANARY-REPORT-70")
        s, _ = self.snap("CANARY-REPORT-70")
        p = os.path.join(self.tmp, name + "." + fmt)
        m = R.export_snapshot(self.conn, s, fmt, p)
        with open(p, encoding="utf-8") as f:
            return f.read(), m, p

    def test_csv_and_html_carry_no_private_token_or_real_id(self):
        for fmt in ("csv", "html"):
            text, m, p = self.export(fmt, "out-" + fmt)
            low = text.lower()
            for c in st.BODY_CANARIES + st.PLANTED + ["CANARY-REPORT-70"]:
                self.assertNotIn(c.lower(), low, (fmt, c))
            self.assertNotIn("canary", low)
            self.assertNotRegex(text, REAL_ID, fmt)
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertEqual(m["bytes"], len(text.encode("utf-8")))

    def test_marketplace_names_stay_revisions_are_pseudonyms_joined_per_export(self):
        text, _, _ = self.export("csv")
        self.assertRegex(text, r"v-[0-9a-f]{12}")
        self.assertNotIn("111111111111", text)
        rows = text.splitlines()
        self.assertGreater(len(rows), 3)
        key = os.urandom(32)
        self.save()
        s, _ = self.snap()
        _, v1 = R.redacted_view(self.conn, s, key)
        _, v2 = R.redacted_view(self.conn, s, key)
        _, v3 = R.redacted_view(self.conn, s, os.urandom(32))
        self.assertEqual(v1["rows"], v2["rows"], "same key: stable pseudonyms")
        k = lambda v: sorted(r["key_revision"] for r in v["rows"] if r["key_revision"] != "unknown")
        self.assertEqual(len(k(v1)), 2)
        self.assertTrue(set(k(v1)).isdisjoint(k(v3)), "another export, another pseudonym")

    def test_html_is_self_contained(self):
        text, _, _ = self.export("html")
        self.assertNotRegex(text.lower(), r"<script|<link|<img|<iframe|src=|href=|https?://")
        self.assertIn("default-src 'none'", text)

    def test_scan_blocks_a_leak_before_writing(self):
        self.save()
        s, _ = self.snap()
        red, view = R.redacted_view(self.conn, s)
        view["report"] = "CANARY-TASK-67"  # simulate a redaction bug: a private task token reaches the output
        with self.assertRaises(R.ReportError) as c:
            R.scan(R.to_html(view), R.sensitive_tokens(self.conn, red))
        self.assertEqual(c.exception.code, "leak")
        with self.assertRaises(R.ReportError):
            R.scan(R.to_csv({"rows": [{"a": "canary-local-66"}]}), R.sensitive_tokens(self.conn, red))

    def test_never_overwrites(self):
        text, _, p = self.export("csv")
        s, _ = self.snap("CANARY-REPORT-70")
        with self.assertRaises(R.ReportError):
            R.export_snapshot(self.conn, s, "csv", p)

    def test_csv_cells_are_formula_safe(self):
        self.assertTrue(R.to_csv({"rows": [{"a": "=1+1"}]}).splitlines()[1].startswith("'="))


class TestCli(Base):
    def test_full_flow_and_owner_only(self):
        code, out, err = run_cli(self.home, "report", "save", "weekly", "--since", SINCE, "--by", "revision")
        self.assertEqual(code, 0, err)
        code, out, err = run_cli(self.home, "report", "run", "weekly", "--json")
        self.assertEqual(code, 0, err)
        sid = json.loads(out)["snapshot"]
        code, out, _ = run_cli(self.home, "report", "list")
        self.assertIn("weekly", out)
        code, out, _ = run_cli(self.home, "report", "show", sid)
        self.assertIn("key_revision", out)
        dest = os.path.join(self.tmp, "r.html")
        code, out, err = run_cli(self.home, "report", "export", sid, "--format", "html", "--out", dest)
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(dest))
        code, _, err = run_cli(self.home, "report", "export", sid, "--out", dest)
        self.assertEqual(code, 2)
        code, _, _ = run_cli(self.home, "report", "run", "nope")
        self.assertEqual(code, 3)
        for argv in (("report", "list"), ("compare", "--by", "revision")):
            code, _, err = run_cli(self.home, *argv, owner=False)
            self.assertEqual(code, 4, argv)
            self.assertIn("user only", err)


if __name__ == "__main__":
    unittest.main()
