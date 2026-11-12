#!/usr/bin/env python3
"""Audit step A: record what `pros` persists today, scrubbed, as fixtures for the audit work (docs/spec/audit.md).

Two controlled runs with the stub claude (tests/fake_claude.py), then one synthetic usage set:
  ask_delegation  the user asks system-designer (short wait -> pending); it delegates one branch to
                  reliability-reviewer, then replies after its deadline (late reply).
  resume_fork     alpha: fresh session, stop, resume, stop, fork; one answered task in each run.
  usage           Claude-style transcripts for the ask_delegation sessions: root task 140 tokens,
                  child task 80, 30 more on the root session only; a forked copy and split rows
                  repeat exchanges that must be counted once.

Regenerate (about 60 s; tmux + the stub, never a real claude):
  python3 tests/audit_fixtures.py [out-dir]     default out-dir: tests/fixtures/audit
Scrubbing: temp paths -> $PRO_HOME/$PROJECT/$HOME/$PROS, ids -> stable placeholders, times rebased to T0.
"""
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import helpers  # noqa: E402

OUT = os.path.join(HERE, "fixtures", "audit")
T0 = 1800000000.0
CANARIES = ["CANARY-ROOT-BODY-41", "CANARY-CHILD-BODY-42", "CANARY-ROOT-REPLY-43", "CANARY-TOOL-ARG-44",
            "CANARY-TOOL-RESULT-45", "CANARY-PATH-46", "CANARY-RESUME-BODY-47"]
TIME_KEYS = {"t", "created", "deadline", "injected_at", "answered_at", "read_at", "started", "last_activity",
             "updated", "expires", "since", "run_started", "start", "added_at"}


class Runner(helpers.ProTestCase):
    names = ("system-designer", "reliability-reviewer", "alpha")

    def runTest(self):  # unittest needs one; the scenarios are called directly
        pass

    def cmd(self, log, *args, **kw):
        t = time.time()
        r = self.pro(*args, **kw)
        log.append({"t": t, "argv": list(args), "exit": r.returncode, "out": (r.stdout + r.stderr).strip()[-600:]})
        return r


def ask_delegation(r):
    """Root ask with one delegated branch, a pending short wait, a missed deadline and a late reply."""
    r.config({"deliver_deadline_s": 36})
    log = []
    r.cmd(log, "summon", "system-designer")
    r.cmd(log, "summon", "reliability-reviewer")
    body = "\n".join(["Review the architecture. %s" % CANARIES[0],
                      "ASK reliability-reviewer ECHO %s branch reviewed" % CANARIES[1],
                      "SLEEP 40", "ECHO %s review done" % CANARIES[2]])
    r.cmd(log, "ask", "system-designer", body, "--task", "fx-root", "--wait", "2")
    r.cmd(log, "wait", "fx-root", "--timeout", "60")
    r.wait_until(lambda: any(m["kind"] == "reply" and m["task"] == "fx-root" and m["from"] == "system-designer"
                             for m in r.msgs()), 40, "late root reply")
    r.cmd(log, "wait", "fx-root", "--timeout", "3")
    r.cmd(log, "inbox")
    r.cmd(log, "stop", "--all")
    return log


def resume_fork(r):
    """alpha: fresh -> stop -> resume -> stop -> fork, one answered task per run."""
    log = []
    for i, extra in enumerate(([], [], ["--fork"])):
        r.cmd(log, "summon", "alpha", *extra)
        r.cmd(log, "ask", "alpha", "ECHO run %d %s" % (i, CANARIES[6]), "--task", "fx-run-%d" % i, "--wait", "15")
        r.cmd(log, "stop", "alpha")
    return log


def collect(r):
    """Every record pros persisted under PRO_HOME, as {relative path: parsed content}."""
    out = {}
    for base, _, files in os.walk(r.home):
        for f in sorted(files):
            p = os.path.join(base, f)
            rel = os.path.relpath(p, r.home)
            if rel.startswith(("home", "run/locks", "run/prompts", "run/settings")) or f.endswith((".lock", ".stamp")):
                continue
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            if f.endswith(".jsonl") or f.endswith(".log"):
                out[rel] = [json.loads(x) for x in text.splitlines() if x.strip().startswith("{")]
            elif f.endswith(".json"):
                out[rel] = json.loads(text) if text.strip() else None
            else:
                out[rel] = text
    return out


class Scrub:
    def __init__(self, r, t_start):
        self.paths = [(os.path.realpath(r.home), "$PRO_HOME"), (r.home, "$PRO_HOME"), (os.path.realpath(r.proj), "$PROJECT"),
                      (r.proj, "$PROJECT"), (os.path.realpath(r.fakehome), "$HOME"), (r.fakehome, "$HOME"),
                      (os.path.realpath(helpers.PRO), "$PROS"), (os.path.realpath(r.tmp), "$TMP"), (r.tmp, "$TMP")]
        self.t_start, self.ids = t_start, {}

    def _id(self, kind, v):
        k = (kind, v)
        if k not in self.ids:
            n = sum(1 for x in self.ids if x[0] == kind) + 1
            self.ids[k] = {"uuid": "00000000-0000-4000-8000-%012d" % n, "m": "m-%010d" % n, "t": "t-%08d" % n, "p": "%06d" % n}[kind]
        return self.ids[k]

    def text(self, s):
        for a, b in self.paths:
            s = s.replace(a, b)
        s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", lambda m: self._id("uuid", m.group(0)), s)
        s = re.sub(r"\bm-[0-9a-f]{10}\b", lambda m: self._id("m", m.group(0)), s)
        s = re.sub(r"\bt-[0-9a-f]{8}\b", lambda m: self._id("t", m.group(0)), s)
        s = re.sub(r"(pro-[a-z0-9-]+--)([0-9a-f]{6})\b", lambda m: m.group(1) + self._id("p", m.group(2)), s)
        s = re.sub(r"\bpro-[0-9a-f]{6}\b", "pro-buffer", s)
        return re.sub(r"secs=[0-9.]+", "secs=N", s)

    def time(self, v):
        return round(T0 + (v - self.t_start), 2)

    def __call__(self, x, key=None):
        if isinstance(x, dict):
            return {self.text(k): self(v, k) for k, v in x.items()}
        if isinstance(x, list):
            return [self(v) for v in x]
        if isinstance(x, str):
            return self.text(x)
        if isinstance(x, (int, float)) and not isinstance(x, bool) and key in TIME_KEYS and x > 1e9:
            return self.time(x)
        return x


def run_scenario(fn):
    r = Runner()
    r.setUp()
    try:
        t_start = time.time()
        log = fn(r)
        recs = collect(r)
        sc = Scrub(r, t_start)
        return sc({"commands": log, "records": recs})
    finally:
        r.doCleanups()


def ts(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % int(round((t % 1) * 1000) % 1000)


def usage_rows(sid, t, req, mid, usage, blocks, parent):
    """One assistant exchange; Claude Code writes one row per content block, each repeating the usage."""
    rows = []
    for i, b in enumerate(blocks):
        u = "%s-r%d" % (mid, i)
        rows.append({"type": "assistant", "uuid": u, "parentUuid": parent, "sessionId": sid, "timestamp": ts(t + i * 0.1),
                     "requestId": req, "isSidechain": False, "version": "2.1.322", "cwd": "$PROJECT",
                     "message": {"id": mid, "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
                                 "content": [b], "usage": dict(usage), "stop_reason": None}})
        parent = u
    return rows, parent


def user_row(sid, t, text, parent, uid):
    return {"type": "user", "uuid": uid, "parentUuid": parent, "sessionId": sid, "timestamp": ts(t), "isSidechain": False,
            "version": "2.1.322", "cwd": "$PROJECT", "message": {"role": "user", "content": text}}


def usage_set(deleg):
    """Synthetic transcripts keyed to the ask_delegation ids; the expected totals are in expected.json."""
    recs = deleg["records"]
    msgs = [m for k, m in recs.items() if k.startswith("run/msgs/")]
    root = next(m for m in msgs if m["task"] == "fx-root" and m["kind"] == "task")
    child = next(m for m in msgs if m["kind"] == "task" and m.get("parent") == root["id"])
    hist = {n: recs["history/%s.jsonl" % n] for n in ("system-designer", "reliability-reviewer")}
    sid_root = next(x["id"] for x in hist["system-designer"] if x["ev"] == "start")
    sid_child = next(x["id"] for x in hist["reliability-reviewer"] if x["ev"] == "start")
    sid_fork = "00000000-0000-4000-8000-0000000000f1"
    hdr = "[pro-msg id=%s from=%s to=%s task=%s kind=task reply-to=%s chain=%s]"
    u_e0 = {"input_tokens": 20, "output_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    u_e1 = {"input_tokens": 50, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 30}
    u_e2 = {"input_tokens": 10, "output_tokens": 30, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    u_e3 = {"input_tokens": 40, "output_tokens": 20, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 0}
    tool = {"type": "tool_use", "id": "toolu_fx1", "name": "Read", "input": {"file_path": "/srv/%s/design.md" % CANARIES[5],
                                                                             "note": CANARIES[3]}}
    t_in = root.get("injected_at", root["created"])
    rows_root, rows_fork = [], []
    p = None
    rows_root.append(user_row(sid_root, t_in - 20, "Session start check (no task header).", p, "u-r0"))
    e0, p = usage_rows(sid_root, t_in - 19, "req_fx0", "msg_fx0", u_e0, [{"type": "text", "text": "Ready."}], "u-r0")
    rows_root += e0
    prompt = hdr % (root["id"], root["from"], root["to"], root["task"], root["reply_to"], ">".join(root["chain"])) + "\n" + root["body"]
    rows_root.append(user_row(sid_root, t_in, prompt, p, "u-r1"))
    e1, p = usage_rows(sid_root, t_in + 1, "req_fx1", "msg_fx1", u_e1, [{"type": "thinking", "thinking": "plan"}, tool], "u-r1")
    rows_root += e1
    rows_root.append(user_row(sid_root, t_in + 2, [{"type": "tool_result", "tool_use_id": "toolu_fx1",
                                                     "content": "file text %s" % CANARIES[4]}], p, "u-r2"))
    e2, p = usage_rows(sid_root, t_in + 41, "req_fx2", "msg_fx2", u_e2, [{"type": "text", "text": "%s review done" % CANARIES[2]}], "u-r2")
    rows_root += e2
    rows_fork = [dict(x, sessionId=sid_fork) for x in rows_root[:4]]  # a fork copies earlier exchanges into a new file
    c_in = child.get("injected_at", child["created"])
    cprompt = hdr % (child["id"], child["from"], child["to"], child["task"], child["reply_to"], ">".join(child["chain"])) + "\n" + child["body"]
    rows_child = [user_row(sid_child, c_in, cprompt, None, "u-c1")]
    e3, _ = usage_rows(sid_child, c_in + 1, "req_fx3", "msg_fx3", u_e3, [{"type": "text", "text": "branch reviewed"}], "u-c1")
    rows_child += e3
    files = {"transcripts/%s.jsonl" % sid_root: rows_root, "transcripts/%s.jsonl" % sid_child: rows_child,
             "transcripts/%s.jsonl" % sid_fork: rows_fork}
    raw = sum(1 for rows in files.values() for x in rows if x["type"] == "assistant")
    expected = {"root_task": root["task"], "root_message": root["id"], "child_task": child["task"], "child_message": child["id"],
                "root_session": sid_root, "child_session": sid_child, "fork_copy_session": sid_fork,
                "tokens": {"root_task_own": 140, "child_task_own": 80, "trace_total": 220, "root_session_only": 30,
                           "all_unique": 250},
                "exchanges": {"unique": 4, "raw_assistant_rows": raw, "dedup_key": "message.id + requestId"},
                "token_sum_rule": "input + output + cache_creation_input + cache_read_input",
                "canaries": CANARIES}
    return files, expected


def write(out, name, obj):
    p = os.path.join(out, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        if name.endswith(".jsonl"):
            f.write("".join(json.dumps(x, sort_keys=True) + "\n" for x in obj))
        else:
            json.dump(obj, f, indent=1, sort_keys=True)
            f.write("\n")


def schema_of(scenarios):
    """Observed keys per record family: the drift test compares a fresh run against this."""
    fam = {}
    for sc in scenarios:
        for rel, v in sc["records"].items():
            name = re.sub(r"/[^/]+$", "/*", rel) if re.match(r"(history|run/(msgs|state|sessions|inbox|queue))/", rel) else rel
            rows = v if isinstance(v, list) else [v]
            for x in rows:
                if isinstance(x, dict):
                    fam.setdefault(name, set()).update(x.keys())
    return {k: sorted(v) for k, v in sorted(fam.items())}


def main(out=OUT):
    deleg = run_scenario(ask_delegation)
    resume = run_scenario(resume_fork)
    write(out, "ask_delegation.json", deleg)
    write(out, "resume_fork.json", resume)
    files, expected = usage_set(deleg)
    for name, rows in files.items():
        write(out, "usage/" + name, rows)
    write(out, "usage/expected.json", expected)
    write(out, "schema.json", schema_of([deleg, resume]))
    print("wrote fixtures to %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else OUT))
