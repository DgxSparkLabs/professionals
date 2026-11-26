"""0.7.0: harness registry (claude, pi), per-professional harness/model/thinking, the Pi RPC bridge, caller evaluations."""
import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from helpers import ProTestCase, load_pros, HERE

FAKE_PI = os.path.join(HERE, "fake_pi.py")


class PiCase(ProTestCase):
    def setUp(self):
        super().setUp()
        self.config({"harnesses": {"pi": {"bin": "%s %s" % (sys.executable, FAKE_PI), "path_add": []}}})

    def entry(self, name):
        with open(os.path.join(self.home, "registry.json")) as f:
            return json.load(f)["entries"][name]

    def pilog(self, s):
        p = os.path.join(self.fakedir, s + ".pilog")
        return open(p).read().splitlines() if os.path.exists(p) else []

    def pi_argv(self, s):
        return json.loads([l for l in self.pilog(s) if l.startswith("START ")][-1][6:])

    def add_pi(self, name, *extra):
        d = self.make_folder(name, agents="# %s\nAnswer briefly.\n" % name)
        self.ok("add", d, "--harness", "pi", *extra)
        return d

    def state(self, s):
        p = os.path.join(self.home, "run", "state", s + ".json")
        return json.load(open(p)) if os.path.exists(p) else {}


class HarnessRegistry(PiCase):
    def test_list_shows_builtins_and_override(self):
        rows = {r["name"]: r for r in json.loads(self.ok("harness", "list", "--json").stdout)}
        self.assertEqual(rows["claude"]["proto"], "claude")
        self.assertEqual(rows["pi"]["proto"], "pi-rpc")
        self.assertEqual(rows["pi"]["source"], "override")
        self.assertEqual(rows["pi"]["found"], "yes")
        self.assertIn("HARNESS", self.ok("harness", "list").stdout.upper())

    def test_builtin_pi_defaults(self):
        mod = load_pros(self.home)
        pi = mod.BUILTIN_HARNESSES["pi"]
        self.assertEqual(pi["bin"], "~/.local/node/bin/pi")
        self.assertEqual(pi["path_add"], ["~/.local/node/bin"])
        self.assertEqual(pi["args"], ["--mode", "rpc"])
        self.assertFalse(pi["blocked_models"])
        self.assertTrue(mod.BUILTIN_HARNESSES["claude"]["blocked_models"])
        path = mod.harness_path(dict(pi, path_add=["/opt/nodebin"])).split(os.pathsep)
        self.assertLess(path.index("/opt/nodebin"), path.index(os.environ["PATH"].split(os.pathsep)[0]))

    def test_add_and_remove_are_user_only(self):
        r = self.pro("harness", "add", "mine", "--proto", "pi-rpc", "--bin", "/bin/true", as_pro="alpha")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("user", r.stderr)
        self.ok("harness", "add", "mine", "--proto", "pi-rpc", "--bin", "/bin/true", "--arg=--mode", "--arg=rpc",
                "--mode-args", "plan=--tools read", "--note", "test one")
        h = json.load(open(os.path.join(self.home, "config.json")))["harnesses"]["mine"]
        self.assertEqual((h["args"], h["modes"]["plan"], h["note"]), (["--mode", "rpc"], ["--tools", "read"], "test one"))
        self.assertNotEqual(self.pro("harness", "remove", "mine", as_pro="alpha").returncode, 0)
        self.add_pi("usesmine")
        self.ok("add", self.make_folder("usesmine"), "--harness", "mine")
        self.assertEqual(self.pro("harness", "remove", "mine").returncode, 4)
        self.ok("add", self.make_folder("usesmine"), "--harness", "pi")
        self.ok("harness", "remove", "mine")
        self.assertNotIn("mine", json.load(open(os.path.join(self.home, "config.json")))["harnesses"])

    def test_bad_new_harness_and_unknown_name(self):
        self.assertEqual(self.pro("harness", "add", "nope", "--bin", "/bin/true").returncode, 2)  # no proto
        self.assertEqual(self.pro("harness", "add", "nope", "--proto", "x", "--bin", "/bin/true").returncode, 2)
        self.assertEqual(self.pro("harness", "remove", "claude").returncode, 2)  # built in, nothing to remove
        self.assertEqual(self.pro("summon", "alpha", "--harness", "nosuch").returncode, 2)


class EntryFields(PiCase):
    def test_add_stores_harness_model_thinking(self):
        self.add_pi("grokw", "--model", "cursor/cursor-grok-4.6-medium", "--thinking", "medium")
        e = self.entry("grokw")
        self.assertEqual((e["harness"], e["model"], e["thinking"]), ("pi", "cursor/cursor-grok-4.6-medium", "medium"))
        self.assertIn("harness: pi", self.ok("show", "grokw").stdout)
        self.ok("enable", "beta", "--harness", "pi", "--thinking", "high")
        self.assertEqual((self.entry("beta")["harness"], self.entry("beta")["thinking"]), ("pi", "high"))
        self.assertNotEqual(self.pro("add", self.make_folder("badthink"), "--thinking", "Very High!").returncode, 0)

    def test_claude_thinking_becomes_effort(self):
        self.ok("add", self.make_folder("cl"), "--thinking", "high")
        self.summon("cl")
        a = self.argv_info("pro-cl")["argv"]
        self.assertEqual(a[a.index("--effort") + 1], "high")


class PiSession(PiCase):
    def test_summon_argv_and_ready(self):
        self.add_pi("piw", "--model", "cursor/cursor-grok-4.6-medium", "--thinking", "medium")
        r = self.summon("piw")
        self.assertIn("pro-piw", r.stdout)
        a = self.pi_argv("pro-piw")
        self.assertEqual(a[:2], ["--mode", "rpc"])
        self.assertEqual(a[a.index("--model") + 1], "cursor/cursor-grok-4.6-medium")
        self.assertEqual(a[a.index("--thinking") + 1], "medium")
        self.assertEqual(a[a.index("--name") + 1], "pro-piw")
        self.assertTrue(a[a.index("--session-dir") + 1].startswith(self.home))
        guard = a[a.index("-e") + 1]
        self.assertTrue(guard.endswith(".guard.mjs"))
        text = open(guard).read()
        self.assertIn(os.path.join(self.fakehome, ".ssh"), text)
        self.assertIn("pros-deny:", text)
        prompt = open(a[a.index("--append-system-prompt") + 1]).read()
        self.assertIn("pi", prompt)
        self.assertIn("reply", prompt)
        st = self.state("pro-piw")
        self.assertTrue(st.get("ready"))
        self.assertEqual(st.get("pi_model"), "cursor/cursor-grok-4.6-medium")

    def test_no_fable_block_and_no_persona_or_config_model_for_pi(self):
        self.config({"model": "sonnet"})
        self.add_pi("fab", "--model", "claude-fable-5-2")
        self.summon("fab")
        self.assertEqual(self.pi_argv("pro-fab")[self.pi_argv("pro-fab").index("--model") + 1], "claude-fable-5-2")
        self.add_pi("plain")
        self.summon("plain")
        self.assertNotIn("--model", self.pi_argv("pro-plain"))  # Pi's own config picks the model
        r = self.pro("summon", "alpha", "--model", "claude-fable-5-2")
        self.assertEqual(r.returncode, 4)  # still blocked for claude

    def test_plan_mode_limits_tools(self):
        self.add_pi("planner")
        self.summon("planner", "--mode", "plan")
        a = self.pi_argv("pro-planner")
        self.assertEqual(a[a.index("--tools") + 1], "read,grep,find,ls")

    def test_marketplace_persona_on_pi_harness(self):
        self.summon("alpha", "--harness", "pi")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        a = self.pi_argv(s)
        self.assertNotIn("--model", a)  # persona model sonnet is a claude name; not passed to pi
        r = self.ok("ask", "alpha", "ECHO from-persona", "--wait", "30")
        self.assertIn("from-persona", r.stdout)

    def test_ask_round_trip_user_to_pi(self):
        self.add_pi("piw", "--model", "fake/m1", "--thinking", "low")
        self.summon("piw")
        r = self.ok("ask", "piw", "ECHO hello pi", "--wait", "30")
        self.assertIn("kind=reply", r.stdout)
        self.assertIn("fake-pi(model=fake/m1 thinking=low) hello pi", r.stdout)
        t = [m for m in self.msgs() if m["kind"] == "task"][0]
        self.assertEqual(t["state"], "answered")
        self.assertEqual(t["via"], "rpc")
        self.assertTrue([l for l in self.pilog("pro-piw") if l.startswith("PROMPT " + t["id"])])
        team = self.ok("team").stdout
        self.assertIn("HARNESS", team.upper())
        row = [l for l in team.splitlines() if l.startswith("piw")][0]
        self.assertIn("pi", row)
        self.assertIn("fake/m1", row)

    def test_queue_delivers_in_order_after_settle(self):
        self.add_pi("q")
        self.summon("q")
        self.ok("send", "q", "SLEEP 2\nECHO first")
        for i in (2, 3):
            self.ok("send", "q", "ECHO q%d" % i)
        self.wait_until(lambda: len([l for l in self.pilog("pro-q") if l.startswith("PROMPT")]) == 3, 30, "three prompts")
        order = [m["id"] for m in self.msgs() if m["kind"] == "task"]
        self.assertEqual([l.split()[1] for l in self.pilog("pro-q") if l.startswith("PROMPT")], order)
        self.wait_until(lambda: all(m["state"] == "answered" for m in self.msgs() if m["kind"] == "task"), 20, "answered")

    def test_pi_asks_claude(self):
        self.add_pi("asker")
        self.summon("asker")
        self.summon("alpha")
        r = self.ok("ask", "asker", "ASK alpha ECHO claude-says-hi", "--wait", "60")
        self.assertIn("ask-exit=0", r.stdout)
        self.assertIn("claude-says-hi", r.stdout)
        sub = [m for m in self.msgs() if m["kind"] == "task" and m["from"] == "asker"]
        self.assertEqual((sub[0]["to"], sub[0]["state"]), ("alpha", "answered"))

    def test_resume_keeps_session_id_only_within_harness(self):
        self.add_pi("res")
        self.summon("res")
        self.ok("ask", "res", "ECHO one", "--wait", "30")
        sid = self.pi_argv("pro-res")[self.pi_argv("pro-res").index("--session-id") + 1]
        self.ok("stop", "res")
        self.wait_until(lambda: "pro-res" not in self.sessions_alive(), 15, "stopped")
        r = self.summon("res")
        self.assertIn("resumed=" + sid, r.stdout)
        self.assertEqual(self.pi_argv("pro-res")[self.pi_argv("pro-res").index("--session-id") + 1], sid)
        self.ok("stop", "res")
        self.wait_until(lambda: "pro-res" not in self.sessions_alive(), 15, "stopped")
        r = self.pro("summon", "res", "--harness", "claude", "--session", sid)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)  # a pi session cannot resume under claude
        r = self.summon("res", "--harness", "claude")
        self.assertNotIn("resumed=" + sid, r.stdout)

    def test_dialogs_are_refused(self):
        self.add_pi("dlg")
        self.summon("dlg")
        self.ok("ask", "dlg", "DIALOG\nECHO after", "--wait", "30")
        line = [l for l in self.pilog("pro-dlg") if l.startswith("DIALOG ")][0]
        ans = json.loads(line[7:])
        self.assertIs(ans["cancelled"], True)
        self.assertNotIn("confirmed", ans)

    def test_guard_denial_is_counted_and_reported(self):
        self.add_pi("den")
        self.summon("den")
        r = self.ok("ask", "den", "READ %s/.ssh/id_ed25519\nREAD notes.txt" % self.fakehome, "--wait", "30")
        self.assertIn("blocked", r.stdout)
        self.assertIn("notes.txt: ok", r.stdout)
        self.assertEqual(self.state("pro-den").get("pi_denials"), {"read": 1})
        note = [m for m in self.msgs() if m["to"] == "user" and "denied" in m.get("body", "")]
        self.assertTrue(note, "the user is told about the denial")
        self.assertIn("den", [l.split()[0] for l in self.ok("team").stdout.splitlines() if l.strip()])

    def test_start_failure_is_reported(self):
        self.add_pi("broken")
        r = self.pro("summon", "broken", env={"FAKE_PI_FAIL": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAKE_PI_FAIL", r.stdout + r.stderr)

    def test_crash_marks_session_gone(self):
        self.add_pi("crash")
        self.summon("crash")
        self.ok("send", "crash", "CRASH")
        self.wait_until(lambda: self.state("pro-crash").get("state") == "gone", 20, "gone")


class GuardJs(PiCase):
    @unittest.skipUnless(shutil.which("node") or os.path.exists(os.path.expanduser("~/.local/node/bin/node")), "node not installed")
    def test_guard_blocks_in_node(self):
        node = shutil.which("node") or os.path.expanduser("~/.local/node/bin/node")
        mod = load_pros(self.home)
        old = os.environ.get("HOME")
        os.environ["HOME"] = self.fakehome
        try:
            with mock.patch.object(mod, "protected_paths", return_value=["~/.ssh", "keys", "/w/vault"]):
                path = mod.write_guard("pro-g", {})
        finally:
            os.environ["HOME"] = old
        test = os.path.join(self.tmp, "t.mjs")
        with open(test, "w") as f:
            f.write("""import g from %s;
let h; g({on: (n, f) => { h = f; }});
const out = [];
for (const inp of %s) { const r = await h({toolName: "read", input: inp}); out.push(r ? r.block : false); }
console.log(JSON.stringify(out));
""" % (json.dumps("file://" + str(path)), json.dumps([{"path": "/etc/hosts"}, {"path": "keys/a"}, {"path": "monkeys.txt"},
                                                      {"command": "cat ~/.ssh/id_rsa"}, {"path": "/w/vault/x"}])))
        r = subprocess.run([node, test], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [False, True, False, True, True])


class Evaluations(PiCase):
    def test_caller_evaluates_and_anyone_reads(self):
        self.add_pi("ev", "--model", "fake/m2")
        self.summon("ev")
        r = self.ok("ask", "ev", "ECHO x", "--wait", "30")
        task = self.task_of(r.stdout)
        self.assertIn("pros evaluate", r.stderr)
        self.assertEqual(self.pro("evaluate", task, "passed", as_pro="beta").returncode, 4)  # not beta's task
        self.assertEqual(self.pro("evaluate", "t-nosuch", "passed").returncode, 3)
        self.ok("evaluate", task, "partial", "--criterion", "complete=failed")
        out = json.loads(self.ok("evaluations", "ev", "--json", as_pro="alpha").stdout)
        b = out["by_professional"][0]
        self.assertEqual((b["pro"], b["harness"], b["model"], b["partial"], b["n"]), ("ev", "pi", "fake/m2", 1, 1))
        self.assertEqual(out["recent"][0]["task"], task)
        self.assertIn("ev", self.ok("evaluations").stdout)

    def test_professional_caller_records_automated_verdict(self):
        self.summon("alpha")
        self.summon("beta")
        r = self.ok("ask", "beta", "ECHO y", "--wait", "30", as_pro="alpha")
        task = self.task_of(r.stdout)
        self.ok("evaluate", task, "failed", as_pro="alpha")
        rec = json.loads(self.ok("evaluations", "--json").stdout)["recent"][0]
        self.assertEqual((rec["pro"], rec["status"], rec["evaluator"]), ("beta", "failed", "alpha"))

    def test_protocol_and_skill_mention_evaluations(self):
        mod = load_pros(self.home)
        self.assertIn("{pros} evaluations", mod.PROTOCOL)
        self.assertIn("{pros} evaluate <task> passed|partial|failed", mod.PROTOCOL)
        skill = open(os.path.join(os.path.dirname(HERE), "plugins", "professionals", "skills", "professionals", "SKILL.md")).read()
        for w in ("pros evaluate", "pros evaluations", "pros harness", "--harness", "--thinking"):
            self.assertIn(w, skill)
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(HERE), "plugins", "professionals", "skills", "professionals", "worker-brief.md")))


if __name__ == "__main__":
    unittest.main()
