import json
import os
import unittest
from helpers import *


class AskAutoSummon(ProTestCase):
    def test_ask_without_summon_starts_allowlisted_pro(self):
        self.ok("enable", "alpha")
        self.assertEqual(self.sessions_alive(), [])
        r = self.ok("ask", "alpha", "ECHO hello", "--wait", "40")
        self.assertIn("hello", r.stdout)
        self.assertIn("summoning", r.stderr)
        self.assertTrue(self.sessions_alive())

    def test_ask_never_summons_outside_allowlist(self):
        self.ok("disable", "beta")
        r = self.pro("ask", "beta", "ECHO hi", "--wait", "5")
        self.assertEqual(r.returncode, 4)
        r = self.pro("ask", "ghost", "ECHO hi", "--wait", "5")
        self.assertEqual(r.returncode, 4)
        self.assertEqual(self.sessions_alive(), [])

    def test_repeated_task_id_returns_stored_answer(self):
        self.ok("enable", "alpha")
        r1 = self.ok("ask", "alpha", "ECHO once", "--task", "t-fixed1", "--wait", "40")
        self.assertIn("once", r1.stdout)
        s = self.sessions_alive()[0]
        n = len(self.acts(s))
        r2 = self.ok("ask", "alpha", "ECHO once", "--task", "t-fixed1", "--wait", "20")
        self.assertIn("once", r2.stdout)
        self.assertEqual(len(self.acts(s)), n)
        self.assertEqual(len([m for m in self.msgs() if m["kind"] == "task" and m["task"] == "t-fixed1"]), 1)


class ImportsDialog(ProTestCase):
    def test_imports_dialog_answered_for_registered_folder(self):
        d = self.make_folder("imp", claude="# imp\n@~/outside/notes.md\n")
        self.ok("add", d)
        r = self.summon("imp", env={"FAKE_IMPORTS": "1"})
        self.assertIn("reused=no", r.stdout)

    def test_imports_dialog_both_dialogs(self):
        d = self.make_folder("both")
        self.ok("add", d)
        self.summon("both", env={"FAKE_IMPORTS": "1", "FAKE_TRUST": "1"})

    def test_imports_dialog_not_answered_when_changed_or_code(self):
        d = self.make_folder("hk2", settings={"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}})
        self.ok("add", d)
        r = self.pro("summon", "hk2", env={"FAKE_IMPORTS": "1"})
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("imports", r.stderr)
        self.assertEqual(self.sessions_alive(), [])

    def test_imports_dialog_never_answered_for_persona(self):
        self.ok("enable", "alpha")
        r = self.pro("summon", "alpha", env={"FAKE_IMPORTS": "1"})
        self.assertEqual(r.returncode, 5, r.stderr)


if __name__ == "__main__":
    unittest.main()
