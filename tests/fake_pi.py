#!/usr/bin/env python3
"""Stand-in for `pi --mode rpc`, good enough to drive the pi-rpc harness of `pros` in tests.

Speaks the JSONL RPC subset pros uses: get_state, prompt, follow_up, extension_ui_response; emits agent_start,
message_update (text_delta), tool_execution_start/end, agent_end, agent_settled.  Obeys commands in a prompt, one per line:
  ECHO <text>        text of the final message
  SLEEP <n>          stay busy n seconds
  SEND <who> <text>  pros send (async), through Bash like a real tool call
  ASK <who> <text>   pros ask --wait 5, appends its output to the final message
  READ <path>        a read tool call; the pros guard (-e file) is applied the way Pi applies it, so a protected path is blocked
  DIALOG             an extension asks a confirm dialog first (pros must cancel it)
  CRASH              exit abruptly
Env switch: FAKE_PI_FAIL=1 exits at start with an error on stderr.
Logs go to $FAKE_DIR/<--name>.pilog: START <argv json>, PROMPT <pro-msg id>, DIALOG <answer json>, DENIED <path>.
"""
import json
import os
import re
import subprocess
import sys
import time

FAKE_DIR = os.environ.get("FAKE_DIR", "/tmp")
PRO = os.environ.get("FAKE_PRO", "pros")
ARGV = sys.argv[1:]


def opt(flag, default=""):
    return ARGV[ARGV.index(flag) + 1] if flag in ARGV and ARGV.index(flag) + 1 < len(ARGV) else default


NAME = opt("--name", "pi")
SID = opt("--session-id", "none")
MODEL = opt("--model", "fake-default")


def log(line):
    with open(os.path.join(FAKE_DIR, NAME + ".pilog"), "a") as f:
        f.write(line + "\n")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def guard_lists():
    path = opt("-e")
    if not path or not os.path.exists(path):
        return [], []
    text = open(path).read()
    ab = re.search(r"const ABS = (\[.*?\]);", text)
    rel = re.search(r"const REL = (\[.*?\]);", text)
    return json.loads(ab.group(1)) if ab else [], json.loads(rel.group(1)) if rel else []


def session_file():
    d = opt("--session-dir")
    if not d:
        return None
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        if f.endswith("_%s.jsonl" % SID):
            return os.path.join(d, f)
    p = os.path.join(d, "%s_%s.jsonl" % (time.strftime("%Y-%m-%dT%H-%M-%S"), SID))
    with open(p, "w") as f:
        f.write(json.dumps({"type": "session", "version": 3, "id": SID, "cwd": os.getcwd(), "fork": opt("--fork") or None}) + "\n")
    return p


def run_prompt(text, sfile, read_line):
    out({"type": "agent_start"})
    m = re.search(r"\[pro-msg id=(m-[0-9a-f]+)", text)
    if m:
        log("PROMPT %s%s" % (m.group(1), " dup" if "possible duplicate" in text else ""))
    else:
        log("HUMAN %s" % text[:60])
    parts = []
    ab, rel = guard_lists()
    for n, line in enumerate(text.splitlines()):
        cmd, _, rest = line.partition(" ")
        tid = "call-%d" % n
        if cmd == "ECHO":
            parts.append(rest)
        elif cmd == "SLEEP":
            time.sleep(float(rest))
        elif cmd in ("SEND", "ASK"):
            who, _, t = rest.partition(" ")
            argv = [sys.executable, PRO, "send", who, t] if cmd == "SEND" else [sys.executable, PRO, "ask", who, t, "--wait", "5"]
            out({"type": "tool_execution_start", "toolCallId": tid, "toolName": "bash", "args": {"command": "pros " + cmd.lower()}})
            r = subprocess.run(argv, capture_output=True, text=True)
            out({"type": "tool_execution_end", "toolCallId": tid, "toolName": "bash", "result": {"content": [{"type": "text", "text": r.stdout}]}, "isError": r.returncode != 0})
            if cmd == "ASK":
                parts.append("ask-exit=%d %s" % (r.returncode, (r.stdout + r.stderr).replace("\n", " ")[:200]))
        elif cmd == "READ":
            out({"type": "tool_execution_start", "toolCallId": tid, "toolName": "read", "args": {"path": rest}})
            blob = json.dumps({"path": rest})
            hit = next((p for p in ab if p in blob), None) or next((p for p in rel if re.search(r"(^|[\s/(=\"'])%s($|[\s/)\"'*])" % re.escape(p), blob)), None)
            if hit:
                log("DENIED %s" % rest)
                res = {"content": [{"type": "text", "text": "pros-deny: read touches the protected path %s" % hit}]}
                out({"type": "tool_execution_end", "toolCallId": tid, "toolName": "read", "result": res, "isError": True})
                parts.append("read %s: blocked" % rest)
            else:
                out({"type": "tool_execution_end", "toolCallId": tid, "toolName": "read", "result": {"content": [{"type": "text", "text": "ok"}]}, "isError": False})
                parts.append("read %s: ok" % rest)
        elif cmd == "DIALOG":
            out({"type": "extension_ui_request", "id": "ui-1", "method": "confirm", "title": "Allow?", "message": "fake"})
            ans = read_line()
            log("DIALOG %s" % ans.strip())
        elif cmd == "CRASH":
            os._exit(9)
    final = "fake-pi(model=%s thinking=%s) %s" % (MODEL, opt("--thinking", "-"), " | ".join(parts) or "done")
    out({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": final}})
    msgs = [{"role": "user", "content": [{"type": "text", "text": text}]}, {"role": "assistant", "content": [{"type": "text", "text": final}]}]
    if sfile:
        with open(sfile, "a") as f:
            for x in msgs:
                f.write(json.dumps({"type": "message", "message": x}) + "\n")
    out({"type": "agent_end", "messages": msgs, "willRetry": False})
    out({"type": "agent_settled", "aborted": False})


def main():
    log("START " + json.dumps(ARGV))
    if os.environ.get("FAKE_PI_FAIL"):
        sys.stderr.write("fake pi: cannot start (FAKE_PI_FAIL)\n")
        sys.exit(3)
    sfile = session_file()
    queue = []

    def read_line():
        return sys.stdin.readline()
    while True:
        line = queue.pop(0) if queue else sys.stdin.readline()
        if not line:
            break
        try:
            cmd = json.loads(line)
        except ValueError:
            out({"type": "response", "command": "parse", "success": False, "error": "bad json"})
            continue
        t, i = cmd.get("type"), cmd.get("id")
        if t == "get_state":
            out({"id": i, "type": "response", "command": t, "success": True, "data": {"model": {"id": MODEL}, "sessionId": SID}})
        elif t in ("prompt", "follow_up"):
            out({"id": i, "type": "response", "command": t, "success": True, "data": {"disposition": "started"}})
            run_prompt(cmd.get("message", ""), sfile, read_line)
        else:
            out({"id": i, "type": "response", "command": t, "success": True, "data": {}})


if __name__ == "__main__":
    main()
