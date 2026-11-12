"""Saved report definitions, local rendered snapshots, revision comparison, CSV and self-contained HTML export (spec 037 F2).

A definition (name + filters + grouping) is a row in `report_def`; versions of one name are kept, the newest is current. A snapshot is the
query_metrics envelope for a definition, stored as `audit/reports/snapshots/<id>.json` (0600) together with what makes it reproducible: the
store snapshot id, the cutoff, the metric version, the rubric versions on disk and the professional revisions the figures cover. The id
derives from definition + store snapshot + cutoff, so the same state renders to the same snapshot (no duplicates, reproducible).
Exports are redacted: group keys, filter values and the report name become per-export pseudonyms (export.Redactor), times floor to whole
seconds, and the finished CSV/HTML is scanned for every user-chosen token in the store before it is written. Standard library only."""
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import time

from . import export as ex
from . import query as Q

METRIC_VERSION = "m1"  # card definitions of docs/spec/audit/metrics.md; bump when a card's meaning changes
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
SPEC_KEYS = ("since", "until", "group_by") + Q.FILTERS
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
SNAP_FORMAT = "pros-audit-report"


class ReportError(Exception):
    def __init__(self, msg, code="bad_request"):
        Exception.__init__(self, msg)
        self.code = code


def reports_dir(home):
    return os.path.join(str(home), "audit", "reports")


def _write_new(path, body):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    return True


def _canon(x):
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def _h(*parts):
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- definitions
def clean_spec(spec):
    out = {}
    for k, v in (spec or {}).items():
        if k not in SPEC_KEYS:
            raise ReportError("unknown report field %r" % k)
        if v is None or v == "":
            continue
        if k == "group_by":
            v = v.split(",") if isinstance(v, str) else list(v)
            for g in v:
                if g not in Q.GROUPS:
                    raise ReportError("unknown group %r (use %s)" % (g, ", ".join(Q.GROUPS)))
        out[k] = v
    if "since" not in out:
        out["since"] = "30d"
    return out


def save_definition(conn, name, spec, now=None):
    """Store a definition; the same content under the same name is a no-op. Returns the definition dict."""
    if not NAME_RE.match(name or ""):
        raise ReportError("report name: 1 to 48 letters, digits, dot, dash or underscore")
    now = time.time() if now is None else now
    spec = clean_spec(spec)
    did = "rd-" + _h(name, _canon(spec))
    row = conn.execute("SELECT id FROM report_def WHERE id = ?", (did,)).fetchone()
    if row is None:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO report_def (id, name, definition, created) VALUES (?,?,?,?)", (did, name, _canon(spec), now))
        conn.execute("COMMIT")
    return {"id": did, "name": name, "spec": spec, "created": now if row is None else conn.execute("SELECT created FROM report_def WHERE id=?", (did,)).fetchone()[0]}


def load_definition(conn, name):
    r = conn.execute("SELECT id, name, definition, created FROM report_def WHERE name = ? ORDER BY created DESC, rowid DESC LIMIT 1", (name,)).fetchone()
    if r is None:
        raise ReportError("no saved report named %s" % name, "not_found")
    return {"id": r[0], "name": r[1], "spec": json.loads(r[2]), "created": r[3]}


def list_definitions(conn):
    out = []
    for name, in conn.execute("SELECT DISTINCT name FROM report_def ORDER BY name").fetchall():
        d = load_definition(conn, name)
        d["versions"] = conn.execute("SELECT COUNT(*) FROM report_def WHERE name = ?", (name,)).fetchone()[0]
        out.append(d)
    return out


# --------------------------------------------------------------------------- snapshots
def _args(spec, now):
    a = dict(spec)
    for k in ("since", "until"):
        if k in a:
            a[k] = ex.parse_when(str(a[k]), now)
    return a


def rubric_versions(home):
    d = os.path.join(str(home), "audit", "rubrics")
    out = {}
    try:
        for name in sorted(os.listdir(d)):
            vs = sorted(f[:-5] for f in os.listdir(os.path.join(d, name)) if f.endswith(".json"))
            if vs:
                out[name] = vs
    except OSError:
        pass
    return out


def _source_revisions(conn, spec, cutoff):
    if spec.get("revision"):
        return [spec["revision"]]
    return [r[0] for r in conn.execute("SELECT DISTINCT revision FROM run WHERE revision IS NOT NULL AND started <= ? ORDER BY revision", (cutoff,))]


def render_snapshot(q, conn, home, definition, now=None):
    """Run the definition now, store the snapshot file (once per distinct state) and return it with its path."""
    now = time.time() if now is None else now
    env = q.call("query_metrics", _args(definition["spec"], now))
    if "error" in env:
        raise ReportError("%s: %s" % (env["error"]["code"], env["error"]["message"]), env["error"]["code"])
    cutoff = env["coverage"]["cutoff"]
    sid = "rs-" + _h(definition["id"], env["snapshot"]["id"], repr(cutoff), METRIC_VERSION)
    snap = {"format": SNAP_FORMAT, "version": 1, "id": sid, "report": {"name": definition["name"], "definition": definition["id"]}, "spec": definition["spec"],
            "created_at": round(now, 3), "metric_version": METRIC_VERSION, "api_version": env["api_version"], "store_snapshot": env["snapshot"],
            "cutoff": cutoff, "rubric_versions": rubric_versions(home), "source_revisions": _source_revisions(conn, definition["spec"], cutoff),
            "coverage": env["coverage"], "result": env["result"]}
    path = os.path.join(reports_dir(home), "snapshots", sid + ".json")
    if not _write_new(path, json.dumps(snap, sort_keys=True, indent=1) + "\n"):
        snap = load_snapshot(home, sid)  # same state already rendered: reuse it
    return snap, path


def load_snapshot(home, sid):
    if not re.fullmatch(r"rs-[0-9a-f]{12}", sid or ""):
        raise ReportError("not a snapshot id", "not_found")
    try:
        with open(os.path.join(reports_dir(home), "snapshots", sid + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise ReportError("no snapshot %s" % sid, "not_found")


def list_snapshots(home, name=None):
    d = os.path.join(reports_dir(home), "snapshots")
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for f in names:
        if f.endswith(".json"):
            try:
                s = load_snapshot(home, f[:-5])
            except ReportError:
                continue
            if name is None or s["report"]["name"] == name:
                out.append(s)
    return sorted(out, key=lambda s: (s["created_at"], s["id"]))


# --------------------------------------------------------------------------- rows (one flat shape for compare, CSV and HTML)
def _frac(r):
    return None if not r or not r[1] else r[0] / r[1]


def card_row(cards):
    ev = cards["evaluation"]["root_tasks"]
    return {"assignments": cards["assigned_work"]["assignments"], "pass_num": (ev["pass_rate"] or [None, None])[0], "pass_den": (ev["pass_rate"] or [None, None])[1],
            "eval_coverage_num": cards["evaluation"]["coverage"][0], "eval_coverage_den": cards["evaluation"]["coverage"][1],
            "deadline_missed": cards["deadline_miss"]["all"]["rate"][0], "deadline_eligible": cards["deadline_miss"]["all"]["rate"][1],
            "queue_p50_s": cards["durations"]["queue"]["p50_s"], "reply_p50_s": cards["durations"]["reply_latency_from_first_delivery"]["p50_s"],
            "tokens": cards["accounted_tokens"]["total_unique"], "cost_usd": cards["accounted_tokens"]["cost_usd_api_equivalent"], "price_state": cards["accounted_tokens"]["price_state"]}


def snapshot_rows(snap):
    """Group rows (or one `all` row) of a snapshot: key columns, n, small_sample, then the card figures."""
    res = snap["result"]
    gb = [g for g in res["group_by"] if g != "none"]
    if not gb:
        return [dict({"group": "all", "n": res["cards"]["assigned_work"]["assignments"], "small_sample": res["cards"]["assigned_work"]["assignments"] < 5}, **card_row(res["cards"]))]
    out = []
    for g in res.get("groups", []):
        key = dict(g["key"])
        out.append(dict({"group": ",".join("%s=%s" % (k, key.get(k)) for k in gb), "n": g["n"], "small_sample": g["small_sample"]}, **{"key_" + k: key.get(k) for k in gb},
                        **card_row(g["cards"])))
    return out


# --------------------------------------------------------------------------- compare by revision
def compare_by_revision(q, conn, spec, now=None):
    """Same window and filters, one row per professional revision, deltas against the earliest revision. Observational: no causal claim."""
    now = time.time() if now is None else now
    spec = clean_spec(dict(spec, group_by="revision"))
    env = q.call("query_metrics", _args(spec, now))
    if "error" in env:
        raise ReportError("%s: %s" % (env["error"]["code"], env["error"]["message"]), env["error"]["code"])
    first = {r[0]: r[1] for r in conn.execute("SELECT revision, MIN(first_seen) FROM revision GROUP BY revision")}
    rows = []
    for g in env["result"].get("groups", []):
        rev = g["key"]["revision"]
        r = dict({"revision": rev, "first_seen": first.get(rev), "n": g["n"], "small_sample": g["small_sample"]}, **card_row(g["cards"]))
        r["pass_rate"], r["deadline_miss_rate"] = _frac([r["pass_num"], r["pass_den"]]) if r["pass_den"] else None, _frac([r["deadline_missed"], r["deadline_eligible"]])
        rows.append(r)
    rows.sort(key=lambda r: (r["revision"] == "unknown", r["first_seen"] if r["first_seen"] is not None else 1e18, str(r["revision"])))
    base = next((r for r in rows if r["revision"] != "unknown"), None)
    for r in rows:
        for k in ("pass_rate", "deadline_miss_rate"):
            ok = base is not None and r is not base and r[k] is not None and base[k] is not None and not r["small_sample"] and not base["small_sample"]
            r["delta_" + k] = round(r[k] - base[k], 4) if ok else None
        r["baseline"] = r is base
    return {"by": "revision", "spec": spec, "store_snapshot": env["snapshot"], "cutoff": env["coverage"]["cutoff"], "coverage": env["coverage"], "rows": rows,
            "note": "observational: revisions differ in time, load and task mix; groups under n=5 carry no delta"}


def render_compare(c):
    out = ["compare by revision, cutoff %s, snapshot %s" % (c["cutoff"], c["store_snapshot"]["id"])]
    for r in c["rows"]:
        pr = "n/a" if r["pass_rate"] is None else "%d/%d" % (r["pass_num"], r["pass_den"])
        dm = "n/a" if r["deadline_miss_rate"] is None else "%d/%d" % (r["deadline_missed"], r["deadline_eligible"])
        dl = " delta pass %+.2f" % r["delta_pass_rate"] if r["delta_pass_rate"] is not None else ""
        out.append("%s%s: n=%d%s, pass %s, deadline miss %s, tokens %d%s" % (r["revision"], " (baseline)" if r["baseline"] else "", r["n"], " (small sample)" if r["small_sample"] else "", pr, dm, r["tokens"], dl))
    out.append(c["note"])
    return "\n".join(out)


# --------------------------------------------------------------------------- export (CSV, self-contained HTML)
def sensitive_tokens(conn, red, extra=()):
    """Every user-chosen value in the store that a redacted output must not contain."""
    sql = ("SELECT task FROM assignment UNION SELECT name FROM professional UNION SELECT evaluator FROM evaluation UNION SELECT rubric FROM evaluation "
           "UNION SELECT rubric_version FROM evaluation UNION SELECT evaluator_version FROM evaluation")
    toks = {r[0] for r in conn.execute(sql) if r[0]}
    for (c,) in conn.execute("SELECT criteria FROM evaluation"):
        try:
            toks |= {x.get("id") for x in json.loads(c) if isinstance(x, dict) and x.get("id")}
        except ValueError:
            pass
    toks -= {n for n in red.market}  # marketplace names are public and kept
    toks |= {x for x in extra if x}
    return {t for t in toks if len(t) >= 4 and t not in ("user", "owner", "operator", "check", "unknown", "unclassified")}


def scan(body, tokens):
    """Raise when any sensitive token appears in the rendered output (case-insensitive)."""
    low = body.lower() if isinstance(body, str) else body.decode("utf-8").lower()
    hit = sorted(t for t in tokens if t.lower() in low)
    if hit:
        raise ReportError("refusing to write: %d private token(s) would leave in the export" % len(hit), "leak")


def _key_value(red, k, v):
    if v in (None, "unknown", "unclassified"):
        return v
    if k == "professional":
        return red.name(v)
    if k == "revision":
        return red.pseudo("revision", v)
    if k == "task_class":
        return red.pseudo("label", v)
    if k == "model":
        return v if MODEL_RE.match(v) else red.pseudo("model", v)
    return v if re.fullmatch(r"[a-z0-9_-]{1,32}|\d{4}-\d{2}-\d{2}", str(v)) else red.pseudo("label", v)


def redacted_view(conn, snap, key=None):
    """(redactor, view): the flat rows and header facts of a snapshot with every user-chosen value pseudonymized."""
    market = [r[0] for r in conn.execute("SELECT name FROM professional WHERE origin='marketplace'")]
    red = ex.Redactor(False, market, key)
    rows = []
    for r in snapshot_rows(snap):
        r = dict(r)
        for k in [k for k in r if k.startswith("key_")]:
            r[k] = _key_value(red, k[4:], r[k])
        if "group" in r and r["group"] != "all":
            r["group"] = ",".join("%s=%s" % (k[4:], r[k]) for k in sorted(r) if k.startswith("key_"))
        rows.append(r)
    spec = {k: (_key_value(red, k, v) if k in Q.FILTERS else v) for k, v in snap["spec"].items()}
    for k in ("since", "until"):
        if k in spec:
            spec[k] = str(spec[k])
    view = {"report": red.pseudo("label", snap["report"]["name"]), "snapshot": snap["id"], "metric_version": snap["metric_version"], "cutoff": red.time(snap["cutoff"]),
            "store_snapshot": red.pseudo("label", snap["store_snapshot"]["id"]), "spec": spec, "revisions": [red.pseudo("revision", x) for x in snap["source_revisions"]],
            "rubric_versions": {red.pseudo("label", k): [red.pseudo("label", v) for v in vs] for k, vs in snap["rubric_versions"].items()}, "rows": rows}
    return red, view


def to_csv(view):
    rows = view["rows"]
    cols = []
    for r in rows:
        cols += [c for c in r if c not in cols]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([ex._cell(r.get(c)) for c in cols])
    return buf.getvalue()


def to_html(view):
    import html
    e = lambda x: html.escape("" if x is None else (_canon(x) if isinstance(x, (dict, list)) else str(x)), quote=True)
    rows = view["rows"]
    cols = []
    for r in rows:
        cols += [c for c in r if c not in cols]
    parts = ['<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
             '<meta name="generator" content="pros audit report export">',
             "<title>Audit report %s</title>" % e(view["report"]),
             "<style>body{font:14px system-ui,sans-serif;margin:16px}table{border-collapse:collapse;margin:8px 0 24px}td,th{border:1px solid #888;padding:2px 6px;text-align:left}</style>",
             "</head><body><h1>Audit report %s</h1>" % e(view["report"]),
             "<p>Snapshot %s, metrics %s, cutoff %s. Filters: %s. Revisions: %s. Rubric versions: %s.</p>" % (e(view["snapshot"]), e(view["metric_version"]), e(view["cutoff"]), e(view["spec"]), e(view["revisions"]), e(view["rubric_versions"])),
             "<table><tr>%s</tr>" % "".join("<th>%s</th>" % e(c) for c in cols)]
    for r in rows:
        parts.append("<tr>%s</tr>" % "".join("<td>%s</td>" % e(r.get(c)) for c in cols))
    parts.append("</table><p>Groups under n=5 are marked small_sample. Figures are observational.</p></body></html>\n")
    return "".join(parts)


def export_snapshot(conn, snap, fmt, out_path, key=None):
    """Write a redacted CSV or HTML of a snapshot to out_path (0600, never overwriting). The output is scanned for private tokens first."""
    if fmt not in ("csv", "html"):
        raise ReportError("format must be csv or html")
    red, view = redacted_view(conn, snap, key)
    body = to_csv(view) if fmt == "csv" else to_html(view)
    scan(body, sensitive_tokens(conn, red, [snap["report"]["name"]]))
    if not _write_new(out_path, body):
        raise ReportError("%s exists; nothing written" % out_path)
    return {"path": out_path, "format": fmt, "snapshot": snap["id"], "bytes": len(body.encode("utf-8")), "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}


# --------------------------------------------------------------------------- CLI
def add_parsers(sub):
    r = sub.add_parser("report", help="saved report definitions and local snapshots (owner only)")
    rs = r.add_subparsers(dest="rpsub", required=True)
    for n in ("save", "run"):
        sp = rs.add_parser(n)
        sp.add_argument("name")
        sp.add_argument("--json", action="store_true")
        if n == "save":
            _spec_args(sp)
    rs.add_parser("list").add_argument("--json", action="store_true")
    sp = rs.add_parser("show")
    sp.add_argument("snapshot")
    sp = rs.add_parser("export")
    sp.add_argument("snapshot")
    sp.add_argument("--format", choices=["csv", "html"], default="csv")
    sp.add_argument("--out", required=True)
    sp.add_argument("--json", action="store_true")
    c = sub.add_parser("compare", help="compare groups of one window")
    c.add_argument("--by", choices=["revision"], required=True)
    _spec_args(c, group=False)
    c.add_argument("--json", action="store_true")


def _spec_args(sp, group=True):
    sp.add_argument("--since", default="30d")
    sp.add_argument("--until")
    if group:
        sp.add_argument("--by", dest="group_by", help="comma list: " + ", ".join(Q.GROUPS))
    for f in Q.FILTERS:
        sp.add_argument("--" + f.replace("_", "-"), dest=f)


def _spec(a):
    return {k: getattr(a, k, None) for k in SPEC_KEYS if getattr(a, k, None) is not None}


def run_cli(a, home, out, err, now=None):
    """`pros audit report ...` and `pros audit compare`; bin/pros has already required the owner."""
    from . import cli
    now = time.time() if now is None else now
    try:
        conn = cli.open_db(home)
    except Exception as e:  # EvalError: no store yet
        print("pros: %s" % e, file=err)
        return 3
    q = Q.Query(cli.store_path(home), owner_check=lambda: True, now=lambda: now)
    try:
        if a.sub == "compare":
            c = compare_by_revision(q, conn, _spec(a), now)
            print(json.dumps(c, sort_keys=True, indent=1) if a.json else render_compare(c), file=out)
            return 0
        s = a.rpsub
        if s == "save":
            d = save_definition(conn, a.name, _spec(a), now)
            print(json.dumps(d, sort_keys=True) if a.json else "saved report %s (%s)" % (d["name"], d["id"]), file=out)
        elif s == "run":
            snap, path = render_snapshot(q, conn, home, load_definition(conn, a.name), now)
            print(json.dumps({"snapshot": snap["id"], "path": path}) if a.json else "snapshot %s at %s (cutoff %s, %d row(s))" % (snap["id"], path, snap["cutoff"], len(snapshot_rows(snap))), file=out)
        elif s == "list":
            ds, ss = list_definitions(conn), list_snapshots(home)
            if a.json:
                print(json.dumps({"reports": ds, "snapshots": [{"id": x["id"], "name": x["report"]["name"], "cutoff": x["cutoff"]} for x in ss]}, sort_keys=True), file=out)
            else:
                for d in ds:
                    print("%s  %s  %s  %s" % (d["name"], d["id"], _canon(d["spec"]), "%d snapshot(s)" % sum(1 for x in ss if x["report"]["name"] == d["name"])), file=out)
        elif s == "show":
            snap = load_snapshot(home, a.snapshot)
            for r in snapshot_rows(snap):
                print(_canon(r), file=out)
        else:
            snap = load_snapshot(home, a.snapshot)
            m = export_snapshot(conn, snap, a.format, a.out)
            print(json.dumps(m, sort_keys=True) if a.json else "wrote %s (%s, redacted, %d bytes)" % (m["path"], m["format"], m["bytes"]), file=out)
        return 0
    except ReportError as e:
        print("pros: %s" % e, file=err)
        return 3 if e.code == "not_found" else 2
    except (sqlite3.Error, ex.ExportError) as e:
        print("pros: %s" % e, file=err)
        return 2
    finally:
        conn.close()
