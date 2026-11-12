"""`pros audit ...` commands. Read side (D3, docs/spec/audit/query.md section 4): status, summary, trace, explain, import, serve; every figure
comes from query.Query and text is rendered from the same envelope as --json. Write side (D4): verdict, rubric add, export.
One parser; bin/pros has already run require_user, and main() checks again before any store access."""
import argparse
import datetime
import json
import os
import sqlite3
import sys
import time

from . import evaluation as ev
from . import export as ex
from . import query as Q

EVAL_SUBS = ("verdict", "rubric", "export")

EXIT = {"bad_request": 2, "too_large": 2, "not_found": 3, "forbidden": 4, "unauthorized": 4, "store_unavailable": 5}


def store_path(home):
    return os.path.join(str(home), "audit", "audit.db")


def dumps(env):
    return json.dumps(env, indent=1, sort_keys=True)


def _ts(t):
    return "-" if t is None else datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _ratio(r):
    return "n/a" if r is None else "%d/%d" % tuple(r)


def _head(env):
    s, c = env["snapshot"], env["coverage"]
    lines = ["snapshot %s (generation %d, max_seq %d, pricing %s %s)" % (s["id"], s["generation"], s["max_seq"], s["pricing"]["table_id"], s["pricing"]["dated"]),
             "coverage cutoff %s; sources: %s" % (_ts(c["cutoff"]), ", ".join("%s=%s" % (x["source"], x["quality"]) for x in c["sources"]))]
    if c["degraded"]:
        lines.append("DEGRADED: %s (figures may be incomplete)" % c.get("degraded_cause", "unknown cause"))
    if c["pre_audit_before"] is not None:
        lines.append("before %s only backfilled data exists" % _ts(c["pre_audit_before"]))
    if c["gaps"]:
        lines.append("gaps: " + ", ".join("%s=%d" % (g["kind"], g["count"]) for g in c["gaps"]))
    return lines


def render_summary(env):
    r, cards = env["result"], env["result"]["cards"]
    w = r["window"]
    out = _head(env) + ["window %s .. %s" % (_ts(w["from"]), _ts(w["to"])), ""]
    a = cards["assigned_work"]
    out.append("Assigned work: %d assignments (%d root, %d child); refusals %s" % (a["assignments"], a["root_tasks"], a["child_assignments"], a["refusals_by_reason"] or "none"))
    e = cards["evaluation"]
    for k in ("root_tasks", "child_assignments"):
        u = e[k]
        out.append("Evaluation (%s): coverage %s, pass rate %s, counts %s, not completed %d" % (u["unit"], _ratio(u["coverage"]), _ratio(u["pass_rate"]), ", ".join("%s %d" % kv for kv in u["counts"].items()), u["not_completed"]))
    d = cards["deadline_miss"]
    for k in ("all", "root_tasks", "child_assignments"):
        out.append("Deadline misses (%s): %s elapsed, late answers among missed %d, not yet due %d" % (k, _ratio(d[k]["rate"]), d[k]["late_answers_among_missed"], d[k]["not_yet_due"]))
    du = cards["durations"]
    for k, name in (("queue", "Queue time"), ("reply_latency_from_first_delivery", "Reply latency"), ("active_model_tool_time", "Active time")):
        x = du[k]
        out.append("%s: p50 %s s, p95 %s s (n %d of %d)" % (name, x["p50_s"], x["p95_s"], x["n"], x["eligible"]))
    t = cards["accounted_tokens"]
    out.append("Tokens: %d unique over %d exchanges = %d linked + %d session-only; unavailable %s (%s)" % (t["total_unique"], t["exchanges"], t["linked_task"], t["session_only"], t["unavailable"], t["unavailable_note"]))
    out.append("  cost %s USD API-equivalent (%s), unpriced share %s; confidence %s" % (t["cost_usd_api_equivalent"], t["price_state"], _ratio(t["unpriced_share"]), t["confidence"]))
    for g in r.get("groups", []):
        flag = " (small sample)" if g["small_sample"] else ""
        out.append("")
        out.append("Group %s: n=%d%s; assignments %d, tokens %d" % (", ".join("%s=%s" % kv for kv in g["key"].items()), g["n"], flag, g["cards"]["assigned_work"]["assignments"], g["cards"]["accounted_tokens"]["total_unique"]))
    return "\n".join(out)


def render_trace(env):
    r = env["result"]
    out = _head(env) + ["trace of %s (tasks: %s)" % (r["root"], ", ".join(r["tasks"])), ""]
    for a in r["assignments"]:
        ev = a["evaluation"]
        out.append("%s%s task=%s to=%s  delivery=%s deadline=%s evaluation=%s confidence=%s" % ("  " * a["depth"], a["msg"], a["task"], a["to"], a["delivery"], a["deadline_status"], ev["status"] if ev else "unreviewed", a["confidence"]))
        out.append("%s  created %s, delivered %s, answered %s%s" % ("  " * a["depth"], _ts(a["created"]), _ts(a["first_delivery"]), _ts(a["answered_at"]), "" if "own_tokens" in a else "; no usage observed"))
        if "own_tokens" in a:
            o = a["own_tokens"]
            out.append("%s  tokens %d (in %d, out %d, cache write %d, cache read %d), cost %s (%s)" % ("  " * a["depth"], o["total"], o["input"], o["output"], o["cache_write"], o["cache_read"], o["cost_usd_api_equivalent"], o["price_state"]))
    for w in r["waits"]:
        out.append("wait %s by %s: outcome %s exit %s%s (not a task outcome)" % (w["wait"], w["caller"], w["outcome"], w["exit"], ", reply shadowed" if w["shadowed_reply"] else ""))
    u = r["usage"]
    out.append("usage: inclusive %d tokens; session-only %d; unavailable %s (%s)" % (u["inclusive"]["total"], sum(s["tokens"]["total"] for s in u["session_only"]), u["unavailable"], u["residual_note"]))
    out.append("spans: %d (derived from wall-clock records)" % len(r["spans"]))
    for g in r["gaps"]:
        out.append("gap: %s" % json.dumps(g, sort_keys=True))
    return "\n".join(out)


def render_explain(env):
    r = env["result"]
    out = _head(env) + ["", "%s: %s / %s %s" % (r["metric"], r["numerator"], r["denominator"], r["unit"]), "definition: " + r["definition"], "coverage: " + r["coverage_note"]]
    out += ["excluded %s: %d" % (x["reason"], x["count"]) for x in r["exclusions"]]
    out.append("included %d ids%s" % (r["included_total"], ": " + ", ".join(r["included_ids"][:20]) if r["included_ids"] else ""))
    return "\n".join(out)


def render_status(env):
    return "\n".join(_head(env) + ["rows: " + ", ".join("%s %d" % kv for kv in sorted(env["result"].items()))])


def build_parser():
    p = argparse.ArgumentParser(prog="pros audit", description="the local audit: read (status, summary, trace, explain, serve), import, verdicts and export")
    sub = p.add_subparsers(dest="sub", required=True)
    for name in ("status", "summary", "trace", "explain", "import", "serve"):
        sp = sub.add_parser(name)
        if name in ("status", "summary", "trace", "explain"):
            sp.add_argument("--json", action="store_true")
        if name == "trace":
            sp.add_argument("id")
            sp.add_argument("--depth", type=int)
        if name == "explain":
            sp.add_argument("metric")
        if name in ("summary", "explain"):
            sp.add_argument("--since", default="30d")
            sp.add_argument("--until")
            for f in Q.FILTERS:
                sp.add_argument("--" + f.replace("_", "-"), dest=f)
        if name == "summary":
            sp.add_argument("--by", dest="group_by", help="comma list: " + ", ".join(Q.GROUPS))
        if name == "serve":
            sp.add_argument("--port", type=int, default=0)
            sp.add_argument("--rotate", action="store_true")
            sp.add_argument("--manage", action="store_true", help="accept owner management writes from the dashboard (token required; read-only without it)")
        if name == "import":
            sp.add_argument("--backfill", action="store_true", help="also import the surviving history (msgs, sessions) once")
            sp.add_argument("--no-usage", action="store_true", help="skip reading transcripts")
            sp.add_argument("--json", action="store_true")
    _add_eval(sub)
    from . import analysis, reports
    reports.add_parsers(sub)
    analysis.add_parsers(sub)
    return p


def main(argv, home, require_user, now=None, out=None, err=None):
    """Run one command. `require_user` is pros' own check: it raises (or exits) when a professional session calls."""
    out, err = out or sys.stdout, err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        require_user("read the audit")
    except SystemExit:
        raise
    if args.sub in EVAL_SUBS:
        return run(argv, home, out, err, None, now() if callable(now) else now)
    if args.sub == "import":
        return run_import(home, args, out)
    if args.sub in ("report", "compare"):
        from . import reports
        return reports.run_cli(args, home, out, err, now() if callable(now) else now)
    if args.sub == "analyze":
        from . import analysis
        return analysis.run_cli(args, home, out, err, now() if callable(now) else now)
    q = Q.Query(store_path(home), owner_check=lambda: require_user("read the audit") or True, now=now)
    if args.sub == "serve":
        from . import http as H
        return H.serve_cli(q, home, args.port, args.rotate, out, manage=args.manage, now=now)
    if args.sub == "status":
        env, render = q.status(), render_status
    elif args.sub == "summary":
        env = q.call("query_metrics", _clean(args, ("since", "until", "group_by") + Q.FILTERS))
        render = render_summary
    elif args.sub == "trace":
        env, render = q.call("task_trace", _clean(args, ("id", "depth"))), render_trace
    else:
        env, render = q.call("explain_metric", _clean(args, ("metric", "since", "until") + Q.FILTERS)), render_explain
    if "error" in env:
        e = env["error"]
        print("pros: %s" % (dumps(env) if getattr(args, "json", False) else "%s: %s%s" % (e["code"], e["message"], " (%s)" % e["field"] if e.get("field") else "")), file=err)
        return EXIT.get(e["code"], 1)
    print(dumps(env) if getattr(args, "json", False) else render(env), file=out)
    return 0


def run_import(home, args, out):
    """Spool -> store (idempotent), project evaluations, optional backfill, then usage from transcripts (rebuilds the derived tables)."""
    from . import importer
    res = {"spool": importer.drain(home)}
    if args.backfill:
        from . import backfill
        res["backfill"] = backfill.backfill(home)
    if not args.no_usage:
        from . import usage
        res["usage"] = usage.import_usage(home)
    if args.json:
        print(json.dumps(res, sort_keys=True, default=str), file=out)
    else:
        sp = res["spool"]
        print("imported %s events from %s spool files%s" % (sp.get("events", 0), sp.get("spool_files", 0), " (%s)" % sp["skipped"] if "skipped" in sp else ""), file=out)
        for k in ("backfill", "usage"):
            if k in res:
                print("%s: %s" % (k, ", ".join("%s %s" % kv for kv in sorted(res[k].items()) if not isinstance(kv[1], (dict, list)))), file=out)
    return 0


def _clean(args, keys):
    return {k: getattr(args, k) for k in keys if getattr(args, k, None) is not None}


def open_db(home):
    p = os.path.join(str(home), "audit", "audit.db")
    if not os.path.exists(p):
        raise ev.EvalError("no audit store yet (capture has not recorded anything)", "not_found")
    conn = sqlite3.connect(p, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    if not conn.execute("SELECT count(*) FROM sqlite_master WHERE name='schema_version'").fetchone()[0]:
        raise ev.EvalError("the audit store is not initialised", "not_found")
    return conn


def _add_eval(sub):
    """D4 subcommands: verdict, rubric add, export (owner only; bin/pros has run require_user)."""
    v = sub.add_parser("verdict", help="record an evaluation of a task or assignment (owner only)")
    v.add_argument("target", help="message id m-<10 hex>")
    v.add_argument("--status", required=True, choices=["passed", "partial", "failed", "inconclusive"])
    v.add_argument("--criterion", action="append", default=[], help="ID=passed|failed|not_assessed[@kind:token] (repeat)")
    v.add_argument("--rest", choices=["passed", "failed", "not_assessed"], help="outcome for rubric criteria not listed")
    v.add_argument("--rubric", help="rubric name (default: ad-hoc)")
    v.add_argument("--rubric-version", help="needed only when the rubric has several versions")
    v.add_argument("--evidence", action="append", default=[], help="opaque kind:token reference (repeat)")
    v.add_argument("--attach", action="append", default=[], help="LABEL=PATH: an existing test or check result, stored only as check-run:LABEL.<hash>")
    v.add_argument("--evaluator", default="owner")
    v.add_argument("--effective-at", type=float)
    v.add_argument("--supersedes", help="evaluation id to supersede (default: the current effective one)")
    v.add_argument("--no-supersede", action="store_true")
    v.add_argument("--kind", choices=["task", "assignment"], dest="target_kind")
    v.add_argument("--json", action="store_true")
    r = sub.add_parser("rubric", help="rubric files (owner only)")
    rs = r.add_subparsers(dest="rsub", required=True)
    ra = rs.add_parser("add")
    ra.add_argument("name")
    ra.add_argument("version")
    ra.add_argument("--criterion", action="append", required=True, help="ID or ID:optional (repeat)")
    e = sub.add_parser("export", help="write an export (redacted by default)")
    e.add_argument("--redact", action="store_true", help="redacted export (the default)")
    e.add_argument("--raw", action="store_true", help="raw export with real ids: separate, explicit")
    e.add_argument("--confirm", help="typed confirmation for --raw: the word raw")
    e.add_argument("--format", choices=["jsonl", "csv", "html"], default="jsonl")
    e.add_argument("--since")
    e.add_argument("--until")
    e.add_argument("--out")
    e.add_argument("--yes", action="store_true", help="write without asking (scripts)")
    e.add_argument("--preview", action="store_true", help="print the preview and stop")
    e.add_argument("--json", action="store_true")


def _crit(items):
    out = []
    for it in items:
        ref = None
        if "@" in it:
            it, ref = it.split("@", 1)
        if "=" not in it:
            raise ev.EvalError("--criterion needs ID=outcome")
        cid, out_ = it.split("=", 1)
        out.append((cid, out_, ref) if ref else (cid, out_))
    return out


def run(argv, home, out=None, err=None, stdin=None, now=None):
    """The write side (verdict, rubric add, export). `now` is a number here (main() turns its clock into one)."""
    out, err = out or sys.stdout, err or sys.stderr
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if a.sub not in EVAL_SUBS:
        print("pros: %s is a read command; use main()" % a.sub, file=err)
        return 2
    try:
        if a.sub == "rubric":
            crit = [(c.split(":")[0], not c.endswith(":optional")) for c in a.criterion]
            path = ev.rubric_add(home, a.name, a.version, crit)
            print("rubric %s %s written" % (a.name, a.version), file=out)
            return 0
        conn = open_db(home)
        try:
            if a.sub == "verdict":
                return _verdict(a, conn, home, out, now)
            return _export(a, conn, home, out, err, stdin, now)
        finally:
            conn.close()
    except (ev.EvalError, ex.ExportError) as e:
        print("pros: %s" % e, file=err)
        return 3 if getattr(e, "code", "") == "not_found" else 2


def _verdict(a, conn, home, out, now):
    refs = list(a.evidence)
    for item in a.attach:
        label, _, path = item.partition("=")
        refs.append(ev.evidence_ref_for_file(label, path))
    sup = None if a.no_supersede else (a.supersedes or "auto")
    e = ev.record_verdict(conn, home, a.target, a.status, _crit(a.criterion), rubric=a.rubric, rubric_version=a.rubric_version, evidence=refs,
                          evaluator=a.evaluator, effective_at=a.effective_at, supersedes=sup, target_kind=a.target_kind, rest=a.rest, now=now)
    d = e["data"]
    best, _ = ev.effective(ev.evaluations(conn, a.target), None)
    if a.json:
        print(json.dumps({"eval": d["eval"], "status": d["status"], "supersedes": d["supersedes"], "effective": best["eval"] if best else None}), file=out)
    else:
        print("recorded %s: %s for %s%s; effective now: %s" % (d["eval"], d["status"], a.target, " (supersedes %s)" % d["supersedes"] if d["supersedes"] else "", best["eval"] if best else "none"), file=out)
    return 0


def _export(a, conn, home, out, err, stdin, now):
    now = time.time() if now is None else now
    since, until = ex.parse_when(a.since, now), ex.parse_when(a.until, now)
    raw = a.raw
    if raw and a.redact:
        raise ex.ExportError("choose --raw or --redact, not both")
    pv = ex.preview(conn, since, until, now)
    mode = "raw" if raw else "redacted"
    dest = a.out or os.path.join(os.getcwd(), "pros-audit-export")
    print("export preview: mode %s, format %s, destination %s, cutoff %s" % (mode, a.format, dest, pv["cutoff"]), file=out)
    for k, n in pv["counts"].items():
        print("  %s: %d rows" % (k, n), file=out)
    if a.preview:
        return 0
    src = stdin if stdin is not None else (sys.stdin if sys.stdin.isatty() else None)
    if raw:
        if (a.confirm or "").strip().lower() != "raw":
            if src is None:
                print("pros: a raw export needs --confirm raw (typed confirmation); --yes is not enough", file=err)
                return 2
            print("type raw to write an export with real ids: ", end="", file=out)
            if src.readline().strip().lower() != "raw":
                print("not confirmed; nothing written", file=err)
                return 2
    elif not a.yes:
        if src is None:
            print("pros: nothing written; add --yes to confirm the preview", file=err)
            return 2
        print("write it? [y/N] ", end="", file=out)
        if src.readline().strip().lower() not in ("y", "yes"):
            print("nothing written", file=err)
            return 2
    degraded = os.path.exists(os.path.join(str(home), "audit", "degraded.json"))
    m = ex.write_export(conn, dest, a.format, raw, since, until, now, degraded)
    if a.json:
        print(json.dumps(m, sort_keys=True), file=out)
    else:
        print("export %s written to %s (%d files + manifest.json)" % (m["export"], dest, len(m["files"])), file=out)
    return 0
