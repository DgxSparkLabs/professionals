"""Audit D4: export. Redacted by default, per-export pseudonyms that keep joins, no planted canary anywhere (data, manifest, HTML metadata), raw separate and explicit."""
import csv
import glob
import html.parser
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
import audit_schema as sch  # noqa: E402
from pros_audit import evaluation as ev  # noqa: E402
from pros_audit import export as ex  # noqa: E402
from pros_audit import store  # noqa: E402

MANIFEST = sch.load("export-manifest-v1.json")
NOW = 1800000300.0
REAL_ID = re.compile(r"m-[0-9a-f]{10}|[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}|p-0000|r-0000|fx-root|a1b2c3d4e5f60718|0a1b2c3d4e5f|pro-[a-z]+-")


def read_all(d):
    out = {}
    for p in sorted(glob.glob(os.path.join(d, "*"))):
        with open(p, encoding="utf-8") as f:
            out[os.path.basename(p)] = f.read()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="auditd4x-")
        self.home = os.path.join(self.tmp, "home")
        self.conn = st.make_store(self.home)
        st.load_release(self.conn)
        st.add_rubrics(self.home)
        self.addCleanup(self.conn.close)
        # the release verdicts, with planted private tokens in evaluator, rubric, criterion and evidence
        ev.record_verdict(self.conn, self.home, st.ROOT, "passed", [("C1", "passed"), ("C2", "passed"), ("C3", "passed")], rubric="architecture-review", evaluator="check",
                          evaluator_kind="automated", effective_at=1800000060.0, now=1800000060.0, supersedes=None)
        code, _, err = st.cli(self.home, "verdict", st.ROOT, "--status", "partial", "--criterion", "C1=failed@note:CANARY-NOTE-65", "--rest", "passed", "--rubric", "architecture-review",
                              "--evaluator", "CANARY-EVAL-61", "--evidence", "note:CANARY-NOTE-65", "--effective-at", "1800000120", now=1800000120.0)
        assert code == 0, err
        code, _, err = st.cli(self.home, "verdict", st.CHILD, "--status", "partial", "--criterion", "C1=failed@note:CANARY-NOTE-65", "--criterion", "CANARY-CRIT-64=passed", "--criterion", "C3=passed",
                              "--rubric", "CANARY-RUBRIC-63", "--evaluator", "CANARY-EVAL-61", now=1800000125.0)
        assert code == 0, err

    def export(self, fmt="jsonl", *extra, name=None):
        d = os.path.join(self.tmp, name or "out-" + fmt)
        code, out, err = st.cli(self.home, "export", "--redact", "--format", fmt, "--out", d, "--yes", *extra, now=NOW)
        self.assertEqual(code, 0, err)
        return d


class TestRedactedExport(Base):
    def test_no_planted_or_body_canary_in_any_format_or_manifest(self):
        for fmt in ("jsonl", "csv", "html"):
            d = self.export(fmt)
            text = "\n".join(read_all(d).values())
            low = text.lower()
            for c in st.BODY_CANARIES + st.PLANTED:
                self.assertNotIn(c.lower(), low, (fmt, c))
            self.assertNotIn("canary", low)
            self.assertNotRegex(text, REAL_ID, fmt)

    def test_jsonl_preserves_joins_under_pseudonyms(self):
        d = self.export("jsonl")
        rows = {n: [json.loads(x) for x in open(os.path.join(d, n + ".jsonl")) if x.strip()] for n in ex.TABLES}
        a = rows["assignments"]
        self.assertEqual(len(a), 3)
        ids = {r["assignment"] for r in a}
        self.assertEqual(len(ids), 3)
        root = [r for r in a if r["parent"] is None and r["to"] == "system-designer"][0]
        child = [r for r in a if r["parent"] is not None][0]
        self.assertEqual(child["parent"], root["assignment"])
        self.assertEqual(child["root"], root["assignment"])
        self.assertEqual(child["to"], "reliability-reviewer", "marketplace names stay")
        local = [r for r in a if r["to"] not in ("system-designer", "reliability-reviewer")][0]
        self.assertRegex(local["to"], r"^p-[0-9a-f]{12}$", "a local folder name is pseudonymized")
        self.assertEqual(root["task"], local["task"], "the same task id maps alike")
        self.assertNotEqual(root["task"], root["assignment"], "class separates families")
        # evaluation joins, effective status per assignment and own tokens
        self.assertEqual({r["evaluation"] for r in a if r["assignment"] == root["assignment"]}, {"partial"})
        self.assertEqual((root["tokens"], child["tokens"]), (140, 80))
        self.assertEqual(root["deadline_status"], "late")
        e = rows["evaluations"]
        self.assertEqual({x["target"] for x in e}, {root["assignment"], child["assignment"]})
        sup = [x for x in e if x["supersedes"]][0]
        self.assertIn(sup["supersedes"], {x["evaluation"] for x in e})
        self.assertEqual(sorted(c["outcome"] for c in sup["criteria"]), ["failed", "passed", "passed"])
        self.assertTrue(all(isinstance(x["evidence"], int) for x in e))
        # exchanges join on messages and sessions
        ex_msgs = {x["msg"] for x in rows["exchanges"]} - {None}
        self.assertEqual(ex_msgs, {root["assignment"], child["assignment"]})
        self.assertEqual({x["session"] for x in rows["exchanges"] if x["msg"] == root["assignment"]}, {root["session"]})
        # events join too, and spawn data lost its tmux name
        ev_msgs = {x["msg"] for x in rows["events"]} - {None}
        self.assertTrue({root["assignment"], child["assignment"]} <= ev_msgs)
        spawn = [x for x in rows["events"] if x["action"] == "spawn"][0]
        self.assertNotIn("tmux", spawn["data"])
        self.assertIn("kind", spawn["data"])
        cfg = [x for x in rows["events"] if x["action"] == "audit_config"][0]
        self.assertEqual(cfg["data"]["setting"], "capture_mode")
        self.assertRegex(cfg["data"]["value"], r"^l-[0-9a-f]{12}$")

    def test_times_are_whole_seconds(self):
        d = self.export("jsonl")
        for x in (json.loads(l) for l in open(os.path.join(d, "assignments.jsonl"))):
            self.assertEqual(x["created"], float(int(x["created"])))
        m = json.load(open(os.path.join(d, "manifest.json")))
        self.assertEqual(m["redaction"]["time_bucket_s"], 1)

    def test_manifest_valid_counts_hashes_and_no_store_ids(self):
        import hashlib
        d = self.export("jsonl")
        m = json.load(open(os.path.join(d, "manifest.json")))
        self.assertEqual(sch.validate(m, MANIFEST), [])
        self.assertEqual((m["mode"], m["format"], m["schema_version"], m["redaction"]["salt_stored"]), ("redacted", "jsonl", store.KNOWN_VERSION, False))
        self.assertEqual(m["counts"]["assignments"], 3)
        self.assertEqual(m["counts"]["evaluations"], 3)
        self.assertEqual(m["counts"]["exchanges"], 4)
        self.assertIn("task", m["redaction"]["pseudonymized"])
        self.assertIn("tmux", m["redaction"]["dropped_fields"])
        for f in m["files"]:
            body = open(os.path.join(d, f["name"]), "rb").read()
            self.assertEqual((hashlib.sha256(body).hexdigest(), len(body)), (f["sha256"], f["bytes"]))
        self.assertRegex(m["snapshot"]["id"], r"^s-[0-9a-f]{12}$")
        self.assertIn("system-designer", m["scope"]["professionals"])
        self.assertNotIn("canary", json.dumps(m).lower())
        self.assertEqual({x["source"]: x["quality"] for x in m["coverage"]["sources"]}, {"emitter": "observed", "transcript": "observed", "session_counter": "missing"})

    def test_pseudonyms_stable_within_and_different_across_exports(self):
        d1, d2 = self.export("jsonl", name="e1"), self.export("jsonl", name="e2")
        a1 = [json.loads(l) for l in open(os.path.join(d1, "assignments.jsonl"))]
        a2 = [json.loads(l) for l in open(os.path.join(d2, "assignments.jsonl"))]
        self.assertEqual(len(a1), len(a2))
        self.assertTrue(set(r["assignment"] for r in a1).isdisjoint(r["assignment"] for r in a2), "no join across two exports")
        self.assertTrue(set(r["task"] for r in a1).isdisjoint(r["task"] for r in a2))

    def test_fixed_key_is_deterministic_and_key_changes_everything(self):
        r1, r2 = ex.Redactor(False, key=b"k" * 32), ex.Redactor(False, key=b"k" * 32)
        self.assertEqual(r1.pseudo("task", "x"), r2.pseudo("task", "x"))
        self.assertNotEqual(r1.pseudo("task", "x"), r1.pseudo("message", "x"))
        self.assertNotEqual(r1.pseudo("task", "x"), ex.Redactor(False, key=b"j" * 32).pseudo("task", "x"))

    def test_the_key_is_never_stored(self):
        for d in (self.export("jsonl"),):
            blob = "".join(read_all(d).values())
        dump = "".join(str(r) for r in self.conn.execute("SELECT data FROM event WHERE action='audit_export'"))
        self.assertNotIn("key", dump.lower().replace("keys", ""))

    def test_export_recorded_with_manifest_hash_and_no_copy_in_store(self):
        import hashlib
        g = self.conn.execute("SELECT generation FROM store_state").fetchone()[0]
        d = self.export("jsonl")
        row = self.conn.execute("SELECT data FROM event WHERE action='audit_export'").fetchone()
        data = json.loads(row[0])
        self.assertEqual(data["mode"], "redacted")
        self.assertEqual(data["manifest_sha256"], hashlib.sha256(open(os.path.join(d, "manifest.json"), "rb").read()).hexdigest())
        self.assertEqual(self.conn.execute("SELECT generation FROM store_state").fetchone()[0], g + 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM audit_log WHERE action='audit_export'").fetchone()[0], 1)
        m = json.load(open(os.path.join(d, "manifest.json")))
        self.assertEqual(data["rows"], sum(m["counts"].values()))

    def test_file_modes(self):
        d = self.export("jsonl")
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        for p in glob.glob(os.path.join(d, "*")):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_since_until_scope(self):
        d = self.export("jsonl", "--since", "1800000060", "--until", "1800000200", name="win")
        a = [json.loads(l) for l in open(os.path.join(d, "assignments.jsonl"))]
        self.assertEqual(len(a), 1, "only the local root was created in the window")
        m = json.load(open(os.path.join(d, "manifest.json")))
        self.assertEqual((m["scope"]["from"], m["scope"]["to"]), (1800000060.0, 1800000200.0))

    def test_preview_writes_nothing_and_needs_confirmation(self):
        d = os.path.join(self.tmp, "pv")
        code, out, _ = st.cli(self.home, "export", "--preview", "--out", d, now=NOW)
        self.assertEqual(code, 0)
        self.assertIn("assignments: 3 rows", out)
        self.assertFalse(os.path.exists(d))
        code, _, err = st.cli(self.home, "export", "--out", d, now=NOW)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(d))
        code, _, _ = st.cli(self.home, "export", "--out", d, now=NOW, stdin=io.StringIO("y\n"))
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(d, "manifest.json")))


class Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs, self.comments = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs += attrs

    def handle_comment(self, data):
        self.comments.append(data)


class TestHtmlAndCsv(Base):
    def test_html_is_self_contained(self):
        d = self.export("html")
        text = read_all(d)["export.html"]
        p = Links()
        p.feed(text)
        self.assertNotIn("script", p.tags)
        self.assertNotIn("iframe", p.tags)
        self.assertEqual(p.comments, [])
        self.assertFalse([a for a in p.attrs if a[0] in ("src", "href", "action") or (a[1] or "").startswith("http")])
        self.assertNotRegex(text, r"https?://")
        self.assertIn("Content-Security-Policy", text)
        self.assertEqual(sorted(os.listdir(d)), ["export.html", "manifest.json"])
        meta = [a for a in p.attrs if a[0] == "content"]
        for _, v in meta:
            self.assertNotIn("canary", (v or "").lower())

    def test_html_escapes_every_value(self):
        tables = {n: [] for n in ex.TABLES}
        tables["assignments"] = [{"x": "<script>alert(1)</script>", "y": '"><img src=x onerror=1>', "z": {"a": "</td><b>"}}]
        body = ex._fmt_html("x-1", "redacted", tables, {"assignments": 1}, 1.0).decode()
        self.assertNotIn("<script>", body)
        self.assertNotIn("<img", body)
        self.assertNotIn("<b>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_csv_round_trips_and_neutralises_formulas(self):
        d = self.export("csv")
        with open(os.path.join(d, "assignments.csv"), newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(int(r["tokens"]) for r in rows if r["parent"] == "" or r["parent"]), 140 + 80)
        self.assertTrue(ex._cell("=1+1").startswith("'"))
        self.assertEqual(ex._cell(None), "")


class TestRaw(Base):
    def test_raw_needs_typed_confirmation_and_keeps_real_ids(self):
        d = os.path.join(self.tmp, "raw")
        code, _, err = st.cli(self.home, "export", "--raw", "--yes", "--out", d, now=NOW)
        self.assertEqual(code, 2)
        self.assertIn("confirm raw", err)
        self.assertFalse(os.path.exists(d))
        code, _, err = st.cli(self.home, "export", "--raw", "--confirm", "raw", "--out", d, now=NOW)
        self.assertEqual(code, 0, err)
        m = json.load(open(os.path.join(d, "manifest.json")))
        self.assertEqual(sch.validate(m, MANIFEST), [])
        self.assertEqual(m["mode"], "raw")
        a = [json.loads(l) for l in open(os.path.join(d, "assignments.jsonl"))]
        self.assertIn(st.ROOT, {r["assignment"] for r in a})
        self.assertIn("CANARY-TASK-67", {r["task"] for r in a})
        self.assertEqual(json.loads(self.conn.execute("SELECT data FROM event WHERE action='audit_export'").fetchone()[0])["mode"], "raw")

    def test_raw_and_redact_together_refused(self):
        code, _, _ = st.cli(self.home, "export", "--raw", "--redact", "--confirm", "raw", "--out", os.path.join(self.tmp, "x"), now=NOW)
        self.assertEqual(code, 2)

    def test_default_is_redacted(self):
        d = os.path.join(self.tmp, "dflt")
        st.cli(self.home, "export", "--yes", "--out", d, now=NOW)
        self.assertEqual(json.load(open(os.path.join(d, "manifest.json")))["mode"], "redacted")


class TestPolicyCoversTheSchema(unittest.TestCase):
    """Every data field of every event action is either dropped, pseudonymized or a closed enum/number: a new string field cannot slip into a redacted export unnoticed."""

    def test_every_string_field_is_classified(self):
        e = sch.load("event-v1.json")
        d = e["$defs"]
        unclassified = []
        for rule in e["allOf"]:
            if "action" not in rule["if"]["properties"]:
                continue
            act = rule["if"]["properties"]["action"]["const"]
            props = rule["then"].get("properties", {}).get("data", {}).get("properties", {})
            for k, s in props.items():
                s = d[s["$ref"].rsplit("/", 1)[1]] if "$ref" in s else s
                alts = s.get("anyOf") or [s]
                strs = [a for a in alts if (d[a["$ref"].rsplit("/", 1)[1]] if "$ref" in a else a).get("type") == "string" or "enum" in (d[a["$ref"].rsplit("/", 1)[1]] if "$ref" in a else a)]
                if not strs or k in ex.DROP or k in ex.PSEUDO or k == "criteria" or k in ex.NAME_KEYS:
                    continue
                if k in ex.KEEP_STR:
                    ok = all("enum" in (d[a["$ref"].rsplit("/", 1)[1]] if "$ref" in a else a) or "const" in a for a in strs)
                    if ok:
                        continue
                unclassified.append((act, k))
        self.assertEqual(unclassified, [], "classify these fields in export.py (PSEUDO, KEEP_STR or DROP)")


if __name__ == "__main__":
    unittest.main()
