"""0.9.4 (card 1594): an audit trail that drives self-improvement.  564 finished tasks had no verdict, and the verdicts that
existed were never turned into better professionals.  `pros audit-trail NAME` lists every task with its request, reply,
verdicts, time taken and refused tools; `pros lessons NAME` turns the bad and neutral reasons and failed evaluations into a
PROPOSED LESSONS.proposed.md in the professional's folder (never its prompt); `pros inbox` prints one line of unrated
replies instead of one rate line per task.  The tests use a fixture store: messages and history rows written directly."""
import hashlib
import json
import os
import time
import unittest

import test_v082
from helpers import ProTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAY = 86400.0


class Fixture(ProTestCase):
    def setUp(self):
        super().setUp()
        self.t0 = time.time() - 3600
        self.n = 0

    def msg(self, frm, to, task, kind, body, created, **kw):
        self.n += 1
        m = {"id": "m-%010x" % self.n, "from": frm, "to": to, "task": task, "kind": kind, "reply_to": frm, "chain": [frm],
             "body": body, "created": created, "deadline": created + 600, "state": kw.pop("state", "answered"), "attempts": 1,
             "parent": None, "session": None}
        m.update(kw)
        d = os.path.join(self.home, "run", "msgs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, m["id"] + ".json"), "w") as f:
            json.dump(m, f)
        return m

    def hist(self, name, *rows):
        d = os.path.join(self.home, "history")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name + ".jsonl"), "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def task(self, name, task, ask, reply, took, at=None, frm="user"):
        at = self.t0 if at is None else at
        q = self.msg(frm, name, task, "task", ask, at, answered_at=at + took if reply else None, state="answered" if reply else "failed")
        if reply:
            self.msg(name, frm, task, "reply", reply, at + took, parent=q["id"], state="queued")
        return q

    def rate(self, name, task, rating, note, source=None):
        r = {"ev": "rate", "id": "s1", "rating": rating, "note": note, "task": task, "t": self.t0 + 900}
        if source:
            r["source"] = source
        self.hist(name, r)

    def store(self, name="alpha"):
        self.hist(name, {"ev": "start", "id": "s1", "t": self.t0 - 60})
        self.task(name, "t-good", "List the open cards\nwith owners", "Found 3 open cards\n- 1589", 125)
        self.rate(name, "t-good", "good", "exact list")
        self.task(name, "t-suite1", "Fix the parser bug", "Fixed it, see diff", 300)
        self.rate(name, "t-suite1", "bad", "did not run the suite before replying")
        self.task(name, "t-suite2", "Add a flag", "Added --x", 200)
        self.rate(name, "t-suite2", "bad", "broke two tests, never ran the suite")
        self.task(name, "t-wordy", "Summarise the log", "Here is a very long summary", 60)
        self.rate(name, "t-wordy", "neutral", "right but far too wordy")
        self.task(name, "t-evalfail", "Port the check to bash", "Done", 45)
        self.rate(name, "t-evalfail", "bad", "evaluate failed by user", source="evaluate")
        self.task(name, "t-open1", "Check the disk", "Disk is 40% full", 30)
        self.task(name, "t-open2", "Check the load", "Load 0.3", 20)
        self.task(name, "t-old", "Old job", "Old answer", 10, at=time.time() - 10 * DAY)
        self.hist(name, {"ev": "denied", "task": "t-suite1", "tool": "Bash", "target": "rm -rf build", "t": self.t0 + 100},
                  {"ev": "denied", "task": "t-suite1", "tool": "Bash", "target": "rm -rf build", "t": self.t0 + 110},
                  {"ev": "denied", "task": "t-evalfail", "tool": "Write", "target": "/etc/hosts", "t": self.t0 + 120})


class AuditTrail(Fixture):
    def test_json_lists_every_task_with_request_reply_verdicts_time_and_refusals(self):
        self.store()
        d = json.loads(self.ok("audit-trail", "alpha", "--json").stdout)
        rows = {r["task"]: r for r in d["tasks"]}
        self.assertEqual(set(rows), {"t-good", "t-suite1", "t-suite2", "t-wordy", "t-evalfail", "t-open1", "t-open2", "t-old"})
        g = rows["t-good"]
        self.assertEqual((g["request"], g["reply"], g["time_s"], g["from"]), ("List the open cards", "Found 3 open cards", 125, "user"))
        self.assertEqual([(v["rating"], v["note"]) for v in g["verdicts"]], [("good", "exact list")])
        self.assertEqual(rows["t-evalfail"]["verdicts"][0]["evaluate"], "failed")
        self.assertEqual(rows["t-suite1"]["refused"], [{"tool": "Bash", "target": "rm -rf build", "n": 2}])
        self.assertEqual(rows["t-evalfail"]["refused"], [{"tool": "Write", "target": "/etc/hosts", "n": 1}])
        self.assertEqual(rows["t-open1"]["verdicts"], [])
        self.assertEqual(d["unrated"], 3)  # t-open1, t-open2, t-old

    def test_since_limits_the_window(self):
        self.store()
        d = json.loads(self.ok("audit-trail", "alpha", "--since", "7d", "--json").stdout)
        self.assertNotIn("t-old", [r["task"] for r in d["tasks"]])
        self.assertIn("t-good", [r["task"] for r in d["tasks"]])

    def test_unrated_lists_only_tasks_without_a_verdict(self):
        self.store()
        d = json.loads(self.ok("audit-trail", "alpha", "--unrated", "--json").stdout)
        self.assertEqual(sorted(r["task"] for r in d["tasks"]), ["t-old", "t-open1", "t-open2"])

    def test_plain_text(self):
        self.store()
        out = self.ok("audit-trail", "alpha").stdout
        for s in ("t-suite1", "Fix the parser bug", "Fixed it, see diff", "bad: did not run the suite before replying", "5m00s",
                  "refused: Bash rm -rf build x2", "evaluate failed", "no verdict"):
            self.assertIn(s, out)

    def test_refused_inside_a_professional_session(self):
        self.store()
        r = self.pro("audit-trail", "alpha", as_pro="beta")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)


class DenialsCarryTheTask(test_v082.Base):
    def test_a_denied_call_is_kept_in_history_with_its_task(self):
        self.config({"default_mode": "dontAsk"})
        self.summon("alpha")
        s = self.session("alpha")
        st = os.path.join(self.home, "run", "state", s + ".json")
        with open(st) as f:
            d = json.load(f)
        d["task"] = "t-den"
        with open(st, "w") as f:
            json.dump(d, f)
        self.deny_call("alpha", "Bash", {"command": "rm -rf build"}, "toolu_d1")
        self.ok("pump")
        rows = [json.loads(l) for l in open(os.path.join(self.home, "history", "alpha.jsonl"))]
        den = [r for r in rows if r.get("ev") == "denied"]
        self.assertEqual([(r["task"], r["tool"], r["target"]) for r in den], [("t-den", "Bash", "rm -rf build")])


class Lessons(Fixture):
    def folder_entry(self):
        d = self.make_folder("fx", agents="# fx\nAnswer briefly.\n")
        self.ok("add", d)
        return d

    def digest(self, d):
        return {f: hashlib.sha256(open(os.path.join(d, f), "rb").read()).hexdigest() for f in ("CLAUDE.md", "AGENTS.md")}

    def test_writes_a_proposal_into_the_folder_and_never_edits_its_prompt(self):
        d = self.folder_entry()
        before = self.digest(d)
        self.store("fx")
        r = self.ok("lessons", "fx")
        p = os.path.join(d, "LESSONS.proposed.md")
        self.assertIn(p, r.stdout)
        text = open(p).read()
        self.assertIn("PROPOSED", text)
        self.assertIn("pros add --refresh", text)
        self.assertIn("suite", text)  # the two bads about the suite are one lesson
        self.assertIn("t-suite1", text)
        self.assertIn("t-suite2", text)
        self.assertIn("did not run the suite before replying", text)
        self.assertIn("wordy", text)  # the neutral one
        self.assertIn("Port the check to bash", text)  # a failed evaluation without a reason names its request
        self.assertNotIn("exact list", text)  # good verdicts are not lessons
        self.assertEqual(self.digest(d), before)
        row = [x for x in json.loads(self.ok("list", "--json").stdout) if x["name"] == "fx"][0]
        self.assertNotEqual(row.get("state"), "changed")

    def test_groups_by_shared_reason(self):
        self.folder_entry()
        self.store("fx")
        d = json.loads(self.ok("lessons", "fx", "--json").stdout)
        suite = [x for x in d["lessons"] if x["key"] == "suite"]
        self.assertEqual(len(suite), 1)
        self.assertEqual(sorted(suite[0]["tasks"]), ["t-suite1", "t-suite2"])
        self.assertEqual(suite[0]["n"], 2)

    def test_never_overwrites_a_file_it_did_not_write(self):
        d = self.folder_entry()
        self.store("fx")
        p = os.path.join(d, "LESSONS.proposed.md")
        with open(p, "w") as f:
            f.write("my own notes\n")
        r = self.pro("lessons", "fx")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(open(p).read(), "my own notes\n")

    def test_nothing_to_learn_writes_nothing(self):
        d = self.folder_entry()
        self.hist("fx", {"ev": "start", "id": "s1", "t": self.t0})
        self.task("fx", "t-g", "Do it", "Done", 5)
        self.rate("fx", "t-g", "good", "fine")
        r = self.ok("lessons", "fx")
        self.assertIn("nothing to propose", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(d, "LESSONS.proposed.md")))

    def test_a_marketplace_professional_gets_its_proposal_in_the_pros_home(self):
        self.store("alpha")
        r = self.ok("lessons", "alpha")
        p = os.path.join(self.home, "lessons", "alpha.proposed.md")
        self.assertIn(p, r.stdout)
        self.assertTrue(os.path.exists(p))

    def test_refused_inside_a_professional_session(self):
        self.folder_entry()
        self.store("fx")
        self.assertEqual(self.pro("lessons", "fx", as_pro="beta").returncode, 4)


class InboxOneLine(Fixture):
    def test_one_line_for_all_unrated_replies(self):
        self.store("alpha")
        self.hist("beta", {"ev": "start", "id": "s2", "t": self.t0})
        self.task("beta", "t-b1", "Beta job", "Beta answer", 12)
        out = self.ok("inbox").stdout
        lines = [l for l in out.splitlines() if "without a verdict" in l]
        self.assertEqual(len(lines), 1, out)
        self.assertIn("4 replies from alpha (3), beta (1) without a verdict", lines[0])
        self.assertNotIn("rate: pros rate", out)

    def test_single_professional(self):
        self.store("alpha")
        out = self.ok("inbox").stdout
        self.assertIn("3 replies from alpha without a verdict", out)
        self.assertIn("pros audit-trail alpha --unrated", out)

    def test_nothing_unrated_no_line(self):
        self.hist("alpha", {"ev": "start", "id": "s1", "t": self.t0})
        self.task("alpha", "t-g", "Do it", "Done", 5)
        self.rate("alpha", "t-g", "good", "fine")
        self.assertNotIn("without a verdict", self.ok("inbox").stdout)


class Documented(unittest.TestCase):
    def test_docs(self):
        with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
            self.assertIn("## 0.9.4", f.read())
        with open(os.path.join(ROOT, "docs", "spec", "spec.md")) as f:
            t = f.read()
        self.assertIn("pros audit-trail", t)
        self.assertIn("LESSONS.proposed.md", t)
        with open(os.path.join(ROOT, "plugins", "professionals", "skills", "professionals", "SKILL.md")) as f:
            self.assertIn("audit-trail", f.read())


if __name__ == "__main__":
    unittest.main()
