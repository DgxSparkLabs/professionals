"""0.8.6 (card 1563, 'learn and grow'): verdicts are used. pros pick --for KIND returns the best free professional for that
kind of job from its verdicts; pros team says what each one is good at and weak at; two bads in a row on a kind keep a
professional from being picked for it until a good one."""
import json
import os
import time
import unittest

from helpers import ProTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Pick(ProTestCase):
    def setUp(self):
        super().setUp()
        self.config({"default_mode": "dontAsk"})
        for n in ("rev-a", "rev-b", "rev-c"):
            self.ok("add", self.make_folder(n), "--name", n)
            self.summon(n)
            self.ok("stop", n)  # a session history, then free (not running)

    def rate(self, name, rating, why, *extra):
        self.ok("rate", name, rating, why, *extra)
        time.sleep(0.02)

    def hist(self, name, row):
        with open(os.path.join(self.home, "history", name + ".jsonl"), "a") as f:
            f.write(json.dumps(dict(row, ev="rate", id="x")) + "\n")

    def pick(self, job):
        r = self.pro("pick", "--for", job, "--json")
        return r.returncode, json.loads(r.stdout)

    def test_recent_good_beats_neutral_beats_bad(self):
        self.rate("rev-a", "bad", "missed the race in the review", "--kind", "review")
        self.rate("rev-b", "good", "found the dead hang in the review", "--kind", "review")
        self.rate("rev-c", "neutral", "review was fine, nothing new", "--kind", "review")
        code, out = self.pick("reliability review")
        self.assertEqual((code, out["pick"], out["by"]), (0, "rev-b", "kind"))
        self.assertEqual([p["name"] for p in out["ranked"] if p["name"].startswith("rev-")], ["rev-b", "rev-c", "rev-a"])
        self.assertEqual(out["ranked"][-1]["name"], "rev-a", "a bad verdict ranks below those with none")
        txt = self.ok("pick", "--for", "review").stdout
        self.assertIn("pick: rev-b", txt)
        self.assertIn("1 good", txt)

    def test_old_good_counts_less_than_a_recent_good(self):
        old = time.time() - 30 * 86400
        self.hist("rev-a", {"rating": "good", "note": "docs ok", "kind": "docs", "t": old})
        self.hist("rev-a", {"rating": "good", "note": "docs ok", "kind": "docs", "t": old})
        self.rate("rev-b", "good", "clear docs", "--kind", "docs")
        self.assertEqual(self.pick("docs")[1]["pick"], "rev-b")

    def test_ties_go_to_the_least_busy(self):
        self.rate("rev-a", "good", "fine voice work", "--kind", "voice")
        self.rate("rev-b", "good", "fine voice work", "--kind", "voice")
        msgs = os.path.join(self.home, "run", "msgs")
        os.makedirs(msgs, exist_ok=True)
        t = time.time()
        with open(os.path.join(msgs, "m-busy000001.json"), "w") as f:
            json.dump({"id": "m-busy000001", "from": "user", "to": "rev-a", "task": "t-long", "kind": "task", "state": "answered",
                       "created": t - 3600, "answered_at": t - 600, "body": "long work", "chain": ["user"], "deadline": t}, f)
        code, out = self.pick("voice")
        self.assertEqual(out["pick"], "rev-b")
        self.assertGreaterEqual([p for p in out["ranked"] if p["name"] == "rev-a"][0]["busy_min"], 49)

    def test_two_bads_in_a_row_skip_the_kind_until_a_good(self):
        self.rate("rev-a", "good", "good review", "--kind", "review")
        self.rate("rev-a", "bad", "review missed the bug", "--kind", "review")
        self.rate("rev-a", "bad", "review missed it again", "--kind", "review")
        code, out = self.pick("review")
        self.assertNotIn("rev-a", [p["name"] for p in out["ranked"]])
        self.assertEqual([p["name"] for p in out["blocked"]], ["rev-a"])
        self.assertIn("skip rev-a: 2 bad verdicts on 'review'", self.ok("pick", "--for", "review").stdout)
        self.assertIn("rev-a", [p["name"] for p in self.pick("docs")[1]["ranked"]], "only that kind is skipped")
        self.rate("rev-a", "good", "review found it", "--kind", "review")
        self.assertIn("rev-a", [p["name"] for p in self.pick("review")[1]["ranked"]])

    def test_a_neutral_between_two_bads_does_not_lift_the_skip(self):
        self.rate("rev-a", "bad", "review missed the bug", "--kind", "review")
        self.rate("rev-a", "neutral", "review so so", "--kind", "review")
        self.rate("rev-a", "bad", "review missed again", "--kind", "review")
        self.assertEqual([p["name"] for p in self.pick("review")[1]["blocked"]], ["rev-a"])

    def test_kind_also_matched_in_the_reason_and_task_of_older_verdicts(self):
        self.hist("rev-c", {"rating": "good", "note": "paused cards out of ready on the board", "task": "board-paused-1126", "t": time.time()})
        self.assertEqual(self.pick("board cleanup")[1]["pick"], "rev-c")

    def test_no_verdict_on_the_kind_falls_back_to_all_verdicts(self):
        self.rate("rev-c", "good", "clear docs", "--kind", "docs")
        code, out = self.pick("kubernetes")
        self.assertEqual((out["by"], out["pick"]), ("all verdicts", "rev-c"))
        self.assertIn("no verdict on 'kubernetes' yet", self.ok("pick", "--for", "kubernetes").stdout)

    def test_equal_scores_go_to_the_description_that_fits(self):
        self.ok("add", self.make_folder("rev-d"), "--name", "rev-d", "--desc", "Finds failure cases: races, dead hangs")
        code, out = self.pick("failure cases")
        self.assertEqual((out["by"], out["pick"]), ("all verdicts", "rev-d"))

    def test_busy_or_disabled_professionals_are_not_picked(self):
        self.rate("rev-a", "good", "great review", "--kind", "review")
        self.ok("disable", "rev-a")
        self.assertNotIn("rev-a", [p["name"] for p in self.pick("review")[1]["ranked"]])

    def test_a_ready_session_is_picked_a_working_one_is_not(self):
        self.rate("rev-a", "good", "great review", "--kind", "review")
        self.summon("rev-a")
        self.assertEqual(self.pick("review")[1]["pick"], "rev-a")
        self.ok("send", "rev-a", "SLEEP 8\nNOREPLY\nwork", "--task", "t-busy")
        self.wait_until(lambda: "rev-a" not in [p["name"] for p in self.pick("review")[1]["ranked"]], 10, "working one left out")

    def test_a_professional_session_may_pick(self):
        self.rate("rev-b", "good", "good review", "--kind", "review")
        r = self.pro("pick", "--for", "review", as_pro="rev-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pick: rev-b", r.stdout)


class GoodAtWeakAt(ProTestCase):
    def test_team_line_from_the_verdict_reasons(self):
        self.config({"default_mode": "dontAsk"})
        self.ok("add", self.make_folder("rev-a"), "--name", "rev-a")
        self.summon("rev-a")
        self.ok("rate", "rev-a", "good", "found the race", "--kind", "review")
        self.ok("rate", "rev-a", "good", "careful review", "--kind", "review")
        self.ok("rate", "rev-a", "bad", "docs were unclear", "--kind", "docs")
        row = json.loads(self.ok("team", "--json").stdout)[0]
        self.assertEqual(row["good_at"][0], "review")
        self.assertIn("docs", row["weak_at"])
        self.assertNotIn("review", row["weak_at"])
        self.assertIn("~ rev-a: good at: review", self.ok("team").stdout)

    def test_no_verdicts_no_line(self):
        self.config({"default_mode": "dontAsk"})
        self.ok("add", self.make_folder("rev-a"), "--name", "rev-a")
        self.summon("rev-a")
        row = json.loads(self.ok("team", "--json").stdout)[0]
        self.assertEqual((row["good_at"], row["weak_at"]), ([], []))
        self.assertNotIn("good at:", self.ok("team").stdout)


class Documented(unittest.TestCase):
    def test_docs(self):
        with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
            self.assertIn("## 0.8.6", f.read())
        with open(os.path.join(ROOT, "plugins", "professionals", "skills", "professionals", "SKILL.md")) as f:
            self.assertIn("pros pick --for", f.read())
        with open(os.path.join(ROOT, "docs", "spec", "spec.md")) as f:
            self.assertIn("pros pick", f.read())


if __name__ == "__main__":
    unittest.main()
