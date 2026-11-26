"""0.8.2: an unblock restart (automatic or `pros unblock`) keeps the model, thinking and harness the live session was summoned
with; before, it fell back to the entry, persona or config model (seen live: --model claude-sonnet-5-5 came back on opus)."""
import json
import os
import subprocess
import time
import unittest

import test_v070
import test_v080
from helpers import ProTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Base(ProTestCase):
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

    def flag(self, argv, f):
        return argv[argv.index(f) + 1] if f in argv else None

    def rec(self, s):
        with open(os.path.join(self.home, "run", "sessions", s + ".json")) as f:
            return json.load(f)


class RestartKeepsModel(Base):
    def test_auto_unblock_restart_keeps_the_summoned_model(self):
        self.config({"default_mode": "dontAsk", "model": "claude-opus-5-5"})
        self.summon("alpha", "--model", "claude-sonnet-5-5", "--thinking", "low")
        s = self.session("alpha")
        first = self.argv_all(s)[-1]["argv"]
        self.assertEqual((self.flag(first, "--model"), self.flag(first, "--effort")), ("claude-sonnet-5-5", "low"))
        self.deny_call("alpha", "Bash", {"command": "wc -l notes.txt"}, "toolu_m1")
        self.ok("pump")
        self.wait_until(lambda: len(self.argv_all(s)) == 2 and s in self.sessions_alive(), 30, "restarted")
        run = self.argv_all(s)[-1]["argv"]
        self.assertIn("--resume", run)
        self.assertEqual((self.flag(run, "--model"), self.flag(run, "--effort")), ("claude-sonnet-5-5", "low"))
        self.wait_until(lambda: os.path.exists(os.path.join(self.home, "run", "sessions", s + ".json")), 10, "record")
        r = self.rec(s)
        self.assertEqual((r["model"], r["thinking"]), ("claude-sonnet-5-5", "low"))

    def test_manual_unblock_restart_keeps_the_summoned_model(self):
        self.config({"default_mode": "dontAsk", "model": "claude-opus-5-5"})
        self.summon("alpha", "--model", "claude-sonnet-5-5")
        s = self.session("alpha")
        self.ok("enable", "alpha", "--auto-unblock", "off")
        self.deny_call("alpha", "Bash", {"command": "wc -l notes.txt"}, "toolu_m2")
        self.ok("pump")
        self.ok("unblock", "alpha", "--allow", "Bash(wc *)")
        self.wait_until(lambda: len(self.argv_all(s)) == 2 and s in self.sessions_alive(), 30, "restarted")
        self.assertEqual(self.flag(self.argv_all(s)[-1]["argv"], "--model"), "claude-sonnet-5-5")


class ResumeKeepsModel(Base):
    """Twin paths: a session that died or was reaped and is woken (send) or summoned again resumes on the model it was asked for."""
    def start(self):
        self.config({"default_mode": "dontAsk", "model": "claude-opus-5-5"})
        self.summon("alpha", "--model", "claude-sonnet-5-5", "--thinking", "low")
        s = self.session("alpha")
        subprocess.run(["tmux", "-L", self.sock, "kill-session", "-t", "=" + s], capture_output=True)  # it dies (crash, reap)
        self.wait_until(lambda: s not in self.sessions_alive(), 10, "dead")
        return s

    def last(self, s):
        run = self.argv_all(s)[-1]["argv"]
        return "--resume" in run, self.flag(run, "--model"), self.flag(run, "--effort")

    def test_woken_by_send_keeps_the_asked_model(self):
        s = self.start()
        self.ok("send", "alpha", "ECHO hi")
        self.wait_until(lambda: len(self.argv_all(s)) == 2, 30, "woken")
        self.assertEqual(self.last(s), (True, "claude-sonnet-5-5", "low"))

    def test_summon_again_keeps_it_and_an_explicit_model_wins(self):
        s = self.start()
        self.summon("alpha")
        self.assertEqual(self.last(s), (True, "claude-sonnet-5-5", "low"))
        subprocess.run(["tmux", "-L", self.sock, "kill-session", "-t", "=" + s], capture_output=True)
        self.wait_until(lambda: s not in self.sessions_alive(), 10, "dead")
        self.summon("alpha", "--model", "claude-haiku-5-5")
        self.assertEqual(self.last(s)[:2], (True, "claude-haiku-5-5"))

    def test_user_stop_resets_to_the_defaults(self):
        self.config({"default_mode": "dontAsk", "model": "claude-opus-5-5"})
        self.summon("alpha", "--model", "claude-haiku-5-5")
        s = self.session("alpha")
        self.ok("stop", "alpha")
        self.summon("alpha")
        self.assertEqual(self.flag(self.argv_all(s)[-1]["argv"], "--model"), "sonnet")  # the fixture persona's model


class GuardMark(test_v070.PiCase):
    def team(self, name):
        return [r for r in json.loads(self.ok("team", "--json").stdout) if r["name"] == name][0]

    def view(self, name):
        return [r for r in json.loads(self.ok("list", "--json").stdout) if r["name"] == name][0]

    def test_cursor_model_on_pi_is_marked_and_warned(self):
        d = self.make_folder("gp", agents="# gp\nAnswer briefly.\n")
        r = self.ok("add", d, "--harness", "pi", "--model", "cursor/grok-4.7-medium")
        self.assertIn("guard: none", r.stdout + r.stderr)
        self.assertEqual(self.view("gp")["guard"], "none")
        self.summon("gp")
        self.assertEqual(self.team("gp")["guard"], "none")
        self.assertIn("guard: none", self.ok("team").stdout)
        self.assertIn("guard: none", self.ok("list").stdout)

    def test_other_pi_model_and_claude_are_not_warned(self):
        d = self.make_folder("gq", agents="# gq\nAnswer briefly.\n")
        r = self.ok("add", d, "--harness", "pi", "--model", "fake/m1")
        self.assertNotIn("guard:", r.stdout + r.stderr)
        self.assertEqual(self.view("gq")["guard"], "paths")  # Pi's own tools: the protected-path guard only
        r = self.ok("add", self.make_folder("gc"))
        self.assertNotIn("guard:", r.stdout + r.stderr)
        self.assertEqual(self.view("gc")["guard"], "pros")
        self.summon("gc")
        self.assertEqual(self.team("gc")["guard"], "pros")
        self.assertNotIn("guard:", self.ok("team").stdout)

    def test_pi_without_a_model_is_unknown(self):
        d = self.make_folder("gu", agents="# gu\nAnswer briefly.\n")
        r = self.ok("add", d, "--harness", "pi")
        self.assertIn("guard: unknown", r.stdout + r.stderr)
        r = self.ok("enable", "gu", "--model", "cursor/gpt-5")
        self.assertIn("guard: none", r.stdout + r.stderr)


class NarrowAutoRules(Base):
    def notes(self):
        return [m for m in self.msgs() if m["to"] == "user" and m["kind"] == "note"]

    def test_interpreter_rule_names_the_script(self):
        self.config({"default_mode": "dontAsk"})
        self.summon("alpha")
        s = self.deny_call("alpha", "Bash", {"command": "python3 tools/check.py --deck 3"}, "toolu_n1")
        self.ok("pump")
        self.wait_until(lambda: "Bash(python3 tools/check.py *)" in self.entry("alpha").get("allow", []), 20, "narrow rule added")
        self.assertNotIn("Bash(python3 *)", self.entry("alpha")["allow"])
        self.wait_until(lambda: len(self.argv_all(s)) == 2, 30, "restarted")

    def test_too_wide_rules_are_never_added_by_pros(self):
        self.config({"default_mode": "dontAsk"})
        self.summon("alpha")
        for i, cmd in enumerate(("python3 -c 'print(1)'", "$(cat x)", "bash")):
            self.deny_call("alpha", "Bash", {"command": cmd}, "toolu_w%d" % i)
            self.ok("pump")
        time.sleep(2)
        self.assertEqual(self.entry("alpha").get("allow", []), [])  # nothing added: the user decides
        self.assertEqual(len(self.argv_all(self.session("alpha"))), 1)  # and no restart
        b = " ".join(m["body"] for m in self.notes())
        for rule in ("--allow 'Bash(python3 *)'", "--allow 'Bash'", "--allow 'Bash(bash *)'"):
            self.assertNotIn(rule, b)  # 0.8.9: never an interpreter wildcard or bare Bash in the note
        self.assertIn("no narrow rule fits inline code", b)  # the usual note, with the narrow fix


class AllowCeiling(Base):
    """A professional the user registered with explicit --allow rules keeps them as its ceiling: auto-unblock adds nothing."""
    def folder_pro(self, name, *allow):
        d = self.make_folder(name)
        self.ok("add", d, *[x for r in allow for x in ("--allow", r)])
        self.summon(name)
        return d

    def test_explicit_allow_is_a_ceiling(self):
        self.config({"default_mode": "dontAsk"})
        self.folder_pro("cv", "Bash(git *)")
        s = self.deny_call("cv", "Bash", {"command": "python3 /work/tools/slides_draw.py --help"}, "toolu_c1")
        self.ok("pump")
        time.sleep(2)
        self.assertEqual(self.entry("cv")["allow"], ["Bash(git *)"])
        self.assertEqual(len(self.argv_all(s)), 1)
        b = " ".join(m["body"] for m in self.msgs() if m["to"] == "user" and m["kind"] == "note")
        self.assertIn("--allow 'Bash(python3 /work/tools/slides_draw.py *)'", b)
        self.assertIn("ceiling", b)
        self.ok("unblock", "cv", "--allow", "Bash(python3 /work/tools/slides_draw.py *)")  # he widens it: the user's rule
        self.assertIn("Bash(python3 /work/tools/slides_draw.py *)", self.entry("cv")["allow"])

    def test_auto_rules_alone_are_no_ceiling(self):
        self.config({"default_mode": "dontAsk"})
        self.folder_pro("cw")
        s = self.deny_call("cw", "Bash", {"command": "python3 /work/tools/slides_draw.py --help"}, "toolu_c2")
        self.ok("pump")
        self.wait_until(lambda: self.entry("cw").get("allow") == ["Bash(python3 /work/tools/slides_draw.py *)"], 20, "narrowest rule")
        self.wait_until(lambda: len(self.argv_all(s)) == 2 and s in self.sessions_alive(), 30, "restarted")
        self.wait_until(lambda: json.load(open(os.path.join(self.home, "run", "state", s + ".json"))).get("state") == "idle", 20, "settled")
        self.deny_call("cw", "Bash", {"command": "python3 -m json.tool x.json"}, "toolu_c3")
        self.ok("pump")
        # a denial seen while the first restart still holds its lock is retried on a later pump pass (before 0.8.2: dropped)
        self.wait_until(lambda: self.pro("pump") and "Bash(python3 -m json.tool *)" in self.entry("cw").get("allow", []), 30, "second auto rule")


class ReAdd(Base):
    """A re-add with --allow sets the allow list to the given rules; rules pros added itself go on any --allow or --refresh re-add."""
    def auto_rule(self, name):
        self.config({"default_mode": "dontAsk"})
        self.summon(name)
        s = self.deny_call(name, "Bash", {"command": "wc -l notes.txt"}, "toolu_r1")
        self.ok("pump")
        self.wait_until(lambda: "Bash(wc *)" in self.entry(name).get("allow", []), 20, "auto rule")
        self.wait_until(lambda: len(self.argv_all(s)) == 2 and s in self.sessions_alive(), 30, "restarted")
        self.ok("stop", name)

    def test_allow_re_add_replaces_the_list(self):
        d = self.make_folder("ra")
        self.ok("add", d, "--allow", "Bash(git *)")
        self.ok("add", d, "--allow", "Bash(make *)", "--allow", "Bash(go test *)")
        self.assertEqual(self.entry("ra")["allow"], ["Bash(make *)", "Bash(go test *)"])

    def test_refresh_drops_auto_rules_and_keeps_the_users(self):
        d = self.make_folder("rb")
        self.ok("add", d)
        self.auto_rule("rb")
        with open(os.path.join(d, "CLAUDE.md"), "a") as f:
            f.write("new instruction\n")
        r = self.ok("add", d, "--refresh", "--force")
        self.assertEqual(self.entry("rb")["allow"], [])
        self.assertIn("Bash(wc *)", r.stdout)  # it says what it dropped
        self.assertEqual(self.entry("rb").get("auto_allow", []), [])

    def test_keep_auto_rules_flag(self):
        d = self.make_folder("rk")
        self.ok("add", d)
        self.auto_rule("rk")
        self.ok("add", d, "--allow", "Bash(make *)", "--keep-auto-rules")
        self.assertEqual(self.entry("rk")["allow"], ["Bash(make *)", "Bash(wc *)"])

    def test_plain_re_add_of_a_changed_folder_says_it_is_still_changed(self):
        d = self.make_folder("rc")
        self.ok("add", d)
        with open(os.path.join(d, "CLAUDE.md"), "a") as f:
            f.write("new instruction\n")
        for extra in ((), ("--model", "claude-sonnet-5-5")):
            r = self.ok("add", d, *extra)
            self.assertIn("still changed", r.stdout)
            self.assertIn("--refresh", r.stdout)
        self.ok("add", d, "--refresh")
        self.assertNotIn("still changed", self.ok("add", d, "--model", "").stdout)


class AdHocWaitingRow(test_v080.AdHoc):
    def test_waiting_joined_row_keeps_task_and_title(self):
        self.start_plain("lead", FAKE_REWAKE="1")
        self.join("lead", "--no-pane")
        self.wait_until(lambda: self.state("lead").get("listen_pid"), 15, "listener armed")
        self.ok("send", "lead", "BG 20\nECHO started", "--task", "t-aw", as_pro="beta")
        row = lambda: [r for r in json.loads(self.ok("team", "--json").stdout) if r["name"] == "lead"][0]
        self.wait_until(lambda: row()["activity"] == "waiting", 30, "joined session waiting")
        r = row()
        self.assertEqual((r["task"], r["title"]), ("t-aw", "BG 20"))


class Documented(unittest.TestCase):
    def test_documented(self):
        with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as f:
            self.assertIn("## 0.8.2", f.read())
        with open(os.path.join(ROOT, "docs", "spec", "spec.md"), encoding="utf-8") as f:
            t = f.read()
        self.assertIn("never adds by itself", t)
        self.assertIn("keeps the model", t)
        self.assertIn("guard: none", t)
        self.assertIn("--keep-auto-rules", t)


if __name__ == "__main__":
    unittest.main()
