"""Audit step E: the first local trace end to end through the real `pros` process and the real store (no hand-built tables).

Spool (release scenario) + transcripts -> `pros audit import` -> trace/summary -> verdict -> reimport after a restart -> redacted export.
The live-capture half (stub claude writing the spool) is covered by test_audit_capture.py; this file joins the D1-D4 parts. Stdlib only."""
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import helpers

CF = os.path.join(helpers.HERE, "fixtures", "audit", "contracts")
USAGE = os.path.join(helpers.HERE, "fixtures", "audit", "usage")
ROOT, CHILD = "m-0000000001", "m-0000000005"
NOW = "1800000200"
TABLES = ("event", "evaluation", "assignment", "attempt", "wait", "run", "exchange", "header_seen")


def table_hash(db):
    c = sqlite3.connect(db)
    try:
        h = hashlib.sha256()
        for t in TABLES:
            cols = [r[1] for r in c.execute("PRAGMA table_info(%s)" % t) if r[1] not in ("seq", "batch")]
            where = " WHERE action != 'audit_import'" if t == "event" else ""  # each import run records itself; the facts must not change
            for row in c.execute("SELECT %s FROM %s%s ORDER BY 1, 2" % (", ".join(cols), t, where)):
                h.update(repr(row).encode("utf-8"))
        return h.hexdigest()
    finally:
        c.close()


class TestFirstLocalTrace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pros-audit-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        for d in ("spool", "pricing"):
            os.makedirs(os.path.join(self.home, "audit", d), mode=0o700)
        os.chmod(os.path.join(self.home, "audit"), 0o700)
        proj = os.path.join(self.tmp, "claude", "projects", "p")
        os.makedirs(proj)
        for p in glob.glob(os.path.join(USAGE, "transcripts", "*.jsonl")):
            shutil.copy(p, proj)
        self.spool(open(os.path.join(CF, "events", "release_scenario.jsonl"), encoding="utf-8").read())

    def spool(self, text, name="fixture-1.jsonl"):
        p = os.path.join(self.home, "audit", "spool", name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(p, 0o600)
        os.utime(p, (1, 1))  # settled, so the importer removes it after the import

    def pros(self, *args, env=None):
        e = {k: v for k, v in os.environ.items() if k not in ("PRO_SESSION", "TMUX")}
        e.update(PRO_HOME=self.home, PRO_AUDIT_NOW=NOW, PRO_TMUX_SOCKET="audit-e2e-none", CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "claude"))
        e.update(env or {})
        return subprocess.run([sys.executable, helpers.PRO, "audit"] + list(args), capture_output=True, text=True, env=e, timeout=60)

    def ok(self, *args, **kw):
        r = self.pros(*args, **kw)
        self.assertEqual(r.returncode, 0, (args, r.stdout, r.stderr))
        return r

    def test_capture_import_trace_verdict_reimport_export(self):
        imp = json.loads(self.ok("import", "--json").stdout)
        self.assertEqual((imp["spool"]["events"], imp["spool"]["evaluations_projected"], imp["usage"]["new"]), (27, 3, 4))
        self.assertEqual(os.listdir(os.path.join(self.home, "audit", "spool")), [])
        tr = json.loads(self.ok("trace", ROOT, "--json").stdout)["result"]
        own = {a["msg"]: a.get("own_tokens", {}).get("total") for a in tr["assignments"]}
        self.assertEqual((own[ROOT], own[CHILD]), (140, 80))
        self.assertEqual(tr["usage"]["inclusive"]["total"], 220)
        self.assertEqual(sum(s["tokens"]["total"] for s in tr["usage"]["session_only"]), 30)
        self.assertEqual({a["msg"]: a["evaluation"]["status"] for a in tr["assignments"]}, {ROOT: "partial", CHILD: "passed"})
        self.assertEqual({a["msg"]: a["confidence"] for a in tr["assignments"]}, {ROOT: "verified", CHILD: "verified"})
        self.assertEqual(sorted(w["exit"] for w in tr["waits"]), [3, 5, 5])
        # an owner verdict through the CLI supersedes the fixture's operator verdict and shows in the trace
        v = json.loads(self.ok("verdict", ROOT, "--status", "passed", "--criterion", "C1=passed", "--json").stdout)
        self.assertEqual(v["effective"], v["eval"])
        tr = json.loads(self.ok("trace", ROOT, "--json").stdout)["result"]
        self.assertEqual(tr["assignments"][0]["evaluation"]["status"], "passed")
        db = os.path.join(self.home, "audit", "audit.db")
        h1 = table_hash(db)
        # restart: the same spool file comes back (crash before delete) and the transcripts are read again: nothing changes
        self.spool(open(os.path.join(CF, "events", "release_scenario.jsonl"), encoding="utf-8").read(), "fixture-1.jsonl")
        imp2 = json.loads(self.ok("import", "--json").stdout)
        self.assertEqual((imp2["spool"]["events"], imp2["spool"]["duplicates"], imp2["spool"]["evaluations_projected"], imp2["usage"]["new"]), (0, 27, 0, 0))
        self.assertEqual(table_hash(db), h1)
        s = json.loads(self.ok("summary", "--since", "1800000000", "--until", NOW, "--json").stdout)["result"]["cards"]
        self.assertEqual((s["assigned_work"]["assignments"], s["assigned_work"]["root_tasks"]), (2, 1))
        # redacted export: no transcript canary, no real message id, joins kept
        out = os.path.join(self.tmp, "export")
        self.ok("export", "--yes", "--out", out)
        blob = "".join(open(p, encoding="utf-8").read() for p in glob.glob(os.path.join(out, "*")))
        canaries = json.load(open(os.path.join(USAGE, "expected.json"), encoding="utf-8"))["canaries"]
        for x in canaries + [ROOT, CHILD, "fx-root"]:  # marketplace names stay (public); ids and task ids are pseudonyms
            self.assertNotIn(x, blob)
        m = json.load(open(os.path.join(out, "manifest.json"), encoding="utf-8"))
        self.assertEqual({s["source"]: s["quality"] for s in m["coverage"]["sources"]}["transcript"], "observed")
        rows = [json.loads(x) for x in open(os.path.join(out, "assignments.jsonl"), encoding="utf-8")]
        self.assertEqual(sorted(r["tokens"] for r in rows), [80, 140], "export token sums agree with the trace (unsplit is a marker, not extra tokens)")
        self.assertEqual(len({r["root"] for r in rows}), 1, "joins kept under pseudonyms")

    def test_a_professional_session_is_denied_import_and_writes(self):
        for args in (["import"], ["verdict", ROOT, "--status", "passed", "--criterion", "C1=passed"], ["export", "--yes", "--out", os.path.join(self.tmp, "x")]):
            r = self.pros(*args, env={"PRO_SESSION": "x"})
            self.assertEqual(r.returncode, 4, (args, r.stderr))
        self.assertEqual(len(os.listdir(os.path.join(self.home, "audit", "spool"))), 1, "nothing imported")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x")))


if __name__ == "__main__":
    unittest.main()
