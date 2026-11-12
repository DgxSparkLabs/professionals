"""0.4.0: the four runtime gaps (B1) and wake on message (B3). Each test fails on 0.3.1."""
import os
import re
import subprocess
import time
import unittest
from helpers import *


class RuntimeGaps(ProTestCase):
    def session(self, name="alpha"):
        return [s for s in self.sessions_alive() if s.startswith("pro-" + name)][0]

    def kill(self, s):
        subprocess.run(["tmux", "-L", self.sock, "kill-session", "-t", "=" + s])

    def test_b1a_one_redelivery_per_restart_then_give_up(self):
        """An unanswered task is resent once after a restart (counter on the message); a crash loop never resends it again."""
        self.summon("alpha")
        s = self.session()
        self.ok("send", "alpha", "SLEEP 60\nNOREPLY", "--task", "t-loop")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        self.kill(s)
        self.summon("alpha")
        self.wait_until(lambda: len(self.acts(s)) == 2, 20, "one resend")
        m = [x for x in self.msgs() if x["kind"] == "task"][0]
        self.assertEqual(m.get("resends"), 1)
        self.assertTrue(self.acts(s)[1].endswith("dup"))
        self.kill(s)
        self.summon("alpha")
        time.sleep(3)
        self.assertEqual(len(self.acts(s)), 2, self.acts(s))  # no second resend
        m = [x for x in self.msgs() if x["kind"] == "task"][0]
        self.assertEqual((m["state"], m.get("resends")), ("failed", 1))
        self.assertIn("resent", m.get("fail_reason", ""))
        notes = [x for x in self.msgs() if x["kind"] == "note" and x["task"] == "t-loop"]
        self.assertEqual(len(notes), 1)
        self.assertIn("not resent again", notes[0]["body"])

    def test_b1a_no_resend_after_the_deadline(self):
        self.config({"deliver_deadline_s": 3})
        self.summon("alpha")
        s = self.session()
        self.ok("send", "alpha", "SLEEP 60\nNOREPLY")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        self.kill(s)
        time.sleep(4)
        self.summon("alpha")
        time.sleep(3)
        self.assertEqual(len(self.acts(s)), 1, self.acts(s))
        m = [x for x in self.msgs() if x["kind"] == "task"][0]
        self.assertEqual(m["state"], "failed")

    def test_b1b_wait_prefers_the_late_reply_over_the_timeout_note(self):
        """T-X1: after the deadline note, a late real reply is what `pros wait` returns (exit 0)."""
        self.config({"deliver_deadline_s": 5})
        self.summon("alpha")
        r = self.pro("ask", "alpha", "SLEEP 9\nECHO late-answer", "--wait", "2")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        task = re.search(r"pros wait (t-\w+)", r.stdout).group(1)
        time.sleep(4)
        self.ok("pump")
        self.wait_until(lambda: any(m["kind"] == "note" and m["task"] == task for m in self.msgs()), 10, "timeout note")
        self.wait_until(lambda: any(m["kind"] == "reply" and m["task"] == task for m in self.msgs()), 20, "late reply")
        r = self.pro("wait", task, "--timeout", "5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("late-answer", r.stdout)
        self.assertIn("kind=reply", r.stdout)

    def test_b1b_reused_task_id_waits_for_the_new_reply(self):
        """B1-2: a second ask with the same task id but new text does not get the stale first reply."""
        self.summon("alpha")
        self.assertIn("first", self.ok("ask", "alpha", "ECHO first", "--task", "t-re", "--wait", "20").stdout)
        self.ok("send", "alpha", "SLEEP 2\nECHO second", "--task", "t-re")
        r = self.pro("wait", "t-re", "--timeout", "20")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("second", r.stdout)
        self.assertNotIn("first", r.stdout)

    def test_b1c_wait_ends_when_the_target_is_stuck(self):
        self.config({"stuck_min": 0.05})  # 3 s without activity while busy
        self.summon("alpha")
        r = self.pro("ask", "alpha", "SLEEP 60\nNOREPLY", "--wait", "1")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        task = re.search(r"pros wait (t-\w+)", r.stdout).group(1)
        t = time.time()
        r = self.pro("wait", task, "--timeout", "40")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertLess(time.time() - t, 20)
        self.assertIn("stuck", r.stdout)

    def test_b1d_audit_status_before_first_import_is_one_line(self):
        r = self.pro("audit", "status")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        lines = [x for x in r.stderr.splitlines() if x.strip()]
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertIn("pros audit import", lines[0])


class WakeOnMessage(ProTestCase):
    def test_b3_send_starts_an_allowlisted_professional(self):
        self.assertEqual(self.sessions_alive(), [])
        r = self.ok("send", "beta", "ECHO woke-up")
        self.assertTrue(any(s.startswith("pro-beta") for s in self.sessions_alive()))
        self.assertIn("not running; starting it", r.stderr)
        task = re.search(r"task=(t-\w+)", r.stdout).group(1)
        r = self.pro("wait", task, "--timeout", "20")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("woke-up", r.stdout)

    def test_b3_no_wake_keeps_the_refusal(self):
        r = self.pro("send", "beta", "hi", "--no-wake")
        self.assertEqual(r.returncode, 5)
        self.assertIn("summon", r.stderr)
        self.assertEqual(self.sessions_alive(), [])
        self.assertEqual(self.msgs(), [])

    def test_b3_caps_still_refuse_with_exit_4(self):
        self.config({"max_live": 1})
        self.summon("alpha")
        r = self.pro("send", "beta", "ECHO x")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("live cap", r.stderr)
        self.assertFalse(any(s.startswith("pro-beta") for s in self.sessions_alive()))
        self.assertEqual([m for m in self.msgs() if m["to"] == "beta"], [])

    def test_b3_hourly_budget_refuses_with_exit_4(self):
        self.config({"summon_budget_per_hour": 1})
        self.summon("alpha")
        r = self.pro("send", "beta", "ECHO x")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("budget", r.stderr)

    def test_b3_store_first_when_the_wake_fails(self):
        """The wake fails to start the target: the message is kept queued and delivered on the next start."""
        r = self.pro("send", "beta", "ECHO kept", env={"FAKE_FAIL": "1"})
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("kept queued", r.stderr)
        ms = [m for m in self.msgs() if m["to"] == "beta"]
        self.assertEqual([m["state"] for m in ms], ["queued"])
        self.summon("beta")
        r = self.pro("wait", ms[0]["task"], "--timeout", "20")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("kept", r.stdout)

    def test_b3_not_listed_target_still_refused(self):
        r = self.pro("send", "nobody-here", "hi")
        self.assertEqual(r.returncode, 4)
        self.assertEqual(self.sessions_alive(), [])


if __name__ == "__main__":
    unittest.main()
