#!/usr/bin/env python3
"""Stand-in for the `claude` CLI, good enough to drive `pros` in tests.

Subcommands: --version, agents --json, plugin validate <dir>.
Session mode (what `pro summon` starts inside tmux): draws a prompt box like the
real TUI, accepts bracketed pastes + Enter, fires the same hooks (via `pros hook`)
and obeys commands found in the message body, one per line:
  ECHO <text>        reply text
  SLEEP <n>          stay busy n seconds
  SEND <who> <text>  pro send (async)
  ASK <who> <text>   pro ask --wait 5, appends its output to the reply
  NOREPLY            do not call pro reply
  CRASH              exit abruptly
Env switches: FAKE_TRUST=1 trust dialog first, FAKE_FAIL=1 exit at once,
FAKE_LIMIT=1 usage-limit banner, FAKE_NOHOOKS=1 never call hooks.
Logs go to $FAKE_DIR/<session>.log: START, ACT <id> [dup], MIDTURN <text>.
"""
import json
import os
import select
import subprocess
import sys
import termios
import time
import tty

FAKE_DIR = os.environ.get("FAKE_DIR", "/tmp")
PRO = os.environ.get("FAKE_PRO", "pros")
ARGV = sys.argv[1:]


def main():
    if ARGV[:1] == ["--version"]:
        print("2.1.322 (Claude Code)")
        return 0
    if ARGV[:2] == ["agents", "--json"]:
        out = []
        for f in os.listdir(FAKE_DIR) if os.path.isdir(FAKE_DIR) else []:
            if f.endswith(".status"):
                out.append({"name": f[:-7], "status": open(os.path.join(FAKE_DIR, f)).read().strip()})
        print(json.dumps(out))
        return 0
    if ARGV[:2] == ["plugin", "validate"]:
        if "claude-invalid" in open(os.path.join(ARGV[2], ".claude-plugin", "plugin.json")).read():
            print("invalid manifest")
            return 1
        print("ok")
        return 0
    return Session().run()


class Session:
    def __init__(self):
        a = ARGV
        self.name = a[a.index("-n") + 1] if "-n" in a else "fake"
        self.logf = os.path.join(FAKE_DIR, self.name + ".log")
        self.statusf = os.path.join(FAKE_DIR, self.name + ".status")
        self.buf, self.out, self.working, self.paste = "", [], False, None
        self.midturn = []
        self.sid = "fake-" + self.name
        info = {"argv": a, "env": {k: v for k, v in os.environ.items() if k.startswith("PRO_")}, "cwd": os.getcwd()}
        for flag in ("--settings", "--append-system-prompt-file"):
            if flag in a:
                try:
                    info[flag] = open(a[a.index(flag) + 1]).read()
                except OSError:
                    pass
        with open(os.path.join(FAKE_DIR, self.name + ".argv.json"), "w") as f:
            json.dump(info, f)
        self.log("START")

    def log(self, line):
        with open(self.logf, "a") as f:
            f.write(line + "\n")

    def status(self, s):
        with open(self.statusf, "w") as f:
            f.write(s)

    def hook(self, event, payload):
        if os.environ.get("FAKE_NOHOOKS"):
            return ""
        payload = dict(payload, session_id=self.sid, transcript_path=os.path.join(FAKE_DIR, self.name + ".jsonl"))
        r = subprocess.run([sys.executable, PRO, "hook", event], input=json.dumps(payload), capture_output=True, text=True)
        return r.stdout

    def draw(self, extra=None):
        w = "\x1b[2J\x1b[H"
        lines = self.out[-8:]
        if extra:
            lines.append(extra)
        w += "\n".join(lines) + "\n"
        w += "-" * 60 + " %s\n" % self.name
        w += "\x1b[39m❯\xa0%s\n" % self.buf
        w += "-" * 70 + "\n"
        w += "  ⏸ manual mode on%s\n" % (" · esc to interrupt" if self.working else "")
        sys.stdout.write(w.replace("\n", "\r\n"))
        sys.stdout.flush()

    def read_input(self, timeout):
        """Collect keystrokes for up to timeout seconds; returns a list of submitted texts."""
        subs, end = [], time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                return subs
            r, _, _ = select.select([sys.stdin], [], [], min(left, 0.1))
            if not r:
                continue
            data = os.read(sys.stdin.fileno(), 4096).decode("utf-8", "replace")
            i = 0
            while i < len(data):
                if data.startswith("\x1b[200~", i):
                    self.paste, i = "", i + 6
                elif data.startswith("\x1b[201~", i):
                    self.buf += self.paste or ""
                    self.paste, i = None, i + 6
                elif data[i] == "\x1b":
                    i += 1
                    while i < len(data) and not data[i].isalpha() and data[i] != "~":
                        i += 1
                    i += 1
                elif self.paste is not None:
                    self.paste += data[i]
                    i += 1
                elif data[i] in "\r":
                    if self.buf.strip():
                        subs.append(self.buf)
                        self.buf = ""
                    i += 1
                else:
                    self.buf += data[i]
                    i += 1
            if not self.working:
                self.draw()
            if subs and not self.working:
                return subs

    def run(self):
        if os.environ.get("FAKE_FAIL"):
            print("fatal: fake startup failure")
            return 3
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        try:
            sys.stdout.write("\x1b[?2004h")
            if os.environ.get("FAKE_TRUST"):
                self.trust()
            if os.environ.get("FAKE_LIMIT"):
                sys.stdout.write("\x1b[2J\x1b[HYou've hit your usage limit. Resets at 5pm\r\n")
                sys.stdout.flush()
                time.sleep(3600)
            self.hook("sessionstart", {})
            self.status("idle")
            self.draw()
            while True:
                for text in self.read_input(0.5):
                    self.turn(text, False)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def trust(self):
        pointer = 0
        while True:
            opts = ["No, exit", "Yes, I trust this folder"]
            sys.stdout.write("\x1b[2J\x1b[H Quick safety check: is this a project you trust?\r\n\r\n")
            for i, o in enumerate(opts):
                sys.stdout.write(" %s%s\n" % ("❯ " if i == pointer else "  ", o))
            sys.stdout.write("\n Enter to confirm\r\n")
            sys.stdout.flush()
            r, _, _ = select.select([sys.stdin], [], [], 30)
            data = os.read(sys.stdin.fileno(), 64).decode() if r else ""
            if "\x1b[B" in data:
                pointer = 1
            elif "\r" in data:
                if pointer == 1:
                    return
                sys.exit(1)

    def turn(self, text, active):
        import re
        while True:
            self.working = True
            self.status("busy")
            self.out.append("> " + text.splitlines()[0][:70])
            self.draw()
            out = self.hook("userpromptsubmit", {"prompt": text})
            m = re.search(r"\[pro-msg id=(m-[0-9a-f]+) from=(\S+) to=(\S+) task=(\S+) kind=(\S+)", text)
            reply, noreply = [], False
            if m:
                mid, frm, to, task, kind = m.groups()
                self.log("ACT %s%s" % (mid, " dup" if "possible duplicate" in text else ""))
                self.log("TEXT %s %d" % (mid, len(text)))
                for line in text.splitlines()[1:]:
                    cmd, _, rest = line.partition(" ")
                    if cmd == "ECHO":
                        reply.append(rest)
                    elif cmd == "SLEEP":
                        self.midturn += self.read_input(float(rest))
                    elif cmd == "SEND":
                        who, _, t = rest.partition(" ")
                        subprocess.run([sys.executable, PRO, "send", who, t], capture_output=True, text=True)
                    elif cmd == "ASK":
                        who, _, t = rest.partition(" ")
                        r = subprocess.run([sys.executable, PRO, "ask", who, t, "--wait", "5"], capture_output=True, text=True)
                        reply.append("ask-exit=%d %s" % (r.returncode, (r.stdout + r.stderr).replace("\n", " ")[:200]))
                    elif cmd == "NOREPLY":
                        noreply = True
                    elif cmd == "CRASH":
                        os._exit(9)
                if kind == "task" and not noreply:
                    body = " | ".join(reply) or "done"
                    subprocess.run([sys.executable, PRO, "reply", task, body], capture_output=True, text=True)
            for t in self.midturn:
                self.log("MIDTURN " + t[:40])
            self.midturn = []
            self.working = False
            out = self.hook("stop", {"stop_hook_active": active, "last_assistant_message": "ok"})
            try:
                d = json.loads(out) if out.strip() else {}
            except ValueError:
                d = {}
            if d.get("decision") == "block":
                text, active = d["reason"], True
                continue
            self.status("idle")
            self.draw()
            return


if __name__ == "__main__":
    sys.exit(main())
