"""`pros audit ...` commands (docs/spec/audit/query.md section 4). Thin: every figure comes from query.Query; text is rendered from the same envelope as --json."""
import argparse
import datetime
import json
import os
import sys

from . import query as Q

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
    p = argparse.ArgumentParser(prog="pros audit", description="read the local audit")
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
    return p


def main(argv, home, require_user, now=None, out=None, err=None):
    """Run one command. `require_user` is pros' own check: it raises (or exits) when a professional session calls."""
    out, err = out or sys.stdout, err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        require_user("read the audit")
    except SystemExit:
        raise
    q = Q.Query(store_path(home), owner_check=lambda: require_user("read the audit") or True, now=now)
    if args.sub == "import":
        try:
            from . import importer
        except ImportError:
            print("pros: the audit importer is not part of this build", file=err)
            return 2
        return importer.run_once(home) or 0
    if args.sub == "serve":
        from . import http as H
        return H.serve_cli(q, home, args.port, args.rotate, out)
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


def _clean(args, keys):
    return {k: getattr(args, k) for k in keys if getattr(args, k, None) is not None}
