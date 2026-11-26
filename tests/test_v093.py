"""0.9.3: the audit tests (24 failures + 2 errors of the suite's known baseline) failed whenever the suite ran inside a
professional session: they built their environment from os.environ and dropped only PRO_SESSION and TMUX, so PRO_SELF
reached `pros audit` and the user-only gate refused it.  They now share helpers.user_env(), which drops every session
variable, as ProTestCase already did.  This test runs them with a professional's environment set."""
import os
import subprocess
import sys
import unittest

import helpers

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = ["test_audit_cli", "test_audit_http", "test_audit_e2e", "test_audit_release", "test_audit_release_supplementary"]
SESSION = {"PRO_SELF": "some-pro", "PRO_SESSION": "pro-some-pro-1", "TMUX": "/tmp/tmux-0/x,1,0", "TMUX_PANE": "%1",
           "CLAUDE_CODE_SESSION_ID": "00000000-0000-0000-0000-000000000000"}


class UserEnv(unittest.TestCase):
    def test_drops_every_session_variable(self):
        old = dict(os.environ)
        try:
            os.environ.update(SESSION)
            e = helpers.user_env(PRO_HOME="/x")
        finally:
            os.environ.clear()
            os.environ.update(old)
        for k in SESSION:
            self.assertNotIn(k, e)
        self.assertEqual(e["PRO_HOME"], "/x")

    def test_audit_tests_pass_inside_a_professional_session(self):
        r = subprocess.run([sys.executable, "-m", "unittest"] + AUDIT, cwd=HERE, capture_output=True, text=True, timeout=900,
                           env=dict(os.environ, **SESSION))
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])

    def test_no_audit_test_builds_its_env_by_hand(self):
        for m in AUDIT:
            with open(os.path.join(HERE, m + ".py")) as f:
                self.assertNotIn("for k, v in os.environ.items()", f.read(), m)


class Documented(unittest.TestCase):
    def test_docs(self):
        root = os.path.dirname(HERE)
        with open(os.path.join(root, "CHANGELOG.md")) as f:
            self.assertIn("## 0.9.3", f.read())
        with open(os.path.join(root, "docs", "spec", "audit", "query.md")) as f:
            self.assertIn("setsid", f.read())


if __name__ == "__main__":
    unittest.main()
