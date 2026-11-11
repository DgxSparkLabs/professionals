import threading
import time
import unittest
from helpers import *


class Chain(ProTestCase):
    def setUp(self):
        super().setUp()
        for n in ("alpha", "beta", "gamma", "delta"):
            self.summon(n)
        r = self.ok("send", "alpha", "SLEEP 1\nNOREPLY")
        self.task = self.task_of(r.stdout)

    def test_chain_header_grows_and_loop_refused(self):
        r = self.ok("send", "beta", "NOREPLY", "--task", self.task, as_pro="alpha")
        self.assertIn("chain=alpha>beta", r.stdout)
        r = self.ok("send", "gamma", "NOREPLY", "--task", self.task, as_pro="beta")
        self.assertIn("chain=alpha>beta>gamma", r.stdout)
        r = self.pro("send", "alpha", "back", "--task", self.task, as_pro="gamma")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("loop", r.stderr)
        self.assertIn("alpha>beta>gamma>alpha", r.stderr)

    def test_depth_cap(self):
        self.config({"max_depth": 3})
        self.ok("send", "beta", "NOREPLY", "--task", self.task, as_pro="alpha")
        self.ok("send", "gamma", "NOREPLY", "--task", self.task, as_pro="beta")
        r = self.pro("send", "delta", "x", "--task", self.task, as_pro="gamma")
        self.assertEqual(r.returncode, 4)
        self.assertIn("too deep", r.stderr)

    def test_delegation_without_task_inherits_chain(self):
        self.wait_until(lambda: any(m["state"] == "injected" for m in self.msgs()), 10, "alpha busy with the task")
        r = self.ok("send", "beta", "NOREPLY", as_pro="alpha")
        self.assertIn("chain=alpha>beta", r.stdout)

    def test_wait_for_cycle_refused(self):
        res = {}
        t = threading.Thread(target=lambda: res.update(r=self.pro("ask", "beta", "SLEEP 6\nNOREPLY", "--wait", "8", as_pro="alpha")))
        t.start()
        time.sleep(2)
        r = self.pro("send", "alpha", "hi", "--task", "t-fresh", as_pro="beta")  # alpha is blocked on beta
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("wait-for cycle", r.stderr)
        t.join()
        self.assertEqual(res["r"].returncode, 3)
        self.assertEqual(self.pro("send", "alpha", "hi", "--task", "t-fresh", as_pro="beta").returncode, 0)  # wait file removed afterwards

    def test_replies_never_checked_for_loops_and_are_terminal(self):
        r = self.ok("ask", "beta", "ECHO fine", "--wait", "20", as_pro="alpha")
        self.assertIn("kind=reply", r.stdout)
        self.assertEqual(len([m for m in self.msgs() if m["from"] == "beta" and m["kind"] == "reply"]), 1)


if __name__ == "__main__":
    unittest.main()
