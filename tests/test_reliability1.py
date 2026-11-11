"""Regression tests for the seven failure cases in docs/reviews/reliability-1.md and the points marked unverified in docs/design.md."""
import fcntl
import importlib.machinery
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from helpers import *


def hold_lock(home, name, seconds):
    os.makedirs(os.path.join(home, "run", "locks"), exist_ok=True)
    f = open(os.path.join(home, "run", "locks", name + ".lock"), "a+")
    fcntl.flock(f, fcntl.LOCK_EX)

    def release():
        time.sleep(seconds)
        f.close()
    t = threading.Thread(target=release)
    t.start()
    return t


class Reliability1(ProTestCase):
    def session(self, name):
        return [s for s in self.sessions_alive() if s.startswith("pro-" + name)][0]

    # 1 ---------------------------------------------------------------- reply lost on a busy lock
    def test_1_reply_is_stored_when_the_asker_lock_is_busy(self):
        self.summon("alpha")
        self.summon("beta")
        task = self.task_of(self.ok("send", "beta", "NOREPLY", as_pro="alpha").stdout)
        self.wait_until(lambda: any(m["to"] == "beta" and m["state"] == "injected" for m in self.msgs()), 10, "beta got it")
        t = hold_lock(self.home, self.session("alpha"), 9)
        t0 = time.time()
        r = self.ok("reply", task, "the answer", as_pro="beta")
        self.assertLess(time.time() - t0, 8)
        replies = [m for m in self.msgs() if m["kind"] == "reply" and m["task"] == task]
        self.assertEqual(len(replies), 1)
        self.assertIn("status=queued", r.stdout)
        t.join()
        self.wait_until(lambda: self.pro("team", env={"PRO_NO_AUTO": ""}) and
                        [m for m in self.msgs() if m["kind"] == "reply"][0]["state"] in ("injected", "answered"), 20, "reply delivered later")

    # 2 ---------------------------------------------------------------- swallowed paste leaves a draft
    def test_2_swallowed_enter_is_retried_then_cleared_and_reported(self):
        self.config({"ack_timeout_s": 3})
        self.summon("alpha", env={"FAKE_IGNORE_ENTER": "all"})
        s = self.session("alpha")
        self.ok("send", "alpha", "ECHO never", env={"FAKE_IGNORE_ENTER": "all"})
        for _ in range(3):
            self.pro("team", env={"PRO_NO_AUTO": ""})
        m = [x for x in self.msgs() if x["kind"] == "task"][0]
        self.assertEqual(m["state"], "failed", m)
        self.assertIn("input box never accepted Enter", m["fail_reason"])
        pane = subprocess.run(["tmux", "-L", self.sock, "capture-pane", "-p", "-t", "=%s:" % s], capture_output=True, text=True).stdout
        self.assertNotIn("pro-msg", pane)  # the box was cleared
        self.assertIn("not delivered", self.ok("inbox").stdout + self.ok("read", [x for x in self.msgs() if x["kind"] == "note"][0]["id"]).stdout)
        self.assertEqual(self.acts(s), [])

    def test_2b_one_swallowed_enter_is_recovered_by_a_second_enter(self):
        self.config({"ack_timeout_s": 6})
        self.summon("alpha", env={"FAKE_IGNORE_ENTER": "1"})
        r = self.ok("ask", "alpha", "ECHO ok", "--wait", "30", env={"FAKE_IGNORE_ENTER": "1"})
        self.assertIn("ok", r.stdout)

    # 3 ---------------------------------------------------------------- no daemon
    def test_3_pump_loop_delivers_without_any_command(self):
        self.config({"pump_interval_s": 1})
        env = {"PRO_NO_AUTO": ""}
        self.summon("alpha", env=env)
        self.assertIn("pros-pump", subprocess.run(["tmux", "-L", self.sock, "ls", "-F", "#{session_name}"], capture_output=True, text=True).stdout)
        s = self.session("alpha")
        t = hold_lock(self.home, s, 6)
        r = self.ok("send", "alpha", "ECHO later", env=env)
        self.assertIn("status=queued", r.stdout)  # the lock was busy, the message is stored
        t.join()
        self.wait_until(lambda: len(self.acts(s)) == 1, 15, "delivered by the pump loop with no pros command running")

    def test_3b_pump_loop_enforces_deadlines(self):
        self.config({"pump_interval_s": 1, "deliver_deadline_s": 3})
        env = {"PRO_NO_AUTO": ""}
        self.summon("alpha", env=env)
        self.ok("send", "alpha", "SLEEP 8\nNOREPLY", env=env)
        self.wait_until(lambda: any(m["kind"] == "note" for m in self.msgs()), 15, "timeout note without anyone running pros")

    # 4 ---------------------------------------------------------------- injected but never seen
    def test_4_hook_killed_after_claim_returns_the_message(self):
        self.config({"delivering_recover_s": 1})
        self.summon("alpha")
        s = self.session("alpha")
        self.ok("send", "alpha", "SLEEP 4\nNOREPLY")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "busy")
        self.ok("send", "alpha", "ECHO survivor")
        e = {"PRO_SELF": "alpha", "PRO_SESSION": s, "PRO_TEST_DIE_AFTER_CLAIM": "1"}
        r = self.pro("hook", "stop", input='{"stop_hook_active": false, "x": 1}', env=e)
        self.assertEqual(r.returncode, -signal.SIGKILL)
        m = [x for x in self.msgs() if x["body"] == "ECHO survivor"][0]
        self.assertEqual(m["state"], "queued")  # claimed but never shown, so it must not count as injected
        time.sleep(1.3)
        self.pro("team", env={"PRO_NO_AUTO": ""})
        self.wait_until(lambda: len(self.acts(s)) == 2, 20, "survivor delivered after recovery")
        self.assertTrue(self.acts(s)[1].endswith("dup"))  # it may have been seen: marked possible duplicate

    def test_4b_concurrent_identical_hooks_give_one_output(self):
        self.summon("alpha")
        s = self.session("alpha")
        self.ok("send", "alpha", "SLEEP 5\nNOREPLY")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "busy")
        self.ok("send", "alpha", "ECHO once")
        e = {"PRO_SELF": "alpha", "PRO_SESSION": s}
        outs = parallel([lambda: self.pro("hook", "stop", input='{"stop_hook_active": false}', env=e).stdout for _ in range(5)])
        self.assertEqual(sum('"decision": "block"' in o for o in outs), 1, outs)
        self.assertEqual([m["state"] for m in self.msgs() if m["body"] == "ECHO once"], ["injected"])

    # 5 ---------------------------------------------------------------- global caps
    def test_5_parallel_summons_respect_max_live(self):
        names = ["p%d" % i for i in range(8)]
        for n in names:
            from helpers import make_persona
            make_persona(self.mdir, n)
        mj = json.load(open(os.path.join(self.mdir, ".claude-plugin", "marketplace.json")))
        mj["plugins"] += [{"name": "pro-" + n, "source": "./plugins/pro-" + n, "description": "x", "tags": ["t"]} for n in names]
        json.dump(mj, open(os.path.join(self.mdir, ".claude-plugin", "marketplace.json"), "w"))
        self.config({"max_live": 3})
        rs = parallel([(lambda n=n: self.pro("summon", n)) for n in names])
        codes = sorted(r.returncode for r in rs)
        self.assertEqual(codes, [0, 0, 0, 4, 4, 4, 4, 4], [r.stderr for r in rs])
        self.assertEqual(len(self.sessions_alive()), 3)

    def test_5b_summon_chain_depth_is_capped(self):
        self.config({"max_depth": 2})
        self.summon("alpha")
        self.summon("beta", as_pro="alpha")
        self.summon("delta", as_pro="beta")
        r = self.pro("summon", "gamma", as_pro="delta")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("too deep", r.stderr)

    # 6 ---------------------------------------------------------------- permission dead end on replies
    def test_6_reply_through_stdin_accepts_any_text(self):
        self.summon("alpha")
        task = self.task_of(self.ok("send", "alpha", "NOREPLY").stdout)
        self.wait_until(lambda: any(m["state"] == "injected" for m in self.msgs()), 10, "injected")
        text = "has `ticks`, a 'quote', a \"double\", $dollar; and\nsecond line\n```code```"
        self.ok("reply", task, "-", input=text, as_pro="alpha")
        reply = [m for m in self.msgs() if m["kind"] == "reply"][0]
        self.assertEqual(reply["body"], text)

    def test_6b_prompts_teach_the_heredoc_form(self):
        loader = importlib.machinery.SourceFileLoader("pros_mod", PRO)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("pros_mod", loader))
        loader.exec_module(mod)
        m = mod.new_msg("user", "alpha", "t-1", "task", "user", ["alpha"], "hi", time.time() + 60)
        self.assertIn("<<'EOF'", mod.render(m))
        self.assertIn("<<'EOF'", mod.PROTOCOL)

    # 7 ---------------------------------------------------------------- silent stop / reap, waits overwritten
    def test_7_stop_tells_the_asker_about_an_unanswered_task(self):
        self.summon("alpha")
        res = {}
        t = threading.Thread(target=lambda: res.update(r=self.pro("ask", "alpha", "SLEEP 30\nNOREPLY", "--wait", "40")))
        t.start()
        time.sleep(3)
        t0 = time.time()
        self.ok("stop", "alpha")
        t.join()
        self.assertEqual(res["r"].returncode, 5, res["r"].stdout + res["r"].stderr)
        self.assertIn("stopped", res["r"].stdout)
        self.assertLess(time.time() - t0, 8)

    def test_7b_two_concurrent_asks_keep_both_wait_edges(self):
        for n in ("alpha", "beta", "gamma"):
            self.summon(n)
        res = {}
        tb = threading.Thread(target=lambda: res.update(b=self.pro("ask", "beta", "SLEEP 9\nNOREPLY", "--wait", "9", as_pro="alpha")))
        tb.start()
        time.sleep(1)
        tg = threading.Thread(target=lambda: res.update(g=self.pro("ask", "gamma", "SLEEP 3\nNOREPLY", "--wait", "3", as_pro="alpha")))
        tg.start()
        tg.join()  # the second ask is over; the first edge (alpha -> beta) must still be there
        r = self.pro("send", "alpha", "hi", "--task", "t-fresh", as_pro="beta")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("wait-for cycle", r.stderr)
        tb.join()

    # unverified points ------------------------------------------------------------------------
    def test_u_expired_replies_are_dropped(self):
        self.summon("alpha")
        self.ok("ask", "alpha", "ECHO hello", "--wait", "20")
        self.ok("send", "alpha", "ECHO second")
        self.wait_until(lambda: len([m for m in self.msgs() if m["kind"] == "reply"]) == 2, 10, "two replies")
        d = os.path.join(self.home, "run", "msgs")
        old = [m for m in self.msgs() if m["kind"] == "reply" and m["state"] == "queued"][0]
        old["deadline"] = time.time() - 5
        json.dump(old, open(os.path.join(d, old["id"] + ".json"), "w"))
        self.pro("team", env={"PRO_NO_AUTO": ""})
        got = [m for m in self.msgs() if m["id"] == old["id"]][0]
        self.assertEqual((got["state"], got["fail_reason"]), ("failed", "expired unread"))
        self.assertIn("0 unread", self.ok("inbox").stdout)

    def test_u_two_tasks_in_flight_without_task_id_is_refused(self):
        self.summon("alpha")
        t1 = self.task_of(self.ok("send", "alpha", "NOREPLY").stdout)
        self.wait_until(lambda: sum(m["state"] == "injected" for m in self.msgs()) == 1, 10, "first")
        t2 = self.task_of(self.ok("send", "alpha", "NOREPLY").stdout)
        self.wait_until(lambda: sum(m["state"] == "injected" for m in self.msgs()) == 2, 10, "second")
        self.summon("beta")
        r = self.pro("send", "beta", "x", as_pro="alpha")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("--task", r.stderr)
        self.ok("send", "beta", "x", "--task", t1, as_pro="alpha")

    def test_u_denied_counter_ignores_file_contents(self):
        self.summon("alpha")
        s = self.session("alpha")
        tp = os.path.join(self.tmp, "t.jsonl")
        lines = [
            {"message": {"content": [{"type": "tool_result", "is_error": True, "content": "Permission to use Bash has been denied because Claude Code is running in don't ask mode."}]}},
            {"message": {"content": [{"type": "tool_result", "is_error": True, "content": "File is in a directory that is denied by your permission settings."}]}},
            {"message": {"content": [{"type": "tool_result", "content": "source code: denied by your permission settings and Permission to use Bash x denied"}]}},
        ]
        open(tp, "w").write("\n".join(json.dumps(x) for x in [{"x": 1}] + lines) + "\n")
        p = os.path.join(self.home, "run", "state", s + ".json")
        st = json.load(open(p))
        st["transcript_path"] = tp
        json.dump(st, open(p, "w"))
        den = json.loads(self.ok("team", "--json").stdout)[0]["denied"]
        self.assertEqual(den, {"Bash": 1, "Read": 1})

    def test_u_project_dir_is_readable(self):
        self.summon("alpha")
        info = json.load(open(os.path.join(self.fakedir, self.session("alpha") + ".argv.json")))
        self.assertIn("--add-dir", info["argv"])
        self.assertEqual(info["argv"][info["argv"].index("--add-dir") + 1], self.proj)
        settings = json.loads(info["--settings"])["permissions"]
        self.assertIn("Read", settings["allow"])
        self.assertFalse([d for d in settings["deny"] if self.proj in d])  # nothing under the project is denied


class AutoReply(ProTestCase):
    """The final message of a turn answers the one open task, so replies never depend on a Bash rule."""

    def session(self, name):
        return [s for s in self.sessions_alive() if s.startswith("pro-" + name)][0]

    def test_final_message_becomes_the_reply(self):
        self.summon("alpha")
        r = self.ok("ask", "alpha", "FINAL the multi-line\nanswer with `ticks`\nNOREPLY", "--wait", "20")
        self.assertIn("kind=reply", r.stdout)
        self.assertIn("auto-reply from the final message", r.stdout)
        self.assertIn("the multi-line", r.stdout)
        task = [m for m in self.msgs() if m["kind"] == "task"][0]
        self.assertEqual((task["state"], task["answered_by"]), ("answered", "auto-reply"))
        self.assertTrue([m for m in self.msgs() if m["kind"] == "reply"][0]["auto_reply"])

    def test_explicit_reply_is_not_doubled(self):
        self.summon("alpha")
        self.ok("ask", "alpha", "ECHO explicit", "--wait", "20")
        time.sleep(1)
        self.assertEqual(len([m for m in self.msgs() if m["kind"] == "reply"]), 1)

    def test_no_auto_reply_while_a_delegation_is_pending(self):
        self.summon("alpha")
        self.summon("beta")
        task = self.task_of(self.ok("send", "alpha", "SEND beta NOREPLY\nFINAL delegated, waiting\nNOREPLY").stdout)
        self.wait_until(lambda: any(m["to"] == "beta" for m in self.msgs()), 10, "delegated")
        time.sleep(2)
        self.assertEqual([m for m in self.msgs() if m["kind"] == "reply" and m["task"] == task], [])

    def test_no_auto_reply_when_two_tasks_are_open(self):
        self.summon("alpha")
        self.ok("send", "alpha", "NOREPLY")  # stays open: no final message, no explicit reply
        self.wait_until(lambda: any(m["state"] == "injected" for m in self.msgs()), 10, "first open")
        t2 = self.task_of(self.ok("send", "alpha", "FINAL which task is this?\nNOREPLY").stdout)
        self.wait_until(lambda: len([m for m in self.msgs() if m["state"] == "injected"]) == 2, 10, "second open")
        time.sleep(1.5)
        self.assertEqual([m for m in self.msgs() if m["kind"] == "reply"], [])

    def test_auto_reply_can_be_switched_off(self):
        self.config({"auto_reply": False})
        self.summon("alpha")
        self.ok("send", "alpha", "FINAL x\nNOREPLY")
        time.sleep(2)
        self.assertEqual([m for m in self.msgs() if m["kind"] == "reply"], [])

    def test_auto_reply_reaches_a_pro_asker(self):
        self.summon("alpha")
        self.summon("beta")
        task = self.task_of(self.ok("send", "beta", "FINAL beta says hi\nNOREPLY", as_pro="alpha").stdout)
        self.wait_until(lambda: any(m["kind"] == "reply" and m["to"] == "alpha" and m["state"] in ("injected", "answered") for m in self.msgs()), 20, "reply delivered to alpha")


class AllowPersonaTools(ProTestCase):
    def settings(self):
        s = self.sessions_alive()[0]
        return json.load(open(os.path.join(self.fakedir, s + ".argv.json")))

    def test_default_drops_and_flag_grants_without_changing_mode(self):
        r = self.summon("alpha")
        self.assertIn("ignored-tools=Bash(rm -rf /)", r.stdout)
        self.assertNotIn("Bash(rm -rf /)", json.loads(self.settings()["--settings"])["permissions"]["allow"])
        self.ok("stop", "alpha")
        r = self.summon("alpha", "--allow-persona-tools")
        self.assertNotIn("ignored-tools", r.stdout)
        info = self.settings()
        perms = json.loads(info["--settings"])["permissions"]
        self.assertIn("Bash(rm -rf /)", perms["allow"])
        self.assertEqual(perms["defaultMode"], "dontAsk")
        self.assertIn("dontAsk", info["argv"])
        self.assertTrue(any(d.startswith("Read(~/.ssh") for d in perms["deny"]))  # deny rules still apply
        self.assertIn("--allow-persona-tools", self.ok("show", "alpha").stdout)


if __name__ == "__main__":
    unittest.main()
