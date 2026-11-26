"""0.9.0: the pump loop runs the code on disk.  Seen live: a task timed out with 'target state: idle' while its professional
waited on its own test run, a case 0.8.7 extends.  The timeout came from `pros pump --loop`, started at 01:31 and still
running the code it loaded then (before 0.8.7): Python never rereads a running script.  The loop now re-execs itself when
its script changes, so an upgrade reaches it within one pump interval."""
import os
import shutil
import subprocess
import sys
import time
import json

from helpers import ProTestCase, PRO

PLUGIN = os.path.dirname(os.path.dirname(PRO))


class PumpLoopReloads(ProTestCase):
    def starts(self):
        p = os.path.join(self.home, "run", "log", "events.log")
        if not os.path.exists(p):
            return []
        return [json.loads(l) for l in open(p) if '"pump-loop-start"' in l]

    def test_loop_re_execs_when_its_script_changes(self):
        self.config({"pump_interval_s": 1})
        copy = os.path.join(self.tmp, "plugin")
        shutil.copytree(PLUGIN, copy)
        script = os.path.join(copy, "bin", "pros")
        p = subprocess.Popen([sys.executable, script, "pump", "--loop"], env=self.env, cwd=self.proj,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(p.kill)
        self.wait_until(lambda: len(self.starts()) == 1, 15, "loop started")
        old = self.starts()[0]["version"]
        with open(script) as f:
            src = f.read()
        tmp = script + ".new"
        with open(tmp, "w") as f:
            f.write(src.replace('VERSION = "%s"' % old, 'VERSION = "9.9.9"', 1))
        os.chmod(tmp, 0o755)
        os.replace(tmp, script)  # as git checkout does: a new file under the same name
        self.wait_until(lambda: [s["version"] for s in self.starts()] == [old, "9.9.9"], 15, "re-exec on the new code")
        self.assertIsNone(p.poll(), "the same process, not a second loop")
        self.assertEqual(self.starts()[1]["pid"], p.pid)

    def test_unchanged_script_is_not_re_execed(self):
        self.config({"pump_interval_s": 1})
        p = subprocess.Popen([sys.executable, PRO, "pump", "--loop"], env=self.env, cwd=self.proj,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(p.kill)
        self.wait_until(lambda: len(self.starts()) == 1, 15, "loop started")
        time.sleep(3)
        self.assertEqual(len(self.starts()), 1)


class Documented(ProTestCase):
    def test_docs(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "CHANGELOG.md")) as f:
            self.assertIn("## 0.9.0", f.read())
        with open(os.path.join(root, "docs", "spec", "spec.md")) as f:
            self.assertIn("re-execs itself when its script changes", f.read())
