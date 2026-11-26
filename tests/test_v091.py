"""0.9.1: the user-only gate cannot be lifted from inside a professional session by changing the environment.  Found while
sorting the suite's known failures: with PRO_SELF unset and PRO_TMUX_SOCKET pointing at a server that does not exist,
`pros audit status` passed the gate, because the ancestry check only looked for the server of PRO_TMUX_SOCKET.  Any tmux
server pros started (its command line is pros' own new-session) now counts, whatever the environment says."""
import os
import subprocess
import sys
import unittest

from helpers import ProTestCase, PRO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class GateIgnoresTheEnvironment(ProTestCase):
    def run_in_pane(self, *args, pre=""):
        """Run pros in a new window of the test's pros tmux server, with every hint of a professional removed."""
        self.config({"default_mode": "dontAsk"})
        self.summon("alpha")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        out = os.path.join(self.tmp, "gate.out")
        cmd = "env -u PRO_SELF -u PRO_SESSION -u TMUX -u TMUX_PANE %sPRO_TMUX_SOCKET=no-such-server-%d %s %s %s > %s 2>&1; echo $? >> %s" % (
            pre, os.getpid(), sys.executable, PRO, " ".join(args), out, out)
        subprocess.run(["tmux", "-L", self.sock, "new-window", "-d", "-t", "=" + s, "sh", "-c", cmd], check=True, env=self.env)
        self.wait_until(lambda: os.path.exists(out) and open(out).read().strip().splitlines()[-1:] != [] and
                        open(out).read().strip().splitlines()[-1].isdigit(), 20, "command finished")
        return open(out).read()

    def test_audit_is_refused_with_a_bogus_socket(self):
        out = self.run_in_pane("audit", "status")
        self.assertIn("only the user may", out)
        self.assertEqual(out.strip().splitlines()[-1], "4", out)

    def test_unblock_is_refused_with_a_bogus_socket(self):
        out = self.run_in_pane("unblock", "alpha", "--allow", "'Bash(wc *)'")
        self.assertIn("only the user may", out)


    def test_a_symlink_to_the_same_home_is_still_refused(self):
        link = os.path.join(self.tmp, "home-link")
        os.symlink(self.home, link)
        out = self.run_in_pane("audit", "status", pre="PRO_HOME=%s " % link)
        self.assertIn("only the user may", out)


class UserShellStillPasses(ProTestCase):
    def test_outside_any_pros_server_the_gate_passes(self):
        r = self.pro("audit", "status", env={"PRO_TMUX_SOCKET": "no-such-server"})
        self.assertNotIn("only the user may", r.stdout + r.stderr)


class Documented(unittest.TestCase):
    def test_docs(self):
        with open(os.path.join(ROOT, "CHANGELOG.md")) as f:
            self.assertIn("## 0.9.1", f.read())
        with open(os.path.join(ROOT, "docs", "spec", "spec.md")) as f:
            self.assertIn("any tmux server pros started", f.read())


if __name__ == "__main__":
    unittest.main()
