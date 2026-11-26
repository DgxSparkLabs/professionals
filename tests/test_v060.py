"""0.6.0: bypassPermissions by default, deny rules kept, no first-run dialog, denial alerts and `pros unblock`."""
import json
import os
import unittest
from unittest import mock

from helpers import ProTestCase, load_pros, PRO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BypassDefault(ProTestCase):
    def info(self, s):
        info = self.argv_info(s)
        return info, json.loads(info["--settings"])

    def session(self, name):
        return [s for s in self.sessions_alive() if s.startswith("pro-" + name)][0]

    def test_marketplace_summon_is_bypass_with_deny_rules_and_no_dialog(self):
        r = self.summon("alpha")
        self.assertNotIn("ignored-tools", r.stdout)  # no safe-list narrowing in bypass
        info, st = self.info(self.session("alpha"))
        a = info["argv"]
        self.assertEqual(a[a.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(st["permissions"]["defaultMode"], "bypassPermissions")
        self.assertIs(st["skipDangerousModePermissionPrompt"], True)
        deny = st["permissions"]["deny"]
        for p in ("~/.ssh", "~/.aws", "~/.gnupg", "~/.netrc", "~/.claude/.credentials.json"):
            self.assertIn("Read(%s)" % p, deny)
            self.assertIn("Edit(%s/**)" % p, deny)
        self.assertTrue(any("config.json" in d for d in deny))  # pros state stays denied
        self.assertIn("Bash(rm -rf /)", st["permissions"]["allow"])  # persona tools as written
        self.assertIn("bypassPermissions", info["--append-system-prompt-file"])

    def test_config_deny_paths_stay_in_bypass(self):
        self.config({"deny_paths": ["/srv/secret-store"]})
        self.summon("alpha")
        deny = self.info(self.session("alpha"))[1]["permissions"]["deny"]
        self.assertIn("Read(//srv/secret-store)", deny)
        self.assertIn("Edit(//srv/secret-store/**)", deny)

    def test_folder_default_is_bypass_and_keeps_folder_denies(self):
        d = self.make_folder("fb")
        self.ok("add", d, "--deny", "keys")
        self.summon("fb")
        info, st = self.info("pro-fb")
        self.assertEqual(st["permissions"]["defaultMode"], "bypassPermissions")
        self.assertIs(st["skipDangerousModePermissionPrompt"], True)
        deny = st["permissions"]["deny"]
        self.assertTrue(any(x.startswith("Read(//%s/.env" % d.lstrip("/")) for x in deny))
        self.assertTrue(any("keys" in x and x.startswith("Edit(") for x in deny))
        self.assertTrue(any(x.startswith("Read(~/.ssh") for x in deny))

    def test_config_default_mode_restores_strict_mode_and_narrowing(self):
        self.config({"default_mode": "dontAsk"})
        r = self.summon("alpha")
        self.assertIn("ignored-tools=Bash(rm -rf /)", r.stdout)
        st = self.info(self.session("alpha"))[1]
        self.assertEqual(st["permissions"]["defaultMode"], "dontAsk")
        self.assertNotIn("skipDangerousModePermissionPrompt", st)
        self.assertNotIn("Bash(rm -rf /)", st["permissions"]["allow"])

    def test_per_professional_mode_via_enable(self):
        self.ok("enable", "beta", "--mode", "dontAsk")
        self.summon("beta")
        self.summon("alpha")
        self.assertEqual(self.info(self.session("beta"))[1]["permissions"]["defaultMode"], "dontAsk")
        self.assertEqual(self.info(self.session("alpha"))[1]["permissions"]["defaultMode"], "bypassPermissions")
        self.assertIn("mode: dontAsk", self.ok("show", "beta").stdout)

    def test_root_falls_back_only_when_mode_not_chosen(self):
        mod = load_pros(self.home)
        c = dict(mod.DEFAULTS)
        with mock.patch.object(mod.os, "geteuid", return_value=0):
            self.assertEqual(mod.effective_mode(None, c), "dontAsk")
            self.assertEqual(mod.effective_mode("bypassPermissions", c), "bypassPermissions")
        with mock.patch.object(mod.os, "geteuid", return_value=1000):
            self.assertEqual(mod.effective_mode(None, c), "bypassPermissions")


class BlockedModels(ProTestCase):
    FABLE = "claude-fable-5-2"

    def entry(self, name):
        with open(os.path.join(self.home, "registry.json")) as f:
            return json.load(f)["entries"][name]

    def test_blocked_model_refused_from_summon_flag_and_config(self):
        r = self.pro("summon", "alpha", "--model", self.FABLE)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("blocked", r.stderr)
        self.assertEqual(self.sessions_alive(), [])
        self.config({"model": self.FABLE})  # a folder has no persona model, so config model applies
        self.ok("add", self.make_folder("cfgm"))
        self.assertEqual(self.pro("summon", "cfgm").returncode, 4)

    def test_add_model_is_stored_and_is_the_one_exception(self):
        d = self.make_folder("architect")
        self.ok("add", d, "--mode", "dontAsk", "--model", self.FABLE)
        e = self.entry("architect")
        self.assertEqual((e["model"], e["mode"]), (self.FABLE, "dontAsk"))
        self.summon("architect")
        a = self.argv_info("pro-architect")["argv"]
        self.assertEqual(a[a.index("--model") + 1], self.FABLE)
        self.assertEqual(a[a.index("--permission-mode") + 1], "dontAsk")
        r = self.pro("summon", "alpha", "--model", self.FABLE)  # the exception is per professional
        self.assertEqual(r.returncode, 4, r.stderr)

    def test_add_model_updates_a_registered_folder_and_enable_model(self):
        d = self.make_folder("later")
        self.ok("add", d)
        self.ok("add", d, "--model", "sonnet")
        self.assertEqual(self.entry("later")["model"], "sonnet")
        self.ok("enable", "beta", "--model", self.FABLE)
        self.summon("beta")
        s = [x for x in self.sessions_alive() if x.startswith("pro-beta")][0]
        a = self.argv_info(s)["argv"]
        self.assertEqual(a[a.index("--model") + 1], self.FABLE)


class AddRefresh(ProTestCase):
    def entry(self, name):
        return json.load(open(os.path.join(self.home, "registry.json")))["entries"][name]

    def test_refresh_updates_allow_list(self):
        d = self.make_folder("rf")
        self.ok("add", d, "--allow", "Bash(go test*)")
        self.ok("add", d, "--refresh", "--allow", "Bash(make *)")
        self.assertEqual(self.entry("rf")["allow"], ["Bash(go test*)", "Bash(make *)"])
        self.ok("add", d, "--refresh", "--mode", "acceptEdits")
        self.assertEqual(self.entry("rf")["mode"], "acceptEdits")
        self.config({"default_mode": "dontAsk"})
        self.ok("add", d, "--refresh", "--mode", "")
        self.summon("rf")
        st = json.loads(self.argv_info("pro-rf")["--settings"])["permissions"]
        self.assertIn("Bash(make *)", st["allow"])

    def test_allow_without_refresh_does_not_accept_a_changed_folder(self):
        d = self.make_folder("rc")
        self.ok("add", d)
        with open(os.path.join(d, "CLAUDE.md"), "a") as f:
            f.write("new instruction\n")
        self.ok("add", d, "--allow", "Bash(make *)")
        self.assertIn("Bash(make *)", self.entry("rc")["allow"])
        r = self.pro("summon", "rc")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("changed", r.stderr)

    def test_add_refuses_protected_allow_rule(self):
        d = self.make_folder("rp")
        r = self.pro("add", d, "--allow", "Read(~/.ssh/**)")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("protected", r.stderr)


class Unblock(ProTestCase):
    def session(self, name):
        return [s for s in self.sessions_alive() if s.startswith("pro-" + name)][0]

    def deny_call(self, name, tool, inp, uid):
        s = self.session(name)
        tp = json.load(open(os.path.join(self.home, "run", "state", s + ".json")))["transcript_path"]
        lines = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": uid, "name": tool, "input": inp}]}},
                 {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": uid, "is_error": True,
                                                           "content": "Permission to use %s has been denied." % tool}]}}]
        with open(tp, "a") as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))
        os.utime(tp)
        return s

    def notes(self):
        return [m for m in self.msgs() if m["to"] == "user" and m["kind"] == "note"]

    def test_denial_sends_one_note_with_the_fix(self):
        self.config({"default_mode": "dontAsk", "auto_unblock": False})  # 0.6.1: the manual path
        self.summon("alpha")
        self.deny_call("alpha", "Bash", {"command": "wc -l notes.txt"}, "toolu_a1")
        self.ok("pump")
        self.ok("pump")
        n = [m for m in self.notes() if "denied" in m["body"]]
        self.assertEqual(len(n), 1, n)
        b = n[0]["body"]
        for part in ("alpha", "Bash", "wc -l notes.txt", "dontAsk", "unblock alpha --allow 'Bash(wc *)'"):
            self.assertIn(part, b)
        den = json.loads(self.ok("team", "--json").stdout)[0]["denied"]
        self.assertEqual(den, {"Bash": 1})
        self.assertIn("pros unblock", self.ok("team").stdout)
        self.assertIn("DENIED", self.ok("team", "--help").stdout.upper())

    def test_secret_denial_note_says_it_cannot_be_lifted(self):
        self.summon("alpha")
        self.deny_call("alpha", "Read", {"file_path": os.path.join(self.fakehome, ".ssh", "id_rsa")}, "toolu_s1")
        self.ok("pump")
        b = [m for m in self.notes() if "denied" in m["body"]][0]["body"]
        self.assertIn("cannot be lifted", b)
        self.assertNotIn("--allow", b)
        r = self.pro("unblock", "alpha")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("protected", r.stderr)

    def test_unblock_adds_rule_restarts_with_history_and_says_retry(self):
        self.config({"default_mode": "dontAsk", "auto_unblock": False})  # 0.6.1: the manual path
        self.summon("alpha")
        s = self.deny_call("alpha", "Bash", {"command": "wc -l notes.txt"}, "toolu_u1")
        self.ok("pump")
        r = self.ok("unblock", "alpha")
        self.assertIn("Bash(wc *)", r.stdout)
        reg = json.load(open(os.path.join(self.home, "registry.json")))["entries"]["alpha"]
        self.assertIn("Bash(wc *)", reg["allow"])
        runs = self.argv_all(s)
        self.assertEqual(len(runs), 2)
        self.assertIn("--resume", runs[-1]["argv"])  # history kept (resume or fork)
        st = json.loads(runs[-1]["--settings"])["permissions"]
        self.assertIn("Bash(wc *)", st["allow"])
        self.assertTrue(any(x.startswith("Read(~/.ssh") for x in st["deny"]))
        self.wait_until(lambda: any(m["to"] == "alpha" and "Retry" in m["body"] for m in self.msgs()), 10, "retry note")
        self.ok("pump")  # the resumed transcript still holds the old denial: it is not reported again
        self.assertEqual(len([m for m in self.notes() if "denied" in m["body"]]), 1)

    def test_unblock_refuses_secret_rules_and_professionals(self):
        self.summon("alpha")
        for rule in ("Read(~/.ssh/**)", "Bash(cat ~/.aws/credentials)", "Edit(%s)" % os.path.join(self.home, "registry.json")):
            r = self.pro("unblock", "alpha", "--allow", rule)
            self.assertEqual(r.returncode, 4, (rule, r.stderr))
            self.assertIn("protected", r.stderr)
        r = self.pro("unblock", "alpha", "--allow", "Bash(wc *)", as_pro="beta")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("only the user", r.stderr)
        r = self.pro("unblock", "alpha", "--allow", "not a rule\n")
        self.assertEqual(r.returncode, 2, r.stderr)

    def test_unblock_when_not_running_only_records(self):
        r = self.ok("unblock", "alpha", "--allow", "Bash(make *)")
        self.assertIn("next start", r.stdout)
        self.assertEqual(self.sessions_alive(), [])


class Docs(unittest.TestCase):
    def read(self, *p):
        return open(os.path.join(ROOT, *p), encoding="utf-8").read()

    def test_default_is_documented_everywhere(self):
        for f in (("docs", "spec", "spec.md"), ("README.md",), ("docs", "design.md"),
                  ("plugins", "professionals", "skills", "professionals", "SKILL.md")):
            t = self.read(*f)
            self.assertIn("bypassPermissions", t, f)
            self.assertIn("pros unblock", t, f)
            self.assertNotIn("start in `dontAsk` mode", t, f)
        self.assertIn('"default_mode": "bypassPermissions"', self.read("plugins", "professionals", "bin", "pros"))
        self.assertIn("skipDangerousModePermissionPrompt", self.read("docs", "spec", "spec.md"))


if __name__ == "__main__":
    unittest.main()
