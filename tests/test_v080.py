"""0.8.0: ad hoc professionals (any Claude Code session joins the bus and is woken when idle) and the three live states
working / waiting on a command / ready."""
import json
import os
import subprocess
import sys
import time
import unittest

from helpers import FAKE, PRO, ProTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AdHoc(ProTestCase):
    def start_plain(self, name, **env):
        """A plain Claude Code session (not summoned by pros) in a tmux server of its own, as the user's lead session runs."""
        e = dict(self.env, **env)
        self.addCleanup(subprocess.run, ["tmux", "-L", self.sock + "-own", "kill-server"], capture_output=True)
        cmd = ["tmux", "-L", self.sock + "-own", "-f", "/dev/null", "new-session", "-d", "-s", name, "-x", "200", "-y", "50"]
        for k in ("PRO_HOME", "PRO_TMUX_SOCKET", "PRO_MARKETPLACE", "FAKE_DIR", "FAKE_PRO", "HOME", "PRO_NO_AUTO", "PRO_CLAUDE_BIN", *env):
            cmd += ["-e", "%s=%s" % (k, e[k])]
        subprocess.run(cmd + [sys.executable, FAKE, "-n", name], check=True, env=e)
        self.wait_until(lambda: "START" in self.fake_log(name), 15, "plain session up")
        self.wait_until(lambda: os.path.exists(os.path.join(self.fakedir, name + ".status")), 15, "plain session ready")

    def type(self, session, text):
        subprocess.run(["tmux", "-L", self.sock + "-own", "send-keys", "-t", "=%s:" % session, "-l", text], check=True, env=self.env)
        time.sleep(0.3)
        subprocess.run(["tmux", "-L", self.sock + "-own", "send-keys", "-t", "=%s:" % session, "Enter"], check=True, env=self.env)

    def runs(self, session):
        return [l for l in self.fake_log(session) if l.startswith("RUN ")]

    def join(self, name, *extra):
        self.type(name, "RUN join %s %s" % (name, " ".join(extra)))
        self.wait_until(lambda: self.runs(name), 20, "join ran")
        self.assertTrue(self.runs(name)[-1].startswith("RUN 0 joined as " + name), self.runs(name))
        self.wait_until(lambda: self.state(name).get("state") == "idle", 15, "turn over")

    def state(self, name):
        p = os.path.join(self.home, "run", "state", "adhoc-%s.json" % name)
        return json.load(open(p)) if os.path.exists(p) else {}

    def sid(self, name):
        return open(os.path.join(self.fakedir, name + ".sid")).read().strip()

    def msg(self, pred):
        return [m for m in self.msgs() if pred(m)]

    def test_idle_session_is_woken_by_paste_and_talks_both_ways(self):
        self.summon("alpha")
        self.start_plain("lead")
        self.join("lead")
        self.type("lead", "RUN send alpha 'ECHO pong-from-alpha'")
        self.wait_until(lambda: len(self.runs("lead")) == 2, 20, "send ran")
        task = self.msg(lambda m: m["to"] == "alpha" and m["kind"] == "task")
        self.assertEqual([m["from"] for m in task], ["lead"])  # it sends as its joined name, not as user
        self.assertEqual(task[0]["reply_to"], "lead")
        # alpha replies to lead; lead's turn is over and nobody types: the reply must reach it on its own
        self.wait_until(lambda: self.msg(lambda m: m["to"] == "lead" and m["kind"] == "reply"), 30, "reply stored")
        rep = self.msg(lambda m: m["to"] == "lead" and m["kind"] == "reply")[0]
        self.wait_until(lambda: any(rep["id"] in a for a in self.acts("lead")), 30, "reply pasted into the idle session")
        self.assertIn("pong-from-alpha", rep["body"])
        self.assertEqual(self.msg(lambda m: m["id"] == rep["id"])[0]["state"], "answered")
        # and the other direction: a professional starts a task to the joined session, which answers it
        r = self.ok("send", "lead", "ECHO lead-answers", "--task", "t-back", as_pro="alpha")
        self.assertIn("status=", r.stdout)
        self.wait_until(lambda: self.msg(lambda m: m["task"] == "t-back" and m["kind"] == "reply" and m["from"] == "lead"), 30, "lead answered")
        team = json.loads(self.ok("team", "--json").stdout)
        row = [x for x in team if x["name"] == "lead"][0]
        self.assertEqual((row["scope"], row["harness"]), ("adhoc", "adhoc"))

    def test_listener_wakes_a_session_without_tmux(self):
        self.start_plain("lead", FAKE_REWAKE="1")
        self.join("lead", "--no-pane")
        self.wait_until(lambda: self.state("lead").get("listen_pid"), 15, "listener armed after the turn")
        self.ok("send", "lead", "ECHO via-listener", "--task", "t-l", as_pro="beta")
        self.wait_until(lambda: any(l.startswith("REWAKE m-") for l in self.fake_log("lead")), 20, "woken by the listener")
        mid = [m for m in self.msgs() if m["task"] == "t-l" and m["kind"] == "task"][0]["id"]
        self.wait_until(lambda: any(mid in a for a in self.acts("lead")), 10, "acted on the woken message")
        self.wait_until(lambda: self.msg(lambda m: m["task"] == "t-l" and m["kind"] == "reply"), 20, "answered")
        self.assertEqual(self.msg(lambda m: m["id"] == mid)[0]["via"], "rewake")

    def test_busy_session_gets_the_message_from_its_stop_hook(self):
        self.start_plain("lead")
        self.join("lead", "--no-pane")
        st = os.path.join(self.home, "run", "state", "adhoc-lead.json")
        d = json.load(open(st))
        d["state"] = "busy"
        json.dump(d, open(st, "w"))
        r = self.ok("send", "lead", "ECHO later", "--task", "t-s", as_pro="beta")
        self.assertIn("status=queued", r.stdout)
        out = self.ok("hook", "stop", input=json.dumps({"session_id": self.sid("lead")})).stdout
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertIn("t-s", json.loads(out)["reason"])

    def test_joined_session_at_a_permission_dialog_needs_approval(self):
        """card 1606: a joined session's pane is read too; its permission dialog shows as 'needs approval'."""
        self.start_plain("lead")
        self.join("lead")
        self.ok("send", "lead", "PERMIT 12\nECHO approved", "--task", "t-aperm", as_pro="beta")
        row = lambda: [x for x in json.loads(self.ok("team", "--json").stdout) if x["name"] == "lead"][0]
        self.wait_until(lambda: row()["activity"] == "needs approval", 20, "joined session needs approval")
        r = row()
        self.assertEqual((r["scope"], r["state"], r["reason"]), ("adhoc", "dialog", "permission dialog"), r)  # a joined row has no task id (pre-1606)
        self.wait_until(lambda: row()["activity"] == "ready", 30, "ready once the dialog is gone")

    def test_join_rules_and_leave(self):
        env = {"CLAUDE_CODE_SESSION_ID": "s-1", "CLAUDE_PID": str(os.getpid())}
        self.assertEqual(self.pro("join", "lead", env=env, as_pro="alpha").returncode, 4)  # a professional has its name
        self.assertEqual(self.pro("join", "alpha", env=env).returncode, 2)  # a listed professional's name
        self.assertEqual(self.pro("join", "lead").returncode, 2)  # not inside a Claude Code session
        self.ok("join", "lead", env=env)
        self.assertEqual(self.pro("join", "lead", env={"CLAUDE_CODE_SESSION_ID": "s-2", "CLAUDE_PID": str(os.getpid())}).returncode, 2)
        self.assertEqual(self.pro("summon", "lead").returncode, 4)  # joined is not listed: never started by pros
        self.ok("send", "lead", "hello", "--kind", "note", as_pro="alpha")
        self.assertIn("1 unread", self.ok("inbox", env=env).stdout)
        r = self.ok("leave", env=env)
        self.assertIn("1 unread message(s) moved", r.stdout)
        self.assertEqual(self.pro("send", "lead", "after leaving", as_pro="alpha").returncode, 4)  # gone from the bus
        n =self.msg(lambda m: m.get("adhoc_left") == "lead")
        self.assertEqual([m["to"] for m in n], ["user"])
        self.assertIn("inbox for user: 1 unread", self.ok("inbox").stdout)


class LiveStates(ProTestCase):
    def row(self, name):
        return [r for r in json.loads(self.ok("team", "--json").stdout) if r["name"] == name][0]

    def test_waiting_on_its_own_command_is_not_ready(self):
        self.config({"idle_reap_min": 0})
        self.summon("alpha")
        self.ok("send", "alpha", "BG 25\nECHO started the suite", "--task", "t-bg")
        self.wait_until(lambda: self.row("alpha")["state"] == "idle" and any(m["task"] == "t-bg" and m["kind"] == "reply" for m in self.msgs()), 20, "turn over")
        r = self.row("alpha")
        self.assertEqual((r["state"], r["activity"]), ("idle", "waiting"), r)
        self.assertGreaterEqual(r["bg"], 1)
        line = [l for l in self.ok("team").stdout.splitlines() if l.startswith("alpha")][0]
        self.assertIn("waiting*", line)
        self.assertEqual(self.listing()["alpha"]["activity"], "waiting")
        self.assertIn("nothing", self.ok("reap").stdout)  # reaping would kill its command
        self.assertEqual(self.pro("send", "alpha", "x", "--fresh").returncode, 5)  # not replaced either
        snaps = [p for p in os.listdir("/proc") if p.isdigit()]
        for p in snaps:  # end the background command: the professional is ready
            try:
                if "snapshot-bash-fake" in open("/proc/%s/cmdline" % p).read() and self.fakedir in open("/proc/%s/cmdline" % p).read():
                    os.kill(int(p), 9)
            except OSError:
                pass
        self.wait_until(lambda: self.row("alpha")["activity"] == "ready", 10, "ready after the command ended")
        self.assertIn("ready", [l for l in self.ok("team").stdout.splitlines() if l.startswith("alpha")][0])

    def test_permission_dialog_needs_approval(self):
        """card 1606: a professional at a tool permission dialog is 'needs approval' (not working, not ready); the footer says so."""
        self.summon("alpha")
        self.ok("send", "alpha", "PERMIT 12\nECHO approved", "--task", "t-perm")
        self.wait_until(lambda: self.row("alpha")["activity"] == "needs approval", 15, "needs approval")
        r = self.row("alpha")
        self.assertEqual((r["state"], r["reason"], r["task"]), ("dialog", "permission dialog", "t-perm"), r)
        out = self.ok("team").stdout
        self.assertIn("needs approval*", [l for l in out.splitlines() if l.startswith("alpha")][0])
        self.assertIn("needs approval = a permission", out)
        self.assertEqual(self.listing()["alpha"]["activity"], "needs approval")
        self.wait_until(lambda: self.row("alpha")["activity"] == "ready", 30, "ready once the dialog is gone")

    def test_working_while_in_a_turn(self):
        self.summon("alpha")
        self.ok("send", "alpha", "SLEEP 4\nECHO done", "--task", "t-w")
        self.wait_until(lambda: self.row("alpha")["activity"] == "working", 10, "working")


class Documented(unittest.TestCase):
    def test_documented(self):
        for f, words in ((("docs", "spec", "spec.md"), ("pros join", "asyncRewake", "waiting")), (("README.md",), ("pros join", "waiting")),
                         (("plugins", "professionals", "skills", "professionals", "SKILL.md"), ("pros join", "waiting")), (("CHANGELOG.md",), ("## 0.8.0",))):
            with open(os.path.join(ROOT, *f), encoding="utf-8") as fh:
                t = fh.read()
            for w in words:
                self.assertIn(w, t, f)
        with open(os.path.join(ROOT, "plugins", "professionals", "hooks", "hooks.json")) as fh:
            stop = json.load(fh)["hooks"]["Stop"][0]["hooks"]
        self.assertTrue(any(h.get("asyncRewake") and h["command"].endswith("hook listen") for h in stop))


if __name__ == "__main__":
    unittest.main()
