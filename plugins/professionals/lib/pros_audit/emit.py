"""Audit event capture (docs/spec/audit/events.md): one JSON line per event into this process's spool file.

Rules (events.md section 1): never raises, never blocks, no fsync, one write() per event. After any write error the
degraded flag is set, the file is abandoned (a torn last line is never followed by a valid event) and the next event
opens a new file, led by a `write_error` gap. At the size cap events are dropped and the degraded flag says so.
Standard library only; nothing here imports sqlite3 (the importer does)."""
import hashlib
import json
import os
import re
import time

SPOOL_CAP = 64 * 1024 * 1024
TASK_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_write = os.write  # test seam: tests replace it to inject write errors
_ST = {"pid": None, "writer": None, "seq": 0, "n": 0, "bytes": 0, "degraded": False, "gap": None, "dropped": 0, "off": None, "home": None}


def hash12(text):
    return hashlib.sha256(str(text).encode("utf-8", "replace")).hexdigest()[:12]


def task_token(raw):
    """(token, form): ids that do not fit the pattern are stored as h-<12 hex> with form 'hashed' (events.md 3b)."""
    raw = "" if raw is None else str(raw)
    if TASK_OK.match(raw):
        return raw, None
    return "h-" + hash12(raw), "hashed"


def _reset(home):
    _ST.update(pid=os.getpid(), writer=os.urandom(8).hex(), seq=0, n=0, bytes=0, degraded=False, gap=None, dropped=0, off=None, home=str(home))


def _disabled(home):
    if os.environ.get("PRO_AUDIT") in ("0", "off"):
        return True
    if _ST["off"] is None:
        off = False
        try:
            with open(os.path.join(str(home), "config.json"), encoding="utf-8") as f:
                off = (json.load(f) or {}).get("capture_mode") == "off"
        except (OSError, ValueError, AttributeError):
            pass
        _ST["off"] = off
    return _ST["off"]


def spool_dir(home):
    return os.path.join(str(home), "audit", "spool")


def _dir(home):
    d = spool_dir(home)
    if not os.path.isdir(d):
        os.makedirs(d, mode=0o700, exist_ok=True)
        for p in (os.path.dirname(d), d):
            os.chmod(p, 0o700)
    return d


def _flag(home, why):
    """Degraded flag: in memory and a small file the status command reads. Best effort, never raises."""
    _ST["degraded"] = True
    try:
        fd = os.open(os.path.join(str(home), "audit", "degraded.json"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps({"since": time.time(), "why": why, "dropped": _ST["dropped"], "writer": _ST["writer"]}).encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def degraded():
    return bool(_ST["degraded"])


def _line(ev):
    return (json.dumps(ev, sort_keys=True, separators=(", ", ": ")) + "\n").encode("utf-8")


def _append(home, ev):
    """Write one line. Raises OSError on any failure or short write."""
    path = os.path.join(_dir(home), "%s.%d.jsonl" % (_ST["writer"], _ST["n"]))
    data = _line(ev)
    if _ST["bytes"] + len(data) > SPOOL_CAP:
        raise OverflowError("spool cap")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if _write(fd, data) != len(data):
            raise OSError("short write")
    finally:
        os.close(fd)
    _ST["bytes"] += len(data)


def emit(home, action, data, actor, env=None, origin="live", provenance=None):
    """Append one event; returns its id or None. Never raises."""
    try:
        if os.environ.get("PRO_AUDIT") in ("0", "off"):
            return None
        if _ST["pid"] != os.getpid() or _ST["home"] != str(home):
            _reset(home)
        if _disabled(home):
            return None
        _ST["seq"] += 1
        ev = {"v": 1, "id": "%s-%d" % (_ST["writer"], _ST["seq"]), "t": round(time.time(), 3), "action": action, "actor": actor, "origin": origin, "data": data}
        if provenance:
            ev["provenance"] = provenance
        for k, v in (env or {}).items():
            if k in ("pro", "pro_name", "run", "session", "msg") and (v is not None or k == "session"):
                ev[k] = v
        if _ST["dropped"]:
            _ST["dropped"] += 1
            return None
        if _ST["gap"]:  # the previous file ended in an error: say so first, in the new file
            g = _ST["gap"]
            gev = {"v": 1, "id": "%s-%d" % (_ST["writer"], _ST["seq"]), "t": ev["t"], "action": "audit_gap", "actor": "system", "origin": "live",
                   "data": {"kind": "write_error", "source": "spool", "from_t": g["from"], "to_t": ev["t"], "count": g["count"]}}
            _ST["seq"] += 1
            ev["id"] = "%s-%d" % (_ST["writer"], _ST["seq"])
            _append(home, gev)
            _ST["gap"] = None
        _append(home, ev)
        return ev["id"]
    except OverflowError:
        _ST["dropped"] = 1
        _flag(home, "spool_overflow")
    except Exception as e:  # noqa: BLE001 - capture must never break pros
        try:
            g = _ST["gap"] or {"from": time.time(), "count": 0}
            g["count"] += 1
            _ST.update(gap=g, n=_ST["n"] + 1, bytes=0)
            _flag(home, "write_error:%s" % type(e).__name__)
        except Exception:  # noqa: BLE001
            pass
    return None
