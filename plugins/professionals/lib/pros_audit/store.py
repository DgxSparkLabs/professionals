"""Audit store: the SQLite file under $PRO_HOME/audit (docs/spec/audit/schemas/store-v1.sql).

The file is created 0600 before sqlite ever opens it, inside a 0700 directory. An existing file must be a regular file
owned by this user; a looser mode is tightened. WAL, short transactions, 5 s busy timeout."""
import contextlib
import os
import sqlite3
import stat
import time

KNOWN_VERSION = 2
HERE = os.path.dirname(os.path.abspath(__file__))


class StoreError(Exception):
    pass


def audit_dir(home):
    return os.path.join(str(home), "audit")


def db_path(home):
    return os.path.join(audit_dir(home), "audit.db")


def ensure_dir(path):
    if not os.path.isdir(path):
        os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if st.st_uid != os.geteuid() or not stat.S_ISDIR(st.st_mode):
        raise StoreError("audit directory %s is not a directory owned by this user" % path)
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)


def _check_file(path):
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise StoreError("%s is not a regular file" % path)
    if st.st_uid != os.geteuid():
        raise StoreError("%s is owned by another user" % path)
    if st.st_mode & 0o077:
        os.chmod(path, 0o600)


def open_store(home, create=True, known=None):
    """Open (and on first use create) the store. Raises StoreError when it must not be written."""
    d = audit_dir(home)
    ensure_dir(d)
    p = db_path(home)
    if not os.path.lexists(p):
        if not create:
            raise StoreError("no audit store yet")
        try:
            os.close(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            pass
    _check_file(p)
    conn = sqlite3.connect(p, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    have = conn.execute("SELECT count(*) FROM sqlite_master WHERE name='schema_version'").fetchone()[0]
    fresh = not have
    if not have:
        with open(os.path.join(HERE, "store-v1.sql"), encoding="utf-8") as f:
            ddl = f.read()
        try:
            conn.executescript("BEGIN IMMEDIATE;\n%s\nINSERT INTO schema_version VALUES (1, %r, 'initial');\nCOMMIT;" % (ddl, time.time()))
        except sqlite3.OperationalError:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if not conn.execute("SELECT count(*) FROM sqlite_master WHERE name='schema_version'").fetchone()[0]:
                raise
    ver = conn.execute("SELECT max(version) FROM schema_version").fetchone()[0] or 0
    known = known or KNOWN_VERSION
    if ver > known:
        conn.close()
        raise StoreError("the store is version %d, this pros knows %d; update pros (reads only)" % (ver, known))
    while ver < KNOWN_VERSION and known >= KNOWN_VERSION:  # additive built-in steps run on first open, no backup needed
        ver = _auto_step(conn, ver)
    if fresh and ver >= 2:
        conn.execute("UPDATE store_state SET generation = 0")  # a new store starts at generation 0, whatever steps ran
    return conn


def _sql_statements(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        text = "\n".join(ln for ln in f.read().splitlines() if not ln.lstrip().startswith("--"))
    return [x.strip() for x in text.split(";\n") if x.strip().rstrip(";")]


def _m1_to_2(conn):
    """v1 -> v2 (additive): event.pro_name, source_meta, header_seen, exchange_conflict, session_residual view (store-v2.sql)."""
    for st in _sql_statements("store-v2.sql"):
        conn.execute(st)


def _auto_step(conn, cur):
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = conn.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        if now == cur:
            MIGRATIONS[cur](conn)
            conn.execute("INSERT INTO schema_version VALUES (?, ?, 'migrate')", (cur + 1, time.time()))
            conn.execute("UPDATE store_state SET generation = generation + 1")
            now = cur + 1
        conn.execute("COMMIT")
        return now
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


MIGRATIONS = {1: _m1_to_2}  # n -> fn(conn): the step from version n to n+1; runs inside one BEGIN IMMEDIATE transaction


class MigrationError(Exception):
    pass


def migrate(home, target, steps=None):
    """Bring the store to `target` one numbered step at a time (events.md section 5). A failing step rolls back, is recorded in
    audit_log as audit_migrate/rolled_back, and leaves the file at the old version, readable. Returns the version reached."""
    steps = MIGRATIONS if steps is None else steps
    conn = open_store(home, known=max(KNOWN_VERSION, target))
    try:
        cur = conn.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        while cur < target:
            bak = db_path(home) + ".bak-v%d" % cur
            dst = sqlite3.connect(bak)
            conn.backup(dst)
            dst.close()
            os.chmod(bak, 0o600)
            try:
                conn.execute("BEGIN IMMEDIATE")
                steps[cur](conn)
                conn.execute("INSERT INTO schema_version VALUES (?, ?, ?)", (cur + 1, time.time(), "migrate"))
                conn.execute("UPDATE store_state SET generation = generation + 1")
                conn.execute("COMMIT")
            except Exception as e:  # noqa: BLE001
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO audit_log (t, action, actor, target, outcome, detail) VALUES (?, 'audit_migrate', 'importer', ?, 'rolled_back', ?)",
                             (time.time(), "v%d->v%d" % (cur, cur + 1), type(e).__name__))
                conn.execute("COMMIT")
                raise MigrationError("migration %d -> %d failed (%s); the store stays at version %d" % (cur, cur + 1, type(e).__name__, cur))
            cur += 1
            conn.execute("INSERT INTO audit_log (t, action, actor, target, outcome, detail) VALUES (?, 'audit_migrate', 'importer', ?, 'ok', NULL)", (time.time(), "v%d->v%d" % (cur - 1, cur)))
            with contextlib.suppress(OSError):
                os.unlink(bak)  # kept only until the next successful batch in the full design; a successful step removes it here
        return cur
    finally:
        conn.close()
