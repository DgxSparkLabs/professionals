import fcntl
import os
import time
import unittest
from helpers import *


class Caps(ProTestCase):
    def test_live_cap(self):
        self.config({"max_live": 2})
        self.summon("alpha")
        self.summon("beta")
        r = self.pro("summon", "gamma")
        self.assertEqual(r.returncode, 4)
        self.assertIn("live cap", r.stderr)
        self.assertEqual(len(self.sessions_alive()), 2)

    def test_summon_budget(self):
        self.config({"summon_budget_per_hour": 1})
        self.summon("alpha")
        self.ok("stop", "alpha")
        r = self.pro("summon", "alpha")
        self.assertEqual(r.returncode, 4)
        self.assertIn("budget", r.stderr)

    def test_task_message_cap(self):
        self.config({"task_msg_cap": 2})
        self.summon("alpha")
        self.ok("send", "alpha", "NOREPLY", "--task", "t-cap")
        self.ok("send", "alpha", "NOREPLY", "--task", "t-cap")
        r = self.pro("send", "alpha", "NOREPLY", "--task", "t-cap")
        self.assertEqual(r.returncode, 4)
        self.assertIn("cap", r.stderr)

    def test_stop_rules_and_stop_all(self):
        self.summon("alpha")
        self.summon("beta")
        r = self.pro("stop", "beta", as_pro="alpha")  # beta was summoned by the user
        self.assertEqual(r.returncode, 4)
        self.assertEqual(len(self.sessions_alive()), 2)
        self.summon("gamma", as_pro="alpha")
        self.ok("stop", "gamma", as_pro="alpha")  # its summoner may
        self.assertEqual(self.ok("stop", "--all").stdout.strip().startswith("stopped:"), True)
        self.assertEqual(self.sessions_alive(), [])
        self.assertEqual(self.ok("team").stdout.strip(), "no live professionals")

    def test_stop_fails_queued_messages(self):
        self.summon("alpha")
        self.ok("send", "alpha", "SLEEP 5\nNOREPLY")
        self.wait_until(lambda: any(m["state"] == "injected" for m in self.msgs()), 10, "busy")
        r = self.ok("send", "alpha", "ECHO queued")
        self.assertIn("status=queued", r.stdout)
        self.ok("stop", "alpha")
        self.assertIn("failed", [m["state"] for m in self.msgs() if m["body"] == "ECHO queued"])
        self.assertIn("unread", self.ok("inbox").stdout)

    def test_reap_idle_only(self):
        self.config({"idle_reap_min": 0.01})
        self.summon("alpha")
        self.summon("beta")
        self.ok("send", "beta", "SLEEP 8\nNOREPLY")
        self.wait_until(lambda: any(m["state"] == "injected" for m in self.msgs()), 10, "beta busy")
        time.sleep(1.2)
        r = self.ok("reap")
        self.assertIn("alpha", r.stdout)
        self.assertNotIn("beta", r.stdout)
        self.assertEqual(len([s for s in self.sessions_alive()]), 1)

    def test_reap_skips_nonempty_queue(self):
        self.config({"idle_reap_min": 0.01})
        self.summon("alpha")
        s = self.sessions_alive()[0]
        os.makedirs(os.path.join(self.home, "run", "queue", s), exist_ok=True)
        from helpers import json
        mid = "m-0000000001"
        os.makedirs(os.path.join(self.home, "run", "msgs"), exist_ok=True)
        with open(os.path.join(self.home, "run", "msgs", mid + ".json"), "w") as f:
            json.dump({"id": mid, "state": "queued", "kind": "task", "to": "alpha", "from": "user", "task": "t", "created": time.time()}, f)
        with open(os.path.join(self.home, "run", "queue", s, "0000000000001-" + mid), "w") as f:
            f.write(mid)
        time.sleep(1.2)
        self.assertIn("nothing", self.ok("reap").stdout)
        self.assertEqual(len(self.sessions_alive()), 1)

    def test_reap_skips_while_summon_lock_held(self):
        self.config({"idle_reap_min": 0.01})
        self.summon("alpha")
        s = self.sessions_alive()[0]
        time.sleep(1.2)
        f = open(os.path.join(self.home, "run", "locks", s + ".lock"), "a+")
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            self.assertIn("nothing", self.ok("reap").stdout)
            self.assertEqual(len(self.sessions_alive()), 1)
        finally:
            f.close()
        self.assertIn("alpha", self.ok("reap").stdout)


if __name__ == "__main__":
    unittest.main()
