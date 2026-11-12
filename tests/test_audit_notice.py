"""0.3.1: audit disclosure. README section, one-time stderr notice, 90-day default retention."""
import os
import sys
import unittest

import helpers
from helpers import ProTestCase

ROOT = os.path.join(helpers.HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "plugins", "professionals", "lib"))
from pros_audit import manage as M  # noqa: E402


class ReadmeDisclosure(unittest.TestCase):
    def test_readme_discloses(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            t = f.read()
        for needle in ("Audit data on your machine", "PRO_AUDIT=off", "docs/audit-operations.md", "$PRO_HOME/audit/", "90 days", "pros audit export"):
            self.assertIn(needle, t)

    def test_default_retention_is_90(self):
        self.assertEqual(M.RETENTION["operational"][1], 90)


class Notice(ProTestCase):
    def marker(self):
        return os.path.join(self.home, "audit", "notice-shown")

    def capture(self, *args, **kw):
        env = dict(kw.pop("env", {}))
        env.pop("PRO_AUDIT", None)
        return self.pro(*args, env=env, **kw)

    def test_once_on_stderr_only(self):
        r = self.capture("summon", "alpha")
        self.assertIn("audit capture is on", r.stderr)
        self.assertNotIn("audit capture", r.stdout)
        self.assertTrue(os.path.exists(self.marker()))
        r2 = self.capture("summon", "alpha")
        self.assertNotIn("audit capture", r2.stderr)

    def test_not_in_professional_session(self):
        r = self.capture("ask", "beta", "hi", as_pro="alpha")
        self.assertNotIn("audit capture", r.stderr + r.stdout)
        self.assertFalse(os.path.exists(self.marker()))
        self.assertTrue(os.listdir(os.path.join(self.home, "audit", "spool")), "capture did write, only the notice was withheld")

    def test_not_when_off(self):
        r = self.pro("summon", "alpha", env={"PRO_AUDIT": "off"})
        self.assertNotIn("audit capture", r.stderr)
        self.assertFalse(os.path.exists(self.marker()))


if __name__ == "__main__":
    unittest.main()
