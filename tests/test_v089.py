"""0.8.9: a denial note never suggests an interpreter wildcard.  Seen live: a python3 -c one-liner that only read a file
got 'pros unblock NAME --allow Bash(python3 *)', which would allow any code at all.  A command that only reads a file
now gets 'use Read'; inline code with no script gets no rule (put it in a script); other rules stay as narrow as before."""
import os
import unittest

from test_v082 import Base

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIDE = ("Bash(python3 *)", "Bash(bash *)", "--allow 'Bash'")


class NarrowNotes(Base):
    def setUp(self):
        super().setUp()
        self.config({"default_mode": "dontAsk", "auto_unblock": False})
        self.summon("alpha")

    def note_for(self, cmd, uid):
        self.deny_call("alpha", "Bash", {"command": cmd}, uid)
        self.ok("pump")
        b = [m["body"] for m in self.msgs() if m["to"] == "user" and m["kind"] == "note" and "was denied" in m["body"]]
        self.assertTrue(b, "a denial note")
        return b[-1]

    def assertNoWide(self, body):
        for w in WIDE:
            self.assertNotIn(w, body)

    def test_python_one_liner_that_only_reads_says_use_read(self):
        b = self.note_for("python3 -c \"import json; print(open('/work/notes/run.jsonl').read()[:400])\"", "toolu_r1")
        self.assertNoWide(b)
        self.assertIn("use Read on /work/notes/run.jsonl", b)
        self.assertNotIn("unblock alpha --allow", b)

    def test_cat_of_a_file_says_use_read(self):
        b = self.note_for("cat /work/notes/run.jsonl", "toolu_r2")
        self.assertIn("use Read on /work/notes/run.jsonl", b)
        self.assertNotIn("--allow", b)

    def test_inline_code_that_writes_gets_no_rule(self):
        b = self.note_for("python3 -c \"import os; os.remove('/work/notes/x')\"", "toolu_r3")
        self.assertNoWide(b)
        self.assertNotIn("unblock alpha --allow", b)
        self.assertIn("no narrow rule fits inline code", b)
        self.assertIn("script", b)

    def test_bare_interpreter_and_unparsed_command_get_no_wildcard(self):
        self.assertNoWide(self.note_for("bash", "toolu_r4"))
        self.assertNoWide(self.note_for("$(cat x)", "toolu_r5"))

    def test_script_rule_stays_narrow(self):
        b = self.note_for("python3 tools/check.py --deck 3", "toolu_r6")
        self.assertIn("--allow 'Bash(python3 tools/check.py *)'", b)
        self.assertIn("--allow 'Bash(wc *)'", self.note_for("wc -l notes.txt", "toolu_r7"))

    def test_unblock_without_a_rule_refuses_the_wildcard(self):
        self.note_for("python3 -c \"print(open('/work/notes/run.jsonl').read())\"", "toolu_r8")
        r = self.pro("unblock", "alpha")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("use Read", r.stdout + r.stderr)
        self.assertEqual(self.entry("alpha").get("allow", []), [])


class Documented(unittest.TestCase):
    def test_docs(self):
        with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
            self.assertIn("## 0.8.9", f.read())
        with open(os.path.join(ROOT, "docs", "spec", "spec.md")) as f:
            self.assertIn("never suggests an interpreter wildcard", f.read())


if __name__ == "__main__":
    unittest.main()
