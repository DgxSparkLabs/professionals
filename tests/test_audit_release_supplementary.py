"""Audit step G (T091): the release fixture's supplementary cases, at release level (the real `pros audit` process on a store built
by `pros audit import`), complementing the unit tests of D1-F3:
crash between capture and import, fork duplicates, missing ids, overlapping tasks, denied access, retention, failed migration and
disk pressure. Stdlib only."""
import glob
import json
import os
import resource
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest

import helpers

LIB = os.path.join(helpers.HERE, "..", "plugins", "professionals", "lib")
sys.path[:0] = [helpers.HERE, LIB]
from test_audit_e2e import table_hash  # noqa: E402
from test_audit_usage import asst, hdr, prompt  # noqa: E402
from pros_audit import manage as M  # noqa: E402
from pros_audit import store as ST  # noqa: E402

CF = os.path.join(helpers.HERE, "fixtures", "audit", "contracts")
USAGE = os.path.join(helpers.HERE, "fixtures", "audit", "usage")
ROOT, CHILD = "m-0000000001", "m-0000000005"
NOW = "1800000200"
T0 = 1800000000.0
S2 = "00000000-0000-4000-8000-000000000002"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Home(unittest.TestCase):
    """A PRO_HOME with the release spool and transcripts, driven through `pros audit`."""
    spool_file = os.path.join(CF, "events", "release_scenario.jsonl")
    transcripts = True

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pros-audit-g-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.new_home("home")

    def new_home(self, name):
        home = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(home, "audit", "spool"), mode=0o700)
        os.chmod(os.path.join(home, "audit"), 0o700)
        self.proj = os.path.join(self.tmp, "claude-" + name, "projects", "p")
        os.makedirs(self.proj)
        if self.transcripts:
            for p in glob.glob(os.path.join(USAGE, "transcripts", "*.jsonl")):
                shutil.copy(p, self.proj)
        if self.spool_file:
            self.spool(home, read(self.spool_file))
        return home

    def spool(self, home, text, name="fixture-1.jsonl"):
        p = os.path.join(home, "audit", "spool", name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(p, 0o600)
        os.utime(p, (1, 1))

    def env(self, home=None, extra=None):
        home = home or self.home
        e = {k: v for k, v in os.environ.items() if k not in ("PRO_SESSION", "TMUX", "PROS_AUDIT_TEST_DIE")}
        e.update(PRO_HOME=home, PRO_AUDIT_NOW=NOW, PRO_TMUX_SOCKET="audit-g-none",
                 CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "claude-" + os.path.basename(home)))
        e.update(extra or {})
        return e

    def run_pros(self, *args, home=None, env=None, preexec=None):
        return subprocess.run([sys.executable, helpers.PRO, "audit"] + list(args), capture_output=True, text=True, env=self.env(home, env), timeout=120,
                              preexec_fn=preexec)

    def ok(self, *args, **kw):
        r = self.run_pros(*args, **kw)
        self.assertEqual(r.returncode, 0, (args, r.stdout[-800:], r.stderr[-800:]))
        return r.stdout

    def jok(self, *args, **kw):
        return json.loads(self.ok(*args, **kw))

    def trace(self, ident=ROOT, home=None):
        return self.jok("trace", ident, "--json", home=home)["result"]

    def totals(self, home=None):
        tr = self.trace(home=home)
        own = {a["msg"]: a.get("own_tokens", {}).get("total") for a in tr["assignments"]}
        return own.get(ROOT), own.get(CHILD), tr["usage"]["inclusive"]["total"], sum(s["tokens"]["total"] for s in tr["usage"]["session_only"])

    def db(self, home=None):
        c = sqlite3.connect(os.path.join(home or self.home, "audit", "audit.db"))
        self.addCleanup(c.close)
        return c


class TestCrashBetweenCaptureAndImport(Home):
    def test_importer_killed_twice_then_finishes_with_the_same_facts(self):
        clean = self.new_home("clean")
        self.ok("import", home=clean)
        for where in ("after_claim", "before_commit"):
            r = self.run_pros("import", env={"PROS_AUDIT_TEST_DIE": where})
            self.assertEqual(r.returncode, -signal.SIGKILL, (where, r.stderr[-400:]))
        self.ok("import")
        self.assertEqual(self.db().execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(table_hash(os.path.join(self.home, "audit", "audit.db")), table_hash(os.path.join(clean, "audit", "audit.db")),
                         "two crashes lose and double nothing")
        self.assertEqual(self.totals(), (140, 80, 220, 30))

    def test_capture_killed_mid_stream_imports_every_whole_line(self):
        # the capturing process died while writing: the last line is torn
        lines = read(self.spool_file).splitlines(True)
        self.spool(self.home, "".join(lines) + lines[0][: len(lines[0]) // 2], "fixture-1.jsonl")
        imp = self.jok("import", "--json")["spool"]
        self.assertEqual((imp["events"], imp["gaps"]), (len(lines), 1), "every whole line in, the torn tail is one gap")
        st = self.jok("status", "--json")
        self.assertIn("torn_line", [g["kind"] for g in st["coverage"]["gaps"]], st["coverage"]["gaps"])
        self.assertEqual(self.totals(), (140, 80, 220, 30))


class TestForkDuplicates(Home):
    def test_copied_transcripts_count_once(self):
        other = os.path.join(os.path.dirname(self.proj), "fork-copy")
        os.makedirs(other)
        for p in glob.glob(os.path.join(USAGE, "transcripts", "*.jsonl")):
            shutil.copy(p, os.path.join(other, "copy-" + os.path.basename(p)))
        imp = self.jok("import", "--json")["usage"]
        self.assertEqual(imp["new"], 4, "4 unique exchanges however many copies")
        self.assertGreater(imp["dup_rows"], 0)
        self.assertEqual(self.totals(), (140, 80, 220, 30))
        cards = self.jok("summary", "--json", "--since", "1799990000", "--until", NOW)["result"]["cards"]
        self.assertEqual((cards["accounted_tokens"]["total_unique"], cards["accounted_tokens"]["session_only"]), (250, 30), "140 + 80 + 30 session-only")


class TestMissingIds(Home):
    spool_file = os.path.join(CF, "scenarios", "missing_parent.jsonl")
    transcripts = False

    def test_orphan_is_a_gap_and_never_attached_by_guess(self):
        # plus an event that lost its id: refused, never stored under a made-up id
        bad = json.loads(read(os.path.join(CF, "events", "release_scenario.jsonl")).splitlines()[0])
        del bad["id"]
        self.spool(self.home, json.dumps(bad) + "\n", "fixture-2.jsonl")
        imp = self.jok("import", "--json")["spool"]
        self.assertEqual(imp["events"], 2)
        tr = self.trace("m-0000000002")
        a = tr["assignments"][0]
        self.assertEqual((a["orphan"], a["confidence"], a["depth"]), (True, "unassigned", 0))
        self.assertIn({"count": 1, "kind": "orphan", "msg": "m-0000000002"}, tr["gaps"])
        self.assertNotIn("own_tokens", a, "usage absent, not zero")
        kinds = [g["kind"] for g in self.jok("status", "--json")["coverage"]["gaps"]]
        self.assertIn("missing_parent", kinds)
        self.assertEqual(self.db().execute("SELECT count(*) FROM event WHERE action = ?", (bad["action"],)).fetchone()[0], 0)


class TestOverlappingTasks(Home):
    spool_file = os.path.join(CF, "scenarios", "overlap.jsonl")
    transcripts = False

    def test_exchange_while_two_tasks_are_in_flight_is_unassigned(self):
        rows = [prompt(S2, T0 + 2.0, hdr("m-0000000001")), asst(S2, T0 + 3, "o1"), prompt(S2, T0 + 4.0, "Stop hook feedback:\n" + hdr("m-0000000002")),
                asst(S2, T0 + 5, "o2", 20), asst(S2, T0 + 9.5, "o3", 40)]
        with open(os.path.join(self.proj, S2 + ".jsonl"), "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        self.ok("import")
        own = {}
        for m in ("m-0000000001", "m-0000000002"):
            a = self.trace(m)["assignments"][0]
            own[m] = a.get("own_tokens", {}).get("total")
        self.assertEqual(own, {"m-0000000001": 10, "m-0000000002": 40}, "the 20 tokens in the overlap belong to neither task")
        got = self.db().execute("SELECT msg, confidence, overlap FROM exchange WHERE key = 'o2:req_o2'").fetchone()
        self.assertEqual(got, (None, "unassigned", 1))
        cards = self.jok("summary", "--json", "--since", "1800000000", "--until", NOW)["result"]["cards"]
        self.assertEqual(cards["accounted_tokens"]["total_unique"], 70, "still counted once at session level")


class TestDeniedAccess(Home):
    def test_professional_session_is_denied_every_command_and_files_are_private(self):
        self.ok("import")
        before = table_hash(os.path.join(self.home, "audit", "audit.db"))
        for args in (["status"], ["summary"], ["trace", ROOT], ["explain", "pass_rate"], ["import"], ["serve"], ["serve", "--manage"],
                     ["verdict", ROOT, "--status", "passed", "--criterion", "C1=passed"], ["export", "--yes", "--out", os.path.join(self.tmp, "x")],
                     ["report", "list"], ["report", "save", "w"], ["compare", "--by", "revision"], ["rubric", "add", "r", "1", "--criterion", "C1"]):
            r = self.run_pros(*args, env={"PRO_SESSION": "x"})
            self.assertEqual(r.returncode, 4, (args, r.stderr[-300:]))
            self.assertIn("only the user", r.stderr, args)
        self.assertEqual(table_hash(os.path.join(self.home, "audit", "audit.db")), before)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x")))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.home, "audit")).st_mode), 0o700)
        for p in glob.glob(os.path.join(self.home, "audit", "audit.db*")):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600, p)


class TestRetention(Home):
    def test_retention_purge_survives_reimport_and_reports_class_removes_snapshots(self):
        self.ok("import")
        now = float(NOW)
        M.set_retention(self.home, "operational", 0, now)
        cut = M.retention_cutoff(self.home, "operational", now)
        self.assertEqual(cut, now, "0 days keeps nothing older than now")
        r = M.purge(self.home, "operational", cut, now)
        self.assertGreater(r["counts"]["events"], 0)
        self.assertEqual(self.jok("status", "--json")["result"]["assignments"], 0)
        # the same spool and transcripts come back after a restart: purged facts are not restored
        self.spool(self.home, read(self.spool_file))
        self.ok("import")
        self.assertEqual(self.jok("status", "--json")["result"]["assignments"], 0)
        self.assertEqual(self.run_pros("trace", ROOT, "--json").returncode, 3)
        log = self.db().execute("SELECT action FROM audit_log WHERE action IN ('audit_config', 'audit_purge')").fetchall()
        self.assertEqual(sorted(x[0] for x in log), ["audit_config", "audit_purge"])

    def test_reports_class(self):
        self.ok("import")
        self.ok("report", "save", "weekly", "--since", "1800000000", "--until", NOW)
        sid = self.jok("report", "run", "weekly", "--json")["snapshot"]
        snap = os.path.join(self.home, "audit", "reports", "snapshots", sid + ".json")
        self.assertEqual(stat.S_IMODE(os.stat(snap).st_mode), 0o600)
        later = float(NOW) + 10
        dry = M.purge(self.home, "reports", later, later, dry_run=True)
        self.assertEqual(dry["counts"], {"reports": 1, "snapshots": 1})
        M.purge(self.home, "reports", later, later)
        self.assertFalse(os.path.exists(snap))
        self.assertEqual(self.jok("report", "list", "--json"), {"reports": [], "snapshots": []})


class TestFailedMigration(Home):
    def test_failing_step_rolls_back_and_the_cli_answers_as_before(self):
        self.ok("import")
        before = json.loads(self.ok("trace", ROOT, "--json"))["result"]

        def bad(c):
            c.execute("ALTER TABLE gap ADD COLUMN extra TEXT")
            raise RuntimeError("boom")
        cur = self.db().execute("SELECT max(version) FROM schema_version").fetchone()[0]
        with self.assertRaises(ST.MigrationError):
            ST.migrate(self.home, cur + 1, {cur: bad})
        self.assertEqual(self.trace(), before, "same answer: nothing changed (the rolled_back audit_log row moves the snapshot id only)")
        c = self.db()
        self.assertNotIn("extra", [r[1] for r in c.execute("PRAGMA table_info(gap)")])
        self.assertEqual(c.execute("SELECT outcome FROM audit_log WHERE action = 'audit_migrate'").fetchall(), [("rolled_back",)])
        for p in glob.glob(os.path.join(self.home, "audit", "audit.db.bak-v*")):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600, p)

    def test_newer_store_is_refused_untouched(self):
        self.ok("import")
        c = self.db()
        c.execute("INSERT INTO schema_version (version, applied_at, note) SELECT max(version) + 7, 0, 'future' FROM schema_version")
        c.commit()
        h = table_hash(os.path.join(self.home, "audit", "audit.db"))
        r = self.run_pros("import")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("update pros", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.ok("status", "--json")  # an older pros still reads a newer store; it never writes one
        self.assertEqual(table_hash(os.path.join(self.home, "audit", "audit.db")), h)


class TestBackupRestore(Home):
    def test_documented_backup_and_restore(self):
        """docs/audit-operations.md: the backup one-liner (taken from the doc), restore, import again: same answers, nothing doubled."""
        self.ok("import")
        doc = read(os.path.join(helpers.HERE, "..", "docs", "audit-operations.md"))
        line = [x for x in doc.splitlines() if x.startswith("python3 -c '") and "backup" in x][0]
        code = line.split("'")[1]
        bdir = os.path.join(self.tmp, "backup")
        os.makedirs(bdir, mode=0o700)
        dest = os.path.join(bdir, "audit.db")
        r = subprocess.run([sys.executable, "-c", code, dest], env=self.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        before = self.trace()
        # after the backup: more work arrives and is imported, then the store is lost
        self.spool(self.home, read(os.path.join(CF, "scenarios", "missing_parent.jsonl")), "later.jsonl")
        self.ok("import")
        lost = os.path.join(self.tmp, "aside")
        os.makedirs(lost)
        for suffix in ("", "-wal", "-shm"):
            p = os.path.join(self.home, "audit", "audit.db" + suffix)
            if os.path.exists(p):
                os.rename(p, os.path.join(lost, "audit.db" + suffix))
        shutil.copy(dest, os.path.join(self.home, "audit", "audit.db"))
        os.chmod(os.path.join(self.home, "audit", "audit.db"), 0o600)
        self.ok("import")
        self.assertEqual(self.trace(), before)
        self.assertEqual(self.totals(), (140, 80, 220, 30))
        self.assertEqual(self.run_pros("trace", "m-0000000002").returncode, 3, "imported after the backup: not in the restored store (documented)")


def _fsize_limit(n):
    def f():
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # report EFBIG like a full disk reports ENOSPC, instead of dying
        resource.setrlimit(resource.RLIMIT_FSIZE, (n, n))
    return f


class TestDiskPressure(Home):
    def test_full_disk_import_fails_cleanly_keeps_the_spool_and_recovers(self):
        r = self.run_pros("import", preexec=_fsize_limit(48 * 1024))
        self.assertEqual(r.returncode, 5, r.stderr[-600:])
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("spool files are kept", r.stderr)
        kept = [f for d in ("spool", "claimed") if os.path.isdir(os.path.join(self.home, "audit", d)) for f in os.listdir(os.path.join(self.home, "audit", d))]
        self.assertTrue(kept, "the claimed spool file stays for the next import")
        db = os.path.join(self.home, "audit", "audit.db")
        if os.path.exists(db):
            self.assertEqual(self.db().execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.ok("import")
        self.assertEqual(self.totals(), (140, 80, 220, 30))


if __name__ == "__main__":
    unittest.main()
