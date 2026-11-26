"""0.8.1: the Stop-hook cap hands off to an immediate delivery (no wait for the pump; T113), and a professional whose turn
ends while a command it started still runs sends its final message as kind=status, not as the reply."""
import glob
import json
import os
import time
import unittest

from helpers import ProTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _session(self, name="alpha"):
    return [s for s in self.sessions_alive() if s.startswith("pro-" + name)][0]


ProTestCase.session = _session


class CapHandoff(ProTestCase):
    def test_cap_does_not_wait_for_the_pump(self):
        # no pump runs in tests (PRO_NO_AUTO) and nobody runs a pros command: before 0.8.1 the queue stopped at the cap
        self.config({"max_blocks": 2, "pump_interval_s": 60})
        self.summon("alpha")
        s = self.session()
        self.ok("send", "alpha", "SLEEP 2")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first acted")
        for i in range(5):
            self.ok("send", "alpha", "ECHO m%d" % i)
        t0 = time.time()
        self.wait_until(lambda: len(self.acts(s)) == 6, 25, "all six delivered without a pump pass")
        self.assertLess(time.time() - t0, 20)
        ids = [a.split()[1] for a in self.acts(s)]
        self.assertEqual(len(set(ids)), 6)  # no duplicates
        via = [m["via"] for m in self.msgs() if m["body"].startswith("ECHO m")]
        self.assertIn("stop-hook", via)
        self.assertIn("paste", via)  # past the cap: a fresh turn, so the cap still breaks the Stop-hook chain
        self.assertFalse([l for l in self.fake_log(s) if l.startswith("MIDTURN")])
        self.assertFalse([m for m in self.msgs() if "delivery cap" in m["body"]])  # nothing is stuck, so no note


class WaitingStatus(ProTestCase):
    def kinds(self, task):
        return [m["kind"] for m in self.msgs() if m["task"] == task and m["kind"] != "task"]

    def test_final_message_while_waiting_is_a_status(self):
        self.summon("alpha")
        s = self.session()
        self.ok("send", "alpha", "BG 30\nNOREPLY\nFINAL Waiting for the container-1 health check.", "--task", "t-hc")
        self.wait_until(lambda: self.kinds("t-hc"), 20, "end-of-turn message stored")
        time.sleep(1)
        self.assertEqual(self.kinds("t-hc"), ["status"])
        st = [m for m in self.msgs() if m["task"] == "t-hc" and m["kind"] == "status"][0]
        self.assertIn("container-1 health check", st["body"])
        self.assertEqual(st["state"], "queued")
        task = [m for m in self.msgs() if m["task"] == "t-hc" and m["kind"] == "task"][0]
        self.assertEqual(task["state"], "injected")  # still open: the real reply comes when the work ends
        inbox = self.ok("inbox").stdout
        self.assertIn("kind=status", inbox)
        self.assertEqual(self.pro("wait", "t-hc", "--timeout", "2").returncode, 3)  # a status is not an answer
        for p in [p for p in os.listdir("/proc") if p.isdigit()]:  # the background work ends
            try:
                c = open("/proc/%s/cmdline" % p).read()
                if "snapshot-bash-fake" in c and self.fakedir in c:
                    os.kill(int(p), 9)
            except OSError:
                pass
        self.wait_until(lambda: json.loads(self.ok("team", "--json").stdout)[0]["activity"] == "ready", 10, "ready")
        # the turn Claude Code starts when the command ends: its final message is the reply
        payload = json.dumps({"stop_hook_active": False, "session_id": "z", "last_assistant_message": "Health check passed."})
        self.ok("hook", "stop", input=payload, env={"PRO_SELF": "alpha", "PRO_SESSION": s})
        self.assertEqual(self.kinds("t-hc"), ["status", "reply"])
        r = self.ok("wait", "t-hc", "--timeout", "5")
        self.assertIn("Health check passed.", r.stdout)
        self.ok("read", st["id"])
        self.assertIn("0 unread", self.ok("inbox").stdout)
        ev = []
        for p in glob.glob(os.path.join(self.home, "audit", "spool", "*.jsonl")):
            ev += [json.loads(x) for x in open(p) if x.strip()]
        self.assertTrue([e for e in ev if e.get("msg") == task["id"]])  # audit runs here
        self.assertFalse([e for e in ev if e.get("msg") == st["id"]])  # event-v1 has no status kind: no event names it

    def test_ready_pro_still_auto_replies(self):
        self.summon("alpha")
        self.ok("send", "alpha", "NOREPLY\nFINAL all done", "--task", "t-rd")
        self.wait_until(lambda: self.kinds("t-rd"), 20, "auto-reply")
        self.assertEqual(self.kinds("t-rd"), ["reply"])


class TeamTitle(ProTestCase):
    def row(self):
        return [r for r in json.loads(self.ok("team", "--json").stdout) if r["name"] == "alpha"][0]

    def test_waiting_row_keeps_task_and_title(self):
        self.summon("alpha")
        self.ok("send", "alpha", "ECHO run the container-1 health check\nBG 20", "--task", "t-wt")
        self.wait_until(lambda: self.row()["activity"] == "waiting", 20, "waiting")
        r = self.row()
        self.assertEqual((r["task"], r["title"]), ("t-wt", "ECHO run the container-1 health check"))

    def test_working_row_title_is_cut(self):
        self.summon("alpha")
        self.ok("send", "alpha", "ECHO " + "x" * 200 + "\nSLEEP 4", "--task", "t-long")
        self.wait_until(lambda: self.row()["activity"] == "working", 10, "working")
        r = self.row()
        self.assertEqual(r["task"], "t-long")
        self.assertEqual(len(r["title"]), 120)
        self.assertTrue(r["title"].startswith("ECHO xxx") and r["title"].endswith("\u2026"))
        self.ok("stop", "alpha")
        self.summon("alpha")
        self.assertEqual((self.row()["task"], self.row()["title"]), ("", ""))  # ready: no task, no title


class Documented(unittest.TestCase):
    def test_documented(self):
        for f, words in ((("docs", "spec", "spec.md"), ("kind=status", "max_blocks")), (("CHANGELOG.md",), ("## 0.8.1",)),
                         (("plugins", "professionals", "skills", "professionals", "SKILL.md"), ("kind=status",))):
            with open(os.path.join(ROOT, *f), encoding="utf-8") as fh:
                t = fh.read()
            for w in words:
                self.assertIn(w, t, f)


if __name__ == "__main__":
    unittest.main()
