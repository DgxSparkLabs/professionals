"""0.5.2: defects from the delegation pilot (tasks 264, 265, 266, 310, 312, 313, 325, 328). Each test fails on 0.5.1."""
import json
import os
import re
import time
import unittest
from helpers import *


class Wait(ProTestCase):
    def session(self, name="alpha"):
        return [s for s in self.sessions_alive() if s.startswith("pro-" + name)][0]

    def test_310_wait_blocks_while_the_task_is_queued(self):
        """A task queued behind a busy session: `pros wait` waits for the answer instead of exiting 3 at once."""
        self.summon("alpha")
        self.ok("send", "alpha", "SLEEP 4\nECHO first", "--task", "t-first")
        s = self.session()
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first delivery")
        self.ok("send", "alpha", "ECHO second-answer", "--task", "t-second")
        t0 = time.time()
        r = self.pro("wait", "t-second", "--timeout", "30")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("second-answer", r.stdout)
        self.assertGreater(time.time() - t0, 1.0)


class Sessions(ProTestCase):
    def test_266_open_session_counts_answered_tasks(self):
        self.summon("alpha")
        self.ok("ask", "alpha", "ECHO one", "--wait", "20")
        self.ok("ask", "alpha", "ECHO two", "--wait", "20")
        rows = json.loads(self.ok("sessions", "alpha", "--json").stdout)
        self.assertEqual(rows[-1]["answered"], 2, rows)
        self.assertIn("2 ok/", self.ok("sessions", "alpha").stdout)

    def test_266_task_queued_before_the_start_counts_for_that_session(self):
        self.ok("enable", "alpha")
        self.ok("send", "alpha", "ECHO woke", "--task", "t-pre")  # wake on send: stored before the session exists
        self.wait_until(lambda: any(m["task"] == "t-pre" and m["kind"] == "reply" for m in self.msgs()), 20, "reply")
        self.ok("stop", "alpha")
        rows = json.loads(self.ok("sessions", "alpha", "--json").stdout)
        self.assertEqual(rows[-1]["answered"], 1, rows)


class Doctor(ProTestCase):
    def test_328_doctor_link_refuses_with_a_message_not_a_number(self):
        d = os.path.join(self.fakehome, ".local", "bin")
        os.makedirs(d)
        with open(os.path.join(d, "pros"), "w") as f:
            f.write("#!/bin/sh\n")
        r = self.pro("doctor", "--link", env={"PATH": "/usr/bin:/bin"})
        self.assertIn("not a link", r.stdout + r.stderr)
        self.assertNotRegex(r.stderr.strip(), r"^pros: 2$")
        self.assertEqual(r.returncode, 2)


class Imports(ProTestCase):
    def test_264_imports_dialog_message_names_scope_global(self):
        self.ok("enable", "alpha")
        r = self.pro("summon", "alpha", env={"FAKE_IMPORTS": "1"})
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("--scope global", r.stderr)


class Personas(unittest.TestCase):
    def tools(self, name):
        p = os.path.join(os.path.dirname(PRO), "..", "..", "pro-" + name, "skills", name, "SKILL.md")
        return re.search(r"^tools:\s*(.*)$", open(p).read(), re.M).group(1)

    def test_312_researcher_can_edit_its_own_notes(self):
        self.assertIn("Edit", self.tools("exhaustive-researcher"))

    def test_312_every_persona_that_greps_is_granted_grep_and_glob(self):
        base = os.path.join(os.path.dirname(PRO), "..", "..")
        for d in os.listdir(base):
            if d.startswith("pro-"):
                t = self.tools(d[4:])
                if "Grep" in t:
                    self.assertIn("Glob", t, d)

    def test_312_reviewer_stays_read_only(self):  # deliberate: findings travel in the reply, not in files
        self.assertNotIn("Write", self.tools("reliability-reviewer"))
        self.assertNotIn("Edit", self.tools("reliability-reviewer"))


class GrepGlobGranted(ProTestCase):
    def test_312_fresh_summon_allows_grep_and_glob_without_the_flag(self):
        self.ok("enable", "alpha")
        self.summon("alpha", "--fresh")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        allow = json.loads(self.argv_info(s)["--settings"])["permissions"]["allow"]
        self.assertIn("Grep", allow)
        self.assertIn("Read", allow)
        # the session is told what it was granted and what to do when a tool is missing
        prompt = self.argv_info(s)["--append-system-prompt-file"]
        self.assertIn("Tools granted to this session", prompt)


class Fresh(ProTestCase):
    def session(self, name="alpha"):
        alive = [s for s in self.sessions_alive() if s.startswith("pro-" + name)]
        return alive[0] if alive else None

    def test_313_send_fresh_replaces_an_idle_session(self):
        self.summon("alpha")
        self.ok("ask", "alpha", "ECHO a", "--wait", "20")
        old = self.session()
        old_sid = open(os.path.join(self.fakedir, old + ".sid")).read()
        r = self.ok("ask", "alpha", "ECHO b", "--fresh", "--wait", "30")
        self.assertIn("b", r.stdout)
        self.assertNotEqual(open(os.path.join(self.fakedir, self.session() + ".sid")).read(), old_sid)
        self.assertEqual(len([x for x in self.argv_all(self.session()) if "--resume" in x["argv"]]), 0)

    def test_313_fresh_refuses_while_the_session_is_busy(self):
        self.summon("alpha")
        self.ok("send", "alpha", "SLEEP 6\nECHO a", "--task", "t-busy")
        s = self.session()
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "busy")
        r = self.pro("send", "alpha", "ECHO b", "--fresh")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("busy", r.stderr)
        self.assertEqual(self.session(), s)  # the busy session was not touched


class Prune(ProTestCase):
    def age(self, mid, days):
        p = os.path.join(self.home, "run", "msgs", mid + ".json")
        m = json.load(open(p))
        for k in ("created", "answered_at", "read_at", "injected_at"):
            if k in m:
                m[k] -= days * 86400
        m["deadline"] -= days * 86400
        json.dump(m, open(p, "w"))

    def test_325_prune_removes_old_finished_tasks_only(self):
        self.summon("alpha")
        self.ok("ask", "alpha", "ECHO old", "--task", "t-old", "--wait", "20")
        self.ok("ask", "alpha", "ECHO new", "--task", "t-new", "--wait", "20")
        for m in self.msgs():
            if m["task"] == "t-old":
                self.age(m["id"], 45)
        r = self.ok("prune")
        self.assertIn("pruned", r.stdout)
        left = {m["task"] for m in self.msgs()}
        self.assertNotIn("t-old", left)
        self.assertIn("t-new", left)

    def test_325_prune_keeps_open_tasks_and_honours_retention(self):
        self.summon("alpha")
        self.ok("send", "alpha", "SLEEP 8\nNOREPLY", "--task", "t-open")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "open")
        for m in self.msgs():
            self.age(m["id"], 90)
        self.ok("prune")
        self.assertIn("t-open", {m["task"] for m in self.msgs()})
        self.config({"msg_retention_days": 0})  # 0 = keep everything
        self.assertIn("pruned 0", self.ok("prune").stdout)

    def test_325_pump_prunes_by_itself_once_per_interval(self):
        self.summon("alpha")
        self.ok("ask", "alpha", "ECHO old", "--task", "t-old2", "--wait", "20")
        for m in self.msgs():
            self.age(m["id"], 45)
        stamp = os.path.join(self.home, "run", "prune.stamp")  # an earlier pass already ran: the next one is an hour later
        self.assertTrue(os.path.exists(stamp))
        os.utime(stamp, (time.time() - 7200, time.time() - 7200))
        self.ok("pump")
        self.assertNotIn("t-old2", {m["task"] for m in self.msgs()})


class AutoReplyFiles(ProTestCase):
    def test_265_auto_reply_carries_the_file_the_session_wrote(self):
        """The reply command was denied; the final message only summarised: the written file's text rides along."""
        self.summon("alpha")
        r = self.ok("ask", "alpha", "WRITE /scratch/spec.md # Spec\\nR-1 the thing\nFINAL done, see the file\nNOREPLY", "--wait", "20")
        self.assertIn("auto-reply", r.stdout)
        self.assertIn("/scratch/spec.md", r.stdout)
        self.assertIn("R-1 the thing", r.stdout)

    def test_265_a_file_from_an_earlier_turn_is_not_attached(self):
        self.summon("alpha")
        self.ok("ask", "alpha", "WRITE /scratch/old.md OLDTEXT\nECHO x", "--wait", "20")
        r = self.ok("ask", "alpha", "FINAL plain\nNOREPLY", "--wait", "20")
        self.assertNotIn("OLDTEXT", r.stdout)


if __name__ == "__main__":
    unittest.main()
