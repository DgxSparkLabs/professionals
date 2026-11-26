"""0.9.2: a Cursor model on Pi ran with 'guard: none': Cursor's own agent runs the tools, so neither the pros guard extension
nor an allowlist sees them.  pros now points the provider at a wrapper (CURSOR_AGENT_PATH) that starts the agent inside bwrap:
the system read-only, writes only in its folder and the agent's own dirs, the protected paths, the pros home and the folder's
secret files hidden.  No bwrap or no agent means no session (fail closed); the user may opt out with tool_sandbox {}."""
import json
import os
import shutil
import subprocess
import sys
import unittest

import test_v070
from test_v070 import FAKE_PI

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

AGENT = r'''#!%s
import json, os, sys
out = {"argv": sys.argv[1:]}
def rd(p):
    try:
        return open(p).read()
    except OSError as e:
        return "ERR " + type(e).__name__
def wr(p):
    try:
        open(p, "w").write("x")
        return "ok"
    except OSError as e:
        return "ERR " + type(e).__name__
c = json.loads(os.environ["CASES"])
out["read"] = {k: rd(v) for k, v in c["read"].items()}
out["write"] = {k: wr(v) for k, v in c["write"].items()}
print(json.dumps(out))
'''


@unittest.skipUnless(shutil.which("bwrap") and subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "true"]).returncode == 0, "bwrap cannot run here")
class CursorToolsSandboxed(test_v070.PiCase):
    def setup_pi(self, agent_bin, tsb_extra=None):
        tsb = {"env": "CURSOR_AGENT_PATH", "bin": agent_bin, "rw": ["~/.cursor", "~/.linked"]}
        if tsb_extra is not None:
            tsb = tsb_extra
        self.config({"harnesses": {"pi": {"bin": "%s %s" % (sys.executable, FAKE_PI), "path_add": [], "tool_sandbox": tsb}},
                     "deny_paths": [os.path.join(self.tmp, "folders", "gr", "private.txt")]})

    def folder(self):
        d = self.make_folder("gr", agents="# gr\nAnswer briefly.\n")
        for rel, text in ((".env", "TOKEN=1\n"), ("sub/keys.pem", "KEY\n"), ("private.txt", "PRIVATE\n"), ("notes.md", "plain\n")):
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            with open(os.path.join(d, rel), "w") as f:
                f.write(text)
        agent = os.path.join(d, "fake-agent")
        with open(agent, "w") as f:
            f.write(AGENT % sys.executable)
        os.chmod(agent, 0o755)
        os.makedirs(os.path.join(self.fakehome, ".cursor"), exist_ok=True)
        os.makedirs(os.path.join(self.tmp, "offload"), exist_ok=True)
        os.symlink(os.path.join(self.tmp, "offload"), os.path.join(self.fakehome, ".linked"))  # as an offloaded agent dir
        return d, agent

    def session(self, name):
        return [s for s in self.sessions_alive() if s.startswith("pro-" + name)][0]

    def spec(self, s):
        with open(os.path.join(self.home, "run", "bridge", s + ".json")) as f:
            return json.load(f)

    def test_cursor_agent_runs_inside_the_sandbox(self):
        d, agent = self.folder()
        self.setup_pi(agent)
        self.ok("add", d, "--harness", "pi", "--model", "cursor/grok-4.7-medium")
        self.summon("gr")
        s = self.session("gr")
        wrap = self.spec(s)["env"]["CURSOR_AGENT_PATH"]
        self.assertTrue(os.access(wrap, os.X_OK))
        cases = {"read": {"env": os.path.join(d, ".env"), "pem": os.path.join(d, "sub", "keys.pem"), "deny": os.path.join(d, "private.txt"),
                          "registry": os.path.join(self.home, "registry.json"), "plain": os.path.join(d, "notes.md")},
                 "write": {"inside": os.path.join(d, "out.txt"), "cursor": os.path.join(self.fakehome, ".cursor", "x"), "linked": os.path.join(self.fakehome, ".linked", "y"),
                           "home": os.path.join(self.fakehome, "escape.txt"), "tmp": os.path.join(self.tmp, "outside.txt")}}
        r = subprocess.run([wrap, "--print", "hello world"], capture_output=True, text=True, timeout=30, env=dict(self.env, CASES=json.dumps(cases)))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["argv"], ["--print", "hello world"])
        self.assertEqual(out["read"]["plain"], "plain\n")
        for k in ("env", "pem", "deny"):
            self.assertEqual(out["read"][k], "", k)  # masked with an empty file
        self.assertTrue(out["read"]["registry"].startswith("ERR"), out["read"]["registry"])
        self.assertEqual(out["write"]["inside"], "ok")
        self.assertTrue(os.path.exists(os.path.join(d, "out.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.fakehome, ".cursor", "x")))
        self.assertEqual(out["write"]["linked"], "ok")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "offload", "y")))
        self.assertTrue(out["write"]["home"].startswith("ERR") or not os.path.exists(cases["write"]["home"]))
        self.assertFalse(os.path.exists(cases["write"]["home"]))
        self.assertFalse(os.path.exists(cases["write"]["tmp"]))
        with open(os.path.join(d, ".env")) as f:
            self.assertEqual(f.read(), "TOKEN=1\n")  # the host file is untouched
        team = [x for x in json.loads(self.ok("team", "--json").stdout) if x["name"] == "gr"][0]
        self.assertEqual(team["guard"], "sandbox")

    def test_the_wrapper_mounts_root_read_only(self):
        d, agent = self.folder()
        self.setup_pi(agent)
        self.ok("add", d, "--harness", "pi", "--model", "cursor/grok-4.7-medium")
        self.summon("gr")
        with open(self.spec(self.session("gr"))["env"]["CURSOR_AGENT_PATH"]) as f:
            text = f.read()
        self.assertIn("--ro-bind / /", text)
        self.assertNotIn("--bind / /", text)
        self.assertNotIn("--bind %s %s" % (self.fakehome, self.fakehome), text)

    def test_no_agent_to_sandbox_refuses_the_summon(self):
        d, _ = self.folder()
        self.setup_pi(os.path.join(self.tmp, "no-such-agent"))
        self.ok("add", d, "--harness", "pi", "--model", "cursor/grok-4.7-medium")
        r = self.pro("summon", "gr")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("sandbox", r.stdout + r.stderr)
        self.assertEqual([x for x in self.sessions_alive() if x.startswith("pro-gr")], [])

    def test_other_models_need_no_agent(self):
        d, _ = self.folder()
        self.setup_pi(os.path.join(self.tmp, "no-such-agent"))
        self.ok("add", d, "--harness", "pi", "--model", "fake/m1")
        self.summon("gr")
        self.assertNotIn("CURSOR_AGENT_PATH", self.spec(self.session("gr"))["env"])

    def test_user_opt_out_runs_unguarded_and_says_so(self):
        d, agent = self.folder()
        self.setup_pi(agent, tsb_extra={})
        r = self.ok("add", d, "--harness", "pi", "--model", "cursor/grok-4.7-medium")
        self.assertIn("guard: none", r.stdout + r.stderr)
        self.summon("gr")
        self.assertNotIn("CURSOR_AGENT_PATH", self.spec(self.session("gr"))["env"])


class PiAnswersWithoutShell(test_v070.PiCase):
    """Seen live: grok-reviewer (Pi + Cursor) hung 10 minutes on Shell calls (pros reply via heredoc, then echo ok).  A Pi
    session's final message already becomes the reply; its prompt and each task now say so instead of asking for Shell."""

    def test_task_tail_and_prompt_ask_for_the_final_message(self):
        self.add_pi("pa", "--model", "fake/m1")
        self.summon("pa")
        r = self.ok("ask", "pa", "ECHO done by final message", "--wait", "30")
        self.assertIn("kind=reply", r.stdout)
        self.assertIn("done by final message", r.stdout)
        tails = [l for l in self.pilog("pro-pa") if l.startswith("TAIL ")]
        self.assertTrue(tails)
        self.assertIn("final message", tails[-1])
        self.assertNotIn("heredoc", tails[-1])
        self.assertNotIn(" reply ", tails[-1].replace("no pros reply", ""))
        s = [x for x in self.sessions_alive() if x.startswith("pro-pa")][0]
        with open(os.path.join(self.home, "run", "prompts", s + ".md")) as f:
            prompt = f.read()
        self.assertNotIn("Answer a task with a quoted heredoc", prompt)
        self.assertIn("Answer a task by ending your turn with the answer as your final message", prompt)
        self.assertNotIn("optional", prompt)

    def test_claude_sessions_keep_the_reply_command(self):
        self.config({"default_mode": "dontAsk"})
        self.summon("alpha")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        with open(os.path.join(self.home, "run", "prompts", s + ".md")) as f:
            self.assertIn("Answer a task with a quoted heredoc", f.read())


class Documented(unittest.TestCase):
    def test_docs(self):
        with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
            self.assertIn("## 0.9.2", f.read())
        with open(os.path.join(ROOT, "docs", "spec", "spec.md")) as f:
            self.assertIn("guard: sandbox", f.read())


if __name__ == "__main__":
    unittest.main()
