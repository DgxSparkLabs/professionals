"""Audit step G (T090): the release acceptance fixture of the assignment, verbatim, through the real `pros` process.

Ask `system-designer` for an architecture review with one branch delegated to `reliability-reviewer`. The caller's short wait returns
pending, the root eventually replies after the task deadline, and an operator records a partial verdict with one failed criterion. A
resumed transcript repeats an earlier exchange. The CLI and dashboard show the same delegation, a pending wait and a late reply, and the
partial evaluation. On a synthetic usage fixture, the root owns 140 tokens and the child owns 80, so the attributable trace total is 220.
A further 30 tokens remain assigned only to the root session. Reimport after restart preserves those totals. A redacted export
preserves joins and reveals none of the fixture's planted sensitive strings.

Here the operator verdict is entered on the dashboard (`pros audit serve --manage`, owner token), not taken from the fixture, so the
whole path is exercised: spool + transcripts -> import -> CLI and dashboard JSON -> verdict -> restart + reimport -> redacted export.
Stdlib only."""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import helpers

sys.path[:0] = [helpers.HERE]
from test_audit_e2e import table_hash  # noqa: E402
from test_audit_http import req  # noqa: E402

CF = os.path.join(helpers.HERE, "fixtures", "audit", "contracts")
USAGE = os.path.join(helpers.HERE, "fixtures", "audit", "usage")
ROOT, CHILD = "m-0000000001", "m-0000000005"
NOW = "1800000200"
WIN = ["--since", "1800000000", "--until", NOW]


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def scenario_without_operator_verdict():
    """The release scenario's events minus the fixture's own operator verdict (the operator enters it in this test)."""
    out = []
    with open(os.path.join(CF, "events", "release_scenario.jsonl"), encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            if e["action"] == "audit_evaluation" and e.get("actor") == "operator":
                continue
            out.append(line if line.endswith("\n") else line + "\n")
    return "".join(out)


class TestReleaseScenario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pros-audit-release-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "audit", "spool"), mode=0o700)
        os.chmod(os.path.join(self.home, "audit"), 0o700)
        proj = os.path.join(self.tmp, "claude", "projects", "p")
        os.makedirs(proj)
        for p in glob.glob(os.path.join(USAGE, "transcripts", "*.jsonl")):
            shutil.copy(p, proj)
        self.events = scenario_without_operator_verdict()
        self.spool(self.events)
        self.server = None

    def spool(self, text, name="fixture-1.jsonl"):
        p = os.path.join(self.home, "audit", "spool", name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(p, 0o600)
        os.utime(p, (1, 1))

    def env(self):
        e = {k: v for k, v in os.environ.items() if k not in ("PRO_SESSION", "TMUX")}
        e.update(PRO_HOME=self.home, PRO_AUDIT_NOW=NOW, PRO_TMUX_SOCKET="audit-release-none", CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "claude"))
        return e

    def pros(self, *args):
        r = subprocess.run([sys.executable, helpers.PRO, "audit"] + list(args), capture_output=True, text=True, env=self.env(), timeout=60)
        self.assertEqual(r.returncode, 0, (args, r.stdout, r.stderr))
        return r.stdout

    def start(self, manage=False):
        """`pros audit serve [--manage]` as its own process (the dashboard's server)."""
        p = subprocess.Popen([sys.executable, helpers.PRO, "audit", "serve"] + (["--manage"] if manage else []), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=self.env())
        line = p.stdout.readline()
        port = int(line.split("http://127.0.0.1:")[1].split("/")[0])
        with open(os.path.join(self.home, "audit", "http.token"), encoding="utf-8") as f:
            token = f.read().strip()
        self.server = (p, port, token)
        self.addCleanup(self.stop, p)
        return port, token

    def stop(self, p):
        if p.poll() is None:
            p.terminate()
            p.wait(10)
        for s in (p.stdout, p.stderr):
            if not s.closed:
                s.close()

    def api(self, path):
        _, port, token = self.server
        st, _, body = req(port, path, token)
        self.assertEqual(st, 200, (path, body[:300]))
        return body

    def same_on_both(self):
        """CLI --json bytes == dashboard API bytes for the views the release names; returns the trace result."""
        pairs = [("/api/v1/task_trace?id=" + ROOT, ["trace", ROOT, "--json"]), ("/api/v1/task_trace?id=fx-root", ["trace", "fx-root", "--json"]),
                 ("/api/v1/query_metrics?since=1800000000&until=" + NOW, ["summary", "--json"] + WIN),
                 ("/api/v1/query_metrics?since=1800000000&until=%s&group_by=professional" % NOW, ["summary", "--json", "--by", "professional"] + WIN)]
        for m in ("pass_rate", "deadline_miss_rate", "accounted_tokens", "evaluation_coverage"):
            pairs.append(("/api/v1/explain_metric?metric=%s&since=1800000000&until=%s" % (m, NOW), ["explain", m, "--json"] + WIN))
        for path, cli in pairs:
            self.assertEqual(self.api(path), self.pros(*cli).encode("utf-8"), path)
        return json.loads(self.api("/api/v1/task_trace?id=" + ROOT))["result"]

    def check_totals(self, tr):
        own = {a["msg"]: a["own_tokens"]["total"] for a in tr["assignments"]}
        self.assertEqual((own[ROOT], own[CHILD]), (140, 80), "root owns 140, child owns 80")
        self.assertEqual(tr["usage"]["inclusive"]["total"], 220, "attributable trace total")
        self.assertEqual([(s["session"], s["tokens"]["total"]) for s in tr["usage"]["session_only"]], [(tr["assignments"][0]["session"], 30)],
                         "30 tokens stay on the root session only")

    def test_release_acceptance_scenario(self):
        imp = json.loads(self.pros("import", "--json"))
        self.assertEqual(imp["usage"]["new"], 4, "7 assistant rows across the transcripts, the resumed/forked copy repeats exchanges: 4 unique")
        port, token = self.start(manage=True)

        # delegation, a pending wait and a late reply: the same on the CLI and the dashboard
        tr = self.same_on_both()
        by = {a["msg"]: a for a in tr["assignments"]}
        self.assertEqual((by[ROOT]["to"], by[ROOT]["parent"], by[ROOT]["depth"]), ("system-designer", None, 0))
        self.assertEqual((by[CHILD]["to"], by[CHILD]["parent"], by[CHILD]["depth"]), ("reliability-reviewer", ROOT, 1), "one branch delegated")
        self.assertEqual(by[ROOT]["delivery"], "answered")
        self.assertEqual(by[ROOT]["deadline_status"], "late", "the root replies after its deadline")
        self.assertGreater(by[ROOT]["answered_at"], by[ROOT]["deadline"])
        self.assertEqual(by[CHILD]["deadline_status"], "met")
        first = sorted(tr["waits"], key=lambda w: w["start"])[0]
        self.assertEqual((first["outcome"], first["exit"], first["task"]), ("pending", 3, "fx-root"), "the caller's short wait returns pending")
        self.check_totals(tr)
        self.assertEqual(by[ROOT]["evaluation"]["evaluator_kind"], "automated", "before the operator: only the automated check")

        # the operator records a partial verdict with one failed criterion, on the dashboard (rubric added once on the CLI)
        self.pros("rubric", "add", "architecture-review", "r1", "--criterion", "C1", "--criterion", "C2", "--criterion", "C3")
        st, _, body = req(port, "/manage/v1/verdict", token, "POST",
                          {"target": ROOT, "status": "partial", "criteria": {"C1": "failed", "C2": "passed", "C3": "passed"}, "rubric": "architecture-review",
                           "evidence": ["note:release-1"]})
        self.assertEqual(st, 200, body)
        v = json.loads(body)["result"]
        self.assertEqual(v["effective"], v["eval"], "the operator verdict is the effective one")
        tr = self.same_on_both()
        e = tr["evaluation"]
        self.assertEqual((e["status"], e["evaluator_kind"], e["rubric"]), ("partial", "operator", "architecture-review"))
        self.assertEqual([c["id"] for c in e["criteria"] if c["outcome"] == "failed"], ["C1"], "exactly one failed criterion")
        self.assertIsNotNone(e["supersedes"], "it supersedes the automated check, which is kept")
        self.assertEqual({a["msg"]: a["evaluation"]["status"] for a in tr["assignments"]}, {ROOT: "partial", CHILD: "passed"})
        cards = json.loads(self.pros("summary", "--json", *WIN))["result"]["cards"]
        self.assertEqual((cards["assigned_work"]["assignments"], cards["assigned_work"]["root_tasks"]), (2, 1))
        before = json.dumps(tr, sort_keys=True)

        # restart: server and importer stop; the same spool file comes back and the transcripts are read again
        self.stop(self.server[0])
        db = os.path.join(self.home, "audit", "audit.db")
        h1 = table_hash(db)
        self.spool(self.events)
        imp2 = json.loads(self.pros("import", "--json"))
        self.assertEqual((imp2["spool"]["events"], imp2["usage"]["new"]), (0, 0), "nothing new on reimport")
        self.assertGreater(imp2["spool"]["duplicates"], 0)
        self.assertEqual(table_hash(db), h1, "the facts did not change")
        self.start()
        tr = self.same_on_both()
        self.check_totals(tr)
        self.assertEqual(json.dumps(tr, sort_keys=True), before, "the trace after the restart equals the one before")

        # redacted export: joins kept under pseudonyms, none of the planted strings
        out = os.path.join(self.tmp, "export")
        self.pros("export", "--yes", "--out", out)
        files = glob.glob(os.path.join(out, "*"))
        blob = "".join(read(p) for p in files)
        with open(os.path.join(USAGE, "expected.json"), encoding="utf-8") as f:
            canaries = json.load(f)["canaries"]
        self.assertTrue(canaries)
        for x in canaries + [ROOT, CHILD, "fx-root", "t-00000001", "release-1", "00000000-0000-4000-8000-000000000002"]:
            self.assertNotIn(x, blob, x)
        rows = [json.loads(x) for x in read(os.path.join(out, "assignments.jsonl")).splitlines()]
        self.assertEqual(sorted(r["tokens"] for r in rows), [80, 140])
        self.assertEqual(len({r["root"] for r in rows}), 1, "both assignments join to one pseudonymous root")
        m = json.loads(read(os.path.join(out, "manifest.json")))
        self.assertEqual(m["mode"], "redacted")


if __name__ == "__main__":
    unittest.main()
