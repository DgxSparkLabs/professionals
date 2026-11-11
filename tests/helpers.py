"""Shared test scaffolding: temp PRO_HOME, private tmux socket, fake claude, fixture marketplace."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
warnings.filterwarnings("ignore", category=ResourceWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
PRO = os.path.join(os.path.dirname(HERE), "plugins", "professionals", "bin", "pros")
FAKE = os.path.join(HERE, "fake_claude.py")
FIXTURES = os.path.join(HERE, "fixtures", "claude-2.1.322")

PERSONA = """---
name: {name}
description: A fixture professional used by the tests ({name}) for checking behaviour.
tags: [{tags}]
tools: [Read, Grep, Bash(git status), Bash(rm -rf /)]
model: sonnet
may_summon: {may}
---

# {name}

## Purpose
Testing.

## In scope
Things.

## Out of scope
Other things.

## Delegate when
Out of scope.

## Tools
Read, Grep.
"""


def make_persona(mdir, name, tags="test", may="true", extra_plugin=""):
    p = os.path.join(mdir, "plugins", "pro-" + name)
    os.makedirs(os.path.join(p, ".claude-plugin"), exist_ok=True)
    os.makedirs(os.path.join(p, "skills", name), exist_ok=True)
    with open(os.path.join(p, ".claude-plugin", "plugin.json"), "w") as f:
        json.dump({"name": "pro-" + name, "version": "0.1.0", "description": "fixture" + extra_plugin}, f)
    with open(os.path.join(p, "skills", name, "SKILL.md"), "w") as f:
        f.write(PERSONA.format(name=name, tags=tags, may=may))
    return p


class ProTestCase(unittest.TestCase):
    names = ("alpha", "beta", "gamma", "delta")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="protest-")
        self.home = os.path.join(self.tmp, "prohome")
        self.fakedir = os.path.join(self.tmp, "fake")
        self.proj = os.path.join(self.tmp, "proj")
        self.mdir = os.path.join(self.tmp, "market")
        self.fakehome = os.path.join(self.tmp, "home")
        for d in (self.home, self.fakedir, self.proj, self.fakehome, os.path.join(self.mdir, ".claude-plugin")):
            os.makedirs(d, exist_ok=True)
        plugins = []
        for n in self.names:
            make_persona(self.mdir, n, tags="test," + n, may="false" if n == "gamma" else "true")
            plugins.append({"name": "pro-" + n, "source": "./plugins/pro-" + n, "description": "fixture " + n, "tags": ["test"]})
        with open(os.path.join(self.mdir, ".claude-plugin", "marketplace.json"), "w") as f:
            json.dump({"name": "testpros", "owner": {"name": "t"}, "plugins": plugins}, f)
        self.sock = "protest-%d-%d" % (os.getpid(), int(time.time() * 1000) % 100000)
        self.config({"trusted_marketplaces": ["testpros"], "ready_timeout_s": 20, "ack_timeout_s": 6})
        self.env = dict(os.environ, PRO_HOME=self.home, PRO_TMUX_SOCKET=self.sock, PRO_MARKETPLACE=self.mdir,
                        PRO_CLAUDE_BIN="%s %s" % (sys.executable, FAKE), FAKE_DIR=self.fakedir, FAKE_PRO=PRO,
                        HOME=self.fakehome, PRO_NO_AUTO="1")
        for k in ("PRO_SELF", "PRO_SESSION", "TMUX", "TMUX_PANE", "CLAUDE_CODE_SESSION_ID"):
            self.env.pop(k, None)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        subprocess.run(["tmux", "-L", self.sock, "kill-server"], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, extra):
        p = os.path.join(self.home, "config.json")
        cur = {}
        if os.path.exists(p):
            cur = json.load(open(p))
        cur.update(extra)
        with open(p, "w") as f:
            json.dump(cur, f)

    def pro(self, *args, env=None, cwd=None, input=None, timeout=120, as_pro=None):
        e = dict(self.env, **(env or {}))
        if as_pro:
            e["PRO_SELF"] = as_pro
        return subprocess.run([sys.executable, PRO, *args], capture_output=True, text=True, env=e,
                              cwd=cwd or self.proj, input=input, timeout=timeout)

    def ok(self, *args, **kw):
        r = self.pro(*args, **kw)
        self.assertEqual(r.returncode, 0, "pros %s -> %d\n%s%s" % (" ".join(args), r.returncode, r.stdout, r.stderr))
        return r

    def summon(self, name, *extra, **kw):
        return self.ok("summon", name, *extra, **kw)

    def fake_log(self, session):
        p = os.path.join(self.fakedir, session + ".log")
        return open(p).read().splitlines() if os.path.exists(p) else []

    def acts(self, session):
        return [l for l in self.fake_log(session) if l.startswith("ACT ")]

    def wait_until(self, cond, timeout=30, what="condition"):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.2)
        self.fail("timed out waiting for " + what)

    def msgs(self):
        d = os.path.join(self.home, "run", "msgs")
        out = []
        for f in (os.listdir(d) if os.path.isdir(d) else []):
            with open(os.path.join(d, f)) as fh:
                out.append(json.load(fh))
        return sorted(out, key=lambda m: m["created"])

    def sessions_alive(self):
        r = subprocess.run(["tmux", "-L", self.sock, "ls", "-F", "#{session_name}"], capture_output=True, text=True)
        return [x for x in r.stdout.split() if x != "pros-pump"] if r.returncode == 0 else []

    def task_of(self, out):
        for tok in out.split():
            if tok.startswith("task="):
                return tok[5:]
        self.fail("no task in " + out)


def parallel(fns):
    res = [None] * len(fns)

    def run(i):
        res[i] = fns[i]()
    ts = [threading.Thread(target=run, args=(i,)) for i in range(len(fns))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return res
