"""Audit step D1: capture (emitter + spool) and the importer into SQLite.

Contract fixtures: tests/fixtures/audit/contracts/. Stdlib only. Timing checks against the budgets in
docs/spec/audit/budgets.md run for real only with PRO_BENCH=1 (timings are host dependent); the default run uses the hard caps."""
import glob
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest

import helpers
from helpers import ProTestCase

LIB = os.path.join(helpers.HERE, "..", "plugins", "professionals", "lib")
sys.path.insert(0, LIB)
sys.path.insert(0, helpers.HERE)
from pros_audit import actions as ACT  # noqa: E402
from pros_audit import emit as E  # noqa: E402
from pros_audit import importer as IMP  # noqa: E402
from pros_audit import store as ST  # noqa: E402
import audit_schema as sch  # noqa: E402

CF = os.path.join(helpers.HERE, "fixtures", "audit", "contracts")
EVENT = sch.load("event-v1.json")
BENCH = os.environ.get("PRO_BENCH") == "1"


def fixture(*parts):
    with open(os.path.join(CF, *parts), encoding="utf-8") as f:
        return f.read()


def jl(*parts):
    return [json.loads(x) for x in fixture(*parts).splitlines() if x.strip()]


def spool_events(home):
    out = []
    for p in sorted(glob.glob(os.path.join(home, "audit", "spool", "*.jsonl"))):
        with open(p, encoding="utf-8") as f:
            out += [json.loads(x) for x in f if x.strip()]
    return out


def put_spool(home, name, lines, age=None):
    d = os.path.join(home, "audit", "spool")
    os.makedirs(d, mode=0o700, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        for ln in lines:
            if isinstance(ln, dict):
                ln = json.dumps(ln, sort_keys=True) + "\n"
            if isinstance(ln, str):
                ln = (ln if ln.endswith("\n") else ln + "\n").encode("utf-8")
            f.write(ln)
    if age:
        os.utime(p, (time.time() - age, time.time() - age))
    return p


def table_hash(db, table, cols):
    h = hashlib.sha256()
    for row in db.execute("SELECT %s FROM %s ORDER BY id" % (cols, table)):
        h.update(repr(row).encode())
    return h.hexdigest()


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="auditd1-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        E._ST.update(pid=None, off=None)
        E._write = os.write
        E.SPOOL_CAP = 64 * 1024 * 1024
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, E, "_write", os.write)
        self.addCleanup(setattr, E, "SPOOL_CAP", 64 * 1024 * 1024)

    def emit(self, n=1, action="msg_read", data=None, **kw):
        ids = []
        for _ in range(n):
            ids.append(E.emit(self.home, action, data or {"reader": "user"}, "user", kw.get("env", {"msg": "m-0000000001"})))
        return ids

    def db(self):
        return ST.open_store(self.home)


class ActionsTable(unittest.TestCase):
    def test_table_matches_schema(self):
        seen = {}
        for a in EVENT["allOf"][:-1]:
            act = a["if"]["properties"]["action"]["const"]
            d = a["then"]["properties"]["data"]
            seen[act] = (tuple(sorted(d.get("required", []))), tuple(sorted(set(d["properties"]) - set(d.get("required", [])))), tuple(sorted(a["then"].get("required", []))))
        self.assertEqual(seen, ACT.ACTIONS)
        self.assertEqual(set(ACT.ACTIONS), set(EVENT["properties"]["action"]["enum"]))

    def test_packaged_ddl_is_the_contract(self):
        with open(os.path.join(sch.SCHEMAS, "store-v1.sql"), encoding="utf-8") as f, open(os.path.join(ST.HERE, "store-v1.sql"), encoding="utf-8") as g:
            self.assertEqual(f.read(), g.read())

    def test_task_tokens_match_fixture(self):
        for row in json.loads(fixture("events", "task_ids.json")):
            tok, form = E.task_token(row["raw"])
            self.assertEqual((tok, form), (row["task"], row["task_form"]))


class Emitter(Tmp):
    def test_events_are_valid_private_and_ordered(self):
        self.emit(3)
        evs = spool_events(self.home)
        self.assertEqual(len(evs), 3)
        for e in evs:
            self.assertEqual(sch.validate(e, EVENT), [], e)
        w = {e["id"].split("-")[0] for e in evs}
        self.assertEqual(len(w), 1)
        self.assertEqual(len(w.pop()), 16)
        self.assertEqual([int(e["id"].split("-")[1]) for e in evs], [1, 2, 3])
        d = os.path.join(self.home, "audit", "spool")
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(d)).st_mode), 0o700)
        for f in os.listdir(d):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, f)).st_mode), 0o600)

    def test_two_processes_get_distinct_writers(self):
        code = "import sys;sys.path.insert(0,%r);from pros_audit import emit as E;E.emit(%r,'msg_read',{'reader':'user'},'user',{'msg':'m-0000000001'})" % (LIB, self.home)
        for _ in range(3):
            subprocess.run([sys.executable, "-c", code], check=True)
        ids = [e["id"] for e in spool_events(self.home)]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len({i.split("-")[0] for i in ids}), 3)

    def test_never_raises_on_unwritable_home(self):
        blocker = os.path.join(self.tmp, "afile")
        open(blocker, "w").close()
        self.assertIsNone(E.emit(blocker, "msg_read", {"reader": "user"}, "user", {}))
        self.assertTrue(E.degraded())

    def test_off_switch(self):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"capture_mode": "off"}, f)
        self.assertIsNone(self.emit()[0])
        self.assertFalse(os.path.exists(os.path.join(self.home, "audit", "spool")))

    def test_write_error_new_file_gap_first_and_no_torn_line_before_valid(self):
        self.emit(2)
        real = os.write
        calls = {"n": 0}

        def torn(fd, data):  # a short write: half the line lands
            calls["n"] += 1
            return real(fd, data[: len(data) // 2])
        E._write = torn
        self.assertIsNone(self.emit()[0])
        self.assertTrue(E.degraded())
        self.assertTrue(os.path.exists(os.path.join(self.home, "audit", "degraded.json")))
        E._write = real
        ok = self.emit(2)
        self.assertTrue(all(ok))
        files = sorted(glob.glob(os.path.join(self.home, "audit", "spool", "*.jsonl")))
        self.assertEqual(len(files), 2)
        first = open(files[0], "rb").read()
        self.assertFalse(first.endswith(b"\n"))  # the torn tail is the last thing in its file
        self.assertEqual(first.count(b"\n"), 2)
        second = [json.loads(x) for x in open(files[1], encoding="utf-8")]
        self.assertEqual([e["action"] for e in second], ["audit_gap", "msg_read", "msg_read"])
        self.assertEqual(second[0]["data"]["kind"], "write_error")
        self.assertEqual(sch.validate(second[0], EVENT), [])

    def test_raising_write_is_absorbed(self):
        def boom(fd, data):
            raise OSError(28, "No space left on device")
        E._write = boom
        for _ in range(3):
            self.assertIsNone(self.emit()[0])
        self.assertTrue(E.degraded())

    def test_cap_drops_and_flags(self):
        E.SPOOL_CAP = 1500
        E._ST["pid"] = None
        ids = self.emit(40)
        n = len([i for i in ids if i])
        self.assertTrue(0 < n < 40)
        self.assertTrue(E.degraded())
        size = sum(os.path.getsize(p) for p in glob.glob(os.path.join(self.home, "audit", "spool", "*")))
        self.assertLessEqual(size, 1500)
        self.assertEqual(len(spool_events(self.home)), n)

    def test_emit_overhead(self):
        self.emit(50)
        lat = []
        for _ in range(2000):
            t = time.perf_counter()
            self.emit()
            lat.append((time.perf_counter() - t) * 1000)
        lat.sort()
        p95 = lat[int(len(lat) * 0.95)]
        self.assertLess(p95, 0.1 if BENCH else 5.0, "emit p95 %.4f ms" % p95)  # budget 0.1 ms; hard cap 5 ms (FR-001)


class Store(Tmp):
    def test_created_private_with_wal(self):
        c = self.db()
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(c.execute("SELECT max(version) FROM schema_version").fetchone()[0], 2)
        c.execute("INSERT INTO professional (pro, name, origin) VALUES ('p-0', 'x', 'folder')")
        for f in glob.glob(os.path.join(self.home, "audit", "audit.db*")):
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600, f)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.home, "audit")).st_mode), 0o700)
        c.close()

    def test_loose_mode_is_tightened_and_symlink_refused(self):
        self.db().close()
        p = ST.db_path(self.home)
        os.chmod(p, 0o666)
        self.db().close()
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        os.rename(p, p + ".real")
        os.symlink(p + ".real", p)
        with self.assertRaises(ST.StoreError):
            ST.open_store(self.home)

    def test_newer_store_refused(self):
        c = self.db()
        c.execute("INSERT INTO schema_version VALUES (3, 0, 'future')")
        c.close()
        with self.assertRaises(ST.StoreError):
            ST.open_store(self.home)

    def test_reopen_keeps_data(self):
        c = self.db()
        c.execute("UPDATE store_state SET generation = 7")
        c.close()
        self.assertEqual(self.db().execute("SELECT generation FROM store_state").fetchone()[0], 7)


class Migration(Tmp):
    def v1(self, n=0):
        c = self.db()
        for i in range(n):
            c.execute("INSERT INTO event (id, payload_sha, t, action, actor, origin, data) VALUES (?, 'x', 1, 'msg_read', 'user', 'live', '{}')", ("0000000000000001-%d" % i,))
        c.close()

    def version(self):
        c = ST.open_store(self.home, known=9)
        try:
            return c.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        finally:
            c.close()

    def test_failing_step_rolls_back_and_stays_readable_then_rerun_succeeds(self):
        self.v1(5)

        def bad(c):
            c.execute("ALTER TABLE gap ADD COLUMN extra TEXT")
            raise RuntimeError("boom")
        with self.assertRaises(ST.MigrationError):
            ST.migrate(self.home, 3, {2: bad})
        self.assertEqual(self.version(), 2)
        c = ST.open_store(self.home)
        self.assertNotIn("extra", [r[1] for r in c.execute("PRAGMA table_info(gap)")])  # the DDL rolled back with it
        self.assertEqual(c.execute("SELECT count(*) FROM event").fetchone()[0], 5)
        self.assertEqual(c.execute("SELECT outcome FROM audit_log WHERE action='audit_migrate'").fetchall(), [("rolled_back",)])
        c.close()
        self.assertEqual(ST.migrate(self.home, 3, {2: lambda c: c.execute("ALTER TABLE gap ADD COLUMN extra TEXT")}), 3)
        self.assertEqual(self.version(), 3)
        for f in glob.glob(os.path.join(self.home, "audit", "audit.db.bak-v*")):
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)

    def test_newer_store_is_refused_by_old_reader_and_noop_is_fast(self):
        n = 100000 if BENCH else 20000
        self.v1(n)
        t = time.time()
        self.assertEqual(ST.migrate(self.home, 3, {2: lambda c: None}), 3)
        self.assertLess(time.time() - t, 5.0)
        with self.assertRaises(ST.StoreError):
            ST.open_store(self.home)  # a v1-only reader never writes a v2 store

    def test_sigkill_mid_step_keeps_integrity(self):
        self.v1(50)
        code = ("import sys,os,signal;sys.path.insert(0,%r);from pros_audit import store as S\n"
                "def step(c):\n c.execute('ALTER TABLE gap ADD COLUMN extra TEXT'); os.kill(os.getpid(), signal.SIGKILL)\n"
                "S.migrate(%r, 3, {2: step})\n" % (LIB, self.home))
        r = subprocess.run([sys.executable, "-c", code])
        self.assertEqual(r.returncode, -9)
        c = ST.open_store(self.home)
        self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(c.execute("SELECT max(version) FROM schema_version").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT count(*) FROM event").fetchone()[0], 50)


class Importer(Tmp):
    def test_release_scenario_imports_and_reimport_is_identical(self):
        lines = fixture("events", "release_scenario.jsonl").splitlines()
        put_spool(self.home, "a.0.jsonl", lines, age=60)
        r = IMP.drain(self.home)
        self.assertEqual((r["events"], r["duplicates"], r["conflicts"]), (27, 0, 0))
        db = self.db()
        h1 = table_hash_event(db)
        self.assertEqual(db.execute("SELECT count(*) FROM event").fetchone()[0], 28)  # 27 + the importer's own audit_import
        self.assertEqual(os.listdir(os.path.join(self.home, "audit", "claimed")), [])
        row = db.execute("SELECT batch, spool_files, events, duplicates, conflicts, finished FROM import_batch").fetchone()
        self.assertEqual(row[1:5], (1, 27, 0, 0))
        self.assertIsNotNone(row[5])
        # restart: the same spool file comes back (crash before delete): nothing changes, nothing doubles
        put_spool(self.home, "a.0.jsonl", lines, age=60)
        r2 = IMP.drain(self.home)
        self.assertEqual((r2["events"], r2["duplicates"]), (0, 27))
        self.assertEqual(table_hash_event(db), h1)
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE action != 'audit_import'").fetchone()[0], 26)
        self.assertEqual(db.execute("SELECT generation FROM store_state").fetchone()[0], 4)  # one bump per transaction: batch flush + final, twice
        db.close()

    def test_id_conflict_is_recorded_not_dropped(self):
        put_spool(self.home, "a.0.jsonl", jl("events", "id_conflict.jsonl"), age=60)
        r = IMP.drain(self.home)
        db = self.db()
        self.assertEqual((r["conflicts"], r["duplicates"]), (1, 1))
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE action != 'audit_import'").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT count(*) FROM event_conflict").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT kind FROM gap").fetchall(), [("id_conflict",)])
        put_spool(self.home, "b.0.jsonl", jl("events", "id_conflict.jsonl"), age=60)
        IMP.drain(self.home)
        self.assertEqual(db.execute("SELECT count(*) FROM event_conflict").fetchone()[0], 1)  # a replay of the conflict is a duplicate
        self.assertEqual(db.execute("SELECT count(*) FROM gap").fetchone()[0], 1)

    def test_structurally_invalid_events_never_enter_event(self):
        bad = [x["event"] for x in json.loads(fixture("events", "invalid.json"))]
        put_spool(self.home, "a.0.jsonl", bad, age=60)
        IMP.drain(self.home)
        db = self.db()
        n = db.execute("SELECT count(*) FROM event WHERE action != 'audit_import'").fetchone()[0]
        self.assertLess(n, len(bad))  # value-level patterns are the emitter's job (tested on emitted events); shape is the importer's
        for why, ev in [(x["why"], x["event"]) for x in json.loads(fixture("events", "invalid.json"))]:
            if why.startswith(("body text", "injected without", "backfill without", "unknown schema")):
                self.assertIsNone(db.execute("SELECT 1 FROM event WHERE id=?", (ev["id"],)).fetchone(), why)
        self.assertEqual(db.execute("SELECT count(*) FROM gap WHERE kind='unparseable'").fetchone()[0], 1)

    def test_unknown_action_goes_to_quarantine(self):
        ev = {"v": 1, "id": "0123456789abcdef-1", "t": 5.0, "action": "future_thing", "actor": "system", "origin": "live", "data": {"x": 1}}
        put_spool(self.home, "a.0.jsonl", [ev], age=60)
        IMP.drain(self.home)
        db = self.db()
        self.assertEqual(db.execute("SELECT id, action FROM quarantine").fetchall(), [("0123456789abcdef-1", "future_thing")])
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE id='0123456789abcdef-1'").fetchone()[0], 0)

    def test_torn_tail_gap_once_and_recent_file_kept(self):
        good = jl("events", "other_actions.jsonl")[0]
        p = put_spool(self.home, "a.0.jsonl", [good, b'{"v": 1, "id": "0123'], age=60)
        r = IMP.drain(self.home)
        self.assertEqual(r["events"], 1)
        db = self.db()
        self.assertEqual(db.execute("SELECT kind, count FROM gap").fetchall(), [("torn_line", 1)])
        self.assertFalse(os.path.exists(p))
        put_spool(self.home, "b.0.jsonl", [good, b'{"v": 1, "id": "0123'], age=0)  # a writer may still be mid-append: keep it, no gap yet
        IMP.drain(self.home)
        left = os.listdir(os.path.join(self.home, "audit", "claimed"))
        self.assertEqual(len(left), 1)
        self.assertEqual(db.execute("SELECT count(*) FROM gap").fetchone()[0], 1)

    def test_purge_watermark_and_tombstones_are_not_restored(self):
        evs = jl("events", "other_actions.jsonl")[:4]
        put_spool(self.home, "a.0.jsonl", evs, age=60)
        IMP.drain(self.home)
        db = self.db()
        mark = evs[2]["t"]
        db.execute("INSERT INTO purge_mark VALUES ('operational', ?, 2, 0)", (mark,))
        db.execute("DELETE FROM event WHERE t < ? AND action != 'audit_import'", (mark,))
        db.execute("INSERT INTO event_tombstone VALUES (?, 0, 'correction')", (evs[3]["id"],))
        db.execute("DELETE FROM event WHERE id=?", (evs[3]["id"],))
        before = db.execute("SELECT count(*) FROM event WHERE action != 'audit_import'").fetchone()[0]
        put_spool(self.home, "b.0.jsonl", evs, age=60)
        r = IMP.drain(self.home)
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE action != 'audit_import'").fetchone()[0], before)
        self.assertGreaterEqual(r["purged_skipped"], 3)
        self.assertEqual(db.execute("SELECT count(*) FROM gap WHERE kind='purged_skipped'").fetchone()[0], 1)

    def test_second_importer_is_refused_by_lock(self):
        with IMP.import_lock(self.home) as got:
            self.assertTrue(got)
            code = "import sys;sys.path.insert(0,%r);from pros_audit import importer as I;print(I.drain(%r))" % (LIB, self.home)
            put_spool(self.home, "a.0.jsonl", jl("events", "other_actions.jsonl")[:1], age=60)
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            self.assertIn("another import", r.stdout)

    def test_kill_between_capture_and_import_loses_and_doubles_nothing(self):
        n = 200
        code = ("import sys,os,signal;sys.path.insert(0,%r);from pros_audit import emit as E\n"
                "for i in range(%d):\n E.emit(%r,'msg_read',{'reader':'user'},'user',{'msg':'m-0000000001'})\n"
                " if i==149: os.kill(os.getpid(), signal.SIGKILL)\n" % (LIB, n, self.home))
        r = subprocess.run([sys.executable, "-c", code])
        self.assertEqual(r.returncode, -9)
        self.assertEqual(len(spool_events(self.home)), 150)  # every completed write is whole; nothing torn
        # importer killed after the claim, then killed before commit, then allowed to finish
        for where in ("after_claim", "before_commit"):
            code2 = "import sys;sys.path.insert(0,%r);from pros_audit import importer as I;I.drain(%r)" % (LIB, self.home)
            r = subprocess.run([sys.executable, "-c", code2], env=dict(os.environ, PROS_AUDIT_TEST_DIE=where))
            self.assertEqual(r.returncode, -9, where)
        res = IMP.drain(self.home)
        db = self.db()
        self.assertEqual(db.execute("SELECT count(*) FROM event WHERE action='msg_read'").fetchone()[0], 150)
        self.assertEqual(db.execute("SELECT count(DISTINCT id) FROM event WHERE action='msg_read'").fetchone()[0], 150)
        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(res["events"], 150)

    def test_failed_batch_leaves_rows_for_next_drain(self):
        evs = jl("events", "other_actions.jsonl")
        put_spool(self.home, "a.0.jsonl", evs, age=60)
        code = "import sys;sys.path.insert(0,%r);from pros_audit import importer as I;I.drain(%r)" % (LIB, self.home)
        subprocess.run([sys.executable, "-c", code], env=dict(os.environ, PROS_AUDIT_TEST_DIE="before_commit"))
        self.assertEqual(self.db().execute("SELECT count(*) FROM event").fetchone()[0], 0)
        self.assertEqual(IMP.drain(self.home)["events"], len(evs))

    def test_bulk_import_throughput_and_bounded_state(self):
        n = 30000 if not BENCH else 200000
        d = os.path.join(self.home, "audit", "spool")
        os.makedirs(d, mode=0o700)
        with open(os.path.join(d, "big.0.jsonl"), "w") as f:
            for i in range(n):
                f.write(json.dumps({"v": 1, "id": "00000000deadbeef-%d" % (i + 1), "t": 1.0 + i, "action": "msg_read", "actor": "user", "origin": "live", "msg": "m-0000000001", "data": {"reader": "user"}}) + "\n")
        os.utime(os.path.join(d, "big.0.jsonl"), (time.time() - 60,) * 2)
        t = time.time()
        r = IMP.drain(self.home)
        rate = n / (time.time() - t)
        self.assertEqual(r["events"], n)
        self.assertGreater(rate, 100000 if BENCH else 8000, "import %.0f events/s" % rate)
        db = self.db()
        self.assertEqual(db.execute("SELECT count(*) FROM event").fetchone()[0], n + 1)
        wal = os.path.join(self.home, "audit", "audit.db-wal")
        self.assertLess(os.path.getsize(wal) if os.path.exists(wal) else 0, 64 * 1024 * 1024)
        self.assertLess(os.path.getsize(ST.db_path(self.home)) / (n + 1), 1000)  # budget 100 MB per 100,000 events


def table_hash_event(db):
    h = hashlib.sha256()
    for row in db.execute("SELECT id, payload_sha, t, action, actor, origin, provenance, pro, run, session, msg, data FROM event WHERE action != 'audit_import' ORDER BY seq"):
        h.update(repr(row).encode())
    return h.hexdigest()


class StubRuns(ProTestCase):
    """Real `pros` processes against the stub claude: capture, import, and the release-fixture scenario shapes."""

    def events(self):
        subprocess.run([sys.executable, "-c", "import sys;sys.path.insert(0,%r);from pros_audit import importer as I;I.SETTLE_S=0;I.drain(%r)" % (LIB, self.home)], check=True)
        db = ST.open_store(self.home)
        rows = db.execute("SELECT id, t, action, actor, origin, provenance, pro, run, session, msg, data FROM event WHERE action != 'audit_import' ORDER BY t, seq").fetchall()
        out = []
        for r in rows:
            e = {"v": 1, "id": r[0], "t": r[1], "action": r[2], "actor": r[3], "origin": r[4], "data": json.loads(r[10])}
            for k, v in zip(("provenance", "pro", "run", "session", "msg"), r[5:10]):
                if v is not None:
                    e[k] = v
            out.append(e)
        db.close()
        return out

    def test_delegation_scenario_emits_valid_events_and_imports_them(self):
        self.summon("alpha")
        self.summon("beta")
        r = self.ok("ask", "alpha", "go\nASK beta question\nECHO relayed", "--wait", "40", "--task", "fx-root")
        self.assertIn("kind=reply", r.stdout)
        self.ok("stop", "--all")
        raw = spool_events(self.home)
        self.assertGreater(len(raw), 15)
        for e in raw:
            self.assertEqual(sch.validate(e, EVENT), [], e)
        evs = self.events()
        self.assertEqual(len(evs), len(raw))
        acts = [e["action"] for e in evs]
        # the release scenario's live shapes (delegation, wait, delivery, reply, read, stop) all appear
        fx = {e["action"] for e in jl("events", "release_scenario.jsonl")} - {"msg_overdue", "audit_evaluation", "audit_import"}
        self.assertTrue(fx - {"msg_read", "session_end"} <= set(acts), fx - set(acts))
        self.assertEqual(acts.count("spawn"), 2)
        self.assertGreaterEqual(acts.count("session_end"), 2)
        spawns = [e for e in evs if e["action"] == "spawn"]
        for e in spawns:
            self.assertRegex(e["run"], r"^r-[0-9a-f]{12}$")
            self.assertRegex(e["session"], r"^[0-9a-f-]{36}$")
            self.assertEqual(e["data"]["kind"], "fresh")
        injected = [e for e in evs if e["action"] == "msg_injected"]
        self.assertGreaterEqual(len(injected), 2)
        runs = {e["run"] for e in spawns}
        for e in injected:
            self.assertIn(e["run"], runs)
            self.assertEqual(e["data"]["via"], "paste")
        created = [e for e in evs if e["action"] == "msg_created"]
        root = [e for e in created if e["data"]["parent"] is None and e["data"]["kind"] == "task"][0]
        child = [e for e in created if e["data"]["kind"] == "task" and e["data"]["parent"] == root["msg"]][0]
        self.assertEqual((root["data"]["task"], root["actor"], child["actor"]), ("fx-root", "user", "professional"))
        self.assertEqual(child["data"]["chain_len"], 2)
        waits = [e for e in evs if e["action"] in ("wait_start", "wait_end") and e["actor"] == "user"]
        self.assertEqual([w["action"] for w in waits], ["wait_start", "wait_end"])
        self.assertTrue(any(e["action"] == "wait_start" and e["actor"] == "professional" for e in evs))  # alpha waited on beta
        end = [w for w in waits if w["action"] == "wait_end"][0]["data"]
        self.assertEqual((end["outcome"], end["exit"], end["returned"]), ("reply", 0, "reply"))
        self.assertTrue(any(e["action"] == "msg_answered" and e["data"]["answered_by"] == "reply" for e in evs))
        self.assertTrue(any(e["action"] == "hook_session" for e in evs))
        # no body text anywhere in the store
        blob = json.dumps(evs)
        for canary in ("relayed", "question"):
            self.assertNotIn(canary, blob)

    def test_overdue_and_late_reply_and_pending(self):
        self.config({"deliver_deadline_s": 2})
        self.summon("alpha")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        r = self.ok("send", "alpha", "SLEEP 5\nECHO late", "--task", "late-1")
        t = self.task_of(r.stdout)
        w = self.pro("wait", t, "--timeout", "1")
        self.assertEqual(w.returncode, 3, w.stdout + w.stderr)
        time.sleep(1.5)
        self.pro("team", env={"PRO_NO_AUTO": ""})  # a pump after the deadline sees it unanswered
        self.wait_until(lambda: any(m["state"] == "answered" for m in self.msgs() if m["kind"] == "task"), 30, "late answer")
        self.ok("stop", "--all")
        evs = self.events()
        acts = [e["action"] for e in evs]
        self.assertIn("msg_overdue", acts)
        ov = [e for e in evs if e["action"] == "msg_overdue"][0]
        ans = [e for e in evs if e["action"] == "msg_answered" and e["msg"] == ov["msg"]][0]
        self.assertGreater(ans["t"], ov["data"]["deadline"])  # deadline miss and a late reply are both visible
        ends = [e["data"] for e in evs if e["action"] == "wait_end"]
        self.assertEqual(ends[0]["outcome"], "pending")
        self.assertEqual((ends[0]["exit"], ends[0]["returned"]), (3, "none"))

    def test_failure_has_time_and_reason(self):
        self.summon("alpha")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        self.ok("send", "alpha", "SLEEP 8")
        self.wait_until(lambda: len(self.acts(s)) == 1, 10, "busy")
        self.ok("send", "alpha", "ECHO queued")
        self.ok("stop", "alpha")
        evs = self.events()
        failed = [e for e in evs if e["action"] == "msg_failed"]
        self.assertTrue(failed)
        self.assertTrue(all(e["data"]["reason"] == "recipient_stopped" and e["t"] > 0 for e in failed))
        self.assertTrue(any(e["action"] == "session_end" and e["data"]["reason"] == "stopped" for e in evs))

    def test_resume_and_fork_run_ids_and_session_stamps(self):
        d = self.make_folder("res")
        self.ok("add", d)
        self.summon("res")
        self.ok("stop", "res")
        self.summon("res")
        self.ok("stop", "res")
        self.summon("res", "--fork")
        self.ok("stop", "res")
        sp = [e for e in self.events() if e["action"] == "spawn"]
        self.assertEqual([e["data"]["kind"] for e in sp], ["fresh", "resume", "fork"])
        self.assertEqual(len({e["run"] for e in sp}), 3)
        self.assertEqual(sp[0]["session"], sp[1]["session"])  # a resume keeps the Claude session id
        self.assertNotEqual(sp[0]["session"], sp[2]["session"])  # a fork gets a new one
        self.assertEqual(sp[1]["data"]["parent_session"], sp[0]["session"])
        self.assertEqual(sp[2]["data"]["parent_session"], sp[0]["session"])
        self.assertEqual(len({e["pro"] for e in sp}), 1)
        reg = json.load(open(os.path.join(self.home, "registry.json")))
        self.assertEqual(reg["entries"]["res"]["pro_id"], sp[0]["pro"])
        self.assertTrue(any(e["action"] == "registry_change" and e["data"]["change"] == "added" for e in self.events()))

    def test_refusals_and_rate(self):
        self.summon("alpha")
        self.ok("send", "alpha", "ECHO a", "--task", "dup-1")
        self.ok("send", "alpha", "ECHO a", "--task", "dup-1")  # same task, same text, same target: duplicate
        r = self.pro("send", "ghost", "x", "--task", "no ghost")
        self.assertNotEqual(r.returncode, 0)
        evs = self.events()
        reasons = [e["data"]["reason"] for e in evs if e["action"] == "msg_refused"]
        self.assertIn("duplicate", reasons)
        self.assertIn("not_allowed", reasons)
        odd = [e for e in evs if e["action"] == "msg_refused" and e["data"]["to"] == "ghost"][0]
        self.assertEqual((odd["data"]["task"], odd["data"]["task_form"]), (E.task_token("no ghost")[0], "hashed"))
        self.assertTrue(any(e["action"] == "rate" and e["data"]["allowed"] for e in evs))

    def test_capture_failure_never_breaks_pros(self):
        os.makedirs(os.path.join(self.home, "audit"), exist_ok=True)
        open(os.path.join(self.home, "audit", "spool"), "w").close()  # a file where the spool directory should be
        self.summon("alpha")
        r = self.ok("ask", "alpha", "ECHO fine", "--wait", "20")
        self.assertIn("kind=reply", r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.home, "audit", "degraded.json")))

    def test_audit_dir_is_denied_to_sessions(self):
        self.summon("alpha")
        s = [x for x in self.sessions_alive() if x.startswith("pro-alpha")][0]
        self.assertIn(os.path.join(self.home, "audit"), json.dumps(json.load(open(os.path.join(self.home, "run", "settings", s + ".json")))))

    def test_capture_overhead_on_a_real_command(self):
        """One pros process pays about 70 ms; the audit part of a command must stay under the hard cap."""
        self.summon("alpha")
        t = []
        for _ in range(5):
            a = time.perf_counter()
            self.ok("send", "alpha", "ECHO x")
            t.append(time.perf_counter() - a)
        pro_mod = helpers.load_pros(self.home)
        lat = []
        for _ in range(300):
            a = time.perf_counter()
            pro_mod.audit_emit("msg_read", {"reader": "user"}, msg="m-0000000001")
            lat.append((time.perf_counter() - a) * 1000)
        lat.sort()
        self.assertLess(lat[int(0.95 * len(lat))], 5.0)


if __name__ == "__main__":
    unittest.main()
