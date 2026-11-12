import json
import os
import subprocess
import time
import unittest
from helpers import *


class Summon(ProTestCase):
    def test_new_reuse_and_files(self):
        r = self.summon("alpha")
        self.assertIn("reused=no", r.stdout)
        self.assertIn("ignored-tools=Bash(rm -rf /)", r.stdout)
        gi = os.path.join(self.proj, ".professionals", ".gitignore")
        self.assertEqual(open(gi).read().strip(), "*")  # users must never commit .professionals/
        self.assertTrue(os.path.exists(os.path.join(self.proj, ".professionals", "alpha", ".pro-owned")))
        t = time.time()
        r2 = self.summon("alpha")
        self.assertIn("reused=yes", r2.stdout)
        self.assertLess(time.time() - t, 5)
        self.assertEqual(len(self.sessions_alive()), 1)
        info = json.load(open(os.path.join(self.fakedir, self.sessions_alive()[0] + ".argv.json")))
        self.assertIn("--plugin-dir", info["argv"])  # core plugin not installed in the fake HOME
        s = json.loads(info["--settings"])["permissions"]
        self.assertEqual(s["defaultMode"], "dontAsk")
        self.assertIn("Read", s["allow"])
        self.assertIn("Bash(git status)", s["allow"])
        self.assertNotIn("Bash(rm -rf /)", s["allow"])
        self.assertTrue(any(x.startswith("Read(~/.ssh") for x in s["deny"]))
        self.assertFalse(any(x.startswith("Write(") for x in s["deny"]))
        self.assertIn("professional", info["--append-system-prompt-file"])

    def test_gitignore_not_overwritten_but_created_for_existing_dir(self):
        os.makedirs(os.path.join(self.proj, ".professionals", "alpha"))
        self.summon("alpha")
        self.assertEqual(open(os.path.join(self.proj, ".professionals", ".gitignore")).read().strip(), "*")

    def test_explicit_mode_keeps_persona_tools(self):
        self.summon("alpha", "--mode", "acceptEdits")
        info = json.load(open(os.path.join(self.fakedir, self.sessions_alive()[0] + ".argv.json")))
        self.assertIn("Bash(rm -rf /)", json.loads(info["--settings"])["permissions"]["allow"])
        self.assertIn("acceptEdits", info["argv"])

    def test_lock_race_one_session(self):
        rs = parallel([lambda: self.pro("summon", "alpha"), lambda: self.pro("summon", "alpha")])
        self.assertEqual([r.returncode for r in rs], [0, 0], [r.stderr for r in rs])
        self.assertEqual(sorted(("reused=yes" in r.stdout) for r in rs), [False, True])
        self.assertEqual(len(self.sessions_alive()), 1)
        self.assertEqual(self.fake_log(self.sessions_alive()[0]).count("START"), 1)

    def test_untrusted_marketplace_refused(self):
        self.config({"trusted_marketplaces": []})
        r = self.pro("summon", "alpha")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("not in your professionals list", r.stderr)  # an untrusted marketplace is never seeded
        self.assertIn("not one you added", self.pro("enable", "alpha").stderr)
        self.assertEqual(self.sessions_alive(), [])

    def test_bad_names(self):
        for bad in ("../x", "A", "a b", "x" * 41, "user"):
            self.assertEqual(self.pro("summon", bad).returncode, 2, bad)

    def test_global_vs_project_scope(self):
        self.summon("alpha", "--scope", "global")
        self.summon("alpha")  # project scope: a second, separate session
        names = sorted(self.sessions_alive())
        self.assertEqual(len(names), 2)
        self.assertIn("pro-alpha", names)
        self.assertTrue(os.path.isdir(os.path.join(self.home, "home", "alpha")))
        self.assertTrue(os.path.isdir(os.path.join(self.proj, ".professionals", "alpha")))
        t = json.loads(self.ok("team", "--json").stdout)
        self.assertEqual(sorted(x["scope"] for x in t), ["global", "project"])

    def test_trust_dialog_owned_dir_answered(self):
        r = self.summon("alpha", env={"FAKE_TRUST": "1"})
        self.assertIn("reused=no", r.stdout)

    def test_trust_dialog_foreign_dir_not_answered(self):
        os.makedirs(os.path.join(self.proj, ".professionals", "alpha"))  # not created by pro: no marker
        r = self.pro("summon", "alpha", env={"FAKE_TRUST": "1"})
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("trust", r.stderr)
        self.assertEqual(self.sessions_alive(), [])

    def test_startup_failure_and_usage_limit(self):
        t = time.time()
        r = self.pro("summon", "alpha", env={"FAKE_FAIL": "1"})
        self.assertEqual(r.returncode, 5)
        self.assertIn("exited", r.stderr)
        self.assertLess(time.time() - t, 15)
        r = self.pro("summon", "beta", env={"FAKE_LIMIT": "1"})
        self.assertEqual(r.returncode, 5)
        self.assertIn("limit", r.stderr)

    def test_tmux_env_does_not_leak_pro_self(self):
        # a professional (PRO_SELF=alpha) starts the tmux server and then summons beta
        self.summon("alpha", env={"PRO_SELF": "intruder", "PRO_CHAIN": "x>y", "PRO_SESSION": "nope"})
        self.summon("beta", as_pro="alpha")
        sess = {s.split("--")[0]: s for s in self.sessions_alive()}
        for n in ("alpha", "beta"):
            info = json.load(open(os.path.join(self.fakedir, sess["pro-" + n] + ".argv.json")))
            self.assertEqual(info["env"]["PRO_SELF"], n)
            self.assertEqual(info["env"]["PRO_SESSION"], sess["pro-" + n])
            self.assertNotIn("PRO_CHAIN", info["env"])  # nothing reads it, so it is not set
        self.assertEqual(json.loads(self.ok("team", "--json").stdout)[1]["summoner"] in ("alpha", "user"), True)

    def test_may_summon_false_cannot_summon(self):
        self.summon("gamma")
        r = self.pro("summon", "alpha", as_pro="gamma")
        self.assertEqual(r.returncode, 4, r.stderr)

    def test_hooks_noop_without_pro_self(self):
        r = self.pro("hook", "stop", input="{}")
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertFalse(os.path.exists(os.path.join(self.home, "run", "state")))


if __name__ == "__main__":
    unittest.main()
