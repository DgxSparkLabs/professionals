#!/usr/bin/env python3
"""Real-claude soak test for `pros` (spec 026 SC-003).  NOT part of the default test run: it uses
real Claude Code sessions (model sonnet) and costs tokens.  Run it by hand:

    python3 tests/soak_real.py [--tasks 50] [--limit-min 25] [--summary out.md]

What it does: private tmux socket (PRO_TMUX_SOCKET=pro-soak-<pid>), temp PRO_HOME, PRO_MARKETPLACE=repo.
Summons two professionals (global scope), sends N tiny tasks ("Reply with exactly: ACK <n>") interleaved,
without --wait, many while the target is busy.  Halfway it kills one professional's claude process while it
holds injected/queued work, then re-summons it (the resend path).  Answers are collected with `pros wait`.
Asserts: every n answered exactly once, no duplicate acted on, no wait past its deadline without a
stuck/timeout notice.  Prints latency p50/p95/max, via=paste vs via=stop-hook, auto-reply count.
Hard limit: --limit-min (default 25); the private tmux server is always killed.
Needs: claude (logged in), tmux, python3.  Exit 0 = all assertions hold.
"""
import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PRO = os.path.join(REPO, "plugins", "professionals", "bin", "pros")
NAMES = ["reliability-reviewer", "docs-designer"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=50)
    ap.add_argument("--limit-min", type=float, default=25)
    ap.add_argument("--summary", default="")
    a = ap.parse_args()
    t0 = time.time()
    hard_end = t0 + a.limit_min * 60
    tmp = tempfile.mkdtemp(prefix="pro-soak-")
    sock = "pro-soak-%d" % os.getpid()
    home = os.path.join(tmp, "prohome")
    proj = os.path.join(tmp, "proj")
    os.makedirs(home)
    os.makedirs(proj)
    env = dict(os.environ, PRO_HOME=home, PRO_TMUX_SOCKET=sock, PRO_MARKETPLACE=REPO)
    for k in list(env):
        if k in ("PRO_SELF", "PRO_SESSION", "TMUX", "TMUX_PANE", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE"):
            env.pop(k)
    log_lines = []

    def log(s):
        line = "[%5.0fs] %s" % (time.time() - t0, s)
        log_lines.append(line)
        print(line, flush=True)

    def pros(*args, timeout=150):
        return subprocess.run([sys.executable, PRO, *args], capture_output=True, text=True, env=env, cwd=proj, timeout=timeout)

    failures = []
    stats = {}
    try:
        with open(os.path.join(home, "config.json"), "w") as f:
            json.dump({"max_live": 6}, f)
        for n in NAMES:
            r = pros("summon", n, "--scope", "global", "--model", "sonnet")
            log("summon %s -> %d %s" % (n, r.returncode, (r.stdout + r.stderr).strip()[:120]))
            if r.returncode != 0:
                failures.append("summon %s failed: %s" % (n, (r.stdout + r.stderr).strip()[:200]))
        if failures:
            raise SystemExit

        tasks = {}          # n -> task id (all successful sends, first one)
        sent_at = {}
        resend_failures = []
        restart_done = False
        restart_info = {}

        def send(n, target):
            r = pros("send", target, "Reply with exactly: ACK %d" % n)
            m = re.search(r"task=(t-\w+)", r.stdout)
            if r.returncode == 0 and m:
                tasks.setdefault(n, []).append((m.group(1), target))
                sent_at[m.group(1)] = time.time()
                return True
            resend_failures.append((n, target, r.returncode, (r.stdout + r.stderr).strip()[:150]))
            return False

        def kill_pane(name):
            r = pros("team", "--json")
            rows = json.loads(r.stdout) if r.stdout.strip().startswith("[") else []
            sess = [x["session"] for x in rows if x["name"] == name]
            if not sess:
                return None
            p = subprocess.run(["tmux", "-L", sock, "list-panes", "-t", sess[0], "-F", "#{pane_pid}"], capture_output=True, text=True)
            pid = p.stdout.split()[0]
            subprocess.run(["pkill", "-9", "-P", pid], capture_output=True)
            subprocess.run(["kill", "-9", pid], capture_output=True)
            return sess[0]

        pending_retry = []
        half = a.tasks // 2
        for n in range(1, a.tasks + 1):
            if time.time() > hard_end - 8 * 60:
                failures.append("ran out of time while sending (n=%d)" % n)
                break
            target = NAMES[n % 2]
            if not send(n, target):
                pending_retry.append((n, target))
            time.sleep(0.4)
            if n == half and not restart_done:
                victim = NAMES[0]
                # keep sending to the victim until it holds one injected (unanswered) and one queued task, then kill it
                def victim_states():
                    d = os.path.join(home, "run", "msgs")
                    out = []
                    for f in os.listdir(d):
                        try:
                            m = json.load(open(os.path.join(d, f)))
                        except Exception:
                            continue
                        if m["kind"] == "task" and m["to"] == victim and m["state"] != "answered":
                            out.append(m["state"])
                    return out
                k = 0
                while time.time() < hard_end - 8 * 60:
                    k += 1
                    send(1000 + k, victim)
                    st = victim_states()
                    if "injected" in st and "queued" in st:
                        break
                    time.sleep(0.5)
                restart_info["states_before_kill"] = victim_states()
                sess = kill_pane(victim)
                time.sleep(0.5)
                gone = subprocess.run(["tmux", "-L", sock, "has-session", "-t", sess or "x"], capture_output=True).returncode != 0
                restart_info["claude_gone_after_kill"] = gone
                restart_done = True
                restart_info.update({"victim": victim, "at_s": round(time.time() - t0)})
                log("killed claude of %s (session %s) with work in flight" % (victim, sess))
                time.sleep(3)
                r = pros("summon", victim, "--scope", "global", "--model", "sonnet")
                log("re-summon %s -> %d %s" % (victim, r.returncode, (r.stdout + r.stderr).strip()[:160].replace("\n", " | ")))
                if r.returncode != 0:
                    failures.append("re-summon failed: " + (r.stdout + r.stderr).strip()[:200])
        # tasks that could not be sent (target gone) are retried once the target is back
        for n, target in pending_retry:
            for attempt in range(3):
                if send(n, target):
                    break
                time.sleep(3)
        log("sent %d distinct n; send failures seen: %d" % (len(tasks), len(resend_failures)))

        # collect: pros wait for every task id, in parallel, until all answered or time is up
        all_ids = [(n, t) for n, lst in tasks.items() for (t, _) in lst]
        results = {}
        bad_waits = []
        lock = threading.Lock()

        def collect(item):
            n, t = item
            while time.time() < hard_end - 30:
                r = pros("wait", t, "--timeout", "40", timeout=90)
                if r.returncode in (0, 5):
                    with lock:
                        results[t] = (r.returncode, r.stdout)
                    return
                if r.returncode != 3:
                    with lock:
                        results[t] = (r.returncode, r.stdout + r.stderr)
                    return
                out = r.stdout
                # pending after the task deadline must have been turned into failed/overdue + note
                with lock:
                    results[t] = (3, out)
                if time.time() - sent_at[t] > 640:
                    return
            return

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(collect, all_ids))

        # analysis from the message store
        msgdir = os.path.join(home, "run", "msgs")
        msgs = []
        for f in os.listdir(msgdir):
            try:
                msgs.append(json.load(open(os.path.join(msgdir, f))))
            except Exception:
                pass
        by_task = {}
        for m in msgs:
            by_task.setdefault(m["task"], []).append(m)
        ack_count = {}
        lat = []
        for m in msgs:
            if m["kind"] == "reply" and m["to"] == "user":
                for k in re.findall(r"ACK (\d+)", m["body"]):
                    ack_count[int(k)] = ack_count.get(int(k), 0) + 1
                task = [x for x in by_task[m["task"]] if x["kind"] == "task"]
                if task:
                    lat.append(m["created"] - task[0]["created"])
        for n in range(1, a.tasks + 1):
            c = ack_count.get(n, 0)
            if c != 1:
                failures.append("n=%d answered %d times (task ids %s)" % (n, c, [t for t, _ in tasks.get(n, [])]))
        dups = sum(1 for c in ack_count.values() if c > 1)
        tmsgs = [m for m in msgs if m["kind"] == "task"]
        via = {}
        for m in tmsgs:
            via[m.get("via") or "none"] = via.get(m.get("via") or "none", 0) + 1
        autor = sum(1 for m in msgs if m.get("auto_reply"))
        dupflag = sum(1 for m in tmsgs if m.get("dup"))
        notes = [m for m in msgs if m["kind"] == "note" and m["to"] == "user"]
        now = time.time()
        for m in tmsgs:
            if m["from"] != "user" or m["state"] == "answered":
                continue
            if m["state"] not in ("failed", "overdue") and now > m.get("deadline", now + 1):
                failures.append("task %s past its deadline, state %s, no timeout state" % (m["task"], m["state"]))
            elif m["state"] in ("failed", "overdue") and not any(m["task"] in x["body"] or x["task"] == m["task"] for x in notes):
                failures.append("task %s %s but no notice reached the user" % (m["task"], m["state"]))
        # 1000+ tasks are the in-flight load for the restart; they are not counted in the 50
        unanswered = [m["task"] for m in tmsgs if m["state"] != "answered" and m["from"] == "user"]
        waits_pending = [t for t, (c, _) in results.items() if c == 3]
        for t in waits_pending:
            st = [m for m in tmsgs if m["task"] == t]
            if st and st[0]["state"] != "answered" and now < st[0].get("deadline", 0):
                failures.append("wait for %s still pending at the time limit, deadline not yet reached (state %s)" % (t, st[0]["state"]))
        lat.sort()
        pct = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))] if lat else float("nan")
        stats = {
            "tasks_requested": a.tasks, "distinct_n_answered_once": sum(1 for n in range(1, a.tasks + 1) if ack_count.get(n) == 1),
            "duplicates_answered": dups, "task_messages_total": len(tmsgs), "dup_flagged_resends": dupflag,
            "via": via, "auto_replies": autor, "unanswered_task_msgs": len(unanswered),
            "latency_s": {"n": len(lat), "p50": round(pct(.5), 1), "p95": round(pct(.95), 1), "max": round(lat[-1], 1) if lat else None},
            "restart": restart_info, "send_failures": [list(x) for x in resend_failures],
            "elapsed_s": round(time.time() - t0),
        }
    except SystemExit:
        pass
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)
        elapsed = time.time() - t0
        out = {"stats": stats, "failures": failures, "elapsed_s": round(elapsed)}
        text = json.dumps(out, indent=1)
        text = text.replace(tmp, "<tmp>").replace(os.path.expanduser("~"), "~")
        print(text)
        if a.summary:
            with open(a.summary, "w") as f:
                f.write(text + "\n")
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
