"""0.8.8: the 0.8.7 deadline rule also covers a professional still in its turn when it shows activity (a hook or a
transcript write: tool calls and results) in the last 2 minutes: the deadline moves, same note and cap. A turn with no
activity for longer keeps the timeout note."""
import json
import os
import time
import unittest

from helpers import ProTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class WorkingWithActivity(ProTestCase):
    def notes(self, task):
        return [m["body"] for m in self.msgs() if m["task"] == task and m["kind"] == "note"]

    def task(self, task):
        return [m for m in self.msgs() if m["task"] == task and m["kind"] == "task"][0]

    def start(self, extra=None):
        self.config(dict({"default_mode": "dontAsk", "deliver_deadline_s": 3}, **(extra or {})))
        self.summon("alpha")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        self.ok("send", "alpha", "SLEEP 25\nNOREPLY\nlong work", "--task", "t-w")
        self.wait_until(lambda: json.loads(self.ok("team", "--json").stdout)[0]["activity"] == "working", 10, "working")
        return s

    def age_activity(self, s, secs):
        p = os.path.join(self.home, "run", "state", s + ".json")
        with open(p) as f:
            st = json.load(f)
        st["last_activity"] = time.time() - secs
        with open(p, "w") as f:
            json.dump(st, f)
        tp = st.get("transcript_path")
        if tp and os.path.exists(tp):
            os.utime(tp, (time.time() - secs, time.time() - secs))

    def test_recent_activity_moves_the_deadline(self):
        self.start()
        time.sleep(3.5)
        self.ok("pump")
        m = self.task("t-w")
        self.assertFalse(m.get("overdue"))
        self.assertGreater(m["deadline"], time.time())
        notes = self.notes("t-w")
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("still in its turn", notes[0])
        self.assertFalse(notes[0].startswith("timeout:"), notes[0])

    def test_no_activity_for_over_2_min_times_out(self):
        s = self.start()
        time.sleep(3.5)
        self.age_activity(s, 150)
        self.ok("pump")
        self.assertTrue(self.task("t-w").get("overdue"))
        self.assertTrue([n for n in self.notes("t-w") if n.startswith("timeout:") and "target state: working" in n], self.notes("t-w"))

    def test_same_cap(self):
        self.start({"deadline_extend_max_s": 1})
        time.sleep(3.5)
        self.ok("pump")
        self.assertTrue([n for n in self.notes("t-w") if n.startswith("timeout:")], self.notes("t-w"))


class Documented(unittest.TestCase):
    def test_docs(self):
        with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
            self.assertIn("## 0.8.8", f.read())
        with open(os.path.join(ROOT, "docs", "spec", "spec.md")) as f:
            self.assertIn("activity in the last 2 minutes", f.read())


if __name__ == "__main__":
    unittest.main()
