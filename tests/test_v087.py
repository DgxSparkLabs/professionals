"""0.8.7: (1) pros pick names the busy professionals that have verdicts on the kind instead of an arbitrary free one;
(2) a task whose professional still works on it (in a turn, or waiting on a command it started) does not time out at its
deadline: seen live, a task timed out with 'target state: idle' while the pane said '1 shell still running'."""
import json
import os
import time
import unittest

from helpers import ProTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class PickNamesBusy(ProTestCase):
    def setUp(self):
        super().setUp()
        self.config({"default_mode": "dontAsk"})
        for n in ("rev-a", "rev-b"):
            self.ok("add", self.make_folder(n), "--name", n)
            self.summon(n)

    def test_busy_ones_with_verdicts_are_named(self):
        self.ok("rate", "rev-a", "good", "careful review", "--kind", "review")
        self.ok("stop", "rev-b")
        self.ok("send", "rev-a", "SLEEP 20\nNOREPLY\nwork", "--task", "t-busy")
        self.wait_until(lambda: json.loads(self.ok("team", "--json").stdout)[0]["activity"] == "working", 10, "working")
        r = self.pro("pick", "--for", "review")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("busy: rev-a (working; 1 verdicts on 'review': 1 good, 0 neutral, 0 bad", r.stdout)
        self.assertNotIn("pick:", r.stdout)
        out = json.loads(self.pro("pick", "--for", "review", "--json").stdout)
        self.assertEqual((out["pick"], [b["name"] for b in out["busy"]]), ("", ["rev-a"]))

    def test_a_free_one_with_verdicts_is_still_picked(self):
        self.ok("rate", "rev-a", "good", "careful review", "--kind", "review")
        self.ok("rate", "rev-b", "neutral", "review was fine", "--kind", "review")
        self.assertIn("pick: rev-a", self.ok("pick", "--for", "review").stdout)

    def test_no_verdict_on_the_kind_anywhere_keeps_the_fallback(self):
        self.ok("rate", "rev-a", "good", "careful review", "--kind", "review")
        self.assertIn("no verdict on 'kubernetes' yet", self.ok("pick", "--for", "kubernetes").stdout)


class WaitingIsNotATimeout(ProTestCase):
    def kill_bg(self):
        for p in [p for p in os.listdir("/proc") if p.isdigit()]:
            try:
                if "snapshot-bash-fake" in open("/proc/%s/cmdline" % p).read() and self.fakedir in open("/proc/%s/cmdline" % p).read():
                    os.kill(int(p), 9)
            except OSError:
                pass

    def notes(self, task):
        return [m["body"] for m in self.msgs() if m["task"] == task and m["kind"] == "note"]

    def start(self, extra):
        self.config(dict({"default_mode": "dontAsk", "deliver_deadline_s": 3}, **extra))
        self.summon("alpha")
        self.addCleanup(self.kill_bg)
        self.ok("send", "alpha", "BG 30\nNOREPLY\nFINAL Waiting for the test run.", "--task", "t-bg")
        self.wait_until(lambda: json.loads(self.ok("team", "--json").stdout)[0]["activity"] == "waiting", 20, "waiting")
        time.sleep(3.5)
        self.ok("pump")

    def test_deadline_moves_while_it_waits_on_its_own_command(self):
        self.start({})
        task = [m for m in self.msgs() if m["task"] == "t-bg" and m["kind"] == "task"][0]
        self.assertEqual(task["state"], "injected")
        self.assertFalse(task.get("overdue"))
        self.assertGreater(task["deadline"], time.time())
        notes = self.notes("t-bg")
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("still waiting on a command it started", notes[0])
        self.assertFalse(notes[0].startswith("timeout:"), notes[0])
        time.sleep(3.5)
        self.ok("pump")
        self.assertEqual(len(self.notes("t-bg")), 1, "one note, not one per move")

    def test_the_wait_has_a_cap(self):
        self.start({"deadline_extend_max_s": 1})
        time.sleep(3.5)
        self.ok("pump")
        notes = self.notes("t-bg")
        self.assertTrue([n for n in notes if "timeout" in n and "target state: waiting" in n], notes)

    def test_a_ready_professional_still_times_out(self):
        self.config({"default_mode": "dontAsk", "deliver_deadline_s": 3})
        self.summon("alpha")
        self.ok("send", "alpha", "NOREPLY\nquiet", "--task", "t-q")
        time.sleep(4)
        self.ok("pump")
        self.assertTrue([n for n in self.notes("t-q") if "timeout" in n and "target state: idle" in n], self.notes("t-q"))


class Documented(unittest.TestCase):
    def test_docs(self):
        with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
            self.assertIn("## 0.8.7", f.read())
        with open(os.path.join(ROOT, "docs", "spec", "spec.md")) as f:
            self.assertIn("deadline_extend_max_s", f.read())


if __name__ == "__main__":
    unittest.main()
