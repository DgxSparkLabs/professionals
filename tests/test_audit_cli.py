"""Audit step D3: `pros audit ...` end to end (real `pros` process, a file store built from the contract fixtures). Stdlib only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import helpers

HERE = helpers.HERE
sys.path[:0] = [HERE, os.path.join(HERE, "..", "plugins", "professionals", "lib")]
import audit_query_fixture as F  # noqa: E402

WIN = ["--since", str(int(F.T0)), "--until", str(int(F.NOW))]


class TestCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pros-audit-cli-")
        cls.home = os.path.join(cls.tmp, "home")
        os.makedirs(os.path.join(cls.home, "audit"), mode=0o700)
        db = F.new_db(os.path.join(cls.home, "audit", "audit.db"))
        F.load_release(db)
        db.close()

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def pros(self, *args, env=None):
        e = {k: v for k, v in os.environ.items() if k not in ("PRO_SESSION", "TMUX")}
        e.update(PRO_HOME=self.home, PRO_AUDIT_NOW=str(F.NOW), PRO_TMUX_SOCKET="audit-cli-test-none")
        e.update(env or {})
        return subprocess.run([sys.executable, helpers.PRO, "audit"] + list(args), capture_output=True, text=True, env=e, timeout=60)

    def golden_text(self, name):
        with open(os.path.join(F.CF, "query", name + ".json"), encoding="utf-8") as f:
            return f.read()

    def test_trace_json_matches_the_golden_exactly(self):
        for arg in ("m-0000000001", "fx-root"):
            r = self.pros("trace", arg, "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout, self.golden_text("task_trace"), arg)

    def test_summary_json_matches_the_golden_exactly(self):
        r = self.pros("summary", "--json", *WIN)
        self.assertEqual((r.returncode, r.stdout), (0, self.golden_text("query_metrics")), r.stderr)

    def test_explain_json_matches_the_goldens_exactly(self):
        for metric, name in (("deadline_miss_rate", "explain_deadline_miss_rate"), ("cache_read_share", "explain_cache_read_share")):
            r = self.pros("explain", metric, "--json", *WIN)
            self.assertEqual((r.returncode, r.stdout), (0, self.golden_text(name)), metric + r.stderr)

    def test_summary_default_window_is_30_days(self):
        r = self.pros("summary", "--since", "30d", "--json", env={"PRO_AUDIT_NOW": str(F.T0 + 86400)})
        self.assertEqual(r.returncode, 0, r.stderr)
        w = json.loads(r.stdout)["result"]["window"]
        self.assertEqual((w["from"], w["to"]), (F.T0 + 86400 - 30 * 86400, F.T0 + 86400))

    def test_text_output_always_names_snapshot_cutoff_and_degraded_note(self):
        for args in (["summary"] + WIN, ["trace", "fx-root"], ["explain", "pass_rate"] + WIN, ["status"]):
            r = self.pros(*args)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
            snap = json.loads(self.pros(*(args + ["--json"])).stdout)["snapshot"]["id"]
            self.assertIn(snap, r.stdout)
            self.assertIn("coverage cutoff 2027-01-15 08:03:20Z", r.stdout)
        r = self.pros("summary", "--by", "professional", *WIN)
        self.assertIn("Group professional=reliability-reviewer: n=1 (small sample)", r.stdout)
        self.assertIn("250 unique over 4 exchanges = 220 linked + 30 session-only", r.stdout)
        self.assertIn("late answers among missed 1", r.stdout)

    def test_degraded_store_is_announced_in_text(self):
        d = tempfile.mkdtemp(prefix="pros-audit-deg-")
        os.makedirs(os.path.join(d, "audit"), mode=0o700)
        db = F.new_db(os.path.join(d, "audit", "audit.db"))
        F.load_release(db)
        db.execute("INSERT INTO gap (kind, source, count) VALUES ('write_error','spool',1)")
        db.close()
        r = self.pros("status", env={"PRO_HOME": d})
        self.assertIn("DEGRADED: write_error", r.stdout)

    def test_errors_exit_codes_and_messages(self):
        r = self.pros("trace", "no-such-task")
        self.assertEqual((r.returncode, r.stdout), (3, ""))
        self.assertIn("not_found", r.stderr)
        r = self.pros("explain", "vibes", *WIN)
        self.assertEqual(r.returncode, 2)
        r = self.pros("summary", "--since", "9999d")
        self.assertEqual(r.returncode, 2)
        r = self.pros("summary", "--since", "30d", env={"PRO_HOME": os.path.join(self.tmp, "empty")})
        self.assertEqual(r.returncode, 5)
        self.assertIn("store_unavailable", r.stderr)

    def test_a_professional_session_cannot_read_the_audit(self):
        for env in ({"PRO_SESSION": "x"},):
            for args in (["summary"] + WIN, ["trace", "fx-root", "--json"], ["status"], ["explain", "assigned_work"] + WIN):
                r = self.pros(*args, env=env)
                self.assertEqual(r.returncode, 4, (args, r.stderr))
                self.assertEqual(r.stdout, "")
                self.assertIn("audit", r.stderr)

    def test_import_on_a_new_home_creates_the_store(self):
        # step E: `import` is wired to the D1 importer (+ D2 usage); the store here is a hand fold, so this runs on a new home
        home = os.path.join(self.tmp, "fresh")
        r = self.pros("import", "--json", env={"PRO_HOME": home, "CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "no-claude")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["spool"]["events"], 0)
        self.assertTrue(os.path.exists(os.path.join(home, "audit", "audit.db")))
        r = self.pros("import", env={"PRO_HOME": home, "PRO_SESSION": "x"})
        self.assertEqual(r.returncode, 4)

    def test_unknown_subcommand_is_a_usage_error(self):
        self.assertEqual(self.pros("explode").returncode, 2)


if __name__ == "__main__":
    unittest.main()
