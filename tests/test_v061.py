"""0.6.1: automatic unblock. A denial that no deny rule caused is lifted by pros itself; protected paths stay denied."""
import json
import os
import unittest

from helpers import ProTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AutoUnblock(ProTestCase):
    def session(self, name):
        return [s for s in self.sessions_alive() if s.startswith("pro-" + name)][0]

    def deny_call(self, name, tool, inp, uid):
        s = self.session(name)
        with open(os.path.join(self.home, "run", "state", s + ".json")) as f:
            tp = json.load(f)["transcript_path"]
        lines = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": uid, "name": tool, "input": inp}]}},
                 {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": uid, "is_error": True,
                                                           "content": "Permission to use %s has been denied." % tool}]}}]
        with open(tp, "a") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))
        os.utime(tp)
        return s

    def entry(self, name):
        with open(os.path.join(self.home, "registry.json")) as f:
            return json.load(f)["entries"][name]

    def notes(self, to="user"):
        return [m for m in self.msgs() if m["to"] == to and m["kind"] == "note"]

    def spool(self):
        d = os.path.join(self.home, "audit", "spool")
        out = []
        for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            with open(os.path.join(d, f)) as fh:
                out += [json.loads(x) for x in fh.read().splitlines() if x.strip()]
        return out

    def test_denial_is_unblocked_and_restarted_without_anyone_acting(self):
        self.config({"default_mode": "dontAsk"})
        self.summon("alpha")
        s = self.deny_call("alpha", "Bash", {"command": "wc -l notes.txt"}, "toolu_x1")
        self.ok("pump")
        self.wait_until(lambda: "Bash(wc *)" in self.entry("alpha").get("allow", []), 20, "rule added")
        self.wait_until(lambda: len(self.argv_all(s)) == 2 and s in self.sessions_alive(), 30, "restarted")
        run = self.argv_all(s)[-1]
        self.assertIn("--resume", run["argv"])  # with its history
        self.assertIn("Bash(wc *)", json.loads(run["--settings"])["permissions"]["allow"])
        self.wait_until(lambda: any("Retry" in m["body"] for m in self.notes("alpha")), 15, "retry note")
        n = self.notes()
        self.assertEqual(len(n), 1, [m["body"] for m in n])  # one line, and no manual-fix note
        b = n[0]["body"]
        for part in ("alpha", "wc -l notes.txt", "auto-unblocked", "Bash(wc *)", "--auto-unblock off"):
            self.assertIn(part, b)
        self.assertEqual(len(b.splitlines()), 1)
        ev = [e for e in self.spool() if e["action"] == "registry_change" and e["data"]["change"] == "unblocked"]
        self.assertEqual([e["actor"] for e in ev], ["pump"])
        self.ok("pump")  # the resumed transcript still holds the old denial: no second unblock, no note
        self.assertEqual(len(self.argv_all(s)), 2)
        self.assertEqual(len(self.notes()), 1)

    def test_protected_denial_stays_blocked_and_is_reported(self):
        self.config({"default_mode": "dontAsk"})
        self.summon("alpha")
        s = self.deny_call("alpha", "Read", {"file_path": os.path.join(self.fakehome, ".ssh", "id_rsa")}, "toolu_p1")
        self.ok("pump")
        b = [m for m in self.notes() if "denied" in m["body"]][0]["body"]
        self.assertIn("cannot be lifted", b)
        self.assertNotIn("auto-unblocked", b)
        self.assertEqual(self.entry("alpha").get("allow", []), [])
        self.assertEqual(len(self.argv_all(s)), 1)

    def test_off_per_professional_and_in_config(self):
        self.config({"default_mode": "dontAsk"})
        self.ok("enable", "alpha", "--auto-unblock", "off")
        self.assertIs(self.entry("alpha")["auto_unblock"], False)
        self.assertIn("auto_unblock: off", self.ok("show", "alpha").stdout)
        self.summon("alpha")
        s = self.deny_call("alpha", "Bash", {"command": "wc -l a.txt"}, "toolu_o1")
        self.ok("pump")
        b = [m for m in self.notes() if "denied" in m["body"]][0]["body"]
        self.assertIn("unblock alpha --allow 'Bash(wc *)'", b)
        self.assertEqual(len(self.argv_all(s)), 1)
        self.config({"auto_unblock": False})
        d = self.make_folder("fo")
        self.ok("add", d)
        self.assertIn("auto_unblock: off", self.ok("show", "fo").stdout)
        self.ok("add", d, "--auto-unblock", "on")  # the user turns it back on for one professional
        self.assertIs(self.entry("fo")["auto_unblock"], True)
        self.assertNotEqual(self.pro("enable", "alpha", "--auto-unblock", "on", as_pro="beta").returncode, 0)

    def test_already_allowed_rule_is_not_a_restart_loop(self):
        self.config({"default_mode": "dontAsk"})
        self.ok("enable", "alpha")
        self.ok("unblock", "alpha", "--allow", "Bash(wc *)")
        self.summon("alpha")
        s = self.deny_call("alpha", "Bash", {"command": "wc -l b.txt"}, "toolu_l1")  # denied by something else
        self.ok("pump")
        b = [m for m in self.notes() if "denied" in m["body"]][0]["body"]
        self.assertNotIn("auto-unblocked", b)
        self.assertEqual(len(self.argv_all(s)), 1)

    def test_auto_unblock_command_does_nothing_without_a_pending_denial(self):
        self.summon("alpha")
        self.ok("auto-unblock", self.session("alpha"), as_pro="alpha")
        self.assertEqual(self.entry("alpha").get("allow", []), [])

    def test_documented(self):
        for f in (("docs", "spec", "spec.md"), ("README.md",), ("plugins", "professionals", "skills", "professionals", "SKILL.md"), ("CHANGELOG.md",)):
            with open(os.path.join(ROOT, *f), encoding="utf-8") as fh:
                self.assertIn("auto_unblock", fh.read(), f)


if __name__ == "__main__":
    unittest.main()
