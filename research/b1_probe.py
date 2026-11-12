#!/usr/bin/env python3
"""Audit spike B1: one controlled run with the stub claude (tests/fake_claude.py), never a real claude.

Scenario on one professional (alpha):
  1. a long task, then a second task sent while busy: the second is delivered by the stop hook
  2. a task whose stop-hook delivery dies after the claim: recovered and redelivered, marked dup
  3. two tasks in flight at once in one session (the first does not reply)
  4. stop, resume (same Claude session id), stop, fork (new id)
Then it joins every task message to a session and run from the persisted records alone and compares
the result with the stub's own delivery log (ACT lines = what the session actually received).
Prints JSON; research/B1.md quotes it.  Runtime about 60 s.   python3 research/b1_probe.py
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tests"))
sys.path.insert(0, HERE)
import helpers  # noqa: E402
import audit_reader  # noqa: E402


class Probe(helpers.ProTestCase):
    names = ("alpha",)

    def runTest(self):
        pass

    def scenario(self):
        out = {}
        self.config({"delivering_recover_s": 1})
        self.summon("alpha")
        s = "pro-alpha--" + [x for x in self.sessions_alive() if x.startswith("pro-alpha--")][0].split("--")[1]
        self.ok("send", "alpha", "SLEEP 3\nECHO first", "--task", "b1-first")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "first busy")
        self.ok("send", "alpha", "ECHO second", "--task", "b1-second")
        self.wait_until(lambda: len(self.acts(s)) == 2, 20, "second via stop hook")
        self.wait_until(lambda: all(m["state"] == "answered" for m in self.msgs() if m["kind"] == "task"), 20, "answers")
        self.ok("send", "alpha", "SLEEP 4\nNOREPLY", "--task", "b1-busy")
        self.wait_until(lambda: len(self.acts(s)) == 3, 10, "busy")
        self.ok("send", "alpha", "ECHO survivor", "--task", "b1-dup")
        e = {"PRO_SELF": "alpha", "PRO_SESSION": s, "PRO_TEST_DIE_AFTER_CLAIM": "1"}
        out["killed_hook_exit"] = self.pro("hook", "stop", input='{"stop_hook_active": false, "x": 1}', env=e).returncode
        time.sleep(1.3)
        self.pro("team", env={"PRO_NO_AUTO": ""})
        self.wait_until(lambda: len(self.acts(s)) >= 4, 30, "dup redelivered")
        time.sleep(2)
        out["acts"] = self.acts(s)
        self.ok("stop", "alpha")
        self.summon("alpha")
        self.ok("ask", "alpha", "ECHO resumed", "--task", "b1-resumed", "--wait", "15")
        self.ok("stop", "alpha")
        self.summon("alpha", "--fork")
        self.ok("ask", "alpha", "ECHO forked", "--task", "b1-forked", "--wait", "15")
        out["acts_all"] = self.acts(s)
        st = json.load(open(os.path.join(self.home, "run", "state", s + ".json")))
        out["state_file_session"] = st.get("claude_session_id")
        self.ok("stop", "alpha")
        return out


def main():
    p = Probe()
    p.setUp()
    try:
        out = p.scenario()
        msgs, hist = audit_reader.load_records(pro_home=p.home)
        res = audit_reader.join(msgs, hist, None)
        sids = {}
        for r in audit_reader.runs_from_history(hist):
            sids.setdefault(r["sid"], "S%d" % (len(sids) + 1))
        out["runs"] = [{"sid": sids[r["sid"]], "mode": r["mode"], "closed": r["end"] is not None}
                       for r in audit_reader.runs_from_history(hist)]
        out["state_file_session"] = sids.get(out["state_file_session"], "other")
        acts = {}
        for line in out.pop("acts_all"):
            mid = line.split()[1]
            acts.setdefault(mid, []).append("dup" if line.endswith("dup") else "act")
        out.pop("acts")
        rows = []
        for mid, a in sorted(res["assignments"].items(), key=lambda kv: msgs[kv[0]]["created"]):
            m = msgs[mid]
            rows.append({"task": a["task"], "via": a["via"], "state": a["state"], "dup_flag": a["dup"],
                         "attempts": m.get("attempts", 0), "stub_deliveries": acts.get(mid, []),
                         "window_runs": [sids[x] for x in a["window_sessions"]], "confidence": a["confidence"],
                         "answered_by": m.get("answered_by"), "overdue": bool(m.get("overdue"))})
        out["tasks"] = rows
        live = [m for m in msgs.values() if m["kind"] == "task"]
        ov = 0
        for m in live:
            if any(x is not m and x.get("injected_at", 9e12) < m.get("injected_at", 0) < x.get("answered_at", 9e12)
                   for x in live):
                ov += 1
        out["overlapping_deliveries"] = ov
        json.dump(out, sys.stdout, indent=1, sort_keys=True)
        print()
    finally:
        p.doCleanups()
    return 0


if __name__ == "__main__":
    sys.exit(main())
