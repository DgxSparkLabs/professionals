import json
import os
import unittest
from helpers import *


class FolderSummon(ProTestCase):
    def test_runs_in_its_own_folder_with_no_persona(self):
        d = self.make_folder("wa", claude="# WhatsApp helper\nCite path:line.\n")
        self.ok("add", d)
        r = self.summon("wa")
        self.assertIn("scope=folder", r.stdout)
        self.assertEqual(self.sessions_alive(), ["pro-wa"])
        info = self.argv_info("pro-wa")
        self.assertEqual(os.path.realpath(info["cwd"]), d)
        self.assertEqual(info["env"]["PRO_SELF"], "wa")
        a = info["argv"]
        self.assertIn("--session-id", a)
        self.assertNotIn("--add-dir", a)
        prompt = info["--append-system-prompt-file"]
        self.assertIn('professional "wa"', prompt)
        self.assertIn("CLAUDE.md of the folder", prompt)
        self.assertNotIn("AGENTS.md of this folder", prompt)
        self.assertFalse(os.path.exists(os.path.join(d, ".pro-owned")))
        self.assertEqual(sorted(os.listdir(d)), ["CLAUDE.md"])  # pros never writes into the folder

    def test_agents_md_appended_only_without_claude_md(self):
        only = self.make_folder("only", claude=None, agents="# Only agents\nUNIQUE-AGENTS-TEXT\n")
        both = self.make_folder("both", claude="@AGENTS.md\n", agents="# Both\nOTHER-AGENTS-TEXT\n")
        self.ok("add", only)
        self.ok("add", both)
        self.summon("only")
        self.summon("both")
        self.assertIn("UNIQUE-AGENTS-TEXT", self.argv_info("pro-only")["--append-system-prompt-file"])
        self.assertNotIn("OTHER-AGENTS-TEXT", self.argv_info("pro-both")["--append-system-prompt-file"])

    def test_baseline_permissions_and_deny_list(self):
        d = self.make_folder("perm")
        self.ok("add", d, "--deny", "data/store")
        self.summon("perm")
        s = json.loads(self.argv_info("pro-perm")["--settings"])["permissions"]
        self.assertEqual(s["defaultMode"], "dontAsk")
        for rule in ("Read", "Edit", "Bash(git status)", "Bash(pros *)"):
            self.assertIn(rule, s["allow"])
        self.assertFalse(any("go test" in x or "pytest" in x for x in s["allow"]))
        self.assertTrue(any(x.startswith("Read(//%s/.env" % d.lstrip("/")) for x in s["deny"]))
        self.assertTrue(any("data/store" in x and x.startswith("Edit(") for x in s["deny"]))
        self.assertTrue(any(x.startswith("Read(~/.ssh") for x in s["deny"]))

    def test_folder_with_own_allow_rules_gets_no_baseline(self):
        d = self.make_folder("own", settings={"permissions": {"allow": ["Bash(make test)"]}})
        self.ok("add", d)
        self.summon("own")
        s = json.loads(self.argv_info("pro-own")["--settings"])["permissions"]
        self.assertNotIn("Edit", s["allow"])
        self.assertNotIn("Bash(git status)", s["allow"])
        self.assertIn("Bash(pros *)", s["allow"])

    def test_entry_allow_and_mode_are_applied(self):
        d = self.make_folder("opts")
        self.ok("add", d, "--allow", "Bash(go test*)", "--mode", "acceptEdits")
        self.summon("opts")
        info = self.argv_info("pro-opts")
        a = info["argv"]
        self.assertEqual(a[a.index("--permission-mode") + 1], "acceptEdits")
        s = json.loads(info["--settings"])["permissions"]
        self.assertIn("Bash(go test*)", s["allow"])

    def test_missing_folder_exit_5(self):
        d = self.make_folder("vanish")
        self.ok("add", d)
        os.rename(d, d + "-x")
        r = self.pro("summon", "vanish", "--json")
        self.assertEqual(r.returncode, 5)
        self.assertEqual(json.loads(r.stdout)["error"], "missing")

    def test_one_live_session_per_folder_and_reuse(self):
        d = self.make_folder("shared")
        self.ok("add", d)
        self.summon("shared")
        r = self.summon("shared", cwd=self.tmp)  # another summoner, other project
        self.assertIn("reused=yes", r.stdout)
        self.assertEqual(self.sessions_alive(), ["pro-shared"])

    def test_message_reaches_folder_professional(self):
        d = self.make_folder("msg")
        self.ok("add", d)
        self.summon("msg")
        r = self.ok("ask", "msg", "ECHO folder-answer", "--wait", "30")
        self.assertIn("folder-answer", r.stdout)

    def test_folder_professional_cannot_change_list(self):
        d = self.make_folder("jail")
        self.ok("add", d)
        r = self.pro("add", self.make_folder("other"), as_pro="jail")
        self.assertEqual(r.returncode, 4)


if __name__ == "__main__":
    unittest.main()
