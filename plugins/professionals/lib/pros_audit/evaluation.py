"""Audit evaluations (docs/spec/audit/metrics.md 2.2 and 4, events.md audit_evaluation).

A verdict is an `audit_evaluation` event; the projection into the `evaluation` table happens in the same transaction as the event
insert (the importer path of the store: one write transaction, one generation bump). Rules enforced at write time:
the target exists, a task verdict names a root message, `supersedes` names an evaluation of the same target, the stored status
agrees with the criterion outcomes of the named rubric, an automated evaluation never replaces an operator one.
Evidence refs are opaque `kind:token` strings: nothing here reads or stores their content. Standard library only."""
import hashlib
import json
import os
import re
import time

STATUSES = ("passed", "partial", "failed", "inconclusive", "unreviewed")
OUTCOMES = ("passed", "failed", "not_assessed")
MSG_RE = re.compile(r"^m-[0-9a-f]{10}$")
IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
VER_RE = re.compile(r"^[A-Za-z0-9._-]{1,16}$")
REF_RE = re.compile(r"^[a-z][a-z-]{0,15}:[A-Za-z0-9._-]{1,64}$")
EID_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
ADHOC = ("ad-hoc", "v1")


class EvalError(Exception):
    def __init__(self, msg, code="bad_request"):
        Exception.__init__(self, msg)
        self.code = code


def rubric_dir(home):
    return os.path.join(str(home), "audit", "rubrics")


def rubric_add(home, name, version, criteria):
    """Write rubrics/<name>/<version>.json. `criteria` is a list of (id, required). Versions are immutable: re-adding fails."""
    if not IDENT_RE.match(name or "") or not VER_RE.match(version or ""):
        raise EvalError("rubric name or version has an invalid form")
    ids = [c[0] for c in criteria]
    if not ids or len(set(ids)) != len(ids) or not all(IDENT_RE.match(i) for i in ids) or len(ids) > 64:
        raise EvalError("a rubric needs 1 to 64 distinct criterion ids")
    d = os.path.join(rubric_dir(home), name)
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, version + ".json")
    body = json.dumps({"rubric": name, "version": version, "criteria": [{"id": i, "required": bool(r)} for i, r in criteria]}, sort_keys=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise EvalError("rubric %s %s exists; versions are immutable, add a new version" % (name, version))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def rubric_version_of(home, name, version):
    """An omitted version is allowed only when the rubric has exactly one."""
    if version:
        return version
    try:
        vs = sorted(f[:-5] for f in os.listdir(os.path.join(rubric_dir(home), name)) if f.endswith(".json"))
    except OSError:
        vs = []
    if len(vs) != 1:
        raise EvalError("rubric %s has %d versions; pass --rubric-version" % (name, len(vs)) if vs else "unknown rubric %s (add it with: pros audit rubric add)" % name)
    return vs[0]


def rubric_load(home, name, version):
    if (name, version) == ADHOC:
        return None
    try:
        with open(os.path.join(rubric_dir(home), name, version + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise EvalError("unknown rubric %s %s (add it with: pros audit rubric add)" % (name, version))


def derive_status(outcomes, required):
    """metrics.md 2.2 rubric consistency. outcomes: {criterion: outcome}; required: criterion ids that must count."""
    req = [outcomes.get(c, "not_assessed") for c in required]
    if not req or any(o == "not_assessed" for o in req):
        return "inconclusive"
    if all(o == "passed" for o in req):
        return "passed"
    if all(o == "failed" for o in req):
        return "failed"
    return "partial"


def check_evidence(refs):
    for r in refs:
        if not REF_RE.match(r or ""):
            raise EvalError("evidence %r is not an opaque kind:token reference" % (r,))


def evidence_ref_for_file(label, path):
    """An existing test or check result as evidence: kind check-run, token label + first 12 hex of the file's sha256. Content is never stored."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        raise EvalError("cannot read the check result %s" % label)
    ref = "check-run:%s.%s" % (label, h.hexdigest()[:12])
    check_evidence([ref])
    return ref


def _row(r):
    return {"eval": r[0], "target_kind": r[1], "target": r[2], "status": r[3], "evaluator_kind": r[4], "evaluator": r[5], "evaluator_version": r[6],
            "rubric": r[7], "rubric_version": r[8], "effective_at": r[9], "supersedes": r[10], "criteria": json.loads(r[11]), "evidence": json.loads(r[12]) if r[12] else [],
            "seq": r[13], "t": r[14]}


def evaluations(conn, target):
    """Every evaluation of a target with the event seq and recorded time `t` of its audit_evaluation event."""
    out = []
    rows = conn.execute("SELECT eval, target_kind, target, status, evaluator_kind, evaluator, evaluator_version, rubric, rubric_version, effective_at, supersedes, criteria, evidence "
                        "FROM evaluation WHERE target = ?", (target,)).fetchall()
    evs = {}
    for seq, t, data in conn.execute("SELECT seq, t, data FROM event WHERE action='audit_evaluation' AND msg = ?", (target,)):
        try:
            evs[json.loads(data).get("eval")] = (seq, t)
        except ValueError:
            pass
    for r in rows:
        seq, t = evs.get(r[0], (0, r[9]))
        out.append(_row(tuple(r) + (seq, t)))
    out.sort(key=lambda e: (e["effective_at"], e["seq"]))
    return out


def effective(evs, cutoff=None):
    """The verdict summaries use (metrics.md 2.2). Eligible: effective_at <= cutoff and recorded t <= cutoff. Among those, drop every
    evaluation superseded by another eligible one (an automated one cannot supersede an operator one); an operator verdict outranks an
    automated one; then the latest effective_at, ties to the higher event seq. Returns (effective or None, history with `superseded`/`eligible` marks)."""
    elig = [e for e in evs if cutoff is None or (e["effective_at"] <= cutoff and e["t"] <= cutoff)]
    kinds = {e["eval"]: e["evaluator_kind"] for e in evs}
    dead = set()
    for e in elig:
        s = e["supersedes"]
        if s and s in kinds and not (e["evaluator_kind"] == "automated" and kinds[s] == "operator"):
            dead.add(s)
    live = [e for e in elig if e["eval"] not in dead]
    ops = [e for e in live if e["evaluator_kind"] == "operator"]
    pool = ops or live
    best = max(pool, key=lambda e: (e["effective_at"], e["seq"])) if pool else None
    hist = [dict(e, superseded=e["eval"] in dead, eligible=e in elig) for e in evs]
    return best, hist


def effective_status(conn, target, cutoff=None):
    best, _ = effective(evaluations(conn, target), cutoff)
    return best["status"] if best else "unreviewed"


def _target_info(conn, target):
    r = conn.execute("SELECT parent FROM assignment WHERE msg = ?", (target,)).fetchone()
    if r is not None:
        return {"root": r[0] is None}
    r = conn.execute("SELECT data FROM event WHERE action='msg_created' AND msg = ?", (target,)).fetchone()
    if r is None:
        return None
    try:
        return {"root": json.loads(r[0]).get("parent") is None}
    except ValueError:
        return None


def build_verdict(conn, home, target, status, criteria, rubric=None, rubric_version=None, evidence=(), evaluator="owner", evaluator_version="1",
                  evaluator_kind="operator", effective_at=None, supersedes="auto", target_kind=None, rest=None, now=None):
    """Validate and return the audit_evaluation event (not yet stored). criteria: list of (id, outcome[, evidence ref])."""
    now = time.time() if now is None else now
    if not MSG_RE.match(target or ""):
        raise EvalError("target must be a message id (m-<10 hex>)")
    if status not in STATUSES or status == "unreviewed":
        raise EvalError("status must be one of passed, partial, failed, inconclusive")
    if evaluator_kind not in ("operator", "automated"):
        raise EvalError("evaluator kind must be operator or automated")
    for v, rx, what in ((evaluator, IDENT_RE, "evaluator"), (evaluator_version, VER_RE, "evaluator version")):
        if not rx.match(v or ""):
            raise EvalError("%s has an invalid form" % what)
    info = _target_info(conn, target)
    if info is None:
        raise EvalError("no such assignment: %s" % target, "not_found")
    kind = target_kind or ("task" if info["root"] else "assignment")
    if kind not in ("task", "assignment"):
        raise EvalError("target kind must be task or assignment")
    if kind == "task" and not info["root"]:
        raise EvalError("a task verdict needs a root message; %s is a child assignment (use target kind assignment)" % target)
    rname, rver = (rubric, rubric_version_of(home, rubric, rubric_version)) if rubric else ADHOC
    rb = rubric_load(home, rname, rver)
    crit, seen = [], set()
    for c in criteria:
        cid, out = c[0], c[1]
        if not IDENT_RE.match(cid or "") or out not in OUTCOMES or cid in seen:
            raise EvalError("criterion %r=%r is invalid or repeated" % (cid, out))
        seen.add(cid)
        item = {"id": cid, "outcome": out}
        if len(c) > 2 and c[2]:
            check_evidence([c[2]])
            item["evidence"] = c[2]
        crit.append(item)
    if rb is not None:
        known = {c["id"]: c["required"] for c in rb["criteria"]}
        for c in crit:
            if c["id"] not in known:
                raise EvalError("criterion %s is not in rubric %s %s" % (c["id"], rname, rver))
        missing = [i for i in known if i not in seen]
        if missing and rest:
            if rest not in OUTCOMES:
                raise EvalError("--rest must be passed, failed or not_assessed")
            crit += [{"id": i, "outcome": rest} for i in missing]
        required = [i for i, req in known.items() if req]
    else:
        if not crit:
            raise EvalError("an ad-hoc verdict needs at least one --criterion")
        required = [c["id"] for c in crit]
    outcomes = {c["id"]: c["outcome"] for c in crit}
    missing = [i for i in required if i not in outcomes]
    derived = derive_status(outcomes, required)
    if missing and status != "inconclusive":
        raise EvalError("criteria %s have no outcome: give each one, or use --rest passed|failed|not_assessed" % ",".join(missing))
    if derived != status:
        raise EvalError("status %s contradicts the criteria: they give %s" % (status, derived))
    refs = list(evidence)
    check_evidence(refs)
    evs = evaluations(conn, target)
    cur, _ = effective(evs, None)
    if supersedes == "auto":
        supersedes = cur["eval"] if (cur is not None and evaluator_kind == "operator") else None
    elif supersedes:
        if not EID_RE.match(supersedes) or supersedes not in {e["eval"] for e in evs}:
            raise EvalError("supersedes must name an evaluation of the same target", "not_found")
    else:
        supersedes = None
    eff = now if effective_at is None else float(effective_at)
    data = {"eval": "e-" + os.urandom(5).hex(), "target_kind": kind, "target": target, "status": status, "evaluator_kind": evaluator_kind,
            "evaluator": evaluator, "evaluator_version": evaluator_version, "rubric": rname, "rubric_version": rver, "effective_at": eff,
            "supersedes": supersedes, "criteria": crit}
    if refs:
        data["evidence"] = refs
    return {"v": 1, "id": "%s-1" % os.urandom(8).hex(), "t": round(now, 3), "action": "audit_evaluation",
            "actor": "operator" if evaluator_kind == "operator" else "system", "origin": "live", "msg": target, "data": data}


def payload_sha(ev):
    body = {k: v for k, v in ev.items() if k != "id"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def store_event(conn, ev):
    """Insert the event and project it into `evaluation` in ONE transaction; append the audit_log row; bump the generation once.
    Idempotent on the event id (a repeat is a no-op), so a re-import of the same spool line never doubles a verdict."""
    d = ev["data"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        g0 = conn.execute("SELECT generation FROM store_state").fetchone()[0]
        if conn.execute("SELECT 1 FROM event WHERE id = ?", (ev["id"],)).fetchone():
            conn.execute("ROLLBACK")
            return False
        conn.execute("INSERT INTO event (id, payload_sha, t, action, actor, origin, msg, data) VALUES (?,?,?,?,?,?,?,?)",
                     (ev["id"], payload_sha(ev), ev["t"], ev["action"], ev["actor"], ev["origin"], ev.get("msg"), json.dumps(d, sort_keys=True)))
        conn.execute("INSERT OR IGNORE INTO evaluation (eval,target_kind,target,status,evaluator_kind,evaluator,evaluator_version,rubric,rubric_version,effective_at,supersedes,criteria,evidence)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (d["eval"], d["target_kind"], d["target"], d["status"], d["evaluator_kind"], d["evaluator"], d["evaluator_version"], d["rubric"],
                      d["rubric_version"], d["effective_at"], d["supersedes"], json.dumps(d["criteria"], sort_keys=True), json.dumps(d["evidence"]) if d.get("evidence") else None))
        conn.execute("INSERT INTO audit_log (t, action, actor, target, outcome, detail) VALUES (?, 'audit_evaluation', ?, ?, 'ok', ?)",
                     (ev["t"], ev["actor"], ev.get("msg"), d["status"]))
        conn.execute("UPDATE store_state SET generation = ?", (g0 + 1,))  # the triggers bumped it per row; the contract is once per write transaction
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return True


def project_missing(conn, in_tx=False):
    """Importer hook: turn audit_evaluation events that have no evaluation row yet into rows (same store_event rules, by event). Returns the count.
    in_tx: the caller holds the write transaction and owns the single generation bump (importer.drain)."""
    n = 0
    have = {r[0] for r in conn.execute("SELECT eval FROM evaluation")}
    todo = []
    for eid, t, actor, msg, data in conn.execute("SELECT id, t, actor, msg, data FROM event WHERE action='audit_evaluation' ORDER BY seq"):
        d = json.loads(data)
        if d.get("eval") not in have:
            todo.append((eid, t, actor, msg, d))
    if not todo:
        return 0
    if not in_tx:
        conn.execute("BEGIN IMMEDIATE")
    g0 = conn.execute("SELECT generation FROM store_state").fetchone()[0]
    for eid, t, actor, msg, d in todo:
        conn.execute("INSERT OR IGNORE INTO evaluation (eval,target_kind,target,status,evaluator_kind,evaluator,evaluator_version,rubric,rubric_version,effective_at,supersedes,criteria,evidence)"
                     " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (d["eval"], d["target_kind"], d["target"], d["status"], d["evaluator_kind"], d["evaluator"], d["evaluator_version"], d["rubric"],
                      d["rubric_version"], d["effective_at"], d.get("supersedes"), json.dumps(d["criteria"], sort_keys=True), json.dumps(d["evidence"]) if d.get("evidence") else None))
        n += 1
    if not in_tx:
        conn.execute("UPDATE store_state SET generation = ?", (g0 + 1,))
        conn.execute("COMMIT")
    return n


def record_verdict(conn, home, target, status, criteria, **kw):
    ev = build_verdict(conn, home, target, status, criteria, **kw)
    store_event(conn, ev)
    return ev
