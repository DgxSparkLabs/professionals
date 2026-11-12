import json
import os
import subprocess
import tempfile
import time
import unittest
from helpers import *


class Resume(ProTestCase):
    def setUp(self):
        super().setUp()
        self.d = self.make_folder("res")
        self.ok("add", self.d)

    def first_sid(self):
        r = self.summon("res")
        self.assertIn("fresh: no earlier session", r.stdout)
        a = self.argv_info("pro-res")["argv"]
        return a[a.index("--session-id") + 1]

    def last_argv(self):
        return self.argv_all("pro-res")[-1]["argv"]

    def test_new_session_gets_id_and_history_row(self):
        u = self.first_sid()
        rec = json.load(open(os.path.join(self.home, "run", "sessions", "pro-res.json")))
        self.assertEqual((rec["session_id"], rec["entry"], rec["kind"]), (u, "res", "folder"))
        rows = self.history("res")
        self.assertEqual([(r["ev"], r["id"]) for r in rows], [("start", u)])
        self.assertEqual(rows[0]["workdir"], self.d)

    def test_resume_after_stop_keeps_id(self):
        u = self.first_sid()
        self.ok("stop", "res")
        self.assertEqual([r["ev"] for r in self.history("res")], ["start", "end"])
        self.assertEqual(self.history("res")[1]["reason"], "stopped")
        r = self.summon("res")
        self.assertIn("resumed=%s" % u, r.stdout)
        a = self.last_argv()
        self.assertEqual(a[a.index("--resume") + 1], u)
        self.assertNotIn("--session-id", a)
        self.assertNotIn("--fork-session", a)
        self.assertIn("Earlier tasks in this context are closed", self.argv_info("pro-res")["--append-system-prompt-file"])
        self.assertEqual(self.listing()["res"]["last_session"], u)

    def test_fresh_flag_wins_over_good_session(self):
        u = self.first_sid()
        self.ok("rate", "res", "good")
        self.ok("stop", "res")
        r = self.summon("res", "--fresh")
        self.assertIn("fresh: --fresh", r.stdout)
        a = self.last_argv()
        self.assertNotIn("--resume", a)
        self.assertNotEqual(a[a.index("--session-id") + 1], u)

    def test_fork_copies_context_into_new_id(self):
        u = self.first_sid()
        self.ok("stop", "res")
        r = self.summon("res", "--fork")
        a = self.last_argv()
        self.assertEqual(a[a.index("--resume") + 1], u)
        self.assertIn("--fork-session", a)
        new = a[a.index("--session-id") + 1]
        self.assertNotEqual(new, u)
        self.assertIn("forked=%s from=%s" % (new, u), r.stdout)
        starts = [r for r in self.history("res") if r["ev"] == "start"]
        self.assertEqual(starts[-1]["forked_from"], u)

    def test_bad_session_starts_fresh_but_session_flag_overrides(self):
        u = self.first_sid()
        self.ok("rate", "res", "bad", "--note", "wrong answers")
        self.ok("stop", "res")
        r = self.summon("res")
        self.assertIn("fresh: last session bad (rated bad)", r.stdout)
        self.ok("stop", "res")
        r = self.summon("res", "--session", u)
        self.assertIn("resumed=%s" % u, r.stdout)

    def test_rating_good_resumes(self):
        u = self.first_sid()
        self.ok("rate", "res", "good")
        self.ok("stop", "res")
        self.assertIn("resumed=%s" % u, self.summon("res").stdout)
        s = json.loads(self.ok("sessions", "res", "--json").stdout)
        self.assertEqual((s[0]["rating"], s[0]["score"]), ("good", "good"))

    def test_missing_transcript_falls_back_to_fresh(self):
        u = self.first_sid()
        self.ok("stop", "res")
        for root, _, files in os.walk(os.path.join(self.fakehome, ".claude")):
            for f in files:
                if f == u + ".jsonl":
                    os.unlink(os.path.join(root, f))
        r = self.summon("res")
        self.assertEqual(r.returncode, 0)
        self.assertIn("fresh: earlier transcript missing", r.stdout)

    def test_old_session_not_resumed(self):
        self.config({"resume_max_days": 0})
        self.first_sid()
        self.ok("stop", "res")
        time.sleep(1.1)
        self.assertIn("older than 0 days", self.summon("res").stdout)

    def test_live_elsewhere_forks(self):
        u = self.first_sid()
        self.ok("stop", "res")
        for ext, val in ((".status", "idle"), (".pid", str(os.getpid())), (".sid", u)):  # a live process holds the id
            open(os.path.join(self.fakedir, "elsewhere" + ext), "w").write(val)
        r = self.summon("res")
        self.assertIn("forked=", r.stdout)
        self.assertIn("live elsewhere", r.stdout)
        self.assertIn("--fork-session", self.last_argv())

    def test_agents_unreadable_means_fork_never_blind_resume(self):
        u = self.first_sid()
        self.ok("stop", "res")
        r = self.summon("res", env={"FAKE_AGENTS_FAIL": "1"})
        self.assertIn("forked=", r.stdout)
        self.assertIn("could not tell", r.stdout)
        self.assertEqual(self.last_argv().count("--fork-session"), 1)

    def test_resume_that_fails_to_start_retries_fresh_once(self):
        u = self.first_sid()
        self.ok("stop", "res")
        r = self.summon("res", env={"FAKE_FAIL_RESUME": "1"})
        self.assertIn("fresh: resume failed", r.stdout)
        argvs = [x["argv"] for x in self.argv_all("pro-res")]
        self.assertIn("--resume", argvs[-2])
        self.assertNotIn("--resume", argvs[-1])
        reasons = [x.get("reason") for x in self.history("res") if x["ev"] == "end"]
        self.assertIn("failed-to-start", reasons)

    def test_reap_keeps_entry_and_history_then_resumes(self):
        self.config({"idle_reap_min": 0.01})
        u = self.first_sid()
        time.sleep(1.5)
        r = self.ok("reap")
        self.assertIn("res", r.stdout)
        self.assertEqual(self.sessions_alive(), [])
        row = self.listing()["res"]
        self.assertEqual(row["state"], "enabled")
        self.assertIn("reaped", row["last"])
        self.assertIn("resumed=%s" % u, self.summon("res").stdout)

    def test_keep_entry_is_never_reaped(self):
        self.config({"idle_reap_min": 0.01})
        self.ok("keep", "res")
        self.first_sid()
        other = self.make_folder("res2")
        self.ok("add", other)
        self.summon("res2")
        time.sleep(1.5)
        r = self.ok("reap")
        self.assertIn("res2", r.stdout)
        self.assertNotIn("res,", r.stdout + ",")
        self.assertEqual(self.sessions_alive(), ["pro-res"])

    def test_tmux_server_kill_is_a_reboot_and_still_resumes(self):
        u = self.first_sid()
        subprocess.run(["tmux", "-L", self.sock, "kill-server"], capture_output=True)
        time.sleep(0.5)
        r = self.summon("res")
        self.assertIn("resumed=%s" % u, r.stdout)
        ends = [x for x in self.history("res") if x["ev"] == "end"]
        self.assertEqual(ends[0]["reason"], "died")

    def test_twenty_parallel_summons_one_process(self):
        rs = parallel([(lambda: self.pro("summon", "res")) for _ in range(20)])
        self.assertTrue(all(r.returncode == 0 for r in rs), [r.stderr for r in rs][:3])
        self.assertEqual(self.sessions_alive(), ["pro-res"])
        self.assertEqual(len([x for x in self.history("res") if x["ev"] == "start"]), 1)
        self.assertEqual(sum("reused=no" in r.stdout for r in rs), 1)

    def test_orphan_start_row_is_reconciled_as_lost(self):
        u = self.first_sid()
        self.ok("stop", "res")
        with open(os.path.join(self.home, "history", "res.jsonl"), "a") as f:
            f.write(json.dumps({"ev": "start", "id": "orphan-1", "workdir": self.d, "t": time.time()}) + "\n")
        self.summon("res")
        s = json.loads(self.ok("sessions", "res", "--json").stdout)
        self.assertEqual([x["reason"] for x in self.history("res") if x["ev"] == "end"], ["stopped", "lost"])
        self.assertEqual([(x["reason"], x["score"]) for x in s], [("", "neutral"), ("lost", "neutral")])  # u resumed (open), orphan lost

    def test_task_counts_and_scoring(self):
        self.first_sid()
        self.ok("ask", "res", "ECHO fine", "--wait", "30")
        self.ok("stop", "res")
        s = json.loads(self.ok("sessions", "res", "--json").stdout)[0]
        self.assertEqual((s["answered"], s["score"]), (1, "good"))
        self.assertIn("good", self.ok("sessions", "res").stdout)

    def test_marketplace_professional_resumes_only_in_same_project(self):
        r = self.summon("alpha")
        self.assertIn("fresh: no earlier session", r.stdout)
        self.ok("stop", "alpha")
        self.assertIn("resumed=", self.summon("alpha").stdout)
        self.ok("stop", "alpha")
        other = os.path.join(self.tmp, "otherproj")
        os.makedirs(other)
        self.assertIn("fresh: no earlier session", self.summon("alpha", cwd=other).stdout)

    def test_rating_needs_history_and_user(self):
        r = self.pro("rate", "res", "good")
        self.assertEqual(r.returncode, 1)
        self.first_sid()
        self.assertEqual(self.pro("rate", "res", "good", as_pro="alpha").returncode, 4)


class HistoryUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prohist-")
        self.m = load_pros(self.tmp)

    def test_rotation_keeps_newest_rows(self):
        for i in range(1010):
            self.m.hist_append("x", {"ev": "start", "id": "s%d" % i, "workdir": "/w"})
        rows = self.m.hist_rows("x")
        self.assertLessEqual(len(rows), 1000)
        self.assertEqual(rows[-1]["id"], "s1009")

    def test_alias_makes_hook_id_the_truth(self):
        self.m.hist_append("y", {"ev": "start", "id": "A", "workdir": "/w"})
        self.m.hist_append("y", {"ev": "alias", "id": "A", "actual": "B"})
        self.m.hist_append("y", {"ev": "end", "id": "A", "reason": "stopped"})
        s = self.m.hist_sessions("y")[0]
        self.assertEqual(s["actual"], "B")

    def test_score_table(self):
        sc = self.m.score_session
        base = {"rating": "", "reason": "stopped", "answered": 0, "failed": 0, "timeout": 0}
        self.assertEqual(sc(base)[0], "neutral")
        self.assertEqual(sc(dict(base, answered=3, failed=1))[0], "good")
        self.assertEqual(sc(dict(base, answered=2, failed=2))[0], "neutral")  # equal: not more failed than answered, but too many to be good
        self.assertEqual(sc(dict(base, answered=1, failed=2))[0], "bad")
        self.assertEqual(sc(dict(base, reason="stuck"))[0], "bad")
        self.assertEqual(sc(dict(base, reason="failed-to-start"))[0], "bad")
        self.assertEqual(sc(dict(base, rating="good", reason="stuck"))[0], "good")
        self.assertEqual(sc(dict(base, rating="bad", answered=9))[0], "bad")


if __name__ == "__main__":
    unittest.main()
