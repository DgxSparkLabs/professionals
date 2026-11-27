"""0.8.3: worker verdicts are hard to forget.  A DONE reply to the user makes `pros inbox` and `pros read` print a one-line nudge
until a rating for that task exists; `pros team` shows each professional's last verdict."""
import json
import os
import unittest

from helpers import ProTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Verdicts(ProTestCase):
    def done(self, task, body="ECHO done"):
        self.ok("send", "alpha", body, "--task", task)
        self.wait_until(lambda: [m for m in self.msgs() if m["task"] == task and m["kind"] == "reply"], 20, "reply")
        return [m for m in self.msgs() if m["task"] == task and m["kind"] == "reply"][0]

    def test_nudge_until_rated(self):
        self.summon("alpha")
        rep = self.done("t-v1")
        nudge = "rate: pros rate alpha good|neutral|bad \"why\" --task t-v1"
        line = "1 reply from alpha without a verdict"  # 0.9.4: inbox has one line for all unrated replies (was the rate line per task)
        self.assertIn(line, self.ok("inbox").stdout)
        self.assertIn(nudge, self.ok("read", rep["id"]).stdout)
        self.assertIn(line, self.ok("inbox").stdout)  # read is not rated: still nudged
        self.ok("rate", "alpha", "neutral", "found it, missed the edge case", "--task", "t-v1")
        self.assertNotIn("without a verdict", self.ok("inbox").stdout)
        self.assertNotIn("rate:", self.ok("read", rep["id"]).stdout)

    def test_rate_defaults_to_the_last_done_task_and_team_shows_it(self):
        self.summon("alpha")
        self.done("t-v2")
        self.done("t-v3")
        r = self.ok("rate", "alpha", "good", "exact list")
        self.assertIn("t-v3", r.stdout)
        self.assertIn("1 reply from alpha without a verdict", self.ok("inbox").stdout)  # 0.9.4: the one line; the trail names the task
        left = [r["task"] for r in json.loads(self.ok("audit-trail", "alpha", "--unrated", "--json").stdout)["tasks"]]
        self.assertEqual(left, ["t-v2"])
        row = [x for x in json.loads(self.ok("team", "--json").stdout) if x["name"] == "alpha"][0]
        self.assertEqual((row["verdict"]["rating"], row["verdict"]["note"], row["verdict"]["task"]), ("good", "exact list", "t-v3"))
        self.assertIn("good: exact list", self.ok("team").stdout)

    def test_evaluate_counts_as_a_rating(self):
        self.summon("alpha")
        self.done("t-v4")
        self.ok("evaluate", "t-v4", "failed")
        self.assertNotIn("--task t-v4", self.ok("inbox").stdout)
        row = [x for x in json.loads(self.ok("team", "--json").stdout) if x["name"] == "alpha"][0]
        self.assertEqual(row["verdict"]["rating"], "bad")

    def test_a_professional_is_not_nudged_to_rate(self):
        self.summon("alpha")
        self.ok("send", "beta", "ECHO hi", "--task", "t-v5", as_pro="alpha")
        self.wait_until(lambda: [m for m in self.msgs() if m["task"] == "t-v5" and m["kind"] == "reply"], 20, "reply")
        self.assertNotIn("rate:", self.ok("inbox", as_pro="alpha").stdout)

    def test_old_rate_still_works(self):
        self.summon("alpha")
        self.ok("rate", "alpha", "bad", "--note", "wrong answers")
        row = [x for x in json.loads(self.ok("team", "--json").stdout) if x["name"] == "alpha"][0]
        self.assertEqual((row["verdict"]["rating"], row["verdict"]["note"]), ("bad", "wrong answers"))


class Documented(unittest.TestCase):
    def test_documented(self):
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            self.assertIn("## 0.8.3", f.read())
        for p, w in ((("docs", "spec", "spec.md"), "good|neutral|bad"), (("plugins", "professionals", "skills", "professionals", "SKILL.md"), "good\\|neutral\\|bad")):
            with open(os.path.join(ROOT, *p), encoding="utf-8") as f:
                self.assertIn(w, f.read(), p)  # SKILL.md is a table: its pipes are escaped


if __name__ == "__main__":
    unittest.main()
