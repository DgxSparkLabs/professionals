#!/usr/bin/env python3
"""Audit spike B4: storage and access measurements (not part of the unittest suite; run by hand).

  python3 tests/bench_audit.py [--events 10000] [--json]

Measures, in a private temp dir:
  emit       append one JSON event line to a per-process spool file (O_APPEND), with and without fsync
  pros_cli   wall time of one `pros version` process (the floor a transition already pays)
  wal        4 writer processes + 2 reader processes on one SQLite WAL store; busy errors, commit latency
  snapshot   a reader's two counts inside one read transaction stay equal while writers commit
  checkpoint WAL growth while a reader holds a snapshot, then a TRUNCATE checkpoint
  crash      a writer killed (SIGKILL) inside a transaction; a spool writer killed mid-line
  disk       bytes per event in the spool and in the store (with the planned indexes)
  disk_full  a spool writer at a file-size limit (stands in for a full disk): errors, torn lines
  modes      dir 0700 / file 0600 under umask 022, incl. the -wal and -shm side files
Everything uses the standard library only.
"""
import argparse
import json
import multiprocessing as mp
import os
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PRO = os.path.join(HERE, "..", "plugins", "professionals", "bin", "pros")
SCHEMA = """CREATE TABLE IF NOT EXISTS audit_event(seq INTEGER PRIMARY KEY, eid TEXT UNIQUE NOT NULL, t REAL NOT NULL,
action TEXT NOT NULL, actor TEXT, target TEXT, outcome TEXT, origin TEXT, schema_ver INTEGER NOT NULL, body TEXT);
CREATE INDEX IF NOT EXISTS ev_target ON audit_event(target, t);
CREATE INDEX IF NOT EXISTS ev_action ON audit_event(action, t);"""


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(-(-p * len(xs) // 100)) - 1))]  # nearest rank


def ms(xs):
    return {"n": len(xs), "p50_ms": round(pct(xs, 50) * 1e3, 4), "p95_ms": round(pct(xs, 95) * 1e3, 4),
            "max_ms": round(max(xs) * 1e3, 3)}


def event(i):
    return {"eid": uuid.uuid4().hex, "t": time.time(), "action": "msg.injected", "actor": "pro:alpha",
            "target": "m-%010x" % i, "outcome": "ok", "origin": "hook", "v": 1,
            "attrs": {"task": "t-%08x" % i, "via": "stop-hook", "run": "r-" + uuid.uuid4().hex[:12]}}


def open_private(path):
    return os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)


def bench_emit(d, n, fsync):
    p = os.path.join(d, "spool-%s.jsonl" % ("fsync" if fsync else "plain"))
    out = []
    for i in range(n):
        t0 = time.perf_counter()
        fd = open_private(p)  # open per event: a pros process emits a handful of events and exits
        os.write(fd, (json.dumps(event(i), separators=(",", ":")) + "\n").encode())
        if fsync:
            os.fsync(fd)
        os.close(fd)
        out.append(time.perf_counter() - t0)
    return dict(ms(out), bytes_per_event=round(os.path.getsize(p) / n, 1))


def bench_cli(n=10):
    out = []
    env = dict(os.environ, PRO_NO_AUTO="1", PRO_HOME=tempfile.mkdtemp(prefix="bench-prohome-"))
    for _ in range(n):
        t0 = time.perf_counter()
        subprocess.run([sys.executable, PRO, "version"], capture_output=True, env=env)
        out.append(time.perf_counter() - t0)
    return ms(out)


def connect(db, timeout=5.0):
    c = sqlite3.connect(db, timeout=timeout, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def writer(db, wid, batches, size, q):
    c = connect(db)
    lat, busy = [], 0
    for b in range(batches):
        rows = [(uuid.uuid4().hex, time.time(), "msg.created", "w%d" % wid, "m-%d-%d-%d" % (wid, b, i), "ok", "bench", 1, "{}")
                for i in range(size)]
        while True:
            t0 = time.perf_counter()
            try:
                c.execute("BEGIN IMMEDIATE")
                c.executemany("INSERT OR IGNORE INTO audit_event(eid,t,action,actor,target,outcome,origin,schema_ver,body)"
                              " VALUES(?,?,?,?,?,?,?,?,?)", rows)
                c.execute("COMMIT")
                lat.append(time.perf_counter() - t0)
                break
            except sqlite3.OperationalError:
                busy += 1
                try:
                    c.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
    q.put(("w", wid, lat, busy))


def reader(db, rid, stop_at, q):
    c = connect(db)
    lat, mismatches, n = [], 0, 0
    while time.time() < stop_at:
        t0 = time.perf_counter()
        c.execute("BEGIN")
        a = c.execute("SELECT count(*) FROM audit_event").fetchone()[0]
        time.sleep(0.002)
        b = c.execute("SELECT count(*) FROM audit_event").fetchone()[0]
        c.execute("COMMIT")
        lat.append(time.perf_counter() - t0)
        mismatches += a != b
        n += 1
    q.put(("r", rid, lat, mismatches))


def bench_wal(d, batches=50, size=1):
    db = os.path.join(d, "wal-%d.db" % size)
    c = connect(db)
    c.executescript(SCHEMA)
    c.close()
    q = mp.Queue()
    t0 = time.time()
    ws = [mp.Process(target=writer, args=(db, i, batches, size, q)) for i in range(4)]
    rs = [mp.Process(target=reader, args=(db, i, t0 + 3, q)) for i in range(2)]
    for p in ws + rs:
        p.start()
    res = [q.get() for _ in ws + rs]
    for p in ws + rs:
        p.join()
    wall = time.time() - t0
    wl = [x for r in res if r[0] == "w" for x in r[2]]
    rl = [x for r in res if r[0] == "r" for x in r[2]]
    c = connect(db)
    rows = c.execute("SELECT count(*) FROM audit_event").fetchone()[0]
    return {"writers": 4, "readers": 2, "batch_size": size, "commits": len(wl), "rows": rows,
            "expected_rows": 4 * batches * size, "busy_retries": sum(r[3] for r in res if r[0] == "w"),
            "commit": ms(wl), "read_txn": ms(rl), "snapshot_mismatches": sum(r[3] for r in res if r[0] == "r"),
            "writer_wall_s": round(wall, 2), "integrity": c.execute("PRAGMA integrity_check").fetchone()[0]}


def bench_checkpoint(d):
    db = os.path.join(d, "ckpt.db")
    w = connect(db)
    w.executescript(SCHEMA)
    r = connect(db)
    r.execute("BEGIN")
    r.execute("SELECT count(*) FROM audit_event").fetchone()  # a long-lived snapshot pins the WAL
    for b in range(200):
        w.execute("BEGIN")
        w.executemany("INSERT INTO audit_event(eid,t,action,schema_ver,body) VALUES(?,?,?,?,?)",
                      [(uuid.uuid4().hex, time.time(), "x", 1, "{}") for _ in range(50)])
        w.execute("COMMIT")
    pinned = os.path.getsize(db + "-wal")
    blocked = w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    r.execute("COMMIT")
    t0 = time.perf_counter()
    done = w.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    return {"wal_bytes_with_reader_pinned": pinned, "checkpoint_while_pinned(busy,log,ckpt)": list(blocked),
            "checkpoint_after(busy,log,ckpt)": list(done), "truncate_ms": round((time.perf_counter() - t0) * 1e3, 2),
            "wal_bytes_after": os.path.getsize(db + "-wal"), "events": 10000}


def crash_writer(db):
    c = connect(db)
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT INTO audit_event(eid,t,action,schema_ver) VALUES('committed-not',1,'x',1)")
    os.kill(os.getpid(), signal.SIGKILL)


def bench_crash(d):
    db = os.path.join(d, "crash.db")
    c = connect(db)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO audit_event(eid,t,action,schema_ver) VALUES('committed',1,'x',1)")
    c.close()
    p = mp.Process(target=crash_writer, args=(db,))
    p.start()
    p.join()
    c = connect(db)
    rows = [x[0] for x in c.execute("SELECT eid FROM audit_event")]
    spool = os.path.join(d, "crash-spool.jsonl")
    with open(spool, "w") as f:
        f.write(json.dumps({"eid": "a"}) + "\n" + '{"eid": "b", "t": 1')  # killed mid-line
    good, partial = [], 0
    with open(spool, "rb") as f:
        for line in f:
            if not line.endswith(b"\n"):
                partial += 1  # never parsed, never advanced past: the importer re-reads it next time
                continue
            good.append(json.loads(line)["eid"])
    return {"killed_writer_exit": p.exitcode, "rows_after": rows, "integrity": c.execute("PRAGMA integrity_check").fetchone()[0],
            "spool_complete_lines": good, "spool_partial_lines_skipped": partial}


def bench_disk(d, n):
    db = os.path.join(d, "disk.db")
    c = connect(db)
    c.executescript(SCHEMA)
    rows = []
    for i in range(n):
        e = event(i)
        rows.append((e["eid"], e["t"], e["action"], e["actor"], e["target"], e["outcome"], e["origin"], 1, json.dumps(e["attrs"])))
    t0 = time.perf_counter()
    c.execute("BEGIN")
    c.executemany("INSERT INTO audit_event(eid,t,action,actor,target,outcome,origin,schema_ver,body) VALUES(?,?,?,?,?,?,?,?,?)", rows)
    c.execute("COMMIT")
    ins = time.perf_counter() - t0
    t0 = time.perf_counter()
    c.execute("BEGIN")
    c.executemany("INSERT OR IGNORE INTO audit_event(eid,t,action,actor,target,outcome,origin,schema_ver,body) VALUES(?,?,?,?,?,?,?,?,?)", rows)
    c.execute("COMMIT")
    reimport = time.perf_counter() - t0
    after = c.execute("SELECT count(*) FROM audit_event").fetchone()[0]
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.execute("VACUUM")
    size = os.path.getsize(db)
    return {"events": n, "db_bytes": size, "db_bytes_per_event": round(size / n, 1), "import_events_per_s": int(n / ins),
            "reimport_rows_after": after, "reimport_s": round(reimport, 3)}


def bench_modes(d):
    old = os.umask(0o022)
    try:
        a = os.path.join(d, "audit")
        os.mkdir(a, 0o700)
        os.chmod(a, 0o700)  # mkdir mode is masked by umask; chmod makes it exact
        db = os.path.join(a, "audit.db")
        fd = os.open(db, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
        c = connect(db)
        c.executescript(SCHEMA)
        c.execute("INSERT INTO audit_event(eid,t,action,schema_ver) VALUES('x',1,'x',1)")
        sp = os.path.join(a, "spool.jsonl")
        os.close(open_private(sp))
        out = {os.path.basename(p): oct(os.stat(p).st_mode & 0o777) for p in (a, db, db + "-wal", db + "-shm", sp)}
        db2 = os.path.join(a, "plain.db")
        c2 = connect(db2)  # created by sqlite itself under umask 022
        c2.executescript(SCHEMA)
        out["sqlite_created_db"] = oct(os.stat(db2).st_mode & 0o777)
        out["sqlite_created_wal"] = oct(os.stat(db2 + "-wal").st_mode & 0o777)
        c.close()
        c2.close()
        return out
    finally:
        os.umask(old)


def full_child(d, q):
    import resource
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # report EFBIG instead of dying, like a full disk reports ENOSPC
    resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))
    p, ok, errs = os.path.join(d, "full-spool.jsonl"), 0, {}
    for i in range(100):
        try:
            fd = open_private(p)
            try:
                line = (json.dumps(event(i)) + "\n").encode()
                if os.write(fd, line) != len(line):
                    raise OSError(28, "short write")
                ok += 1
            finally:
                os.close(fd)
        except OSError as e:
            errs[e.errno] = errs.get(e.errno, 0) + 1
    with open(p, "rb") as f:
        data = f.read()
    q.put({"emitted_ok": ok, "errors_by_errno": errs, "file_bytes": len(data), "ends_with_newline": data.endswith(b"\n")})


def bench_full(d):
    q = mp.Queue()
    p = mp.Process(target=full_child, args=(d, q))
    p.start()
    r = q.get()
    p.join()
    return r


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=10000)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dir", help="parent dir for the temp dir (use the file system PRO_HOME lives on)")
    a = ap.parse_args(argv)
    d = tempfile.mkdtemp(prefix="bench-audit-", dir=a.dir)
    res = {"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version, "cpus": os.cpu_count()}
    res["emit_plain"] = bench_emit(d, a.events, False)
    res["emit_fsync"] = bench_emit(d, min(a.events, 2000), True)
    res["pros_cli"] = bench_cli()
    res["wal_single"] = bench_wal(d, 200, 1)
    res["wal_batch100"] = bench_wal(d, 50, 100)
    res["checkpoint"] = bench_checkpoint(d)
    res["crash"] = bench_crash(d)
    res["disk"] = bench_disk(d, a.events)
    res["modes"] = bench_modes(d)
    res["disk_full"] = bench_full(d)
    res["dir_fs"] = subprocess.run(["df", "--output=fstype", d], capture_output=True, text=True).stdout.split()[-1:]
    print(json.dumps(res, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
