"""0.7.1 (T200): `pros add <registered folder> --name b` registers a second name for the same folder; each name has its own sessions."""
import json
import os
import unittest

from helpers import ProTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SecondName(ProTestCase):
    def entries(self):
        with open(os.path.join(self.home, "registry.json")) as f:
            return json.load(f)["entries"]

    def session(self, name):
        return [s for s in self.sessions_alive() if s.startswith("pro-" + name)][0]

    def sid(self, session):
        argv = self.argv_info(session)["argv"]
        return argv[argv.index("--session-id") + 1]

    def test_second_name_is_a_second_entry(self):
        d = self.make_folder("dual")
        self.ok("add", d, "--name", "a")
        r = self.ok("add", d, "--name", "b")
        self.assertIn("added b", r.stdout)
        e = self.entries()
        self.assertEqual((e["a"]["path"], e["b"]["path"]), (d, d))
        self.assertNotEqual(e["a"]["pro_id"], e["b"]["pro_id"])
        self.assertIn("already registered as b", self.ok("add", d, "--name", "b").stdout)
        self.ok("add", d, "--name", "b", "--mode", "dontAsk")  # flags with an existing name update only that entry
        e = self.entries()
        self.assertEqual((e["a"]["mode"], e["b"]["mode"]), ("", "dontAsk"))
        self.assertEqual(sorted(n for n, x in e.items() if x.get("path") == d), ["a", "b"])
        self.assertIn("already registered as a", self.ok("add", d).stdout)  # no --name: as before, nothing new

    def test_two_names_never_share_a_session(self):
        d = self.make_folder("dual")
        self.ok("add", d, "--name", "a")
        self.ok("add", d, "--name", "b")
        self.summon("a")
        self.summon("b")
        sa, sb = self.session("a"), self.session("b")
        self.assertNotEqual(sa, sb)
        self.assertEqual({sa, sb} & set(self.sessions_alive()), {sa, sb})  # both live at once
        ida, idb = self.sid(sa), self.sid(sb)
        self.assertNotEqual(ida, idb)
        self.ok("stop", "b")
        self.summon("b")  # b resumes its own session, never a's
        run = self.argv_all(self.session("b"))[-1]["argv"]
        self.assertNotIn(ida, run)
        self.assertIn(idb, run)
        self.summon("a")  # one live session per name: a second summon reuses it
        self.assertEqual([s for s in self.sessions_alive() if s.startswith("pro-a")], [sa])

    def test_documented(self):
        with open(os.path.join(ROOT, "docs", "spec", "spec.md"), encoding="utf-8") as f:
            self.assertIn("second name for the same folder", f.read())
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            self.assertIn("## 0.7.1", f.read())


if __name__ == "__main__":
    unittest.main()
