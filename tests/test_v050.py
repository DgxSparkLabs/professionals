"""0.5.0: failover (B2). A task whose professional dies, or misses its deadline while broken, is re-run once on a fresh
session of the same persona, or on a fallback the sender named; never more than max_attempts runs. Each test fails on 0.4.0."""
import json
import os
import re
import subprocess
import sys
import time
import unittest
from helpers import *

LIB = os.path.join(os.path.dirname(PRO), "..", "lib")


class Failover(ProTestCase):
    def session(self, name="alpha"):
        alive = [s for s in self.sessions_alive() if s.startswith("pro-" + name)]
        return alive[0] if alive else None

    def kill(self, s):
        subprocess.run(["tmux", "-L", self.sock, "kill-session", "-t", "=" + s])

    def task_msgs(self, task):
        return [m for m in self.msgs() if m["kind"] == "task" and m["task"] == task]

    def notes(self, task):
        return [m for m in self.msgs() if m["kind"] == "note" and m["task"] == task]

    def replies(self, task):
        return [m for m in self.msgs() if m["kind"] == "reply" and m["task"] == task]

    def trace(self, task):
        import audit_schema as sch
        self.ok("audit", "import")
        env = json.loads(self.ok("audit", "trace", task, "--json").stdout)
        self.assertEqual(sch.validate(env, sch.load("query-v1.json")), [])  # the additive `failover` field is in the contract
        return env["result"]

    def test_scenario_killed_mid_task_is_rerun_on_a_fresh_session_and_answered_once(self):
        """The acceptance scenario: kill the target mid-task; the task is re-run on a fresh session, answered once,
        the sender gets one note for the failed attempt, and `pros audit trace` shows both attempts."""
        self.summon("alpha")
        s = self.session()
        self.ok("send", "alpha", "SLEEP 4\nECHO rerun-answer", "--task", "t-fo")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        self.kill(s)
        r = self.pro("wait", "t-fo", "--timeout", "60")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("rerun-answer", r.stdout)
        tm = self.task_msgs("t-fo")
        self.assertEqual(len(tm), 2, tm)
        first, second = tm
        self.assertEqual((first["state"], first.get("superseded_by")), ("failed", second["id"]))
        self.assertEqual((second["attempt"], second["retry_of"], second["fo_via"], second["to"]), (2, first["id"], "alpha", "alpha"))
        self.assertEqual(second["state"], "answered")
        self.assertEqual(len(self.replies("t-fo")), 1)
        notes = self.notes("t-fo")
        self.assertEqual(len(notes), 1, [n["body"] for n in notes])
        self.assertIn("attempt 1 of 2", notes[0]["body"])
        self.assertIn("died", notes[0]["body"])
        argv = self.argv_all(s)[-1]["argv"]  # the re-run started fresh, not by resuming the dead session
        self.assertNotIn("--resume", argv)
        t = self.trace("t-fo")
        self.assertEqual(len(t["assignments"]), 2, t["assignments"])
        fo = [a for a in t["assignments"] if a.get("failover")]
        self.assertEqual(len(fo), 1)
        self.assertEqual((fo[0]["failover"]["attempt"], fo[0]["failover"]["of"], fo[0]["failover"]["via"]), (2, first["id"], "alpha"))
        self.assertEqual(fo[0]["delivery"], "answered")
        text = self.ok("audit", "trace", "t-fo").stdout
        self.assertIn("attempt 2", text)

    def test_never_more_than_max_attempts(self):
        self.summon("alpha")
        s = self.session()
        self.ok("send", "alpha", "SLEEP 60\nNOREPLY", "--task", "t-max")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        self.kill(s)
        self.ok("pump")
        self.wait_until(lambda: self.session() and len(self.acts(self.session())) >= 2, 30, "second attempt delivered")
        self.kill(self.session())
        r = self.pro("wait", "t-max", "--timeout", "15")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("no attempts left", r.stdout)
        time.sleep(2)
        self.ok("pump")
        self.assertEqual(len(self.task_msgs("t-max")), 2)
        self.assertIsNone(self.session())  # no third start
        notes = self.notes("t-max")
        self.assertEqual(len(notes), 2, [n["body"] for n in notes])  # one note per attempt
        self.assertIn("attempt 2 of 2", notes[1]["body"])
        self.assertEqual([m["state"] for m in self.task_msgs("t-max")], ["failed", "failed"])

    def test_max_attempts_config_is_read(self):
        self.config({"max_attempts": 3})
        self.summon("alpha")
        self.ok("send", "alpha", "SLEEP 60\nNOREPLY", "--task", "t-three")
        for n in (1, 2, 3):
            self.wait_until(lambda: self.session() and any(m.get("attempt", 1) == n and m["state"] == "injected"
                                                           for m in self.task_msgs("t-three")), 30, "attempt %d delivered" % n)
            self.kill(self.session())
            self.ok("pump")
        self.assertEqual([m.get("attempt", 1) for m in self.task_msgs("t-three")], [1, 2, 3])
        self.assertEqual(len(self.notes("t-three")), 3)

    def test_fallback_named_by_the_sender(self):
        self.summon("alpha")
        s = self.session()
        r = self.ok("send", "alpha", "SLEEP 4\nECHO from-fallback", "--task", "t-fb", "--fallback", "beta")
        self.assertIn("fallback=beta", r.stdout)
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        self.kill(s)
        r = self.pro("wait", "t-fb", "--timeout", "60")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("from-fallback", r.stdout)
        self.assertIn("from=beta", r.stdout)
        first, second = self.task_msgs("t-fb")
        self.assertEqual((second["to"], second["fo_via"], second["chain"], second["attempt"]), ("beta", "alpha", ["beta"], 2))
        self.assertIsNone(self.session("alpha"))  # alpha was not restarted
        b = self.session("beta")
        self.assertTrue(any(second["id"] in a for a in self.acts(b)))
        notes = self.notes("t-fb")
        self.assertEqual(len(notes), 1)
        self.assertIn("beta", notes[0]["body"])

    def test_fallback_header_records_via(self):
        mod = load_pros(self.home)
        m = mod.new_msg("user", "beta", "t-h", "task", "user", ["beta"], "x", 0)
        m.update(attempt=2, fo_via="alpha")
        self.assertIn(" chain=beta via=alpha attempt=2]", mod.header(m))
        m2 = mod.new_msg("user", "beta", "t-h", "task", "user", ["beta"], "x", 0)
        self.assertTrue(mod.header(m2).endswith("chain=beta]"))  # first attempts keep the 0.4.0 header

    def test_ask_accepts_fallback(self):
        self.summon("alpha")
        r = self.ok("ask", "alpha", "ECHO direct", "--fallback", "beta", "--wait", "20")
        self.assertIn("direct", r.stdout)
        self.assertEqual(self.task_msgs_any()[0]["fallback"], "beta")

    def task_msgs_any(self):
        return [m for m in self.msgs() if m["kind"] == "task"]

    def test_fallback_must_be_allowlisted_exit_4_nothing_stored(self):
        self.summon("alpha")
        r = self.pro("send", "alpha", "ECHO x", "--fallback", "nobody-here")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(self.task_msgs_any(), [])
        r = self.pro("send", "alpha", "ECHO x", "--fallback", "alpha")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.task_msgs_any(), [])

    def test_sender_that_may_not_summon_cannot_name_a_fallback(self):
        self.summon("alpha")
        r = self.pro("send", "alpha", "ECHO x", "--fallback", "beta", as_pro="gamma")  # gamma: may_summon false
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("may_summon", r.stderr)
        self.assertEqual(self.task_msgs_any(), [])

    def test_caps_still_apply_to_the_rerun_and_the_message_is_kept(self):
        self.config({"summon_budget_per_hour": 1})
        self.summon("alpha")
        s = self.session()
        self.ok("send", "alpha", "SLEEP 30\nNOREPLY", "--task", "t-cap", "--fallback", "beta")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        self.kill(s)
        self.ok("pump")
        self.assertIsNone(self.session("beta"))
        tm = self.task_msgs("t-cap")
        self.assertEqual([m["state"] for m in tm], ["failed", "queued"])  # store-first: the re-run waits for a start
        notes = self.notes("t-cap")
        self.assertEqual(len(notes), 1)
        self.assertIn("budget", notes[0]["body"])

    def test_deadline_missed_by_a_stuck_target_reruns_fresh(self):
        self.config({"deliver_deadline_s": 4, "stuck_min": 0.03})
        self.summon("alpha")
        s = self.session()
        self.ok("send", "alpha", "SLEEP 30\nNOREPLY", "--task", "t-dl", env={})
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        time.sleep(4.5)
        self.ok("pump")
        tm = self.task_msgs("t-dl")
        self.assertEqual(len(tm), 2, tm)
        self.assertIn("deadline", tm[0]["fail_reason"])
        self.assertEqual(tm[1]["attempt"], 2)
        notes = self.notes("t-dl")
        self.assertEqual(len(notes), 1, [n["body"] for n in notes])  # the failover note replaces the timeout note
        self.assertIn("deadline", notes[0]["body"])
        self.wait_until(lambda: self.session() and any(tm[1]["id"] in a for a in self.acts(self.session())), 30, "re-run delivered")

    def test_deadline_missed_with_fallback_answered_once(self):
        """The original target is busy past its deadline: the fallback runs in parallel; the first reply wins,
        a second one is stored (never dropped) but not delivered."""
        self.config({"deliver_deadline_s": 4})
        self.summon("alpha")
        s = self.session()
        self.ok("send", "alpha", "SLEEP 6\nECHO answer", "--task", "t-par", "--fallback", "beta")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        time.sleep(4.5)
        self.ok("pump")
        self.assertEqual(len(self.task_msgs("t-par")), 2)
        self.wait_until(lambda: len(self.replies("t-par")) == 2, 40, "both attempts replied")
        rs = self.replies("t-par")
        self.assertEqual(sum(1 for m in rs if not m.get("suppressed")), 1, rs)
        out = self.ok("inbox").stdout
        self.assertEqual(out.count("kind=reply"), 1, out)
        r = self.pro("wait", "t-par", "--timeout", "5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        time.sleep(1)
        self.ok("pump")
        self.assertEqual(len([n for n in self.notes("t-par") if "timeout" in n["body"]]), 0)

    def test_healthy_busy_target_past_deadline_keeps_the_late_reply(self):
        """No fallback and a target that is busy, not broken: no re-run (it would kill working progress); 0.4.0's
        timeout note and late reply stay."""
        self.config({"deliver_deadline_s": 3})
        self.summon("alpha")
        s = self.session()
        self.ok("send", "alpha", "SLEEP 6\nECHO late", "--task", "t-slow")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        time.sleep(3.5)
        self.ok("pump")
        self.assertEqual(len(self.task_msgs("t-slow")), 1)
        self.assertTrue(any("timeout" in n["body"] for n in self.notes("t-slow")))

    def test_max_attempts_1_keeps_the_040_died_note(self):
        self.config({"max_attempts": 1})
        self.summon("alpha")
        s = self.session()
        self.ok("send", "alpha", "SLEEP 30\nNOREPLY", "--task", "t-one")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        self.kill(s)
        self.ok("pump")
        self.assertEqual(len(self.task_msgs("t-one")), 1)
        self.assertIsNone(self.session())
        self.assertIn("summon it again to resend", self.notes("t-one")[0]["body"])

    def test_audit_events_for_each_attempt_validate(self):
        self.summon("alpha")
        s = self.session()
        self.ok("send", "alpha", "SLEEP 3\nECHO ok", "--task", "t-aud")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        self.kill(s)
        r = self.pro("wait", "t-aud", "--timeout", "60")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sys.path.insert(0, LIB)
        from pros_audit import importer as I
        evs = []
        spool = os.path.join(self.home, "audit", "spool")
        for f in sorted(os.listdir(spool)):
            with open(os.path.join(spool, f)) as fh:
                evs += [json.loads(x) for x in fh.read().splitlines() if x.strip()]
        self.assertEqual([e for e in evs if I.check(e)], [])
        import audit_schema as sch
        schema = sch.load("event-v1.json")
        for e in evs:
            self.assertEqual(sch.validate(e, schema), [], e)
        first, second = self.task_msgs("t-aud")
        created = [e for e in evs if e["action"] == "msg_created" and e.get("msg") == second["id"]][0]["data"]
        self.assertEqual((created["attempt"], created["retry_of"], created["via_pro"]), (2, first["id"], "alpha"))
        failed = [e for e in evs if e["action"] == "msg_failed" and e.get("msg") == first["id"]]
        self.assertEqual([e["data"]["reason"] for e in failed], ["recipient_died"])
        self.ok("audit", "import")
        import sqlite3
        db = sqlite3.connect(os.path.join(self.home, "audit", "audit.db"))
        self.assertEqual(db.execute("SELECT count(*) FROM quarantine").fetchone()[0], 0)
        db.close()


class FailoverUnit(unittest.TestCase):
    def test_derive_puts_a_rerun_in_the_tree_of_the_first_attempt(self):
        sys.path.insert(0, LIB)
        from pros_audit import derive as D
        msgs = {"m-a": {"parent": None}, "m-b": {"parent": None, "retry_of": "m-a"}, "m-c": {"parent": "m-b"},
                "m-x": {"parent": "m-p", "retry_of": "m-y"}, "m-y": {"parent": "m-p"}, "m-p": {"parent": None}}
        self.assertEqual(D._walk(msgs, "m-b"), ("m-a", 0, 0))
        self.assertEqual(D._walk(msgs, "m-c"), ("m-a", 1, 0))
        self.assertEqual(D._walk(msgs, "m-x"), ("m-p", 1, 0))


if __name__ == "__main__":
    unittest.main()


class Switches051(ProTestCase):
    """0.5.1: config switches wake_on_send and failover; the re-run says the first attempt may have partly run."""
    def test_wake_on_send_false_refuses_send_and_ask(self):
        self.config({"wake_on_send": False})
        r = self.pro("send", "beta", "hi")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        r = self.pro("ask", "beta", "hi", "--wait", "5")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertEqual(self.sessions_alive(), [])

    def test_wake_on_send_default_still_wakes(self):
        r = self.pro("send", "beta", "hi")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(any(s.startswith("pro-beta") for s in self.sessions_alive()))

    def test_failover_false_does_not_rerun(self):
        self.config({"failover": False})
        self.summon("alpha")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        self.ok("send", "alpha", "SLEEP 60\nNOREPLY", "--task", "t-off")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        subprocess.run(["tmux", "-L", self.sock, "kill-session", "-t", "=" + s])
        self.ok("pump")
        time.sleep(2)
        self.ok("pump")
        tm = [m for m in self.msgs() if m["kind"] == "task" and m["task"] == "t-off"]
        self.assertEqual(len(tm), 1, tm)

    def test_rerun_message_warns_about_side_effects(self):
        self.summon("alpha")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        self.ok("send", "alpha", "SLEEP 4\nECHO again", "--task", "t-hdr")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        subprocess.run(["tmux", "-L", self.sock, "kill-session", "-t", "=" + s])
        r = self.pro("wait", "t-hdr", "--timeout", "60")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        import importlib.machinery, importlib.util
        ld = importlib.machinery.SourceFileLoader("pros_mod", PRO)  # render() is what is pasted into the session
        spec = importlib.util.spec_from_loader("pros_mod", ld)
        mod = importlib.util.module_from_spec(spec)
        ld.exec_module(mod)
        first, second = [m for m in self.msgs() if m["kind"] == "task" and m["task"] == "t-hdr"]
        self.assertNotIn("previous attempt failed", mod.render(first))
        self.assertIn("previous attempt failed: check what is already done before repeating side effects", mod.render(second))
