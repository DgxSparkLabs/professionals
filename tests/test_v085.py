"""0.8.5: a professional whose program exited leaves its tmux pane open for 20 s ("[pro: exited N]"); a summon in that
window failed with "tmux could not start the session: duplicate session" (seen live on a voice pro after its process died)."""
import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import unittest

from helpers import PRO, ProTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ExitHelpers:
    def pane(self, s):
        return subprocess.run(["tmux", "-L", self.sock, "capture-pane", "-p", "-t", s], capture_output=True, text=True).stdout

    def kill_program(self, s):
        top = subprocess.run(["tmux", "-L", self.sock, "list-panes", "-t", s, "-F", "#{pane_pid}"], capture_output=True, text=True).stdout.split()[0]
        kids = [p for p in os.listdir("/proc") if p.isdigit() and self.ppid(p) == top]
        for p in kids:
            os.kill(int(p), signal.SIGKILL)
        self.assertTrue(kids, "the program under the pane shell")

    def ppid(self, p):
        try:
            with open("/proc/%s/stat" % p) as f:
                return f.read().rsplit(")", 1)[1].split()[1]
        except OSError:
            return ""


class SummonAfterExit(ExitHelpers, ProTestCase):
    def test_summon_right_after_the_program_exited_starts_a_new_run(self):
        self.config({"default_mode": "dontAsk"})
        self.summon("alpha", "--model", "claude-sonnet-5-5", "--thinking", "high")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        self.kill_program(s)
        self.wait_until(lambda: "[pro: exited" in self.pane(s), 10, "pane shows the exit")
        r = self.pro("summon", "alpha")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reused=no", r.stdout)
        run = self.argv_all(s)[-1]["argv"]
        self.assertEqual(len(self.argv_all(s)), 2)
        self.assertEqual((run[run.index("--model") + 1], run[run.index("--effort") + 1]), ("claude-sonnet-5-5", "high"))

    def test_a_live_session_is_still_reused(self):
        self.config({"default_mode": "dontAsk"})
        self.summon("alpha")
        self.assertIn("reused=yes", self.ok("summon", "alpha").stdout)


loader = importlib.machinery.SourceFileLoader("pro_mod085", PRO)
spec = importlib.util.spec_from_loader("pro_mod085", loader)
pro = importlib.util.module_from_spec(spec)
loader.exec_module(pro)


class ExitMarkerInOutput(unittest.TestCase):
    """A live session that shows the text "[pro: exited" (a grep of pros, a test it writes) was taken for exited:
    summon dropped its record ("died") and tried to start it again (seen live on pros-dev)."""

    def test_marker_shown_in_tool_output_is_not_an_exit(self):
        live = ('  ⎿  shell = "%s; code=$?; echo \\"[pro: exited $code]\\"; sleep 20"\n'
                '     wait_until(lambda: "[pro: exited" in self.pane(s))\n'
                '[pro: exited 1]\n'
                '✻ Thinking… (esc to interrupt)\n'
                '─' * 40 + '\n❯ \n' + '─' * 40 + '\n')
        self.assertNotEqual(pro.classify_pane(live), "gone")

    def test_the_shells_own_last_line_is_an_exit(self):
        self.assertEqual(pro.classify_pane("Resume this session with:\nclaude --resume x\n[pro: exited 0]\n\n"), "gone")
        self.assertEqual(pro.classify_pane("Killed\n      [pro: exited 137]\n\n"), "gone")  # raw tty: indented


class AnsweredTasksStayClosed(ExitHelpers, ProTestCase):
    """A retry re-queued tasks answered long before: add-ons sent under the same task id stayed 'injected'."""

    def states(self, task):
        return [(m["state"], bool(m.get("dup"))) for m in self.msgs() if m["task"] == task and m["kind"] == "task"]

    def test_one_reply_closes_every_delivered_message_of_the_task(self):
        self.config({"default_mode": "dontAsk"})
        self.summon("alpha")
        self.ok("send", "alpha", "NOREPLY\nthe work", "--task", "t-two")
        self.wait_until(lambda: [s for s, _ in self.states("t-two")] == ["injected"], 20, "first delivered")
        self.ok("send", "alpha", "NOREPLY\nadd-on to the same work", "--task", "t-two")
        self.wait_until(lambda: [s for s, _ in self.states("t-two")] == ["injected"] * 2, 20, "add-on delivered")
        self.ok("reply", "t-two", "done, both parts", as_pro="alpha")
        self.assertEqual(self.states("t-two"), [("answered", False)] * 2)

    def test_restart_resends_only_the_task_in_flight(self):
        self.config({"default_mode": "dontAsk"})
        self.summon("alpha")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        self.ok("send", "alpha", "NOREPLY\nold work", "--task", "t-old")
        self.wait_until(lambda: [x for x, _ in self.states("t-old")] == ["injected"], 20, "old delivered")
        self.ok("reply", "t-old", "old work done", as_pro="alpha")
        old = [m for m in self.msgs() if m["task"] == "t-old" and m["kind"] == "task"][0]
        old["state"] = "injected"  # as an add-on left by pros before 0.8.5
        with open(os.path.join(self.home, "run", "msgs", old["id"] + ".json"), "w") as f:
            json.dump(old, f)
        self.ok("send", "alpha", "NOREPLY\ncurrent work", "--task", "t-now")
        self.wait_until(lambda: [x for x, _ in self.states("t-now")] == ["injected"], 20, "current delivered")
        self.kill_program(s)
        self.wait_until(lambda: "[pro: exited" in self.pane(s), 10, "exited")
        self.summon("alpha")
        self.assertEqual(self.states("t-old"), [("answered", False)])
        self.assertTrue(self.states("t-now")[0][1], "the task in flight is resent, marked possible duplicate")


class Documented(unittest.TestCase):
    def test_changelog(self):
        with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
            self.assertIn("## 0.8.5", f.read())


if __name__ == "__main__":
    unittest.main()
